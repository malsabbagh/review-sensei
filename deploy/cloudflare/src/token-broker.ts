import type { WorkerEnv } from "./env";
import { GitHubApi, type PublicWorkflowRuntimeShas } from "./github-api";
import { type OidcClaims, verifyOidcAssertion } from "./oidc";
import {
  PUBLIC_REPOSITORY,
  PUBLIC_WORKFLOW_PATH,
  brokerAcceptedPublicWorkflowTags,
  publicWorkflowTagFromJobRef,
  validatePublicWorkflowTag,
} from "./setup-content";

const WORKFLOW_PATH = ".github/workflows/review-sensei-run.yml";
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const CAPABILITIES = {
  review_publish: { "pull_requests": "write" },
  // Session authority is separate from publication authority, but it asks for
  // no extra GitHub permission: the durable session is an issue comment, so
  // pull-request write is sufficient. ADR 0022/0006 register the App without
  // Checks: write, and this broker never requests it.
  review_session: { "pull_requests": "write" },
  review_status: { "pull_requests": "read" },
  inline_reply: { "pull_requests": "write" },
  issue_reply: { "pull_requests": "write" },
  learning_write: { contents: "write", "pull_requests": "write" },
} as const;
const REPOSITORY_METADATA_PERMISSIONS = { metadata: "read" } as const;
const SESSION_ATTESTATION_VERSION = 1;
const SESSION_ATTESTATION_TTL_MS = 10 * 60 * 1000;
const SESSION_ATTESTATION_SKEW_MS = 30 * 1000;
const SESSION_GRANT_AUDIENCE = "reviewsensei-session-ledger";
const SHA_PATTERN = /^[a-f0-9]{40}$/;
const RUN_ID_PATTERN = /^[1-9][0-9]{0,18}$/;
const ASSOCIATIONS = new Set(["OWNER", "MEMBER", "COLLABORATOR"]);
const MAX_COMMAND_REASON_BYTES = 512;
// Keep this command grammar in lockstep with Python's parser. The explicit
// ASCII set rejects control and Unicode separators instead of letting the two
// runtimes disagree about whether a mention is command-shaped.
const COMMAND_WHITESPACE = "[ \\t\\r\\n]";
const COMMAND_WHITESPACE_RUN = `${COMMAND_WHITESPACE}+`;
const COMMAND_TRIM = /^[ \t\r\n]+|[ \t\r\n]+$/g;
const RECOGNIZED_COMMAND = new RegExp(
  `(?:^|${COMMAND_WHITESPACE})@sensei${COMMAND_WHITESPACE_RUN}([\\s\\S]*?)${COMMAND_WHITESPACE}*$`,
);
const RECOGNIZED_FINDING = new RegExp(
  `^(?:dismiss|defer|accept-risk)${COMMAND_WHITESPACE_RUN}[a-f0-9]{16,64}${COMMAND_WHITESPACE_RUN}--reason${COMMAND_WHITESPACE_RUN}([^ \\t\\r\\n][\\s\\S]*)$`,
  "i",
);
const RECOGNIZED_REVIEW_STATUS = new RegExp(
  `^review${COMMAND_WHITESPACE_RUN}(?:status|pause)${COMMAND_WHITESPACE}*$`,
  "i",
);
const RECOGNIZED_REENROLL = new RegExp(
  `^review${COMMAND_WHITESPACE_RUN}reenroll${COMMAND_WHITESPACE}*$`,
  "i",
);
const RECOGNIZED_VERIFY = new RegExp(`^verify${COMMAND_WHITESPACE}*$`, "i");
const RECOGNIZED_CONTINUE = new RegExp(
  `^review${COMMAND_WHITESPACE_RUN}continue(?:${COMMAND_WHITESPACE_RUN}--rounds${COMMAND_WHITESPACE_RUN}(?:0|1))?${COMMAND_WHITESPACE}*$`,
  "i",
);

