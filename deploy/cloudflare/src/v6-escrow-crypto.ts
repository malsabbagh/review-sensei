export const V6_ESCROW_HKDF_SALT = "v6-pr-coordinator-escrow-v1";
export const V6_ESCROW_HKDF_INFO = "reviewsensei:v6:token-escrow:v1";
export const V6_ESCROW_IV_BYTES = 12;

export const V6_ATTESTATION_HKDF_SALT = "v6-pr-coordinator-owner-attestation-v1";
export const V6_ATTESTATION_HKDF_INFO = "reviewsensei:v6:owner-attestation:v1";

export interface EncryptedEscrow {
  version: 1;
  iv_b64: string;
  ciphertext_b64: string;
}

export interface EscrowAadFields {
  slot_key: string;
  generation: number;
  reservation_id: string;
}

export interface OwnerAttestationTuple {
  slot_key: string;
  generation: number;
  lease_epoch: number;
  reservation_id: string;
  run_id: string;
  run_attempt: number;
  pr_head_sha: string;
  workflow_sha: string;
}

const ENCODER = new TextEncoder();
const DECODER = new TextDecoder("utf-8", { fatal: true });

function utf8Bytes(value: string): Uint8Array {
  return ENCODER.encode(value);
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function fromBase64(value: string): Uint8Array | null {
  if (typeof value !== "string" || value.length === 0 || value.length % 4 !== 0) {
    return null;
  }
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
    return null;
  }
  try {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  } catch {
    return null;
  }
}

function hexFromBytes(bytes: Uint8Array): string {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function bytesFromHex64(value: string): Uint8Array | null {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) {
    return null;
  }
  const bytes = new Uint8Array(32);
  for (let index = 0; index < 32; index += 1) {
    bytes[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

function normalizeJsonValue(value: unknown): unknown {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("v6_canonical_json_invalid");
    }
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => normalizeJsonValue(item));
  }
  if (typeof value === "object") {
    const sorted: Record<string, unknown> = {};
    const record = value as Record<string, unknown>;
    for (const key of Object.keys(record).sort()) {
      const entry = record[key];
      if (entry !== undefined) {
        sorted[key] = normalizeJsonValue(entry);
      }
    }
    return sorted;
  }
  throw new Error("v6_canonical_json_invalid");
}

/**
 * Deterministic compact canonical JSON matching Python's `canonical_json_dumps`
 * and escaping `<` and `>` as `\u003c` and `\u003e` for HTML comment safety.
 */
export function canonicalJsonString(value: unknown): string {
  return JSON.stringify(normalizeJsonValue(value))
    .replaceAll("<", "\\u003c")
    .replaceAll(">", "\\u003e");
}

export async function sha256Hex(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === "string" ? utf8Bytes(value) : value;
  const digest = await crypto.subtle.digest("SHA-256", toArrayBuffer(bytes));
  return hexFromBytes(new Uint8Array(digest));
}

function requireSigningKey(signingKey: string | undefined): string {
  if (typeof signingKey !== "string" || !signingKey.trim()) {
    throw new Error("configuration_unavailable");
  }
  return signingKey;
}

async function importHkdfRoot(signingKey: string | undefined): Promise<CryptoKey> {
  const secret = requireSigningKey(signingKey);
  return crypto.subtle.importKey(
    "raw",
    toArrayBuffer(utf8Bytes(secret)),
    "HKDF",
    false,
    ["deriveKey"],
  );
}

function escrowAadBytes(aad: EscrowAadFields): Uint8Array {
  if (
    typeof aad.slot_key !== "string" ||
    !aad.slot_key ||
    typeof aad.generation !== "number" ||
    !Number.isSafeInteger(aad.generation) ||
    aad.generation < 1 ||
    typeof aad.reservation_id !== "string" ||
    !aad.reservation_id
  ) {
    throw new Error("v6_lease_escrow_lost");
  }
  return utf8Bytes(
    canonicalJsonString({
      slot_key: aad.slot_key,
      generation: aad.generation,
      reservation_id: aad.reservation_id,
    }),
  );
}

function ownerAttestationBytes(tuple: OwnerAttestationTuple): Uint8Array {
  return utf8Bytes(
    canonicalJsonString({
      slot_key: tuple.slot_key,
      generation: tuple.generation,
      lease_epoch: tuple.lease_epoch,
      reservation_id: tuple.reservation_id,
      run_id: tuple.run_id,
      run_attempt: tuple.run_attempt,
      pr_head_sha: tuple.pr_head_sha,
      workflow_sha: tuple.workflow_sha,
    }),
  );
}

export function isEncryptedEscrow(value: unknown): value is EncryptedEscrow {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    Object.keys(record).length === 3 &&
    record.version === 1 &&
    typeof record.iv_b64 === "string" &&
    record.iv_b64.length > 0 &&
    typeof record.ciphertext_b64 === "string" &&
    record.ciphertext_b64.length > 0
  );
}

const REPOSITORY_SLUG_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

/** Parse a comma-separated list of `owner/repo` slugs into a lowercase Set. */
export function parseRepositoryCsv(value: string | undefined): Set<string> {
  if (typeof value !== "string" || !value.trim()) {
    return new Set();
  }
  const result = new Set<string>();
  for (const raw of value.split(",")) {
    const trimmed = raw.trim().toLowerCase();
    if (trimmed && REPOSITORY_SLUG_PATTERN.test(trimmed)) {
      result.add(trimmed);
    }
  }
  return result;
}

