import { beforeEach, describe, expect, it, vi } from "vitest";

const broker = vi.hoisted(() => ({ exchange: vi.fn() }));

vi.mock("../src/token-broker", () => ({
  TokenBroker: class {
    exchange = broker.exchange;
  },
}));

import type { WorkerEnv } from "../src/env";
import worker from "../src/worker";

const env = {} as WorkerEnv;

function request(body = { oidc_token: "signed-jwt" }, headers: HeadersInit = {}): Request {
  const encoded = JSON.stringify(body);
  return new Request("https://github.reviewsensei.dev/github/token", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "content-length": String(new TextEncoder().encode(encoded).byteLength),
      ...headers,
    },
    body: encoded,
  });
}

beforeEach(() => {
  broker.exchange.mockReset();
  broker.exchange.mockResolvedValue({ token: "ghs_scoped_token", capability: "review_publish" });
});

describe("token route response security", () => {
  it("returns a successful exchange with no-store and no CORS exposure", async () => {
    const result = await worker.fetch(request(), env);
    expect(result.status).toBe(200);
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(result.headers.has("access-control-allow-origin")).toBe(false);
    expect(await result.json()).toEqual({ token: "ghs_scoped_token", capability: "review_publish" });
  });

  it.each([
    [{ origin: "https://attacker.invalid" }],
    [{ "access-control-request-method": "POST" }],
  ])("rejects browser-originated requests without reflecting CORS", async (headers) => {
    const result = await worker.fetch(request(undefined, headers), env);
    expect(result.status).toBe(400);
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(result.headers.has("access-control-allow-origin")).toBe(false);
    expect(await result.json()).toEqual({ error: "cors_not_supported" });
    expect(broker.exchange).not.toHaveBeenCalled();
  });

  it("redacts broker failure details and assertion/token-like values", async () => {
    broker.exchange.mockRejectedValue(
      new Error("github rejected ghs_sensitive and signed-jwt with repository metadata"),
    );
    const result = await worker.fetch(request(), env);
    const body = await result.text();
    expect(result.status).toBe(403);
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(body).toBe('{"error":"capability_not_issued"}');
    expect(body).not.toContain("ghs_sensitive");
    expect(body).not.toContain("signed-jwt");
  });

  it.each([
    ["broker_rate_limited", 429],
    ["broker_ledger_unavailable", 503],
  ])("maps %s without exposing it", async (message, status) => {
    broker.exchange.mockRejectedValue(new Error(message));
    const result = await worker.fetch(request(), env);
    expect(result.status).toBe(status);
    expect(await result.json()).toEqual({ error: "capability_not_issued" });
  });

  it("applies no-store to method and malformed-body failures", async () => {
    const method = await worker.fetch(
      new Request("https://github.reviewsensei.dev/github/token", { method: "GET" }),
      env,
    );
    expect(method.status).toBe(405);
    expect(method.headers.get("cache-control")).toBe("no-store");

    const malformed = await worker.fetch(
      new Request("https://github.reviewsensei.dev/github/token", {
        method: "POST",
        headers: { "content-length": "8" },
        body: "not-json",
      }),
      env,
    );
    expect(malformed.status).toBe(400);
    expect(malformed.headers.get("cache-control")).toBe("no-store");

    const missingLength = await worker.fetch(
      new Request("https://github.reviewsensei.dev/github/token", {
        method: "POST",
        body: "{}",
      }),
      env,
    );
    expect(missingLength.status).toBe(411);
    expect(missingLength.headers.get("cache-control")).toBe("no-store");
  });
});
