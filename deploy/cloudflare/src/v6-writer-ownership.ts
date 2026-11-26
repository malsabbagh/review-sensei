import type { GitHubApiResponse, JsonObject } from "./github-api";
import type { OidcClaims } from "./oidc";

const SHA_PATTERN = /^[a-f0-9]{40}$/;
const POSITIVE_INT_STRING_PATTERN = /^[1-9][0-9]{0,15}$/;
const MARKER_LINE_PATTERN =
  /^<!-- reviewsensei:v6:(?:record|ownership) sha256=([0-9a-f]{64}) payload=(\{[^\r\n]*\}) -->$/;
const REQUEST_KEYS = [
  "schema",
  "repository_id",
  "pull_request",
  "operation_id",
  "writer_epoch",
  "head_sha",
] as const;
const AUTHORITATIVE_MARKER_SCHEMAS = new Set([
  "reviewsensei:v6:intent:v1",
  "reviewsensei:v6:ownership:v1",
]);
const COMMENTS_PAGE_SIZE = 100;
const MAX_COMMENT_PAGES = 25;
const MAX_OPERATION_ID_LENGTH = 256;

export interface V6WriterOwnershipRequest {
  schema: "v6-writer-ownership-v1";
  repository_id: number;
  pull_request: number;
  operation_id: string;
  writer_epoch: number;
  head_sha: string;
}

export interface V6AuthoritativeWriterProof {
  schema: "v6-writer-ownership-proof-v1";
  verified_by_broker: true;
  repository_id: number;
  pull_request: number;
  operation_id: string;
  authoritative_writer_epoch: number;
  active_run_id: number;
  active_run_attempt: number;
  head_sha: string;
}

export interface V6OwnershipGitHubClient {
  request(
    method: string,
    path: string,
    token: string | undefined,
    body?: JsonObject,
  ): Promise<GitHubApiResponse>;
}

export interface VerifyV6WriterOwnershipParams {
  repository: string;
  request: V6WriterOwnershipRequest;
  claims: Pick<OidcClaims, "run_id" | "run_attempt">;
  appId: number;
  pullToken: string;
  actionsToken: string;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPositiveSafeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function isNonNegativeSafeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function parsePositiveIntString(value: unknown): number | null {
  if (typeof value !== "string" || !POSITIVE_INT_STRING_PATTERN.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function canonicalizeJson(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalizeJson(item));
  }
  if (isObject(value)) {
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      sorted[key] = canonicalizeJson(value[key]);
    }
    return sorted;
  }
  return value;
}

