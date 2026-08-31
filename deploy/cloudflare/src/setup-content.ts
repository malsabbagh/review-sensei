/**
 * Generated customer setup-v4 files.
 *
 * The Worker receives the public reusable-workflow git tag as the install-time
 * update channel and resolves it to the configured immutable commit before
 * generating these files. The customer-owned OLLAMA_API_KEY is referenced by
 * name in the caller and passed to the workflow; this module never handles its
 * value.
 */

const GITHUB_EXPRESSION = "@@";
const PUBLIC_SHA_PATTERN = /^[a-f0-9]{40}$/;
const PUBLIC_TAG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const LEGACY_V3_SOURCE_INPUTS = [
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
].join("\n") + "\n";
const LEGACY_V3_UNINSTALL_BODY =
  "Remove the ReviewSensei workflow, cleanup workflow, and generated configuration. " +
  "ReviewSensei learnings and repository secrets are left untouched.";

export const SETUP_VERSION = 4;
export const SETUP_VERSION_MARKER = `ReviewSensei setup version: ${SETUP_VERSION}`;
export const DEFAULT_PUBLIC_WORKFLOW_TAG = "v4";
export const DEFAULT_PUBLIC_WORKFLOW_SHA = "f".repeat(40);
export const DEFAULT_PROVIDER_MODE = "local";
export const DEFAULT_LOCAL_MODEL = "qwen3.5:4b";
export const DEFAULT_CLOUD_MODEL = "deepseek-v4-flash:cloud";

export interface SetupFile {
  path: string;
  content: string;
}

export interface SetupVariable {
  readonly name: string;
  readonly value: string;
}

export const SETUP_FILE_PATHS: readonly string[] = [
  ".github/workflows/review-sensei-review.yml",
  ".github/workflows/review-sensei-uninstall.yml",
  ".github/review-sensei/config.yml",
];

export const SETUP_VARIABLES: readonly SetupVariable[] = [
  { name: "REVIEWSENSEI_PROVIDER_MODE", value: DEFAULT_PROVIDER_MODE },
  { name: "REVIEWSENSEI_LOCAL_MODEL", value: DEFAULT_LOCAL_MODEL },
  { name: "REVIEWSENSEI_CLOUD_MODEL", value: DEFAULT_CLOUD_MODEL },
  { name: "REVIEWSENSEI_VERSION", value: "0.1.0" },
  { name: "REVIEWSENSEI_AUTO_REVIEW", value: "false" },
  { name: "REVIEWSENSEI_GITHUB_WRITES", value: "false" },
  { name: "REVIEWSENSEI_LEARNING_PRS", value: "false" },
  { name: "REVIEWSENSEI_MENTION_REPLIES", value: "false" },
  { name: "REVIEWSENSEI_UPLOAD_ARTIFACTS", value: "false" },
];

export function validatePublicWorkflowSha(value: string): string {
  if (
    typeof value !== "string" ||
    /[\r\n]/.test(value) ||
    !PUBLIC_SHA_PATTERN.test(value)
  ) {
    throw new Error(
      "PUBLIC_WORKFLOW_SHA must be exactly 40 lowercase hexadecimal characters",
    );
  }
  return value;
}

export function validatePublicWorkflowTag(value: string): string {
  if (
    typeof value !== "string" ||
    /[\r\n]/.test(value) ||
    !PUBLIC_TAG_PATTERN.test(value) ||
    value.includes("..") ||
    value.endsWith(".") ||
    value.endsWith(".lock")
  ) {
    throw new Error("PUBLIC_WORKFLOW_TAG must be a valid single-segment git tag");
  }
  return value;
}

