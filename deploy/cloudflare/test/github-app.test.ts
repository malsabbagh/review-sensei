import { describe, expect, it, vi } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import type { WorkerEnv } from "../src/env";
import {
  GitHubSetupService,
  WebhookPayloadError,
  type VerifiedDelivery,
  parseVerifiedDelivery,
} from "../src/github-app";
import {
  SETUP_FILE_PATHS,
  buildCurrentV3SetupFiles,
  buildHistoricalProviderParityV4SetupFiles,
  buildLegacyAutoApproveV4SetupFiles,
  buildTaggedV4SetupFiles,
  buildSetupFiles,
} from "../src/setup-content";

const SHA = "a".repeat(40);
const TAG = "v4";
const BASE_SHA = "b".repeat(40);
const SETUP_BRANCH = `review-sensei/setup-v4-${BASE_SHA.slice(0, 12)}-${TAG}`;
const ALL_PERMISSIONS = {
  contents: "write",
  pull_requests: "write",
  variables: "write",
  workflows: "write",
};

function env(): WorkerEnv {
  return {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused-by-test-adapter",
    GITHUB_APP_WEBHOOK_SECRET: "unused",
    PUBLIC_WORKFLOW_TAG: TAG,
  } as WorkerEnv;
}

function body(value: unknown): ArrayBuffer {
  return new TextEncoder().encode(JSON.stringify(value)).buffer as ArrayBuffer;
}

function payload(action: string, repositoriesKey = "repositories", repositories = ["acme/one", "acme/two"]) {
  return {
    action,
    installation: {
      id: 2468,
      suspended_at: null,
      permissions: {
        contents: "write",
        pull_requests: "write",
        actions_variables: "write",
        workflows: "write",
      },
    },
    [repositoriesKey]: repositories.map((full_name) => ({ full_name })),
  };
}

function delivery(overrides: Partial<VerifiedDelivery> = {}): VerifiedDelivery {
  return {
    appId: 12345,
    event: "installation",
    action: "created",
    installationId: 2468,
    deliveryId: "delivery-1",
    repository: "acme/widgets",
    repositories: ["acme/widgets"],
    suspended: false,
    permissions: ALL_PERMISSIONS,
    ...overrides,
  };
}

type SetupFiles = Record<string, string | null>;

class FakeGitHub {
  readonly requests: Array<{ method: string; path: string; body?: Record<string, unknown> }> = [];
  readonly installationToken = vi.fn(async () => ({
    token: "ghs_setup",
    expiresAt: Date.now() + 60_000,
    permissions: ALL_PERMISSIONS,
  }));
  readonly installationRepositories = vi.fn(async () => ["acme/one", "acme/two"]);
  readonly publicWorkflowSha = vi.fn(async () => SHA);
  files: SetupFiles = Object.fromEntries(SETUP_FILE_PATHS.map((path) => [path, null]));
  branchFiles: SetupFiles = Object.fromEntries(
    buildSetupFiles(TAG).map(({ path, content }) => [path, content]),
  );
  branchExists = false;
  branchManaged = true;
  refCollision = false;
  collisionObserved = false;
  missingVariables = false;
  existingPullRequest: number | null = null;

