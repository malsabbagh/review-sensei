/** Synthetic external IO, production broker/JWKS/GitHub policy. Never deploy. */
import assert from "node:assert/strict";
import { registerHooks, createRequire } from "node:module";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

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

const { consumingBroker, requestAttestation, SHA } = await import("../deploy/cloudflare/test/epic238-consuming-broker-fixture.ts");
const { TokenBroker } = await import("../deploy/cloudflare/src/token-broker.ts");
const { GitHubApi } = await import("../deploy/cloudflare/src/github-api.ts");
const { resetOidcJwksCacheForTesting } = await import("../deploy/cloudflare/src/oidc.ts");

function pktLine(value) { return (Buffer.byteLength(value) + 4).toString(16).padStart(4, "0") + value; }
function witnessReads(attempts) {
  return {
    review: attempts.filter(item => item.path === "/repos/acme/widgets/pulls/7/reviews").length,
    comment: attempts.filter(item => item.path === "/repos/acme/widgets/issues/7/comments").length,
  };
}
const advertisement = pktLine("# service=git-upload-pack\n") + "0000" + pktLine(`${SHA} refs/tags/v5\0multi_ack\n`) + "0000";
const output = [];
for (const capability of ["review_session", "check_publish"]) {
  for (const mode of ["git", "fallback-annotated"]) {
    for (const history of capability === "review_session" ? ["owned-review", "no-review", "full-page"] : ["not-read"]) {
      const fixture = await consumingBroker();
      const env = { ...fixture.broker.env, GITHUB_API_URL: "https://api.github.com" };
      const broker = new TokenBroker(env, new GitHubApi(env));
      const attempts = [];
      resetOidcJwksCacheForTesting();
      globalThis.fetch = async (input, init) => {
        const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
        const method = init?.method ?? "GET";
        attempts.push({ method, path: url.pathname });
        if (url.hostname === "github.com") return mode === "git" ? new Response(advertisement) : new Response("", { status: 503 });
        if (url.pathname === "/repos/malsabbagh/review-sensei/git/ref/tags/v5" && mode === "fallback-annotated") return Response.json({ object: { type: "tag", sha: "b".repeat(40) } });
        if (url.pathname.startsWith("/repos/malsabbagh/review-sensei/git/tags/")) return Response.json({ object: { type: "commit", sha: SHA } });
        if (url.pathname === "/repos/acme/widgets/pulls/7/reviews") {
          if (history === "full-page") return Response.json(Array.from({ length: 100 }, (_, index) => ({ id: index + 1, user: { type: "Bot", login: "reviewsensei[bot]" }, state: "COMMENTED", commit_id: SHA })));
          if (history === "owned-review") return Response.json([{ id: 42, user: { type: "Bot", login: "reviewsensei[bot]" }, state: "COMMENTED", commit_id: SHA }]);
        }
        if (url.pathname === "/repos/acme/widgets/issues/comments/13579") return Response.json({ id: 13579, body: "@reviewsensei I reviewed the media files and I approve", updated_at: "2026-10-22T00:00:00Z", user: { id: 12345678, login: "octocat", type: "User" }, author_association: "OWNER", issue_url: "https://api.github.com/repos/acme/widgets/issues/7" });
        const mapped = new URL(url);
        if (mapped.hostname === "api.github.com") mapped.hostname = "api.github.test";
        return fixture.transport(mapped, init);
      };
      const control = { schema_version: "1.0", operation_id: "c".repeat(64), request_id: "d".repeat(64), deadline_unix_ms: Date.now() + 60_000, max_dispatches: history === "full-page" ? 10 : 60 };
      let receipt;
      let error;
      try {
        const result = await broker.exchange({ oidc_token: await fixture.oidcToken(), capability, control_budget: control, ...(capability === "review_session" ? { session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: requestAttestation("command") } : {}) });
        receipt = result.control_receipt;
        assert.equal(receipt.dispatched, attempts.length);
      } catch (caught) { error = caught.message; }
      if (history === "full-page") {
        assert.equal(error, "broker_control_budget_exhausted");
        assert.equal(attempts.length, 10);
      } else {
        assert.equal(error, undefined);
        assert.equal(attempts.length, (capability === "check_publish" ? 6 : history === "owned-review" ? 10 : 11) + (mode === "fallback-annotated" ? 2 : 0));
      }
      output.push({ capability, mode, history, dispatched: attempts.length, ...(receipt ? { admitted: receipt.max_dispatches } : {}), ...(error ? { error } : {}), attempts: structuredClone(attempts) });
      if (mode === "git" && (history === "owned-review" || capability === "check_publish")) {
        resetOidcJwksCacheForTesting();
        const refusedAttempts = [];
        const priorFetch = globalThis.fetch;
        globalThis.fetch = async (input, init) => {
          const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
          refusedAttempts.push({ method: init?.method ?? "GET", path: url.pathname });
          return priorFetch(input, init);
        };
        const maximum = capability === "review_session" ? 9 : 5;
        const request = { ...control, max_dispatches: maximum, deadline_unix_ms: Date.now() + 60_000 };
        await assert.rejects(broker.exchange({ oidc_token: await fixture.oidcToken(), capability, control_budget: request, ...(capability === "review_session" ? { session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: requestAttestation("command") } : {}) }), { message: "broker_control_budget_exhausted" });
        assert.equal(refusedAttempts.length, maximum);
        globalThis.fetch = priorFetch;
        output.push({ capability, mode, history, admitted: maximum, dispatched: refusedAttempts.length, error: "broker_control_budget_exhausted", attempts: refusedAttempts });
      }
    }
  }
}
// Two actual exchanges in one operation: the first completes the existing
// immutable public-JWKS cache. No warm-up dispatch is hidden or refunded.
for (const order of [["review_session", "check_publish"], ["check_publish", "review_session"]]) {
  const fixture = await consumingBroker();
  const env = { ...fixture.broker.env, GITHUB_API_URL: "https://api.github.com" };
  const broker = new TokenBroker(env, new GitHubApi(env));
  const attempts = [];
  resetOidcJwksCacheForTesting();
  globalThis.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    attempts.push({ method: init?.method ?? "GET", path: url.pathname });
    if (url.hostname === "github.com") return new Response(advertisement);
    if (url.pathname === "/repos/acme/widgets/pulls/7/reviews") return Response.json([{ id: 42, user: { type: "Bot", login: "reviewsensei[bot]" }, state: "COMMENTED", commit_id: SHA }]);
    if (url.pathname === "/repos/acme/widgets/issues/comments/13579") return Response.json({ id: 13579, body: "@reviewsensei I reviewed the media files and I approve", updated_at: "2026-10-22T00:00:00Z", user: { id: 12345678, login: "octocat", type: "User" }, author_association: "OWNER", issue_url: "https://api.github.com/repos/acme/widgets/issues/7" });
    const mapped = new URL(url);
    if (mapped.hostname === "api.github.com") mapped.hostname = "api.github.test";
    return fixture.transport(mapped, init);
  };
  const sequence = [];
  for (const [index, capability] of order.entries()) {
    const liability = (capability === "check_publish" ? 6 : 10) - index;
    const control = { schema_version: "1.0", operation_id: "c".repeat(64), request_id: String(index + 1).repeat(64), deadline_unix_ms: Date.now() + 60_000, max_dispatches: liability };
    const start = attempts.length;
    const result = await broker.exchange({ oidc_token: await fixture.oidcToken(), capability, control_budget: control, ...(capability === "review_session" ? { session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: requestAttestation("command") } : {}) });
    assert.equal(result.control_receipt.dispatched, liability);
    assert.equal(attempts.length - start, liability);
    sequence.push({ capability, dispatched: liability, admitted: liability });
  }
  assert.equal(attempts.length, 15);
  output.push({ mode: "git", history: "owned-review", sequence, internal_dispatches: attempts.length, caller_posts: 2, combined_liability: 17, attempts });
}
// Unattended analysis does not carry a command attestation. Exercise the actual
// Git and annotated-tag fallback with the caller's admitted liability: the old
// underfunded prefix-only value, and the funded value that reaches the reader's
// own per-witness 10-request page ladder. The funded value is the caller's
// admitted liability, not a second copy of the ceiling: exact drift is pinned
// to the ADR literal in test_analysis_admission_budget, so the Python oracle
// supplies the production constant and this harness only refuses funding below
// the documented minimum.
const analysisMaximum = Number(process.argv[2] ?? 30);
assert.ok(Number.isSafeInteger(analysisMaximum) && analysisMaximum >= 30,
  "analysis admission must fund at least the documented 30-dispatch ceiling");
