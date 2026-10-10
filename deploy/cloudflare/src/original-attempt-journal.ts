/**
 * Dormant metadata-only original-attempt storage primitive.
 *
 * No Worker route or broker grant calls this module. A future trusted
 * authorizer must prove the original source, admission, root and bootstrap
 * origin independently before these methods become a restore witness.
 * The storage result alone is neither a grant nor GitHub root authority.
 */
export interface AttemptBinding {
  scope_digest: string;
  event_digest: string;
  operation_id: string;
  source_digest: string;
  authority_digest: string;
  execution_identity: string;
  inventory_digest: string;
  inventory_generation: number;
  owner_digest: string;
  reservation_digest: string;
  initial_root_digest: string;
  initial_root_generation: number;
}

export interface AttemptTransition {
  attempt_id: string;
  sequence: number;
  prior_root_digest: string;
  prior_root_generation: number;
  target_root_digest: string;
  target_root_generation: number;
  request_digest: string;
  dispatch_digest: string;
  sealed_plan_digest: string;
  ordinary_dispatches: number;
  fence_dispatches: number;
}

export interface AttemptSnapshot {
  created_at_ms: number;
  control_deadline_ms: number;
  calls: number;
  sequence: number;
  root_digest: string;
  root_generation: number;
}

interface EventRow {
  binding: string;
  created_at_ms: number;
  control_deadline_ms: number;
  calls: number;
  sequence: number;
}
interface ScopeRow { root_digest: string; root_generation: number }
interface TransitionRow { transition: string; state: string }
const BINDING_KEYS = ["scope_digest", "event_digest", "operation_id", "source_digest", "authority_digest", "execution_identity", "inventory_digest", "inventory_generation", "owner_digest", "reservation_digest", "initial_root_digest", "initial_root_generation"];
const TRANSITION_KEYS = ["attempt_id", "sequence", "prior_root_digest", "prior_root_generation", "target_root_digest", "target_root_generation", "request_digest", "dispatch_digest", "sealed_plan_digest", "ordinary_dispatches", "fence_dispatches"];
export const ORIGINAL_ATTEMPT_LIMITS = Object.freeze({
  scopes: 1024, sourcesPerScope: 256, attemptsPerSource: 64,
  totalAttempts: 16384, ordinaryDispatches: 60, dispatches: 64,
  controlMilliseconds: 60_000,
});

function fail(reason: string): never { throw new Error("original_attempt_" + reason); }
function exact(value: unknown, keys: string[]): asserts value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail("invalid");
  const actual = Object.keys(value);
  if (actual.length !== keys.length || keys.some(key => !Object.hasOwn(value, key))) fail("invalid");
}
function integer(value: unknown, max = Number.MAX_SAFE_INTEGER): asserts value is number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || value > max) fail("invalid");
}
function hash(value: unknown, width = 64): void {
  if (typeof value !== "string" || value.length !== width || !/^[a-f0-9]+$/.test(value)) fail("invalid");
}
function binding(value: AttemptBinding): string {
  exact(value, BINDING_KEYS);
  for (const key of BINDING_KEYS.filter(key => !key.endsWith("_generation"))) {
    hash(value[key], key === "execution_identity" ? 32 : 64);
  }
  integer(value.inventory_generation, 2147483647);
  integer(value.initial_root_generation, 2147483647);
  return JSON.stringify(Object.fromEntries(BINDING_KEYS.map(key => [key, value[key]])));
}
function transition(value: AttemptTransition): string {
  exact(value, TRANSITION_KEYS);
  for (const key of ["attempt_id", "prior_root_digest", "target_root_digest", "request_digest", "dispatch_digest", "sealed_plan_digest"]) hash(value[key]);
  integer(value.sequence, ORIGINAL_ATTEMPT_LIMITS.attemptsPerSource - 1);
  integer(value.prior_root_generation, 2147483646);
  integer(value.target_root_generation, 2147483647);
  if (value.target_root_generation !== value.prior_root_generation + 1) fail("invalid");
  integer(value.ordinary_dispatches, ORIGINAL_ATTEMPT_LIMITS.ordinaryDispatches);
  integer(value.fence_dispatches, 4);
  if (value.ordinary_dispatches + value.fence_dispatches === 0) fail("invalid");
  return JSON.stringify(Object.fromEntries(TRANSITION_KEYS.map(key => [key, value[key]])));
}

