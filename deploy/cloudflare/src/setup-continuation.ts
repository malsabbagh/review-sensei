import { repositorySlug } from "./github-app";

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
}

export interface SetupContinuationOps {
  processRepository(repository: string): Promise<void>;
  resolveRepositories(): Promise<string[]>;
  schedule(next: SetupContinuationRequest): Promise<void>;
  complete(): Promise<void>;
  release(): Promise<void>;
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
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

function isFatalSetupError(error: unknown): boolean {
  const message = errorMessage(error);
  return (
    message.startsWith("PUBLIC_WORKFLOW_TAG") ||
    message.startsWith("PUBLIC_WORKFLOW_REF") ||
    message.startsWith("PUBLIC_WORKFLOW_SHA") ||
    message.startsWith("github_app_") ||
    message.startsWith("github_api_url_") ||
    message.startsWith("github_workflow_tag_invalid") ||
    message === "github_installation_invalid" ||
    message === "github_installation_token_failed_401" ||
    message === "github_installation_token_invalid" ||
    message.startsWith("github_installation_permissions") ||
    message === "GitHub App installation lacks setup permissions" ||
    message === "setup_continuation_invalid" ||
    message === "setup_continuation_unavailable"
  );
}

function isTransientSetupError(error: unknown): boolean {
  if (error instanceof Error && error.name === "GitHubSetupTransientError") {
    return true;
  }
  const message = errorMessage(error);
  return (
    message === "GitHub request failed temporarily" ||
    message === "GitHub request failed" ||
    message.startsWith("github_request_transient") ||
    /^github_installation_token_failed_5\d\d$/.test(message) ||
    message === "github_installation_repositories_failed" ||
    message.startsWith("github_workflow_tag_unavailable") ||
    message === "public workflow tag unavailable"
  );
}

function continuationFailure(error: unknown, attempt: number): "retry" | "fatal" | "skip" {
  if (isFatalSetupError(error)) {
    return "fatal";
  }
  if (isTransientSetupError(error) && attempt + 1 < MAX_SETUP_ATTEMPTS) {
    return "retry";
  }
  return "skip";
}

async function finish(input: SetupContinuationRequest, ops: SetupContinuationOps): Promise<void> {
  if (input.failed) {
    await ops.release();
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
        await ops.schedule({ ...input, attempt: input.attempt + 1 });
        return;
      }
      ops.report(error);
      await ops.release();
      return;
    }
    if (repositories.length > MAX_SETUP_REPOSITORIES) {
      ops.report(new Error("github_installation_repositories_limit"));
      await ops.release();
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
      await ops.schedule({ ...input, attempt: input.attempt + 1 });
      return;
    }
    ops.report(error);
    if (failure === "fatal") {
      await ops.release();
      return;
    }
    await ops.schedule({
      ...input,
      repositories: rest,
      attempt: 0,
      failed: true,
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
