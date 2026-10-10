import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { GitHubApi } from "../src/github-api";
import { feedbackDigest } from "../src/feedback-attestation";
import type { WorkerEnv } from "../src/env";

const vector = JSON.parse(readFileSync(new URL("../../../tests/fixtures/feedback-session-attestation.json", import.meta.url), "utf8"));
const API = "https://api.example.test";
const source = vector.selection.sources[0];

function api(): GitHubApi {
  return new GitHubApi({ GITHUB_APP_ID: "12345", GITHUB_APP_PRIVATE_KEY: "unused", GITHUB_API_URL: API } as WorkerEnv);
}
function canonicalComment(kind: "issue" | "inline" = "issue"): Record<string, unknown> {
  return {
    id: source.comment_id, updated_at: source.updated_at, body: source.body,
    user: { id: source.author_id, login: source.author, type: "User" }, author_association: source.association,
    ...(kind === "issue" ? { issue_url: `${API}/repos/acme/widgets/issues/7` } : { pull_request_url: `${API}/repos/acme/widgets/pulls/7`, in_reply_to_id: 999 }),
  };
}
async function readComment(value: unknown, kind: "issue" | "inline" = "issue") {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(value), { status: 200 }));
  try {
    const result = await api().feedbackComment("acme/widgets", 7, kind, source.comment_id, "synthetic-token");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(String(fetch.mock.calls[0]?.[0])).toBe(`${API}/repos/acme/widgets/${kind === "issue" ? "issues" : "pulls"}/comments/${source.comment_id}`);
    return result;
  } finally { fetch.mockRestore(); }
}

describe("canonical complete broker feedback reads", () => {
  it("retains the exact long Unicode body and Python digest through the real HTTP adapter", async () => {
    expect(Buffer.byteLength(source.body, "utf8")).toBeGreaterThan(4096);
    const result = await readComment(canonicalComment());
    expect(result).toEqual(source);
    expect(await feedbackDigest({ domain: "reviewsensei:feedback-event:v1", repository: "acme/widgets", pull_request: 7, trigger: { kind: "issue", comment_id: source.comment_id } })).toBe(vector.request.feedback.event_key);
    expect(await feedbackDigest(vector.selection)).toBe(vector.request.feedback.selection_digest);
  });

  it("keeps inline root identity and uses the canonical endpoint", async () => {
    const result = await readComment(canonicalComment("inline"), "inline");
    expect(result.root_comment_id).toBe(999);
    expect(result.kind).toBe("inline");
    expect(result.body).toBe(source.body);
  });

  it.each([
    ["wrong PR URL", { issue_url: `${API}/repos/acme/widgets/issues/8` }],
    ["foreign repository", { issue_url: `${API}/repos/acme/foreign/issues/7` }],
    ["wrong numeric ID", { id: source.comment_id + 1 }],
    ["string ID", { id: String(source.comment_id) }],
    ["revoked association", { author_association: "CONTRIBUTOR" }],
    ["empty body", { body: "" }],
    ["oversized body", { body: "a".repeat(65537) }],
    ["invalid Unicode body", { body: "\ud800" }],
    ["empty timestamp", { updated_at: "" }],
    ["invalid Unicode timestamp", { updated_at: "\ud800" }],
    ["oversized timestamp", { updated_at: "a".repeat(129) }],
    ["Bot actor", { user: { id: source.author_id, login: source.author, type: "Bot" } }],
    ["invalid numeric actor", { user: { id: 0, login: source.author, type: "User" } }],
    ["invalid Unicode login", { user: { id: source.author_id, login: "\ud800", type: "User" } }],
  ])("refuses %s without a second endpoint or normalized replacement", async (_name, change) => {
    await expect(readComment({ ...canonicalComment(), ...change })).rejects.toThrow("github_feedback_source_invalid");
  });

  it("accepts exactly 65536 UTF-8 bytes without slicing", async () => {
    const body = "界".repeat(21845) + "a";
    const result = await readComment({ ...canonicalComment(), body });
    expect(result.body_bytes).toBe(65536);
    expect(result.body).toBe(body);
  });

  it("preserves valid leading BOM bytes in complete bodies and metadata", async () => {
    const body = "\ufeffpreface\n@sensei Explain this.";
    const author = "\ufeffcollaborator";
    const result = await readComment({ ...canonicalComment(), body, user: { id: source.author_id, login: author, type: "User" } });
    expect(result.body).toBe(body);
    expect(result.body_bytes).toBe(32);
    expect(result.author).toBe(author);
  });

  it.each([
    ["closed", { state: "closed" }], ["draft", { draft: true }],
    ["foreign PR response", { number: 8 }], ["malformed head", { head: { sha: "short" } }],
  ])("refuses a %s snapshot", async (_name, change) => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ number: 7, state: "open", draft: false, base: { sha: "c".repeat(40) }, head: { sha: "a".repeat(40) }, ...change }), { status: 200 }));
    try {
      await expect(api().feedbackPullRequest("acme/widgets", 7, "synthetic-token")).rejects.toThrow("github_feedback_snapshot_invalid");
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally { fetch.mockRestore(); }
  });
});