  async request(method: string, path: string, _token: string, requestBody?: Record<string, unknown>) {
    this.requests.push({ method, path, body: requestBody });
    if (method === "GET" && /^\/repos\/acme\/(widgets|one|two)$/.test(path)) {
      return { status: 200, data: { default_branch: "main" } };
    }
    if (method === "GET" && path.includes("/branches/review-sensei%2Fsetup-v4-")) {
      const exists = this.branchExists || this.collisionObserved;
      return {
        status: exists ? 200 : 404,
        data: exists
          ? {
              commit: {
                sha: "c".repeat(40),
                commit: {
                  message: this.branchManaged
                    ? "Add ReviewSensei review setup files"
                    : "Customer branch",
                },
                parents: [{ sha: BASE_SHA }],
                author: {
                  login: this.branchManaged ? "reviewsensei[bot]" : "customer",
                  type: this.branchManaged ? "Bot" : "User",
                },
              },
            }
          : null,
      };
    }
    if (method === "GET" && path.includes("/compare/")) {
      return {
        status: 200,
        data: {
          files: [
            {
              filename: this.branchManaged
                ? ".github/review-sensei/config.yml"
                : "customer.txt",
            },
          ],
        },
      };
    }
    if (method === "GET" && path.includes("/pulls?")) {
      return {
        status: 200,
        data: this.existingPullRequest === null ? [] : [{ number: this.existingPullRequest }],
      };
    }
    if (method === "GET" && path.includes("/contents/")) {
      const encoded = path.split("/contents/")[1].split("?", 1)[0];
      const filePath = encoded.split("/").map(decodeURIComponent).join("/");
      const content = path.includes("?ref=review-sensei%2Fsetup-v4-")
        ? this.branchFiles[filePath]
        : this.files[filePath];
      return content === null || content === undefined
        ? { status: 404, data: null }
        : {
            status: 200,
            data: {
              type: "file",
              encoding: "base64",
              truncated: false,
              content: Buffer.from(content, "utf8").toString("base64"),
            },
          };
    }
    if (method === "GET" && path.includes("/actions/variables/")) {
      return this.missingVariables
        ? { status: 404, data: null }
        : { status: 200, data: { value: "operator-owned" } };
    }
    if (method === "POST" && path.endsWith("/actions/variables")) {
      return { status: 201, data: null };
    }
    if (method === "GET" && path.includes("/git/ref/heads/main")) {
      return { status: 200, data: { object: { sha: BASE_SHA } } };
    }
    if (method === "POST" && path.endsWith("/git/trees")) {
      return { status: 201, data: { sha: "tree-sha" } };
    }
    if (method === "POST" && path.endsWith("/git/commits")) {
      return { status: 201, data: { sha: "d".repeat(40) } };
    }
    if (method === "POST" && path.endsWith("/git/refs")) {
      if (this.refCollision) {
        this.collisionObserved = true;
        return { status: 422, data: { message: "Reference already exists" } };
      }
      return { status: 201, data: { ref: `refs/heads/${SETUP_BRANCH}` } };
    }
    if (method === "POST" && path.endsWith("/pulls")) {
      return { status: 201, data: { number: 42 } };
    }
    throw new Error(`unexpected GitHub request: ${method} ${path}`);
  }
}

function serviceWith(fake: FakeGitHub): GitHubSetupService {
  return new GitHubSetupService(env(), fake);
}

function mutationRequests(fake: FakeGitHub) {
  return fake.requests.filter(({ method }) => method !== "GET");
}

function historicalFixture(name: string): string {
  return readFileSync(
    new URL(`../../../tests/fixtures/setup-legacy/${name}`, import.meta.url),
    "utf8",
  );
}

function historicalV3Files(publicWorkflowSha: string): SetupFiles {
  const files = Object.fromEntries(
    buildCurrentV3SetupFiles(publicWorkflowSha).map(({ path, content }) => [path, content]),
  ) as SetupFiles;
  files[SETUP_FILE_PATHS[0]] = files[SETUP_FILE_PATHS[0]]!
    .replace(
      [
        "      source_kind:",
        "        description: Source kind for manual dispatch (issue or inline)",
        "        required: false",
        "      source_comment_id:",
        "        description: Source comment ID for manual reply",
        "        required: false",
        "      source_updated_at:",
        "        description: Timestamp of source comment",
        "        required: false",
        "      root_comment_id:",
        "        description: Root comment ID for manual reply thread",
        "        required: false",
      ].join("\n") + "\n",
      "",
    )
    .replace("  trusted-local-manual:\n", "  manual-or-trusted-local:\n")
    .replace(
      "      head_ref: ${{ inputs.head_ref || '' }}\n",
      "      head_ref: ${{ inputs.head_ref || github.event.pull_request.head.ref || '' }}\n",
    );
  files[SETUP_FILE_PATHS[1]] = files[SETUP_FILE_PATHS[1]]!.replace(
    '              "--body", "Remove generated ReviewSensei setup files; ' +
      'learnings and secrets remain untouched.",\n',
    '              "--body", "Remove the ReviewSensei workflow, cleanup workflow, ' +
      'and generated configuration. ReviewSensei learnings and repository secrets ' +
      'are left untouched.",\n',
  );
  return files;
}

