import { beforeEach, describe, expect, it, vi } from "vitest";

const advanceStoredContinuation = vi.hoisted(() => vi.fn());

vi.mock("../src/setup-alarm", () => ({
  advanceStoredContinuation,
}));

import { DeliveryLedger } from "../src/delivery-ledger";
import type { WorkerEnv } from "../src/env";

interface DeliveryRow {
  app_id: number;
  delivery_id: string;
  digest: string;
  state: string;
  lease_until: number;
  updated_at: number;
}

interface CursorRow {
  app_id: number;
  delivery_id: string;
  digest: string;
  cursor: string;
  updated_at: number;
  not_before: number;
}

interface OutcomeRow {
  app_id: number;
  delivery_id: string;
  error_code: string;
  failure_count: number;
  repository: string | null;
  updated_at: number;
}

class MemorySql {
  readonly deliveries: DeliveryRow[] = [];
  readonly cursors: CursorRow[] = [];
  readonly outcomes: OutcomeRow[] = [];

  exec<T = Record<string, unknown>>(query: string, ...args: unknown[]): Iterable<T> {
    const normalized = query.replaceAll(/\s+/g, " ").trim();
    if (
      normalized.startsWith("CREATE TABLE") ||
      normalized.startsWith("DELETE FROM deliveries WHERE state = 'accepted'") ||
      normalized.startsWith("DELETE FROM setup_outcomes") ||
      normalized.startsWith("ALTER TABLE")
    ) {
      return [] as T[];
    }
    if (normalized.startsWith("PRAGMA table_info(setup_continuations)")) {
      return [{ name: "not_before" }] as T[];
    }
    if (normalized.startsWith("SELECT app_id, delivery_id, digest, state, lease_until, updated_at FROM deliveries")) {
      return this.deliveries.filter(
        (row) => row.app_id === args[0] && row.delivery_id === args[1],
      ) as T[];
    }
    if (normalized.startsWith("INSERT INTO deliveries")) {
      this.deliveries.push({
        app_id: args[0] as number,
        delivery_id: args[1] as string,
        digest: args[2] as string,
        state: "processing",
        lease_until: args[3] as number,
        updated_at: args[4] as number,
      });
      return [] as T[];
    }
    if (normalized.startsWith("SELECT state, digest, lease_until FROM deliveries")) {
      return this.deliveries
        .filter((row) => row.app_id === args[0] && row.delivery_id === args[1])
        .map((row) => ({
          state: row.state,
          digest: row.digest,
          lease_until: row.lease_until,
        })) as T[];
    }
    if (normalized.startsWith("INSERT OR REPLACE INTO setup_continuations")) {
      const next = {
        app_id: args[0] as number,
        delivery_id: args[1] as string,
        digest: args[2] as string,
        cursor: args[3] as string,
        updated_at: args[4] as number,
        not_before: args[5] as number,
      };
      const index = this.cursors.findIndex(
        (row) => row.app_id === next.app_id && row.delivery_id === next.delivery_id,
      );
      if (index >= 0) {
        this.cursors[index] = next;
      } else {
        this.cursors.push(next);
      }
      return [] as T[];
    }
    if (normalized.startsWith("SELECT app_id, delivery_id, digest, cursor, updated_at FROM setup_continuations")) {
      const now = args[0] as number;
      return [...this.cursors]
        .filter((row) => row.not_before <= now)
        .sort((left, right) => left.updated_at - right.updated_at)
        .slice(0, 1) as T[];
    }
    if (normalized.startsWith("SELECT app_id FROM setup_continuations WHERE app_id = ?")) {
      return this.cursors
        .filter((row) => row.app_id === args[0] && row.delivery_id === args[1])
        .slice(0, 1)
        .map((row) => ({ app_id: row.app_id })) as T[];
    }
    if (normalized.startsWith("SELECT app_id FROM setup_continuations WHERE not_before")) {
      const now = args[0] as number;
      return this.cursors
        .filter((row) => row.not_before <= now)
        .slice(0, 1)
        .map((row) => ({ app_id: row.app_id })) as T[];
    }
    if (normalized.startsWith("SELECT MIN(not_before)")) {
      if (this.cursors.length === 0) {
        return [{ not_before: null }] as T[];
      }
      return [{
        not_before: Math.min(...this.cursors.map((row) => row.not_before)),
      }] as T[];
    }
    if (normalized.startsWith("UPDATE deliveries SET lease_until")) {
      const row = this.deliveries.find(
        (item) =>
          item.app_id === args[2] &&
          item.delivery_id === args[3] &&
          item.digest === args[4] &&
          item.state === "processing",
      );
      if (row) {
        row.lease_until = args[0] as number;
        row.updated_at = args[1] as number;
      }
      return [] as T[];
    }
    if (normalized.startsWith("SELECT state, digest FROM deliveries")) {
      return this.deliveries
        .filter((row) => row.app_id === args[0] && row.delivery_id === args[1])
        .map((row) => ({ state: row.state, digest: row.digest })) as T[];
    }
    if (normalized.startsWith("UPDATE deliveries SET state = 'accepted'")) {
      const row = this.deliveries.find(
        (item) => item.app_id === args[3] && item.delivery_id === args[4] && item.digest === args[5],
      );
      if (row) {
        row.state = "accepted";
        row.lease_until = args[0] as number;
        row.updated_at = args[1] as number;
      }
      return [] as T[];
    }
    if (normalized.startsWith("DELETE FROM deliveries WHERE app_id")) {
      const index = this.deliveries.findIndex(
        (row) =>
          row.app_id === args[0] &&
          row.delivery_id === args[1] &&
          row.digest === args[2] &&
          row.state === "processing",
      );
      if (index >= 0) {
        this.deliveries.splice(index, 1);
      }
      return [] as T[];
    }
    if (normalized.startsWith("DELETE FROM setup_continuations")) {
      const index = this.cursors.findIndex(
        (row) => row.app_id === args[0] && row.delivery_id === args[1],
      );
      if (index >= 0) {
        this.cursors.splice(index, 1);
      }
      return [] as T[];
    }
    if (normalized.startsWith("INSERT OR REPLACE INTO setup_outcomes")) {
      this.outcomes.push({
        app_id: args[0] as number,
        delivery_id: args[1] as string,
        error_code: args[2] as string,
        failure_count: args[3] as number,
        repository: (args[4] as string | null) ?? null,
        updated_at: args[5] as number,
      });
      return [] as T[];
    }
    throw new Error(`unexpected SQL: ${normalized}`);
  }
}

