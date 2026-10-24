/** Test IPC bridge. Runtime policy is production TS; only external transports
 * and the existing Cloudflare runtime shim are synthetic. Never deploy this.
 */
import { registerHooks, createRequire } from "node:module";
import { existsSync, readFileSync, openSync, writeSync, fsyncSync, closeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import { createServer } from "node:http";

const typescript = createRequire(import.meta.url)("../deploy/cloudflare/node_modules/typescript");

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
    if (url.endsWith(".json")) {
      return { format: "module", shortCircuit: true, source: `export default ${JSON.stringify(JSON.parse(readFileSync(fileURLToPath(url), "utf8")))};` };
    }
    if (url.endsWith(".yml")) {
      return { format: "module", shortCircuit: true, source: `export default ${JSON.stringify(readFileSync(fileURLToPath(url), "utf8"))};` };
    }
    if (!url.endsWith(".ts")) return nextLoad(url, context);
    return {
      format: "module", shortCircuit: true,
      source: typescript.transpileModule(readFileSync(fileURLToPath(url), "utf8"), {
        compilerOptions: { target: typescript.ScriptTarget.ES2022, module: typescript.ModuleKind.ESNext },
        fileName: fileURLToPath(url),
      }).outputText,
    };
  },
});

const { brokerDiagnostic } = await import("../deploy/cloudflare/src/broker-diagnostics.ts");
const { BrokerDispatchControl } = await import("../deploy/cloudflare/src/broker-dispatch-control.ts");
const { GitHubApi } = await import("../deploy/cloudflare/src/github-api.ts");
const { verifyOidcAssertion, resetOidcJwksCacheForTesting } = await import("../deploy/cloudflare/src/oidc.ts");
const { consumingBroker } = await import("../deploy/cloudflare/test/epic238-consuming-broker-fixture.ts");
const fixture = await consumingBroker(process.argv[2], process.argv[3]);
const physicalFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  if (process.argv[3]) {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const descriptor = openSync(process.argv[3], "a");
    try {
      writeSync(descriptor, JSON.stringify({ phase: "attempt", origin: "broker-internal", method: init?.method ?? "GET", path: url.pathname }) + "\n");
      fsyncSync(descriptor);
    } finally { closeSync(descriptor); }
  }
  return fixture.transport(input, init);
};
async function redirectProbe(site, blocked = false) {
  // Exercise the production fetch init with real HTTP redirect handling. Only
  // the destination is routed to loopback; no fake redirect/fetch response.
  const requests = [];
  const server = createServer((request, response) => {
    requests.push(request.url);
    if (blocked) return;
    if (request.url === "/redirect") {
      response.writeHead(302, { location: "/destination" });
      response.end();
    } else {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ id: 7, full_name: "owner/repo" }));
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const previousFetch = globalThis.fetch;
  const control = new BrokerDispatchControl({ schema_version: "1.0", operation_id: "c".repeat(64), request_id: "d".repeat(64), deadline_unix_ms: Date.now() + (blocked ? 80 : 60_000), max_dispatches: 1 });
  const deadline = control.request.deadline_unix_ms;
  const started = Date.now();
  const beforeDispatch = control.beforeDispatch.bind(control);
  let dispatches = 0;
  let signal;
  let aborted = false;
  let elapsed;
  control.beforeDispatch = () => { const value = beforeDispatch(); dispatches += 1; return value; };
  globalThis.fetch = (input, init) => {
    signal = init?.signal;
    return physicalFetch(`http://127.0.0.1:${server.address().port}/redirect`, init);
  };
  let category = "accepted";
  try {
    const api = new GitHubApi({ GITHUB_API_URL: site === "git" ? "https://api.github.com" : `http://127.0.0.1:${server.address().port}` }, control);
    if (site === "rest") await api.repositoryInfo("owner/repo", "synthetic");
    else if (site === "git") await api.publicWorkflowRuntimeShas("v5");
    else if (site === "jwks") {
      resetOidcJwksCacheForTesting();
      await verifyOidcAssertion(await fixture.oidcToken(), { dispatchControl: control });
    } else throw new Error("invalid probe site");
  } catch (error) {
    aborted = signal?.aborted === true;
    elapsed = Date.now() - started;
    category = error instanceof Error && /^(broker|github|oidc)_[a-z0-9_]+$/.test(error.message) ? error.message : "transport_refused";
  } finally {
    globalThis.fetch = previousFetch;
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
  return { requests, category, aborted, elapsed_ms: elapsed, dispatches,
    original_deadline: deadline, final_deadline: control.request.deadline_unix_ms,
    deadline_expired: Date.now() >= deadline };
}
const lines = createInterface({ input: process.stdin });
for await (const line of lines) {
  const request = JSON.parse(line);
  let result;
  let status = 200;
  if (request.action === "verify" && request.pause_before_verify === true) {
    // Before production verification dispatch, not an invented SQL boundary.
    process.stdout.write(JSON.stringify({ id: request.id, checkpoint: "before-grant-verification" }) + "\n");
    await new Promise(() => {});
  }
  try {
    switch (request.action) {
      case "oidc": result = await fixture.oidcToken(request.overrides ?? {}); break;
      case "exchange": result = await fixture.broker.exchange(request.body); break;
      case "verify": {
        const control = request.body.control_budget === undefined ? undefined : new BrokerDispatchControl(request.body.control_budget);
        result = { session_attestation: await fixture.broker.verifySessionGrant(request.body.session_grant, request.body.session_attestation, control), ...(control === undefined ? {} : { control_receipt: control.receipt() }) }; break;
      }
      case "grant_count": result = fixture.grantCount(); break;
      case "request_log": result = { transport: fixture.transportRequests, ledger: fixture.ledgerRequests }; break;
      case "set_source": Object.assign(fixture.githubInputs.source, request.source); result = null; break;
      case "set_inline_source": Object.assign(fixture.githubInputs.inlineSource, request.source); result = null; break;
      case "set_snapshot": {
        for (const name of ["number", "state", "draft", "base", "head", "repository", "repositoryId"]) {
          if (Object.hasOwn(request.snapshot, name)) fixture.githubInputs[name] = request.snapshot[name];
        }
        result = null; break;
      }
      case "configure_feedback": result = fixture.configureFeedback(); break;
      case "redirect_probe": result = await redirectProbe(request.site, request.blocked); break;
      case "shutdown": fixture.close(); lines.close(); result = null; break;
      default: throw new Error("unknown fixture action");
    }
  } catch (error) {
    status = 403;
    // Production errors here are closed diagnostic categories. Never return
    // arbitrary exception details or request/credential text.
    const category = error instanceof Error && /^(broker|github|oidc)_[a-z0-9_]+$/.test(error.message)
      ? error.message : "fixture_rejected";
    result = { error: category, ...(request.body?.control_budget === undefined ? {} : { diagnostic: brokerDiagnostic(error) }) };
  }
  if (request.action === "verify" && status === 200 && request.pause_after_consume === true) {
    // Signal only: this is deliberately not the verified HTTP response. The
    // parent kills this process after the production SQL consume commits.
    process.stdout.write(JSON.stringify({ id: request.id, checkpoint: "grant-consumed" }) + "\n");
    await new Promise(() => {});
  }
  process.stdout.write(JSON.stringify({ id: request.id, status, result }) + "\n");
  if (request.action === "shutdown") break;
}
