import { authorizeControlledRestart } from "./controlled-restart";
import type { BrokerObservation } from "./broker-observation";
import { BrokerLifetime } from "./broker-lifetime";
import { BrokerDispatchControl, type BrokerControlReceipt } from "./broker-dispatch-control";
import type { WorkerEnv } from "./env";
import { GitHubApi, type PublicWorkflowRuntimeShas } from "./github-api";
import { type OidcClaims, verifyOidcAssertion } from "./oidc";
import { issueSessionGrant, sessionGrantAuthentic } from "./signed-mac";
import { RUN_AUTHORITY_AUDIENCE, authorizeRunCapability, authorizeRunSource, bindRunAuthority, canonicalRunAuthority, parseRunAuthority, type RunAuthority } from "./run-authority";
import {
  PUBLIC_REPOSITORY,
  brokerAcceptedPublicWorkflowTags,
  publicWorkflowTagFromJobRef,
  validatePublicWorkflowTag,
} from "./setup-content";

const WORKFLOW_PATH = ".github/workflows/review-sensei-run.yml";
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const CAPABILITIES = {
  review_publish: { "pull_requests": "write" },
  // GitHub thread resolution also needs repository write authority. Keep it
  // separate from ordinary review publication. Admission below derives the
  // installation and single repository from verified OIDC identity, never a
  // repository allowlist or caller-supplied installation identifier.
  thread_resolve: { contents: "write", "pull_requests": "write" },
  // The merge gate is a check run, so enforcing github.reviews: blocking (or
  // the default auto-approve mode) needs Checks: write. It is a separate
  // capability from review publication so a broker that has not been granted
  // it still publishes reviews and withholds approval instead.
  check_publish: { checks: "write" },
  // Session authority is separate from publication authority, but it asks for
  // no extra GitHub permission: the durable session is an issue comment, so
  // pull-request write is sufficient. ADR 0022/0006 register the App without
  // Checks: write, and only the check_publish capability above requests it.
  review_session: { "pull_requests": "write" },
  review_status: { "pull_requests": "read" },
  // Recovery proof reads cannot mutate reviews, checks, contents or sessions.
  review_actions: { actions: "read" },
  inline_reply: { "pull_requests": "write" },
  issue_reply: { "pull_requests": "write" },
  learning_write: { contents: "write", "pull_requests": "write" },
} as const;
const REPOSITORY_METADATA_PERMISSIONS = { metadata: "read" } as const;
const SESSION_GRANT_AUDIENCE = RUN_AUTHORITY_AUDIENCE;
const SHA_PATTERN = /^[a-f0-9]{40}$/;
const MAX_COMMAND_REASON_BYTES = 512;
const MAX_COMMAND_BODY_BYTES = 4096;
// Keep this command grammar in lockstep with Python's parser. The explicit
// ASCII set rejects control and Unicode separators instead of letting the two
// runtimes disagree about whether a mention is command-shaped.
const COMMAND_WHITESPACE = "[ \\t\\r\\n]";
const COMMAND_WHITESPACE_RUN = `${COMMAND_WHITESPACE}+`;
const COMMAND_HORIZONTAL_WHITESPACE = "[ \\t]";
const COMMAND_HORIZONTAL_RUN = `${COMMAND_HORIZONTAL_WHITESPACE}+`;
const COMMAND_TRIM = /^[ \t\r\n]+|[ \t\r\n]+$/g;
// Equivalent to Python's scoped \A(?:@sensei|(?ai:@reviewsensei)): canonical
// ASCII case variants qualify at the very start of the comment body (index 0),
// Unicode lookalikes do not, the legacy gate stays lowercase, and leading
// indentation, blockquotes (`>`), code fences, and inline prose are rejected.
const RECOGNIZED_COMMAND = new RegExp(
  `^@(?:sensei|[rR][eE][vV][iI][eE][wW][sS][eE][nN][sS][eE][iI])${COMMAND_HORIZONTAL_RUN}([^\\r\\n]+?)${COMMAND_WHITESPACE}*$`,
);
const RECOGNIZED_FINDING = new RegExp(
  `^(?:dismiss|defer|accept-risk)${COMMAND_HORIZONTAL_RUN}[a-f0-9]{16,64}${COMMAND_HORIZONTAL_RUN}--reason${COMMAND_HORIZONTAL_RUN}([^ \\t\\r\\n][^\\r\\n]*)$`,
  "i",
);
const RECOGNIZED_COVERAGE_UNCERTAINTY = new RegExp(
  `^(?:review${COMMAND_HORIZONTAL_RUN})?accept-coverage-uncertainty${COMMAND_HORIZONTAL_RUN}--reason${COMMAND_HORIZONTAL_RUN}([^ \\t\\r\\n][^\\r\\n]*)$`,
  "i",
);
const RECOGNIZED_REVIEW_STATUS = new RegExp(
  `^review${COMMAND_HORIZONTAL_RUN}(?:status|pause)${COMMAND_WHITESPACE}*$`,
  "i",
);
const RECOGNIZED_REENROLL = new RegExp(
  `^review${COMMAND_HORIZONTAL_RUN}reenroll${COMMAND_WHITESPACE}*$`,
  "i",
);
const RECOGNIZED_VERIFY = new RegExp(`^verify${COMMAND_WHITESPACE}*$`, "i");
const RECOGNIZED_CONTINUE = new RegExp(
  `^review${COMMAND_HORIZONTAL_RUN}continue${COMMAND_WHITESPACE}*$`,
  "i",
);
const RECOGNIZED_MEDIA_APPROVAL = new RegExp(
  `^i${COMMAND_HORIZONTAL_RUN}reviewed${COMMAND_HORIZONTAL_RUN}the${COMMAND_HORIZONTAL_RUN}media${COMMAND_HORIZONTAL_RUN}files${COMMAND_HORIZONTAL_RUN}and${COMMAND_HORIZONTAL_RUN}i${COMMAND_HORIZONTAL_RUN}approve${COMMAND_WHITESPACE}*$`,
  "i",
);
const MAX_OVERRIDE_IDS = 10;
const OVERRIDE_ID = "(?:rs-)?[a-f0-9]{6,64}";
const OVERRIDE_SEP = `(?:${COMMAND_HORIZONTAL_WHITESPACE}*,${COMMAND_HORIZONTAL_WHITESPACE}*|${COMMAND_HORIZONTAL_WHITESPACE}+)`;
const OVERRIDE_LIST = `${OVERRIDE_ID}(?:${OVERRIDE_SEP}${OVERRIDE_ID}){0,${MAX_OVERRIDE_IDS - 1}}`;
const RECOGNIZED_OVERRIDE = new RegExp(
  `^override${COMMAND_HORIZONTAL_RUN}${OVERRIDE_LIST}(?:${COMMAND_HORIZONTAL_RUN}acceptable${COMMAND_HORIZONTAL_RUN}risk)?${COMMAND_WHITESPACE}*$`,
  "i",
);
const RECOGNIZED_ACCEPTABLE_RISK = new RegExp(
  `^${OVERRIDE_LIST}${COMMAND_HORIZONTAL_RUN}(?:is|are)${COMMAND_HORIZONTAL_RUN}acceptable${COMMAND_HORIZONTAL_RUN}risk${COMMAND_WHITESPACE}*$`,
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
  release_only?: unknown;
  control_budget?: unknown;
}

