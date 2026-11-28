import type { WorkerEnv } from "./env";
import type { PublicWorkflowRuntimeShas } from "./github-api";
import type { VerifiedDelivery } from "./github-app";
import type { OidcClaims } from "./oidc";
import {
  MAX_SETUP_INSTALL_REPOSITORIES,
  issueV6OnboardingReceipt,
  sha256Hex,
  verifyV6OnboardingReceipt,
  type SetupInstallSelection,
} from "./signed-mac";
import { PUBLIC_REPOSITORY, SETUP_WORKFLOW_PATH } from "./setup-content";
import { parseRepositoryCsv } from "./v6-escrow-crypto";

export const V6_ONBOARDING_TAG = "v6";
export const V6_ONBOARDING_WORKFLOW = ".github/workflows/review-sensei-v6-pilot.yml";
const CONFIG_LIMIT = 4096;
const LEGACY_TOKEN_DRAIN_MS = 60 * 60 * 1000;
const RECEIPT = /^[A-Za-z0-9_-]{43}$/;
const SHA1 = /^[a-f0-9]{40}$/;
const SLUG = /^[a-z0-9-]{1,100}\/[a-z0-9_.-]{1,100}$/;

export interface SetupInstallIdentity {
  installationId: number;
  repositoryId: number;
  repository: string;
}

interface SetupActivation extends SetupInstallIdentity {
  receipt: string;
  fencedAt?: number;
}

export interface V6Onboarding {
  setupEnabled: boolean;
  startAt: number;
  tagObject: string;
  sourceSha: string;
  activations: ReadonlyMap<number, SetupActivation>;
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function timestamp(value: unknown): number {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(value)) return NaN;
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) && new Date(milliseconds).toISOString().replace(".000Z", "Z") === value
    ? milliseconds : NaN;
}

function parseActivation(value: unknown, startAt: number): SetupActivation {
  const row = object(value);
  if (row === null || Object.keys(row).length !== (row.fenced_at === undefined ? 4 : 5) ||
      !positiveInteger(row.installation_id) || !positiveInteger(row.repository_id) ||
      typeof row.repository !== "string" || !SLUG.test(row.repository) ||
      typeof row.receipt !== "string" || !RECEIPT.test(row.receipt)) {
    throw new Error("configuration_unavailable");
  }
  const fencedAt = row.fenced_at === undefined ? undefined : timestamp(row.fenced_at);
  if (fencedAt !== undefined && !(fencedAt >= startAt)) {
    throw new Error("configuration_unavailable");
  }
  return {
    installationId: row.installation_id,
    repositoryId: row.repository_id,
    repository: row.repository,
    receipt: row.receipt,
    fencedAt,
  };
}

/** The policy enables proposals; each activation authorizes a verified writer cutover. */
export function readV6Onboarding(env: WorkerEnv): V6Onboarding | null {
  const configured = env.V6_NEW_INSTALL_ONBOARDING;
  if (configured === undefined) return null;
  let value: unknown;
  try {
    if (configured.length > CONFIG_LIMIT) throw new Error();
    value = JSON.parse(configured);
  } catch {
    throw new Error("configuration_unavailable");
  }
  const policy = object(value);
  const startAt = timestamp(policy?.start_at);
  const setupEnabled = policy?.setup_enabled;
  if (policy === null || Object.keys(policy).length !== (setupEnabled === undefined ? 5 : 6) ||
      (setupEnabled !== undefined && typeof setupEnabled !== "boolean") ||
      !positiveInteger(policy.app_id) || String(policy.app_id) !== env.GITHUB_APP_ID || env.PUBLIC_WORKFLOW_TAG !== "v5" ||
      !["https://api.github.com", "https://api.github.com/"].includes(env.GITHUB_API_URL ?? "https://api.github.com") ||
      !Number.isFinite(startAt) ||
      typeof policy.tag_object !== "string" || !SHA1.test(policy.tag_object) ||
      typeof policy.source_sha !== "string" || !SHA1.test(policy.source_sha) ||
      policy.tag_object === policy.source_sha || !Array.isArray(policy.activations)) {
    throw new Error("configuration_unavailable");
  }
  const activations = new Map<number, SetupActivation>();
  const repositories = new Set<string>();
  for (const row of policy.activations) {
    const activation = parseActivation(row, startAt);
    if (activations.has(activation.repositoryId) || repositories.has(activation.repository)) {
      throw new Error("configuration_unavailable");
    }
    repositories.add(activation.repository);
    activations.set(activation.repositoryId, activation);
  }
  return {
    setupEnabled: setupEnabled === true,
    startAt,
    tagObject: policy.tag_object,
    sourceSha: policy.source_sha,
    activations,
  };
}

function policyIdentity(policy: V6Onboarding): string {
  return JSON.stringify([policy.startAt, policy.tagObject, policy.sourceSha]);
}

