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

function ledger(scheduleStatus = 200) {
  const actions: string[] = [];
  const bodies: unknown[] = [];
  const namespace = {
    idFromName: () => "ledger",
    get: () => ({
      fetch: async (input: Request | string, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.url;
        const action = new URL(url).pathname.replace(/^\//, "");
        actions.push(action);
        const raw = typeof input === "string" ? init?.body : input instanceof Request ? await input.text() : undefined;
        if (typeof raw === "string" && raw.length > 0) {
          bodies.push(JSON.parse(raw) as unknown);
        }
        if (action === "schedule" && scheduleStatus !== 200) {
          return new Response(JSON.stringify({ error: "delivery_ledger_unavailable" }), {
            status: scheduleStatus,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ state: action === "claim" ? "claimed" : "scheduled" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    }),
  };
  return { actions, bodies, namespace };
}

async function webhook(repositories: string[], scheduleStatus = 200) {
  const bodyText = JSON.stringify(installationPayload(repositories));
  const body = new TextEncoder().encode(bodyText);
  const book = ledger(scheduleStatus);
  const env = {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused",
    GITHUB_APP_WEBHOOK_SECRET: SECRET,
    PUBLIC_WORKFLOW_TAG: "v5",
    DELIVERY_LEDGER: book.namespace,
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
  );
  return { response, actions: book.actions, bodies: book.bodies };
}

beforeEach(() => {
  processDelivery.mockClear();
  processDelivery.mockResolvedValue([]);
});

describe("multi-repository installation webhooks", () => {
  it("claims and schedules before accepting a multi-repository install", async () => {
    const { response, actions, bodies } = await webhook(["acme/one", "acme/two"]);
    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ accepted: true });
    expect(processDelivery).not.toHaveBeenCalled();
    expect(actions).toEqual(["claim", "schedule"]);
    expect(bodies[1]).toMatchObject({
      continuation: {
        installationId: 2468,
        repositories: ["acme/one", "acme/two"],
        unresolved: false,
        attempt: 0,
        failed: false,
      },
    });
  });

  it("returns 503 and releases the claim when scheduling fails", async () => {
    const { response, actions } = await webhook(["acme/one", "acme/two"], 503);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: "setup_unavailable",
      error_code: "setup_failed",
    });
    expect(processDelivery).not.toHaveBeenCalled();
    expect(actions).toEqual(["claim", "schedule", "release"]);
  });

  it("returns 409 and releases the claim when scheduling conflicts", async () => {
    const { response, actions } = await webhook(["acme/one", "acme/two"], 409);
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({
      error: "delivery_conflict",
      error_code: "delivery_conflict",
    });
    expect(processDelivery).not.toHaveBeenCalled();
    expect(actions).toEqual(["claim", "schedule", "release"]);
  });

  it("keeps a single selected repository on the webhook invocation", async () => {
    const { response, actions } = await webhook(["acme/one"]);
    expect(response.status).toBe(202);
    expect(processDelivery).toHaveBeenCalledOnce();
    expect(actions).toEqual(["claim", "complete"]);
  });

  it("loads an omitted repository list from the scheduled continuation", async () => {
    const { response, actions, bodies } = await webhook([]);
    expect(response.status).toBe(202);
    expect(processDelivery).not.toHaveBeenCalled();
    expect(bodies[1]).toMatchObject({
      continuation: {
        repositories: [],
        unresolved: true,
      },
    });
    expect(actions).toEqual(["claim", "schedule"]);
  });
});
