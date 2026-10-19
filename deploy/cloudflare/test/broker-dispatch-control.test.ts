import { afterEach, describe, expect, it, vi } from "vitest";
import { consumingBroker, SHA } from "./epic238-consuming-broker-fixture";
import { resetOidcJwksCacheForTesting } from "../src/oidc";
import { BrokerDispatchControl } from "../src/broker-dispatch-control";

const control = (maximum = 20) => ({
  schema_version: "1.0", operation_id: "c".repeat(64), request_id: "d".repeat(64),
  deadline_unix_ms: Date.now() + 60_000, max_dispatches: maximum,
});

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); resetOidcJwksCacheForTesting(); });

describe("opt-in broker physical dispatch control", () => {
  it("refuses every redirect status without spending another dispatch", async () => {
    for (const status of [300, 301, 302, 303, 304, 307, 308, 399]) {
      const budget = new BrokerDispatchControl(control(1));
      budget.beforeDispatch();
      const response = new Response(null, { status });
      await expect(budget.checkResponse(response)).rejects.toThrow("broker_control_redirect_refused");
      expect(budget.receipt().dispatched).toBe(1);
    }
  });
  it("counts actual production OIDC/GitHub dispatches and returns the original scoped receipt", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    const bound = { ...control(), operation_id: request.mutation.operation_id, deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms };
    vi.stubGlobal("fetch", fixture.transport);
    const result = await fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: request, control_budget: bound,
    });
    expect(result.control_receipt).toEqual({ ...bound, dispatched: fixture.transportRequests.length });
    expect(result.control_receipt!.dispatched).toBe(13);
    expect(result.session_grant).toMatch(/^sg1\./);
  });

  it("refuses the next subrequest before exceeding its funded finite liability", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    vi.stubGlobal("fetch", fixture.transport);
    await expect(fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: request, control_budget: { ...control(1), operation_id: request.mutation.operation_id, deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms },
    })).rejects.toThrow("broker_control_budget_exhausted");
    expect(fixture.transportRequests).toHaveLength(1);
  });

  it("does not renew an absolute deadline while waiting for a response", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    const bound = { ...control(), operation_id: request.mutation.operation_id, deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms };
    const origin = Date.now();
    let clock = origin;
    vi.spyOn(Date, "now").mockImplementation(() => clock);
    vi.stubGlobal("fetch", async (input: string | URL | Request, init?: RequestInit) => {
      const result = await fixture.transport(input, init);
      clock = bound.deadline_unix_ms;
      return result;
    });
    await expect(fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: request, control_budget: bound,
    })).rejects.toThrow("broker_control_deadline_exhausted");
    expect(fixture.transportRequests).toHaveLength(1);
  });

  it("verifies the genuine signed grant under the same original operation/deadline", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    vi.stubGlobal("fetch", fixture.transport);
    const issued = await fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
    });
    const budget = new BrokerDispatchControl({ ...control(1), operation_id: request.mutation.operation_id, deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms });
    const before = fixture.transportRequests.length;
    expect(await fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation, budget)).toEqual(issued.session_attestation);
    expect(budget.receipt().dispatched).toBe(1);
    expect(fixture.transportRequests.length - before).toBe(1);
  });

  it("binds control operation/deadline to the exact signed mutation before network", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    vi.stubGlobal("fetch", fixture.transport);
    for (const change of [{ operation_id: "f".repeat(64) }, { deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms - 1 }]) {
      await expect(fixture.broker.exchange({
        oidc_token: await fixture.oidcToken(), capability: "review_session",
        session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
        control_budget: { ...control(), operation_id: request.mutation.operation_id, deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms, ...change },
      })).rejects.toThrow("broker_control_binding_invalid");
    }
    expect(fixture.transportRequests).toHaveLength(0);
  });

  it("keeps concurrent request budgets separate while sharing only the existing JWKS cache", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    vi.stubGlobal("fetch", fixture.transport);
    const make = async (maximum: number) => fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
      control_budget: { ...control(maximum), operation_id: request.mutation.operation_id, deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms },
    });
    const [narrow, wide] = await Promise.allSettled([make(1), make(20)]);
    expect(narrow.status).toBe("rejected");
    expect(wide.status).toBe("fulfilled");
    if (wide.status !== "fulfilled") throw new Error("wide control failed");
    expect(fixture.transportRequests.length).toBe(wide.value.control_receipt!.dispatched + 1);
  });

  it("cancels a waiting physical fetch at the original absolute deadline", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    request.mutation.read_accounting.deadline_unix_ms = Date.now() + 30;
    let aborted = false;
    vi.stubGlobal("fetch", (_input: unknown, init?: RequestInit) => new Promise((_resolve, reject) => {
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      init!.signal!.addEventListener("abort", () => { aborted = true; reject(init!.signal!.reason); });
    }));
    await expect(fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
      control_budget: { ...control(), operation_id: request.mutation.operation_id, deadline_unix_ms: request.mutation.read_accounting.deadline_unix_ms },
    })).rejects.toThrow();
    expect(aborted).toBe(true);
  });

  it("bounds a wait on another request's existing JWKS fetch without canceling its owner", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    let release: (() => void) | undefined;
    vi.stubGlobal("fetch", (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes(".well-known/jwks")) return new Promise<Response>(resolve => {
        release = () => { fixture.transport(input, init).then(resolve); };
      });
      return fixture.transport(input, init);
    });
    const body = {
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
    };
    const original = fixture.broker.exchange(body);
    const bound = { ...control(), operation_id: request.mutation.operation_id, deadline_unix_ms: Date.now() + 30 };
    const selected = structuredClone(request);
    selected.mutation.read_accounting.deadline_unix_ms = bound.deadline_unix_ms;
    try {
      await expect(fixture.broker.exchange({ ...body, oidc_token: await fixture.oidcToken(), session_attestation: selected, control_budget: bound })).rejects.toThrow("broker_control_deadline_exhausted");
      expect(fixture.transportRequests).toHaveLength(0);
    } finally {
      release!();
      await original;
    }
  });

  it("refuses wall-clock regression without another internal dispatch", () => {
    let now = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const budget = new BrokerDispatchControl(control());
    budget.beforeDispatch();
    now -= 1;
    expect(() => budget.beforeDispatch()).toThrow("broker_control_clock_regressed");
  });

  it("requires closed finite integer control fields before any dispatch", () => {
    for (const change of [
      { extra: true }, { schema_version: "2.0" }, { operation_id: "bad" },
      { max_dispatches: true }, { max_dispatches: 0 }, { max_dispatches: 61 },
      { max_dispatches: 1.5 }, { deadline_unix_ms: Date.now() - 1 },
      { deadline_unix_ms: Date.now() + 61_000 },
    ]) expect(() => new BrokerDispatchControl({ ...control(), ...change })).toThrow();
  });
});
