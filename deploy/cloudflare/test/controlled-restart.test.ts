import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { consumingBroker, SHA } from "./epic238-consuming-broker-fixture";
import { resetOidcJwksCacheForTesting } from "../src/oidc";
import { authorizeControlledRestart } from "../src/controlled-restart";

const state = vi.hoisted(() => ({ rows: [] as Record<string, unknown>[] }));
vi.mock("../../../packages/python/review-sensei-github/src/review_sensei_github/controlled_restarts.json", () => ({
  default: { schema_version: "controlled-restart-registry-v1", get registrations() { return state.rows; } },
}));

function row() {
  return { request_id: "f".repeat(64), repository: "acme/widgets", repository_id: 987654321,
    pull_request: 7, head_sha: SHA, base_sha: "b".repeat(40), workflow_sha: SHA,
    policy_commit: "c".repeat(40), run_id: "10000000001", job: "hosted",
    issuance_disabled_at: 1000, retire_after: 4600, old_runs: [1],
    old_comments: [{ id: 2, bytes: 3, sha256: "d".repeat(64) }] };
}
beforeEach(() => { state.rows = []; resetOidcJwksCacheForTesting(); });
afterEach(() => { state.rows = []; vi.unstubAllGlobals(); });

describe("controlled restart existing Worker endpoints", () => {
  it("leaves unregistered ordinary entry unchanged", async () => {
    const fixture = await consumingBroker();
    const authority = fixture.authority("review");
    expect(() => authorizeControlledRestart(authority, "review_publish", "pull_request")).not.toThrow();
  });

  it.each(["review_session", "review_actions", "review_publish", "check_publish", "review_status"])("allows designated existing %s capability after retirement", async capability => {
    const fixture = await consumingBroker(); state.rows = [row()];
    expect(() => authorizeControlledRestart(fixture.authority("review"), capability, "workflow_dispatch")).not.toThrow();
  });

  it.each(["thread_resolve", "learning_write", "inline_reply", "issue_reply"])("refuses %s in the restart scope", async capability => {
    const fixture = await consumingBroker(); state.rows = [row()];
    expect(() => authorizeControlledRestart(fixture.authority("review"), capability, "workflow_dispatch")).toThrow("broker_controlled_restart_held");
  });

  it.each([
    ["old run", { run_id: "10000000002" }, { run_id: "10000000002", event_name: "workflow_dispatch" }],
    ["rerun", { run_attempt: 2 }, { run_attempt: "2", event_name: "workflow_dispatch" }],
    ["old workflow", { job_workflow_sha: "c".repeat(40) }, { job_workflow_sha: "c".repeat(40), event_name: "workflow_dispatch" }],
    ["ordinary event", {}, { event_name: "pull_request" }],
  ])("refuses %s before installation/token effects", async (_name, change, claims) => {
    const fixture = await consumingBroker(); state.rows = [row()];
    vi.stubGlobal("fetch", fixture.transport);
    await expect(fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(claims), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: { ...fixture.authority("review"), ...change },
    })).rejects.toThrow("broker_controlled_restart_held");
    expect(fixture.transportRequests.filter(value => value.path.endsWith("/access_tokens"))).toEqual([]);
  });

  it("refuses command authority before token effects", async () => {
    const fixture = await consumingBroker(); state.rows = [row()];
    vi.stubGlobal("fetch", fixture.transport);
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(),
      capability: "review_session", session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: fixture.authority("command"),
    })).rejects.toThrow("broker_controlled_restart_held");
    expect(fixture.transportRequests.filter(value => value.path.endsWith("/access_tokens"))).toEqual([]);
  });

  it("refuses early expiry and duplicate designations", async () => {
    const fixture = await consumingBroker(); const authority = fixture.authority("review");
    state.rows = [{ ...row(), retire_after: Math.floor(Date.now() / 1000) + 3600 }];
    expect(() => authorizeControlledRestart(authority, "review_session", "workflow_dispatch")).toThrow("broker_controlled_restart_held");
    state.rows = [row(), row()];
    expect(() => authorizeControlledRestart(authority, "review_session", "workflow_dispatch")).toThrow("broker_restart_registry_invalid");
  });

  it("issues the designated session through production OIDC and refuses its grant after designation changes", async () => {
    const fixture = await consumingBroker(); state.rows = [row()];
    vi.stubGlobal("fetch", fixture.transport);
    const grant = await fixture.broker.exchange({
      oidc_token: await fixture.oidcToken({ event_name: "workflow_dispatch" }), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: fixture.authority("review"),
    });
    expect(await fixture.broker.verifySessionGrant(grant.session_grant, grant.session_attestation)).toEqual(grant.session_attestation);
    const tokenCalls = fixture.transportRequests.filter(value => value.path.endsWith("/access_tokens")).length;
    state.rows = [{ ...row(), run_id: "10000000002" }];
    await expect(fixture.broker.verifySessionGrant(grant.session_grant, grant.session_attestation)).rejects.toThrow("broker_controlled_restart_held");
    expect(fixture.transportRequests.filter(value => value.path.endsWith("/access_tokens"))).toHaveLength(tokenCalls);
  });
});
