import type { WorkerEnv } from "./env";
import { GitHubApi } from "./github-api";
import { type OidcClaims, verifyOidcAssertion } from "./oidc";
import { validatePublicWorkflowTag } from "./setup-content";

const WORKFLOW_PATH = ".github/workflows/review-sensei-run.yml";
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const CAPABILITIES = {
  review_publish: { "pull_requests": "write" },
  inline_reply: { "pull_requests": "write" },
  issue_reply: { issues: "write" },
  learning_write: { contents: "write", "pull_requests": "write" },
} as const;
const REPOSITORY_METADATA_PERMISSIONS = { metadata: "read" } as const;

type Capability = keyof typeof CAPABILITIES;

interface BrokerBody {
  oidc_token?: unknown;
  capability?: unknown;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function capability(value: unknown): Capability {
  if (value === undefined) {
    return "review_publish";
  }
  if (typeof value !== "string" || !(value in CAPABILITIES)) {
    throw new Error("broker_capability_invalid");
  }
  return value as Capability;
}

function workflowRef(repository: string, tag: string): string {
  return `${repository}/${WORKFLOW_PATH}@refs/tags/${tag}`;
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

async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(bytes)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

function clientScope(value: string | undefined): string {
  return value && /^[0-9A-Fa-f:.]{1,64}$/.test(value) ? value : "unknown";
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
  ): Promise<{ token: string; capability: Capability }> {
    if (!isObject(body) || typeof body.oidc_token !== "string" || body.oidc_token.length === 0) {
      throw new Error("broker_request_invalid");
    }
    const requested = capability(body.capability);
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
    const publicWorkflowTag = validatePublicWorkflowTag(
      this.env.PUBLIC_WORKFLOW_TAG ?? "",
    );
    const publicWorkflowSha = await this.github.publicWorkflowSha(publicWorkflowTag);
    this.authorizeClaims(claims, publicWorkflowTag, publicWorkflowSha);
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
    const token = await this.github.capabilityToken(
      installationId,
      claims.repository,
      CAPABILITIES[requested],
    );
    return { token, capability: requested };
  }

  private authorizeClaims(
    claims: OidcClaims,
    publicWorkflowTag: string,
    publicWorkflowSha: string,
  ): void {
    if (!REPOSITORY_PATTERN.test(claims.repository)) {
      throw new Error("broker_repository_rejected");
    }
    // v4 is the operator-managed update channel. Resolve its current commit
    // above, then require both the tag ref and the runtime-resolved SHA so a
    // caller cannot substitute a different ref or commit.
    const workflowAuthorized =
      claims.job_workflow_ref === workflowRef("malsabbagh/review-sensei", publicWorkflowTag) &&
      claims.job_workflow_sha === publicWorkflowSha;
    if (!workflowAuthorized) {
      throw new Error("broker_workflow_rejected");
    }
    if (claims.event_name === "pull_request") {
      if (claims.runner_environment !== "github-hosted") {
        throw new Error("broker_runner_rejected");
      }
    } else if (!["workflow_dispatch", "issue_comment", "pull_request_review_comment"].includes(claims.event_name)) {
      throw new Error("broker_event_rejected");
    }
    if (claims.repository_owner !== claims.repository.split("/", 1)[0]) {
      throw new Error("broker_repository_rejected");
    }
  }
}

export type { BrokerBody, Capability };
