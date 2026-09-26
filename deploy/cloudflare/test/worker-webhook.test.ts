import { beforeEach, describe, expect, it, vi } from "vitest";

const processDelivery = vi.hoisted(() => vi.fn(async () => []));

vi.mock("../src/github-app", async () => {
  const actual = await vi.importActual<typeof import("../src/github-app")>("../src/github-app");
  return {
    ...actual,
    processDelivery,
  };
});

import type { WorkerEnv } from "../src/env";
import worker from "../src/worker";

const SECRET = "test-webhook-secret";

function installationPayload(repositories: string[]) {
  return {
    action: "created",
    installation: {
      id: 2468,
      suspended_at: null,
      permissions: {
        contents: "write",
        pull_requests: "write",
        actions_variables: "write",
        workflows: "write",
      },
    },
    repositories: repositories.map((full_name) => ({ full_name })),
  };
}

async function signature(body: Uint8Array): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, body);
  const hex = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `sha256=${hex}`;
}

function ledger() {
  const actions: string[] = [];
  const namespace = {
    idFromName: () => "ledger",
    get: () => ({
      fetch: async (input: Request | string) => {
        const url = typeof input === "string" ? input : input.url;
        const action = new URL(url).pathname.replace(/^\//, "");
        actions.push(action);
        return new Response(JSON.stringify({ state: action === "claim" ? "claimed" : "accepted" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    }),
  };
  return { actions, namespace };
}

async function webhook(
  repositories: string[],
  run = vi.fn(async () => undefined),
  ctx?: ExecutionContext,
) {
  const bodyText = JSON.stringify(installationPayload(repositories));
  const body = new TextEncoder().encode(bodyText);
  const book = ledger();
  const env = {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused",
    GITHUB_APP_WEBHOOK_SECRET: SECRET,
    PUBLIC_WORKFLOW_TAG: "v5",
    DELIVERY_LEDGER: book.namespace,
    SETUP_CONTINUATION: { run },
  } as unknown as WorkerEnv;
  const response = await worker.fetch(
    new Request("https://github.reviewsensei.dev/github/webhook", {
      method: "POST",
      headers: {
        "content-length": String(body.byteLength),
        "content-type": "application/json",
        "x-github-event": "installation",
        "x-github-delivery": "delivery-1",
        "x-hub-signature-256": await signature(body),
      },
      body,
    }),
    env,
    ctx,
  );
  return { response, run, actions: book.actions };
}

beforeEach(() => {
  processDelivery.mockClear();
  processDelivery.mockResolvedValue([]);
});

describe("multi-repository installation webhooks", () => {
  it("schedules one continuation for every selected repository and returns 202", async () => {
    const { response, run, actions } = await webhook(["acme/one", "acme/two"]);
    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ accepted: true });
    expect(processDelivery).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledOnce();
    expect(run.mock.calls[0]?.[0]).toMatchObject({
      installationId: 2468,
      repositories: ["acme/one", "acme/two"],
      unresolved: false,
      attempt: 0,
      failed: false,
    });
    expect(actions).toEqual(["claim"]);
  });

  it("keeps a single selected repository on the webhook invocation", async () => {
    const { response, run, actions } = await webhook(["acme/one"]);
    expect(response.status).toBe(202);
    expect(run).not.toHaveBeenCalled();
    expect(processDelivery).toHaveBeenCalledOnce();
    expect(actions).toEqual(["claim", "complete"]);
  });

  it("acknowledges the webhook before the continuation settles", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const run = vi.fn(() => gate);
    const waits: Promise<unknown>[] = [];
    const ctx = {
      waitUntil(promise: Promise<unknown>) {
        waits.push(promise);
      },
      passThroughOnException() {
        return undefined;
      },
    } as ExecutionContext;
    const { response, actions } = await webhook(["acme/one", "acme/two"], run, ctx);
    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ accepted: true });
    expect(waits).toHaveLength(1);
    expect(actions).toEqual(["claim"]);
    release?.();
    await waits[0];
  });

  it("loads an omitted repository list in the continuation", async () => {
    const { response, run, actions } = await webhook([]);
    expect(response.status).toBe(202);
    expect(processDelivery).not.toHaveBeenCalled();
    expect(run.mock.calls[0]?.[0]).toMatchObject({
      repositories: [],
      unresolved: true,
    });
    expect(actions).toEqual(["claim"]);
  });
});
