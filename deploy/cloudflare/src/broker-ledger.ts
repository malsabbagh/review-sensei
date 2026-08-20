import { DurableObject } from "cloudflare:workers";
import type { WorkerEnv } from "./env";

const MAX_REQUEST_BYTES = 8 * 1024;
const JTI_PATTERN = /^[\x21-\x7e]{1,320}$/;
const SCOPE_PATTERN = /^[\x21-\x7e]{1,512}$/;
const RETENTION_MS = 10 * 60 * 1000;
const RATE_WINDOW_MS = 60 * 1000;
const RATE_LIMIT = 10;

interface BrokerRequest {
  action?: unknown;
  jti?: unknown;
  scope?: unknown;
}

interface BrokerReply {
  state: "accepted" | "replay" | "rate_limited" | "invalid";
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(bytes)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

function validRequest(
  value: BrokerRequest,
): value is BrokerRequest & { action: "claim" | "admit"; jti: string; scope: string } {
  return (
    (value.action === "claim" || value.action === "admit") &&
    typeof value.jti === "string" &&
    JTI_PATTERN.test(value.jti) &&
    typeof value.scope === "string" &&
    SCOPE_PATTERN.test(value.scope)
  );
}

/**
 * Durable replay and rate state. Only SHA-256 identities and counters are
 * retained; assertions, tokens, repository text, and review data never enter
 * SQLite.
 */
export class BrokerLedger extends DurableObject<WorkerEnv> {
  private readonly sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: WorkerEnv) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS broker_replays (
        jti_hash TEXT PRIMARY KEY,
        expires_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS broker_rates (
        scope_hash TEXT PRIMARY KEY,
        window_started INTEGER NOT NULL,
        count INTEGER NOT NULL
      );
    `);
  }

  async fetch(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return json({ error: "method_not_allowed" }, 405);
    }
    let data: BrokerRequest;
    try {
      const body = await request.arrayBuffer();
      if (body.byteLength === 0 || body.byteLength > MAX_REQUEST_BYTES) {
        return json({ error: "invalid_request" }, 400);
      }
      data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)) as BrokerRequest;
    } catch {
      return json({ error: "invalid_request" }, 400);
    }
    if (!validRequest(data)) {
      return json({ error: "invalid_request" }, 400);
    }

    const jtiHash = await digest(data.jti);
    const scopeHash = await digest(data.scope);
    const now = Date.now();
    const result = this.ctx.storage.transactionSync(() => {
      this.sql.exec(
        "DELETE FROM broker_replays WHERE expires_at < ?",
        now,
      );
      this.sql.exec(
        "DELETE FROM broker_rates WHERE window_started < ?",
        now - RATE_WINDOW_MS,
      );
      if (data.action === "claim") {
        const replayRows = [
          ...this.sql.exec(
            "SELECT jti_hash FROM broker_replays WHERE jti_hash = ?",
            jtiHash,
          ),
        ];
        if (replayRows.length > 0) {
          return { state: "replay" } as BrokerReply;
        }
      }

      const rateRows = [
        ...this.sql.exec<{ window_started: number; count: number }>(
          "SELECT window_started, count FROM broker_rates WHERE scope_hash = ?",
          scopeHash,
        ),
      ];
      const rate = rateRows[0];
      const windowStarted = rate?.window_started ?? now;
      const count = rate && now - windowStarted < RATE_WINDOW_MS ? rate.count : 0;
      if (count >= RATE_LIMIT) {
        return { state: "rate_limited" } as BrokerReply;
      }
      if (data.action === "claim") {
        this.sql.exec(
          "INSERT INTO broker_replays (jti_hash, expires_at) VALUES (?, ?)",
          jtiHash,
          now + RETENTION_MS,
        );
      }
      if (count === 0 || !rate || now - windowStarted >= RATE_WINDOW_MS) {
        this.sql.exec(
          "INSERT OR REPLACE INTO broker_rates (scope_hash, window_started, count) VALUES (?, ?, 1)",
          scopeHash,
          now,
        );
      } else {
        this.sql.exec(
          "UPDATE broker_rates SET count = ? WHERE scope_hash = ?",
          count + 1,
          scopeHash,
        );
      }
      return { state: "accepted" } as BrokerReply;
    });
    return json(result);
  }
}
