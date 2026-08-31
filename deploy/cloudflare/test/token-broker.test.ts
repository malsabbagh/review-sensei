import { beforeEach, describe, expect, it, vi } from "vitest";

const oidc = vi.hoisted(() => ({ verify: vi.fn() }));

vi.mock("../src/oidc", () => ({ verifyOidcAssertion: oidc.verify }));

import type { WorkerEnv } from "../src/env";
import { TokenBroker } from "../src/token-broker";

const SHA = "a".repeat(40);
const TAG = "v4";

function claims(overrides: Record<string, unknown> = {}) {
  return {
    iss: "https://token.actions.githubusercontent.com",
    aud: "sts.reviewsensei.dev",
    exp: 1_800_000_000,
    iat: 1_700_000_000,
    sub: "repo:acme/widgets:pull_request",
    jti: "assertion-1",
    repository: "acme/widgets",
    repository_owner: "acme",
    repository_id: 987654321,
    actor: "octocat",
    actor_id: 12345678,
    event_name: "pull_request",
    workflow: "ReviewSensei review",
    workflow_ref: "acme/widgets/.github/workflows/review-sensei-review.yml@refs/heads/main",
    workflow_sha: SHA,
    job_workflow_ref: `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${SHA}`,
    job_workflow_sha: SHA,
    ref: "refs/pull/7/merge",
    sha: SHA,
    run_id: "10000000001",
    run_number: "42",
    run_attempt: "1",
    runner_environment: "github-hosted",
    ...overrides,
  };
}

function harness(ledgerState: "accepted" | "replay" | "rate_limited" = "accepted") {
  const ledgerFetch = vi.fn(async (_url: string, init?: RequestInit) => {
    const request = JSON.parse(String(init?.body)) as { action?: string };
    const state = request.action === "admit" ? "accepted" : ledgerState;
    return new Response(JSON.stringify({ state }), {
      headers: { "content-type": "application/json" },
    });
  });
  const env = {
    PUBLIC_WORKFLOW_TAG: TAG,
    PUBLIC_WORKFLOW_SHA: SHA,
    BROKER_LEDGER: {
      idFromName: vi.fn(() => ({ name: "broker" })),
      get: vi.fn(() => ({ fetch: ledgerFetch })),
    },
  } as unknown as WorkerEnv;
  const github = {
    repositoryInfo: vi.fn(async () => ({ id: 987654321, fork: false })),
    installationFor: vi.fn(async () => 2468),
    capabilityToken: vi.fn(async () => "ghs_scoped_token"),
  };
  const broker = new TokenBroker(env, github as never);
  return { broker, github, ledgerFetch };
}

beforeEach(() => {
  oidc.verify.mockReset();
  oidc.verify.mockResolvedValue(claims());
});

