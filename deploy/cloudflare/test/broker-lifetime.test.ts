import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkerEnv } from "../src/env";
import { GitHubApi } from "../src/github-api";
import { BrokerLifetime } from "../src/broker-lifetime";
import { BrokerDispatchControl } from "../src/broker-dispatch-control";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });
const env = { GITHUB_APP_ID: "123", GITHUB_APP_PRIVATE_KEY: "unused" } as WorkerEnv;

describe("uncontrolled broker upstream lifetime", () => {
  it("keeps the original control reason in a direct adapter's deadline timer", async () => {
    vi.useFakeTimers();
    const control = new BrokerDispatchControl({schema_version:"1.0",operation_id:"a".repeat(64),request_id:"b".repeat(64),deadline_unix_ms:Date.now()+40,max_dispatches:1});
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    const result = new GitHubApi(env, control).request("GET", "/fixture", undefined).catch(error => error.message);
    expect(control.receipt().dispatched).toBe(1);
    await vi.advanceTimersByTimeAsync(40);
    await expect(result).resolves.toBe("broker_control_deadline_exhausted");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("preserves controlled deadline precedence when the caller header is equal", async () => {
    vi.useFakeTimers();
    const deadline = Date.now() + 40;
    const control = new BrokerDispatchControl({schema_version:"1.0",operation_id:"a".repeat(64),request_id:"b".repeat(64),deadline_unix_ms:deadline,max_dispatches:1});
    const lifetime = new BrokerLifetime(deadline);
    const beforeDispatch = vi.spyOn(control, "beforeDispatch");
    let signal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn((_url, init) => { signal = init.signal; return new Promise(() => {}); }));
    const result = lifetime.run(() => lifetime.upstream(scope => scope.fetch("https://fixture.invalid", {}), control)).catch(error => error.message);
    expect(control.receipt().dispatched).toBe(1);
    await vi.advanceTimersByTimeAsync(40);
    await expect(result).resolves.toBe("broker_control_deadline_exhausted");
    expect(signal?.aborted).toBe(true);
    expect(beforeDispatch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("aborts a stalled Git fetch before one bounded REST fallback", async () => {
    vi.useFakeTimers();
    let release!: (value: Response) => void;
    let signal: AbortSignal | undefined;
    const pending = new Promise<Response>(resolve => { release = resolve; });
    vi.stubGlobal("fetch", vi.fn((_url, init) => { signal = init.signal; return pending; }));
    const github = new GitHubApi(env);
    const rest = vi.spyOn(github, "request").mockResolvedValue({status: 200, data: {object: {type: "commit", sha: "a".repeat(40)}}});
    const result = github.publicWorkflowRuntimeShas("v5");
    try {
      await vi.advanceTimersByTimeAsync(10_000);
      expect(signal).toBeInstanceOf(AbortSignal);
      expect(signal?.aborted).toBe(true);
      expect(rest).toHaveBeenCalledTimes(1);
      await expect(result).resolves.toEqual({commitSha: "a".repeat(40), refSha: "a".repeat(40)});
    } finally { release(new Response("", {status: 503})); await result.catch(() => {}); }
  });

  it("cancels a stalled REST body read and rejects late data", async () => {
    vi.useFakeTimers();
    let finish!: () => void;
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({start(controller) { finish = () => { try {controller.close();} catch {} }; },cancel});
    let signal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn(async (_url, init) => { signal = init.signal; return new Response(stream); }));
    const result = new GitHubApi(env).request("GET", "/public", undefined).then(() => "accepted", error => error.message);
    try {
      await vi.advanceTimersByTimeAsync(10_000);
      expect(signal?.aborted).toBe(true);
      expect(cancel).toHaveBeenCalledTimes(1);
      await expect(result).resolves.toBe("broker_upstream_timeout");
    } finally { finish(); await result; }
  });
});
