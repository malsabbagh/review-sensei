import { createHash, generateKeyPairSync } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const oidc = vi.hoisted(() => ({ verify: vi.fn() }));
vi.mock("../src/oidc", () => ({ verifyOidcAssertion: oidc.verify }));

import type { WorkerEnv } from "../src/env";
import type { RunAuthorityRequest } from "../src/run-authority";
import {
  canonicalPayloadSha256,
  parseV6WriterOwnershipRequest,
} from "../src/v6-writer-ownership";
import worker from "../src/worker";

const SHA = "a".repeat(40);
const OTHER_SHA = "b".repeat(40);
const WORKFLOW =
  "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5";
const COMMAND_BODY = "@sensei review continue";
const APP_ID = 12345;
const REPO = "acme/widgets";
const REPO_ID = 987654321;
const PR_NUMBER = 7;

const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const APP_PRIVATE_KEY = privateKey
  .export({ type: "pkcs8", format: "pem" })
  .toString();

function sha256Hex(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function makeClaims(overrides: Record<string, unknown> = {}) {
  return {
    repository: REPO,
    repository_owner: "acme",
    repository_id: REPO_ID,
    actor: "octocat",
    actor_id: 12345678,
    event_name: "pull_request",
    run_id: "10000000001",
    run_attempt: "1",
    job_workflow_ref: WORKFLOW,
    job_workflow_sha: SHA,
    runner_environment: "github-hosted",
    ...overrides,
  };
}

function makeAttestation(
  purpose: "review" | "command" | "reply" = "review",
  identity = makeClaims(),
  sourceKind: "pull_request" | "issue_comment" | "review_comment" =
    purpose === "review" ? "pull_request" : "issue_comment",
): RunAuthorityRequest {
  const now = Math.floor(Date.now() / 1000);
  return {
    version: 3,
    purpose,
    repository: identity.repository,
    repository_id: identity.repository_id,
    pull_request: PR_NUMBER,
    head_sha: SHA,
    run_id: identity.run_id,
    run_attempt: Number(identity.run_attempt),
    job: purpose === "command" ? "command" : "hosted",
    job_workflow_ref: identity.job_workflow_ref,
    job_workflow_sha: identity.job_workflow_sha,
    issued_at: now,
    expires_at: now + 600,
    audience: "reviewsensei-run-action",
    source:
      purpose === "review"
        ? { kind: "pull_request", id: PR_NUMBER, updated_at: null, body_sha256: null }
        : {
            kind: sourceKind,
            id: 13579,
            updated_at: "2026-10-09T00:00:00Z",
            body_sha256: sha256Hex(COMMAND_BODY),
          },
  } as RunAuthorityRequest;
}

function makeOwnership(overrides: Record<string, unknown> = {}) {
  return {
    schema: "v6-writer-ownership-v1",
    repository_id: REPO_ID,
    pull_request: PR_NUMBER,
    operation_id: "op-1",
    writer_epoch: 1,
    head_sha: SHA,
    ...overrides,
  };
}

function makeEnv(): WorkerEnv {
  return {
    PUBLIC_WORKFLOW_TAG: "v5",
    REVIEWSENSEI_SIGNING_KEY: "test-signing-key",
    GITHUB_APP_ID: String(APP_ID),
    GITHUB_APP_PRIVATE_KEY: APP_PRIVATE_KEY,
    GITHUB_APP_WEBHOOK_SECRET: "unused",
  } as WorkerEnv;
}

function tokenHttpRequest(body: unknown): Request {
  const encoded = JSON.stringify(body);
  return new Request("https://github.reviewsensei.dev/github/token", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "content-length": String(new TextEncoder().encode(encoded).byteLength),
    },
    body: encoded,
  });
}

