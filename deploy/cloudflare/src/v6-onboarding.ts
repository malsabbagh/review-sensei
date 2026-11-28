import type { WorkerEnv } from "./env";
import type { PublicWorkflowRuntimeShas } from "./github-api";
import type { VerifiedDelivery } from "./github-app";
import type { OidcClaims } from "./oidc";
import { issueV6OnboardingReceipt, sha256Hex, verifyV6OnboardingReceipt } from "./signed-mac";
import { PUBLIC_REPOSITORY } from "./setup-content";
import { parseRepositoryCsv } from "./v6-escrow-crypto";

export const V6_ONBOARDING_TAG = "v6";
export const V6_ONBOARDING_WORKFLOW = ".github/workflows/review-sensei-v6-pilot.yml";
const CONFIG_LIMIT = 4096;
const LEGACY_TOKEN_DRAIN_MS = 60 * 60 * 1000;
const RECEIPT = /^[A-Za-z0-9_-]{43}$/;
const SHA1 = /^[a-f0-9]{40}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const SLUG = /^[a-z0-9-]{1,100}\/[a-z0-9_.-]{1,100}$/;

interface Enrollment {
  installationId: number;
  repositoryId: number;
  repository: string;
  deliveryId: string;
  bodySha256: string;
}

interface SetupActivation {
  receipt: string;
  fencedAt?: number;
}

export interface V6Onboarding {
  setupEnabled: boolean;
  startAt: number;
  tagObject: string;
  sourceSha: string;
  enrollments: readonly Enrollment[];
  activations: ReadonlyMap<number, SetupActivation>;
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function timestamp(value: unknown): number {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(value)) {
    return NaN;
  }
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) && new Date(milliseconds).toISOString().replace(".000Z", "Z") === value
    ? milliseconds
    : NaN;
}

function parseEnrollment(entry: unknown, startAt: number): { enrollment: Enrollment; activation?: SetupActivation } {
  const row = object(entry);
  const hasActivation = row?.activation !== undefined;
  if (row === null || Object.keys(row).length !== (hasActivation ? 6 : 5) ||
      !positiveInteger(row.installation_id) || !positiveInteger(row.repository_id) ||
      typeof row.repository !== "string" || !SLUG.test(row.repository) ||
      typeof row.delivery_id !== "string" || !/^[a-zA-Z0-9-]{1,100}$/.test(row.delivery_id) ||
      typeof row.body_sha256 !== "string" || !SHA256.test(row.body_sha256)) {
    throw new Error("configuration_unavailable");
  }
  const enrollment = {
    installationId: row.installation_id,
    repositoryId: row.repository_id,
    repository: row.repository,
    deliveryId: row.delivery_id,
    bodySha256: row.body_sha256,
  };
  if (!hasActivation) return { enrollment };
  const activation = object(row.activation);
  const hasFenceTime = activation?.fenced_at !== undefined;
  const fencedAt = hasFenceTime ? timestamp(activation?.fenced_at) : undefined;
  if (activation === null || Object.keys(activation).length !== (hasFenceTime ? 2 : 1) ||
      typeof activation.receipt !== "string" || !RECEIPT.test(activation.receipt) ||
      (fencedAt !== undefined && !(fencedAt >= startAt))) {
    throw new Error("configuration_unavailable");
  }
  return { enrollment, activation: { receipt: activation.receipt, fencedAt } };
}

/** An operator attests first-time history. A webhook or installation age alone cannot prove it. */
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
      policy.tag_object === policy.source_sha ||
      !Array.isArray(policy.enrollments) || policy.enrollments.length === 0) {
    throw new Error("configuration_unavailable");
  }
  const entries = policy.enrollments.map((entry) => parseEnrollment(entry, startAt));
  const enrollments = entries.map(({ enrollment }) => enrollment);
  const activations = new Map<number, SetupActivation>();
  for (const { enrollment, activation } of entries) {
    if (activation !== undefined) activations.set(enrollment.repositoryId, activation);
  }
  if (new Set(enrollments.map((entry) => entry.repositoryId)).size !== enrollments.length ||
      new Set(enrollments.map((entry) => entry.repository)).size !== enrollments.length) {
    throw new Error("configuration_unavailable");
  }
  return {
    setupEnabled: setupEnabled === true,
    startAt,
    tagObject: policy.tag_object,
    sourceSha: policy.source_sha,
    enrollments,
    activations,
  };
}

export function onboardingForRepository(policy: V6Onboarding | null, repositoryId: number): Enrollment | undefined {
  return policy?.enrollments.find((entry) => entry.repositoryId === repositoryId);
}

function enrollmentRows(policy: V6Onboarding, delivery: Pick<VerifiedDelivery, "installationId" | "deliveryId">): Enrollment[] {
  return policy.enrollments.filter((entry) =>
    entry.installationId === delivery.installationId && entry.deliveryId === delivery.deliveryId
  ).sort((left, right) => left.repositoryId - right.repositoryId);
}

async function setupProof(policy: V6Onboarding, rows: Enrollment[]): Promise<string> {
  return sha256Hex(JSON.stringify([policy.startAt, policy.tagObject, policy.sourceSha, rows]));
}

