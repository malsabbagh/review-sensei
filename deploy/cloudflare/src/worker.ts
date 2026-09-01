import type { WorkerEnv } from "./env";
import { DeliveryLedger } from "./delivery-ledger";
import { BrokerLedger } from "./broker-ledger";
import { TokenBroker } from "./token-broker";
import {
  MAX_WEBHOOK_BODY_BYTES,
  WebhookPayloadError,
  parseVerifiedDelivery,
  processDelivery,
} from "./github-app";

const LEDGER_NAME = "reviewsensei-deliveries";

export { DeliveryLedger };
export { BrokerLedger };

function response(body: unknown, status = 200, noStore = false): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...(noStore ? { "cache-control": "no-store" } : {}),
    },
  });
}

/**
 * Convert setup failures into stable, non-sensitive diagnostics.
 *
 * GitHub/API errors in this Worker use fixed machine-readable codes, while
 * setup validation errors use human-readable messages. Never return an
 * arbitrary error message: it could contain a repository name, response
 * body, or another value that should remain private.
 */
export function setupErrorCode(error: unknown): string {
  const message = error instanceof Error ? error.message.trim() : "";
  if (/^github_[a-z0-9_]{1,80}$/.test(message)) {
    return message;
  }
  if (message === "GitHub App installation lacks setup permissions") {
    return "github_installation_permissions_missing";
  }
  if (message === "GitHub request failed temporarily") {
    return "github_request_transient";
  }
  if (message === "GitHub request was rejected") {
    return "github_request_rejected";
  }
  if (message === "GitHub request failed") {
    return "github_request_failed";
  }
  if (message === "Setup repository must be an owner/repo slug") {
    return "setup_repository_invalid";
  }
  if (message === "Setup branch inputs were invalid") {
    return "setup_branch_invalid";
  }
  if (message.startsWith("GitHub setup file")) {
    return "github_setup_file_invalid";
  }
  if (message.startsWith("GitHub setup response")) {
    return "github_setup_response_invalid";
  }
  if (message.startsWith("GitHub setup branch")) {
    return "github_setup_branch_invalid";
  }
  if (message.startsWith("GitHub installation repositories")) {
    return "github_installation_repositories_failed";
  }
  if (message.startsWith("PUBLIC_WORKFLOW_TAG")) {
    return "public_workflow_tag_invalid";
  }
  if (message.startsWith("PUBLIC_WORKFLOW_REF")) {
    return "public_workflow_ref_invalid";
  }
  if (message.startsWith("PUBLIC_WORKFLOW_SHA")) {
    return "public_workflow_sha_invalid";
  }
  if (message === "public workflow tag unavailable") {
    return "public_workflow_tag_unavailable";
  }
  return "setup_failed";
}

const BROKER_CAPABILITIES = new Set([
  "review_publish",
  "inline_reply",
  "issue_reply",
  "learning_write",
]);

/**
 * Keep broker diagnostics useful without copying arbitrary exception text into
 * Worker logs. GitHub and OIDC failures use stable machine-readable codes;
 * every other exception is intentionally collapsed to one generic code.
 */
export function brokerErrorCode(error: unknown): string {
  const message = error instanceof Error ? error.message.trim() : "";
  return /^(?:broker|oidc|github)_[a-z0-9_]{1,120}$/.test(message)
    ? message
    : "broker_failed";
}

function brokerCapability(body: unknown): string {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return "invalid";
  }
  const value = (body as Record<string, unknown>).capability;
  if (value === undefined) {
    return "review_publish";
  }
  return typeof value === "string" && BROKER_CAPABILITIES.has(value)
    ? value
    : "invalid";
}

function brokerRayId(request: Request): string | undefined {
  const value = request.headers.get("cf-ray");
  return value !== null && /^[a-f0-9]{16,64}-[a-z]{3}$/i.test(value)
    ? value
    : undefined;
}

const MAX_BROKER_REQUEST_BYTES = 64 * 1024;