function workflowTemplate(publicWorkflowSha: string): string {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return String.raw`# ReviewSensei setup version: 3
name: ReviewSensei review

on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]
  workflow_dispatch:
    inputs:
      operation:
        description: Review the selected pull request
        required: true
        default: review
        type: choice
        options: [review]
      base_ref:
        description: Repository default branch (must match the repository setting)
        required: true
      head_ref:
        description: Head branch or ref to review
        required: true
      head_repository:
        description: Optional owner/repo slug for fork review
        required: false
      pull_request_number:
        description: Pull request number to review for manual dispatch
        required: true
      head_sha:
        description: Exact pull request head commit SHA
        required: true
      base_sha:
        description: Exact reviewed base commit SHA
        required: false
      review_sensei_version:
        description: Exact ReviewSensei package version (X.Y.Z or vX.Y.Z)
        required: true
      source_kind:
        description: Source kind for manual dispatch (issue or inline)
        required: false
      source_comment_id:
        description: Source comment ID for manual reply
        required: false
      source_updated_at:
        description: Timestamp of source comment
        required: false
      root_comment_id:
        description: Root comment ID for manual reply thread
        required: false
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: read
  issues: read
  id-token: write

jobs:
  automatic-cloud-review:
    if: >-
      github.event_name == 'pull_request' &&
      vars.REVIEWSENSEI_AUTO_REVIEW == 'true' &&
      vars.REVIEWSENSEI_GITHUB_WRITES == 'true' &&
      vars.REVIEWSENSEI_PROVIDER_MODE == 'cloud' &&
      github.event.pull_request.head.repo.full_name == github.repository
    uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@__PUBLIC_WORKFLOW_SHA__
    with:
      mode: automatic
      operation: review
      repository: @@{{ github.repository }}
      repository_id: @@{{ github.repository_id }}
      pull_request_number: @@{{ github.event.pull_request.number }}
      base_ref: @@{{ github.event.pull_request.base.ref }}
      base_sha: @@{{ github.event.pull_request.base.sha }}
      head_ref: @@{{ github.event.pull_request.head.ref }}
      head_repository: @@{{ github.event.pull_request.head.repo.full_name }}
      head_sha: @@{{ github.event.pull_request.head.sha }}
      review_sensei_version: @@{{ vars.REVIEWSENSEI_VERSION }}
      enable_review: @@{{ vars.REVIEWSENSEI_AUTO_REVIEW }}
      enable_github_writes: @@{{ vars.REVIEWSENSEI_GITHUB_WRITES }}
      enable_learning_prs: @@{{ vars.REVIEWSENSEI_LEARNING_PRS }}
      enable_mention_replies: @@{{ vars.REVIEWSENSEI_MENTION_REPLIES }}
      upload_artifacts: @@{{ vars.REVIEWSENSEI_UPLOAD_ARTIFACTS }}
    secrets:
      OLLAMA_API_KEY: @@{{ secrets.OLLAMA_API_KEY }}

  trusted-local-manual:
    if: >-
      (github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'issue_comment' &&
      github.event.action == 'created' &&
      github.event.issue.pull_request &&
      contains(github.event.comment.body, '@sensei') &&
      (github.event.comment.author_association == 'OWNER' ||
      github.event.comment.author_association == 'MEMBER' ||
      github.event.comment.author_association == 'COLLABORATOR') &&
      github.event.comment.user.type != 'Bot') ||
      (github.event_name == 'pull_request_review_comment' &&
      github.event.action == 'created' &&
      contains(github.event.comment.body, '@sensei') &&
      (github.event.comment.author_association == 'OWNER' ||
      github.event.comment.author_association == 'MEMBER' ||
      github.event.comment.author_association == 'COLLABORATOR') &&
      github.event.comment.user.type != 'Bot')) &&
      vars.REVIEWSENSEI_PROVIDER_MODE != 'cloud'
    uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@__PUBLIC_WORKFLOW_SHA__
    with:
      mode: manual
      operation: @@{{ inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || 'reply' }}
      repository: @@{{ github.repository }}
      repository_id: @@{{ github.repository_id }}
      pull_request_number: @@{{ inputs.pull_request_number || github.event.issue.number || github.event.pull_request.number }}
      base_ref: @@{{ inputs.base_ref || github.event.pull_request.base.ref || github.event.repository.default_branch }}
      base_sha: @@{{ inputs.base_sha || github.event.pull_request.base.sha }}
      head_ref: @@{{ inputs.head_ref || '' }}
      head_repository: @@{{ inputs.head_repository || github.event.pull_request.head.repo.full_name || github.repository }}
      head_sha: @@{{ inputs.head_sha || github.event.pull_request.head.sha }}
      source_kind: @@{{ inputs.source_kind || (github.event_name == 'pull_request_review_comment' && 'inline') || 'issue' }}
      source_comment_id: @@{{ inputs.source_comment_id || github.event.comment.id }}
      source_updated_at: @@{{ inputs.source_updated_at || github.event.comment.updated_at }}
      root_comment_id: @@{{ inputs.root_comment_id || github.event.comment.in_reply_to_id || github.event.comment.id }}
      review_sensei_version: @@{{ inputs.review_sensei_version || vars.REVIEWSENSEI_VERSION }}
      enable_review: @@{{ (inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || 'reply') == 'review' && vars.REVIEWSENSEI_AUTO_REVIEW || 'false' }}
      enable_github_writes: @@{{ vars.REVIEWSENSEI_GITHUB_WRITES }}
      enable_learning_prs: @@{{ vars.REVIEWSENSEI_LEARNING_PRS }}
      enable_mention_replies: @@{{ vars.REVIEWSENSEI_MENTION_REPLIES }}
      upload_artifacts: @@{{ vars.REVIEWSENSEI_UPLOAD_ARTIFACTS }}
    secrets:
      OLLAMA_API_KEY: @@{{ secrets.OLLAMA_API_KEY }}
`.replaceAll("__PUBLIC_WORKFLOW_SHA__", sha).replaceAll(GITHUB_EXPRESSION, "$");
}

