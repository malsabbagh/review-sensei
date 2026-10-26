/** Current caller authority. Accounting remains exclusively in the caller root. */
import type { GitHubApi } from "./github-api";
import type { OidcClaims } from "./oidc";

export const RUN_AUTHORITY_AUDIENCE = "reviewsensei-run-action";
const SHA = /^[a-f0-9]{40}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const RUN = /^[1-9][0-9]{0,18}$/;
const ASSOCIATIONS = new Set(["OWNER", "MEMBER", "COLLABORATOR"]);
const REQUEST_KEYS = ["version", "purpose", "repository", "repository_id", "pull_request", "head_sha", "run_id", "run_attempt", "job", "job_workflow_ref", "job_workflow_sha", "issued_at", "expires_at", "audience", "source"] as const;

export interface RunActionSource {
  kind: "pull_request" | "issue_comment" | "review_comment";
  id: number;
  updated_at: string | null;
  body_sha256: string | null;
}
export interface RunAuthorityRequest {
  version: 3;
  purpose: "review" | "command" | "reply";
  repository: string;
  repository_id: number;
  pull_request: number;
  head_sha: string;
  run_id: string;
  run_attempt: 1 | 2;
  job: "hosted" | "local" | "command";
  job_workflow_ref: string;
  job_workflow_sha: string;
  issued_at: number;
  expires_at: number;
  audience: typeof RUN_AUTHORITY_AUDIENCE;
  source: RunActionSource;
}
export interface RunAuthority extends RunAuthorityRequest {
  actor: { login: string; type: "User"; association: string } | null;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}
function invalid(): never { throw new Error("broker_session_attestation_invalid"); }

/** Reconstruct a canonical closed document; no caller-provided extension survives. */
export function parseRunAuthority(value: unknown, grant: true): RunAuthority;
export function parseRunAuthority(value: unknown, grant?: false): RunAuthorityRequest;
export function parseRunAuthority(value: unknown, grant = false): RunAuthorityRequest | RunAuthority {
  if (!object(value) || !exact(value, grant ? [...REQUEST_KEYS, "actor"] : REQUEST_KEYS)) invalid();
  const source = value.source;
  const now = Date.now() / 1000;
  if (value.version !== 3 || typeof value.purpose !== "string" || !["review", "command", "reply"].includes(value.purpose) ||
    typeof value.repository !== "string" || !REPOSITORY.test(value.repository) ||
    !positive(value.repository_id) || !positive(value.pull_request) ||
    typeof value.head_sha !== "string" || !SHA.test(value.head_sha) ||
    typeof value.run_id !== "string" || !RUN.test(value.run_id) ||
    (value.run_attempt !== 1 && value.run_attempt !== 2) ||
    typeof value.job !== "string" || !["hosted", "local", "command"].includes(value.job) ||
    (value.purpose === "command" ? value.job !== "command" : value.job === "command") ||
    typeof value.job_workflow_ref !== "string" || value.job_workflow_ref.length > 2048 ||
    typeof value.job_workflow_sha !== "string" || !SHA.test(value.job_workflow_sha) ||
    typeof value.issued_at !== "number" || !Number.isSafeInteger(value.issued_at) ||
    typeof value.expires_at !== "number" || !Number.isSafeInteger(value.expires_at) ||
    value.issued_at > now + 30 || value.expires_at <= now ||
    value.expires_at <= value.issued_at || value.expires_at - value.issued_at > 600 ||
    value.audience !== RUN_AUTHORITY_AUDIENCE || !object(source) ||
    !exact(source, ["kind", "id", "updated_at", "body_sha256"]) || !positive(source.id)) invalid();
  if (value.purpose === "review") {
    if (source.kind !== "pull_request" || source.id !== value.pull_request ||
      source.updated_at !== null || source.body_sha256 !== null) invalid();
  } else if ((value.purpose === "command" ? source.kind !== "issue_comment" : typeof source.kind !== "string" || !["issue_comment", "review_comment"].includes(source.kind)) ||
    typeof source.updated_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(source.updated_at) ||
    typeof source.body_sha256 !== "string" || !DIGEST.test(source.body_sha256)) invalid();
  const parsed: RunAuthorityRequest = {
    version: 3, purpose: value.purpose as RunAuthorityRequest["purpose"],
    repository: value.repository, repository_id: value.repository_id,
    pull_request: value.pull_request, head_sha: value.head_sha,
    run_id: value.run_id, run_attempt: value.run_attempt,
    job: value.job as RunAuthorityRequest["job"],
    job_workflow_ref: value.job_workflow_ref, job_workflow_sha: value.job_workflow_sha,
    issued_at: value.issued_at, expires_at: value.expires_at,
    audience: RUN_AUTHORITY_AUDIENCE,
    source: { kind: source.kind as RunActionSource["kind"], id: source.id,
      updated_at: source.updated_at as string | null, body_sha256: source.body_sha256 as string | null },
  };
  if (!grant) return parsed;
  const actor = value.actor;
  if (parsed.purpose === "review") {
    if (actor !== null) invalid();
    return { ...parsed, actor: null };
  }
  if (!object(actor) || !exact(actor, ["login", "type", "association"]) ||
    typeof actor.login !== "string" || actor.login.length === 0 || actor.login.length > 256 ||
    actor.type !== "User" || typeof actor.association !== "string" || !ASSOCIATIONS.has(actor.association)) invalid();
  return { ...parsed, actor: { login: actor.login, type: "User", association: actor.association } };
}

