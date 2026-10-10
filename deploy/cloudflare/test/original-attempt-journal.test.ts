import { DatabaseSync } from "node:sqlite";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { OriginalAttemptJournal, ORIGINAL_ATTEMPT_LIMITS, type AttemptBinding, type AttemptTransition } from "../src/original-attempt-journal";

const binding = (): AttemptBinding => ({
  scope_digest: "a".repeat(64), event_digest: "b".repeat(64),
  operation_id: "c".repeat(64), source_digest: "d".repeat(64),
  authority_digest: "e".repeat(64), execution_identity: "f".repeat(32),
  inventory_digest: "0".repeat(64), inventory_generation: 0,
  owner_digest: "1".repeat(64), reservation_digest: "2".repeat(64),
  initial_root_digest: "3".repeat(64), initial_root_generation: 0,
});
const step = (attempt = "4"): AttemptTransition => ({
  attempt_id: attempt.repeat(64), sequence: 0,
  prior_root_digest: "3".repeat(64), prior_root_generation: 0,
  target_root_digest: "5".repeat(64), target_root_generation: 1,
  request_digest: "6".repeat(64), dispatch_digest: "7".repeat(64),
  sealed_plan_digest: "8".repeat(64), ordinary_dispatches: 6, fence_dispatches: 1,
});
function open(filename = ":memory:") {
  const database = new DatabaseSync(filename);
  const storage = {
    sql: {
      exec(query: string, ...args: unknown[]) {
        if (query.trim().startsWith("CREATE TABLE")) { database.exec(query); return []; }
        const command = database.prepare(query);
        return query.trim().startsWith("SELECT")
          ? command.all(...args as never[])
          : (command.run(...args as never[]), []);
      },
    },
    transactionSync<T>(callback: () => T): T {
      database.exec("BEGIN IMMEDIATE");
      try { const result = callback(); database.exec("COMMIT"); return result; }
      catch (error) { database.exec("ROLLBACK"); throw error; }
    },
  } as unknown as Pick<DurableObjectStorage, "sql" | "transactionSync">;
  let tick = 1000;
  const journal = new OriginalAttemptJournal(storage, () => tick);
  database.exec("CREATE TABLE IF NOT EXISTS fixture_grants (id TEXT PRIMARY KEY)");
  const grant = (id = "grant-1") => {
    database.prepare("INSERT INTO fixture_grants VALUES (?)").run(id);
    return () => database.prepare("DELETE FROM fixture_grants WHERE id=?").run(id).changes === 1;
  };
  return { journal, database, grant, tick: (value: number) => { tick = value; }, close: () => database.close() };
}

