/** F fixture: production broker/ledger over actual transactional SQLite.
 * GitHub/OIDC inputs are synthetic; grant verification is never stubbed.
 * This supplies no Python host persistence or writer activation policy.
 */
import { DatabaseSync } from "node:sqlite";
import { BrokerLedger } from "../src/broker-ledger";
import { TokenBroker } from "../src/token-broker";
import { GitHubApi } from "../src/github-api";
import type { WorkerEnv } from "../src/env";

export const SHA = "a".repeat(40);
export const WORKFLOW = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5";

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

export async function consumingBroker(filename = ":memory:") {
  const keys = await keyPair;
  const pem = Buffer.from(await crypto.subtle.exportKey("pkcs8", keys.privateKey)).toString("base64");
  const jwk = { ...await crypto.subtle.exportKey("jwk", keys.publicKey), kid: "synthetic-fixture", alg: "RS256", use: "sig" };
  const database = new DatabaseSync(filename);
  const ledgerRequests: string[] = [];
  const sql = {
    exec(query: string, ...args: unknown[]) {
      // Production constructor creates several tables in one statement.
      if (query.trim().startsWith("CREATE TABLE")) {
        database.exec(query);
        return [];
      }
      const statement = database.prepare(query);
      return query.trim().startsWith("SELECT")
        ? statement.all(...args as never[])
        : (statement.run(...args as never[]), []);
    },
  };
  const context = {
    storage: {
      sql,
      transactionSync<T>(callback: () => T): T {
        database.exec("BEGIN IMMEDIATE");
        try {
          const value = callback();
          database.exec("COMMIT");
          return value;
        } catch (error) {
          database.exec("ROLLBACK");
          throw error;
        }
      },
    },
  } as unknown as DurableObjectState;
  const ledger = new BrokerLedger(context, {} as WorkerEnv);
  const env = {
    PUBLIC_WORKFLOW_TAG: "v5",
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: `-----BEGIN PRIVATE KEY-----\n${pem}\n-----END PRIVATE KEY-----`,
    GITHUB_API_URL: "https://api.github.test",
    BROKER_LEDGER: {
      idFromName: () => ({ name: "broker" }),
      get: () => ({ fetch: async (url: string, init: RequestInit) => {
        ledgerRequests.push(JSON.parse(String(init.body)).action);
        return ledger.fetch(new Request(url, init));
      } }),
    },
  } as unknown as WorkerEnv;
  const githubInputs = {
    base: "b".repeat(40),
    head: SHA,
    source: {
      id: 13579, body: "@sensei review continue", login: "octocat",
      authorId: 12345678, updatedAt: "2026-10-09T00:00:00Z",
      userType: "User", association: "OWNER",
    },
  };
  const transportRequests: { method: string; path: string; kind: string }[] = [];
  const transport = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const method = init?.method ?? (input instanceof Request ? input.method : "GET");
    transportRequests.push({ method, path: url.pathname, kind: url.hostname === "token.actions.githubusercontent.com" ? "oidc" : "broker-github" });
    let body: unknown;
    if (url.href === "https://token.actions.githubusercontent.com/.well-known/jwks") body = { keys: [jwk] };
    else if (url.hostname !== "api.github.test") throw new Error("unexpected fixture transport host");
    else if (url.pathname === "/repos/malsabbagh/review-sensei/git/ref/tags/v5") body = { object: { type: "commit", sha: SHA } };
    else if (url.pathname === "/repos/acme/widgets/installation") body = { id: 2468 };
    else if (url.pathname === "/app/installations/2468/access_tokens" && method === "POST") {
      const request = JSON.parse(String(init?.body));
      body = { token: "synthetic-scoped", expires_at: new Date(Date.now() + 60_000).toISOString(), permissions: { metadata: "read", ...request.permissions } };
    } else if (url.pathname === "/repos/acme/widgets") body = { id: 987654321, fork: false };
    else if (url.pathname === "/repos/acme/widgets/pulls/7") body = { state: "open", draft: false, base: { sha: githubInputs.base }, head: { sha: githubInputs.head } };
    else if (url.pathname === "/repos/acme/widgets/issues/comments/13579") {
      const source = githubInputs.source;
      body = { id: source.id, body: source.body, updated_at: source.updatedAt, user: { id: source.authorId, login: source.login, type: source.userType }, author_association: source.association, issue_url: "https://api.github.test/repos/acme/widgets/issues/7" };
    } else throw new Error(`unexpected fixture GitHub transport: ${method} ${url.pathname}`);
    return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
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
    broker: new TokenBroker(env, new GitHubApi(env)), ledger, ledgerRequests, githubInputs,
    transport, transportRequests, oidcToken,
    grantCount: () => Number(database.prepare("SELECT COUNT(*) AS count FROM broker_session_grants").get()?.count),
    close: () => database.close(),
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
