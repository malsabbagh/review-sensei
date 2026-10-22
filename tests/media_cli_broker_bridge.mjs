/** Offline media CLI IPC fixture. Authority policy and signatures are production.
 * All external HTTP is synthetic; this file is never a deployed broker.
 */
import { registerHooks, createRequire } from "node:module";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const require = createRequire(new URL("../deploy/cloudflare/package.json", import.meta.url));
const typescript = require("typescript");
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "cloudflare:workers") {
      specifier = new URL("../deploy/cloudflare/test/shims/cloudflare-workers.ts", import.meta.url).href;
    } else if (specifier.startsWith(".") && context.parentURL) {
      const candidate = new URL(specifier + ".ts", context.parentURL);
      if (existsSync(fileURLToPath(candidate))) specifier = candidate.href;
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (url.endsWith(".json")) return { format: "module", shortCircuit: true, source: `export default ${JSON.stringify(JSON.parse(readFileSync(fileURLToPath(url), "utf8")))};` };
    if (url.endsWith(".yml")) return { format: "module", shortCircuit: true, source: `export default ${JSON.stringify(readFileSync(fileURLToPath(url), "utf8"))};` };
    if (!url.endsWith(".ts")) return nextLoad(url, context);
    return { format: "module", shortCircuit: true, source: typescript.transpileModule(readFileSync(fileURLToPath(url), "utf8"), { compilerOptions: { target: typescript.ScriptTarget.ES2022, module: typescript.ModuleKind.ESNext }, fileName: fileURLToPath(url) }).outputText };
  },
});

const { consumingBroker, SHA, WORKFLOW } = await import("../deploy/cloudflare/test/epic238-consuming-broker-fixture.ts");
const { TokenBroker } = await import("../deploy/cloudflare/src/token-broker.ts");
const { GitHubApi } = await import("../deploy/cloudflare/src/github-api.ts");
const { resetOidcJwksCacheForTesting } = await import("../deploy/cloudflare/src/oidc.ts");
const { brokerDiagnostic } = await import("../deploy/cloudflare/src/broker-diagnostics.ts");
const fixture = await consumingBroker();
// The existing fixture owns a generated test-only private key and real signer.
// Its environment is copied only to change the synthetic repository projection.
const env = { ...fixture.broker.env, GITHUB_API_URL: "https://api.github.test" };
const broker = new TokenBroker(env, new GitHubApi(env));
let authority;
let attempts;
globalThis.fetch = async (input, init) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
  const method = init?.method ?? "GET";
  attempts.push({ origin: "broker-internal", method, path: url.pathname });
  if (url.href === "https://token.actions.githubusercontent.com/.well-known/jwks") return fixture.transport(input, init);
  if (url.hostname !== "api.github.test") throw new Error("fixture_external_fetch_refused");
  if (url.pathname === "/repos/malsabbagh/review-sensei/git/ref/tags/v5") return Response.json({ object: { type: "commit", sha: SHA } });
  if (url.pathname === "/repos/owner/repo/installation") return fixture.transport("https://api.github.test/repos/acme/widgets/installation", init);
  if (url.pathname === "/app/installations/2468/access_tokens" && method === "POST") return fixture.transport(input, init);
  if (url.pathname === "/repos/owner/repo") return Response.json({ id: 1, fork: authority.fork });
  if (url.pathname === "/repos/owner/repo/pulls/2") return Response.json({ number: 2, state: "open", draft: authority.draft, base: { sha: authority.base }, head: { sha: authority.head } });
  if (url.pathname === "/repos/owner/repo/issues/comments/20") return authority.source === null ? Response.json({ message: "Not Found" }, { status: 404 }) : Response.json(authority.source);
  if (url.pathname === "/app") return fixture.transport(input, init);
  if (url.pathname === "/repos/owner/repo/pulls/2/reviews") {
    const size = Number(url.searchParams.get("per_page") ?? "100");
    const page = Number(url.searchParams.get("page") ?? "1");
    return Response.json(authority.reviews.slice((page - 1) * size, page * size));
  }
  if (url.pathname === "/repos/owner/repo/issues/2/comments") return Response.json([]);
  throw new Error("fixture_external_fetch_refused");
};

const lines = createInterface({ input: process.stdin });
for await (const line of lines) {
  const request = JSON.parse(line);
  let result;
  let status = 200;
  attempts = [];
  try {
    if (request.action === "oidc") {
      result = await fixture.oidcToken({ repository: "owner/repo", repository_owner: "owner", repository_id: "1", actor: "alice", actor_id: "123", run_id: "123", workflow_ref: "owner/repo/.github/workflows/review-sensei-review.yml@refs/heads/main", job_workflow_ref: WORKFLOW, job_workflow_sha: SHA });
    } else if (request.action === "reset_oidc_cache") {
      // A new cold Worker invocation is an independent fixture condition, not
      // an uncharged fetch or a refund from the measured command allowance.
      resetOidcJwksCacheForTesting();
      result = null;
    } else if (request.action === "exchange") {
      authority = request.authority;
      result = await broker.exchange(request.body);
    } else if (request.action === "shutdown") {
      fixture.close();
      lines.close();
      result = null;
    } else throw new Error("fixture_action_invalid");
  } catch (error) {
    status = 403;
    const category = error instanceof Error && /^(broker|github|oidc)_[a-z0-9_]+$/.test(error.message) ? error.message : "fixture_rejected";
    result = { error: category, diagnostic: brokerDiagnostic(error) };
  }
  process.stdout.write(JSON.stringify({ id: request.id, status, result, attempts }) + "\n");
  if (request.action === "shutdown") break;
}
