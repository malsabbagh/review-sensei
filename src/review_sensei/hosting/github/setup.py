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
REQUIRED_SETUP_PERMISSIONS = frozenset({"contents", "pull_requests", "variables"})
SETUP_VERSION = 2
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
)
SETUP_FILE_PATHS = (WORKFLOW_PATH, UNINSTALL_WORKFLOW_PATH, CONFIG_PATH)
SETUP_VERSION_PATTERN = re.compile(
    r"(?m)^[ \t]*#[ \t]*ReviewSensei setup version:[ \t]*(?P<version>[0-9]+)[ \t]*$"
)
SETUP_VERSION_PREFIX = "ReviewSensei setup version:"


def _setup_marker_version(content: str) -> int | None:
    match = SETUP_VERSION_PATTERN.search(content)
    if match is None:
        return None
    try:
        return int(match.group("version"))
    except ValueError:
        return None


def _looks_like_legacy_setup(path: str, content: str) -> bool:
    """Recognize only the known pre-versioned generated setup files."""

    if path == WORKFLOW_PATH:
        return all(
            marker in content
            for marker in (
                "name: ReviewSensei review",
                "review_sensei_version:",
                "prepare-diff",
            )
        )
    if path == UNINSTALL_WORKFLOW_PATH:
        return all(
            marker in content
            for marker in (
                "name: Remove ReviewSensei setup",
                "git rm",
                "Remove ReviewSensei setup",
            )
        )
    if path == CONFIG_PATH:
        return all(marker in content for marker in ("provider: ollama", "base_url:"))
    return False


def _classify_setup_files(files: dict[str, str | None]) -> str:
    """Return absent, migration, current, or unknown for known setup paths.

    A setup is migrated only when every present file is either a current file,
    an older ReviewSensei-generated file, or a missing companion file. Any
    foreign content or future version causes a no-write result instead.
    """

    present = {path: content for path, content in files.items() if content is not None}
    if not present:
        return "absent"

    has_legacy = False
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
                has_current = True
                continue
            has_legacy = True
            continue
        if _looks_like_legacy_setup(path, content):
            has_legacy = True
            continue
        return "unknown"

    if len(present) == len(SETUP_FILE_PATHS) and has_current and not has_legacy:
        return "current"
    if has_legacy or has_current:
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