async function formatMarker(
  payload: Record<string, unknown>,
  kind: "record" | "ownership" = "record",
): Promise<string> {
  const digest = await canonicalPayloadSha256(payload);
  return `<!-- reviewsensei:v6:${kind} sha256=${digest} payload=${JSON.stringify(payload)} -->`;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

interface GitHubMockState {
  prStatus: number;
  prBody: unknown;
  runStatusCode: number;
  runBody: unknown;
  commentsPages: Map<number, { status: number; body: unknown }>;
  calledPaths: string[];
}

function installGitHubFetchMock(overrides: Partial<GitHubMockState> = {}): GitHubMockState {
  const state: GitHubMockState = {
    prStatus: 200,
    prBody: {
      state: "open",
      head: { sha: SHA },
      base: { repo: { id: REPO_ID } },
    },
    runStatusCode: 200,
    runBody: {
      id: 10000000001,
      run_attempt: 1,
      status: "in_progress",
    },
    commentsPages: new Map([[1, { status: 200, body: [] }]]),
    calledPaths: [],
    ...overrides,
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input.toString());
      state.calledPaths.push(`${init?.method ?? "GET"} ${url.pathname}${url.search}`);

      if (url.pathname === "/repos/malsabbagh/review-sensei/git/ref/tags/v5") {
        return jsonResponse({ object: { type: "commit", sha: SHA } });
      }
      if (url.pathname === `/repos/${REPO}/installation`) {
        return jsonResponse({ id: 2468 });
      }
      if (url.pathname === "/app") {
        return jsonResponse({ slug: "reviewsensei" });
      }
      if (url.pathname === "/app/installations/2468/access_tokens") {
        const parsed = init?.body ? JSON.parse(String(init.body)) : {};
        return jsonResponse({
          token: "ghs_test_token",
          expires_at: new Date(Date.now() + 3600_000).toISOString(),
          permissions: { metadata: "read", ...(parsed.permissions ?? {}) },
        });
      }
      if (url.pathname === `/repos/${REPO}`) {
        return jsonResponse({ id: REPO_ID, fork: false });
      }
      if (url.pathname === `/repos/${REPO}/pulls/${PR_NUMBER}`) {
        return jsonResponse(state.prBody, state.prStatus);
      }
      if (url.pathname === `/repos/${REPO}/pulls/${PR_NUMBER}/reviews`) {
        return jsonResponse([]);
      }
      if (url.pathname.startsWith(`/repos/${REPO}/actions/runs/`)) {
        return jsonResponse(state.runBody, state.runStatusCode);
      }
      if (url.pathname === `/repos/${REPO}/issues/${PR_NUMBER}/comments`) {
        if (url.searchParams.get("sort") === "created") {
          const page = Number(url.searchParams.get("page") ?? "1");
          const entry = state.commentsPages.get(page) ?? { status: 200, body: [] };
          return jsonResponse(entry.body, entry.status);
        }
        return jsonResponse([]);
      }
      if (url.pathname === `/repos/${REPO}/issues/comments/13579`) {
        return jsonResponse({
          id: 13579,
          issue_url: `https://api.github.com/repos/${REPO}/issues/${PR_NUMBER}`,
          body: COMMAND_BODY,
          updated_at: "2026-10-09T00:00:00Z",
          author_association: "OWNER",
          user: { id: 12345678, login: "octocat", type: "User" },
        });
      }
      if (url.pathname === `/repos/${REPO}/pulls/comments/13579`) {
        return jsonResponse({
          id: 13579,
          pull_request_url: `https://api.github.com/repos/${REPO}/pulls/${PR_NUMBER}`,
          body: COMMAND_BODY,
          updated_at: "2026-10-09T00:00:00Z",
          author_association: "OWNER",
          user: { id: 12345678, login: "octocat", type: "User" },
        });
      }
      throw new Error(`unexpected fetch call: ${url.pathname}${url.search}`);
    }),
  );

  return state;
}

