"""Bounded read-only Git reconstruction of the original default caller context.

No historical code, hook, filter, diff driver, fetch, checkout or credentials are
executed. The lifecycle independently authenticates the selected P/B/H and old
configuration digest. Materialized text remains untrusted review context.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from review_sensei.api_work import FrozenApiConfiguration
from review_sensei.bounded_evidence import MAX_PARTITION_DECODED_BYTES
from review_sensei.configuration import MAX_CONFIG_BYTES
from review_sensei.context import (
    MAX_CONTEXT_FILE_BYTES,
    MAX_DOCUMENT_DISCOVERY_ENTRIES,
    _matches,
)
from review_sensei.errors import ReviewInputError
from review_sensei.service import DEFAULT_STAGES
from review_sensei.validation import DEFAULT_REVIEW_LIMITS, validate_repository_path


@dataclass(frozen=True)
class FrozenPublicationSource:
    root: Path
    policy_commit: str
    base_sha: str
    head_sha: str
    diff: str
    files: tuple[tuple[str, str, str], ...]

    @property
    def diff_sha256(self):
        return hashlib.sha256(self.diff.encode()).hexdigest()

    def to_document(self, *, include_file_text=True):
        return {
            "schema_version": "quarantine-original-context-source-v1"
            if include_file_text
            else "quarantine-original-context-source-v2",
            "policy_commit": self.policy_commit,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "diff": self.diff,
            "files": [
                {
                    "path": path,
                    "git_blob": blob,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "bytes": len(text.encode()),
                    "text": text
                    if include_file_text or path == ".reviewsensei.yml"
                    else None,
                }
                for path, blob, text in self.files
            ],
        }


def _git(repository, arguments, maximum, deadline):
    # Prevent partial-clone lazy fetching and ambient replacement objects. All
    # commands below are read-only object/plumbing operations or a safe diff.
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environ.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReviewInputError("original context local read deadline exhausted")
    try:
        process = subprocess.Popen(
            [
                "git",
                "--no-pager",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "core.quotePath=true",
                "-c",
                "diff.external=",
                *arguments,
            ],
            cwd=repository,
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ReviewInputError("original context local Git is unavailable") from exc
    pipe = process.stdout
    if pipe is None:
        process.kill()
        process.wait()
        raise ReviewInputError("original context Git pipe is unavailable")
    output = []
    failed = []

    def drain():
        try:
            raw = pipe.read(maximum + 1)
            output.append(raw)
            if len(raw) > maximum:
                process.kill()
        except (OSError, ValueError) as exc:
            failed.append(exc)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise ReviewInputError(
            "original context local read deadline exhausted"
        ) from exc
    finally:
        reader.join(timeout=1)
        pipe.close()
    if (
        reader.is_alive()
        or failed
        or not output
        or process.returncode
        or len(output[0]) > maximum
    ):
        raise ReviewInputError(
            "original context local object is unavailable or exceeds its bound"
        )
    return output[0]


@contextmanager
def frozen_publication_source(repository, configuration, *, base_sha, head_sha):
    """Expose exact frozen default context, preserving the current dirty tree.

    The diff follows the recognized caller's three-dot Git operation. Original
    retained diff media is not silently substituted: source-qualified acquisition
    and original transaction/context checks are still required by the lifecycle.
    Missing objects refuse; the lifecycle must provision them through its normal
    trusted checkout before entering this read-only operation.
    """
    if (
        not isinstance(repository, Path)
        or not repository.is_dir()
        or not isinstance(configuration, FrozenApiConfiguration)
        or any(
            not isinstance(v, str) or re.fullmatch(r"[a-f0-9]{40}", v) is None
            for v in (configuration.commit, base_sha, head_sha)
        )
    ):
        raise ReviewInputError("original context Git scope is invalid")
    deadline = time.monotonic() + 30
    for commit in dict.fromkeys((configuration.commit, base_sha, head_sha)):
        if _git(repository, ["cat-file", "-t", commit], 16, deadline) != b"commit\n":
            raise ReviewInputError("original context revision is not a commit")
    sources = {
        source
        for stage in DEFAULT_STAGES
        for category in stage.categories
        for source in category.document_sources
    }
    roots = sorted(
        {
            ".reviewsensei.yml",
            ".github/review-sensei/learnings",
            *(source.path for source in sources),
        }
    )
    inventory = _git(
        repository,
        [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            configuration.commit,
            "--",
            *(":(top,literal)" + path for path in roots),
        ],
        1_048_576,
        deadline,
    )
    rows = inventory.split(b"\0")
    if rows[-1:] != [b""] or len(rows) - 1 > MAX_DOCUMENT_DISCOVERY_ENTRIES:
        raise ReviewInputError(
            "original context Git inventory is incomplete or oversized"
        )
    selected = []
    seen = set()
    for row in rows[:-1]:
        try:
            metadata, raw_path = row.split(b"\t", 1)
            mode, kind, raw_blob = metadata.decode("ascii").split(" ")
            path, blob = raw_path.decode("utf-8"), raw_blob
        except (ValueError, UnicodeError) as exc:
            raise ReviewInputError("original context Git entry is malformed") from exc
        validate_repository_path(
            path, allow_glob_chars=True, label="original context path"
        )
        if path in seen or re.fullmatch(r"[a-f0-9]{40}", blob) is None:
            raise ReviewInputError(
                "original context Git entry is duplicated or invalid"
            )
        seen.add(path)
        included = path == ".reviewsensei.yml" or (
            path.startswith(".github/review-sensei/learnings/")
            and Path(path).parent.as_posix() == ".github/review-sensei/learnings"
            and path.endswith(".json")
        )
        for source in sources:
            if path == source.path:
                included = True
            elif path.startswith(source.path + "/"):
                relative = path[len(source.path) + 1 :]
                if kind != "blob" or mode not in {"100644", "100755"}:
                    raise ReviewInputError(
                        "original context sources cannot contain links"
                    )
                included |= any(
                    _matches(relative, p) for p in source.include
                ) and not any(_matches(relative, p) for p in source.exclude)
        if not included:
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ReviewInputError("original context source is not a regular file")
        selected.append((path, blob))
    if ".reviewsensei.yml" not in {path for path, _ in selected}:
        raise ReviewInputError("original frozen configuration is missing")
    diff_raw = _git(
        repository,
        ["diff", "--no-ext-diff", "--no-textconv", base_sha + "..." + head_sha],
        DEFAULT_REVIEW_LIMITS.max_diff_bytes,
        deadline,
    )
    try:
        diff = diff_raw.decode("utf-8")
    except UnicodeError as exc:
        raise ReviewInputError("original Git diff is not UTF-8") from exc
    with tempfile.TemporaryDirectory(
        prefix="reviewsensei-original-context-"
    ) as temporary:
        root = Path(temporary)
        files = []
        total = len(diff_raw)
        for path, blob in sorted(selected):
            limit = (
                MAX_CONFIG_BYTES
                if path == ".reviewsensei.yml"
                else MAX_CONTEXT_FILE_BYTES
            )
            size = _git(repository, ["cat-file", "-s", blob], 32, deadline)
            if not size.strip().isdigit() or int(size) > limit:
                raise ReviewInputError(
                    "original context file exceeds its existing bound"
                )
            raw = _git(repository, ["cat-file", "blob", blob], limit, deadline)
            if (
                len(raw) != int(size)
                or hashlib.sha1(
                    b"blob " + str(len(raw)).encode() + b"\0" + raw
                ).hexdigest()
                != blob
            ):
                raise ReviewInputError("original context blob readback conflicts")
            if (
                path == ".reviewsensei.yml"
                and hashlib.sha256(raw).hexdigest() != configuration.digest
            ):
                raise ReviewInputError("original frozen configuration digest conflicts")
            total += len(raw)
            if total > MAX_PARTITION_DECODED_BYTES:
                raise ReviewInputError(
                    "combined original context exceeds its carrier bound"
                )
            try:
                text = raw.decode("utf-8")
            except UnicodeError as exc:
                raise ReviewInputError("original context source is not UTF-8") from exc
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            files.append((path, blob, text))
        yield FrozenPublicationSource(
            root, configuration.commit, base_sha, head_sha, diff, tuple(files)
        )
