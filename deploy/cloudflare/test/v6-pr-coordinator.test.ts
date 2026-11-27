import { afterEach, describe, expect, it, vi } from "vitest";
import contract from "../../../src/review_sensei/schemas/v6-coordinator-contract.json";
import type { WorkerEnv } from "../src/env";
import type {
  LiveCheckRun,
  LiveIssueComment,
  LiveIssueCommentPage,
  LivePullRequestReview,
  LivePullRequestReviewComment,
  PullRequestBinding,
  PublicWorkflowRuntimeShas,
  RevocationResult,
  WorkflowRunAttemptInfo,
} from "../src/github-api";
import { resetOidcJwksCacheForTesting, type OidcClaims } from "../src/oidc";
import { TokenBroker } from "../src/token-broker";
import {
  V6_ATTESTATION_HKDF_INFO,
  V6_ATTESTATION_HKDF_SALT,
  V6_ESCROW_HKDF_INFO,
  V6_ESCROW_HKDF_SALT,
  V6_ESCROW_IV_BYTES,
  V6EscrowCrypto,
  canonicalJsonString,
  redactSensitiveText,
  sha256Hex,
} from "../src/v6-escrow-crypto";
import {
  DurableObjectSqlV6CoordinatorStore,
  InMemoryV6CoordinatorStore,
  V6_MINTING_STALE_MS,
  V6_RESERVED_STALE_MS,
  V6PrCoordinator,
  V6PrCoordinatorEngine,
  buildCanonicalSlotKey,
  canonicalSlotHost,
  serializeV6RouteResponseBody,
  type V6AuditEvent,
  type V6Clock,
  type V6ProviderAdapter,
  type V6RouteResult,
} from "../src/v6-pr-coordinator";
import worker from "../src/worker";

const SIGNING_KEY = "test-v6-signing-key-material-32-bytes";
const WORKFLOW_SHA = "a".repeat(40);
const HEAD_SHA_1 = "1".repeat(40);
const HEAD_SHA_2 = "2".repeat(40);
const PILOT_REPO = "acme/pilot-repo";
const NON_PILOT_REPO = "acme/regular-repo";
const WORKFLOW_REF =
  "malsabbagh/review-sensei/.github/workflows/review-sensei-v6.yml@refs/tags/v6-pilot";

function baseEnv(overrides: Partial<WorkerEnv> = {}): WorkerEnv {
  return {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused-by-mock-provider",
    GITHUB_APP_WEBHOOK_SECRET: "whsec",
    PUBLIC_WORKFLOW_TAG: "v5",
    REVIEWSENSEI_SIGNING_KEY: SIGNING_KEY,
    V6_PILOT_REPOSITORIES: PILOT_REPO,
    V6_PILOT_WORKFLOW_REF: WORKFLOW_REF,
    ...overrides,
  };
}

function makeClaims(overrides: Partial<OidcClaims> = {}): OidcClaims {
  return {
    iss: "https://token.actions.githubusercontent.com",
    aud: "sts.reviewsensei.dev",
    exp: 1_795_000_600,
    iat: 1_795_000_000,
    sub: `repo:${PILOT_REPO}:pull_request`,
    jti: "jti-1",
    repository: PILOT_REPO,
    repository_owner: "acme",
    repository_id: 987654321,
    actor: "octocat",
    actor_id: 42,
    event_name: "pull_request",
    workflow: "ReviewSensei v6",
    workflow_ref: `${PILOT_REPO}/.github/workflows/caller.yml@refs/heads/main`,
    workflow_sha: "c".repeat(40),
    job_workflow_ref: WORKFLOW_REF,
    job_workflow_sha: WORKFLOW_SHA,
    ref: "refs/pull/42/merge",
    sha: HEAD_SHA_1,
    run_id: "10001",
    run_number: "1",
    run_attempt: "1",
    runner_environment: "github-hosted",
    ...overrides,
  };
}

class MutableClock implements V6Clock {
  constructor(
    public now = 1_795_000_000_000,
    public trusted = true,
  ) {}

  nowMs(): number {
    return this.now;
  }

  isTrusted(): boolean {
    return this.trusted;
  }
}

interface FakeProviderCalls {
  mints: number;
  revocations: Array<{ token: string }>;
  prReads: number;
  commentReads: number;
  runAttemptReads: number;
  writes: number;
}

class FakeV6Provider implements V6ProviderAdapter {
  readonly calls: FakeProviderCalls = {
    mints: 0,
    revocations: [],
    prReads: 0,
    commentReads: 0,
    commentListReads: 0,
    runAttemptReads: 0,
    writes: 0,
  };

  claimsByToken = new Map<string, OidcClaims>();
  runtimeShas: PublicWorkflowRuntimeShas = {
    commitSha: WORKFLOW_SHA,
    refSha: WORKFLOW_SHA,
  };
  installationId: number | null = 67890;
  repoInfo: { id: number; fork: boolean } | null = { id: 987654321, fork: false };
  prBindings = new Map<number, PullRequestBinding | null>();
  runAttempts = new Map<string, WorkflowRunAttemptInfo | null>();
  comments = new Map<number, LiveIssueComment | null>();
  reviews = new Map<number, LivePullRequestReview | null>();
  reviewComments = new Map<number, LivePullRequestReviewComment | null>();
  checkRuns = new Map<number, LiveCheckRun | null>();
  commentsSinceOverride: LiveIssueCommentPage | null = null;
  issueCommentsSinceArgs: string[] = [];
  commentCreatedAtMs = new Map<number, number>();
  botLogin = "reviewsensei[bot]";
  mintQueue: Array<() => Promise<{ token: string; permissions: Record<string, string>; expiresAt: string; expiresAtMs: number }>> = [];
  revokeQueue: Array<(token: string) => Promise<RevocationResult>> = [];
  prReadHook: ((pr: number) => Promise<void>) | undefined;
  private tokenSeq = 0;

  constructor(private readonly clock: MutableClock) {
    this.prBindings.set(42, {
      head_sha: HEAD_SHA_1,
      base_repo_id: 987654321,
      state: "open",
      head_repo_id: 987654321,
      is_fork_pr: false,
    });
    this.prBindings.set(43, {
      head_sha: HEAD_SHA_1,
      base_repo_id: 987654321,
      state: "open",
      head_repo_id: 987654321,
      is_fork_pr: false,
    });
  }

  registerOidc(token: string, claims: OidcClaims): string {
    this.claimsByToken.set(token, claims);
    return token;
  }

  async verifyOidc(token: string): Promise<OidcClaims> {
    const found = this.claimsByToken.get(token);
    if (!found) {
      throw new Error("oidc_signature_invalid");
    }
    return found;
  }

  async workflowRuntimeShas(): Promise<PublicWorkflowRuntimeShas> {
    return this.runtimeShas;
  }

  async appBotLogin(): Promise<string> {
    return this.botLogin;
  }

  async installationFor(): Promise<number | null> {
    return this.installationId;
  }

  async readOnlyToken(): Promise<string> {
    return "ghs_internal_readonly_never_disclosed";
  }

  async repositoryInfo(): Promise<{ id: number; fork: boolean } | null> {
    return this.repoInfo;
  }

  async pullRequestBinding(_repo: string, pullRequest: number): Promise<PullRequestBinding | null> {
    this.calls.prReads += 1;
    if (this.prReadHook) {
      await this.prReadHook(pullRequest);
    }
    return this.prBindings.get(pullRequest) ?? null;
  }

  async workflowRunAttempt(
    _repo: string,
    runId: string,
    attempt: string,
  ): Promise<WorkflowRunAttemptInfo | null> {
    this.calls.runAttemptReads += 1;
    const key = `${runId}:${attempt}`;
    if (this.runAttempts.has(key)) {
      return this.runAttempts.get(key) ?? null;
    }
    return {
      run_id: runId,
      run_attempt: Number(attempt),
      status: "in_progress",
      conclusion: null,
      head_sha: HEAD_SHA_1,
    };
  }

  async issueComment(
    _repo: string,
    _pullRequest: number,
    commentId: number,
  ): Promise<LiveIssueComment | null> {
    this.calls.commentReads += 1;
    return this.comments.get(commentId) ?? null;
  }

  async issueCommentsSince(
    _repo: string,
    _pullRequest: number,
    sinceIso: string,
    _token: string,
  ): Promise<LiveIssueCommentPage> {
    this.calls.commentListReads += 1;
    this.issueCommentsSinceArgs.push(sinceIso);
    if (this.commentsSinceOverride !== null) {
      return this.commentsSinceOverride;
    }
    const sinceMs = Date.parse(sinceIso);
    const list: LiveIssueComment[] = [];
    for (const [id, comment] of this.comments.entries()) {
      if (comment !== null) {
        const createdAtMs = this.commentCreatedAtMs.get(id);
        if (createdAtMs !== undefined && Number.isFinite(sinceMs) && createdAtMs < sinceMs) {
          continue;
        }
        list.push({ ...comment, id });
      }
    }
    return { comments: list, truncated: false };
  }

  async pullRequestReview(
    _repo: string,
    _pullRequest: number,
    reviewId: number,
  ): Promise<LivePullRequestReview | null> {
    return this.reviews.get(reviewId) ?? null;
  }

  async pullRequestReviewComment(
    _repo: string,
    _pullRequest: number,
    commentId: number,
  ): Promise<LivePullRequestReviewComment | null> {
    return this.reviewComments.get(commentId) ?? null;
  }

  async checkRun(_repo: string, checkRunId: number): Promise<LiveCheckRun | null> {
    return this.checkRuns.get(checkRunId) ?? null;
  }

  async mintInstallationToken(
    _installationId: number,
    _repository: string,
    permissions: Record<string, "read" | "write">,
  ) {
    this.calls.mints += 1;
    const scripted = this.mintQueue.shift();
    if (scripted) {
      return scripted();
    }
    this.tokenSeq += 1;
    const expiresAtMs = this.clock.nowMs() + 3_600_000;
    return {
      token: `ghs_minted_secret_token_${this.tokenSeq}`,
      permissions: { metadata: "read", ...permissions },
      expiresAt: new Date(expiresAtMs).toISOString(),
      expiresAtMs,
    };
  }

  async revokeInstallationToken(token: string): Promise<RevocationResult> {
    this.calls.revocations.push({ token });
    const scripted = this.revokeQueue.shift();
    if (scripted) {
      return scripted(token);
    }
    return {
      status: 204,
      kind: "http_204",
      retryAfterSeconds: null,
      rateLimitResetEpoch: null,
      isRateLimit: false,
    };
  }
}

function createHarness(envOverrides: Partial<WorkerEnv> = {}) {
  const env = baseEnv(envOverrides);
  const clock = new MutableClock();
  const store = new InMemoryV6CoordinatorStore();
  const provider = new FakeV6Provider(clock);
  const auditEvents: V6AuditEvent[] = [];
  let resSeq = 0;

  const makeEngine = () =>
    new V6PrCoordinatorEngine({
      store,
      provider,
      clock,
      sleep: async (ms) => {
        clock.now += ms;
      },
      audit: {
        emit(event) {
          auditEvents.push(event);
        },
      },
      generateReservationId: () => {
        resSeq += 1;
        return `res-${resSeq}`;
      },
    });

  const engine = makeEngine();
  provider.registerOidc("oidc-run-1", makeClaims({ run_id: "10001", run_attempt: "1" }));
  provider.registerOidc("oidc-run-1-att2", makeClaims({ run_id: "10001", run_attempt: "2" }));
  provider.registerOidc("oidc-run-2", makeClaims({ run_id: "20002", run_attempt: "1" }));

  const slotKey42 = buildCanonicalSlotKey({
    host: "github.com",
    appId: 12345,
    installationId: 67890,
    baseRepoId: 987654321,
    pullRequest: 42,
  });

  return { env, clock, store, provider, engine, makeEngine, auditEvents, slotKey42 };
}

async function buildLedgerComment(
  crypto: V6EscrowCrypto,
  payload: Record<string, unknown>,
  login = "reviewsensei[bot]",
  userType = "Bot",
): Promise<LiveIssueComment> {
  const canonical = canonicalJsonString(payload);
  const sha = await sha256Hex(canonical);
  return {
    id: 9001,
    body: `<!-- reviewsensei:v6:record sha256=${sha} payload=${canonical} -->`,
    login,
    userType,
    association: "NONE",
  };
}

function assertZeroPlaintextSecrets(
  store: InMemoryV6CoordinatorStore,
  auditEvents: V6AuditEvent[],
  disclosedTokens: string[],
  nonAuthorizedResponses: Array<V6RouteResult | { status: number; body: unknown; headers?: Record<string, string> }> = [],
): void {
  const serializedStore = JSON.stringify(store.snapshot());
  const serializedAudit = JSON.stringify(auditEvents);
  for (const tok of disclosedTokens) {
    expect(serializedStore).not.toContain(tok);
    expect(serializedAudit).not.toContain(tok);
  }
  expect(serializedStore).not.toContain("ghs_");
  expect(serializedAudit).not.toContain("ghs_");
  expect(serializedStore).not.toContain("oidc-");
  expect(serializedAudit).not.toContain("oidc-");
  for (const res of nonAuthorizedResponses) {
    const serializedRes = JSON.stringify({
      status: res.status,
      body: res.body,
      headers: "headers" in res ? res.headers ?? {} : {},
    });
    for (const tok of disclosedTokens) {
      expect(serializedRes).not.toContain(tok);
    }
    expect(serializedRes).not.toContain("ghs_");
    expect(serializedRes).not.toContain("ghp_");
    expect(serializedRes).not.toContain("Bearer ");
    expect(serializedRes).not.toContain("oidc-");
  }
  for (const slot of Object.values(store.snapshot())) {
    for (const banned of contract.coordinator_storage.never_keep_fields) {
      expect(slot).not.toHaveProperty(banned);
    }
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  resetOidcJwksCacheForTesting();
});