type Capability = keyof typeof CAPABILITIES;

// GitHub may return contents:read alongside review publication's requested
// pull-request write scope so the token can read repository data for the
// review. This is a narrow acceptance rule for GitHub's returned scope, not a
// broker request for an additional permission; reply capabilities reject it.
// This policy overlay is keyed by Capability (the keys of CAPABILITIES above),
// so a new capability must explicitly opt in here before it can accept it.
const CAPABILITIES_ACCEPTING_RETURNED_CONTENTS_READ = new Set<Capability>(["review_publish"]);

interface BrokerBody {
  oidc_token?: unknown;
  capability?: unknown;
  session?: unknown;
  session_attestation?: unknown;
}

interface SessionScope {
  repository_id: number;
  pull_request: number;
  head_sha: string;
}

interface SessionAttestationRequest {
  version: 1;
  repository: string;
  repository_id: number;
  pull_request: number;
  head_sha: string;
  operation: "review" | "command";
  source_comment_id: number | null;
  run_id: string;
  issued_at: number;
  concurrency_group: string;
  job_workflow_ref: string;
  job_workflow_sha: string;
}

interface SessionAttestation extends SessionAttestationRequest {
  actor: string | null;
  actor_type: string | null;
  association: string | null;
  command_id: number | null;
  command_digest: string | null;
}

interface SessionGrant {
  grant: string;
  attestation: SessionAttestation;
}

const SESSION_ATTESTATION_REQUEST_KEYS = [
  "version",
  "repository",
  "repository_id",
  "pull_request",
  "head_sha",
  "operation",
  "source_comment_id",
  "run_id",
  "issued_at",
  "concurrency_group",
  "job_workflow_ref",
  "job_workflow_sha",
] as const;
const SESSION_ATTESTATION_GRANT_KEYS = [
  ...SESSION_ATTESTATION_REQUEST_KEYS,
  "actor",
  "actor_type",
  "association",
  "command_id",
  "command_digest",
] as const;

type SessionState = "enrolled" | "known";

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => Object.hasOwn(value, key));
}

function capability(value: unknown): Capability {
  if (value === undefined) {
    return "review_publish";
  }
  // `in` also accepts names inherited from Object.prototype (for example
  // `constructor` and `toString`).  Capability names are a closed protocol;
  // never let a prototype property become a broker capability.
  if (typeof value !== "string" || !Object.hasOwn(CAPABILITIES, value)) {
    throw new Error("broker_capability_invalid");
  }
  return value as Capability;
}

function workflowRef(repository: string, tag: string): string {
  return `${repository}/${WORKFLOW_PATH}@refs/tags/${tag}`;
}

function authorizeObservedWorkflowTag(
  configuredTag: string,
  jobWorkflowRef: string,
): string {
  const observedTag = publicWorkflowTagFromJobRef(jobWorkflowRef);
  if (
    observedTag === null ||
    !brokerAcceptedPublicWorkflowTags(configuredTag).includes(observedTag)
  ) {
    throw new Error("broker_workflow_rejected");
  }
  return observedTag;
}

function authorizeWorkflowRuntimeSha(
  jobWorkflowSha: string,
  runtime: PublicWorkflowRuntimeShas,
): void {
  if (jobWorkflowSha !== runtime.commitSha && jobWorkflowSha !== runtime.refSha) {
    throw new Error("broker_workflow_rejected");
  }
}

async function claimLedger(
  env: WorkerEnv,
  action: "claim" | "admit",
  jti: string,
  scope: string,
): Promise<"accepted" | "replay" | "rate_limited"> {
  const id = env.BROKER_LEDGER.idFromName("reviewsensei-broker");
  const stub = env.BROKER_LEDGER.get(id);
  const response = await stub.fetch("https://broker/claim", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, jti, scope }),
  });
  if (!response.ok) {
    throw new Error("broker_ledger_unavailable");
  }
  const value = (await response.json()) as { state?: unknown };
  if (value.state === "accepted" || value.state === "replay" || value.state === "rate_limited") {
    return value.state;
  }
  throw new Error("broker_ledger_invalid");
}

