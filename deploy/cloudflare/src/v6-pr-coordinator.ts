import { DurableObject } from "cloudflare:workers";
import { brokerDiagnostic } from "./broker-diagnostics";
import type { BrokerDispatchControl } from "./broker-dispatch-control";
import type { WorkerEnv } from "./env";
import {
  GitHubApi,
  type LiveCheckRun,
  type LiveIssueComment,
  type LiveIssueCommentPage,
  type LivePullRequestReview,
  type LivePullRequestReviewComment,
  type PullRequestBinding,
  type PublicWorkflowRuntimeShas,
  type RevocationResult,
  type WorkflowRunAttemptInfo,
} from "./github-api";
import { verifyOidcAssertion, type OidcClaims, type OidcValidationOptions } from "./oidc";
import { PUBLIC_REPOSITORY, validatePublicWorkflowTag } from "./setup-content";
import {
  V6EscrowCrypto,
  canonicalJsonString,
  isEncryptedEscrow,
  parseRepositoryCsv,
  redactSensitiveText,
  sha256Hex,
  type EncryptedEscrow,
} from "./v6-escrow-crypto";

export const V6_CLOCK_MARGIN_MS_DEFAULT = 300_000;
export const V6_PR_LEDGER_MAX_BYTES = 65_536;
export const V6_RESERVED_STALE_MS = 30_000;
export const V6_MINTING_STALE_MS = 30_000;
export const V6_SLOT_KEY_PATTERN =
  /^v6-pr:[a-z0-9.-]+:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*$/;
const SHA1_HEX40_PATTERN = /^[a-f0-9]{40}$/;
const SHA256_HEX64_PATTERN = /^[a-f0-9]{64}$/;
const POSITIVE_DECIMAL_PATTERN = /^[1-9][0-9]*$/;
const V6_WORKFLOW_TAG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const DEFAULT_REVOCATION_MAX_ATTEMPTS = 3;
const MAX_RETRY_BACKOFF_MS = 2_000;

export type CoordinatorState =
  | "IDLE"
  | "RESERVED"
  | "MINTING"
  | "ACTIVE"
  | "DRAINING"
  | "RECONCILING"
  | "HOLD";

export type V6DiagnosticCode =
  | "configuration_unavailable"
  | "broker_invalid_request"
  | "broker_capability_invalid"
  | "v6_lease_reserved"
  | "v6_lease_active_other_owner"
  | "v6_lease_draining"
  | "v6_lease_reconciling"
  | "v6_lease_hold"
  | "v6_lease_head_changed"
  | "v6_lease_predecessor_not_terminal"
  | "v6_lease_revocation_uncertain"
  | "v6_lease_escrow_lost"
  | "v6_lease_clock_untrusted"
  | "v6_lease_slot_continuity_unproved"
  | "v6_lease_unauthorized_tuple"
  | "v6_lease_rollback_blocked"
  | "v6_lease_mint_uncertain"
  | "v6_lease_reconciliation_evidence_invalid";

const VALID_COORDINATOR_STATES = new Set<CoordinatorState>([
  "IDLE",
  "RESERVED",
  "MINTING",
  "ACTIVE",
  "DRAINING",
  "RECONCILING",
  "HOLD",
]);

const VALID_V6_DIAGNOSTIC_CODES = new Set<V6DiagnosticCode>([
  "configuration_unavailable",
  "broker_invalid_request",
  "broker_capability_invalid",
  "v6_lease_reserved",
  "v6_lease_active_other_owner",
  "v6_lease_draining",
  "v6_lease_reconciling",
  "v6_lease_hold",
  "v6_lease_head_changed",
  "v6_lease_predecessor_not_terminal",
  "v6_lease_revocation_uncertain",
  "v6_lease_escrow_lost",
  "v6_lease_clock_untrusted",
  "v6_lease_slot_continuity_unproved",
  "v6_lease_unauthorized_tuple",
  "v6_lease_rollback_blocked",
  "v6_lease_mint_uncertain",
  "v6_lease_reconciliation_evidence_invalid",
]);

export interface SlotRecord {
  slot_key: string;
  state: CoordinatorState;
  generation: number;
  lease_epoch: number;
  reservation_id: string | null;
  owner_run_id: string | null;
  owner_run_attempt: number | null;
  owner_head_sha: string | null;
  owner_workflow_sha: string | null;
  owner_job: string | null;
  owner_comment_id: number | null;
  owner_record_sha256: string | null;
  acquired_at_ms: number | null;
  token_expires_at_ms: number | null;
  token_cleared: boolean;
  clock_margin_ms: number;
  encrypted_escrow: EncryptedEscrow | null;
  last_reason_code: V6DiagnosticCode | null;
  updated_at_ms: number;
}

export interface V6CoordinatorStore {
  getSlot(slotKey: string): Promise<SlotRecord | null>;
  putSlot(record: SlotRecord): Promise<void>;
}

export interface V6Clock {
  nowMs(): number;
  isTrusted(): boolean;
}

export interface V6AuditEvent {
  route: string;
  slot_key: string | null;
  outcome: "success" | "refused";
  state?: CoordinatorState;
  generation?: number;
  lease_epoch?: number;
  diagnostic?: string;
  timestamp_ms: number;
}

export interface V6AuditSink {
  emit(event: V6AuditEvent): void;
}

export interface V6ProviderAdapter {
  verifyOidc(token: string, options?: OidcValidationOptions): Promise<OidcClaims>;
  workflowRuntimeShas(refOrTag: string): Promise<PublicWorkflowRuntimeShas>;
  appBotLogin(): Promise<string>;
  installationFor(repository: string): Promise<number | null>;
  readOnlyToken(installationId: number, repository: string, scope?: "status" | "actions"): Promise<string>;
  repositoryInfo(repository: string, token: string): Promise<{ id: number; fork: boolean } | null>;
  pullRequestBinding(
    repository: string,
    pullRequest: number,
    token: string,
  ): Promise<PullRequestBinding | null>;
  workflowRunAttempt(
    repository: string,
    runId: string,
    attempt: string,
    token: string,
  ): Promise<WorkflowRunAttemptInfo | null>;
  issueComment(
    repository: string,
    pullRequest: number,
    commentId: number,
    token: string,
  ): Promise<LiveIssueComment | null>;
  issueCommentsSince(
    repository: string,
    pullRequest: number,
    sinceIso: string,
    token: string,
  ): Promise<LiveIssueCommentPage>;
  pullRequestReview(
    repository: string,
    pullRequest: number,
    reviewId: number,
    token: string,
  ): Promise<LivePullRequestReview | null>;
  pullRequestReviewComment(
    repository: string,
    pullRequest: number,
    commentId: number,
    token: string,
  ): Promise<LivePullRequestReviewComment | null>;
  checkRun(
    repository: string,
    checkRunId: number,
    token: string,
  ): Promise<LiveCheckRun | null>;
  mintInstallationToken(
    installationId: number,
    repository: string,
    permissions: Record<string, "read" | "write">,
  ): Promise<{
    token: string;
    permissions: Record<string, string>;
    expiresAt: string;
    expiresAtMs: number;
  }>;
  revokeInstallationToken(token: string): Promise<RevocationResult>;
}

export interface V6CoordinatorEngineOptions {
  store: V6CoordinatorStore;
  provider: V6ProviderAdapter;
  clock?: V6Clock;
  audit?: V6AuditSink;
  sleep?: (ms: number) => Promise<void>;
  maxRevocationAttempts?: number;
  generateReservationId?: () => string;
}

export interface V6RouteResult {
  status: number;
  body: Record<string, unknown>;
}

const V6_CAPABILITY_PERMISSIONS: Record<string, Record<string, "read" | "write">> = {
  review_publish: { contents: "read", pull_requests: "write" },
  thread_resolve: { contents: "write", pull_requests: "write" },
  check_publish: { checks: "write" },
  review_session: { contents: "read", pull_requests: "write" },
  review_status: { contents: "read", pull_requests: "read" },
  review_actions: { actions: "read" },
  inline_reply: { contents: "read", pull_requests: "write" },
  issue_reply: { contents: "read", pull_requests: "write" },
  learning_write: { contents: "write", pull_requests: "write" },
};

const ALLOWED_PERMISSION_NAMES = new Set([
  "actions",
  "checks",
  "contents",
  "issues",
  "pull_requests",
]);

const ALLOWED_EVENTS = new Set([
  "pull_request",
  "workflow_dispatch",
  "issue_comment",
  "pull_request_review_comment",
]);

const ALLOWED_RECONCILE_OUTCOMES = new Set([
  "clean_receipts",
  "adopted_missing_receipt",
  "proved_not_submitted",
  "ambiguous_hold",
  "incomplete",
]);

const TERMINAL_RECONCILE_OUTCOMES = new Set([
  "clean_receipts",
  "adopted_missing_receipt",
  "proved_not_submitted",
]);
const ALLOWED_INTENT_RESOLUTIONS = new Set([
  "receipt_present",
  "adopted_effect",
  "proved_not_submitted",
  "ambiguous_hold",
]);
const ALLOWED_NATIVE_EFFECT_KINDS = new Set([
  "pull_request_review",
  "pull_request_review_comment",
  "issue_comment",
  "check_run",
]);
const ALLOWED_HOLD_RESOLUTIONS = new Set(["reconciled_clean", "enter_reconcile"]);

const SYSTEM_CLOCK: V6Clock = {
  nowMs: () => Date.now(),
  isTrusted: () => Number.isFinite(Date.now()) && Date.now() > 1_700_000_000_000,
};

const DEFAULT_AUDIT_SINK: V6AuditSink = {
  emit(event) {
    const line = redactSensitiveText(JSON.stringify(event));
    if (event.outcome === "refused") {
      console.error("v6_coordinator_event", line);
    } else {
      console.info("v6_coordinator_event", line);
    }
  },
};

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPositiveSafeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function isNonNegativeSafeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function parsePositiveTimestampMs(value: unknown): number | null {
  const parsed = Date.parse(String(value ?? ""));
  return Number.isFinite(parsed) && Number.isSafeInteger(parsed) && parsed > 0
    ? parsed
    : null;
}

export function configuredAppId(env: WorkerEnv): number {
  const raw = env.GITHUB_APP_ID;
  if (typeof raw !== "string" || !/^[1-9][0-9]{0,15}$/.test(raw)) {
    throw new Error("configuration_unavailable");
  }
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error("configuration_unavailable");
  }
  return parsed;
}

export function configuredClockMarginMs(env: WorkerEnv): number {
  const raw = env.V6_CLOCK_MARGIN_MS;
  if (raw === undefined || !raw.trim()) {
    return V6_CLOCK_MARGIN_MS_DEFAULT;
  }
  if (!/^[0-9]{1,9}$/.test(raw.trim())) {
    throw new Error("configuration_unavailable");
  }
  const parsed = Number(raw.trim());
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error("configuration_unavailable");
  }
  return parsed;
}

/**
 * Canonicalize the GitHub API host into a lowercase ASCII hostname with no
 * port or path (`github.com` for `api.github.com`).
 */
export function canonicalSlotHost(githubApiUrl?: string): string {
  const raw = (githubApiUrl ?? "https://api.github.com").trim();
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("configuration_unavailable");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("configuration_unavailable");
  }
  let hostname = url.hostname.toLowerCase();
  if (hostname === "api.github.com") {
    hostname = "github.com";
  }
  if (!/^[a-z0-9.-]+$/.test(hostname) || hostname.startsWith(".") || hostname.endsWith(".")) {
    throw new Error("configuration_unavailable");
  }
  return hostname;
}

export function buildCanonicalSlotKey(parts: {
  host: string;
  appId: number;
  installationId: number;
  baseRepoId: number;
  pullRequest: number;
}): string {
  if (
    !/^[a-z0-9.-]+$/.test(parts.host) ||
    !isPositiveSafeInt(parts.appId) ||
    !isPositiveSafeInt(parts.installationId) ||
    !isPositiveSafeInt(parts.baseRepoId) ||
    !isPositiveSafeInt(parts.pullRequest)
  ) {
    throw new Error("v6_lease_unauthorized_tuple");
  }
  const key = `v6-pr:${parts.host}:${parts.appId}:${parts.installationId}:${parts.baseRepoId}:${parts.pullRequest}`;
  if (!V6_SLOT_KEY_PATTERN.test(key)) {
    throw new Error("v6_lease_unauthorized_tuple");
  }
  return key;
}

export function parseCanonicalSlotKey(slotKey: unknown): {
  host: string;
  appId: number;
  installationId: number;
  baseRepoId: number;
  pullRequest: number;
} | null {
  if (typeof slotKey !== "string" || !V6_SLOT_KEY_PATTERN.test(slotKey)) {
    return null;
  }
  const segments = slotKey.split(":");
  if (segments.length !== 6) {
    return null;
  }
  const appId = Number(segments[2]);
  const installationId = Number(segments[3]);
  const baseRepoId = Number(segments[4]);
  const pullRequest = Number(segments[5]);
  if (
    !isPositiveSafeInt(appId) ||
    !isPositiveSafeInt(installationId) ||
    !isPositiveSafeInt(baseRepoId) ||
    !isPositiveSafeInt(pullRequest)
  ) {
    return null;
  }
  return {
    host: segments[1],
    appId,
    installationId,
    baseRepoId,
    pullRequest,
  };
}

function mergePermissions(capabilitiesInput: unknown): {
  permissions: Record<string, "read" | "write">;
  hasWriteScope: boolean;
} {
  const merged: Record<string, "read" | "write"> = Object.create(null) as Record<
    string,
    "read" | "write"
  >;
  const addLevel = (name: string, level: "read" | "write") => {
    if (merged[name] !== "write") {
      merged[name] = level;
    }
  };
  if (Array.isArray(capabilitiesInput)) {
    if (capabilitiesInput.length === 0 || capabilitiesInput.length > 10) {
      throw new Error("broker_capability_invalid");
    }
    const seen = new Set<string>();
    for (const item of capabilitiesInput) {
      if (
        typeof item !== "string" ||
        !Object.hasOwn(V6_CAPABILITY_PERMISSIONS, item) ||
        seen.has(item)
      ) {
        throw new Error("broker_capability_invalid");
      }
      seen.add(item);
      for (const [perm, level] of Object.entries(V6_CAPABILITY_PERMISSIONS[item])) {
        addLevel(perm, level);
      }
    }
  } else if (isObject(capabilitiesInput)) {
    const entries = Object.entries(capabilitiesInput);
    if (entries.length === 0 || entries.length > ALLOWED_PERMISSION_NAMES.size) {
      throw new Error("broker_capability_invalid");
    }
    for (const [name, level] of entries) {
      if (
        !ALLOWED_PERMISSION_NAMES.has(name) ||
        (level !== "read" && level !== "write")
      ) {
        throw new Error("broker_capability_invalid");
      }
      addLevel(name, level);
    }
  } else {
    throw new Error("broker_capability_invalid");
  }
  if (Object.keys(merged).length === 0) {
    throw new Error("broker_capability_invalid");
  }
  const hasWriteScope = Object.values(merged).some((level) => level === "write");
  return { permissions: merged, hasWriteScope };
}

function hasExactGrantedPermissions(
  granted: Record<string, string>,
  requested: Record<string, "read" | "write">,
): boolean {
  for (const [name, level] of Object.entries(requested)) {
    if (granted[name] !== level) {
      return false;
    }
  }
  if (!Object.hasOwn(requested, "metadata") && granted.metadata !== "read") {
    return false;
  }
  return Object.entries(granted).every(([name, level]) => {
    if (Object.hasOwn(requested, name)) {
      return true;
    }
    return name === "metadata" && level === "read";
  });
}

function cloneSlot(slot: SlotRecord): SlotRecord {
  return {
    ...slot,
    encrypted_escrow:
      slot.encrypted_escrow === null ? null : { ...slot.encrypted_escrow },
  };
}

function absentSlotView(slotKey: string): Record<string, unknown> {
  return {
    slot_key: slotKey,
    state: "IDLE",
    generation: 0,
    lease_epoch: 0,
    reservation_id: null,
    owner_run_id: null,
    owner_run_attempt: null,
    owner_head_sha: null,
    owner_workflow_sha: null,
    owner_job: null,
    owner_comment_id: null,
    owner_record_sha256: null,
    acquired_at_ms: null,
    token_expires_at_ms: null,
    token_cleared: false,
    last_reason_code: null,
    updated_at_ms: null,
  };
}

function slotStatusView(slot: SlotRecord): Record<string, unknown> {
  return {
    slot_key: slot.slot_key,
    state: slot.state,
    generation: slot.generation,
    lease_epoch: slot.lease_epoch,
    reservation_id: slot.reservation_id,
    owner_run_id: slot.owner_run_id,
    owner_run_attempt: slot.owner_run_attempt,
    owner_head_sha: slot.owner_head_sha,
    owner_workflow_sha: slot.owner_workflow_sha,
    owner_job: slot.owner_job,
    owner_comment_id: slot.owner_comment_id,
    owner_record_sha256: slot.owner_record_sha256,
    acquired_at_ms: slot.acquired_at_ms,
    token_expires_at_ms: slot.token_expires_at_ms,
    token_cleared: slot.token_cleared,
    last_reason_code: slot.last_reason_code,
    updated_at_ms: slot.updated_at_ms,
  };
}

function diagnosticResponse(
  status: number,
  diagnosticCode: string,
  options: {
    error?: string;
    slotState?: CoordinatorState;
    retryAfterMs?: number;
    rayId?: string;
  } = {},
): V6RouteResult {
  const diagnostic = brokerDiagnostic(new Error(diagnosticCode), options.rayId);
  return {
    status,
    body: {
      error: options.error ?? diagnostic.code,
      diagnostic,
      ...(options.slotState !== undefined ? { slot_state: options.slotState } : {}),
      ...(options.retryAfterMs !== undefined ? { retry_after_ms: options.retryAfterMs } : {}),
    },
  };
}

function stateConflictDiagnostic(state: CoordinatorState, slot: SlotRecord): V6DiagnosticCode {
  switch (state) {
    case "RESERVED":
      return "v6_lease_reserved";
    case "MINTING":
      return "v6_lease_mint_uncertain";
    case "ACTIVE":
      return "v6_lease_active_other_owner";
    case "DRAINING":
      return "v6_lease_draining";
    case "RECONCILING":
      return "v6_lease_reconciling";
    case "HOLD":
      return slot.last_reason_code ?? "v6_lease_hold";
    case "IDLE":
      return "v6_lease_rollback_blocked";
  }
}

interface ParsedPrLedgerRecord {
  sha256: string;
  payload: Record<string, unknown>;
}

async function parseAndVerifyPrLedgerMarker(
  body: string,
): Promise<ParsedPrLedgerRecord | null> {
  if (typeof body !== "string") {
    return null;
  }
  const encodedBytes = new TextEncoder().encode(body).byteLength;
  if (encodedBytes > V6_PR_LEDGER_MAX_BYTES) {
    return null;
  }
  const markerLines = body
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("<!-- reviewsensei:v6:record "));
  if (markerLines.length !== 1) {
    return null;
  }
  const line = markerLines[0];
  const match = /^<!-- reviewsensei:v6:record sha256=([0-9a-f]{64}) payload=(\{.*\}) -->$/.exec(
    line,
  );
  if (match === null) {
    return null;
  }
  const [, expectedSha, rawPayload] = match;
  if (rawPayload.includes("-->") || rawPayload.includes("<!--")) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawPayload);
  } catch {
    return null;
  }
  if (!isObject(parsed)) {
    return null;
  }
  const actualSha = await sha256Hex(canonicalJsonString(parsed));
  if (actualSha !== expectedSha) {
    return null;
  }
  return { sha256: expectedSha, payload: parsed };
}

