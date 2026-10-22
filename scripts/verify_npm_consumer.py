#!/usr/bin/env python3
"""Verify an exact public npm release through its installed native launcher."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from prepare_npm_packages import TARGETS, project_version

NPM_VERSION = "11.16.0"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
REPOSITORY = "https://github.com/malsabbagh/review-sensei"
PLATFORMS = {
    "darwin-arm64": ("darwin", "arm64"),
    "darwin-x64": ("darwin", "x64"),
    "linux-arm64-gnu": ("linux", "arm64"),
    "linux-x64-gnu": ("linux", "x64"),
    "win32-x64": ("win32", "x64"),
}


class ConsumerVerificationError(ValueError):
    """A public consumer did not satisfy the exact release contract."""


def read_json(path: Path) -> dict:
    if path.stat().st_size > MAX_OUTPUT_BYTES:
        raise ConsumerVerificationError("consumer JSON exceeds the receipt bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ConsumerVerificationError("consumer JSON must be an object")
    return value


def consumer_environment(directory: Path, node: Path) -> dict[str, str]:
    """Allow only machine loader state and fresh consumer-owned state."""
    environment = {
        name: os.environ[name]
        for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT")
        if name in os.environ
    }
    for name in ("home", "tmp", "cache"):
        (directory / name).mkdir()
    for name in ("user.npmrc", "global.npmrc"):
        (directory / name).write_text("", encoding="utf-8")
    environment.update(
        {
            "PATH": str(node.parent),
            "HOME": str(directory / "home"),
            "USERPROFILE": str(directory / "home"),
            "TMPDIR": str(directory / "tmp"),
            "TEMP": str(directory / "tmp"),
            "TMP": str(directory / "tmp"),
            "NPM_CONFIG_USERCONFIG": str(directory / "user.npmrc"),
            "NPM_CONFIG_GLOBALCONFIG": str(directory / "global.npmrc"),
            "NPM_CONFIG_CACHE": str(directory / "cache"),
            "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
        }
    )
    return environment


def npm_cli_path(node: Path) -> Path:
    npm = shutil.which("npm")
    if npm is not None:
        resolved = Path(npm).resolve()
        if resolved.name == "npm-cli.js":
            return resolved
    candidate = node.parent / "node_modules/npm/bin/npm-cli.js"
    if not candidate.is_file():
        raise ConsumerVerificationError("Node.js must provide a bootstrap npm CLI")
    return candidate


def launcher_command(
    prefix: Path, launcher: Path, node: Path, target: str
) -> list[str]:
    shim = prefix / "node_modules/.bin/review-sensei"
    if target == "win32-x64":
        cmd = shim.with_suffix(".cmd")
        if (
            cmd.stat().st_size > 64 * 1024
            or '"%dp0%/../@reviewsensei/cli/bin/review-sensei.js"'
            not in cmd.read_text(encoding="utf-8").replace("\\", "/")
        ):
            raise ConsumerVerificationError(
                "installed Windows command does not own the launcher"
            )
        # Validate npm's cmd ownership, then avoid introducing a command shell.
        return [str(node), str(launcher)]
    if not shim.is_file() or shim.resolve() != launcher.resolve():
        raise ConsumerVerificationError(
            "installed npm command does not own the launcher"
        )
    return [str(shim)]


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    stdout: Path,
    stderr: Path,
) -> int:
    with stdout.open("wb") as out, stderr.open("wb") as err:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=out,
            stderr=err,
            timeout=timeout,
            check=False,
            shell=False,
        )
    return completed.returncode


def verify_provenance(
    audit: dict, lock: dict, *, version: str, source_sha: str, target: str
) -> None:
    expected = {"@reviewsensei/cli", str(TARGETS[target]["package"])}
    verified = audit.get("verified")
    if (
        audit.get("invalid") != []
        or audit.get("missing") != []
        or not isinstance(verified, list)
    ):
        raise ConsumerVerificationError(
            "npm signatures or attestations were not verified"
        )
    if {item.get("name") for item in verified} != expected or len(verified) != 2:
        raise ConsumerVerificationError(
            "signature audit must cover both installed packages"
        )
    for item in verified:
        if item.get("version") != version:
            raise ConsumerVerificationError(
                "signature audit version differs from the release"
            )
        package = lock["packages"][item["location"]]
        integrity = package.get("integrity", "")
        if not integrity.startswith("sha512-"):
            raise ConsumerVerificationError("installed package integrity is missing")
        digest = base64.b64decode(integrity[7:], validate=True).hex()
        statements = []
        for wrapped in item.get("attestationBundles", ()):
            envelope = wrapped.get("bundle", {}).get("dsseEnvelope", {})
            statement = json.loads(base64.b64decode(envelope["payload"], validate=True))
            if statement.get("predicateType") == "https://slsa.dev/provenance/v1":
                statements.append(statement)
        if len(statements) != 1:
            raise ConsumerVerificationError(
                "verified package provenance is missing or ambiguous"
            )
        statement = statements[0]
        subjects = statement.get("subject", ())
        definition = statement.get("predicate", {}).get("buildDefinition", {})
        workflow = definition.get("externalParameters", {}).get("workflow", {})
        dependencies = definition.get("resolvedDependencies", ())
        if (
            not subjects
            or any(s.get("digest", {}).get("sha512") != digest for s in subjects)
            or workflow
            != {
                "ref": f"refs/tags/v{version}",
                "repository": REPOSITORY,
                "path": ".github/workflows/publish-npm.yml",
            }
            or not any(
                d.get("uri") == f"git+{REPOSITORY}@refs/tags/v{version}"
                and d.get("digest", {}).get("gitCommit") == source_sha
                for d in dependencies
            )
        ):
            raise ConsumerVerificationError(
                "verified npm provenance differs from signed source or installed bytes"
            )


def verify_consumer(
    *,
    root: Path,
    version: str,
    source_sha: str,
    target: str,
    receipt_dir: Path,
    node: Path,
    npm_cli: Path,
    runner: Callable = run_command,
) -> dict:
    directory = receipt_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema": 1,
        "version": version,
        "source_sha": source_sha,
        "target": target,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
        "successful": False,
    }

    def save() -> None:
        (directory / "receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )

    def step(
        name: str, arguments: list[str], *, expected: int = 0, timeout: int = 120
    ) -> str:
        stdout, stderr = directory / f"{name}.stdout", directory / f"{name}.stderr"
        entry = {
            "name": name,
            "command": arguments,
            "expected_exit": expected,
            "stdout": stdout.name,
            "stderr": stderr.name,
            "timeout_seconds": timeout,
        }
        receipt["steps"].append(entry)
        save()
        oversized = False
        try:
            code = runner(
                arguments,
                cwd=work,
                env=environment,
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
            )
            entry["exit_code"] = code
        finally:
            for stream, path in (("stdout", stdout), ("stderr", stderr)):
                if path.is_file():
                    size = path.stat().st_size
                    entry[f"{stream}_bytes"] = size
                    entry[f"{stream}_truncated"] = size > MAX_OUTPUT_BYTES
                    if size > MAX_OUTPUT_BYTES:
                        oversized = True
                        with path.open("r+b") as output:
                            output.truncate(MAX_OUTPUT_BYTES)
            save()
        if oversized:
            raise ConsumerVerificationError(f"{name} output exceeds the receipt bound")
        if code != expected:
            raise ConsumerVerificationError(
                f"{name} exit {code} differs from expected {expected}"
            )
        return stdout.read_text(encoding="utf-8")

    save()
    try:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(
            r"[a-f0-9]{40}", source_sha
        ):
            raise ConsumerVerificationError(
                "exact version and source commit are required"
            )
        if target not in TARGETS or project_version(root) != version:
            raise ConsumerVerificationError(
                "consumer version differs from qualified source"
            )
        node, npm_cli = node.resolve(), npm_cli.resolve()
        if not node.is_file() or not npm_cli.is_file():
            raise ConsumerVerificationError(
                "Node.js and bootstrap npm must be installed"
            )
        environment = consumer_environment(directory, node)
        work = directory / "work"
        work.mkdir()
        git = shutil.which("git")
        if (
            git is None
            or step("source", [git, "-C", str(root), "rev-parse", "HEAD"]).strip()
            != source_sha
        ):
            raise ConsumerVerificationError(
                "consumer fixtures differ from qualified source"
            )
        platform = json.loads(
            step(
                "platform",
                [
                    str(node),
                    "-p",
                    "JSON.stringify({platform:process.platform,arch:process.arch,node:process.version,glibc:process.report.getReport().header.glibcVersionRuntime||null})",
                ],
            )
        )
        if (platform.get("platform"), platform.get("arch")) != PLATFORMS[target] or (
            target.startswith("linux-") and not platform.get("glibc")
        ):
            raise ConsumerVerificationError(
                "consumer runner differs from the requested target"
            )
        receipt["platform"] = platform
        tooling = directory / "tooling"
        step(
            "npm-tooling",
            [
                str(node),
                str(npm_cli),
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--prefix",
                str(tooling),
                f"npm@{NPM_VERSION}",
            ],
            timeout=300,
        )
        npm = [str(node), str(tooling / "node_modules/npm/bin/npm-cli.js")]
        if step("npm-version", [*npm, "--version"]).strip() != NPM_VERSION:
            raise ConsumerVerificationError(
                "npm verification tool version differs from its pin"
            )
        prefix = directory / "prefix"
        step(
            "install",
            [
                *npm,
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--prefix",
                str(prefix),
                f"@reviewsensei/cli@{version}",
            ],
            timeout=300,
        )
        lock = read_json(prefix / "package-lock.json")
        shutil.copyfile(prefix / "package-lock.json", directory / "package-lock.json")
        receipt["package_lock_sha256"] = hashlib.sha256(
            (directory / "package-lock.json").read_bytes()
        ).hexdigest()
        packages = prefix / "node_modules/@reviewsensei"
        launcher_manifest = read_json(packages / "cli/package.json")
        native_manifest = read_json(packages / f"cli-{target}/package.json")
        if (
            launcher_manifest.get("name") != "@reviewsensei/cli"
            or launcher_manifest.get("version") != version
            or native_manifest.get("name") != TARGETS[target]["package"]
            or native_manifest.get("version") != version
            or launcher_manifest.get("optionalDependencies")
            != {str(t["package"]): version for t in TARGETS.values()}
        ):
            raise ConsumerVerificationError(
                "installed launcher/native package identity differs from release"
            )
        launcher = packages / "cli/bin/review-sensei.js"
        if not launcher.is_file() or launcher_manifest.get("bin") != {
            "review-sensei": "bin/review-sensei.js"
        }:
            raise ConsumerVerificationError(
                "installed launcher entry point is unavailable"
            )
        command = launcher_command(prefix, launcher, node, target)
        receipt["entrypoint"] = (
            "owned-cmd-target-via-node" if target == "win32-x64" else "owned-npm-bin"
        )
        if step("version", [*command, "--version"]).strip() != version:
            raise ConsumerVerificationError("installed CLI reported another version")
        if "--diff" not in step("help", [*command, "--help"]):
            raise ConsumerVerificationError("installed CLI help lacks review interface")
        corpus = root / "evaluation/v1/corpus.json"
        receipt["corpus_sha256"] = hashlib.sha256(corpus.read_bytes()).hexdigest()
        evaluation = directory / "evaluation-report.json"
        invalid = work / "invalid.patch"
        invalid.write_text("not a unified diff\n", encoding="utf-8")
        step(
            "evaluate-fixture",
            [
                *command,
                "evaluate",
                "--mode",
                "fixture",
                "--corpus",
                str(corpus),
                "--output",
                str(evaluation),
            ],
        )
        report = read_json(evaluation)
        if (
            report.get("passed") is not True
            or report.get("threshold_failures") != []
            or report.get("run", {}).get("mode") != "fixture"
        ):
            raise ConsumerVerificationError(
                "installed CLI deterministic fixture evaluation failed"
            )
        receipt["evaluation"] = {
            key: report[key]
            for key in ("deterministic", "quality", "threshold_failures")
        }
        fixture = root / "tests/fixtures/standalone-smoke"
        review = [
            *command,
            "--provider",
            "fixture",
            "--fixture-response",
            str(fixture / "blocking-review.json"),
            "--diff",
            str(fixture / "blocking-review.patch"),
            "--no-learning-proposals",
            "--format",
            "json",
        ]
        expected_review = read_json(fixture / "blocking-review.json")
        for comment in expected_review["comments"]:
            if comment.get("side") == "RIGHT":
                comment.pop("side")
        for label, extra, code in (
            ("review-required-fix", [], 1),
            ("review-operational", ["--exit-semantics", "operational"], 0),
        ):
            document = directory / f"{label}.json"
            step(label, [*review, *extra, "--output", str(document)], expected=code)
            reviewed = read_json(document)
            if (
                reviewed.get("summary") != expected_review["summary"]
                or reviewed.get("comments") != expected_review["comments"]
            ):
                raise ConsumerVerificationError(
                    "installed CLI changed the exact blocking fixture findings"
                )
        step(
            "invalid-input",
            [
                *command,
                "--provider",
                "fixture",
                "--fixture-response",
                str(fixture / "blocking-review.json"),
                "--diff",
                str(invalid),
            ],
            expected=2,
        )
        audit_path = directory / "audit-signatures.json"
        audit_path.write_text(
            step(
                "audit-signatures",
                [
                    *npm,
                    "audit",
                    "signatures",
                    "--prefix",
                    str(prefix),
                    "--json",
                    "--include-attestations",
                ],
            ),
            encoding="utf-8",
        )
        verify_provenance(
            read_json(audit_path),
            lock,
            version=version,
            source_sha=source_sha,
            target=target,
        )
        receipt["installed"] = {
            "launcher": launcher_manifest["version"],
            "native": native_manifest["version"],
        }
        receipt["successful"] = True
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        receipt["failure"] = str(error)
        receipt["failure_type"] = type(error).__name__
    finally:
        receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
        lock_path = directory / "prefix/package-lock.json"
        retained_lock = directory / "package-lock.json"
        if lock_path.is_file() and lock_path.stat().st_size <= MAX_OUTPUT_BYTES:
            shutil.copyfile(lock_path, retained_lock)
        receipt.pop("package_lock_sha256", None)
        if retained_lock.is_file() and retained_lock.stat().st_size <= MAX_OUTPUT_BYTES:
            receipt["package_lock_sha256"] = hashlib.sha256(
                retained_lock.read_bytes()
            ).hexdigest()
        save()
        for name in ("prefix", "tooling", "cache", "home", "tmp", "work"):
            shutil.rmtree(directory / name, ignore_errors=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--target", choices=tuple(TARGETS), required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        node_path = shutil.which("node")
        if node_path is None:
            raise ConsumerVerificationError(
                "Node.js is required for npm consumer verification"
            )
        node = Path(node_path).resolve()
        result = verify_consumer(
            root=args.root.resolve(),
            version=args.version,
            source_sha=args.source_sha,
            target=args.target,
            receipt_dir=args.receipt_dir,
            node=node,
            npm_cli=npm_cli_path(node),
        )
    except (OSError, ValueError) as error:
        if not args.receipt_dir.exists():
            args.receipt_dir.mkdir(parents=True)
            (args.receipt_dir / "receipt.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "version": args.version,
                        "source_sha": args.source_sha,
                        "target": args.target,
                        "successful": False,
                        "failure_type": type(error).__name__,
                        "failure": str(error),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        print("npm consumer verification could not initialize", file=sys.stderr)
        return 1
    print(
        "npm consumer verification "
        + ("passed" if result["successful"] else "failed; see receipt")
    )
    return 0 if result["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
