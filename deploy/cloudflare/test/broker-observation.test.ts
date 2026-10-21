import { afterEach, describe, expect, it, vi } from "vitest";
import { BrokerObservation, BROKER_STAGES, type BrokerStage } from "../src/broker-observation";
import { BrokerLifetime } from "../src/broker-lifetime";
import { BrokerDispatchControl } from "../src/broker-dispatch-control";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });
const control = (max = 1) => new BrokerDispatchControl({schema_version: "1.0", operation_id: "a".repeat(64), request_id: "b".repeat(64), deadline_unix_ms: Date.now() + 40, max_dispatches: max});

describe("bounded operational metrics", () => {
  it("observes actual HTTP failure without changing the original debit or retrying", async () => {
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    let now = 100;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const observation = new BrokerObservation("exchange", "a4dd3d310f874964-LAX");
    const lifetime = new BrokerLifetime(undefined, observation);
    const budget = control();
    vi.stubGlobal("fetch", vi.fn(async () => { now += 7; return new Response("PRIVATE-BODY", { status: 403 }); }));
    const response = await lifetime.run(() => lifetime.upstream(scope => scope.fetch("https://PRIVATE-REPOSITORY.invalid/?token=PRIVATE", { headers: {authorization: "PRIVATE-TOKEN"} }), budget, "workflow_ref"));
    expect(response.status).toBe(403);
    observation.finish("success"); // Operation resolved; HTTP status stays a separate counter.
    observation.finish("refused", new Error("PRIVATE-TOKEN"));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(budget.receipt().dispatched).toBe(1);
    expect(log).toHaveBeenCalledTimes(1);
    expect(log.mock.calls[0][1]).toEqual({version: 1, operation: "exchange", outcome: "success", code: "ok", duration_ms: 7, retries: 0, correlation_id: "a4dd3d310f874964-LAX", stages: {workflow_ref: {attempts: 1, admitted: 1, dispatched: 1, failed: 0, timed_out: 0, http_error: 1, duration_ms: 7}}});
    expect(JSON.stringify(log.mock.calls)).not.toContain("PRIVATE");
  });

  it("preserves original cancellation, deadline category and late-response fencing", async () => {
    vi.useFakeTimers();
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    const observation = new BrokerObservation("exchange");
    const lifetime = new BrokerLifetime(undefined, observation);
    const budget = control();
    const debit = vi.spyOn(budget, "beforeDispatch");
    let signal: AbortSignal | undefined;
    let release!: (value: Response) => void;
    const cancel = vi.fn();
    vi.stubGlobal("fetch", vi.fn((_url, init) => { signal = init.signal; return new Promise<Response>(resolve => { release = resolve; }); }));
    const result = lifetime.run(() => lifetime.upstream(scope => scope.fetch("https://fixture.invalid", {}), budget, "oidc_jwks")).catch(error => error);
    expect(budget.receipt().dispatched).toBe(1);
    await vi.advanceTimersByTimeAsync(40);
    const error = await result;
    expect(error.message).toBe("broker_control_deadline_exhausted");
    expect(signal?.aborted).toBe(true);
    expect(debit).toHaveBeenCalledTimes(1);
    observation.finish("refused", error);
    const snapshot = JSON.stringify(log.mock.calls);
    release(new Response(new ReadableStream({ cancel })));
    await Promise.resolve(); await Promise.resolve();
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(log.mock.calls)).toBe(snapshot);
    expect(log.mock.calls[0][1]).toMatchObject({outcome: "refused", code: "broker_control_deadline_exhausted", stages: {oidc_jwks: {admitted: 1, dispatched: 1, failed: 1, timed_out: 1}}});
  });

  it("records refused admission without a dispatch or a second debit", async () => {
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    const observation = new BrokerObservation("session_grant");
    const budget = control();
    budget.beforeDispatch();
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const lifetime = new BrokerLifetime(undefined, observation);
    const error = await lifetime.run(() => lifetime.upstream(scope => scope.fetch("https://fixture.invalid", {}), budget, "github_read")).catch(error => error);
    observation.finish("refused", error);
    expect(fetch).not.toHaveBeenCalled();
    expect(budget.receipt().dispatched).toBe(1);
    expect(log.mock.calls[0][1]).toMatchObject({stages: {github_read: {attempts: 1, admitted: 0, dispatched: 0, failed: 1}}});
  });

  it("bounds labels/counters/durations, omits invalid correlation and tolerates sink failure", () => {
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    const clock = vi.spyOn(performance, "now").mockReturnValue(100);
    const observation = new BrokerObservation("exchange", "PRIVATE-REPOSITORY-AND-TOKEN");
    expect(observation.start("PRIVATE-TOKEN" as BrokerStage)).toBeUndefined();
    const span = observation.start("github_read")!;
    for (let i = 0; i < 70_000; i++) span.dispatched();
    clock.mockReturnValue(Number.POSITIVE_INFINITY); span.close(); span.close();
    observation.finish("refused", new Error("PRIVATE-BODY"));
    const event = log.mock.calls[0][1];
    expect(event.duration_ms).toBe(0);
    expect(event.stages.github_read.dispatched).toBe(65_535);
    expect(Object.keys(event.stages)).toEqual(["github_read"]);
    expect(BROKER_STAGES).toHaveLength(7);
    expect(event).not.toHaveProperty("correlation_id");
    expect(JSON.stringify(event)).not.toContain("PRIVATE");
    log.mockImplementation(() => { throw new Error("sink unavailable"); });
    expect(() => new BrokerObservation("exchange").finish("success")).not.toThrow();
  });
});
