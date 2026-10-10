/** Metadata-only feedback authority; bodies are reloaded from canonical GitHub endpoints. */
import { GitHubApi } from "./github-api";
import type { OidcClaims } from "./oidc";

const HASH = /^[a-f0-9]{64}$/;
const SHA = /^[a-f0-9]{40}$/;
const ASSOCIATIONS = new Set(["OWNER", "MEMBER", "COLLABORATOR"]);
// Python's conversation \s and scoped case matching, without FEFF normalization.
const SPACE = "[\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const MENTION = new RegExp(`(?:^|${SPACE})@(?:[sSſ][eE][nN][sSſ][eE][iIİı]|[rR][eE][vV][iI][eE][wW][sS][eE][nN][sS][eE][iI])(?:$|${SPACE}|[.,!?])`);
const SOURCE_KEYS = ["kind", "comment_id", "updated_at", "author", "author_id", "association", "root_comment_id", "body_bytes", "body_sha256"];
const SELECTION_KEYS = ["interface", "repository", "pull_request", "base_sha", "head_sha", "trigger", "target_ids", "total_bytes", "sources", "selection_digest", "event_key"];
export const FEEDBACK_REQUEST_KEYS = ["version", "repository", "repository_id", "pull_request", "head_sha", "operation", "source_comment_id", "run_id", "issued_at", "concurrency_group", "job_workflow_ref", "job_workflow_sha", "feedback", "mutation"];
export const FEEDBACK_GRANT_KEYS = [...FEEDBACK_REQUEST_KEYS, "actor", "actor_id", "actor_type", "association"];
const MUTATION_KEYS = ["operation_id", "source_digest", "authority_digest", "execution_identity", "inventory_digest", "inventory_generation", "reservation_id", "root_digest", "root_generation", "reason", "request_digest", "dispatch_digest", "read_accounting"];