for (const mode of ["git", "fallback-annotated"]) {
  for (const history of ["empty", "full-page"]) {
    for (const maximum of [10, analysisMaximum]) {
      const fixture = await consumingBroker();
      const env = { ...fixture.broker.env, GITHUB_API_URL: "https://api.github.com" };
      const broker = new TokenBroker(env, new GitHubApi(env));
      const attempts = [];
      resetOidcJwksCacheForTesting();
      globalThis.fetch = async (input, init) => {
        const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
        attempts.push({ method: init?.method ?? "GET", path: url.pathname });
        if (url.hostname === "github.com") return mode === "git" ? new Response(advertisement) : new Response("", { status: 503 });
        if (url.pathname === "/repos/malsabbagh/review-sensei/git/ref/tags/v5" && mode === "fallback-annotated") return Response.json({ object: { type: "tag", sha: "b".repeat(40) } });
        if (url.pathname.startsWith("/repos/malsabbagh/review-sensei/git/tags/")) return Response.json({ object: { type: "commit", sha: SHA } });
        if (url.pathname === "/repos/acme/widgets/pulls/7/reviews" && history === "full-page") return Response.json(Array.from({ length: 100 }, (_, index) => ({ id: index + 1, user: { type: "Bot", login: "reviewsensei[bot]" }, state: "COMMENTED", commit_id: SHA })));
        const mapped = new URL(url);
        if (mapped.hostname === "api.github.com") mapped.hostname = "api.github.test";
        return fixture.transport(mapped, init);
      };
      try {
        for (const temperature of ["cold", "warm"]) {
          const start = attempts.length;
          const control = { schema_version: "1.0", operation_id: "a".repeat(64), request_id: (temperature === "cold" ? "b" : "c").repeat(64), deadline_unix_ms: Date.now() + 60_000, max_dispatches: maximum };
          let result;
          let error;
          try {
            result = await broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session", session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, control_budget: control });
          } catch (caught) { error = caught.message; }
          const dispatched = attempts.length - start;
          // JWKS, the Git advertisement or annotated-tag fallback, and the four
          // installation/repository/capability reads, then head, App identity and
          // up to the reader's own 10-request ladder per enrollment witness.
          const prefix = 8 + (mode === "fallback-annotated" ? 2 : 0);
          if (history === "full-page" && maximum < prefix + 10) {
            // Each arm is cold by construction: the cache is reset above and the
            // refusal breaks before the warm exchange runs. The deterministic
            // prefix is therefore charged in full, and the budget refuses the
            // witness page the ladder asked for once its slots run out.
            assert.equal(error, "broker_control_budget_exhausted");
            assert.equal(dispatched, maximum);
            const reads = witnessReads(attempts.slice(start));
            assert.equal(reads.review, Math.max(0, maximum - prefix));
            assert.equal(reads.comment, 0);
            assert.equal(result, undefined);
          } else if (history === "full-page") {
            // The funded liability covers the whole ladder, so the refusal is now
            // the reader's closed page cap rather than an unfunded dispatch. Ten
            // review-witness reads prove the ladder ran to that cap; the
            // operation record is never read because the first witness fails.
            assert.equal(error, "github_session_witness_failed");
            assert.equal(dispatched, prefix + 10);
            const reads = witnessReads(attempts.slice(start));
            assert.equal(reads.review, 10);
            assert.equal(reads.comment, 0);
            assert.equal(result, undefined);
          } else if (mode === "fallback-annotated" && maximum === 10) {
            // The ten-dispatch cap is the whole prefix, so refusing its next
            // dispatch is refusing the operation record's first page, not a
            // witness page that was ever charged.
            assert.equal(error, "broker_control_budget_exhausted");
            assert.equal(dispatched, maximum);
            const reads = witnessReads(attempts.slice(start));
            assert.equal(reads.review, 0);
            assert.equal(reads.comment, 0);
            assert.equal(result, undefined);
          } else {
            assert.equal(error, undefined);
            assert.equal(dispatched, prefix + 2 - (temperature === "warm" ? 1 : 0));
            // An empty history is not prefix-only: both enrollment witnesses are
            // read once, the operation record because the review witness finds
            // no match. A witness ladder stops early only on a partial page, so
            // multi-page funding is observable solely in the full-page arm.
            const reads = witnessReads(attempts.slice(start));
            assert.equal(reads.review, 1);
            assert.equal(reads.comment, 1);
            assert.equal(result.session_state, "enrolled");
            assert.deepEqual(result.control_receipt, { ...control, dispatched });
          }
          output.push({ capability: "analysis-admission", mode, history, temperature, admitted: maximum, dispatched, ...(error ? { error } : { session_state: result.session_state }), attempts: attempts.slice(start) });
          if (error) break;
        }
      } finally { fixture.close(); }
    }
  }
}
process.stdout.write(JSON.stringify({ schema_version: "media-broker-profile-v1", profiles: output }, null, 2) + "\n");
