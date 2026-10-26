import { afterEach, describe, expect, it, vi } from "vitest";
import { consumingBroker, SHA } from "./epic238-consuming-broker-fixture";
import { resetOidcJwksCacheForTesting } from "../src/oidc";
import { BrokerDispatchControl } from "../src/broker-dispatch-control";

const control = (maximum = 20) => ({
  schema_version: "1.0", operation_id: "c".repeat(64), request_id: "d".repeat(64),
  deadline_unix_ms: Date.now() + 60_000, max_dispatches: maximum,
});

afterEach(() => {
  if (vi.isFakeTimers()) vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetOidcJwksCacheForTesting();
});

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
    const bound = control();
    vi.stubGlobal("fetch", fixture.transport);
    const result = await fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: request, control_budget: bound,
    });
    expect(result.control_receipt).toEqual({ ...bound, dispatched: fixture.transportRequests.length });
    expect(result.control_receipt!.dispatched).toBe(11);
    expect(result.session_grant).toMatch(/^sg1\./);
  });

  it("refuses the next subrequest before exceeding its funded finite liability", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    vi.stubGlobal("fetch", fixture.transport);
    await expect(fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: request, control_budget: control(1),
    })).rejects.toThrow("broker_control_budget_exhausted");
    expect(fixture.transportRequests).toHaveLength(1);
  });

  it("does not renew an absolute deadline while waiting for a response", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    const bound = control();
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
    const budget = new BrokerDispatchControl(control(1));
    const before = fixture.transportRequests.length;
    expect(await fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation, budget)).toEqual(issued.session_attestation);
    expect(budget.receipt().dispatched).toBe(1);
    expect(fixture.transportRequests.length - before).toBe(1);
  });

  it("keeps downward service controls separate from signed caller authority", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    vi.stubGlobal("fetch", fixture.transport);
    for (const change of [{ operation_id: "f".repeat(64) }, { deadline_unix_ms: Date.now() + 20_000 }]) {
      const bound = { ...control(), ...change };
      const result = await fixture.broker.exchange({
        oidc_token: await fixture.oidcToken(), capability: "review_session",
        session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
        control_budget: bound,
      });
      expect(result.control_receipt).toMatchObject(bound);
      expect(result.session_attestation).not.toHaveProperty("mutation");
      expect(result.session_attestation).not.toHaveProperty("read_accounting");
    }
  });

  it("keeps concurrent request budgets separate while sharing only the existing JWKS cache", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    vi.stubGlobal("fetch", fixture.transport);
    const make = async (maximum: number) => fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
      control_budget: control(maximum),
    });
    const [narrow, wide] = await Promise.allSettled([make(1), make(20)]);
    expect(narrow.status).toBe("rejected");
    expect(wide.status).toBe("fulfilled");
    if (wide.status !== "fulfilled") throw new Error("wide control failed");
    expect(fixture.transportRequests.length).toBe(wide.value.control_receipt!.dispatched + 1);
  });

  it("refuses an exchange whose original deadline expired during fixture setup before fetch", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    let clock = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => clock);
    const deadline = clock + 30;
    const oidcToken = await fixture.oidcToken();
    // Signing/setup can consume the entire window; this is a refusal oracle,
    // not evidence that an already waiting physical request was cancelled.
    clock = deadline;
    const fetch = vi.fn(fixture.transport);
    vi.stubGlobal("fetch", fetch);
    await expect(fixture.broker.exchange({
      oidc_token: oidcToken, capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
      control_budget: { ...control(), deadline_unix_ms: deadline },
    })).rejects.toThrow("broker_control_invalid");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("cancels a waiting physical fetch at the original absolute deadline", async () => {
    const fixture = await consumingBroker();
    const oidcToken = await fixture.oidcToken();
    let clock = Date.now();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.spyOn(Date, "now").mockImplementation(() => clock);
    const request = fixture.configureFeedback();
    const deadline = clock + 30;
    const timeouts: { start: number; delay: number }[] = [];
    // Native AbortSignal.timeout uses Node's internal timers. Drive only its
    // timer wiring here, retaining real AbortController/AbortSignal delivery.
    // The production-broker IPC suite separately retains actual HTTP/80ms
    // native cancellation; this deterministic unit never replaces that proof.
    vi.spyOn(AbortSignal, "timeout").mockImplementation(delay => {
      timeouts.push({ start: Date.now(), delay });
      const controller = new AbortController();
      setTimeout(() => controller.abort(new DOMException("Original deadline expired", "TimeoutError")), delay);
      return controller.signal;
    });
    let started!: () => void;
    const dispatchStarted = new Promise<void>(resolve => { started = resolve; });
    let aborted = false;
    let settled = false;
    const fetch = vi.fn((_input: unknown, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      expect(signal).toBeInstanceOf(AbortSignal);
      if (!signal) throw new Error("physical dispatch signal is missing");
      expect(signal.aborted).toBe(false);
      signal.addEventListener("abort", () => { aborted = true; reject(signal.reason); }, { once: true });
      started();
    }));
    vi.stubGlobal("fetch", fetch);
    const outcome = fixture.broker.exchange({
      oidc_token: oidcToken, capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
      control_budget: { ...control(), deadline_unix_ms: deadline },
    }).then(value => { settled = true; return { status: "fulfilled" as const, value }; },
      error => { settled = true; return { status: "rejected" as const, error }; });
    await dispatchStarted;
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(timeouts[0]).toEqual({ start: clock, delay: 30 });
    clock += 29;
    await vi.advanceTimersByTimeAsync(29);
    expect(aborted).toBe(false);
    expect(settled).toBe(false);
    clock += 1;
    await vi.advanceTimersByTimeAsync(1);
    expect((await outcome).status).toBe("rejected");
    expect(aborted).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(timeouts.every(({ start, delay }) => start + delay === deadline)).toBe(true);
    expect(deadline).toBe(clock);
  });

  it("bounds each JWKS cache miss without canceling another request", async () => {
    const fixture = await consumingBroker();
    const request = fixture.configureFeedback();
    const releases: (() => void)[] = [];
    const signals: AbortSignal[] = [];
    vi.stubGlobal("fetch", (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes(".well-known/jwks")) return new Promise<Response>((resolve, reject) => {
        const signal = init?.signal as AbortSignal; signals.push(signal);
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        releases.push(() => { fixture.transport(input, init).then(resolve); });
      });
      return fixture.transport(input, init);
    });
    const body = {
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: request,
    };
    const original = fixture.broker.exchange(body);
    const bound = { ...control(), deadline_unix_ms: Date.now() + 30 };
    const selected = structuredClone(request);
    try {
      await expect(fixture.broker.exchange({ ...body, oidc_token: await fixture.oidcToken(), session_attestation: selected, control_budget: bound })).rejects.toThrow("broker_control_deadline_exhausted");
      expect(fixture.transportRequests).toHaveLength(0);
      expect(signals).toHaveLength(2);
      expect(signals[0].aborted).toBe(false);
      expect(signals[1].aborted).toBe(true);
    } finally {
      releases[0]();
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