/** Compute canonical SHA-256 hex matching Python's ``canonical_sha256``. */
export async function canonicalPayloadSha256(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(canonicalizeJson(value)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

/** Parse and validate a closed v6 writer ownership request payload. */
export function parseV6WriterOwnershipRequest(
  value: unknown,
): V6WriterOwnershipRequest {
  if (
    !isObject(value) ||
    Object.keys(value).length !== REQUEST_KEYS.length ||
    !REQUEST_KEYS.every((key) => Object.hasOwn(value, key))
  ) {
    throw new Error("broker_writer_ownership_invalid");
  }
  const {
    schema,
    repository_id,
    pull_request,
    operation_id,
    writer_epoch,
    head_sha,
  } = value;
  if (
    schema !== "v6-writer-ownership-v1" ||
    !isPositiveSafeInt(repository_id) ||
    !isPositiveSafeInt(pull_request) ||
    !isPositiveSafeInt(writer_epoch) ||
    typeof operation_id !== "string" ||
    operation_id.length === 0 ||
    operation_id.length > MAX_OPERATION_ID_LENGTH ||
    operation_id.trim() !== operation_id ||
    /[\x00-\x1f\x7f]/.test(operation_id) ||
    typeof head_sha !== "string" ||
    !SHA_PATTERN.test(head_sha)
  ) {
    throw new Error("broker_writer_ownership_invalid");
  }
  return {
    schema: "v6-writer-ownership-v1",
    repository_id,
    pull_request,
    operation_id,
    writer_epoch,
    head_sha,
  };
}

async function executeAuthoritativeRead(
  github: V6OwnershipGitHubClient,
  path: string,
  token: string,
): Promise<GitHubApiResponse> {
  try {
    return await github.request("GET", path, token);
  } catch (error) {
    if (
      error instanceof Error &&
      /^(broker_control_|broker_request_)/.test(error.message)
    ) {
      throw error;
    }
    throw new Error("broker_writer_ownership_unverified");
  }
}

async function verifyLivePullRequest(
  github: V6OwnershipGitHubClient,
  repository: string,
  request: V6WriterOwnershipRequest,
  pullToken: string,
): Promise<void> {
  const response = await executeAuthoritativeRead(
    github,
    `/repos/${repository}/pulls/${request.pull_request}`,
    pullToken,
  );
  if (response.status !== 200 || !isObject(response.data)) {
    throw new Error("broker_writer_ownership_unverified");
  }
  const { state, base, head } = response.data;
  if (typeof state !== "string" || state.length === 0) {
    throw new Error("broker_writer_ownership_unverified");
  }
  if (state !== "open") {
    throw new Error("broker_stale_writer_rejected");
  }
  if (!isObject(base) || !isObject(base.repo) || !isPositiveSafeInt(base.repo.id)) {
    throw new Error("broker_writer_ownership_unverified");
  }
  if (base.repo.id !== request.repository_id) {
    throw new Error("broker_repository_rejected");
  }
  if (
    !isObject(head) ||
    typeof head.sha !== "string" ||
    !SHA_PATTERN.test(head.sha)
  ) {
    throw new Error("broker_writer_ownership_unverified");
  }
  if (head.sha !== request.head_sha) {
    throw new Error("broker_session_head_rejected");
  }
}

async function verifyLiveWorkflowAttempt(
  github: V6OwnershipGitHubClient,
  repository: string,
  runId: number,
  runAttempt: number,
  actionsToken: string,
): Promise<void> {
  const response = await executeAuthoritativeRead(
    github,
    `/repos/${repository}/actions/runs/${runId}/attempts/${runAttempt}`,
    actionsToken,
  );
  if (response.status !== 200 || !isObject(response.data)) {
    throw new Error("broker_writer_ownership_unverified");
  }
  const { id, run_attempt, status } = response.data;
  if (id !== undefined) {
    if (!isPositiveSafeInt(id)) {
      throw new Error("broker_writer_ownership_unverified");
    }
    if (id !== runId) {
      throw new Error("broker_stale_writer_rejected");
    }
  }
  if (!isPositiveSafeInt(run_attempt)) {
    throw new Error("broker_writer_ownership_unverified");
  }
  if (run_attempt !== runAttempt) {
    throw new Error("broker_stale_writer_rejected");
  }
  if (typeof status !== "string" || status.length === 0) {
    throw new Error("broker_writer_ownership_unverified");
  }
  if (status !== "in_progress") {
    throw new Error("broker_stale_writer_rejected");
  }
}

interface EpochOwner {
  runId: number;
  runAttempt: number;
}

async function scanAuthoritativeOwnershipMarkers(
  github: V6OwnershipGitHubClient,
  repository: string,
  request: V6WriterOwnershipRequest,
  appId: number,
  pullToken: string,
): Promise<{ maxWriterEpoch: number; ownersAtMax: EpochOwner[] }> {
  let maxWriterEpoch = 0;
  const ownersByEpoch = new Map<number, EpochOwner[]>();

  for (let page = 1; page <= MAX_COMMENT_PAGES; page += 1) {
    const pageSuffix = page === 1 ? "" : `&page=${page}`;
    const response = await executeAuthoritativeRead(
      github,
      `/repos/${repository}/issues/${request.pull_request}/comments?sort=created&direction=asc&per_page=${COMMENTS_PAGE_SIZE}${pageSuffix}`,
      pullToken,
    );
    if (
      response.status !== 200 ||
      !Array.isArray(response.data) ||
      response.data.length > COMMENTS_PAGE_SIZE
    ) {
      throw new Error("broker_writer_ownership_unverified");
    }
    for (const comment of response.data) {
      if (!isObject(comment)) {
        throw new Error("broker_writer_ownership_unverified");
      }
      if (
        !isObject(comment.performed_via_github_app) ||
        comment.performed_via_github_app.id !== appId
      ) {
        continue;
      }
      if (typeof comment.body !== "string") {
        throw new Error("broker_writer_ownership_unverified");
      }
      for (const rawLine of comment.body.split(/\r?\n/)) {
        const match = MARKER_LINE_PATTERN.exec(rawLine.trim());
        if (match === null) {
          continue;
        }
        let payload: unknown;
        try {
          payload = JSON.parse(match[2]);
        } catch {
          throw new Error("broker_writer_ownership_unverified");
        }
        if (!isObject(payload)) {
          throw new Error("broker_writer_ownership_unverified");
        }
        if ((await canonicalPayloadSha256(payload)) !== match[1]) {
          throw new Error("broker_writer_ownership_unverified");
        }
        if (
          typeof payload.schema !== "string" ||
          !AUTHORITATIVE_MARKER_SCHEMAS.has(payload.schema)
        ) {
          continue;
        }
        if (typeof payload.operation_id !== "string" || payload.operation_id.length === 0) {
          throw new Error("broker_writer_ownership_unverified");
        }
        if (payload.operation_id !== request.operation_id) {
          continue;
        }
        const rawEpoch = Object.hasOwn(payload, "authoritative_writer_epoch")
          ? payload.authoritative_writer_epoch
          : payload.writer_epoch;
        if (!isNonNegativeSafeInt(rawEpoch)) {
          throw new Error("broker_writer_ownership_unverified");
        }
        const rawOwnerRunId = Object.hasOwn(payload, "active_run_id")
          ? payload.active_run_id
          : payload.run_id;
        const rawOwnerAttempt = Object.hasOwn(payload, "active_run_attempt")
          ? payload.active_run_attempt
          : payload.run_attempt;
        if (rawOwnerRunId !== undefined || rawOwnerAttempt !== undefined) {
          if (!isPositiveSafeInt(rawOwnerRunId) || !isPositiveSafeInt(rawOwnerAttempt)) {
            throw new Error("broker_writer_ownership_unverified");
          }
          const owners = ownersByEpoch.get(rawEpoch) ?? [];
          owners.push({ runId: rawOwnerRunId, runAttempt: rawOwnerAttempt });
          ownersByEpoch.set(rawEpoch, owners);
        }
        if (rawEpoch > maxWriterEpoch) {
          maxWriterEpoch = rawEpoch;
        }
      }
    }
    if (response.data.length < COMMENTS_PAGE_SIZE) {
      return {
        maxWriterEpoch,
        ownersAtMax: ownersByEpoch.get(maxWriterEpoch) ?? [],
      };
    }
  }

  throw new Error("broker_writer_ownership_unverified");
}

/**
 * Authoritatively verify exclusive v6 writer ownership against live GitHub PR,
 * workflow attempt, and App-authored PR ownership/intent comment markers.
 */
export async function verifyAuthoritativeV6WriterOwnership(
  github: V6OwnershipGitHubClient,
  params: VerifyV6WriterOwnershipParams,
): Promise<V6AuthoritativeWriterProof> {
  const { repository, request, claims, appId, pullToken, actionsToken } = params;
  if (!isPositiveSafeInt(appId)) {
    throw new Error("configuration_unavailable");
  }
  const runId = parsePositiveIntString(claims.run_id);
  const runAttempt = parsePositiveIntString(claims.run_attempt);
  if (runId === null || runAttempt === null) {
    throw new Error("broker_writer_ownership_unverified");
  }

  await verifyLivePullRequest(github, repository, request, pullToken);
  await verifyLiveWorkflowAttempt(github, repository, runId, runAttempt, actionsToken);
  const { maxWriterEpoch, ownersAtMax } = await scanAuthoritativeOwnershipMarkers(
    github,
    repository,
    request,
    appId,
    pullToken,
  );

  if (request.writer_epoch < maxWriterEpoch) {
    throw new Error("broker_stale_writer_rejected");
  }
  if (request.writer_epoch === maxWriterEpoch) {
    for (const owner of ownersAtMax) {
      if (owner.runId !== runId || owner.runAttempt !== runAttempt) {
        throw new Error("broker_stale_writer_rejected");
      }
    }
  }

  return {
    schema: "v6-writer-ownership-proof-v1",
    verified_by_broker: true,
    repository_id: request.repository_id,
    pull_request: request.pull_request,
    operation_id: request.operation_id,
    authoritative_writer_epoch: request.writer_epoch,
    active_run_id: runId,
    active_run_attempt: runAttempt,
    head_sha: request.head_sha,
  };
}