describe("v6 escrow crypto & redaction", () => {
  it("fails closed on missing or whitespace signing key", () => {
    expect(() => new V6EscrowCrypto(undefined)).toThrow("configuration_unavailable");
    expect(() => new V6EscrowCrypto("")).toThrow("configuration_unavailable");
    expect(() => new V6EscrowCrypto("   ")).toThrow("configuration_unavailable");
  });

  it("matches frozen HKDF constants, uses fresh 12-byte IVs, and enforces AAD & key separation", async () => {
    expect(V6_ESCROW_HKDF_SALT).toBe(contract.coordinator_storage.escrow.hkdf_salt);
    expect(V6_ESCROW_HKDF_INFO).toBe(contract.coordinator_storage.escrow.hkdf_info);
    expect(V6_ESCROW_IV_BYTES).toBe(contract.coordinator_storage.escrow.iv_bytes);
    expect(V6_ATTESTATION_HKDF_SALT).toBe(
      contract.coordinator_storage.owner_attestation.hkdf_salt,
    );
    expect(V6_ATTESTATION_HKDF_INFO).toBe(
      contract.coordinator_storage.owner_attestation.hkdf_info,
    );

    const cryptoA = new V6EscrowCrypto(SIGNING_KEY);
    const cryptoRotated = new V6EscrowCrypto("rotated-signing-key-material-32-bytes");
    const aad = {
      slot_key: "v6-pr:github.com:12345:67890:987654321:42",
      generation: 1,
      reservation_id: "res-1",
    };

    const sealed1 = await cryptoA.sealToken("ghs_token_abc", aad);
    const sealed2 = await cryptoA.sealToken("ghs_token_abc", aad);
    expect(sealed1.iv_b64).not.toBe(sealed2.iv_b64);
    expect(Buffer.from(sealed1.iv_b64, "base64").byteLength).toBe(12);
    await expect(cryptoA.unsealToken(sealed1, aad)).resolves.toBe("ghs_token_abc");

    await expect(
      cryptoA.unsealToken(sealed1, { ...aad, generation: 2 }),
    ).rejects.toThrow("v6_lease_escrow_lost");
    await expect(
      cryptoA.unsealToken(sealed1, { ...aad, reservation_id: "res-2" }),
    ).rejects.toThrow("v6_lease_escrow_lost");
    await expect(cryptoRotated.unsealToken(sealed1, aad)).rejects.toThrow(
      "v6_lease_escrow_lost",
    );

    const tuple = {
      slot_key: aad.slot_key,
      generation: 1,
      lease_epoch: 1,
      reservation_id: "res-1",
      run_id: "10001",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    };
    const mac = await cryptoA.signOwnerAttestation(tuple);
    expect(mac).toMatch(/^[0-9a-f]{64}$/);
    await expect(cryptoA.verifyOwnerAttestation(tuple, mac)).resolves.toBe(true);
    await expect(
      cryptoA.verifyOwnerAttestation({ ...tuple, run_attempt: 2 }, mac),
    ).resolves.toBe(false);
    await expect(cryptoRotated.verifyOwnerAttestation(tuple, mac)).resolves.toBe(false);
  });

  it("redacts tokens, bearer headers, JWTs, and token query params", () => {
    const raw =
      "failed Authorization: Bearer ghs_secret12345 and ghp_personal98765 jwt=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig_part_here https://example.test/cb?token=abc123&ok=1&access_token=xyz999";
    const redacted = redactSensitiveText(raw);
    expect(redacted).not.toContain("ghs_secret12345");
    expect(redacted).not.toContain("ghp_personal98765");
    expect(redacted).not.toContain("eyJhbGciOiJSUzI1NiJ9");
    expect(redacted).not.toContain("abc123");
    expect(redacted).not.toContain("xyz999");
    expect(redacted).toContain("ok=1");
  });
});

