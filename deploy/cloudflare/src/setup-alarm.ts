import type { WorkerEnv } from "./env";
import { GitHubSetupService, type VerifiedDelivery } from "./github-app";
import {
  continuationErrorCode,
  runSetupContinuationStep,
  type SetupContinuationRequest,
  type SetupFailureSummary,
} from "./setup-continuation";

export type ContinuationAdvance =
  | { kind: "continue"; next: SetupContinuationRequest; delayMs: number }
  | { kind: "complete" }
  | { kind: "release"; summary: SetupFailureSummary };

function continuationDelivery(
  input: SetupContinuationRequest,
  repositories: string[],
): VerifiedDelivery {
  return {
    appId: input.appId,
    event: input.event,
    action: input.action,
    installationId: input.installationId,
    deliveryId: input.deliveryId,
    repository: repositories[0] ?? null,
    repositories,
    suspended: false,
    permissions: input.permissions,
  };
}

/**
 * Advance one stored continuation by a single repository.
 * The Durable Object alarm persists the result and schedules the next alarm,
 * so a dropped Worker waitUntil cannot lose the installation.
 */
export async function advanceStoredContinuation(
  env: WorkerEnv,
  input: SetupContinuationRequest,
): Promise<ContinuationAdvance> {
  let advance: ContinuationAdvance = {
    kind: "release",
    summary: {
      errorCode: input.failureCode ?? "setup_failed",
      failureCount: input.failureCount,
      repository: input.failedRepository,
    },
  };
  await runSetupContinuationStep(input, {
    processRepository: async (repository) => {
      await new GitHubSetupService(env).process(continuationDelivery(input, [repository]));
    },
    resolveRepositories: () =>
      new GitHubSetupService(env).selectSetupRepositories(continuationDelivery(input, [])),
    schedule: async (next, delayMs = 0) => {
      advance = { kind: "continue", next, delayMs };
    },
    complete: async () => {
      advance = { kind: "complete" };
    },
    release: async (summary) => {
      advance = { kind: "release", summary };
    },
    report: (error) => {
      console.error("github_setup_failed", {
        delivery_id: input.deliveryId,
        event: input.event,
        error_code: continuationErrorCode(error),
      });
    },
  });
  return advance;
}
