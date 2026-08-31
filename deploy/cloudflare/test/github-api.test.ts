import { describe, expect, it, vi } from "vitest";
import type { WorkerEnv } from "../src/env";
import { GitHubApi } from "../src/github-api";

const SHA = "a".repeat(40);
const TAG_OBJECT_SHA = "b".repeat(40);

function api() {
  const value = {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused-by-test",
  } as unknown as WorkerEnv;
  const client = new GitHubApi(value);
  vi.spyOn(client as never, "appJwt" as never).mockResolvedValue("app-jwt");
  return client;
}

describe("GitHubApi public workflow resolution", () => {
  it("resolves a lightweight tag to its commit SHA", async () => {
    const client = api();
    const request = vi.spyOn(client, "request").mockResolvedValue({
      status: 200,
      data: { object: { type: "commit", sha: SHA } },
    });

    await expect(client.publicWorkflowSha("v4")).resolves.toBe(SHA);
    expect(request).toHaveBeenCalledWith(
      "GET",
      "/repos/malsabbagh/review-sensei/git/ref/tags/v4",
      "app-jwt",
    );
  });

  it("dereferences an annotated tag before accepting its commit", async () => {
    const client = api();
    const request = vi
      .spyOn(client, "request")
      .mockResolvedValueOnce({
        status: 200,
        data: { object: { type: "tag", sha: TAG_OBJECT_SHA } },
      })
      .mockResolvedValueOnce({
        status: 200,
        data: { object: { type: "commit", sha: SHA } },
      });

    await expect(client.publicWorkflowSha("v4")).resolves.toBe(SHA);
    expect(request).toHaveBeenNthCalledWith(
      2,
      "GET",
      `/repos/malsabbagh/review-sensei/git/tags/${TAG_OBJECT_SHA}`,
      "app-jwt",
    );
  });

  it("fails closed for unsafe tags and non-commit tag objects", async () => {
    const client = api();
    const request = vi.spyOn(client, "request");

    await expect(client.publicWorkflowSha("refs/tags/v4")).rejects.toThrow(
      "github_workflow_tag_invalid",
    );
    expect(request).not.toHaveBeenCalled();

    request.mockResolvedValue({
      status: 200,
      data: { object: { type: "tree", sha: SHA } },
    });
    await expect(client.publicWorkflowSha("v4")).rejects.toThrow(
      "github_workflow_tag_invalid",
    );
  });
});
