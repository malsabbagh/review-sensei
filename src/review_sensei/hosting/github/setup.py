"""GitHub App installation-time setup bootstrap.

This module creates a reviewable setup pull request when a repository is newly
selected by a ReviewSensei GitHub App installation or needs a generated setup
migration. It is intentionally outside the provider-neutral review core and
never writes credentials, private keys, installation tokens, or raw webhook
bodies into generated files.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .errors import (
    GitHubSetupError,
    GitHubSetupTransientError,
)
from .webhooks import REPOSITORY_SLUG_PATTERN, VerifiedDelivery

MAX_SETUP_RESPONSE_BYTES = 512 * 1024
MAX_SETUP_FILE_BYTES = 128 * 1024
DEFAULT_SETUP_BRANCH = "review-sensei/setup"
DEFAULT_SETUP_TITLE = "ReviewSensei review setup"
SETUP_COMMIT_MESSAGE = "Add ReviewSensei review setup files"
SETUP_BRANCH_PREFIX = "review-sensei/setup-v3"
SETUP_APP_LOGIN = "reviewsensei[bot]"
REQUIRED_SETUP_PERMISSIONS = frozenset(
    {"contents", "pull_requests", "variables", "workflows"}
)
SETUP_VERSION = 3
SETUP_VERSION_MARKER = f"ReviewSensei setup version: {SETUP_VERSION}"
WORKFLOW_PATH = ".github/workflows/review-sensei-review.yml"
UNINSTALL_WORKFLOW_PATH = ".github/workflows/review-sensei-uninstall.yml"
CONFIG_PATH = ".github/review-sensei/config.yml"
DEFAULT_PROVIDER_MODE = "local"
DEFAULT_LOCAL_MODEL = "qwen3.5:4b"
DEFAULT_CLOUD_MODEL = "deepseek-v4-flash:cloud"
SETUP_VARIABLES = (
    ("REVIEWSENSEI_PROVIDER_MODE", DEFAULT_PROVIDER_MODE),
    ("REVIEWSENSEI_LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
    ("REVIEWSENSEI_CLOUD_MODEL", DEFAULT_CLOUD_MODEL),
    ("REVIEWSENSEI_VERSION", "0.1.0"),
    ("REVIEWSENSEI_AUTO_REVIEW", "false"),
    ("REVIEWSENSEI_GITHUB_WRITES", "false"),
    ("REVIEWSENSEI_LEARNING_PRS", "false"),
    ("REVIEWSENSEI_MENTION_REPLIES", "false"),
    ("REVIEWSENSEI_UPLOAD_ARTIFACTS", "false"),
)
SETUP_FILE_PATHS = (WORKFLOW_PATH, UNINSTALL_WORKFLOW_PATH, CONFIG_PATH)
PUBLIC_WORKFLOW_SHA_PATTERN = re.compile(r"^[a-f0-9]{40}$")
# The Python builder is also used by local contract tests and by older setup
# transports that do not have Worker configuration. The Worker always passes
# its explicitly configured value through ``public_workflow_sha`` and rejects
# the fallback before any GitHub write.
DEFAULT_PUBLIC_WORKFLOW_SHA = "f" * 40
SETUP_VERSION_PATTERN = re.compile(
    r"(?m)^[ \t]*#[ \t]*ReviewSensei setup version:[ \t]*(?P<version>[0-9]+)[ \t]*$"
)
SETUP_VERSION_PREFIX = "ReviewSensei setup version:"
PUBLIC_WORKFLOW_REFERENCE_PATTERN = re.compile(
    r"malsabbagh/review-sensei/\.github/workflows/review-sensei-run\.yml@"
    r"(?P<sha>[a-f0-9]{40})"
)
LEGACY_V3_SOURCE_INPUTS = (
    "      source_kind:\n"
    "        description: Source kind for manual dispatch (issue or inline)\n"
    "        required: false\n"
    "      source_comment_id:\n"
    "        description: Source comment ID for manual reply\n"
    "        required: false\n"
    "      source_updated_at:\n"
    "        description: Timestamp of source comment\n"
    "        required: false\n"
    "      root_comment_id:\n"
    "        description: Root comment ID for manual reply thread\n"
    "        required: false\n"
)
LEGACY_V3_UNINSTALL_BODY = (
    "Remove the ReviewSensei workflow, cleanup workflow, and generated "
    "configuration. ReviewSensei learnings and repository secrets are left "
    "untouched."
)


def _setup_marker_version(content: str) -> int | None:
    match = SETUP_VERSION_PATTERN.search(content)
    if match is None:
        return None
    try:
        return int(match.group("version"))
    except ValueError:
        return None


LEGACY_SHA256 = {
    WORKFLOW_PATH: frozenset(
        {
            # Released setup-v2 Python output.
            "415ae46804c95dc388c4daa8f31db195a4dfba97ab6de03e1837c7f34d4db3d6",
            # Released setup-v2 Worker output.
            "f32527ebe4476eeadc7fc607d2529190a42a4c39820c9c35e3ae3497c1dc15b2",
            # Pre-marker Worker and Python outputs released in 8afa49f.
            "f273b9c220a4e1e8336b776f919f7a83b78d9cc6473ad8920f9a7cd1fe250078",
            "bb43f1cf081a46e03fa83baa4906759e202a9b8915c9f38db2cb0cd7ddd79be8",
            # Released intermediate pre-marker Worker output.
            "4888b04a0e577cb95ceaaabb0b612b55c8cd0aa30aba86fdbfac716d772d57ea",
        }
    ),
    UNINSTALL_WORKFLOW_PATH: frozenset(
        {
            "6d330e41a8df5fe7e1a54875091ffc353bbacf6fde727dc28d7bbdc76aedeca0",
            "350dcf9960e1c325a2ce1e6189ffeb5d993dfc053511791991c67044a0a3edf3",
            "e4f56f488214db287b05ee45adb3c84db98060d9b73412a459b7f78f087321a5",
        }
    ),
    CONFIG_PATH: frozenset(
        {
            "a1ebe48445cab35ffde125b7a8a66253d7a007cbed11dc03118c0b9b14d9a58b",
            "d20c350134df752db4d03a67676c5ee244b1f7421650c620e36fa8ecb22e24af",
            "904d1ef6d8afbdb7a5dc1e9d04f6a0bff87d8a04eb8c1a561a17ed2d877e4987",
        }
    ),
}


def _looks_like_legacy_setup(path: str, content: str) -> bool:
    """Recognize only byte-exact generated files from the released v2 catalog."""

    expected = LEGACY_SHA256.get(path)
    if expected is None:
        return False
    return hashlib.sha256(content.encode("utf-8")).hexdigest() in expected


def _looks_like_current_setup(
    path: str,
    content: str,
    *,
    public_workflow_sha: str = DEFAULT_PUBLIC_WORKFLOW_SHA,
) -> bool:
    """Recognize only the exact configured setup-v3 generated artifact."""

    expected = {
        WORKFLOW_PATH: _immutable_workflow(public_workflow_sha),
        UNINSTALL_WORKFLOW_PATH: _uninstall_workflow(),
        CONFIG_PATH: _config_file(),
    }
    return expected.get(path) == content


def _historical_v3_workflow(
    public_workflow_sha: str = DEFAULT_PUBLIC_WORKFLOW_SHA,
) -> str:
    """Return the previously released setup-v3 workflow template.

    The first setup-v3 release already carried the source metadata expressions,
    but did not declare their manual-dispatch inputs. It also used the original
    trusted-local job name and head-ref fallback. Keep this exact compatibility
    shape so real installations can be recognized as managed stale content.
    """

    return (
        _immutable_workflow(public_workflow_sha)
        .replace(LEGACY_V3_SOURCE_INPUTS, "")
        .replace("  trusted-local-manual:\n", "  manual-or-trusted-local:\n")
        .replace(
            "      head_ref: ${{ inputs.head_ref || '' }}\n",
            "      head_ref: ${{ inputs.head_ref || github.event.pull_request.head.ref || '' }}\n",
        )
    )


def _historical_v3_uninstall_workflow() -> str:
    """Return the previously released setup-v3 uninstall workflow template."""

    return _uninstall_workflow().replace(
        '              "--body", "Remove generated ReviewSensei setup files; '
        'learnings and secrets remain untouched.",\n',
        f'              "--body", "{LEGACY_V3_UNINSTALL_BODY}",\n',
    )


def _looks_like_managed_v3_setup(path: str, content: str) -> bool:
    """Recognize a released v3 artifact generated for any valid public SHA."""

    if path == WORKFLOW_PATH:
        matches = [
            match.group("sha")
            for match in PUBLIC_WORKFLOW_REFERENCE_PATTERN.finditer(content)
        ]
        if len(matches) != 2 or len(set(matches)) != 1:
            return False
        public_workflow_sha = matches[0]
        return content in {
            _immutable_workflow(public_workflow_sha),
            _historical_v3_workflow(public_workflow_sha),
        }
    if path == UNINSTALL_WORKFLOW_PATH:
        return content == _historical_v3_uninstall_workflow()
    return False


def _classify_setup_files(
    files: dict[str, str | None],
    *,
    public_workflow_sha: str = DEFAULT_PUBLIC_WORKFLOW_SHA,
) -> str:
    """Return absent, migration, current, or unknown for known setup paths.

    A setup is migrated only when every present file is either current, a
    managed stale-v3 workflow, an older ReviewSensei-generated file, or a
    missing companion file. Any foreign content or future version causes a
    no-write result instead.
    """

    present = {path: content for path, content in files.items() if content is not None}
    if not present:
        return "absent"

    has_managed = False
    has_current = False
    for path, content in present.items():
        if content is None:
            continue
        marker = _setup_marker_version(content)
        if SETUP_VERSION_PREFIX in content and marker is None:
            return "unknown"
        if marker is not None:
            if marker > SETUP_VERSION:
                return "unknown"
            if marker == SETUP_VERSION:
                if _looks_like_current_setup(
                    path,
                    content,
                    public_workflow_sha=public_workflow_sha,
                ):
                    has_current = True
                elif _looks_like_managed_v3_setup(path, content):
                    has_managed = True
                else:
                    return "unknown"
                continue
            if not _looks_like_legacy_setup(path, content):
                return "unknown"
            has_managed = True
            continue
        if _looks_like_legacy_setup(path, content):
            has_managed = True
            continue
        return "unknown"

    if len(present) == len(SETUP_FILE_PATHS) and has_current and not has_managed:
        return "current"
    if has_managed or has_current:
        return "migration"
    return "unknown"


@dataclass(frozen=True)
class SetupFile:
    """One generated repository file for the setup plan."""

    path: str
    content: str

    def __post_init__(self) -> None:
        if not self.path or not self.path.strip():
            raise GitHubSetupError("Setup file path must be non-empty")
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise GitHubSetupError("Setup file path must be repository-relative")


@dataclass(frozen=True)
class SetupPlan:
    """Generated setup branch contents for one repository."""

    repository: str
    branch_name: str
    base_branch: str | None
    title: str
    body: str
    files: tuple[SetupFile, ...]


def _validate_public_workflow_sha(value: str) -> str:
    if (
        not isinstance(value, str)
        or PUBLIC_WORKFLOW_SHA_PATTERN.fullmatch(value) is None
    ):
        raise GitHubSetupError(
            "PUBLIC_WORKFLOW_SHA must be exactly 40 lowercase hexadecimal characters"
        )
    return value


def _setup_branch(base_sha: str, public_workflow_sha: str) -> str:
    if (
        not isinstance(base_sha, str)
        or PUBLIC_WORKFLOW_SHA_PATTERN.fullmatch(base_sha) is None
    ):
        raise GitHubSetupError("Setup branch inputs were invalid")
    public_sha = _validate_public_workflow_sha(public_workflow_sha)
    return f"{SETUP_BRANCH_PREFIX}-{base_sha[:12]}-{public_sha[:12]}"


def _immutable_workflow(public_workflow_sha: str = DEFAULT_PUBLIC_WORKFLOW_SHA) -> str:
    """Return the thin setup-v3 caller for the immutable public workflow."""

    sha = _validate_public_workflow_sha(public_workflow_sha)
    reusable = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@" + sha
    return f"""\
