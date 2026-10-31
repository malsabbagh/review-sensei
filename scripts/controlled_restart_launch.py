#!/usr/bin/env python3
"""Frozen-source, restart-only installer; no capability or PR API is available.

Run after the protected prerequisite, before materializing provider credentials.
Reuse the ordinary official release verifier against the LATER publication's
own source. Only its verified compiled row may bind that artifact back to this
actual frozen execution. Ordinary assert_current is intentionally unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pypi_channel_publication as publication
from review_sensei_github.controlled_restart_workflow import select_late_registration
from review_sensei_github.http import GitHubHttp

VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
REGISTRY = "review_sensei_github/controlled_restarts.json"


class ReleaseReads:
    """Bound only official release-provenance reads, never a PR/history census.

    These are installation control-plane reads like the existing signed-source
    installer, not a new business transport or a replacement lifecycle budget.
    The review lifecycle still owns its original 512 allowance and all tails.
    """

    def __init__(self):
        self.http = GitHubHttp(timeout=15)
        self.paths: list[str] = []
        self.release_runs: set[int] = set()
        self.deadline = time.monotonic() + 240

    def __enter__(self):
        self.original = publication.docs.api
        publication.docs.api = self.read
        return self

    def __exit__(self, *_):
        publication.docs.api = self.original

    def read(self, path):
        run = (
            re.fullmatch(r"actions/runs/([1-9][0-9]*)(?:/jobs\?[^\s#]+)?", path)
            if isinstance(path, str)
            else None
        )
        if (
            not isinstance(path, str)
            or len(path.encode()) > 8192
            or re.fullmatch(
                r"(?:git/(?:ref/tags|tags|commits|blobs)/[A-Za-z0-9._/-]+"
                r"|git/trees/[a-f0-9]{40}(?:\?recursive=1)?"
                r"|actions/workflows/release(?:-github)?\.yml/runs\?[^\s#]+"
                r"|actions/runs/[1-9][0-9]*(?:/jobs\?[^\s#]+)?)",
                path,
            )
            is None
            or ".." in path
            or run is not None
            and int(run[1]) not in self.release_runs
            or len(self.paths) >= 48
            or time.monotonic() >= self.deadline
        ):
            raise ValueError("restart release provenance read is outside its bound")
        self.paths.append(path)
        status, value = self.http.request(
            "GET",
            "/repos/malsabbagh/review-sensei/" + path,
            token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", ""),
        )
        if status != 200:
            raise ValueError("restart release provenance is unavailable")
        if path.startswith("actions/workflows/") and isinstance(value, dict):
            runs = value.get("workflow_runs")
            if not isinstance(runs, list):
                raise ValueError("release workflow inventory is unavailable")
            self.release_runs.update(
                item["id"]
                for item in runs
                if isinstance(item, dict)
                and type(item.get("id")) is int
                and item["id"] > 0
            )
        return value


def registry_bytes(raw: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        found = [entry for entry in archive.infolist() if entry.filename == REGISTRY]
        if len(found) != 1 or not 0 < found[0].file_size <= 32768:
            raise ValueError("verified hosted wheel has no unique bounded registry")
        with archive.open(found[0]) as stream:
            value = stream.read(32769)
        if len(value) != found[0].file_size or len(value) > 32768:
            raise ValueError("verified compiled registry size differs")
        return value


@dataclass(frozen=True)
class Prepared:
    request_id: str
    directory: Path
    source: str
    wheels: tuple[Path, ...]
    digests: tuple[str, ...]
    registry: bytes
    receipt: dict

    def require_unchanged(self):
        for path, expected in zip(self.wheels, self.digests, strict=True):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(
                    "verified restart artifact changed before installation"
                )


def prepare(*, core_version, hosted_version, parent, output, **scope):
    if not all(
        isinstance(v, str) and VERSION.fullmatch(v)
        for v in (core_version, hosted_version)
    ):
        raise ValueError("restart requires exact reserved release versions")
    _, source = publication.signed_release("github-v" + hosted_version)
    receipt = publication.qualify(source, core_version, hosted_version, output)
    if receipt.get("source") != source:
        raise ValueError("restart publication source changed")
    wheels, digests, raw_registry = [], [], None
    for project, version in (
        ("review-sensei", core_version),
        ("review-sensei-github", hosted_version),
    ):
        record = receipt["projects"][project]
        name = project.replace("-", "_") + "-" + version + "-py3-none-any.whl"
        item = record["artifacts"][name]
        raw = (output / name).read_bytes()
        if (
            record["version"] != version
            or len(raw) != item["size"]
            or hashlib.sha256(raw).hexdigest() != item["sha256"]
        ):
            raise ValueError("restart wheel differs from verified publication")
        wheels.append(output / name)
        digests.append(item["sha256"])
        if project == "review-sensei-github":
            if record["source"] != source:
                raise ValueError("restart hosted artifact is foreign")
            raw_registry = registry_bytes(raw)
    assert raw_registry is not None
    request, path = select_late_registration(raw_registry, parent=parent, **scope)
    return Prepared(
        request, path, source, tuple(wheels), tuple(digests), raw_registry, receipt
    )


def install(
    prepared: Prepared, *, python: Path, core_version: str, hosted_version: str
):
    # Revalidate mutable public names and local bytes; installation uses only
    # the already cryptographically verified wheels, never a resolver fallback.
    publication.assert_current(prepared.receipt)
    prepared.require_unchanged()
    subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            *map(str, prepared.wheels),
        ],
        check=True,
        timeout=120,
    )
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.metadata as m,hashlib,json; print(json.dumps([m.version('review-sensei'),m.version('review-sensei-github'),hashlib.sha256(m.distribution('review-sensei-github').locate_file('review_sensei_github/controlled_restarts.json').read_bytes()).hexdigest()]))",
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    if json.loads(result.stdout) != [
        core_version,
        hosted_version,
        hashlib.sha256(prepared.registry).hexdigest(),
    ]:
        raise ValueError("installed restart versions or compiled registry differ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--hosted-version", required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", type=int, required=True)
    parser.add_argument("--pull-request", type=int, required=True)
    for key in ("base-sha", "head-sha", "policy-commit", "workflow-sha", "job"):
        parser.add_argument("--" + key, required=True)
    args = vars(parser.parse_args())
    core, hosted = args.pop("core_version"), args.pop("hosted_version")
    python = args.pop("python")
    try:
        with ReleaseReads() as reads:
            prepared = prepare(core_version=core, hosted_version=hosted, **args)
            install(prepared, python=python, core_version=core, hosted_version=hosted)
        # No registration is loaded from this receipt. Admission reads only the
        # verified package's compiled row and independently checks live scope.
        with (args["output"] / "launch-result.json").open("x") as stream:
            json.dump(
                dict(
                    source=prepared.source,
                    request_id=prepared.request_id,
                    directory=str(prepared.directory),
                    release_reads=len(reads.paths),
                ),
                stream,
            )
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write("request_id=" + prepared.request_id + "\n")
            stream.write("directory=" + str(prepared.directory) + "\n")
        return 0
    except Exception:
        print(
            "controlled restart installation refused; no review entry authorized",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