describe("v6 PR coordinator Phase 4 matrix", () => {
  it("canonicalizes slot hosts and rejects wrong head/repo/workflow/fork before mint", async () => {
    expect(canonicalSlotHost("https://api.github.com")).toBe("github.com");
    expect(canonicalSlotHost("https://api.github.example.com:8443/api/v3")).toBe(
      "api.github.example.com",
    );
    expect(canonicalSlotHost("https://GHE-01.Internal/api/v3")).toBe("ghe-01.internal");

    const { env, engine, provider, store, auditEvents } = createHarness();

    // Wrong head
    const wrongHead = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_2,
      capabilities: ["review_publish"],
    });
    expect(wrongHead.status).toBe(409);
    expect(wrongHead.body.diagnostic).toMatchObject({ code: "v6_lease_head_changed" });

    // Non-pilot repo
    provider.registerOidc(
      "oidc-nonpilot",
      makeClaims({ repository: NON_PILOT_REPO, repository_owner: "acme" }),
    );
    const nonPilot = await engine.handleAcquire(env, {
      oidc_token: "oidc-nonpilot",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(nonPilot.status).toBe(403);
    expect(nonPilot.body.diagnostic).toMatchObject({ code: "v6_lease_unauthorized_tuple" });

    // Wrong workflow ref
    provider.registerOidc(
      "oidc-badwf",
      makeClaims({ job_workflow_ref: "attacker/repo/.github/workflows/x.yml@refs/tags/v6" }),
    );
    const badWf = await engine.handleAcquire(env, {
      oidc_token: "oidc-badwf",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(badWf.status).toBe(403);
    expect(badWf.body.diagnostic).toMatchObject({ code: "v6_lease_unauthorized_tuple" });

    // Fork PR rejected before mint
    provider.prBindings.set(42, {
      head_sha: HEAD_SHA_1,
      base_repo_id: 987654321,
      state: "open",
      head_repo_id: 555555,
      is_fork_pr: true,
    });
    const forkPr = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(forkPr.status).toBe(403);
    expect(forkPr.body.diagnostic).toMatchObject({ code: "v6_lease_unauthorized_tuple" });

    expect(provider.calls.mints).toBe(0);
    assertZeroPlaintextSecrets(store, auditEvents, [], [wrongHead, nonPilot, badWf, forkPr]);
  });

  it("reuses active escrowed token for same owner (1 mint) and blocks contenders + separates PRs", async () => {
    const { env, engine, provider, store, auditEvents, slotKey42 } = createHarness();

    const first = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish", "check_publish"],
    });
    expect(first.status).toBe(200);
    expect(first.body.slot_key).toBe(slotKey42);
    expect(first.body.state).toBe("ACTIVE");
    expect(first.body.generation).toBe(1);
    const token1 = first.body.token as string;
    expect(token1).toBe("ghs_minted_secret_token_1");

    // Same requester retries -> reuses escrowed token, 0 extra mints
    const reuse = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish", "check_publish"],
    });
    expect(reuse.status).toBe(200);
    expect(reuse.body.token).toBe(token1);
    expect(provider.calls.mints).toBe(1);

    const unauthorizedResponses: V6RouteResult[] = [];
    // Alternative run & alternative attempt rejected without token disclosure
    for (const badToken of ["oidc-run-2", "oidc-run-1-att2"]) {
      const denied = await engine.handleAcquire(env, {
        oidc_token: badToken,
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      unauthorizedResponses.push(denied);
      expect(denied.status).toBe(409);
      expect(denied.body.diagnostic).toMatchObject({ code: "v6_lease_active_other_owner" });
      expect(denied.body).not.toHaveProperty("token");
    }

    // Separate PR #43 gets independent slot & token; revoking PR #42 leaves PR #43 ACTIVE
    const pr43 = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 43,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(pr43.status).toBe(200);
    expect(pr43.body.token).toBe("ghs_minted_secret_token_2");

    const rel42 = await engine.handleRelease(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 1,
      lease_epoch: 1,
      reservation_id: first.body.reservation_id,
    });
    expect(rel42.status).toBe(200);
    expect(rel42.body.state).toBe("RECONCILING");
    expect(rel42.body.token_cleared).toBe(true);

    const rec42 = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 1,
        intent_resolutions: [],
      },
    });
    expect(rec42.status).toBe(200);
    expect(rec42.body.state).toBe("IDLE");

    const status43 = await engine.handleStatus(env, {
      oidc_token: "oidc-run-2",
      pull_request: 43,
    });
    expect(status43.status).toBe(200);
    expect(status43.body.state).toBe("ACTIVE");

    assertZeroPlaintextSecrets(
      store,
      auditEvents,
      [token1, pr43.body.token as string],
      [...unauthorizedResponses, rel42, status43],
    );
  });

  it("blocks simultaneous contender in RESERVED and fences delayed stale mint continuation", async () => {
    const { env, engine, provider, store, auditEvents, slotKey42 } = createHarness();

    let resolveSlowMint!: (val: {
      token: string;
      permissions: Record<string, string>;
      expiresAt: string;
      expiresAtMs: number;
    }) => void;
    provider.mintQueue.push(
      () =>
        new Promise((resolve) => {
          resolveSlowMint = resolve;
        }),
    );

    const contenderA = engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });

    // Wait until mint is in flight
    await vi.waitFor(() => expect(provider.calls.mints).toBe(1));

    // Simultaneous contender B is refused immediately
    const contenderB = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(contenderB.status).toBe(409);
    expect(contenderB.body.diagnostic).toMatchObject({ code: "v6_lease_reserved" });

    // Simulate timeout/reconcile fencing the slot while contender A's mint was delayed
    const current = await store.getSlot(slotKey42);
    expect(current?.state).toBe("MINTING");
    await store.putSlot({
      ...current!,
      state: "RECONCILING",
      last_reason_code: "v6_lease_mint_uncertain",
      updated_at_ms: current!.updated_at_ms + 1000,
    });

    // Now delayed mint resolves with a late token: must NOT activate or disclose!
    resolveSlowMint({
      token: "ghs_late_stale_token",
      permissions: { metadata: "read", contents: "read", pull_requests: "write" },
      expiresAt: new Date(1_795_003_600_000).toISOString(),
      expiresAtMs: 1_795_003_600_000,
    });

    const lateResult = await contenderA;
    expect(lateResult.status).toBe(409);
    expect(lateResult.body).not.toHaveProperty("token");
    expect((await store.getSlot(slotKey42))?.state).toBe("RECONCILING");
    expect(provider.calls.revocations).toEqual([{ token: "ghs_late_stale_token" }]);
    assertZeroPlaintextSecrets(store, auditEvents, ["ghs_late_stale_token"], [contenderB, lateResult]);
  });

  it("handles all crash points across fresh engine instances over durable store", async () => {
    const { env, clock, store, makeEngine, provider, slotKey42 } = createHarness();

    // 1. Crash before mint (RESERVED persisted, never reached MINTING)
    await store.putSlot({
      slot_key: slotKey42,
      state: "RESERVED",
      generation: 1,
      lease_epoch: 1,
      reservation_id: "res-crashed",
      owner_run_id: "10001",
      owner_run_attempt: 1,
      owner_head_sha: HEAD_SHA_1,
      owner_workflow_sha: WORKFLOW_SHA,
      owner_job: "ReviewSensei v6",
      owner_comment_id: null,
      owner_record_sha256: null,
      acquired_at_ms: null,
      token_expires_at_ms: null,
      token_cleared: false,
      clock_margin_ms: 300_000,
      encrypted_escrow: null,
      last_reason_code: null,
      updated_at_ms: clock.now,
    });

    const fresh1 = makeEngine();
    // Different run without terminal proof cannot preempt RESERVED
    const blocked = await fresh1.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(blocked.status).toBe(409);
    expect(blocked.body.diagnostic).toMatchObject({ code: "v6_lease_reserved" });
    expect(provider.calls.mints).toBe(0);

    // With non-mint proof (run 10001:1 completed), fresh engine clears RESERVED -> IDLE and acquires
    provider.runAttempts.set("10001:1", {
      run_id: "10001",
      run_attempt: 1,
      status: "completed",
      conclusion: "cancelled",
      head_sha: HEAD_SHA_1,
    });
    const resumed = await fresh1.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(resumed.status).toBe(200);
    expect(resumed.body.generation).toBe(2);
    const activeToken = resumed.body.token as string;

    // 2. Response lost after escrow (ACTIVE persisted): fresh engine returns exact same token to owner, denies second owner
    const fresh2 = makeEngine();
    const sameRetry = await fresh2.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(sameRetry.status).toBe(200);
    expect(sameRetry.body.token).toBe(activeToken);
    expect(provider.calls.mints).toBe(1);

    const otherRetry = await fresh2.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(otherRetry.status).toBe(409);
    expect(otherRetry.body).not.toHaveProperty("token");

    // 3. Mint timeout / uncertain outcome -> RECONCILING, NEVER uses 1-hour-from-timeout
    await fresh2.handleRelease(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 2,
      lease_epoch: 2,
      reservation_id: resumed.body.reservation_id,
    });
    await fresh2.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 2,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 2,
        intent_resolutions: [],
      },
    });
    provider.mintQueue.push(async () => {
      throw new Error("broker_upstream_timeout");
    });
    const timedOut = await fresh2.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(timedOut.status).toBe(503);
    expect(timedOut.body.diagnostic).toMatchObject({ code: "v6_lease_mint_uncertain" });
    expect((await store.getSlot(slotKey42))?.token_expires_at_ms).toBeNull();

    // Advance clock by 3 hours -> unobserved mint MUST NOT auto-expire!
    clock.now += 3 * 3_600_000;
    const after3h = await makeEngine().handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 3,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 3,
        intent_resolutions: [],
      },
    });
    expect(after3h.status).toBe(409);
    expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");
  });

  it("enforces predecessor terminal check on /drain and head-change denial without erasing obligations", async () => {
    const { env, engine, provider, store, slotKey42 } = createHarness();
    const acq = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(acq.status).toBe(200);

    // Predecessor still in_progress -> takeover drain refused
    provider.runAttempts.set("10001:1", {
      run_id: "10001",
      run_attempt: 1,
      status: "in_progress",
      conclusion: null,
      head_sha: HEAD_SHA_1,
    });
    const earlyDrain = await engine.handleDrain(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
    });
    expect(earlyDrain.status).toBe(409);
    expect(earlyDrain.body.diagnostic).toMatchObject({
      code: "v6_lease_predecessor_not_terminal",
    });
    expect(provider.calls.revocations).toHaveLength(0);

    // PR head changes during active lease -> old generation further acquire denied without erasing slot
    provider.prBindings.set(42, {
      head_sha: HEAD_SHA_2,
      base_repo_id: 987654321,
      state: "open",
      head_repo_id: 987654321,
      is_fork_pr: false,
    });
    const oldHeadRetry = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(oldHeadRetry.status).toBe(409);
    expect(oldHeadRetry.body.diagnostic).toMatchObject({ code: "v6_lease_head_changed" });
    expect((await store.getSlot(slotKey42))?.generation).toBe(1);

    // Once predecessor is terminal and anchor exists, drain revokes token and moves to RECONCILING
    provider.runAttempts.set("10001:1", {
      run_id: "10001",
      run_attempt: 1,
      status: "completed",
      conclusion: "failure",
      head_sha: HEAD_SHA_1,
    });
    const crypto = new V6EscrowCrypto(SIGNING_KEY);
    const mac = await crypto.signOwnerAttestation({
      slot_key: slotKey42,
      generation: 1,
      lease_epoch: 1,
      reservation_id: acq.body.reservation_id as string,
      run_id: "10001",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    });
    provider.comments.set(
      9001,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:owner:v1",
        record_id: "rec-owner-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
        operation_id: "op-1",
        run_id: "10001",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
        acquired_at: "2026-11-27T00:00:00Z",
        expires_at: "2026-11-27T01:00:00Z",
        coordinator_attestation_mac: mac,
      }),
    );

    const drainMissingGen = await engine.handleDrain(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      predecessor_anchor_comment_id: 9001,
    });
    expect(drainMissingGen.status).toBe(400);

    const drained = await engine.handleDrain(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      expected_generation: 1,
      predecessor_anchor_comment_id: 9001,
    });
    expect(drained.status).toBe(200);
    expect(drained.body.state).toBe("RECONCILING");
    expect(drained.body.token_cleared).toBe(true);
    expect(provider.calls.revocations).toHaveLength(1);

    // Reconcile completes to IDLE cleanly with verified owner + bounded empty intent inventory
    const reconciled = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 1,
        old_owner_comment_id: 9001,
        intent_resolutions: [],
      },
      anchor_comment_id: 9001,
    });
    expect(reconciled.status).toBe(200);
    expect(reconciled.body.state).toBe("IDLE");
  });

  it("implements every DELETE /installation/token revocation rule including null-expiry and other-4xx guards", async () => {
    // A. 5xx/timeout retries exact token and recovers on 204
    {
      const { env, engine, provider, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      provider.revokeQueue.push(
        async () => ({
          status: null,
          kind: "http_5xx_or_timeout",
          retryAfterSeconds: null,
          rateLimitResetEpoch: null,
          isRateLimit: false,
        }),
        async () => ({
          status: 204,
          kind: "http_204",
          retryAfterSeconds: null,
          rateLimitResetEpoch: null,
          isRateLimit: false,
        }),
      );
      const rel = await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      expect(rel.status).toBe(200);
      expect(rel.body.state).toBe("RECONCILING");
      expect(rel.body.token_cleared).toBe(true);
      expect(provider.calls.revocations.map((r) => r.token)).toEqual([
        acq.body.token,
        acq.body.token,
      ]);
      const rec = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: {
          old_generation: 1,
          intent_resolutions: [],
        },
      });
      expect(rec.status).toBe(200);
      expect(rec.body.state).toBe("IDLE");
    }

    // B. 401 before vs after expires_at + clock_margin_ms
    {
      const { env, clock, engine, provider, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      const expiresAtMs = acq.body.token_expires_at_ms as number;
      provider.revokeQueue.push(async () => ({
        status: 401,
        kind: "http_401",
        retryAfterSeconds: null,
        rateLimitResetEpoch: null,
        isRateLimit: false,
      }));
      const beforeMargin = await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      expect(beforeMargin.status).toBe(409);
      expect(beforeMargin.body.diagnostic).toMatchObject({
        code: "v6_lease_revocation_uncertain",
      });
      expect((await store.getSlot(slotKey42))?.state).toBe("RECONCILING");

      // Advance past token_expires_at_ms + 300_000 and supply verified non-submission evidence
      const cryptoB = new V6EscrowCrypto(SIGNING_KEY);
      const payloadSha = "b".repeat(64);
      provider.comments.set(
        9201,
        await buildLedgerComment(cryptoB, {
          schema: "reviewsensei:v6:intent:v1",
          record_id: "rec-intent-b",
          operation_id: "op-b",
          writer_epoch: 1,
          pr_head_sha: HEAD_SHA_1,
          effect_kind: "pull_request_review",
          payload_sha256: payloadSha,
          target_digest: "d".repeat(64),
          generation: 1,
          target_location: "pr:42",
        }),
      );
      provider.comments.set(
        9202,
        await buildLedgerComment(cryptoB, {
          schema: "reviewsensei:v6:non-submission:v1",
          record_id: "rec-nonsub-b",
          slot_key: slotKey42,
          generation: 1,
          operation_id: "op-b",
          pr_head_sha: HEAD_SHA_1,
          proof_kind: "definitive_github_rejection",
          payload_sha256: payloadSha,
          recorded_at: "2026-11-27T00:05:00Z",
          http_status: 422,
          diagnostic: "rejected",
        }),
      );
      clock.now = expiresAtMs + 300_000;
      const done = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "proved_not_submitted",
        reconciliation_evidence: {
          old_generation: 1,
          intent_resolutions: [
            {
              operation_id: "op-b",
              intent_comment_id: 9201,
              resolution: "proved_not_submitted",
              proof_comment_id: 9202,
            },
          ],
        },
      });
      expect(done.status).toBe(200);
      expect(done.body.state).toBe("IDLE");
    }

    // C. 401 with token_expires_at_ms = null (RS-4DBC1C) -> HOLD + v6_lease_escrow_lost
    {
      const { env, clock, engine, provider, store, slotKey42 } = createHarness();
      const crypto = new V6EscrowCrypto(SIGNING_KEY);
      const escrow = await crypto.sealToken("ghs_null_exp_tok", {
        slot_key: slotKey42,
        generation: 1,
        reservation_id: "res-1",
      });
      await store.putSlot({
        slot_key: slotKey42,
        state: "ACTIVE",
        generation: 1,
        lease_epoch: 1,
        reservation_id: "res-1",
        owner_run_id: "10001",
        owner_run_attempt: 1,
        owner_head_sha: HEAD_SHA_1,
        owner_workflow_sha: WORKFLOW_SHA,
        owner_job: "ReviewSensei v6",
        owner_comment_id: null,
        owner_record_sha256: null,
        acquired_at_ms: null,
        token_expires_at_ms: null,
        token_cleared: false,
        clock_margin_ms: 300_000,
        encrypted_escrow: escrow,
        last_reason_code: null,
        updated_at_ms: clock.now,
      });
      provider.revokeQueue.push(async () => ({
        status: 401,
        kind: "http_401",
        retryAfterSeconds: null,
        rateLimitResetEpoch: null,
        isRateLimit: false,
      }));
      const res = await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: "res-1",
      });
      expect(res.status).toBe(409);
      expect(res.body.diagnostic).toMatchObject({ code: "v6_lease_escrow_lost" });
      expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");
    }

    // D. Permission 403 (no retry -> HOLD) vs Rate-limit 403 / 429 (bounded retry -> RECONCILING)
    {
      const { env, engine, provider, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      provider.revokeQueue.push(async () => ({
        status: 403,
        kind: "permission_403",
        retryAfterSeconds: null,
        rateLimitResetEpoch: null,
        isRateLimit: false,
      }));
      const perm403 = await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      expect(perm403.status).toBe(409);
      expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");
      expect(provider.calls.revocations).toHaveLength(1);
    }
    {
      const { env, clock, engine, provider, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      for (let i = 0; i < 3; i += 1) {
        provider.revokeQueue.push(async () => ({
          status: 429,
          kind: "http_429_or_ratelimit_403",
          retryAfterSeconds: 1,
          rateLimitResetEpoch: Math.floor((clock.now + 1000) / 1000),
          isRateLimit: true,
        }));
      }
      const rateLimited = await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      expect(rateLimited.status).toBe(429);
      expect(provider.calls.revocations).toHaveLength(3);
      expect((await store.getSlot(slotKey42))?.state).toBe("RECONCILING");
    }

    // E. http_other_4xx (RS-A57116) -> HOLD & preserve escrow
    {
      const { env, engine, provider, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      provider.revokeQueue.push(async () => ({
        status: 422,
        kind: "http_other_4xx",
        retryAfterSeconds: null,
        rateLimitResetEpoch: null,
        isRateLimit: false,
      }));
      const other4xx = await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      expect(other4xx.status).toBe(409);
      expect(other4xx.body.diagnostic).toMatchObject({
        code: "v6_lease_revocation_uncertain",
      });
      const heldSlot = await store.getSlot(slotKey42);
      expect(heldSlot?.state).toBe("HOLD");
      expect(heldSlot?.encrypted_escrow).not.toBeNull();
      expect(provider.calls.revocations).toHaveLength(1);
    }

    // F. Untrusted clock & escrow loss with surviving inventory
    {
      const { env, clock, engine, provider, store, slotKey42 } = createHarness();
      clock.trusted = false;
      const untrusted = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      expect(untrusted.status).toBe(409);
      expect(untrusted.body.diagnostic).toMatchObject({ code: "v6_lease_clock_untrusted" });

      clock.trusted = true;
      const expiresAtMs = clock.now + 600_000;
      await store.putSlot({
        slot_key: slotKey42,
        state: "ACTIVE",
        generation: 1,
        lease_epoch: 1,
        reservation_id: "res-1",
        owner_run_id: "10001",
        owner_run_attempt: 1,
        owner_head_sha: HEAD_SHA_1,
        owner_workflow_sha: WORKFLOW_SHA,
        owner_job: "ReviewSensei v6",
        owner_comment_id: null,
        owner_record_sha256: null,
        acquired_at_ms: null,
        token_expires_at_ms: expiresAtMs,
        token_cleared: false,
        clock_margin_ms: 300_000,
        encrypted_escrow: null,
        last_reason_code: null,
        updated_at_ms: clock.now,
      });
      const waitLost = await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: "res-1",
      });
      expect(waitLost.status).toBe(409);
      expect(waitLost.body.diagnostic).toMatchObject({ code: "v6_lease_escrow_lost" });
      expect((await store.getSlot(slotKey42))?.state).toBe("RECONCILING");

      const cryptoAfter = new V6EscrowCrypto(SIGNING_KEY);
      const adoptedSha = "e".repeat(64);
      provider.comments.set(
        9301,
        await buildLedgerComment(cryptoAfter, {
          schema: "reviewsensei:v6:intent:v1",
          record_id: "rec-intent-after",
          operation_id: "op-after",
          writer_epoch: 1,
          pr_head_sha: HEAD_SHA_1,
          effect_kind: "pull_request_review",
          payload_sha256: adoptedSha,
          target_digest: "f".repeat(64),
          generation: 1,
          target_location: "pr:42",
        }),
      );
      provider.reviews.set(5009, {
        id: 5009,
        login: "reviewsensei[bot]",
        userType: "Bot",
        commit_id: HEAD_SHA_1,
        body: `Review body <!-- reviewsensei:v6:effect op=op-after sha256=${adoptedSha} -->`,
        state: "COMMENTED",
        performedViaAppId: 12345,
      });

      clock.now = expiresAtMs + 300_000;
      const resolvedAfterMargin = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "adopted_missing_receipt",
        reconciliation_evidence: {
          old_generation: 1,
          intent_resolutions: [
            {
              operation_id: "op-after",
              intent_comment_id: 9301,
              resolution: "adopted_effect",
              native_effect_kind: "pull_request_review",
              native_effect_id: 5009,
            },
          ],
        },
      });
      expect(resolvedAfterMargin.status).toBe(200);
      expect(resolvedAfterMargin.body.state).toBe("IDLE");
    }
  });

  it("enforces /status absent default, /release ACTIVE-only, /resolve-hold proof, continuity loss, v5 pilot isolation & rollback", async () => {
    const { env, clock, engine, provider, store, slotKey42 } = createHarness();

    // 1. /status when absent returns exact absent_slot_default
    const statusAbsent = await engine.handleStatus(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
    });
    expect(statusAbsent.status).toBe(200);
    expect(statusAbsent.body).toEqual({
      slot_key: slotKey42,
      state: "IDLE",
      generation: 0,
      lease_epoch: 0,
      reservation_id: null,
      owner_run_id: null,
      owner_run_attempt: null,
      owner_head_sha: null,
      owner_workflow_sha: null,
      owner_job: null,
      owner_comment_id: null,
      owner_record_sha256: null,
      acquired_at_ms: null,
      token_expires_at_ms: null,
      token_cleared: false,
      last_reason_code: null,
      updated_at_ms: null,
    });

    // 2. Cold-start coordinator state loss with existing PR owner anchor fails closed
    const crypto = new V6EscrowCrypto(SIGNING_KEY);
    const mac = await crypto.signOwnerAttestation({
      slot_key: slotKey42,
      generation: 3,
      lease_epoch: 3,
      reservation_id: "res-lost",
      run_id: "999",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    });
    provider.comments.set(
      9001,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:owner:v1",
        record_id: "rec-lost",
        slot_key: slotKey42,
        generation: 3,
        lease_epoch: 3,
        reservation_id: "res-lost",
        operation_id: "op-lost",
        run_id: "999",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
        acquired_at: "2026-11-27T00:00:00Z",
        expires_at: "2026-11-27T01:00:00Z",
        coordinator_attestation_mac: mac,
      }),
    );
    const stateLoss = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
      predecessor_anchor_comment_id: 9001,
    });
    expect(stateLoss.status).toBe(409);
    expect(stateLoss.body.diagnostic).toMatchObject({
      code: "v6_lease_slot_continuity_unproved",
    });
    expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");
    expect((await store.getSlot(slotKey42))?.generation).toBe(3);

    // 3. /release in non-ACTIVE states fails closed with 409
    const relHold = await engine.handleRelease(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 3,
      lease_epoch: 3,
      reservation_id: "res-lost",
    });
    expect(relHold.status).toBe(409);
    expect(relHold.body.diagnostic).toMatchObject({
      code: "v6_lease_slot_continuity_unproved",
    });

    // 4. /resolve-hold rejects unverified/unresolved hold, succeeds with verified hold comment
    provider.comments.set(
      9002,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:hold:v1",
        record_id: "rec-hold-1",
        slot_key: slotKey42,
        generation: 3,
        lease_epoch: 3,
        operation_id: "op-lost",
        pr_head_sha: HEAD_SHA_1,
        reason_code: "v6_lease_slot_continuity_unproved",
        missing_evidence: ["slot_anchor"],
        held_at: "2026-11-27T00:05:00Z",
        resolved_by_record_id: null,
      }),
    );
    const unresolvedHold = await engine.handleResolveHold(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 3,
      hold_record_comment_id: 9002,
      resolution_kind: "reconciled_clean",
    });
    expect(unresolvedHold.status).toBe(409);
    expect(unresolvedHold.body.diagnostic).toMatchObject({
      code: "v6_lease_slot_continuity_unproved",
    });

    // With resolved_by_record_id present (and no active unrevoked token), reconciled_clean moves HOLD -> RECONCILING
    provider.comments.set(
      9003,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:hold:v1",
        record_id: "rec-hold-2",
        slot_key: slotKey42,
        generation: 3,
        lease_epoch: 3,
        operation_id: "op-lost",
        pr_head_sha: HEAD_SHA_1,
        reason_code: "v6_lease_slot_continuity_unproved",
        missing_evidence: [],
        held_at: "2026-11-27T00:05:00Z",
        resolved_by_record_id: "rec-receipt-1",
      }),
    );
    // Simulate credential already confirmed cleared (last_reason_code: null) or expired past margin
    await store.putSlot({
      ...(await store.getSlot(slotKey42))!,
      token_expires_at_ms: clock.now - 400_000,
    });
    const resolved = await engine.handleResolveHold(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 3,
      hold_record_comment_id: 9003,
      resolution_kind: "reconciled_clean",
      anchor_comment_id: 9001,
    });
    expect(resolved.status).toBe(200);
    expect(resolved.body.state).toBe("RECONCILING");

    // Immediate /acquire MUST fail closed with 409 v6_lease_reconciling until Gate 2 completes
    const blockedBeforeReconcile = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(blockedBeforeReconcile.status).toBe(409);
    expect(blockedBeforeReconcile.body.diagnostic).toMatchObject({
      code: "v6_lease_reconciling",
    });

    const gen3Complete = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 3,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 3,
        old_owner_comment_id: 9001,
        intent_resolutions: [],
      },
      anchor_comment_id: 9001,
      head_sha: HEAD_SHA_1,
    });
    expect(gen3Complete.status).toBe(200);
    expect(gen3Complete.body.state).toBe("IDLE");

    // 5. Rollback gate blocks /acquire while allowing /drain, /release, /status
    const acqBeforeRollback = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(acqBeforeRollback.status).toBe(200);

    const rollbackEnv = baseEnv({ V6_PILOT_ROLLBACK_REPOSITORIES: PILOT_REPO });
    const blockedAcquire = await engine.handleAcquire(rollbackEnv, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(blockedAcquire.status).toBe(403);
    expect(blockedAcquire.body.diagnostic).toMatchObject({
      code: "v6_lease_rollback_blocked",
    });

    const rollbackStatus = await engine.handleStatus(rollbackEnv, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
    });
    expect(rollbackStatus.status).toBe(200);
    expect(rollbackStatus.body.state).toBe("ACTIVE");

    const rollbackRelease = await engine.handleRelease(rollbackEnv, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 4,
      lease_epoch: 4,
      reservation_id: acqBeforeRollback.body.reservation_id,
    });
    expect(rollbackRelease.status).toBe(200);
    expect(rollbackRelease.body.state).toBe("RECONCILING");
    expect(rollbackRelease.body.token_cleared).toBe(true);

    const rollbackReconcile = await engine.handleCompleteReconcile(rollbackEnv, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 4,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 4,
        intent_resolutions: [],
      },
    });
    expect(rollbackReconcile.status).toBe(200);
    expect(rollbackReconcile.body.state).toBe("IDLE");

    expect(provider.calls.writes).toBe(0);
  });

  it("blocks v5 TokenBroker write capabilities on pilot repos while preserving v5 read-only & non-pilot repos", async () => {
    const env = baseEnv({ V6_PILOT_REPOSITORIES: `  ${PILOT_REPO.toUpperCase()}  ` });
    const fakeGithub = {
      publicWorkflowRuntimeShas: vi.fn(async () => ({
        commitSha: WORKFLOW_SHA,
        refSha: WORKFLOW_SHA,
      })),
      installationFor: vi.fn(async () => 2468),
      installationToken: vi.fn(async () => ({ token: "meta-tok" })),
      repositoryInfo: vi.fn(async () => ({ id: 987654321, fork: false })),
      capabilityToken: vi.fn(async () => "v5-read-tok"),
      pullRequestHead: vi.fn(async () => HEAD_SHA_1),
    };
    Object.assign(fakeGithub, { withRequestLifetime: vi.fn(() => fakeGithub) });

    // Build a valid RS256 OIDC JWT signed with an ephemeral test key and mock JWKS fetch
    const kp = await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    );
    const pubJwk = await crypto.subtle.exportKey("jwk", kp.publicKey);
    const signJwt = async (claims: Record<string, unknown>) => {
      const b64u = (obj: unknown) =>
        Buffer.from(JSON.stringify(obj)).toString("base64url");
      const input = `${b64u({ alg: "RS256", kid: "k1" })}.${b64u(claims)}`;
      const sig = await crypto.subtle.sign(
        "RSASSA-PKCS1-v1_5",
        kp.privateKey,
        new TextEncoder().encode(input),
      );
      return `${input}.${Buffer.from(sig).toString("base64url")}`;
    };
    const nowSec = Math.floor(Date.now() / 1000);
    const pilotJwt = await signJwt({
      ...makeClaims({
        iat: nowSec,
        exp: nowSec + 300,
        job_workflow_ref:
          "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
      }),
      repository_id: "987654321",
      actor_id: "42",
    });

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ keys: [{ ...pubJwk, kid: "k1", alg: "RS256" }] }), {
        status: 200,
      }),
    );

    const broker = new TokenBroker(env, fakeGithub as never);
    const v5Authority = {
      version: 3,
      purpose: "review",
      repository: PILOT_REPO,
      repository_id: 987654321,
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      run_id: "10001",
      run_attempt: 1,
      job: "hosted",
      job_workflow_ref:
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
      job_workflow_sha: WORKFLOW_SHA,
      issued_at: nowSec,
      expires_at: nowSec + 300,
      audience: "reviewsensei-run-action",
      source: { kind: "pull_request", id: 42, updated_at: null, body_sha256: null },
    };

    await expect(
      broker.exchange({
        oidc_token: pilotJwt,
        capability: "review_publish",
        session_attestation: v5Authority,
      }),
    ).rejects.toThrow("broker_v6_pilot_v5_writer_disabled");
    expect(fakeGithub.capabilityToken).not.toHaveBeenCalled();

    // Read-only v5 capability still allowed
    await expect(
      broker.exchange({
        oidc_token: pilotJwt,
        capability: "review_actions",
        session_attestation: v5Authority,
      }),
    ).resolves.toEqual({ token: "v5-read-tok", capability: "review_actions" });
  });

  it("routes /github/v6/lease/* via worker.fetch to V6PrCoordinator Durable Object and respects admission hold", async () => {
    const mockDoStub = {
      fetch: vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              slot_key: "v6-pr:github.com:12345:67890:987654321:42",
              state: "IDLE",
              generation: 0,
              lease_epoch: 0,
            }),
            { status: 200 },
          ),
      ),
    };
    const idFromName = vi.fn(() => "do-id-1" as unknown as DurableObjectId);
    const get = vi.fn(() => mockDoStub as unknown as DurableObjectStub);

    const kp = await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    );
    const pubJwk = await crypto.subtle.exportKey("jwk", kp.publicKey);
    const nowSec = Math.floor(Date.now() / 1000);
    const b64u = (obj: unknown) => Buffer.from(JSON.stringify(obj)).toString("base64url");
    const claims = makeClaims({ iat: nowSec, exp: nowSec + 300 });
    const jwtInput = `${b64u({ alg: "RS256", kid: "k1" })}.${b64u({
      ...claims,
      repository_id: "987654321",
      actor_id: "42",
    })}`;
    const sig = await crypto.subtle.sign(
      "RSASSA-PKCS1-v1_5",
      kp.privateKey,
      new TextEncoder().encode(jwtInput),
    );
    const jwt = `${jwtInput}.${Buffer.from(sig).toString("base64url")}`;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/.well-known/jwks")) {
        return new Response(
          JSON.stringify({ keys: [{ ...pubJwk, kid: "k1", alg: "RS256" }] }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}/installation`)) {
        return new Response(JSON.stringify({ id: 67890 }), { status: 200 });
      }
      if (url.endsWith("/app/installations/67890/access_tokens")) {
        return new Response(
          JSON.stringify({
            token: "ghs_edge_read",
            expires_at: new Date(Date.now() + 60_000).toISOString(),
            permissions: { metadata: "read", contents: "read", pull_requests: "read" },
          }),
          { status: 201 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}`)) {
        return new Response(JSON.stringify({ id: 987654321, fork: false }), {
          status: 200,
        });
      }
      return new Response("not found", { status: 404 });
    });

    const env = baseEnv({
      V6_PR_COORDINATOR: { idFromName, get } as unknown as DurableObjectNamespace,
    });
    // Stub private key signing in GitHubApi via subtle or test-only valid PKCS8 key
    const appKp = await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    );
    const pkcs8 = Buffer.from(await crypto.subtle.exportKey("pkcs8", appKp.privateKey)).toString(
      "base64",
    );
    env.GITHUB_APP_PRIVATE_KEY = `-----BEGIN PRIVATE KEY-----\n${pkcs8}\n-----END PRIVATE KEY-----`;

    const payload = JSON.stringify({ oidc_token: jwt, pull_request: 42 });
    const res = await worker.fetch(
      new Request("https://github.reviewsensei.dev/github/v6/lease/status", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": String(Buffer.byteLength(payload)),
        },
        body: payload,
      }),
      env,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(idFromName).toHaveBeenCalledWith(
      "v6-pr:github.com:12345:67890:987654321:42",
    );
    expect(mockDoStub.fetch).toHaveBeenCalledTimes(1);

    // Held admission mode blocks v6 routes before parsing
    const heldRes = await worker.fetch(
      new Request("https://github.reviewsensei.dev/github/v6/lease/acquire", {
        method: "POST",
        body: "bad",
      }),
      { ...env, BROKER_ADMISSION_MODE: "held" },
    );
    expect(heldRes.status).toBe(503);
    expect(await heldRes.json()).toEqual({ error: "broker_admission_held", temporary: true });
  });

  it("instantiates V6PrCoordinator Durable Object with SQLite ctx.storage & ctx.blockConcurrencyWhile", async () => {
    const execCalls: string[] = [];
    const sqlMock = {
      exec: vi.fn((query: string) => {
        execCalls.push(query);
        return {
          toArray: () => [],
        };
      }),
    };
    const stateMock = {
      storage: { sql: sqlMock },
      blockConcurrencyWhile: vi.fn(async (fn: () => Promise<unknown>) => fn()),
    } as unknown as DurableObjectState;

    const doInstance = new V6PrCoordinator(stateMock, baseEnv());
    const badMethod = await doInstance.fetch(
      new Request("https://do.internal/github/v6/lease/status", { method: "GET" }),
    );
    expect(badMethod.status).toBe(405);

    const invalidJson = await doInstance.fetch(
      new Request("https://do.internal/github/v6/lease/status", {
        method: "POST",
        body: "not-json",
      }),
    );
    expect(invalidJson.status).toBe(400);

    // Missing signing key fails closed with 503 configuration_unavailable
    const unconfigured = new V6PrCoordinator(
      stateMock,
      baseEnv({ REVIEWSENSEI_SIGNING_KEY: "" }),
    );
    const res503 = await unconfigured.fetch(
      new Request("https://do.internal/github/v6/lease/status", {
        method: "POST",
        body: JSON.stringify({ oidc_token: "x", pull_request: 42 }),
      }),
    );
    expect(res503.status).toBe(503);
    expect(await res503.json()).toMatchObject({ error: "configuration_unavailable" });
    expect(execCalls.length).toBe(0);
  });

  it("fails closed on corrupt SQLite state or last_reason_code rows without proceeding", async () => {
    const slotKey = "v6-pr:github.com:12345:67890:987654321:42";
    const badStateSql = {
      exec: vi.fn((query: string) => {
        if (query.trim().startsWith("SELECT")) {
          return {
            toArray: () => [
              {
                slot_key: slotKey,
                state: "BOGUS_STATE",
                generation: 1,
                lease_epoch: 1,
                reservation_id: "res-1",
                owner_run_id: "10001",
                owner_run_attempt: 1,
                owner_head_sha: HEAD_SHA_1,
                owner_workflow_sha: WORKFLOW_SHA,
                owner_job: "ReviewSensei v6",
                owner_comment_id: null,
                owner_record_sha256: null,
                token_expires_at_ms: null,
                clock_margin_ms: 300_000,
                encrypted_escrow: null,
                last_reason_code: null,
                updated_at_ms: 1_795_000_000_000,
              },
            ],
          };
        }
        return { toArray: () => [] };
      }),
    } as unknown as SqlStorage;

    const sqlStore = new DurableObjectSqlV6CoordinatorStore(badStateSql);
    await expect(sqlStore.getSlot(slotKey)).rejects.toThrow("v6_lease_escrow_lost");

    const clock = new MutableClock();
    const provider = new FakeV6Provider(clock);
    provider.registerOidc("oidc-run-1", makeClaims({ run_id: "10001", run_attempt: "1" }));
    const engine = new V6PrCoordinatorEngine({
      store: sqlStore,
      provider,
      clock,
    });
    const res = await engine.handleStatus(baseEnv(), {
      oidc_token: "oidc-run-1",
      pull_request: 42,
    });
    expect(res.status).toBe(409);
    expect(res.body.diagnostic).toMatchObject({ code: "v6_lease_escrow_lost" });
    expect(res.body.slot_state).toBe("HOLD");
  });

  it("rejects owner records with expires_at <= acquired_at or mismatched slot/epoch/head", async () => {
    const { env, clock, engine, provider, store, slotKey42 } = createHarness();
    await store.putSlot({
      slot_key: slotKey42,
      state: "HOLD",
      generation: 2,
      lease_epoch: 2,
      reservation_id: null,
      owner_run_id: "10001",
      owner_run_attempt: 1,
      owner_head_sha: HEAD_SHA_1,
      owner_workflow_sha: WORKFLOW_SHA,
      owner_job: "ReviewSensei v6",
      owner_comment_id: null,
      owner_record_sha256: null,
      acquired_at_ms: null,
      token_expires_at_ms: clock.now - 400_000,
      token_cleared: false,
      clock_margin_ms: 300_000,
      encrypted_escrow: null,
      last_reason_code: "v6_lease_hold",
      updated_at_ms: clock.now,
    });

    const crypto = new V6EscrowCrypto(SIGNING_KEY);
    provider.comments.set(
      9101,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:hold:v1",
        record_id: "rec-hold-ok",
        slot_key: slotKey42,
        generation: 2,
        lease_epoch: 2,
        operation_id: "op-2",
        pr_head_sha: HEAD_SHA_1,
        reason_code: "v6_lease_hold",
        missing_evidence: [],
        held_at: "2026-11-27T00:05:00Z",
        resolved_by_record_id: "rec-receipt-2",
      }),
    );

    const mac = await crypto.signOwnerAttestation({
      slot_key: slotKey42,
      generation: 2,
      lease_epoch: 2,
      reservation_id: "res-2",
      run_id: "10001",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    });
    // Inverted acquired_at / expires_at must fail closed
    provider.comments.set(
      9102,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:owner:v1",
        record_id: "rec-owner-bad-time",
        slot_key: slotKey42,
        generation: 2,
        lease_epoch: 2,
        reservation_id: "res-2",
        operation_id: "op-2",
        run_id: "10001",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
        acquired_at: "2026-11-27T01:00:00Z",
        expires_at: "2026-11-27T00:00:00Z",
        coordinator_attestation_mac: mac,
      }),
    );
    const badTime = await engine.handleResolveHold(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 2,
      hold_record_comment_id: 9101,
      resolution_kind: "reconciled_clean",
      anchor_comment_id: 9102,
    });
    expect(badTime.status).toBe(409);
    expect(badTime.body.diagnostic).toMatchObject({ code: "v6_lease_slot_continuity_unproved" });

    // Mismatched lease_epoch must fail closed
    const badEpochMac = await crypto.signOwnerAttestation({
      slot_key: slotKey42,
      generation: 2,
      lease_epoch: 99,
      reservation_id: "res-2",
      run_id: "10001",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    });
    provider.comments.set(
      9103,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:owner:v1",
        record_id: "rec-owner-bad-epoch",
        slot_key: slotKey42,
        generation: 2,
        lease_epoch: 99,
        reservation_id: "res-2",
        operation_id: "op-2",
        run_id: "10001",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
        acquired_at: "2026-11-27T00:00:00Z",
        expires_at: "2026-11-27T01:00:00Z",
        coordinator_attestation_mac: badEpochMac,
      }),
    );
    const badEpoch = await engine.handleResolveHold(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 2,
      hold_record_comment_id: 9101,
      resolution_kind: "reconciled_clean",
      anchor_comment_id: 9103,
    });
    expect(badEpoch.status).toBe(409);
    expect(badEpoch.body.diagnostic).toMatchObject({ code: "v6_lease_slot_continuity_unproved" });
  });

  it("guards live MINTING isolates via bounded freshness and recovers stale RESERVED/MINTING + snapshot-mutated drain", async () => {
    const { env, clock, store, makeEngine, provider, slotKey42 } = createHarness();

    // Live parallel isolate in MINTING within V6_MINTING_STALE_MS is NOT flipped to RECONCILING
    await store.putSlot({
      slot_key: slotKey42,
      state: "MINTING",
      generation: 1,
      lease_epoch: 1,
      reservation_id: "res-live-other-isolate",
      owner_run_id: "10001",
      owner_run_attempt: 1,
      owner_head_sha: HEAD_SHA_1,
      owner_workflow_sha: WORKFLOW_SHA,
      owner_job: "ReviewSensei v6",
      owner_comment_id: null,
      owner_record_sha256: null,
      acquired_at_ms: null,
      token_expires_at_ms: null,
      token_cleared: false,
      clock_margin_ms: 300_000,
      encrypted_escrow: null,
      last_reason_code: null,
      updated_at_ms: clock.now,
    });

    const freshEngine = makeEngine();
    const whileLive = await freshEngine.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(whileLive.status).toBe(409);
    expect(whileLive.body.diagnostic).toMatchObject({ code: "v6_lease_reserved" });
    expect((await store.getSlot(slotKey42))?.state).toBe("MINTING");

    // Once V6_MINTING_STALE_MS elapses, fresh engine transitions MINTING -> RECONCILING
    clock.now += V6_MINTING_STALE_MS;
    const afterStale = await freshEngine.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(afterStale.status).toBe(409);
    expect(afterStale.body.diagnostic).toMatchObject({ code: "v6_lease_mint_uncertain" });
    expect((await store.getSlot(slotKey42))?.state).toBe("RECONCILING");

    // Same-run crashed RESERVED with non-terminal status recovers after V6_RESERVED_STALE_MS
    await store.putSlot({
      slot_key: slotKey42,
      state: "RESERVED",
      generation: 2,
      lease_epoch: 2,
      reservation_id: "res-same-run-crashed",
      owner_run_id: "10001",
      owner_run_attempt: 1,
      owner_head_sha: HEAD_SHA_1,
      owner_workflow_sha: WORKFLOW_SHA,
      owner_job: "ReviewSensei v6",
      owner_comment_id: null,
      owner_record_sha256: null,
      acquired_at_ms: null,
      token_expires_at_ms: null,
      token_cleared: false,
      clock_margin_ms: 300_000,
      encrypted_escrow: null,
      last_reason_code: null,
      updated_at_ms: clock.now,
    });
    // Fresh (< V6_RESERVED_STALE_MS) while run 10001:1 is in_progress -> refused
    const sameRunTooEarly = await freshEngine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(sameRunTooEarly.status).toBe(409);
    expect(sameRunTooEarly.body.diagnostic).toMatchObject({ code: "v6_lease_reserved" });

    // Stale (>= V6_RESERVED_STALE_MS) -> same run recovers cleanly without stranding
    clock.now += V6_RESERVED_STALE_MS;
    const sameRunRecovered = await freshEngine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(sameRunRecovered.status).toBe(200);
    expect(sameRunRecovered.body.generation).toBe(3);

    // Drain aborts with 409 if slot mutates during workflowRunAttempt check
    provider.runAttempts.set("10001:1", {
      run_id: "10001",
      run_attempt: 1,
      status: "completed",
      conclusion: "success",
      head_sha: HEAD_SHA_1,
    });
    const origRunAttempt = provider.workflowRunAttempt.bind(provider);
    vi.spyOn(provider, "workflowRunAttempt").mockImplementationOnce(async (repo, runId, att) => {
      const current = await store.getSlot(slotKey42);
      await store.putSlot({
        ...current!,
        lease_epoch: current!.lease_epoch + 1,
      });
      return origRunAttempt(repo, runId, att);
    });
    const racedDrain = await freshEngine.handleDrain(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
    });
    expect(racedDrain.status).toBe(409);
    expect(racedDrain.body.diagnostic).toMatchObject({ code: "v6_lease_draining" });
  });

  it("rejects non-4-multiple base64 strings cleanly in escrow unsealing (RS-061BF3)", async () => {
    const crypto = new V6EscrowCrypto(SIGNING_KEY);
    const aad = { slot_key: "v6-pr:github.com:12345:67890:987654321:42", generation: 1, reservation_id: "res-1" };
    await expect(
      crypto.unsealToken({ v: 1, alg: "A256GCM", iv_b64: "abc", ct_b64: "AAAAAAAAAAAAAAAAAAAAAA==" }, aad),
    ).rejects.toThrow("v6_lease_escrow_lost");
    await expect(
      crypto.unsealToken({ v: 1, alg: "A256GCM", iv_b64: "AAAAAAAAAAAAAAAA", ct_b64: "ab" }, aad),
    ).rejects.toThrow("v6_lease_escrow_lost");
  });

  it("enforces Two-Gate separation, live effect adoption via resolve-hold, and clean_receipts Gen-2 cycle", async () => {
    const { env, engine, provider, store, slotKey42 } = createHarness();
    const crypto = new V6EscrowCrypto(SIGNING_KEY);

    // Generation 1 acquires, posts owner + intent + live review 5001, then crashes before receipt.
    const gen1 = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(gen1.status).toBe(200);
    expect(gen1.body.generation).toBe(1);
    const gen1Slot = await store.getSlot(slotKey42);
    expect(gen1Slot?.token_cleared).toBe(false);
    expect(gen1Slot?.acquired_at_ms).toBeTypeOf("number");

    const ownerMac1 = await crypto.signOwnerAttestation({
      slot_key: slotKey42,
      generation: 1,
      lease_epoch: 1,
      reservation_id: gen1.body.reservation_id as string,
      run_id: "10001",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    });
    provider.comments.set(
      9401,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:owner:v1",
        record_id: "rec-owner-gen1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: gen1.body.reservation_id,
        operation_id: "op-gen1",
        run_id: "10001",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
        acquired_at: "2026-11-27T00:00:00Z",
        expires_at: "2026-11-27T01:00:00Z",
        coordinator_attestation_mac: ownerMac1,
      }),
    );
    const payloadSha1 = "1".repeat(64);
    provider.comments.set(
      9402,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:intent:v1",
        record_id: "rec-intent-gen1",
        operation_id: "op-gen1",
        writer_epoch: 1,
        pr_head_sha: HEAD_SHA_1,
        effect_kind: "pull_request_review",
        payload_sha256: payloadSha1,
        target_digest: "2".repeat(64),
        generation: 1,
        target_location: "pr:42",
      }),
    );
    provider.reviews.set(5001, {
      id: 5001,
      login: "reviewsensei[bot]",
      userType: "Bot",
      commit_id: HEAD_SHA_1,
      body: `LGTM <!-- reviewsensei:v6:effect op=op-gen1 sha256=${payloadSha1} -->`,
      state: "COMMENTED",
      performedViaAppId: 12345,
    });

    // Gate 1: Run 2 drains crashed Gen 1 -> RECONCILING (token_cleared=true), /acquire still blocked.
    provider.runAttempts.set("10001:1", {
      run_id: "10001",
      run_attempt: 1,
      status: "completed",
      conclusion: "failure",
      head_sha: HEAD_SHA_1,
    });
    const drained = await engine.handleDrain(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      expected_generation: 1,
      predecessor_anchor_comment_id: 9401,
    });
    expect(drained.status).toBe(200);
    expect(drained.body).toMatchObject({
      state: "RECONCILING",
      generation: 1,
      token_cleared: true,
    });

    const blockedAcq = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(blockedAcq.status).toBe(409);
    expect(blockedAcq.body.diagnostic).toMatchObject({ code: "v6_lease_reconciling" });

    // Bare runner claim without intent coverage fails closed to HOLD!
    const bareClaim = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "adopted_missing_receipt",
      reconciliation_evidence: {
        old_generation: 1,
        old_owner_comment_id: 9401,
        intent_resolutions: [],
      },
    });
    expect(bareClaim.status).toBe(409);
    expect(bareClaim.body.diagnostic).toMatchObject({
      code: "v6_lease_reconciliation_evidence_invalid",
    });
    expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");

    // Resolve hold back into RECONCILING with verified hold:v1 comment.
    provider.comments.set(
      9403,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:hold:v1",
        record_id: "rec-hold-gen1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        operation_id: "op-gen1",
        pr_head_sha: HEAD_SHA_1,
        reason_code: "v6_lease_reconciliation_evidence_invalid",
        missing_evidence: ["intent:op-gen1"],
        held_at: "2026-11-27T00:05:00Z",
        resolved_by_record_id: null,
      }),
    );
    const enteredReconcile = await engine.handleResolveHold(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      hold_record_comment_id: 9403,
      resolution_kind: "enter_reconcile",
      anchor_comment_id: 9401,
    });
    expect(enteredReconcile.status).toBe(200);
    expect(enteredReconcile.body.state).toBe("RECONCILING");

    // Gate 2: Complete reconcile with verified live review 5001 -> IDLE!
    const adoptedDone = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "adopted_missing_receipt",
      reconciliation_evidence: {
        old_generation: 1,
        old_owner_comment_id: 9401,
        intent_resolutions: [
          {
            operation_id: "op-gen1",
            intent_comment_id: 9402,
            resolution: "adopted_effect",
            native_effect_kind: "pull_request_review",
            native_effect_id: 5001,
          },
        ],
      },
    });
    expect(adoptedDone.status).toBe(200);
    expect(adoptedDone.body.state).toBe("IDLE");

    // Generation 2 acquires, posts intent + receipt, runs Gate 1 /release -> RECONCILING -> Gate 2 clean_receipts -> IDLE.
    const gen2 = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
      predecessor_anchor_comment_id: 9401,
    });
    expect(gen2.status).toBe(200);
    expect(gen2.body.generation).toBe(2);

    const payloadSha2 = "3".repeat(64);
    provider.comments.set(
      9404,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:intent:v1",
        record_id: "rec-intent-gen2",
        operation_id: "op-gen2",
        writer_epoch: 2,
        pr_head_sha: HEAD_SHA_1,
        effect_kind: "pull_request_review",
        payload_sha256: payloadSha2,
        target_digest: "4".repeat(64),
        generation: 2,
        target_location: "pr:42",
      }),
    );
    provider.comments.set(
      9405,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:receipt:v1",
        record_id: "rec-receipt-gen2",
        operation_id: "op-gen2",
        writer_epoch: 2,
        github_effect_id: 5002,
        payload_sha256: payloadSha2,
        completed_at: "2026-11-27T00:08:00Z",
      }),
    );

    const relGen2 = await engine.handleRelease(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 2,
      lease_epoch: 2,
      reservation_id: gen2.body.reservation_id,
    });
    expect(relGen2.status).toBe(200);
    expect(relGen2.body).toMatchObject({ state: "RECONCILING", token_cleared: true });

    const recGen2 = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 2,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 2,
        intent_resolutions: [
          {
            operation_id: "op-gen2",
            intent_comment_id: 9404,
            resolution: "receipt_present",
            receipt_comment_id: 9405,
          },
        ],
      },
    });
    expect(recGen2.status).toBe(200);
    expect(recGen2.body.state).toBe("IDLE");
  });

  it("rejects empty proved_not_submitted and truncated issueCommentsSince to HOLD without pagination", async () => {
    // 1. Empty intent_resolutions with proved_not_submitted is never accepted -> HOLD
    {
      const { env, engine, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      const emptyNonSub = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "proved_not_submitted",
        reconciliation_evidence: {
          old_generation: 1,
          intent_resolutions: [],
        },
      });
      expect(emptyNonSub.status).toBe(409);
      expect(emptyNonSub.body.diagnostic).toMatchObject({
        code: "v6_lease_reconciliation_evidence_invalid",
      });
      expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");
    }

    // 2. Truncated issueCommentsSince fails closed to HOLD in a single bounded call
    {
      const { env, engine, provider, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      provider.commentsSinceOverride = { comments: [], truncated: true };
      const trunc = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: {
          old_generation: 1,
          intent_resolutions: [],
        },
      });
      expect(trunc.status).toBe(409);
      expect(trunc.body.diagnostic).toMatchObject({
        code: "v6_lease_reconciliation_evidence_invalid",
      });
      expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");
      expect(provider.calls.commentListReads).toBe(1);
    }
  });

  it("accepts review-sensei-v6-pilot.yml@refs/tags/<tag> under V6_PILOT_WORKFLOW_TAG when runtime SHA matches", async () => {
    const tag = "v6-pilot-rc1";
    const pilotWorkflowRef = `malsabbagh/review-sensei/.github/workflows/review-sensei-v6-pilot.yml@refs/tags/${tag}`;
    const { env, engine, provider } = createHarness({
      V6_PILOT_WORKFLOW_REF: undefined,
      V6_PILOT_WORKFLOW_TAG: tag,
    });
    provider.runtimeShas = {
      commitSha: WORKFLOW_SHA,
      refSha: WORKFLOW_SHA,
    };
    provider.registerOidc(
      "oidc-pilot-tag",
      makeClaims({
        run_id: "20001",
        run_attempt: "1",
        job_workflow_ref: pilotWorkflowRef,
        job_workflow_sha: WORKFLOW_SHA,
      }),
    );

    const acq = await engine.handleAcquire(env, {
      oidc_token: "oidc-pilot-tag",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(acq.status).toBe(200);
    expect(acq.body.state).toBe("ACTIVE");

    // Mismatched runtime SHA must still be rejected.
    provider.registerOidc(
      "oidc-pilot-bad-sha",
      makeClaims({
        run_id: "20002",
        run_attempt: "1",
        job_workflow_ref: pilotWorkflowRef,
        job_workflow_sha: "f".repeat(40),
      }),
    );
    const badSha = await engine.handleStatus(env, {
      oidc_token: "oidc-pilot-bad-sha",
      pull_request: 42,
    });
    expect(badSha.status).toBe(403);
    expect(badSha.body.diagnostic).toMatchObject({ code: "v6_lease_unauthorized_tuple" });
  });

  it("floors issueCommentsSince down by 2s to whole seconds for sub-second acquired_at_ms", async () => {
    const { env, clock, engine, provider, slotKey42 } = createHarness();
    // Set sub-second timestamp: ...005.678s -> -2000ms = ...003.678s -> floor = ...003.000s
    clock.now = 1_795_000_005_678;

    const acq = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(acq.status).toBe(200);

    clock.now = 1_795_000_020_123;
    const rel = await engine.handleRelease(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 1,
      lease_epoch: 1,
      reservation_id: acq.body.reservation_id,
    });
    expect(rel.status).toBe(200);

    const rec = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-1",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 1,
        intent_resolutions: [],
      },
    });
    expect(rec.status).toBe(200);
    expect(rec.body.state).toBe("IDLE");
    expect(provider.issueCommentsSinceArgs).toEqual([
      new Date(1_795_000_003_000).toISOString(),
    ]);
  });

  it("preserves predecessor acquired_at across cold-start HOLD -> enter_reconcile -> complete-reconcile", async () => {
    const { env, clock, engine, provider, store, slotKey42 } = createHarness();
    const crypto = new V6EscrowCrypto(SIGNING_KEY);
    const predecessorAcquiredAtMs = 1_795_000_010_750;
    const intentCreatedAtMs = 1_795_000_012_400;
    const coldStartHoldAtMs = 1_795_000_090_000;

    // Predecessor owner (gen 1) acquired earlier and wrote intent before cold-start.
    const ownerMac1 = await crypto.signOwnerAttestation({
      slot_key: slotKey42,
      generation: 1,
      lease_epoch: 1,
      reservation_id: "res-gen1",
      run_id: "10001",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    });
    provider.comments.set(
      9501,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:owner:v1",
        record_id: "rec-owner-gen1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: "res-gen1",
        operation_id: "op-gen1",
        run_id: "10001",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
        acquired_at: new Date(predecessorAcquiredAtMs).toISOString(),
        expires_at: new Date(predecessorAcquiredAtMs + 3_600_000).toISOString(),
        coordinator_attestation_mac: ownerMac1,
      }),
    );
    provider.commentCreatedAtMs.set(9501, predecessorAcquiredAtMs + 50);

    const payloadSha = "5".repeat(64);
    provider.comments.set(
      9502,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:intent:v1",
        record_id: "rec-intent-gen1",
        operation_id: "op-gen1",
        writer_epoch: 1,
        pr_head_sha: HEAD_SHA_1,
        effect_kind: "pull_request_review",
        payload_sha256: payloadSha,
        target_digest: "6".repeat(64),
        generation: 1,
        target_location: "pr:42",
      }),
    );
    provider.commentCreatedAtMs.set(9502, intentCreatedAtMs);

    provider.comments.set(
      9503,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:receipt:v1",
        record_id: "rec-receipt-gen1",
        operation_id: "op-gen1",
        writer_epoch: 1,
        github_effect_id: 5050,
        payload_sha256: payloadSha,
        completed_at: new Date(intentCreatedAtMs + 500).toISOString(),
      }),
    );
    provider.commentCreatedAtMs.set(9503, intentCreatedAtMs + 500);

    // Predecessor run is terminal so enter_reconcile is cleared.
    provider.runAttempts.set("10001:1", {
      run_id: "10001",
      run_attempt: 1,
      status: "completed",
      conclusion: "failure",
      head_sha: HEAD_SHA_1,
    });

    // Cold-start acquire with predecessor_anchor_comment_id creates HOLD much later.
    clock.now = coldStartHoldAtMs;
    const coldAcq = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-2",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
      predecessor_anchor_comment_id: 9501,
    });
    expect(coldAcq.status).toBe(409);
    expect(coldAcq.body.diagnostic).toMatchObject({
      code: "v6_lease_slot_continuity_unproved",
    });
    const heldSlot = await store.getSlot(slotKey42);
    expect(heldSlot?.state).toBe("HOLD");
    expect(heldSlot?.acquired_at_ms).toBe(predecessorAcquiredAtMs);

    // enter_reconcile transitions HOLD -> RECONCILING while preserving acquired_at_ms.
    provider.comments.set(
      9504,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:hold:v1",
        record_id: "rec-hold-cold-gen1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        operation_id: "op-gen1",
        pr_head_sha: HEAD_SHA_1,
        reason_code: "v6_lease_slot_continuity_unproved",
        missing_evidence: ["intent:op-gen1"],
        held_at: new Date(coldStartHoldAtMs).toISOString(),
        resolved_by_record_id: null,
      }),
    );
    provider.commentCreatedAtMs.set(9504, coldStartHoldAtMs + 100);
    clock.now = coldStartHoldAtMs + 5_000;
    const enterRec = await engine.handleResolveHold(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      hold_record_comment_id: 9504,
      resolution_kind: "enter_reconcile",
      anchor_comment_id: 9501,
    });
    expect(enterRec.status).toBe(200);
    expect(enterRec.body.state).toBe("RECONCILING");

    // complete-reconcile uses predecessor acquired_at (floored -2s), catching intent 9502 written before cold-start.
    const recDone = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 1,
        intent_resolutions: [
          {
            operation_id: "op-gen1",
            intent_comment_id: 9502,
            resolution: "receipt_present",
            receipt_comment_id: 9503,
          },
        ],
      },
    });
    expect(recDone.status).toBe(200);
    expect(recDone.body.state).toBe("IDLE");
    expect(provider.issueCommentsSinceArgs[0]).toBe(
      new Date(1_795_000_008_000).toISOString(),
    );

    // Also verify fallback when an existing HOLD row has acquired_at_ms === null.
    await store.putSlot({
      slot_key: slotKey42,
      state: "HOLD",
      generation: 1,
      lease_epoch: 1,
      reservation_id: null,
      owner_run_id: "10002",
      owner_run_attempt: 1,
      owner_head_sha: HEAD_SHA_1,
      owner_workflow_sha: WORKFLOW_SHA,
      owner_job: "ReviewSensei v6",
      owner_comment_id: 9501,
      owner_record_sha256: null,
      acquired_at_ms: null,
      token_expires_at_ms: null,
      token_cleared: true,
      clock_margin_ms: 300_000,
      encrypted_escrow: null,
      last_reason_code: "v6_lease_slot_continuity_unproved",
      updated_at_ms: coldStartHoldAtMs,
    });
    const enterRecFallback = await engine.handleResolveHold(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      hold_record_comment_id: 9504,
      resolution_kind: "enter_reconcile",
      anchor_comment_id: 9501,
    });
    expect(enterRecFallback.status).toBe(200);
    const recFallback = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "clean_receipts",
      reconciliation_evidence: {
        old_generation: 1,
        intent_resolutions: [
          {
            operation_id: "op-gen1",
            intent_comment_id: 9502,
            resolution: "receipt_present",
            receipt_comment_id: 9503,
          },
        ],
      },
    });
    expect(recFallback.status).toBe(200);
    expect(recFallback.body.state).toBe("IDLE");
    expect(provider.issueCommentsSinceArgs[1]).toBe(
      new Date(1_795_000_008_000).toISOString(),
    );
  });

  it("completes reconcile on HEAD_A slot with HEAD_A anchor when req.head_sha is HEAD_B (anchored takeover), and when req.head_sha is HEAD_A", async () => {
    const setupCrashedHeadA = async () => {
      const harness = createHarness();
      const { env, engine, provider, slotKey42 } = harness;
      const crypto = new V6EscrowCrypto(SIGNING_KEY);

      const gen1 = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      expect(gen1.status).toBe(200);

      const mac1 = await crypto.signOwnerAttestation({
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: gen1.body.reservation_id as string,
        run_id: "10001",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
      });
      provider.comments.set(
        9601,
        await buildLedgerComment(crypto, {
          schema: "reviewsensei:v6:owner:v1",
          record_id: "rec-owner-heada",
          slot_key: slotKey42,
          generation: 1,
          lease_epoch: 1,
          reservation_id: gen1.body.reservation_id,
          operation_id: "op-heada",
          run_id: "10001",
          run_attempt: 1,
          pr_head_sha: HEAD_SHA_1,
          workflow_sha: WORKFLOW_SHA,
          acquired_at: "2026-11-27T00:00:00Z",
          expires_at: "2026-11-27T01:00:00Z",
          coordinator_attestation_mac: mac1,
        }),
      );
      const shaA = "a".repeat(64);
      provider.comments.set(
        9602,
        await buildLedgerComment(crypto, {
          schema: "reviewsensei:v6:intent:v1",
          record_id: "rec-intent-heada",
          operation_id: "op-heada",
          writer_epoch: 1,
          pr_head_sha: HEAD_SHA_1,
          effect_kind: "pull_request_review",
          payload_sha256: shaA,
          target_digest: "b".repeat(64),
          generation: 1,
          target_location: "pr:42",
        }),
      );
      provider.reviews.set(5601, {
        id: 5601,
        login: "reviewsensei[bot]",
        userType: "Bot",
        commit_id: HEAD_SHA_1,
        body: `Review body <!-- reviewsensei:v6:effect op=op-heada sha256=${shaA} -->`,
        state: "COMMENTED",
        performedViaAppId: 12345,
      });

      provider.runAttempts.set("10001:1", {
        run_id: "10001",
        run_attempt: 1,
        status: "completed",
        conclusion: "failure",
        head_sha: HEAD_SHA_1,
      });
      provider.prBindings.set(42, {
        head_sha: HEAD_SHA_2,
        base_repo_id: 987654321,
        state: "open",
        head_repo_id: 987654321,
        is_fork_pr: false,
      });

      const drained = await engine.handleDrain(env, {
        oidc_token: "oidc-run-2",
        slot_key: slotKey42,
        expected_generation: 1,
        predecessor_anchor_comment_id: 9601,
      });
      expect(drained.status).toBe(200);
      expect(drained.body.state).toBe("RECONCILING");
      return harness;
    };

    // Case A: req.head_sha is HEAD_B while slot.owner_head_sha and anchor are HEAD_A -> anchored takeover succeeds.
    {
      const { env, engine, store, slotKey42 } = await setupCrashedHeadA();
      const recDone = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-2",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "adopted_missing_receipt",
        reconciliation_evidence: {
          old_generation: 1,
          old_owner_comment_id: 9601,
          intent_resolutions: [
            {
              operation_id: "op-heada",
              intent_comment_id: 9602,
              resolution: "adopted_effect",
              native_effect_kind: "pull_request_review",
              native_effect_id: 5601,
            },
          ],
        },
        anchor_comment_id: 9601,
        head_sha: HEAD_SHA_2,
      });
      expect(recDone.status).toBe(200);
      expect(recDone.body.state).toBe("IDLE");

      const gen2 = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-2",
        pull_request: 42,
        head_sha: HEAD_SHA_2,
        capabilities: ["review_publish"],
        predecessor_anchor_comment_id: 9601,
      });
      expect(gen2.status).toBe(200);
      expect(gen2.body.state).toBe("ACTIVE");
      expect(gen2.body.generation).toBe(2);
      expect((await store.getSlot(slotKey42))?.owner_head_sha).toBe(HEAD_SHA_2);
    }

    // Case B: req.head_sha is reconciled HEAD_A matching slot.owner_head_sha -> succeeds then acquires HEAD_B.
    {
      const { env, engine, store, slotKey42 } = await setupCrashedHeadA();
      const recDone = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-2",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "adopted_missing_receipt",
        reconciliation_evidence: {
          old_generation: 1,
          old_owner_comment_id: 9601,
          intent_resolutions: [
            {
              operation_id: "op-heada",
              intent_comment_id: 9602,
              resolution: "adopted_effect",
              native_effect_kind: "pull_request_review",
              native_effect_id: 5601,
            },
          ],
        },
        anchor_comment_id: 9601,
        head_sha: HEAD_SHA_1,
      });
      expect(recDone.status).toBe(200);
      expect(recDone.body.state).toBe("IDLE");

      const gen2 = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-2",
        pull_request: 42,
        head_sha: HEAD_SHA_2,
        capabilities: ["review_publish"],
        predecessor_anchor_comment_id: 9601,
      });
      expect(gen2.status).toBe(200);
      expect(gen2.body.state).toBe("ACTIVE");
      expect(gen2.body.generation).toBe(2);
      expect((await store.getSlot(slotKey42))?.owner_head_sha).toBe(HEAD_SHA_2);
    }
  });

  it("enforces handleCompleteReconcile pre-lock 409 guards, lease_epoch lock race, and unanchored head_sha mismatch HOLD", async () => {
    // 1. Missing slot -> 409 v6_lease_rollback_blocked (slot_state: IDLE)
    {
      const { env, engine, slotKey42 } = createHarness();
      const missing = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: { old_generation: 1, intent_resolutions: [] },
      });
      expect(missing.status).toBe(409);
      expect(missing.body.diagnostic).toMatchObject({ code: "v6_lease_rollback_blocked" });
      expect(missing.body.slot_state).toBe("IDLE");
    }

    // 2. Non-RECONCILING slot -> 409 state conflict (ACTIVE -> v6_lease_active_other_owner)
    {
      const { env, engine, slotKey42 } = createHarness();
      await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      const wrongState = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-2",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: { old_generation: 1, intent_resolutions: [] },
      });
      expect(wrongState.status).toBe(409);
      expect(wrongState.body.diagnostic).toMatchObject({ code: "v6_lease_active_other_owner" });
      expect(wrongState.body.slot_state).toBe("ACTIVE");
    }

    // 3. Generation mismatch -> 409 v6_lease_rollback_blocked (slot_state: RECONCILING)
    {
      const { env, engine, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      const badGen = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 2,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: { old_generation: 2, intent_resolutions: [] },
      });
      expect(badGen.status).toBe(409);
      expect(badGen.body.diagnostic).toMatchObject({ code: "v6_lease_rollback_blocked" });
      expect(badGen.body.slot_state).toBe("RECONCILING");

      // 4. lease_epoch changed between pre-lock read and reconcilePrep lock -> 409 v6_lease_rollback_blocked
      const origGetSlot = store.getSlot.bind(store);
      let reads = 0;
      vi.spyOn(store, "getSlot").mockImplementation(async (key) => {
        reads += 1;
        const row = await origGetSlot(key);
        if (reads === 2 && row !== null) {
          return { ...row, lease_epoch: row.lease_epoch + 1 };
        }
        return row;
      });
      const epochRace = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: { old_generation: 1, intent_resolutions: [] },
      });
      expect(epochRace.status).toBe(409);
      expect(epochRace.body.diagnostic).toMatchObject({ code: "v6_lease_rollback_blocked" });
    }

    // 4b. Early anchor verification catch blocks HOLD and returns 409 v6_lease_rollback_blocked if generation/epoch changed
    {
      const { env, engine, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      const origGetSlot = store.getSlot.bind(store);
      let reads = 0;
      vi.spyOn(store, "getSlot").mockImplementation(async (key) => {
        reads += 1;
        const row = await origGetSlot(key);
        if (reads === 2 && row !== null) {
          return { ...row, lease_epoch: row.lease_epoch + 1 };
        }
        return row;
      });
      const anchorCatchRace = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        anchor_comment_id: 99999,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: { old_generation: 1, intent_resolutions: [] },
      });
      expect(anchorCatchRace.status).toBe(409);
      expect(anchorCatchRace.body.diagnostic).toMatchObject({ code: "v6_lease_rollback_blocked" });
      const slotAfter = await origGetSlot(slotKey42);
      expect(slotAfter?.state).toBe("RECONCILING");
    }

    // 5. Unanchored caller with mismatched req.head_sha -> HOLD v6_lease_slot_continuity_unproved
    {
      const { env, engine, store, slotKey42 } = createHarness();
      const acq = await engine.handleAcquire(env, {
        oidc_token: "oidc-run-1",
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
      await engine.handleRelease(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: acq.body.reservation_id,
      });
      const unanchoredMismatch = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "clean_receipts",
        reconciliation_evidence: { old_generation: 1, intent_resolutions: [] },
        head_sha: HEAD_SHA_2,
      });
      expect(unanchoredMismatch.status).toBe(409);
      expect(unanchoredMismatch.body.diagnostic).toMatchObject({
        code: "v6_lease_slot_continuity_unproved",
      });
      expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");
    }
  });

  it("allows mixed adopted_effect + proved_not_submitted in adopted_missing_receipt and rejects ambiguous_hold", async () => {
    const { env, engine, provider, store, slotKey42 } = createHarness();
    const crypto = new V6EscrowCrypto(SIGNING_KEY);

    const gen1 = await engine.handleAcquire(env, {
      oidc_token: "oidc-run-1",
      pull_request: 42,
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(gen1.status).toBe(200);

    const mac1 = await crypto.signOwnerAttestation({
      slot_key: slotKey42,
      generation: 1,
      lease_epoch: 1,
      reservation_id: gen1.body.reservation_id as string,
      run_id: "10001",
      run_attempt: 1,
      pr_head_sha: HEAD_SHA_1,
      workflow_sha: WORKFLOW_SHA,
    });
    provider.comments.set(
      9701,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:owner:v1",
        record_id: "rec-owner-mixed",
        slot_key: slotKey42,
        generation: 1,
        lease_epoch: 1,
        reservation_id: gen1.body.reservation_id,
        operation_id: "op-mixed-1",
        run_id: "10001",
        run_attempt: 1,
        pr_head_sha: HEAD_SHA_1,
        workflow_sha: WORKFLOW_SHA,
        acquired_at: "2026-11-27T00:00:00Z",
        expires_at: "2026-11-27T01:00:00Z",
        coordinator_attestation_mac: mac1,
      }),
    );

    const sha1 = "1".repeat(64);
    const sha2 = "2".repeat(64);
    provider.comments.set(
      9702,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:intent:v1",
        record_id: "rec-intent-mixed-1",
        operation_id: "op-mixed-1",
        writer_epoch: 1,
        pr_head_sha: HEAD_SHA_1,
        effect_kind: "pull_request_review",
        payload_sha256: sha1,
        target_digest: "3".repeat(64),
        generation: 1,
        target_location: "pr:42",
      }),
    );
    provider.reviews.set(5701, {
      id: 5701,
      login: "reviewsensei[bot]",
      userType: "Bot",
      commit_id: HEAD_SHA_1,
      body: `Review body <!-- reviewsensei:v6:effect op=op-mixed-1 sha256=${sha1} -->`,
      state: "COMMENTED",
      performedViaAppId: 12345,
    });
    provider.comments.set(
      9703,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:intent:v1",
        record_id: "rec-intent-mixed-2",
        operation_id: "op-mixed-2",
        writer_epoch: 1,
        pr_head_sha: HEAD_SHA_1,
        effect_kind: "pull_request_review",
        payload_sha256: sha2,
        target_digest: "4".repeat(64),
        generation: 1,
        target_location: "pr:42",
      }),
    );
    provider.comments.set(
      9704,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:non-submission:v1",
        record_id: "rec-nonsub-mixed-2",
        slot_key: slotKey42,
        generation: 1,
        operation_id: "op-mixed-2",
        pr_head_sha: HEAD_SHA_1,
        proof_kind: "definitive_github_rejection",
        payload_sha256: sha2,
        recorded_at: "2026-11-27T00:05:00Z",
        http_status: 422,
        diagnostic: "rejected",
      }),
    );

    provider.runAttempts.set("10001:1", {
      run_id: "10001",
      run_attempt: 1,
      status: "completed",
      conclusion: "failure",
      head_sha: HEAD_SHA_1,
    });
    const drained = await engine.handleDrain(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      expected_generation: 1,
      predecessor_anchor_comment_id: 9701,
    });
    expect(drained.status).toBe(200);

    // Adding an ambiguous_hold intent must still fail closed to HOLD
    const sha3 = "5".repeat(64);
    provider.comments.set(
      9705,
      await buildLedgerComment(crypto, {
        schema: "reviewsensei:v6:intent:v1",
        record_id: "rec-intent-mixed-3",
        operation_id: "op-mixed-3",
        writer_epoch: 1,
        pr_head_sha: HEAD_SHA_1,
        effect_kind: "pull_request_review",
        payload_sha256: sha3,
        target_digest: "6".repeat(64),
        generation: 1,
        target_location: "pr:42",
      }),
    );
    const mixedWithHold = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "adopted_missing_receipt",
      reconciliation_evidence: {
        old_generation: 1,
        old_owner_comment_id: 9701,
        intent_resolutions: [
          {
            operation_id: "op-mixed-1",
            intent_comment_id: 9702,
            resolution: "adopted_effect",
            native_effect_kind: "pull_request_review",
            native_effect_id: 5701,
          },
          {
            operation_id: "op-mixed-2",
            intent_comment_id: 9703,
            resolution: "proved_not_submitted",
            proof_comment_id: 9704,
          },
          {
            operation_id: "op-mixed-3",
            intent_comment_id: 9705,
            resolution: "ambiguous_hold",
          },
        ],
      },
      anchor_comment_id: 9701,
      head_sha: HEAD_SHA_1,
    });
    expect(mixedWithHold.status).toBe(409);
    expect(mixedWithHold.body.diagnostic).toMatchObject({
      code: "v6_lease_reconciliation_evidence_invalid",
    });
    expect((await store.getSlot(slotKey42))?.state).toBe("HOLD");

    // Without the ambiguous_hold intent, mixed adopted_effect + proved_not_submitted succeeds to IDLE
    provider.comments.delete(9705);
    await store.putSlot({
      ...(await store.getSlot(slotKey42))!,
      state: "RECONCILING",
      last_reason_code: null,
    });
    const mixedOk = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "adopted_missing_receipt",
      reconciliation_evidence: {
        old_generation: 1,
        old_owner_comment_id: 9701,
        intent_resolutions: [
          {
            operation_id: "op-mixed-1",
            intent_comment_id: 9702,
            resolution: "adopted_effect",
            native_effect_kind: "pull_request_review",
            native_effect_id: 5701,
          },
          {
            operation_id: "op-mixed-2",
            intent_comment_id: 9703,
            resolution: "proved_not_submitted",
            proof_comment_id: 9704,
          },
        ],
      },
      anchor_comment_id: 9701,
      head_sha: HEAD_SHA_1,
    });
    expect(mixedOk.status).toBe(200);
    expect(mixedOk.body.state).toBe("IDLE");
  });
});