function taggedWorkflowTemplate(publicWorkflowTag: string): string {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return workflowTemplate(DEFAULT_PUBLIC_WORKFLOW_SHA)
    .replaceAll(
      `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${DEFAULT_PUBLIC_WORKFLOW_SHA}`,
      `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${tag}`,
    )
    .replace("# ReviewSensei setup version: 3", "# ReviewSensei setup version: 4");
}

function pinnedV4WorkflowTemplate(publicWorkflowSha: string): string {
  return workflowTemplate(publicWorkflowSha).replace(
    "# ReviewSensei setup version: 3",
    "# ReviewSensei setup version: 4",
  );
}

function historicalV3WorkflowTemplate(publicWorkflowSha: string): string {
  return workflowTemplate(publicWorkflowSha)
    .replace(LEGACY_V3_SOURCE_INPUTS, "")
    .replace("  trusted-local-manual:\n", "  manual-or-trusted-local:\n")
    .replace(
      "      head_ref: ${{ inputs.head_ref || '' }}\n",
      "      head_ref: ${{ inputs.head_ref || github.event.pull_request.head.ref || '' }}\n",
    );
}

function uninstallWorkflowTemplate(): string {
  return String.raw`# ReviewSensei setup version: 3
name: Remove ReviewSensei setup

on:
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - name: Check out the default branch
        uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
        with:
          ref: @@{{ github.event.repository.default_branch }}
          fetch-depth: 0

      - name: Create cleanup pull request
        env:
          GH_TOKEN: @@{{ github.token }}
          DEFAULT_BRANCH: @@{{ github.event.repository.default_branch }}
          RUN_ID: @@{{ github.run_id }}
        run: |
          python - <<'PY'
          import os
          import re
          import subprocess
          import sys

          default_branch = os.environ.get("DEFAULT_BRANCH", "")
          run_id = os.environ.get("RUN_ID", "")
          if not re.fullmatch(r"[A-Za-z0-9._/-]+", default_branch):
              print("::error::repository default branch is unavailable", file=sys.stderr)
              sys.exit(1)
          if not re.fullmatch(r"[0-9]+", run_id):
              print("::error::workflow run id is unavailable", file=sys.stderr)
              sys.exit(1)
          branch = f"review-sensei/uninstall-{run_id}"
          paths = (
              ".github/workflows/review-sensei-review.yml",
              ".github/workflows/review-sensei-uninstall.yml",
              ".github/review-sensei/config.yml",
          )
          subprocess.run(["git", "switch", "--create", branch], check=True)
          for path in paths:
              subprocess.run(["git", "rm", "--force", "--ignore-unmatch", "--", path], check=True)
          if subprocess.run(["git", "diff", "--cached", "--quiet", "--", *paths]).returncode == 0:
              print("ReviewSensei setup files are already absent; no cleanup PR is needed.")
              sys.exit(0)
          subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
          subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
          subprocess.run(["git", "commit", "-m", "Remove ReviewSensei setup"], check=True)
          subprocess.run(["git", "push", "--set-upstream", "origin", branch], check=True)
          subprocess.run([
              "gh", "pr", "create", "--base", default_branch, "--head", branch,
              "--title", "Remove ReviewSensei setup",
              "--body", "Remove generated ReviewSensei setup files; learnings and secrets remain untouched.",
          ], check=True)
          PY
`.replaceAll(GITHUB_EXPRESSION, "$");
}

