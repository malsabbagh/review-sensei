import { createHash } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const oidc = vi.hoisted(() => ({ verify: vi.fn() }));
vi.mock("../src/oidc", () => ({ verifyOidcAssertion: oidc.verify }));
import type { WorkerEnv } from "../src/env";
import { TokenBroker } from "../src/token-broker";
import { BrokerLifetime } from "../src/broker-lifetime";
import type { RunAuthorityRequest } from "../src/run-authority";
import commandParityCases from "../../../tests/fixtures/maintainer-command-parity.json";
import feedbackFixture from "../../../tests/fixtures/feedback-session-attestation.json";

const SHA = "a".repeat(40);
const TAG_OBJECT_SHA = "b".repeat(40);
const WORKFLOW = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5";
const BODY = "@sensei review continue";
const hash = (body: string) => createHash("sha256").update(body).digest("hex");
function claims(overrides: Record<string, unknown> = {}) {
  return { repository: "acme/widgets", repository_owner: "acme", repository_id: 987654321,
    actor: "octocat", actor_id: 12345678, event_name: "pull_request", run_id: "10000000001", run_attempt: "1",
    job_workflow_ref: WORKFLOW, job_workflow_sha: SHA, runner_environment: "github-hosted", ...overrides };
}
function request(purpose: "review" | "command" | "reply" = "review", overrides: Record<string, unknown> = {}, identity = claims()): RunAuthorityRequest {
  const now = Math.floor(Date.now() / 1000);
  return { version: 3, purpose, repository: identity.repository, repository_id: identity.repository_id,
    pull_request: 7, head_sha: SHA, run_id: identity.run_id, run_attempt: Number(identity.run_attempt),
    job: purpose === "command" ? "command" : "hosted", job_workflow_ref: identity.job_workflow_ref,
    job_workflow_sha: identity.job_workflow_sha, issued_at: now, expires_at: now + 600,
    audience: "reviewsensei-run-action", source: purpose === "review"
      ? { kind: "pull_request", id: 7, updated_at: null, body_sha256: null }
      : { kind: "issue_comment", id: 13579, updated_at: "2026-10-09T00:00:00Z", body_sha256: hash(BODY) },
    ...overrides } as RunAuthorityRequest;
}
function harness(tag = "v5") {
  const env = { PUBLIC_WORKFLOW_TAG: tag, REVIEWSENSEI_SIGNING_KEY: "test-signing-key", GITHUB_APP_ID: "12345" } as WorkerEnv;
  const github = {
    publicWorkflowRuntimeShas: vi.fn(async () => ({ commitSha: SHA, refSha: SHA })),
    installationFor: vi.fn(async () => 2468),
    installationToken: vi.fn(async () => ({ token: "metadata-token" })),
    repositoryInfo: vi.fn(async () => ({ id: 987654321, fork: false })),
    capabilityToken: vi.fn(async () => "scoped-token"),
    pullRequestHead: vi.fn(async () => SHA),
    sessionAlreadyKnown: vi.fn(async () => false),
    feedbackComment: vi.fn(async () => ({ kind: "issue", comment_id: 13579, updated_at: "2026-10-09T00:00:00Z",
      author: "octocat", author_id: 12345678, association: "OWNER", body: BODY, body_sha256: hash(BODY) })),
  };
  Object.assign(github, { withRequestLifetime: vi.fn(() => github) });
  return { broker: new TokenBroker(env, github as never), github, env };
}
function sessionBody(authority = request()) {
  return { oidc_token: "signed-jwt", capability: "review_session", session_attestation: authority,
    session: { repository_id: authority.repository_id, pull_request: authority.pull_request, head_sha: authority.head_sha } };
}
beforeEach(() => { oidc.verify.mockReset(); oidc.verify.mockResolvedValue(claims()); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

describe("current token broker authorization", () => {
  it("bounds an uncontrolled exchange and refuses late OIDC continuation before minting", async () => {
    vi.useFakeTimers();
    const { env } = harness();
    const broker = new TokenBroker({ ...env, GITHUB_APP_ID: "123", GITHUB_APP_PRIVATE_KEY: "unused" });
    const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("must not dispatch"));
    let release!: (value: ReturnType<typeof claims>) => void;
    oidc.verify.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    const outcome = broker.exchange({ oidc_token: "signed", session_attestation: request() }).then(() => "accepted", error => error.message);
    await vi.advanceTimersByTimeAsync(25_000);
    expect(await outcome).toBe("broker_request_deadline_exhausted");
    release(claims()); await vi.advanceTimersByTimeAsync(0);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("accepts only downward request deadlines before any OIDC effects", async () => {
    const { broker, github } = harness();
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request() }, undefined, Date.now() - 1)).rejects.toThrow("broker_request_deadline_exhausted");
    expect(oidc.verify).not.toHaveBeenCalled(); expect(github.capabilityToken).not.toHaveBeenCalled();
  });
  it.each([undefined, "constructor", "toString", "__proto__", "hasOwnProperty"])("refuses missing authority or inherited capability %s", async capability => {
    const { broker, github } = harness();
    await expect(broker.exchange({ oidc_token: "signed", capability })).rejects.toThrow();
    expect(github.capabilityToken).not.toHaveBeenCalled();
  });
  it.each([1, 2])("rejects obsolete executable version%i before token minting", async version => {
    const { broker, github } = harness();
    await expect(broker.exchange({ ...sessionBody(), session_attestation: version === 2 ? feedbackFixture.request : { ...request(), version: 1 } })).rejects.toThrow("broker_session_attestation_invalid");
    expect(github.capabilityToken).not.toHaveBeenCalled(); expect(github.sessionAlreadyKnown).not.toHaveBeenCalled();
  });
  it.each([
    [undefined, "review_publish", { pull_requests: "write" }, true, "review"],
    ["review_publish", "review_publish", { pull_requests: "write" }, true, "review"],
    ["thread_resolve", "thread_resolve", { contents: "write", pull_requests: "write" }, false, "command"],
    ["check_publish", "check_publish", { checks: "write" }, false, "reply"],
    ["review_actions", "review_actions", { actions: "read" }, false, "review"],
    ["review_status", "review_status", { pull_requests: "read" }, false, "review"],
    ["inline_reply", "inline_reply", { pull_requests: "write" }, false, "reply"],
    ["issue_reply", "issue_reply", { pull_requests: "write" }, false, "command"],
    ["learning_write", "learning_write", { contents: "write", pull_requests: "write" }, false, "review"],
  ] as const)("maps %s to exact returned scope with current purpose", async (capability, returned, permissions, contents, purpose) => {
    const { broker, github } = harness();
    const identity = claims({ event_name: capability === "inline_reply" ? "pull_request_review_comment" : purpose === "review" ? "pull_request" : "issue_comment" });
    oidc.verify.mockResolvedValue(identity);
    const authority = request(purpose, {}, identity);
    if (capability === "inline_reply") authority.source.kind = "review_comment";
    expect(await broker.exchange({ oidc_token: "signed", capability, session_attestation: authority })).toEqual({ token: "scoped-token", capability: returned });
    expect(github.installationToken).toHaveBeenCalledWith(2468, "acme/widgets", { metadata: "read" });
    expect(github.capabilityToken).toHaveBeenCalledWith(2468, "acme/widgets", permissions, contents);
    expect(github.pullRequestHead).toHaveBeenCalledWith("acme/widgets", 7, "scoped-token");
  });
  it.each([
    ["wrong workflow", { job_workflow_ref: "attacker/repo/.github/workflows/review-sensei-run.yml@refs/tags/v5" }, "broker_workflow_rejected"],
    ["unqualified workflow ref", { job_workflow_ref: "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5" }, "broker_workflow_rejected"],
    ["wrong runtime", { job_workflow_sha: "c".repeat(40) }, "broker_workflow_rejected"],
    ["wrong runner", { runner_environment: "private-cloud" }, "broker_runner_rejected"],
    ["wrong event", { event_name: "push" }, "broker_event_rejected"],
    ["wrong owner", { repository_owner: "attacker" }, "broker_repository_rejected"],
  ])("preserves %s refusal independently of current envelope binding", async (_name, overrides, error) => {
    const identity = claims(overrides); oidc.verify.mockResolvedValue(identity);
    const { broker, github } = harness();
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request("review", {}, identity) })).rejects.toThrow(error);
    expect(github.capabilityToken).not.toHaveBeenCalled();
  });
  it("accepts an annotated tag object only while it matches resolved runtime", async () => {
    const identity = claims({ job_workflow_sha: TAG_OBJECT_SHA }); oidc.verify.mockResolvedValue(identity);
    const { broker, github } = harness();
    github.publicWorkflowRuntimeShas.mockResolvedValue({ commitSha: SHA, refSha: TAG_OBJECT_SHA });
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request("review", {}, identity) })).resolves.toMatchObject({ capability: "review_publish" });
    github.publicWorkflowRuntimeShas.mockResolvedValue({ commitSha: "c".repeat(40), refSha: "d".repeat(40) });
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request("review", {}, identity) })).rejects.toThrow("broker_workflow_rejected");
  });
  it("rejects public tags outside the configured migration window before installation lookup", async () => {
    const identity = claims({ job_workflow_ref: "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v3" });
    oidc.verify.mockResolvedValue(identity);
    const { broker, github } = harness();
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request("review", {}, identity) })).rejects.toThrow("broker_workflow_rejected");
    expect(github.publicWorkflowRuntimeShas).not.toHaveBeenCalled(); expect(github.installationFor).not.toHaveBeenCalled();
  });
  it("binds another repository and installation exclusively to the verified OIDC identity", async () => {
    const identity = claims({ repository: "other-org/another-repository", repository_owner: "other-org", repository_id: 1234567 });
    oidc.verify.mockResolvedValue(identity);
    const { broker, github } = harness();
    github.installationFor.mockResolvedValue(9753); github.repositoryInfo.mockResolvedValue({ id: 1234567, fork: false });
    await expect(broker.exchange({ oidc_token: "signed", capability: "thread_resolve", session_attestation: request("review", {}, identity) })).resolves.toMatchObject({ capability: "thread_resolve" });
    expect(github.installationFor).toHaveBeenCalledWith("other-org/another-repository");
    expect(github.capabilityToken).toHaveBeenCalledWith(9753, "other-org/another-repository", { contents: "write", pull_requests: "write" }, false);
  });
  it("accepts only the exact authenticated successor attempt", async () => {
    const identity = claims({ run_attempt: "2" }); oidc.verify.mockResolvedValue(identity);
    const { broker } = harness();
    await expect(broker.exchange(sessionBody(request("review", {}, identity)))).resolves.toMatchObject({ session_attestation: { run_attempt: 2 } });
    await expect(broker.exchange(sessionBody(request("review")))).rejects.toThrow("broker_session_attestation_invalid");
  });
  it.each(["workflow_dispatch", "issue_comment", "pull_request_review_comment", "pull_request"])("retains authenticated %s source workflow event", async event => {
    const identity = claims({ event_name: event, runner_environment: "self-hosted" }); oidc.verify.mockResolvedValue(identity);
    const { broker } = harness();
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request("review", {}, identity) })).resolves.toMatchObject({ capability: "review_publish" });
  });
  it.each([null, { id: 987654321, fork: true }, { id: 999, fork: false }])("rejects unavailable, fork or different repository %#", async info => {
    const { broker, github } = harness(); github.repositoryInfo.mockResolvedValue(info as never);
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request() })).rejects.toThrow("broker_repository_rejected");
    expect(github.capabilityToken).not.toHaveBeenCalled();
  });
  it("rejects missing installation and invalid OIDC before target effects", async () => {
    const { broker, github } = harness(); github.installationFor.mockResolvedValue(null as never);
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request() })).rejects.toThrow("broker_installation_unavailable");
    expect(github.installationToken).not.toHaveBeenCalled();
    oidc.verify.mockRejectedValue(new Error("invalid OIDC"));
    await expect(broker.exchange({ oidc_token: "signed", session_attestation: request() })).rejects.toThrow("invalid OIDC");
  });
  it("refuses stale head before enrollment and keeps release-only owner cleanup finite", async () => {
    const { broker, github } = harness(); github.pullRequestHead.mockResolvedValue("c".repeat(40));
    await expect(broker.exchange(sessionBody())).rejects.toThrow("broker_session_head_rejected");
    expect(github.sessionAlreadyKnown).not.toHaveBeenCalled();
    const result = await broker.exchange({ ...sessionBody(), release_only: true });
    expect(result).toEqual({ token: "scoped-token", capability: "review_session", release_only: true, app_id: 12345, reservation_owner: { run_id: "10000000001", head_sha: SHA } });
    expect(result.session_grant).toBeUndefined(); expect(github.sessionAlreadyKnown).not.toHaveBeenCalled();
  });
  it("requires signing key only for current grant issuance", async () => {
    const { broker, env } = harness(); env.REVIEWSENSEI_SIGNING_KEY = "";
    await expect(broker.exchange(sessionBody())).rejects.toThrow("configuration_unavailable");
  });
  it.each([undefined, null, "", "0", "-1", "1.5", " 12345", "12345 ", "0012345", "9007199254740992", true, 12345])("refuses invalid configured App ID%j before authentication or token issuance", async value => {
    const { broker, env, github } = harness();
    env.GITHUB_APP_ID = value as string;
    await expect(broker.exchange(sessionBody())).rejects.toThrow("configuration_unavailable");
    expect(oidc.verify).not.toHaveBeenCalled();
    expect(github.installationFor).not.toHaveBeenCalled();
    expect(github.capabilityToken).not.toHaveBeenCalled();
    expect(github.sessionAlreadyKnown).not.toHaveBeenCalled();
  });
  it.each(commandParityCases)("matches current Python command grammar for $body", async ({ body, accepted }) => {
    const { broker, github } = harness();
    const identity = claims({ event_name: "issue_comment" }); oidc.verify.mockResolvedValue(identity);
    github.feedbackComment.mockResolvedValue({ kind: "issue", comment_id: 13579, updated_at: "2026-10-09T00:00:00Z",
      author: "octocat", author_id: 12345678, association: "OWNER", body, body_sha256: hash(body) });
    const authority = request("command", {}, identity); authority.source.body_sha256 = hash(body);
    const result = broker.exchange(sessionBody(authority));
    if (accepted) await expect(result).resolves.toMatchObject({ session_grant: expect.stringMatching(/^sg1\./), session_attestation: { purpose: "command", actor: { login: "octocat", type: "User", association: "OWNER" } } });
    else await expect(result).rejects.toThrow("broker_session_actor_rejected");
  });
  it("does not store or rate limit exact current grants within the assertion window", async () => {
    const { broker } = harness();
    const first = await broker.exchange(sessionBody());
    for (let index = 0; index < 11; index++) expect(await broker.verifySessionGrant(first.session_grant, first.session_attestation)).toEqual(first.session_attestation);
  });
  it("revalidates current runtime on grant verification and preserves opaque grant secrecy", async () => {
    const { broker, github } = harness(); const result = await broker.exchange(sessionBody());
    github.publicWorkflowRuntimeShas.mockResolvedValue({ commitSha: "c".repeat(40), refSha: "c".repeat(40) });
    await expect(broker.verifySessionGrant(result.session_grant, result.session_attestation)).rejects.toThrow("broker_workflow_rejected");
  });
});

