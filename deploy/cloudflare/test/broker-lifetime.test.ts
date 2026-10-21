import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkerEnv } from "../src/env";
import { GitHubApi } from "../src/github-api";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });
const env = { GITHUB_APP_ID: "123", GITHUB_APP_PRIVATE_KEY: "unused" } as WorkerEnv;

describe("uncontrolled broker upstream lifetime", () => {
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
