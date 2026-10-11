const SESSION_GRANT_TTL_MS = 10 * 60 * 1000;
const SETUP_CURSOR_TTL_MS = 10 * 60 * 1000;
const HKDF_SALT = new Uint8Array(32);
const SESSION_GRANT_LABEL = "session-grant-v1";
const SETUP_CURSOR_LABEL = "setup-cursor-v1";

export interface SessionGrantFields {
  scope: string;
  attestationDigest: string;
  audience: string;
  runId: string;
  now?: number;
}

export interface SetupCursorClaims {
  installationId: number;
  deliveryId: string;
  page: number;
  offset: number;
  expiry: number;
  listingSha256: string;
  failureCount: number;
}

function text(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function fromBase64Url(value: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) {
    return null;
  }
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "===".slice((value.length + 3) % 4);
  try {
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  } catch {
    return null;
  }
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", text(value).buffer as ArrayBuffer);
  return [...new Uint8Array(digest)].map((part) => part.toString(16).padStart(2, "0")).join("");
}

async function hmacKey(secret: string, label: string): Promise<CryptoKey> {
  if (!secret.trim()) {
    throw new Error("configuration_unavailable");
  }
  const ikm = await crypto.subtle.importKey("raw", text(secret).buffer as ArrayBuffer, "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: HKDF_SALT, info: text(label).buffer as ArrayBuffer },
    ikm,
    { name: "HMAC", hash: "SHA-256", length: 256 },
    false,
    ["sign", "verify"],
  );
}

async function macMatches(secret: string, label: string, message: string, mac: Uint8Array): Promise<boolean> {
  const key = await hmacKey(secret, label);
  return crypto.subtle.verify("HMAC", key, mac.buffer as ArrayBuffer, text(message).buffer as ArrayBuffer);
}

function grantMessage(fields: {
  scope: string;
  attestationDigest: string;
  audience: string;
  runIdHash: string;
  expiry: number;
}): string {
  return [
    SESSION_GRANT_LABEL,
    fields.scope,
    fields.attestationDigest,
    fields.audience,
    fields.runIdHash,
    String(fields.expiry),
  ].join("\n");
}

/** HMAC-SHA256 session grant. The signature covers scope, attestation, audience, run id hash, and expiry. */
export async function issueSessionGrant(secret: string, fields: SessionGrantFields): Promise<string> {
  const expiry = (fields.now ?? Date.now()) + SESSION_GRANT_TTL_MS;
  const runIdHash = await sha256Hex(fields.runId);
  const message = grantMessage({ ...fields, runIdHash, expiry });
  const key = await hmacKey(secret, SESSION_GRANT_LABEL);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, text(message).buffer as ArrayBuffer));
  return `sg1.${expiry}.${base64Url(mac)}`;
}

export async function sessionGrantAuthentic(
  secret: string,
  grant: string,
  fields: {
    scope: string;
    attestationDigest: string;
    audience: string;
    runId: string;
    now?: number;
  },
): Promise<boolean> {
  const match = /^sg1\.([1-9][0-9]{10,15})\.([A-Za-z0-9_-]{43})$/.exec(grant);
  if (match === null) {
    return false;
  }
  const expiry = Number(match[1]);
  const mac = fromBase64Url(match[2]);
  const now = fields.now ?? Date.now();
  if (!Number.isSafeInteger(expiry) || mac === null || mac.length !== 32 || now >= expiry) {
    return false;
  }
  const runIdHash = await sha256Hex(fields.runId);
  return macMatches(
    secret,
    SESSION_GRANT_LABEL,
    grantMessage({
      scope: fields.scope,
      attestationDigest: fields.attestationDigest,
      audience: fields.audience,
      runIdHash,
      expiry,
    }),
    mac,
  );
}

function cursorMessage(claims: SetupCursorClaims): string {
  return [
    SETUP_CURSOR_LABEL,
    String(claims.installationId),
    claims.deliveryId,
    String(claims.page),
    String(claims.offset),
    String(claims.expiry),
    claims.listingSha256,
    String(claims.failureCount),
  ].join("\n");
}

function cursorPayload(claims: SetupCursorClaims): string {
  return JSON.stringify({
    installation_id: claims.installationId,
    delivery_id: claims.deliveryId,
    page: claims.page,
    offset: claims.offset,
    expiry: claims.expiry,
    listing_sha256: claims.listingSha256,
    failure_count: claims.failureCount,
  });
}

function parseCursorPayload(value: unknown): SetupCursorClaims | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== 7) {
    return null;
  }
  const installationId = record.installation_id;
  const deliveryId = record.delivery_id;
  const page = record.page;
  const offset = record.offset;
  const expiry = record.expiry;
  const listingSha256 = record.listing_sha256;
  const failureCount = record.failure_count;
  if (
    typeof installationId !== "number" ||
    !Number.isSafeInteger(installationId) ||
    installationId <= 0 ||
    typeof deliveryId !== "string" ||
    !/^[\x21-\x7e]{1,200}$/.test(deliveryId) ||
    typeof page !== "number" ||
    !Number.isSafeInteger(page) ||
    page < 1 ||
    page > 100 ||
    typeof offset !== "number" ||
    !Number.isSafeInteger(offset) ||
    offset < 0 ||
    offset > 99 ||
    typeof expiry !== "number" ||
    !Number.isSafeInteger(expiry) ||
    expiry <= 0 ||
    typeof listingSha256 !== "string" ||
    !/^(?:|[a-f0-9]{64})$/.test(listingSha256) ||
    typeof failureCount !== "number" ||
    !Number.isSafeInteger(failureCount) ||
    failureCount < 0 ||
    failureCount > 1000
  ) {
    return null;
  }
  return {
    installationId,
    deliveryId,
    page,
    offset,
    expiry,
    listingSha256,
    failureCount,
  };
}

export function freshSetupCursor(input: {
  installationId: number;
  deliveryId: string;
  now?: number;
}): SetupCursorClaims {
  return {
    installationId: input.installationId,
    deliveryId: input.deliveryId,
    page: 1,
    offset: 0,
    expiry: (input.now ?? Date.now()) + SETUP_CURSOR_TTL_MS,
    listingSha256: "",
    failureCount: 0,
  };
}

export async function signSetupCursor(secret: string, claims: SetupCursorClaims): Promise<string> {
  const key = await hmacKey(secret, SETUP_CURSOR_LABEL);
  const mac = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, text(cursorMessage(claims)).buffer as ArrayBuffer),
  );
  return `sc1.${base64Url(text(cursorPayload(claims)))}.${base64Url(mac)}`;
}

export async function verifySetupCursor(
  secret: string,
  token: string,
  now = Date.now(),
): Promise<SetupCursorClaims | null> {
  const match = /^sc1\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$/.exec(token);
  if (match === null) {
    return null;
  }
  const payloadBytes = fromBase64Url(match[1]);
  const mac = fromBase64Url(match[2]);
  if (payloadBytes === null || mac === null || mac.length !== 32) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(payloadBytes));
  } catch {
    return null;
  }
  const claims = parseCursorPayload(parsed);
  if (claims === null || now >= claims.expiry) {
    return null;
  }
  const matches = await macMatches(secret, SETUP_CURSOR_LABEL, cursorMessage(claims), mac);
  return matches ? claims : null;
}
