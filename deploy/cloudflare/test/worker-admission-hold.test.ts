import { afterEach, describe, expect, it, vi } from "vitest";
const calls = vi.hoisted(() => ({ construct: vi.fn(), exchange: vi.fn(), verify: vi.fn() }));
vi.mock("../src/token-broker", () => ({ TokenBroker: class {
  constructor() { calls.construct(); }
  exchange = calls.exchange;
  verifySessionGrant = calls.verify;
} }));
import worker from "../src/worker";
import type { WorkerEnv } from "../src/env";

afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals(); });
describe("named operator broker admission hold", () => {
  it.each(["https://github.reviewsensei.dev", "https://reviewsensei-github-app.example.workers.dev", "https://self.internal"])("holds every broker surface through the same handler at%s", async origin => {
    const fetch = vi.fn(() => { throw new Error("unexpected external dispatch"); });
    vi.stubGlobal("fetch", fetch);
    const waitUntil = vi.fn();
    for (const mode of ["held", "", "invalid", "OPEN"]) {
      for (const path of ["/github/token", "/github/session-grant"]) {
        for (const method of ["POST", "GET", "OPTIONS"]) {
          for (const callerOrigin of [undefined, "https://untrusted.example"]) {
            const request = new Request(origin + path, { method, headers: callerOrigin ? { origin: callerOrigin } : {} });
            const result = await worker.fetch(request, { BROKER_ADMISSION_MODE: mode } as WorkerEnv, { waitUntil });
            expect(result.status).toBe(503);
            expect(result.headers.get("cache-control")).toBe("no-store");
            expect(result.headers.has("retry-after")).toBe(false);
            expect(await result.json()).toEqual({ error: "broker_admission_held", temporary: true });
          }
        }
      }
    }
    expect(calls.construct).not.toHaveBeenCalled();
    expect(calls.exchange).not.toHaveBeenCalled(); expect(calls.verify).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled(); expect(waitUntil).not.toHaveBeenCalled();
  });
  it("does not read even malformed request bodies while held", async () => {
    const request = new Request("https://self.internal/github/token", { method: "POST", body: "invalid-json" });
    const read = vi.spyOn(request.body!, "getReader");
    expect((await worker.fetch(request, { BROKER_ADMISSION_MODE: "held" } as WorkerEnv)).status).toBe(503);
    expect(read).not.toHaveBeenCalled(); expect(calls.construct).not.toHaveBeenCalled();
  });
  it("keeps health available under held and invalid modes", async () => {
    for (const mode of ["held", "invalid"]) {
      const result = await worker.fetch(new Request("https://self.internal/healthz"), { BROKER_ADMISSION_MODE: mode } as WorkerEnv);
      expect(result.status).toBe(200); expect(await result.json()).toEqual({ ok: true });
    }
    expect(calls.construct).not.toHaveBeenCalled();
  });
  it.each([undefined, "open"])("delegates normally when mode is%s without scheduling retries", async mode => {
    calls.exchange.mockResolvedValue({ token: "synthetic", capability: "review_publish" });
    const waitUntil = vi.fn();
    const body = JSON.stringify({ oidc_token: "synthetic" });
    const request = new Request("https://self.internal/github/token", { method: "POST",
      headers: { "content-type": "application/json", "content-length": String(body.length) }, body });
    expect((await worker.fetch(request, { BROKER_ADMISSION_MODE: mode } as WorkerEnv, { waitUntil })).status).toBe(200);
    expect(calls.exchange).toHaveBeenCalledTimes(1); expect(waitUntil).not.toHaveBeenCalled();
  });
});
