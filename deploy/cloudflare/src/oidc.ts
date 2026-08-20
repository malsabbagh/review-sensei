const MAX_TOKEN_BYTES = 64 * 1024;
const MAX_JWKS_BYTES = 256 * 1024;
const JWT_PART = /^[A-Za-z0-9_-]+$/;
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const SHA_PATTERN = /^[a-f0-9]{40}$/;
const DEFAULT_ISSUER = "https://token.actions.githubusercontent.com";
const DEFAULT_AUDIENCE = "sts.reviewsensei.dev";
const JWKS_CACHE_TTL_MS = 5 * 60 * 1000;

let jwksCache: {
  issuer: string;
  expiresAt: number;
  value: Record<string, unknown>;
} | null = null;
let jwksInFlight: {
  issuer: string;
  promise: Promise<Record<string, unknown>>;
} | null = null;

export interface OidcClaims {
  iss: string;
  aud: string | string[];
  exp: number;
  iat: number;
  nbf?: number;
  sub: string;
  jti: string;
  repository: string;
  repository_owner: string;
  repository_id: number;
  actor: string;
  actor_id: number;
  event_name: string;
  workflow: string;
  workflow_ref: string;
  workflow_sha: string;
  job_workflow_ref: string;
  job_workflow_sha: string;
  ref: string;
  sha: string;
  run_id: string;
  run_number: string;
  run_attempt: string;
  runner_environment: string;
}

export interface OidcValidationOptions {
  issuer?: string;
  audience?: string;
  now?: number;
  clockSkewSeconds?: number;
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("oidc_claims_invalid");
  }
  return value as Record<string, unknown>;
}

function decodePart(value: string): Uint8Array {
  if (!JWT_PART.test(value)) {
    throw new Error("oidc_token_invalid");
  }
  const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
  const text = atob(padded.replaceAll("-", "+").replaceAll("_", "/"));
  return Uint8Array.from(text, (character) => character.charCodeAt(0));
}

function decodeJson(value: string): Record<string, unknown> {
  try {
    return object(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(decodePart(value))));
  } catch {
    throw new Error("oidc_token_invalid");
  }
}

function numberClaim(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error("oidc_claims_invalid");
  }
  return value;
}

function stringClaim(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 2048) {
    throw new Error(`oidc_${name}_invalid`);
  }
  return value;
}

function positiveId(value: unknown): number {
  if (typeof value !== "string" || !/^[1-9][0-9]*$/.test(value)) {
    throw new Error("oidc_claims_invalid");
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error("oidc_claims_invalid");
  }
  return parsed;
}

