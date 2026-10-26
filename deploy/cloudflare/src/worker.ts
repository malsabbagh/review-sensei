import { BrokerObservation } from "./broker-observation";
import { WorkflowLookupError } from "./broker-lookup-diagnostic";
import { BrokerDispatchControl } from "./broker-dispatch-control";
import type { WorkerEnv, WorkerExecution } from "./env";
import { TokenBroker } from "./token-broker";
import { brokerDiagnostic } from "./broker-diagnostics";
import {
  MAX_WEBHOOK_BODY_BYTES,
  WebhookPayloadError,
  deferInstallationSetup,
  parseVerifiedDelivery,
  processDelivery,
} from "./github-app";
import { freshSetupCursor, signSetupCursor } from "./signed-mac";
import { runSignedSetupStep, scheduleSetupContinue } from "./setup-step";

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
  if (
    message === "setup_continuation_unavailable" ||
    message === "setup_continuation_invalid"
  ) {
    return message;
  }
  return "setup_failed";
}

const BROKER_CAPABILITIES = new Set([
  "review_publish",
  "check_publish",
  "review_session",
  "review_status",
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
  return brokerDiagnostic(error).code;
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
const MAX_BODY_READ_MS = 10_000;

async function readBoundedBody(
  request: Request,
  maximum: number,
  deadlineUnixMs?: number,
): Promise<ArrayBuffer> {
  if (request.body === null) {
    return new ArrayBuffer(0);
  }
  const effectiveDeadline =
    deadlineUnixMs === undefined
      ? Date.now() + MAX_BODY_READ_MS
      : Math.min(Date.now() + MAX_BODY_READ_MS, deadlineUnixMs);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const remaining = effectiveDeadline - Date.now();
    if (remaining <= 0) {
      void reader.cancel().catch(() => undefined);
      throw new Error("broker_request_deadline_exhausted");
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    let next: ReadableStreamReadResult<Uint8Array>;
    try {
      next = await Promise.race([
        reader.read(),
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () => reject(new Error("broker_request_deadline_exhausted")),
            remaining,
          );
        }),
      ]);
    } catch (error) {
      void reader.cancel().catch(() => undefined);
      throw error;
    } finally {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    }
    if (next.done) {
      break;
    }
    total += next.value.byteLength;
    if (total > maximum) {
      void reader.cancel().catch(() => undefined);
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
  const observation = new BrokerObservation("exchange", brokerRayId(request));
  const refuse = (
    errorCode: string,
    diagnosticError: Error,
    status: number,
  ): Response => {
    observation.finish("refused", diagnosticError);
    return response(
      {
        error: errorCode,
        diagnostic: brokerDiagnostic(diagnosticError, brokerRayId(request)),
      },
      status,
      true,
    );
  };
  if (
    request.headers.has("origin") ||
    request.headers.has("access-control-request-method")
  ) {
    return refuse(
      "cors_not_supported",
      new Error("broker_cors_not_supported"),
      400,
    );
  }
  const length = request.headers.get("content-length");
  if (length === null) {
    return refuse(
      "content_length_required",
      new Error("broker_content_length_required"),
      411,
    );
  }
  if (!/^\d+$/.test(length) || Number(length) > MAX_BROKER_REQUEST_BYTES) {
    return refuse(
      "payload_too_large",
      new Error("broker_payload_too_large"),
      413,
    );
  }
  const deadlineHeader = request.headers.get("x-reviewsensei-deadline-unix-ms");
  const parsedDeadline =
    deadlineHeader !== null && /^[0-9]{1,13}$/.test(deadlineHeader)
      ? Number(deadlineHeader)
      : undefined;
  let body: unknown;
  try {
    const bytes = await readBoundedBody(
      request,
      MAX_BROKER_REQUEST_BYTES,
      parsedDeadline,
    );
    if (bytes.byteLength === 0 || bytes.byteLength > MAX_BROKER_REQUEST_BYTES) {
      return refuse(
        "payload_too_large",
        new Error("broker_payload_too_large"),
        413,
      );
    }
    body = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    ) as unknown;
  } catch (error) {
    if (
      error instanceof Error &&
      error.message === "broker_request_deadline_exhausted"
    ) {
      return refuse("capability_not_issued", error, 403);
    }
    if (error instanceof Error && error.message === "request_body_too_large") {
      return refuse(
        "payload_too_large",
        new Error("broker_payload_too_large"),
        413,
      );
    }
    return refuse("invalid_request", new Error("broker_invalid_request"), 400);
  }
  let failure: unknown;
  let outcome: "success" | "refused" = "success";
  try {
    if (deadlineHeader !== null && parsedDeadline === undefined) {
      throw new Error("broker_request_deadline_invalid");
    }
    const result = await new TokenBroker(env, undefined, observation).exchange(
      body as Record<string, unknown>,
      request.headers.get("cf-connecting-ip") ?? undefined,
      parsedDeadline,
    );
    return response(result, 200, true);
  } catch (error) {
    failure = error;
    outcome = "refused";
    const message = error instanceof Error ? error.message : "";
    const status = message === "configuration_unavailable" ? 503 : 403;
    const rayId = brokerRayId(request);
    const diagnostic = brokerDiagnostic(error, rayId);
    const lookup =
      error instanceof WorkflowLookupError ? error.lookup : undefined;
    console.error("github_broker_failed", {
      ...(lookup === undefined ? {} : { lookup }),
      error_code: diagnostic.code,
      stage: diagnostic.stage,
      action: diagnostic.action,
      ...("upstream_status" in diagnostic
        ? { upstream_status: diagnostic.upstream_status }
        : {}),
      capability: brokerCapability(body),
      ...(rayId === undefined ? {} : { cf_ray: rayId }),
    });
    const errorCode =
      message === "configuration_unavailable"
        ? "configuration_unavailable"
        : "capability_not_issued";
    return response(
      {
        error: errorCode,
        diagnostic,
        ...(lookup === undefined ? {} : { lookup }),
      },
      status,
      true,
    );
  } finally {
    observation.finish(outcome, failure);
  }
}