/** Uses the existing SQLite storage API; no new namespace or public protocol. */
export class OriginalAttemptJournal {
  private readonly sql: SqlStorage;
  constructor(private readonly storage: Pick<DurableObjectStorage, "sql" | "transactionSync">, private readonly now: () => number = Date.now) {
    this.sql = storage.sql;
    this.sql.exec(
      "CREATE TABLE IF NOT EXISTS original_attempt_scopes (" +
      "scope_digest TEXT PRIMARY KEY, root_digest TEXT NOT NULL, root_generation INTEGER NOT NULL);" +
      "CREATE TABLE IF NOT EXISTS original_attempt_events (" +
      "scope_digest TEXT NOT NULL, event_digest TEXT NOT NULL, binding TEXT NOT NULL, " +
      "created_at_ms INTEGER NOT NULL, control_deadline_ms INTEGER NOT NULL, calls INTEGER NOT NULL, sequence INTEGER NOT NULL, " +
      "PRIMARY KEY(scope_digest,event_digest));" +
      "CREATE TABLE IF NOT EXISTS original_attempt_transitions (" +
      "scope_digest TEXT NOT NULL, event_digest TEXT NOT NULL, attempt_id TEXT NOT NULL, " +
      "transition TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(scope_digest,event_digest,attempt_id));" +
      "CREATE INDEX IF NOT EXISTS original_attempt_inflight ON original_attempt_transitions(scope_digest,state);"
    );
  }
  private first<T extends Record<string, SqlStorageValue>>(query: string, ...args: unknown[]): T | undefined {
    return Array.from(this.sql.exec<T>(query, ...args))[0];
  }
  private count(query: string, ...args: unknown[]): number {
    return this.first<{ count: number }>(query, ...args)?.count ?? 0;
  }
  private clock(): number {
    const value = this.now();
    integer(value, Number.MAX_SAFE_INTEGER - ORIGINAL_ATTEMPT_LIMITS.controlMilliseconds);
    return value;
  }
  private event(value: AttemptBinding): EventRow {
    const row = this.first<EventRow & Record<string, SqlStorageValue>>("SELECT * FROM original_attempt_events WHERE scope_digest=? AND event_digest=?", value.scope_digest, value.event_digest);
    if (!row || row.binding !== binding(value)) fail("binding_conflict");
    return row;
  }
  private scope(value: AttemptBinding): ScopeRow {
    const row = this.first<ScopeRow & Record<string, SqlStorageValue>>("SELECT * FROM original_attempt_scopes WHERE scope_digest=?", value.scope_digest);
    if (!row) fail("missing");
    return row;
  }
  private snapshot(value: AttemptBinding): AttemptSnapshot {
    const event = this.event(value), scope = this.scope(value);
    return { created_at_ms: event.created_at_ms, control_deadline_ms: event.control_deadline_ms, calls: event.calls, sequence: event.sequence, root_digest: scope.root_digest, root_generation: scope.root_generation };
  }

  /**
   * The start tick and bootstrap liability must come from trusted server
   * accounting, never request JSON. First use creates a sticky event guard.
   * Expired guards remain retained; repeated admission cannot renew time.
   */
  admit(value: AttemptBinding, serverStartedAtMs: number, bootstrapDispatches: number): AttemptSnapshot {
    const encoded = binding(value), now = this.clock();
    integer(serverStartedAtMs, now);
    integer(bootstrapDispatches, ORIGINAL_ATTEMPT_LIMITS.ordinaryDispatches);
    if (bootstrapDispatches === 0) fail("invalid");
    return this.storage.transactionSync(() => {
      const existing = this.first<EventRow & Record<string, SqlStorageValue>>("SELECT * FROM original_attempt_events WHERE scope_digest=? AND event_digest=?", value.scope_digest, value.event_digest);
      if (existing) {
        if (existing.binding !== encoded) fail("binding_conflict");
        return this.snapshot(value);
      }
      const scope = this.first<ScopeRow & Record<string, SqlStorageValue>>("SELECT * FROM original_attempt_scopes WHERE scope_digest=?", value.scope_digest);
      if (scope && (scope.root_digest !== value.initial_root_digest || scope.root_generation !== value.initial_root_generation)) fail("root_conflict");
      if (!scope) {
        if (this.count("SELECT COUNT(*) AS count FROM original_attempt_scopes") >= ORIGINAL_ATTEMPT_LIMITS.scopes) fail("capacity");
        this.sql.exec("INSERT INTO original_attempt_scopes VALUES (?,?,?)", value.scope_digest, value.initial_root_digest, value.initial_root_generation);
      }
      if (this.count("SELECT COUNT(*) AS count FROM original_attempt_events WHERE scope_digest=?", value.scope_digest) >= ORIGINAL_ATTEMPT_LIMITS.sourcesPerScope) fail("capacity");
      this.sql.exec("INSERT INTO original_attempt_events VALUES (?,?,?,?,?,?,?)", value.scope_digest, value.event_digest, encoded, serverStartedAtMs, serverStartedAtMs + ORIGINAL_ATTEMPT_LIMITS.controlMilliseconds, bootstrapDispatches, 0);
      return this.snapshot(value);
    });
  }

