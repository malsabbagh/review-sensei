import type { WorkerEnv } from "./env";
import { DeliveryLedger } from "./delivery-ledger";
import {
  MAX_WEBHOOK_BODY_BYTES,
  WebhookPayloadError,
  parseVerifiedDelivery,
  processDelivery,
} from "./github-app";

const LEDGER_NAME = "reviewsensei-deliveries";

export { DeliveryLedger };

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
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
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_WEBHOOK_BODY_BYTES) {
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
    return response({ error: "setup_unavailable" }, 503);
  }
}

const worker = {
  async fetch(request: Request, env: WorkerEnv): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/healthz" && request.method === "GET") {
      return response({ ok: true });
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
