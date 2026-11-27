import { afterEach, describe, expect, it, vi } from "vitest";
import contract from "../../../src/review_sensei/schemas/v6-coordinator-contract.json";
import type { WorkerEnv } from "../src/env";
import type {
  LiveIssueComment,
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
  InMemoryV6CoordinatorStore,
  V6PrCoordinator,
  V6PrCoordinatorEngine,
  buildCanonicalSlotKey,
  canonicalSlotHost,
  type V6AuditEvent,
  type V6Clock,
  type V6ProviderAdapter,
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
    assertZeroPlaintextSecrets(store, auditEvents, []);
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

    // Alternative run & alternative attempt rejected without token disclosure
    for (const badToken of ["oidc-run-2", "oidc-run-1-att2"]) {
      const denied = await engine.handleAcquire(env, {
        oidc_token: badToken,
        pull_request: 42,
        head_sha: HEAD_SHA_1,
        capabilities: ["review_publish"],
      });
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
    expect(rel42.body.state).toBe("IDLE");

    const status43 = await engine.handleStatus(env, {
      oidc_token: "oidc-run-2",
      pull_request: 43,
    });
    expect(status43.status).toBe(200);
    expect(status43.body.state).toBe("ACTIVE");

    assertZeroPlaintextSecrets(store, auditEvents, [token1, pr43.body.token as string]);
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
    expect(contenderB.body.diagnostic).toMatchObject({ code: "v6_lease_mint_uncertain" });

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
    assertZeroPlaintextSecrets(store, auditEvents, ["ghs_late_stale_token"]);
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
      token_expires_at_ms: null,
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
      reconciliation_outcome: "proved_not_submitted",
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

    const drained = await engine.handleDrain(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      predecessor_anchor_comment_id: 9001,
    });
    expect(drained.status).toBe(200);
    expect(drained.body.state).toBe("RECONCILING");
    expect(provider.calls.revocations).toHaveLength(1);

    // Reconcile completes to IDLE cleanly
    const reconciled = await engine.handleCompleteReconcile(env, {
      oidc_token: "oidc-run-2",
      slot_key: slotKey42,
      generation: 1,
      reconciliation_outcome: "adopted",
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
      expect(rel.body.state).toBe("IDLE");
      expect(provider.calls.revocations.map((r) => r.token)).toEqual([
        acq.body.token,
        acq.body.token,
      ]);
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

      // Advance past token_expires_at_ms + 300_000
      clock.now = expiresAtMs + 300_000;
      const done = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "proved_not_submitted",
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
        token_expires_at_ms: null,
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
      const { env, clock, engine, store, slotKey42 } = createHarness();
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
        token_expires_at_ms: expiresAtMs,
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

      clock.now = expiresAtMs + 300_000;
      const resolvedAfterMargin = await engine.handleCompleteReconcile(env, {
        oidc_token: "oidc-run-1",
        slot_key: slotKey42,
        generation: 1,
        reconciliation_outcome: "adopted",
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
      token_expires_at_ms: null,
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

    // With resolved_by_record_id present (and no active unrevoked token), enter_reconcile & reconciled_clean work
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
    expect(resolved.body.state).toBe("IDLE");

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
    expect(rollbackRelease.body.state).toBe("IDLE");

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
});
