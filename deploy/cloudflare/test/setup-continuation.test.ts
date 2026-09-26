import { describe, expect, it, vi } from "vitest";
import { GitHubSetupTransientError } from "../src/github-app";
import {
  SetupContinuationError,
  parseSetupContinuationRequest,
  runSetupContinuationStep,
  type SetupContinuationOps,
  type SetupContinuationRequest,
} from "../src/setup-continuation";

const PERMISSIONS = {
  contents: "write",
  pull_requests: "write",
  variables: "write",
  workflows: "write",
};

function request(
  overrides: Partial<SetupContinuationRequest> = {},
): SetupContinuationRequest {
  return {
    appId: 12345,
    event: "installation",
    action: "created",
    installationId: 2468,
    deliveryId: "delivery-1",
    digest: "a".repeat(64),
    permissions: PERMISSIONS,
    repositories: ["acme/one", "acme/two"],
    unresolved: false,
    attempt: 0,
    failed: false,
    ...overrides,
  };
}

function ops(overrides: Partial<SetupContinuationOps> = {}): SetupContinuationOps {
  return {
    processRepository: vi.fn(async () => undefined),
    resolveRepositories: vi.fn(async () => ["acme/one", "acme/two"]),
    schedule: vi.fn(async () => undefined),
    complete: vi.fn(async () => undefined),
    release: vi.fn(async () => undefined),
    report: vi.fn(),
    ...overrides,
  };
}

describe("setup continuation requests", () => {
  it("accepts a bounded multi-repository request", () => {
    expect(parseSetupContinuationRequest(request())).toEqual(request());
  });

  it("rejects an unresolved request that already names repositories", () => {
    expect(() =>
      parseSetupContinuationRequest(request({ unresolved: true })),
    ).toThrow(SetupContinuationError);
  });

  it("rejects an invalid repository slug", () => {
    expect(() =>
      parseSetupContinuationRequest(request({ repositories: ["not a repo"] })),
    ).toThrow(SetupContinuationError);
  });
});

describe("setup continuation steps", () => {
  it("resolves an omitted repository list before reconciling any repository", async () => {
    const continuation = ops();
    await runSetupContinuationStep(
      request({ repositories: [], unresolved: true }),
      continuation,
    );
    expect(continuation.resolveRepositories).toHaveBeenCalledOnce();
    expect(continuation.processRepository).not.toHaveBeenCalled();
    expect(continuation.schedule).toHaveBeenCalledWith(
      expect.objectContaining({
        repositories: ["acme/one", "acme/two"],
        unresolved: false,
        attempt: 0,
      }),
    );
  });

  it("reconciles only the head repository and schedules the remainder", async () => {
    const continuation = ops();
    await runSetupContinuationStep(request(), continuation);
    expect(continuation.processRepository).toHaveBeenCalledWith("acme/one");
    expect(continuation.schedule).toHaveBeenCalledWith(
      expect.objectContaining({
        repositories: ["acme/two"],
        attempt: 0,
        failed: false,
      }),
    );
    expect(continuation.complete).not.toHaveBeenCalled();
  });

  it("completes the delivery after the last repository succeeds", async () => {
    const continuation = ops();
    await runSetupContinuationStep(
      request({ repositories: ["acme/two"] }),
      continuation,
    );
    expect(continuation.processRepository).toHaveBeenCalledWith("acme/two");
    expect(continuation.complete).toHaveBeenCalledOnce();
    expect(continuation.schedule).not.toHaveBeenCalled();
  });

  it("retries a transient repository failure on a fresh invocation", async () => {
    const continuation = ops({
      processRepository: vi.fn(async () => {
        throw new GitHubSetupTransientError("github_request_transient_503");
      }),
    });
    await runSetupContinuationStep(request(), continuation);
    expect(continuation.schedule).toHaveBeenCalledWith(
      expect.objectContaining({
        repositories: ["acme/one", "acme/two"],
        attempt: 1,
      }),
    );
    expect(continuation.release).not.toHaveBeenCalled();
  });

  it("continues with later repositories after retry budget is exhausted", async () => {
    const error = new GitHubSetupTransientError("github_request_transient_503");
    const continuation = ops({
      processRepository: vi.fn(async () => {
        throw error;
      }),
    });
    await runSetupContinuationStep(request({ attempt: 2 }), continuation);
    expect(continuation.report).toHaveBeenCalledWith(error);
    expect(continuation.schedule).toHaveBeenCalledWith(
      expect.objectContaining({
        repositories: ["acme/two"],
        attempt: 0,
        failed: true,
      }),
    );
  });

  it("releases the claim on a fatal configuration error", async () => {
    const error = new Error("PUBLIC_WORKFLOW_TAG must be a movable release channel");
    const continuation = ops({
      processRepository: vi.fn(async () => {
        throw error;
      }),
    });
    await runSetupContinuationStep(request(), continuation);
    expect(continuation.report).toHaveBeenCalledWith(error);
    expect(continuation.release).toHaveBeenCalledOnce();
    expect(continuation.schedule).not.toHaveBeenCalled();
  });

  it("releases the claim when the last repository fails after earlier failures", async () => {
    const continuation = ops();
    await runSetupContinuationStep(
      request({ repositories: ["acme/two"], failed: true }),
      continuation,
    );
    expect(continuation.complete).not.toHaveBeenCalled();
    expect(continuation.release).toHaveBeenCalledOnce();
  });
});
