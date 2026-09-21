import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  RELEASED_RUNNER_SWITCH_V4_SHA256,
  releasedRunnerSwitchV4CallerBytes,
} from "../src/released-runner-switch-v4-caller";
import {
  BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS,
  DEFAULT_PUBLIC_WORKFLOW_TAG,
  SETUP_VARIABLES,
  SETUP_VERSION,
  brokerAcceptedPublicWorkflowTags,
  buildHistoricalProviderParityV4SetupFiles,
  buildHistoricalTaggedV4SetupFiles,
  buildTaggedV4SetupFiles,
  buildSetupFiles,
  publicWorkflowTagFromJobRef,
  releasedRunnerSwitchV4WorkflowTemplate,
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
    expect(validatePublicWorkflowTag(DEFAULT_PUBLIC_WORKFLOW_TAG)).toBe("v5");
  });

  it("accepts both migration tags in the broker policy helper", () => {
    expect(BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS).toEqual(["v4", "v5"]);
    expect(brokerAcceptedPublicWorkflowTags("v4")).toEqual(["v4", "v5"]);
    expect(brokerAcceptedPublicWorkflowTags("v5")).toEqual(["v5", "v4"]);
  });

  it("parses the public workflow tag from OIDC job refs", () => {
    expect(
      publicWorkflowTagFromJobRef(
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
      ),
    ).toBe("v5");
    expect(
      publicWorkflowTagFromJobRef(
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
      ),
    ).toBeNull();
  });

  it("generates fork-safe callers at the supplied public workflow tag", () => {
    const tag = "stable";
    const workflow = buildSetupFiles(tag)[0].content;
    expect(SETUP_VERSION).toBe(4);
    expect(SETUP_VARIABLES).toContainEqual(
      expect.objectContaining({ name: "REVIEWSENSEI_AUTO_APPROVE", value: "true" }),
    );
    expect(workflow).toContain("opened, reopened, synchronize, ready_for_review");
    expect(workflow).toContain(
      `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${tag}`,
    );
    expect(workflow).toContain("head.repo.full_name == github.repository");
    expect(workflow).toContain("github.event.issue.pull_request");
    expect(workflow).toContain("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}");
    expect(workflow).toContain(
      "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}",
    );
    expect(workflow).toContain("model: ${{ vars.REVIEWSENSEI_MODEL || '' }}");
    expect(SETUP_VARIABLES).toContainEqual(
      expect.objectContaining({ name: "REVIEWSENSEI_MODEL", value: "" }),
    );
    expect(workflow).toContain("REVIEWSENSEI_GITHUB_WRITES == 'true'");
    expect(workflow).toContain("enable_auto_approve");
    expect(workflow).toContain(
      "enable_auto_approve: ${{ vars.REVIEWSENSEI_AUTO_APPROVE || 'true' }}",
    );
    expect(workflow).toContain("source_kind:\n        description: Source kind for manual dispatch");
    expect(workflow).toContain("root_comment_id:\n        description: Root comment ID for manual reply thread");
    expect(workflow).not.toContain("GITHUB_APP_PRIVATE_KEY");
  });

  it("forwards command context and routes the command operation for the generated caller", () => {
    const workflow = buildTaggedV4SetupFiles("v5")[0].content;
    const forwarding: ReadonlyArray<readonly [string, string]> = [
      ["comment_body", "github.event.comment.body || ''"],
      ["comment_actor", "github.event.comment.user.login || ''"],
      ["comment_actor_type", "github.event.comment.user.type || 'User'"],
      ["comment_association", "github.event.comment.author_association || ''"],
    ];
    for (const [name, expression] of forwarding) {
      const matches = workflow
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.startsWith(name + ": "));
      expect(matches).toEqual([name + ": ${{ " + expression + " }}"]);
    }
    expect(workflow).toContain(
      "      (needs.resolve-trigger.outputs.operation == 'command' ||\n" +
        "      (vars.REVIEWSENSEI_MENTION_REPLIES == 'true' &&\n",
    );
    // Rendering must collapse every @@{{ }} escape and leave the raw ${{ }}
    // in the trigger guard untouched.
    expect(workflow).not.toContain("@@");
  });

  it("does not invent a SHA-based concurrency key; hosted reviews use the reusable workflow", () => {
    const workflow = buildTaggedV4SetupFiles("v5")[0].content;
    expect(workflow).toContain(
      "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
    );
    expect(workflow).not.toMatch(/^\s*group:.*head_sha/m);
    expect(workflow).not.toContain("source_comment_id || head_sha");
    expect(workflow).not.toContain("head_sha || head_ref || run_id");
    expect(workflow).not.toContain("head_sha || github.run_id");
  });

  it("loads the released runner-switch caller through the shipping module", () => {
    const caller = releasedRunnerSwitchV4CallerBytes();
    expect(
      createHash("sha256").update(caller).digest("hex"),
    ).toBe(RELEASED_RUNNER_SWITCH_V4_SHA256);
    expect(releasedRunnerSwitchV4WorkflowTemplate("v4")).toBe(caller);
    expect(releasedRunnerSwitchV4WorkflowTemplate("stable")).toBe(
      caller.replaceAll(
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v4",
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@stable",
      ),
    );
  });

  it("matches the canonical repository fixture bytes", () => {
    const canonical = readFileSync(
      new URL(
        "../../../tests/fixtures/setup-legacy/released-v4-resolve-trigger-runner-switch.yml",
        import.meta.url,
      ),
      "utf8",
    );
    expect(releasedRunnerSwitchV4CallerBytes()).toBe(canonical);
  });

  it("generates one provider-neutral reusable job with the supplied tag", () => {
    const tag = "stable";
    const workflow = buildTaggedV4SetupFiles(tag)[0].content;
    const workflowPattern = new RegExp(`review-sensei-run\\.yml@${tag}`, "g");
    expect(workflow.match(workflowPattern)).toHaveLength(1);
    expect(workflow).toContain("# ReviewSensei setup version: 4");
    expect(workflow).toContain("pull-requests: write");
    expect(workflow).toContain("issues: write");
    expect(workflow).toContain("github.event.pull_request.draft != true");
    expect(workflow).toContain("provider_mode: ${{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}");
    expect(workflow).toContain("resolve-trigger:");
    expect(workflow).toContain(
      "operation: ${{ needs.resolve-trigger.outputs.operation }}",
    );
    expect(workflow).not.toContain(
      "operation: ${{ inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || 'reply' }}",
    );
    expect(workflow).not.toContain("vars.REVIEWSENSEI_PROVIDER_MODE != 'cloud'");
    expect(workflow).not.toContain("vars.REVIEWSENSEI_PROVIDER_MODE == 'cloud'");
    expect(workflow).not.toContain("default: main");
    expect(buildTaggedV4SetupFiles(tag)[2].content).not.toContain("auto_approve");
    expect(buildTaggedV4SetupFiles(tag)[2].content).toContain("learning_proposals: false");
    expect(buildTaggedV4SetupFiles(tag)[2].content).toContain("model: ''");
    expect(buildHistoricalTaggedV4SetupFiles(tag)[2].content).not.toContain(
      "learning_proposals",
    );
    expect(buildTaggedV4SetupFiles(tag)[2].content).toContain("version: 0.6.0");
    expect(buildHistoricalTaggedV4SetupFiles(tag)[2].content).toContain(
      "version: 0.1.1",
    );
    expect(buildHistoricalProviderParityV4SetupFiles(tag)[2].content).toContain(
      "version: 0.1.1",
    );
    expect(workflow).toBe(
      readFileSync(
        new URL("../../../examples/github-actions/review-sensei-review.yml", import.meta.url),
        "utf8",
      ).replace("@v5", `@${tag}`),
    );
  });
});
