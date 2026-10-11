/** Production broker over signed grants. GitHub/OIDC inputs are synthetic.
 * Grant verification is never stubbed. The worker stores nothing.
 */
import { readFileSync } from "node:fs";
import { TokenBroker } from "../src/token-broker";
import { GitHubApi } from "../src/github-api";
import type { WorkerEnv } from "../src/env";

export const SHA = "a".repeat(40);
export const WORKFLOW = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5";

const feedbackVector = JSON.parse(readFileSync(new URL(
  "../../../tests/fixtures/feedback-session-attestation.json", import.meta.url,
), "utf8"));

export function feedbackAttestation() {
  const request = structuredClone(feedbackVector.request);
  request.issued_at = Math.floor(Date.now() / 1000);
  // A test reference, never a durable attempt or root authorization witness.
  request.mutation.read_accounting.deadline_unix_ms = Date.now() + 60_000;
  return request;
}

export function claims(jti = "assertion-1") {
  return {
    iss: "https://token.actions.githubusercontent.com", aud: "sts.reviewsensei.dev",
    exp: 1_800_000_000, iat: 1_700_000_000,
    sub: "repo:acme/widgets:pull_request", jti,
    repository: "acme/widgets", repository_owner: "acme", repository_id: "987654321",
    actor: "octocat", actor_id: "12345678", event_name: "issue_comment",
    workflow: "ReviewSensei review",
    workflow_ref: "acme/widgets/.github/workflows/review-sensei-review.yml@refs/heads/main",
    workflow_sha: SHA, job_workflow_ref: WORKFLOW, job_workflow_sha: SHA,
    ref: "refs/heads/main", sha: SHA, run_id: "10000000001", run_number: "42",
    run_attempt: "1", runner_environment: "github-hosted",
  };
}

export function requestAttestation(operation: "review" | "command" = "command") {
  return {
    version: 1, repository: "acme/widgets", repository_id: 987654321,
    pull_request: 7, head_sha: SHA, operation,
    source_comment_id: operation === "command" ? 13579 : null,
    run_id: "10000000001", issued_at: Math.floor(Date.now() / 1000),
    concurrency_group: "reviewsensei-session-987654321-7",
    job_workflow_ref: WORKFLOW, job_workflow_sha: SHA,
  };
}

const keyPair = crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"],
);

