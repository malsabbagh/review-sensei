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
    try:
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not path.is_dir()
            or path.name != request
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
        if any(path.iterdir()):
            raise ReviewInputError("controlled restart retained slot is not empty")
    except OSError as error:
        raise ReviewInputError(
            "controlled restart retained directory is unavailable"
        ) from error
    return path
