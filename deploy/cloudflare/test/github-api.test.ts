import { describe, expect, it, vi } from "vitest";
import type { WorkerEnv } from "../src/env";
import { GitHubApi } from "../src/github-api";

const SHA = "a".repeat(40);
const TAG_OBJECT_SHA = "b".repeat(40);

function api() {
  const value = {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: "unused-by-test",
    GITHUB_API_URL: "https://api.example.test",
  } as unknown as WorkerEnv;
  const client = new GitHubApi(value);
  vi.spyOn(client as never, "appJwt" as never).mockResolvedValue("app-jwt");
  return client;
}

describe("GitHubApi public workflow resolution", () => {
  it("omits bearer authentication for public GitHub requests", async () => {
    const client = api();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );

    try {
      await expect(client.request("GET", "/public", undefined)).resolves.toEqual({
        status: 200,
        data: { ok: true },
      });
      const [, init] = fetchMock.mock.calls[0] ?? [];
      expect(new Headers(init?.headers).has("authorization")).toBe(false);
    } finally {
      fetchMock.mockRestore();
    }
  });

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
      undefined,
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

    await expect(client.publicWorkflowRuntimeShas("v4")).resolves.toEqual({
      commitSha: SHA,
      refSha: TAG_OBJECT_SHA,
    });
    expect(request).toHaveBeenNthCalledWith(
      2,
      "GET",
      `/repos/malsabbagh/review-sensei/git/tags/${TAG_OBJECT_SHA}`,
      undefined,
    );
  });

  it("uses public Git ref advertisement before the REST resolver", async () => {
    const client = api();
    const packet = (payload: string): string =>
      `${(payload.length + 4).toString(16).padStart(4, "0")}${payload}`;
    const refs = [
      packet("# service=git-upload-pack\n"),
      "0000",
      packet(`${SHA} refs/tags/v4\0multi_ack\n`),
      "0000",
    ].join("");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new TextEncoder().encode(refs), { status: 200 }),
    );

    try {
      const githubApi = new GitHubApi({
        GITHUB_APP_ID: "12345",
        GITHUB_APP_PRIVATE_KEY: "unused-by-test",
      } as unknown as WorkerEnv);
      const request = vi.spyOn(githubApi, "request").mockResolvedValue({
        status: 403,
        data: { message: "API rate limit exceeded" },
      });
      await expect(githubApi.publicWorkflowSha("v4")).resolves.toBe(SHA);
      expect(request).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledWith(
        "https://github.com/malsabbagh/review-sensei.git/info/refs?service=git-upload-pack",
        expect.objectContaining({ headers: expect.anything() }),
      );
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("uses the peeled commit for an annotated Git tag", async () => {
    const client = new GitHubApi({
      GITHUB_APP_ID: "12345",
      GITHUB_APP_PRIVATE_KEY: "unused-by-test",
    } as unknown as WorkerEnv);
    const packet = (payload: string): string =>
      `${(payload.length + 4).toString(16).padStart(4, "0")}${payload}`;
    const refs = [
      packet(`${TAG_OBJECT_SHA} refs/tags/v4\n`),
      packet(`${SHA} refs/tags/v4^{}\n`),
      "0000",
    ].join("");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new TextEncoder().encode(refs), { status: 200 }),
    );

    try {
      await expect(client.publicWorkflowRuntimeShas("v4")).resolves.toEqual({
        commitSha: SHA,
        refSha: TAG_OBJECT_SHA,
      });
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("falls back to REST refs when public Git advertisement is unavailable", async () => {
    const client = new GitHubApi({
      GITHUB_APP_ID: "12345",
      GITHUB_APP_PRIVATE_KEY: "unused-by-test",
    } as unknown as WorkerEnv);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("", { status: 503 }),
    );
    const request = vi.spyOn(client, "request").mockResolvedValue({
      status: 200,
      data: { object: { type: "commit", sha: SHA } },
    });

    try {
      await expect(client.publicWorkflowSha("v4")).resolves.toBe(SHA);
      expect(request).toHaveBeenCalledWith(
        "GET",
        "/repos/malsabbagh/review-sensei/git/ref/tags/v4",
        undefined,
      );
    } finally {
      fetchMock.mockRestore();
    }
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

describe("GitHubApi capability issuance", () => {
  it("accepts contents read only when the capability opts in", async () => {
    const client = api();
    const request = vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_scoped_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: {
          pull_requests: "write",
          metadata: "read",
          contents: "read",
        },
      },
    });

    await expect(
      client.capabilityToken(2468, "acme/widgets", { pull_requests: "write" }, true),
    ).resolves.toBe("ghs_scoped_token");
    expect(request).toHaveBeenCalledWith(
      "POST",
      "/app/installations/2468/access_tokens",
      "app-jwt",
      { repositories: ["widgets"], permissions: { pull_requests: "write" } },
    );
  });

  it("does not require optional contents read when GitHub omits it", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_scoped_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: { pull_requests: "write", metadata: "read" },
      },
    });

    await expect(
      client.capabilityToken(2468, "acme/widgets", { pull_requests: "write" }),
    ).resolves.toBe("ghs_scoped_token");
  });

  it("rejects implicit contents read for a capability that did not opt in", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_scoped_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: {
          pull_requests: "write",
          metadata: "read",
          contents: "read",
        },
      },
    });

    await expect(
      client.capabilityToken(2468, "acme/widgets", { pull_requests: "write" }),
    ).rejects.toThrow("github_capability_permissions_invalid");
  });

  it("rejects implicit contents read for the read-only status capability", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_scoped_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: {
          pull_requests: "read",
          metadata: "read",
          contents: "read",
        },
      },
    });

    await expect(
      client.capabilityToken(2468, "acme/widgets", { pull_requests: "read" }),
    ).rejects.toThrow("github_capability_permissions_invalid");
  });

  it("rejects a missing metadata grant when metadata was requested explicitly", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_scoped_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: { pull_requests: "write" },
      },
    });

    await expect(
      client.capabilityToken(2468, "acme/widgets", {
        pull_requests: "write",
        metadata: "read",
      }),
    ).rejects.toThrow("github_capability_permissions_invalid");
  });

  it("rejects a downgraded requested contents grant even when returned contents read is accepted", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_scoped_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: {
          pull_requests: "write",
          contents: "read",
          metadata: "read",
        },
      },
    });

    await expect(
      client.capabilityToken(
        2468,
        "acme/widgets",
        { pull_requests: "write", contents: "write" },
        true,
      ),
    ).rejects.toThrow("github_capability_permissions_invalid");
  });

  it.each([
    ["an inherited writable capability", { pull_requests: "write", contents: "write", metadata: "read" }],
    ["an unrecognized read capability", { pull_requests: "write", checks: "read", metadata: "read" }],
    ["a hyphenated lookalike permission", { "pull-requests": "write", metadata: "read" }],
    ["a missing mandatory metadata grant", { pull_requests: "write" }],
    ["an unexpected writable implicit capability", {
      pull_requests: "write",
      metadata: "read",
      contents: "write",
    }],
  ])("rejects %s instead of issuing an over-scoped token", async (_name, permissions) => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_scoped_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions,
      },
    });

    await expect(
      client.capabilityToken(2468, "acme/widgets", { pull_requests: "write" }),
    ).rejects.toThrow("github_capability_permissions_invalid");
  });

  it("uses an installation token for private repository metadata", async () => {
    const client = api();
    const request = vi.spyOn(client, "request").mockResolvedValue({
      status: 200,
      data: { id: 987654321, fork: false },
    });

    await expect(client.repositoryInfo("acme/widgets", "ghs_metadata_token")).resolves.toEqual({
      id: 987654321,
      fork: false,
    });
    expect(request).toHaveBeenCalledWith(
      "GET",
      "/repos/acme/widgets",
      "ghs_metadata_token",
    );
  });

  it("includes the upstream HTTP status in capability failures", async () => {
    const client = api();
    const request = vi.spyOn(client, "request").mockResolvedValue({
      status: 422,
      data: { message: "validation failed" },
    });

    await expect(
      client.capabilityToken(2468, "acme/widgets", { pull_requests: "write" }),
    ).rejects.toThrow("github_capability_issue_failed_422");
    expect(request).toHaveBeenCalledWith(
      "POST",
      "/app/installations/2468/access_tokens",
      "app-jwt",
      { repositories: ["widgets"], permissions: { pull_requests: "write" } },
    );
  });

  it("classifies a successful response with an invalid body separately", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({ status: 201, data: null });

    await expect(
      client.capabilityToken(2468, "acme/widgets", { pull_requests: "write" }),
    ).rejects.toThrow("github_capability_response_invalid");
  });

  it("rejects malformed or over-scoped installation-token permissions", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_metadata_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: { metadata: "read", contents: "write" },
      },
    });

    await expect(
      client.installationToken(2468, "acme/widgets", { metadata: "read" }),
    ).rejects.toThrow("github_installation_permissions_invalid");
  });

  it("requires GitHub's mandatory metadata grant on installation-token responses", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_setup_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: {
          contents: "write",
          pull_requests: "write",
          variables: "write",
          workflows: "write",
        },
      },
    });

    await expect(
      client.installationToken(2468, "acme/widgets", {
        contents: "write",
        pull_requests: "write",
        variables: "write",
        workflows: "write",
      }),
    ).rejects.toThrow("github_installation_permissions_invalid");
  });

  it("fails closed when GitHub returns both aliases for one permission", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_metadata_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: {
          metadata: "read",
          variables: "read",
          actions_variables: "read",
        },
      },
    });

    await expect(
      client.installationToken(2468, "acme/widgets", {
        metadata: "read",
        variables: "read",
      }),
    ).rejects.toThrow("github_installation_permissions_invalid");
  });

  it("accepts the controlled setup Variables alias only as the requested Variables scope", async () => {
    const client = api();
    vi.spyOn(client, "request").mockResolvedValue({
      status: 201,
      data: {
        token: "ghs_setup_token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        permissions: {
          metadata: "read",
          actions_variables: "write",
        },
      },
    });

    await expect(
      client.installationToken(2468, "acme/widgets", {
        metadata: "read",
        actions_variables: "write",
      }),
    ).resolves.toMatchObject({ permissions: { metadata: "read", variables: "write" } });
  });

  it("validates repository identity before requesting an installation token", async () => {
    const client = api();
    const request = vi.spyOn(client, "request");

    await expect(
      client.capabilityToken(2468, "acme/widgets/other", { pull_requests: "write" }),
    ).rejects.toThrow("github_repository_invalid");
    expect(request).not.toHaveBeenCalled();
  });
});
