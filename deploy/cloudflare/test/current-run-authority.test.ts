import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { consumingBroker, SHA, WORKFLOW } from "./epic238-consuming-broker-fixture";
import { resetOidcJwksCacheForTesting } from "../src/oidc";
import { canonicalRunAuthority, parseRunAuthority, type RunAuthorityRequest } from "../src/run-authority";
import { BrokerLifetime, BROKER_MAX_DISPATCHES } from "../src/broker-lifetime";
import { BrokerDispatchControl } from "../src/broker-dispatch-control";

async function request(purpose: "review" | "command" | "reply" = "command"): Promise<RunAuthorityRequest> {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("@sensei review continue"));
  const now = Math.floor(Date.now() / 1000);
  return {
    version: 3, purpose, repository: "acme/widgets", repository_id: 987654321,
    pull_request: 7, head_sha: SHA, run_id: "10000000001", run_attempt: 1,
    job: purpose === "command" ? "command" : "hosted",
    job_workflow_ref: WORKFLOW, job_workflow_sha: SHA,
    issued_at: now, expires_at: now + 600, audience: "reviewsensei-run-action",
    source: purpose === "review"
      ? { kind: "pull_request", id: 7, updated_at: null, body_sha256: null }
      : { kind: "issue_comment", id: 13579, updated_at: "2026-10-09T00:00:00Z",
        body_sha256: [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, "0")).join("") },
  };
}

beforeEach(() => resetOidcJwksCacheForTesting());
afterEach(() => vi.unstubAllGlobals());

