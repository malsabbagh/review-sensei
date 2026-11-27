/**
 * Integrated acceptance test HTTPS bridge for the real Cloudflare Worker.
 * Runs production TypeScript (`worker.ts` + `V6PrCoordinator`) over `node:https`
 * backed by `node:sqlite` (`DatabaseSync`) Durable Object storage and redirects
 * GitHub/OIDC `fetch` calls to the local simulated HTTPS server. Never deploy.
 */
import { registerHooks, createRequire } from "node:module";
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { createServer } from "node:https";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { format } from "node:util";

const configPath = process.argv[2];
if (!configPath) {
  throw new Error("usage: node tests/v6_worker_http_bridge.mjs <config.json>");
}
const config = JSON.parse(readFileSync(configPath, "utf8"));
const logPath = config.logPath;

function writeBridgeLog(level, args) {
  if (typeof logPath === "string" && logPath.length > 0) {
    const rendered = format(...args);
    appendFileSync(logPath, `[${level}] ${rendered}\n`, "utf8");
  }
}

for (const level of ["log", "info", "warn", "error", "debug"]) {
  const original = console[level].bind(console);
  console[level] = (...args) => {
    writeBridgeLog(level, args);
    // Keep stdout strictly reserved for the initial {"port": ...} ready line.
    if (level === "warn" || level === "error") {
      original(...args);
    }
  };
}

const typescript = createRequire(import.meta.url)(
  "../deploy/cloudflare/node_modules/typescript",
);

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "cloudflare:workers") {
      specifier = new URL(
        "../deploy/cloudflare/test/shims/cloudflare-workers.ts",
        import.meta.url,
      ).href;
    } else if (specifier.startsWith(".") && context.parentURL) {
      const candidate = new URL(specifier + ".ts", context.parentURL);
      if (existsSync(fileURLToPath(candidate))) {
        specifier = candidate.href;
      }
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (url.endsWith(".json")) {
      return {
        format: "module",
        shortCircuit: true,
        source: `export default ${JSON.stringify(
          JSON.parse(readFileSync(fileURLToPath(url), "utf8")),
        )};`,
      };
    }
    if (url.endsWith(".yml")) {
      return {
        format: "module",
        shortCircuit: true,
        source: `export default ${JSON.stringify(
          readFileSync(fileURLToPath(url), "utf8"),
        )};`,
      };
    }
    if (!url.endsWith(".ts")) {
      return nextLoad(url, context);
    }
    return {
      format: "module",
      shortCircuit: true,
      source: typescript.transpileModule(
        readFileSync(fileURLToPath(url), "utf8"),
        {
          compilerOptions: {
            target: typescript.ScriptTarget.ES2022,
            module: typescript.ModuleKind.ESNext,
          },
          fileName: fileURLToPath(url),
        },
      ).outputText,
    };
  },
});

const physicalFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const rawUrl =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;
  const parsed = new URL(rawUrl);
  if (
    parsed.origin === "https://api.github.com" ||
    parsed.origin === "https://token.actions.githubusercontent.com"
  ) {
    const targetUrl = `https://127.0.0.1:${config.githubPort}${parsed.pathname}${parsed.search}`;
    if (input instanceof Request) {
      const reqInit = {
        method: input.method,
        headers: input.headers,
        body: input.body,
        signal: input.signal,
        ...(input.body !== null ? { duplex: "half" } : {}),
        ...init,
      };
      return physicalFetch(targetUrl, reqInit);
    }
    return physicalFetch(targetUrl, init);
  }
  throw new Error(`unexpected_external_fetch:${parsed.origin}`);
};

const { default: worker } = await import("../deploy/cloudflare/src/worker.ts");
const { V6PrCoordinator } = await import(
  "../deploy/cloudflare/src/v6-pr-coordinator.ts"
);