export function bindRunAuthority(value: unknown, claims: OidcClaims): RunAuthorityRequest {
  const parsed = parseRunAuthority(value);
  if (parsed.repository !== claims.repository || parsed.repository_id !== claims.repository_id ||
    parsed.run_id !== claims.run_id || String(parsed.run_attempt) !== claims.run_attempt ||
    parsed.job_workflow_ref !== claims.job_workflow_ref || parsed.job_workflow_sha !== claims.job_workflow_sha ||
    (parsed.purpose === "command" && claims.event_name !== "issue_comment") ||
    (parsed.purpose === "reply" && (parsed.source.kind === "issue_comment" ? claims.event_name !== "issue_comment" : claims.event_name !== "pull_request_review_comment"))) invalid();
  return parsed;
}

/** Purpose scopes the entry; human reply reassessment may refinalize reviews. */
export function authorizeRunCapability(value: RunAuthorityRequest, capability: string): void {
  const common = ["review_session", "review_actions", "review_status"];
  const finalization = ["review_publish", "check_publish", "thread_resolve"];
  const allowed = value.purpose === "reply"
    ? [...common, ...finalization, value.source.kind === "review_comment" ? "inline_reply" : "issue_reply"]
    : [...common, ...finalization, ...(value.purpose === "review" ? ["learning_write"] : ["issue_reply"])];
  if (!allowed.includes(capability)) throw new Error("broker_capability_purpose_rejected");
}

export async function authorizeRunSource(
  github: GitHubApi, value: RunAuthorityRequest, claims: OidcClaims,
  token: string, recognizedCommand: (body: string) => boolean,
): Promise<RunAuthority> {
  if (value.purpose === "review") return { ...value, actor: null };
  const source = await github.feedbackComment(value.repository, value.pull_request,
    value.source.kind === "issue_comment" ? "issue" : "inline", value.source.id, token);
  if (source.comment_id !== value.source.id || source.updated_at !== value.source.updated_at ||
    source.body_sha256 !== value.source.body_sha256 || source.author_id !== claims.actor_id ||
    typeof source.author !== "string" || source.author.toLowerCase() !== claims.actor.toLowerCase() ||
    typeof source.body !== "string" || typeof source.association !== "string" ||
    !ASSOCIATIONS.has(source.association) ||
    (value.purpose === "command" ? !recognizedCommand(source.body) : !commentMentionsSensei(source.body))) {
    throw new Error("broker_session_actor_rejected");
  }
  return { ...value, actor: { login: source.author, type: "User", association: source.association } };
}

function commentMentionsSensei(body: string): boolean {
  return body.includes("@sensei") || /(?:^|\s)@[rR][eE][vV][iI][eE][wW][sS][eE][nN][sS][eE][iI](?:$|\s|[.,!?])/.test(body);
}

export function canonicalRunAuthority(value: RunAuthority): string {
  return JSON.stringify(parseRunAuthority(value, true));
}