describe("current run authority through production OIDC and TokenBroker", () => {
  it.each(["review", "command", "reply"] as const)("issues and verifies exact %s authority", async purpose => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const authority = await request(purpose);
    const grant = await fixture.broker.exchange({
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: authority,
    });
    expect(grant.session_attestation).toEqual({ ...authority,
      actor: purpose === "review" ? null : { login: "octocat", type: "User", association: "OWNER" } });
    expect(grant.reservation_owner).toEqual({ run_id: "10000000001", head_sha: SHA });
    expect(grant.app_id).toBe(12345);
    const first = await fixture.broker.verifySessionGrant(grant.session_grant, grant.session_attestation);
    expect(first).toEqual(grant.session_attestation);
    expect(await fixture.broker.verifySessionGrant(grant.session_grant, grant.session_attestation)).toEqual(first);
    expect(canonicalRunAuthority(first)).not.toContain("read_accounting");
    expect(fixture.transportRequests.some(value => value.kind === "oidc")).toBe(true);
  });

  it.each(["body", "session", "authority"])("refuses caller App ID at%s without overriding trusted identity", async placement => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const body = {
      oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: await request("review"),
    };
    const supplied = placement === "body" ? { ...body, app_id: 999 }
      : placement === "session" ? { ...body, session: { ...body.session, app_id: 999 } }
      : { ...body, session_attestation: { ...body.session_attestation, app_id: 999 } };
    await expect(fixture.broker.exchange(supplied)).rejects.toThrow();
    expect(fixture.transportRequests.filter(value => value.path.endsWith("/access_tokens"))).toEqual([]);
  });

  it.each([
    ["purpose", { purpose: "feedback", job: "hosted" }],
    ["run", { run_id: "10000000002" }],
    ["attempt", { run_attempt: 2 }],
    ["job", { job: "hosted" }],
    ["workflow", { job_workflow_sha: "c".repeat(40) }],
    ["audience", { audience: "reviewsensei-session-ledger" }],
    ["obsolete v1", { version: 1 }],
    ["obsolete v2", { version: 2 }],
    ["nested accounting", { read_accounting: { calls: 0 } }],
  ])("refuses %s authority before installation effects", async (_name, changes) => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_publish",
      session_attestation: { ...await request(), ...changes } })).rejects.toThrow();
    expect(fixture.transportRequests.filter(value => value.path.includes("/access_tokens"))).toEqual([]);
  });

  it.each(["head", "body", "updatedAt", "actor", "bot", "association", "cross-pr"])("refuses changed live %s", async fault => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const source = fixture.githubInputs.source;
    if (fault === "head") fixture.githubInputs.head = "c".repeat(40);
    if (fault === "body") source.body = "@sensei review pause";
    if (fault === "updatedAt") source.updatedAt = "2026-10-10T00:00:00Z";
    if (fault === "actor") source.authorId = 999;
    if (fault === "bot") source.userType = "Bot";
    if (fault === "association") source.association = "NONE";
    if (fault === "cross-pr") source.url = "https://api.github.test/repos/acme/widgets/issues/8";
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: await request() })).rejects.toThrow();
    expect(fixture.transportRequests.some(value => value.path.endsWith("/issues/7/comments"))).toBe(false);
  });

  it.each(["learning_write", "inline_reply"])("issue reply purpose cannot acquire %s", async capability => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability,
      session_attestation: await request("reply") })).rejects.toThrow("broker_capability_purpose_rejected");
    expect(fixture.transportRequests.filter(value => value.path.includes("/access_tokens"))).toEqual([]);
  });

  it.each(["review_publish", "check_publish", "thread_resolve"])("reply reassessment may acquire %s with fresh exact source proof", async capability => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const authority = await request("reply");
    expect((await fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability,
      session_attestation: authority })).capability).toBe(capability);
    fixture.githubInputs.source.body = "edited source";
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability,
      session_attestation: authority })).rejects.toThrow("broker_session_actor_rejected");
  });

  it("review cannot use an issue reply publication capability", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "issue_reply",
      session_attestation: await request("review") })).rejects.toThrow("broker_capability_purpose_rejected");
  });

  it("command can publish its source-bound issue receipt but cannot publish inline replies", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const authority = await request("command");
    expect((await fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "issue_reply",
      session_attestation: authority })).capability).toBe("issue_reply");
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "inline_reply",
      session_attestation: authority })).rejects.toThrow("broker_capability_purpose_rejected");
  });

  it("binds inline reply source and exact capability independently", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const source = fixture.githubInputs.inlineSource;
    source.body = "@reviewsensei explain this";
    source.login = "octocat"; source.authorId = 12345678;
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(source.body));
    const authority = await request("reply");
    authority.source = { kind: "review_comment", id: source.id, updated_at: source.updatedAt,
      body_sha256: [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, "0")).join("") };
    const grant = await fixture.broker.exchange({ oidc_token: await fixture.oidcToken({ event_name: "pull_request_review_comment" }),
      capability: "inline_reply", session_attestation: authority });
    expect(grant.capability).toBe("inline_reply");
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken({ event_name: "pull_request_review_comment" }),
      capability: "issue_reply", session_attestation: authority })).rejects.toThrow("broker_capability_purpose_rejected");
  });

  it("keeps Actions capability narrow while separately authenticating PR source", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const result = await fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_actions",
      session_attestation: await request("review") });
    expect(result.capability).toBe("review_actions");
    expect(fixture.transportRequests.filter(value => value.path.endsWith("/access_tokens"))).toHaveLength(3);
  });

  it("fails closed at the independent Worker service ceiling", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const control_budget = { schema_version: "1.0", operation_id: "a".repeat(64), request_id: "b".repeat(64),
      max_dispatches: 1, deadline_unix_ms: Date.now() + 20_000 };
    await expect(fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: await request(), control_budget })).rejects.toThrow("broker_control_budget_exhausted");
    expect(fixture.transportRequests.length).toBe(1);
  });

  it.each([12, 30])("reports internal fanout under service ceiling %i without nested caller authority", async maximum => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const control_budget = { schema_version: "1.0", operation_id: "a".repeat(64), request_id: "b".repeat(64),
      max_dispatches: maximum, deadline_unix_ms: Date.now() + 20_000 };
    const result = await fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
      session_attestation: await request(), control_budget });
    expect(result.control_receipt!.dispatched).toBe(fixture.transportRequests.length);
    expect(result.control_receipt!.dispatched).toBeLessThanOrEqual(maximum);
    expect(result.session_attestation!.purpose).toBe("command");
    expect(result.session_attestation).not.toHaveProperty("mutation");
    expect(result.session_attestation).not.toHaveProperty("read_accounting");
  });

  it("refuses changed purpose/run/intent on exact signed grant verification", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    const result = await fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session",
      session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: await request() });
    for (const changes of [{ run_id: "10000000002" }, { purpose: "reply", job: "hosted" },
      { source: { ...result.session_attestation!.source, body_sha256: "f".repeat(64) } }, { version: 1 }, { version: 2 }]) {
      await expect(fixture.broker.verifySessionGrant(result.session_grant,
        { ...result.session_attestation, ...changes })).rejects.toThrow();
    }
  });
});

describe("closed current authority parsing", () => {
  it("rejects expired, future, extended or oversized lifetime documents", async () => {
    const good = await request();
    for (const changes of [{ expires_at: good.issued_at }, { issued_at: good.issued_at + 60 },
      { expires_at: good.issued_at + 601 }, { expires_at: good.issued_at - 1 },
      { source: { ...good.source, extra: true } }, { purpose: "feedback" },
      { operation: "command" }, { run_attempt: true }, { repository_id: true },
      { purpose: ["review"] }, { job: ["hosted"] }, { source: { ...good.source, kind: ["issue_comment"] } }]) {
      expect(() => parseRunAuthority({ ...good, ...changes })).toThrow("broker_session_attestation_invalid");
    }
  });
});

