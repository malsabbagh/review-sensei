/**
 * Generated customer setup-v5 files.
 *
 * The Worker receives the public reusable-workflow git tag as the update
 * channel and validates that it resolves before generating these files. The
 * customer-owned OLLAMA_API_KEY is referenced by name in the caller and passed
 * to the workflow; this module never handles its value.
 */

import {
  historicalV4UninstallBytes,
  mergeFocusedV4CallerBytes,
  mergeFocusedV4ConfigBytes,
} from "./managed-v4-recognition-artifacts";
import { releasedRunnerSwitchV4CallerBytes } from "./released-runner-switch-v4-caller";

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

export const SETUP_VERSION = 5;
export const SETUP_VERSION_MARKER = `ReviewSensei setup version: ${SETUP_VERSION}`;
export const DEFAULT_PUBLIC_WORKFLOW_TAG = "v5";
export const PUBLIC_REPOSITORY = "malsabbagh/review-sensei";
export const PUBLIC_WORKFLOW_PATH = ".github/workflows/review-sensei-run.yml";
/** Public workflow tags the OIDC broker still accepts during channel migrations. */
export const BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS: readonly string[] = [
  "v4",
  "v5",
];
export const DEFAULT_LOCAL_MODEL = "qwen3.5:4b";
export const DEFAULT_CLOUD_MODEL = "deepseek-v4.1-flash:cloud";
/** Package version embedded in byte-exact setup-v3 recognition templates. */
export const HISTORICAL_V3_SETUP_PACKAGE_VERSION = "0.1.0";
/** Package version embedded in byte-exact setup-v4 recognition templates. */
export const HISTORICAL_V4_SETUP_PACKAGE_VERSION = "0.1.1";

export interface SetupFile {
  path: string;
  content: string;
}

export const SETUP_WORKFLOW_PATH = ".github/workflows/review-sensei-review.yml";
export const SETUP_UNINSTALL_WORKFLOW_PATH =
  ".github/workflows/review-sensei-uninstall.yml";
/**
 * The canonical configuration surface: a root file the package reads from the
 * trusted policy commit. Setup generates it once with the setup-time backend
 * choice and never rewrites it: the file belongs to the operator.
 */
export const CONFIG_PATH = ".reviewsensei.yml";
/**
 * The retired setup-v5 configuration location. Nothing reads it any more
 * (there is no dual-read), and the uninstall flow still removes it, so an
 * installation that migrates does not leave a second, silently ignored
 * configuration behind.
 */
export const LEGACY_CONFIG_PATH = ".github/review-sensei/config.yml";
export const SETUP_FILE_PATHS: readonly string[] = [
  SETUP_WORKFLOW_PATH,
  SETUP_UNINSTALL_WORKFLOW_PATH,
  CONFIG_PATH,
];
// Setup creates no behavioral repository variables. The configuration file is
// the canonical surface; the only two optional Actions overrides the product
// consumes are REVIEWSENSEI_PROVIDER and REVIEWSENSEI_MODEL, read once by the
// reusable workflow itself. Setup never creates, reads, or rewrites either one.

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

/** Return the configured channel plus any in-flight migration tags. */
export function brokerAcceptedPublicWorkflowTags(
  configuredTag: string,
): readonly string[] {
  const configured = validatePublicWorkflowTag(configuredTag);
  const accepted = new Set<string>([configured, ...BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS]);
  for (const tag of BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS) {
    validatePublicWorkflowTag(tag);
  }
  return [...accepted];
}

/** Parse the public reusable-workflow tag from an OIDC `job_workflow_ref`. */
export function publicWorkflowTagFromJobRef(jobWorkflowRef: string): string | null {
  const prefix = `${PUBLIC_REPOSITORY}/${PUBLIC_WORKFLOW_PATH}@refs/tags/`;
  if (
    typeof jobWorkflowRef !== "string" ||
    !jobWorkflowRef.startsWith(prefix) ||
    jobWorkflowRef.length <= prefix.length
  ) {
    return null;
  }
  try {
    return validatePublicWorkflowTag(jobWorkflowRef.slice(prefix.length));
  } catch {
    return null;
  }
}

