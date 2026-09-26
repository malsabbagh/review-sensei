import {
  GitHubAuthConfigurationError,
  GitHubSetupTransientError,
  repositorySlug,
} from "./github-app";

const MAX_SETUP_REPOSITORIES = 1000;
const MAX_SETUP_ATTEMPTS = 3;
const MAX_PERMISSIONS = 32;
const DELIVERY_ID_PATTERN = /^[\x21-\x7e]{1,200}$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const PERMISSION_NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const PERMISSION_LEVEL_PATTERN = /^[a-z]{1,16}$/;

export interface SetupContinuationRequest {
  appId: number;
  event: string;
  action: string;
  installationId: number;
  deliveryId: string;
  digest: string;
  permissions: Record<string, string>;
  repositories: string[];
  unresolved: boolean;
  attempt: number;
  failed: boolean;
  /** Stable machine code for the latest terminal repository failure. No prose. */
  failureCode: string | null;
  failureCount: number;
  /** Repository whose setup failed. Null when the failure happened before one was selected. */
  failedRepository: string | null;
}

export interface SetupFailureSummary {
  errorCode: string;
  failureCount: number;
  repository: string | null;
}

export interface SetupContinuationOps {
  processRepository(repository: string): Promise<void>;
  resolveRepositories(): Promise<string[]>;
  schedule(next: SetupContinuationRequest, delayMs?: number): Promise<void>;
  complete(): Promise<void>;
  release(summary: SetupFailureSummary): Promise<void>;
  report(error: unknown): void;
}

export class SetupContinuationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SetupContinuationError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function setupAction(event: unknown, action: unknown): boolean {
  return (
    (event === "installation" &&
      (action === "created" || action === "new_permissions_accepted")) ||
    (event === "installation_repositories" && action === "added")
  );
}

function permissionsFrom(value: unknown): Record<string, string> {
  if (!isRecord(value)) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  const names = Object.keys(value);
  if (names.length > MAX_PERMISSIONS) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  const permissions: Record<string, string> = {};
  for (const name of names) {
    const level = value[name];
    if (
      !PERMISSION_NAME_PATTERN.test(name) ||
      typeof level !== "string" ||
      !PERMISSION_LEVEL_PATTERN.test(level)
    ) {
      throw new SetupContinuationError("setup_continuation_invalid");
    }
    permissions[name] = level;
  }
  return permissions;
}

function repositoriesFrom(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > MAX_SETUP_REPOSITORIES) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  const repositories: string[] = [];
  for (const repository of value) {
    if (!repositorySlug(repository)) {
      throw new SetupContinuationError("setup_continuation_invalid");
    }
    repositories.push(repository);
  }
  return repositories;
}

