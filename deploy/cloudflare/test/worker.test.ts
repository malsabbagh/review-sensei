import { beforeEach, describe, expect, it, vi } from "vitest";

const broker = vi.hoisted(() => ({ exchange: vi.fn(), verifySessionGrant: vi.fn() }));

vi.mock("../src/token-broker", () => ({
  TokenBroker: class {
    exchange = broker.exchange;
    verifySessionGrant = broker.verifySessionGrant;
  },
}));

import type { WorkerEnv } from "../src/env";
import worker, { brokerErrorCode, setupErrorCode } from "../src/worker";
import { brokerDiagnostic } from "../src/broker-diagnostics";
import { WorkflowLookupError, type LookupDiagnostic } from "../src/broker-lookup-diagnostic";

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

function grantRequest(
  body = { session_grant: "a".repeat(43), session_attestation: { version: 1 } },
  headers: HeadersInit = {},
  method = "POST",
  includeContentLength = true,
): Request {
  const encoded = JSON.stringify(body);
  const requestHeaders = new Headers(headers);
  if (includeContentLength && !requestHeaders.has("content-length")) {
    requestHeaders.set(
      "content-length",
      String(new TextEncoder().encode(encoded).byteLength),
    );
  }
  return new Request("https://github.reviewsensei.dev/github/session-grant", {
    method,
    headers: requestHeaders,
    body: method === "GET" || method === "HEAD" ? undefined : encoded,
  });
}

beforeEach(() => {
  broker.exchange.mockReset();
  broker.verifySessionGrant.mockReset();
  broker.exchange.mockResolvedValue({ token: "ghs_scoped_token", capability: "review_publish" });
  broker.verifySessionGrant.mockResolvedValue({ repository_id: 987654321, pull_request: 7 });
});

describe("session-grant verification route", () => {
  it("returns only verified immutable session fields and never echoes the grant", async () => {
    const result = await worker.fetch(grantRequest(), env);
    expect(result.status).toBe(200);
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(await result.json()).toEqual({
      session_attestation: { repository_id: 987654321, pull_request: 7 },
    });
    expect(broker.verifySessionGrant).toHaveBeenCalledWith(
      "a".repeat(43), { version: 1 }, undefined, undefined,
    );
  });

  it("rejects expired or invalid grants without exposing their values", async () => {
    broker.verifySessionGrant.mockRejectedValue(new Error("broker_session_grant_invalid"));
    const result = await worker.fetch(grantRequest(), env);
    expect(result.status).toBe(403);
    expect(await result.json()).toMatchObject({ error: "session_grant_not_verified" });
  });

  it.each([
    [{ origin: "https://attacker.invalid" }, 400, "cors_not_supported", true],
    [{}, 411, "content_length_required", false],
    [{ "content-length": "not-a-number" }, 413, "payload_too_large", true],
    [{ "content-length": "262145" }, 413, "payload_too_large", true],
  ])("rejects malformed session-grant transport metadata", async (headers, status, error, includeContentLength) => {
    const result = await worker.fetch(
      grantRequest(undefined, headers, "POST", includeContentLength),
      env,
    );
    expect(result.status).toBe(status);
    expect(await result.json()).toEqual({ error, diagnostic: brokerDiagnostic(new Error(`broker_${error}`)) });
    expect(broker.verifySessionGrant).not.toHaveBeenCalled();
  });

  it("rejects malformed session-grant JSON before broker verification", async () => {
    const result = await worker.fetch(
      new Request("https://github.reviewsensei.dev/github/session-grant", {
        method: "POST",
        headers: { "content-length": "8" },
        body: "not-json",
      }),
      env,
    );
    expect(result.status).toBe(400);
    expect(await result.json()).toEqual({ error: "invalid_request", diagnostic: brokerDiagnostic(new Error("broker_invalid_request")) });
    expect(broker.verifySessionGrant).not.toHaveBeenCalled();
  });

  it("rejects unknown session-grant request fields", async () => {
    const result = await worker.fetch(
      grantRequest({
        session_grant: "a".repeat(43),
        session_attestation: { version: 1 },
        unexpected: true,
      }),
      env,
    );
    expect(result.status).toBe(400);
    expect(await result.json()).toEqual({ error: "invalid_request", diagnostic: brokerDiagnostic(new Error("broker_invalid_request")) });
    expect(broker.verifySessionGrant).not.toHaveBeenCalled();
  });

  it("maps a missing signing key to configuration_unavailable", async () => {
    broker.verifySessionGrant.mockRejectedValue(new Error("configuration_unavailable"));
    const result = await worker.fetch(grantRequest(), env);
    expect(result.status).toBe(503);
    expect(await result.json()).toMatchObject({ error: "configuration_unavailable" });
  });

  it("rejects non-POST session-grant requests", async () => {
    const result = await worker.fetch(grantRequest(undefined, {}, "GET"), env);
    expect(result.status).toBe(405);
    expect(await result.json()).toEqual({ error: "method_not_allowed" });
    expect(broker.verifySessionGrant).not.toHaveBeenCalled();
  });
});

