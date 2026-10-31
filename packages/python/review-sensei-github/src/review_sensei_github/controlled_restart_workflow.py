"""Read-only filesystem checks for an explicitly requested workflow restart.

These checks neither install a registration nor prove storage retention. The
operator must arrange retained storage on the existing self-hosted runner. The
restart entry still validates the installed designation and actual run identity.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from review_sensei.errors import ReviewInputError


def select_late_registration(
    raw: bytes,
    *,
    parent: Path,
    repository: str,
    repository_id: int,
    pull_request: int,
    base_sha: str,
    head_sha: str,
    policy_commit: str,
    workflow_sha: str,
    job: str,
) -> tuple[str, Path]:
    """Select data from an already signature-verified wheel, never grant access.

    The frozen installer calls this before installing/executing the later wheel.
    Runtime admission independently rechecks its installed compiled designation,
    current live scope, cessation/expiry, archive and one-shot claim.
    """
    from .controlled_restart import read_registry

    if (
        os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
        or os.environ.get("RUNNER_ENVIRONMENT") != "self-hosted"
        or type(repository_id) is not int
        or type(pull_request) is not int
    ):
        raise ReviewInputError("late restart requires the actual held first execution")
    expected = dict(
        repository=repository,
        repository_id=repository_id,
        pull_request=pull_request,
        base_sha=base_sha,
        head_sha=head_sha,
        policy_commit=policy_commit,
        workflow_sha=workflow_sha,
        job=job,
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
    )
    matches = [
        row
        for row in read_registry(raw)
        if all(row[k] == v for k, v in expected.items())
    ]
    if len(matches) != 1:
        raise ReviewInputError("late restart package has no unique execution binding")
    row = matches[0]
    _retained_path(parent, empty=False)
    path = parent / row["request_id"]
    _retained_path(path, empty=True)
    return row["request_id"], path


def _retained_path(path: Path, *, empty: bool) -> Path:
    try:
        if (
            not path.is_absolute()
            or any(ord(c) < 32 for c in str(path))
            or path.resolve(strict=True) != path
            or not path.is_dir()
            or path.stat().st_mode & 0o077
            or hasattr(os, "getuid")
            and path.stat().st_uid != os.getuid()
        ):
            raise ReviewInputError("controlled restart retained directory is invalid")
        for name in ("RUNNER_TEMP", "GITHUB_WORKSPACE"):
            root = os.environ.get(name)
            if not root:
                raise ReviewInputError("controlled restart runner paths are missing")
            if path.is_relative_to(Path(root).resolve(strict=True)):
                raise ReviewInputError(
                    "controlled restart archive cannot use ephemeral runner storage"
                )
        if empty and any(path.iterdir()):
            raise ReviewInputError("controlled restart retained slot is not empty")
    except OSError as error:
        raise ReviewInputError(
            "controlled restart retained directory is unavailable"
        ) from error
    return path


def retained_directory() -> Path:
    request = os.environ.get("CONTROLLED_RESTART_ID", "")
    value = os.environ.get("CONTROLLED_RESTART_DIRECTORY", "")
    if (
        re.fullmatch(r"[a-f0-9]{64}", request) is None
        or os.environ.get("MODE") != "manual"
        or os.environ.get("OPERATION") != "review"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("RUNNER_ENVIRONMENT") != "self-hosted"
        or os.environ.get("QUARANTINE_PREDECESSOR_COMMENT_ID", "")
        or not value
    ):
        raise ReviewInputError(
            "controlled restart requires a manual self-hosted retained directory"
        )
    path = Path(value)
    if path.name != request:
        raise ReviewInputError("controlled restart retained directory is invalid")
    return _retained_path(path, empty=True)
