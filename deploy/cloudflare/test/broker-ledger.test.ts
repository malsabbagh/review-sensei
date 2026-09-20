import { afterEach, describe, expect, it, vi } from "vitest";
import { BrokerLedger } from "../src/broker-ledger";
import type { WorkerEnv } from "../src/env";

interface ReplayRow {
  expiresAt: number;
}

interface RateRow {
  windowStarted: number;
  count: number;
}

interface EnrollmentRow {
  enrolledAt: number;
}

class MemorySql {
  readonly replays = new Map<string, ReplayRow>();
  readonly rates = new Map<string, RateRow>();
  readonly enrollments = new Map<string, EnrollmentRow>();
  readonly observedArguments: unknown[] = [];

  exec<T = Record<string, unknown>>(query: string, ...args: unknown[]): Iterable<T> {
    this.observedArguments.push(...args);
    const normalized = query.replaceAll(/\s+/g, " ").trim();
    if (normalized.startsWith("CREATE TABLE")) {
      return [];
    }
    if (normalized.startsWith("DELETE FROM broker_replays WHERE expires_at")) {
      const threshold = args[0] as number;
      for (const [key, row] of this.replays) {
        if (row.expiresAt < threshold) this.replays.delete(key);
      }
      return [];
    }
    if (normalized.startsWith("DELETE FROM broker_rates WHERE window_started")) {
      const threshold = args[0] as number;
      for (const [key, row] of this.rates) {
        if (row.windowStarted < threshold) this.rates.delete(key);
      }
      return [];
    }
    if (normalized.startsWith("DELETE FROM broker_session_enrollments WHERE enrolled_at")) {
      const threshold = args[0] as number;
      for (const [key, row] of this.enrollments) {
        if (row.enrolledAt < threshold) this.enrollments.delete(key);
      }
      return [];
    }
    if (normalized.startsWith("SELECT jti_hash")) {
      return (this.replays.has(args[0] as string)
        ? [{ jti_hash: args[0] }]
        : []) as T[];
    }
    if (normalized.startsWith("SELECT window_started")) {
      const row = this.rates.get(args[0] as string);
      return (row ? [{ window_started: row.windowStarted, count: row.count }] : []) as T[];
    }
    if (normalized.startsWith("INSERT INTO broker_replays")) {
      this.replays.set(args[0] as string, { expiresAt: args[1] as number });
      return [];
    }
    if (normalized.startsWith("INSERT OR REPLACE INTO broker_rates")) {
      this.rates.set(args[0] as string, { windowStarted: args[1] as number, count: 1 });
      return [];
    }
    if (normalized.startsWith("UPDATE broker_rates SET count")) {
      const existing = this.rates.get(args[1] as string);
      this.rates.set(args[1] as string, {
        windowStarted: existing?.windowStarted ?? 0,
        count: args[0] as number,
      });
      return [];
    }
    if (normalized.startsWith("SELECT scope_hash FROM broker_session_enrollments")) {
      return (this.enrollments.has(args[0] as string)
        ? [{ scope_hash: args[0] }]
        : []) as T[];
    }
    if (normalized.startsWith("INSERT INTO broker_session_enrollments")) {
      this.enrollments.set(args[0] as string, { enrolledAt: args[1] as number });
      return [];
    }
    if (normalized.startsWith("UPDATE broker_session_enrollments SET enrolled_at")) {
      const existing = this.enrollments.get(args[1] as string);
      if (existing) {
        this.enrollments.set(args[1] as string, { enrolledAt: args[0] as number });
      }
      return [];
    }
    throw new Error(`unexpected SQL: ${normalized}`);
  }
}

function ledgerHarness(): { ledger: BrokerLedger; sql: MemorySql } {
  const sql = new MemorySql();
  const state = {
    storage: {
      sql,
      transactionSync: <T>(callback: () => T): T => callback(),
    },
  } as unknown as DurableObjectState;
  return {
    ledger: new BrokerLedger(state, {} as WorkerEnv),
    sql,
  };
}

async function claim(ledger: BrokerLedger, jti: string, scope = "acme/repo:review"): Promise<Response> {
  return ledger.fetch(
    new Request("https://broker/claim", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "claim", jti, scope }),
    }),
  );
}

async function admit(ledger: BrokerLedger, jti: string, scope = "preauth:203.0.113.7"): Promise<Response> {
  return ledger.fetch(
    new Request("https://broker/admit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "admit", jti, scope }),
    }),
  );
}

async function enroll(ledger: BrokerLedger, scope = "987654321:7"): Promise<Response> {
  return ledger.fetch(
    new Request("https://broker/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "session_enroll", scope }),
    }),
  );
}

afterEach(() => vi.restoreAllMocks());

