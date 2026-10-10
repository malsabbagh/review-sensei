/**
 * Gated original-attempt authorizer for the uninterrupted-v1 profile.
 *
 * This is delegated execution assurance. A closed server-side proof carries
 * the GitHub-hosted workflow, artifact, dependency lock, declaration,
 * contract, repository, pull request, actor and server challenge. An approved
 * mapping must match that material. The host still checks live source, root,
 * inventory, trusted policy and ordered liabilities before it builds the proof.
 *
 * OriginalAttemptJournal remains the storage primitive. This module does not
 * replace it and does not add a Worker route. Authentication and a committed
 * admit run in one method: hashing finishes first, then challenge use and
 * journal.admit run in the same synchronous continuation. The journal opens
 * its own SQL transaction, so challenge use cannot be enlisted inside admit
 * without changing the journal. A false committed flag burns the proof and
 * does not insert an event.
 */
import {
  OriginalAttemptJournal,
  ORIGINAL_ATTEMPT_LIMITS,
  type AttemptBinding,
  type AttemptSnapshot,
  type AttemptTransition,
} from "./original-attempt-journal";

export const UNINTERRUPTED_PROFILE = "uninterrupted-v1" as const;
/** Server prepaid bootstrap. Callers cannot select or raise it. */
export const UNINTERRUPTED_BOOTSTRAP_DISPATCHES = 1;
export const ORIGINAL_CONTRACTS = Object.freeze([
  "existing-inventory-reassessment",
  "first-full-review-inventory",
] as const);
export const PRODUCER_PROOF_KEYS = Object.freeze([
  "workflow_ref",
  "runner_environment",
  "artifact_sha256",
  "dependency_lock_sha256",
  "declaration_sha256",
  "contract",
  "repository_id",
  "pull_request",
  "actor_id",
  "server_challenge",
  "token_jti",
] as const);
export const ADMISSION_KEYS = Object.freeze([
  "inventory_sha256",
  "inventory_generation",
  "operation_sha256",
  "source_sha256",
  "initial_root_sha256",
  "initial_root_generation",
] as const);
const MAPPING_KEYS = Object.freeze([
  "workflow_ref",
  "artifact_sha256",
  "dependency_lock_sha256",
  "declaration_sha256",
  "contract",
] as const);
const WORKFLOW_REF = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/\.github\/workflows\/[A-Za-z0-9_.-]+\.ya?ml@\S+$/;
const DIGEST = /^[a-f0-9]{64}$/;

export type OriginalContract = (typeof ORIGINAL_CONTRACTS)[number];

export interface ProducerProof {
  workflow_ref: string;
  runner_environment: string;
  artifact_sha256: string;
  dependency_lock_sha256: string;
  declaration_sha256: string;
  contract: OriginalContract;
  repository_id: number;
  pull_request: number;
  actor_id: number;
  server_challenge: string;
  token_jti: string;
}

export interface OriginalAdmission {
  inventory_sha256: string;
  inventory_generation: number;
  operation_sha256: string;
  source_sha256: string;
  initial_root_sha256: string;
  initial_root_generation: number;
}

export interface ApprovedProducerMapping {
  workflow_ref: string;
  artifact_sha256: string;
  dependency_lock_sha256: string;
  declaration_sha256: string;
  contract: OriginalContract;
}

export interface BeginResult {
  profile: typeof UNINTERRUPTED_PROFILE;
  snapshot: AttemptSnapshot;
}

export interface TransitionView {
  attempt_id: string;
  state: string;
}

export interface RetainedAttempt {
  profile: typeof UNINTERRUPTED_PROFILE;
  snapshot: AttemptSnapshot;
  transitions: readonly TransitionView[];
}

export interface ConsumptionResult extends AttemptSnapshot {
  profile: typeof UNINTERRUPTED_PROFILE;
  first_consumption: boolean;
  state: string;
}

function fail(reason: string): never {
  throw new Error(`original_attempt_authorizer_${reason}`);
}

function closedCopy(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail("invalid");
  if (Object.getPrototypeOf(value) !== Object.prototype) fail("invalid");
  if (Object.getOwnPropertySymbols(value).length !== 0) fail("invalid");
  const names = Object.getOwnPropertyNames(value);
  if (names.length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) fail("invalid");
  const entries = keys.map((key) => {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !Object.hasOwn(descriptor, "value") || descriptor.get || descriptor.set) fail("invalid");
    return [key, descriptor.value] as const;
  });
  return Object.freeze(Object.fromEntries(entries));
}

function digest64(value: unknown): string {
  if (typeof value !== "string" || !DIGEST.test(value)) fail("invalid");
  return value;
}

