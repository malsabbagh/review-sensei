import { describe, expect, it } from "vitest";
import {
  SETUP_VERSION,
  buildSetupFiles,
  validatePublicWorkflowSha,
} from "../src/setup-content";

describe("setup-v3 public boundary", () => {
  it("requires a full lowercase public workflow SHA", () => {
    expect(() => validatePublicWorkflowSha("")).toThrow();
    expect(() => validatePublicWorkflowSha("A".repeat(40))).toThrow();
    expect(validatePublicWorkflowSha("a".repeat(40))).toBe("a".repeat(40));
  });

  it("generates opt-in, fork-safe callers at the supplied public SHA", () => {
    const workflow = buildSetupFiles("a".repeat(40))[0].content;
    expect(SETUP_VERSION).toBe(3);
    expect(workflow).toContain("opened, reopened, synchronize, ready_for_review");
    expect(workflow).toContain(
      "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@" + "a".repeat(40),
    );
    expect(workflow).toContain("head.repo.full_name == github.repository");
    expect(workflow).toContain("github.event.issue.pull_request");
    expect(workflow).toContain("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}");
    expect(workflow).toContain("REVIEWSENSEI_GITHUB_WRITES == 'true'");
    expect(workflow).not.toContain("GITHUB_APP_PRIVATE_KEY");
  });
});