def _immutable_workflow() -> str:
    return """\
# ReviewSensei setup version: 2
name: ReviewSensei review

on:
  workflow_dispatch:
    inputs:
      base_ref:
        description: Repository default branch (must match the repository setting)
        required: true
      head_ref:
        description: Head branch or ref to review
        required: true
      head_repository:
        description: Optional owner/repo slug for fork review
        required: false
      review_sensei_version:
        description: Exact ReviewSensei package version (X.Y.Z or vX.Y.Z)
        required: true
permissions:
  contents: read

jobs:
  review:
    runs-on: [self-hosted, linux, x64, ollama]
    steps:
      - name: Set up Python
        uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6
        with:
          python-version: "3.11"

      - name: Validate refs, version, and provider variables
        id: version
        env:
          BASE_REF: ${{ inputs.base_ref }}
          HEAD_REF: ${{ inputs.head_ref }}
          HEAD_REPOSITORY: ${{ inputs.head_repository }}
          REVIEW_SENSEI_VERSION: ${{ inputs.review_sensei_version }}
          DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}
          CONFIG_PROVIDER_MODE: ${{ vars.REVIEWSENSEI_PROVIDER_MODE }}
          CONFIG_LOCAL_MODEL: ${{ vars.REVIEWSENSEI_LOCAL_MODEL }}
          CONFIG_CLOUD_MODEL: ${{ vars.REVIEWSENSEI_CLOUD_MODEL }}
        run: |
          python - <<'PY'
          import os
          import re
          import sys
          ref_pattern = re.compile(r"^[A-Za-z0-9._/-]+$")
          repo_pattern = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
          for name in ("BASE_REF", "HEAD_REF"):
              value = os.environ.get(name, "")
              if not value or value.startswith("-") or not ref_pattern.fullmatch(value):
                  print(f"::error::{name} is not a supported Git ref", file=sys.stderr)
                  sys.exit(1)
          default_branch = os.environ.get("DEFAULT_BRANCH", "")
          if not default_branch or not ref_pattern.fullmatch(default_branch):
              print("::error::repository default branch is unavailable", file=sys.stderr)
              sys.exit(1)
          if os.environ.get("BASE_REF") != default_branch:
              print("::error::base_ref must match the repository default branch", file=sys.stderr)
              sys.exit(1)
          head_repository = os.environ.get("HEAD_REPOSITORY", "")
          if head_repository and not repo_pattern.fullmatch(head_repository):
              print("::error::head_repository must be an owner/repo slug", file=sys.stderr)
              sys.exit(1)
          raw = os.environ.get("REVIEW_SENSEI_VERSION", "")
          if not re.fullmatch(r"v?[0-9]+\\.[0-9]+\\.[0-9]+", raw):
              print("::error::review_sensei_version must be an exact X.Y.Z version", file=sys.stderr)
              sys.exit(1)
          normalized = raw[1:] if raw.startswith("v") else raw
          provider_mode = os.environ.get("CONFIG_PROVIDER_MODE", "").strip().lower() or "local"
          if provider_mode not in {"local", "cloud"}:
              print("::error::REVIEWSENSEI_PROVIDER_MODE must be local or cloud", file=sys.stderr)
              sys.exit(1)
          model_pattern = re.compile(r"^[A-Za-z0-9._:/-]+$")
          local_model = os.environ.get("CONFIG_LOCAL_MODEL", "").strip() or "qwen3.5:4b"
          cloud_model = os.environ.get("CONFIG_CLOUD_MODEL", "").strip() or "deepseek-v4-flash:cloud"
          for label, model in (("REVIEWSENSEI_LOCAL_MODEL", local_model), ("REVIEWSENSEI_CLOUD_MODEL", cloud_model)):
              if not model_pattern.fullmatch(model):
                  print(f"::error::{label} is not a supported Ollama model name", file=sys.stderr)
                  sys.exit(1)
          selected_model = cloud_model if provider_mode == "cloud" else local_model
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
              handle.write(f"normalized_version={normalized}\\n")
              handle.write(f"provider_mode={provider_mode}\\n")
              handle.write(f"ollama_model={selected_model}\\n")
          PY

      - name: Check out base ref only
        uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
        with:
          ref: ${{ github.event.repository.default_branch }}
          fetch-depth: 0

      - name: Install exact ReviewSensei package
        env:
          REVIEW_SENSEI_VERSION: ${{ steps.version.outputs.normalized_version }}
        run: |
          python -m venv "$RUNNER_TEMP/review-sensei-venv"
          "$RUNNER_TEMP/review-sensei-venv/bin/python" -m pip install --upgrade pip
          "$RUNNER_TEMP/review-sensei-venv/bin/python" -m pip install "review-sensei==${REVIEW_SENSEI_VERSION}"
          "$RUNNER_TEMP/review-sensei-venv/bin/review-sensei" --version | tee review-sensei-version.txt

      - name: Prepare bounded diff
        env:
          BASE_REF: ${{ inputs.base_ref }}
          HEAD_REF: ${{ inputs.head_ref }}
          HEAD_REPOSITORY: ${{ inputs.head_repository }}
        run: |
          args=()
          if [[ -n "$HEAD_REPOSITORY" ]]; then
            args+=(--head-repository "$HEAD_REPOSITORY")
          fi
          "$RUNNER_TEMP/review-sensei-venv/bin/review-sensei" prepare-diff \
            --repository "$PWD" \
            --base-ref "$BASE_REF" \
            --head-ref "$HEAD_REF" \
            "${args[@]}" \
            --output pr.patch \
            --max-diff-bytes 1048576 \
            --max-diff-lines 50000 \
            --max-diff-files 500 \
            --max-diff-hunks 5000

      - name: Verify local Ollama service
        if: steps.version.outputs.provider_mode == 'local'
        env:
          OLLAMA_MODEL: ${{ steps.version.outputs.ollama_model }}
        run: |
          curl --fail --silent --show-error --max-time 10 \
            "http://127.0.0.1:11434/api/tags" >/dev/null
          ollama show "$OLLAMA_MODEL" >/dev/null

      - name: Run ReviewSensei review
        env:
          PROVIDER_MODE: ${{ steps.version.outputs.provider_mode }}
          OLLAMA_MODEL: ${{ steps.version.outputs.ollama_model }}
          OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}
        run: |
          if [[ "$PROVIDER_MODE" == "cloud" ]]; then
            export OLLAMA_BASE_URL="https://ollama.com/api"
            if [[ -z "$OLLAMA_API_KEY" ]]; then
              echo "::error::OLLAMA_API_KEY is required for cloud mode" >&2
              exit 1
            fi
          else
            export OLLAMA_BASE_URL="http://127.0.0.1:11434/api"
            unset OLLAMA_API_KEY
          fi
          "$RUNNER_TEMP/review-sensei-venv/bin/review-sensei" \
            --diff pr.patch \
            --learning-root . \
            --learning-directory .github/review-sensei/learnings \
            --base-url "$OLLAMA_BASE_URL" \
            --model "$OLLAMA_MODEL" \
            --output review.json

      - name: Upload review artifacts
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7
        with:
          name: review-sensei-review
          path: |
            review.json
            review-sensei-version.txt
          if-no-files-found: error
          retention-days: 7
"""


