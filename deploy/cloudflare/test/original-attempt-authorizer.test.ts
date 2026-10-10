import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  OriginalAttemptAuthorizer,
  ORIGINAL_CONTRACTS,
  UNINTERRUPTED_BOOTSTRAP_DISPATCHES,
  UNINTERRUPTED_PROFILE,
  type OriginalAdmission,
} from "../src/original-attempt-authorizer";
import { OriginalAttemptJournal, ORIGINAL_ATTEMPT_LIMITS, type AttemptTransition } from "../src/original-attempt-journal";

const hex = (character: string) => character.repeat(64);
const WORKFLOW = "acme/widgets/.github/workflows/review.yml@refs/tags/uninterrupted-v1";
const REASSESSMENT = {
  workflow_ref: WORKFLOW,
  artifact_sha256: hex("a"),
  dependency_lock_sha256: hex("b"),
  declaration_sha256: hex("c"),
  contract: "existing-inventory-reassessment" as const,
};
const FIRST_REVIEW = {
  workflow_ref: WORKFLOW,
  artifact_sha256: hex("d"),
  dependency_lock_sha256: hex("e"),
  declaration_sha256: hex("f"),
  contract: "first-full-review-inventory" as const,
};

function admission(edit: Partial<OriginalAdmission> = {}): OriginalAdmission {
  return {
    inventory_sha256: hex("1"),
    inventory_generation: 0,
    operation_sha256: hex("2"),
    source_sha256: hex("3"),
    initial_root_sha256: hex("4"),
    initial_root_generation: 0,
    ...edit,
  };
}

function step(attempt = "5", edit: Partial<AttemptTransition> = {}): AttemptTransition {
  return {
    attempt_id: attempt.repeat(64),
    sequence: 0,
    prior_root_digest: hex("4"),
    prior_root_generation: 0,
    target_root_digest: hex("6"),
    target_root_generation: 1,
    request_digest: hex("7"),
    dispatch_digest: hex("8"),
    sealed_plan_digest: hex("9"),
    ordinary_dispatches: 6,
    fence_dispatches: 1,
    ...edit,
  };
}

