import { describe, expect, it } from "vitest";
import {
  DEFAULT_PUBLIC_WORKFLOW_TAG,
  SETUP_VERSION,
  buildTaggedV4SetupFiles,
  buildSetupFiles,
  validatePublicWorkflowTag,
  validatePublicWorkflowSha,
} from "../src/setup-content";

describe("setup-v4 public boundary", () => {
  it("requires a full lowercase public workflow SHA", () => {
    expect(() => validatePublicWorkflowSha("")).toThrow();
    expect(() => validatePublicWorkflowSha("A".repeat(40))).toThrow();
    expect(() => validatePublicWorkflowSha(`${"a".repeat(40)}\n`)).toThrow();
    expect(validatePublicWorkflowSha("a".repeat(40))).toBe("a".repeat(40));
  });

  it("requires a safe single-segment public workflow tag", () => {
    expect(() => validatePublicWorkflowTag("")).toThrow();
    expect(() => validatePublicWorkflowTag("refs/tags/v4")).toThrow();
    expect(() => validatePublicWorkflowTag("v4..next")).toThrow();
    expect(() => validatePublicWorkflowTag("v4.")).toThrow();
    expect(() => validatePublicWorkflowTag("v4.lock")).toThrow();
    expect(() => validatePublicWorkflowTag("v4\n")).toThrow();
    expect(validatePublicWorkflowTag(DEFAULT_PUBLIC_WORKFLOW_TAG)).toBe("v4");
  });

  it("generates opt-in, fork-safe callers at the supplied public workflow SHA", () => {
    const sha = "a".repeat(40);
    const workflow = buildSetupFiles(sha)[0].content;
    expect(SETUP_VERSION).toBe(4);
    expect(workflow).toContain("opened, reopened, synchronize, ready_for_review");
    expect(workflow).toContain(
      `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${sha}`,
    );
    expect(workflow).toContain("head.repo.full_name == github.repository");
    expect(workflow).toContain("github.event.issue.pull_request");
    expect(workflow).toContain("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}");
    expect(workflow).toContain("REVIEWSENSEI_GITHUB_WRITES == 'true'");
    expect(workflow).toContain("source_kind:\n        description: Source kind for manual dispatch");
    expect(workflow).toContain("root_comment_id:\n        description: Root comment ID for manual reply thread");
    expect(workflow).not.toContain("GITHUB_APP_PRIVATE_KEY");
  });

  it("keeps the prior tag-following output available for migration recognition", () => {
    const workflow = buildTaggedV4SetupFiles("stable")[0].content;
    expect(workflow.match(/review-sensei-run\.yml@stable/g)).toHaveLength(2);
    expect(workflow).toContain("# ReviewSensei setup version: 4");
  });
});
