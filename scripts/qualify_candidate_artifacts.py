#!/usr/bin/env python3
"""Qualify exact, unpublished native/npm candidate artifacts from one CI head."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from build_standalone import validate_host_target
from prepare_npm_packages import (
    LAUNCHER_NAME,
    TARGETS,
    assemble_packages,
    project_version,
)
from standalone_smoke import clean_host_environment, run_smoke, smoke_fixture_root
from validate_npm_package import validate

MAX_FILES = 20_000
MAX_BYTES = 2 * 1024**3


class CandidateError(ValueError):
    """A candidate is incomplete, inconsistent, or fails its consumer contract."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def npm_isolation(directory: Path) -> list[str]:
    """Exclude ambient npm credentials/config and any registry fallback."""
    user = directory / "empty-user.npmrc"
    global_config = directory / "empty-global.npmrc"
    user.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    return [
        "--cache",
        str(directory / "empty-npm-cache"),
        "--userconfig",
        str(user),
        "--globalconfig",
        str(global_config),
        "--registry=http://127.0.0.1:9",
    ]


def source_identity(root: Path, expected: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-f0-9]{40}", expected):
        raise CandidateError("candidate source must be one exact commit SHA")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    if actual != expected:
        raise CandidateError("candidate source does not match the checked-out head")
    changes = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    if changes:
        raise CandidateError("candidate source worktree must be clean")
    return {"source_sha": actual, "version": project_version(root)}


def inventory(root: Path) -> list[dict[str, object]]:
    if root.is_symlink() or not root.is_dir():
        raise CandidateError("native bundle must be a regular directory")
    files = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise CandidateError("native bundle contains a non-regular member")
        if path.is_file():
            total += path.stat().st_size
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": digest(path),
                }
            )
            if len(files) > MAX_FILES or total > MAX_BYTES:
                raise CandidateError("native bundle exceeds candidate receipt bounds")
    if not files:
        raise CandidateError("native bundle is empty")
    return files


def materialize_bundle(source: Path, destination: Path) -> list[dict[str, str]]:
    """Fit PyInstaller's internal framework links to npm's regular-file contract."""
    if source.is_symlink() or not source.is_dir():
        raise CandidateError("native bundle must be a regular directory")
    root = source.resolve()
    links = []
    count = total = 0

    def copy(path: Path, target: Path, ancestors: frozenset[Path]) -> None:
        nonlocal count, total
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or resolved in ancestors:
            raise CandidateError(
                "native bundle link escapes its root or creates a cycle"
            )
        if path.is_symlink():
            links.append(
                {
                    "path": path.relative_to(source).as_posix(),
                    "target": str(path.readlink()),
                }
            )
        if path.is_dir():
            target.mkdir(parents=True)
            for child in sorted(path.iterdir()):
                copy(child, target / child.name, ancestors | {resolved})
            shutil.copystat(path, target)
        elif path.is_file():
            count += 1
            total += path.stat().st_size
            if count > MAX_FILES or total > MAX_BYTES:
                raise CandidateError(
                    "materialized native bundle exceeds receipt bounds"
                )
            shutil.copy2(path, target)
        else:
            raise CandidateError("native bundle contains a non-regular member")

    copy(source, destination, frozenset())
    return links


def archive_native(
    root: Path, target: str, bundle: Path, output: Path, source: str
) -> Path:
    identity = source_identity(root, source)
    if target not in TARGETS:
        raise CandidateError("unsupported candidate target")
    executable = Path(str(TARGETS[target]["payload"])).name
    if not (bundle / executable).is_file():
        raise CandidateError("native bundle executable is missing")
    output.mkdir(parents=True, exist_ok=False)
    archive = output / (target + ".tar.gz")
    with tempfile.TemporaryDirectory(
        prefix="reviewsensei-native-snapshot-"
    ) as temporary:
        snapshot = Path(temporary) / "review-sensei"
        links = materialize_bundle(bundle, snapshot)
        files = inventory(snapshot)
        with tarfile.open(archive, "w:gz", dereference=True) as stream:
            stream.add(snapshot, arcname="review-sensei")
        receipt = {
            **identity,
            "schema_version": 1,
            "kind": "native-candidate",
            "target": target,
            "normalization": "internal-symlinks-materialized-v1",
            "materialized_symlinks": links,
            "archive": archive.name,
            "archive_sha256": digest(archive),
            "files": files,
            "executable_sha256": digest(snapshot / executable),
        }
    write_json(output / "native-receipt.json", receipt)
    return archive