function positive(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) fail("invalid");
  return value;
}

function generation(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || value > 2147483647) fail("invalid");
  return value;
}

function workflowRef(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048 || !WORKFLOW_REF.test(value)) fail("invalid");
  return value;
}

function runnerLabel(value: unknown): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 64 || /\s/.test(value)) fail("invalid");
  return value;
}

function tokenJti(value: unknown): string {
  if (typeof value !== "string" || value.length < 8 || value.length > 256 || /[\s\u0000-\u001f]/.test(value)) fail("invalid");
  return value;
}

function contractName(value: unknown): OriginalContract {
  if (value !== "existing-inventory-reassessment" && value !== "first-full-review-inventory") fail("invalid");
  return value;
}

function parseProof(value: unknown): ProducerProof {
  const data = closedCopy(value, PRODUCER_PROOF_KEYS);
  return Object.freeze({
    workflow_ref: workflowRef(data.workflow_ref),
    runner_environment: runnerLabel(data.runner_environment),
    artifact_sha256: digest64(data.artifact_sha256),
    dependency_lock_sha256: digest64(data.dependency_lock_sha256),
    declaration_sha256: digest64(data.declaration_sha256),
    contract: contractName(data.contract),
    repository_id: positive(data.repository_id),
    pull_request: positive(data.pull_request),
    actor_id: positive(data.actor_id),
    server_challenge: digest64(data.server_challenge),
    token_jti: tokenJti(data.token_jti),
  });
}

function parseAdmission(value: unknown): OriginalAdmission {
  const data = closedCopy(value, ADMISSION_KEYS);
  return Object.freeze({
    inventory_sha256: digest64(data.inventory_sha256),
    inventory_generation: generation(data.inventory_generation),
    operation_sha256: digest64(data.operation_sha256),
    source_sha256: digest64(data.source_sha256),
    initial_root_sha256: digest64(data.initial_root_sha256),
    initial_root_generation: generation(data.initial_root_generation),
  });
}

function parseMapping(value: unknown): ApprovedProducerMapping {
  const data = closedCopy(value, MAPPING_KEYS);
  return Object.freeze({
    workflow_ref: workflowRef(data.workflow_ref),
    artifact_sha256: digest64(data.artifact_sha256),
    dependency_lock_sha256: digest64(data.dependency_lock_sha256),
    declaration_sha256: digest64(data.declaration_sha256),
    contract: contractName(data.contract),
  });
}

function parseOptions(value: unknown): { committed: boolean } {
  const data = closedCopy(value, ["committed"]);
  if (typeof data.committed !== "boolean") fail("invalid");
  return Object.freeze({ committed: data.committed });
}