async function readBoundedBody(request: Request, maximum: number): Promise<ArrayBuffer> {
  if (request.body === null) {
    return new ArrayBuffer(0);
  }
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const next = await reader.read();
    if (next.done) {
      break;
    }
    total += next.value.byteLength;
    if (total > maximum) {
      await reader.cancel();
      throw new Error("request_body_too_large");
    }
    chunks.push(next.value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body.buffer;
}

async function token(request: Request, env: WorkerEnv): Promise<Response> {
  if (request.headers.has("origin") || request.headers.has("access-control-request-method")) {
    return response({ error: "cors_not_supported" }, 400, true);
  }
  const length = request.headers.get("content-length");
  if (length === null) {
    return response({ error: "content_length_required" }, 411, true);
  }
  if (!/^\d+$/.test(length) || Number(length) > MAX_BROKER_REQUEST_BYTES) {
    return response({ error: "payload_too_large" }, 413, true);
  }
  let body: unknown;
  try {
    const bytes = await readBoundedBody(request, MAX_BROKER_REQUEST_BYTES);
    if (bytes.byteLength === 0 || bytes.byteLength > MAX_BROKER_REQUEST_BYTES) {
      return response({ error: "payload_too_large" }, 413, true);
    }
    body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch {
    return response({ error: "invalid_request" }, 400, true);
  }
  try {
    const result = await new TokenBroker(env).exchange(
      body as Record<string, unknown>,
      request.headers.get("cf-connecting-ip") ?? undefined,
    );
    return response(result, 200, true);
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    const status =
      message === "broker_rate_limited"
        ? 429
        : message === "broker_ledger_unavailable"
          ? 503
          : 403;
    const rayId = brokerRayId(request);
    console.error("github_broker_failed", {
      error_code: brokerErrorCode(error),
      capability: brokerCapability(body),
      ...(rayId === undefined ? {} : { cf_ray: rayId }),
    });
    return response({ error: "capability_not_issued" }, status, true);
  }
}

function appId(value: string | undefined): number | null {
  if (!value || !/^[1-9][0-9]{0,18}$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function printableHeader(value: string | null, maxLength: number): string | null {
  if (
    !value ||
    value.length > maxLength ||
    /[\r\n]/.test(value) ||
    !/^[\x21-\x7e]+$/.test(value)
  ) {
    return null;
  }
  return value;
}

function hex(bytes: ArrayBuffer): string {
  return [...new Uint8Array(bytes)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function constantTimeEquals(left: string, right: string): boolean {
  if (left.length !== right.length) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

async function hmacMatches(
  body: ArrayBuffer,
  signature: string | null,
  secret: string,
): Promise<boolean> {
  if (
    !signature ||
    !secret ||
    !secret.trim() ||
    !/^sha256=[0-9a-f]{64}$/.test(signature)
  ) {
    return false;
  }
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, body);
  return constantTimeEquals(signature, `sha256=${hex(digest)}`);
}

async function bodyDigest(body: ArrayBuffer): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", body));
}

interface LedgerReply {
  state: "accepted" | "in_flight" | "claimed" | "conflict";
}

async function ledgerRequest(
  env: WorkerEnv,
  action: "claim" | "complete" | "release",
  app: number,
  deliveryId: string,
  digest: string,
): Promise<LedgerReply> {
  const id = env.DELIVERY_LEDGER.idFromName(LEDGER_NAME);
  const stub = env.DELIVERY_LEDGER.get(id);
  const request = await stub.fetch(`https://ledger/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ app_id: app, delivery_id: deliveryId, digest }),
  });
  if (!request.ok) {
    throw new Error("delivery ledger request failed");
  }
  return (await request.json()) as LedgerReply;
}

async function webhook(request: Request, env: WorkerEnv): Promise<Response> {
  const app = appId(env.GITHUB_APP_ID);
  if (
    app === null ||
    !env.GITHUB_APP_WEBHOOK_SECRET ||
    !env.GITHUB_APP_WEBHOOK_SECRET.trim()
  ) {
    return response({ error: "configuration_unavailable" }, 503);
  }
  const lengthHeader = request.headers.get("content-length");
  const length = Number(lengthHeader ?? "-1");
  if (!Number.isSafeInteger(length) || length < 0) {
    return response({ error: "content_length_required" }, 411);
  }
  if (length > MAX_WEBHOOK_BODY_BYTES) {
    return response({ error: "payload_too_large" }, 413);
  }
  const event = printableHeader(request.headers.get("x-github-event"), 100);
  const deliveryId = printableHeader(
    request.headers.get("x-github-delivery"),
    200,
  );
  if (!event || !deliveryId) {
    return response({ error: "github_headers_required" }, 400);
  }
  let body: ArrayBuffer;
  try {
    body = await readBoundedBody(request, MAX_WEBHOOK_BODY_BYTES);
  } catch {
    return response({ error: "payload_too_large" }, 413);
  }
  if (
    !(await hmacMatches(
      body,
      request.headers.get("x-hub-signature-256"),
      env.GITHUB_APP_WEBHOOK_SECRET,
    ))
  ) {
    return response({ error: "signature_invalid" }, 401);
  }

  const digest = await bodyDigest(body);
  let claim: LedgerReply;
  try {
    claim = await ledgerRequest(env, "claim", app, deliveryId, digest);
  } catch {
    return response({ error: "delivery_ledger_unavailable" }, 503);
  }
  if (claim.state === "accepted" || claim.state === "in_flight") {
    return response({ accepted: true, duplicate: true }, 202);
  }
  if (claim.state === "conflict") {
    return response({ error: "delivery_conflict" }, 409);
  }

  try {
    const delivery = parseVerifiedDelivery(body, event, deliveryId, app);
    if (delivery !== null) {
      await processDelivery(delivery, env);
    }
    await ledgerRequest(env, "complete", app, deliveryId, digest);
    return response({ accepted: true }, 202);
  } catch (error) {
    if (error instanceof WebhookPayloadError) {
      try {
        await ledgerRequest(env, "complete", app, deliveryId, digest);
      } catch {
        // The lease expiry remains the recovery path if completion fails.
      }
      return response({ accepted: false }, 202);
    }
    try {
      await ledgerRequest(env, "release", app, deliveryId, digest);
    } catch {
      // The lease expiry remains the recovery path if release also fails.
    }
    const errorCode = setupErrorCode(error);
    console.error("github_setup_failed", {
      delivery_id: deliveryId,
      event,
      error_code: errorCode,
    });
    return response({ error: "setup_unavailable", error_code: errorCode }, 503, true);
  }
}

const worker = {
  async fetch(request: Request, env: WorkerEnv): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/healthz" && request.method === "GET") {
      return response({ ok: true });
    }
    if (url.pathname === "/github/token") {
      if (request.method !== "POST") {
        return response({ error: "method_not_allowed" }, 405, true);
      }
      return token(request, env);
    }
    if (url.pathname !== "/github/webhook") {
      return response({ error: "not_found" }, 404);
    }
    if (request.method !== "POST") {
      return response({ error: "method_not_allowed" }, 405);
    }
    return webhook(request, env);
  },
};

export default worker;
