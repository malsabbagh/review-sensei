import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { mergeFocusedV4CallerBytes } from "../src/managed-v4-recognition-artifacts";
import {
  RELEASED_RUNNER_SWITCH_V4_SHA256,
  releasedRunnerSwitchV4CallerBytes,
} from "../src/released-runner-switch-v4-caller";
import {
  BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS,
  DEFAULT_PUBLIC_WORKFLOW_TAG,
  LEGACY_SETUP_RETIRED_FIELDS,
  SETUP_VERSION,
  brokerAcceptedPublicWorkflowTags,
  buildHistoricalProviderParityV4SetupFiles,
  buildHistoricalTaggedV4SetupFiles,
  buildTaggedV4SetupFiles,
  buildSetupFiles,
  historicalV4UninstallWorkflow,
  importLegacySetupConfiguration,
  mergeFocusedV4ConfigFile,
  mergeFocusedV4WorkflowTemplate,
  publicWorkflowTagFromJobRef,
  releasedRunnerSwitchV4WorkflowTemplate,
  setupPullRequestBody,
  validatePublicWorkflowTag,
  validatePublicWorkflowSha,
} from "../src/setup-content";

const INLINE_COMMAND_PREFILTER_START =
  'len(comment_body.encode("utf-8")) <= 4096 and re.search(r"';

/**
 * Return the inline command prefilter as the generated caller's heredoc
 * evaluates it. The template stores the pattern in a Python raw string
 * literal, so the literal text is the regex source. Python's `(?m)` flag is
 * dropped and its `\Z` anchor becomes a JavaScript end-of-input `$` without
 * the `m` flag. That translation is only equivalent while the pattern uses
 * neither anchor itself, so an inner `^` or `$` is rejected below instead of
 * silently changing what the pattern matches.
 */
function inlineCommandPrefilter(text: string): string {
  const start = text.indexOf(INLINE_COMMAND_PREFILTER_START);
  if (start < 0) {
    throw new Error("caller is missing the inline command prefilter");
  }
  const patternStart = start + INLINE_COMMAND_PREFILTER_START.length;
  const end = text.indexOf('"', patternStart);
  if (end < 0) {
    throw new Error("inline command prefilter terminator is missing");
  }
  const literal = text.slice(patternStart, end);
  if (!literal.startsWith("(?m)") || !literal.endsWith("\\Z")) {
    throw new Error(
      "inline command prefilter anchors changed; update the JavaScript translation",
    );
  }
  const translated = literal.slice("(?m)".length, -"\\Z".length);
  if (/[\^$]/.test(translated)) {
    throw new Error(
      "inline command prefilter uses an inner anchor; the JavaScript translation is not equivalent",
    );
  }
  return translated + "$";
}

interface CommandParityCase {
  readonly body: string;
  readonly accepted: boolean;
}