describe("broker replay and rate ledger", () => {
  it("accepts once, rejects a replay, and stores only hashed identities", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    const { ledger, sql } = ledgerHarness();

    expect(await (await claim(ledger, "delivery-1")).json()).toEqual({ state: "accepted" });
    expect(await (await claim(ledger, "delivery-1")).json()).toEqual({ state: "replay" });

    expect([...sql.replays.keys()]).toHaveLength(1);
    expect([...sql.rates.keys()]).toHaveLength(1);
    for (const identity of [...sql.replays.keys(), ...sql.rates.keys()]) {
      expect(identity).toMatch(/^[0-9a-f]{64}$/);
      expect(identity).not.toContain("delivery-1");
      expect(identity).not.toContain("acme/repo");
    }
    expect(sql.observedArguments).not.toContain("delivery-1");
    expect(sql.observedArguments).not.toContain("acme/repo:review");
  });

  it("rate limits the eleventh distinct assertion in one scope", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    const { ledger } = ledgerHarness();
    for (let index = 1; index <= 10; index += 1) {
      expect(await (await claim(ledger, `delivery-${index}`)).json()).toEqual({ state: "accepted" });
    }
    expect(await (await claim(ledger, "delivery-11")).json()).toEqual({ state: "rate_limited" });
  });

  it("rate-limits pre-auth admission without creating replay entries", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    const { ledger, sql } = ledgerHarness();
    for (let index = 1; index <= 10; index += 1) {
      expect(await (await admit(ledger, `request-${index}`)).json()).toEqual({
        state: "accepted",
      });
    }
    expect(await (await admit(ledger, "request-11")).json()).toEqual({
      state: "rate_limited",
    });
    expect(sql.replays.size).toBe(0);
  });

  it("records a first session enrollment separately from the mutable comment ledger", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    const { ledger, sql } = ledgerHarness();

    expect(await (await enroll(ledger)).json()).toEqual({ state: "enrolled" });
    expect(await (await enroll(ledger)).json()).toEqual({ state: "known" });
    expect([...sql.enrollments.keys()]).toHaveLength(1);
    expect([...sql.enrollments.keys()][0]).toMatch(/^[0-9a-f]{64}$/);
    expect(sql.observedArguments).not.toContain("987654321:7");
  });

  it("rejects malformed session enrollments without persisting rows", async () => {
    const { ledger, sql } = ledgerHarness();

    const missingScope = await ledger.fetch(
      new Request("https://broker/session", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: "session_enroll" }),
      }),
    );
    expect(missingScope.status).toBe(400);
    expect(await missingScope.json()).toEqual({ error: "invalid_request" });

    const nonStringScope = await ledger.fetch(
      new Request("https://broker/session", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: "session_enroll", scope: 123 }),
      }),
    );
    expect(nonStringScope.status).toBe(400);
    expect(await nonStringScope.json()).toEqual({ error: "invalid_request" });

    const outOfPatternScope = await enroll(ledger, "has a space");
    expect(outOfPatternScope.status).toBe(400);
    expect(await outOfPatternScope.json()).toEqual({ error: "invalid_request" });

    expect(sql.enrollments.size).toBe(0);
  });

  it("reuses the retention cleanup inside the session enrollment transaction", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    const { ledger, sql } = ledgerHarness();
    // Stale replay and rate rows from a previous claim must be reclaimed by
    // the enrollment transaction, while a still-retained replay survives.
    sql.replays.set("a".repeat(64), { expiresAt: 1_699_999_000_000 });
    sql.replays.set("b".repeat(64), { expiresAt: 1_700_000_100_000 });
    sql.rates.set("c".repeat(64), { windowStarted: 1_699_999_000_000, count: 3 });

    expect(await (await enroll(ledger)).json()).toEqual({ state: "enrolled" });

    expect([...sql.replays.keys()]).toEqual(["b".repeat(64)]);
    expect(sql.rates.size).toBe(0);
    expect(sql.enrollments.size).toBe(1);
  });

  it("slides the enrollment retention window on every known lookup", async () => {
    const day = 24 * 60 * 60 * 1000;
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    const { ledger } = ledgerHarness();
    const scope = `987654321:7:${"a".repeat(40)}`;

    expect(await (await enroll(ledger, scope)).json()).toEqual({ state: "enrolled" });

    // An active pull request can keep extending its session, so a known lookup
    // moves the witness window instead of letting it expire mid-session.
    clock.mockReturnValue(1_700_000_000_000 + 89 * day);
    expect(await (await enroll(ledger, scope)).json()).toEqual({ state: "known" });

    clock.mockReturnValue(1_700_000_000_000 + 95 * day);
    expect(await (await enroll(ledger, scope)).json()).toEqual({ state: "known" });
  });

  it("prunes enrollment witnesses past the session retention window", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    const { ledger, sql } = ledgerHarness();
    const retention = 90 * 24 * 60 * 60 * 1000;
    sql.enrollments.set("expired".padEnd(64, "0"), {
      enrolledAt: 1_700_000_000_000 - retention - 1,
    });
    sql.enrollments.set("retained".padEnd(64, "0"), {
      enrolledAt: 1_700_000_000_000 - retention + 1,
    });

    expect(await (await enroll(ledger, "987654321:7:" + "a".repeat(40))).json()).toEqual({
      state: "enrolled",
    });

    expect([...sql.enrollments.keys()]).not.toContain("expired".padEnd(64, "0"));
    expect(sql.enrollments.size).toBe(2);
  });

  it("rejects malformed and non-POST requests without persisting them", async () => {
    const { ledger, sql } = ledgerHarness();
    const method = await ledger.fetch(new Request("https://broker/claim"));
    expect(method.status).toBe(405);
    expect(method.headers.get("cache-control")).toBe("no-store");

    const invalid = await claim(ledger, "contains a space");
    expect(invalid.status).toBe(400);
    expect(sql.replays.size).toBe(0);
    expect(sql.rates.size).toBe(0);
  });
});
