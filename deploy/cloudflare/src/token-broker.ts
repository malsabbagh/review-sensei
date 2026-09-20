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
}

interface SessionScope {
  repository_id: number;
  pull_request: number;
  head_sha: string;
}

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
  if (!isObject(value)) {
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
    !/^[a-f0-9]{40}$/.test(headSha)
  ) {
    throw new Error("broker_session_invalid");
  }
  return {
    repository_id: repositoryId,
    pull_request: pullRequest,
    head_sha: headSha,
  };
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
  ): Promise<{ token: string; capability: Capability; session_state?: SessionState }> {
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
      const enrollment = await enrollSession(
        this.env,
        `${requestedSession.repository_id}:${requestedSession.pull_request}:${requestedSession.head_sha}`,
      );
      return { token, capability: requested, session_state: enrollment };
    }
    return { token, capability: requested };
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
    const workflowShaAuthorized =
      claims.job_workflow_sha === runtime.commitSha ||
      claims.job_workflow_sha === runtime.refSha;
    const workflowAuthorized =
      claims.job_workflow_ref === workflowRef(PUBLIC_REPOSITORY, publicWorkflowTag) &&
      workflowShaAuthorized;
    if (!workflowAuthorized) {
      throw new Error("broker_workflow_rejected");
    }
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

export type { BrokerBody, Capability, SessionScope, SessionState };