/**
 * Validate a broker-held opaque session grant immediately before a ledger
 * mutation. The response contains only the immutable, already-attested scope;
 * it never echoes the bearer grant or an installation capability token.
 */
async function sessionGrant(
  request: Request,
  env: WorkerEnv,
): Promise<Response> {
  const observation = new BrokerObservation(
    "session_grant",
    brokerRayId(request),
  );
  const refuse = (
    errorCode: string,
    diagnosticError: Error,
    status: number,
  ): Response => {
    observation.finish("refused", diagnosticError);
    return response(
      {
        error: errorCode,
        diagnostic: brokerDiagnostic(diagnosticError, brokerRayId(request)),
      },
      status,
      true,
    );
  };
  if (
    request.headers.has("origin") ||
    request.headers.has("access-control-request-method")
  ) {
    return refuse(
      "cors_not_supported",
      new Error("broker_cors_not_supported"),
      400,
    );
  }
  const length = request.headers.get("content-length");
  if (length === null) {
    return refuse(
      "content_length_required",
      new Error("broker_content_length_required"),
      411,
    );
  }
  if (!/^\d+$/.test(length) || Number(length) > MAX_BROKER_REQUEST_BYTES) {
    return refuse(
      "payload_too_large",
      new Error("broker_payload_too_large"),
      413,
    );
  }
  const deadlineHeader = request.headers.get("x-reviewsensei-deadline-unix-ms");
  const parsedDeadline =
    deadlineHeader !== null && /^[0-9]{1,13}$/.test(deadlineHeader)
      ? Number(deadlineHeader)
      : undefined;
  let body: unknown;
  try {
    const bytes = await readBoundedBody(
      request,
      MAX_BROKER_REQUEST_BYTES,
      parsedDeadline,
    );
    if (bytes.byteLength === 0 || bytes.byteLength > MAX_BROKER_REQUEST_BYTES) {
      return refuse(
        "payload_too_large",
        new Error("broker_payload_too_large"),
        413,
      );
    }
    body = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    ) as unknown;
  } catch (error) {
    if (
      error instanceof Error &&
      error.message === "broker_request_deadline_exhausted"
    ) {
      return refuse("session_grant_not_verified", error, 403);
    }
    if (error instanceof Error && error.message === "request_body_too_large") {
      return refuse(
        "payload_too_large",
        new Error("broker_payload_too_large"),
        413,
      );
    }
    return refuse("invalid_request", new Error("broker_invalid_request"), 400);
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return refuse("invalid_request", new Error("broker_invalid_request"), 400);
  }
  let failure: unknown;
  let outcome: "success" | "refused" = "success";
  try {
    const values = body as Record<string, unknown>;
    const bodyKeys = Object.keys(values);
    if (
      bodyKeys.length !== (Object.hasOwn(values, "control_budget") ? 3 : 2) ||
      !Object.hasOwn(values, "session_grant") ||
      !Object.hasOwn(values, "session_attestation")
    ) {
      outcome = "refused";
      failure = new Error("broker_invalid_request");
      return response(
        {
          error: "invalid_request",
          diagnostic: brokerDiagnostic(
            new Error("broker_invalid_request"),
            brokerRayId(request),
          ),
        },
        400,
        true,
      );
    }
    const control =
      values.control_budget === undefined
        ? undefined
        : new BrokerDispatchControl(values.control_budget);
    if (deadlineHeader !== null && parsedDeadline === undefined) {
      throw new Error("broker_request_deadline_invalid");
    }
    const broker = new TokenBroker(env, undefined, observation);
    const attestation = await broker.verifySessionGrant(
      values.session_grant,
      values.session_attestation,
      control,
      parsedDeadline,
    );
    return response({ session_attestation: attestation, ...(control === undefined ? {} : { control_receipt: control.receipt() }) }, 200, true);
  } catch (error) {
    failure = error;
    outcome = "refused";
    const rayId = brokerRayId(request);
    const diagnostic = brokerDiagnostic(error, rayId);
    const lookup = error instanceof WorkflowLookupError ? error.lookup : undefined;
    console.error("github_session_grant_failed", {
      ...(lookup === undefined ? {} : {lookup}),
      error_code: diagnostic.code,
      stage: diagnostic.stage,
      action: diagnostic.action,
      ...("upstream_status" in diagnostic ? { upstream_status: diagnostic.upstream_status } : {}),
      ...(rayId === undefined ? {} : { cf_ray: rayId }),
    });
    const unavailable = error instanceof Error && error.message === "configuration_unavailable";
    const status = unavailable ? 503 : 403;
    const errorCode = unavailable ? "configuration_unavailable" : "session_grant_not_verified";
    return response({ error: errorCode, diagnostic, ...(lookup === undefined ? {} : {lookup}) }, status, true);
  } finally { observation.finish(outcome, failure); }
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

