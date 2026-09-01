import { readFileSync } from "node:fs";
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
  it("retains strict SHA validation for historical setup recognition", () => {
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

  it("generates opt-in, fork-safe callers at the supplied public workflow tag", () => {
    const tag = "stable";
    const workflow = buildSetupFiles(tag)[0].content;
    expect(SETUP_VERSION).toBe(4);
    expect(workflow).toContain("opened, reopened, synchronize, ready_for_review");
    expect(workflow).toContain(
      `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${tag}`,
    );
    expect(workflow).toContain("head.repo.full_name == github.repository");
    expect(workflow).toContain("github.event.issue.pull_request");
    expect(workflow).toContain("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}");
    expect(workflow).toContain("REVIEWSENSEI_GITHUB_WRITES == 'true'");
    expect(workflow).toContain("source_kind:\n        description: Source kind for manual dispatch");
    expect(workflow).toContain("root_comment_id:\n        description: Root comment ID for manual reply thread");
    expect(workflow).not.toContain("GITHUB_APP_PRIVATE_KEY");
  });

  it("generates one provider-neutral reusable job with the supplied tag", () => {
    const tag = "stable";
    const workflow = buildTaggedV4SetupFiles(tag)[0].content;
    const workflowPattern = new RegExp(`review-sensei-run\\.yml@${tag}`, "g");
    expect(workflow.match(workflowPattern)).toHaveLength(1);
    expect(workflow).toContain("# ReviewSensei setup version: 4");
    expect(workflow).toContain("provider_mode: ${{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}");
    expect(workflow).not.toContain("vars.REVIEWSENSEI_PROVIDER_MODE != 'cloud'");
    expect(workflow).not.toContain("vars.REVIEWSENSEI_PROVIDER_MODE == 'cloud'");
    expect(workflow).not.toContain("default: main");
    expect(workflow).toBe(
      readFileSync(
        new URL("../../../examples/github-actions/review-sensei-review.yml", import.meta.url),
        "utf8",
      ).replace("@v4", `@${tag}`),
    );
  });
});
