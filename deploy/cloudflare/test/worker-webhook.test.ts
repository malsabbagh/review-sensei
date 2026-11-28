import { createHash } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";

const processDelivery = vi.hoisted(() => vi.fn(async () => []));

vi.mock("../src/github-app", async () => {
  const actual = await vi.importActual<typeof import("../src/github-app")>("../src/github-app");
  return {
    ...actual,
    processDelivery,
  };
});

import type { WorkerEnv, WorkerExecution } from "../src/env";
import { verifySetupCursor } from "../src/signed-mac";
import worker from "../src/worker";

const SECRET = "test-webhook-secret";
const SIGNING_KEY = "test-signing-key";

function installationPayload(repositories: string[], verified = false) {
  return {
    action: "created",
    installation: {
      id: 2468,
      suspended_at: null,
      ...(verified ? { created_at: "2026-11-28T12:00:00Z" } : {}),
      permissions: {
        contents: "write",
        pull_requests: "write",
        actions_variables: "write",
        workflows: "write",
      },
    },
    repositories: repositories.map((full_name, index) => ({ full_name, ...(verified ? { id: 100 + index } : {}) })),
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

async function webhook(repositories: string[], options: { signingKey?: string; self?: boolean; onboarding?: boolean; cutoff?: string; wrongBodyDigest?: boolean } = {}) {
  const bodyText = JSON.stringify(installationPayload(repositories, options.onboarding));
  const body = new TextEncoder().encode(bodyText);
  const calls: string[] = [];
  const self = {
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      calls.push(url);
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    }),
  };
  const tasks: Promise<unknown>[] = [];
  const ctx: WorkerExecution = { waitUntil: (promise) => { tasks.push(promise); } };
  const env = {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused",
    GITHUB_APP_WEBHOOK_SECRET: SECRET,
    PUBLIC_WORKFLOW_TAG: "v5",
    ...(options.onboarding ? { V6_NEW_INSTALL_ONBOARDING: JSON.stringify({
      app_id: 12345,
      setup_enabled: true,
      start_at: options.cutoff ?? "2026-11-28T11:00:00Z",
      tag_object: "e".repeat(40),
      source_sha: "f".repeat(40),
      enrollments: repositories.map((repository, index) => ({
        installation_id: 2468,
        repository_id: 100 + index,
        repository,
        delivery_id: "delivery-1",
        body_sha256: options.wrongBodyDigest ? "0".repeat(64) : createHash("sha256").update(body).digest("hex"),
      })),
    }) } : {}),
    REVIEWSENSEI_SIGNING_KEY: options.signingKey === undefined ? SIGNING_KEY : options.signingKey,
    SELF: options.self === false ? undefined : self,
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
  await Promise.all(tasks);
  return { response, calls, self, init: self.fetch.mock.calls[0]?.[1] };
}

beforeEach(() => {
  processDelivery.mockClear();
  processDelivery.mockResolvedValue([]);
});

describe("stateless installation webhooks", () => {
  it("schedules a signed continuation for a multi-repository install", async () => {
    const { response, calls, init } = await webhook(["acme/one", "acme/two"]);
    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ accepted: true });
    expect(processDelivery).not.toHaveBeenCalled();
    expect(calls).toEqual(["https://setup.internal/github/setup-continue"]);
    expect(calls[0]).not.toContain("github.reviewsensei.dev");
    const cursor = JSON.parse(String(init?.body)).cursor as string;
    const claims = await verifySetupCursor(SIGNING_KEY, cursor);
    expect(claims).toMatchObject({
      installationId: 2468,
      deliveryId: "delivery-1",
      page: 1,
      offset: 0,
      listingSha256: "",
      failureCount: 0,
    });
  });

  it("returns configuration_unavailable when the signing key is missing", async () => {
    const { response } = await webhook(["acme/one", "acme/two"], { signingKey: "" });
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "configuration_unavailable" });
    expect(processDelivery).not.toHaveBeenCalled();
  });

  it("keeps a single selected repository on the webhook invocation", async () => {
    const { response, calls } = await webhook(["acme/one"]);
    expect(response.status).toBe(202);
    expect(processDelivery).toHaveBeenCalledOnce();
    expect(calls).toEqual([]);
  });

  it("continues an installation whose repository list was omitted", async () => {
    const { response, calls, init } = await webhook([]);
    expect(response.status).toBe(202);
    expect(processDelivery).not.toHaveBeenCalled();
    expect(calls).toEqual(["https://setup.internal/github/setup-continue"]);
    const cursor = JSON.parse(String(init?.body)).cursor as string;
    expect(await verifySetupCursor(SIGNING_KEY, cursor)).toMatchObject({
      installationId: 2468,
      page: 1,
      offset: 0,
    });
  });
});

it("binds a verified first-time setup delivery to its continuation", async () => {
  const repositories = ["acme/one", "acme/two"];
  const { response, init } = await webhook(repositories, { onboarding: true });
  expect(response.status).toBe(202);
  const cursor = JSON.parse(String(init?.body)).cursor as string;
  const digest = createHash("sha256").update(JSON.stringify(installationPayload(repositories, true))).digest("hex");
  const rows = repositories.map((repository, index) => ({
    installationId: 2468, repositoryId: 100 + index, repository, deliveryId: "delivery-1", bodySha256: digest,
  }));
  const proof = createHash("sha256").update(JSON.stringify([
    Date.parse("2026-11-28T11:00:00Z"), "e".repeat(40), "f".repeat(40), rows,
  ])).digest("hex");
  expect(await verifySetupCursor(SIGNING_KEY, cursor)).toMatchObject({ onboardingProof: proof });
});

it.each([
  { cutoff: "2026-11-28T12:00:01Z" },
  { wrongBodyDigest: true },
])("does not mark old or changed delivery evidence as first-time (%j)", async (options) => {
  const { response, init } = await webhook(["acme/one", "acme/two"], { onboarding: true, ...options });
  expect(response.status).toBe(202);
  const cursor = JSON.parse(String(init?.body)).cursor as string;
  expect(await verifySetupCursor(SIGNING_KEY, cursor)).not.toHaveProperty("onboardingProof");
});