async function enrollSession(
  env: WorkerEnv,
  scope: string,
): Promise<SessionState> {
  const id = env.BROKER_LEDGER.idFromName("reviewsensei-broker");
  const stub = env.BROKER_LEDGER.get(id);
  const response = await stub.fetch("https://broker/session", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ action: "session_enroll", scope }),
  });
  if (!response.ok) {
    throw new Error("broker_ledger_unavailable");
  }
  const value = (await response.json()) as { state?: unknown };
  if (value.state === "enrolled" || value.state === "known") {
    return value.state;
  }
  throw new Error("broker_ledger_invalid");
}

async function sessionGrantLedger(
  env: WorkerEnv,
  action: "session_issue" | "session_verify",
  values: {
    grant: string;
    scope: string;
    attestationDigest: string;
    runId?: string;
  },
): Promise<"issued" | "verified" | "replay" | "invalid"> {
  const id = env.BROKER_LEDGER.idFromName("reviewsensei-broker");
  const stub = env.BROKER_LEDGER.get(id);
  const response = await stub.fetch("https://broker/session-grant", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      action,
      grant: values.grant,
      scope: values.scope,
      attestation_digest: values.attestationDigest,
      audience: SESSION_GRANT_AUDIENCE,
      ...(action === "session_issue" ? { run_id: values.runId } : {}),
    }),
  });
  if (!response.ok) {
    throw new Error("broker_ledger_unavailable");
  }
  const value = (await response.json()) as { state?: unknown };
  if (
    value.state === "issued" ||
    value.state === "verified" ||
    value.state === "replay" ||
    value.state === "invalid"
  ) {
    return value.state;
  }
  throw new Error("broker_ledger_invalid");
}

async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(bytes)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

function clientScope(value: string | undefined): string {
  return value && /^[0-9A-Fa-f:.]{1,64}$/.test(value) ? value : "unknown";
}

function sessionScope(value: unknown, repositoryId: number): SessionScope {
  if (
    !isObject(value) ||
    !Number.isSafeInteger(repositoryId) ||
    repositoryId <= 0
  ) {
    throw new Error("broker_session_invalid");
  }
  const pullRequest = value.pull_request;
  const headSha = value.head_sha;
  if (
    value.repository_id !== repositoryId ||
    typeof pullRequest !== "number" ||
    !Number.isSafeInteger(pullRequest) ||
    pullRequest <= 0 ||
    typeof headSha !== "string" ||
    !SHA_PATTERN.test(headSha)
  ) {
    throw new Error("broker_session_invalid");
  }
  return {
    repository_id: repositoryId,
    pull_request: pullRequest,
    head_sha: headSha,
  };
}