async function fetchJwks(issuer: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${issuer}/.well-known/jwks.json`, {
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error("oidc_jwks_unavailable");
  }
  const length = response.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_JWKS_BYTES)) {
    throw new Error("oidc_jwks_too_large");
  }
  if (response.body === null) {
    throw new Error("oidc_jwks_invalid");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const next = await reader.read();
    if (next.done) {
      break;
    }
    total += next.value.byteLength;
    if (total > MAX_JWKS_BYTES) {
      await reader.cancel();
      throw new Error("oidc_jwks_too_large");
    }
    chunks.push(next.value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return object(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
  } catch {
    throw new Error("oidc_jwks_invalid");
  }
}

async function cachedJwks(issuer: string): Promise<Record<string, unknown>> {
  const now = Date.now();
  if (jwksCache?.issuer === issuer && jwksCache.expiresAt > now) {
    return jwksCache.value;
  }
  if (jwksInFlight?.issuer === issuer) {
    return await jwksInFlight.promise;
  }
  const promise = fetchJwks(issuer);
  jwksInFlight = { issuer, promise };
  try {
    const value = await promise;
    jwksCache = { issuer, value, expiresAt: now + JWKS_CACHE_TTL_MS };
    return value;
  } finally {
    if (jwksInFlight?.promise === promise) {
      jwksInFlight = null;
    }
  }
}

/** Test isolation seam; production callers never need to clear public JWKS. */
export function resetOidcJwksCacheForTesting(): void {
  jwksCache = null;
  jwksInFlight = null;
}

function equalAudience(value: unknown, expected: string): value is string | string[] {
  return value === expected || (Array.isArray(value) && value.length === 1 && value[0] === expected);
}

function claimsFromPayload(
  payload: Record<string, unknown>,
  expectedAudience: string,
): OidcClaims {
  const repository = stringClaim(payload.repository, "repository");
  if (!REPOSITORY_PATTERN.test(repository)) {
    throw new Error("oidc_repository_invalid");
  }
  const workflowSha = stringClaim(payload.workflow_sha, "workflow_sha");
  const jobWorkflowSha = stringClaim(payload.job_workflow_sha, "job_workflow_sha");
  if (!SHA_PATTERN.test(workflowSha) || !SHA_PATTERN.test(jobWorkflowSha)) {
    throw new Error("oidc_workflow_sha_invalid");
  }
  const claims = {
    iss: stringClaim(payload.iss, "issuer"),
    aud: payload.aud as string | string[],
    exp: numberClaim(payload.exp),
    iat: numberClaim(payload.iat),
    ...(payload.nbf === undefined ? {} : { nbf: numberClaim(payload.nbf) }),
    sub: stringClaim(payload.sub, "subject"),
    jti: stringClaim(payload.jti, "jti"),
    repository,
    repository_owner: stringClaim(payload.repository_owner, "repository_owner"),
    repository_id: positiveId(payload.repository_id),
    actor: stringClaim(payload.actor, "actor"),
    actor_id: positiveId(payload.actor_id),
    event_name: stringClaim(payload.event_name, "event"),
    workflow: stringClaim(payload.workflow, "workflow"),
    workflow_ref: stringClaim(payload.workflow_ref, "workflow_ref"),
    workflow_sha: workflowSha,
    job_workflow_ref: stringClaim(payload.job_workflow_ref, "job_workflow_ref"),
    job_workflow_sha: jobWorkflowSha,
    ref: stringClaim(payload.ref, "ref"),
    sha: stringClaim(payload.sha, "sha"),
    run_id: stringClaim(payload.run_id, "run_id"),
    run_number: stringClaim(payload.run_number, "run_number"),
    run_attempt: stringClaim(payload.run_attempt, "run_attempt"),
    runner_environment: stringClaim(payload.runner_environment, "runner_environment"),
  } satisfies OidcClaims;
  if (!equalAudience(claims.aud, expectedAudience)) {
    throw new Error("oidc_audience_invalid");
  }
  return claims;
}

/** Validate a GitHub Actions RS256 assertion using the issuer JWKS. */
export async function verifyOidcAssertion(
  token: string,
  options: OidcValidationOptions = {},
): Promise<OidcClaims> {
  const issuer = options.issuer ?? DEFAULT_ISSUER;
  const audience = options.audience ?? DEFAULT_AUDIENCE;
  const now = options.now ?? Date.now() / 1000;
  const skew = options.clockSkewSeconds ?? 30;
  if (typeof token !== "string" || token.length === 0 || token.length > MAX_TOKEN_BYTES) {
    throw new Error("oidc_token_invalid");
  }
  const parts = token.split(".");
  if (parts.length !== 3) {
    throw new Error("oidc_token_invalid");
  }
  const header = decodeJson(parts[0]);
  const payload = decodeJson(parts[1]);
  if (header.alg !== "RS256" || typeof header.kid !== "string") {
    throw new Error("oidc_header_invalid");
  }
  const claims = claimsFromPayload(payload, audience);
  if (claims.iss !== issuer || !equalAudience(claims.aud, audience)) {
    throw new Error("oidc_identity_invalid");
  }
  if (now > claims.exp + skew || claims.iat - skew > now) {
    throw new Error("oidc_time_invalid");
  }
  if (claims.nbf !== undefined && claims.nbf - skew > now) {
    throw new Error("oidc_time_invalid");
  }

  const jwks = await cachedJwks(issuer);
  const keys = jwks.keys;
  if (!Array.isArray(keys)) {
    throw new Error("oidc_jwks_invalid");
  }
  const jwk = keys.find(
    (value) =>
      typeof value === "object" &&
      value !== null &&
      !Array.isArray(value) &&
      (value as Record<string, unknown>).kid === header.kid &&
      (value as Record<string, unknown>).alg === "RS256",
  );
  if (!jwk || typeof jwk !== "object" || Array.isArray(jwk)) {
    throw new Error("oidc_key_not_found");
  }
  try {
    const key = await crypto.subtle.importKey(
      "jwk",
      jwk as JsonWebKey,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      decodePart(parts[2]).buffer as ArrayBuffer,
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
    );
    if (!valid) {
      throw new Error("oidc_signature_invalid");
    }
  } catch (error) {
    if (error instanceof Error && error.message === "oidc_signature_invalid") {
      throw error;
    }
    throw new Error("oidc_signature_invalid");
  }
  return claims;
}
