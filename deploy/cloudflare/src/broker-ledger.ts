import { DurableObject } from "cloudflare:workers";
import type { WorkerEnv } from "./env";

const MAX_REQUEST_BYTES = 8 * 1024;
const JTI_PATTERN = /^[\x21-\x7e]{1,320}$/;
const SCOPE_PATTERN = /^[\x21-\x7e]{1,512}$/;
const RETENTION_MS = 10 * 60 * 1000;
const RATE_WINDOW_MS = 60 * 1000;
const RATE_LIMIT = 10;
// An enrollment witness is only meaningful while the session it guards can
// still exist. Session records expire at 90 days at the latest (ADR 0047), so
// the witness is pruned on the same schedule: the table stays bounded, the
// Durable Object does not permanently record that a pull request ever had a
// session, and a session that outlived its own record cannot demand recovery
// forever.
const ENROLLMENT_RETENTION_MS = 90 * 24 * 60 * 60 * 1000;

interface BrokerRequest {
  action?: unknown;
  jti?: unknown;
  scope?: unknown;
}

interface BrokerReply {
  state: "accepted" | "replay" | "rate_limited" | "enrolled" | "known" | "invalid";
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

/** A replay/rate assertion. Only these actions carry a JTI. */
type AssertionRequest = BrokerRequest & {
  action: "claim" | "admit";
  jti: string;
  scope: string;
};

/** A session enrollment. It intentionally has no JTI to assert. */
type SessionRequest = BrokerRequest & {
  action: "session_enroll";
  scope: string;
};

const VALID_SCOPES = (value: unknown): value is string =>
  typeof value === "string" && SCOPE_PATTERN.test(value);

function validAssertionRequest(value: BrokerRequest): value is AssertionRequest {
  return (
    (value.action === "claim" || value.action === "admit") &&
    typeof value.jti === "string" &&
    JTI_PATTERN.test(value.jti) &&
    VALID_SCOPES(value.scope)
  );
}

function validSessionRequest(value: BrokerRequest): value is SessionRequest {
  return value.action === "session_enroll" && VALID_SCOPES(value.scope);
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
      CREATE TABLE IF NOT EXISTS broker_session_enrollments (
        scope_hash TEXT PRIMARY KEY,
        enrolled_at INTEGER NOT NULL
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
    if (!validAssertionRequest(data) && !validSessionRequest(data)) {
      return json({ error: "invalid_request" }, 400);
    }

    const scopeHash = await digest(data.scope);
    const now = Date.now();
    if (data.action === "session_enroll") {
      const result = this.ctx.storage.transactionSync(() => {
        this.sql.exec(
          "DELETE FROM broker_replays WHERE expires_at < ?",
          now,
        );
        this.sql.exec(
          "DELETE FROM broker_rates WHERE window_started < ?",
          now - RATE_WINDOW_MS,
        );
        this.sql.exec(
          "DELETE FROM broker_session_enrollments WHERE enrolled_at < ?",
          now - ENROLLMENT_RETENTION_MS,
        );
        const rows = [
          ...this.sql.exec(
            "SELECT scope_hash FROM broker_session_enrollments WHERE scope_hash = ?",
            scopeHash,
          ),
        ];
        if (rows.length > 0) {
          // Retention is a sliding window anchored to the most recent use, not
          // to the first enrollment. A pull request can extend its session up
          // to the 90-day maximum while it stays active, and a witness that
          // expired mid-session would report a live marker as a first
          // enrollment.
          this.sql.exec(
            "UPDATE broker_session_enrollments SET enrolled_at = ? WHERE scope_hash = ?",
            now,
            scopeHash,
          );
          return { state: "known" } as BrokerReply;
        }
        this.sql.exec(
          "INSERT INTO broker_session_enrollments (scope_hash, enrolled_at) VALUES (?, ?)",
          scopeHash,
          now,
        );
        return { state: "enrolled" } as BrokerReply;
      });
      return json(result);
    }
    // The request validator requires a JTI for replay/rate claims. Keep this
    // guard explicit so a future action cannot accidentally enter this path
    // without replay identity.
    if (typeof data.jti !== "string") {
      return json({ error: "invalid_request" }, 400);
    }
    const jtiHash = await digest(data.jti);
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
