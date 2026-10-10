/** Test IPC bridge. Runtime policy is production TS; only external transports
 * and the existing Cloudflare runtime shim are synthetic. Never deploy this.
 */
import { registerHooks, createRequire } from "node:module";
import { existsSync, readFileSync, openSync, writeSync, fsyncSync, closeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

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

const { consumingBroker } = await import("../deploy/cloudflare/test/epic238-consuming-broker-fixture.ts");
const fixture = await consumingBroker(process.argv[2]);
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
const lines = createInterface({ input: process.stdin });
for await (const line of lines) {
  const request = JSON.parse(line);
  let result;
  let status = 200;
  try {
    switch (request.action) {
      case "oidc": result = await fixture.oidcToken(request.overrides ?? {}); break;
      case "exchange": result = await fixture.broker.exchange(request.body); break;
      case "verify": result = { session_attestation: await fixture.broker.verifySessionGrant(request.body.session_grant, request.body.session_attestation) }; break;
      case "grant_count": result = fixture.grantCount(); break;
      case "request_log": result = { transport: fixture.transportRequests, ledger: fixture.ledgerRequests }; break;
      case "set_source": Object.assign(fixture.githubInputs.source, request.source); result = null; break;
      case "shutdown": fixture.close(); lines.close(); result = null; break;
      default: throw new Error("unknown fixture action");
    }
  } catch (error) {
    status = 403;
    // Production errors here are closed diagnostic categories. Never return
    // arbitrary exception details or request/credential text.
    const category = error instanceof Error && /^(broker|github|oidc)_[a-z0-9_]+$/.test(error.message)
      ? error.message : "fixture_rejected";
    result = { error: category };
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