function open(filename = ":memory:") {
  const database = new DatabaseSync(filename);
  const storage = {
    sql: {
      exec(query: string, ...args: unknown[]) {
        if (query.trim().startsWith("CREATE TABLE")) {
          database.exec(query);
          return [];
        }
        const command = database.prepare(query);
        return query.trim().startsWith("SELECT")
          ? command.all(...args as never[])
          : (command.run(...args as never[]), []);
      },
    },
    transactionSync<T>(callback: () => T): T {
      database.exec("BEGIN IMMEDIATE");
      try {
        const result = callback();
        database.exec("COMMIT");
        return result;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
  } as unknown as Pick<DurableObjectStorage, "sql" | "transactionSync">;
  let tick = 1000;
  const authorizer = new OriginalAttemptAuthorizer(storage, [REASSESSMENT, FIRST_REVIEW], () => tick);
  database.exec("CREATE TABLE IF NOT EXISTS fixture_grants (id TEXT PRIMARY KEY)");
  const grant = (id = "grant-1") => {
    database.prepare("INSERT INTO fixture_grants VALUES (?)").run(id);
    return () => database.prepare("DELETE FROM fixture_grants WHERE id=?").run(id).changes === 1;
  };
  return {
    authorizer,
    database,
    grant,
    tick: (value: number) => {
      tick = value;
    },
    close: () => database.close(),
  };
}

function issued(authorizer: OriginalAttemptAuthorizer, edit: Record<string, unknown> = {}) {
  const challenge = authorizer.issueChallenge();
  return {
    workflow_ref: REASSESSMENT.workflow_ref,
    runner_environment: "github-hosted",
    artifact_sha256: REASSESSMENT.artifact_sha256,
    dependency_lock_sha256: REASSESSMENT.dependency_lock_sha256,
    declaration_sha256: REASSESSMENT.declaration_sha256,
    contract: REASSESSMENT.contract,
    repository_id: 42,
    pull_request: 7,
    actor_id: 99,
    server_challenge: challenge,
    token_jti: `jti-${challenge.slice(0, 16)}`,
    ...edit,
  };
}

function events(database: DatabaseSync): number {
  return (database.prepare("SELECT COUNT(*) AS n FROM original_attempt_events").get() as { n: number }).n;
}

function origins(database: DatabaseSync) {
  return database.prepare(
    "SELECT created_at_ms, control_deadline_ms, calls FROM original_attempt_events ORDER BY created_at_ms, calls",
  ).all() as { created_at_ms: number; control_deadline_ms: number; calls: number }[];
}

function origin(database: DatabaseSync) {
  const rows = origins(database);
  if (rows.length !== 1) {
    throw new Error(`expected one original event, found ${rows.length}`);
  }
  return rows[0];
}

function transitionRows(database: DatabaseSync) {
  return database.prepare("SELECT state FROM original_attempt_transitions").all() as { state: string }[];
}

function scopeRoot(database: DatabaseSync) {
  return database.prepare("SELECT root_digest, root_generation FROM original_attempt_scopes").get() as {
    root_digest: string;
    root_generation: number;
  };
}

function grants(database: DatabaseSync): number {
  return (database.prepare("SELECT COUNT(*) AS n FROM fixture_grants").get() as { n: number }).n;
}

function trackJournal() {
  const counts = { admit: 0, reserve: 0 };
  const admit = OriginalAttemptJournal.prototype.admit;
  const reserve = OriginalAttemptJournal.prototype.reserve;
  OriginalAttemptJournal.prototype.admit = function(value, started, bootstrap) {
    counts.admit += 1;
    return admit.call(this, value, started, bootstrap);
  };
  OriginalAttemptJournal.prototype.reserve = function(value, proposed, consumeGrant) {
    counts.reserve += 1;
    return reserve.call(this, value, proposed, consumeGrant);
  };
  return {
    counts,
    restore() {
      OriginalAttemptJournal.prototype.admit = admit;
      OriginalAttemptJournal.prototype.reserve = reserve;
    },
  };
}

describe("uninterrupted-v1 original-attempt authorizer", () => {
  it("names delegated execution assurance and stays off the worker route", () => {
    const source = readFileSync(new URL("../src/original-attempt-authorizer.ts", import.meta.url), "utf8");
    expect(source).toContain("delegated execution assurance");
    expect(source.toLowerCase()).not.toContain("proves the process executed");
    expect(UNINTERRUPTED_PROFILE).toBe("uninterrupted-v1");
    expect(ORIGINAL_CONTRACTS).toEqual(["existing-inventory-reassessment", "first-full-review-inventory"]);
    for (const name of ["worker.ts", "token-broker.ts", "broker-ledger.ts"]) {
      const entry = readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");
      expect(entry).not.toContain("original-attempt-authorizer");
    }
  });

  it("refuses unauthenticated and unapproved producers before any victim event", async () => {
    const fixture = open();
    const tracker = trackJournal();
    try {
      expect(() => fixture.authorizer.issueChallenge("caller-nonce")).toThrow("original_attempt_authorizer_challenge");
      for (let index = 0; index < 20; index += 1) {
        await expect(fixture.authorizer.beginAuthenticated(
          { signed_oidc_already_verified: true, event_digest: index.toString(16).padStart(64, "0") },
          admission(),
          { committed: true },
        )).rejects.toThrow("original_attempt_authorizer_invalid");
      }
      const valid = issued(fixture.authorizer);
      await expect(fixture.authorizer.beginAuthenticated(
        { ...valid, signed_oidc_already_verified: true },
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_invalid");
      await expect(fixture.authorizer.beginAuthenticated(
        { ...issued(fixture.authorizer), server_challenge: hex("0") },
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_challenge");
      for (const edit of [
        { artifact_sha256: "A".repeat(64) },
        { dependency_lock_sha256: "g".repeat(64) },
        { declaration_sha256: "ab" },
      ]) {
        await expect(fixture.authorizer.beginAuthenticated(
          { ...issued(fixture.authorizer), ...edit },
          admission(),
          { committed: true },
        )).rejects.toThrow("original_attempt_authorizer_invalid");
      }
      const accessor = issued(fixture.authorizer);
      Object.defineProperty(accessor, "artifact_sha256", {
        enumerable: true,
        get() {
          throw new Error("accessor_invoked");
        },
      });
      await expect(fixture.authorizer.beginAuthenticated(accessor, admission(), { committed: true })).rejects.toThrow("original_attempt_authorizer_invalid");
      await expect(fixture.authorizer.beginAuthenticated(
        { ...issued(fixture.authorizer), runner_environment: "self-hosted" },
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_self_hosted");
      await expect(fixture.authorizer.beginAuthenticated(
        { ...issued(fixture.authorizer), artifact_sha256: hex("9") },
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_artifact_mismatch");
      await expect(fixture.authorizer.beginAuthenticated(
        {
          ...issued(fixture.authorizer),
          contract: FIRST_REVIEW.contract,
          artifact_sha256: FIRST_REVIEW.artifact_sha256,
          dependency_lock_sha256: FIRST_REVIEW.dependency_lock_sha256,
        },
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_declaration_replay");
      await expect(fixture.authorizer.beginAuthenticated(
        issued(fixture.authorizer),
        { ...admission(), reservation_sha256: hex("e"), event_digest: hex("f") },
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_invalid");
      expect(events(fixture.database)).toBe(0);
      expect(tracker.counts.admit).toBe(0);
    } finally {
      tracker.restore();
      fixture.close();
    }
  });

  it("rejects a registry that replays one declaration under both contracts", () => {
    expect(() => new OriginalAttemptAuthorizer({} as never, [])).toThrow("original_attempt_authorizer_registry");
    expect(() => new OriginalAttemptAuthorizer({} as never, [
      REASSESSMENT,
      { ...FIRST_REVIEW, declaration_sha256: REASSESSMENT.declaration_sha256 },
    ])).toThrow("original_attempt_authorizer_registry");
    expect(() => new OriginalAttemptAuthorizer({} as never, [
      { ...REASSESSMENT, runner_environment: "self-hosted" },
    ])).toThrow("original_attempt_authorizer_invalid");
  });

  it("leaves no event when admission is interrupted and admits once a later proof commits", async () => {
    const fixture = open();
    const tracker = trackJournal();
    try {
      await expect(fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: false })).rejects.toThrow("original_attempt_authorizer_interrupted");
      await expect(fixture.authorizer.refuseInterrupted(issued(fixture.authorizer), admission())).rejects.toThrow("original_attempt_authorizer_interrupted");
      await expect(fixture.authorizer.refuseInterrupted({ signed_oidc_already_verified: true }, admission())).rejects.toThrow("original_attempt_authorizer_invalid");
      expect(events(fixture.database)).toBe(0);
      expect(tracker.counts.admit).toBe(0);
      const begun = await fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: true });
      expect(begun.profile).toBe(UNINTERRUPTED_PROFILE);
      expect(begun.snapshot).toMatchObject({
        created_at_ms: 1000,
        control_deadline_ms: 61_000,
        calls: UNINTERRUPTED_BOOTSTRAP_DISPATCHES,
        sequence: 0,
      });
      expect(begun.snapshot.control_deadline_ms - begun.snapshot.created_at_ms).toBe(ORIGINAL_ATTEMPT_LIMITS.controlMilliseconds);
      expect(events(fixture.database)).toBe(1);
      expect(tracker.counts.admit).toBe(1);
    } finally {
      tracker.restore();
      fixture.close();
    }
  });

  it("returns the original snapshot for a fresh token and conflicts without refreshing time", async () => {
    const fixture = open();
    try {
      const begun = await fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: true });
      fixture.tick(5500);
      const replayed = await fixture.authorizer.beginAuthenticated(
        issued(fixture.authorizer, { token_jti: "fresh-token-jti-22" }),
        admission(),
        { committed: true },
      );
      expect(replayed.snapshot).toEqual(begun.snapshot);
      expect(replayed.snapshot).toMatchObject({ created_at_ms: 1000, control_deadline_ms: 61_000, calls: 1 });
      expect(events(fixture.database)).toBe(1);
      const spent = issued(fixture.authorizer);
      await fixture.authorizer.beginAuthenticated(spent, admission(), { committed: true });
      await expect(fixture.authorizer.beginAuthenticated(
        { ...spent, server_challenge: fixture.authorizer.issueChallenge() },
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_token_replay");
      fixture.tick(8000);
      await expect(fixture.authorizer.beginAuthenticated(
        issued(fixture.authorizer),
        admission({ inventory_sha256: hex("a") }),
        { committed: true },
      )).rejects.toThrow("original_attempt_binding_conflict");
      await expect(fixture.authorizer.beginAuthenticated(
        issued(fixture.authorizer),
        admission({ operation_sha256: hex("b") }),
        { committed: true },
      )).rejects.toThrow("original_attempt_binding_conflict");
      await expect(fixture.authorizer.beginAuthenticated(
        issued(fixture.authorizer, { actor_id: 100 }),
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_binding_conflict");
      expect(events(fixture.database)).toBe(1);
      expect(origin(fixture.database)).toMatchObject({ created_at_ms: 1000, control_deadline_ms: 61_000, calls: 1 });
      const other = await fixture.authorizer.beginAuthenticated(
        issued(fixture.authorizer, {
          contract: FIRST_REVIEW.contract,
          artifact_sha256: FIRST_REVIEW.artifact_sha256,
          dependency_lock_sha256: FIRST_REVIEW.dependency_lock_sha256,
          declaration_sha256: FIRST_REVIEW.declaration_sha256,
        }),
        admission(),
        { committed: true },
      );
      expect(other.snapshot).toMatchObject({ created_at_ms: 8000, control_deadline_ms: 68_000, calls: 1 });
      expect(events(fixture.database)).toBe(2);
      await expect(fixture.authorizer.beginAuthenticated(
        issued(fixture.authorizer, {
          contract: FIRST_REVIEW.contract,
          artifact_sha256: FIRST_REVIEW.artifact_sha256,
          dependency_lock_sha256: FIRST_REVIEW.dependency_lock_sha256,
        }),
        admission(),
        { committed: true },
      )).rejects.toThrow("original_attempt_authorizer_declaration_replay");
      expect(events(fixture.database)).toBe(2);
      expect(origins(fixture.database)).toEqual([
        { created_at_ms: 1000, control_deadline_ms: 61_000, calls: 1 },
        { created_at_ms: 8000, control_deadline_ms: 68_000, calls: 1 },
      ]);
    } finally {
      fixture.close();
    }
  });

  it("restores retained state from a fresh proof without admitting or granting", async () => {
    const fixture = open();
    try {
      await expect(fixture.authorizer.restore(
        { created_at_ms: 1000, control_deadline_ms: 61_000, calls: 1, sequence: 0, root_digest: hex("4"), root_generation: 0 },
        admission(),
      )).rejects.toThrow("original_attempt_authorizer_invalid");
      await expect(fixture.authorizer.restore(issued(fixture.authorizer), admission())).rejects.toThrow("original_attempt_authorizer_missing");
      expect(events(fixture.database)).toBe(0);
      const begun = await fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: true });
      const tracker = trackJournal();
      try {
        const restored = await fixture.authorizer.restore(issued(fixture.authorizer), admission());
        expect(restored.profile).toBe(UNINTERRUPTED_PROFILE);
        expect(restored.snapshot).toEqual(begun.snapshot);
        expect(restored.transitions).toEqual([]);
        expect(restored).not.toHaveProperty("grant");
        expect(restored).not.toHaveProperty("permit");
        expect(restored).not.toHaveProperty("first_consumption");
        expect(tracker.counts.admit).toBe(0);
        expect(origin(fixture.database).calls).toBe(UNINTERRUPTED_BOOTSTRAP_DISPATCHES);
      } finally {
        tracker.restore();
      }
    } finally {
      fixture.close();
    }
  });

  it("debits once, rolls back a refused grant, and keeps unknown inflight after expiry", async () => {
    const fixture = open();
    try {
      await fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: true });
      const consume = fixture.grant();
      await expect(fixture.authorizer.consume(issued(fixture.authorizer), admission(), step(), () => {
        consume();
        return false;
      })).rejects.toThrow("original_attempt_grant_invalid");
      expect(transitionRows(fixture.database)).toEqual([]);
      expect(grants(fixture.database)).toBe(1);
      expect(origin(fixture.database).calls).toBe(UNINTERRUPTED_BOOTSTRAP_DISPATCHES);
      const reserved = await fixture.authorizer.consume(issued(fixture.authorizer), admission(), step(), consume);
      expect(reserved).toMatchObject({ profile: UNINTERRUPTED_PROFILE, first_consumption: true, state: "inflight", calls: 8, sequence: 1 });
      expect(reserved).not.toHaveProperty("permit");
      expect(reserved).not.toHaveProperty("grant");
      expect(grants(fixture.database)).toBe(0);
      const replay = await fixture.authorizer.consume(issued(fixture.authorizer), admission(), step(), () => {
        throw new Error("replay must not consume");
      });
      expect(replay).toMatchObject({ first_consumption: false, state: "inflight", calls: 8, control_deadline_ms: 61_000 });
      expect(Object.keys(replay).sort()).toEqual([
        "calls", "control_deadline_ms", "created_at_ms", "first_consumption", "profile", "root_digest", "root_generation", "sequence", "state",
      ]);
      fixture.tick(61_001);
      const retained = await fixture.authorizer.consume(issued(fixture.authorizer), admission(), step(), () => {
        throw new Error("expired replay must not consume");
      });
      expect(retained).toMatchObject({ first_consumption: false, state: "inflight", calls: 8, control_deadline_ms: 61_000, created_at_ms: 1000 });
      await expect(fixture.authorizer.consume(
        issued(fixture.authorizer),
        admission(),
        step("a", { sequence: 1 }),
        () => true,
      )).rejects.toThrow("original_attempt_expired");
      expect(transitionRows(fixture.database)).toEqual([{ state: "inflight" }]);
      expect(origin(fixture.database)).toMatchObject({ created_at_ms: 1000, control_deadline_ms: 61_000, calls: 8 });
    } finally {
      fixture.close();
    }
  });

  it("calling confirm with a non-matching root throws and leaves state inflight", async () => {
    const fixture = open();
    try {
      const begun = await fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: true });
      await fixture.authorizer.consume(issued(fixture.authorizer), admission(), step(), fixture.grant());
      await expect(fixture.authorizer.confirm(
        issued(fixture.authorizer),
        admission(),
        step().attempt_id,
        hex("4"),
        0,
      )).rejects.toThrow("original_attempt_readback_conflict");
      expect(transitionRows(fixture.database)).toEqual([{ state: "inflight" }]);
      await expect(fixture.authorizer.confirm(
        issued(fixture.authorizer),
        admission(),
        step().attempt_id,
        hex("e"),
        2,
      )).rejects.toThrow("original_attempt_readback_conflict");
      expect(transitionRows(fixture.database)).toEqual([{ state: "inflight" }]);
      expect(scopeRoot(fixture.database)).toEqual({ root_digest: hex("4"), root_generation: 0 });
      expect(origin(fixture.database)).toMatchObject({
        created_at_ms: begun.snapshot.created_at_ms,
        control_deadline_ms: begun.snapshot.control_deadline_ms,
        calls: 8,
      });
      const settled = await fixture.authorizer.confirm(
        issued(fixture.authorizer),
        admission(),
        step().attempt_id,
        step().target_root_digest,
        step().target_root_generation,
      );
      expect(settled.snapshot).toMatchObject({ root_digest: hex("6"), root_generation: 1, calls: 8, control_deadline_ms: 61_000 });
      expect(transitionRows(fixture.database)).toEqual([{ state: "confirmed" }]);
    } finally {
      fixture.close();
    }
  });

  it("observes committed metadata after the deadline without reserving, admitting, or extending it", async () => {
    const fixture = open();
    try {
      const begun = await fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: true });
      await fixture.authorizer.consume(issued(fixture.authorizer), admission(), step(), fixture.grant());
      await expect(fixture.authorizer.observe(issued(fixture.authorizer), admission())).rejects.toThrow("original_attempt_authorizer_observe_not_due");
      expect(origin(fixture.database).control_deadline_ms).toBe(61_000);
      fixture.tick(begun.snapshot.control_deadline_ms + 5000);
      const tracker = trackJournal();
      try {
        const observed = await fixture.authorizer.observe(issued(fixture.authorizer), admission());
        expect(observed.snapshot).toMatchObject({
          created_at_ms: 1000,
          control_deadline_ms: 61_000,
          calls: 8,
        });
        expect(observed.transitions).toEqual([{ attempt_id: step().attempt_id, state: "inflight" }]);
        expect(observed).not.toHaveProperty("grant");
        expect(observed).not.toHaveProperty("permit");
        expect(tracker.counts.admit).toBe(0);
        expect(tracker.counts.reserve).toBe(0);
        await expect(fixture.authorizer.observe(
          issued(fixture.authorizer),
          admission(),
          step(),
          () => true,
        )).rejects.toThrow("original_attempt_authorizer_observe_refused");
        expect(tracker.counts.admit).toBe(0);
        expect(tracker.counts.reserve).toBe(0);
        expect(origin(fixture.database)).toMatchObject({ created_at_ms: 1000, control_deadline_ms: 61_000, calls: 8 });
        expect(transitionRows(fixture.database)).toEqual([{ state: "inflight" }]);
      } finally {
        tracker.restore();
      }
    } finally {
      fixture.close();
    }
  });

  it("keeps the journal budget and clock ceilings", async () => {
    const fixture = open();
    try {
      expect(ORIGINAL_ATTEMPT_LIMITS.ordinaryDispatches).toBe(60);
      expect(ORIGINAL_ATTEMPT_LIMITS.dispatches).toBe(64);
      expect(ORIGINAL_ATTEMPT_LIMITS.controlMilliseconds).toBe(60_000);
      expect(UNINTERRUPTED_BOOTSTRAP_DISPATCHES).toBe(1);
      await fixture.authorizer.beginAuthenticated(issued(fixture.authorizer), admission(), { committed: true });
      fixture.tick(0);
      const early = fixture.grant();
      await expect(fixture.authorizer.consume(issued(fixture.authorizer), admission(), step(), early)).rejects.toThrow("original_attempt_clock_rollback");
      expect(grants(fixture.database)).toBe(1);
      expect(origin(fixture.database).calls).toBe(1);
      fixture.tick(1000);
      await expect(fixture.authorizer.consume(
        issued(fixture.authorizer),
        admission(),
        step("b", { ordinary_dispatches: 60, fence_dispatches: 1 }),
        early,
      )).rejects.toThrow("original_attempt_budget");
      expect(transitionRows(fixture.database)).toEqual([]);
      expect(grants(fixture.database)).toBe(1);
      const filled = await fixture.authorizer.consume(
        issued(fixture.authorizer),
        admission(),
        step("b", { ordinary_dispatches: 59, fence_dispatches: 4 }),
        early,
      );
      expect(filled).toMatchObject({ calls: 64, first_consumption: true, state: "inflight" });
      await fixture.authorizer.confirm(
        issued(fixture.authorizer),
        admission(),
        step("b").attempt_id,
        step().target_root_digest,
        1,
      );
      await expect(fixture.authorizer.consume(
        issued(fixture.authorizer),
        admission(),
        step("c", {
          sequence: 1,
          prior_root_digest: hex("6"),
          prior_root_generation: 1,
          target_root_digest: hex("d"),
          target_root_generation: 2,
          ordinary_dispatches: 1,
          fence_dispatches: 1,
        }),
        () => true,
      )).rejects.toThrow("original_attempt_budget");
      expect(origin(fixture.database)).toMatchObject({ calls: 64, control_deadline_ms: 61_000 });
    } finally {
      fixture.close();
    }
  });

  it("restores the committed journal row after reopen without a new allowance", async () => {
    const directory = mkdtempSync(join(tmpdir(), "attempt-authorizer-"));
    try {
      const filename = join(directory, "attempt.sqlite");
      const first = open(filename);
      const begun = await first.authorizer.beginAuthenticated(issued(first.authorizer), admission(), { committed: true });
      first.close();
      const restarted = open(filename);
      const tracker = trackJournal();
      try {
        const restored = await restarted.authorizer.restore(issued(restarted.authorizer), admission());
        expect(restored.snapshot).toEqual(begun.snapshot);
        expect(tracker.counts.admit).toBe(0);
        expect(events(restarted.database)).toBe(1);
        expect(origin(restarted.database).calls).toBe(1);
      } finally {
        tracker.restore();
        restarted.close();
      }
    } finally {
      rmSync(directory, { recursive: true });
    }
  });
});