function randomChallenge(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function sha256(parts: readonly (string | number)[]): Promise<string> {
  const encoded = new TextEncoder().encode(parts.map((part) => String(part)).join("\n"));
  const digest = await crypto.subtle.digest("SHA-256", encoded);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function freezeSnapshot(snapshot: AttemptSnapshot): AttemptSnapshot {
  return Object.freeze({ ...snapshot });
}

/**
 * Delegated execution assurance over the dormant journal.
 * Capacity, clock, debit and confirm rules stay on OriginalAttemptJournal.
 */
export class OriginalAttemptAuthorizer {
  private readonly sql: SqlStorage;
  private readonly journal: OriginalAttemptJournal;
  private readonly byDeclaration: ReadonlyMap<string, ApprovedProducerMapping>;

  constructor(
    private readonly storage: Pick<DurableObjectStorage, "sql" | "transactionSync">,
    approved: readonly unknown[],
    private readonly now: () => number = Date.now,
  ) {
    this.byDeclaration = registerMappings(approved);
    this.sql = storage.sql;
    this.sql.exec(
      "CREATE TABLE IF NOT EXISTS original_attempt_authorizer_proofs (" +
      "kind TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY(kind, token));",
    );
    this.journal = new OriginalAttemptJournal(storage, now);
  }

  /** Server-issued challenge. A caller nonce is not accepted. */
  issueChallenge(...extra: unknown[]): string {
    if (extra.length > 0) fail("challenge");
    const challenge = randomChallenge();
    this.storage.transactionSync(() => {
      this.sql.exec("INSERT INTO original_attempt_authorizer_proofs VALUES (?, ?)", "challenge", challenge);
    });
    return challenge;
  }

  /**
   * Authenticate delegated execution assurance and admit only when the
   * authoritative SQL admission committed. committed false is interrupted
   * pre-admission: the proof is spent and no event row is created.
   */
  async beginAuthenticated(rawProof: unknown, rawAdmission: unknown, rawOptions: unknown): Promise<BeginResult> {
    const options = parseOptions(rawOptions);
    const binding = await this.gate(rawProof, rawAdmission);
    if (options.committed !== true) fail("interrupted");
    const snapshot = this.journal.admit(binding, this.now(), UNINTERRUPTED_BOOTSTRAP_DISPATCHES);
    return Object.freeze({ profile: UNINTERRUPTED_PROFILE, snapshot: freezeSnapshot(snapshot) });
  }

  /** Interrupted pre-admission. Authentication runs; admit does not. */
  async refuseInterrupted(rawProof: unknown, rawAdmission: unknown): Promise<never> {
    await this.beginAuthenticated(rawProof, rawAdmission, { committed: false });
    fail("interrupted");
  }

  /**
   * Authenticated read of the retained snapshot and transition states.
   * Does not admit, does not increase calls, and returns no grant or permit.
   */
  async restore(rawProof: unknown, rawAdmission: unknown): Promise<RetainedAttempt> {
    const binding = await this.gate(rawProof, rawAdmission);
    this.requireEvent(binding);
    const snapshot = this.journal.inspect(binding);
    return this.retained(snapshot, binding);
  }

  /**
   * Debit through journal.reserve. consumeGrant must be synchronous and use
   * this same storage. Replay reports the retained state and returns no
   * second permit.
   */
  async consume(rawProof: unknown, rawAdmission: unknown, step: unknown, consumeGrant: unknown): Promise<ConsumptionResult> {
    if (typeof consumeGrant !== "function") fail("invalid");
    const binding = await this.gate(rawProof, rawAdmission);
    this.requireEvent(binding);
    const reserved = this.journal.reserve(binding, step as AttemptTransition, consumeGrant as () => boolean);
    return Object.freeze({ profile: UNINTERRUPTED_PROFILE, ...reserved });
  }

  /** Confirm only an exact owned target root. A mismatch leaves inflight unchanged. */
  async confirm(
    rawProof: unknown,
    rawAdmission: unknown,
    attemptId: unknown,
    rootDigest: unknown,
    rootGeneration: unknown,
  ): Promise<BeginResult> {
    const binding = await this.gate(rawProof, rawAdmission);
    this.requireEvent(binding);
    const snapshot = this.journal.confirm(binding, attemptId as string, rootDigest as string, rootGeneration as number);
    return Object.freeze({ profile: UNINTERRUPTED_PROFILE, snapshot: freezeSnapshot(snapshot) });
  }

  /**
   * Post-deadline observation. Reads committed metadata only.
   * Refuses reserve, admit and dispatch, and leaves the original deadline in place.
   */
  async observe(rawProof: unknown, rawAdmission: unknown, ...extra: unknown[]): Promise<RetainedAttempt> {
    if (extra.length > 0) fail("observe_refused");
    const binding = await this.gate(rawProof, rawAdmission);
    this.requireEvent(binding);
    const snapshot = this.journal.inspect(binding);
    if (this.now() < snapshot.control_deadline_ms) fail("observe_not_due");
    return this.retained(snapshot, binding);
  }

  private retained(snapshot: AttemptSnapshot, binding: AttemptBinding): RetainedAttempt {
    return Object.freeze({
      profile: UNINTERRUPTED_PROFILE,
      snapshot: freezeSnapshot(snapshot),
      transitions: Object.freeze(this.transitionStates(binding)),
    });
  }

  private async gate(rawProof: unknown, rawAdmission: unknown): Promise<AttemptBinding> {
    const proof = parseProof(rawProof);
    const admission = parseAdmission(rawAdmission);
    this.rejectRunner(proof);
    this.rejectProducer(proof);
    const binding = await this.derive(proof, admission);
    this.burn(proof.server_challenge, proof.token_jti);
    return binding;
  }

  private rejectRunner(proof: ProducerProof): void {
    if (proof.runner_environment.toLowerCase() === "self-hosted") fail("self_hosted");
    if (proof.runner_environment !== "github-hosted") fail("runner_rejected");
  }

  private rejectProducer(proof: ProducerProof): void {
    const mapped = this.byDeclaration.get(proof.declaration_sha256);
    if (!mapped) fail("unapproved");
    if (mapped.contract !== proof.contract) fail("declaration_replay");
    if (mapped.workflow_ref !== proof.workflow_ref || mapped.dependency_lock_sha256 !== proof.dependency_lock_sha256) {
      fail("unapproved");
    }
    if (mapped.artifact_sha256 !== proof.artifact_sha256) fail("artifact_mismatch");
  }

  /**
   * Stable event is profile, repository, pull request and contract.
   * Token jti, server challenge, inventory and operation stay out of that key.
   * Inventory and operation remain inside the journal binding.
   */
  private async derive(proof: ProducerProof, admission: OriginalAdmission): Promise<AttemptBinding> {
    const scope = await sha256([UNINTERRUPTED_PROFILE, "scope", proof.repository_id, proof.pull_request]);
    const event = await sha256([UNINTERRUPTED_PROFILE, "event", proof.repository_id, proof.pull_request, proof.contract]);
    const authority = await sha256([
      UNINTERRUPTED_PROFILE, "authority", proof.repository_id, proof.actor_id, proof.workflow_ref,
      proof.contract, proof.artifact_sha256, proof.dependency_lock_sha256, proof.declaration_sha256,
    ]);
    const owner = await sha256([UNINTERRUPTED_PROFILE, "owner", proof.repository_id, proof.actor_id]);
    const reservation = await sha256([
      UNINTERRUPTED_PROFILE, "bootstrap-reservation", proof.contract, proof.workflow_ref,
      proof.artifact_sha256, proof.dependency_lock_sha256, proof.declaration_sha256,
    ]);
    const execution = (await sha256([
      UNINTERRUPTED_PROFILE, "execution", proof.artifact_sha256, proof.workflow_ref, proof.contract,
    ])).slice(0, 32);
    return {
      scope_digest: scope,
      event_digest: event,
      operation_id: admission.operation_sha256,
      source_digest: admission.source_sha256,
      authority_digest: authority,
      execution_identity: execution,
      inventory_digest: admission.inventory_sha256,
      inventory_generation: admission.inventory_generation,
      owner_digest: owner,
      reservation_digest: reservation,
      initial_root_digest: admission.initial_root_sha256,
      initial_root_generation: admission.initial_root_generation,
    };
  }

  private burn(challenge: string, jti: string): void {
    let replay = false;
    this.storage.transactionSync(() => {
      const found = this.first<{ token: string }>(
        "SELECT token FROM original_attempt_authorizer_proofs WHERE kind=? AND token=?",
        "challenge",
        challenge,
      );
      if (!found) fail("challenge");
      this.sql.exec("DELETE FROM original_attempt_authorizer_proofs WHERE kind=? AND token=?", "challenge", challenge);
      const existing = this.first<{ token: string }>(
        "SELECT token FROM original_attempt_authorizer_proofs WHERE kind=? AND token=?",
        "jti",
        jti,
      );
      if (existing) {
        replay = true;
        return;
      }
      this.sql.exec("INSERT INTO original_attempt_authorizer_proofs VALUES (?, ?)", "jti", jti);
    });
    if (replay) fail("token_replay");
  }

  private requireEvent(binding: AttemptBinding): void {
    const row = this.first<{ binding: string }>(
      "SELECT binding FROM original_attempt_events WHERE scope_digest=? AND event_digest=?",
      binding.scope_digest,
      binding.event_digest,
    );
    if (!row) fail("missing");
  }

  private transitionStates(binding: AttemptBinding): TransitionView[] {
    const rows = Array.from(this.sql.exec<{ attempt_id: string; state: string }>(
      "SELECT attempt_id, state FROM original_attempt_transitions WHERE scope_digest=? AND event_digest=? ORDER BY attempt_id",
      binding.scope_digest,
      binding.event_digest,
    ));
    return rows.map((row) => Object.freeze({ attempt_id: String(row.attempt_id), state: String(row.state) }));
  }

  private first<T extends Record<string, SqlStorageValue>>(query: string, ...args: unknown[]): T | undefined {
    return Array.from(this.sql.exec<T>(query, ...args))[0];
  }
}

function registerMappings(approved: readonly unknown[]): ReadonlyMap<string, ApprovedProducerMapping> {
  if (!Array.isArray(approved) || approved.length < 1) fail("registry");
  const byDeclaration = new Map<string, ApprovedProducerMapping>();
  for (const value of approved) {
    const mapping = parseMapping(value);
    if (byDeclaration.has(mapping.declaration_sha256)) fail("registry");
    byDeclaration.set(mapping.declaration_sha256, mapping);
  }
  if (UNINTERRUPTED_BOOTSTRAP_DISPATCHES < 1 || UNINTERRUPTED_BOOTSTRAP_DISPATCHES > ORIGINAL_ATTEMPT_LIMITS.ordinaryDispatches) {
    fail("registry");
  }
  return byDeclaration;
}