function harness(failAlarmAfter = Number.POSITIVE_INFINITY) {
  const sql = new MemorySql();
  const alarms: number[] = [];
  let alarmCalls = 0;
  const state = {
    storage: {
      sql,
      transactionSync: <T>(callback: () => T): T => callback(),
      setAlarm: async (when: number) => {
        alarmCalls += 1;
        if (alarmCalls > failAlarmAfter) {
          throw new Error("alarm storage unavailable");
        }
        alarms.push(when);
      },
    },
  } as unknown as DurableObjectState;
  return {
    sql,
    alarms,
    ledger: new DeliveryLedger(state, {} as WorkerEnv),
  };
}

const DIGEST = "ab".repeat(32);

function continuation(overrides: Record<string, unknown> = {}) {
  return {
    appId: 12345,
    event: "installation",
    action: "created",
    installationId: 2468,
    deliveryId: "delivery-1",
    digest: DIGEST,
    permissions: {
      contents: "write",
      pull_requests: "write",
      variables: "write",
      workflows: "write",
    },
    repositories: ["acme/one", "acme/two"],
    unresolved: false,
    attempt: 0,
    failed: false,
    failureCode: null,
    failureCount: 0,
    failedRepository: null,
    ...overrides,
  };
}

async function post(ledger: DeliveryLedger, action: string, body: unknown): Promise<Response> {
  return ledger.fetch(
    new Request(`https://ledger/${action}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  );
}

beforeEach(() => {
  advanceStoredContinuation.mockReset();
});

describe("delivery continuation alarm", () => {
  it("arms an alarm only after the claim and cursor are stored", async () => {
    const { ledger, sql, alarms } = harness();
    expect(
      await (await post(ledger, "claim", {
        app_id: 12345,
        delivery_id: "delivery-1",
        digest: DIGEST,
      })).json(),
    ).toMatchObject({ state: "claimed" });
    expect(alarms).toEqual([]);

    const scheduled = await post(ledger, "schedule", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
      continuation: continuation(),
    });
    expect(scheduled.status).toBe(200);
    expect(sql.cursors).toHaveLength(1);
    expect(JSON.parse(sql.cursors[0]?.cursor ?? "{}")).toMatchObject({
      repositories: ["acme/one", "acme/two"],
    });
    expect(alarms).toHaveLength(1);
  });

  it("records the failed repository when the alarm releases the claim", async () => {
    const { ledger, sql } = harness();
    await post(ledger, "claim", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
    });
    await post(ledger, "schedule", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
      continuation: continuation(),
    });
    advanceStoredContinuation.mockResolvedValue({
      kind: "release",
      summary: {
        errorCode: "github_request_rejected_404",
        failureCount: 1,
        repository: "acme/one",
      },
    });

    await ledger.alarm();

    expect(sql.cursors).toEqual([]);
    expect(sql.deliveries).toEqual([]);
    expect(sql.outcomes).toEqual([
      expect.objectContaining({
        delivery_id: "delivery-1",
        error_code: "github_request_rejected_404",
        failure_count: 1,
        repository: "acme/one",
      }),
    ]);
  });

  it("waits out the retry delay before the same continuation is due again", async () => {
    const { ledger, sql, alarms } = harness();
    await post(ledger, "claim", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
    });
    await post(ledger, "schedule", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
      continuation: continuation(),
    });
    const before = Date.now();
    advanceStoredContinuation.mockResolvedValue({
      kind: "continue",
      next: continuation({ attempt: 1 }),
      delayMs: 1000,
    });

    await ledger.alarm();

    expect(sql.cursors[0]?.not_before).toBeGreaterThanOrEqual(before + 1000);
    expect(alarms.at(-1)).toBe(sql.cursors[0]?.not_before);
  });

  it("records an outcome when the stored cursor cannot be parsed", async () => {
    const { ledger, sql } = harness();
    await post(ledger, "claim", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
    });
    sql.cursors.push({
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
      cursor: "{",
      updated_at: Date.now(),
      not_before: 0,
    });
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);

    await ledger.alarm();

    expect(sql.cursors).toEqual([]);
    expect(sql.deliveries).toEqual([]);
    expect(sql.outcomes).toEqual([
      expect.objectContaining({
        error_code: "setup_continuation_invalid",
        failure_count: 1,
        repository: null,
      }),
    ]);
    expect(error).toHaveBeenCalledWith("github_setup_incomplete", {
      delivery_id: "delivery-1",
      error_code: "setup_continuation_invalid",
      failure_count: 1,
    });
    error.mockRestore();
  });

  it("rearms at not_before when the only continuation is not due", async () => {
    const { ledger, sql, alarms } = harness();
    const notBefore = Date.now() + 5_000;
    sql.cursors.push({
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
      cursor: JSON.stringify(continuation()),
      updated_at: Date.now(),
      not_before: notBefore,
    });

    await ledger.alarm();

    expect(advanceStoredContinuation).not.toHaveBeenCalled();
    expect(alarms).toEqual([notBefore]);
  });

  it("keeps the claim and cursor when arming the schedule alarm fails", async () => {
    const { ledger, sql } = harness(0);
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await post(ledger, "claim", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
    });

    const scheduled = await post(ledger, "schedule", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
      continuation: continuation(),
    });

    expect(scheduled.status).toBe(503);
    expect(sql.deliveries).toEqual([
      expect.objectContaining({ state: "processing", delivery_id: "delivery-1" }),
    ]);
    expect(sql.cursors).toHaveLength(1);
    expect(error).toHaveBeenCalledWith("github_setup_alarm_failed", {
      error_code: "setup_alarm_unavailable",
    });

    const released = await post(ledger, "release", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
    });
    expect(released.status).toBe(200);
    expect(sql.deliveries).toEqual([]);
    expect(sql.cursors).toEqual([]);
    error.mockRestore();
  });

  it("logs and throws when the follow-up alarm cannot be armed", async () => {
    const { ledger, sql } = harness(1);
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await post(ledger, "claim", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
    });
    await post(ledger, "schedule", {
      app_id: 12345,
      delivery_id: "delivery-1",
      digest: DIGEST,
      continuation: continuation(),
    });
    advanceStoredContinuation.mockResolvedValue({
      kind: "continue",
      next: continuation({ attempt: 1 }),
      delayMs: 1000,
    });

    await expect(ledger.alarm()).rejects.toThrow("alarm storage unavailable");
    expect(sql.cursors).toHaveLength(1);
    expect(sql.deliveries[0]?.state).toBe("processing");
    expect(error).toHaveBeenCalledWith("github_setup_alarm_failed", {
      error_code: "setup_alarm_unavailable",
    });
    error.mockRestore();
  });
});