function sessionAttestation(
  value: unknown,
  claims: OidcClaims,
  scope: SessionScope,
): SessionAttestationRequest {
  if (!isObject(value) || !hasExactKeys(value, SESSION_ATTESTATION_REQUEST_KEYS)) {
    throw new Error("broker_session_attestation_invalid");
  }
  const attestation = value as Record<string, unknown>;
  const operation = attestation.operation;
  const sourceCommentId = attestation.source_comment_id;
  const issuedAt = attestation.issued_at;
  const now = Date.now();
  const expectedGroup = `reviewsensei-session-${scope.repository_id}-${scope.pull_request}`;
  if (
    attestation.version !== SESSION_ATTESTATION_VERSION ||
    attestation.repository !== claims.repository ||
    attestation.repository_id !== scope.repository_id ||
    attestation.pull_request !== scope.pull_request ||
    attestation.head_sha !== scope.head_sha ||
    (operation !== "review" && operation !== "command") ||
    (sourceCommentId !== null &&
      (typeof sourceCommentId !== "number" ||
        !Number.isSafeInteger(sourceCommentId) || sourceCommentId <= 0)) ||
    (operation === "command" && sourceCommentId === null) ||
    typeof attestation.run_id !== "string" ||
    !RUN_ID_PATTERN.test(attestation.run_id) ||
    attestation.run_id !== claims.run_id ||
    typeof issuedAt !== "number" ||
    !Number.isSafeInteger(issuedAt) ||
    issuedAt * 1000 > now + SESSION_ATTESTATION_SKEW_MS ||
    now - issuedAt * 1000 > SESSION_ATTESTATION_TTL_MS + SESSION_ATTESTATION_SKEW_MS ||
    attestation.concurrency_group !== expectedGroup ||
    attestation.job_workflow_ref !== claims.job_workflow_ref ||
    attestation.job_workflow_sha !== claims.job_workflow_sha
  ) {
    throw new Error("broker_session_attestation_invalid");
  }
  return {
    version: SESSION_ATTESTATION_VERSION,
    repository: claims.repository,
    repository_id: scope.repository_id,
    pull_request: scope.pull_request,
    head_sha: scope.head_sha,
    operation,
    source_comment_id: sourceCommentId as number | null,
    run_id: claims.run_id,
    issued_at: issuedAt as number,
    concurrency_group: expectedGroup,
    job_workflow_ref: claims.job_workflow_ref,
    job_workflow_sha: claims.job_workflow_sha,
  };
}

async function authorizeLiveSessionActor(
  github: GitHubApi,
  repository: string,
  scope: SessionScope,
  claims: OidcClaims,
  attestation: SessionAttestationRequest,
  token: string,
): Promise<SessionAttestation> {
  if (attestation.operation === "review") {
    return {
      ...attestation,
      actor: null,
      actor_type: null,
      association: null,
      command_id: null,
      command_digest: null,
    };
  }
  const comment = await github.issueComment(
    repository,
    scope.pull_request,
    attestation.source_comment_id as number,
    token,
  );
  if (
    comment === null ||
    comment.login.toLowerCase() !== claims.actor.toLowerCase() ||
    comment.userType.toLowerCase() !== "user" ||
    !ASSOCIATIONS.has(comment.association.toUpperCase()) ||
    !recognizedCommand(comment.body)
  ) {
    throw new Error("broker_session_actor_rejected");
  }
  return {
    ...attestation,
    actor: comment.login,
    actor_type: comment.userType,
    association: comment.association.toUpperCase(),
    command_id: comment.id,
    command_digest: await digest(comment.body),
  };
}

function canonicalAttestation(value: SessionAttestation): string {
  return JSON.stringify(value);
}

