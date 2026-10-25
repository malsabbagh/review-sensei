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
const { GitHubApi, readBoundedHistory } = await import("../deploy/cloudflare/src/github-api.ts");
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
// own per-witness 10-request page ladder. Usage:
//   node tests/media_broker_profile.mjs [analysis-liability]
// The optional positional argument is the analysis-admission liability and must
// be a decimal integer, so a shifted or non-numeric argument fails at parsing
// rather than as an unexplained reservation error. The only special arm is the
// pre-ADR prefix-only 12; every other value must fund at least the current
// 30-dispatch ceiling. The exact ceiling is not restated here as an upper bound:
// it is pinned to the production constant by test_analysis_admission_budget and
// by the qualification oracle, which pass that constant in, so a silent drift
// fails there instead of being echoed back as this harness's own expectation.
const analysisArgument = process.argv[2];
const analysisMaximum = analysisArgument === undefined ? 30 : Number(analysisArgument);
assert.ok(
  analysisArgument === undefined ||
    (/^\d+$/.test(analysisArgument) && Number.isSafeInteger(analysisMaximum)),
  `analysis admission liability must be a decimal integer, got ${JSON.stringify(analysisArgument)}`,
);
assert.ok(analysisMaximum === 12 || analysisMaximum >= 30,
  "analysis admission must be the pre-ADR 12 or fund the 30-dispatch ceiling");
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
            const reads = witnessReads(attempts.slice(start));
            assert.equal(reads.review, 10);
            assert.equal(reads.comment, 0);
            // Every dispatch beyond the deterministic prefix is a witness read,
            // which proves the witness paths are the only place the funded arm
            // spends past the prefix; the message points at the prefix if a
            // future exchange adds a dispatch outside those paths.
            assert.equal(dispatched, prefix + reads.review + reads.comment,
              "funded full-page dispatches must be the prefix plus witness reads");
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
// Reuse the consuming-broker fixture used by enrollment-plan qualification.
// These legacy-envelope cases add no candidate capacity/profile policy. Every
// transport is synthetic, including signed OIDC; unknown fixture URLs refuse.
const finiteReviews = Array.from({ length: 61 }, (_, index) => ({ id: index + 1, user: { type: "User", login: "synthetic" }, state: "COMMENTED", commit_id: "b".repeat(40), body: "r".repeat(4800) }));
const finiteComments = Array.from({ length: 46 }, (_, index) => ({ id: index + 1, user: { type: "User", login: "synthetic" }, body: "x".repeat(14300) }));
assert(Buffer.byteLength(JSON.stringify(finiteReviews)) < 524288);
assert(Buffer.byteLength(JSON.stringify(finiteComments)) > 524288);
assert(Buffer.byteLength(JSON.stringify(finiteComments.slice(0, 25))) < 524288);
async function qualifiedWitnessExchange({ name, mode, maximum, history, dispatched: expected, reviewReads, commentReads, error: expectedError }) {
  const fixture = await consumingBroker();
  const env = { ...fixture.broker.env, GITHUB_API_URL: "https://api.github.com" };
  const broker = new TokenBroker(env, new GitHubApi(env));
  const attempts = [];
  const priorFetch = globalThis.fetch;
  resetOidcJwksCacheForTesting();
  globalThis.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const attempt = { method: init?.method ?? "GET", path: url.pathname, query: url.search };
    attempts.push(attempt);
    if (url.hostname === "github.com") return mode === "git" ? new Response(advertisement) : new Response("", { status: 503 });
    if (url.pathname === "/repos/malsabbagh/review-sensei/git/ref/tags/v5") return Response.json({ object: { type: "tag", sha: "b".repeat(40) } });
    if (url.pathname.startsWith("/repos/malsabbagh/review-sensei/git/tags/")) return Response.json({ object: { type: "commit", sha: SHA } });
    if (url.pathname === "/repos/acme/widgets/pulls/7/reviews" || url.pathname === "/repos/acme/widgets/issues/7/comments") {
      const isReview = url.pathname.endsWith("/reviews");
      const size = Number(url.searchParams.get("per_page"));
      const page = Number(url.searchParams.get("page"));
      assert([100, 50, 25, 5, 1].includes(size));
      assert(page >= 1 && page <= 10);
      let selected;
      if (history === "maximum") {
        // Nine full pages and a final empty page complete each inventory with
        // ten reads. No row can match an App witness and shortcut comments.
        selected = page < 10 ? Array.from({ length: size }, (_, index) => ({ id: (page - 1) * size + index + 1, body: "synthetic" })) : [];
      } else if (history === "byte-cap-exhausted") {
        // Four oversized responses, then six full single-item pages consume
        // the SAME ten-request counter across page-size restarts.
        selected = size > 1 ? [{ body: "x".repeat(525000) }] : [{ id: page, body: "synthetic" }];
      } else {
        const rows = isReview && history !== "both-oversized" ? finiteReviews : finiteComments;
        selected = rows.slice((page - 1) * size, page * size);
      }
      const payload = JSON.stringify(selected);
      attempt.response_bytes = Buffer.byteLength(payload);
      return new Response(payload, { headers: { "content-type": "application/json" } });
    }
    const mapped = new URL(url);
    if (mapped.hostname === "api.github.com") mapped.hostname = "api.github.test";
    return fixture.transport(mapped, init);
  };
  const control = { schema_version: "1.0", operation_id: "c".repeat(64), request_id: "d".repeat(64), deadline_unix_ms: Date.now() + 60000, max_dispatches: maximum };
  let result, error;
  try {
    result = await broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session", session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, control_budget: control });
  } catch (caught) { error = caught.message; }
  finally { globalThis.fetch = priorFetch; fixture.close(); }
  assert.equal(error, expectedError);
  assert.equal(attempts.length, expected);
  assert(attempts.length <= maximum);
  const reads = witnessReads(attempts);
  assert.deepEqual(reads, { review: reviewReads, comment: commentReads });
  if (error) assert.equal(result, undefined);
  else {
    assert.equal(result.session_state, "enrolled");
    assert.deepEqual(result.control_receipt, { ...control, dispatched: expected });
  }
  const commentQueries = attempts.filter(row => row.path.endsWith("/comments")).map(row => row.query);
  if (history === "finite") assert.deepEqual(commentQueries, ["?per_page=100&page=1", "?per_page=50&page=1", "?per_page=25&page=1", "?per_page=25&page=2"]);
  if (history === "both-oversized") {
    for (const path of ["/reviews", "/comments"]) {
      const pages = attempts.filter(row => row.path.endsWith(path));
      assert.deepEqual(pages.map(row => row.query), ["?per_page=100&page=1", "?per_page=50&page=1", "?per_page=25&page=1", "?per_page=25&page=2"]);
      assert(pages[0].response_bytes > 524288 && pages[1].response_bytes > 524288);
      assert(pages[2].response_bytes < 524288 && pages[3].response_bytes < 524288);
    }
  }
  if (history === "byte-cap-exhausted") assert.deepEqual(attempts.filter(row => row.path.endsWith("/reviews")).map(row => Number(new URLSearchParams(row.query).get("per_page"))), [100, 50, 25, 5, 1, 1, 1, 1, 1, 1]);
  return { name, mode, maximum, history, dispatched: attempts.length, witness_reads: reads, ...(error ? { error } : { session_state: result.session_state }), attempts };
}
const witnessQualification = [];
for (const row of [
  { name: "finite-git", mode: "git", maximum: 30, history: "finite", dispatched: 13, reviewReads: 1, commentReads: 4 },
  { name: "finite-fallback-annotated", mode: "fallback-annotated", maximum: 30, history: "finite", dispatched: 15, reviewReads: 1, commentReads: 4 },
  { name: "both-ladders-git", mode: "git", maximum: 28, history: "maximum", dispatched: 28, reviewReads: 10, commentReads: 10 },
  { name: "both-ladders-fallback-annotated", mode: "fallback-annotated", maximum: 30, history: "maximum", dispatched: 30, reviewReads: 10, commentReads: 10 },
  { name: "both-ladders-one-short-git", mode: "git", maximum: 27, history: "maximum", dispatched: 27, reviewReads: 10, commentReads: 9, error: "broker_control_budget_exhausted" },
  { name: "both-ladders-one-short-fallback-annotated", mode: "fallback-annotated", maximum: 29, history: "maximum", dispatched: 29, reviewReads: 10, commentReads: 9, error: "broker_control_budget_exhausted" },
  { name: "both-oversized-fallback-annotated", mode: "fallback-annotated", maximum: 30, history: "both-oversized", dispatched: 18, reviewReads: 4, commentReads: 4 },
  { name: "byte-ladder-cap-fallback-annotated", mode: "fallback-annotated", maximum: 30, history: "byte-cap-exhausted", dispatched: 20, reviewReads: 10, commentReads: 0, error: "github_session_witness_failed" },
]) witnessQualification.push(await qualifiedWitnessExchange(row));

// A restart after a partial prefix discards its items but retains all four
// physical reads. This is the same bounded reader used by both witnesses.
let restartedReads = 0;
const restarted = await readBoundedHistory(async (size, page) => {
  restartedReads++;
  if (size === 100 && page === 1) return { status: 200, data: Array.from({ length: size }, () => ({ old: true })) };
  if (size === 100) throw new Error("github_response_too_large");
  return { status: 200, data: Array.from({ length: page === 1 ? size : 1 }, () => ({ fresh: true })) };
}, "history_invalid");
assert.equal(restartedReads, 4);
assert.equal(restarted.length, 51);
assert(restarted.every(row => row.fresh));
process.stdout.write(JSON.stringify({ schema_version: "media-broker-profile-v1", profiles: output, witness_qualification: witnessQualification, reader_qualification: { restarted_reads: restartedReads, fresh_items: restarted.length } }, null, 2) + "\n");