export async function consumingBroker(_filename = ":memory:", traceFile?: string) {
  const keys = await keyPair;
  const pem = Buffer.from(await crypto.subtle.exportKey("pkcs8", keys.privateKey)).toString("base64");
  const jwk = { ...await crypto.subtle.exportKey("jwk", keys.publicKey), kid: "synthetic-fixture", alg: "RS256", use: "sig" };
  const ledgerRequests: string[] = [];
  const env = {
    PUBLIC_WORKFLOW_TAG: "v5",
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: `-----BEGIN PRIVATE KEY-----\n${pem}\n-----END PRIVATE KEY-----`,
    GITHUB_API_URL: "https://api.github.test",
    REVIEWSENSEI_SIGNING_KEY: "test-signing-key",
  } as unknown as WorkerEnv;
  void traceFile;
  const githubInputs = {
    number: 7, state: "open", draft: false,
    base: "b".repeat(40),
    head: SHA,
    source: {
      id: 13579, body: "@sensei review continue", login: "octocat",
      authorId: 12345678, updatedAt: "2026-10-09T00:00:00Z",
      userType: "User", association: "OWNER",
      url: "https://api.github.test/repos/acme/widgets/issues/7", status: 200,
    },
    inlineSource: {
      id: 24680, body: "The linked caller rejects remote input.", login: "collaborator",
      authorId: 42, updatedAt: "2026-10-10T00:00:01Z",
      userType: "User", association: "COLLABORATOR", rootCommentId: 24680,
      url: "https://api.github.test/repos/acme/widgets/pulls/7", status: 200,
    },
  };
  const configureFeedback = () => {
    githubInputs.base = feedbackVector.request.feedback.base_sha;
    for (const source of feedbackVector.selection.sources) {
      const target = source.kind === "issue" ? githubInputs.source : githubInputs.inlineSource;
      Object.assign(target, {
        id: source.comment_id, body: source.body, login: source.author,
        authorId: source.author_id, updatedAt: source.updated_at,
        association: source.association, rootCommentId: source.root_comment_id,
      });
    }
    return feedbackAttestation();
  };
  const transportRequests: { method: string; path: string; kind: string }[] = [];
  const transport = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const method = init?.method ?? (input instanceof Request ? input.method : "GET");
    transportRequests.push({ method, path: url.pathname, kind: url.hostname === "token.actions.githubusercontent.com" ? "oidc" : "broker-github" });
    let body: unknown;
    let status = 200;
    if (url.href === "https://token.actions.githubusercontent.com/.well-known/jwks") body = { keys: [jwk] };
    else if (url.hostname !== "api.github.test") throw new Error("unexpected fixture transport host");
    else if (url.pathname === "/repos/malsabbagh/review-sensei/git/ref/tags/v5") body = { object: { type: "commit", sha: SHA } };
    else if (url.pathname === "/repos/acme/widgets/installation") body = { id: 2468 };
    else if (url.pathname === "/app/installations/2468/access_tokens" && method === "POST") {
      const request = JSON.parse(String(init?.body));
      body = { token: "synthetic-scoped", expires_at: new Date(Date.now() + 60_000).toISOString(), permissions: { metadata: "read", ...request.permissions } };
    } else if (url.pathname === "/repos/acme/widgets") body = { id: 987654321, fork: false };
    else if (url.pathname === "/repos/acme/widgets/pulls/7") body = { number: githubInputs.number, state: githubInputs.state, draft: githubInputs.draft, base: { sha: githubInputs.base }, head: { sha: githubInputs.head } };
    else if (url.pathname === "/repos/acme/widgets/issues/comments/13579") {
      const source = githubInputs.source;
      body = { id: source.id, body: source.body, updated_at: source.updatedAt, user: { id: source.authorId, login: source.login, type: source.userType }, author_association: source.association, issue_url: source.url };
      status = source.status;
    }     else if (url.pathname === "/repos/acme/widgets/pulls/comments/24680") {
      const source = githubInputs.inlineSource;
      body = { id: source.id, body: source.body, updated_at: source.updatedAt, user: { id: source.authorId, login: source.login, type: source.userType }, author_association: source.association, pull_request_url: source.url, in_reply_to_id: source.rootCommentId };
      status = source.status;
    } else if (url.pathname === "/app") body = { slug: "reviewsensei" };
    else if (url.pathname === "/repos/acme/widgets/pulls/7/reviews") body = [];
    else if (url.pathname === "/repos/acme/widgets/issues/7/comments") body = [];
    else { body = { message: "Not Found" }; status = 404; }
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  };
  let assertion = 0;
  const assertionPrefix = crypto.randomUUID();
  const oidcToken = async (overrides: Record<string, unknown> = {}) => {
    const now = Math.floor(Date.now() / 1000);
    const header = Buffer.from(JSON.stringify({ alg: "RS256", kid: "synthetic-fixture" })).toString("base64url");
    const payload = Buffer.from(JSON.stringify({ ...claims(`${assertionPrefix}-${assertion++}`), iat: now - 10, exp: now + 300, ...overrides })).toString("base64url");
    const content = `${header}.${payload}`;
    const signature = Buffer.from(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", keys.privateKey, new TextEncoder().encode(content))).toString("base64url");
    return `${content}.${signature}`;
  };
  return {
    broker: new TokenBroker(env, new GitHubApi(env)), ledgerRequests, githubInputs, configureFeedback,
    transport, transportRequests, oidcToken,
    grantCount: () => 0,
    close: () => undefined,
  };
}

export async function issue(
  fixture: Awaited<ReturnType<typeof consumingBroker>>,
  operation: "review" | "command" = "command",
) {
  return fixture.broker.exchange({
    oidc_token: await fixture.oidcToken(), capability: "review_session",
    session: { repository_id: 987654321, pull_request: 7, head_sha: SHA },
    session_attestation: requestAttestation(operation),
  });
}