function isValidOwnerRecordShape(record: Record<string, unknown>): boolean {
  const required = [
    "schema",
    "record_id",
    "slot_key",
    "generation",
    "lease_epoch",
    "reservation_id",
    "operation_id",
    "run_id",
    "run_attempt",
    "pr_head_sha",
    "workflow_sha",
    "acquired_at",
    "expires_at",
    "coordinator_attestation_mac",
  ];
  if (required.some((field) => !Object.hasOwn(record, field))) {
    return false;
  }
  const acquiredMs =
    typeof record.acquired_at === "string" ? Date.parse(record.acquired_at) : Number.NaN;
  const expiresMs =
    typeof record.expires_at === "string" ? Date.parse(record.expires_at) : Number.NaN;
  return (
    record.schema === "reviewsensei:v6:owner:v1" &&
    typeof record.record_id === "string" &&
    record.record_id.length > 0 &&
    typeof record.slot_key === "string" &&
    V6_SLOT_KEY_PATTERN.test(record.slot_key) &&
    isPositiveSafeInt(record.generation) &&
    isPositiveSafeInt(record.lease_epoch) &&
    typeof record.reservation_id === "string" &&
    record.reservation_id.length > 0 &&
    typeof record.operation_id === "string" &&
    record.operation_id.length > 0 &&
    typeof record.run_id === "string" &&
    POSITIVE_DECIMAL_PATTERN.test(record.run_id) &&
    isPositiveSafeInt(record.run_attempt) &&
    typeof record.pr_head_sha === "string" &&
    SHA1_HEX40_PATTERN.test(record.pr_head_sha) &&
    typeof record.workflow_sha === "string" &&
    SHA1_HEX40_PATTERN.test(record.workflow_sha) &&
    Number.isFinite(acquiredMs) &&
    Number.isFinite(expiresMs) &&
    expiresMs > acquiredMs &&
    typeof record.coordinator_attestation_mac === "string" &&
    SHA256_HEX64_PATTERN.test(record.coordinator_attestation_mac)
  );
}

function assertRecordMatchesSlot(
  record: Record<string, unknown>,
  expected: {
    slot_key: string;
    generation?: number;
    lease_epoch?: number;
    owner_head_sha?: string | null;
  },
): void {
  if (record.slot_key !== undefined && record.slot_key !== expected.slot_key) {
    throw new Error("v6_lease_slot_continuity_unproved");
  }
  if (
    expected.generation !== undefined &&
    record.generation !== undefined &&
    record.generation !== expected.generation
  ) {
    throw new Error("v6_lease_slot_continuity_unproved");
  }
  if (
    expected.lease_epoch !== undefined &&
    record.lease_epoch !== undefined &&
    record.lease_epoch !== expected.lease_epoch
  ) {
    throw new Error("v6_lease_slot_continuity_unproved");
  }
  if (
    expected.owner_head_sha !== undefined &&
    expected.owner_head_sha !== null &&
    record.pr_head_sha !== undefined &&
    record.pr_head_sha !== expected.owner_head_sha
  ) {
    throw new Error("v6_lease_slot_continuity_unproved");
  }
}

function isValidHoldRecordShape(record: Record<string, unknown>): boolean {
  const required = [
    "schema",
    "record_id",
    "slot_key",
    "generation",
    "lease_epoch",
    "operation_id",
    "pr_head_sha",
    "reason_code",
    "missing_evidence",
    "held_at",
    "resolved_by_record_id",
  ];
  if (required.some((field) => !Object.hasOwn(record, field))) {
    return false;
  }
  return (
    record.schema === "reviewsensei:v6:hold:v1" &&
    typeof record.record_id === "string" &&
    record.record_id.length > 0 &&
    typeof record.slot_key === "string" &&
    V6_SLOT_KEY_PATTERN.test(record.slot_key) &&
    isPositiveSafeInt(record.generation) &&
    isNonNegativeSafeInt(record.lease_epoch) &&
    typeof record.operation_id === "string" &&
    record.operation_id.length > 0 &&
    typeof record.pr_head_sha === "string" &&
    SHA1_HEX40_PATTERN.test(record.pr_head_sha) &&
    typeof record.reason_code === "string" &&
    record.reason_code.length > 0 &&
    Array.isArray(record.missing_evidence) &&
    record.missing_evidence.every((item) => typeof item === "string") &&
    typeof record.held_at === "string" &&
    Number.isFinite(Date.parse(record.held_at)) &&
    (record.resolved_by_record_id === null ||
      (typeof record.resolved_by_record_id === "string" &&
        record.resolved_by_record_id.trim().length > 0))
  );
}

function isValidIntentRecordShape(record: Record<string, unknown>): boolean {
  const required = [
    "schema",
    "record_id",
    "operation_id",
    "writer_epoch",
    "pr_head_sha",
    "effect_kind",
    "payload_sha256",
    "target_digest",
    "generation",
    "target_location",
  ];
  if (required.some((field) => !Object.hasOwn(record, field))) {
    return false;
  }
  return (
    record.schema === "reviewsensei:v6:intent:v1" &&
    typeof record.record_id === "string" &&
    record.record_id.length > 0 &&
    typeof record.operation_id === "string" &&
    record.operation_id.length > 0 &&
    isNonNegativeSafeInt(record.writer_epoch) &&
    typeof record.pr_head_sha === "string" &&
    SHA1_HEX40_PATTERN.test(record.pr_head_sha) &&
    typeof record.effect_kind === "string" &&
    record.effect_kind.length > 0 &&
    typeof record.payload_sha256 === "string" &&
    SHA256_HEX64_PATTERN.test(record.payload_sha256) &&
    typeof record.target_digest === "string" &&
    isPositiveSafeInt(record.generation) &&
    typeof record.target_location === "string" &&
    record.target_location.length > 0
  );
}

function isValidReceiptRecordShape(record: Record<string, unknown>): boolean {
  const required = [
    "schema",
    "record_id",
    "operation_id",
    "writer_epoch",
    "github_effect_id",
    "payload_sha256",
    "completed_at",
  ];
  if (required.some((field) => !Object.hasOwn(record, field))) {
    return false;
  }
  return (
    record.schema === "reviewsensei:v6:receipt:v1" &&
    typeof record.record_id === "string" &&
    record.record_id.length > 0 &&
    typeof record.operation_id === "string" &&
    record.operation_id.length > 0 &&
    isNonNegativeSafeInt(record.writer_epoch) &&
    ((typeof record.github_effect_id === "string" && record.github_effect_id.length > 0) ||
      isPositiveSafeInt(record.github_effect_id)) &&
    typeof record.payload_sha256 === "string" &&
    SHA256_HEX64_PATTERN.test(record.payload_sha256) &&
    typeof record.completed_at === "string" &&
    Number.isFinite(Date.parse(record.completed_at))
  );
}

function isValidNonSubmissionRecordShape(record: Record<string, unknown>): boolean {
  const required = [
    "schema",
    "record_id",
    "slot_key",
    "generation",
    "operation_id",
    "pr_head_sha",
    "proof_kind",
    "payload_sha256",
    "recorded_at",
  ];
  if (required.some((field) => !Object.hasOwn(record, field))) {
    return false;
  }
  return (
    record.schema === "reviewsensei:v6:non-submission:v1" &&
    typeof record.record_id === "string" &&
    record.record_id.length > 0 &&
    typeof record.slot_key === "string" &&
    V6_SLOT_KEY_PATTERN.test(record.slot_key) &&
    isPositiveSafeInt(record.generation) &&
    typeof record.operation_id === "string" &&
    record.operation_id.length > 0 &&
    typeof record.pr_head_sha === "string" &&
    SHA1_HEX40_PATTERN.test(record.pr_head_sha) &&
    (record.proof_kind === "definitive_github_rejection" ||
      record.proof_kind === "pre_dispatch_terminal_abort") &&
    typeof record.payload_sha256 === "string" &&
    SHA256_HEX64_PATTERN.test(record.payload_sha256) &&
    typeof record.recorded_at === "string" &&
    Number.isFinite(Date.parse(record.recorded_at))
  );
}

/**
 * In-memory durable store used by deterministic Phase 4 unit tests.
 * Deep-clones records on every read and write so crash/restart tests see real serialization boundaries.
 */
export class InMemoryV6CoordinatorStore implements V6CoordinatorStore {
  private readonly slots = new Map<string, SlotRecord>();

  async getSlot(slotKey: string): Promise<SlotRecord | null> {
    const found = this.slots.get(slotKey);
    return found === undefined ? null : cloneSlot(found);
  }

  async putSlot(record: SlotRecord): Promise<void> {
    this.slots.set(record.slot_key, cloneSlot(record));
  }

  snapshot(): Record<string, SlotRecord> {
    const out: Record<string, SlotRecord> = {};
    for (const [key, value] of this.slots.entries()) {
      out[key] = cloneSlot(value);
    }
    return out;
  }

  deleteSlotForTesting(slotKey: string): void {
    this.slots.delete(slotKey);
  }
}

export function validateSlotRecord(record: SlotRecord, expectedSlotKey: string): SlotRecord {
  if (
    typeof record.slot_key !== "string" ||
    record.slot_key !== expectedSlotKey ||
    typeof record.state !== "string" ||
    !VALID_COORDINATOR_STATES.has(record.state) ||
    !isNonNegativeSafeInt(record.generation) ||
    !isNonNegativeSafeInt(record.lease_epoch) ||
    (record.acquired_at_ms !== null && !isPositiveSafeInt(record.acquired_at_ms)) ||
    typeof record.token_cleared !== "boolean" ||
    !isNonNegativeSafeInt(record.clock_margin_ms) ||
    !isNonNegativeSafeInt(record.updated_at_ms) ||
    (record.last_reason_code !== null &&
      (typeof record.last_reason_code !== "string" ||
        !VALID_V6_DIAGNOSTIC_CODES.has(record.last_reason_code))) ||
    (record.encrypted_escrow !== null && !isEncryptedEscrow(record.encrypted_escrow))
  ) {
    throw new Error("v6_lease_escrow_lost");
  }
  return record;
}

/**
 * SQLite-backed `ctx.storage` adapter for `V6PrCoordinator` Durable Objects.
 * Columns strictly match `v6-coordinator-contract.json`'s `keep_fields`.
 */
export class DurableObjectSqlV6CoordinatorStore implements V6CoordinatorStore {
  private initialized = false;

  constructor(private readonly sql: SqlStorage) {}