describe("independent Worker physical admission ceiling", () => {
  it("refuses attempt61 without optional caller control, preserving failed admissions", async () => {
    const fetch = vi.fn(async () => { throw new Error("unknown upstream outcome"); });
    vi.stubGlobal("fetch", fetch);
    const lifetime = new BrokerLifetime();
    await lifetime.run(async () => {
      for (let attempt = 0; attempt < BROKER_MAX_DISPATCHES; attempt++) {
        await expect(lifetime.upstream(scope => scope.fetch("https://api.github.test/test"))).rejects.toThrow("unknown upstream outcome");
      }
      await expect(lifetime.upstream(scope => scope.fetch("https://api.github.test/test"))).rejects.toThrow("broker_request_budget_exhausted");
    });
    expect(fetch).toHaveBeenCalledTimes(60);
  });

  it.each([9, 12, 30])("optional downward ceiling%i never replenishes the service allowance", async maximum => {
    const fetch = vi.fn(async () => new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    const lifetime = new BrokerLifetime();
    const control = new BrokerDispatchControl({ schema_version: "1.0", operation_id: "a".repeat(64),
      request_id: "b".repeat(64), max_dispatches: maximum, deadline_unix_ms: Date.now() + 20_000 });
    await lifetime.run(async () => {
      for (let attempt = 0; attempt < maximum; attempt++) {
        await lifetime.upstream(scope => scope.fetch("https://api.github.test/test"), control);
      }
      await expect(lifetime.upstream(scope => scope.fetch("https://api.github.test/test"), control)).rejects.toThrow("broker_control_budget_exhausted");
      // Removing a downward constraint cannot reset already admitted work.
      for (let attempt = maximum; attempt < BROKER_MAX_DISPATCHES; attempt++) {
        await lifetime.upstream(scope => scope.fetch("https://api.github.test/test"));
      }
      await expect(lifetime.upstream(scope => scope.fetch("https://api.github.test/test"))).rejects.toThrow("broker_request_budget_exhausted");
    });
    expect(fetch).toHaveBeenCalledTimes(60);
    expect(control.receipt().dispatched).toBe(maximum);
  });
});

describe("v4 observation through production OIDC and HTTP extraction", () => {
  it.each(["review", "command", "reply"] as const)("signs current %s facts using the same bounded exchange requests", async purpose => {
    const counts: number[] = [];
    for (const version of [3, 4] as const) {
      resetOidcJwksCacheForTesting();
      const fixture = await consumingBroker();
      vi.stubGlobal("fetch", async (input: string | URL | Request, init?: RequestInit) => {
        const response = await fixture.transport(input, init);
        const url = new URL(input instanceof Request ? input.url : String(input));
        if (url.pathname === "/repos/acme/widgets/installation") return Response.json({ id: 2468, repository_selection: "selected", permissions: { metadata: "read", pull_requests: "write" } });
        if (url.pathname === "/repos/acme/widgets") return Response.json({ id: 987654321, fork: false, full_name: "acme/widgets", owner: { id: 77, login: "acme" } });
        return response;
      });
      const authority = { ...await request(purpose), version };
      const result = await fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session", session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: authority });
      counts.push(fixture.transportRequests.length);
      if (version === 3) expect(result.session_attestation?.current_observation).toBeUndefined();
      else {
        const observation = result.session_attestation!.current_observation!;
        expect(observation.repository.owner_id).toBe(77);
        expect(observation.installation.repository_access).toBe(true);
        expect(observation.actor.id).toBeGreaterThan(0);
        expect(observation.actor.source_association).toBe(purpose === "review" ? null : "OWNER");
        expect(await fixture.broker.verifySessionGrant(result.session_grant, result.session_attestation)).toEqual(result.session_attestation);
      }
    }
    expect(counts[1]).toBe(counts[0]);
  });
  it("rejects v4 supplied observations and missing observation grants; leaves v3 closed", async () => {
    const authority = { ...await request("review"), version: 4 };
    expect(parseRunAuthority(authority).version).toBe(4);
    expect(() => parseRunAuthority({ ...authority, current_observation: {} })).toThrow();
    expect(() => parseRunAuthority({ ...authority, actor: null }, true)).toThrow();
    expect(() => parseRunAuthority({ ...authority, version: 3, actor: null, current_observation: {} }, true)).toThrow();
  });
});