describe("installation and migration events", () => {
  it.each([
    ["installation", "created", "repositories"],
    ["installation", "new_permissions_accepted", "repositories"],
    ["installation_repositories", "added", "repositories_added"],
  ])("selects every repository for %s.%s", (event, action, key) => {
    const parsed = parseVerifiedDelivery(body(payload(action, key)), event, "delivery-1", 12345);
    expect(parsed).toMatchObject({
      event,
      action,
      repositories: ["acme/one", "acme/two"],
      permissions: ALL_PERMISSIONS,
      suspended: false,
    });
  });

  it("accepts lifecycle delivery without a repository list for API fallback", () => {
    const parsed = parseVerifiedDelivery(
      body(payload("new_permissions_accepted", "repositories", [])),
      "installation",
      "delivery-fallback",
      12345,
    );
    expect(parsed).toMatchObject({
      event: "installation",
      action: "new_permissions_accepted",
      repository: null,
      repositories: [],
    });
  });

  it("keeps added-repository selection scoped to repositories_added", () => {
    expect(() =>
      parseVerifiedDelivery(
        body(payload("added", "repositories")),
        "installation_repositories",
        "delivery-added-scope",
        12345,
      ),
    ).toThrow(WebhookPayloadError);
  });

  it("ignores repository removals and suspended installations", async () => {
    const removed = parseVerifiedDelivery(
      body(payload("removed", "repositories_removed")),
      "installation_repositories",
      "delivery-2",
      12345,
    );
    expect(removed).not.toBeNull();
    expect(await new GitHubSetupService(env()).process(removed!)).toEqual([]);
    expect(
      await new GitHubSetupService(env()).process(delivery({ suspended: true })),
    ).toEqual([]);
  });

  it("reports missing setup permissions without calling GitHub", async () => {
    const fake = new FakeGitHub();
    const results = await serviceWith(fake).process(
      delivery({ permissions: { contents: "read" } }),
    );
    expect(results).toEqual([{ repository: "acme/widgets", status: "skipped_permissions" }]);
    expect(fake.installationToken).not.toHaveBeenCalled();
    expect(fake.requests).toEqual([]);
  });

  it("rejects an invalid configured tag before any setup write", async () => {
    const fake = new FakeGitHub();
    const invalidEnvironment = {
      ...env(),
      PUBLIC_WORKFLOW_TAG: "v4.lock",
    } as WorkerEnv;

    await expect(
      new GitHubSetupService(invalidEnvironment, fake).process(delivery()),
    ).rejects.toThrow("PUBLIC_WORKFLOW_TAG");
    expect(mutationRequests(fake)).toEqual([]);
  });

  it("fails closed when the configured tag cannot be resolved", async () => {
    const fake = new FakeGitHub();
    fake.publicWorkflowSha.mockRejectedValue(new Error("public workflow tag unavailable"));

    await expect(serviceWith(fake).process(delivery())).rejects.toThrow("public workflow tag unavailable");
    expect(fake.installationToken).not.toHaveBeenCalled();
    expect(mutationRequests(fake)).toEqual([]);
  });

  it.each([
    ["installation", "created"],
    ["installation", "new_permissions_accepted"],
    ["installation_repositories", "added"],
  ])("reconciles every selected repository for %s.%s", async (event, action) => {
    const fake = new FakeGitHub();
    const results = await serviceWith(fake).process(
      delivery({
        event,
        action,
        repository: "acme/one",
        repositories: ["acme/one", "acme/two"],
        permissions: { contents: "read" },
      }),
    );
    expect(results).toEqual([
      { repository: "acme/one", status: "skipped_permissions" },
      { repository: "acme/two", status: "skipped_permissions" },
    ]);
    expect(fake.installationToken).not.toHaveBeenCalled();
  });

  it("lists every selected installation repository when lifecycle payload omits them", async () => {
    const fake = new FakeGitHub();
    const results = await serviceWith(fake).process(
      delivery({ repository: null, repositories: [] }),
    );
    expect(results.map(({ repository }) => repository)).toEqual([
      "acme/one",
      "acme/two",
    ]);
    expect(fake.installationRepositories).toHaveBeenCalledWith(2468);
  });
});