export interface FeedbackAttestationRequest {
  version: 2;
  repository: string;
  repository_id: number;
  pull_request: number;
  head_sha: string;
  operation: "feedback";
  source_comment_id: number;
  run_id: string;
  issued_at: number;
  concurrency_group: string;
  job_workflow_ref: string;
  job_workflow_sha: string;
  feedback: Record<string, unknown>;
  mutation: Record<string, unknown>;
}
export interface FeedbackAttestation extends FeedbackAttestationRequest {
  actor: string;
  actor_id: number;
  actor_type: "User";
  association: string;
}
function fail(): never { throw new Error("broker_feedback_attestation_invalid"); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return fail();
  return value as Record<string, unknown>;
}
function exact(value: unknown, keys: readonly string[]): Record<string, unknown> {
  const result = object(value);
  if (Object.keys(result).length !== keys.length || keys.some(key => !Object.hasOwn(result, key))) return fail();
  return result;
}
function integer(value: unknown, min = 1, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) return fail();
  return value;
}
function text(value: unknown, maximum: number): string {
  if (typeof value !== "string" || value.length === 0) return fail();
  const bytes = new TextEncoder().encode(value);
  if (bytes.length > maximum || new TextDecoder("utf-8", { fatal: true }).decode(bytes) !== value) return fail();
  return value;
}
function hash(value: unknown): string { const result = text(value, 64); if (!HASH.test(result)) return fail(); return result; }
export function canonicalFeedback(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalFeedback).join(",")}]`;
  if (value && typeof value === "object") {
    const item = value as Record<string, unknown>;
    return `{${Object.keys(item).sort().map(key => `${JSON.stringify(key)}:${canonicalFeedback(item[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
export async function feedbackDigest(value: unknown): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalFeedback(value)));
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
}
export function parseFeedbackAttestation(value: unknown, grant = false): FeedbackAttestationRequest | FeedbackAttestation {
  const data = exact(value, grant ? FEEDBACK_GRANT_KEYS : FEEDBACK_REQUEST_KEYS);
  if (data.version !== 2 || data.operation !== "feedback") return fail();
  const repository = text(data.repository, 512);
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository) || repository.split("/").some(part => part === "." || part === "..")) return fail();
  integer(data.repository_id); integer(data.pull_request); integer(data.source_comment_id); integer(data.issued_at);
  if (!SHA.test(text(data.head_sha, 40)) || !SHA.test(text(data.job_workflow_sha, 40)) || !/^[1-9][0-9]{0,18}$/.test(text(data.run_id, 19))) return fail();
  text(data.job_workflow_ref, 2048);
  if (data.concurrency_group !== `reviewsensei-session-${data.repository_id}-${data.pull_request}`) return fail();
  const selection = exact(data.feedback, SELECTION_KEYS);
  if (selection.interface !== "feedback-v1" || selection.repository !== repository || selection.pull_request !== data.pull_request || selection.head_sha !== data.head_sha || !SHA.test(text(selection.base_sha, 40))) return fail();
  const trigger = exact(selection.trigger, ["kind", "comment_id", "updated_at"]);
  if (!["issue", "inline"].includes(String(trigger.kind)) || trigger.comment_id !== data.source_comment_id) return fail();
  text(trigger.updated_at, 128); hash(selection.selection_digest); hash(selection.event_key);
  if (!Array.isArray(selection.target_ids) || selection.target_ids.length > 250 || new Set(selection.target_ids).size !== selection.target_ids.length) return fail();
  selection.target_ids.forEach(hash);
  if (!Array.isArray(selection.sources) || selection.sources.length < 1 || selection.sources.length > 32) return fail();
  const identities = new Set<string>(); let total = 0; let selectedTrigger: Record<string, unknown> | undefined;
  for (const raw of selection.sources) {
    const source = exact(raw, SOURCE_KEYS);
    if (!["issue", "inline"].includes(String(source.kind))) return fail();
    integer(source.comment_id); integer(source.author_id); text(source.updated_at, 128); text(source.author, 256);
    if (!ASSOCIATIONS.has(String(source.association))) return fail();
    if (source.kind === "issue" ? source.root_comment_id !== null : !Number.isSafeInteger(source.root_comment_id) || Number(source.root_comment_id) <= 0) return fail();
    total += integer(source.body_bytes, 1, 65536); hash(source.body_sha256);
    const identity = `${source.kind}:${source.comment_id}`;
    if (identities.has(identity)) return fail(); identities.add(identity);
    if (source.kind === trigger.kind && source.comment_id === trigger.comment_id && source.updated_at === trigger.updated_at) selectedTrigger = source;
  }
  if (!selectedTrigger || total !== integer(selection.total_bytes, 1, 262144)) return fail();
  const mutation = exact(data.mutation, MUTATION_KEYS);
  for (const key of ["operation_id", "source_digest", "authority_digest", "inventory_digest", "root_digest"]) hash(mutation[key]);
  if (!/^[a-f0-9]{32}$/.test(text(mutation.execution_identity, 32))) return fail();
  text(mutation.reservation_id, 128); integer(mutation.inventory_generation, 0, 2147483647); integer(mutation.root_generation, 0, 2147483647);
  if (!["admission", "dispatch", "accounting", "accepted", "pending", "replay", "finalize"].includes(String(mutation.reason))) return fail();
  for (const key of ["request_digest", "dispatch_digest"]) if (mutation[key] !== null) hash(mutation[key]);
  const accounting = exact(mutation.read_accounting, ["schema_version", "calls", "deadline_unix_ms"]);
  if (accounting.schema_version !== "1.0") return fail(); integer(accounting.calls, 0, 64); integer(accounting.deadline_unix_ms);
  if (grant) {
    text(data.actor, 256); integer(data.actor_id);
    if (data.actor_type !== "User" || data.actor !== selectedTrigger.author || data.actor_id !== selectedTrigger.author_id || data.association !== selectedTrigger.association) return fail();
  }
  const request: FeedbackAttestationRequest = {
    version: 2, operation: "feedback", repository,
    repository_id: integer(data.repository_id), pull_request: integer(data.pull_request),
    head_sha: text(data.head_sha, 40), source_comment_id: integer(data.source_comment_id),
    run_id: text(data.run_id, 19), issued_at: integer(data.issued_at),
    concurrency_group: text(data.concurrency_group, 256), job_workflow_ref: text(data.job_workflow_ref, 2048),
    job_workflow_sha: text(data.job_workflow_sha, 40), feedback: selection, mutation,
  };
  return grant ? { ...request, actor: text(data.actor, 256), actor_id: integer(data.actor_id), actor_type: "User", association: text(data.association, 16) } : request;
}

export async function authorizeFeedback(github: GitHubApi, data: FeedbackAttestationRequest, claims: OidcClaims, token: string): Promise<FeedbackAttestation> {
  if (data.repository !== claims.repository || data.repository_id !== claims.repository_id || data.run_id !== claims.run_id || data.job_workflow_ref !== claims.job_workflow_ref || data.job_workflow_sha !== claims.job_workflow_sha) return fail();
  if (!["issue_comment", "pull_request_review_comment", "workflow_dispatch"].includes(claims.event_name)) return fail();
  const snapshot = await github.feedbackPullRequest(data.repository, data.pull_request, token);
  if (snapshot.base_sha !== data.feedback.base_sha || snapshot.head_sha !== data.head_sha) return fail();
  const sources: Record<string, unknown>[] = [];
  let trigger: Record<string, unknown> | undefined;
  for (const raw of data.feedback.sources as unknown[]) {
    const expected = object(raw);
    const live = await github.feedbackComment(data.repository, data.pull_request, expected.kind as "issue" | "inline", expected.comment_id as number, token);
    const metadata = { ...live }; delete metadata.body;
    if (canonicalFeedback(metadata) !== canonicalFeedback(expected)) return fail();
    sources.push(live);
    if (live.kind === object(data.feedback.trigger).kind && live.comment_id === data.source_comment_id) trigger = live;
  }
  if (!trigger || trigger.author_id !== claims.actor_id || String(trigger.author).toLowerCase() !== claims.actor.toLowerCase() || !MENTION.test(String(trigger.body))) return fail();
  const complete: Record<string, unknown> = { ...data.feedback, sources }; delete complete.selection_digest; delete complete.event_key;
  if (await feedbackDigest(complete) !== data.feedback.selection_digest) return fail();
  const event = { domain: "reviewsensei:feedback-event:v1", repository: data.repository, pull_request: data.pull_request, trigger: { kind: trigger.kind, comment_id: data.source_comment_id } };
  if (await feedbackDigest(event) !== data.feedback.event_key) return fail();
  return { ...data, actor: String(trigger.author), actor_id: claims.actor_id, actor_type: "User", association: String(trigger.association) };
}