describe("token route response security", () => {
  it("forwards only a validated downward deadline without an authority grant", async () => {
    const deadline=Date.now()+30;
    await worker.fetch(request(undefined,{"x-reviewsensei-deadline-unix-ms":String(deadline)}),env);
    expect(broker.exchange).toHaveBeenCalledWith({oidc_token:"signed-jwt"},undefined,deadline);
    broker.exchange.mockClear();
    const rejected=await worker.fetch(request(undefined,{"x-reviewsensei-deadline-unix-ms":"secret untrusted deadline"}),env);
    expect(rejected.status).toBe(403);
    expect(broker.exchange).not.toHaveBeenCalled();
    expect(JSON.stringify(await rejected.json())).not.toContain("secret");
  });

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
    expect(await result.json()).toEqual({ error: "cors_not_supported", diagnostic: brokerDiagnostic(new Error("broker_cors_not_supported")) });
    expect(broker.exchange).not.toHaveBeenCalled();
  });

  it("redacts broker failure details and assertion/token-like values", async () => {
    broker.exchange.mockRejectedValue(
      new Error("github rejected ghs_sensitive and signed-jwt with repository metadata"),
    );
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const result = await worker.fetch(request(), env);
      const body = await result.text();
      expect(result.status).toBe(403);
      expect(result.headers.get("cache-control")).toBe("no-store");
      expect(JSON.parse(body)).toEqual({ error: "capability_not_issued", diagnostic: brokerDiagnostic(new Error("unknown")) });
      expect(body).not.toContain("ghs_sensitive");
      expect(body).not.toContain("signed-jwt");
      expect(log).toHaveBeenCalledWith("github_broker_failed", {
        error_code: "broker_unknown",
        stage: "broker",
        action: "contact_operator",
        capability: "review_publish",
      });
    } finally {
      log.mockRestore();
    }
  });

  it("logs a stable broker code and bounded request metadata", async () => {
    broker.exchange.mockRejectedValue(new Error("github_capability_issue_failed_422"));
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const result = await worker.fetch(
        request({ oidc_token: "signed-jwt", capability: "learning_write" }, {
          "cf-ray": "0123456789abcdef-YYZ",
        }),
        env,
      );
      expect(result.status).toBe(403);
      expect(await result.json()).toMatchObject({ error: "capability_not_issued" });
      expect(log).toHaveBeenCalledWith("github_broker_failed", {
        error_code: "github_capability_issue_failed",
        stage: "capability_issuance",
        action: "contact_operator",
        upstream_status: 422,
        capability: "learning_write",
        cf_ray: "0123456789abcdef-YYZ",
      });
    } finally {
      log.mockRestore();
    }
  });

  it.each([
    ["configuration_unavailable", 503],
  ])("maps %s without exposing it", async (message, status) => {
    broker.exchange.mockRejectedValue(new Error(message));
    const result = await worker.fetch(request(), env);
    expect(result.status).toBe(status);
    expect(await result.json()).toMatchObject({ error: "configuration_unavailable" });
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

describe("failure diagnostics", () => {
  it.each([
    [new Error("github_capability_issue_failed_422"), "github_capability_issue_failed"],
    [new Error("oidc_audience_invalid"), "oidc_audience_invalid"],
    [new Error("unexpected provider response"), "broker_unknown"],
  ])("normalizes broker diagnostics for %s", (error, expected) => {
    expect(brokerErrorCode(error)).toBe(expected);
  });

  it.each([
    [new Error("github_installation_token_failed_401"), "github_installation_token_failed_401"],
    [new Error("GitHub App installation lacks setup permissions"), "github_installation_permissions_missing"],
    [new Error("GitHub request was rejected"), "github_request_rejected"],
  ])("keeps the diagnostic code stable for %s", (error, expected) => {
    expect(setupErrorCode(error)).toBe(expected);
  });

  it("does not expose arbitrary error text", () => {
    expect(setupErrorCode(new Error("github rejected ghs_sensitive-token"))).toBe(
      "setup_failed",
    );
  });
});

describe("broker diagnostic response contract", () => {
  const lookupFailure = (status: number | undefined, failure: LookupDiagnostic["failure"] = "http", metadata: Partial<LookupDiagnostic> = {}) =>
    new WorkflowLookupError({ phase: "rest_tag", failure, ...metadata }, status);

  describe.each(["token", "session-grant"])("%s lookup classification", route => {
    it.each([
      ["primary exhaustion", lookupFailure(403, "http", { rate_limit_limit: 60, rate_limit_remaining: 0, rate_limit_reset: 1_793_574_563, rate_limit_resource: "core" })],
      ["secondary throttle", lookupFailure(403, "http", { retry_after: 1 })],
      ["429 without timing", lookupFailure(429)],
      ["upstream unavailable", lookupFailure(503)],
      ["upstream timeout", lookupFailure(undefined, "timeout")],
      ["upstream transport", lookupFailure(undefined, "transport")],
      ["failed 502 body read", lookupFailure(502, "timeout")],
      ["successful-status body timeout", lookupFailure(200, "timeout")],
      ["successful-status body transport", lookupFailure(200, "transport")],
    ])("reports %s as temporary, retaining facts without authority", async (_label, error) => {
      const exchange = route === "token" ? broker.exchange : broker.verifySessionGrant;
      exchange.mockRejectedValueOnce(error);
      const headers = { "cf-ray": "0123456789abcdef-YYZ" };
      const result = await worker.fetch(route === "token" ? request(undefined, headers) : grantRequest(undefined, headers), env);
      expect(result.status).toBe(503);
      expect(result.headers.get("cache-control")).toBe("no-store");
      expect(await result.json()).toEqual({
        error: route === "token" ? "capability_not_issued" : "session_grant_not_verified",
        diagnostic: {
          version: 1, code: "github_workflow_lookup_temporary", stage: "workflow_identity",
          action: "inspect_original_attempt", correlation_id: "0123456789abcdef-YYZ",
          ...(error.status === undefined ? {} : { upstream_status: error.status }),
        },
        lookup: error.lookup,
      });
      expect(exchange).toHaveBeenCalledTimes(1);
    });

    it.each([
      ["unauthorized", lookupFailure(401)],
      ["ordinary forbidden", lookupFailure(403)],
      ["tag absent", lookupFailure(404)],
      ["forbidden body timeout", lookupFailure(403, "timeout", { retry_after: 1 })],
      ["absent body transport", lookupFailure(404, "transport")],
      ["zero retry", lookupFailure(403, "http", { retry_after: 0 })],
      ["negative retry", lookupFailure(403, "http", { retry_after: -1 })],
      ["fractional retry", lookupFailure(403, "http", { retry_after: 0.5 })],
      ["unbounded retry", lookupFailure(403, "http", { retry_after: Infinity })],
      ["string retry", lookupFailure(403, "http", { retry_after: "1" as never })],
      ["boolean remaining", lookupFailure(403, "http", { rate_limit_remaining: false as never })],
      ["malformed throttled body", lookupFailure(429, "malformed", { retry_after: 1 })],
      ["oversized unavailable body", lookupFailure(503, "oversized")],
      ["malformed successful body", lookupFailure(200, "malformed")],
      ["oversized successful body", lookupFailure(200, "oversized")],
      ["untyped upstream code", new Error("github_workflow_tag_unavailable_429")],
      ["untyped temporary code", new Error("github_workflow_lookup_temporary")],
      ...["broker_workflow_rejected", "github_workflow_tag_invalid", "oidc_token_invalid", "broker_control_deadline_exhausted", "broker_control_budget_exhausted", "broker_control_clock_regressed", "broker_control_redirect_refused"].map(code => [code, new Error(code)] as const),
    ])("keeps %s refused without another call", async (_label, error) => {
      const exchange = route === "token" ? broker.exchange : broker.verifySessionGrant;
      exchange.mockRejectedValueOnce(error);
      const result = await worker.fetch(route === "token" ? request() : grantRequest(), env);
      expect(result.status).toBe(403);
      const body = await result.json() as { diagnostic: { code: string }; token?: string; session_attestation?: object };
      expect(body.diagnostic.code).not.toBe("github_workflow_lookup_temporary");
      expect(body).not.toHaveProperty("token");
      expect(body).not.toHaveProperty("session_attestation");
      expect(exchange).toHaveBeenCalledTimes(1);
    });
  });

  it.each(["token", "session-grant"])("reports a temporary workflow lookup failure on %s without issuing authority", async route => {
    const lookup = { phase: "rest_ref", failure: "http", git_failure: "http", retry_after: 1 } as const;
    const error = new WorkflowLookupError(lookup, 429);
    const exchange = route === "token" ? broker.exchange : broker.verifySessionGrant;
    exchange.mockRejectedValueOnce(error);
    const result = await worker.fetch(route === "token" ? request() : grantRequest(), env);
    expect(result.status).toBe(503);
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(await result.json()).toEqual({
      error: route === "token" ? "capability_not_issued" : "session_grant_not_verified",
      diagnostic: {
        version: 1, code: "github_workflow_lookup_temporary", stage: "workflow_identity",
        action: "inspect_original_attempt", upstream_status: 429,
      },
      lookup,
    });
    expect(exchange).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["broker_workflow_rejected", 403],
    ["broker_rate_limited", 403],
    ["broker_ledger_unavailable", 403],
    ["github_capability_issue_failed_500", 403],
    ["broker_ghs_sensitive_token", 403],
  ])("emits safe facts and retains token HTTP classification for %s", async (message, status) => {
    broker.exchange.mockRejectedValue(new Error(message));
    const result = await worker.fetch(request(undefined, { "cf-ray": "0123456789abcdef-YYZ" }), env);
    expect(result.status).toBe(status);
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(await result.json()).toEqual({
      error: "capability_not_issued",
      diagnostic: brokerDiagnostic(new Error(message), "0123456789abcdef-YYZ"),
    });
  });

  it("retains the session-grant rejection classification and includes validated correlation", async () => {
    broker.verifySessionGrant.mockRejectedValue(new Error("broker_session_grant_invalid"));
    const result = await worker.fetch(grantRequest(undefined, { "cf-ray": "0123456789abcdef-YYZ" }), env);
    expect(result.status).toBe(403);
    expect(await result.json()).toEqual({
      error: "session_grant_not_verified",
      diagnostic: brokerDiagnostic(new Error("broker_session_grant_invalid"), "0123456789abcdef-YYZ"),
    });
  });
});


describe("bounded broker operation telemetry", () => {
  it("emits one safe success or refusal observation without request or token content", async () => {
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const secret = "PRIVATE-TOKEN-AND-REPOSITORY-CONTENT";
      broker.exchange.mockResolvedValueOnce({ token: secret, capability: "issue_reply" });
      const accepted = await worker.fetch(request({ oidc_token: secret }, { "cf-ray": "a4dd3d310f874964-LAX" }), env);
      expect(accepted.status).toBe(200);
      broker.exchange.mockRejectedValueOnce(new Error(secret));
      const refused = await worker.fetch(request({ oidc_token: secret }), env);
      expect(refused.status).toBe(403);
      const events = log.mock.calls.filter(call => call[0] === "github_broker_observation");
      expect(events).toHaveLength(2);
      expect(events[0][1]).toMatchObject({ version: 1, operation: "exchange", outcome: "success", retries: 0, correlation_id: "a4dd3d310f874964-LAX" });
      expect(events[1][1]).toMatchObject({ outcome: "refused", code: "broker_unknown", retries: 0 });
      expect(JSON.stringify(log.mock.calls)).not.toContain(secret);
      expect(JSON.stringify(errorLog.mock.calls)).not.toContain(secret);
      expect(broker.exchange).toHaveBeenCalledTimes(2);
    } finally { log.mockRestore(); errorLog.mockRestore(); }
  });

  it("bounds stalled request body streams by deadline and emits refusal observation", async () => {
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    try {
      const stalledStream = new ReadableStream<Uint8Array>({
        start() {
          // Never enqueues or closes; simulates a stalled client stream.
        },
      });
      const expiredDeadline = Date.now() + 15;
      const stalledRequest = new Request("https://github.reviewsensei.dev/github/token", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": "32",
          "x-reviewsensei-deadline-unix-ms": String(expiredDeadline),
          "cf-ray": "a4dd3d310f874964-LAX",
        },
        body: stalledStream,
        duplex: "half",
      } as RequestInit);
      const response = await worker.fetch(stalledRequest, env);
      expect(response.status).toBe(403);
      const payload = await response.json();
      expect(payload).toMatchObject({
        error: "capability_not_issued",
        diagnostic: {
          code: "broker_request_deadline_exhausted",
        },
      });
      const events = log.mock.calls.filter(call => call[0] === "github_broker_observation");
      expect(events).toHaveLength(1);
      expect(events[0][1]).toMatchObject({
        operation: "exchange",
        outcome: "refused",
        code: "broker_request_deadline_exhausted",
        correlation_id: "a4dd3d310f874964-LAX",
      });
    } finally {
      log.mockRestore();
    }
  });

  it("does not hang on never-settling stream cancellation across both Worker handlers", async () => {
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    try {
      for (const [url, expectedOp] of [
        ["https://github.reviewsensei.dev/github/token", "exchange"],
        ["https://github.reviewsensei.dev/github/session-grant", "session_grant"],
      ] as const) {
        const neverCancellingStream = new ReadableStream<Uint8Array>({
          start() {
            // Stalled stream
          },
          cancel() {
            return new Promise<void>(() => {
              // Never settles
            });
          },
        });
        const deadline = Date.now() + 15;
        const stalledReq = new Request(url, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "content-length": "32",
            "x-reviewsensei-deadline-unix-ms": String(deadline),
          },
          body: neverCancellingStream,
          duplex: "half",
        } as RequestInit);
        const response = await Promise.race([
          worker.fetch(stalledReq, env),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error("handler_hung_on_cancel")), 150),
          ),
        ]);
        expect(response.status).toBe(403);
        const body = await response.json();
        expect(body).toMatchObject({
          diagnostic: { code: "broker_request_deadline_exhausted" },
        });
        const opEvents = log.mock.calls.filter(
          call =>
            call[0] === "github_broker_observation" &&
            (call[1] as { operation?: string }).operation === expectedOp,
        );
        expect(opEvents).toHaveLength(1);
      }
    } finally {
      log.mockRestore();
    }
  });
});