# ReviewSensei setup version: 3
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
    uses: {reusable}
    with:
      mode: automatic
      operation: review
      repository: ${{{{ github.repository }}}}
      repository_id: ${{{{ github.repository_id }}}}
      pull_request_number: ${{{{ github.event.pull_request.number }}}}
      base_ref: ${{{{ github.event.pull_request.base.ref }}}}
      base_sha: ${{{{ github.event.pull_request.base.sha }}}}
      head_ref: ${{{{ github.event.pull_request.head.ref }}}}
      head_repository: ${{{{ github.event.pull_request.head.repo.full_name }}}}
      head_sha: ${{{{ github.event.pull_request.head.sha }}}}
      review_sensei_version: ${{{{ vars.REVIEWSENSEI_VERSION }}}}
      enable_review: ${{{{ vars.REVIEWSENSEI_AUTO_REVIEW }}}}
      enable_github_writes: ${{{{ vars.REVIEWSENSEI_GITHUB_WRITES }}}}
      enable_learning_prs: ${{{{ vars.REVIEWSENSEI_LEARNING_PRS }}}}
      enable_mention_replies: ${{{{ vars.REVIEWSENSEI_MENTION_REPLIES }}}}
      upload_artifacts: ${{{{ vars.REVIEWSENSEI_UPLOAD_ARTIFACTS }}}}
    secrets:
      OLLAMA_API_KEY: ${{{{ secrets.OLLAMA_API_KEY }}}}

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
    uses: {reusable}
    with:
      mode: manual
      operation: ${{{{ inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || 'reply' }}}}
      repository: ${{{{ github.repository }}}}
      repository_id: ${{{{ github.repository_id }}}}
      pull_request_number: ${{{{ inputs.pull_request_number || github.event.issue.number || github.event.pull_request.number }}}}
      base_ref: ${{{{ inputs.base_ref || github.event.pull_request.base.ref || github.event.repository.default_branch }}}}
      base_sha: ${{{{ inputs.base_sha || github.event.pull_request.base.sha }}}}
      head_ref: ${{{{ inputs.head_ref || '' }}}}
      head_repository: ${{{{ inputs.head_repository || github.event.pull_request.head.repo.full_name || github.repository }}}}
      head_sha: ${{{{ inputs.head_sha || github.event.pull_request.head.sha }}}}
      source_kind: ${{{{ inputs.source_kind || (github.event_name == 'pull_request_review_comment' && 'inline') || 'issue' }}}}
      source_comment_id: ${{{{ inputs.source_comment_id || github.event.comment.id }}}}
      source_updated_at: ${{{{ inputs.source_updated_at || github.event.comment.updated_at }}}}
      root_comment_id: ${{{{ inputs.root_comment_id || github.event.comment.in_reply_to_id || github.event.comment.id }}}}
      review_sensei_version: ${{{{ inputs.review_sensei_version || vars.REVIEWSENSEI_VERSION }}}}
      enable_review: ${{{{ (inputs.operation || (github.event_name == 'workflow_dispatch' && 'review') || 'reply') == 'review' && vars.REVIEWSENSEI_AUTO_REVIEW || 'false' }}}}
      enable_github_writes: ${{{{ vars.REVIEWSENSEI_GITHUB_WRITES }}}}
      enable_learning_prs: ${{{{ vars.REVIEWSENSEI_LEARNING_PRS }}}}
      enable_mention_replies: ${{{{ vars.REVIEWSENSEI_MENTION_REPLIES }}}}
      upload_artifacts: ${{{{ vars.REVIEWSENSEI_UPLOAD_ARTIFACTS }}}}
    secrets:
      OLLAMA_API_KEY: ${{{{ secrets.OLLAMA_API_KEY }}}}
