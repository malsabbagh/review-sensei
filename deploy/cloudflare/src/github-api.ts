import { observationPermissions } from "./current-observation";
import { PullRequestLookupError, type PullRequestLookupOutcome } from "./pull-request-lookup-diagnostic";
import { WorkflowLookupError, lookupFailure, lookupHeaders, type LookupDiagnostic } from "./broker-lookup-diagnostic";
import { BrokerLifetime, type BrokerUpstream } from "./broker-lifetime";
import type { BrokerDispatchControl } from "./broker-dispatch-control";
import type { WorkerEnv } from "./env";

const MAX_RESPONSE_BYTES = 512 * 1024;
const MAX_PRIVATE_KEY_BYTES = 64 * 1024;
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const PUBLIC_WORKFLOW_REPOSITORY = "malsabbagh/review-sensei";
const PUBLIC_WORKFLOW_GIT_URL = "https://github.com/malsabbagh/review-sensei.git";
const PUBLIC_WORKFLOW_TAG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const PUBLIC_WORKFLOW_SHA_PATTERN = /^[a-f0-9]{40}$/;
const MAX_TAG_DEREFERENCE_DEPTH = 3;
const MAX_PUBLIC_REF_BYTES = 64 * 1024;
// GitHub always grants repository metadata read access to installation tokens.
// A capability can separately opt in to accepting contents:read only when its
// broker policy documents that GitHub may add the grant implicitly.
const IMPLICIT_METADATA_PERMISSION = "read";
const RETURNED_CONTENTS_READ_PERMISSION = "read";
// Permission names supported by the adapter. ADR 0057's check_publish
// capability requests `checks`: recognizing it still requires an exact match
// to the requested scope; unrequested Checks grants are rejected. The retired
// `variables` permission (payload spelling `actions_variables`) stays excluded:
// setup no longer provisions repository variables and no capability requests it.
const KNOWN_PERMISSION_NAMES = new Set([
  "actions",
  "checks",
  "contents",
  "metadata",
  "pull_requests",
  "workflows",
]);

export interface JsonObject {
  [key: string]: unknown;
}

export interface GitHubApiResponse {
  status: number;
  data: unknown;
  hasNextPage?: boolean;
  lookupMetadata?: Omit<LookupDiagnostic, "phase" | "failure" | "git_failure">;
}

/** Complete bounded history, restarting rather than mixing pagination offsets. */
export async function readBoundedHistory(
  readPage: (size: number, page: number) => Promise<GitHubApiResponse>,
  invalidCode: string,
): Promise<unknown[]> {
  let requests = 0;
  for (const size of [100, 50, 25, 5, 1]) {
    const items: unknown[] = [];
    try {
      for (let page = 1; requests < 10; page += 1) {
        requests += 1;
        const response = await readPage(size, page);
        if (response.status < 200 || response.status >= 300 ||
          !Array.isArray(response.data) || response.data.length > size) {
          throw new Error(invalidCode);
        }
        items.push(...response.data);
        if (items.length > 1000) throw new Error(invalidCode);
        if (response.data.length < size) return items;
      }
      throw new Error(invalidCode);
    } catch (error) {
      if (!(error instanceof Error) || error.message !== "github_response_too_large" ||
        size === 1 || requests >= 10) throw error;
      // The byte cap is unchanged. Discard this incomplete inventory and
      // restart at page one; failed physical reads consume the same budget.
    }
  }
  throw new Error(invalidCode);
}

class GitHubResponseError extends Error {
  constructor(message: string, readonly status: number, readonly metadata: ReturnType<typeof lookupHeaders>) { super(message); }
}

interface GitHubRetrySignal {
  isRateLimit: boolean;
  isTransient: boolean;
  retryAfterSeconds: number | null;
  rateLimitResetEpoch: number | null;
  retryAfterMs: number | undefined;
}

function classifyGitHubRetrySignal(
  status: number | null,
  metadata: ReturnType<typeof lookupHeaders> | undefined,
  data: unknown,
  nowMs: number,
): GitHubRetrySignal {
  const retryAfterSeconds = metadata?.retry_after ?? null;
  const rateLimitResetEpoch = metadata?.rate_limit_reset ?? null;
  const hasRateLimitMessage =
    isObject(data) &&
    typeof data.message === "string" &&
    /rate\s*limit|secondary\s*rate|abuse/i.test(data.message);
  const hasRateLimitHeader =
    metadata?.rate_limit_remaining === 0 ||
    (retryAfterSeconds !== null && retryAfterSeconds > 0);
  const isRateLimit =
    status === 429 ||
    (status === 403 && (hasRateLimitHeader || hasRateLimitMessage));
  const isTransient = status === null || (status >= 500 && status <= 599);
  const retryAfterMs =
    retryAfterSeconds !== null && retryAfterSeconds >= 0
      ? retryAfterSeconds * 1000
      : rateLimitResetEpoch !== null
        ? Math.max(0, rateLimitResetEpoch * 1000 - nowMs)
        : undefined;
  return {
    isRateLimit,
    isTransient,
    retryAfterSeconds,
    rateLimitResetEpoch,
    retryAfterMs,
  };
}

export interface InstallationToken {
  token: string;
  expiresAt: number;
  permissions: Record<string, string>;
}

export interface LiveIssueComment {
  id: number;
  body: string;
  login: string;
  userType: string;
  association: string;
  performedViaAppId?: number | null;
}

export interface IssueCommentsScanOptions {
  maxBodyBytes?: number;
  maxRequests?: number;
  maxComments?: number;
  startPage?: number;
  initialPageSize?: number;
  nowMs?: number;
}

export interface LiveIssueCommentPage {
  comments: LiveIssueComment[];
  truncated: boolean;
  incomplete?: boolean;
  nextPage?: number | null;
  pageSize?: number;
  retryAfterMs?: number;
  rateLimitResetEpoch?: number;
  rateLimited?: boolean;
  transientFailure?: boolean;
}

export interface LivePullRequestReview {
  id: number;
  login: string;
  userType: string;
  commit_id: string | null;
  body: string;
  state: string;
  performedViaAppId: number | null;
}

export interface LivePullRequestReviewComment {
  id: number;
  login: string;
  userType: string;
  commit_id: string | null;
  original_commit_id: string | null;
  body: string;
  pull_request_url: string;
  performedViaAppId: number | null;
}

export interface LiveCheckRun {
  id: number;
  appId: number | null;
  head_sha: string;
  external_id: string | null;
  name: string;
  status: string;
  conclusion: string | null;
  outputTitle: string | null;
  outputSummary: string | null;
  outputText: string | null;
}

export type RevocationOutcomeKind =
  | "http_204"
  | "http_401"
  | "http_429_or_ratelimit_403"
  | "permission_403"
  | "http_other_4xx"
  | "http_5xx_or_timeout";

export interface RevocationResult {
  status: number | null;
  kind: RevocationOutcomeKind;
  retryAfterSeconds: number | null;
  rateLimitResetEpoch: number | null;
  isRateLimit: boolean;
}

export interface PullRequestBinding {
  head_sha: string;
  base_repo_id: number;
  state: "open" | "closed";
  head_repo_id: number | null;
  is_fork_pr: boolean;
}

export interface WorkflowRunAttemptInfo {
  run_id: string;
  run_attempt: number;
  status: string;
  conclusion: string | null;
  head_sha: string;
}

const PUBLIC_WORKFLOW_TOKEN_SKEW_MS = 60_000;
const PUBLIC_WORKFLOW_CACHE_IDLE_TTL_MS = 60 * 60 * 1000;
const MAX_PUBLIC_WORKFLOW_TOKEN_CACHE_ENTRIES = 16;
const publicWorkflowTokenCache = new Map<
  string,
  { installationId: number; token?: InstallationToken; cachedAt: number }
>();

const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function syncSha256Hex(value: string): string {
  const bytes = new TextEncoder().encode(value);
  const bitLen = bytes.length * 8;
  const paddedLen = Math.ceil((bytes.length + 9) / 64) * 64;
  const buf = new Uint8Array(paddedLen);
  buf.set(bytes);
  buf[bytes.length] = 0x80;
  const view = new DataView(buf.buffer);
  view.setUint32(paddedLen - 8, Math.floor(bitLen / 0x1_0000_0000), false);
  view.setUint32(paddedLen - 4, bitLen >>> 0, false);

  const h = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));

  for (let offset = 0; offset < paddedLen; offset += 64) {
    for (let i = 0; i < 16; i += 1) {
      w[i] = view.getUint32(offset + i * 4, false);
    }
    for (let i = 16; i < 64; i += 1) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i += 1) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (hh + s1 + ch + SHA256_K[i] + w[i]) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + maj) >>> 0;
      hh = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    h[0] = (h[0] + a) >>> 0;
    h[1] = (h[1] + b) >>> 0;
    h[2] = (h[2] + c) >>> 0;
    h[3] = (h[3] + d) >>> 0;
    h[4] = (h[4] + e) >>> 0;
    h[5] = (h[5] + f) >>> 0;
    h[6] = (h[6] + g) >>> 0;
    h[7] = (h[7] + hh) >>> 0;
  }
  return [...h].map(word => word.toString(16).padStart(8, "0")).join("");
}

export function resetPublicWorkflowTokenCacheForTesting(): void {
  publicWorkflowTokenCache.clear();
}

export function publicWorkflowTokenCacheSnapshotForTesting(): Array<{
  key: string;
  installationId: number;
  hasToken: boolean;
}> {
  return [...publicWorkflowTokenCache.entries()].map(([key, entry]) => ({
    key,
    installationId: entry.installationId,
    hasToken: entry.token !== undefined,
  }));
}

function prunePublicWorkflowTokenCache(now = Date.now()): void {
  for (const [key, entry] of publicWorkflowTokenCache.entries()) {
    if (now - entry.cachedAt >= PUBLIC_WORKFLOW_CACHE_IDLE_TTL_MS) {
      publicWorkflowTokenCache.delete(key);
    } else if (
      entry.token &&
      entry.token.expiresAt - now <= PUBLIC_WORKFLOW_TOKEN_SKEW_MS
    ) {
      entry.token = undefined;
    }
  }
  while (publicWorkflowTokenCache.size > MAX_PUBLIC_WORKFLOW_TOKEN_CACHE_ENTRIES) {
    const oldest = publicWorkflowTokenCache.keys().next();
    if (oldest.done) break;
    publicWorkflowTokenCache.delete(oldest.value);
  }
}

