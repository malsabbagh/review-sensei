import { DurableObject } from "cloudflare:workers";
import type { WorkerEnv } from "./env";

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

function validRequest(data: LedgerRequest): data is Required<LedgerRequest> {
  return (
    validAppId(data.app_id) &&
    typeof data.delivery_id === "string" &&
    DELIVERY_ID_PATTERN.test(data.delivery_id) &&
    typeof data.digest === "string" &&
    DIGEST_PATTERN.test(data.digest)
  );
}

/**
 * Durable delivery state, not a webhook payload store.
 *
 * One named instance serializes claims across Worker instances. Only a
 * digest, identity, state, and bounded lease are retained; the raw GitHub
 * body stays in the request path and is never written to SQLite.
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
  }

  async fetch(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return json({ error: "method_not_allowed" }, 405);
    }
    const action = new URL(request.url).pathname;
    if (action !== "/claim" && action !== "/complete" && action !== "/release") {
      return json({ error: "not_found" }, 404);
    }

    let data: LedgerRequest;
    try {
      const body = await request.arrayBuffer();
      if (body.byteLength === 0 || body.byteLength > 8 * 1024) {
        return json({ error: "invalid_request" }, 400);
      }
      data = JSON.parse(new TextDecoder().decode(body)) as LedgerRequest;
    } catch {
      return json({ error: "invalid_request" }, 400);
    }
    if (!validRequest(data)) {
      return json({ error: "invalid_request" }, 400);
    }

    if (action === "/claim") {
      return json(this.claim(data));
    }
    if (action === "/complete") {
      return json(this.complete(data));
    }
    return json(this.release(data));
  }

  private claim(data: Required<LedgerRequest>): LedgerResponse {
    const now = Date.now();
    return this.ctx.storage.transactionSync(() => {
      this.sql.exec(
        "DELETE FROM deliveries WHERE state = 'accepted' AND updated_at < ?",
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

  private complete(data: Required<LedgerRequest>): LedgerResponse {
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

  private release(data: Required<LedgerRequest>): LedgerResponse {
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
}