describe("setup repository reconciliation", () => {
  it("creates a setup PR for an older client with no generated files", async () => {
    const fake = new FakeGitHub();
    const results = await serviceWith(fake).process(delivery());

    expect(results).toEqual([{ repository: "acme/widgets", status: "created", pull_request_number: 42 }]);
    expect(fake.publicWorkflowSha).toHaveBeenCalledWith(TAG);
    expect(fake.installationToken).toHaveBeenCalledWith(2468, "acme/widgets", {
      contents: "write",
      pull_requests: "write",
      actions_variables: "write",
      workflows: "write",
    });
    const treeRequest = fake.requests.find(({ path }) => path.endsWith("/git/trees"));
    expect((treeRequest?.body?.tree as Array<{ path: string }>).map(({ path }) => path)).toEqual(
      SETUP_FILE_PATHS,
    );
    expect(fake.requests.find(({ method, path }) => method === "POST" && path.endsWith("/pulls"))?.body)
      .toMatchObject({ head: SETUP_BRANCH, base: "main" });
  });

  it("reuses a canonical create-only branch for recognized legacy files", async () => {
    const fake = new FakeGitHub();
    fake.branchExists = true;
    fake.files = {
      ".github/workflows/review-sensei-review.yml": null,
      ".github/workflows/review-sensei-uninstall.yml": null,
      ".github/review-sensei/config.yml": [
        "# ReviewSensei setup version: 2",
        "setup_version: 2",
        "provider: ollama",
        "provider_mode: local",
        "base_url: http://127.0.0.1:11434/api",
        "cloud_base_url: https://ollama.com/api",
        "local_model: qwen3.5:4b",
        "cloud_model: deepseek-v4-flash:cloud",
        "",
      ].join("\n"),
    };

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
    expect(fake.requests.some(({ method }) => method === "PATCH")).toBe(false);
    expect(fake.requests.find(({ method, path }) => method === "POST" && path.endsWith("/pulls"))?.body)
      .toMatchObject({ head: SETUP_BRANCH, base: "main" });
  });

  it("does not overwrite a customer-owned deterministic branch", async () => {
    const fake = new FakeGitHub();
    fake.branchExists = true;
    fake.branchManaged = false;

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "skipped_branch_conflict" },
    ]);
    expect(mutationRequests(fake)).toEqual([]);
  });

  it("does not overwrite a branch created during the create-only ref race", async () => {
    const fake = new FakeGitHub();
    fake.refCollision = true;
    fake.branchManaged = false;
    fake.missingVariables = true;

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "skipped_branch_conflict" },
    ]);
    expect(mutationRequests(fake).filter(({ path }) =>
      path.includes("/actions/variables"),
    )).toEqual([]);
    expect(fake.requests.some(({ method }) => method === "PATCH")).toBe(false);
    expect(fake.requests.some(({ method, path }) =>
      method === "POST" && path.endsWith("/pulls"),
    )).toBe(false);
  });

  it("does not replace customized setup-v2 content", async () => {
    const fake = new FakeGitHub();
    fake.files[".github/review-sensei/config.yml"] = [
      "# ReviewSensei setup version: 2",
      "setup_version: 2",
      "provider: ollama",
      "base_url: https://customer.example/api",
    ].join("\n");
    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "skipped_unknown_setup" },
    ]);
    expect(mutationRequests(fake)).toEqual([]);
  });

  it.each(["pre-marker-worker-review.yml", "pre-marker-python-review.yml"])(
    "migrates the released historical fixture %s",
    async (fixture) => {
      const fake = new FakeGitHub();
      fake.files = {
        ".github/workflows/review-sensei-review.yml": historicalFixture(fixture),
        ".github/workflows/review-sensei-uninstall.yml": null,
        ".github/review-sensei/config.yml": historicalFixture("pre-marker-config.yml"),
      };

      expect(await serviceWith(fake).process(delivery())).toEqual([
        { repository: "acme/widgets", status: "created", pull_request_number: 42 },
      ]);
    },
  );

  it.each([
    ["v2-worker-review.yml", "v2-worker-uninstall.yml"],
    ["v2-python-review.yml", "v2-python-uninstall.yml"],
  ])("migrates a complete cross-runtime v2 client %s", async (review, uninstall) => {
    const fake = new FakeGitHub();
    fake.files = {
      ".github/workflows/review-sensei-review.yml": historicalFixture(review),
      ".github/workflows/review-sensei-uninstall.yml": historicalFixture(uninstall),
      ".github/review-sensei/config.yml": historicalFixture("v2-config.yml"),
    };

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
  });

  it("migrates the released intermediate pre-marker setup", async () => {
    const fake = new FakeGitHub();
    fake.files = {
      ".github/workflows/review-sensei-review.yml": historicalFixture(
        "intermediate-pre-marker-worker-review.yml",
      ),
      ".github/workflows/review-sensei-uninstall.yml": historicalFixture(
        "intermediate-pre-marker-worker-uninstall.yml",
      ),
      ".github/review-sensei/config.yml": historicalFixture(
        "intermediate-pre-marker-config.yml",
      ),
    };

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
  });

  it("migrates a managed v3 setup with a stale public workflow SHA", async () => {
    const fake = new FakeGitHub();
    fake.files = Object.fromEntries(
      buildCurrentV3SetupFiles("c".repeat(40)).map(({ path, content }) => [path, content]),
    );

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
    expect(
      mutationRequests(fake).some(({ method, path }) =>
        method === "POST" && path.endsWith("/pulls"),
      ),
    ).toBe(true);
    expect(
      fake.requests.find(
        ({ method, path }) => method === "POST" && path.endsWith("/pulls"),
      )?.body,
    ).toMatchObject({ head: SETUP_BRANCH, base: "main" });
  });

  it("migrates a managed v4 setup following an older public tag", async () => {
    const fake = new FakeGitHub();
    fake.files = Object.fromEntries(
      buildTaggedV4SetupFiles("old-v4").map(({ path, content }) => [path, content]),
    );

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
    expect(
      fake.requests.find(
        ({ method, path }) => method === "POST" && path.endsWith("/pulls"),
      )?.body,
    ).toMatchObject({ head: SETUP_BRANCH, base: "main" });
  });

  it("migrates the released provider-parity v4 caller with the reply default", async () => {
    const fake = new FakeGitHub();
    fake.files = Object.fromEntries(
      buildHistoricalProviderParityV4SetupFiles(TAG).map(({ path, content }) => [path, content]),
    );

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
    expect(
      fake.requests.find(
        ({ method, path }) => method === "POST" && path.endsWith("/pulls"),
      )?.body,
    ).toMatchObject({ head: SETUP_BRANCH, base: "main" });
  });

  it("migrates the released v4 caller with the removed approval toggle", async () => {
    const fake = new FakeGitHub();
    fake.files = Object.fromEntries(
      buildLegacyAutoApproveV4SetupFiles(TAG).map(({ path, content }) => [path, content]),
    );

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
    expect(
      fake.requests.find(
        ({ method, path }) => method === "POST" && path.endsWith("/pulls"),
      )?.body,
    ).toMatchObject({ head: SETUP_BRANCH, base: "main" });
  });

  it("migrates a released v3 setup with a stale public workflow SHA", async () => {
    const fake = new FakeGitHub();
    fake.files = historicalV3Files("c".repeat(40));
    expect(
      createHash("sha256")
        .update(fake.files[SETUP_FILE_PATHS[0]]!, "utf8")
        .digest("hex"),
    ).toBe("a693256f243dbeafc2910297375064b6133b7ace8f71b75712c72db43a6fafee");
    expect(
      createHash("sha256")
        .update(fake.files[SETUP_FILE_PATHS[1]]!, "utf8")
        .digest("hex"),
    ).toBe("fdf0b34c76cd8e0ec7330d307315f7579a4b1fac5a8c5c0cc5f627788c398df8");

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "created", pull_request_number: 42 },
    ]);
    expect(
      mutationRequests(fake).some(({ method, path }) =>
        method === "POST" && path.endsWith("/pulls"),
      ),
    ).toBe(true);
  });

  it("does not replace customized partial setup-v4 content", async () => {
    const fake = new FakeGitHub();
    const current = buildSetupFiles(TAG)[0].content;
    fake.files[".github/workflows/review-sensei-review.yml"] = current.replace(
      "name: ReviewSensei review",
      "name: Customer ReviewSensei review",
    );

    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "skipped_unknown_setup" },
    ]);
    expect(mutationRequests(fake)).toEqual([]);
  });

  it("does not write when setup-v4 is already current", async () => {
    const fake = new FakeGitHub();
    fake.files = Object.fromEntries(buildSetupFiles(TAG).map(({ path, content }) => [path, content]));
    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "skipped_current" },
    ]);
    expect(mutationRequests(fake)).toEqual([]);
  });

  it("does not overwrite custom, malformed, or future setup", async () => {
    for (const content of [
      "name: Customer-owned workflow\n",
      "# ReviewSensei setup version: not-a-number\nname: ReviewSensei review\n",
      "# ReviewSensei setup version: 99\nname: ReviewSensei review\n",
      buildSetupFiles(TAG)[0].content.replaceAll(`@${TAG}`, "@v4.lock"),
    ]) {
      const fake = new FakeGitHub();
      fake.files[".github/workflows/review-sensei-review.yml"] = content;
      expect(await serviceWith(fake).process(delivery())).toEqual([
        { repository: "acme/widgets", status: "skipped_unknown_setup" },
      ]);
      expect(mutationRequests(fake)).toEqual([]);
    }
  });

  it("reuses an existing setup pull request after refreshing absent setup", async () => {
    const fake = new FakeGitHub();
    fake.existingPullRequest = 17;
    expect(await serviceWith(fake).process(delivery())).toEqual([
      { repository: "acme/widgets", status: "skipped_pull_request_exists", pull_request_number: 17 },
    ]);
    expect(fake.requests.some(({ path }) => path.includes("/contents/"))).toBe(true);
    expect(mutationRequests(fake).some(({ path }) => path.endsWith("/git/trees"))).toBe(true);
  });
});