describe("dormant original-attempt SQLite primitive", () => {
  it("uses server origin and never refreshes the original deadline or charges", () => {
    const fixture = open();
    try {
      const original = fixture.journal.admit(binding(), 500, 12);
      fixture.tick(5500);
      expect(fixture.journal.admit(binding(), 5500, 1)).toEqual(original);
      expect(original).toMatchObject({ created_at_ms: 500, control_deadline_ms: 60500, calls: 12, sequence: 0 });
      fixture.tick(60500);
      expect(fixture.journal.admit(binding(), 60500, 1)).toEqual(original);
      expect(() => fixture.journal.reserve(binding(), step(), fixture.grant())).toThrow("original_attempt_expired");
    } finally { fixture.close(); }
  });

  it("keeps the event guard sticky across inventory, authority, execution and root edits", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 3);
      for (const edit of [
        { inventory_digest: "9".repeat(64) }, { inventory_generation: 1 },
        { operation_id: "9".repeat(64) }, { authority_digest: "9".repeat(64) },
        { execution_identity: "9".repeat(32) }, { owner_digest: "9".repeat(64) },
        { initial_root_generation: 1 }, { reservation_digest: "9".repeat(64) },
      ]) expect(() => fixture.journal.admit({ ...binding(), ...edit }, 1000, 1)).toThrow("original_attempt_binding_conflict");
      expect(fixture.journal.inspect(binding()).calls).toBe(3);
    } finally { fixture.close(); }
  });

  it("atomically consumes a grant and predebits the complete declared transition", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 3);
      const consume = fixture.grant();
      expect(fixture.journal.reserve(binding(), step(), consume)).toMatchObject({ first_consumption: true, state: "inflight", calls: 10, sequence: 1 });
      expect(fixture.database.prepare("SELECT COUNT(*) AS n FROM fixture_grants").get()?.n).toBe(0);
      expect(fixture.journal.reserve(binding(), step(), () => { throw new Error("replay must not consume"); })).toMatchObject({ first_consumption: false, state: "inflight", calls: 10 });
      expect(() => fixture.journal.reserve(binding(), { ...step(), dispatch_digest: "9".repeat(64) }, consume)).toThrow("original_attempt_transition_conflict");
    } finally { fixture.close(); }
  });

  it("rolls back both grant consumption and liability if the transaction fails", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 3);
      const consume = fixture.grant();
      expect(() => fixture.journal.reserve(binding(), step(), () => { consume(); return false; })).toThrow("original_attempt_grant_invalid");
      expect(fixture.journal.inspect(binding())).toMatchObject({ calls: 3, sequence: 0 });
      expect(fixture.database.prepare("SELECT COUNT(*) AS n FROM fixture_grants").get()?.n).toBe(1);
      expect(fixture.journal.reserve(binding(), step(), consume).first_consumption).toBe(true);
    } finally { fixture.close(); }
  });

  it("retains an unknown transition after database reopen and lease expiry", () => {
    const directory = mkdtempSync(join(tmpdir(), "attempt-journal-"));
    try {
      const filename = join(directory, "attempt.sqlite");
      const first = open(filename);
      first.journal.admit(binding(), 1000, 3);
      first.journal.reserve(binding(), step(), first.grant());
      first.close();
      const restarted = open(filename);
      try {
        restarted.tick(100_000);
        expect(restarted.journal.reserve(binding(), step(), () => false)).toMatchObject({ first_consumption: false, state: "inflight", calls: 10, control_deadline_ms: 61000 });
        expect(() => restarted.journal.reserve(binding(), { ...step("9"), sequence: 1 }, () => true)).toThrow("original_attempt_expired");
        expect(() => restarted.journal.confirm(binding(), step().attempt_id, binding().initial_root_digest, 0)).toThrow("original_attempt_readback_conflict");
        expect(restarted.journal.inspect(binding()).calls).toBe(10);
      } finally { restarted.close(); }
    } finally { rmSync(directory, { recursive: true }); }
  });

  it("serializes same-root mutations from distinct events without a time lease", () => {
    const fixture = open();
    try {
      const other = { ...binding(), event_digest: "9".repeat(64), operation_id: "9".repeat(64) };
      fixture.journal.admit(binding(), 1000, 3);
      fixture.journal.admit(other, 1000, 3);
      fixture.journal.reserve(binding(), step(), fixture.grant());
      const consume = fixture.grant("grant-2");
      expect(() => fixture.journal.reserve(other, step("a"), consume)).toThrow("original_attempt_inflight");
      expect(fixture.database.prepare("SELECT COUNT(*) AS n FROM fixture_grants").get()?.n).toBe(1);
      fixture.journal.confirm(binding(), step().attempt_id, step().target_root_digest, 1);
      expect(() => fixture.journal.reserve(other, step("a"), consume)).toThrow("original_attempt_root_conflict");
      expect(fixture.journal.reserve(other, { ...step("a"), prior_root_digest: "5".repeat(64), prior_root_generation: 1, target_root_digest: "b".repeat(64), target_root_generation: 2 }, consume)).toMatchObject({ first_consumption: true, calls: 10 });
    } finally { fixture.close(); }
  });

  it("retains all reserved calls after exact readback and repeated confirmation", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 3);
      fixture.journal.reserve(binding(), step(), fixture.grant());
      const settled = fixture.journal.confirm(binding(), step().attempt_id, step().target_root_digest, 1);
      expect(settled).toMatchObject({ calls: 10, sequence: 1, root_generation: 1 });
      expect(fixture.journal.confirm(binding(), step().attempt_id, step().target_root_digest, 1)).toEqual(settled);
      expect(fixture.journal.reserve(binding(), step(), () => false)).toMatchObject({ first_consumption: false, state: "confirmed", calls: 10 });
    } finally { fixture.close(); }
  });

  it("refuses prepaid overflow before consuming and preserves the four-fence ceiling", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 60);
      const consume = fixture.grant();
      expect(() => fixture.journal.reserve(binding(), step(), consume)).toThrow("original_attempt_budget");
      expect(fixture.journal.reserve(binding(), { ...step(), ordinary_dispatches: 0, fence_dispatches: 4 }, consume)).toMatchObject({ calls: 64, first_consumption: true });
      fixture.journal.confirm(binding(), step().attempt_id, step().target_root_digest, 1);
      expect(() => fixture.journal.reserve(binding(), { ...step("9"), sequence: 1, prior_root_digest: "5".repeat(64), prior_root_generation: 1, target_root_generation: 2 }, () => true)).toThrow("original_attempt_budget");
    } finally { fixture.close(); }
  });

  it("refuses source capacity without evicting expired guards or changing the scope", () => {
    const fixture = open();
    try {
      for (let index = 0; index < ORIGINAL_ATTEMPT_LIMITS.sourcesPerScope; index++) {
        fixture.journal.admit({ ...binding(), event_digest: index.toString(16).padStart(64, "0") }, 1000, 1);
      }
      fixture.tick(100_000);
      expect(() => fixture.journal.admit(binding(), 100_000, 1)).toThrow("original_attempt_capacity");
      expect(fixture.journal.inspect({ ...binding(), event_digest: "0".repeat(64) })).toMatchObject({ calls: 1, control_deadline_ms: 61000 });
      expect(fixture.database.prepare("SELECT COUNT(*) AS n FROM original_attempt_events").get()?.n).toBe(256);
    } finally { fixture.close(); }
  });

  it("refuses clock rollback below admission or committed time without consuming", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 3);
      const consume = fixture.grant();
      fixture.tick(0);
      expect(() => fixture.journal.reserve(binding(), step(), consume)).toThrow("original_attempt_clock_rollback");
      expect(fixture.journal.inspect(binding()).calls).toBe(3);
      expect(fixture.database.prepare("SELECT COUNT(*) AS n FROM fixture_grants").get()?.n).toBe(1);
      fixture.tick(5000);
      fixture.journal.reserve(binding(), step(), consume);
      fixture.journal.confirm(binding(), step().attempt_id, step().target_root_digest, 1);
      const next = { ...step("9"), sequence: 1, prior_root_digest: "5".repeat(64), prior_root_generation: 1, target_root_generation: 2 };
      fixture.tick(4999);
      expect(() => fixture.journal.reserve(binding(), next, fixture.grant("grant-2"))).toThrow("original_attempt_clock_rollback");
      expect(fixture.journal.inspect(binding())).toMatchObject({ calls: 10, control_deadline_ms: 61000 });
    } finally { fixture.close(); }
  });

  it("retains observed expiry across clock rollback and refuses missing clock origin", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 3);
      const consume = fixture.grant();
      fixture.tick(61000);
      expect(() => fixture.journal.reserve(binding(), step(), consume)).toThrow("original_attempt_expired");
      fixture.tick(60000);
      expect(() => fixture.journal.reserve(binding(), step(), consume)).toThrow("original_attempt_clock_rollback");
      expect(fixture.journal.inspect(binding())).toMatchObject({ calls: 3, control_deadline_ms: 61000 });
      expect(fixture.database.prepare("SELECT COUNT(*) AS n FROM fixture_grants").get()?.n).toBe(1);
      fixture.database.exec("DELETE FROM original_attempt_clocks");
      expect(() => fixture.journal.reserve(binding(), step(), consume)).toThrow("original_attempt_clock_origin_required");
    } finally { fixture.close(); }
  });

  it("captures immutable validated keys before the grant callback", () => {
    const fixture = open();
    try {
      const input = binding(), proposed = step();
      fixture.journal.admit(input, 1000, 3);
      const consume = fixture.grant();
      expect(fixture.journal.reserve(input, proposed, () => {
        const consumed = consume();
        proposed.attempt_id = "9".repeat(64);
        proposed.ordinary_dispatches = 60;
        input.event_digest = "9".repeat(64);
        return consumed;
      })).toMatchObject({ first_consumption: true, calls: 10 });
      const row = fixture.database.prepare("SELECT attempt_id,transition FROM original_attempt_transitions").get();
      expect(row?.attempt_id).toBe(step().attempt_id);
      expect(JSON.parse(row?.transition as string)).toEqual(step());
      expect(fixture.journal.reserve(binding(), step(), () => { throw new Error("must not consume again"); })).toMatchObject({ first_consumption: false, calls: 10 });
      fixture.journal.confirm(binding(), step().attempt_id, step().target_root_digest, 1);
    } finally { fixture.close(); }
  });

  it("rejects accessor metadata before the callback can cross validation", () => {
    const fixture = open();
    try {
      fixture.journal.admit(binding(), 1000, 3);
      const proposed = step();
      Object.defineProperty(proposed, "attempt_id", { enumerable: true, get: () => step().attempt_id });
      expect(() => fixture.journal.reserve(binding(), proposed, () => { throw new Error("must not consume"); })).toThrow("original_attempt_invalid");
      expect(fixture.journal.inspect(binding()).calls).toBe(3);
    } finally { fixture.close(); }
  });

  it("has closed scalar metadata and refuses raw prose, coercion or zero liability", () => {
    const fixture = open();
    try {
      for (const edit of [{ body: "untrusted prose" }, { inventory_generation: true }, { operation_id: ["c".repeat(64)] }]) {
        expect(() => fixture.journal.admit({ ...binding(), ...edit } as never, 1000, 3)).toThrow("original_attempt_invalid");
      }
      fixture.journal.admit(binding(), 1000, 3);
      for (const edit of [{ ordinary_dispatches: 0, fence_dispatches: 0 }, { sequence: true }, { fence_dispatches: 5 }, { target_root_generation: 2 }]) {
        expect(() => fixture.journal.reserve(binding(), { ...step(), ...edit } as never, () => true)).toThrow("original_attempt_invalid");
      }
      expect(fixture.journal.inspect(binding()).calls).toBe(3);
    } finally { fixture.close(); }
  });
});