async function createSignedTestCredentials(claimsOverrides: Partial<OidcClaims> = {}) {
  const kp = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  const pubJwk = await crypto.subtle.exportKey("jwk", kp.publicKey);
  const b64u = (obj: unknown) => Buffer.from(JSON.stringify(obj)).toString("base64url");
  const signClaims = async (overrides: Partial<OidcClaims> = {}) => {
    const nowSec = Math.floor(Date.now() / 1000);
    const claims = makeClaims({ iat: nowSec, exp: nowSec + 300, ...overrides });
    const jwtInput = `${b64u({ alg: "RS256", kid: "k1" })}.${b64u({
      ...claims,
      repository_id: String(claims.repository_id),
      actor_id: String(claims.actor_id),
    })}`;
    const sig = await crypto.subtle.sign(
      "RSASSA-PKCS1-v1_5",
      kp.privateKey,
      new TextEncoder().encode(jwtInput),
    );
    return `${jwtInput}.${Buffer.from(sig).toString("base64url")}`;
  };
  const jwt = await signClaims(claimsOverrides);

  const appKp = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  const pkcs8 = Buffer.from(await crypto.subtle.exportKey("pkcs8", appKp.privateKey)).toString(
    "base64",
  );
  const appPrivateKey = `-----BEGIN PRIVATE KEY-----\n${pkcs8}\n-----END PRIVATE KEY-----`;
  return { jwt, pubJwk, appPrivateKey, signClaims };
}

