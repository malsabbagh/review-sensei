import { DurableObject } from "cloudflare:workers";
import type { WorkerEnv } from "./env";
import { advanceStoredContinuation } from "./setup-alarm";
import {
  parseSetupContinuationRequest,
  type SetupContinuationRequest,
  type SetupFailureSummary,
} from "./setup-continuation";

const DELIVERY_ID_PATTERN = /^[\x21-\x7e]{1,200}$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const LEASE_MS = 5 * 60 * 1000;
const RETENTION_MS = 60 * 60 * 1000;

type DeliveryState = "accepted" | "in_flight" | "claimed" | "conflict";

interface DeliveryRecord {
  app_id: number;
  delivery_id: string;
  digest: string;
  state: "processing" | "accepted";
  lease_until: number;
  updated_at: number;
}

interface LedgerRequest {
  app_id?: unknown;
  delivery_id?: unknown;
  digest?: unknown;
}

interface LedgerResponse {
  state: DeliveryState;
  lease_until?: number;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function validAppId(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value > 0
  );
}

function validRequest(data: LedgerRequest): data is LedgerRequest & {
  app_id: number;
  delivery_id: string;
  digest: string;
} {
  return (
    validAppId(data.app_id) &&
    typeof data.delivery_id === "string" &&
    DELIVERY_ID_PATTERN.test(data.delivery_id) &&
    typeof data.digest === "string" &&
    DIGEST_PATTERN.test(data.digest)
  );
}

const MAX_CONTINUATION_BYTES = 64 * 1024;
const ERROR_CODE_PATTERN = /^[a-z0-9_]{1,80}$/;

interface ContinuationRow {
  app_id: number;
  delivery_id: string;
  digest: string;
  cursor: string;
  updated_at: number;
}

/**
 * Durable delivery state, not a raw webhook store.
 *
 * One named instance serializes claims across Worker instances. Accepted
 * deliveries retain a digest, identity, state, and bounded lease. A
 * multi-repository setup also stores a bounded continuation cursor (repository
 * slugs and the permission map) until that chain finishes, plus at most one
 * terminal failure record for the retention window. The raw webhook body is
 * never written to SQLite.
 */