function publicWorkflowTag(value: string): string {
  if (
    typeof value !== "string" ||
    !PUBLIC_WORKFLOW_TAG_PATTERN.test(value) ||
    value.includes("..") ||
    value.endsWith(".") ||
    value.endsWith(".lock")
  ) {
    throw new Error("github_workflow_tag_invalid");
  }
  return value;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function canonicalPermissionName(value: string): string | null {
  const normalized = value.trim().toLowerCase();
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(normalized)) {
    return null;
  }
  // Every name must be an explicitly supported canonical permission, so a
  // retired or future GitHub permission cannot silently normalize into this
  // boundary.
  return KNOWN_PERMISSION_NAMES.has(normalized) ? normalized : null;
}

/**
 * Normalize and validate a GitHub permission map. Own enumerable keys only
 * are considered, so inherited properties can never grant authority.
 */
function normalizePermissionMap(value: unknown): Record<string, string> | null {
  if (!isObject(value)) {
    return null;
  }
  const normalized = Object.create(null) as Record<string, string>;
  for (const [rawName, rawLevel] of Object.entries(value)) {
    const name = canonicalPermissionName(rawName);
    const level = typeof rawLevel === "string" ? rawLevel.trim().toLowerCase() : null;
    if (name === null || (level !== "read" && level !== "write" && level !== "none")) {
      return null;
    }
    // Reject aliases which collapse to one canonical name rather than
    // silently allowing the last property to win.
    if (Object.hasOwn(normalized, name)) {
      return null;
    }
    normalized[name] = level;
  }
  return normalized;
}

/**
 * Require the response scope to equal the requested scope plus only GitHub's
 * known implicit read grants. This prevents a token with inherited write
 * capabilities from crossing the broker boundary while accepting legitimate
 * repository-data permissions returned by GitHub.
 */
function hasExactPermissions(
  granted: unknown,
  requested: Record<string, string>,
  acceptReturnedContentsRead = false,
): boolean {
  const actual = normalizePermissionMap(granted);
  const expectedRequested = normalizePermissionMap(requested);
  if (actual === null || expectedRequested === null || Object.keys(expectedRequested).length === 0) {
    return false;
  }
  // Requests made by this adapter are always positive read/write grants. In
  // particular, metadata is an implicit read-only permission and must never
  // be elevated by a caller-controlled map.
  if (
    Object.entries(expectedRequested).some(
      ([name, level]) =>
        (level !== "read" && level !== "write") ||
        (name === "metadata" && level !== "read"),
    )
  ) {
    return false;
  }
  // Every requested permission must be granted at exactly its requested
  // level. The intrinsic metadata grant is mandatory even when callers omit
  // it from their request.
  for (const [name, level] of Object.entries(expectedRequested)) {
    if (actual[name] !== level) {
      return false;
    }
  }
  if (
    !Object.hasOwn(expectedRequested, "metadata") &&
    actual.metadata !== IMPLICIT_METADATA_PERMISSION
  ) {
    return false;
  }
  // Apart from mandatory metadata, the broker accepts contents:read only for
  // capabilities that explicitly opt in. Requested contents grants were
  // already required at their exact level above, so this exception applies
  // only when contents was omitted from the requested scope. Every other
  // extra permission, especially any write grant, is rejected.
  return Object.entries(actual).every(([name, level]) => {
    if (Object.hasOwn(expectedRequested, name)) {
      return true;
    }
    return (
      (name === "metadata" && level === IMPLICIT_METADATA_PERMISSION) ||
      (acceptReturnedContentsRead &&
        name === "contents" &&
        level === RETURNED_CONTENTS_READ_PERMISSION)
    );
  });
}

function base64Url(value: Uint8Array): string {
  let binary = "";
  for (let index = 0; index < value.length; index += 0x8000) {
    binary += String.fromCharCode(...value.subarray(index, index + 0x8000));
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function derLength(length: number): Uint8Array {
  if (length < 0x80) {
    return Uint8Array.of(length);
  }
  const bytes: number[] = [];
  let remaining = length;
  while (remaining > 0) {
    bytes.unshift(remaining & 0xff);
    remaining >>>= 8;
  }
  return Uint8Array.of(0x80 | bytes.length, ...bytes);
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const output = new Uint8Array(parts.reduce((total, part) => total + part.byteLength, 0));
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.byteLength;
  }
  return output;
}

function wrapRsaPrivateKey(pkcs1: Uint8Array): Uint8Array {
  const version = Uint8Array.of(0x02, 0x01, 0x00);
  const algorithm = Uint8Array.of(
    0x30,
    0x0d,
    0x06,
    0x09,
    0x2a,
    0x86,
    0x48,
    0x86,
    0xf7,
    0x0d,
    0x01,
    0x01,
    0x01,
    0x05,
    0x00,
  );
  const privateKey = concat(Uint8Array.of(0x04), derLength(pkcs1.byteLength), pkcs1);
  const sequence = concat(version, algorithm, privateKey);
  return concat(Uint8Array.of(0x30), derLength(sequence.byteLength), sequence);
}

function pemBytes(value: string): Uint8Array {
  if (!value || new TextEncoder().encode(value).byteLength > MAX_PRIVATE_KEY_BYTES) {
    throw new Error("github_app_key_invalid");
  }
  const match = value.match(
    /-----BEGIN (PRIVATE KEY|RSA PRIVATE KEY)-----([\s\S]*?)-----END \1-----/,
  );
  if (!match) {
    throw new Error("github_app_key_invalid");
  }
  const encoded = match[2].replaceAll(/\s+/g, "");
  if (
    !encoded ||
    encoded.length % 4 === 1 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)
  ) {
    throw new Error("github_app_key_invalid");
  }
  try {
    const binary = atob(encoded);
    const der = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return match[1] === "RSA PRIVATE KEY" ? wrapRsaPrivateKey(der) : der;
  } catch {
    throw new Error("github_app_key_invalid");
  }
}

function repositoryPath(repository: string, suffix = ""): string {
  if (!REPOSITORY_PATTERN.test(repository)) {
    throw new Error("github_repository_invalid");
  }
  const [owner, name] = repository.split("/");
  return `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}${suffix}`;
}

async function boundedJson(response: Response, scope: BrokerUpstream): Promise<unknown> {
  const length = response.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_RESPONSE_BYTES)) {
    await response.body?.cancel();
    throw new Error("github_response_too_large");
  }
  if (response.body === null) {
    return null;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const next = await scope.read(reader);
    if (next.done) {
      break;
    }
    total += next.value.byteLength;
    if (total > MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new Error("github_response_too_large");
    }
    chunks.push(next.value);
  }
  if (total === 0) {
    return null;
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)) as unknown;
  } catch {
    throw new Error("github_response_invalid");
  }
}