def unpack_native(archive: Path, output: Path) -> Path:
    """Restore modes without allowing archive links, traversal or unbounded expansion."""
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        seen = set()
        total = 0
        if len(members) > MAX_FILES * 2:
            raise CandidateError("candidate archive contains too many members")
        for item in members:
            path = PurePosixPath(item.name)
            if (
                not path.parts
                or path.parts[0] != "review-sensei"
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in item.name
                or not (item.isfile() or item.isdir())
                or item.name in seen
            ):
                raise CandidateError("candidate archive contains an unsafe member")
            seen.add(item.name)
            total += item.size
            if total > MAX_BYTES:
                raise CandidateError("candidate archive exceeds expanded byte bounds")
        stream.extractall(output, members=members, filter="data")
    return output / "review-sensei"


def assemble_candidate(root: Path, native: Path, output: Path, source: str) -> Path:
    identity = source_identity(root, source)
    if output.exists():
        raise CandidateError("candidate package output must be new")
    output.mkdir(parents=True)
    receipts = {}
    bundles = {}
    for path in sorted(native.rglob("native-receipt.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        target = receipt.get("target")
        if (
            target not in TARGETS
            or target in receipts
            or any(receipt.get(k) != v for k, v in identity.items())
        ):
            raise CandidateError(
                "candidate native inventory has wrong source, target or duplicate"
            )
        if (
            receipt.get("schema_version") != 1
            or receipt.get("kind") != "native-candidate"
        ):
            raise CandidateError("candidate native receipt version is unsupported")
        archive = path.parent / (target + ".tar.gz")
        if receipt.get("archive") != archive.name or digest(archive) != receipt.get(
            "archive_sha256"
        ):
            raise CandidateError("candidate native archive digest does not match")
        bundles[target] = unpack_native(archive, output / "native" / target)
        if inventory(bundles[target]) != receipt.get("files"):
            raise CandidateError("candidate native file inventory does not match")
        receipts[target] = receipt
    if set(receipts) != set(TARGETS):
        raise CandidateError("candidate requires all five native targets")
    assembled = assemble_packages(root, output / "assembled", bundles)
    validate(assembled, expected_version=identity["version"])
    packed = output / "packed"
    packed.mkdir()
    packages = []
    isolation = npm_isolation(output.resolve())
    for target in ("cli", *TARGETS):
        result = subprocess.run(
            [
                shutil.which("npm") or "npm",
                "pack",
                "--ignore-scripts",
                *isolation,
                "--json",
                "--pack-destination",
                str(packed.resolve()),
                str((assembled / target).resolve()),
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        metadata = json.loads(result.stdout)
        if not isinstance(metadata, list) or len(metadata) != 1:
            raise CandidateError("npm pack returned an invalid receipt")
        name = metadata[0].get("filename")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".tgz")
        ):
            raise CandidateError("npm pack returned an invalid filename")
        tarball = packed / name
        validated = validate(tarball, expected_version=identity["version"])
        expected_name = LAUNCHER_NAME if target == "cli" else TARGETS[target]["package"]
        if validated["name"] != expected_name:
            raise CandidateError("packed candidate has the wrong package identity")
        packages.append(
            {
                "target": target,
                "name": expected_name,
                "file": name,
                "sha256": digest(tarball),
            }
        )
    write_json(
        packed / "candidate-packages.json",
        {
            **identity,
            "schema_version": 1,
            "kind": "unpublished-candidate",
            "native": receipts,
            "packages": packages,
        },
    )
    return packed


def verify_packed(root: Path, packed: Path, source: str) -> dict:
    identity = source_identity(root, source)
    receipt = json.loads(
        (packed / "candidate-packages.json").read_text(encoding="utf-8")
    )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "unpublished-candidate"
        or any(receipt.get(k) != v for k, v in identity.items())
    ):
        raise CandidateError(
            "packed candidate source or receipt version does not match"
        )
    entries = receipt.get("packages", [])
    if len(entries) != 6 or {item.get("target") for item in entries} != {
        "cli",
        *TARGETS,
    }:
        raise CandidateError("packed candidate requires exactly six packages")
    if set(receipt.get("native", {})) != set(TARGETS):
        raise CandidateError("packed candidate is missing native provenance")
    for target, native in receipt["native"].items():
        if (
            native.get("kind") != "native-candidate"
            or native.get("schema_version") != 1
            or native.get("target") != target
            or any(native.get(k) != v for k, v in identity.items())
        ):
            raise CandidateError("packed candidate native provenance does not match")
    for item in entries:
        name = item.get("file")
        if not isinstance(name, str) or Path(name).name != name:
            raise CandidateError("packed candidate filename is invalid")
        path = packed / name
        expected_name = (
            LAUNCHER_NAME
            if item["target"] == "cli"
            else TARGETS[item["target"]]["package"]
        )
        if item.get("name") != expected_name or digest(path) != item.get("sha256"):
            raise CandidateError("packed candidate identity or digest does not match")
        if (
            validate(path, expected_version=identity["version"])["name"]
            != expected_name
        ):
            raise CandidateError("packed candidate archive identity does not match")
    return receipt


def consume_candidate(
    root: Path, packed: Path, target: str, output: Path, source: str
) -> None:
    validate_host_target(target)
    receipt = verify_packed(root, packed, source)
    if output.exists():
        raise CandidateError("candidate consumer output must be new")
    output.mkdir(parents=True)
    by_target = {item["target"]: item for item in receipt["packages"]}
    with tempfile.TemporaryDirectory(prefix="reviewsensei-candidate-") as scratch:
        scratch = Path(scratch)
        consumer = scratch / "consumer"
        consumer.mkdir()
        selected = [by_target[item] for item in ("cli", target)]
        write_json(
            consumer / "package.json",
            {
                "name": "reviewsensei-candidate-consumer",
                "private": True,
                "dependencies": {
                    item["name"]: "file:" + str((packed / item["file"]).resolve())
                    for item in selected
                },
            },
        )
        environment = clean_host_environment(scratch)
        environment.pop("PYTHONPATH", None)
        subprocess.run(
            [
                shutil.which("npm") or "npm",
                "install",
                "--offline",
                "--ignore-scripts",
                "--omit=optional",
                "--no-audit",
                "--no-fund",
                "--package-lock=false",
                *npm_isolation(scratch),
            ],
            cwd=consumer,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        platform = consumer / "node_modules" / str(TARGETS[target]["package"])
        if inventory(platform / "bin") != receipt["native"][target]["files"]:
            raise CandidateError(
                "installed native bytes differ from the built candidate"
            )
        executable = platform / str(TARGETS[target]["payload"])
        if digest(executable) != receipt["native"][target]["executable_sha256"]:
            raise CandidateError(
                "installed executable digest differs from native receipt"
            )
        launcher = consumer / "node_modules" / LAUNCHER_NAME / "bin/review-sensei.js"
        native_cases = run_smoke(
            executable,
            repository=root,
            diff=root / "evaluation/v1/diffs/off-by-one.patch",
            fixture_response=root / "evaluation/v1/responses/off-by-one.json",
        )
        fixtures = smoke_fixture_root()
        cases = [
            ("version", ["--version"], 0),
            ("help", ["--help"], 0),
            (
                "required-fix",
                [
                    "--provider",
                    "fixture",
                    "--diff",
                    str(fixtures / "blocking-review.patch"),
                    "--fixture-response",
                    str(fixtures / "blocking-review.json"),
                    "--no-learning-proposals",
                    "--format",
                    "json",
                ],
                1,
            ),
        ]
        launcher_cases = []
        for name, args, expected in cases:
            result = subprocess.run(
                [shutil.which("node") or "node", str(launcher), *args],
                cwd=scratch,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != expected:
                raise CandidateError(
                    f"installed launcher {name} returned {result.returncode}, expected {expected}"
                )
            if name == "version" and receipt["version"] not in result.stdout:
                raise CandidateError("installed launcher reports the wrong version")
            if (
                name == "required-fix"
                and json.loads(result.stdout).get("review_status") != "complete"
            ):
                raise CandidateError(
                    "installed launcher did not produce a complete review"
                )
            launcher_cases.append(
                {
                    "case": name,
                    "exit": result.returncode,
                    "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                }
            )
        write_json(
            output / (target + ".json"),
            {
                "schema_version": 1,
                "kind": "unpublished-candidate-consumer",
                "source_sha": source,
                "version": receipt["version"],
                "target": target,
                "platform": sys.platform,
                "package_receipt_sha256": digest(packed / "candidate-packages.json"),
                "packages": selected,
                "executable_sha256": digest(executable),
                "native_cases": native_cases,
                "launcher_cases": launcher_cases,
                "boundary": "matching CI runner, offline local tarballs; not registry installation or release qualification",
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("archive", "assemble", "consume"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        if args.operation == "archive":
            if args.target is None:
                raise CandidateError("archive requires a target")
            archive_native(root, args.target, args.input, args.output, args.source_sha)
        elif args.operation == "assemble":
            assemble_candidate(root, args.input, args.output, args.source_sha)
        else:
            if args.target is None:
                raise CandidateError("consume requires a target")
            consume_candidate(
                root, args.input.resolve(), args.target, args.output, args.source_sha
            )
    except (CandidateError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(
            f"candidate qualification failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