export class DeliveryLedger extends DurableObject<WorkerEnv> {
  private readonly sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: WorkerEnv) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS deliveries (
        app_id INTEGER NOT NULL,
        delivery_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('processing', 'accepted')),
        lease_until INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (app_id, delivery_id)
      )
    `);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS setup_continuations (
        app_id INTEGER NOT NULL,
        delivery_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        cursor TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (app_id, delivery_id)
      )
    `);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS setup_outcomes (
        app_id INTEGER NOT NULL,
        delivery_id TEXT NOT NULL,
        error_code TEXT NOT NULL,
        failure_count INTEGER NOT NULL,
        repository TEXT,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (app_id, delivery_id)
      )
    `);
  }

  /**
   * Resume one stored setup continuation. Cloudflare retries this alarm if it
   * throws, and the cursor stays put until that step finishes.
   */
  async alarm(): Promise<void> {
    const row = this.oldestContinuation();
    if (!row) {
      return;
    }
    let input: SetupContinuationRequest;
    try {
      input = parseSetupContinuationRequest(JSON.parse(row.cursor) as unknown);
    } catch {
      this.deleteContinuation(row.app_id, row.delivery_id);
      this.release({
        app_id: row.app_id,
        delivery_id: row.delivery_id,
        digest: row.digest,
      });
      await this.armIfPending();
      return;
    }
    const advance = await advanceStoredContinuation(this.env, input);
    if (advance.kind === "continue") {
      this.saveContinuation(row.app_id, row.delivery_id, row.digest, advance.next);
      this.refreshLease(row.app_id, row.delivery_id, row.digest);
    } else if (advance.kind === "complete") {
      this.complete({
        app_id: row.app_id,
        delivery_id: row.delivery_id,
        digest: row.digest,
      });
      this.deleteContinuation(row.app_id, row.delivery_id);
    } else {
      this.recordOutcome(row.app_id, row.delivery_id, advance.summary);
      this.release({
        app_id: row.app_id,
        delivery_id: row.delivery_id,
        digest: row.digest,
      });
      this.deleteContinuation(row.app_id, row.delivery_id);
      console.error("github_setup_incomplete", {
        delivery_id: row.delivery_id,
        error_code: advance.summary.errorCode,
        failure_count: advance.summary.failureCount,
      });
    }
    await this.armIfPending();
  }

  async fetch(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return json({ error: "method_not_allowed" }, 405);
    }
    const action = new URL(request.url).pathname;
    if (
      action !== "/claim" &&
      action !== "/complete" &&
      action !== "/release" &&
      action !== "/schedule"
    ) {
      return json({ error: "not_found" }, 404);
    }

    let data: LedgerRequest & { continuation?: unknown };
    try {
      const body = await request.arrayBuffer();
      const limit = action === "/schedule" ? MAX_CONTINUATION_BYTES : 8 * 1024;
      if (body.byteLength === 0 || body.byteLength > limit) {
        return json({ error: "invalid_request" }, 400);
      }
      data = JSON.parse(new TextDecoder().decode(body)) as LedgerRequest & {
        continuation?: unknown;
      };
    } catch {
      return json({ error: "invalid_request" }, 400);
    }
    const continuation = data.continuation;
    if (!validRequest(data)) {
      return json({ error: "invalid_request" }, 400);
    }

    if (action === "/claim") {
      return json(this.claim(data));
    }
    if (action === "/complete") {
      return json(this.complete(data));
    }
    if (action === "/schedule") {
      return this.schedule(data, continuation);
    }
    return json(this.release(data));
  }

  private claim(data: LedgerRequest & {
    app_id: number;
    delivery_id: string;
    digest: string;
  }): LedgerResponse {
    const now = Date.now();
    return this.ctx.storage.transactionSync(() => {
      this.sql.exec(
        "DELETE FROM deliveries WHERE state = 'accepted' AND updated_at < ?",
        now - RETENTION_MS,
      );
      this.sql.exec(
        "DELETE FROM setup_outcomes WHERE updated_at < ?",
        now - RETENTION_MS,
      );
      const rows = [
        ...this.sql.exec(
          "SELECT app_id, delivery_id, digest, state, lease_until, updated_at FROM deliveries WHERE app_id = ? AND delivery_id = ?",
          data.app_id,
          data.delivery_id,
        ),
      ] as unknown as DeliveryRecord[];
      const existing = rows[0];
      if (existing && existing.digest !== data.digest) {
        return { state: "conflict" };
      }
      if (existing?.state === "accepted") {
        return { state: "accepted" };
      }
      if (existing && existing.lease_until >= now) {
        return { state: "in_flight", lease_until: existing.lease_until };
      }
      const leaseUntil = now + LEASE_MS;
      if (existing) {
        this.sql.exec(
          "UPDATE deliveries SET state = 'processing', lease_until = ?, updated_at = ? WHERE app_id = ? AND delivery_id = ? AND digest = ?",
          leaseUntil,
          now,
          data.app_id,
          data.delivery_id,
          data.digest,
        );
      } else {
        this.sql.exec(
          "INSERT INTO deliveries (app_id, delivery_id, digest, state, lease_until, updated_at) VALUES (?, ?, ?, 'processing', ?, ?)",
          data.app_id,
          data.delivery_id,
          data.digest,
          leaseUntil,
          now,
        );
      }
      return { state: "claimed", lease_until: leaseUntil };
    });
  }

  private complete(data: LedgerRequest & {
    app_id: number;
    delivery_id: string;
    digest: string;
  }): LedgerResponse {
    const now = Date.now();
    return this.ctx.storage.transactionSync(() => {
      const rows = [
        ...this.sql.exec(
          "SELECT state, digest FROM deliveries WHERE app_id = ? AND delivery_id = ?",
          data.app_id,
          data.delivery_id,
        ),
      ] as unknown as Array<{ state: string; digest: string }>;
      const existing = rows[0];
      if (!existing || existing.digest !== data.digest) {
        return { state: "conflict" };
      }
      if (existing.state === "accepted") {
        return { state: "accepted" };
      }
      this.sql.exec(
        "UPDATE deliveries SET state = 'accepted', lease_until = ?, updated_at = ? WHERE app_id = ? AND delivery_id = ? AND digest = ?",
        now,
        now,
        data.app_id,
        data.delivery_id,
        data.digest,
      );
      return { state: "accepted" };
    });
  }

  private release(data: LedgerRequest & {
    app_id: number;
    delivery_id: string;
    digest: string;
  }): LedgerResponse {
    return this.ctx.storage.transactionSync(() => {
      this.sql.exec(
        "DELETE FROM deliveries WHERE app_id = ? AND delivery_id = ? AND digest = ? AND state = 'processing'",
        data.app_id,
        data.delivery_id,
        data.digest,
      );
      return { state: "claimed" };
    });
  }

  private async schedule(
    data: LedgerRequest & {
      app_id: number;
      delivery_id: string;
      digest: string;
    },
    continuation: unknown,
  ): Promise<Response> {
    let cursor: SetupContinuationRequest;
    try {
      cursor = parseSetupContinuationRequest(continuation);
    } catch {
      return json({ error: "invalid_request" }, 400);
    }
    if (
      cursor.appId !== data.app_id ||
      cursor.deliveryId !== data.delivery_id ||
      cursor.digest !== data.digest
    ) {
      return json({ error: "invalid_request" }, 400);
    }
    const encoded = JSON.stringify(cursor);
    if (new TextEncoder().encode(encoded).byteLength > MAX_CONTINUATION_BYTES) {
      return json({ error: "invalid_request" }, 400);
    }
    const accepted = this.ctx.storage.transactionSync(() => {
      const rows = [
        ...this.sql.exec(
          "SELECT state, digest, lease_until FROM deliveries WHERE app_id = ? AND delivery_id = ?",
          data.app_id,
          data.delivery_id,
        ),
      ] as unknown as Array<{ state: string; digest: string; lease_until: number }>;
      const existing = rows[0];
      if (
        !existing ||
        existing.state !== "processing" ||
        existing.digest !== data.digest ||
        existing.lease_until < Date.now()
      ) {
        return false;
      }
      this.saveContinuation(data.app_id, data.delivery_id, data.digest, cursor);
      return true;
    });
    if (!accepted) {
      return json({ error: "delivery_conflict" }, 409);
    }
    try {
      await this.ctx.storage.setAlarm(Date.now());
    } catch {
      this.deleteContinuation(data.app_id, data.delivery_id);
      return json({ error: "delivery_ledger_unavailable" }, 503);
    }
    return json({ state: "scheduled" });
  }

  private saveContinuation(
    appId: number,
    deliveryId: string,
    digest: string,
    cursor: SetupContinuationRequest,
  ): void {
    const now = Date.now();
    this.sql.exec(
      "INSERT OR REPLACE INTO setup_continuations (app_id, delivery_id, digest, cursor, updated_at) VALUES (?, ?, ?, ?, ?)",
      appId,
      deliveryId,
      digest,
      JSON.stringify(cursor),
      now,
    );
  }

  private deleteContinuation(appId: number, deliveryId: string): void {
    this.sql.exec(
      "DELETE FROM setup_continuations WHERE app_id = ? AND delivery_id = ?",
      appId,
      deliveryId,
    );
  }

  private oldestContinuation(): ContinuationRow | null {
    const rows = [
      ...this.sql.exec(
        "SELECT app_id, delivery_id, digest, cursor, updated_at FROM setup_continuations ORDER BY updated_at LIMIT 1",
      ),
    ] as unknown as ContinuationRow[];
    return rows[0] ?? null;
  }

  private refreshLease(appId: number, deliveryId: string, digest: string): void {
    const now = Date.now();
    this.sql.exec(
      "UPDATE deliveries SET lease_until = ?, updated_at = ? WHERE app_id = ? AND delivery_id = ? AND digest = ? AND state = 'processing'",
      now + LEASE_MS,
      now,
      appId,
      deliveryId,
      digest,
    );
  }

  private recordOutcome(
    appId: number,
    deliveryId: string,
    summary: SetupFailureSummary,
  ): void {
    const errorCode = ERROR_CODE_PATTERN.test(summary.errorCode)
      ? summary.errorCode
      : "setup_failed";
    const repository =
      typeof summary.repository === "string" && summary.repository.length > 0
        ? summary.repository
        : null;
    this.sql.exec(
      "INSERT OR REPLACE INTO setup_outcomes (app_id, delivery_id, error_code, failure_count, repository, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
      appId,
      deliveryId,
      errorCode,
      summary.failureCount,
      repository,
      Date.now(),
    );
  }

  private async armIfPending(): Promise<void> {
    const rows = [
      ...this.sql.exec("SELECT app_id FROM setup_continuations LIMIT 1"),
    ] as unknown as Array<{ app_id: number }>;
    if (rows.length > 0) {
      await this.ctx.storage.setAlarm(Date.now());
    }
  }
}