/** Call only after the upstream GitHub webhook body MAC has been verified. */
export async function verifyFirstInstallDelivery(
  env: WorkerEnv,
  delivery: VerifiedDelivery,
  body: ArrayBuffer,
): Promise<string | undefined> {
  const policy = readV6Onboarding(env);
  if (!policy?.setupEnabled || delivery.event !== "installation" || delivery.action !== "created" || delivery.suspended) {
    return undefined;
  }
  const rows = enrollmentRows(policy, delivery);
  if (rows.length === 0) return undefined;
  const bodyText = new TextDecoder("utf-8", { fatal: true }).decode(body);
  const payload = object(JSON.parse(bodyText));
  const installation = object(payload?.installation);
  const createdAt = timestamp(installation?.created_at);
  if (installation?.id !== delivery.installationId || !(createdAt >= policy.startAt) ||
      (installation.app_id !== undefined && String(installation.app_id) !== env.GITHUB_APP_ID) ||
      !Array.isArray(payload?.repositories)) return undefined;

  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", body)),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
  const repositories = new Set(payload.repositories.map((value: unknown) => {
    const repo = object(value);
    return positiveInteger(repo?.id) && typeof repo?.full_name === "string"
      ? JSON.stringify([repo.id, repo.full_name.toLowerCase()])
      : null;
  }));
  if (rows.some((row) => row.bodySha256 !== digest ||
      !repositories.has(JSON.stringify([row.repositoryId, row.repository])))) return undefined;
  return setupProof(policy, rows);
}

export async function setupEnrollmentMatches(
  policy: V6Onboarding,
  delivery: VerifiedDelivery,
): Promise<boolean> {
  if (!policy.setupEnabled || delivery.onboardingProof === undefined) return false;
  const rows = enrollmentRows(policy, delivery);
  return rows.length > 0 && delivery.onboardingProof === await setupProof(policy, rows);
}

function receiptMessage(env: WorkerEnv, policy: V6Onboarding, entry: Enrollment): string {
  return JSON.stringify([env.GITHUB_APP_ID, policy.startAt, policy.tagObject, policy.sourceSha, entry]);
}

export function generateOnboardingReceipt(env: WorkerEnv, policy: V6Onboarding, entry: Enrollment): Promise<string> {
  return issueV6OnboardingReceipt(env.REVIEWSENSEI_SIGNING_KEY ?? "", receiptMessage(env, policy, entry));
}

export async function onboardingStage(
  env: WorkerEnv,
  policy: V6Onboarding,
  entry: Enrollment,
  now = Date.now(),
): Promise<"setup" | "draining" | "active"> {
  const activation = policy.activations.get(entry.repositoryId);
  if (activation === undefined) return "setup";
  if (!(await verifyV6OnboardingReceipt(
    env.REVIEWSENSEI_SIGNING_KEY ?? "", receiptMessage(env, policy, entry), activation.receipt,
  ))) throw new Error("configuration_unavailable");
  return activation.fencedAt !== undefined && now >= activation.fencedAt + LEGACY_TOKEN_DRAIN_MS
    ? "active" : "draining";
}

export async function v6OnboardingFencesV5(env: WorkerEnv, repositoryId: number): Promise<boolean> {
  const policy = readV6Onboarding(env);
  if (policy === null) return false;
  const entry = onboardingForRepository(policy, repositoryId);
  return entry !== undefined && await onboardingStage(env, policy, entry) !== "setup";
}

export function onboardingRuntimeMatches(
  policy: Pick<V6Onboarding, "tagObject" | "sourceSha">,
  runtime: PublicWorkflowRuntimeShas,
): boolean {
  return runtime.refSha === policy.tagObject && runtime.commitSha === policy.sourceSha;
}

function onboardingWorkflowMatches(policy: V6Onboarding, entry: Enrollment, claims: OidcClaims): boolean {
  return entry.repository === claims.repository.toLowerCase() &&
    claims.event_name === "pull_request" &&
    claims.workflow_ref.toLowerCase().startsWith(entry.repository + "/.github/workflows/review-sensei-review.yml@") &&
    claims.job_workflow_ref === PUBLIC_REPOSITORY + "/" + V6_ONBOARDING_WORKFLOW + "@refs/tags/" + V6_ONBOARDING_TAG &&
    (claims.job_workflow_sha === policy.sourceSha || claims.job_workflow_sha === policy.tagObject);
}

interface V6EnrollmentAuthorization {
  installationId: number;
  tagObject: string;
  sourceSha: string;
}

/** A known enrollment takes precedence over any older slug-based pilot rule. */
export async function requireV6Admission(
  env: WorkerEnv,
  claims: OidcClaims,
  now = Date.now(),
): Promise<V6EnrollmentAuthorization | null> {
  const policy = readV6Onboarding(env);
  const entry = onboardingForRepository(policy, claims.repository_id);
  if (policy !== null && entry !== undefined) {
    if (!onboardingWorkflowMatches(policy, entry, claims) ||
        await onboardingStage(env, policy, entry, now) !== "active") {
      throw new Error("v6_lease_unauthorized_tuple");
    }
    return { installationId: entry.installationId, tagObject: policy.tagObject, sourceSha: policy.sourceSha };
  }
  const slug = claims.repository.toLowerCase();
  if (policy?.enrollments.some((item) => item.repository === slug) ||
      !parseRepositoryCsv(env.V6_PILOT_REPOSITORIES).has(slug)) {
    throw new Error("v6_lease_unauthorized_tuple");
  }
  return null;
}
