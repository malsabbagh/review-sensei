export type LookupPhase = "rest_ref" | "rest_tag";
export type LookupFailure = "http" | "timeout" | "transport" | "malformed" | "oversized";
export interface LookupDiagnostic {
  phase: LookupPhase;
  failure: LookupFailure;
  git_failure?: LookupFailure;
  request_id?: string;
  rate_limit_limit?: number;
  rate_limit_remaining?: number;
  rate_limit_reset?: number;
  retry_after?: number;
  rate_limit_resource?: "core";
}

export function lookupFailure(error: unknown): LookupFailure {
  const code = error instanceof Error ? error.message : "";
  if (code === "broker_upstream_timeout") return "timeout";
  if (code === "github_response_too_large") return "oversized";
  if (code === "github_workflow_tag_invalid" || code === "github_response_invalid") return "malformed";
  if (/^github_workflow_tag_unavailable_[1-5][0-9]{2}$/.test(code)) return "http";
  return "transport";
}

export function lookupHeaders(headers: Headers): Omit<LookupDiagnostic, "phase" | "failure" | "git_failure"> {
  const result: Omit<LookupDiagnostic, "phase" | "failure" | "git_failure"> = {};
  const id = headers.get("x-github-request-id");
  if (id && /^[A-Za-z0-9:-]{1,128}$/.test(id)) result.request_id = id;
  for (const [header, key] of [
    ["x-ratelimit-limit", "rate_limit_limit"], ["x-ratelimit-remaining", "rate_limit_remaining"],
    ["x-ratelimit-reset", "rate_limit_reset"], ["retry-after", "retry_after"],
  ] as const) {
    const value = headers.get(header);
    if (value && /^[0-9]{1,13}$/.test(value) && Number.isSafeInteger(Number(value))) result[key] = Number(value);
  }
  if (headers.get("x-ratelimit-resource") === "core") result.rate_limit_resource = "core";
  return result;
}

export class WorkflowLookupError extends Error {
  constructor(readonly lookup: LookupDiagnostic, readonly status?: number) {
    super(status === undefined ? "github_workflow_tag_unavailable" : `github_workflow_tag_unavailable_${status}`);
  }
}

/** External lookup availability only; never promote policy/control refusals. */
export function temporaryWorkflowLookup(error: unknown): boolean {
  if (!(error instanceof WorkflowLookupError)) return false;
  const { lookup, status } = error;
  if (lookup.phase !== "rest_ref" && lookup.phase !== "rest_tag") return false;
  if (!["http", "timeout", "transport"].includes(lookup.failure)) return false;
  const expected = status === undefined ? "github_workflow_tag_unavailable" : `github_workflow_tag_unavailable_${status}`;
  if (error.message !== expected) return false;
  if (status === undefined) return lookup.failure === "timeout" || lookup.failure === "transport";
  if (!Number.isSafeInteger(status)) return false;
  if (status === 429 || (status >= 500 && status <= 599)) return true;
  if (status >= 200 && status <= 299) return lookup.failure === "timeout" || lookup.failure === "transport";
  return status === 403 && lookup.failure === "http" &&
    (lookup.rate_limit_remaining === 0 ||
      (typeof lookup.retry_after === "number" && Number.isSafeInteger(lookup.retry_after) &&
        lookup.retry_after > 0 && lookup.retry_after <= 9_999_999_999_999));
}
