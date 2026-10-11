import type { WorkerEnv, WorkerExecution } from "./env";
import { GitHubApi } from "./github-api";
import { hasSetupPermissions, processDelivery, type VerifiedDelivery } from "./github-app";
import { sha256Hex, signSetupCursor, verifySetupCursor, type SetupCursorClaims } from "./signed-mac";
import { continuationErrorCode } from "./setup-continuation";

export const SETUP_CONTINUE_URL = "https://setup.internal/github/setup-continue";
const PAGE_SIZE = 100;

const FATAL_SETUP_CODES = new Set([
  "github_app_key_invalid",
  "github_app_id_invalid",
  "github_api_url_invalid",
  "github_installation_invalid",
  "github_installation_permissions_missing",
  "github_installation_permissions_invalid",
  "github_installation_repositories_invalid",
  "github_installation_repositories_limit",
  "public_workflow_tag_invalid",
  "public_workflow_ref_invalid",
  "public_workflow_sha_invalid",
  "configuration_unavailable",
]);

export interface SetupStepDeps {
  github?: GitHubApi;
  now?: number;
  processRepository?: (repository: string, permissions: Record<string, string>) => Promise<void>;
}

function appId(value: string | undefined): number | null {
  if (!value || !/^[1-9][0-9]{0,18}$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

export async function listingDigest(
  permissions: Record<string, string>,
  repositories: string[],
): Promise<string> {
  const permissionText = Object.keys(permissions)
    .sort()
    .map((name) => `${name}=${permissions[name]}`)
    .join("\n");
  return sha256Hex(`${permissionText}\n--\n${repositories.join("\n")}`);
}

function logSetupFailure(deliveryId: string, errorCode: string, failureCount: number): void {
  console.error("github_setup_failed", {
    delivery_id: deliveryId,
    error_code: errorCode,
    failure_count: failureCount,
  });
}

export function scheduleSetupContinue(
  env: WorkerEnv,
  ctx: WorkerExecution,
  cursor: string,
): void {
  if (env.SELF === undefined) {
    throw new Error("configuration_unavailable");
  }
  ctx.waitUntil(env.SELF.fetch(SETUP_CONTINUE_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ cursor }),
  }));
}

function nextPosition(page: number, offset: number): { page: number; offset: number } {
  if (offset + 1 < PAGE_SIZE) {
    return { page, offset: offset + 1 };
  }
  return { page: page + 1, offset: 0 };
}

/**
 * One setup repository, then a signed self-call for the next.
 * The cursor carries no repository name and no permission map.
 */
export async function runSignedSetupStep(
  env: WorkerEnv,
  cursorToken: string,
  ctx: WorkerExecution,
  deps: SetupStepDeps = {},
): Promise<"continued" | "complete" | "stopped"> {
  const secret = env.REVIEWSENSEI_SIGNING_KEY ?? "";
  const now = deps.now ?? Date.now();
  const claims = await verifySetupCursor(secret, cursorToken, now);
  if (claims === null) {
    throw new Error("setup_continuation_invalid");
  }
  const github = deps.github ?? new GitHubApi(env);
  let view: { permissions: Record<string, string>; repositories: string[] };
  try {
    view = await github.installationSetupView(claims.installationId);
  } catch (error) {
    const failureCount = claims.failureCount + 1;
    const code = continuationErrorCode(error);
    logSetupFailure(claims.deliveryId, code, failureCount);
    return "stopped";
  }
  if (!hasSetupPermissions(view.permissions)) {
    const failureCount = claims.failureCount + 1;
    logSetupFailure(claims.deliveryId, "github_installation_permissions_missing", failureCount);
    return "stopped";
  }
  const digest = await listingDigest(view.permissions, view.repositories);
  let page = claims.page;
  let offset = claims.offset;
  if (claims.listingSha256 !== "" && claims.listingSha256 !== digest) {
    page = 1;
    offset = 0;
  }
  const index = (page - 1) * PAGE_SIZE + offset;
  if (index >= view.repositories.length) {
    return "complete";
  }
  const repository = view.repositories[index];
  if (repository === undefined) {
    return "complete";
  }
  try {
    if (deps.processRepository !== undefined) {
      await deps.processRepository(repository, view.permissions);
    } else {
      await processOneRepository(env, claims, repository, view.permissions);
    }
  } catch (error) {
    const failureCount = claims.failureCount + 1;
    const code = continuationErrorCode(error);
    logSetupFailure(claims.deliveryId, code, failureCount);
    if (FATAL_SETUP_CODES.has(code)) {
      return "stopped";
    }
    await scheduleNext(env, ctx, claims, view, digest, page, offset, failureCount, now);
    return "continued";
  }
  const following = index + 1;
  if (following >= view.repositories.length) {
    return "complete";
  }
  await scheduleNext(env, ctx, claims, view, digest, page, offset, claims.failureCount, now);
  return "continued";
}

async function scheduleNext(
  env: WorkerEnv,
  ctx: WorkerExecution,
  claims: SetupCursorClaims,
  view: { repositories: string[] },
  digest: string,
  page: number,
  offset: number,
  failureCount: number,
  now: number,
): Promise<void> {
  const position = nextPosition(page, offset);
  const nextIndex = (position.page - 1) * PAGE_SIZE + position.offset;
  if (nextIndex >= view.repositories.length || position.page > 100) {
    return;
  }
  const cursor = await signSetupCursor(env.REVIEWSENSEI_SIGNING_KEY ?? "", {
    installationId: claims.installationId,
    deliveryId: claims.deliveryId,
    page: position.page,
    offset: position.offset,
    expiry: now + 10 * 60 * 1000,
    listingSha256: digest,
    failureCount,
  });
  scheduleSetupContinue(env, ctx, cursor);
}

async function processOneRepository(
  env: WorkerEnv,
  claims: SetupCursorClaims,
  repository: string,
  permissions: Record<string, string>,
): Promise<void> {
  const app = appId(env.GITHUB_APP_ID);
  if (app === null) {
    throw new Error("github_app_id_invalid");
  }
  const delivery: VerifiedDelivery = {
    appId: app,
    event: "installation",
    action: "created",
    installationId: claims.installationId,
    deliveryId: claims.deliveryId,
    repository,
    repositories: [repository],
    suspended: false,
    permissions,
  };
  await processDelivery(delivery, env);
}