describe("opt-in v4 current observations", () => {
  function observedHarness() {
    const h = harness();
    const observed = {
      installationObservationFor: vi.fn(async () => ({ id: 2468, repository_selection: "selected", permissions: { metadata: "read", pull_requests: "write" } })),
      capabilityTokenObservation: vi.fn(async () => ({ token: "observed-token", permissions: { metadata: "read", pull_requests: "write" } })),
    };
    Object.assign(h.github, observed);
    h.github.repositoryInfo.mockResolvedValue({ id: 987654321, fork: false, full_name: "acme/widgets", owner_id: 987, owner_login: "acme" } as never);
    return { ...h, observed };
  }
  it("MAC-binds only source-derived observations without additional reads or grants", async () => {
    const { broker, github, observed } = observedHarness();
    const result = await broker.exchange(sessionBody(request("review", { version: 4 })));
    expect(result.session_attestation?.version).toBe(4);
    expect(result.session_attestation?.current_observation).toEqual({
      schema_version: "current-observation-v1",
      repository: { id: 987654321, full_name: "acme/widgets", owner_id: 987, owner_login: "acme", fork: false },
      installation: { id: 2468, repository_selection: "selected", repository_access: true, permissions: { metadata: "read", pull_requests: "write" } },
      capability: { name: "review_session", permissions: { metadata: "read", pull_requests: "write" } },
      actor: { id: 12345678, login: "octocat", source_association: null },
    });
    expect(observed.installationObservationFor).toHaveBeenCalledTimes(1);
    expect(observed.capabilityTokenObservation).toHaveBeenCalledTimes(1);
    expect(github.installationFor).not.toHaveBeenCalled();
    expect(github.capabilityToken).not.toHaveBeenCalled();
    expect(github.repositoryInfo).toHaveBeenCalledTimes(1);
    expect(github.feedbackComment).not.toHaveBeenCalled();
    expect(await broker.verifySessionGrant(result.session_grant, result.session_attestation)).toEqual(result.session_attestation);
    for (const mutate of [
      (a: any) => a.current_observation.actor.id++,
      (a: any) => a.current_observation.repository.owner_id++,
      (a: any) => a.current_observation.installation.id++,
      (a: any) => a.run_id = "10000000002",
      (a: any) => a.head_sha = "c".repeat(40),
      (a: any) => a.current_observation.installation.repository_selection = "all",
    ]) {
      const altered = structuredClone(result.session_attestation); mutate(altered);
      await expect(broker.verifySessionGrant(result.session_grant, altered)).rejects.toThrow();
    }
  });
  it("keeps authenticated numeric source author and association distinct from permission", async () => {
    oidc.verify.mockResolvedValue(claims({ event_name: "issue_comment" }));
    const { broker } = observedHarness();
    const result = await broker.exchange(sessionBody(request("command", { version: 4 })));
    expect(result.session_attestation?.current_observation?.actor).toEqual({ id: 12345678, login: "octocat", source_association: "OWNER" });
    expect(JSON.stringify(result.session_attestation)).not.toContain("permission_epoch");
    expect(JSON.stringify(result.session_attestation)).not.toContain("actor_permission");
  });
  it.each(["missing owner", "foreign owner", "fork", "foreign repository", "missing installation permissions", "permission downgrade", "extra effective permission", "missing actor", "unknown selection"])("refuses incomplete/contradictory v4: %s", async mode => {
    const { broker, github, observed } = observedHarness();
    if (mode === "missing owner") github.repositoryInfo.mockResolvedValue({ id: 987654321, fork: false });
    if (mode === "foreign owner") github.repositoryInfo.mockResolvedValue({ id: 987654321, fork: false, full_name: "acme/widgets", owner_id: 987, owner_login: "other" } as never);
    if (mode === "fork") github.repositoryInfo.mockResolvedValue({ id: 987654321, fork: true });
    if (mode === "foreign repository") github.repositoryInfo.mockResolvedValue({ id: 7, fork: false });
    if (mode === "missing installation permissions") observed.installationObservationFor.mockResolvedValue({ id: 2468, repository_selection: "selected" } as never);
    if (mode === "permission downgrade") observed.installationObservationFor.mockResolvedValue({ id: 2468, repository_selection: "selected", permissions: { metadata: "read", pull_requests: "read" } });
    if (mode === "extra effective permission") observed.capabilityTokenObservation.mockResolvedValue({ token: "observed-token", permissions: { metadata: "read", pull_requests: "write", checks: "write" } } as never);
    if (mode === "missing actor") oidc.verify.mockResolvedValue(claims({ actor_id: undefined }));
    if (mode === "unknown selection") observed.installationObservationFor.mockResolvedValue({ id: 2468, repository_selection: "unknown", permissions: { metadata: "read", pull_requests: "write" } });
    await expect(broker.exchange(sessionBody(request("review", { version: 4 })))).rejects.toThrow();
    expect(github.capabilityToken).not.toHaveBeenCalled();
    expect(github.sessionAlreadyKnown).not.toHaveBeenCalled();
  });
  it.each(["other capability", "release", "missing session", "injected observation", "foreign actor", "wrong head"])("never downgrades unsupported v4: %s", async mode => {
    const { broker, github } = observedHarness();
    const body: any = sessionBody(request(mode === "foreign actor" ? "command" : "review", { version: 4 }));
    if (mode === "other capability") { body.capability = "review_publish"; delete body.session; }
    if (mode === "release") body.release_only = true;
    if (mode === "missing session") delete body.session;
    if (mode === "injected observation") body.session_attestation.current_observation = {};
    if (mode === "foreign actor") { oidc.verify.mockResolvedValue(claims({ event_name: "issue_comment", actor_id: 99 })); }
    if (mode === "wrong head") github.pullRequestHead.mockResolvedValue("c".repeat(40));
    await expect(broker.exchange(body)).rejects.toThrow();
    expect(github.sessionAlreadyKnown).not.toHaveBeenCalled();
  });
});