  private ensureSchema(): void {
    if (this.initialized) {
      return;
    }
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS v6_pr_slot (
        slot_key TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        generation INTEGER NOT NULL,
        lease_epoch INTEGER NOT NULL,
        reservation_id TEXT,
        owner_run_id TEXT,
        owner_run_attempt INTEGER,
        owner_head_sha TEXT,
        owner_workflow_sha TEXT,
        owner_job TEXT,
        owner_comment_id INTEGER,
        owner_record_sha256 TEXT,
        acquired_at_ms INTEGER,
        token_expires_at_ms INTEGER,
        token_cleared INTEGER NOT NULL DEFAULT 0,
        clock_margin_ms INTEGER NOT NULL,
        encrypted_escrow TEXT,
        last_reason_code TEXT,
        updated_at_ms INTEGER NOT NULL
      )
    `);
    const columns = this.sql
      .exec<{ name: SqlStorageValue }>("PRAGMA table_info(v6_pr_slot)")
      .toArray()
      .map((col) => String(col.name));
    if (columns.length > 0) {
      if (!columns.includes("acquired_at_ms")) {
        this.sql.exec("ALTER TABLE v6_pr_slot ADD COLUMN acquired_at_ms INTEGER");
      }
      if (!columns.includes("token_cleared")) {
        this.sql.exec("ALTER TABLE v6_pr_slot ADD COLUMN token_cleared INTEGER NOT NULL DEFAULT 0");
      }
    }
    this.initialized = true;
  }

  async getSlot(slotKey: string): Promise<SlotRecord | null> {
    this.ensureSchema();
    const rows = this.sql
      .exec<Record<string, SqlStorageValue>>(
        `SELECT slot_key, state, generation, lease_epoch, reservation_id,
                owner_run_id, owner_run_attempt, owner_head_sha, owner_workflow_sha,
                owner_job, owner_comment_id, owner_record_sha256,
                acquired_at_ms, token_expires_at_ms, token_cleared,
                clock_margin_ms, encrypted_escrow,
                last_reason_code, updated_at_ms
           FROM v6_pr_slot
          WHERE slot_key = ?`,
        slotKey,
      )
      .toArray();
    if (rows.length === 0) {
      return null;
    }
    const row = rows[0];
    if (
      typeof row.state !== "string" ||
      !VALID_COORDINATOR_STATES.has(row.state as CoordinatorState)
    ) {
      throw new Error("v6_lease_escrow_lost");
    }
    if (
      row.last_reason_code !== null &&
      (typeof row.last_reason_code !== "string" ||
        !VALID_V6_DIAGNOSTIC_CODES.has(row.last_reason_code as V6DiagnosticCode))
    ) {
      throw new Error("v6_lease_escrow_lost");
    }
    if (
      row.token_cleared !== 0 &&
      row.token_cleared !== 1 &&
      typeof row.token_cleared !== "boolean"
    ) {
      throw new Error("v6_lease_escrow_lost");
    }
    let parsedEscrow: EncryptedEscrow | null = null;
    if (typeof row.encrypted_escrow === "string" && row.encrypted_escrow.length > 0) {
      try {
        const candidate = JSON.parse(row.encrypted_escrow) as unknown;
        if (!isEncryptedEscrow(candidate)) {
          throw new Error("v6_lease_escrow_lost");
        }
        parsedEscrow = candidate;
      } catch {
        throw new Error("v6_lease_escrow_lost");
      }
    } else if (row.encrypted_escrow !== null) {
      throw new Error("v6_lease_escrow_lost");
    }
    return validateSlotRecord(
      {
        slot_key: String(row.slot_key),
        state: row.state as CoordinatorState,
        generation: Number(row.generation),
        lease_epoch: Number(row.lease_epoch),
        reservation_id: row.reservation_id === null ? null : String(row.reservation_id),
        owner_run_id: row.owner_run_id === null ? null : String(row.owner_run_id),
        owner_run_attempt:
          row.owner_run_attempt === null ? null : Number(row.owner_run_attempt),
        owner_head_sha: row.owner_head_sha === null ? null : String(row.owner_head_sha),
        owner_workflow_sha:
          row.owner_workflow_sha === null ? null : String(row.owner_workflow_sha),
        owner_job: row.owner_job === null ? null : String(row.owner_job),
        owner_comment_id:
          row.owner_comment_id === null ? null : Number(row.owner_comment_id),
        owner_record_sha256:
          row.owner_record_sha256 === null ? null : String(row.owner_record_sha256),
        acquired_at_ms:
          row.acquired_at_ms === null ? null : Number(row.acquired_at_ms),
        token_expires_at_ms:
          row.token_expires_at_ms === null ? null : Number(row.token_expires_at_ms),
        token_cleared: Boolean(row.token_cleared),
        clock_margin_ms: Number(row.clock_margin_ms),
        encrypted_escrow: parsedEscrow,
        last_reason_code:
          row.last_reason_code === null
            ? null
            : (row.last_reason_code as V6DiagnosticCode),
        updated_at_ms: Number(row.updated_at_ms),
      },
      slotKey,
    );
  }

  async putSlot(record: SlotRecord): Promise<void> {
    this.ensureSchema();
    const escrowJson =
      record.encrypted_escrow === null ? null : JSON.stringify(record.encrypted_escrow);
    this.sql.exec(
      `INSERT INTO v6_pr_slot (
         slot_key, state, generation, lease_epoch, reservation_id,
         owner_run_id, owner_run_attempt, owner_head_sha, owner_workflow_sha,
         owner_job, owner_comment_id, owner_record_sha256,
         acquired_at_ms, token_expires_at_ms, token_cleared,
         clock_margin_ms, encrypted_escrow,
         last_reason_code, updated_at_ms
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(slot_key) DO UPDATE SET
         state = excluded.state,
         generation = excluded.generation,
         lease_epoch = excluded.lease_epoch,
         reservation_id = excluded.reservation_id,
         owner_run_id = excluded.owner_run_id,
         owner_run_attempt = excluded.owner_run_attempt,
         owner_head_sha = excluded.owner_head_sha,
         owner_workflow_sha = excluded.owner_workflow_sha,
         owner_job = excluded.owner_job,
         owner_comment_id = excluded.owner_comment_id,
         owner_record_sha256 = excluded.owner_record_sha256,
         acquired_at_ms = excluded.acquired_at_ms,
         token_expires_at_ms = excluded.token_expires_at_ms,
         token_cleared = excluded.token_cleared,
         clock_margin_ms = excluded.clock_margin_ms,
         encrypted_escrow = excluded.encrypted_escrow,
         last_reason_code = excluded.last_reason_code,
         updated_at_ms = excluded.updated_at_ms`,
      record.slot_key,
      record.state,
      record.generation,
      record.lease_epoch,
      record.reservation_id,
      record.owner_run_id,
      record.owner_run_attempt,
      record.owner_head_sha,
      record.owner_workflow_sha,
      record.owner_job,
      record.owner_comment_id,
      record.owner_record_sha256,
      record.acquired_at_ms,
      record.token_expires_at_ms,
      record.token_cleared ? 1 : 0,
      record.clock_margin_ms,
      escrowJson,
      record.last_reason_code,
      record.updated_at_ms,
    );
  }
}

export class GitHubV6ProviderAdapter implements V6ProviderAdapter {
  constructor(private readonly github: GitHubApi) {}

  async verifyOidc(token: string, options: OidcValidationOptions = {}): Promise<OidcClaims> {
    return verifyOidcAssertion(token, {
      issuer: "https://token.actions.githubusercontent.com",
      audience: "sts.reviewsensei.dev",
      ...options,
    });
  }

  async workflowRuntimeShas(tag: string): Promise<PublicWorkflowRuntimeShas> {
    return this.github.publicWorkflowRuntimeShas(tag);
  }

  async appBotLogin(): Promise<string> {
    return this.github.appBotLogin();
  }

  async installationFor(repository: string): Promise<number | null> {
    return this.github.installationFor(repository);
  }

  async readOnlyToken(
    installationId: number,
    repository: string,
    scope: "status" | "actions" = "status",
  ): Promise<string> {
    const perms: Record<string, "read" | "write"> =
      scope === "actions"
        ? { actions: "read", contents: "read", pull_requests: "read" }
        : { contents: "read", pull_requests: "read" };
    return this.github.capabilityToken(installationId, repository, perms);
  }

  async repositoryInfo(
    repository: string,
    token: string,
  ): Promise<{ id: number; fork: boolean } | null> {
    return this.github.repositoryInfo(repository, token);
  }

  async pullRequestBinding(
    repository: string,
    pullRequest: number,
    token: string,
  ): Promise<PullRequestBinding | null> {
    return this.github.pullRequestBinding(repository, pullRequest, token);
  }

  async workflowRunAttempt(
    repository: string,
    runId: string,
    attempt: string,
    token: string,
  ): Promise<WorkflowRunAttemptInfo | null> {
    return this.github.workflowRunAttempt(repository, runId, attempt, token);
  }

  async issueComment(
    repository: string,
    pullRequest: number,
    commentId: number,
    token: string,
  ): Promise<LiveIssueComment | null> {
    return this.github.issueComment(
      repository,
      pullRequest,
      commentId,
      token,
      V6_PR_LEDGER_MAX_BYTES,
    );
  }

  async issueCommentsSince(
    repository: string,
    pullRequest: number,
    sinceIso: string,
    token: string,
  ): Promise<LiveIssueCommentPage> {
    return this.github.issueCommentsSince(
      repository,
      pullRequest,
      sinceIso,
      token,
      V6_PR_LEDGER_MAX_BYTES,
    );
  }

  async pullRequestReview(
    repository: string,
    pullRequest: number,
    reviewId: number,
    token: string,
  ): Promise<LivePullRequestReview | null> {
    return this.github.pullRequestReview(repository, pullRequest, reviewId, token);
  }

  async pullRequestReviewComment(
    repository: string,
    pullRequest: number,
    commentId: number,
    token: string,
  ): Promise<LivePullRequestReviewComment | null> {
    return this.github.pullRequestReviewComment(repository, pullRequest, commentId, token);
  }

  async checkRun(
    repository: string,
    checkRunId: number,
    token: string,
  ): Promise<LiveCheckRun | null> {
    return this.github.checkRun(repository, checkRunId, token);
  }

  async mintInstallationToken(
    installationId: number,
    repository: string,
    permissions: Record<string, "read" | "write">,
  ) {
    return this.github.capabilityTokenObservation(installationId, repository, permissions);
  }

  async revokeInstallationToken(token: string): Promise<RevocationResult> {
    return this.github.revokeInstallationToken(token);
  }
}

interface VerifiedEdgeContext {
  claims: OidcClaims;
  appId: number;
  installationId: number;
  baseRepoId: number;
  host: string;
  clockMarginMs: number;
  crypto: V6EscrowCrypto;
  pilotRepos: Set<string>;
  rollbackRepos: Set<string>;
  readToken: string;
}

export class V6PrCoordinatorEngine {
  private readonly store: V6CoordinatorStore;
  private readonly provider: V6ProviderAdapter;
  private readonly clock: V6Clock;
  private readonly audit: V6AuditSink;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly maxRevocationAttempts: number;
  private readonly generateReservationId: () => string;
  private readonly slotLocks = new Map<string, Promise<unknown>>();
  private readonly activeReservations = new Set<string>();

  constructor(options: V6CoordinatorEngineOptions) {
    this.store = options.store;
    this.provider = options.provider;
    this.clock = options.clock ?? SYSTEM_CLOCK;
    this.audit = options.audit ?? DEFAULT_AUDIT_SINK;
    this.sleep = options.sleep ?? defaultSleep;
    this.maxRevocationAttempts = Math.max(
      1,
      options.maxRevocationAttempts ?? DEFAULT_REVOCATION_MAX_ATTEMPTS,
    );
    this.generateReservationId = options.generateReservationId ?? (() => crypto.randomUUID());
  }

  private async readSlot(slotKey: string): Promise<SlotRecord | null> {
    const slot = await this.store.getSlot(slotKey);
    if (slot === null) {
      return null;
    }
    return validateSlotRecord(slot, slotKey);
  }

  private async withSlotLock<T>(slotKey: string, fn: () => Promise<T>): Promise<T> {
    const previous = this.slotLocks.get(slotKey) ?? Promise.resolve();
    const next = previous.then(fn, fn);
    const settled = next.then(
      () => undefined,
      () => undefined,
    );
    this.slotLocks.set(slotKey, settled);
    try {
      return await next;
    } finally {
      if (this.slotLocks.get(slotKey) === settled) {
        this.slotLocks.delete(slotKey);
      }
    }
  }

  private emitAudit(
    route: string,
    slotKey: string | null,
    outcome: "success" | "refused",
    extra: Partial<Omit<V6AuditEvent, "route" | "slot_key" | "outcome" | "timestamp_ms">> = {},
  ): void {
    this.audit.emit({
      route,
      slot_key: slotKey,
      outcome,
      ...extra,
      timestamp_ms: this.clock.nowMs(),
    });
  }

  private async verifyTupleAndEdge(
    env: WorkerEnv,
    oidcToken: unknown,
    control?: BrokerDispatchControl,
  ): Promise<VerifiedEdgeContext> {
    const appId = configuredAppId(env);
    const clockMarginMs = configuredClockMarginMs(env);
    const host = canonicalSlotHost(env.GITHUB_API_URL);
    const escrowCrypto = new V6EscrowCrypto(env.REVIEWSENSEI_SIGNING_KEY);

    if (typeof oidcToken !== "string" || !oidcToken.trim()) {
      throw new Error("broker_invalid_request");
    }

    const claims = await this.provider.verifyOidc(oidcToken, {
      now: this.clock.nowMs() / 1000,
      dispatchControl: control,
    });

    if (
      !POSITIVE_DECIMAL_PATTERN.test(claims.run_id) ||
      !POSITIVE_DECIMAL_PATTERN.test(claims.run_attempt) ||
      !Number.isSafeInteger(Number(claims.run_attempt))
    ) {
      throw new Error("v6_lease_unauthorized_tuple");
    }
    if (!ALLOWED_EVENTS.has(claims.event_name)) {
      throw new Error("v6_lease_unauthorized_tuple");
    }
    if (!["github-hosted", "self-hosted"].includes(claims.runner_environment)) {
      throw new Error("v6_lease_unauthorized_tuple");
    }
    if (claims.repository_owner !== claims.repository.split("/", 1)[0]) {
      throw new Error("v6_lease_unauthorized_tuple");
    }

    const pilotRepos = parseRepositoryCsv(env.V6_PILOT_REPOSITORIES);
    if (!pilotRepos.has(claims.repository.toLowerCase())) {
      throw new Error("v6_lease_unauthorized_tuple");
    }

    await this.verifyPinnedWorkflow(env, claims);

    const installationId = await this.provider.installationFor(claims.repository);
    if (installationId === null) {
      throw new Error("v6_lease_unauthorized_tuple");
    }
    const readToken = await this.provider.readOnlyToken(
      installationId,
      claims.repository,
      "actions",
    );
    const repoInfo = await this.provider.repositoryInfo(claims.repository, readToken);
    if (repoInfo === null || repoInfo.fork || repoInfo.id !== claims.repository_id) {
      throw new Error("v6_lease_unauthorized_tuple");
    }

    return {
      claims,
      appId,
      installationId,
      baseRepoId: repoInfo.id,
      host,
      clockMarginMs,
      crypto: escrowCrypto,
      pilotRepos,
      rollbackRepos: parseRepositoryCsv(env.V6_PILOT_ROLLBACK_REPOSITORIES),
      readToken,
    };
  }

  private async verifyPinnedWorkflow(env: WorkerEnv, claims: OidcClaims): Promise<void> {
    const pinnedRef = env.V6_PILOT_WORKFLOW_REF?.trim();
    const pinnedTag = env.V6_PILOT_WORKFLOW_TAG?.trim();

    if (pinnedRef) {
      if (claims.job_workflow_ref !== pinnedRef) {
        throw new Error("v6_lease_unauthorized_tuple");
      }
      const atIndex = pinnedRef.lastIndexOf("@");
      if (atIndex === -1) {
        throw new Error("v6_lease_unauthorized_tuple");
      }
      const refPart = pinnedRef.slice(atIndex + 1);
      if (SHA1_HEX40_PATTERN.test(refPart)) {
        if (claims.job_workflow_sha !== refPart) {
          throw new Error("v6_lease_unauthorized_tuple");
        }
        return;
      }
      if (refPart.startsWith("refs/tags/")) {
        const tag = validatePublicWorkflowTag(refPart.slice("refs/tags/".length));
        const runtime = await this.provider.workflowRuntimeShas(tag);
        if (
          claims.job_workflow_sha !== runtime.commitSha &&
          claims.job_workflow_sha !== runtime.refSha
        ) {
          throw new Error("v6_lease_unauthorized_tuple");
        }
        return;
      }
      const runtime = await this.provider.workflowRuntimeShas(refPart);
      if (
        claims.job_workflow_sha !== runtime.commitSha &&
        claims.job_workflow_sha !== runtime.refSha
      ) {
        throw new Error("v6_lease_unauthorized_tuple");
      }
      return;
    }

    if (pinnedTag && V6_WORKFLOW_TAG_PATTERN.test(pinnedTag)) {
      const validatedTag = validatePublicWorkflowTag(pinnedTag);
      const acceptedRefs = new Set([
        `${PUBLIC_REPOSITORY}/.github/workflows/review-sensei-v6.yml@refs/tags/${validatedTag}`,
        `${PUBLIC_REPOSITORY}/.github/workflows/review-sensei-v6-pilot.yml@refs/tags/${validatedTag}`,
        `${PUBLIC_REPOSITORY}/.github/workflows/review-sensei-run.yml@refs/tags/${validatedTag}`,
      ]);
      if (!acceptedRefs.has(claims.job_workflow_ref)) {
        throw new Error("v6_lease_unauthorized_tuple");
      }
      const runtime = await this.provider.workflowRuntimeShas(validatedTag);
      if (
        claims.job_workflow_sha !== runtime.commitSha &&
        claims.job_workflow_sha !== runtime.refSha
      ) {
        throw new Error("v6_lease_unauthorized_tuple");
      }
      return;
    }

    throw new Error("v6_lease_unauthorized_tuple");
  }

  private async verifyLedgerComment(
    edge: VerifiedEdgeContext,
    pullRequest: number,
    commentId: number,
    expectedSchema: "owner" | "hold" | "any",
  ): Promise<{
    comment: LiveIssueComment;
    parsed: ParsedPrLedgerRecord;
  }> {
    const [botLogin, comment] = await Promise.all([
      this.provider.appBotLogin(),
      this.provider.issueComment(edge.claims.repository, pullRequest, commentId, edge.readToken),
    ]);
    if (
      comment === null ||
      comment.userType !== "Bot" ||
      comment.login.toLowerCase() !== botLogin.toLowerCase()
    ) {
      throw new Error("v6_lease_slot_continuity_unproved");
    }
    const parsed = await parseAndVerifyPrLedgerMarker(comment.body);
    if (parsed === null) {
      throw new Error("v6_lease_slot_continuity_unproved");
    }
    const schema = parsed.payload.schema;
    if (expectedSchema === "owner") {
      if (!isValidOwnerRecordShape(parsed.payload)) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
      const validMac = await edge.crypto.verifyOwnerAttestation(
        {
          slot_key: parsed.payload.slot_key as string,
          generation: parsed.payload.generation as number,
          lease_epoch: parsed.payload.lease_epoch as number,
          reservation_id: parsed.payload.reservation_id as string,
          run_id: parsed.payload.run_id as string,
          run_attempt: parsed.payload.run_attempt as number,
          pr_head_sha: parsed.payload.pr_head_sha as string,
          workflow_sha: parsed.payload.workflow_sha as string,
        },
        parsed.payload.coordinator_attestation_mac as string,
      );
      if (!validMac) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } else if (expectedSchema === "hold") {
      if (!isValidHoldRecordShape(parsed.payload)) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } else if (schema === "reviewsensei:v6:owner:v1") {
      if (!isValidOwnerRecordShape(parsed.payload)) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
      const validMac = await edge.crypto.verifyOwnerAttestation(
        {
          slot_key: parsed.payload.slot_key as string,
          generation: parsed.payload.generation as number,
          lease_epoch: parsed.payload.lease_epoch as number,
          reservation_id: parsed.payload.reservation_id as string,
          run_id: parsed.payload.run_id as string,
          run_attempt: parsed.payload.run_attempt as number,
          pr_head_sha: parsed.payload.pr_head_sha as string,
          workflow_sha: parsed.payload.workflow_sha as string,
        },
        parsed.payload.coordinator_attestation_mac as string,
      );
      if (!validMac) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } else if (schema === "reviewsensei:v6:intent:v1") {
      if (!isValidIntentRecordShape(parsed.payload)) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } else if (schema === "reviewsensei:v6:receipt:v1") {
      if (!isValidReceiptRecordShape(parsed.payload)) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } else if (schema === "reviewsensei:v6:non-submission:v1") {
      if (!isValidNonSubmissionRecordShape(parsed.payload)) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } else if (schema === "reviewsensei:v6:hold:v1") {
      if (!isValidHoldRecordShape(parsed.payload)) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } else {
      throw new Error("v6_lease_slot_continuity_unproved");
    }
    return { comment, parsed };
  }

  private mapEdgeError(route: string, error: unknown, rayId?: string): V6RouteResult {
    const code = error instanceof Error ? error.message : "v6_lease_unauthorized_tuple";
    this.emitAudit(route, null, "refused", { diagnostic: code });
    if (code === "configuration_unavailable") {
      return diagnosticResponse(503, "configuration_unavailable", { rayId });
    }
    if (code === "broker_invalid_request" || code === "broker_capability_invalid") {
      return diagnosticResponse(400, code, { rayId });
    }
    if (code === "oidc_jwks_unavailable" || code === "oidc_jwks_invalid" || code === "oidc_jwks_too_large") {
      return diagnosticResponse(503, code, { rayId });
    }
    if (code.startsWith("oidc_")) {
      return diagnosticResponse(403, "v6_lease_unauthorized_tuple", { rayId });
    }
    if (
      code === "github_workflow_tag_unavailable" ||
      /^github_workflow_tag_unavailable_5/.test(code) ||
      code === "github_pull_request_lookup_temporary"
    ) {
      return diagnosticResponse(503, "v6_lease_unauthorized_tuple", { rayId });
    }
    return diagnosticResponse(403, "v6_lease_unauthorized_tuple", { rayId });
  }

  private validateSlotKeyForEdge(
    slotKey: unknown,
    edge: VerifiedEdgeContext,
  ): {
    slotKey: string;
    pullRequest: number;
  } {
    const parsed = parseCanonicalSlotKey(slotKey);
    if (parsed === null) {
      throw new Error("broker_invalid_request");
    }
    const expected = buildCanonicalSlotKey({
      host: edge.host,
      appId: edge.appId,
      installationId: edge.installationId,
      baseRepoId: edge.baseRepoId,
      pullRequest: parsed.pullRequest,
    });
    if (slotKey !== expected) {
      throw new Error("v6_lease_unauthorized_tuple");
    }
    return { slotKey: expected, pullRequest: parsed.pullRequest };
  }

  private async isCredentialClearedForReconcileOrHold(
    slot: SlotRecord,
    edge: VerifiedEdgeContext,
  ): Promise<
    | { cleared: true; clearReasonCode: true }
    | { cleared: false; reason: V6DiagnosticCode; nextState: "HOLD" | "RECONCILING" | "DRAINING"; retryAfterMs?: number }
  > {
    if (!this.clock.isTrusted()) {
      return { cleared: false, reason: "v6_lease_clock_untrusted", nextState: "HOLD" };
    }
    if (slot.token_cleared) {
      if (slot.encrypted_escrow !== null) {
        return { cleared: false, reason: "v6_lease_escrow_lost", nextState: "HOLD" };
      }
      return { cleared: true, clearReasonCode: true };
    }
    if (slot.encrypted_escrow !== null) {
      if (
        isPositiveSafeInt(slot.token_expires_at_ms) &&
        this.clock.nowMs() >= slot.token_expires_at_ms + slot.clock_margin_ms
      ) {
        return { cleared: true, clearReasonCode: true };
      }
      return {
        cleared: false,
        reason: "v6_lease_revocation_uncertain",
        nextState: "DRAINING",
        retryAfterMs: isPositiveSafeInt(slot.token_expires_at_ms)
          ? Math.max(0, slot.token_expires_at_ms + slot.clock_margin_ms - this.clock.nowMs())
          : undefined,
      };
    }
    if (slot.token_expires_at_ms !== null) {
      if (!isPositiveSafeInt(slot.token_expires_at_ms)) {
        return { cleared: false, reason: "v6_lease_escrow_lost", nextState: "HOLD" };
      }
      if (this.clock.nowMs() >= slot.token_expires_at_ms + slot.clock_margin_ms) {
        return { cleared: true, clearReasonCode: true };
      }
      return {
        cleared: false,
        reason: "v6_lease_revocation_uncertain",
        nextState: slot.state === "HOLD" ? "HOLD" : "RECONCILING",
        retryAfterMs: slot.token_expires_at_ms + slot.clock_margin_ms - this.clock.nowMs(),
      };
    }
    // `token_cleared === false`, `encrypted_escrow === null`, and `token_expires_at_ms === null`:
    // fail closed (e.g., unobserved mint timeout or 401 without known expiry).
    if (slot.last_reason_code === "v6_lease_clock_untrusted") {
      return { cleared: false, reason: "v6_lease_clock_untrusted", nextState: "HOLD" };
    }
    if (slot.last_reason_code === "v6_lease_escrow_lost") {
      return { cleared: false, reason: "v6_lease_escrow_lost", nextState: "HOLD" };
    }
    return { cleared: false, reason: "v6_lease_revocation_uncertain", nextState: "HOLD" };
  }

  private async executeRevocation(
    slot: SlotRecord,
    edge: VerifiedEdgeContext,
    nextStateOnClear: "IDLE" | "RECONCILING",
  ): Promise<{
    updatedSlot: SlotRecord;
    revocationStatus?: string;
    errorStatus?: number;
    diagnostic?: V6DiagnosticCode;
    retryAfterMs?: number;
  }> {
    const clearFields = {
      state: nextStateOnClear,
      reservation_id: null,
      acquired_at_ms: nextStateOnClear === "IDLE" ? null : slot.acquired_at_ms,
      encrypted_escrow: null,
      token_expires_at_ms: null,
      token_cleared: nextStateOnClear === "RECONCILING",
      last_reason_code: null,
    } as const;

    if (!this.clock.isTrusted()) {
      const held: SlotRecord = {
        ...slot,
        state: "HOLD",
        token_cleared: false,
        last_reason_code: "v6_lease_clock_untrusted",
        updated_at_ms: this.clock.nowMs(),
      };
      return {
        updatedSlot: held,
        errorStatus: 409,
        diagnostic: "v6_lease_clock_untrusted",
      };
    }

    if (slot.encrypted_escrow === null) {
      if (!isPositiveSafeInt(slot.token_expires_at_ms)) {
        const held: SlotRecord = {
          ...slot,
          state: "HOLD",
          token_cleared: false,
          last_reason_code: "v6_lease_escrow_lost",
          updated_at_ms: this.clock.nowMs(),
        };
        return {
          updatedSlot: held,
          errorStatus: 409,
          diagnostic: "v6_lease_escrow_lost",
        };
      }
      const expiryThreshold = slot.token_expires_at_ms + slot.clock_margin_ms;
      if (this.clock.nowMs() >= expiryThreshold) {
        const cleared: SlotRecord = {
          ...slot,
          ...clearFields,
          updated_at_ms: this.clock.nowMs(),
        };
        return { updatedSlot: cleared, revocationStatus: "expired_past_margin" };
      }
      const waiting: SlotRecord = {
        ...slot,
        state: "RECONCILING",
        token_cleared: false,
        last_reason_code: "v6_lease_escrow_lost",
        updated_at_ms: this.clock.nowMs(),
      };
      return {
        updatedSlot: waiting,
        errorStatus: 409,
        diagnostic: "v6_lease_escrow_lost",
        retryAfterMs: expiryThreshold - this.clock.nowMs(),
      };
    }

    let plaintextToken: string;
    try {
      if (!slot.reservation_id || !isPositiveSafeInt(slot.generation)) {
        throw new Error("v6_lease_escrow_lost");
      }
      plaintextToken = await edge.crypto.unsealToken(slot.encrypted_escrow, {
        slot_key: slot.slot_key,
        generation: slot.generation,
        reservation_id: slot.reservation_id,
      });
    } catch (error) {
      if (error instanceof Error && error.message === "configuration_unavailable") {
        throw error;
      }
      if (
        isPositiveSafeInt(slot.token_expires_at_ms) &&
        this.clock.nowMs() >= slot.token_expires_at_ms + slot.clock_margin_ms
      ) {
        const cleared: SlotRecord = {
          ...slot,
          ...clearFields,
          updated_at_ms: this.clock.nowMs(),
        };
        return { updatedSlot: cleared, revocationStatus: "expired_past_margin" };
      }
      const held: SlotRecord = {
        ...slot,
        state: "HOLD",
        token_cleared: false,
        last_reason_code: "v6_lease_escrow_lost",
        updated_at_ms: this.clock.nowMs(),
      };
      return {
        updatedSlot: held,
        errorStatus: 409,
        diagnostic: "v6_lease_escrow_lost",
      };
    }

    let lastResult: RevocationResult | null = null;
    for (let attempt = 1; attempt <= this.maxRevocationAttempts; attempt += 1) {
      lastResult = await this.provider.revokeInstallationToken(plaintextToken);

      if (lastResult.kind === "http_204") {
        const cleared: SlotRecord = {
          ...slot,
          ...clearFields,
          updated_at_ms: this.clock.nowMs(),
        };
        return { updatedSlot: cleared, revocationStatus: "confirmed_revoked" };
      }

      if (lastResult.kind === "http_401") {
        // Guard against null or non-positive token_expires_at_ms (RS-4DBC1C).
        if (!isPositiveSafeInt(slot.token_expires_at_ms)) {
          const held: SlotRecord = {
            ...slot,
            state: "HOLD",
            token_cleared: false,
            last_reason_code: "v6_lease_escrow_lost",
            updated_at_ms: this.clock.nowMs(),
          };
          return {
            updatedSlot: held,
            errorStatus: 409,
            diagnostic: "v6_lease_escrow_lost",
          };
        }
        if (!this.clock.isTrusted()) {
          const held: SlotRecord = {
            ...slot,
            state: "HOLD",
            token_cleared: false,
            last_reason_code: "v6_lease_clock_untrusted",
            updated_at_ms: this.clock.nowMs(),
          };
          return {
            updatedSlot: held,
            errorStatus: 409,
            diagnostic: "v6_lease_clock_untrusted",
          };
        }
        const expiryThreshold = slot.token_expires_at_ms + slot.clock_margin_ms;
        if (this.clock.nowMs() >= expiryThreshold) {
          const cleared: SlotRecord = {
            ...slot,
            ...clearFields,
            updated_at_ms: this.clock.nowMs(),
          };
          return { updatedSlot: cleared, revocationStatus: "expired_past_margin" };
        }
        const reconciling: SlotRecord = {
          ...slot,
          state: "RECONCILING",
          encrypted_escrow: null,
          token_cleared: false,
          last_reason_code: "v6_lease_revocation_uncertain",
          updated_at_ms: this.clock.nowMs(),
        };
        return {
          updatedSlot: reconciling,
          errorStatus: 409,
          diagnostic: "v6_lease_revocation_uncertain",
          retryAfterMs: expiryThreshold - this.clock.nowMs(),
        };
      }

      if (lastResult.kind === "permission_403" || lastResult.kind === "http_other_4xx") {
        // Fail closed immediately, no retry, preserve encrypted_escrow (RS-A57116).
        const held: SlotRecord = {
          ...slot,
          state: "HOLD",
          token_cleared: false,
          last_reason_code: "v6_lease_revocation_uncertain",
          updated_at_ms: this.clock.nowMs(),
        };
        return {
          updatedSlot: held,
          errorStatus: 409,
          diagnostic: "v6_lease_revocation_uncertain",
        };
      }

      // `http_429_or_ratelimit_403` or `http_5xx_or_timeout`: bounded retry on exact token.
      if (attempt < this.maxRevocationAttempts) {
        const retryAfterMs =
          lastResult.retryAfterSeconds !== null
            ? lastResult.retryAfterSeconds * 1000
            : lastResult.rateLimitResetEpoch !== null
              ? Math.max(0, lastResult.rateLimitResetEpoch * 1000 - this.clock.nowMs())
              : 100 * attempt;
        await this.sleep(Math.min(MAX_RETRY_BACKOFF_MS, Math.max(0, retryAfterMs)));
      }
    }

    const reconciling: SlotRecord = {
      ...slot,
      state: "RECONCILING",
      token_cleared: false,
      last_reason_code: "v6_lease_revocation_uncertain",
      updated_at_ms: this.clock.nowMs(),
    };
    const retryAfterMs =
      lastResult?.retryAfterSeconds !== null && lastResult?.retryAfterSeconds !== undefined
        ? lastResult.retryAfterSeconds * 1000
        : lastResult?.rateLimitResetEpoch !== null &&
            lastResult?.rateLimitResetEpoch !== undefined
          ? Math.max(0, lastResult.rateLimitResetEpoch * 1000 - this.clock.nowMs())
          : undefined;
    return {
      updatedSlot: reconciling,
      errorStatus: lastResult?.kind === "http_429_or_ratelimit_403" ? 429 : 503,
      diagnostic: "v6_lease_revocation_uncertain",
      retryAfterMs,
    };
  }

  async handleAcquire(
    env: WorkerEnv,
    body: unknown,
    control?: BrokerDispatchControl,
    rayId?: string,
  ): Promise<V6RouteResult> {
    if (!isObject(body)) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }
    const allowedKeys = new Set([
      "oidc_token",
      "pull_request",
      "head_sha",
      "capabilities",
      "operation_id",
      "predecessor_anchor_comment_id",
      "control_budget",
    ]);
    if (
      Object.keys(body).some((key) => !allowedKeys.has(key)) ||
      !isPositiveSafeInt(body.pull_request) ||
      typeof body.head_sha !== "string" ||
      !SHA1_HEX40_PATTERN.test(body.head_sha) ||
      (body.operation_id !== undefined &&
        (typeof body.operation_id !== "string" || !body.operation_id.trim())) ||
      (body.predecessor_anchor_comment_id !== undefined &&
        !isPositiveSafeInt(body.predecessor_anchor_comment_id))
    ) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }

    let requestedScope: { permissions: Record<string, "read" | "write">; hasWriteScope: boolean };
    try {
      requestedScope = mergePermissions(body.capabilities);
    } catch {
      return diagnosticResponse(400, "broker_capability_invalid", { rayId });
    }

    let edge: VerifiedEdgeContext;
    try {
      edge = await this.verifyTupleAndEdge(env, body.oidc_token, control);
    } catch (error) {
      return this.mapEdgeError("/github/v6/lease/acquire", error, rayId);
    }

    const pullRequest = body.pull_request;
    const headSha = body.head_sha;
    const predecessorAnchorId = body.predecessor_anchor_comment_id as number | undefined;
    const slotKey = buildCanonicalSlotKey({
      host: edge.host,
      appId: edge.appId,
      installationId: edge.installationId,
      baseRepoId: edge.baseRepoId,
      pullRequest,
    });

    if (edge.rollbackRepos.has(edge.claims.repository.toLowerCase())) {
      this.emitAudit("/github/v6/lease/acquire", slotKey, "refused", {
        diagnostic: "v6_lease_rollback_blocked",
      });
      return diagnosticResponse(403, "v6_lease_rollback_blocked", { rayId });
    }

    if (!this.clock.isTrusted()) {
      await this.withSlotLock(slotKey, async () => {
        const existing = await this.readSlot(slotKey);
        if (existing !== null) {
          await this.store.putSlot({
            ...existing,
            state: "HOLD",
            last_reason_code: "v6_lease_clock_untrusted",
            updated_at_ms: this.clock.nowMs(),
          });
        }
      });
      this.emitAudit("/github/v6/lease/acquire", slotKey, "refused", {
        diagnostic: "v6_lease_clock_untrusted",
      });
      return diagnosticResponse(409, "v6_lease_clock_untrusted", {
        slotState: "HOLD",
        rayId,
      });
    }

    // Reserve slot or validate reuse/blocking state atomically before external minting.
    let reservationOutcome:
      | { kind: "error"; result: V6RouteResult }
      | { kind: "reused"; slot: SlotRecord }
      | { kind: "verify_cold_start_anchor" }
      | {
          kind: "reserved";
          slot: SlotRecord;
          priorSlotCommentId: number | null;
          priorSlotRecordSha256: string | null;
          priorSlotLeaseEpoch: number;
          priorSlotHeadSha: string | null;
        };

    let crashedReservedSlot: SlotRecord | null = null;
    try {
      reservationOutcome = await this.withSlotLock(slotKey, async () => {
        const existing = await this.readSlot(slotKey);
        if (existing === null || existing.state === "IDLE") {
          // Guard against coordinator state loss if PR already holds a prior owner anchor.
          if (existing === null && predecessorAnchorId !== undefined) {
            return { kind: "verify_cold_start_anchor" };
          }
          if (
            existing !== null &&
            existing.owner_comment_id !== null &&
            predecessorAnchorId !== undefined &&
            predecessorAnchorId !== existing.owner_comment_id
          ) {
            return {
              kind: "error",
              result: diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
                slotState: existing.state,
                rayId,
              }),
            };
          }
          const generation = (existing?.generation ?? 0) + 1;
          const leaseEpoch = (existing?.lease_epoch ?? 0) + 1;
          const reservationId = this.generateReservationId();
          const reserved: SlotRecord = {
            slot_key: slotKey,
            state: "RESERVED",
            generation,
            lease_epoch: leaseEpoch,
            reservation_id: reservationId,
            owner_run_id: edge.claims.run_id,
            owner_run_attempt: Number(edge.claims.run_attempt),
            owner_head_sha: headSha,
            owner_workflow_sha: edge.claims.job_workflow_sha,
            owner_job: edge.claims.workflow,
            owner_comment_id: existing?.owner_comment_id ?? null,
            owner_record_sha256: existing?.owner_record_sha256 ?? null,
            acquired_at_ms: null,
            token_expires_at_ms: null,
            token_cleared: false,
            clock_margin_ms: edge.clockMarginMs,
            encrypted_escrow: null,
            last_reason_code: null,
            updated_at_ms: this.clock.nowMs(),
          };
          await this.store.putSlot(reserved);
          this.activeReservations.add(reservationId);
          return {
            kind: "reserved",
            slot: reserved,
            priorSlotCommentId: existing?.owner_comment_id ?? null,
            priorSlotRecordSha256: existing?.owner_record_sha256 ?? null,
            priorSlotLeaseEpoch: existing?.lease_epoch ?? 0,
            priorSlotHeadSha: existing?.owner_head_sha ?? null,
          };
        }

        if (existing.state === "RESERVED") {
          if (
            existing.reservation_id !== null &&
            this.activeReservations.has(existing.reservation_id)
          ) {
            return {
              kind: "error",
              result: diagnosticResponse(409, "v6_lease_reserved", {
                slotState: "RESERVED",
                rayId,
              }),
            };
          }
          crashedReservedSlot = existing;
          return {
            kind: "error",
            result: diagnosticResponse(409, "v6_lease_reserved", {
              slotState: "RESERVED",
              rayId,
            }),
          };
        }

        if (existing.state === "MINTING") {
          if (
            (existing.reservation_id !== null &&
              this.activeReservations.has(existing.reservation_id)) ||
            this.clock.nowMs() - existing.updated_at_ms < V6_MINTING_STALE_MS
          ) {
            return {
              kind: "error",
              result: diagnosticResponse(409, "v6_lease_reserved", {
                slotState: "MINTING",
                rayId,
              }),
            };
          }
          // Bounded minting timeout elapsed without completing: transition to RECONCILING, never re-mint.
          const reconciling: SlotRecord = {
            ...existing,
            state: "RECONCILING",
            last_reason_code: "v6_lease_mint_uncertain",
            updated_at_ms: this.clock.nowMs(),
          };
          await this.store.putSlot(reconciling);
          return {
            kind: "error",
            result: diagnosticResponse(409, "v6_lease_mint_uncertain", {
              slotState: "RECONCILING",
              rayId,
            }),
          };
        }

        if (existing.state === "ACTIVE") {
          const sameOwner =
            existing.owner_run_id === edge.claims.run_id &&
            existing.owner_run_attempt === Number(edge.claims.run_attempt) &&
            existing.owner_workflow_sha === edge.claims.job_workflow_sha;
          if (!sameOwner) {
            return {
              kind: "error",
              result: diagnosticResponse(409, "v6_lease_active_other_owner", {
                slotState: "ACTIVE",
                rayId,
              }),
            };
          }
          if (existing.owner_head_sha !== headSha) {
            return {
              kind: "error",
              result: diagnosticResponse(409, "v6_lease_head_changed", {
                slotState: "ACTIVE",
                rayId,
              }),
            };
          }
          if (
            existing.encrypted_escrow === null ||
            !isPositiveSafeInt(existing.token_expires_at_ms)
          ) {
            const held: SlotRecord = {
              ...existing,
              state: "HOLD",
              last_reason_code: "v6_lease_escrow_lost",
              updated_at_ms: this.clock.nowMs(),
            };
            await this.store.putSlot(held);
            return {
              kind: "error",
              result: diagnosticResponse(409, "v6_lease_escrow_lost", {
                slotState: "HOLD",
                rayId,
              }),
            };
          }
          if (this.clock.nowMs() >= existing.token_expires_at_ms) {
            return {
              kind: "error",
              result: diagnosticResponse(409, "v6_lease_draining", {
                slotState: "ACTIVE",
                rayId,
              }),
            };
          }
          return { kind: "reused", slot: existing };
        }

        const code = stateConflictDiagnostic(existing.state, existing);
        return {
          kind: "error",
          result: diagnosticResponse(409, code, {
            slotState: existing.state,
            rayId,
          }),
        };
      });
    } catch (error) {
      if (error instanceof Error && error.message === "v6_lease_escrow_lost") {
        return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
      }
      throw error;
    }

    // Handle cold-start anchor verification when coordinator store has no row for this slot.
    if (reservationOutcome.kind === "verify_cold_start_anchor") {
      let verifiedAnchor: ParsedPrLedgerRecord | null = null;
      try {
        const verified = await this.verifyLedgerComment(
          edge,
          pullRequest,
          predecessorAnchorId!,
          "owner",
        );
        assertRecordMatchesSlot(verified.parsed.payload, { slot_key: slotKey });
        verifiedAnchor = verified.parsed;
      } catch {
        verifiedAnchor = null;
      }

      const persistedHold = await this.withSlotLock(slotKey, async () => {
        const current = await this.readSlot(slotKey);
        if (current !== null) {
          return { conflict: true, slot: current };
        }
        const held: SlotRecord = {
          slot_key: slotKey,
          state: "HOLD",
          generation:
            verifiedAnchor !== null ? (verifiedAnchor.payload.generation as number) : 1,
          lease_epoch:
            verifiedAnchor !== null ? (verifiedAnchor.payload.lease_epoch as number) : 1,
          reservation_id: null,
          owner_run_id: edge.claims.run_id,
          owner_run_attempt: Number(edge.claims.run_attempt),
          owner_head_sha: headSha,
          owner_workflow_sha: edge.claims.job_workflow_sha,
          owner_job: edge.claims.workflow,
          owner_comment_id: predecessorAnchorId!,
          owner_record_sha256: verifiedAnchor?.sha256 ?? null,
          acquired_at_ms:
            verifiedAnchor !== null
              ? parsePositiveTimestampMs(verifiedAnchor.payload.acquired_at)
              : null,
          token_expires_at_ms: null,
          token_cleared: true,
          clock_margin_ms: edge.clockMarginMs,
          encrypted_escrow: null,
          last_reason_code: "v6_lease_slot_continuity_unproved",
          updated_at_ms: this.clock.nowMs(),
        };
        await this.store.putSlot(held);
        return { conflict: false, slot: held };
      });
      return diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
        slotState: persistedHold.slot.state,
        rayId,
      });
    }

    // Handle crash-before-mint recovery (`RESERVED` left behind by a dead engine instance).
    if (crashedReservedSlot !== null) {
      const stale = crashedReservedSlot as SlotRecord;
      const sameCrashedOwner =
        stale.owner_run_id === edge.claims.run_id &&
        stale.owner_run_attempt === Number(edge.claims.run_attempt);
      let attemptTerminal = false;
      if (stale.owner_run_id && stale.owner_run_attempt) {
        const attemptInfo = await this.provider.workflowRunAttempt(
          edge.claims.repository,
          stale.owner_run_id,
          String(stale.owner_run_attempt),
          edge.readToken,
        );
        attemptTerminal =
          attemptInfo !== null &&
          attemptInfo.status === "completed" &&
          attemptInfo.conclusion !== null;
      }
      const staleElapsed = this.clock.nowMs() - stale.updated_at_ms >= V6_RESERVED_STALE_MS;
      const nonMintProofOk = attemptTerminal || (sameCrashedOwner && staleElapsed);
      if (!nonMintProofOk) {
        this.emitAudit("/github/v6/lease/acquire", slotKey, "refused", {
          state: "RESERVED",
          diagnostic: "v6_lease_reserved",
        });
        return diagnosticResponse(409, "v6_lease_reserved", {
          slotState: "RESERVED",
          rayId,
        });
      }
      reservationOutcome = await this.withSlotLock(slotKey, async () => {
        const current = await this.readSlot(slotKey);
        if (
          current === null ||
          current.state !== "RESERVED" ||
          current.reservation_id !== stale.reservation_id ||
          (current.reservation_id !== null && this.activeReservations.has(current.reservation_id))
        ) {
          return {
            kind: "error",
            result: diagnosticResponse(409, "v6_lease_reserved", {
              slotState: current?.state,
              rayId,
            }),
          };
        }
        const idle: SlotRecord = {
          ...current,
          state: "IDLE",
          reservation_id: null,
          last_reason_code: null,
          updated_at_ms: this.clock.nowMs(),
        };
        await this.store.putSlot(idle);
        const generation = idle.generation + 1;
        const leaseEpoch = idle.lease_epoch + 1;
        const reservationId = this.generateReservationId();
        const reserved: SlotRecord = {
          ...idle,
          state: "RESERVED",
          generation,
          lease_epoch: leaseEpoch,
          reservation_id: reservationId,
          owner_run_id: edge.claims.run_id,
          owner_run_attempt: Number(edge.claims.run_attempt),
          owner_head_sha: headSha,
          owner_workflow_sha: edge.claims.job_workflow_sha,
          owner_job: edge.claims.workflow,
          updated_at_ms: this.clock.nowMs(),
        };
        await this.store.putSlot(reserved);
        this.activeReservations.add(reservationId);
        return {
          kind: "reserved",
          slot: reserved,
          priorSlotCommentId: idle.owner_comment_id,
          priorSlotRecordSha256: idle.owner_record_sha256,
          priorSlotLeaseEpoch: idle.lease_epoch,
          priorSlotHeadSha: idle.owner_head_sha,
        };
      });
    }

    if (reservationOutcome.kind === "error") {
      this.emitAudit("/github/v6/lease/acquire", slotKey, "refused", {
        diagnostic: String((reservationOutcome.result.body.diagnostic as { code?: string })?.code ?? ""),
      });
      return reservationOutcome.result;
    }

    // `ACTIVE` same-owner reuse path: verify live head and unseal existing token without minting.
    if (reservationOutcome.kind === "reused") {
      const slot = reservationOutcome.slot;
      const prBinding = await this.provider.pullRequestBinding(
        edge.claims.repository,
        pullRequest,
        edge.readToken,
      );
      if (prBinding === null || prBinding.base_repo_id !== edge.baseRepoId) {
        return diagnosticResponse(403, "v6_lease_unauthorized_tuple", { rayId });
      }
      if (prBinding.head_sha !== headSha) {
        return diagnosticResponse(409, "v6_lease_head_changed", {
          slotState: "ACTIVE",
          rayId,
        });
      }
      let decryptedToken: string;
      try {
        decryptedToken = await edge.crypto.unsealToken(slot.encrypted_escrow, {
          slot_key: slot.slot_key,
          generation: slot.generation,
          reservation_id: slot.reservation_id!,
        });
      } catch {
        await this.withSlotLock(slotKey, async () => {
          const latest = await this.store.getSlot(slotKey);
          if (
            latest !== null &&
            latest.state === "ACTIVE" &&
            latest.generation === slot.generation &&
            latest.reservation_id === slot.reservation_id
          ) {
            await this.store.putSlot({
              ...latest,
              state: "HOLD",
              last_reason_code: "v6_lease_escrow_lost",
              updated_at_ms: this.clock.nowMs(),
            });
          }
        });
        return diagnosticResponse(409, "v6_lease_escrow_lost", {
          slotState: "HOLD",
          rayId,
        });
      }
      const attestationMac = await edge.crypto.signOwnerAttestation({
        slot_key: slot.slot_key,
        generation: slot.generation,
        lease_epoch: slot.lease_epoch,
        reservation_id: slot.reservation_id!,
        run_id: slot.owner_run_id!,
        run_attempt: slot.owner_run_attempt!,
        pr_head_sha: slot.owner_head_sha!,
        workflow_sha: slot.owner_workflow_sha!,
      });
      this.emitAudit("/github/v6/lease/acquire", slotKey, "success", {
        state: "ACTIVE",
        generation: slot.generation,
        lease_epoch: slot.lease_epoch,
      });
      return {
        status: 200,
        body: {
          slot_key: slot.slot_key,
          state: "ACTIVE",
          generation: slot.generation,
          lease_epoch: slot.lease_epoch,
          reservation_id: slot.reservation_id,
          token: decryptedToken,
          token_expires_at_ms: slot.token_expires_at_ms,
          app_id: edge.appId,
          installation_id: edge.installationId,
          base_repo_id: edge.baseRepoId,
          pull_request: pullRequest,
          head_sha: headSha,
          coordinator_attestation_mac: attestationMac,
        },
      };
    }

    // New reservation path (`RESERVED` -> pre-mint checks -> `MINTING` -> `ACTIVE`).
    const reservedSlot = reservationOutcome.slot;
    const myReservationId = reservedSlot.reservation_id!;
    const myGeneration = reservedSlot.generation;

    const releaseActiveReservation = () => {
      this.activeReservations.delete(myReservationId);
    };

    const abortReservedToIdle = async (
      status: number,
      code: V6DiagnosticCode,
    ): Promise<V6RouteResult> => {
      await this.withSlotLock(slotKey, async () => {
        const current = await this.store.getSlot(slotKey);
        if (
          current !== null &&
          current.state === "RESERVED" &&
          current.reservation_id === myReservationId &&
          current.generation === myGeneration
        ) {
          await this.store.putSlot({
            ...current,
            state: "IDLE",
            reservation_id: null,
            last_reason_code: code,
            updated_at_ms: this.clock.nowMs(),
          });
        }
      });
      this.emitAudit("/github/v6/lease/acquire", slotKey, "refused", {
        state: "IDLE",
        diagnostic: code,
      });
      return diagnosticResponse(status, code, { slotState: "IDLE", rayId });
    };

    try {
      // 1. Validate live PR binding + fork exclusion + head SHA.
      const prBinding = await this.provider.pullRequestBinding(
        edge.claims.repository,
        pullRequest,
        edge.readToken,
      );
      if (prBinding === null || prBinding.base_repo_id !== edge.baseRepoId || prBinding.state !== "open") {
        return await abortReservedToIdle(403, "v6_lease_unauthorized_tuple");
      }
      if (
        (prBinding.is_fork_pr || prBinding.head_repo_id !== prBinding.base_repo_id) &&
        (requestedScope.hasWriteScope || edge.claims.event_name !== "pull_request")
      ) {
        return await abortReservedToIdle(403, "v6_lease_unauthorized_tuple");
      }
      if (prBinding.head_sha !== headSha) {
        return await abortReservedToIdle(409, "v6_lease_head_changed");
      }

      // 2. Validate caller run_id / run_attempt is active and matches OIDC claims.
      const callerAttempt = await this.provider.workflowRunAttempt(
        edge.claims.repository,
        edge.claims.run_id,
        edge.claims.run_attempt,
        edge.readToken,
      );
      if (
        callerAttempt === null ||
        callerAttempt.run_id !== edge.claims.run_id ||
        callerAttempt.run_attempt !== Number(edge.claims.run_attempt) ||
        callerAttempt.status === "completed"
      ) {
        return await abortReservedToIdle(403, "v6_lease_unauthorized_tuple");
      }

      // 3. Validate optional predecessor_anchor_comment_id continuity against persisted slot state.
      if (predecessorAnchorId !== undefined) {
        let verifiedAnchor: ParsedPrLedgerRecord;
        try {
          const verified = await this.verifyLedgerComment(
            edge,
            pullRequest,
            predecessorAnchorId,
            "owner",
          );
          verifiedAnchor = verified.parsed;
          assertRecordMatchesSlot(verifiedAnchor.payload, {
            slot_key: slotKey,
            generation: myGeneration - 1,
            lease_epoch: reservationOutcome.priorSlotLeaseEpoch || undefined,
            owner_head_sha: reservationOutcome.priorSlotHeadSha,
          });
        } catch {
          return await abortReservedToIdle(409, "v6_lease_slot_continuity_unproved");
        }

        const existingInStore = await this.readSlot(slotKey);
        if (
          existingInStore === null ||
          (existingInStore.owner_comment_id !== null &&
            predecessorAnchorId !== existingInStore.owner_comment_id) ||
          (existingInStore.owner_record_sha256 !== null &&
            verifiedAnchor.sha256 !== existingInStore.owner_record_sha256)
        ) {
          return await abortReservedToIdle(409, "v6_lease_slot_continuity_unproved");
        }
      }

      // 4. Persist MINTING call marker BEFORE calling GitHub POST /access_tokens.
      const movedToMinting = await this.withSlotLock(slotKey, async () => {
        const current = await this.store.getSlot(slotKey);
        if (
          current === null ||
          current.state !== "RESERVED" ||
          current.reservation_id !== myReservationId ||
          current.generation !== myGeneration
        ) {
          return false;
        }
        await this.store.putSlot({
          ...current,
          state: "MINTING",
          updated_at_ms: this.clock.nowMs(),
        });
        return true;
      });
      if (!movedToMinting) {
        return diagnosticResponse(409, "v6_lease_reserved", { rayId });
      }

      // 5. Call GitHub POST /app/installations/{id}/access_tokens.
      let minted: {
        token: string;
        permissions: Record<string, string>;
        expiresAt: string;
        expiresAtMs: number;
      };
      try {
        minted = await this.provider.mintInstallationToken(
          edge.installationId,
          edge.claims.repository,
          requestedScope.permissions,
        );
      } catch (mintError) {
        const msg = mintError instanceof Error ? mintError.message : "unknown";
        const deterministic4xx = /^github_capability_issue_failed_(400|401|403|404|422)$/.exec(msg);
        const rateLimit429 = msg === "github_capability_issue_failed_429";
        if (deterministic4xx || rateLimit429) {
          await this.withSlotLock(slotKey, async () => {
            const current = await this.store.getSlot(slotKey);
            if (
              current !== null &&
              current.state === "MINTING" &&
              current.reservation_id === myReservationId &&
              current.generation === myGeneration
            ) {
              await this.store.putSlot({
                ...current,
                state: "IDLE",
                reservation_id: null,
                last_reason_code: rateLimit429 ? null : "v6_lease_unauthorized_tuple",
                updated_at_ms: this.clock.nowMs(),
              });
            }
          });
          return diagnosticResponse(
            rateLimit429 ? 429 : 403,
            "v6_lease_unauthorized_tuple",
            { slotState: "IDLE", rayId },
          );
        }
        // Uncertain outcome (timeout / 5xx / transport error): transition MINTING -> RECONCILING.
        // NEVER synthesize a 1-hour expiry from timeout; token_expires_at_ms remains null.
        await this.withSlotLock(slotKey, async () => {
          const current = await this.store.getSlot(slotKey);
          if (
            current !== null &&
            current.state === "MINTING" &&
            current.reservation_id === myReservationId &&
            current.generation === myGeneration
          ) {
            await this.store.putSlot({
              ...current,
              state: "RECONCILING",
              token_expires_at_ms: null,
              encrypted_escrow: null,
              last_reason_code: "v6_lease_mint_uncertain",
              updated_at_ms: this.clock.nowMs(),
            });
          }
        });
        this.emitAudit("/github/v6/lease/acquire", slotKey, "refused", {
          state: "RECONCILING",
          diagnostic: "v6_lease_mint_uncertain",
        });
        return diagnosticResponse(503, "v6_lease_mint_uncertain", {
          slotState: "RECONCILING",
          rayId,
        });
      }

      // 6. Verify exact granted permissions and clock/expiry margin.
      if (!hasExactGrantedPermissions(minted.permissions, requestedScope.permissions)) {
        await this.provider.revokeInstallationToken(minted.token).catch(() => undefined);
        await this.withSlotLock(slotKey, async () => {
          const current = await this.store.getSlot(slotKey);
          if (
            current !== null &&
            current.state === "MINTING" &&
            current.reservation_id === myReservationId &&
            current.generation === myGeneration
          ) {
            await this.store.putSlot({
              ...current,
              state: "HOLD",
              last_reason_code: "v6_lease_unauthorized_tuple",
              updated_at_ms: this.clock.nowMs(),
            });
          }
        });
        return diagnosticResponse(403, "v6_lease_unauthorized_tuple", {
          slotState: "HOLD",
          rayId,
        });
      }

      const nowMs = this.clock.nowMs();
      if (
        !this.clock.isTrusted() ||
        !isPositiveSafeInt(minted.expiresAtMs) ||
        minted.expiresAtMs <= nowMs + edge.clockMarginMs
      ) {
        const rev = await this.provider
          .revokeInstallationToken(minted.token)
          .catch(() => null);
        await this.withSlotLock(slotKey, async () => {
          const current = await this.store.getSlot(slotKey);
          if (
            current !== null &&
            current.state === "MINTING" &&
            current.reservation_id === myReservationId &&
            current.generation === myGeneration
          ) {
            await this.store.putSlot({
              ...current,
              state: "HOLD",
              token_expires_at_ms:
                rev?.kind === "http_204"
                  ? null
                  : isPositiveSafeInt(minted.expiresAtMs)
                    ? minted.expiresAtMs
                    : null,
              last_reason_code: "v6_lease_clock_untrusted",
              updated_at_ms: this.clock.nowMs(),
            });
          }
        });
        return diagnosticResponse(409, "v6_lease_clock_untrusted", {
          slotState: "HOLD",
          rayId,
        });
      }

      // 7. Seal token escrow and compute owner attestation MAC.
      let encryptedEscrow: EncryptedEscrow;
      let attestationMac: string;
      try {
        encryptedEscrow = await edge.crypto.sealToken(minted.token, {
          slot_key: slotKey,
          generation: myGeneration,
          reservation_id: myReservationId,
        });
        attestationMac = await edge.crypto.signOwnerAttestation({
          slot_key: slotKey,
          generation: myGeneration,
          lease_epoch: reservedSlot.lease_epoch,
          reservation_id: myReservationId,
          run_id: edge.claims.run_id,
          run_attempt: Number(edge.claims.run_attempt),
          pr_head_sha: headSha,
          workflow_sha: edge.claims.job_workflow_sha,
        });
      } catch {
        await this.provider.revokeInstallationToken(minted.token).catch(() => undefined);
        await this.withSlotLock(slotKey, async () => {
          const current = await this.store.getSlot(slotKey);
          if (
            current !== null &&
            current.state === "MINTING" &&
            current.reservation_id === myReservationId &&
            current.generation === myGeneration
          ) {
            await this.store.putSlot({
              ...current,
              state: "HOLD",
              token_expires_at_ms: minted.expiresAtMs,
              last_reason_code: "v6_lease_escrow_lost",
              updated_at_ms: this.clock.nowMs(),
            });
          }
        });
        return diagnosticResponse(409, "v6_lease_escrow_lost", {
          slotState: "HOLD",
          rayId,
        });
      }

      // 8. Recheck (reservation_id, generation) and persist ACTIVE BEFORE returning token.
      let fencedResult:
        | { activated: true; slot: SlotRecord }
        | { activated: false; currentSlot: SlotRecord | null };
      try {
        fencedResult = await this.withSlotLock(slotKey, async () => {
          const current = await this.store.getSlot(slotKey);
          if (
            current === null ||
            current.state !== "MINTING" ||
            current.reservation_id !== myReservationId ||
            current.generation !== myGeneration
          ) {
            return { activated: false, currentSlot: current };
          }
          const active: SlotRecord = {
            ...current,
            state: "ACTIVE",
            acquired_at_ms: this.clock.nowMs(),
            token_expires_at_ms: minted.expiresAtMs,
            token_cleared: false,
            encrypted_escrow: encryptedEscrow,
            last_reason_code: null,
            updated_at_ms: this.clock.nowMs(),
          };
          await this.store.putSlot(active);
          return { activated: true, slot: active };
        });
      } catch {
        // Storage failure while persisting escrow: revoke unescrowed token and fail closed.
        await this.provider.revokeInstallationToken(minted.token).catch(() => undefined);
        return diagnosticResponse(503, "v6_lease_escrow_lost", { rayId });
      }

      if (!fencedResult.activated) {
        // Delayed stale continuation: fence and revoke late minted token without disclosure!
        await this.provider.revokeInstallationToken(minted.token).catch(() => undefined);
        const currentState = fencedResult.currentSlot?.state ?? "IDLE";
        const code =
          fencedResult.currentSlot === null
            ? "v6_lease_mint_uncertain"
            : stateConflictDiagnostic(currentState, fencedResult.currentSlot);
        this.emitAudit("/github/v6/lease/acquire", slotKey, "refused", {
          state: currentState,
          diagnostic: code,
        });
        return diagnosticResponse(409, code, { slotState: currentState, rayId });
      }

      this.emitAudit("/github/v6/lease/acquire", slotKey, "success", {
        state: "ACTIVE",
        generation: fencedResult.slot.generation,
        lease_epoch: fencedResult.slot.lease_epoch,
      });

      return {
        status: 200,
        body: {
          slot_key: fencedResult.slot.slot_key,
          state: "ACTIVE",
          generation: fencedResult.slot.generation,
          lease_epoch: fencedResult.slot.lease_epoch,
          reservation_id: fencedResult.slot.reservation_id,
          token: minted.token,
          token_expires_at_ms: minted.expiresAtMs,
          app_id: edge.appId,
          installation_id: edge.installationId,
          base_repo_id: edge.baseRepoId,
          pull_request: pullRequest,
          head_sha: headSha,
          coordinator_attestation_mac: attestationMac,
        },
      };
    } finally {
      releaseActiveReservation();
    }
  }

  async handleDrain(
    env: WorkerEnv,
    body: unknown,
    control?: BrokerDispatchControl,
    rayId?: string,
  ): Promise<V6RouteResult> {
    if (!isObject(body)) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }
    const allowedKeys = new Set([
      "oidc_token",
      "slot_key",
      "expected_generation",
      "expected_lease_epoch",
      "predecessor_anchor_comment_id",
      "control_budget",
    ]);
    if (
      Object.keys(body).some((key) => !allowedKeys.has(key)) ||
      typeof body.slot_key !== "string" ||
      (body.expected_generation !== undefined && !isNonNegativeSafeInt(body.expected_generation)) ||
      (body.expected_lease_epoch !== undefined &&
        !isNonNegativeSafeInt(body.expected_lease_epoch)) ||
      (body.predecessor_anchor_comment_id !== undefined &&
        !isPositiveSafeInt(body.predecessor_anchor_comment_id)) ||
      (body.predecessor_anchor_comment_id !== undefined &&
        body.expected_generation === undefined)
    ) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }

    let edge: VerifiedEdgeContext;
    let slotKey: string;
    let pullRequest: number;
    try {
      edge = await this.verifyTupleAndEdge(env, body.oidc_token, control);
      ({ slotKey, pullRequest } = this.validateSlotKeyForEdge(body.slot_key, edge));
    } catch (error) {
      return this.mapEdgeError("/github/v6/lease/drain", error, rayId);
    }

    const expectedGen = body.expected_generation as number | undefined;
    const expectedEpoch = body.expected_lease_epoch as number | undefined;
    const anchorCommentId = body.predecessor_anchor_comment_id as number | undefined;

    let verifiedAnchorRecord: ParsedPrLedgerRecord | null = null;
    if (anchorCommentId !== undefined) {
      try {
        const verified = await this.verifyLedgerComment(edge, pullRequest, anchorCommentId, "owner");
        assertRecordMatchesSlot(verified.parsed.payload, {
          slot_key: slotKey,
          generation: expectedGen,
          lease_epoch: expectedEpoch,
        });
        verifiedAnchorRecord = verified.parsed;
      } catch {
        try {
          await this.withSlotLock(slotKey, async () => {
            const existing = await this.readSlot(slotKey);
            if (existing !== null && existing.state !== "IDLE") {
              await this.store.putSlot({
                ...existing,
                state: "HOLD",
                last_reason_code: "v6_lease_slot_continuity_unproved",
                updated_at_ms: this.clock.nowMs(),
              });
            }
          });
        } catch (err) {
          if (err instanceof Error && err.message === "v6_lease_escrow_lost") {
            return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
          }
          throw err;
        }
        return diagnosticResponse(409, "v6_lease_slot_continuity_unproved", { rayId });
      }
    }

    let drainPrep:
      | { kind: "error"; result: V6RouteResult }
      | { kind: "already_idle"; generation: number; leaseEpoch: number }
      | {
          kind: "proceed";
          slot: SlotRecord;
          snapshot: {
            state: CoordinatorState;
            generation: number;
            lease_epoch: number;
            reservation_id: string | null;
            owner_run_id: string | null;
            owner_run_attempt: number | null;
          };
        };

    try {
      drainPrep = await this.withSlotLock(slotKey, async () => {
        const slot = await this.readSlot(slotKey);
        if (slot === null || slot.state === "IDLE") {
          if (expectedGen !== undefined && expectedGen !== (slot?.generation ?? 0)) {
            return {
              kind: "error" as const,
              result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
                slotState: "IDLE",
                rayId,
              }),
            };
          }
          return {
            kind: "already_idle" as const,
            generation: slot?.generation ?? 0,
            leaseEpoch: slot?.lease_epoch ?? 0,
          };
        }
        if (
          (expectedGen !== undefined && expectedGen !== slot.generation) ||
          (expectedEpoch !== undefined && expectedEpoch !== slot.lease_epoch)
        ) {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
              slotState: slot.state,
              rayId,
            }),
          };
        }
        if (verifiedAnchorRecord !== null) {
          try {
            assertRecordMatchesSlot(verifiedAnchorRecord.payload, {
              slot_key: slotKey,
              generation: slot.generation,
              lease_epoch: slot.lease_epoch,
              owner_head_sha: slot.owner_head_sha,
            });
          } catch {
            await this.store.putSlot({
              ...slot,
              state: "HOLD",
              last_reason_code: "v6_lease_slot_continuity_unproved",
              updated_at_ms: this.clock.nowMs(),
            });
            return {
              kind: "error" as const,
              result: diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
                slotState: "HOLD",
                rayId,
              }),
            };
          }
        }
        if (slot.state === "HOLD") {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_hold", { slotState: "HOLD", rayId }),
          };
        }
        if (slot.state === "RESERVED") {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_reserved", {
              slotState: "RESERVED",
              rayId,
            }),
          };
        }
        if (slot.state === "MINTING") {
          if (
            (slot.reservation_id !== null && this.activeReservations.has(slot.reservation_id)) ||
            this.clock.nowMs() - slot.updated_at_ms < V6_MINTING_STALE_MS
          ) {
            return {
              kind: "error" as const,
              result: diagnosticResponse(409, "v6_lease_draining", {
                slotState: "MINTING",
                rayId,
              }),
            };
          }
          const reconciling: SlotRecord = {
            ...slot,
            state: "RECONCILING",
            last_reason_code: "v6_lease_mint_uncertain",
            updated_at_ms: this.clock.nowMs(),
          };
          await this.store.putSlot(reconciling);
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_reconciling", {
              slotState: "RECONCILING",
              rayId,
            }),
          };
        }
        if (
          slot.state === "RECONCILING" &&
          (slot.token_cleared ||
            (slot.encrypted_escrow === null &&
              !(
                isPositiveSafeInt(slot.token_expires_at_ms) &&
                this.clock.nowMs() >= slot.token_expires_at_ms + slot.clock_margin_ms
              )))
        ) {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_reconciling", {
              slotState: "RECONCILING",
              rayId,
            }),
          };
        }
        return {
          kind: "proceed" as const,
          slot,
          snapshot: {
            state: slot.state,
            generation: slot.generation,
            lease_epoch: slot.lease_epoch,
            reservation_id: slot.reservation_id,
            owner_run_id: slot.owner_run_id,
            owner_run_attempt: slot.owner_run_attempt,
          },
        };
      });
    } catch (error) {
      if (error instanceof Error && error.message === "v6_lease_escrow_lost") {
        return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
      }
      throw error;
    }

    if (drainPrep.kind === "error") {
      return drainPrep.result;
    }
    if (drainPrep.kind === "already_idle") {
      return {
        status: 200,
        body: {
          slot_key: slotKey,
          state: "IDLE",
          generation: drainPrep.generation,
          lease_epoch: drainPrep.leaseEpoch,
          token_cleared: true,
          revocation_status: "already_idle",
        },
      };
    }

    const { slot, snapshot } = drainPrep;
    const isSameOwner =
      slot.owner_run_id === edge.claims.run_id &&
      slot.owner_run_attempt === Number(edge.claims.run_attempt);

    // Predecessor terminal check BEFORE completing takeover revocation.
    if (!isSameOwner && slot.owner_run_id !== null && slot.owner_run_attempt !== null) {
      const predecessorAttempt = await this.provider.workflowRunAttempt(
        edge.claims.repository,
        slot.owner_run_id,
        String(slot.owner_run_attempt),
        edge.readToken,
      );
      if (
        predecessorAttempt === null ||
        predecessorAttempt.status !== "completed" ||
        predecessorAttempt.conclusion === null
      ) {
        this.emitAudit("/github/v6/lease/drain", slotKey, "refused", {
          state: slot.state,
          diagnostic: "v6_lease_predecessor_not_terminal",
        });
        return diagnosticResponse(409, "v6_lease_predecessor_not_terminal", {
          slotState: slot.state,
          rayId,
        });
      }
    }

    // Recheck exact captured snapshot before persisting DRAINING.
    const drainingSlot = await this.withSlotLock(slotKey, async () => {
      const current = await this.readSlot(slotKey);
      if (
        current === null ||
        current.state !== snapshot.state ||
        current.generation !== snapshot.generation ||
        current.lease_epoch !== snapshot.lease_epoch ||
        current.reservation_id !== snapshot.reservation_id ||
        current.owner_run_id !== snapshot.owner_run_id ||
        current.owner_run_attempt !== snapshot.owner_run_attempt ||
        (current.state !== "ACTIVE" &&
          current.state !== "DRAINING" &&
          current.state !== "RECONCILING")
      ) {
        return null;
      }
      const next: SlotRecord = {
        ...current,
        state: "DRAINING",
        owner_comment_id: anchorCommentId ?? current.owner_comment_id,
        owner_record_sha256: verifiedAnchorRecord?.sha256 ?? current.owner_record_sha256,
        updated_at_ms: this.clock.nowMs(),
      };
      await this.store.putSlot(next);
      return next;
    });

    if (drainingSlot === null) {
      return diagnosticResponse(409, "v6_lease_draining", { rayId });
    }

    // Gate 1 (Token Clearance): ALWAYS transitions DRAINING -> RECONCILING | HOLD (never IDLE).
    const revOutcome = await this.executeRevocation(drainingSlot, edge, "RECONCILING");
    const applied = await this.withSlotLock(slotKey, async () => {
      const latest = await this.readSlot(slotKey);
      if (
        latest === null ||
        latest.state !== "DRAINING" ||
        latest.generation !== drainingSlot.generation ||
        latest.lease_epoch !== drainingSlot.lease_epoch ||
        latest.reservation_id !== drainingSlot.reservation_id
      ) {
        return { conflict: true, state: latest?.state ?? ("IDLE" as CoordinatorState) };
      }
      await this.store.putSlot(revOutcome.updatedSlot);
      return { conflict: false, state: revOutcome.updatedSlot.state };
    });

    if (applied.conflict) {
      return diagnosticResponse(409, "v6_lease_draining", {
        slotState: applied.state,
        rayId,
      });
    }

    if (revOutcome.errorStatus !== undefined && revOutcome.diagnostic !== undefined) {
      this.emitAudit("/github/v6/lease/drain", slotKey, "refused", {
        state: revOutcome.updatedSlot.state,
        diagnostic: revOutcome.diagnostic,
      });
      return diagnosticResponse(revOutcome.errorStatus, revOutcome.diagnostic, {
        slotState: revOutcome.updatedSlot.state,
        retryAfterMs: revOutcome.retryAfterMs,
        rayId,
      });
    }

    this.emitAudit("/github/v6/lease/drain", slotKey, "success", {
      state: revOutcome.updatedSlot.state,
      generation: revOutcome.updatedSlot.generation,
      lease_epoch: revOutcome.updatedSlot.lease_epoch,
    });
    return {
      status: 200,
      body: {
        slot_key: slotKey,
        state: revOutcome.updatedSlot.state,
        generation: revOutcome.updatedSlot.generation,
        lease_epoch: revOutcome.updatedSlot.lease_epoch,
        token_cleared: revOutcome.updatedSlot.token_cleared,
        revocation_status: revOutcome.revocationStatus ?? "confirmed_revoked",
      },
    };
  }

  async handleRelease(
    env: WorkerEnv,
    body: unknown,
    control?: BrokerDispatchControl,
    rayId?: string,
  ): Promise<V6RouteResult> {
    if (!isObject(body)) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }
    const allowedKeys = new Set([
      "oidc_token",
      "slot_key",
      "generation",
      "lease_epoch",
      "reservation_id",
      "control_budget",
    ]);
    if (
      Object.keys(body).some((key) => !allowedKeys.has(key)) ||
      typeof body.slot_key !== "string" ||
      !isPositiveSafeInt(body.generation) ||
      !isPositiveSafeInt(body.lease_epoch) ||
      typeof body.reservation_id !== "string" ||
      !body.reservation_id.trim()
    ) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }

    let edge: VerifiedEdgeContext;
    let slotKey: string;
    try {
      edge = await this.verifyTupleAndEdge(env, body.oidc_token, control);
      ({ slotKey } = this.validateSlotKeyForEdge(body.slot_key, edge));
    } catch (error) {
      return this.mapEdgeError("/github/v6/lease/release", error, rayId);
    }

    const reqGeneration = body.generation;
    const reqLeaseEpoch = body.lease_epoch;
    const reqReservationId = body.reservation_id;

    let prep:
      | { kind: "error"; result: V6RouteResult }
      | { kind: "proceed"; slot: SlotRecord };
    try {
      prep = await this.withSlotLock(slotKey, async () => {
        const slot = await this.readSlot(slotKey);
        if (slot === null) {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
              slotState: "IDLE",
              rayId,
            }),
          };
        }
        if (slot.state !== "ACTIVE") {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, stateConflictDiagnostic(slot.state, slot), {
              slotState: slot.state,
              rayId,
            }),
          };
        }
        if (
          slot.owner_run_id !== edge.claims.run_id ||
          slot.owner_run_attempt !== Number(edge.claims.run_attempt)
        ) {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_active_other_owner", {
              slotState: "ACTIVE",
              rayId,
            }),
          };
        }
        if (
          slot.generation !== reqGeneration ||
          slot.lease_epoch !== reqLeaseEpoch ||
          slot.reservation_id !== reqReservationId
        ) {
          return {
            kind: "error" as const,
            result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
              slotState: "ACTIVE",
              rayId,
            }),
          };
        }
        const draining: SlotRecord = {
          ...slot,
          state: "DRAINING",
          updated_at_ms: this.clock.nowMs(),
        };
        await this.store.putSlot(draining);
        return { kind: "proceed" as const, slot: draining };
      });
    } catch (error) {
      if (error instanceof Error && error.message === "v6_lease_escrow_lost") {
        return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
      }
      throw error;
    }

    if (prep.kind === "error") {
      return prep.result;
    }

    // Gate 1 (Token Clearance): ALWAYS transitions DRAINING -> RECONCILING | HOLD (never IDLE).
    const revOutcome = await this.executeRevocation(prep.slot, edge, "RECONCILING");
    const releaseApplied = await this.withSlotLock(slotKey, async () => {
      const latest = await this.readSlot(slotKey);
      if (
        latest === null ||
        latest.state !== "DRAINING" ||
        latest.generation !== prep.slot.generation ||
        latest.lease_epoch !== prep.slot.lease_epoch ||
        latest.reservation_id !== prep.slot.reservation_id
      ) {
        return { conflict: true, state: latest?.state ?? ("IDLE" as CoordinatorState) };
      }
      await this.store.putSlot(revOutcome.updatedSlot);
      return { conflict: false, state: revOutcome.updatedSlot.state };
    });

    if (releaseApplied.conflict) {
      return diagnosticResponse(409, "v6_lease_draining", {
        slotState: releaseApplied.state,
        rayId,
      });
    }

    if (revOutcome.errorStatus !== undefined && revOutcome.diagnostic !== undefined) {
      this.emitAudit("/github/v6/lease/release", slotKey, "refused", {
        state: revOutcome.updatedSlot.state,
        diagnostic: revOutcome.diagnostic,
      });
      return diagnosticResponse(revOutcome.errorStatus, revOutcome.diagnostic, {
        slotState: revOutcome.updatedSlot.state,
        retryAfterMs: revOutcome.retryAfterMs,
        rayId,
      });
    }

    this.emitAudit("/github/v6/lease/release", slotKey, "success", {
      state: revOutcome.updatedSlot.state,
      generation: revOutcome.updatedSlot.generation,
      lease_epoch: revOutcome.updatedSlot.lease_epoch,
    });
    return {
      status: 200,
      body: {
        slot_key: slotKey,
        state: revOutcome.updatedSlot.state,
        generation: revOutcome.updatedSlot.generation,
        lease_epoch: revOutcome.updatedSlot.lease_epoch,
        token_cleared: revOutcome.updatedSlot.token_cleared,
        revocation_status: revOutcome.revocationStatus ?? "confirmed_revoked",
      },
    };
  }

  private parseReconciliationEvidence(raw: Record<string, unknown>): {
    old_generation: number;
    old_owner_comment_id: number | null;
    intent_resolutions: Array<{
      operation_id: string;
      intent_comment_id: number;
      resolution: "receipt_present" | "adopted_effect" | "proved_not_submitted" | "ambiguous_hold";
      receipt_comment_id?: number;
      native_effect_kind?: "pull_request_review" | "pull_request_review_comment" | "issue_comment" | "check_run";
      native_effect_id?: number;
      proof_comment_id?: number;
    }>;
  } | null {
    const allowedEvidenceKeys = new Set([
      "old_generation",
      "old_owner_comment_id",
      "intent_resolutions",
    ]);
    if (
      Object.keys(raw).some((key) => !allowedEvidenceKeys.has(key)) ||
      !isPositiveSafeInt(raw.old_generation) ||
      (raw.old_owner_comment_id !== undefined &&
        raw.old_owner_comment_id !== null &&
        !isPositiveSafeInt(raw.old_owner_comment_id)) ||
      !Array.isArray(raw.intent_resolutions)
    ) {
      return null;
    }

    const allowedItemKeys = new Set([
      "operation_id",
      "intent_comment_id",
      "resolution",
      "receipt_comment_id",
      "native_effect_kind",
      "native_effect_id",
      "proof_comment_id",
    ]);
    const seenOps = new Set<string>();
    const seenIntentComments = new Set<number>();
    const parsedItems: Array<{
      operation_id: string;
      intent_comment_id: number;
      resolution: "receipt_present" | "adopted_effect" | "proved_not_submitted" | "ambiguous_hold";
      receipt_comment_id?: number;
      native_effect_kind?: "pull_request_review" | "pull_request_review_comment" | "issue_comment" | "check_run";
      native_effect_id?: number;
      proof_comment_id?: number;
    }> = [];

    for (const item of raw.intent_resolutions) {
      if (
        !isObject(item) ||
        Object.keys(item).some((key) => !allowedItemKeys.has(key)) ||
        typeof item.operation_id !== "string" ||
        !item.operation_id.trim() ||
        !isPositiveSafeInt(item.intent_comment_id) ||
        typeof item.resolution !== "string" ||
        !ALLOWED_INTENT_RESOLUTIONS.has(item.resolution)
      ) {
        return null;
      }
      if (seenOps.has(item.operation_id) || seenIntentComments.has(item.intent_comment_id)) {
        return null;
      }
      seenOps.add(item.operation_id);
      seenIntentComments.add(item.intent_comment_id);

      const resolution = item.resolution as
        | "receipt_present"
        | "adopted_effect"
        | "proved_not_submitted"
        | "ambiguous_hold";
      if (resolution === "receipt_present" && !isPositiveSafeInt(item.receipt_comment_id)) {
        return null;
      }
      if (
        resolution === "adopted_effect" &&
        (typeof item.native_effect_kind !== "string" ||
          !ALLOWED_NATIVE_EFFECT_KINDS.has(item.native_effect_kind) ||
          !isPositiveSafeInt(item.native_effect_id))
      ) {
        return null;
      }
      if (resolution === "proved_not_submitted" && !isPositiveSafeInt(item.proof_comment_id)) {
        return null;
      }

      parsedItems.push({
        operation_id: item.operation_id,
        intent_comment_id: item.intent_comment_id,
        resolution,
        ...(isPositiveSafeInt(item.receipt_comment_id)
          ? { receipt_comment_id: item.receipt_comment_id }
          : {}),
        ...(typeof item.native_effect_kind === "string" &&
        ALLOWED_NATIVE_EFFECT_KINDS.has(item.native_effect_kind)
          ? {
              native_effect_kind: item.native_effect_kind as
                | "pull_request_review"
                | "pull_request_review_comment"
                | "issue_comment"
                | "check_run",
            }
          : {}),
        ...(isPositiveSafeInt(item.native_effect_id)
          ? { native_effect_id: item.native_effect_id }
          : {}),
        ...(isPositiveSafeInt(item.proof_comment_id)
          ? { proof_comment_id: item.proof_comment_id }
          : {}),
      });
    }

    return {
      old_generation: raw.old_generation,
      old_owner_comment_id: isPositiveSafeInt(raw.old_owner_comment_id)
        ? raw.old_owner_comment_id
        : null,
      intent_resolutions: parsedItems,
    };
  }

  private evidenceMatchesTerminalOutcome(
    outcome: string,
    items: Array<{ resolution: string }>,
  ): boolean {
    if (items.some((item) => item.resolution === "ambiguous_hold")) {
      return false;
    }
    if (outcome === "clean_receipts") {
      return items.every((item) => item.resolution === "receipt_present");
    }
    if (outcome === "adopted_missing_receipt") {
      return (
        items.length >= 1 &&
        items.every(
          (item) =>
            item.resolution === "receipt_present" ||
            item.resolution === "adopted_effect" ||
            item.resolution === "proved_not_submitted",
        ) &&
        items.some((item) => item.resolution === "adopted_effect")
      );
    }
    if (outcome === "proved_not_submitted") {
      return (
        items.length >= 1 &&
        items.every(
          (item) =>
            item.resolution === "receipt_present" || item.resolution === "proved_not_submitted",
        ) &&
        items.some((item) => item.resolution === "proved_not_submitted")
      );
    }
    return false;
  }

  private hasEffectEvidence(body: string, operationId: string, payloadSha256: string): boolean {
    const markerMatch = /<!--\s*reviewsensei:v6:effect\s+op=([^\s]+)\s+sha256=([0-9a-f]{64})\s*-->/.exec(
      body,
    );
    if (markerMatch !== null) {
      return markerMatch[1] === operationId && markerMatch[2] === payloadSha256;
    }
    return body.includes(operationId) && body.includes(payloadSha256);
  }

  private isAppBotAuthor(
    login: string,
    userType: string,
    botLogin: string,
    performedViaAppId: number | null,
    expectedAppId: number,
  ): boolean {
    return (
      userType === "Bot" &&
      login.toLowerCase() === botLogin.toLowerCase() &&
      (performedViaAppId === null || performedViaAppId === expectedAppId)
    );
  }

  private async verifyAdoptedNativeEffect(
    edge: VerifiedEdgeContext,
    pullRequest: number,
    botLogin: string,
    kind: "pull_request_review" | "pull_request_review_comment" | "issue_comment" | "check_run",
    effectId: number,
    operationId: string,
    payloadSha256: string,
    intentHeadSha: string,
  ): Promise<void> {
    if (kind === "pull_request_review") {
      const review = await this.provider.pullRequestReview(
        edge.claims.repository,
        pullRequest,
        effectId,
        edge.readToken,
      );
      if (
        review === null ||
        !this.isAppBotAuthor(
          review.login,
          review.userType,
          botLogin,
          review.performedViaAppId,
          edge.appId,
        ) ||
        review.state === "PENDING" ||
        (review.commit_id !== null && review.commit_id !== intentHeadSha) ||
        !this.hasEffectEvidence(review.body, operationId, payloadSha256)
      ) {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
      return;
    }
    if (kind === "pull_request_review_comment") {
      const reviewComment = await this.provider.pullRequestReviewComment(
        edge.claims.repository,
        pullRequest,
        effectId,
        edge.readToken,
      );
      if (
        reviewComment === null ||
        !this.isAppBotAuthor(
          reviewComment.login,
          reviewComment.userType,
          botLogin,
          reviewComment.performedViaAppId,
          edge.appId,
        ) ||
        (reviewComment.commit_id !== null &&
          reviewComment.commit_id !== intentHeadSha &&
          reviewComment.original_commit_id !== intentHeadSha) ||
        !this.hasEffectEvidence(reviewComment.body, operationId, payloadSha256)
      ) {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
      return;
    }
    if (kind === "issue_comment") {
      const issueComment = await this.provider.issueComment(
        edge.claims.repository,
        pullRequest,
        effectId,
        edge.readToken,
      );
      if (
        issueComment === null ||
        issueComment.userType !== "Bot" ||
        issueComment.login.toLowerCase() !== botLogin.toLowerCase() ||
        !this.hasEffectEvidence(issueComment.body, operationId, payloadSha256)
      ) {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
      return;
    }
    const run = await this.provider.checkRun(edge.claims.repository, effectId, edge.readToken);
    if (
      run === null ||
      (run.appId !== null && run.appId !== edge.appId) ||
      run.head_sha !== intentHeadSha
    ) {
      throw new Error("v6_lease_reconciliation_evidence_invalid");
    }
    const combinedOutput = [
      run.external_id ?? "",
      run.outputTitle ?? "",
      run.outputSummary ?? "",
      run.outputText ?? "",
    ].join("\n");
    if (!this.hasEffectEvidence(combinedOutput, operationId, payloadSha256)) {
      throw new Error("v6_lease_reconciliation_evidence_invalid");
    }
  }

  private async verifyReconciliationEvidence(
    edge: VerifiedEdgeContext,
    slotKey: string,
    pullRequest: number,
    slot: SlotRecord,
    evidence: NonNullable<ReturnType<V6PrCoordinatorEngine["parseReconciliationEvidence"]>>,
    anchorCommentId: number | undefined,
    reqHeadSha: string | undefined,
  ): Promise<{ oldOwnerSha256: string | null }> {
    if (
      anchorCommentId !== undefined &&
      evidence.old_owner_comment_id !== null &&
      anchorCommentId !== evidence.old_owner_comment_id
    ) {
      throw new Error("v6_lease_reconciliation_evidence_invalid");
    }

    let oldOwnerSha256: string | null = null;
    let verifiedOwnerAcquiredAtMs: number | null = null;
    if (evidence.old_owner_comment_id !== null) {
      if (
        slot.owner_comment_id !== null &&
        evidence.old_owner_comment_id !== slot.owner_comment_id
      ) {
        // Verify old_owner_comment_id directly against current generation (may have updated slot anchor).
      }
      try {
        const verifiedOldOwner = await this.verifyLedgerComment(
          edge,
          pullRequest,
          evidence.old_owner_comment_id,
          "owner",
        );
        assertRecordMatchesSlot(verifiedOldOwner.parsed.payload, {
          slot_key: slotKey,
          generation: slot.generation,
          lease_epoch: slot.lease_epoch,
          owner_head_sha: slot.owner_head_sha ?? reqHeadSha,
        });
        oldOwnerSha256 = verifiedOldOwner.parsed.sha256;
        verifiedOwnerAcquiredAtMs = parsePositiveTimestampMs(
          verifiedOldOwner.parsed.payload.acquired_at,
        );
      } catch {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
    } else if (slot.owner_comment_id !== null) {
      // `slot.owner_comment_id` may be a predecessor anchor inherited at acquire time (generation <= slot.generation).
      try {
        const verifiedPriorOwner = await this.verifyLedgerComment(
          edge,
          pullRequest,
          slot.owner_comment_id,
          "owner",
        );
        const priorGen = verifiedPriorOwner.parsed.payload.generation;
        if (
          verifiedPriorOwner.parsed.payload.slot_key !== slotKey ||
          !isPositiveSafeInt(priorGen) ||
          priorGen > slot.generation
        ) {
          throw new Error("v6_lease_reconciliation_evidence_invalid");
        }
        if (priorGen === slot.generation) {
          assertRecordMatchesSlot(verifiedPriorOwner.parsed.payload, {
            slot_key: slotKey,
            generation: slot.generation,
            lease_epoch: slot.lease_epoch,
            owner_head_sha: slot.owner_head_sha ?? reqHeadSha,
          });
        }
        oldOwnerSha256 = verifiedPriorOwner.parsed.sha256;
        verifiedOwnerAcquiredAtMs = parsePositiveTimestampMs(
          verifiedPriorOwner.parsed.payload.acquired_at,
        );
      } catch {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
    }

    const rawSinceMs =
      slot.acquired_at_ms ?? verifiedOwnerAcquiredAtMs ?? slot.updated_at_ms;
    const flooredSinceMs = Math.floor(Math.max(0, rawSinceMs - 2000) / 1000) * 1000;
    const sinceIso = new Date(flooredSinceMs).toISOString();
    const [botLogin, page] = await Promise.all([
      this.provider.appBotLogin(),
      this.provider.issueCommentsSince(edge.claims.repository, pullRequest, sinceIso, edge.readToken),
    ]);
    if (page.truncated) {
      throw new Error("v6_lease_reconciliation_evidence_invalid");
    }

    const discoveredIntents = new Map<number, string>();
    for (const comment of page.comments) {
      if (comment.userType !== "Bot" || comment.login.toLowerCase() !== botLogin.toLowerCase()) {
        continue;
      }
      if (!comment.body.includes("<!-- reviewsensei:v6:record ")) {
        continue;
      }
      const parsed = await parseAndVerifyPrLedgerMarker(comment.body);
      if (parsed === null) {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
      if (
        parsed.payload.schema === "reviewsensei:v6:intent:v1" &&
        parsed.payload.generation === evidence.old_generation
      ) {
        if (!isValidIntentRecordShape(parsed.payload)) {
          throw new Error("v6_lease_reconciliation_evidence_invalid");
        }
        discoveredIntents.set(comment.id, parsed.payload.operation_id as string);
      }
    }

    const resolutionByIntentId = new Map(
      evidence.intent_resolutions.map((item) => [item.intent_comment_id, item] as const),
    );
    for (const [intentCommentId, expectedOpId] of discoveredIntents.entries()) {
      const match = resolutionByIntentId.get(intentCommentId);
      if (match === undefined || match.operation_id !== expectedOpId) {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
    }

    for (const item of evidence.intent_resolutions) {
      let intentRecord: ParsedPrLedgerRecord;
      try {
        const verifiedIntent = await this.verifyLedgerComment(
          edge,
          pullRequest,
          item.intent_comment_id,
          "any",
        );
        intentRecord = verifiedIntent.parsed;
      } catch {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
      if (
        intentRecord.payload.schema !== "reviewsensei:v6:intent:v1" ||
        intentRecord.payload.operation_id !== item.operation_id ||
        intentRecord.payload.generation !== evidence.old_generation
      ) {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
      const intentPayloadSha = intentRecord.payload.payload_sha256 as string;
      const intentHeadSha = intentRecord.payload.pr_head_sha as string;
      const intentWriterEpoch = intentRecord.payload.writer_epoch as number;

      if (item.resolution === "receipt_present") {
        let receiptRecord: ParsedPrLedgerRecord;
        try {
          const verifiedReceipt = await this.verifyLedgerComment(
            edge,
            pullRequest,
            item.receipt_comment_id!,
            "any",
          );
          receiptRecord = verifiedReceipt.parsed;
        } catch {
          throw new Error("v6_lease_reconciliation_evidence_invalid");
        }
        if (
          receiptRecord.payload.schema !== "reviewsensei:v6:receipt:v1" ||
          receiptRecord.payload.operation_id !== item.operation_id ||
          receiptRecord.payload.payload_sha256 !== intentPayloadSha ||
          receiptRecord.payload.writer_epoch !== intentWriterEpoch
        ) {
          throw new Error("v6_lease_reconciliation_evidence_invalid");
        }
      } else if (item.resolution === "adopted_effect") {
        await this.verifyAdoptedNativeEffect(
          edge,
          pullRequest,
          botLogin,
          item.native_effect_kind!,
          item.native_effect_id!,
          item.operation_id,
          intentPayloadSha,
          intentHeadSha,
        );
      } else if (item.resolution === "proved_not_submitted") {
        let proofRecord: ParsedPrLedgerRecord;
        try {
          const verifiedProof = await this.verifyLedgerComment(
            edge,
            pullRequest,
            item.proof_comment_id!,
            "any",
          );
          proofRecord = verifiedProof.parsed;
        } catch {
          throw new Error("v6_lease_reconciliation_evidence_invalid");
        }
        if (
          proofRecord.payload.schema !== "reviewsensei:v6:non-submission:v1" ||
          proofRecord.payload.slot_key !== slotKey ||
          proofRecord.payload.generation !== evidence.old_generation ||
          proofRecord.payload.operation_id !== item.operation_id ||
          proofRecord.payload.payload_sha256 !== intentPayloadSha ||
          proofRecord.payload.pr_head_sha !== intentHeadSha
        ) {
          throw new Error("v6_lease_reconciliation_evidence_invalid");
        }
      } else {
        throw new Error("v6_lease_reconciliation_evidence_invalid");
      }
    }

    return { oldOwnerSha256 };
  }

  async handleCompleteReconcile(
    env: WorkerEnv,
    body: unknown,
    control?: BrokerDispatchControl,
    rayId?: string,
  ): Promise<V6RouteResult> {
    if (!isObject(body)) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }
    const allowedKeys = new Set([
      "oidc_token",
      "slot_key",
      "generation",
      "reconciliation_outcome",
      "reconciliation_evidence",
      "anchor_comment_id",
      "head_sha",
      "control_budget",
    ]);
    if (
      Object.keys(body).some((key) => !allowedKeys.has(key)) ||
      typeof body.slot_key !== "string" ||
      !isPositiveSafeInt(body.generation) ||
      typeof body.reconciliation_outcome !== "string" ||
      !ALLOWED_RECONCILE_OUTCOMES.has(body.reconciliation_outcome) ||
      !isObject(body.reconciliation_evidence) ||
      (body.anchor_comment_id !== undefined && !isPositiveSafeInt(body.anchor_comment_id)) ||
      (body.head_sha !== undefined &&
        (typeof body.head_sha !== "string" || !SHA1_HEX40_PATTERN.test(body.head_sha)))
    ) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }

    let edge: VerifiedEdgeContext;
    let slotKey: string;
    let pullRequest: number;
    try {
      edge = await this.verifyTupleAndEdge(env, body.oidc_token, control);
      ({ slotKey, pullRequest } = this.validateSlotKeyForEdge(body.slot_key, edge));
    } catch (error) {
      return this.mapEdgeError("/github/v6/lease/complete-reconcile", error, rayId);
    }

    const reqGeneration = body.generation;
    const outcome = body.reconciliation_outcome;
    const parsedEvidence = this.parseReconciliationEvidence(body.reconciliation_evidence);
    const anchorCommentId = body.anchor_comment_id as number | undefined;
    const reqHeadSha = body.head_sha as string | undefined;

    let initialSlot: SlotRecord | null;
    try {
      initialSlot = await this.withSlotLock(slotKey, async () => this.readSlot(slotKey));
    } catch (err) {
      if (err instanceof Error && err.message === "v6_lease_escrow_lost") {
        return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
      }
      throw err;
    }
    if (initialSlot === null) {
      return diagnosticResponse(409, "v6_lease_rollback_blocked", {
        slotState: "IDLE",
        rayId,
      });
    }
    if (initialSlot.state !== "RECONCILING") {
      return diagnosticResponse(409, stateConflictDiagnostic(initialSlot.state, initialSlot), {
        slotState: initialSlot.state,
        rayId,
      });
    }
    if (initialSlot.generation !== reqGeneration) {
      return diagnosticResponse(409, "v6_lease_rollback_blocked", {
        slotState: "RECONCILING",
        rayId,
      });
    }

    let verifiedAnchor: ParsedPrLedgerRecord | null = null;
    if (anchorCommentId !== undefined) {
      try {
        const verified = await this.verifyLedgerComment(edge, pullRequest, anchorCommentId, "any");
        verifiedAnchor = verified.parsed;
        assertRecordMatchesSlot(verifiedAnchor.payload, {
          slot_key: slotKey,
          generation: reqGeneration,
          lease_epoch: initialSlot.lease_epoch,
          owner_head_sha: initialSlot.owner_head_sha ?? reqHeadSha,
        });
      } catch {
        try {
          await this.withSlotLock(slotKey, async () => {
            const current = await this.readSlot(slotKey);
            if (current !== null && current.state === "RECONCILING") {
              await this.store.putSlot({
                ...current,
                state: "HOLD",
                last_reason_code: "v6_lease_slot_continuity_unproved",
                updated_at_ms: this.clock.nowMs(),
              });
            }
          });
        } catch (err) {
          if (err instanceof Error && err.message === "v6_lease_escrow_lost") {
            return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
          }
          throw err;
        }
        return diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
          slotState: "HOLD",
          rayId,
        });
      }
    }

    let reconcilePrep:
      | { kind: "done"; result: V6RouteResult }
      | { kind: "drain"; slot: SlotRecord }
      | { kind: "verify"; slot: SlotRecord };

    try {
      reconcilePrep = await this.withSlotLock(slotKey, async () => {
        const slot = await this.readSlot(slotKey);
        if (slot === null) {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
              slotState: "IDLE",
              rayId,
            }),
          };
        }
        if (slot.state !== "RECONCILING") {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, stateConflictDiagnostic(slot.state, slot), {
              slotState: slot.state,
              rayId,
            }),
          };
        }
        if (slot.generation !== reqGeneration) {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
              slotState: "RECONCILING",
              rayId,
            }),
          };
        }
        if (verifiedAnchor !== null) {
          try {
            assertRecordMatchesSlot(verifiedAnchor.payload, {
              slot_key: slotKey,
              generation: slot.generation,
              lease_epoch: slot.lease_epoch,
              owner_head_sha: slot.owner_head_sha ?? reqHeadSha,
            });
          } catch {
            await this.store.putSlot({
              ...slot,
              state: "HOLD",
              last_reason_code: "v6_lease_slot_continuity_unproved",
              updated_at_ms: this.clock.nowMs(),
            });
            return {
              kind: "done" as const,
              result: diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
                slotState: "HOLD",
                rayId,
              }),
            };
          }
        }

        if (parsedEvidence === null || parsedEvidence.old_generation !== slot.generation) {
          await this.store.putSlot({
            ...slot,
            state: "HOLD",
            last_reason_code: "v6_lease_reconciliation_evidence_invalid",
            updated_at_ms: this.clock.nowMs(),
          });
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_reconciliation_evidence_invalid", {
              slotState: "HOLD",
              rayId,
            }),
          };
        }

        if (!TERMINAL_RECONCILE_OUTCOMES.has(outcome)) {
          const held: SlotRecord = {
            ...slot,
            state: "HOLD",
            owner_comment_id: anchorCommentId ?? slot.owner_comment_id,
            owner_record_sha256: verifiedAnchor?.sha256 ?? slot.owner_record_sha256,
            last_reason_code: "v6_lease_hold",
            updated_at_ms: this.clock.nowMs(),
          };
          await this.store.putSlot(held);
          return {
            kind: "done" as const,
            result: {
              status: 200,
              body: {
                slot_key: slotKey,
                state: "HOLD",
                generation: held.generation,
                lease_epoch: held.lease_epoch,
              },
            },
          };
        }

        if (!this.evidenceMatchesTerminalOutcome(outcome, parsedEvidence.intent_resolutions)) {
          await this.store.putSlot({
            ...slot,
            state: "HOLD",
            last_reason_code: "v6_lease_reconciliation_evidence_invalid",
            updated_at_ms: this.clock.nowMs(),
          });
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_reconciliation_evidence_invalid", {
              slotState: "HOLD",
              rayId,
            }),
          };
        }

        // Terminal reconciliation outcome requires Gate 1 credential clearance FIRST.
        const clearance = await this.isCredentialClearedForReconcileOrHold(slot, edge);
        if (!clearance.cleared) {
          if (clearance.nextState === "DRAINING") {
            const draining: SlotRecord = {
              ...slot,
              state: "DRAINING",
              owner_comment_id: anchorCommentId ?? slot.owner_comment_id,
              owner_record_sha256: verifiedAnchor?.sha256 ?? slot.owner_record_sha256,
              updated_at_ms: this.clock.nowMs(),
            };
            await this.store.putSlot(draining);
            return { kind: "drain" as const, slot: draining };
          }
          const updated: SlotRecord = {
            ...slot,
            state: clearance.nextState,
            last_reason_code: clearance.reason,
            updated_at_ms: this.clock.nowMs(),
          };
          await this.store.putSlot(updated);
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, clearance.reason, {
              slotState: updated.state,
              retryAfterMs: clearance.retryAfterMs,
              rayId,
            }),
          };
        }

        if (!slot.token_cleared || slot.encrypted_escrow !== null || slot.token_expires_at_ms !== null) {
          const clearedSlot: SlotRecord = {
            ...slot,
            reservation_id: null,
            token_cleared: true,
            encrypted_escrow: null,
            token_expires_at_ms: null,
            last_reason_code: null,
            updated_at_ms: this.clock.nowMs(),
          };
          await this.store.putSlot(clearedSlot);
          return { kind: "verify" as const, slot: clearedSlot };
        }
        return { kind: "verify" as const, slot };
      });
    } catch (error) {
      if (error instanceof Error && error.message === "v6_lease_escrow_lost") {
        return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
      }
      throw error;
    }

    if (reconcilePrep.kind === "done") {
      return reconcilePrep.result;
    }

    let slotToVerify: SlotRecord;
    if (reconcilePrep.kind === "drain") {
      const drainingSlot = reconcilePrep.slot;
      const rev = await this.executeRevocation(drainingSlot, edge, "RECONCILING");
      const applied = await this.withSlotLock(slotKey, async () => {
        const latest = await this.readSlot(slotKey);
        if (
          latest === null ||
          latest.state !== "DRAINING" ||
          latest.generation !== drainingSlot.generation ||
          latest.lease_epoch !== drainingSlot.lease_epoch ||
          latest.reservation_id !== drainingSlot.reservation_id
        ) {
          return { conflict: true, state: latest?.state ?? ("IDLE" as CoordinatorState) };
        }
        await this.store.putSlot(rev.updatedSlot);
        return { conflict: false, state: rev.updatedSlot.state };
      });
      if (applied.conflict) {
        return diagnosticResponse(409, "v6_lease_draining", {
          slotState: applied.state,
          rayId,
        });
      }
      if (rev.errorStatus !== undefined && rev.diagnostic !== undefined) {
        return diagnosticResponse(rev.errorStatus, rev.diagnostic, {
          slotState: rev.updatedSlot.state,
          retryAfterMs: rev.retryAfterMs,
          rayId,
        });
      }
      if (!rev.updatedSlot.token_cleared || rev.updatedSlot.state !== "RECONCILING") {
        return diagnosticResponse(409, "v6_lease_revocation_uncertain", {
          slotState: rev.updatedSlot.state,
          rayId,
        });
      }
      slotToVerify = rev.updatedSlot;
    } else {
      slotToVerify = reconcilePrep.slot;
    }

    // Gate 2 (Write Reconciliation): independently verify old_owner + 100% of old_generation intents.
    let oldOwnerSha256: string | null = null;
    let verificationError: string | null = null;
    try {
      const verifiedEvidence = await this.verifyReconciliationEvidence(
        edge,
        slotKey,
        pullRequest,
        slotToVerify,
        parsedEvidence!,
        anchorCommentId,
        reqHeadSha,
      );
      oldOwnerSha256 = verifiedEvidence.oldOwnerSha256;
    } catch (error) {
      verificationError =
        error instanceof Error && VALID_V6_DIAGNOSTIC_CODES.has(error.message as V6DiagnosticCode)
          ? error.message
          : "v6_lease_reconciliation_evidence_invalid";
    }

    const commitOutcome = await this.withSlotLock(slotKey, async () => {
      const latest = await this.readSlot(slotKey);
      if (
        latest === null ||
        latest.state !== "RECONCILING" ||
        latest.generation !== slotToVerify.generation ||
        latest.lease_epoch !== slotToVerify.lease_epoch
      ) {
        return {
          kind: "done" as const,
          result: diagnosticResponse(
            409,
            latest === null ? "v6_lease_rollback_blocked" : stateConflictDiagnostic(latest.state, latest),
            { slotState: latest?.state ?? "IDLE", rayId },
          ),
        };
      }

      if (verificationError !== null) {
        const held: SlotRecord = {
          ...latest,
          state: "HOLD",
          last_reason_code: verificationError as V6DiagnosticCode,
          updated_at_ms: this.clock.nowMs(),
        };
        await this.store.putSlot(held);
        this.emitAudit("/github/v6/lease/complete-reconcile", slotKey, "refused", {
          state: "HOLD",
          generation: held.generation,
          lease_epoch: held.lease_epoch,
          diagnostic: verificationError,
        });
        return {
          kind: "done" as const,
          result: diagnosticResponse(409, verificationError, {
            slotState: "HOLD",
            rayId,
          }),
        };
      }

      if (!latest.token_cleared || latest.encrypted_escrow !== null) {
        const held: SlotRecord = {
          ...latest,
          state: "HOLD",
          last_reason_code: "v6_lease_revocation_uncertain",
          updated_at_ms: this.clock.nowMs(),
        };
        await this.store.putSlot(held);
        return {
          kind: "done" as const,
          result: diagnosticResponse(409, "v6_lease_revocation_uncertain", {
            slotState: "HOLD",
            rayId,
          }),
        };
      }

      const idle: SlotRecord = {
        ...latest,
        state: "IDLE",
        reservation_id: null,
        owner_comment_id:
          anchorCommentId ?? parsedEvidence!.old_owner_comment_id ?? latest.owner_comment_id,
        owner_record_sha256:
          verifiedAnchor?.sha256 ?? oldOwnerSha256 ?? latest.owner_record_sha256,
        acquired_at_ms: null,
        token_expires_at_ms: null,
        token_cleared: false,
        encrypted_escrow: null,
        last_reason_code: null,
        updated_at_ms: this.clock.nowMs(),
      };
      await this.store.putSlot(idle);
      this.emitAudit("/github/v6/lease/complete-reconcile", slotKey, "success", {
        state: "IDLE",
        generation: idle.generation,
        lease_epoch: idle.lease_epoch,
      });
      return {
        kind: "done" as const,
        result: {
          status: 200,
          body: {
            slot_key: slotKey,
            state: "IDLE",
            generation: idle.generation,
            lease_epoch: idle.lease_epoch,
          },
        },
      };
    });

    return commitOutcome.result;
  }

  async handleStatus(
    env: WorkerEnv,
    body: unknown,
    control?: BrokerDispatchControl,
    rayId?: string,
  ): Promise<V6RouteResult> {
    if (!isObject(body)) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }
    const allowedKeys = new Set([
      "oidc_token",
      "pull_request",
      "slot_key",
      "control_budget",
    ]);
    if (
      Object.keys(body).some((key) => !allowedKeys.has(key)) ||
      !isPositiveSafeInt(body.pull_request) ||
      (body.slot_key !== undefined && typeof body.slot_key !== "string")
    ) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }

    let edge: VerifiedEdgeContext;
    try {
      edge = await this.verifyTupleAndEdge(env, body.oidc_token, control);
    } catch (error) {
      return this.mapEdgeError("/github/v6/lease/status", error, rayId);
    }

    const derivedSlotKey = buildCanonicalSlotKey({
      host: edge.host,
      appId: edge.appId,
      installationId: edge.installationId,
      baseRepoId: edge.baseRepoId,
      pullRequest: body.pull_request,
    });
    if (body.slot_key !== undefined && body.slot_key !== derivedSlotKey) {
      return diagnosticResponse(403, "v6_lease_unauthorized_tuple", { rayId });
    }

    try {
      const slot = await this.readSlot(derivedSlotKey);
      return {
        status: 200,
        body: slot === null ? absentSlotView(derivedSlotKey) : slotStatusView(slot),
      };
    } catch (error) {
      if (error instanceof Error && error.message === "v6_lease_escrow_lost") {
        return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
      }
      throw error;
    }
  }

  async handleResolveHold(
    env: WorkerEnv,
    body: unknown,
    control?: BrokerDispatchControl,
    rayId?: string,
  ): Promise<V6RouteResult> {
    if (!isObject(body)) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }
    const allowedKeys = new Set([
      "oidc_token",
      "slot_key",
      "generation",
      "hold_record_comment_id",
      "resolution_kind",
      "anchor_comment_id",
      "control_budget",
    ]);
    if (
      Object.keys(body).some((key) => !allowedKeys.has(key)) ||
      typeof body.slot_key !== "string" ||
      !isPositiveSafeInt(body.generation) ||
      !isPositiveSafeInt(body.hold_record_comment_id) ||
      typeof body.resolution_kind !== "string" ||
      !ALLOWED_HOLD_RESOLUTIONS.has(body.resolution_kind) ||
      (body.anchor_comment_id !== undefined && !isPositiveSafeInt(body.anchor_comment_id))
    ) {
      return diagnosticResponse(400, "broker_invalid_request", { rayId });
    }

    let edge: VerifiedEdgeContext;
    let slotKey: string;
    let pullRequest: number;
    try {
      edge = await this.verifyTupleAndEdge(env, body.oidc_token, control);
      ({ slotKey, pullRequest } = this.validateSlotKeyForEdge(body.slot_key, edge));
    } catch (error) {
      return this.mapEdgeError("/github/v6/lease/resolve-hold", error, rayId);
    }

    const reqGeneration = body.generation;
    const holdCommentId = body.hold_record_comment_id;
    const resolutionKind = body.resolution_kind as "reconciled_clean" | "enter_reconcile";
    const anchorCommentId = body.anchor_comment_id as number | undefined;

    let holdRecord: ParsedPrLedgerRecord;
    try {
      const verifiedHold = await this.verifyLedgerComment(
        edge,
        pullRequest,
        holdCommentId,
        "hold",
      );
      holdRecord = verifiedHold.parsed;
      if (
        holdRecord.payload.slot_key !== slotKey ||
        holdRecord.payload.generation !== reqGeneration
      ) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
      if (
        resolutionKind === "reconciled_clean" &&
        holdRecord.payload.resolved_by_record_id === null
      ) {
        throw new Error("v6_lease_slot_continuity_unproved");
      }
    } catch {
      return diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
        slotState: "HOLD",
        rayId,
      });
    }

    let verifiedAnchor: ParsedPrLedgerRecord | null = null;
    if (anchorCommentId !== undefined) {
      try {
        const verified = await this.verifyLedgerComment(edge, pullRequest, anchorCommentId, "any");
        verifiedAnchor = verified.parsed;
        if (
          verifiedAnchor.payload.slot_key !== undefined &&
          verifiedAnchor.payload.slot_key !== slotKey
        ) {
          throw new Error("v6_lease_slot_continuity_unproved");
        }
        if (
          verifiedAnchor.payload.generation !== undefined &&
          verifiedAnchor.payload.generation !== reqGeneration
        ) {
          throw new Error("v6_lease_slot_continuity_unproved");
        }
      } catch {
        return diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
          slotState: "HOLD",
          rayId,
        });
      }
    }

    let resolvePrep:
      | { kind: "done"; result: V6RouteResult }
      | { kind: "revoke"; slot: SlotRecord };

    try {
      resolvePrep = await this.withSlotLock(slotKey, async () => {
        const slot = await this.readSlot(slotKey);
        if (slot === null) {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
              slotState: "IDLE",
              rayId,
            }),
          };
        }
        if (slot.state !== "HOLD") {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, stateConflictDiagnostic(slot.state, slot), {
              slotState: slot.state,
              rayId,
            }),
          };
        }
        if (slot.generation !== reqGeneration) {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_rollback_blocked", {
              slotState: "HOLD",
              rayId,
            }),
          };
        }

        try {
          assertRecordMatchesSlot(holdRecord.payload, {
            slot_key: slotKey,
            generation: slot.generation,
            lease_epoch: slot.lease_epoch,
            owner_head_sha: slot.owner_head_sha,
          });
          if (verifiedAnchor !== null) {
            assertRecordMatchesSlot(verifiedAnchor.payload, {
              slot_key: slotKey,
              generation: slot.generation,
              lease_epoch: slot.lease_epoch,
              owner_head_sha: slot.owner_head_sha,
            });
          }
        } catch {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_slot_continuity_unproved", {
              slotState: "HOLD",
              rayId,
            }),
          };
        }

        if (!this.clock.isTrusted()) {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, "v6_lease_clock_untrusted", {
              slotState: "HOLD",
              rayId,
            }),
          };
        }

        if (slot.encrypted_escrow !== null) {
          return { kind: "revoke" as const, slot };
        }

        const clearance = await this.isCredentialClearedForReconcileOrHold(slot, edge);
        if (!clearance.cleared) {
          return {
            kind: "done" as const,
            result: diagnosticResponse(409, clearance.reason, {
              slotState: "HOLD",
              retryAfterMs: clearance.retryAfterMs,
              rayId,
            }),
          };
        }

        const nextState: CoordinatorState = "RECONCILING";
        const resolvedAcquiredAtMs =
          slot.acquired_at_ms ??
          (verifiedAnchor !== null
            ? parsePositiveTimestampMs(verifiedAnchor.payload.acquired_at)
            : null);
        const updated: SlotRecord = {
          ...slot,
          state: nextState,
          reservation_id: null,
          owner_comment_id: anchorCommentId ?? slot.owner_comment_id,
          owner_record_sha256: verifiedAnchor?.sha256 ?? slot.owner_record_sha256,
          acquired_at_ms: resolvedAcquiredAtMs,
          token_expires_at_ms: null,
          token_cleared: true,
          encrypted_escrow: null,
          last_reason_code: null,
          updated_at_ms: this.clock.nowMs(),
        };
        await this.store.putSlot(updated);
        this.emitAudit("/github/v6/lease/resolve-hold", slotKey, "success", {
          state: nextState,
          generation: updated.generation,
          lease_epoch: updated.lease_epoch,
        });
        return {
          kind: "done" as const,
          result: {
            status: 200,
            body: {
              slot_key: slotKey,
              state: nextState,
              generation: updated.generation,
              lease_epoch: updated.lease_epoch,
            },
          },
        };
      });
    } catch (error) {
      if (error instanceof Error && error.message === "v6_lease_escrow_lost") {
        return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
      }
      throw error;
    }

    if (resolvePrep.kind === "done") {
      return resolvePrep.result;
    }

    const heldSlot = resolvePrep.slot;
    const rev = await this.executeRevocation(
      { ...heldSlot, state: "DRAINING" },
      edge,
      "RECONCILING",
    );
    const applied = await this.withSlotLock(slotKey, async () => {
      const latest = await this.readSlot(slotKey);
      if (
        latest === null ||
        latest.state !== "HOLD" ||
        latest.generation !== heldSlot.generation ||
        latest.lease_epoch !== heldSlot.lease_epoch ||
        latest.reservation_id !== heldSlot.reservation_id
      ) {
        return { conflict: true, state: latest?.state ?? ("IDLE" as CoordinatorState) };
      }
      if (rev.errorStatus !== undefined && rev.diagnostic !== undefined) {
        await this.store.putSlot({ ...rev.updatedSlot, state: "HOLD" });
        return { conflict: false, failed: true, state: "HOLD" as CoordinatorState };
      }
      const resolvedAcquiredAtMs =
        rev.updatedSlot.acquired_at_ms ??
        (verifiedAnchor !== null
          ? parsePositiveTimestampMs(verifiedAnchor.payload.acquired_at)
          : null);
      const resolvedSlot: SlotRecord = {
        ...rev.updatedSlot,
        state: "RECONCILING",
        owner_comment_id: anchorCommentId ?? heldSlot.owner_comment_id,
        owner_record_sha256: verifiedAnchor?.sha256 ?? heldSlot.owner_record_sha256,
        acquired_at_ms: resolvedAcquiredAtMs,
        token_cleared: true,
      };
      await this.store.putSlot(resolvedSlot);
      return { conflict: false, failed: false, slot: resolvedSlot };
    });

    if (applied.conflict) {
      return diagnosticResponse(409, "v6_lease_hold", { slotState: applied.state, rayId });
    }
    if (applied.failed) {
      return diagnosticResponse(rev.errorStatus!, rev.diagnostic!, {
        slotState: "HOLD",
        rayId,
      });
    }
    const resolvedSlot = applied.slot!;
    return {
      status: 200,
      body: {
        slot_key: slotKey,
        state: resolvedSlot.state,
        generation: resolvedSlot.generation,
        lease_epoch: resolvedSlot.lease_epoch,
      },
    };
  }
}

/**
 * Serializes a v6 coordinator JSON response body, preserving the granted
 * installation token exclusively on `200 OK` `/github/v6/lease/acquire`
 * responses while redacting all other fields, routes, and statuses.
 */
export function serializeV6RouteResponseBody(
  pathname: string,
  status: number,
  body: Record<string, unknown>,
): string {
  if (
    pathname === "/github/v6/lease/acquire" &&
    status === 200 &&
    typeof body.token === "string" &&
    body.token.length > 0
  ) {
    const grantedToken = body.token;
    const rest = Object.fromEntries(
      Object.entries(body).filter(([key]) => key !== "token"),
    );
    const redactedRest = JSON.parse(
      redactSensitiveText(JSON.stringify(rest)),
    ) as Record<string, unknown>;
    return JSON.stringify({ ...redactedRest, token: grantedToken });
  }
  return redactSensitiveText(JSON.stringify(body));
}

export class V6PrCoordinator extends DurableObject<WorkerEnv> {
  private readonly store: DurableObjectSqlV6CoordinatorStore;
  private engine: V6PrCoordinatorEngine | undefined;

  constructor(ctx: DurableObjectState, env: WorkerEnv) {
    super(ctx, env);
    this.store = new DurableObjectSqlV6CoordinatorStore(ctx.storage.sql);
  }

  private getEngine(): V6PrCoordinatorEngine {
    if (this.engine === undefined) {
      // Validate signing key fail-closed on initialization.
      new V6EscrowCrypto(this.env.REVIEWSENSEI_SIGNING_KEY);
      this.engine = new V6PrCoordinatorEngine({
        store: this.store,
        provider: new GitHubV6ProviderAdapter(new GitHubApi(this.env)),
      });
    }
    return this.engine;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (request.method !== "POST") {
      return new Response(JSON.stringify({ error: "method_not_allowed" }), {
        status: 405,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      });
    }

    let body: unknown;
    try {
      body = (await request.json()) as unknown;
    } catch {
      return new Response(
        JSON.stringify({
          error: "broker_invalid_request",
          diagnostic: brokerDiagnostic(new Error("broker_invalid_request")),
        }),
        {
          status: 400,
          headers: {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
          },
        },
      );
    }

    const rayId = request.headers.get("cf-ray") ?? undefined;
    const result = await this.ctx.blockConcurrencyWhile(async () => {
      let engine: V6PrCoordinatorEngine;
      try {
        engine = this.getEngine();
      } catch {
        return diagnosticResponse(503, "configuration_unavailable", { rayId });
      }
      try {
        switch (url.pathname) {
          case "/github/v6/lease/acquire":
            return await engine.handleAcquire(this.env, body, undefined, rayId);
          case "/github/v6/lease/drain":
            return await engine.handleDrain(this.env, body, undefined, rayId);
          case "/github/v6/lease/complete-reconcile":
            return await engine.handleCompleteReconcile(this.env, body, undefined, rayId);
          case "/github/v6/lease/release":
            return await engine.handleRelease(this.env, body, undefined, rayId);
          case "/github/v6/lease/status":
            return await engine.handleStatus(this.env, body, undefined, rayId);
          case "/github/v6/lease/resolve-hold":
            return await engine.handleResolveHold(this.env, body, undefined, rayId);
          default:
            return { status: 404, body: { error: "not_found" } };
        }
      } catch (error) {
        if (error instanceof Error && error.message === "v6_lease_escrow_lost") {
          return diagnosticResponse(409, "v6_lease_escrow_lost", { slotState: "HOLD", rayId });
        }
        return diagnosticResponse(503, "configuration_unavailable", { rayId });
      }
    });

    return new Response(
      serializeV6RouteResponseBody(url.pathname, result.status, result.body),
      {
        status: result.status,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }
}