/** Called only on a GitHub webhook whose original body MAC has verified. */
export async function attestInstallDelivery(
  env: WorkerEnv,
  delivery: VerifiedDelivery,
  body: ArrayBuffer,
  now = Date.now(),
): Promise<SetupInstallSelection | undefined> {
  const policy = readV6Onboarding(env);
  if (policy === null || delivery.event !== "installation" || delivery.action !== "created" || delivery.suspended) {
    return undefined;
  }
  const payload = object(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)));
  const installation = object(payload?.installation);
  const createdAt = timestamp(installation?.created_at);
  if (!(createdAt <= now) || installation?.id !== delivery.installationId ||
      (installation.app_id !== undefined && String(installation.app_id) !== env.GITHUB_APP_ID)) {
    throw new Error("v6_installation_identity_unavailable");
  }
  if (createdAt < policy.startAt) return undefined;

  const entries = Array.isArray(payload?.repositories) && payload.repositories.length > 0
    ? payload.repositories : [payload?.repository];
  if (entries.length > MAX_SETUP_INSTALL_REPOSITORIES) {
    throw new Error("v6_installation_selection_unavailable");
  }
  const selected = new Set(delivery.repositories.map(repository => repository.toLowerCase()));
  const repositoryIds = new Set<number>();
  for (const entry of entries) {
    const repository = object(entry);
    if (!positiveInteger(repository?.id) || typeof repository.full_name !== "string" ||
        !selected.delete(repository.full_name.toLowerCase()) || repositoryIds.has(repository.id)) {
      throw new Error("v6_installation_selection_unavailable");
    }
    repositoryIds.add(repository.id);
  }
  if (selected.size > 0 || repositoryIds.size === 0) throw new Error("v6_installation_selection_unavailable");
  return {
    createdAt,
    policySha256: await sha256Hex(policyIdentity(policy)),
    repositoryIds: [...repositoryIds].sort((a, b) => a - b),
  };
}

export async function setupSelectionMatches(policy: V6Onboarding, selection: SetupInstallSelection): Promise<boolean> {
  return policy.setupEnabled && selection.createdAt >= policy.startAt &&
    selection.policySha256 === await sha256Hex(policyIdentity(policy));
}

function receiptMessage(env: WorkerEnv, policy: V6Onboarding, identity: SetupInstallIdentity): string {
  return JSON.stringify([env.GITHUB_APP_ID, policyIdentity(policy),
    identity.installationId, identity.repositoryId, identity.repository]);
}

export function generateOnboardingReceipt(env: WorkerEnv, policy: V6Onboarding, identity: SetupInstallIdentity): Promise<string> {
  return issueV6OnboardingReceipt(env.REVIEWSENSEI_SIGNING_KEY ?? "", receiptMessage(env, policy, identity));
}

async function verifyActivation(env: WorkerEnv, policy: V6Onboarding, activation: SetupActivation): Promise<void> {
  if (!(await verifyV6OnboardingReceipt(
    env.REVIEWSENSEI_SIGNING_KEY ?? "", receiptMessage(env, policy, activation), activation.receipt,
  ))) throw new Error("configuration_unavailable");
}

export async function v6OnboardingFencesV5(env: WorkerEnv, repositoryId: number): Promise<boolean> {
  const policy = readV6Onboarding(env);
  const activation = policy?.activations.get(repositoryId);
  if (policy === null || activation === undefined) return false;
  await verifyActivation(env, policy, activation);
  return true;
}

export function onboardingRuntimeMatches(
  policy: Pick<V6Onboarding, "tagObject" | "sourceSha">,
  runtime: PublicWorkflowRuntimeShas,
): boolean {
  return runtime.refSha === policy.tagObject && runtime.commitSha === policy.sourceSha;
}

interface V6EnrollmentAuthorization {
  installationId: number;
  tagObject: string;
  sourceSha: string;
  firstPrAfter: number;
}

/** Bind the enrolled caller to GitHub's ref for the actual PR or default branch. */
export function verifyOnboardingEventRef(claims: OidcClaims, defaultBranch: string | undefined, pr: number): void {
  const usesPrRef = claims.event_name === "pull_request" || claims.event_name === "pull_request_review_comment";
  const ref = usesPrRef ? `refs/pull/${pr}/merge` : defaultBranch && `refs/heads/${defaultBranch}`;
  if (!ref || claims.ref !== ref || !claims.workflow_ref.endsWith("@" + ref)) {
    throw new Error("v6_lease_unauthorized_tuple");
  }
}

/** A configured cutover takes precedence over the older slug-based pilot rule. */
export async function requireV6Admission(
  env: WorkerEnv,
  claims: OidcClaims,
  now = Date.now(),
): Promise<V6EnrollmentAuthorization | null> {
  const policy = readV6Onboarding(env);
  const activation = policy?.activations.get(claims.repository_id);
  const slug = claims.repository.toLowerCase();
  if (policy !== null && activation !== undefined) {
    if (activation.repository !== slug || !["pull_request", "issue_comment", "pull_request_review_comment", "workflow_dispatch"].includes(claims.event_name) ||
        !claims.workflow_ref.toLowerCase().startsWith(slug + "/" + SETUP_WORKFLOW_PATH + "@") ||
        claims.job_workflow_ref !== PUBLIC_REPOSITORY + "/" + V6_ONBOARDING_WORKFLOW + "@refs/tags/" + V6_ONBOARDING_TAG ||
        (claims.job_workflow_sha !== policy.sourceSha && claims.job_workflow_sha !== policy.tagObject)) {
      throw new Error("v6_lease_unauthorized_tuple");
    }
    await verifyActivation(env, policy, activation);
    const firstPrAfter = (activation.fencedAt ?? Infinity) + LEGACY_TOKEN_DRAIN_MS;
    if (!(now >= firstPrAfter)) throw new Error("v6_lease_unauthorized_tuple");
    return { installationId: activation.installationId, tagObject: policy.tagObject, sourceSha: policy.sourceSha, firstPrAfter };
  }
  if ((policy && Array.from(policy.activations.values()).some(item => item.repository === slug)) ||
      !parseRepositoryCsv(env.V6_PILOT_REPOSITORIES).has(slug)) {
    throw new Error("v6_lease_unauthorized_tuple");
  }
  return null;
}