function commandParityCases(): readonly CommandParityCase[] {
  return JSON.parse(
    readFileSync(
      new URL(
        "../../../tests/fixtures/maintainer-command-parity.json",
        import.meta.url,
      ),
      "utf8",
    ),
  ) as readonly CommandParityCase[];
}

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

  it("generates the thin caller at the supplied public workflow tag", () => {
    const tag = "stable";
    const workflow = buildSetupFiles(tag)[0].content;
    expect(SETUP_VERSION).toBe(5);
    expect(workflow).toContain("opened, reopened, synchronize, ready_for_review");
    expect(workflow).toContain(
      `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${tag}`,
    );
    // A fork head is forwarded, never admitted here: eligibility and
    // authorization belong to the reusable workflow.
    expect(workflow).toContain(
      "head_repository: ${{ inputs.head_repository || github.event.pull_request.head.repo.full_name || github.repository }}",
    );
    expect(workflow).toContain("github.event.issue.pull_request");
    expect(workflow).toContain("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}");
    expect(workflow).toContain(
      "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}",
    );
    expect(workflow).toContain("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}");
    // The caller reads no repository variable and restates no policy: the only
    // two product overrides are mapped once by the reusable workflow.
    expect(workflow).not.toContain("vars.");
    expect(workflow).not.toContain("enable_auto_approve");
    expect(workflow).not.toContain("REVIEWSENSEI_GITHUB_WRITES");
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
    // The resolver guard is event shape only: which events may start a run,
    // and which commenters may address @sensei. Nothing in it reads
    // configuration, so the reusable workflow stays the single authority on
    // eligibility, authorization, and whether anything is published.
    expect(workflow).toContain(
      "      github.event_name == 'workflow_dispatch' ||\n" +
        "      github.event_name == 'pull_request' ||\n" +
        "      (github.event_name == 'issue_comment' &&\n" +
        "      github.event.action == 'created' &&\n" +
        "      github.event.issue.pull_request &&\n" +
        "      contains(github.event.comment.body, '@sensei') &&\n" +
        "      (github.event.comment.author_association == 'OWNER' ||\n" +
        "      github.event.comment.author_association == 'MEMBER' ||\n" +
        "      github.event.comment.author_association == 'COLLABORATOR') &&\n" +
        "      github.event.comment.user.type != 'Bot') ||\n",
    );
    expect(workflow).toContain(
      "      (github.event_name == 'pull_request_review_comment' &&\n" +
        "      github.event.action == 'created' &&\n" +
        "      contains(github.event.comment.body, '@sensei') &&\n",
    );
    expect(workflow).not.toContain("vars.");
    // Rendering must collapse every @@{{ }} escape and leave the raw ${{ }}
    // in the trigger guard untouched.
    expect(workflow).not.toContain("@@");
  });

  it("matches every parser-accepted command with the Worker-generated prefilter", () => {
    const workflow = buildTaggedV4SetupFiles(DEFAULT_PUBLIC_WORKFLOW_TAG)[0].content;
    // The Worker template is the fourth copy of the command grammar. Locking
    // its literal to the checked-in example is what makes the fixture
    // assertion below cover the copy operators actually receive.
    expect(inlineCommandPrefilter(workflow)).toBe(
      inlineCommandPrefilter(
        readFileSync(
          new URL(
            "../../../examples/github-actions/review-sensei-review.yml",
            import.meta.url,
          ),
          "utf8",
        ),
      ),
    );
    const prefilter = new RegExp(inlineCommandPrefilter(workflow), "i");
    // The invariant that keeps the feature working: a command the authoritative
    // parser accepts must never fall through to a conversational reply on a
    // caller without the packaged module. The over-acceptance budget for
    // bodies the parser rejects is pinned by tests/test_github_trigger.py,
    // which owns the Python-side copies.
    const missed = commandParityCases()
      .filter((item) => item.accepted && !prefilter.test(item.body))
      .map((item) => item.body);
    expect(missed).toEqual([]);
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

  it("keeps the managed v4 recognition bytes frozen across implementations", () => {
    // The Python setup recognizer asserts these same digests
    // (tests/test_github_setup.py::MANAGED_V4_RECOGNITION_SHA256), pinning the
    // byte-exact contract for artifacts that already exist in installations.
    expect(
      createHash("sha256").update(mergeFocusedV4WorkflowTemplate("v5")).digest("hex"),
    ).toBe("ce69d43119e2573853545edf90e595cda93fc0f4a018ede87aa5b604f6ab7742");
    expect(
      createHash("sha256").update(historicalV4UninstallWorkflow()).digest("hex"),
    ).toBe("e349ede8fa3eca6a303a04d688679b1abc41d13c31ba0d10651c376e9c77a6ec");
    expect(
      createHash("sha256").update(mergeFocusedV4ConfigFile()).digest("hex"),
    ).toBe("2a81144f0c22d295b8be49474979f9fa073271b3c763da302ba4f0fcf68cefb0");
  });

  it("reads the managed v4 recognition bytes from the frozen fixtures", () => {
    // The Python setup recognizer loads these same frozen bytes from its
    // packaged fixtures (src/review_sensei/hosting/github/fixtures). The v4
    // recognizer must not derive them from the live setup-v5 templates: an
    // edit to those templates must never change what an existing installation
    // looks like.
    const fixture = (name: string) =>
      readFileSync(
        new URL(
          `../../../src/review_sensei/hosting/github/fixtures/${name}`,
          import.meta.url,
        ),
        "utf8",
      );
    const caller = fixture("merge-focused-v4-caller.yml");
    expect(mergeFocusedV4CallerBytes()).toBe(caller);
    expect(mergeFocusedV4WorkflowTemplate("v5")).toBe(caller);
    expect(mergeFocusedV4WorkflowTemplate("stable")).toBe(
      caller.replaceAll(
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@stable",
      ),
    );
    expect(mergeFocusedV4ConfigFile()).toBe(
      fixture("merge-focused-v4-config.yml"),
    );
    expect(historicalV4UninstallWorkflow()).toBe(
      fixture("historical-v4-uninstall.yml"),
    );
  });

  it("generates one provider-neutral reusable job with the supplied tag", () => {
    const tag = "stable";
    const files = buildTaggedV4SetupFiles(tag);
    const workflow = files[0].content;
    const workflowPattern = new RegExp(`review-sensei-run\\.yml@${tag}`, "g");
    expect(workflow.match(workflowPattern)).toHaveLength(1);
    expect(workflow).toContain("# ReviewSensei setup version: 5");
    // Read-only plus OIDC: the run is authorized through the broker, and the
    // reusable workflow can never exceed what this caller grants.
    expect(workflow).toContain(
      "permissions:\n" +
        "  contents: read\n" +
        "  pull-requests: read\n" +
        "  issues: read\n" +
        "  id-token: write\n",
    );
    expect(workflow).not.toContain("pull-requests: write");
    expect(workflow).not.toContain("issues: write");
    expect(workflow).toContain("resolve-trigger:");
    expect(workflow).toContain(
      "operation: ${{ needs.resolve-trigger.outputs.operation }}",
    );
    expect(workflow).not.toContain(
      "operation: ${{ inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || 'reply' }}",
    );
    expect(workflow).not.toContain("vars.");
    expect(workflow).not.toContain("default: main");
    // The generated configuration is the minimal operator-owned document:
    // every behavior field belongs to the operator copy, not to generated
    // bytes, and the package supplies the defaults.
    expect(files[2].content).toBe(
      "# ReviewSensei setup version: 5\n" +
        "schema: 1\n" +
        "\n" +
        "inference:\n" +
        "  backend: local-ollama\n",
    );
    expect(files[2].content).not.toContain("auto_approve");
    expect(files[2].content).not.toContain("learning_proposals");
    expect(files[2].content).not.toContain("model: ''");
    // The historical builders keep their frozen v4 config bytes.
    expect(buildHistoricalTaggedV4SetupFiles(tag)[2].content).not.toContain(
      "learning_proposals",
    );
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

interface LegacySetupImportCase {
  readonly name: string;
  readonly content: string;
  readonly carried: readonly string[];
  readonly notes: readonly string[];
  readonly rendered: string;
  readonly pull_request_body: string;
}

function legacySetupImportCases(): readonly LegacySetupImportCase[] {
  return (
    JSON.parse(
      readFileSync(
        new URL("../../../tests/fixtures/legacy-setup-import.json", import.meta.url),
        "utf8",
      ),
    ) as { cases: readonly LegacySetupImportCase[] }
  ).cases;
}

describe("retired setup configuration import", () => {
  it("matches the shared importer fixture the package suite pins", () => {
    // tests/fixtures/legacy-setup-import.json is asserted by this suite and by
    // tests/test_legacy_setup_import.py; the fixture bytes are the parity
    // contract between the Worker and the installed package.
    const cases = legacySetupImportCases();
    expect(cases.length).toBeGreaterThan(0);
    for (const testCase of cases) {
      const imported = importLegacySetupConfiguration(testCase.content);
      expect(imported.carried, testCase.name).toEqual(testCase.carried);
      expect(imported.notes, testCase.name).toEqual(testCase.notes);
      expect(imported.content, testCase.name).toBe(testCase.rendered);
      expect(setupPullRequestBody(imported.carried), testCase.name).toBe(
        testCase.pull_request_body,
      );
    }
  });

  it("keeps the retired replacement table aligned with the package", () => {
    // tests/test_legacy_setup_import.py compares these strings with the Python
    // RETIRED_FIELDS table, so a replacement that exists on only one side fails.
    expect(Object.keys(LEGACY_SETUP_RETIRED_FIELDS).length).toBeGreaterThan(0);
    expect(LEGACY_SETUP_RETIRED_FIELDS.review_mode).toContain("github.reviews");
    expect(LEGACY_SETUP_RETIRED_FIELDS.provider_mode).toContain("inference.backend");
  });

  it("shows the default approval policy and the import without the retired file", () => {
    const plain = setupPullRequestBody();
    expect(plain).toContain("github.reviews: auto-approve");
    expect(plain).toContain("backend choice and nothing else");
    expect(plain).not.toContain("carried these settings");
    const imported = setupPullRequestBody(["inference.backend"]);
    expect(imported).toContain(
      "carried these settings into .reviewsensei.yml: inference.backend",
    );
    expect(imported).toContain("plus the settings imported from your retired configuration");
  });
});