function sessionAttestationForVerification(
  value: Record<string, unknown>,
  scope: SessionScope,
  runtime: PublicWorkflowRuntimeShas,
): SessionAttestation {
  const operation = value.operation;
  const sourceCommentId = value.source_comment_id;
  const issuedAt = value.issued_at;
  const now = Date.now();
  if (typeof value.job_workflow_ref !== "string") {
    throw new Error("broker_session_attestation_invalid");
  }
  if (
    value.version !== SESSION_ATTESTATION_VERSION ||
    typeof value.repository !== "string" ||
    !REPOSITORY_PATTERN.test(value.repository) ||
    value.repository_id !== scope.repository_id ||
    value.pull_request !== scope.pull_request ||
    value.head_sha !== scope.head_sha ||
    (operation !== "review" && operation !== "command") ||
    (sourceCommentId !== null &&
      (typeof sourceCommentId !== "number" ||
        !Number.isSafeInteger(sourceCommentId) || sourceCommentId <= 0)) ||
    (operation === "command" && sourceCommentId === null) ||
    typeof value.run_id !== "string" ||
    !RUN_ID_PATTERN.test(value.run_id) ||
    typeof issuedAt !== "number" ||
    !Number.isSafeInteger(issuedAt) ||
    issuedAt * 1000 > now + SESSION_ATTESTATION_SKEW_MS ||
    now - issuedAt * 1000 > SESSION_ATTESTATION_TTL_MS + SESSION_ATTESTATION_SKEW_MS ||
    typeof value.concurrency_group !== "string" ||
    value.concurrency_group !== `reviewsensei-session-${scope.repository_id}-${scope.pull_request}` ||
    typeof value.job_workflow_sha !== "string" ||
    !SHA_PATTERN.test(value.job_workflow_sha) ||
    (operation === "review" &&
      (value.actor !== null ||
        value.actor_type !== null ||
        value.association !== null ||
        value.command_id !== null ||
        value.command_digest !== null)) ||
    (operation === "command" &&
      (typeof value.actor !== "string" ||
        value.actor.length === 0 ||
        typeof value.actor_type !== "string" ||
        value.actor_type.toLowerCase() !== "user" ||
        typeof value.association !== "string" ||
        !ASSOCIATIONS.has(value.association) ||
        typeof value.command_id !== "number" ||
        !Number.isSafeInteger(value.command_id) ||
        value.command_id <= 0 ||
        typeof value.command_digest !== "string" ||
        !/^[a-f0-9]{64}$/.test(value.command_digest)))
  ) {
    throw new Error("broker_session_attestation_invalid");
  }
  authorizeWorkflowRuntimeSha(value.job_workflow_sha as string, runtime);
  return {
    version: SESSION_ATTESTATION_VERSION,
    repository: value.repository,
    repository_id: scope.repository_id,
    pull_request: scope.pull_request,
    head_sha: scope.head_sha,
    operation,
    source_comment_id: sourceCommentId as number | null,
    run_id: value.run_id,
    issued_at: issuedAt as number,
    concurrency_group: value.concurrency_group,
    job_workflow_ref: value.job_workflow_ref,
    job_workflow_sha: value.job_workflow_sha,
    actor: value.actor as string | null,
    actor_type: value.actor_type as string | null,
    association: value.association as string | null,
    command_id: value.command_id as number | null,
    command_digest: value.command_digest as string | null,
  };
}