function makeSqlStorage(db) {
  const makeCursor = (rows) => ({
    toArray: () => rows.map((row) => ({ ...row })),
    one: () => (rows.length > 0 ? { ...rows[0] } : undefined),
    [Symbol.iterator]: () => rows.map((row) => ({ ...row }))[Symbol.iterator](),
  });
  return {
    exec(query, ...bindings) {
      const trimmed = query.trim();
      const returnsRows =
        /^(SELECT|PRAGMA|WITH)\b/i.test(trimmed) ||
        /\bRETURNING\b/i.test(trimmed);
      if (returnsRows) {
        const rows = db.prepare(query).all(...bindings);
        return makeCursor(rows);
      }
      if (bindings.length === 0) {
        db.exec(query);
        return makeCursor([]);
      }
      db.prepare(query).run(...bindings);
      return makeCursor([]);
    },
  };
}

class SqliteDurableObjectNamespace {
  constructor(envFactory) {
    this.envFactory = envFactory;
    this.instances = new Map();
  }

  idFromName(name) {
    return {
      name: String(name),
      toString() {
        return String(name);
      },
    };
  }

  get(id) {
    const key = typeof id?.name === "string" ? id.name : String(id);
    let entry = this.instances.get(key);
    if (!entry) {
      const db = new DatabaseSync(":memory:");
      let mutex = Promise.resolve();
      const ctx = {
        id: { toString: () => key, name: key },
        storage: {
          sql: makeSqlStorage(db),
        },
        blockConcurrencyWhile(fn) {
          const run = mutex.then(
            () => fn(),
            () => fn(),
          );
          mutex = run.then(
            () => undefined,
            () => undefined,
          );
          return run;
        },
      };
      const coordinator = new V6PrCoordinator(ctx, this.envFactory());
      entry = { db, coordinator };
      this.instances.set(key, entry);
    }
    return {
      fetch(request) {
        return entry.coordinator.fetch(request);
      },
    };
  }

  closeAll() {
    for (const entry of this.instances.values()) {
      try {
        entry.db.close();
      } catch {
        // Ignore close errors during shutdown.
      }
    }
    this.instances.clear();
  }
}

const workerEnv = {
  PUBLIC_WORKFLOW_TAG: "v5",
  GITHUB_API_URL: "https://api.github.com",
  ...config.env,
};
const doNamespace = new SqliteDurableObjectNamespace(() => workerEnv);
workerEnv.V6_PR_COORDINATOR = doNamespace;

const server = createServer(
  {
    cert: readFileSync(config.certPath),
    key: readFileSync(config.keyPath),
  },
  async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) {
        chunks.push(chunk);
      }
      const bodyBuf = Buffer.concat(chunks);
      const headers = new Headers();
      for (const [k, v] of Object.entries(req.headers)) {
        if (Array.isArray(v)) {
          for (const item of v) headers.append(k, item);
        } else if (v !== undefined) {
          headers.set(k, String(v));
        }
      }
      const addr = server.address();
      const port = typeof addr === "object" && addr !== null ? addr.port : 0;
      const fullUrl = `https://127.0.0.1:${port}${req.url ?? "/"}`;
      const method = req.method ?? "GET";
      const init = {
        method,
        headers,
        ...(method !== "GET" && method !== "HEAD" ? { body: bodyBuf } : {}),
      };
      const webResp = await worker.fetch(new Request(fullUrl, init), workerEnv, {
        waitUntil() {},
        passThroughOnException() {},
      });
      const respHeaders = {};
      for (const [k, v] of webResp.headers.entries()) {
        respHeaders[k] = v;
      }
      const respBody = Buffer.from(await webResp.arrayBuffer());
      res.writeHead(webResp.status, respHeaders);
      res.end(respBody);
    } catch (error) {
      writeBridgeLog("error", ["bridge_request_failed", String(error)]);
      res.writeHead(500, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "bridge_internal_error" }));
    }
  },
);

await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", resolve);
});

const address = server.address();
const port = typeof address === "object" && address !== null ? address.port : 0;
process.stdout.write(JSON.stringify({ port }) + "\n");

function shutdown() {
  server.closeAllConnections?.();
  server.close(() => {
    doNamespace.closeAll();
    process.exit(0);
  });
  setTimeout(() => {
    doNamespace.closeAll();
    process.exit(0);
  }, 1500).unref();
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
process.stdin.on("end", shutdown);
process.stdin.resume();
