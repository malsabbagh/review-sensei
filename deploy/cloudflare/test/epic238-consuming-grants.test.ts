import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { consumingBroker, issue, requestAttestation, SHA } from "./epic238-consuming-broker-fixture";

const opened: Awaited<ReturnType<typeof consumingBroker>>[] = [];
async function open(filename?: string) {
  const fixture = await consumingBroker(filename);
  vi.spyOn(globalThis, "fetch").mockImplementation(fixture.transport);
  opened.push(fixture);
  return fixture;
}
beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => {
  for (const fixture of opened.splice(0)) fixture.close();
  vi.restoreAllMocks();
});

describe("Epic238 actual consuming broker composition", () => {
  it("real parent kill after committed consumption cannot replay an ambiguous grant", async () => {
    const directory = mkdtempSync(join(tmpdir(), "epic238-killed-grant-"));
    const children: ReturnType<typeof spawn>[] = [];
    function start() {
      const child = spawn(process.execPath, [fileURLToPath(new URL("../../../tests/epic238_consuming_broker_bridge.mjs", import.meta.url)), join(directory, "broker.sqlite"), join(directory, "dispatches.jsonl")], { stdio: ["pipe", "pipe", "pipe"] });
      children.push(child);
      const lines = createInterface({ input: child.stdout! });
      let id = 0;
      async function call(action: string, arguments_: Record<string, unknown> = {}) {
        const requestId = ++id;
        const response = new Promise<Record<string, unknown>>((resolve, reject) => {
          const timeout = setTimeout(() => reject(new Error("IPC checkpoint timeout")), 15_000);
          lines.once("line", line => { clearTimeout(timeout); resolve(JSON.parse(line)); });
        });
        child.stdin!.write(JSON.stringify({ id: requestId, action, ...arguments_ }) + "\n");
        const value = await response;
        expect(value.id).toBe(requestId);
        return value;
      }
      return { child, call };
    }
    try {
      const first = start();
      const oidc = await first.call("oidc");
      const issued = await first.call("exchange", { body: { oidc_token: oidc.result, capability: "review_session", session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: requestAttestation() } });
      expect(issued.status).toBe(200);
      const grant = issued.result as { session_grant: string; session_attestation: unknown };
      const checkpoint = await first.call("verify", { body: grant, pause_after_consume: true });
      expect(checkpoint.checkpoint).toBe("grant-consumed");
      expect(checkpoint).not.toHaveProperty("status");
      const terminated = new Promise(resolve => first.child.once("exit", (code, signal) => resolve({ code, signal })));
      first.child.kill("SIGKILL");
      expect(await terminated).toMatchObject(process.platform === "win32" ? { code: 1 } : { signal: "SIGKILL" });
      const restarted = start();
      expect((await restarted.call("grant_count")).result).toBe(0);
      expect((await restarted.call("verify", { body: grant })).status).toBe(200);
      await restarted.call("shutdown");
    } finally {
      await Promise.all(children.map(child => {
        if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
        const stopped = new Promise<void>(resolve => child.once("exit", () => resolve()));
        child.kill("SIGKILL");
        return stopped;
      }));
      rmSync(directory, { recursive: true });
    }
  }, 30_000);

  it("verifies a signed grant more than once because the worker does not store it", async () => {
    const fixture = await open();
    const issued = await issue(fixture);
    expect(fixture.grantCount()).toBe(0);
    const outcomes = await Promise.allSettled([
      fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation),
      fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation),
    ]);
    expect(outcomes.filter(item => item.status === "fulfilled")).toHaveLength(2);
    expect(fixture.grantCount()).toBe(0);
    await expect(fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation)).resolves.toEqual(issued.session_attestation);
    expect(fixture.ledgerRequests).toEqual([]);
  });

  it("refuses altered source or target without consuming the exact original grant", async () => {
    const fixture = await open();
    const issued = await issue(fixture);
    for (const change of [{ head_sha: "b".repeat(40) }, { command_digest: "f".repeat(64) }, { source_comment_id: 24680 }]) {
      await expect(fixture.broker.verifySessionGrant(issued.session_grant, { ...issued.session_attestation, ...change })).rejects.toThrow("broker_session_grant_invalid");
      expect(fixture.grantCount()).toBe(0);
    }
    await fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation);
    expect(fixture.grantCount()).toBe(0);
  });

  it("retains consumption after a failed host attempt and a fresh broker process state", async () => {
    const directory = mkdtempSync(join(tmpdir(), "epic238-grants-"));
    try {
      const filename = join(directory, "broker.sqlite");
      const first = await open(filename);
      const issued = await issue(first);
      await first.broker.verifySessionGrant(issued.session_grant, issued.session_attestation);
      opened.splice(opened.indexOf(first), 1);
      first.close();
      const restarted = await open(filename);
      await expect(restarted.broker.verifySessionGrant(issued.session_grant, issued.session_attestation)).resolves.toEqual(issued.session_attestation);
      expect(restarted.grantCount()).toBe(0);
    } finally {
      for (const fixture of opened.splice(0)) fixture.close();
      rmSync(directory, { recursive: true });
    }
  });

  it("does not reinterpret an ordinary explanatory reply as a maintainer command", async () => {
    const fixture = await open();
    fixture.githubInputs.source.body = "@sensei This path is intentionally local-only.";
    await expect(issue(fixture)).rejects.toThrow("broker_session_actor_rejected");
    expect(fixture.grantCount()).toBe(0);
    expect(fixture.ledgerRequests).not.toContain("session_issue");
  });

  it("does not apply a worker rate ceiling to newly requested grants", async () => {
    const fixture = await open();
    for (let index = 0; index < 11; index++) {
      const issued = await issue(fixture);
      await fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation);
    }
    expect(fixture.grantCount()).toBe(0);
  });

  it("accepts the same OIDC assertion again inside five minutes and refuses an older one", async () => {
    const fixture = await open();
    const body = {
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: requestAttestation(),
    };
    const first = await fixture.broker.exchange(body);
    await fixture.broker.verifySessionGrant(first.session_grant, first.session_attestation);
    const second = await fixture.broker.exchange(body);
    await fixture.broker.verifySessionGrant(second.session_grant, second.session_attestation);
    await expect(fixture.broker.exchange({
      ...body,
      oidc_token: await fixture.oidcToken({ iat: Math.floor(Date.now() / 1000) - 301 }),
    })).rejects.toThrow("oidc_time_invalid");
    expect(fixture.grantCount()).toBe(0);
  });
});