function createInMemorySqlStorage(): SqlStorage {
  const rows = new Map<string, Record<string, unknown>>();
  return {
    exec: vi.fn((query: string, ...bindings: unknown[]) => {
      const normalized = query.trim().replace(/\s+/g, " ");
      if (normalized.startsWith("PRAGMA table_info(v6_pr_slot)")) {
        return {
          toArray: () => [
            { name: "slot_key" },
            { name: "acquired_at_ms" },
            { name: "token_cleared" },
          ],
        };
      }
      if (normalized.startsWith("SELECT")) {
        const slotKey = String(bindings[0]);
        const row = rows.get(slotKey);
        return { toArray: () => (row ? [{ ...row }] : []) };
      }
      if (normalized.startsWith("INSERT INTO v6_pr_slot")) {
        const record: Record<string, unknown> = {
          slot_key: bindings[0],
          state: bindings[1],
          generation: bindings[2],
          lease_epoch: bindings[3],
          reservation_id: bindings[4],
          owner_run_id: bindings[5],
          owner_run_attempt: bindings[6],
          owner_head_sha: bindings[7],
          owner_workflow_sha: bindings[8],
          owner_job: bindings[9],
          owner_comment_id: bindings[10],
          owner_record_sha256: bindings[11],
          acquired_at_ms: bindings[12],
          token_expires_at_ms: bindings[13],
          token_cleared: bindings[14],
          clock_margin_ms: bindings[15],
          encrypted_escrow: bindings[16],
          last_reason_code: bindings[17],
          updated_at_ms: bindings[18],
        };
        rows.set(String(bindings[0]), record);
        return { toArray: () => [] };
      }
      return { toArray: () => [] };
    }),
  } as unknown as SqlStorage;
}