  /** Read-only metadata; this cannot acquire mutation or restore authority. */
  inspect(value: AttemptBinding): AttemptSnapshot {
    binding(value);
    return this.storage.transactionSync(() => this.snapshot(value));
  }

  /**
   * Debit and consume the independently authenticated grant in ONE SQL
   * transaction. consumeGrant must be synchronous and use this storage.
   * A repeated transition is inspectable but never consumes/returns it again.
   */
  reserve(value: AttemptBinding, step: AttemptTransition, consumeGrant: () => boolean): AttemptSnapshot & { first_consumption: boolean; state: string } {
    binding(value);
    const encoded = transition(step), now = this.clock();
    return this.storage.transactionSync(() => {
      const event = this.event(value), scope = this.scope(value);
      const previous = this.first<TransitionRow & Record<string, SqlStorageValue>>("SELECT transition,state FROM original_attempt_transitions WHERE scope_digest=? AND event_digest=? AND attempt_id=?", value.scope_digest, value.event_digest, step.attempt_id);
      if (previous) {
        if (previous.transition !== encoded) fail("transition_conflict");
        return { ...this.snapshot(value), first_consumption: false, state: previous.state };
      }
      if (now >= event.control_deadline_ms) fail("expired");
      if (event.sequence !== step.sequence) fail("sequence_conflict");
      if (scope.root_digest !== step.prior_root_digest || scope.root_generation !== step.prior_root_generation) fail("root_conflict");
      if (this.count("SELECT COUNT(*) AS count FROM original_attempt_transitions WHERE scope_digest=? AND state='inflight'", value.scope_digest) > 0) fail("inflight");
      if (this.count("SELECT COUNT(*) AS count FROM original_attempt_transitions") >= ORIGINAL_ATTEMPT_LIMITS.totalAttempts) fail("capacity");
      const ordinary = event.calls + step.ordinary_dispatches, calls = ordinary + step.fence_dispatches;
      if (ordinary > ORIGINAL_ATTEMPT_LIMITS.ordinaryDispatches || calls > ORIGINAL_ATTEMPT_LIMITS.dispatches) fail("budget");
      if (consumeGrant() !== true) fail("grant_invalid");
      this.sql.exec("INSERT INTO original_attempt_transitions VALUES (?,?,?,?,?)", value.scope_digest, value.event_digest, step.attempt_id, encoded, "inflight");
      this.sql.exec("UPDATE original_attempt_events SET calls=?,sequence=? WHERE scope_digest=? AND event_digest=?", calls, event.sequence + 1, value.scope_digest, value.event_digest);
      return { ...this.snapshot(value), first_consumption: true, state: "inflight" };
    });
  }

  /**
   * Only an independent exact owned readback can supply these root values.
   * Never refund prepaid work or clear unknown attempts on expiry/old-root
   * observation. Remote writes and SQL cannot be one atomic transaction.
   */
  confirm(value: AttemptBinding, attemptId: string, rootDigest: string, rootGeneration: number): AttemptSnapshot {
    binding(value); hash(attemptId); hash(rootDigest); integer(rootGeneration, 2147483647);
    return this.storage.transactionSync(() => {
      this.event(value);
      const row = this.first<TransitionRow & Record<string, SqlStorageValue>>("SELECT transition,state FROM original_attempt_transitions WHERE scope_digest=? AND event_digest=? AND attempt_id=?", value.scope_digest, value.event_digest, attemptId);
      if (!row) fail("missing");
      const step = JSON.parse(row.transition) as AttemptTransition;
      if (rootDigest !== step.target_root_digest || rootGeneration !== step.target_root_generation) fail("readback_conflict");
      if (row.state === "confirmed") return this.snapshot(value);
      const scope = this.scope(value);
      if (scope.root_digest !== step.prior_root_digest || scope.root_generation !== step.prior_root_generation) fail("root_conflict");
      this.sql.exec("UPDATE original_attempt_scopes SET root_digest=?,root_generation=? WHERE scope_digest=?", rootDigest, rootGeneration, value.scope_digest);
      this.sql.exec("UPDATE original_attempt_transitions SET state='confirmed' WHERE scope_digest=? AND event_digest=? AND attempt_id=?", value.scope_digest, value.event_digest, attemptId);
      return this.snapshot(value);
    });
  }
}