function newSessionGrant(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function recognizedCommand(value: string): boolean {
  const match = RECOGNIZED_COMMAND.exec(value);
  if (match === null) return false;
  const command = match[1];
  const finding = RECOGNIZED_FINDING.exec(command);
  return (
    RECOGNIZED_REVIEW_STATUS.test(command) ||
    RECOGNIZED_REENROLL.test(command) ||
    RECOGNIZED_VERIFY.test(command) ||
    RECOGNIZED_CONTINUE.test(command) ||
    (finding !== null && validCommandReason(finding[1]))
  );
}

function validCommandReason(value: string): boolean {
  const reason = value.replace(COMMAND_TRIM, "").replace(/^["']+|["']+$/g, "").replace(COMMAND_TRIM, "");
  if (reason.length === 0 || new TextEncoder().encode(reason).byteLength > MAX_COMMAND_REASON_BYTES) {
    return false;
  }
  for (const character of reason) {
    // Match Python str.isprintable(): ASCII space is allowed, while control,
    // format, surrogate, private-use, unassigned, and separator characters
    // are not valid durable command reasons.
    if (character !== " " && /[\p{C}\p{Z}]/u.test(character)) {
      return false;
    }
  }
  return true;
}

/** Issuance-only broker policy. It never accepts installation ids from a job. */
export class TokenBroker {
  private readonly github: GitHubApi;

  constructor(private readonly env: WorkerEnv, github?: GitHubApi) {
    this.github = github ?? new GitHubApi(env);
  }

  async exchange(
    body: BrokerBody,
    sourceAddress?: string,
  ): Promise<{
    token: string;
    capability: Capability;
    session_state?: SessionState;
    session_grant?: string;
    session_attestation?: SessionAttestation;
  }> {
    if (!isObject(body) || typeof body.oidc_token !== "string" || body.oidc_token.length === 0) {
      throw new Error("broker_request_invalid");
    }
    const requested = capability(body.capability);
    if (requested !== "review_session" && body.session !== undefined) {
      throw new Error("broker_session_invalid");
    }
    const admissionState = await claimLedger(
      this.env,
      "admit",
      `preauth:${await digest(body.oidc_token)}:${requested}`,
      `preauth:${clientScope(sourceAddress)}`,
    );
    if (admissionState === "rate_limited") {
      throw new Error("broker_rate_limited");
    }
    const claims = await verifyOidcAssertion(body.oidc_token, {
      audience: "sts.reviewsensei.dev",
      issuer: "https://token.actions.githubusercontent.com",
    });
    const configuredTag = validatePublicWorkflowTag(this.env.PUBLIC_WORKFLOW_TAG ?? "");
    const observedTag = authorizeObservedWorkflowTag(
      configuredTag,
      claims.job_workflow_ref,
    );
    const runtime = await this.github.publicWorkflowRuntimeShas(observedTag);
    this.authorizeClaims(claims, observedTag, runtime);
    // Reject replay/rate abuse immediately after cryptographic and local
    // policy validation, before consuming shared GitHub App API capacity.
    const ledgerState = await claimLedger(
      this.env,
      "claim",
      `${claims.jti}:${requested}`,
      `${claims.repository_id}:${claims.actor_id}:${claims.job_workflow_sha}:${requested}`,
    );
    if (ledgerState === "replay") {
      throw new Error("broker_replay");
    }
    if (ledgerState === "rate_limited") {
      throw new Error("broker_rate_limited");
    }
    const installationId = await this.github.installationFor(claims.repository);
    if (installationId === null) {
      throw new Error("broker_installation_unavailable");
    }
    // The repository endpoint requires an installation (or user) access
    // token for private repositories. App JWTs are accepted for the
    // installation lookup above, but not for repository metadata.
    const metadataToken = await this.github.installationToken(
      installationId,
      claims.repository,
      REPOSITORY_METADATA_PERMISSIONS,
    );
    const info = await this.github.repositoryInfo(
      claims.repository,
      metadataToken.token,
    );
    if (info === null || info.fork || info.id !== claims.repository_id) {
      throw new Error("broker_repository_rejected");
    }
    const requestedSession = requested === "review_session"
      ? sessionScope(body.session, claims.repository_id)
      : undefined;
    const token = await this.github.capabilityToken(
      installationId,
      claims.repository,
      CAPABILITIES[requested],
      CAPABILITIES_ACCEPTING_RETURNED_CONTENTS_READ.has(requested),
    );
    if (requestedSession !== undefined) {
      const currentHead = await this.github.pullRequestHead(
        claims.repository,
        requestedSession.pull_request,
        token,
      );
      if (currentHead !== requestedSession.head_sha) {
        throw new Error("broker_session_head_rejected");
      }
      // The enrollment witness is keyed by the current head, which the check
      // above just verified against the live pull request. A new head is a new
      // enrollment, so a legitimate re-review at an advanced head cannot be
      // mistaken for a deleted marker; the same head keeps its witness, which
      // is what makes a missing comment detectable as deletion.
      const requestedAttestation = body.session_attestation === undefined
        ? undefined
        : sessionAttestation(body.session_attestation, claims, requestedSession);
      const attestation = requestedAttestation === undefined
        ? undefined
        : await authorizeLiveSessionActor(
          this.github,
          claims.repository,
          requestedSession,
          claims,
          requestedAttestation,
          token,
        );
      const enrollment = await enrollSession(
        this.env,
        `${requestedSession.repository_id}:${requestedSession.pull_request}:${requestedSession.head_sha}`,
      );
      if (attestation !== undefined) {
        const grant = newSessionGrant();
        const state = await sessionGrantLedger(this.env, "session_issue", {
          grant,
          scope: `${requestedSession.repository_id}:${requestedSession.pull_request}:${requestedSession.head_sha}`,
          attestationDigest: await digest(canonicalAttestation(attestation)),
          runId: attestation.run_id,
        });
        if (state !== "issued") {
          throw new Error("broker_session_grant_rejected");
        }
        return {
          token,
          capability: requested,
          session_state: enrollment,
          session_grant: grant,
          session_attestation: attestation,
        };
      }
      return { token, capability: requested, session_state: enrollment };
    }
    return { token, capability: requested };
  }

  /** Verify an opaque broker grant before a hosted session-ledger mutation. */
  async verifySessionGrant(
    grant: unknown,
    attestation: unknown,
  ): Promise<SessionAttestation> {
    if (typeof grant !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(grant)) {
      throw new Error("broker_session_grant_invalid");
    }
    if (!isObject(attestation) || !hasExactKeys(attestation, SESSION_ATTESTATION_GRANT_KEYS)) {
      throw new Error("broker_session_attestation_invalid");
    }
    const value = attestation as Record<string, unknown>;
    const repositoryId = value.repository_id;
    if (
      typeof repositoryId !== "number" ||
      !Number.isSafeInteger(repositoryId) ||
      repositoryId <= 0
    ) {
      throw new Error("broker_session_attestation_invalid");
    }
    const scope = sessionScope(value, repositoryId);
    const configuredTag = validatePublicWorkflowTag(this.env.PUBLIC_WORKFLOW_TAG ?? "");
    if (typeof value.job_workflow_ref !== "string") {
      throw new Error("broker_session_attestation_invalid");
    }
    const observedTag = authorizeObservedWorkflowTag(configuredTag, value.job_workflow_ref);
    const runtime = await this.github.publicWorkflowRuntimeShas(observedTag);
    const parsed = sessionAttestationForVerification(value, scope, runtime);
    const state = await sessionGrantLedger(this.env, "session_verify", {
      grant,
      scope: `${scope.repository_id}:${scope.pull_request}:${scope.head_sha}`,
      attestationDigest: await digest(canonicalAttestation(parsed)),
    });
    if (state !== "verified") {
      throw new Error("broker_session_grant_invalid");
    }
    return parsed;
  }

  private authorizeClaims(
    claims: OidcClaims,
    publicWorkflowTag: string,
    runtime: PublicWorkflowRuntimeShas,
  ): void {
    if (!REPOSITORY_PATTERN.test(claims.repository)) {
      throw new Error("broker_repository_rejected");
    }
    // Resolve the observed public tag above, then require both the canonical
    // tag ref and a runtime SHA bound to that tag. GitHub Actions emits the
    // peeled commit for lightweight tags and the tag object SHA for annotated
    // tags; accept either when it matches the resolved tag.
    if (claims.job_workflow_ref !== workflowRef(PUBLIC_REPOSITORY, publicWorkflowTag)) {
      throw new Error("broker_workflow_rejected");
    }
    authorizeWorkflowRuntimeSha(claims.job_workflow_sha, runtime);
    if (
      !["pull_request", "workflow_dispatch", "issue_comment", "pull_request_review_comment"].includes(
        claims.event_name,
      )
    ) {
      throw new Error("broker_event_rejected");
    }
    if (!["github-hosted", "self-hosted"].includes(claims.runner_environment)) {
      throw new Error("broker_runner_rejected");
    }
    if (claims.repository_owner !== claims.repository.split("/", 1)[0]) {
      throw new Error("broker_repository_rejected");
    }
  }
}

export type {
  BrokerBody,
  Capability,
  SessionAttestation,
  SessionGrant,
  SessionScope,
  SessionState,
};