describe("v6 acquire response redaction", () => {
  afterEach(() => {
    resetOidcJwksCacheForTesting();
    vi.restoreAllMocks();
  });

  it("serializeV6RouteResponseBody preserves token only on 200 /acquire while redacting all other fields, routes, and statuses", () => {
    const acquiredJson = serializeV6RouteResponseBody("/github/v6/lease/acquire", 200, {
      slot_key: "v6-pr:github.com:12345:67890:987654321:42",
      state: "ACTIVE",
      token: "ghs_realToken123456",
      operation_id: "op-ghs_leakedSecret999",
      error: "err ghs_anotherSecret888",
      nested: { auth: "Bearer secretHeaderToken" },
    });
    const acquired = JSON.parse(acquiredJson) as Record<string, unknown>;
    expect(acquired.token).toBe("ghs_realToken123456");
    expect(acquired.operation_id).toBe("op-[REDACTED_TOKEN]");
    expect(acquired.error).toBe("err [REDACTED_TOKEN]");
    expect(acquired.nested).toEqual({ auth: "Bearer [REDACTED]" });
    expect(acquired.operation_id).not.toContain("ghs_");

    for (const status of [409, 500]) {
      const non200 = JSON.parse(
        serializeV6RouteResponseBody("/github/v6/lease/acquire", status, {
          error: "v6_lease_active_other_owner",
          token: "ghs_realToken123456",
          detail: "ghs_otherSecret777",
        }),
      ) as Record<string, unknown>;
      expect(non200.token).toBe("[REDACTED_TOKEN]");
      expect(non200.detail).toBe("[REDACTED_TOKEN]");
    }

    for (const route of [
      "/github/v6/lease/status",
      "/github/v6/lease/drain",
      "/github/v6/lease/release",
      "/github/v6/lease/complete-reconcile",
      "/github/v6/lease/resolve-hold",
    ]) {
      const serialized = JSON.parse(
        serializeV6RouteResponseBody(route, 200, {
          slot_key: "v6-pr:github.com:12345:67890:987654321:42",
          state: "ACTIVE",
          token: "ghs_realToken123456",
          note: "ghs_noteSecret555",
        }),
      ) as Record<string, unknown>;
      expect(serialized.token, route).toBe("[REDACTED_TOKEN]");
      expect(serialized.note, route).toBe("[REDACTED_TOKEN]");
    }

    const emptyToken = JSON.parse(
      serializeV6RouteResponseBody("/github/v6/lease/acquire", 200, {
        state: "ACTIVE",
        token: "",
        error: "ghs_leakWithEmptyToken",
      }),
    ) as Record<string, unknown>;
    expect(emptyToken.token).toBe("");
    expect(emptyToken.error).toBe("[REDACTED_TOKEN]");
  });

  it("V6PrCoordinator.fetch returns intact ghs_ token on 200 /acquire and redacts /status and non-200 responses", async () => {
    const ownerCreds = await createSignedTestCredentials({ run_id: "10001", run_attempt: "1" });
    const contenderJwt = await ownerCreds.signClaims({ run_id: "10002", run_attempt: "1" });
    const mintedToken = "ghs_doLiveAcquireToken123456";

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/.well-known/jwks")) {
        return new Response(
          JSON.stringify({ keys: [{ ...ownerCreds.pubJwk, kid: "k1", alg: "RS256" }] }),
          { status: 200 },
        );
      }
      if (url.endsWith("/app")) {
        return new Response(JSON.stringify({ slug: "reviewsensei" }), { status: 200 });
      }
      if (url.endsWith("/repos/malsabbagh/review-sensei/git/ref/tags/v6-pilot")) {
        return new Response(
          JSON.stringify({ object: { type: "commit", sha: WORKFLOW_SHA } }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}/installation`)) {
        return new Response(JSON.stringify({ id: 67890 }), { status: 200 });
      }
      if (url.endsWith("/app/installations/67890/access_tokens")) {
        const parsedInit = JSON.parse(String(init?.body ?? "{}")) as {
          permissions?: Record<string, string>;
        };
        const isWriteMint = parsedInit.permissions?.pull_requests === "write";
        return new Response(
          JSON.stringify({
            token: isWriteMint ? mintedToken : "ghs_doInternalReadOnly",
            expires_at: new Date(Date.now() + 600_000).toISOString(),
            permissions: { metadata: "read", ...(parsedInit.permissions ?? {}) },
          }),
          { status: 201 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}`)) {
        return new Response(JSON.stringify({ id: 987654321, fork: false }), { status: 200 });
      }
      if (url.endsWith(`/repos/${PILOT_REPO}/pulls/42`)) {
        return new Response(
          JSON.stringify({
            state: "open",
            head: { sha: HEAD_SHA_1, repo: { id: 987654321, fork: false } },
            base: { repo: { id: 987654321, fork: false } },
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}/actions/runs/10001/attempts/1`)) {
        return new Response(
          JSON.stringify({
            id: 10001,
            run_attempt: 1,
            status: "in_progress",
            conclusion: null,
            head_sha: HEAD_SHA_1,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}/actions/runs/10002/attempts/1`)) {
        return new Response(
          JSON.stringify({
            id: 10002,
            run_attempt: 1,
            status: "in_progress",
            conclusion: null,
            head_sha: HEAD_SHA_1,
          }),
          { status: 200 },
        );
      }
      return new Response("not found", { status: 404 });
    });

    const stateMock = {
      storage: { sql: createInMemorySqlStorage() },
      blockConcurrencyWhile: vi.fn(async (fn: () => Promise<unknown>) => fn()),
    } as unknown as DurableObjectState;
    const env = baseEnv({ GITHUB_APP_PRIVATE_KEY: ownerCreds.appPrivateKey });
    const doInstance = new V6PrCoordinator(stateMock, env);

    const acquireRes = await doInstance.fetch(
      new Request("https://do.internal/github/v6/lease/acquire", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          oidc_token: ownerCreds.jwt,
          pull_request: 42,
          head_sha: HEAD_SHA_1,
          capabilities: ["review_publish"],
        }),
      }),
    );
    const acquireBody = (await acquireRes.json()) as Record<string, unknown>;
    expect(acquireBody).toMatchObject({ state: "ACTIVE", token: mintedToken });
    expect(acquireRes.status).toBe(200);
    expect(acquireRes.headers.get("cache-control")).toBe("no-store");

    const statusRes = await doInstance.fetch(
      new Request("https://do.internal/github/v6/lease/status", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          oidc_token: ownerCreds.jwt,
          pull_request: 42,
        }),
      }),
    );
    expect(statusRes.status).toBe(200);
    expect(statusRes.headers.get("cache-control")).toBe("no-store");
    const statusText = await statusRes.text();
    expect(statusText).not.toContain("ghs_");

    const contenderRes = await doInstance.fetch(
      new Request("https://do.internal/github/v6/lease/acquire", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          oidc_token: contenderJwt,
          pull_request: 42,
          head_sha: HEAD_SHA_1,
          capabilities: ["review_publish"],
        }),
      }),
    );
    expect(contenderRes.status).toBe(409);
    expect(contenderRes.headers.get("cache-control")).toBe("no-store");
    const contenderText = await contenderRes.text();
    expect(contenderText).not.toContain("ghs_");
  });

  it("worker.fetch v6Lease preserves token on 200 /acquire while redacting non-token fields, /status, and 409 responses", async () => {
    const creds = await createSignedTestCredentials();
    const slotKey = "v6-pr:github.com:12345:67890:987654321:42";
    const liveAcquireToken = "ghs_liveAcquireToken98765";

    const stubFetch = vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      if (path === "/github/v6/lease/acquire") {
        const callIndex = stubFetch.mock.calls.filter(
          ([callReq]) => new URL((callReq as Request).url).pathname === "/github/v6/lease/acquire",
        ).length;
        if (callIndex === 1) {
          return new Response(
            JSON.stringify({
              slot_key: slotKey,
              state: "ACTIVE",
              generation: 1,
              lease_epoch: 1,
              capability: "review_publish",
              token: liveAcquireToken,
              operation_id: "op-ghs_mustBeRedactedInOtherField",
            }),
            { status: 200 },
          );
        }
        if (callIndex === 2) {
          const doBody = serializeV6RouteResponseBody(path, 200, {
            slot_key: slotKey,
            state: "ACTIVE",
            generation: 1,
            lease_epoch: 1,
            capability: "review_publish",
            token: liveAcquireToken,
            operation_id: "op_clean",
          });
          return new Response(doBody, { status: 200 });
        }
        return new Response(
          JSON.stringify({
            error: "v6_lease_active_other_owner",
            slot_state: "ACTIVE",
            token: "ghs_mustBeRedactedOn409",
          }),
          { status: 409 },
        );
      }
      return new Response(
        JSON.stringify({
          slot_key: slotKey,
          state: "ACTIVE",
          generation: 1,
          lease_epoch: 1,
          token: "ghs_mustBeRedactedOnStatus",
        }),
        { status: 200 },
      );
    });

    const idFromName = vi.fn(() => "do-id-1" as unknown as DurableObjectId);
    const get = vi.fn(() => ({ fetch: stubFetch }) as unknown as DurableObjectStub);

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/.well-known/jwks")) {
        return new Response(
          JSON.stringify({ keys: [{ ...creds.pubJwk, kid: "k1", alg: "RS256" }] }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}/installation`)) {
        return new Response(JSON.stringify({ id: 67890 }), { status: 200 });
      }
      if (url.endsWith("/app/installations/67890/access_tokens")) {
        return new Response(
          JSON.stringify({
            token: "ghs_edge_read",
            expires_at: new Date(Date.now() + 60_000).toISOString(),
            permissions: { metadata: "read", contents: "read", pull_requests: "read" },
          }),
          { status: 201 },
        );
      }
      if (url.endsWith(`/repos/${PILOT_REPO}`)) {
        return new Response(JSON.stringify({ id: 987654321, fork: false }), { status: 200 });
      }
      return new Response("not found", { status: 404 });
    });

    const env = baseEnv({
      GITHUB_APP_PRIVATE_KEY: creds.appPrivateKey,
      V6_PR_COORDINATOR: { idFromName, get } as unknown as DurableObjectNamespace,
    });

    const callWorker = (path: string, bodyObj: Record<string, unknown>) => {
      const payload = JSON.stringify({ oidc_token: creds.jwt, pull_request: 42, ...bodyObj });
      return worker.fetch(
        new Request(`https://github.reviewsensei.dev${path}`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "content-length": String(Buffer.byteLength(payload)),
          },
          body: payload,
        }),
        env,
      );
    };

    const acquire1 = await callWorker("/github/v6/lease/acquire", {
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(acquire1.status).toBe(200);
    expect(acquire1.headers.get("cache-control")).toBe("no-store");
    const acquire1Body = (await acquire1.json()) as Record<string, unknown>;
    expect(acquire1Body.token).toBe(liveAcquireToken);
    expect(acquire1Body.operation_id).toBe("op-[REDACTED_TOKEN]");

    const acquire2 = await callWorker("/github/v6/lease/acquire", {
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(acquire2.status).toBe(200);
    expect(acquire2.headers.get("cache-control")).toBe("no-store");
    const acquire2Body = (await acquire2.json()) as Record<string, unknown>;
    expect(acquire2Body.token).toBe(liveAcquireToken);

    const statusRes = await callWorker("/github/v6/lease/status", {});
    expect(statusRes.status).toBe(200);
    expect(statusRes.headers.get("cache-control")).toBe("no-store");
    const statusBody = (await statusRes.json()) as Record<string, unknown>;
    expect(statusBody.token).toBe("[REDACTED_TOKEN]");

    const conflictRes = await callWorker("/github/v6/lease/acquire", {
      head_sha: HEAD_SHA_1,
      capabilities: ["review_publish"],
    });
    expect(conflictRes.status).toBe(409);
    expect(conflictRes.headers.get("cache-control")).toBe("no-store");
    const conflictBody = (await conflictRes.json()) as Record<string, unknown>;
    expect(conflictBody.token).toBe("[REDACTED_TOKEN]");
  });
});
