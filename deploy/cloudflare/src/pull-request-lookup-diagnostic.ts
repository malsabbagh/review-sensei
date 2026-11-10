/** Supplementary evidence only: never retry, authorize or change HTTP handling. */
const CODES = new Set([
  "github_pull_request_lookup_failed", "github_pull_request_forbidden",
  "github_pull_request_response_invalid", "github_response_invalid",
  "github_response_too_large", "broker_upstream_timeout", "github_response_unavailable",
]);
const OUTCOMES = new Set(["http", "malformed", "oversized", "timeout", "transport", "unknown"]);
const CAPABILITIES = new Set([
  "review_publish", "thread_resolve", "check_publish", "review_session", "review_status",
  "review_actions", "inline_reply", "issue_reply", "learning_write",
]);
export type PullRequestLookupOutcome = "http" | "malformed" | "oversized" | "timeout" | "transport" | "unknown";
export interface PullRequestLookupEvidence {
  outcome: PullRequestLookupOutcome;
  upstream_status?: number;
  requested_capability?: string;
  worker_version?: string;
}
function status(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 100 && value <= 599 ? value : undefined;
}
export class PullRequestLookupError extends Error {
  readonly pullRequestLookup: Readonly<PullRequestLookupEvidence>;
  readonly diagnosticCode: string;
  constructor(code: string, outcome: PullRequestLookupOutcome, upstreamStatus?: number) {
    super(CODES.has(code) ? code : "broker_unknown");
    this.diagnosticCode = this.message;
    const observed = status(upstreamStatus);
    this.pullRequestLookup = Object.freeze({
      outcome: OUTCOMES.has(outcome) ? outcome : "unknown",
      ...(observed === undefined ? {} : { upstream_status: observed }),
    });
  }
}
/** Build a new closed object; never serialize exception properties or runtime tags. */
export function pullRequestLookupEvidence(error: unknown, capability: unknown, runtimeEnvironment?: unknown): PullRequestLookupEvidence | undefined {
  try {
    if (!(error instanceof PullRequestLookupError) || !CODES.has(error.message) || error.message !== error.diagnosticCode) return undefined;
    const evidence = error.pullRequestLookup;
    const observed = status(evidence?.upstream_status);
    const versionMetadata = typeof runtimeEnvironment === "object" && runtimeEnvironment !== null
      ? (runtimeEnvironment as {CF_VERSION_METADATA?: unknown}).CF_VERSION_METADATA : undefined;
    const version = typeof versionMetadata === "object" && versionMetadata !== null
      ? (versionMetadata as {id?: unknown}).id : undefined;
    return {
      outcome: typeof evidence?.outcome === "string" && OUTCOMES.has(evidence.outcome) ? evidence.outcome : "unknown",
      ...(observed === undefined ? {} : { upstream_status: observed }),
      ...(typeof capability === "string" && CAPABILITIES.has(capability) ? { requested_capability: capability } : {}),
      ...(typeof version === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(version) ? { worker_version: version } : {}),
    };
  } catch { return undefined; } // Supplementary metadata never replaces the original refusal.
}