interface SessionScope {
  repository_id: number;
  pull_request: number;
  head_sha: string;
}

type SessionAttestation = RunAuthority;
interface SessionGrant { grant: string; attestation: SessionAttestation; }

type SessionState = "enrolled" | "known";

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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

async function enrollSession(
  github: GitHubApi,
  repository: string,
  scope: SessionScope,
  token: string,
): Promise<SessionState> {
  const known = await github.sessionAlreadyKnown(
    repository,
    scope.pull_request,
    scope.head_sha,
    token,
  );
  return known ? "known" : "enrolled";
}

function signingKey(env: WorkerEnv): string {
  const secret = env.REVIEWSENSEI_SIGNING_KEY ?? "";
  if (!secret.trim()) {
    throw new Error("configuration_unavailable");
  }
  return secret;
}

function configuredAppId(env: WorkerEnv): number {
  const value = env.GITHUB_APP_ID;
  if (typeof value !== "string" || !/^[1-9][0-9]{0,15}$/.test(value)) {
    throw new Error("configuration_unavailable");
  }
  const appId = Number(value);
  if (!Number.isSafeInteger(appId) || appId <= 0) {
    throw new Error("configuration_unavailable");
  }
  return appId;
}

async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(bytes)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
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

function recognizedCommand(value: string): boolean {
  if (new TextEncoder().encode(value).byteLength > MAX_COMMAND_BODY_BYTES) {
    return false;
  }
  const match = RECOGNIZED_COMMAND.exec(value);
  if (match === null) return false;
  const command = match[1];
  const finding = RECOGNIZED_FINDING.exec(command);
  const coverage = RECOGNIZED_COVERAGE_UNCERTAINTY.exec(command);
  return (
    RECOGNIZED_REVIEW_STATUS.test(command) ||
    RECOGNIZED_REENROLL.test(command) ||
    RECOGNIZED_VERIFY.test(command) ||
    RECOGNIZED_CONTINUE.test(command) ||
    RECOGNIZED_MEDIA_APPROVAL.test(command) ||
    RECOGNIZED_OVERRIDE.test(command) ||
    RECOGNIZED_ACCEPTABLE_RISK.test(command) ||
    (finding !== null && validCommandReason(finding[1])) ||
    (coverage !== null && validCommandReason(coverage[1]))
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

  constructor(private readonly env: WorkerEnv, github?: GitHubApi, private readonly observation?: BrokerObservation) {
    this.github = github ?? new GitHubApi(env);
  }

  async exchange(body: BrokerBody, sourceAddress?: string, deadlineUnixMs?: number) {
    const lifetime = new BrokerLifetime(deadlineUnixMs, this.observation);
    return await lifetime.run(() => this.exchangeWithinLifetime(body, sourceAddress, lifetime));
  }

  private async exchangeWithinLifetime(
    body: BrokerBody,
    _sourceAddress: string | undefined,
    lifetime: BrokerLifetime,
  ): Promise<{
    token: string;
    capability: Capability;
    control_receipt?: BrokerControlReceipt;
    session_state?: SessionState;
    session_grant?: string;
    session_attestation?: SessionAttestation;
    app_id?: number;
    release_only?: true;
    reservation_owner?: { run_id: string; head_sha: string };
  }> {
    if (!isObject(body) || typeof body.oidc_token !== "string" || body.oidc_token.length === 0) {
      throw new Error("broker_request_invalid");
    }
    const control = body.control_budget === undefined ? undefined : new BrokerDispatchControl(body.control_budget);
    lifetime.constrain(control);
    const github = this.github.withRequestLifetime(lifetime, control);
    const receipt = () => control === undefined ? {} : { control_receipt: control.receipt() };
    const requested = capability(body.capability);
    if (Object.hasOwn(body, "app_id") ||
      (isObject(body.session) && Object.hasOwn(body.session, "app_id"))) {
      throw new Error("broker_request_invalid");
    }
    // This identity belongs to the configured App, never an Actions event or
    // caller assertion. Validate it before authentication or token issuance.
    const appId = requested === "review_session" ? configuredAppId(this.env) : undefined;
    if (requested !== "review_session" && body.session !== undefined) {
      throw new Error("broker_session_invalid");
    }
    if (
      body.release_only !== undefined
      && (requested !== "review_session" || body.release_only !== true)
    ) {
      throw new Error("broker_session_invalid");
    }
    const claims = await verifyOidcAssertion(body.oidc_token, {
      audience: "sts.reviewsensei.dev",
      issuer: "https://token.actions.githubusercontent.com",
      dispatchControl: control,
      requestLifetime: lifetime,
    });
    // Reject obsolete or wrong-purpose authority before requesting installation tokens.
    const requestedAuthority = bindRunAuthority(body.session_attestation, claims);
    authorizeRunCapability(requestedAuthority, requested);
    authorizeControlledRestart(requestedAuthority, requested, claims.event_name);
    const configuredTag = validatePublicWorkflowTag(this.env.PUBLIC_WORKFLOW_TAG ?? "");
    const observedTag = authorizeObservedWorkflowTag(
      configuredTag,
      claims.job_workflow_ref,
    );
    const runtime = await github.publicWorkflowRuntimeShas(observedTag);
    this.authorizeClaims(claims, observedTag, runtime);
    const installationId = await github.installationFor(claims.repository);
    if (installationId === null) {
      throw new Error("broker_installation_unavailable");
    }
    // The repository endpoint requires an installation (or user) access
    // token for private repositories. App JWTs are accepted for the
    // installation lookup above, but not for repository metadata.
    const metadataToken = await github.installationToken(
      installationId,
      claims.repository,
      REPOSITORY_METADATA_PERMISSIONS,
    );
    const info = await github.repositoryInfo(
      claims.repository,
      metadataToken.token,
    );
    if (info === null || info.fork || info.id !== claims.repository_id) {
      throw new Error("broker_repository_rejected");
    }
    const requestedSession = requested === "review_session"
      ? sessionScope(body.session, claims.repository_id)
      : undefined;
    if (requestedSession !== undefined && (
      requestedSession.pull_request !== requestedAuthority.pull_request ||
      requestedSession.head_sha !== requestedAuthority.head_sha
    )) throw new Error("broker_session_attestation_invalid");
    const token = await github.capabilityToken(
      installationId, claims.repository, CAPABILITIES[requested],
      CAPABILITIES_ACCEPTING_RETURNED_CONTENTS_READ.has(requested),
    );
    // Actions/check tokens deliberately have no PR scope. Use an independent
    // internal read token for source validation, never broaden the returned token.
    const hasPullScope = Object.hasOwn(CAPABILITIES[requested], "pull_requests");
    const validationToken = hasPullScope ? token : await github.capabilityToken(
      installationId, claims.repository, CAPABILITIES.review_status,
    );
    const currentHead = await github.pullRequestHead(
      claims.repository, requestedAuthority.pull_request, validationToken,
    );
    if (currentHead !== requestedAuthority.head_sha && body.release_only !== true) {
      throw new Error("broker_session_head_rejected");
    }
    const attestation = await authorizeRunSource(
      github, requestedAuthority, claims, validationToken, recognizedCommand,
    );
    if (requestedSession !== undefined) {
      if (body.release_only === true) {
        return {
          ...receipt(), token, capability: requested, release_only: true, app_id: appId,
          reservation_owner: { run_id: claims.run_id, head_sha: requestedSession.head_sha },
        };
      }
      const enrollment = await enrollSession(
        github,
        claims.repository,
        requestedSession,
        token,
      );
      const scope = `${requestedSession.repository_id}:${requestedSession.pull_request}:${requestedSession.head_sha}`;
      const grant = await issueSessionGrant(signingKey(this.env), {
          scope,
          attestationDigest: await digest(canonicalRunAuthority(attestation)),
          audience: SESSION_GRANT_AUDIENCE,
          runId: attestation.run_id,
      });
      return {
          ...receipt(), token,
          capability: requested,
          session_state: enrollment,
          session_grant: grant,
          session_attestation: attestation,
          app_id: appId,
          reservation_owner: { run_id: claims.run_id, head_sha: requestedSession.head_sha },
      };
    }
    return { ...receipt(), token, capability: requested };
  }

  /** Verify an opaque broker grant before a hosted session-ledger mutation. */
  async verifySessionGrant(grant: unknown, attestation: unknown, control?: BrokerDispatchControl, deadlineUnixMs?: number): Promise<SessionAttestation> {
    const lifetime = new BrokerLifetime(deadlineUnixMs, this.observation);
    lifetime.constrain(control);
    return await lifetime.run(() => this.verifyGrantWithinLifetime(grant, attestation, lifetime, control));
  }

  private async verifyGrantWithinLifetime(
    grant: unknown,
    attestation: unknown,
    lifetime: BrokerLifetime,
    control?: BrokerDispatchControl,
  ): Promise<SessionAttestation> {
    if (typeof grant !== "string" || !/^sg1\.[1-9][0-9]{10,15}\.[A-Za-z0-9_-]{43}$/.test(grant)) {
      throw new Error("broker_session_grant_invalid");
    }
    const parsed = parseRunAuthority(attestation, true);
    authorizeControlledRestart(parsed, "review_session");
    const github = this.github.withRequestLifetime(lifetime, control);
    const scope = sessionScope(parsed, parsed.repository_id);
    const configuredTag = validatePublicWorkflowTag(this.env.PUBLIC_WORKFLOW_TAG ?? "");
    const observedTag = authorizeObservedWorkflowTag(configuredTag, parsed.job_workflow_ref);
    const runtime = await github.publicWorkflowRuntimeShas(observedTag);
    authorizeWorkflowRuntimeSha(parsed.job_workflow_sha, runtime);
    const authentic = await sessionGrantAuthentic(signingKey(this.env), grant, {
      scope: `${scope.repository_id}:${scope.pull_request}:${scope.head_sha}`,
      attestationDigest: await digest(canonicalRunAuthority(parsed)),
      audience: SESSION_GRANT_AUDIENCE,
      runId: parsed.run_id,
    });
    if (!authentic) {
      throw new Error("broker_session_grant_invalid");
    }
    control?.check();
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