/**
 * Strip GitHub tokens, bearer headers, three-segment JWTs, and sensitive query
 * parameters from text before logging or returning diagnostics.
 */
export function redactSensitiveText(text: string): string {
  if (typeof text !== "string" || text.length === 0) {
    return "";
  }
  return text
    .replace(/\bgh[psour]_[A-Za-z0-9_]{4,}\b/g, "[REDACTED_TOKEN]")
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]{4,}/gi, "Bearer [REDACTED]")
    .replace(
      /\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g,
      "[REDACTED_JWT]",
    )
    .replace(
      /([?&](?:token|access_token|code|client_secret|authorization|sig|signature|key|api_key)=)[^&\s#]+/gi,
      "$1[REDACTED]",
    );
}

/**
 * HKDF-SHA256 domain-separated token escrow (AES-GCM-256) and owner-anchor
 * attestation (HMAC-SHA256) for `V6PrCoordinator`.
 */
export class V6EscrowCrypto {
  private escrowKeyPromise: Promise<CryptoKey> | undefined;
  private attestationKeyPromise: Promise<CryptoKey> | undefined;

  constructor(private readonly signingKey: string | undefined) {
    requireSigningKey(signingKey);
  }

  private async escrowKey(): Promise<CryptoKey> {
    this.escrowKeyPromise ??= (async () => {
      const root = await importHkdfRoot(this.signingKey);
      return crypto.subtle.deriveKey(
        {
          name: "HKDF",
          hash: "SHA-256",
          salt: toArrayBuffer(utf8Bytes(V6_ESCROW_HKDF_SALT)),
          info: toArrayBuffer(utf8Bytes(V6_ESCROW_HKDF_INFO)),
        },
        root,
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"],
      );
    })();
    return this.escrowKeyPromise;
  }

  private async attestationKey(): Promise<CryptoKey> {
    this.attestationKeyPromise ??= (async () => {
      const root = await importHkdfRoot(this.signingKey);
      return crypto.subtle.deriveKey(
        {
          name: "HKDF",
          hash: "SHA-256",
          salt: toArrayBuffer(utf8Bytes(V6_ATTESTATION_HKDF_SALT)),
          info: toArrayBuffer(utf8Bytes(V6_ATTESTATION_HKDF_INFO)),
        },
        root,
        { name: "HMAC", hash: "SHA-256", length: 256 },
        false,
        ["sign", "verify"],
      );
    })();
    return this.attestationKeyPromise;
  }

  async sealToken(token: string, aad: EscrowAadFields): Promise<EncryptedEscrow> {
    if (typeof token !== "string" || !token.trim()) {
      throw new Error("v6_lease_escrow_lost");
    }
    const key = await this.escrowKey();
    const iv = crypto.getRandomValues(new Uint8Array(V6_ESCROW_IV_BYTES));
    const additionalData = escrowAadBytes(aad);
    const ciphertext = new Uint8Array(
      await crypto.subtle.encrypt(
        {
          name: "AES-GCM",
          iv: toArrayBuffer(iv),
          additionalData: toArrayBuffer(additionalData),
        },
        key,
        toArrayBuffer(utf8Bytes(token)),
      ),
    );
    return {
      version: 1,
      iv_b64: toBase64(iv),
      ciphertext_b64: toBase64(ciphertext),
    };
  }

  async unsealToken(escrow: unknown, aad: EscrowAadFields): Promise<string> {
    if (!isEncryptedEscrow(escrow)) {
      throw new Error("v6_lease_escrow_lost");
    }
    const iv = fromBase64(escrow.iv_b64);
    const ciphertext = fromBase64(escrow.ciphertext_b64);
    if (iv === null || iv.byteLength !== V6_ESCROW_IV_BYTES || ciphertext === null || ciphertext.byteLength === 0) {
      throw new Error("v6_lease_escrow_lost");
    }
    const key = await this.escrowKey();
    const additionalData = escrowAadBytes(aad);
    try {
      const plaintext = await crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: toArrayBuffer(iv),
          additionalData: toArrayBuffer(additionalData),
        },
        key,
        toArrayBuffer(ciphertext),
      );
      const token = DECODER.decode(new Uint8Array(plaintext));
      if (!token.trim()) {
        throw new Error("v6_lease_escrow_lost");
      }
      return token;
    } catch {
      throw new Error("v6_lease_escrow_lost");
    }
  }

  async signOwnerAttestation(tuple: OwnerAttestationTuple): Promise<string> {
    const key = await this.attestationKey();
    const mac = await crypto.subtle.sign(
      "HMAC",
      key,
      toArrayBuffer(ownerAttestationBytes(tuple)),
    );
    return hexFromBytes(new Uint8Array(mac));
  }

  async verifyOwnerAttestation(
    tuple: OwnerAttestationTuple,
    macHex: string,
  ): Promise<boolean> {
    const macBytes = bytesFromHex64(macHex);
    if (macBytes === null) {
      return false;
    }
    const key = await this.attestationKey();
    return crypto.subtle.verify(
      "HMAC",
      key,
      toArrayBuffer(macBytes),
      toArrayBuffer(ownerAttestationBytes(tuple)),
    );
  }
}
