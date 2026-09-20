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
const GRANT_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const ATTESTATION_DIGEST_PATTERN = /^[a-f0-9]{64}$/;
const RUN_ID_PATTERN = /^[1-9][0-9]{0,18}$/;
const SESSION_GRANT_TTL_MS = 10 * 60 * 1000;
const SESSION_GRANT_AUDIENCE = "reviewsensei-session-ledger";

interface BrokerRequest {
  action?: unknown;
  jti?: unknown;
  scope?: unknown;
  grant?: unknown;
  attestation_digest?: unknown;
  run_id?: unknown;
  audience?: unknown;
}

interface BrokerReply {
  state:
    | "accepted"
    | "replay"
    | "rate_limited"
    | "enrolled"
    | "known"
    | "issued"
    | "verified"
    | "expired"
    | "invalid";
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
): value is BrokerRequest & {
  action: "claim" | "admit" | "session_enroll" | "session_issue" | "session_verify";
  jti?: string;
  scope: string;
} {
  const grantRequest = value.action === "session_issue" || value.action === "session_verify";
  return (
    (value.action === "claim" || value.action === "admit" || value.action === "session_enroll" || grantRequest) &&
    (value.action === "session_enroll" || grantRequest || (
      typeof value.jti === "string" && JTI_PATTERN.test(value.jti)
    )) &&
    typeof value.scope === "string" &&
    SCOPE_PATTERN.test(value.scope) &&
    (!grantRequest || (
      typeof value.grant === "string" && GRANT_PATTERN.test(value.grant) &&
      typeof value.attestation_digest === "string" && ATTESTATION_DIGEST_PATTERN.test(value.attestation_digest) &&
      value.audience === SESSION_GRANT_AUDIENCE &&
      (value.action === "session_verify" ||
        (typeof value.run_id === "string" && RUN_ID_PATTERN.test(value.run_id)))
    ))
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
      CREATE TABLE IF NOT EXISTS broker_session_enrollments (
        scope_hash TEXT PRIMARY KEY,
        enrolled_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS broker_session_grants (
        grant_hash TEXT PRIMARY KEY,
        scope_hash TEXT NOT NULL,
        attestation_hash TEXT NOT NULL,
        audience_hash TEXT NOT NULL,
        run_id_hash TEXT NOT NULL,
        issued_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
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
        // Enrollment refreshes the sliding retention window, so it spends the
        // same per-scope budget as an assertion: without it a caller could
        // keep any number of witnesses alive by re-enrolling in a loop.
        if (!this.admitScope(scopeHash, now)) {
          return { state: "rate_limited" } as BrokerReply;
        }
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
    if (data.action === "session_issue" || data.action === "session_verify") {
      // The request validator above establishes these values. Keep them local
      // to this branch so ordinary replay/rate actions cannot accidentally
      // acquire a grant-shaped authority path.
      const grantHash = await digest(data.grant as string);
      const attestationHash = await digest(data.attestation_digest as string);
      const audienceHash = await digest(data.audience as string);
      const runIdHash = data.action === "session_issue"
        ? await digest(data.run_id as string)
        : undefined;
      const result = this.ctx.storage.transactionSync(() => {
        this.sql.exec(
          "DELETE FROM broker_session_grants WHERE expires_at < ?",
          now,
        );
        if (data.action === "session_issue") {
          const rows = [
            ...this.sql.exec(
              "SELECT grant_hash FROM broker_session_grants WHERE grant_hash = ?",
              grantHash,
            ),
          ];
          if (rows.length > 0) {
            return { state: "replay" } as BrokerReply;
          }
          this.sql.exec(
            "INSERT INTO broker_session_grants (grant_hash, scope_hash, attestation_hash, audience_hash, run_id_hash, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            grantHash,
            scopeHash,
            attestationHash,
            audienceHash,
            // The run id is only a correlation identity; persist its digest.
            // `session_verify` does not receive it, so it cannot choose one.
            runIdHash as string,
            now,
            now + SESSION_GRANT_TTL_MS,
          );
          return { state: "issued" } as BrokerReply;
        }
        const rows = [
          ...this.sql.exec<{ scope_hash: string; attestation_hash: string; audience_hash: string }>(
            "SELECT scope_hash, attestation_hash, audience_hash FROM broker_session_grants WHERE grant_hash = ?",
            grantHash,
          ),
        ];
        const record = rows[0];
        if (record === undefined) {
          return { state: "invalid" } as BrokerReply;
        }
        return record.scope_hash === scopeHash &&
          record.attestation_hash === attestationHash &&
          record.audience_hash === audienceHash
          ? { state: "verified" } as BrokerReply
          : { state: "invalid" } as BrokerReply;
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

      if (!this.admitScope(scopeHash, now)) {
        return { state: "rate_limited" } as BrokerReply;
      }
      if (data.action === "claim") {
        this.sql.exec(
          "INSERT INTO broker_replays (jti_hash, expires_at) VALUES (?, ?)",
          jtiHash,
          now + RETENTION_MS,
        );
      }
      return { state: "accepted" } as BrokerReply;
    });
    return json(result);
  }

  /**
   * Admit one call for a scope and advance its fixed-window counter.
   *
   * Replay and rate claims admit their assertion scope; session enrollment
   * admits its head-bound scope with the same budget, so a caller cannot keep
   * a witness alive indefinitely by refreshing it in a loop. Returns false when
   * the scope has already spent its window.
   */
  private admitScope(scopeHash: string, now: number): boolean {
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
      return false;
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
    return true;
  }
}