def _config_file() -> str:
    return """\
# ReviewSensei setup version: 2
setup_version: 2
provider: ollama
provider_mode: local
base_url: http://127.0.0.1:11434/api
cloud_base_url: https://ollama.com/api
local_model: qwen3.5:4b
cloud_model: deepseek-v4-flash:cloud
"""


def _uninstall_workflow() -> str:
    return """\
# ReviewSensei setup version: 2
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
              "--body", "Remove the ReviewSensei workflow, cleanup workflow, and generated configuration. ReviewSensei learnings and repository secrets are left untouched.",
          ], check=True)
          PY
"""


def _setup_pull_request_body() -> str:
    return (
        "This pull request adds or updates the ReviewSensei review workflow "
        "(setup version 2), provider defaults, and a manual uninstall-cleanup "
        "workflow. The installation bootstrap also "
        "creates the visible repository variables REVIEWSENSEI_PROVIDER_MODE (local), "
        "REVIEWSENSEI_LOCAL_MODEL (qwen3.5:4b), and "
        "REVIEWSENSEI_CLOUD_MODEL (deepseek-v4-flash:cloud) without overwriting "
        "existing values. Change REVIEWSENSEI_PROVIDER_MODE to cloud and add "
        "OLLAMA_API_KEY as a repository secret to use Ollama Cloud. The uninstall "
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
    ) -> None:
        if not branch_name.strip():
            raise GitHubSetupError("Setup branch name must be non-empty")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch_name):
            raise GitHubSetupError("Setup branch name is invalid")
        if any(segment in {".", ".."} for segment in branch_name.split("/")):
            raise GitHubSetupError("Setup branch name is invalid")
        if not title.strip():
            raise GitHubSetupError("Setup pull request title must be non-empty")
        self.branch_name = branch_name
        self.title = title

    def build(
        self,
        repository: str,
        *,
        base_branch: str | None = None,
    ) -> SetupPlan:
        if not isinstance(repository, str) or not _is_repository_slug(repository):
            raise GitHubSetupError("Setup repository must be an owner/repo slug")
        files = (
            SetupFile(path=WORKFLOW_PATH, content=_immutable_workflow()),
            SetupFile(path=UNINSTALL_WORKFLOW_PATH, content=_uninstall_workflow()),
            SetupFile(path=CONFIG_PATH, content=_config_file()),
        )
        return SetupPlan(
            repository=repository,
            branch_name=self.branch_name,
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

    def branch_exists(
        self,
        *,
        repository: str,
        installation_token: str,
        branch: str,
    ) -> bool:
        """Return whether the setup branch already exists."""

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
        branch: str,
        files: Sequence[SetupFile],
    ) -> None:
        """Create or update the setup branch with generated files."""

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
        branch: str,
        files: Sequence[SetupFile],
    ) -> None:
        base_ref = self._request_json(
            "GET",
            f"/repos/{repository}/git/ref/heads/{quote(base_branch, safe='')}",
            installation_token=installation_token,
        )
        if not isinstance(base_ref, dict):
            raise GitHubSetupError("GitHub setup response did not include a base ref")
        base_object = base_ref.get("object")
        if not isinstance(base_object, dict):
            raise GitHubSetupError("GitHub setup response did not include a base ref")
        base_sha = base_object.get("sha")
        if not isinstance(base_sha, str) or not base_sha:
            raise GitHubSetupError(
                "GitHub setup response did not include a base commit"
            )
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
                "message": "Add ReviewSensei review setup files",
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
            self._request_json(
                "PATCH",
                f"/repos/{repository}/git/refs/heads/{quote(branch, safe='')}",
                installation_token=installation_token,
                body={"sha": commit_sha},
            )
            return
        if status < 200 or status >= 300:
            self._raise_for_status(status)

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
            plan = self.plan_builder.build(repository, base_branch=base_branch)
            with self._repository_lock(repository, plan.branch_name):
                branch_exists = self.transport.branch_exists(
                    repository=repository,
                    installation_token=installation_token,
                    branch=plan.branch_name,
                )
                existing_prs = self.transport.list_pull_requests(
                    repository=repository,
                    installation_token=installation_token,
                    head_branch=plan.branch_name,
                )
                existing = self._existing_pr_number(existing_prs)
                if existing is not None:
                    results.append(
                        SetupPullRequestResult(
                            repository=repository,
                            status="skipped_pull_request_exists",
                            pull_request_number=existing,
                        )
                    )
                    continue
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
                if not branch_exists or setup_state == "migration":
                    self.transport.create_or_update_branch(
                        repository=repository,
                        installation_token=installation_token,
                        base_branch=base_branch,
                        branch=plan.branch_name,
                        files=plan.files,
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
        return _classify_setup_files(files)

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