function workflowTemplate(publicWorkflowRef: string): string {
  if (
    typeof publicWorkflowRef !== "string" ||
    /[\r\n]/.test(publicWorkflowRef) ||
    publicWorkflowRef.length === 0
  ) {
    throw new Error("PUBLIC_WORKFLOW_REF must be a non-empty single-line value");
  }
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
    uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@__PUBLIC_WORKFLOW_REF__
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
    uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@__PUBLIC_WORKFLOW_REF__
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
`.replaceAll("__PUBLIC_WORKFLOW_REF__", publicWorkflowRef).replaceAll(GITHUB_EXPRESSION, "$");
}

function taggedWorkflowTemplate(publicWorkflowTag: string): string {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return workflowTemplate(tag)
    .replace("# ReviewSensei setup version: 3", "# ReviewSensei setup version: 4");
}

/** Released pre-resolve-trigger setup-v4 caller. Stale managed content only. */
export function providerParityWorkflowTemplate(publicWorkflowTag: string): string {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return String.raw`# ReviewSensei setup version: 4
name: ReviewSensei review

# The installer and this example follow the operator-managed v5 git tag. Moving
# that tag is the public setup-v4 release action. The reusable workflow installs
# the requested package from PyPI first and falls back to its executing commit
# only when the package version is not yet published.
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
        description: Pull request number to review
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
      pull_request_title:
        description: Authoritative pull request title for review context
        required: false
      stages_dir:
        description: Trusted-base stage JSON directory (optional)
        required: false
      categories_dir:
        description: Trusted-base category JSON directory (optional)
        required: false
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
  pull-requests: write
  issues: write
  id-token: write

jobs:
  review-or-reply:
    if: >-
      (github.event_name == 'pull_request' &&
      vars.REVIEWSENSEI_AUTO_REVIEW == 'true' &&
      github.event.pull_request.draft != true &&
      github.event.pull_request.head.repo.full_name == github.repository) ||
      github.event_name == 'workflow_dispatch' ||
      ((github.event_name == 'issue_comment' &&
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
      vars.REVIEWSENSEI_GITHUB_WRITES == 'true' &&
      vars.REVIEWSENSEI_MENTION_REPLIES == 'true'
    uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@__PUBLIC_WORKFLOW_TAG__
    with:
      mode: @@{{ github.event_name == 'pull_request' && 'automatic' || 'manual' }}
      review_mode: @@{{ vars.REVIEWSENSEI_REVIEW_MODE || 'merge-focused' }}
      provider_mode: @@{{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}
      model: @@{{ vars.REVIEWSENSEI_MODEL || '' }}
      operation: @@{{ github.event_name == 'pull_request' && 'review' || inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || (github.event_name == 'issue_comment' && (contains(github.event.comment.body, 're-scan') || contains(github.event.comment.body, 're scan') || contains(github.event.comment.body, 'rescan')) && 'review') || 'reply' }}
      repository: @@{{ github.repository }}
      repository_id: @@{{ github.repository_id }}
      pull_request_number: @@{{ github.event_name == 'workflow_dispatch' && inputs.pull_request_number || github.event_name == 'issue_comment' && github.event.issue.number || github.event.pull_request.number }}
      base_ref: @@{{ inputs.base_ref || github.event.pull_request.base.ref || github.event.repository.default_branch }}
      base_sha: @@{{ inputs.base_sha || github.event.pull_request.base.sha }}
      head_ref: @@{{ inputs.head_ref || github.event.pull_request.head.ref || '' }}
      head_repository: @@{{ inputs.head_repository || github.event.pull_request.head.repo.full_name || github.repository }}
      head_sha: @@{{ inputs.head_sha || github.event.pull_request.head.sha }}
      source_kind: @@{{ inputs.source_kind || (github.event_name == 'pull_request_review_comment' && 'inline') || 'issue' }}
      source_comment_id: @@{{ inputs.source_comment_id || github.event.comment.id }}
      source_updated_at: @@{{ inputs.source_updated_at || github.event.comment.updated_at }}
      root_comment_id: @@{{ inputs.root_comment_id || github.event.comment.in_reply_to_id || github.event.comment.id }}
      review_sensei_version: @@{{ inputs.review_sensei_version || vars.REVIEWSENSEI_VERSION }}
      pull_request_title: @@{{ inputs.pull_request_title || github.event.pull_request.title }}
      stages_dir: @@{{ inputs.stages_dir || vars.REVIEWSENSEI_STAGES_DIR || '' }}
      categories_dir: @@{{ inputs.categories_dir || vars.REVIEWSENSEI_CATEGORIES_DIR || '' }}
      enable_review: @@{{ github.event_name == 'workflow_dispatch' && 'true' || (github.event_name == 'issue_comment' && (contains(github.event.comment.body, 're-scan') || contains(github.event.comment.body, 're scan') || contains(github.event.comment.body, 'rescan')) && 'true') || vars.REVIEWSENSEI_AUTO_REVIEW || 'false' }}
      enable_auto_approve: @@{{ vars.REVIEWSENSEI_AUTO_APPROVE || 'true' }}
      enable_learning_proposals: @@{{ vars.REVIEWSENSEI_LEARNING_PROPOSALS || 'false' }}
      enable_github_writes: @@{{ vars.REVIEWSENSEI_GITHUB_WRITES }}
      enable_learning_prs: @@{{ vars.REVIEWSENSEI_LEARNING_PRS }}
      enable_mention_replies: @@{{ vars.REVIEWSENSEI_MENTION_REPLIES }}
      upload_artifacts: @@{{ vars.REVIEWSENSEI_UPLOAD_ARTIFACTS }}
    secrets:
      OLLAMA_API_KEY: @@{{ secrets.OLLAMA_API_KEY }}
      OPENROUTER_API_KEY: @@{{ secrets.OPENROUTER_API_KEY }}
`
    .replaceAll("__PUBLIC_WORKFLOW_TAG__", tag)
    .replaceAll(GITHUB_EXPRESSION, "$");
}

/**
 * Released setup-v5 caller bytes, kept frozen for managed recognition.
 *
 * These are the bytes every existing installation carries; recognition of an
 * installed setup must not change when the current template changes.
 */
export function historicalV5WorkflowTemplate(publicWorkflowTag: string): string {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return String.raw`# ReviewSensei setup version: 5
name: ReviewSensei review
run-name: "ReviewSensei @@{{ github.event.pull_request && format('PR #{0}', github.event.pull_request.number) || 'manual' }}"

# The installer and this example follow the operator-managed v5 git tag. Moving
# that tag is the public setup-v5 release action. The reusable workflow installs
# the requested package from PyPI first and falls back to its executing commit
# only when the package version is not yet published.
#
# REVIEWSENSEI_GITHUB_WRITES also exposes the maintainer command surface:
# @sensei review status|pause|continue|reenroll, @sensei verify, and
# @sensei dismiss|defer|accept-risk run whenever github writes are enabled,
# regardless of REVIEWSENSEI_MENTION_REPLIES. That variable now only controls
# conversational @sensei replies. Commands are read from the pull-request
# conversation (issue comments); an inline review comment on a diff line
# always resolves as a conversational reply and still requires
# REVIEWSENSEI_MENTION_REPLIES=true.
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
        description: Pull request number to review
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
      pull_request_title:
        description: Authoritative pull request title for review context
        required: false
      stages_dir:
        description: Trusted-base stage JSON directory (optional)
        required: false
      categories_dir:
        description: Trusted-base category JSON directory (optional)
        required: false
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
  pull-requests: write
  issues: write
  id-token: write

jobs:
  resolve-trigger:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' &&
      vars.REVIEWSENSEI_AUTO_REVIEW == 'true' &&
      github.event.pull_request.draft != true &&
      github.event.pull_request.head.repo.full_name == github.repository) ||
      (vars.REVIEWSENSEI_GITHUB_WRITES == 'true' &&
      ((github.event_name == 'issue_comment' &&
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
      github.event.comment.user.type != 'Bot')))
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
      issues: read
    outputs:
      operation: @@{{ steps.resolve.outputs.operation }}
      head_sha: @@{{ steps.resolve.outputs.head_sha }}
      head_ref: @@{{ steps.resolve.outputs.head_ref }}
      base_ref: @@{{ steps.resolve.outputs.base_ref }}
      base_sha: @@{{ steps.resolve.outputs.base_sha }}
      pull_request_number: @@{{ steps.resolve.outputs.pull_request_number }}
      pull_request_title: @@{{ steps.resolve.outputs.pull_request_title }}
      enable_review: @@{{ steps.resolve.outputs.enable_review }}
    steps:
      - name: Check out trusted trigger resolver
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
          ref: @@{{ github.event.repository.default_branch }}

      - name: Resolve pull-request trigger metadata
        id: resolve
        env:
          GH_TOKEN: @@{{ github.token }}
          EVENT_NAME: @@{{ github.event_name }}
          REPOSITORY: @@{{ github.repository }}
          PULL_REQUEST: @@{{ github.event_name == 'workflow_dispatch' && inputs.pull_request_number || github.event_name == 'issue_comment' && github.event.issue.number || github.event.pull_request.number }}
          PULL_REQUEST_URL: @@{{ github.event.comment.pull_request_url }}
          COMMENT_BODY: @@{{ github.event.comment.body }}
          AUTO_REVIEW: @@{{ vars.REVIEWSENSEI_AUTO_REVIEW || 'false' }}
          PULL_REQUEST_JSON: @@{{ github.event_name == 'pull_request' && toJson(github.event.pull_request) || '' }}
        run: |
          set -euo pipefail
          PULL_REQUEST="@@{PULL_REQUEST:-}"
          PULL_REQUEST_URL="@@{PULL_REQUEST_URL:-}"
          COMMENT_BODY="@@{COMMENT_BODY:-}"
          if [[ -z "@@{PULL_REQUEST}" && "@@{PULL_REQUEST_URL}" =~ /pulls/([1-9][0-9]*)$ ]]; then
            PULL_REQUEST="@@{BASH_REMATCH[1]}"
          fi
          if [[ ! "@@{PULL_REQUEST}" =~ ^[1-9][0-9]*$ ]]; then
            echo "::error::pull request number is unavailable"
            exit 1
          fi
          if [[ ! "@@{REPOSITORY}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
            echo "::error::repository identity is invalid"
            exit 1
          fi
          pull_json="$RUNNER_TEMP/review-sensei-pull.json"
          if [[ "$EVENT_NAME" == "pull_request" ]]; then
            if [[ -z "@@{PULL_REQUEST_JSON}" ]]; then
              echo "::error::pull request payload is unavailable"
              exit 1
            fi
            printf '%s' "$PULL_REQUEST_JSON" > "$pull_json"
          else
            gh api --method GET "repos/@@{REPOSITORY}/pulls/@@{PULL_REQUEST}" > "$pull_json"
          fi
          resolver="src/review_sensei/hosting/github/trigger.py"
          if [[ -f "$resolver" ]]; then
            PYTHONPATH=src python "$resolver" \
              --event "$EVENT_NAME" \
              --comment-body "$COMMENT_BODY" \
              --pull-json "$pull_json" \
              --auto-review "$AUTO_REVIEW"
            exit 0
          fi
          python - "$pull_json" "$AUTO_REVIEW" "$EVENT_NAME" "$COMMENT_BODY" <<'PY'
          import json
          import os
          import re
          import sys
          from pathlib import Path

          sha_full = re.compile(r"^[a-f0-9]{40}$")
          sha_prefix = re.compile(r"^[a-f0-9]{7,39}$")
          ref = re.compile(r"^[A-Za-z0-9._/-]+$")
          rescan = re.compile(r"\bre[\s-]?scan\b", re.IGNORECASE)
          commit_sha = re.compile(r"\bcommit\s+([a-f0-9]{7,40})\b", re.IGNORECASE)
          pull = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
          auto_review = sys.argv[2]
          event_name = sys.argv[3]
          comment_body = sys.argv[4] if len(sys.argv) > 4 else ""

          def valid_ref(value):
              return (
                  isinstance(value, str)
                  and ref.fullmatch(value) is not None
                  and ".." not in value
                  and not value.startswith("/")
                  and not value.endswith("/")
                  and "//" not in value
              )

          head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
          base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
          number = pull.get("number")
          head_sha = head.get("sha")
          head_ref = head.get("ref")
          base_sha = base.get("sha")
          base_ref = base.get("ref")
          title = pull.get("title") if isinstance(pull.get("title"), str) else ""
          title = " ".join(title.split())
          if (
              isinstance(number, bool)
              or not isinstance(number, int)
              or number < 1
              or not isinstance(head_sha, str)
              or sha_full.fullmatch(head_sha) is None
              or not valid_ref(head_ref)
              or not isinstance(base_sha, str)
              or sha_full.fullmatch(base_sha) is None
              or not valid_ref(base_ref)
          ):
              print("::error::pull request identity metadata is invalid", file=sys.stderr)
              raise SystemExit(1)

          def choose_head(requested):
              if not requested:
                  return head_sha
              requested = requested.casefold()
              if sha_full.fullmatch(requested):
                  if requested != head_sha:
                      print(
                          "::error::requested head sha does not match the pull request head",
                          file=sys.stderr,
                      )
                      raise SystemExit(1)
                  return requested
              if head_sha.startswith(requested):
                  return head_sha
              print(
                  "::error::requested commit does not match the pull request head",
                  file=sys.stderr,
              )
              raise SystemExit(1)

          operation = "review"
          enable_review = "true" if auto_review == "true" else "false"
          resolved_head = head_sha
          if event_name == "pull_request":
              pass
          elif event_name == "workflow_dispatch":
              enable_review = "true"
          elif event_name == "pull_request_review_comment":
              operation = "reply"
              enable_review = "false"
          elif event_name == "issue_comment":
              wants_rescan = (
                  isinstance(comment_body, str)
                  and "@sensei" in comment_body
                  and rescan.search(comment_body) is not None
              )
              if wants_rescan:
                  enable_review = "true"
                  match = commit_sha.search(comment_body)
                  token = match.group(1).casefold() if match is not None else None
                  if token is not None and not (
                      sha_full.fullmatch(token) or sha_prefix.fullmatch(token)
                  ):
                      token = None
                  resolved_head = choose_head(token)
              elif isinstance(comment_body, str) and len(comment_body.encode("utf-8")) <= 4096 and re.search(r"(?m)(?<!\S)@sensei\s+(?:review\s+(?:status|pause|continue|reenroll)|verify|(?:dismiss|defer|accept-risk)\s+[a-f0-9]{16,64}\s+--reason\s+\S.*)\s*\Z", comment_body, re.IGNORECASE):
                  operation = "command"
                  enable_review = "false"
              else:
                  operation = "reply"
                  enable_review = "false"
          else:
              print("::error::unsupported event", file=sys.stderr)
              raise SystemExit(1)
          output = os.environ.get("GITHUB_OUTPUT")
          if not output:
              print("::error::GITHUB_OUTPUT is unavailable", file=sys.stderr)
              raise SystemExit(1)
          delimiter = "RS_PULL_REQUEST_TITLE"
          while delimiter in title:
              delimiter += "_EOF"
          with open(output, "a", encoding="utf-8") as handle:
              handle.write(f"operation={operation}\n")
              handle.write(f"head_sha={resolved_head}\n")
              handle.write(f"head_ref={head_ref}\n")
              handle.write(f"base_ref={base_ref}\n")
              handle.write(f"base_sha={base_sha}\n")
              handle.write(f"pull_request_number={number}\n")
              handle.write(f"pull_request_title<<{delimiter}\n")
              handle.write(f"{title}\n{delimiter}\n")
              handle.write(f"enable_review={enable_review}\n")
          PY

  review-or-reply:
    needs: resolve-trigger
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' &&
      vars.REVIEWSENSEI_AUTO_REVIEW == 'true' &&
      github.event.pull_request.draft != true &&
      github.event.pull_request.head.repo.full_name == github.repository) ||
      (vars.REVIEWSENSEI_GITHUB_WRITES == 'true' &&
      ((github.event_name == 'issue_comment' &&
      github.event.action == 'created' &&
      github.event.issue.pull_request &&
      contains(github.event.comment.body, '@sensei') &&
      (github.event.comment.author_association == 'OWNER' ||
      github.event.comment.author_association == 'MEMBER' ||
      github.event.comment.author_association == 'COLLABORATOR') &&
      github.event.comment.user.type != 'Bot' &&
      (needs.resolve-trigger.outputs.operation == 'command' ||
      vars.REVIEWSENSEI_MENTION_REPLIES == 'true')) ||
      (vars.REVIEWSENSEI_MENTION_REPLIES == 'true' &&
      github.event_name == 'pull_request_review_comment' &&
      github.event.action == 'created' &&
      contains(github.event.comment.body, '@sensei') &&
      (github.event.comment.author_association == 'OWNER' ||
      github.event.comment.author_association == 'MEMBER' ||
      github.event.comment.author_association == 'COLLABORATOR') &&
      github.event.comment.user.type != 'Bot')))
    uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@__PUBLIC_WORKFLOW_TAG__
    with:
      mode: @@{{ github.event_name == 'pull_request' && 'automatic' || 'manual' }}
      review_mode: @@{{ vars.REVIEWSENSEI_REVIEW_MODE || 'merge-focused' }}
      provider_mode: @@{{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}
      model: @@{{ vars.REVIEWSENSEI_MODEL || '' }}
      operation: @@{{ needs.resolve-trigger.outputs.operation }}
      repository: @@{{ github.repository }}
      repository_id: @@{{ github.repository_id }}
      pull_request_number: @@{{ needs.resolve-trigger.outputs.pull_request_number }}
      base_ref: @@{{ needs.resolve-trigger.outputs.base_ref || inputs.base_ref || github.event.pull_request.base.ref || github.event.repository.default_branch }}
      base_sha: @@{{ needs.resolve-trigger.outputs.base_sha || inputs.base_sha || github.event.pull_request.base.sha }}
      head_ref: @@{{ needs.resolve-trigger.outputs.head_ref || inputs.head_ref || github.event.pull_request.head.ref || '' }}
      head_repository: @@{{ inputs.head_repository || github.event.pull_request.head.repo.full_name || github.repository }}
      head_sha: @@{{ needs.resolve-trigger.outputs.head_sha || inputs.head_sha || github.event.pull_request.head.sha }}
      source_kind: @@{{ inputs.source_kind || (github.event_name == 'pull_request_review_comment' && 'inline') || 'issue' }}
      source_comment_id: @@{{ inputs.source_comment_id || github.event.comment.id }}
      source_updated_at: @@{{ inputs.source_updated_at || github.event.comment.updated_at }}
      root_comment_id: @@{{ inputs.root_comment_id || github.event.comment.in_reply_to_id || github.event.comment.id }}
      comment_body: @@{{ github.event.comment.body || '' }}
      comment_actor: @@{{ github.event.comment.user.login || '' }}
      comment_actor_type: @@{{ github.event.comment.user.type || 'User' }}
      comment_association: @@{{ github.event.comment.author_association || '' }}
      review_sensei_version: @@{{ inputs.review_sensei_version || vars.REVIEWSENSEI_VERSION }}
      pull_request_title: @@{{ needs.resolve-trigger.outputs.pull_request_title || inputs.pull_request_title || github.event.pull_request.title }}
      stages_dir: @@{{ inputs.stages_dir || vars.REVIEWSENSEI_STAGES_DIR || '' }}
      categories_dir: @@{{ inputs.categories_dir || vars.REVIEWSENSEI_CATEGORIES_DIR || '' }}
      enable_review: @@{{ needs.resolve-trigger.outputs.enable_review == 'true' && 'true' || 'false' }}
      enable_auto_approve: @@{{ vars.REVIEWSENSEI_AUTO_APPROVE || 'true' }}
      enable_learning_proposals: @@{{ vars.REVIEWSENSEI_LEARNING_PROPOSALS || 'false' }}
      enable_github_writes: @@{{ vars.REVIEWSENSEI_GITHUB_WRITES }}
      enable_learning_prs: @@{{ vars.REVIEWSENSEI_LEARNING_PRS }}
      enable_mention_replies: @@{{ vars.REVIEWSENSEI_MENTION_REPLIES }}
      upload_artifacts: @@{{ vars.REVIEWSENSEI_UPLOAD_ARTIFACTS }}
    secrets:
      OLLAMA_API_KEY: @@{{ secrets.OLLAMA_API_KEY }}
      OPENROUTER_API_KEY: @@{{ secrets.OPENROUTER_API_KEY }}
`
    .replaceAll("__PUBLIC_WORKFLOW_TAG__", tag)
    .replaceAll(GITHUB_EXPRESSION, "$");
}

/**
 * Current setup-v5 caller: invocation-only.
 *
 * The caller resolves the event into the reusable workflow's declared inputs,
 * states the read-only permissions and the optional OIDC scope that workflow
 * needs, and names the three optional provider secrets. Backend, model,
 * endpoint, credential, and every github.* policy decision come from
 * .reviewsensei.yml on the trusted policy commit, so this file reads no
 * repository variables and carries no policy expression.
 */
function resolveTriggerWorkflowTemplate(publicWorkflowTag: string): string {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return String.raw`# ReviewSensei setup version: 5
name: ReviewSensei review
run-name: "ReviewSensei @@{{ github.event.pull_request && format('PR #{0}', github.event.pull_request.number) || 'manual' }}"

# Configuration lives in .reviewsensei.yml at the repository root of the
# trusted policy commit (the repository default branch) that the reusable
# workflow freezes. This caller reads no repository variables: the only two
# product overrides are REVIEWSENSEI_PROVIDER (inference.backend) and
# REVIEWSENSEI_MODEL (inference.model), and the reusable workflow maps them,
# once, from configuration.
#
# The installer and this caller follow the operator-managed v5 git tag. Moving
# that tag is the public setup-v5 release action, so the reusable workflow,
# its exact package install, and every policy decision arrive without editing
# this file.
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
      pull_request_number:
        description: Pull request number to review
        required: true
      head_repository:
        description: Optional owner/repo slug for fork review
        required: false
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

# Read-only plus OIDC. The reusable workflow needs no write scope from this
# repository - every publication is authorized by the broker - and it can
# never exceed what this caller grants. Whether anything is published is
# github.writes in .reviewsensei.yml, not a permission.
permissions:
  contents: read
  pull-requests: read
  issues: read
  id-token: write

jobs:
  resolve-trigger:
    # Event shape only: which events may start a run, and which commenters may
    # address @sensei. Nothing here depends on configuration.
    if: >-
      github.event_name == 'workflow_dispatch' ||
      github.event_name == 'pull_request' ||
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
      github.event.comment.user.type != 'Bot')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
      issues: read
    outputs:
      operation: @@{{ steps.resolve.outputs.operation }}
      head_sha: @@{{ steps.resolve.outputs.head_sha }}
      head_ref: @@{{ steps.resolve.outputs.head_ref }}
      base_ref: @@{{ steps.resolve.outputs.base_ref }}
      base_sha: @@{{ steps.resolve.outputs.base_sha }}
      pull_request_number: @@{{ steps.resolve.outputs.pull_request_number }}
    steps:
      - name: Check out trusted trigger resolver
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
          ref: @@{{ github.event.repository.default_branch }}

      - name: Resolve pull-request trigger metadata
        id: resolve
        env:
          GH_TOKEN: @@{{ github.token }}
          EVENT_NAME: @@{{ github.event_name }}
          REPOSITORY: @@{{ github.repository }}
          PULL_REQUEST: @@{{ github.event_name == 'workflow_dispatch' && inputs.pull_request_number || github.event_name == 'issue_comment' && github.event.issue.number || github.event.pull_request.number }}
          PULL_REQUEST_URL: @@{{ github.event.comment.pull_request_url }}
          COMMENT_BODY: @@{{ github.event.comment.body }}
          PULL_REQUEST_JSON: @@{{ github.event_name == 'pull_request' && toJson(github.event.pull_request) || '' }}
        run: |
          set -euo pipefail
          PULL_REQUEST="@@{PULL_REQUEST:-}"
          PULL_REQUEST_URL="@@{PULL_REQUEST_URL:-}"
          COMMENT_BODY="@@{COMMENT_BODY:-}"
          if [[ -z "@@{PULL_REQUEST}" && "@@{PULL_REQUEST_URL}" =~ /pulls/([1-9][0-9]*)$ ]]; then
            PULL_REQUEST="@@{BASH_REMATCH[1]}"
          fi
          if [[ ! "@@{PULL_REQUEST}" =~ ^[1-9][0-9]*$ ]]; then
            echo "::error::pull request number is unavailable"
            exit 1
          fi
          if [[ ! "@@{REPOSITORY}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
            echo "::error::repository identity is invalid"
            exit 1
          fi
          pull_json="$RUNNER_TEMP/review-sensei-pull.json"
          if [[ "$EVENT_NAME" == "pull_request" ]]; then
            if [[ -z "@@{PULL_REQUEST_JSON}" ]]; then
              echo "::error::pull request payload is unavailable"
              exit 1
            fi
            printf '%s' "$PULL_REQUEST_JSON" > "$pull_json"
          else
            gh api --method GET "repos/@@{REPOSITORY}/pulls/@@{PULL_REQUEST}" > "$pull_json"
          fi
          resolver="src/review_sensei/hosting/github/trigger.py"
          if [[ -f "$resolver" ]]; then
            PYTHONPATH=src python "$resolver" \
              --event "$EVENT_NAME" \
              --comment-body "$COMMENT_BODY" \
              --pull-json "$pull_json"
            exit 0
          fi
          python - "$pull_json" "$EVENT_NAME" "$COMMENT_BODY" <<'PY'
          import json
          import os
          import re
          import sys
          from pathlib import Path

          sha_full = re.compile(r"^[a-f0-9]{40}$")
          sha_prefix = re.compile(r"^[a-f0-9]{7,39}$")
          ref = re.compile(r"^[A-Za-z0-9._/-]+$")
          rescan = re.compile(r"\bre[\s-]?scan\b", re.IGNORECASE)
          commit_sha = re.compile(r"\bcommit\s+([a-f0-9]{7,40})\b", re.IGNORECASE)
          pull = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
          event_name = sys.argv[2]
          comment_body = sys.argv[3] if len(sys.argv) > 3 else ""

          def valid_ref(value):
              return (
                  isinstance(value, str)
                  and ref.fullmatch(value) is not None
                  and ".." not in value
                  and not value.startswith("/")
                  and not value.endswith("/")
                  and "//" not in value
              )

          head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
          base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
          number = pull.get("number")
          head_sha = head.get("sha")
          head_ref = head.get("ref")
          base_sha = base.get("sha")
          base_ref = base.get("ref")
          if (
              isinstance(number, bool)
              or not isinstance(number, int)
              or number < 1
              or not isinstance(head_sha, str)
              or sha_full.fullmatch(head_sha) is None
              or not valid_ref(head_ref)
              or not isinstance(base_sha, str)
              or sha_full.fullmatch(base_sha) is None
              or not valid_ref(base_ref)
          ):
              print("::error::pull request identity metadata is invalid", file=sys.stderr)
              raise SystemExit(1)

          def choose_head(requested):
              if not requested:
                  return head_sha
              requested = requested.casefold()
              if sha_full.fullmatch(requested):
                  if requested != head_sha:
                      print(
                          "::error::requested head sha does not match the pull request head",
                          file=sys.stderr,
                      )
                      raise SystemExit(1)
                  return requested
              if head_sha.startswith(requested):
                  return head_sha
              print(
                  "::error::requested commit does not match the pull request head",
                  file=sys.stderr,
              )
              raise SystemExit(1)

          operation = "review"
          resolved_head = head_sha
          if event_name == "pull_request" or event_name == "workflow_dispatch":
              pass
          elif event_name == "pull_request_review_comment":
              operation = "reply"
          elif event_name == "issue_comment":
              wants_rescan = (
                  isinstance(comment_body, str)
                  and "@sensei" in comment_body
                  and rescan.search(comment_body) is not None
              )
              if wants_rescan:
                  match = commit_sha.search(comment_body)
                  token = match.group(1).casefold() if match is not None else None
                  if token is not None and not (
                      sha_full.fullmatch(token) or sha_prefix.fullmatch(token)
                  ):
                      token = None
                  resolved_head = choose_head(token)
              elif isinstance(comment_body, str) and len(comment_body.encode("utf-8")) <= 4096 and re.search(r"(?m)(?<!\S)@sensei\s+(?:review\s+(?:status|pause|continue|reenroll)|verify|(?:dismiss|defer|accept-risk)\s+[a-f0-9]{16,64}\s+--reason\s+\S.*)\s*\Z", comment_body, re.IGNORECASE):
                  operation = "command"
              else:
                  operation = "reply"
          else:
              print("::error::unsupported event", file=sys.stderr)
              raise SystemExit(1)
          output = os.environ.get("GITHUB_OUTPUT")
          if not output:
              print("::error::GITHUB_OUTPUT is unavailable", file=sys.stderr)
              raise SystemExit(1)
          with open(output, "a", encoding="utf-8") as handle:
              handle.write(f"operation={operation}\n")
              handle.write(f"head_sha={resolved_head}\n")
              handle.write(f"head_ref={head_ref}\n")
              handle.write(f"base_ref={base_ref}\n")
              handle.write(f"base_sha={base_sha}\n")
              handle.write(f"pull_request_number={number}\n")
          PY

  review-or-reply:
    # A skipped dependency skips this job, so the event guard above is the
    # only guard: the reusable workflow decides eligibility, authorization,
    # and whether anything is published.
    needs: resolve-trigger
    uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@__PUBLIC_WORKFLOW_TAG__
    with:
      mode: @@{{ github.event_name == 'pull_request' && 'automatic' || 'manual' }}
      operation: @@{{ needs.resolve-trigger.outputs.operation }}
      repository: @@{{ github.repository }}
      repository_id: @@{{ github.repository_id }}
      pull_request_number: @@{{ needs.resolve-trigger.outputs.pull_request_number || inputs.pull_request_number }}
      base_ref: @@{{ needs.resolve-trigger.outputs.base_ref }}
      base_sha: @@{{ needs.resolve-trigger.outputs.base_sha }}
      head_ref: @@{{ needs.resolve-trigger.outputs.head_ref }}
      head_repository: @@{{ inputs.head_repository || github.event.pull_request.head.repo.full_name || github.repository }}
      head_sha: @@{{ needs.resolve-trigger.outputs.head_sha }}
      source_kind: @@{{ inputs.source_kind || (github.event_name == 'pull_request_review_comment' && 'inline') || 'issue' }}
      source_comment_id: @@{{ inputs.source_comment_id || github.event.comment.id }}
      source_updated_at: @@{{ inputs.source_updated_at || github.event.comment.updated_at }}
      root_comment_id: @@{{ inputs.root_comment_id || github.event.comment.in_reply_to_id || github.event.comment.id }}
      comment_body: @@{{ github.event.comment.body || '' }}
      comment_actor: @@{{ github.event.comment.user.login || '' }}
      comment_actor_type: @@{{ github.event.comment.user.type || 'User' }}
      comment_association: @@{{ github.event.comment.author_association || '' }}
    secrets:
      OLLAMA_API_KEY: @@{{ secrets.OLLAMA_API_KEY }}
      OPENROUTER_API_KEY: @@{{ secrets.OPENROUTER_API_KEY }}
      OPENAI_API_KEY: @@{{ secrets.OPENAI_API_KEY }}
`
    .replaceAll("__PUBLIC_WORKFLOW_TAG__", tag)
    .replaceAll(GITHUB_EXPRESSION, "$");
}

const FROZEN_RUN_WORKFLOW_TAG_REFERENCE =
  /malsabbagh\/review-sensei\/\.github\/workflows\/review-sensei-run\.yml@([A-Za-z0-9][A-Za-z0-9._-]{0,127})/g;

/**
 * Point a frozen caller's single run-workflow reference at the live tag.
 *
 * The reference is located in the frozen bytes instead of a parallel
 * constant, so a fixture edit can never be silently mis-substituted; a
 * fixture whose reference is missing or duplicated fails here instead.
 */
function retagFrozenCaller(content: string, publicWorkflowTag: string): string {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  const references = [...content.matchAll(FROZEN_RUN_WORKFLOW_TAG_REFERENCE)].map(
    (match) => match[0],
  );
  const reference = references.length === 1 ? references[0] : undefined;
  if (reference === undefined) {
    throw new Error(
      "frozen setup caller must reference the run workflow exactly once",
    );
  }
  const separator = reference.lastIndexOf("@");
  if (reference.slice(separator + 1) === tag) {
    return content;
  }
  return content.replace(reference, `${reference.slice(0, separator)}@${tag}`);
}

/** Immediate pre-cutover caller retained only for managed v4 recognition. */
export function mergeFocusedV4WorkflowTemplate(publicWorkflowTag: string): string {
  return retagFrozenCaller(mergeFocusedV4CallerBytes(), publicWorkflowTag);
}

function pinnedV4WorkflowTemplate(publicWorkflowSha: string): string {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return workflowTemplate(sha).replace(
    "# ReviewSensei setup version: 3",
    "# ReviewSensei setup version: 4",
  );
}

const PROVIDER_PARITY_DRAFT_SKIP =
  "      github.event.pull_request.draft != true &&\n";

/** Byte-exact released setup-v4 caller retained for managed migration. */
export function releasedRunnerSwitchV4WorkflowTemplate(
  publicWorkflowTag: string,
): string {
  return retagFrozenCaller(releasedRunnerSwitchV4CallerBytes(), publicWorkflowTag);
}

export function providerParityWorkflowBeforeDraftSkip(
  publicWorkflowTag: string,
): string {
  // The exact draft-skip literal must remain byte-identical to the released
  // caller. A mismatch throws so an unrecognized file is not migrated.
  const current = providerParityWorkflowTemplate(publicWorkflowTag);
  const previous = current.replace(PROVIDER_PARITY_DRAFT_SKIP, "");
  if (previous === current) {
    throw new Error("draft skip gate is missing from the current caller");
  }
  return previous;
}

export function previousProviderParityWorkflowTemplate(publicWorkflowTag: string): string {
  return providerParityWorkflowBeforeDraftSkip(publicWorkflowTag)
    .replace("  pull-requests: write\n", "  pull-requests: read\n")
    .replace("  issues: write\n", "  issues: read\n");
}

function historicalProviderParityWorkflowTemplate(publicWorkflowTag: string): string {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return previousProviderParityWorkflowTemplate(tag).replace(
    "      operation: ${{ github.event_name == 'pull_request' && 'review' || inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || (github.event_name == 'issue_comment' && (contains(github.event.comment.body, 're-scan') || contains(github.event.comment.body, 're scan') || contains(github.event.comment.body, 'rescan')) && 'review') || 'reply' }}\n",
    "      operation: ${{ inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || 'reply' }}\n",
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

/**
 * Released setup-v5 uninstall bytes.
 *
 * The released uninstall removed the retired config location. The current one
 * also removes the root configuration file, so the released bytes are frozen
 * here for recognition rather than derived from the live template.
 */
export function historicalV5UninstallWorkflow(): string {
  return uninstallWorkflowTemplate().replace(
    "# ReviewSensei setup version: 3",
    `# ReviewSensei setup version: ${SETUP_VERSION}`,
  );
}

/** Current setup-v5 uninstall: also removes the root configuration file. */
export function currentUninstallWorkflow(): string {
  const releasedPaths =
    "          paths = (\n" +
    '              ".github/workflows/review-sensei-review.yml",\n' +
    '              ".github/workflows/review-sensei-uninstall.yml",\n' +
    '              ".github/review-sensei/config.yml",\n' +
    "          )\n";
  const currentPaths =
    "          paths = (\n" +
    '              ".github/workflows/review-sensei-review.yml",\n' +
    '              ".github/workflows/review-sensei-uninstall.yml",\n' +
    '              ".reviewsensei.yml",\n' +
    '              ".github/review-sensei/config.yml",\n' +
    "          )\n";
  const current = historicalV5UninstallWorkflow().replace(
    releasedPaths,
    currentPaths,
  );
  if (current === historicalV5UninstallWorkflow()) {
    throw new Error("uninstall cleanup paths are missing from the template");
  }
  return current;
}

function configFile(version: number, packageVersion?: string): string {
  const resolvedPackageVersion =
    packageVersion ??
    (version === 3
      ? HISTORICAL_V3_SETUP_PACKAGE_VERSION
      : HISTORICAL_V4_SETUP_PACKAGE_VERSION);
  return (
    `# ReviewSensei setup version: ${version}\n` +
    `setup_version: ${version}\n` +
    "provider: ollama\n" +
    "provider_mode: local\n" +
    "model: ''\n" +
    "base_url: http://127.0.0.1:11434/api\n" +
    "cloud_base_url: https://ollama.com/api\n" +
    `local_model: ${DEFAULT_LOCAL_MODEL}\n` +
    `cloud_model: ${DEFAULT_CLOUD_MODEL}\n` +
    `version: ${resolvedPackageVersion}\n` +
    "auto_review: false\n" +
    "github_writes: false\n" +
    "learning_prs: false\n" +
    "mention_replies: false\n" +
    "upload_artifacts: false\n" +
    "stages_dir: ''\n" +
    "categories_dir: ''\n"
  );
}

/**
 * Current setup-v5 configuration: minimal and operator-owned.
 *
 * The package defines a default for every other field, so the generated file
 * states the setup-time backend choice and nothing else; the byte equivalence
 * with the Python builder is pinned by test_current_config_matches_ts_builder_bytes.
 */
export function currentConfigFile(): string {
  return (
    `# ReviewSensei setup version: ${SETUP_VERSION}\n` +
    "schema: 1\n" +
    "\n" +
    "inference:\n" +
    "  backend: local-ollama\n"
  );
}

/**
 * Bounded translation of the retired flat setup configuration.
 *
 * The retired file was never read by a runtime. This one-time import keeps the
 * explicit choices an operator made in it; values it cannot translate are
 * reported as comments in the rendered file and never guessed at. The
 * algorithm mirrors the Python implementation in review_sensei.configuration
 * and renders identical bytes for the same input: the shared cases in
 * tests/fixtures/legacy-setup-import.json are asserted by both suites.
 */
export const LEGACY_SETUP_IMPORT_MAX_LINES = 256;
export const LEGACY_SETUP_IMPORT_MAX_LINE_BYTES = 1024;
export const LEGACY_SETUP_IMPORT_MAX_BYTES = 65536;
export const LEGACY_SETUP_IMPORT_NOTE_LIMIT = 8;

export interface LegacySetupImport {
  readonly content: string;
  readonly carried: readonly string[];
  readonly notes: readonly string[];
}

const LEGACY_SETUP_BACKEND_ALIASES: Readonly<Record<string, string>> = {
  ollama: "local-ollama",
  local: "local-ollama",
  "local-ollama": "local-ollama",
  local_ollama: "local-ollama",
  cloud: "cloud-ollama",
  "cloud-ollama": "cloud-ollama",
  cloud_ollama: "cloud-ollama",
  "ollama-cloud": "cloud-ollama",
  openrouter: "openrouter",
  openai: "openai-compatible",
};

const LEGACY_SETUP_BASE_URLS: Readonly<Record<string, string>> = {
  "local-ollama": "http://127.0.0.1:11434/api",
  "cloud-ollama": "https://ollama.com/api",
};

const LEGACY_SETUP_BEHAVIOR_KEYS: readonly string[] = [
  "auto_review",
  "github_writes",
  "auto_approve",
  "mention_replies",
  "learning_proposals",
  "learning_prs",
  "upload_artifacts",
];

const LEGACY_SETUP_IMPORTED_KEYS = new Set<string>([
  "provider",
  "provider_mode",
  "model",
  "local_model",
  "cloud_model",
  "base_url",
  "cloud_base_url",
  "schema",
  ...LEGACY_SETUP_BEHAVIOR_KEYS,
]);

// The retired flat keys an import can name when it reports a replacement.
// tests/test_legacy_setup_import.py guards these strings against the Python
// RETIRED_FIELDS table.
export const LEGACY_SETUP_RETIRED_FIELDS: Readonly<Record<string, string>> = {
  setup_version: "release/installer identity; the configuration version is 'schema'",
  provider: "inference.backend",
  provider_mode: "inference.backend (use 'local-ollama' or 'cloud-ollama')",
  model: "inference.model",
  base_url: "advanced.endpoint.base_url",
  cloud_base_url: "advanced.endpoint.base_url",
  local_model: "inference.model",
  cloud_model: "inference.model",
  version: "release/installer identity; the configuration version is 'schema'",
  auto_review: "github.automatic_reviews",
  auto_approve: "github.reviews",
  github_writes: "github.writes",
  learning_prs: "github.learning: pull-requests",
  learning_proposals: "github.learning: proposals",
  mention_replies: "github.mentions",
  upload_artifacts: "github.artifacts: diagnostics",
  stages_dir: ".reviewsensei/stages/",
  categories_dir: ".reviewsensei/categories/",
  review_mode:
    "one evidence-focused pipeline is the only engine; integration policy is github.reviews",
  provider_profile:
    "select inference.backend and set the advanced.endpoint fields",
  reviewsensei_version: "release/installer identity; remove it",
};

// Python's str.splitlines() and str.strip() use character sets that differ
// from String.prototype.split/trim, so the byte-parity contract needs them
// spelled out here.
const PYTHON_WHITESPACE =
  "\\t\\n\\v\\f\\r \\x85\\x1c-\\x1f\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PYTHON_STRIP_PATTERN = new RegExp(
  `^[${PYTHON_WHITESPACE}]+|[${PYTHON_WHITESPACE}]+$`,
  "g",
);
const PYTHON_LINE_PATTERN = /[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/;
const LEGACY_SETUP_KEY_PATTERN = /^[a-z][a-z0-9_]*$/;
const LEGACY_SETUP_BARE_VALUE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;
const LEGACY_SETUP_ECHO_LENGTH = 64;

function pythonStrip(value: string): string {
  return value.replace(PYTHON_STRIP_PATTERN, "");
}

function pythonSplitLines(value: string): string[] {
  if (value === "") {
    return [];
  }
  const normalized = value.replace(/\r\n/g, "\n");
  const lines = normalized.split(PYTHON_LINE_PATTERN);
  const last = normalized.at(-1) ?? "";
  if (PYTHON_LINE_PATTERN.test(last)) {
    lines.pop();
  }
  return lines;
}

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).length;
}

function hasLoneSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) {
        return true;
      }
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function legacySetupEcho(value: string): string {
  const text = pythonStrip(value);
  if (text.length <= LEGACY_SETUP_ECHO_LENGTH) {
    return text;
  }
  return `${text.slice(0, LEGACY_SETUP_ECHO_LENGTH)}...`;
}

function legacySetupScalar(raw: string): string {
  if (
    raw.length >= 2 &&
    raw[0] === raw[raw.length - 1] &&
    (raw[0] === "'" || raw[0] === '"')
  ) {
    return raw.slice(1, -1);
  }
  return raw;
}

function legacySetupQuoted(value: string): string {
  if (LEGACY_SETUP_BARE_VALUE_PATTERN.test(value)) {
    return value;
  }
  return `'${value.replaceAll("'", "''")}'`;
}

function renderValue(value: boolean): string {
  return value ? "true" : "false";
}

function legacySetupValues(content: string): {
  values: Map<string, string>;
  notes: string[];
} {
  if (hasLoneSurrogate(content)) {
    return { values: new Map(), notes: ["the retired file is not valid UTF-8"] };
  }
  if (utf8Length(content) > LEGACY_SETUP_IMPORT_MAX_BYTES) {
    return {
      values: new Map(),
      notes: ["the retired file is larger than the import bound"],
    };
  }
  const lines = pythonSplitLines(content);
  if (lines.length > LEGACY_SETUP_IMPORT_MAX_LINES) {
    return {
      values: new Map(),
      notes: ["the retired file is longer than the import bound"],
    };
  }
  const values = new Map<string, string>();
  const notes: string[] = [];
  for (const line of lines) {
    const stripped = pythonStrip(line);
    if (!stripped || stripped.startsWith("#")) {
      continue;
    }
    if (utf8Length(line) > LEGACY_SETUP_IMPORT_MAX_LINE_BYTES) {
      notes.push("a line longer than the import bound was not imported");
      continue;
    }
    const separator = stripped.indexOf(":");
    const key = separator === -1 ? "" : pythonStrip(stripped.slice(0, separator));
    if (separator === -1 || !LEGACY_SETUP_KEY_PATTERN.test(key)) {
      notes.push(`the line '${legacySetupEcho(stripped)}' was not imported`);
      continue;
    }
    if (values.has(key)) {
      notes.push(`'${key}' is repeated; the first value was kept`);
      continue;
    }
    values.set(key, legacySetupScalar(pythonStrip(stripped.slice(separator + 1))));
  }
  return { values, notes };
}

export function importLegacySetupConfiguration(
  content: string,
): LegacySetupImport {
  const parsed = legacySetupValues(content);
  let values = parsed.values;
  const notes = parsed.notes;
  if (values.has("schema")) {
    // A canonical document at the retired path is not a flat legacy file:
    // translate nothing rather than misread nested keys as retired ones.
    notes.push("the retired path holds a canonical configuration; it was not imported");
    values = new Map();
  }

  const text = (key: string): string | null => {
    const raw = values.get(key);
    if (raw === undefined) {
      return null;
    }
    const value = pythonStrip(raw);
    return value || null;
  };

  const boolean = (key: string): boolean | null => {
    const raw = text(key);
    if (raw === null) {
      return null;
    }
    const lowered = raw.toLowerCase();
    if (lowered === "true" || lowered === "false") {
      return lowered === "true";
    }
    notes.push(
      `'${key}': '${legacySetupEcho(raw)}' is not a boolean and was not imported`,
    );
    return null;
  };

  const carried: string[] = [];

  let backend: string | null = null;
  for (const key of ["provider_mode", "provider"]) {
    const raw = text(key);
    if (raw === null) {
      continue;
    }
    const mapped = LEGACY_SETUP_BACKEND_ALIASES[raw.toLowerCase()];
    if (mapped === undefined) {
      notes.push(`'${key}': '${legacySetupEcho(raw)}' is not a backend; it was not imported`);
      continue;
    }
    if (backend === null) {
      backend = mapped;
      carried.push("inference.backend");
    }
  }
  const resolvedBackend = backend ?? "local-ollama";

  let model = text("model");
  let selectedModelKey: string | null = model === null ? null : "model";
  if (model === null && resolvedBackend in LEGACY_SETUP_BASE_URLS) {
    const fallbackKey =
      resolvedBackend === "cloud-ollama" ? "cloud_model" : "local_model";
    model = text(fallbackKey);
    if (model !== null) {
      selectedModelKey = fallbackKey;
    }
  }
  if (model !== null) {
    carried.push("inference.model");
  }
  for (const key of ["local_model", "cloud_model"]) {
    if (key !== selectedModelKey && values.has(key)) {
      notes.push(`'${key}' is not imported; the selected backend uses inference.model`);
    }
  }

  let endpoint: string | null = null;
  const selectedEndpointKey =
    { "local-ollama": "base_url", "cloud-ollama": "cloud_base_url" }[
      resolvedBackend
    ] ?? null;
  const endpointKeys: ReadonlyArray<readonly [string, string]> = [
    ["base_url", "local-ollama"],
    ["cloud_base_url", "cloud-ollama"],
  ];
  for (const [key, endpointBackend] of endpointKeys) {
    const rawEndpoint = text(key);
    if (rawEndpoint === null) {
      continue;
    }
    const defaultEndpoint = LEGACY_SETUP_BASE_URLS[endpointBackend];
    if (key === selectedEndpointKey) {
      if (rawEndpoint !== defaultEndpoint) {
        endpoint = rawEndpoint;
        carried.push("advanced.endpoint.base_url");
      }
      continue;
    }
    if (rawEndpoint !== defaultEndpoint) {
      notes.push(
        `'${key}' is not imported for this backend; set advanced.endpoint.base_url explicitly`,
      );
    }
  }

  const presentBehavior = LEGACY_SETUP_BEHAVIOR_KEYS.filter((key) =>
    values.has(key),
  );

  const automaticReviews = boolean("auto_review");
  if (automaticReviews !== null) {
    carried.push("github.automatic_reviews");
  }
  const writes = boolean("github_writes");
  if (writes !== null) {
    carried.push("github.writes");
  }
  const autoApprove = boolean("auto_approve");
  if (autoApprove !== null) {
    carried.push("github.reviews");
  }
  const mentions = boolean("mention_replies");
  if (mentions !== null) {
    carried.push("github.mentions");
  }
  const proposals = boolean("learning_proposals");
  const learningPrs = boolean("learning_prs");
  if (values.has("learning_proposals") || values.has("learning_prs")) {
    carried.push("github.learning");
  }
  const artifactsFlag = boolean("upload_artifacts");
  if (artifactsFlag !== null) {
    carried.push("github.artifacts");
  }

  for (const key of values.keys()) {
    if (LEGACY_SETUP_IMPORTED_KEYS.has(key)) {
      continue;
    }
    const replacement = LEGACY_SETUP_RETIRED_FIELDS[key];
    if (replacement !== undefined) {
      notes.push(`'${key}' was retired; ${replacement}`);
    } else {
      notes.push(`'${key}' is not a retired setup setting and was not imported`);
    }
  }

  const boundedNotes = notes.slice(0, LEGACY_SETUP_IMPORT_NOTE_LIMIT);
  if (notes.length > LEGACY_SETUP_IMPORT_NOTE_LIMIT) {
    boundedNotes.push(
      `${notes.length - LEGACY_SETUP_IMPORT_NOTE_LIMIT} more import notes were omitted`,
    );
  }

  let lines: string[];
  let body: string[];
  if (carried.length === 0) {
    lines = [
      `# ReviewSensei setup version: ${SETUP_VERSION}`,
      "# The retired .github/review-sensei/config.yml is no longer read, and",
      "# this import did not translate anything from it. Review the retired",
      "# file, then delete it after merging.",
    ];
    body = ["schema: 1", "", "inference:", "  backend: local-ollama"];
  } else {
    lines = [
      `# ReviewSensei setup version: ${SETUP_VERSION}`,
      "# One-time import of the retired .github/review-sensei/config.yml,",
      "# which nothing reads any more. The settings below are this file's",
      "# policies now; review this diff, then delete the retired file",
      "# after merging.",
    ];
    body = ["schema: 1", "", "inference:", `  backend: ${resolvedBackend}`];
    if (model !== null) {
      body.push(`  model: ${legacySetupQuoted(model)}`);
    }
    if (presentBehavior.length > 0) {
      body.push(
        "",
        "github:",
        `  automatic_reviews: ${renderValue(automaticReviews ?? true)}`,
        `  writes: ${renderValue(writes ?? false)}`,
        `  reviews: ${autoApprove === false ? "advisory" : "auto-approve"}`,
        `  mentions: ${renderValue(mentions ?? true)}`,
        `  learning: ${
          learningPrs ? "pull-requests" : proposals ? "proposals" : "disabled"
        }`,
        `  artifacts: ${artifactsFlag ? "diagnostics" : "none"}`,
      );
    }
    if (endpoint !== null) {
      body.push(
        "",
        "advanced:",
        "  endpoint:",
        `    base_url: ${legacySetupQuoted(endpoint)}`,
        "    allow_custom_endpoint: true",
      );
    }
  }
  lines.push(...boundedNotes.map((note) => `# NOTE: ${note}`));
  lines.push(...body);
  return {
    content: `${lines.join("\n")}\n`,
    carried,
    notes,
  };
}

/** Released setup-v4 uninstall bytes retained for exact managed migration recognition. */
export function historicalV4UninstallWorkflow(): string {
  return historicalV4UninstallBytes();
}

/** Immediate pre-cutover config retained only for managed v4 recognition. */
export function mergeFocusedV4ConfigFile(): string {
  return mergeFocusedV4ConfigBytes();
}

/** Exact setup-v4 output from the historical SHA-pinning contract. */
export function buildPinnedV4SetupFiles(
  publicWorkflowSha: string,
): readonly SetupFile[] {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return [
    { path: SETUP_WORKFLOW_PATH, content: pinnedV4WorkflowTemplate(sha) },
    {
      path: SETUP_UNINSTALL_WORKFLOW_PATH,
      content: historicalV4UninstallWorkflow(),
    },
    {
      path: LEGACY_CONFIG_PATH,
      content: configFile(4, HISTORICAL_V4_SETUP_PACKAGE_VERSION),
    },
  ];
}

/** Current setup-v5 output: the public git tag is the only workflow ref. */
export function buildTaggedV4SetupFiles(
  publicWorkflowTag: string,
): readonly SetupFile[] {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return [
    { path: SETUP_WORKFLOW_PATH, content: resolveTriggerWorkflowTemplate(tag) },
    {
      path: SETUP_UNINSTALL_WORKFLOW_PATH,
      content: currentUninstallWorkflow(),
    },
    { path: CONFIG_PATH, content: currentConfigFile() },
  ];
}

/** Released setup-v4 output retained for exact managed migration recognition. */
export function buildHistoricalTaggedV4SetupFiles(
  publicWorkflowTag: string,
): readonly SetupFile[] {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return [
    { path: SETUP_WORKFLOW_PATH, content: taggedWorkflowTemplate(tag) },
    {
      path: SETUP_UNINSTALL_WORKFLOW_PATH,
      content: historicalV4UninstallWorkflow(),
    },
    {
      path: LEGACY_CONFIG_PATH,
      content: configFile(4, HISTORICAL_V4_SETUP_PACKAGE_VERSION),
    },
  ];
}

/** Released setup-v4 provider-parity output retained for migration recognition. */
export function buildHistoricalProviderParityV4SetupFiles(
  publicWorkflowTag: string,
): readonly SetupFile[] {
  const tag = validatePublicWorkflowTag(publicWorkflowTag);
  return [
    {
      path: SETUP_WORKFLOW_PATH,
      content: historicalProviderParityWorkflowTemplate(tag),
    },
    {
      path: SETUP_UNINSTALL_WORKFLOW_PATH,
      content: historicalV4UninstallWorkflow(),
    },
    {
      path: LEGACY_CONFIG_PATH,
      content: configFile(4, HISTORICAL_V4_SETUP_PACKAGE_VERSION),
    },
  ];
}

export function buildSetupFiles(
  publicWorkflowTag: string = DEFAULT_PUBLIC_WORKFLOW_TAG,
): readonly SetupFile[] {
  return buildTaggedV4SetupFiles(publicWorkflowTag);
}

export function buildHistoricalV3SetupFiles(
  publicWorkflowSha: string,
): readonly SetupFile[] {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return [
    { path: SETUP_WORKFLOW_PATH, content: historicalV3WorkflowTemplate(sha) },
    {
      path: SETUP_UNINSTALL_WORKFLOW_PATH,
      content: historicalV3UninstallWorkflowTemplate(),
    },
    { path: LEGACY_CONFIG_PATH, content: configFile(3) },
  ];
}

/** Exact current setup-v3 output retained only for migration recognition. */
export function buildCurrentV3SetupFiles(
  publicWorkflowSha: string,
): readonly SetupFile[] {
  const sha = validatePublicWorkflowSha(publicWorkflowSha);
  return [
    { path: SETUP_WORKFLOW_PATH, content: workflowTemplate(sha) },
    { path: SETUP_UNINSTALL_WORKFLOW_PATH, content: uninstallWorkflowTemplate() },
    { path: LEGACY_CONFIG_PATH, content: configFile(3) },
  ];
}

export const SETUP_FILES: readonly SetupFile[] = buildSetupFiles();

export const SETUP_PULL_REQUEST_TITLE = "ReviewSensei review setup";

/**
 * Setup pull request body, byte-identical to the Python builder in
 * review_sensei.hosting.github.setup for the same imported settings.
 */
export function setupPullRequestBody(
  importedSettings: readonly string[] = [],
): string {
  const generatedSummary =
    importedSettings.length > 0
      ? "The generated file states the setup-time backend choice plus the " +
        "settings imported from your retired configuration"
      : "The generated file states the setup-time backend choice and " +
        "nothing else";
  let body =
    "This pull request adds or updates the ReviewSensei review workflow " +
    "(setup version 5): a thin caller that follows the operator-managed " +
    "public v5 git tag, the canonical .reviewsensei.yml configuration, and " +
    "a manual uninstall-cleanup workflow. " +
    "Configuration lives in .reviewsensei.yml at the repository root of the " +
    "default branch. " +
    generatedSummary +
    "; every other setting has a package default and the file belongs to " +
    "you from here on. Setup creates no repository variables and never " +
    "rewrites the file. " +
    "Approval policy: the package default is github.reviews: auto-approve, " +
    "so ReviewSensei approves an eligible exact head as part of its normal " +
    "pipeline. Set github.reviews: blocking to publish and enforce without " +
    "ever approving, or github.reviews: advisory for findings with no " +
    "ReviewSensei merge gate. " +
    "The only two optional Actions overrides are REVIEWSENSEI_PROVIDER " +
    "(inference.backend) and REVIEWSENSEI_MODEL (inference.model); set them " +
    "under Settings → Secrets and variables → Actions → Variables only to " +
    "override the file, and the reusable workflow reads them once. ";
  if (importedSettings.length > 0) {
    body +=
      "This setup read the retired .github/review-sensei/config.yml once " +
      "and carried these settings into .reviewsensei.yml: " +
      importedSettings.join(", ") +
      ". Review the generated file before merging; it is not " +
      "regenerated afterwards, and nothing reads the retired file once " +
      "this pull request is merged. ";
  }
  body +=
    "The selected backend applies to automatic/manual reviews and " +
    "authorized mention conversations: local-ollama uses the labelled " +
    "self-hosted runner, cloud-ollama uses GitHub-hosted Ollama Cloud, " +
    "openrouter uses GitHub-hosted OpenRouter, and openai-compatible uses " +
    "the endpoint and model named in the configuration. Cloud credentials " +
    "are read by name only: OLLAMA_API_KEY, OPENROUTER_API_KEY, or " +
    "OPENAI_API_KEY. The App never creates or reads secret values. " +
    "Upgrading from an earlier setup: the old workflow's repository " +
    "variables and the retired .github/review-sensei/config.yml are no " +
    "longer read by the review runtime. If you want them removed, do it as " +
    "a separate, explicitly authorized cleanup: delete only the " +
    "ReviewSensei-managed variables the old workflow read, and leave " +
    "credentials and unrelated repository settings untouched. Leaving them " +
    "in place is harmless but silently ignored. " +
    "The uninstall workflow creates a reviewable PR that removes these " +
    "generated files, including .reviewsensei.yml and the retired config " +
    "location; it does not delete learnings or secrets. No private keys, " +
    "installation tokens, or webhook bodies are included in these files.";
  return body;
}