function historicalV3UninstallWorkflowTemplate(): string {
  return uninstallWorkflowTemplate().replace(
    '              "--body", "Remove generated ReviewSensei setup files; ' +
      'learnings and secrets remain untouched.",\n',
    `              "--body", "${LEGACY_V3_UNINSTALL_BODY}",\n`,
  );
}

function configFile(version: number): string {
  return (
    `# ReviewSensei setup version: ${version}\n` +
    `setup_version: ${version}\n` +
    "provider: ollama\n" +
    "provider_mode: local\n" +
    "base_url: http://127.0.0.1:11434/api\n" +
    "cloud_base_url: https://ollama.com/api\n" +
    `local_model: ${DEFAULT_LOCAL_MODEL}\n` +
    `cloud_model: ${DEFAULT_CLOUD_MODEL}\n` +
    "version: 0.1.0\n" +
    "auto_review: false\n" +
    "github_writes: false\n" +
    "learning_prs: false\n" +
    "mention_replies: false\n" +
    "upload_artifacts: false\n"
  );
}

export function buildSetupFiles(publicWorkflowSha: string): readonly SetupFile[] {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return [
    { path: SETUP_FILE_PATHS[0], content: pinnedV4WorkflowTemplate(sha) },
    {
      path: SETUP_FILE_PATHS[1],
      content: uninstallWorkflowTemplate().replace(
        "# ReviewSensei setup version: 3",
        "# ReviewSensei setup version: 4",
      ),
    },
    { path: SETUP_FILE_PATHS[2], content: configFile(SETUP_VERSION) },
  ];
}

/** Setup-v4 output from the previous tag-following contract, for migration only. */
export function buildTaggedV4SetupFiles(
  publicWorkflowTag: string,
): readonly SetupFile[] {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return [
    { path: SETUP_FILE_PATHS[0], content: taggedWorkflowTemplate(tag) },
    {
      path: SETUP_FILE_PATHS[1],
      content: uninstallWorkflowTemplate().replace(
        "# ReviewSensei setup version: 3",
        "# ReviewSensei setup version: 4",
      ),
    },
    { path: SETUP_FILE_PATHS[2], content: configFile(SETUP_VERSION) },
  ];
}

export function buildHistoricalV3SetupFiles(
  publicWorkflowSha: string,
): readonly SetupFile[] {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return [
    { path: SETUP_FILE_PATHS[0], content: historicalV3WorkflowTemplate(sha) },
    {
      path: SETUP_FILE_PATHS[1],
      content: historicalV3UninstallWorkflowTemplate(),
    },
    { path: SETUP_FILE_PATHS[2], content: configFile(3) },
  ];
}

/** Exact current setup-v3 output retained only for migration recognition. */
export function buildCurrentV3SetupFiles(
  publicWorkflowSha: string,
): readonly SetupFile[] {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return [
    { path: SETUP_FILE_PATHS[0], content: workflowTemplate(sha) },
    { path: SETUP_FILE_PATHS[1], content: uninstallWorkflowTemplate() },
    { path: SETUP_FILE_PATHS[2], content: configFile(3) },
  ];
}

export const SETUP_FILES: readonly SetupFile[] = buildSetupFiles(
  DEFAULT_PUBLIC_WORKFLOW_SHA,
);

export const SETUP_PULL_REQUEST_TITLE = "ReviewSensei review setup";

export const SETUP_PULL_REQUEST_BODY =
  "This pull request adds or updates the ReviewSensei setup-v4 caller, which " +
  "pins the public reusable workflow to the SHA resolved from the configured " +
  "v4 git tag at installation time. " +
  "Automatic pull-request review is cloud-provider-only on GitHub-hosted compute; " +
  "local Ollama remains manual or trusted-event-only. All write and artifact " +
  "switches default to false. Cloud mode passes the existing customer-owned " +
  "OLLAMA_API_KEY secret by name only; the App never creates or reads its value. " +
  "Migration changes only these generated paths through a reviewable PR and " +
  "never overwrites custom or future setup files.";
