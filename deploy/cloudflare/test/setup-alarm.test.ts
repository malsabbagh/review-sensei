import { beforeEach, describe, expect, it, vi } from "vitest";

const selectSetupRepositories = vi.hoisted(() => vi.fn(async () => ["acme/one"]));
const processRepository = vi.hoisted(() => vi.fn(async () => []));

vi.mock("../src/github-app", async () => {
  const actual = await vi.importActual<typeof import("../src/github-app")>("../src/github-app");
  return {
    ...actual,
    GitHubSetupService: class {
      selectSetupRepositories = selectSetupRepositories;
      process = processRepository;
    },
  };
});

import type { WorkerEnv } from "../src/env";
import { advanceStoredContinuation } from "../src/setup-alarm";
import type { SetupContinuationRequest } from "../src/setup-continuation";

const PERMISSIONS = {
  contents: "write",
  pull_requests: "write",
  variables: "write",
  workflows: "write",
};

function request(permissions: Record<string, string>): SetupContinuationRequest {
  return {
    appId: 12345,
    event: "installation",
    action: "created",
    installationId: 2468,
    deliveryId: "delivery-1",
    digest: "ab".repeat(32),
    permissions,
    repositories: [],
    unresolved: true,
    attempt: 0,
    failed: false,
    failureCode: null,
    failureCount: 0,
    failedRepository: null,
  };
}

beforeEach(() => {
  selectSetupRepositories.mockClear();
  processRepository.mockClear();
});

describe("stored continuation permissions", () => {
  it("does not list installation repositories after setup permissions are gone", async () => {
    const advance = await advanceStoredContinuation(
      {} as WorkerEnv,
      request({ contents: "read" }),
    );
    expect(selectSetupRepositories).not.toHaveBeenCalled();
    expect(advance).toEqual({ kind: "complete" });
  });

  it("lists installation repositories when setup permissions are still present", async () => {
    const advance = await advanceStoredContinuation({} as WorkerEnv, request(PERMISSIONS));
    expect(selectSetupRepositories).toHaveBeenCalledOnce();
    expect(advance).toMatchObject({
      kind: "continue",
      delayMs: 0,
      next: {
        repositories: ["acme/one"],
        unresolved: false,
      },
    });
  });
});
