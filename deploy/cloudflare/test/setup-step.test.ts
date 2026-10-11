import { describe, expect, it, vi } from "vitest";
import type { WorkerEnv, WorkerExecution } from "../src/env";
import { signSetupCursor, verifySetupCursor } from "../src/signed-mac";
import { SETUP_CONTINUE_URL, listingDigest, runSignedSetupStep } from "../src/setup-step";

const KEY = "test-signing-key";
const PERMISSIONS = {
  contents: "write",
  pull_requests: "write",
  workflows: "write",
};

function env(): WorkerEnv {
  return {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused",
    GITHUB_APP_WEBHOOK_SECRET: "unused",
    PUBLIC_WORKFLOW_TAG: "v5",
    REVIEWSENSEI_SIGNING_KEY: KEY,
  } as WorkerEnv;
}

function context(calls: Array<{ url: string; cursor: string }>): WorkerExecution & { tasks: Promise<unknown>[] } {
  const tasks: Promise<unknown>[] = [];
  return {
    tasks,
    waitUntil(promise) {
      tasks.push(promise);
    },
  };
}

describe("signed setup continuation", () => {
  it("re-reads the installation and schedules the next repository through the self binding", async () => {
    const calls: Array<{ url: string; cursor: string }> = [];
    const processed: string[] = [];
    const runtime = env();
    runtime.SELF = {
      fetch: async (input, init) => {
        const url = String(input);
        calls.push({ url, cursor: JSON.parse(String(init?.body)).cursor as string });
        return new Response("{}", { status: 202 });
      },
    };
    const ctx = context(calls);
    const cursor = await signSetupCursor(KEY, {
      installationId: 2468,
      deliveryId: "delivery-1",
      page: 1,
      offset: 0,
      expiry: Date.now() + 60_000,
      listingSha256: "",
      failureCount: 0,
    });
    const result = await runSignedSetupStep(runtime, cursor, ctx, {
      github: {
        installationSetupView: async () => ({
          permissions: PERMISSIONS,
          repositories: ["acme/one", "acme/two"],
        }),
      } as never,
      processRepository: async (repository) => {
        processed.push(repository);
      },
    });
    await Promise.all(ctx.tasks);
    expect(result).toBe("continued");
    expect(processed).toEqual(["acme/one"]);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(SETUP_CONTINUE_URL);
    expect(calls[0]?.url).not.toContain("github.reviewsensei.dev");
    const next = await verifySetupCursor(KEY, calls[0]!.cursor);
    expect(next).toMatchObject({
      installationId: 2468,
      deliveryId: "delivery-1",
      page: 1,
      offset: 1,
      listingSha256: await listingDigest(PERMISSIONS, ["acme/one", "acme/two"]),
    });
  });

  it("restarts at page 1 when the repository listing changes", async () => {
    const processed: string[] = [];
    const calls: string[] = [];
    const runtime = env();
    runtime.SELF = {
      fetch: async (_input, init) => {
        calls.push(JSON.parse(String(init?.body)).cursor as string);
        return new Response("{}", { status: 202 });
      },
    };
    const previous = await listingDigest(PERMISSIONS, ["acme/old"]);
    const cursor = await signSetupCursor(KEY, {
      installationId: 2468,
      deliveryId: "delivery-9",
      page: 1,
      offset: 4,
      expiry: Date.now() + 60_000,
      listingSha256: previous,
      failureCount: 2,
    });
    await runSignedSetupStep(runtime, cursor, { waitUntil: () => undefined }, {
      github: {
        installationSetupView: async () => ({
          permissions: PERMISSIONS,
          repositories: ["acme/one", "acme/two"],
        }),
      } as never,
      processRepository: async (repository) => {
        processed.push(repository);
      },
    });
    expect(processed).toEqual(["acme/one"]);
    const next = await verifySetupCursor(KEY, calls[0]!);
    expect(next).toMatchObject({ page: 1, offset: 1, failureCount: 2 });
  });

  it("logs only the delivery id, error code, and failure count", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const cursor = await signSetupCursor(KEY, {
      installationId: 2468,
      deliveryId: "delivery-7",
      page: 1,
      offset: 0,
      expiry: Date.now() + 60_000,
      listingSha256: "",
      failureCount: 0,
    });
    try {
      await runSignedSetupStep(env(), cursor, { waitUntil: () => undefined }, {
        github: {
          installationSetupView: async () => ({
            permissions: { contents: "read" },
            repositories: ["acme/secret-name"],
          }),
        } as never,
      });
      expect(error).toHaveBeenCalledWith("github_setup_failed", {
        delivery_id: "delivery-7",
        error_code: "github_installation_permissions_missing",
        failure_count: 1,
      });
      const logged = JSON.stringify(error.mock.calls);
      expect(logged).not.toContain("acme/secret-name");
      expect(logged).not.toContain("secret-name");
    } finally {
      error.mockRestore();
    }
  });
});