beforeEach(() => {
  oidc.verify.mockReset();
  oidc.verify.mockResolvedValue(makeClaims());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("v6 authoritative writer ownership", () => {
  it("parses valid requests and fails closed on unknown or malformed fields", () => {
    const parsed = parseV6WriterOwnershipRequest(makeOwnership());
    expect(parsed).toEqual(makeOwnership());

    for (const invalid of [
      null,
      [],
      "not-an-object",
      { ...makeOwnership(), extra: 1 },
      (() => {
        const copy = { ...makeOwnership() } as Record<string, unknown>;
        delete copy.operation_id;
        return copy;
      })(),
      makeOwnership({ schema: "v6-writer-ownership-v2" }),
      makeOwnership({ repository_id: 0 }),
      makeOwnership({ repository_id: 1.5 }),
      makeOwnership({ pull_request: -1 }),
      makeOwnership({ writer_epoch: 0 }),
      makeOwnership({ writer_epoch: "1" }),
      makeOwnership({ operation_id: "" }),
      makeOwnership({ operation_id: " op-1" }),
      makeOwnership({ operation_id: "op-1\nop-2" }),
      makeOwnership({ operation_id: "x".repeat(257) }),
      makeOwnership({ head_sha: "not-a-sha" }),
      makeOwnership({ head_sha: "A".repeat(40) }),
    ]) {
      expect(() => parseV6WriterOwnershipRequest(invalid)).toThrow(
        "broker_writer_ownership_invalid",
      );
    }
  });

  it.each([
    ["review_publish", "review", "pull_request", "pull_request"],
    ["review_session", "review", "pull_request", "pull_request"],
    ["check_publish", "review", "pull_request", "pull_request"],
    ["inline_reply", "reply", "pull_request_review_comment", "review_comment"],
    ["issue_reply", "command", "issue_comment", "issue_comment"],
    ["thread_resolve", "review", "pull_request", "pull_request"],
  ] as const)(
    "grants token and v6_writer_ownership_proof for valid active writer (%s)",
    async (capability, purpose, eventName, sourceKind) => {
      const state = installGitHubFetchMock();
      const claims = makeClaims({ event_name: eventName });
      oidc.verify.mockResolvedValue(claims);
      const attestation = makeAttestation(purpose, claims, sourceKind);
      const ownership = makeOwnership({ writer_epoch: 2 });

      const body: Record<string, unknown> = {
        oidc_token: "signed-jwt",
        capability,
        session_attestation: attestation,
        v6_writer_ownership: ownership,
      };
      if (capability === "review_session") {
        body.session = {
          repository_id: REPO_ID,
          pull_request: PR_NUMBER,
          head_sha: SHA,
        };
      }

      const res = await worker.fetch(tokenHttpRequest(body), makeEnv());
      expect(res.status).toBe(200);
      const payload = (await res.json()) as Record<string, unknown>;
      expect(payload.token).toBe("ghs_test_token");
      expect(payload.capability).toBe(capability);
      expect(payload.v6_writer_ownership_proof).toEqual({
        schema: "v6-writer-ownership-proof-v1",
        verified_by_broker: true,
        repository_id: REPO_ID,
        pull_request: PR_NUMBER,
        operation_id: "op-1",
        authoritative_writer_epoch: 2,
        active_run_id: 10000000001,
        active_run_attempt: 1,
        head_sha: SHA,
      });
      expect(
        state.calledPaths.some((p) =>
          p.includes("/actions/runs/10000000001/attempts/1"),
        ),
      ).toBe(true);
    },
  );

  it("keeps v4/v5 callers omitting v6_writer_ownership 100% unchanged", async () => {
    const state = installGitHubFetchMock();
    const res = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
      }),
      makeEnv(),
    );
    expect(res.status).toBe(200);
    const payload = (await res.json()) as Record<string, unknown>;
    expect(payload).toEqual({
      token: "ghs_test_token",
      capability: "review_publish",
    });
    expect(
      state.calledPaths.some((p) => p.includes("/actions/runs/")),
    ).toBe(false);
    expect(
      state.calledPaths.some((p) => p.includes("/issues/7/comments")),
    ).toBe(false);
  });

  it("rejects unsupported capabilities and mismatched scope with 400", async () => {
    installGitHubFetchMock();
    for (const cap of ["review_status", "review_actions", "learning_write"]) {
      const res = await worker.fetch(
        tokenHttpRequest({
          oidc_token: "signed-jwt",
          capability: cap,
          session_attestation: makeAttestation("review"),
          v6_writer_ownership: makeOwnership(),
        }),
        makeEnv(),
      );
      expect(res.status).toBe(400);
      const data = (await res.json()) as { diagnostic: { code: string } };
      expect(data.diagnostic.code).toBe("broker_writer_ownership_invalid");
    }

    const mismatchRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership({ pull_request: 99 }),
      }),
      makeEnv(),
    );
    expect(mismatchRes.status).toBe(400);
    const mismatchData = (await mismatchRes.json()) as {
      diagnostic: { code: string };
    };
    expect(mismatchData.diagnostic.code).toBe("broker_writer_ownership_invalid");
  });

  it("rejects stale writer_epoch < max_writer_epoch with 409 and ignores non-App comments", async () => {
    const spoofedHumanComment = {
      id: 10,
      performed_via_github_app: null,
      body: await formatMarker(
        {
          schema: "reviewsensei:v6:ownership:v1",
          operation_id: "op-1",
          writer_epoch: 99,
          run_id: 999999,
          run_attempt: 1,
        },
        "ownership",
      ),
    };
    const state = installGitHubFetchMock({
      commentsPages: new Map([[1, { status: 200, body: [spoofedHumanComment] }]]),
    });

    // Human-spoofed marker is ignored -> epoch 1 still succeeds.
    const okRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership({ writer_epoch: 1 }),
      }),
      makeEnv(),
    );
    expect(okRes.status).toBe(200);

    // Now add an App-authored epoch 3 intent marker -> epoch 2 must fail with 409.
    const appEpoch3Comment = {
      id: 11,
      performed_via_github_app: { id: APP_ID },
      body: await formatMarker({
        schema: "reviewsensei:v6:intent:v1",
        record_id: "intent:op-1:3",
        operation_id: "op-1",
        writer_epoch: 3,
        pr_head_sha: SHA,
        effect_kind: "pr_review_publish",
        payload_sha256: "c".repeat(64),
        target_digest: "t1",
      }),
    };
    state.commentsPages.set(1, {
      status: 200,
      body: [spoofedHumanComment, appEpoch3Comment],
    });

    const staleRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership({ writer_epoch: 2 }),
      }),
      makeEnv(),
    );
    expect(staleRes.status).toBe(409);
    const staleData = (await staleRes.json()) as {
      diagnostic: { code: string };
    };
    expect(staleData.diagnostic.code).toBe("broker_stale_writer_rejected");
  });

  it("rejects equal writer_epoch owned by a different run_id or run_attempt with 409", async () => {
    const otherRunOwnership = {
      id: 20,
      performed_via_github_app: { id: APP_ID },
      body: await formatMarker(
        {
          schema: "reviewsensei:v6:ownership:v1",
          operation_id: "op-1",
          writer_epoch: 2,
          run_id: 20000000002,
          run_attempt: 1,
        },
        "ownership",
      ),
    };
    const state = installGitHubFetchMock({
      commentsPages: new Map([[1, { status: 200, body: [otherRunOwnership] }]]),
    });

    const diffRunRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership({ writer_epoch: 2 }),
      }),
      makeEnv(),
    );
    expect(diffRunRes.status).toBe(409);
    expect(
      ((await diffRunRes.json()) as { diagnostic: { code: string } }).diagnostic
        .code,
    ).toBe("broker_stale_writer_rejected");

    // Same run_id, newer run_attempt 2 owns epoch 2 -> attempt 1 is stale (409).
    const newerAttemptOwnership = {
      id: 21,
      performed_via_github_app: { id: APP_ID },
      body: await formatMarker(
        {
          schema: "reviewsensei:v6:ownership:v1",
          operation_id: "op-1",
          writer_epoch: 2,
          run_id: 10000000001,
          run_attempt: 2,
        },
        "ownership",
      ),
    };
    state.commentsPages.set(1, { status: 200, body: [newerAttemptOwnership] });

    const olderAttemptRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership({ writer_epoch: 2 }),
      }),
      makeEnv(),
    );
    expect(olderAttemptRes.status).toBe(409);
    expect(
      ((await olderAttemptRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_stale_writer_rejected");

    // Same (run_id, run_attempt=2) owns epoch 2 -> succeeds!
    const claimsAttempt2 = makeClaims({ run_attempt: "2" });
    oidc.verify.mockResolvedValue(claimsAttempt2);
    state.runBody = {
      id: 10000000001,
      run_attempt: 2,
      status: "in_progress",
    };
    const activeAttempt2Res = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review", claimsAttempt2),
        v6_writer_ownership: makeOwnership({ writer_epoch: 2 }),
      }),
      makeEnv(),
    );
    expect(activeAttempt2Res.status).toBe(200);
  });

  it.each(["completed", "cancelled", "queued"])(
    "rejects non-in_progress workflow run (%s) with 409",
    async (runStatus) => {
      installGitHubFetchMock({
        runBody: {
          id: 10000000001,
          run_attempt: 1,
          status: runStatus,
        },
      });
      const res = await worker.fetch(
        tokenHttpRequest({
          oidc_token: "signed-jwt",
          capability: "review_publish",
          session_attestation: makeAttestation("review"),
          v6_writer_ownership: makeOwnership(),
        }),
        makeEnv(),
      );
      expect(res.status).toBe(409);
      const data = (await res.json()) as { diagnostic: { code: string } };
      expect(data.diagnostic.code).toBe("broker_stale_writer_rejected");
    },
  );

  it("rejects closed PR and moved head_sha with 409", async () => {
    const state = installGitHubFetchMock({
      prBody: {
        state: "closed",
        head: { sha: SHA },
        base: { repo: { id: REPO_ID } },
      },
    });
    const closedRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(closedRes.status).toBe(409);
    expect(
      ((await closedRes.json()) as { diagnostic: { code: string } }).diagnostic
        .code,
    ).toBe("broker_stale_writer_rejected");

    state.prBody = {
      state: "open",
      head: { sha: OTHER_SHA },
      base: { repo: { id: REPO_ID } },
    };
    const movedHeadRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(movedHeadRes.status).toBe(409);
    expect(
      ((await movedHeadRes.json()) as { diagnostic: { code: string } }).diagnostic
        .code,
    ).toBe("broker_session_head_rejected");
  });

  it("fails closed with 503 broker_writer_ownership_unverified on missing/non-200 or truncated lookups", async () => {
    // 1. Non-200 run lookup
    const state = installGitHubFetchMock({
      runStatusCode: 404,
      runBody: { message: "Not Found" },
    });
    const missingRunRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(missingRunRes.status).toBe(503);
    expect(
      ((await missingRunRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_writer_ownership_unverified");

    // 2. Non-200 PR lookup
    state.runStatusCode = 200;
    state.runBody = { id: 10000000001, run_attempt: 1, status: "in_progress" };
    state.prStatus = 502;
    state.prBody = { message: "Bad Gateway" };
    const missingPrRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(missingPrRes.status).toBe(503);
    expect(
      ((await missingPrRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_writer_ownership_unverified");

    // 3. Non-200 comment scan
    state.prStatus = 200;
    state.prBody = {
      state: "open",
      head: { sha: SHA },
      base: { repo: { id: REPO_ID } },
    };
    state.commentsPages.set(1, { status: 500, body: { message: "Error" } });
    const failedCommentsRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(failedCommentsRes.status).toBe(503);
    expect(
      ((await failedCommentsRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_writer_ownership_unverified");

    // 4. Truncated comment pagination (> 25 full pages of 100 comments)
    const fullPage = Array.from({ length: 100 }, (_, i) => ({
      id: i + 1,
      performed_via_github_app: null,
      body: "ordinary comment",
    }));
    for (let page = 1; page <= 26; page += 1) {
      state.commentsPages.set(page, { status: 200, body: fullPage });
    }
    const truncatedRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(truncatedRes.status).toBe(503);
    expect(
      ((await truncatedRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_writer_ownership_unverified");

    // 5. Tampered App-authored marker digest fails closed (503).
    state.commentsPages.set(1, {
      status: 200,
      body: [
        {
          id: 99,
          performed_via_github_app: { id: APP_ID },
          body: `<!-- reviewsensei:v6:record sha256=${"0".repeat(64)} payload={"schema":"reviewsensei:v6:ownership:v1","operation_id":"op-1","writer_epoch":1,"run_id":10000000001,"run_attempt":1} -->`,
        },
      ],
    });
    const tamperedRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(tamperedRes.status).toBe(503);
    expect(
      ((await tamperedRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_writer_ownership_unverified");
  });

  it("rejects release_only with v6_writer_ownership (400) and repository mismatch (403)", async () => {
    const state = installGitHubFetchMock();
    const releaseOnlyRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_session",
        release_only: true,
        session: {
          repository_id: REPO_ID,
          pull_request: PR_NUMBER,
          head_sha: SHA,
        },
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(releaseOnlyRes.status).toBe(400);
    expect(
      ((await releaseOnlyRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_writer_ownership_invalid");

    state.prBody = {
      state: "open",
      head: { sha: SHA },
      base: { repo: { id: 111111 } },
    };
    const repoMismatchRes = await worker.fetch(
      tokenHttpRequest({
        oidc_token: "signed-jwt",
        capability: "review_publish",
        session_attestation: makeAttestation("review"),
        v6_writer_ownership: makeOwnership(),
      }),
      makeEnv(),
    );
    expect(repoMismatchRes.status).toBe(403);
    expect(
      ((await repoMismatchRes.json()) as { diagnostic: { code: string } })
        .diagnostic.code,
    ).toBe("broker_repository_rejected");
  });
});
