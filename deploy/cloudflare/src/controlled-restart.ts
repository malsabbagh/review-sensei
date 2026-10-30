/** Static per-PR hold for the explicitly designated code-only restart. */
import installed from "../../../packages/python/review-sensei-github/src/review_sensei_github/controlled_restarts.json";
import type { RunAuthorityRequest } from "./run-authority";

interface Registration {
  repository: string;
  repository_id: number;
  pull_request: number;
  run_id: string;
  head_sha: string;
  workflow_sha: string;
  job: string;
  issuance_disabled_at: number;
  retire_after: number;
}

export function authorizeControlledRestart(
  authority: RunAuthorityRequest, capability: string,
  event?: string, registry: unknown = installed,
): void {
  if (typeof registry !== "object" || registry === null || Array.isArray(registry)) throw new Error("broker_restart_registry_invalid");
  const document = registry as Record<string, unknown>;
  if (document.schema_version !== "controlled-restart-registry-v1" ||
      Object.keys(document).length !== 2 || !Array.isArray(document.registrations) ||
      document.registrations.length > 32) throw new Error("broker_restart_registry_invalid");
  const rows = document.registrations as Registration[];
  if (rows.some(row => typeof row !== "object" || row === null ||
      !Number.isSafeInteger(row.repository_id) || !Number.isSafeInteger(row.pull_request))) throw new Error("broker_restart_registry_invalid");
  const matches = rows.filter(row => row.repository_id === authority.repository_id && row.pull_request === authority.pull_request);
  if (matches.length === 0) return;
  if (matches.length !== 1) throw new Error("broker_restart_registry_invalid");
  const row = matches[0];
  if (row.repository !== authority.repository || row.run_id !== authority.run_id ||
      row.head_sha !== authority.head_sha || row.workflow_sha !== authority.job_workflow_sha ||
      row.job !== authority.job || authority.run_attempt !== 1 || authority.purpose !== "review" ||
      event !== undefined && event !== "workflow_dispatch" ||
      !Number.isSafeInteger(row.issuance_disabled_at) || !Number.isSafeInteger(row.retire_after) ||
      row.issuance_disabled_at < 1 || row.retire_after < row.issuance_disabled_at + 3600 ||
      Date.now() / 1000 < row.retire_after ||
      !["review_session", "review_actions", "review_publish", "check_publish", "review_status"].includes(capability)) {
    throw new Error("broker_controlled_restart_held");
  }
}