async function boundedBytes(response: Response, maximum: number, scope: BrokerUpstream): Promise<Uint8Array> {
  const length = response.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > maximum)) {
    throw new Error("github_response_too_large");
  }
  if (response.body === null) {
    return new Uint8Array();
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const next = await scope.read(reader);
    if (next.done) {
      break;
    }
    total += next.value.byteLength;
    if (total > maximum) {
      await reader.cancel();
      throw new Error("github_response_too_large");
    }
    chunks.push(next.value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

export interface PublicWorkflowRuntimeShas {
  /** Peeled commit SHA the configured tag ultimately references. */
  commitSha: string;
  /** Direct refs/tags/<tag> object SHA (commit for lightweight tags, tag object for annotated tags). */
  refSha: string;
}

function advertisedTagShas(bytes: Uint8Array, tag: string): PublicWorkflowRuntimeShas {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const directRef = `refs/tags/${tag}`;
  const peeledRef = `${directRef}^{}`;
  let direct: string | undefined;
  let peeled: string | undefined;
  let offset = 0;
  while (offset + 4 <= bytes.byteLength) {
    const header = decoder.decode(bytes.subarray(offset, offset + 4));
    if (!/^[0-9a-f]{4}$/.test(header)) {
      throw new Error("github_workflow_tag_invalid");
    }
    const length = Number.parseInt(header, 16);
    if (length === 0) {
      offset += 4;
      continue;
    }
    if (length === 1) {
      break;
    }
    if (length < 4 || offset + length > bytes.byteLength) {
      throw new Error("github_workflow_tag_invalid");
    }
    const packet = decoder
      .decode(bytes.subarray(offset + 4, offset + length))
      .replace(/\n$/, "");
    offset += length;
    const separator = packet.indexOf(" ");
    if (separator < 40) {
      continue;
    }
    const sha = packet.slice(0, separator);
    const ref = packet.slice(separator + 1).split("\0", 1)[0];
    if (!PUBLIC_WORKFLOW_SHA_PATTERN.test(sha)) {
      continue;
    }
    if (ref === directRef) {
      direct = sha;
    } else if (ref === peeledRef) {
      peeled = sha;
    }
  }
  const commitSha = peeled ?? direct;
  if (!commitSha || !direct) {
    throw new Error("github_workflow_tag_unavailable");
  }
  return { commitSha, refSha: direct };
}

function advertisedTagSha(bytes: Uint8Array, tag: string): string {
  return advertisedTagShas(bytes, tag).commitSha;
}

const PULL_REQUEST_HEAD_RETRY_DELAYS_MS = [200, 500] as const;
const RETRYABLE_PR_HTTP_STATUSES = new Set([429, 500, 502, 503, 504]);
const defaultRetryDelay = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/** Bounded GitHub transport shared by the broker and setup adapters. */
export class GitHubApi {
  private privateKey: Promise<CryptoKey> | undefined;
  private cachedPublicWorkflowKeyInput:
    | { apiUrl: string; appId: string; rawKey: string; key: string }
    | undefined;
  private readonly apiUrl: string;

  constructor(
    private readonly env: WorkerEnv,
    private readonly dispatchControl?: BrokerDispatchControl,
    private readonly lifetime?: BrokerLifetime,
    private readonly retryDelay: (ms: number) => Promise<void> = defaultRetryDelay,
    private readonly reserveDispatches = 0,
  ) {
    const apiUrl = (env.GITHUB_API_URL ?? "https://api.github.com").trim();
    if (!/^https:\/\//i.test(apiUrl) && !/^http:\/\/(?:127\.0\.0\.1|localhost)(?::|\/|$)/i.test(apiUrl)) {
      throw new Error("github_api_url_invalid");
    }
    this.apiUrl = apiUrl.replace(/\/+$/, "");
  }

  withDispatchControl(control: BrokerDispatchControl): GitHubApi {
    return new GitHubApi(this.env, control, this.lifetime, this.retryDelay, this.reserveDispatches);
  }

  withRequestLifetime(lifetime: BrokerLifetime, control?: BrokerDispatchControl): GitHubApi {
    return new GitHubApi(this.env, control, lifetime, this.retryDelay, this.reserveDispatches);
  }

  withHeadReserve(reserveDispatches: number): GitHubApi {
    return new GitHubApi(this.env, this.dispatchControl, this.lifetime, this.retryDelay, reserveDispatches);
  }

  private async key(): Promise<CryptoKey> {
    this.privateKey ??= crypto.subtle.importKey(
      "pkcs8",
      pemBytes(this.env.GITHUB_APP_PRIVATE_KEY).buffer as ArrayBuffer,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["sign"],
    );
    try {
      return await this.privateKey;
    } catch {
      throw new Error("github_app_key_invalid");
    }
  }

  private async appJwt(): Promise<string> {
    const appId = this.env.GITHUB_APP_ID;
    if (!/^[1-9][0-9]{0,18}$/.test(appId)) {
      throw new Error("github_app_id_invalid");
    }
    const now = Math.floor(Date.now() / 1000);
    const header = base64Url(new TextEncoder().encode('{"alg":"RS256","typ":"JWT"}'));
    const payload = base64Url(
      new TextEncoder().encode(JSON.stringify({ iss: appId, iat: now - 30, exp: now + 540 })),
    );
    const input = `${header}.${payload}`;
    const signature = await crypto.subtle.sign(
      "RSASSA-PKCS1-v1_5",
      await this.key(),
      new TextEncoder().encode(input),
    );
    return `${input}.${base64Url(new Uint8Array(signature))}`;
  }

  async request(
    method: string,
    path: string,
    token: string | undefined,
    body?: JsonObject,
  ): Promise<GitHubApiResponse> {
    if (!path.startsWith("/") || /[\r\n]/.test(path)) {
      throw new Error("github_path_invalid");
    }
    let observed: {status: number; metadata: ReturnType<typeof lookupHeaders>} | undefined;
    try {
      return await (this.lifetime ?? new BrokerLifetime()).upstream(async scope => {
        const response = await scope.fetch(`${this.apiUrl}${path}`, {
          method,
          redirect: "manual",
          headers: {
            accept: "application/vnd.github+json",
            ...(token === undefined ? {} : { authorization: `Bearer ${token}` }),
            "user-agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
            "x-github-api-version": "2022-11-28",
            ...(body === undefined ? {} : { "content-type": "application/json" }),
          },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        observed = {status: response.status, metadata: lookupHeaders(response.headers)};
        await this.dispatchControl?.checkResponse(response);
        const data = await boundedJson(response, scope);
        this.dispatchControl?.check();
        const metadata = lookupHeaders(response.headers);
        const linkHeader = response.headers.get("link");
        const hasNextPage = typeof linkHeader === "string" && /;\s*rel=["']?next["']?(?:\s*;|\s*,|\s*$)/i.test(linkHeader);
        return {
          status: response.status,
          data,
          ...(hasNextPage ? { hasNextPage: true } : linkHeader !== null ? { hasNextPage: false } : {}),
          ...(Object.keys(metadata).length ? {lookupMetadata: metadata} : {}),
        };
      }, this.dispatchControl, path.includes("/git/ref/") ? "workflow_ref" : path.includes("/git/tags/") ? "workflow_tag" : path.endsWith("/access_tokens") || path === "/installation/token" ? "installation_token" : method === "GET" ? "github_read" : "github_write");
    } catch (error) {
      if (error instanceof Error && /^(broker_control_|broker_request_)/.test(error.message)) throw error;
      if (observed) throw new GitHubResponseError(error instanceof Error && ["github_response_invalid", "github_response_too_large", "broker_upstream_timeout"].includes(error.message) ? error.message : "github_response_unavailable", observed.status, observed.metadata);
      throw error;
    }
  }

  async installationFor(repository: string): Promise<number | null> {
    const response = await this.request("GET", `${repositoryPath(repository)}/installation`, await this.appJwt());
    if (response.status === 404 || response.status === 403) {
      return null;
    }
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_installation_lookup_failed");
    }
    const id = response.data.id;
    return typeof id === "number" && Number.isSafeInteger(id) && id > 0 ? id : null;
  }

  /** One authenticated lookup. Association and caller-supplied role are not permission. */
  async actorRepositoryPermission(repository: string, token: string, id: number, login: string): Promise<{ repository_permission: string; repository_role: string }> {
    if (!Number.isSafeInteger(id) || id <= 0 || !/^[A-Za-z0-9-]{1,39}$/.test(login)) throw new Error("github_actor_permission_invalid");
    const response = await this.request("GET", `${repositoryPath(repository)}/collaborators/${encodeURIComponent(login)}/permission`, token);
    const data = response.data;
    if (response.status !== 200 || !isObject(data) || !isObject(data.user) || data.user.id !== id ||
        typeof data.user.login !== "string" || data.user.login.toLowerCase() !== login.toLowerCase() || data.user.type !== "User" ||
        !["none", "read", "write", "admin"].includes(data.permission as string) ||
        typeof data.role_name !== "string" || !/^[A-Za-z0-9_. -]{1,100}$/.test(data.role_name)) throw new Error("github_actor_permission_invalid");
    return { repository_permission: data.permission as string, repository_role: data.role_name };
  }

  async installationObservationFor(repository: string): Promise<{ id: number; repository_selection: "all" | "selected"; permissions: Record<string, string> } | null> {
    const response = await this.request("GET", `${repositoryPath(repository)}/installation`, await this.appJwt());
    if (response.status === 404 || response.status === 403) return null;
    if (response.status !== 200 || !isObject(response.data) || !Number.isSafeInteger(response.data.id) || (response.data.id as number) <= 0 || !["all", "selected"].includes(response.data.repository_selection as string)) throw new Error("github_installation_observation_invalid");
    return { id: response.data.id as number, repository_selection: response.data.repository_selection as "all" | "selected", permissions: observationPermissions(response.data.permissions) };
  }

  async repositoryInfo(
    repository: string,
    token: string,
    observed = false,
  ): Promise<{ id: number; fork: boolean; full_name?: string; owner_id?: number; owner_login?: string } | null> {
    const response = await this.request("GET", repositoryPath(repository), token);
    if (response.status === 404 || response.status === 403) {
      return null;
    }
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_repository_lookup_failed");
    }
    const id = response.data.id;
    const fork = response.data.fork;
    if (typeof id !== "number" || !Number.isSafeInteger(id) || id <= 0 || typeof fork !== "boolean") {
      throw new Error("github_repository_response_invalid");
    }
    if (observed) {
      const owner = response.data.owner;
      const fullName = response.data.full_name;
      if (!isObject(owner) || !Number.isSafeInteger(owner.id) || (owner.id as number) <= 0 || typeof owner.login !== "string" || !/^[A-Za-z0-9-]{1,100}$/.test(owner.login) || typeof fullName !== "string" || fullName.toLowerCase() !== repository.toLowerCase() || fullName.split("/")[0].toLowerCase() !== owner.login.toLowerCase()) throw new Error("github_repository_observation_invalid");
      return { id, fork, full_name: fullName, owner_id: owner.id as number, owner_login: owner.login };
    }
    return { id, fork };
  }

  /** Return the current immutable head for one pull request, or null when it cannot be read. */
  async pullRequestHead(
    repository: string,
    pullRequest: number,
    token: string,
    reserveDispatches = this.reserveDispatches,
  ): Promise<string | null> {
    if (!Number.isSafeInteger(pullRequest) || pullRequest <= 0) {
      throw new Error("github_pull_request_invalid");
    }
    const maxAttempts = 1 + PULL_REQUEST_HEAD_RETRY_DELAYS_MS.length;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      let response: GitHubApiResponse | undefined;
      let transientError: PullRequestLookupError | undefined;
      try {
        response = await this.request("GET", `${repositoryPath(repository)}/pulls/${pullRequest}`, token);
      } catch (error) {
        if (!(error instanceof GitHubResponseError)) throw error;
        const outcomes: Record<string, PullRequestLookupOutcome> = {
          github_response_invalid: "malformed", github_response_too_large: "oversized",
          broker_upstream_timeout: "timeout", github_response_unavailable: "transport",
        };
        const lookupError = new PullRequestLookupError(error.message, outcomes[error.message] ?? "unknown", error.status);
        if (error.message !== "broker_upstream_timeout" && error.message !== "github_response_unavailable") {
          throw lookupError;
        }
        transientError = lookupError;
      }
      if (transientError === undefined) {
        // A 403 on this read is a permission, installation-scope, or rate-limit
        // problem, not a missing or stale pull request. Reporting it as an absent
        // head would send the operator after the wrong failure, so it keeps its
        // own diagnosis instead of collapsing into the 404 result.
        if (response!.status === 403) {
          throw new PullRequestLookupError("github_pull_request_forbidden", "http", response!.status);
        }
        if (response!.status === 404) {
          return null;
        }
        if (response!.status >= 200 && response!.status < 300) {
          if (!isObject(response!.data)) {
            throw new PullRequestLookupError("github_pull_request_lookup_failed", "malformed", response!.status);
          }
          const head = response!.data.head;
          if (!isObject(head) || typeof head.sha !== "string" || !PUBLIC_WORKFLOW_SHA_PATTERN.test(head.sha)) {
            throw new PullRequestLookupError("github_pull_request_response_invalid", "malformed", response!.status);
          }
          return head.sha;
        }
        const lookupError = new PullRequestLookupError("github_pull_request_lookup_failed", "http", response!.status);
        if (!RETRYABLE_PR_HTTP_STATUSES.has(response!.status)) {
          throw lookupError;
        }
        transientError = lookupError;
      }
      if (attempt >= PULL_REQUEST_HEAD_RETRY_DELAYS_MS.length) {
        throw transientError;
      }
      const delayMs = PULL_REQUEST_HEAD_RETRY_DELAYS_MS[attempt];
      const controlSpare = this.dispatchControl === undefined
        ? Number.POSITIVE_INFINITY
        : this.dispatchControl.request.max_dispatches - this.dispatchControl.receipt().dispatched;
      const lifetimeSpare = this.lifetime === undefined
        ? Number.POSITIVE_INFINITY
        : this.lifetime.remainingDispatches();
      if (Math.min(controlSpare, lifetimeSpare) <= reserveDispatches) {
        throw transientError;
      }
      const remainingMs = this.lifetime?.check() ?? this.dispatchControl?.check();
      if (remainingMs !== undefined && remainingMs <= delayMs) {
        throw transientError;
      }
      await this.retryDelay(delayMs);
      this.lifetime?.check();
      this.dispatchControl?.check();
    }
    throw new PullRequestLookupError("github_pull_request_lookup_failed", "unknown");
  }

  /** Read pull request head/base/fork binding metadata for v6 coordinator validation. */
  async pullRequestBinding(
    repository: string,
    pullRequest: number,
    token: string,
  ): Promise<PullRequestBinding | null> {
    if (!Number.isSafeInteger(pullRequest) || pullRequest <= 0) {
      throw new Error("github_pull_request_invalid");
    }
    const response = await this.request(
      "GET",
      `${repositoryPath(repository)}/pulls/${pullRequest}`,
      token,
    );
    if (response.status === 404) {
      return null;
    }
    if (response.status === 403) {
      throw new PullRequestLookupError("github_pull_request_forbidden", "http", response.status);
    }
    if (response.status < 200 || response.status >= 300) {
      throw new PullRequestLookupError("github_pull_request_lookup_failed", "http", response.status);
    }
    if (!isObject(response.data)) {
      throw new PullRequestLookupError("github_pull_request_response_invalid", "malformed", response.status);
    }
    const head = response.data.head;
    const base = response.data.base;
    const state = response.data.state;
    if (
      !isObject(head) ||
      typeof head.sha !== "string" ||
      !PUBLIC_WORKFLOW_SHA_PATTERN.test(head.sha) ||
      !isObject(base) ||
      !isObject(base.repo) ||
      typeof base.repo.id !== "number" ||
      !Number.isSafeInteger(base.repo.id) ||
      base.repo.id <= 0 ||
      (state !== "open" && state !== "closed")
    ) {
      throw new PullRequestLookupError("github_pull_request_response_invalid", "malformed", response.status);
    }
    const baseRepoId = base.repo.id;
    if (head.repo === null || head.repo === undefined) {
      return null;
    }
    if (
      !isObject(head.repo) ||
      typeof head.repo.id !== "number" ||
      !Number.isSafeInteger(head.repo.id) ||
      head.repo.id <= 0
    ) {
      throw new PullRequestLookupError("github_pull_request_response_invalid", "malformed", response.status);
    }
    const headRepoId = head.repo.id;
    const isForkPr = headRepoId !== baseRepoId || (head.repo.fork === true && base.repo.fork !== true);
    return {
      head_sha: head.sha,
      base_repo_id: baseRepoId,
      state,
      head_repo_id: headRepoId,
      is_fork_pr: isForkPr,
    };
  }

  /** Verify workflow run attempt status using existing `actions: read` permission. */
  async workflowRunAttempt(
    repository: string,
    runId: string,
    attempt: string,
    token: string,
  ): Promise<WorkflowRunAttemptInfo | null> {
    if (!/^[1-9][0-9]{0,19}$/.test(runId) || !/^[1-9][0-9]{0,9}$/.test(attempt)) {
      throw new Error("github_workflow_run_invalid");
    }
    const expectedRunId = Number(runId);
    const expectedAttempt = Number(attempt);
    if (!Number.isSafeInteger(expectedRunId) || !Number.isSafeInteger(expectedAttempt)) {
      throw new Error("github_workflow_run_invalid");
    }
    const root = repositoryPath(repository);
    let response = await this.request(
      "GET",
      `${root}/actions/runs/${runId}/attempts/${attempt}`,
      token,
    );
    let usedFallback = false;
    if (response.status === 404) {
      usedFallback = true;
      response = await this.request("GET", `${root}/actions/runs/${runId}`, token);
    }
    if (response.status === 404) {
      return null;
    }
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_workflow_run_lookup_failed");
    }
    const data = response.data;
    const rawAttempt = data.run_attempt;
    if (
      usedFallback &&
      (typeof rawAttempt !== "number" ||
        !Number.isSafeInteger(rawAttempt) ||
        rawAttempt !== expectedAttempt)
    ) {
      return null;
    }
    if (
      data.id !== expectedRunId ||
      (rawAttempt !== undefined && rawAttempt !== expectedAttempt) ||
      typeof data.status !== "string" ||
      !data.status.trim() ||
      (data.conclusion !== null && (typeof data.conclusion !== "string" || !data.conclusion.trim())) ||
      typeof data.head_sha !== "string" ||
      !PUBLIC_WORKFLOW_SHA_PATTERN.test(data.head_sha)
    ) {
      throw new Error("github_workflow_run_response_invalid");
    }
    return {
      run_id: runId,
      run_attempt: expectedAttempt,
      status: data.status,
      conclusion: (data.conclusion as string | null) ?? null,
      head_sha: data.head_sha,
    };
  }

  private parseIssueCommentObject(
    raw: unknown,
    expectedIssueUrl: string,
    maxBodyBytes: number,
    expectedId?: number,
  ): LiveIssueComment {
    if (!isObject(raw)) {
      throw new Error("github_issue_comment_response_invalid");
    }
    const id = raw.id;
    const body = raw.body;
    const association = raw.author_association;
    const issueUrl = raw.issue_url;
    const user = raw.user;
    if (
      typeof id !== "number" ||
      !Number.isSafeInteger(id) ||
      id <= 0 ||
      (expectedId !== undefined && id !== expectedId) ||
      typeof body !== "string" ||
      new TextEncoder().encode(body).byteLength > maxBodyBytes ||
      typeof association !== "string" ||
      issueUrl !== expectedIssueUrl ||
      !isObject(user) ||
      typeof user.login !== "string" ||
      user.login.length === 0 ||
      user.login.length > 256 ||
      typeof user.type !== "string"
    ) {
      throw new Error("github_issue_comment_response_invalid");
    }
    const performedViaAppId = isObject(raw.performed_via_github_app)
      ? this.optionalPositiveId(raw.performed_via_github_app.id)
      : null;
    return {
      id,
      body,
      login: user.login,
      userType: user.type,
      association,
      ...(performedViaAppId !== null ? { performedViaAppId } : {}),
    };
  }

  /**
   * Read the command comment from GitHub immediately before broker issuance.
   * Event payload identity is deliberately not trusted for a session mutation:
   * a deleted, edited, bot-authored, or cross-issue comment must fail closed.
   */
  async issueComment(
    repository: string,
    pullRequest: number,
    commentId: number,
    token: string,
    maxBodyBytes = 4 * 1024,
  ): Promise<LiveIssueComment | null> {
    if (
      !Number.isSafeInteger(pullRequest) || pullRequest <= 0 ||
      !Number.isSafeInteger(commentId) || commentId <= 0
    ) {
      throw new Error("github_issue_comment_invalid");
    }
    const response = await this.request(
      "GET",
      `${repositoryPath(repository)}/issues/comments/${commentId}`,
      token,
    );
    if (response.status === 404) {
      return null;
    }
    if (response.status === 403) {
      throw new Error("github_issue_comment_forbidden");
    }
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_issue_comment_lookup_failed");
    }
    const expectedIssueUrl = `${this.apiUrl}${repositoryPath(repository)}/issues/${pullRequest}`;
    return this.parseIssueCommentObject(response.data, expectedIssueUrl, maxBodyBytes, commentId);
  }

  /**
   * Bounded adaptive, resumable pagination of issue comments modified at or after `sinceIso`.
   * Steps down page sizes [100, 12, 5, 1] on github_response_too_large (preserving covered
   * offset when resuming from `startPage > 1`) and deduplicating by comment id within bounded
   * ceilings. Honors `Link: rel="next"` / `hasNextPage === false` so an exact full final page
   * is never treated as truncated. Returns structured resumable state on request/comment
   * ceilings, 429/rate-limited 403, or 5xx/timeout without discarding scanned progress.
   * When pagination spans multiple pages in a turn (N >= 2), verifies page ordered ID tuple
   * equality and tail monotonicity (with bounded retries) so deletions cannot silently shift
   * comments across page boundaries.
   */
  async issueCommentsSince(
    repository: string,
    pullRequest: number,
    sinceIso: string,
    token: string,
    maxBodyBytesOrOptions: number | IssueCommentsScanOptions = 65536,
    maxRequestsArg = 40,
    maxCommentsArg = 1000,
  ): Promise<LiveIssueCommentPage> {
    const options: IssueCommentsScanOptions =
      typeof maxBodyBytesOrOptions === "number"
        ? {
            maxBodyBytes: maxBodyBytesOrOptions,
            maxRequests: maxRequestsArg,
            maxComments: maxCommentsArg,
          }
        : maxBodyBytesOrOptions;
    const maxBodyBytes = options.maxBodyBytes ?? 65536;
    const maxRequests = Math.max(1, options.maxRequests ?? 40);
    const maxComments = Math.max(1, options.maxComments ?? 1000);
    const startPage = options.startPage ?? 1;
    const initialPageSize = options.initialPageSize ?? 100;
    const nowMs = options.nowMs ?? Date.now();

    if (
      !Number.isSafeInteger(pullRequest) ||
      pullRequest <= 0 ||
      typeof sinceIso !== "string" ||
      !sinceIso.trim() ||
      !Number.isFinite(Date.parse(sinceIso)) ||
      !Number.isSafeInteger(startPage) ||
      startPage <= 0 ||
      !Number.isSafeInteger(initialPageSize) ||
      initialPageSize <= 0 ||
      initialPageSize > 100
    ) {
      throw new Error("github_issue_comment_invalid");
    }
    const repoBase = repositoryPath(repository);
    const expectedIssueUrl = `${this.apiUrl}${repoBase}/issues/${pullRequest}`;
    const encodedSince = encodeURIComponent(sinceIso);
    const defaultLadder = [100, 12, 5, 1];
    const pageSizes = defaultLadder.includes(initialPageSize)
      ? defaultLadder.filter((size) => size <= initialPageSize)
      : [initialPageSize, ...defaultLadder.filter((size) => size < initialPageSize)];
    const coveredOffset = (startPage - 1) * initialPageSize;
    const maxStabilityRetries = 2;
    let requests = 0;

    type FetchPageResult =
      | {
          kind: "ok";
          items: LiveIssueComment[];
          hasMorePage: boolean;
          hasNextPageExplicit: boolean | undefined;
        }
      | { kind: "too_large" }
      | {
          kind: "paused";
          rateLimited: boolean;
          transientFailure: boolean;
          retryAfterMs: number | undefined;
          rateLimitResetEpoch: number | undefined;
        };

    const fetchPage = async (size: number, page: number): Promise<FetchPageResult> => {
      requests += 1;
      let response: GitHubApiResponse;
      try {
        response = await this.request(
          "GET",
          `${repoBase}/issues/${pullRequest}/comments?per_page=${size}&since=${encodedSince}&page=${page}`,
          token,
        );
      } catch (error) {
        if (
          error instanceof Error &&
          error.message === "github_response_too_large" &&
          size > 1
        ) {
          return { kind: "too_large" };
        }
        if (
          error instanceof Error &&
          /^(broker_control_|broker_request_)/.test(error.message) &&
          error.message !== "broker_upstream_timeout"
        ) {
          throw error;
        }
        if (error instanceof GitHubResponseError) {
          const signal = classifyGitHubRetrySignal(error.status, error.metadata, null, nowMs);
          if (signal.isRateLimit) {
            return {
              kind: "paused",
              rateLimited: true,
              transientFailure: false,
              retryAfterMs: signal.retryAfterMs,
              rateLimitResetEpoch: signal.rateLimitResetEpoch ?? undefined,
            };
          }
          if (
            error.message === "broker_upstream_timeout" ||
            error.message === "github_response_unavailable" ||
            signal.isTransient
          ) {
            return {
              kind: "paused",
              rateLimited: false,
              transientFailure: true,
              retryAfterMs: signal.retryAfterMs,
              rateLimitResetEpoch: signal.rateLimitResetEpoch ?? undefined,
            };
          }
          throw error;
        }
        if (
          error instanceof Error &&
          (error.message === "broker_upstream_timeout" ||
            error.message === "github_response_unavailable")
        ) {
          return {
            kind: "paused",
            rateLimited: false,
            transientFailure: true,
            retryAfterMs: undefined,
            rateLimitResetEpoch: undefined,
          };
        }
        throw error;
      }

      const signal = classifyGitHubRetrySignal(
        response.status,
        response.lookupMetadata,
        response.data,
        nowMs,
      );
      if (signal.isRateLimit) {
        return {
          kind: "paused",
          rateLimited: true,
          transientFailure: false,
          retryAfterMs: signal.retryAfterMs,
          rateLimitResetEpoch: signal.rateLimitResetEpoch ?? undefined,
        };
      }
      if (response.status === 403) {
        throw new Error("github_issue_comment_forbidden");
      }
      if (signal.isTransient) {
        return {
          kind: "paused",
          rateLimited: false,
          transientFailure: true,
          retryAfterMs: signal.retryAfterMs,
          rateLimitResetEpoch: signal.rateLimitResetEpoch ?? undefined,
        };
      }
      if (response.status < 200 || response.status >= 300 || !Array.isArray(response.data)) {
        throw new Error("github_issue_comment_lookup_failed");
      }
      const hasMorePage =
        response.hasNextPage !== undefined
          ? response.hasNextPage === true
          : response.data.length >= size;
      const items = response.data.map((entry) =>
        this.parseIssueCommentObject(entry, expectedIssueUrl, maxBodyBytes),
      );
      return {
        kind: "ok",
        items,
        hasMorePage,
        hasNextPageExplicit: response.hasNextPage,
      };
    };

    const pausedPage = (
      comments: LiveIssueComment[],
      nextPage: number,
      pageSize: number,
      paused?: Extract<FetchPageResult, { kind: "paused" }>,
    ): LiveIssueCommentPage => ({
      comments,
      truncated: true,
      incomplete: true,
      nextPage,
      pageSize,
      ...(paused?.retryAfterMs !== undefined ? { retryAfterMs: paused.retryAfterMs } : {}),
      ...(paused?.rateLimitResetEpoch !== undefined
        ? { rateLimitResetEpoch: paused.rateLimitResetEpoch }
        : {}),
      ...(paused?.rateLimited ? { rateLimited: true } : {}),
      ...(paused?.transientFailure ? { transientFailure: true } : {}),
    });

    let lastComments: LiveIssueComment[] = [];
    let lastPageSize = initialPageSize;

    for (let attempt = 0; attempt <= maxStabilityRetries; attempt += 1) {
      let retryForInstability = false;
      let exhausted = false;

      for (let sizeIdx = 0; sizeIdx < pageSizes.length; sizeIdx += 1) {
        const size = pageSizes[sizeIdx];
        lastPageSize = size;
        const resumeStartPage =
          size === initialPageSize ? startPage : Math.floor(coveredOffset / size) + 1;
        const commentsById = new Map<number, LiveIssueComment>();
        const pageIds: number[][] = [];
        let hitTooLarge = false;
        let scanDone = false;

        for (let page = resumeStartPage; ; page += 1) {
          if (requests >= maxRequests) {
            return pausedPage(Array.from(commentsById.values()), page, size);
          }
          const fetched = await fetchPage(size, page);
          if (fetched.kind === "too_large") {
            hitTooLarge = true;
            break;
          }
          if (fetched.kind === "paused") {
            return pausedPage(Array.from(commentsById.values()), page, size, fetched);
          }
          const currentPageIds: number[] = [];
          let hitMaxCommentsMidPage = false;
          for (let idx = 0; idx < fetched.items.length; idx += 1) {
            const parsed = fetched.items[idx];
            if (!commentsById.has(parsed.id) && commentsById.size >= maxComments) {
              hitMaxCommentsMidPage = true;
              break;
            }
            currentPageIds.push(parsed.id);
            if (!commentsById.has(parsed.id)) {
              commentsById.set(parsed.id, parsed);
            }
          }

          if (hitMaxCommentsMidPage) {
            if (page === resumeStartPage && sizeIdx + 1 < pageSizes.length) {
              const smallerSize = pageSizes[sizeIdx + 1];
              const steppedPage = Math.floor(((page - 1) * size) / smallerSize) + 1;
              return pausedPage(Array.from(commentsById.values()), steppedPage, smallerSize);
            }
            return pausedPage(Array.from(commentsById.values()), page, size);
          }

          if (commentsById.size >= maxComments && fetched.hasNextPageExplicit === true) {
            return pausedPage(Array.from(commentsById.values()), page + 1, size);
          }

          pageIds.push(currentPageIds);
          if (!fetched.hasMorePage) {
            scanDone = true;
            break;
          }
        }

        if (hitTooLarge) {
          lastComments = Array.from(commentsById.values());
          continue;
        }
        if (!scanDone) {
          break;
        }
        lastComments = Array.from(commentsById.values());
        if (pageIds.length <= 1) {
          return {
            comments: lastComments,
            truncated: false,
            incomplete: false,
            nextPage: null,
            pageSize: size,
          };
        }

        let unstable = false;
        for (let verifyIdx = 0; verifyIdx < pageIds.length - 1; verifyIdx += 1) {
          if (requests >= maxRequests) {
            return pausedPage(lastComments, resumeStartPage, size);
          }
          const verifyPage = resumeStartPage + verifyIdx;
          const refetched = await fetchPage(size, verifyPage);
          if (refetched.kind === "too_large") {
            hitTooLarge = true;
            break;
          }
          if (refetched.kind === "paused") {
            return pausedPage(lastComments, resumeStartPage, size, refetched);
          }
          const expectedIds = pageIds[verifyIdx];
          if (
            refetched.items.length !== expectedIds.length ||
            refetched.items.some((item, idx) => item.id !== expectedIds[idx])
          ) {
            unstable = true;
            break;
          }
        }
        if (hitTooLarge) {
          lastComments = Array.from(commentsById.values());
          continue;
        }
        if (!unstable) {
          if (requests >= maxRequests) {
            return pausedPage(lastComments, resumeStartPage, size);
          }
          const lastPageNumber = resumeStartPage + pageIds.length - 1;
          const tailRefetched = await fetchPage(size, lastPageNumber);
          if (tailRefetched.kind === "too_large") {
            continue;
          }
          if (tailRefetched.kind === "paused") {
            return pausedPage(lastComments, resumeStartPage, size, tailRefetched);
          }
          const prevTailIds = pageIds[pageIds.length - 1];
          const prefixMatches =
            tailRefetched.items.length >= prevTailIds.length &&
            prevTailIds.every((id, idx) => tailRefetched.items[idx].id === id);
          if (!prefixMatches || tailRefetched.hasMorePage) {
            unstable = true;
          } else {
            for (let idx = prevTailIds.length; idx < tailRefetched.items.length; idx += 1) {
              const appended = tailRefetched.items[idx];
              if (!commentsById.has(appended.id) && commentsById.size >= maxComments) {
                return pausedPage(Array.from(commentsById.values()), lastPageNumber, size);
              }
              if (!commentsById.has(appended.id)) {
                commentsById.set(appended.id, appended);
              }
            }
            return {
              comments: Array.from(commentsById.values()),
              truncated: false,
              incomplete: false,
              nextPage: null,
              pageSize: size,
            };
          }
        }

        if (unstable) {
          if (attempt < maxStabilityRetries) {
            retryForInstability = true;
          } else {
            exhausted = true;
          }
          break;
        }
      }

      if (exhausted) {
        return {
          comments: lastComments,
          truncated: true,
          incomplete: false,
          nextPage: null,
          pageSize: lastPageSize,
        };
      }
      if (!retryForInstability) {
        break;
      }
    }
    throw new Error("github_response_too_large");
  }

  private async getObjectOrNull(
    path: string,
    token: string,
    forbiddenCode: string,
    failedCode: string,
  ): Promise<JsonObject | null> {
    const response = await this.request("GET", path, token);
    if (response.status === 404) {
      return null;
    }
    if (response.status === 403) {
      throw new Error(forbiddenCode);
    }
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error(failedCode);
    }
    return response.data;
  }

  private optionalPositiveId(value: unknown): number | null {
    return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : null;
  }

  /** Read a single PR review by ID (O(1) reconciliation verification; zero writes). */
  async pullRequestReview(
    repository: string,
    pullRequest: number,
    reviewId: number,
    token: string,
  ): Promise<LivePullRequestReview | null> {
    if (
      !Number.isSafeInteger(pullRequest) ||
      pullRequest <= 0 ||
      !Number.isSafeInteger(reviewId) ||
      reviewId <= 0
    ) {
      throw new Error("github_pull_request_review_invalid");
    }
    const repoBase = repositoryPath(repository);
    const data = await this.getObjectOrNull(
      `${repoBase}/pulls/${pullRequest}/reviews/${reviewId}`,
      token,
      "github_pull_request_review_forbidden",
      "github_pull_request_review_lookup_failed",
    );
    if (data === null) {
      return null;
    }
    const user = data.user;
    const commitId = data.commit_id;
    const body = data.body === null || data.body === undefined ? "" : data.body;
    const state = data.state;
    const prUrl = data.pull_request_url;
    const appObj = data.performed_via_github_app;
    if (
      data.id !== reviewId ||
      !isObject(user) ||
      typeof user.login !== "string" ||
      user.login.length === 0 ||
      user.login.length > 256 ||
      typeof user.type !== "string" ||
      typeof state !== "string" ||
      state.length === 0 ||
      typeof body !== "string" ||
      new TextEncoder().encode(body).byteLength > 65536 ||
      (commitId !== null &&
        commitId !== undefined &&
        (typeof commitId !== "string" || !PUBLIC_WORKFLOW_SHA_PATTERN.test(commitId))) ||
      (typeof prUrl === "string" && prUrl !== `${this.apiUrl}${repoBase}/pulls/${pullRequest}`)
    ) {
      throw new Error("github_pull_request_review_response_invalid");
    }
    return {
      id: reviewId,
      login: user.login,
      userType: user.type,
      commit_id: typeof commitId === "string" ? commitId : null,
      body,
      state,
      performedViaAppId: isObject(appObj) ? this.optionalPositiveId(appObj.id) : null,
    };
  }

  /** Read a single PR review comment by ID (O(1) reconciliation verification; zero writes). */
  async pullRequestReviewComment(
    repository: string,
    pullRequest: number,
    commentId: number,
    token: string,
  ): Promise<LivePullRequestReviewComment | null> {
    if (
      !Number.isSafeInteger(pullRequest) ||
      pullRequest <= 0 ||
      !Number.isSafeInteger(commentId) ||
      commentId <= 0
    ) {
      throw new Error("github_pull_request_comment_invalid");
    }
    const repoBase = repositoryPath(repository);
    const data = await this.getObjectOrNull(
      `${repoBase}/pulls/comments/${commentId}`,
      token,
      "github_pull_request_comment_forbidden",
      "github_pull_request_comment_lookup_failed",
    );
    if (data === null) {
      return null;
    }
    const user = data.user;
    const commitId = data.commit_id;
    const originalCommitId = data.original_commit_id;
    const body = data.body;
    const prUrl = data.pull_request_url;
    const appObj = data.performed_via_github_app;
    if (
      data.id !== commentId ||
      !isObject(user) ||
      typeof user.login !== "string" ||
      user.login.length === 0 ||
      user.login.length > 256 ||
      typeof user.type !== "string" ||
      typeof body !== "string" ||
      new TextEncoder().encode(body).byteLength > 65536 ||
      prUrl !== `${this.apiUrl}${repoBase}/pulls/${pullRequest}` ||
      (commitId !== null &&
        commitId !== undefined &&
        (typeof commitId !== "string" || !PUBLIC_WORKFLOW_SHA_PATTERN.test(commitId))) ||
      (originalCommitId !== null &&
        originalCommitId !== undefined &&
        (typeof originalCommitId !== "string" || !PUBLIC_WORKFLOW_SHA_PATTERN.test(originalCommitId)))
    ) {
      throw new Error("github_pull_request_comment_response_invalid");
    }
    return {
      id: commentId,
      login: user.login,
      userType: user.type,
      commit_id: typeof commitId === "string" ? commitId : null,
      original_commit_id: typeof originalCommitId === "string" ? originalCommitId : null,
      body,
      pull_request_url: prUrl,
      performedViaAppId: isObject(appObj) ? this.optionalPositiveId(appObj.id) : null,
    };
  }

  /** Read a single check run by ID (O(1) reconciliation verification; zero writes). */
  async checkRun(
    repository: string,
    checkRunId: number,
    token: string,
  ): Promise<LiveCheckRun | null> {
    if (!Number.isSafeInteger(checkRunId) || checkRunId <= 0) {
      throw new Error("github_check_run_invalid");
    }
    const data = await this.getObjectOrNull(
      `${repositoryPath(repository)}/check-runs/${checkRunId}`,
      token,
      "github_check_run_forbidden",
      "github_check_run_lookup_failed",
    );
    if (data === null) {
      return null;
    }
    const headSha = data.head_sha;
    const name = data.name;
    const status = data.status;
    const conclusion = data.conclusion ?? null;
    const externalId = data.external_id ?? null;
    const appObj = data.app;
    const output = data.output;
    if (
      data.id !== checkRunId ||
      typeof headSha !== "string" ||
      !PUBLIC_WORKFLOW_SHA_PATTERN.test(headSha) ||
      typeof name !== "string" ||
      name.length === 0 ||
      name.length > 256 ||
      typeof status !== "string" ||
      status.length === 0 ||
      (conclusion !== null && typeof conclusion !== "string") ||
      (externalId !== null && typeof externalId !== "string")
    ) {
      throw new Error("github_check_run_response_invalid");
    }
    const readOutputField = (field: unknown): string | null =>
      typeof field === "string" && new TextEncoder().encode(field).byteLength <= 65536
        ? field
        : null;
    return {
      id: checkRunId,
      appId: isObject(appObj) ? this.optionalPositiveId(appObj.id) : null,
      head_sha: headSha,
      external_id: externalId,
      name,
      status,
      conclusion,
      outputTitle: isObject(output) ? readOutputField(output.title) : null,
      outputSummary: isObject(output) ? readOutputField(output.summary) : null,
      outputText: isObject(output) ? readOutputField(output.text) : null,
    };
  }

  /** Exact open PR snapshot for complete feedback authorization. */
  async feedbackPullRequest(repository: string, pullRequest: number, token: string): Promise<{ base_sha: string; head_sha: string }> {
    if (!Number.isSafeInteger(pullRequest) || pullRequest <= 0) throw new Error("github_feedback_scope_invalid");
    const response = await this.request("GET", `${repositoryPath(repository)}/pulls/${pullRequest}`, token);
    if (response.status !== 200 || !isObject(response.data) || response.data.number !== pullRequest || response.data.state !== "open" || response.data.draft !== false || !isObject(response.data.base) || !isObject(response.data.head)) throw new Error("github_feedback_snapshot_invalid");
    const base = response.data.base.sha; const head = response.data.head.sha;
    if (typeof base !== "string" || !PUBLIC_WORKFLOW_SHA_PATTERN.test(base) || typeof head !== "string" || !PUBLIC_WORKFLOW_SHA_PATTERN.test(head)) throw new Error("github_feedback_snapshot_invalid");
    return { base_sha: base, head_sha: head };
  }

  /** Full bounded source from a host-selected canonical endpoint, never a supplied URL. */
  async feedbackComment(repository: string, pullRequest: number, kind: "issue" | "inline", commentId: number, token: string): Promise<JsonObject> {
    if (!Number.isSafeInteger(pullRequest) || pullRequest <= 0 || !Number.isSafeInteger(commentId) || commentId <= 0 || !["issue", "inline"].includes(kind)) throw new Error("github_feedback_scope_invalid");
    const base = repositoryPath(repository);
    const path = kind === "issue" ? `${base}/issues/comments/${commentId}` : `${base}/pulls/comments/${commentId}`;
    const response = await this.request("GET", path, token);
    if (response.status !== 200 || !isObject(response.data)) throw new Error("github_feedback_source_unavailable");
    const source = response.data; const user = source.user;
    const body = source.body; const updatedAt = source.updated_at;
    const expectedUrl = `${this.apiUrl}${base}/${kind === "issue" ? "issues" : "pulls"}/${pullRequest}`;
    if (source.id !== commentId || (kind === "issue" ? source.issue_url : source.pull_request_url) !== expectedUrl || !isObject(user) || user.type !== "User" || typeof user.id !== "number" || !Number.isSafeInteger(user.id) || user.id <= 0 || typeof user.login !== "string" || typeof body !== "string" || body.length === 0 || typeof updatedAt !== "string" || updatedAt.length === 0 || typeof source.author_association !== "string" || !["OWNER", "MEMBER", "COLLABORATOR"].includes(source.author_association)) throw new Error("github_feedback_source_invalid");
    const bytes = new TextEncoder().encode(body);
    const loginBytes = new TextEncoder().encode(user.login);
    const updatedBytes = new TextEncoder().encode(updatedAt);
    const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
    if (bytes.length > 65536 || decoder.decode(bytes) !== body || loginBytes.length > 256 || user.login.length === 0 || decoder.decode(loginBytes) !== user.login || updatedBytes.length > 128 || decoder.decode(updatedBytes) !== updatedAt) throw new Error("github_feedback_source_invalid");
    const root = kind === "issue" ? null : source.in_reply_to_id ?? source.id;
    if (kind === "inline" && (typeof root !== "number" || !Number.isSafeInteger(root) || root <= 0)) throw new Error("github_feedback_source_invalid");
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const bodyHash = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
    return { kind, comment_id: commentId, updated_at: updatedAt, author: user.login, author_id: user.id, association: source.author_association, root_comment_id: root, body_bytes: bytes.length, body_sha256: bodyHash, body };
  }

  /**
   * Resolve the operator-managed public workflow tag to its immutable commit.
   *
   * Prefer GitHub's public Git ref advertisement so this hot path is not
   * constrained by the 60-request/hour anonymous REST bucket. The REST refs
   * endpoint remains a fallback for GitHub-compatible environments where the
   * Git advertisement cannot be read or parsed.
   */
  async publicWorkflowSha(tag: string): Promise<string> {
    return (await this.publicWorkflowRuntimeShas(tag)).commitSha;
  }

  async publicWorkflowRuntimeShas(tag: string): Promise<PublicWorkflowRuntimeShas> {
    const validatedTag = publicWorkflowTag(tag);
    let gitFailure: LookupDiagnostic["git_failure"];
    if (this.apiUrl === "https://api.github.com") {
      try {
        return await this.publicWorkflowRuntimeShasFromGit(validatedTag);
      } catch (error) {
        if (error instanceof Error && /^(broker_control_|broker_request_)/.test(error.message)) throw error;
        gitFailure = lookupFailure(error);
        // Fall back to the bounded REST resolver below when the public Git
        // endpoint is unavailable or returns an unsupported advertisement.
      }
    }
    return await this.publicWorkflowRuntimeShasFromApi(validatedTag, gitFailure);
  }

  private publicWorkflowCacheKey(): string {
    const appId = this.env.GITHUB_APP_ID ?? "";
    const rawKey = this.env.GITHUB_APP_PRIVATE_KEY ?? "";
    const memo = this.cachedPublicWorkflowKeyInput;
    if (
      memo !== undefined &&
      memo.apiUrl === this.apiUrl &&
      memo.appId === appId &&
      memo.rawKey === rawKey
    ) {
      return memo.key;
    }
    const key = `${this.apiUrl}|${appId}|${syncSha256Hex(rawKey)}`;
    this.cachedPublicWorkflowKeyInput = { apiUrl: this.apiUrl, appId, rawKey, key };
    return key;
  }

  private cachedPublicWorkflowReadToken(): { token: string | undefined; fromEnv: boolean } {
    const envToken = this.env.PUBLIC_WORKFLOW_READ_TOKEN?.trim();
    if (envToken) {
      return { token: envToken, fromEnv: true };
    }
    const now = Date.now();
    prunePublicWorkflowTokenCache(now);
    const cached = publicWorkflowTokenCache.get(this.publicWorkflowCacheKey());
    if (
      cached?.token &&
      cached.token.expiresAt - now > PUBLIC_WORKFLOW_TOKEN_SKEW_MS
    ) {
      return { token: cached.token.token, fromEnv: false };
    }
    return { token: undefined, fromEnv: false };
  }

  private invalidatePublicWorkflowReadToken(): void {
    const key = this.publicWorkflowCacheKey();
    const existing = publicWorkflowTokenCache.get(key);
    if (existing) {
      publicWorkflowTokenCache.delete(key);
      publicWorkflowTokenCache.set(key, {
        installationId: existing.installationId,
        cachedAt: Date.now(),
      });
    }
  }

  private async mintPublicWorkflowReadToken(forceRefresh = false): Promise<string | undefined> {
    const key = this.publicWorkflowCacheKey();
    const now = Date.now();
    prunePublicWorkflowTokenCache(now);
    const cached = publicWorkflowTokenCache.get(key);
    try {
      const installationId =
        !forceRefresh && cached?.installationId
          ? cached.installationId
          : await this.installationFor(PUBLIC_WORKFLOW_REPOSITORY);
      if (installationId === null) {
        publicWorkflowTokenCache.delete(key);
        return undefined;
      }
      const issued = await this.installationToken(
        installationId,
        PUBLIC_WORKFLOW_REPOSITORY,
        { contents: "read" },
      );
      publicWorkflowTokenCache.delete(key);
      publicWorkflowTokenCache.set(key, {
        installationId,
        token: issued,
        cachedAt: Date.now(),
      });
      prunePublicWorkflowTokenCache();
      return issued.token;
    } catch (error) {
      if (error instanceof Error && /^(broker_control_|broker_request_)/.test(error.message)) {
        throw error;
      }
      publicWorkflowTokenCache.delete(key);
      return undefined;
    }
  }

  private async publicWorkflowRuntimeShasFromApi(tag: string, gitFailure?: LookupDiagnostic["git_failure"]): Promise<PublicWorkflowRuntimeShas> {
    let { token, fromEnv } = this.cachedPublicWorkflowReadToken();
    const read = async (path: string, phase: "rest_ref" | "rest_tag") => {
      let response: GitHubApiResponse;
      try {
        response = await this.request("GET", path, token);
        if (response.status === 401 && token !== undefined && !fromEnv) {
          this.invalidatePublicWorkflowReadToken();
          const refreshed = await this.mintPublicWorkflowReadToken(true);
          if (refreshed !== undefined) {
            token = refreshed;
            response = await this.request("GET", path, token);
          }
        } else if (
          token === undefined &&
          (response.status === 403 || response.status === 429) &&
          response.lookupMetadata?.rate_limit_remaining === 0
        ) {
          const minted = await this.mintPublicWorkflowReadToken(true);
          if (minted !== undefined) {
            token = minted;
            fromEnv = false;
            response = await this.request("GET", path, token);
          }
        }
      }
      catch (error) {
        if (error instanceof Error && /^(broker_control_|broker_request_)/.test(error.message)) throw error;
        throw new WorkflowLookupError({phase, failure: lookupFailure(error), ...(gitFailure ? {git_failure: gitFailure} : {}), ...(error instanceof GitHubResponseError ? error.metadata : {})}, error instanceof GitHubResponseError ? error.status : undefined);
      }
      if (response.status < 200 || response.status >= 300) throw new WorkflowLookupError({phase, failure: "http", ...(gitFailure ? {git_failure: gitFailure} : {}), ...response.lookupMetadata}, response.status);
      return response;
    };
    let response = await read(`${repositoryPath(PUBLIC_WORKFLOW_REPOSITORY)}/git/ref/tags/${encodeURIComponent(tag)}`, "rest_ref");
    for (let depth = 0; depth <= MAX_TAG_DEREFERENCE_DEPTH; depth += 1) {
      if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
        throw new Error(`github_workflow_tag_unavailable_${response.status}`);
      }
      const object = response.data.object;
      if (!isObject(object) || typeof object.sha !== "string" || !PUBLIC_WORKFLOW_SHA_PATTERN.test(object.sha)) {
        throw new Error("github_workflow_tag_invalid");
      }
      const refSha = object.sha;
      if (object.type === "commit") {
        return { commitSha: refSha, refSha };
      }
      if (object.type !== "tag" || depth === MAX_TAG_DEREFERENCE_DEPTH) {
        throw new Error("github_workflow_tag_invalid");
      }
      response = await read(`${repositoryPath(PUBLIC_WORKFLOW_REPOSITORY)}/git/tags/${encodeURIComponent(object.sha)}`, "rest_tag");
      if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
        throw new Error(`github_workflow_tag_unavailable_${response.status}`);
      }
      const target = response.data.object;
      if (
        !isObject(target) ||
        target.type !== "commit" ||
        typeof target.sha !== "string" ||
        !PUBLIC_WORKFLOW_SHA_PATTERN.test(target.sha)
      ) {
        throw new Error("github_workflow_tag_invalid");
      }
      return { commitSha: target.sha, refSha };
    }
    throw new Error("github_workflow_tag_invalid");
  }

  /** Resolve a public tag without consuming GitHub's anonymous REST quota. */
  private async publicWorkflowRuntimeShasFromGit(tag: string): Promise<PublicWorkflowRuntimeShas> {
    return await (this.lifetime ?? new BrokerLifetime()).upstream(async scope => {
      let response: Response;
      try {
        response = await scope.fetch(
          `${PUBLIC_WORKFLOW_GIT_URL}/info/refs?service=git-upload-pack`,
          {
            redirect: "manual",
            headers: {
              accept: "application/x-git-upload-pack-advertisement",
              "user-agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
            },
          },
        );
      } catch (error) {
        if (error instanceof Error && /^(broker_control_|broker_request_|broker_upstream_)/.test(error.message)) throw error;
        throw new Error("github_workflow_tag_unavailable");
      }
      await this.dispatchControl?.checkResponse(response);
      if (!response.ok) {
        throw new Error(`github_workflow_tag_unavailable_${response.status}`);
      }
      try {
        const result = advertisedTagShas(await boundedBytes(response, MAX_PUBLIC_REF_BYTES, scope), tag);
        this.dispatchControl?.check();
        return result;
      } catch (error) {
        if (error instanceof Error && /^(github_workflow_tag_|github_response_too_large$|broker_control_|broker_request_|broker_upstream_)/.test(error.message)) {
          throw error;
        }
        throw new Error("github_workflow_tag_invalid");
      }
    }, this.dispatchControl, "workflow_git");
  }

  /**
   * List every repository selected for an installation using a bounded,
   * installation-scoped token. The webhook payload for permission acceptance
   * may omit its repository list, so this is the authoritative reconciliation
   * fallback for installation lifecycle events only.
   */
  async installationRepositories(installationId: number): Promise<string[]> {
    if (!Number.isSafeInteger(installationId) || installationId <= 0) {
      throw new Error("github_installation_invalid");
    }
    const tokenResponse = await this.request(
      "POST",
      `/app/installations/${installationId}/access_tokens`,
      await this.appJwt(),
    );
    if (
      tokenResponse.status < 200 ||
      tokenResponse.status >= 300 ||
      !isObject(tokenResponse.data)
    ) {
      throw new Error(`github_installation_token_failed_${tokenResponse.status}`);
    }
    const token = tokenResponse.data.token;
    const expiresAt = tokenResponse.data.expires_at;
    if (
      typeof token !== "string" ||
      !token.trim() ||
      typeof expiresAt !== "string" ||
      !Number.isFinite(Date.parse(expiresAt)) ||
      Date.parse(expiresAt) <= Date.now()
    ) {
      throw new Error("github_installation_token_invalid");
    }

    const raw = await readBoundedHistory(async (size, page) => {
      const response = await this.request(
        "GET",
        `/installation/repositories?per_page=${size}&page=${page}`,
        token,
      );
      if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
        throw new Error("github_installation_repositories_failed");
      }
      if (!Array.isArray(response.data.repositories)) {
        throw new Error("github_installation_repositories_invalid");
      }
      return { status: response.status, data: response.data.repositories };
    }, "github_installation_repositories_limit");
    const repositories: string[] = [];
    const seen = new Set<string>();
    for (const item of raw) {
      const fullName = isObject(item) ? item.full_name : null;
      if (typeof fullName !== "string" || !REPOSITORY_PATTERN.test(fullName)) {
        throw new Error("github_installation_repository_invalid");
      }
      if (!seen.has(fullName)) {
        seen.add(fullName);
        repositories.push(fullName);
      }
    }
    return repositories;
  }

  /** Issue one short-lived GitHub App installation token for the exact scope. */
  async capabilityToken(installationId: number, repository: string, permissions: Record<string, "read" | "write">, acceptReturnedContentsRead = false): Promise<string> {
    return (await this.capabilityTokenObservation(installationId, repository, permissions, acceptReturnedContentsRead)).token;
  }

  async capabilityTokenObservation(
    installationId: number,
    repository: string,
    permissions: Record<string, "read" | "write">,
    acceptReturnedContentsRead = false,
  ): Promise<{
    token: string;
    permissions: Record<string, string>;
    expiresAt: string;
    expiresAtMs: number;
  }> {
    if (!Number.isSafeInteger(installationId) || installationId <= 0) {
      throw new Error("github_installation_invalid");
    }
    // Validate the full owner/repository slug before deriving the repository
    // name used in the installation-token request.
    repositoryPath(repository);
    const [, name] = repository.split("/");
    const response = await this.request(
      "POST",
      `/app/installations/${installationId}/access_tokens`,
      await this.appJwt(),
      { repositories: [name], permissions },
    );
    if (response.status < 200 || response.status >= 300) {
      throw new Error(`github_capability_issue_failed_${response.status}`);
    }
    if (!isObject(response.data)) {
      throw new Error("github_capability_response_invalid");
    }
    const token = response.data.token;
    const expiresAt = response.data.expires_at;
    const granted = response.data.permissions;
    const expiresAtMs = typeof expiresAt === "string" ? Date.parse(expiresAt) : Number.NaN;
    if (
      typeof token !== "string" ||
      token.length === 0 ||
      typeof expiresAt !== "string" ||
      !Number.isFinite(expiresAtMs) ||
      expiresAtMs <= Date.now() ||
      !isObject(granted)
    ) {
      throw new Error("github_capability_response_invalid");
    }
    if (!hasExactPermissions(granted, permissions, acceptReturnedContentsRead)) {
      throw new Error("github_capability_permissions_invalid");
    }
    return {
      token,
      permissions: observationPermissions(normalizePermissionMap(granted)),
      expiresAt,
      expiresAtMs,
    };
  }

  /**
   * Revoke an installation token via `DELETE /installation/token`.
   * Classifies 204, 401, 429/rate-limit-403, permission 403, other 4xx, and 5xx/timeout.
   */
  async revokeInstallationToken(token: string): Promise<RevocationResult> {
    if (typeof token !== "string" || !token.trim()) {
      throw new Error("github_installation_token_invalid");
    }
    const classify = (
      status: number,
      metadata: ReturnType<typeof lookupHeaders> | undefined,
      data: unknown,
    ): RevocationResult => {
      const signal = classifyGitHubRetrySignal(status, metadata, data, Date.now());
      const { retryAfterSeconds, rateLimitResetEpoch, isRateLimit } = signal;
      if (status === 204) {
        return { status, kind: "http_204", retryAfterSeconds, rateLimitResetEpoch, isRateLimit: false };
      }
      if (status === 401) {
        return { status, kind: "http_401", retryAfterSeconds, rateLimitResetEpoch, isRateLimit: false };
      }
      if (isRateLimit) {
        return {
          status,
          kind: "http_429_or_ratelimit_403",
          retryAfterSeconds,
          rateLimitResetEpoch,
          isRateLimit: true,
        };
      }
      if (status === 403) {
        return { status, kind: "permission_403", retryAfterSeconds, rateLimitResetEpoch, isRateLimit: false };
      }
      if (status >= 400 && status <= 499) {
        return { status, kind: "http_other_4xx", retryAfterSeconds, rateLimitResetEpoch, isRateLimit: false };
      }
      return { status, kind: "http_5xx_or_timeout", retryAfterSeconds, rateLimitResetEpoch, isRateLimit: false };
    };

    try {
      const response = await this.request("DELETE", "/installation/token", token);
      return classify(response.status, response.lookupMetadata, response.data);
    } catch (error) {
      if (
        error instanceof Error &&
        /^(broker_control_|broker_request_)/.test(error.message) &&
        error.message !== "broker_upstream_timeout"
      ) {
        throw error;
      }
      if (error instanceof GitHubResponseError) {
        if (error.message === "broker_upstream_timeout") {
          return {
            status: null,
            kind: "http_5xx_or_timeout",
            retryAfterSeconds: error.metadata.retry_after ?? null,
            rateLimitResetEpoch: error.metadata.rate_limit_reset ?? null,
            isRateLimit: false,
          };
        }
        return classify(error.status, error.metadata, null);
      }
      return {
        status: null,
        kind: "http_5xx_or_timeout",
        retryAfterSeconds: null,
        rateLimitResetEpoch: null,
        isRateLimit: false,
      };
    }
  }

  async installationToken(
    installationId: number,
    repository: string,
    permissions: Record<string, string>,
  ): Promise<InstallationToken> {
    if (!Number.isSafeInteger(installationId) || installationId <= 0) {
      throw new Error("github_installation_invalid");
    }
    repositoryPath(repository);
    const [, name] = repository.split("/");
    const response = await this.request(
      "POST",
      `/app/installations/${installationId}/access_tokens`,
      await this.appJwt(),
      { repositories: [name], permissions },
    );
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error(`github_installation_token_failed_${response.status}`);
    }
    const token = response.data.token;
    const expiresAt = response.data.expires_at;
    const rawPermissions = response.data.permissions;
    if (
      typeof token !== "string" ||
      !token.trim() ||
      typeof expiresAt !== "string" ||
      !Number.isFinite(Date.parse(expiresAt)) ||
      Date.parse(expiresAt) <= Date.now()
    ) {
      throw new Error("github_installation_token_invalid");
    }
    if (!hasExactPermissions(rawPermissions, permissions)) {
      throw new Error("github_installation_permissions_invalid");
    }
    const normalized: Record<string, string> = {};
    if (isObject(rawPermissions)) {
      for (const [key, value] of Object.entries(rawPermissions)) {
        if (typeof value === "string") {
          const normalizedKey = key.trim().toLowerCase().replaceAll("-", "_");
          normalized[normalizedKey] = value.trim().toLowerCase();
        }
      }
    }
    return {
      token,
      expiresAt: Date.parse(expiresAt),
      permissions: normalized,
    };
  }

  /** App user login. Submitted reviews and operation records are matched to this bot. */
  async appBotLogin(): Promise<string> {
    const response = await this.request("GET", "/app", await this.appJwt());
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_app_lookup_failed");
    }
    const slug = response.data.slug;
    if (typeof slug !== "string" || !/^[A-Za-z0-9-]{1,100}$/.test(slug)) {
      throw new Error("github_app_lookup_failed");
    }
    return `${slug}[bot]`;
  }

  /**
   * Installation permissions plus the current repository list.
   * Callers compare a digest of this view and restart when it changes.
   */
  async installationSetupView(installationId: number): Promise<{
    permissions: Record<string, string>;
    repositories: string[];
  }> {
    if (!Number.isSafeInteger(installationId) || installationId <= 0) {
      throw new Error("github_installation_invalid");
    }
    const response = await this.request(
      "GET",
      `/app/installations/${installationId}`,
      await this.appJwt(),
    );
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error(`github_installation_token_failed_${response.status}`);
    }
    const permissions: Record<string, string> = {};
    if (isObject(response.data.permissions)) {
      for (const [key, value] of Object.entries(response.data.permissions)) {
        if (typeof value === "string") {
          const normalizedKey = key.trim().toLowerCase().replaceAll("-", "_");
          permissions[normalizedKey] = value.trim().toLowerCase();
        }
      }
    }
    const repositories = await this.installationRepositories(installationId);
    return { permissions, repositories };
  }

  /**
   * Known when an App-authored submitted review matches the head, or an
   * App-authored operation record already exists. Otherwise the caller enrolls.
   */
  async sessionAlreadyKnown(
    repository: string,
    pullRequest: number,
    headSha: string,
    token: string,
  ): Promise<boolean> {
    if (!Number.isSafeInteger(pullRequest) || pullRequest <= 0 || !/^[a-f0-9]{40}$/.test(headSha)) {
      throw new Error("broker_session_invalid");
    }
    const login = (await this.appBotLogin()).toLowerCase();
    const root = repositoryPath(repository);
    if (await this.appSubmittedReview(root, pullRequest, headSha, login, token)) {
      return true;
    }
    return this.appOperationRecord(root, pullRequest, login, token);
  }

  private async pagedItems(path: string, token: string, invalidCode: string): Promise<unknown[]> {
    return readBoundedHistory(
      (size, page) => this.request("GET", `${path}?per_page=${size}&page=${page}`, token),
      invalidCode,
    );
  }

  private appAuthored(item: unknown, login: string): item is JsonObject {
    if (!isObject(item) || !isObject(item.user)) {
      return false;
    }
    return item.user.type === "Bot" &&
      typeof item.user.login === "string" &&
      item.user.login.toLowerCase() === login;
  }

  private async appSubmittedReview(
    root: string,
    pullRequest: number,
    headSha: string,
    login: string,
    token: string,
  ): Promise<boolean> {
    const reviews = await this.pagedItems(
      `${root}/pulls/${pullRequest}/reviews`,
      token,
      "github_session_witness_failed",
    );
    return reviews.some((review) => {
      if (!this.appAuthored(review, login)) {
        return false;
      }
      const state = review.state;
      return review.commit_id === headSha &&
        (state === "APPROVED" ||
          state === "CHANGES_REQUESTED" ||
          state === "COMMENTED" ||
          state === "DISMISSED");
    });
  }

  private async appOperationRecord(
    root: string,
    pullRequest: number,
    login: string,
    token: string,
  ): Promise<boolean> {
    const comments = await this.pagedItems(
      `${root}/issues/${pullRequest}/comments`,
      token,
      "github_session_witness_failed",
    );
    return comments.some((comment) =>
      this.appAuthored(comment, login) &&
      typeof comment.body === "string" &&
      comment.body.includes("<!-- review-sensei-operation -->"),
    );
  }
}