describe("token broker authorization", () => {
  it.each([
    [undefined, "review_publish", { pull_requests: "write" }],
    ["review_publish", "review_publish", { pull_requests: "write" }],
    ["inline_reply", "inline_reply", { pull_requests: "write" }],
    ["issue_reply", "issue_reply", { issues: "write" }],
    ["learning_write", "learning_write", { contents: "write", pull_requests: "write" }],
  ])("maps %s to the least-privilege installation capability", async (requested, returned, permissions) => {
    const { broker, github } = harness();
    const result = await broker.exchange({ oidc_token: "signed-jwt", capability: requested });

    expect(result).toEqual({ token: "ghs_scoped_token", capability: returned });
    expect(github.capabilityToken).toHaveBeenCalledWith(2468, "acme/widgets", permissions);
  });

  it.each([
    ["different reusable workflow", { job_workflow_ref: `attacker/repo/.github/workflows/review-sensei-run.yml@refs/tags/${TAG}` }, "broker_workflow_rejected"],
    ["mutable public workflow tag", { job_workflow_ref: `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/${TAG}` }, "broker_workflow_rejected"],
    ["different reusable workflow SHA", { job_workflow_sha: "b".repeat(40) }, "broker_workflow_rejected"],
    ["self-hosted automatic PR", { runner_environment: "self-hosted" }, "broker_runner_rejected"],
    ["unsupported event", { event_name: "push" }, "broker_event_rejected"],
    ["repository owner mismatch", { repository_owner: "someone-else" }, "broker_repository_rejected"],
  ])("rejects %s", async (_name, override, message) => {
    oidc.verify.mockResolvedValue(claims(override));
    const { broker, github } = harness();
    await expect(broker.exchange({ oidc_token: "signed-jwt" })).rejects.toThrow(message);
    expect(github.capabilityToken).not.toHaveBeenCalled();
  });

  it("accepts a configured v3 SHA during the migration window", async () => {
    const legacySha = "c".repeat(40);
    const legacyEnv = {
      PUBLIC_WORKFLOW_TAG: TAG,
      PUBLIC_WORKFLOW_SHA: SHA,
      PUBLIC_WORKFLOW_LEGACY_SHAS: legacySha,
      BROKER_LEDGER: {
        idFromName: vi.fn(() => ({ name: "broker" })),
        get: vi.fn(() => ({ fetch: async () => new Response(JSON.stringify({ state: "accepted" })) })),
      },
    } as unknown as WorkerEnv;
    const legacyBroker = new TokenBroker(legacyEnv, {
      repositoryInfo: vi.fn(async () => ({ id: 987654321, fork: false })),
      installationFor: vi.fn(async () => 2468),
      capabilityToken: vi.fn(async () => "ghs_scoped_token"),
    } as never);
    oidc.verify.mockResolvedValue(
      claims({
        job_workflow_ref: `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${legacySha}`,
        job_workflow_sha: legacySha,
      }),
    );
    await expect(legacyBroker.exchange({ oidc_token: "signed-jwt" })).resolves.toMatchObject({
      capability: "review_publish",
    });
  });

  it.each(["workflow_dispatch", "issue_comment", "pull_request_review_comment"])(
    "allows the trusted manual %s event on a self-hosted runner",
    async (eventName) => {
      oidc.verify.mockResolvedValue(claims({ event_name: eventName, runner_environment: "self-hosted" }));
      const { broker } = harness();
      await expect(broker.exchange({ oidc_token: "signed-jwt" })).resolves.toMatchObject({
        capability: "review_publish",
      });
    },
  );

  it.each([
    ["missing repository", null],
    ["fork repository", { id: 987654321, fork: true }],
    ["repository id mismatch", { id: 111, fork: false }],
  ])("rejects a %s", async (_name, repositoryInfo) => {
    const { broker, github } = harness();
    github.repositoryInfo.mockResolvedValue(repositoryInfo);
    await expect(broker.exchange({ oidc_token: "signed-jwt" })).rejects.toThrow(
      "broker_repository_rejected",
    );
    expect(github.installationFor).not.toHaveBeenCalled();
  });

  it("rejects a repository without an App installation", async () => {
    const { broker, github } = harness();
    github.installationFor.mockResolvedValue(null);
    await expect(broker.exchange({ oidc_token: "signed-jwt" })).rejects.toThrow(
      "broker_installation_unavailable",
    );
    expect(github.capabilityToken).not.toHaveBeenCalled();
  });

  it.each([
    ["replay", "broker_replay"],
    ["rate_limited", "broker_rate_limited"],
  ] as const)("rejects a ledger %s decision before token issuance", async (state, message) => {
    const { broker, github } = harness(state);
    await expect(broker.exchange({ oidc_token: "signed-jwt" })).rejects.toThrow(message);
    expect(github.repositoryInfo).not.toHaveBeenCalled();
    expect(github.installationFor).not.toHaveBeenCalled();
    expect(github.capabilityToken).not.toHaveBeenCalled();
  });

  it("binds the replay scope to repository, exact workflow ref, and capability", async () => {
    const { broker, ledgerFetch } = harness();
    await broker.exchange({ oidc_token: "signed-jwt", capability: "issue_reply" });
    const request = ledgerFetch.mock.calls[1][1] as RequestInit;
    expect(JSON.parse(request.body as string)).toEqual({
      action: "claim",
      jti: "assertion-1:issue_reply",
      scope: `987654321:12345678:${SHA}:issue_reply`,
    });
  });

  it("rate-limits unauthenticated admission before OIDC or GitHub work", async () => {
    const { broker, github, ledgerFetch } = harness();
    ledgerFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ state: "rate_limited" })),
    );

    await expect(
      broker.exchange({ oidc_token: "forged-jwt" }, "203.0.113.7"),
    ).rejects.toThrow("broker_rate_limited");
    expect(oidc.verify).not.toHaveBeenCalled();
    expect(github.repositoryInfo).not.toHaveBeenCalled();
    const request = ledgerFetch.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(request.body as string)).toMatchObject({
      action: "admit",
      scope: "preauth:203.0.113.7",
    });
  });
});