async function webhook(
  request: Request,
  env: WorkerEnv,
  ctx: WorkerExecution | undefined,
): Promise<Response> {
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

  try {
    const delivery = parseVerifiedDelivery(body, event, deliveryId, app);
    if (delivery !== null && deferInstallationSetup(delivery)) {
      if (
        ctx === undefined ||
        env.SELF === undefined ||
        !env.REVIEWSENSEI_SIGNING_KEY ||
        !env.REVIEWSENSEI_SIGNING_KEY.trim()
      ) {
        return response({ error: "configuration_unavailable" }, 503);
      }
      const cursor = await signSetupCursor(
        env.REVIEWSENSEI_SIGNING_KEY,
        freshSetupCursor({
          installationId: delivery.installationId,
          deliveryId: delivery.deliveryId,
        }),
      );
      scheduleSetupContinue(env, ctx, cursor);
      return response({ accepted: true }, 202);
    }
    if (delivery !== null) {
      await processDelivery(delivery, env);
    }
    return response({ accepted: true }, 202);
  } catch (error) {
    if (error instanceof WebhookPayloadError) {
      return response({ accepted: false }, 202);
    }
    const errorCode = setupErrorCode(error);
    console.error("github_setup_failed", {
      delivery_id: deliveryId,
      error_code: errorCode,
      failure_count: 1,
    });
    return response({ error: "setup_unavailable", error_code: errorCode }, 503, true);
  }
}

async function setupContinue(
  request: Request,
  env: WorkerEnv,
  ctx: WorkerExecution | undefined,
): Promise<Response> {
  if (
    ctx === undefined ||
    env.SELF === undefined ||
    !env.REVIEWSENSEI_SIGNING_KEY ||
    !env.REVIEWSENSEI_SIGNING_KEY.trim()
  ) {
    return response({ error: "configuration_unavailable" }, 503);
  }
  let body: unknown;
  try {
    const bytes = await readBoundedBody(request, MAX_BROKER_REQUEST_BYTES);
    body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch {
    return response({ error: "invalid_request" }, 400);
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return response({ error: "invalid_request" }, 400);
  }
  const cursor = (body as Record<string, unknown>).cursor;
  if (typeof cursor !== "string" || Object.keys(body as Record<string, unknown>).length !== 1) {
    return response({ error: "invalid_request" }, 400);
  }
  try {
    const result = await runSignedSetupStep(env, cursor, ctx);
    return response({ accepted: true, setup: result }, 202);
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (message === "configuration_unavailable") {
      return response({ error: "configuration_unavailable" }, 503);
    }
    if (message === "setup_continuation_invalid") {
      return response({ error: "setup_continuation_invalid" }, 403);
    }
    const errorCode = setupErrorCode(error);
    console.error("github_setup_failed", {
      delivery_id: "unavailable",
      error_code: errorCode,
      failure_count: 1,
    });
    return response({ error: "setup_unavailable", error_code: errorCode }, 503);
  }
}

const worker = {
  async fetch(request: Request, env: WorkerEnv, ctx?: WorkerExecution): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/healthz" && request.method === "GET") {
      return response({ ok: true });
    }
    if (
      (url.pathname === "/github/token" || url.pathname === "/github/session-grant") &&
      env.BROKER_ADMISSION_MODE !== undefined && env.BROKER_ADMISSION_MODE !== "open"
    ) {
      // A sustained operator hold spans caller and Worker cutover. Refuse
      // before parsing, OIDC, token/grant issuance or any queued continuation.
      // Invalid configured modes fail held. This never revokes existing tokens.
      return response({ error: "broker_admission_held", temporary: true }, 503, true);
    }
    if (url.pathname === "/github/token") {
      if (request.method !== "POST") {
        return response({ error: "method_not_allowed" }, 405, true);
      }
      return token(request, env);
    }
    if (url.pathname === "/github/session-grant") {
      if (request.method !== "POST") {
        return response({ error: "method_not_allowed" }, 405, true);
      }
      return sessionGrant(request, env);
    }
    if (url.pathname === "/github/setup-continue") {
      if (request.method !== "POST") {
        return response({ error: "method_not_allowed" }, 405);
      }
      return setupContinue(request, env, ctx);
    }
    if (url.pathname !== "/github/webhook") {
      return response({ error: "not_found" }, 404);
    }
    if (request.method !== "POST") {
      return response({ error: "method_not_allowed" }, 405);
    }
    return webhook(request, env, ctx);
  },
};

export default worker;