export function parseSetupContinuationRequest(value: unknown): SetupContinuationRequest {
  if (!isRecord(value) || !setupAction(value.event, value.action)) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (!positiveInteger(value.appId) || !positiveInteger(value.installationId)) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (typeof value.deliveryId !== "string" || !DELIVERY_ID_PATTERN.test(value.deliveryId)) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (typeof value.digest !== "string" || !DIGEST_PATTERN.test(value.digest)) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (
    typeof value.attempt !== "number" ||
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 0 ||
    value.attempt >= MAX_SETUP_ATTEMPTS
  ) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (typeof value.unresolved !== "boolean" || typeof value.failed !== "boolean") {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (
    value.failureCode !== null &&
    (typeof value.failureCode !== "string" || !/^[a-z0-9_]{1,80}$/.test(value.failureCode))
  ) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (
    typeof value.failureCount !== "number" ||
    !Number.isSafeInteger(value.failureCount) ||
    value.failureCount < 0 ||
    value.failureCount > MAX_SETUP_REPOSITORIES
  ) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  if (value.failedRepository !== null && !repositorySlug(value.failedRepository)) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  const repositories = repositoriesFrom(value.repositories);
  if (value.unresolved && repositories.length > 0) {
    throw new SetupContinuationError("setup_continuation_invalid");
  }
  return {
    appId: value.appId,
    event: value.event as string,
    action: value.action as string,
    installationId: value.installationId,
    deliveryId: value.deliveryId,
    digest: value.digest,
    permissions: permissionsFrom(value.permissions),
    repositories,
    unresolved: value.unresolved,
    attempt: value.attempt,
    failed: value.failed,
    failureCode: value.failureCode,
    failureCount: value.failureCount,
    failedRepository: value.failedRepository,
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

const EXACT_PROSE_CODES: Readonly<Record<string, string>> = {
  "GitHub App installation lacks setup permissions": "github_installation_permissions_missing",
  "PUBLIC_WORKFLOW_TAG must be a valid single-segment git tag": "public_workflow_tag_invalid",
  "PUBLIC_WORKFLOW_REF must be a non-empty single-line value": "public_workflow_ref_invalid",
  "PUBLIC_WORKFLOW_SHA must be exactly 40 lowercase hexadecimal characters": "public_workflow_sha_invalid",
  "public workflow tag unavailable": "public_workflow_tag_unavailable",
  "GitHub request failed": "github_request_failed",
  "GitHub request failed temporarily": "github_request_transient",
};

const FATAL_CODES = new Set([
  "github_app_key_invalid",
  "github_app_id_invalid",
  "github_api_url_invalid",
  "github_installation_invalid",
  "github_installation_token_invalid",
  "github_installation_permissions_invalid",
  "github_installation_permissions_missing",
  "github_installation_repositories_invalid",
  "github_installation_repositories_limit",
  "github_workflow_tag_invalid",
  "public_workflow_tag_invalid",
  "public_workflow_ref_invalid",
  "public_workflow_sha_invalid",
  "setup_continuation_invalid",
  "setup_continuation_unavailable",
]);

const TRANSIENT_CODES = new Set([
  "github_installation_repositories_failed",
  "github_workflow_tag_unavailable",
  "public_workflow_tag_unavailable",
]);

const STATUS_CODE = /^(github_request_transient|github_request_rejected|github_installation_token_failed|github_workflow_tag_unavailable)_(\d{3})$/;

/** Static setup codes safe to store and log. Anything else collapses to setup_failed. */
const RECORDED_EXACT_CODES = new Set([
  ...FATAL_CODES,
  ...TRANSIENT_CODES,
  "github_request_failed",
  "github_request_transient",
  "github_repository_invalid",
  "github_response_invalid",
  "github_response_too_large",
  "github_installation_repository_invalid",
  "setup_failed",
]);

const SETUP_RETRY_DELAYS_MS = [1_000, 5_000] as const;

/**
 * Wait before repeating a transient step. The GitHub client only surfaces the
 * status code, so this stays a short bounded delay: 1s, then 5s.
 */
export function setupRetryDelayMs(attempt: number): number {
  const index = Math.max(0, Math.min(attempt, SETUP_RETRY_DELAYS_MS.length - 1));
  return SETUP_RETRY_DELAYS_MS[index] ?? SETUP_RETRY_DELAYS_MS[1];
}

/** Keep outcome rows and logs on the allowlist. Unknown tokens become setup_failed. */
export function recordedErrorCode(code: string): string {
  if (RECORDED_EXACT_CODES.has(code) || STATUS_CODE.test(code)) {
    return code;
  }
  return "setup_failed";
}

/** Stable code for logs and the delivery outcome. Unknown prose collapses to setup_failed. */
export function continuationErrorCode(error: unknown): string {
  const message = errorMessage(error);
  if (/^[a-z0-9_]{1,80}$/.test(message)) {
    return recordedErrorCode(message);
  }
  return EXACT_PROSE_CODES[message] ?? "setup_failed";
}

function statusDisposition(code: string): "transient" | "fatal" | "skip" | null {
  const match = STATUS_CODE.exec(code);
  if (!match) {
    return null;
  }
  const status = Number(match[2]);
  if (match[1] === "github_installation_token_failed" && status === 401) {
    return "fatal";
  }
  if (status === 429 || status >= 500) {
    return "transient";
  }
  return "skip";
}

function classifySetupError(error: unknown): "transient" | "fatal" | "skip" {
  if (error instanceof GitHubSetupTransientError) {
    return "transient";
  }
  if (error instanceof GitHubAuthConfigurationError) {
    return "fatal";
  }
  const code = continuationErrorCode(error);
  if (FATAL_CODES.has(code)) {
    return "fatal";
  }
  const status = statusDisposition(code);
  if (status !== null) {
    return status;
  }
  if (TRANSIENT_CODES.has(code)) {
    return "transient";
  }
  return "skip";
}

function continuationFailure(error: unknown, attempt: number): "retry" | "fatal" | "skip" {
  const kind = classifySetupError(error);
  if (kind === "fatal") {
    return "fatal";
  }
  if (kind === "transient" && attempt + 1 < MAX_SETUP_ATTEMPTS) {
    return "retry";
  }
  return "skip";
}

function failureSummary(input: SetupContinuationRequest): SetupFailureSummary {
  return {
    errorCode: input.failureCode ?? "setup_failed",
    failureCount: input.failureCount,
    repository: input.failedRepository,
  };
}

function withFailure(
  input: SetupContinuationRequest,
  error: unknown,
  repository: string | null,
): SetupContinuationRequest {
  return {
    ...input,
    failed: true,
    failureCode: continuationErrorCode(error),
    failureCount: input.failureCount + 1,
    failedRepository: repository,
  };
}

async function finish(input: SetupContinuationRequest, ops: SetupContinuationOps): Promise<void> {
  // An earlier skip stays the terminal outcome after later repositories
  // succeed. failedRepository names that skipped repository, and the delivery
  // is released rather than completed.
  if (input.failed) {
    await ops.release(failureSummary(input));
    return;
  }
  await ops.complete();
}

/**
 * Reconcile one repository, then schedule the remainder as a new invocation.
 * Keeping each invocation to a single repository stays inside the Workers Free
 * CPU and subrequest limits that otherwise fail the webhook with 503.
 */
export async function runSetupContinuationStep(
  input: SetupContinuationRequest,
  ops: SetupContinuationOps,
): Promise<void> {
  if (input.unresolved) {
    let repositories: string[];
    try {
      repositories = await ops.resolveRepositories();
    } catch (error) {
      const failure = continuationFailure(error, input.attempt);
      if (failure === "retry") {
        await ops.schedule(
          { ...input, attempt: input.attempt + 1 },
          setupRetryDelayMs(input.attempt),
        );
        return;
      }
      const failed = withFailure(input, error, null);
      ops.report(error);
      await ops.release(failureSummary(failed));
      return;
    }
    if (repositories.length > MAX_SETUP_REPOSITORIES) {
      const error = new Error("github_installation_repositories_limit");
      const failed = withFailure(input, error, null);
      ops.report(error);
      await ops.release(failureSummary(failed));
      return;
    }
    if (repositories.length === 0) {
      await finish(input, ops);
      return;
    }
    await ops.schedule({
      ...input,
      repositories,
      unresolved: false,
      attempt: 0,
    });
    return;
  }

  const [current, ...rest] = input.repositories;
  if (current === undefined) {
    await finish(input, ops);
    return;
  }

  try {
    await ops.processRepository(current);
  } catch (error) {
    const failure = continuationFailure(error, input.attempt);
    if (failure === "retry") {
      await ops.schedule(
        { ...input, attempt: input.attempt + 1 },
        setupRetryDelayMs(input.attempt),
      );
      return;
    }
    const failed = withFailure(input, error, current);
    ops.report(error);
    if (failure === "fatal") {
      await ops.release(failureSummary(failed));
      return;
    }
    await ops.schedule({
      ...failed,
      repositories: rest,
      attempt: 0,
    });
    return;
  }

  if (rest.length === 0) {
    await finish(input, ops);
    return;
  }
  await ops.schedule({
    ...input,
    repositories: rest,
    attempt: 0,
  });
}