"""


def _config_file() -> str:
    return """\
# ReviewSensei setup version: 3
setup_version: 3
provider: ollama
provider_mode: local
base_url: http://127.0.0.1:11434/api
cloud_base_url: https://ollama.com/api
local_model: qwen3.5:4b
cloud_model: deepseek-v4-flash:cloud
version: 0.1.0
auto_review: false
github_writes: false
learning_prs: false
mention_replies: false
upload_artifacts: false
"""


def _uninstall_workflow() -> str:
    return """\
# ReviewSensei setup version: 3
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
          ref: ${{ github.event.repository.default_branch }}
          fetch-depth: 0

      - name: Create cleanup pull request
        env:
          GH_TOKEN: ${{ github.token }}
          DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}
          RUN_ID: ${{ github.run_id }}
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
"""


def _setup_pull_request_body() -> str:
    return (
        "This pull request adds or updates the ReviewSensei review workflow "
        "(setup version 3), opt-in provider defaults, and a manual uninstall-cleanup "
        "workflow. The installation bootstrap also "
        "creates the visible repository variables REVIEWSENSEI_PROVIDER_MODE (local), "
        "REVIEWSENSEI_LOCAL_MODEL (qwen3.5:4b), and "
        "REVIEWSENSEI_CLOUD_MODEL (deepseek-v4-flash:cloud), an exact package "
        "version, and false-by-default opt-ins without overwriting existing "
        "values. Change the opt-in variables explicitly to enable publication. "
        "Cloud mode reads the existing customer-owned OLLAMA_API_KEY secret by "
        "name only; the App never creates or reads its value. The uninstall "
        "workflow creates a reviewable PR to remove these generated scripts; it does "
        "not delete learnings or secrets. No private keys, installation tokens, or "
        "webhook bodies are included in these files."
    )


class SetupPlanBuilder:
    """Build idempotent setup plans without secrets."""

    def __init__(
        self,
        *,
        branch_name: str = DEFAULT_SETUP_BRANCH,
        title: str = DEFAULT_SETUP_TITLE,
        public_workflow_sha: str = DEFAULT_PUBLIC_WORKFLOW_SHA,
    ) -> None:
        if not branch_name.strip():
            raise GitHubSetupError("Setup branch name must be non-empty")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch_name):
            raise GitHubSetupError("Setup branch name is invalid")
        if any(segment in {".", ".."} for segment in branch_name.split("/")):
            raise GitHubSetupError("Setup branch name is invalid")
        if not title.strip():
            raise GitHubSetupError("Setup pull request title must be non-empty")
        self.public_workflow_sha = _validate_public_workflow_sha(public_workflow_sha)
        self.branch_name = branch_name
        self.title = title

    def build(
        self,
        repository: str,
        *,
        base_branch: str | None = None,
        base_sha: str | None = None,
    ) -> SetupPlan:
        if not isinstance(repository, str) or not _is_repository_slug(repository):
            raise GitHubSetupError("Setup repository must be an owner/repo slug")
        files = (
            SetupFile(
                path=WORKFLOW_PATH,
                content=_immutable_workflow(self.public_workflow_sha),
            ),
            SetupFile(path=UNINSTALL_WORKFLOW_PATH, content=_uninstall_workflow()),
            SetupFile(path=CONFIG_PATH, content=_config_file()),
        )
        return SetupPlan(
            repository=repository,
            branch_name=(
                self.branch_name
                if base_sha is None
                else _setup_branch(base_sha, self.public_workflow_sha)
            ),
            base_branch=base_branch,
            title=self.title,
            body=_setup_pull_request_body(),
            files=files,
        )


def _is_repository_slug(value: str) -> bool:
    parts = value.split("/")
    return len(parts) == 2 and bool(
        parts[0]
        and parts[1]
        and not re.fullmatch(r"\.+", parts[0])
        and not re.fullmatch(r"\.+", parts[1])
        and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[0])
        and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1])
    )


@runtime_checkable
class GitHubSetupTransport(Protocol):
    """Transport seam for GitHub setup API calls."""

    def get_default_branch(
        self,
        *,
        repository: str,
        installation_token: str,
    ) -> str:
        """Return the repository default branch name."""

    def get_default_head_sha(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
    ) -> str:
        """Return the exact authoritative default-branch commit SHA."""

    def branch_exists(
        self,
        *,
        repository: str,
        installation_token: str,
        branch: str,
    ) -> bool:
        """Return whether the setup branch already exists."""

    def is_managed_setup_branch(
        self,
        *,
        repository: str,
        installation_token: str,
        branch: str,
        base_sha: str,
        public_workflow_sha: str,
    ) -> bool:
        """Return whether an existing branch has the App-owned setup shape."""

    def get_repository_file(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
        path: str,
    ) -> str | None:
        """Return a bounded text file from the default branch, or ``None``."""

    def create_or_update_branch(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
        base_sha: str,
        branch: str,
        files: Sequence[SetupFile],
    ) -> bool:
        """Create the content-addressed setup branch, or report a collision."""

    def ensure_repository_variables(
        self,
        *,
        repository: str,
        installation_token: str,
        variables: Sequence[tuple[str, str]],
    ) -> None:
        """Create missing plain-text Actions variables without overwriting values."""

    def list_pull_requests(
        self,
        *,
        repository: str,
        installation_token: str,
        head_branch: str,
    ) -> Sequence[dict[str, object]]:
        """Return open pull requests whose head matches the setup branch."""

    def create_pull_request(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> dict[str, object]:
        """Create a setup pull request."""


Opener = Callable[..., Any]


class GitHubSetupClient:
    """GitHub REST client for setup bootstrap requests."""

    def __init__(
        self,
        *,
        api_url: str = "https://api.github.com",
        opener: Opener = urlopen,
        timeout: int = 30,
    ) -> None:
        if not api_url.strip():
            raise GitHubSetupError("GitHub API URL must be non-empty")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise GitHubSetupError("GitHub setup request timeout must be positive")
        self.api_url = api_url.rstrip("/")
        self.opener = opener
        self.timeout = timeout

    def get_default_branch(
        self,
        *,
        repository: str,
        installation_token: str,
    ) -> str:
        data = self._request_json(
            "GET",
            f"/repos/{repository}",
            installation_token=installation_token,
        )
        if not isinstance(data, dict):
            raise GitHubSetupError("GitHub setup response was invalid")
        default_branch = data.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise GitHubSetupError(
                "GitHub setup response did not include a default branch"
            )
        return default_branch

    def branch_exists(
        self,
        *,
        repository: str,
        installation_token: str,
        branch: str,
    ) -> bool:
        status, _ = self._open(
            "GET",
            f"/repos/{repository}/branches/{quote(branch, safe='')}",
            installation_token=installation_token,
        )
        if status == 404:
            return False
        if status < 200 or status >= 300:
            self._raise_for_status(status)
        return True

    def get_default_head_sha(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
    ) -> str:
        data = self._request_json(
            "GET",
            f"/repos/{repository}/git/ref/heads/{quote(base_branch, safe='')}",
            installation_token=installation_token,
        )
        if not isinstance(data, dict):
            raise GitHubSetupError("GitHub setup response did not include a base ref")
        base_object = data.get("object")
        if not isinstance(base_object, dict):
            raise GitHubSetupError("GitHub setup response did not include a base ref")
        base_sha = base_object.get("sha")
        if (
            not isinstance(base_sha, str)
            or PUBLIC_WORKFLOW_SHA_PATTERN.fullmatch(base_sha) is None
        ):
            raise GitHubSetupError(
                "GitHub setup response did not include a base commit"
            )
        return base_sha

    def is_managed_setup_branch(
        self,
        *,
        repository: str,
        installation_token: str,
        branch: str,
        base_sha: str,
        public_workflow_sha: str,
    ) -> bool:
        data = self._request_json(
            "GET",
            f"/repos/{repository}/branches/{quote(branch, safe='')}",
            installation_token=installation_token,
        )
        if not isinstance(data, dict):
            raise GitHubSetupError("GitHub setup branch response was invalid")
        commit = data.get("commit")
        if not isinstance(commit, dict):
            raise GitHubSetupError("GitHub setup branch response was invalid")
        metadata = commit.get("commit")
        parents = commit.get("parents")
        sha = commit.get("sha")
        author = commit.get("author")
        if (
            not isinstance(metadata, dict)
            or metadata.get("message") != SETUP_COMMIT_MESSAGE
            or not isinstance(sha, str)
            or re.fullmatch(r"[a-f0-9]{40}", sha) is None
            or not isinstance(parents, list)
            or len(parents) != 1
            or not isinstance(parents[0], dict)
            or not isinstance(author, dict)
            or author.get("login") != SETUP_APP_LOGIN
            or author.get("type") != "Bot"
        ):
            return False
        parent_sha = parents[0].get("sha")
        if not isinstance(parent_sha, str) or parent_sha != base_sha:
            return False
        comparison = self._request_json(
            "GET",
            f"/repos/{repository}/compare/{parent_sha}...{sha}",
            installation_token=installation_token,
        )
        if not isinstance(comparison, dict):
            raise GitHubSetupError("GitHub setup branch comparison was invalid")
        files = comparison.get("files")
        if not isinstance(files, list) or not files:
            return False
        generated_only = all(
            isinstance(file, dict)
            and file.get("filename") in SETUP_FILE_PATHS
            and (
                "previous_filename" not in file
                or file.get("previous_filename") in SETUP_FILE_PATHS
            )
            for file in files
        )
        if not generated_only:
            return False
        branch_files = {
            path: self.get_repository_file(
                repository=repository,
                installation_token=installation_token,
                base_branch=branch,
                path=path,
            )
            for path in SETUP_FILE_PATHS
        }
        return (
            _classify_setup_files(
                branch_files,
                public_workflow_sha=public_workflow_sha,
            )
            == "current"
        )

    def get_repository_file(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
        path: str,
    ) -> str | None:
        """Read one generated file from a branch without exposing its content."""

        encoded_path = "/".join(quote(segment, safe="") for segment in path.split("/"))
        status, raw = self._open(
            "GET",
            f"/repos/{repository}/contents/{encoded_path}?ref={quote(base_branch, safe='')}",
            installation_token=installation_token,
        )
        if status == 404:
            return None
        if status < 200 or status >= 300:
            self._raise_for_status(status)
        try:
            data = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubSetupError("GitHub setup file response was invalid") from exc
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubSetupError("GitHub setup file response was invalid")
        if data.get("encoding") != "base64" or data.get("truncated") is True:
            raise GitHubSetupError("GitHub setup file response was invalid")
        encoded = data.get("content")
        if not isinstance(encoded, str):
            raise GitHubSetupError("GitHub setup file response was invalid")
        encoded = "".join(encoded.split())
        max_encoded = ((MAX_SETUP_FILE_BYTES + 2) // 3) * 4 + 4
        if len(encoded) > max_encoded:
            raise GitHubSetupError(
                "GitHub setup file exceeded the configured size limit"
            )
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise GitHubSetupError("GitHub setup file response was invalid") from exc
        if len(decoded) > MAX_SETUP_FILE_BYTES:
            raise GitHubSetupError(
                "GitHub setup file exceeded the configured size limit"
            )
        try:
            return decoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitHubSetupError("GitHub setup file was not valid UTF-8") from exc

    def create_or_update_branch(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
        base_sha: str,
        branch: str,
        files: Sequence[SetupFile],
    ) -> bool:
        tree_entries = [
            {
                "path": file.path,
                "mode": "100644",
                "type": "blob",
                "content": file.content,
            }
            for file in files
        ]
        tree_payload: dict[str, object] = {
            "base_tree": base_sha,
            "tree": tree_entries,
        }
        tree = self._request_json(
            "POST",
            f"/repos/{repository}/git/trees",
            installation_token=installation_token,
            body=tree_payload,
        )
        if not isinstance(tree, dict):
            raise GitHubSetupError("GitHub setup response did not include a tree")
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str) or not tree_sha:
            raise GitHubSetupError("GitHub setup response did not include a tree")
        commit = self._request_json(
            "POST",
            f"/repos/{repository}/git/commits",
            installation_token=installation_token,
            body={
                "message": SETUP_COMMIT_MESSAGE,
                "tree": tree_sha,
                "parents": [base_sha],
            },
        )
        if not isinstance(commit, dict):
            raise GitHubSetupError("GitHub setup response did not include a commit")
        commit_sha = commit.get("sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise GitHubSetupError("GitHub setup response did not include a commit")
        ref = f"refs/heads/{branch}"
        status, _ = self._open(
            "POST",
            f"/repos/{repository}/git/refs",
            installation_token=installation_token,
            body={"ref": ref, "sha": commit_sha},
        )
        if status == 422:
            return False
        if status < 200 or status >= 300:
            self._raise_for_status(status)
        return True

    def ensure_repository_variables(
        self,
        *,
        repository: str,
        installation_token: str,
        variables: Sequence[tuple[str, str]],
    ) -> None:
        base_path = f"/repos/{repository}/actions/variables"
        for name, value in variables:
            variable_path = f"{base_path}/{quote(name, safe='')}"
            status, _ = self._open(
                "GET",
                variable_path,
                installation_token=installation_token,
            )
            if status == 200:
                continue
            if status != 404:
                self._raise_for_status(status)
            status, _ = self._open(
                "POST",
                base_path,
                installation_token=installation_token,
                body={"name": name, "value": value},
            )
            if status == 409:
                concurrent_status, _ = self._open(
                    "GET",
                    variable_path,
                    installation_token=installation_token,
                )
                if concurrent_status == 200:
                    continue
            if status < 200 or status >= 300:
                self._raise_for_status(status)

    def list_pull_requests(
        self,
        *,
        repository: str,
        installation_token: str,
        head_branch: str,
    ) -> Sequence[dict[str, object]]:
        owner = repository.split("/", 1)[0]
        query = urlencode({"head": f"{owner}:{head_branch}", "state": "open"})
        data = self._request_json(
            "GET",
            f"/repos/{repository}/pulls?{query}",
            installation_token=installation_token,
        )
        if not isinstance(data, list):
            raise GitHubSetupError(
                "GitHub setup response did not include a pull request list"
            )
        return [item for item in data if isinstance(item, dict)]

    def create_pull_request(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> dict[str, object]:
        data = self._request_json(
            "POST",
            f"/repos/{repository}/pulls",
            installation_token=installation_token,
            body={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
            },
        )
        if not isinstance(data, dict):
            raise GitHubSetupError(
                "GitHub setup response did not include a pull request"
            )
        return data

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        installation_token: str,
        body: dict[str, object] | None = None,
    ) -> Any:
        status, raw = self._open(
            method,
            path,
            installation_token=installation_token,
            body=body,
        )
        if status < 200 or status >= 300:
            self._raise_for_status(status)
        if not raw:
            return None
        try:
            text = bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitHubSetupError("GitHub setup response was not valid UTF-8") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitHubSetupError("GitHub setup response was invalid JSON") from exc

    def _open(
        self,
        method: str,
        path: str,
        *,
        installation_token: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        url = f"{self.api_url}{path}"
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {installation_token}",
            "User-Agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_SETUP_RESPONSE_BYTES + 1)
                if not isinstance(raw, (bytes, bytearray)):
                    raise GitHubSetupError("GitHub setup response was invalid")
                if len(raw) > MAX_SETUP_RESPONSE_BYTES:
                    raise GitHubSetupError(
                        "GitHub setup response exceeded the configured size limit"
                    )
                status = int(getattr(response, "status", 200))
                return status, bytes(raw)
        except HTTPError as exc:
            raw = self._read_bounded_http_error_body(exc)
            return exc.code, raw.encode("utf-8")
        except (URLError, TimeoutError) as exc:
            raise GitHubSetupTransientError("GitHub setup request failed") from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise GitHubSetupTransientError(
                    "GitHub setup request timed out"
                ) from exc
            raise GitHubSetupTransientError("GitHub setup request failed") from exc

    def _raise_for_status(self, status: int) -> None:
        if status == 404:
            raise GitHubSetupError("GitHub setup target was not found")
        if status in (401, 403):
            raise GitHubSetupError("GitHub setup lacks the requested permission")
        if status == 429 or status >= 500:
            raise GitHubSetupTransientError("GitHub setup request failed temporarily")
        raise GitHubSetupError("GitHub setup request was rejected")

    def _read_bounded_http_error_body(self, exc: HTTPError) -> str:
        try:
            body = exc.read(MAX_SETUP_RESPONSE_BYTES + 1)
            if isinstance(body, (bytes, bytearray)):
                return bytes(body)[:MAX_SETUP_RESPONSE_BYTES].decode(
                    "utf-8", errors="replace"
                )
        except OSError:
            pass
        return ""


@dataclass(frozen=True)
class SetupPullRequestResult:
    """Outcome for one repository setup attempt."""

    repository: str
    status: str
    pull_request_number: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not _is_repository_slug(
            self.repository
        ):
            raise GitHubSetupError("Setup result repository must be an owner/repo slug")
        if not self.status:
            raise GitHubSetupError("Setup result status must be non-empty")


class SetupPullRequestService:
    """Create one setup PR per newly selected repository, idempotently."""

    def __init__(
        self,
        transport: GitHubSetupTransport,
        *,
        plan_builder: SetupPlanBuilder | None = None,
    ) -> None:
        self.transport = transport
        self.plan_builder = plan_builder or SetupPlanBuilder()
        self._guard = threading.RLock()
        self._repository_locks: dict[tuple[str, str], threading.RLock] = {}

    def _repository_lock(self, repository: str, branch: str) -> threading.RLock:
        key = (repository, branch)
        with self._guard:
            return self._repository_locks.setdefault(key, threading.RLock())

    def ensure_setup_pull_requests(
        self,
        delivery: VerifiedDelivery,
        *,
        installation_token: str,
        repositories: Iterable[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> list[SetupPullRequestResult]:
        if delivery.suspended:
            return []
        if delivery.event == "installation":
            if delivery.action not in {"created", "new_permissions_accepted"}:
                return []
            selected = self._selected_repositories(
                delivery=delivery,
                repositories=repositories,
            )
        elif delivery.event == "installation_repositories":
            if delivery.action != "added":
                return []
            selected = self._selected_repositories(
                delivery=delivery,
                repositories=repositories,
            )
        else:
            return []
        if permissions is None:
            permissions = delivery.permissions
        if not self._has_setup_permissions(permissions):
            return [
                SetupPullRequestResult(repository=repo, status="skipped_permissions")
                for repo in selected
            ]
        results: list[SetupPullRequestResult] = []
        for repository in selected:
            if not isinstance(repository, str) or not REPOSITORY_SLUG_PATTERN.fullmatch(
                repository
            ):
                raise GitHubSetupError("Setup repository must be an owner/repo slug")
            base_branch = self.transport.get_default_branch(
                repository=repository,
                installation_token=installation_token,
            )
            base_sha = self.transport.get_default_head_sha(
                repository=repository,
                installation_token=installation_token,
                base_branch=base_branch,
            )
            plan = self.plan_builder.build(
                repository,
                base_branch=base_branch,
                base_sha=base_sha,
            )
            with self._repository_lock(repository, plan.branch_name):
                setup_state = self._inspect_repository_setup(
                    repository=repository,
                    installation_token=installation_token,
                    base_branch=base_branch,
                )
                if setup_state == "current":
                    results.append(
                        SetupPullRequestResult(
                            repository=repository,
                            status="skipped_current",
                        )
                    )
                    continue
                if setup_state == "unknown":
                    results.append(
                        SetupPullRequestResult(
                            repository=repository,
                            status="skipped_unknown_setup",
                        )
                    )
                    continue
                branch_exists = self.transport.branch_exists(
                    repository=repository,
                    installation_token=installation_token,
                    branch=plan.branch_name,
                )
                if branch_exists:
                    branch_checker = getattr(
                        self.transport, "is_managed_setup_branch", None
                    )
                    if not callable(branch_checker) or not branch_checker(
                        repository=repository,
                        installation_token=installation_token,
                        branch=plan.branch_name,
                        base_sha=base_sha,
                        public_workflow_sha=self.plan_builder.public_workflow_sha,
                    ):
                        results.append(
                            SetupPullRequestResult(
                                repository=repository,
                                status="skipped_branch_conflict",
                            )
                        )
                        continue
                if not branch_exists:
                    created_branch = self.transport.create_or_update_branch(
                        repository=repository,
                        installation_token=installation_token,
                        base_branch=base_branch,
                        base_sha=base_sha,
                        branch=plan.branch_name,
                        files=plan.files,
                    )
                    if not created_branch:
                        branch_checker = getattr(
                            self.transport, "is_managed_setup_branch", None
                        )
                        if not callable(branch_checker) or not branch_checker(
                            repository=repository,
                            installation_token=installation_token,
                            branch=plan.branch_name,
                            base_sha=base_sha,
                            public_workflow_sha=(self.plan_builder.public_workflow_sha),
                        ):
                            results.append(
                                SetupPullRequestResult(
                                    repository=repository,
                                    status="skipped_branch_conflict",
                                )
                            )
                            continue
                ensure_variables = getattr(
                    self.transport, "ensure_repository_variables", None
                )
                if callable(ensure_variables) and self._has_permission(
                    permissions, "variables"
                ):
                    ensure_variables(
                        repository=repository,
                        installation_token=installation_token,
                        variables=SETUP_VARIABLES,
                    )
                existing_after_branch = self._existing_pr_number(
                    self.transport.list_pull_requests(
                        repository=repository,
                        installation_token=installation_token,
                        head_branch=plan.branch_name,
                    )
                )
                if existing_after_branch is not None:
                    results.append(
                        SetupPullRequestResult(
                            repository=repository,
                            status="skipped_pull_request_exists",
                            pull_request_number=existing_after_branch,
                        )
                    )
                    continue
                created = self.transport.create_pull_request(
                    repository=repository,
                    installation_token=installation_token,
                    base_branch=base_branch,
                    head_branch=plan.branch_name,
                    title=plan.title,
                    body=plan.body,
                )
                number = created.get("number")
                if not isinstance(number, int):
                    raise GitHubSetupError(
                        "GitHub setup response did not include a pull request number"
                    )
                results.append(
                    SetupPullRequestResult(
                        repository=repository,
                        status="created",
                        pull_request_number=number,
                    )
                )
        return results

    def _inspect_repository_setup(
        self,
        *,
        repository: str,
        installation_token: str,
        base_branch: str,
    ) -> str:
        """Classify known generated files on the default branch.

        Older test integrations may not implement the optional reader seam;
        those transports retain the original fresh-install behavior.
        """

        reader = getattr(self.transport, "get_repository_file", None)
        if not callable(reader):
            return "absent"
        files: dict[str, str | None] = {}
        for path in SETUP_FILE_PATHS:
            content = reader(
                repository=repository,
                installation_token=installation_token,
                base_branch=base_branch,
                path=path,
            )
            if content is not None and not isinstance(content, str):
                raise GitHubSetupError("GitHub setup file response was invalid")
            files[path] = content
        return _classify_setup_files(
            files,
            public_workflow_sha=self.plan_builder.public_workflow_sha,
        )

    @staticmethod
    def _existing_pr_number(existing_prs: Sequence[dict[str, object]]) -> int | None:
        existing = next(
            (pr for pr in existing_prs if isinstance(pr.get("number"), int)),
            None,
        )
        if existing is None:
            return None
        pr_number = existing["number"]
        if not isinstance(pr_number, int):
            raise GitHubSetupError(
                "GitHub setup response did not include a pull request number"
            )
        return pr_number

    @staticmethod
    def _selected_repositories(
        *,
        delivery: VerifiedDelivery,
        repositories: Iterable[str] | None,
    ) -> list[str]:
        selected = (
            list(repositories)
            if repositories is not None
            else list(delivery.repositories)
        )
        if delivery.repository and delivery.repository not in selected:
            selected.insert(0, delivery.repository)
        for repository in selected:
            if not isinstance(repository, str) or not _is_repository_slug(repository):
                raise GitHubSetupError("Setup repository must be an owner/repo slug")
        return selected

    @staticmethod
    def _has_setup_permissions(permissions: dict[str, str]) -> bool:
        if not isinstance(permissions, dict):
            return False
        normalized = {
            str(key).strip().lower().replace("-", "_"): str(value).strip().lower()
            for key, value in permissions.items()
        }
        if "actions_variables" in normalized and "variables" not in normalized:
            # GitHub webhook payloads call the UI's Variables permission
            # `actions_variables`; keep the setup contract's neutral name.
            normalized["variables"] = normalized["actions_variables"]
        return all(
            permission in normalized and normalized[permission] == "write"
            for permission in REQUIRED_SETUP_PERMISSIONS
        )

    @staticmethod
    def _has_permission(permissions: dict[str, str], name: str) -> bool:
        normalized = {
            str(key).strip().lower().replace("-", "_"): str(value).strip().lower()
            for key, value in permissions.items()
        }
        if "actions_variables" in normalized and "variables" not in normalized:
            normalized["variables"] = normalized["actions_variables"]
        return normalized.get(name) == "write"
