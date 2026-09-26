#!/usr/bin/env python3
"""Compare a native standalone CLI with the direct Python CLI contract."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


class SmokeError(ValueError):
    """Raised when smoke-test inputs or observable output are invalid."""


# Hosted-run identity and provider-specific defaults.  A local review must
# produce its documented output and exit code when none of these are present.
_CLEAN_HOST_PREFIXES = (
    "ACTIONS_",
    "GH_",
    "GITHUB_",
    "OLLAMA_",
    "OPENAI_",
    "OPENROUTER_",
    "REVIEWSENSEI_",
    "RUNNER_",
)


# State directories are replaced rather than inherited: the cases must run as
# a workstation session, not as the hosted runner's user.  Locale, loader, and
# PATH variables are kept because they describe the machine, not the session.
_CLEAN_HOST_OVERRIDDEN = (
    "HOME",
    "PWD",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERNAME",
    "USERPROFILE",
)


# Hosted-run annotations are gated on GitHub Actions identity; a clean-host
# case that printed one would still pass output parity, so every clean-host
# case asserts its stderr stays free of them.
_CLEAN_HOST_STDERR_EXCLUDES = ("::error",)


def clean_host_environment(root: Path) -> dict[str, str]:
    """Return a deterministic workstation-like environment sandboxed in ``root``.

    Hosted identity and provider defaults are dropped, and the per-user state
    directories point at a synthetic home and temporary directory instead of
    the caller's, so the smoke cases prove the documented local contract on a
    host that shares nothing with the runner (or the developer) that ran them.
    """

    home = root / "home"
    temporary = root / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "CI"
        and not name.startswith(_CLEAN_HOST_PREFIXES)
        and name not in _CLEAN_HOST_OVERRIDDEN
    }
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "USER": "review-sensei-smoke",
            "USERNAME": "review-sensei-smoke",
            "USERPROFILE": str(home),
        }
    )
    return environment


def smoke_fixture_root() -> Path:
    """Return the checkout fixture directory for the local smoke cases."""

    return (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "standalone-smoke"
    )


def validate_executable(path: Path) -> Path:
    if path.is_symlink():
        raise SmokeError("standalone executable must be a regular file")
    resolved = path.resolve()
    if not resolved.is_file():
        raise SmokeError("standalone executable must be a regular file")
    if resolved.suffix.lower() != ".exe" and not os.access(resolved, os.X_OK):
        raise SmokeError("standalone executable is not executable")
    return resolved


def _run(
    command: Sequence[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            # The CLI contract streams UTF-8; the runner's locale codec (for
            # example cp1252) may not decode the rendered emoji markers.
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SmokeError("standalone smoke command could not start") from exc


def _normalize(output: str, root: Path) -> str:
    # Paths in argparse errors can differ between the checkout and a copied
    # bundle. Normalize only the explicitly supplied repository root.
    return output.replace(str(root.resolve()), "<repository>")


def _consume_output(path: Path | None) -> bytes | None:
    """Read a generated file and remove it even when comparison later fails."""

    if path is None:
        return None
    try:
        return path.read_bytes() if path.is_file() else None
    finally:
        path.unlink(missing_ok=True)


def compare_case(
    label: str,
    arguments: Sequence[str],
    *,
    executable: Path,
    python_executable: str,
    repository: Path,
    standalone_env: dict[str, str] | None = None,
    output_path: Path | None = None,
    shared_env: dict[str, str] | None = None,
    expected_returncode: int | None = None,
    stderr_excludes: Sequence[str] | None = None,
) -> dict[str, object]:
    if output_path is not None and (output_path.exists() or output_path.is_symlink()):
        raise SmokeError(f"smoke output path already exists for {label}")
    baseline_env = dict(os.environ) if shared_env is None else dict(shared_env)
    native_env = dict(baseline_env)
    if standalone_env:
        native_env.update(standalone_env)
    native = _run([str(executable), *arguments], repository, native_env)
    native_output = _consume_output(output_path)
    direct = _run(
        [python_executable, "-m", "review_sensei", *arguments],
        repository,
        baseline_env,
    )
    direct_output = _consume_output(output_path)
    native_out = _normalize(native.stdout, repository)
    direct_out = _normalize(direct.stdout, repository)
    native_err = _normalize(native.stderr, repository)
    direct_err = _normalize(direct.stderr, repository)
    if (native.returncode, native_out, native_err) != (
        direct.returncode,
        direct_out,
        direct_err,
    ):
        raise SmokeError(f"standalone output mismatch for {label}")
    if expected_returncode is not None:
        for name, completed in (("standalone", native), ("direct", direct)):
            if completed.returncode != expected_returncode:
                raise SmokeError(
                    f"{name} {label} exit {completed.returncode} does not match "
                    f"the documented exit {expected_returncode}"
                )
    for name, text in (("standalone", native_err), ("direct", direct_err)):
        for excluded in stderr_excludes or ():
            if excluded in text:
                raise SmokeError(
                    f"{name} {label} stderr unexpectedly contains {excluded!r}"
                )
    if output_path is not None:
        # A review that completed with or without required fixes writes its
        # document; only an incomplete review (exit 2) leaves none behind.
        writes_document = (
            expected_returncode in (0, 1)
            if expected_returncode is not None
            else native.returncode == 0
        )
        if writes_document and native_output is None:
            raise SmokeError(f"standalone file output missing for {label}")
        if writes_document and direct_output is None:
            raise SmokeError(f"direct file output missing for {label}")
        if native_output != direct_output:
            raise SmokeError(f"standalone file output mismatch for {label}")
    return {
        "case": label,
        "returncode": native.returncode,
        "stdout_bytes": len(native_out.encode("utf-8")),
        "stderr_bytes": len(native_err.encode("utf-8")),
    }


def _local_review_cases(
    *,
    executable: Path,
    python_executable: str,
    repository: Path,
) -> list[dict[str, object]]:
    """Exercise the documented local review contract on a synthetic clean host.

    Every case runs with no GitHub Actions identity, OIDC token endpoint,
    broker, or hosted pull-request metadata, and with its own home and
    temporary directories instead of the caller's.  The blocking fixture keeps
    one required fix, so the review exit contract is observable: 1 while the
    fix remains, and the operational contract's 0 for the same completed run.
    No case may print a hosted ``::error`` annotation: the annotation is gated
    on Actions identity, which this host deliberately lacks.
    """

    fixture_root = smoke_fixture_root()
    diff = fixture_root / "blocking-review.patch"
    fixture_response = fixture_root / "blocking-review.json"
    if not diff.is_file() or not fixture_response.is_file():
        raise SmokeError("local review smoke fixtures are unavailable")
    host_sandbox = Path(tempfile.mkdtemp(prefix="review-sensei-smoke-host-"))
    clean_env = clean_host_environment(host_sandbox)
    base = [
        "--provider",
        "fixture",
        "--fixture-response",
        str(fixture_response),
        "--diff",
        str(diff),
        "--no-learning-proposals",
    ]
    cases = [
        ("local-review-text", base, 1),
        ("local-review-markdown", [*base, "--format", "markdown"], 1),
        ("local-review-json", [*base, "--format", "json"], 1),
        ("local-review-operational", [*base, "--exit-semantics", "operational"], 0),
    ]
    try:
        results = [
            compare_case(
                label,
                arguments,
                executable=executable,
                python_executable=python_executable,
                repository=repository,
                shared_env=clean_env,
                expected_returncode=returncode,
                stderr_excludes=_CLEAN_HOST_STDERR_EXCLUDES,
            )
            for label, arguments, returncode in cases
        ]
        results.append(
            compare_case(
                "local-provider-model-override",
                [
                    "--provider",
                    "local-ollama",
                    "--model",
                    "not-a-local-model:cloud",
                    "--diff",
                    str(diff),
                ],
                executable=executable,
                python_executable=python_executable,
                repository=repository,
                shared_env=clean_env,
                expected_returncode=2,
                stderr_excludes=_CLEAN_HOST_STDERR_EXCLUDES,
            )
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".review-sensei-smoke-", suffix=".md", dir=repository
        )
        os.close(descriptor)
        output_path = Path(temporary_name)
        output_path.unlink()
        try:
            results.append(
                compare_case(
                    "local-review-output-file",
                    [*base, "--format", "markdown", "--output", output_path.name],
                    executable=executable,
                    python_executable=python_executable,
                    repository=repository,
                    shared_env=clean_env,
                    output_path=output_path,
                    expected_returncode=1,
                    stderr_excludes=_CLEAN_HOST_STDERR_EXCLUDES,
                )
            )
        finally:
            output_path.unlink(missing_ok=True)
        return results
    finally:
        shutil.rmtree(host_sandbox, ignore_errors=True)


def run_smoke(
    executable: Path,
    *,
    repository: Path,
    diff: Path | None = None,
    fixture_response: Path | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    python_executable: str = sys.executable,
) -> list[dict[str, object]]:
    executable = validate_executable(executable)
    repository = repository.resolve()
    if not repository.is_dir():
        raise SmokeError("smoke repository must be a directory")
    results = [
        compare_case(
            "version",
            ["--version"],
            executable=executable,
            python_executable=python_executable,
            repository=repository,
            # Version/help must be usable when PATH contains no Python. The
            # executable is absolute; this also exercises no runtime fallback.
            standalone_env={"PATH": ""},
        ),
        compare_case(
            "help",
            ["--help"],
            executable=executable,
            python_executable=python_executable,
            repository=repository,
            standalone_env={"PATH": ""},
        ),
    ]
    if diff is not None and fixture_response is not None:
        diff = diff.resolve()
        fixture_response = fixture_response.resolve()
        if not diff.is_file() or not fixture_response.is_file():
            raise SmokeError("fixture diff and response must be regular files")
        results.append(
            compare_case(
                "fixture-review",
                [
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(fixture_response),
                    "--diff",
                    str(diff),
                ],
                executable=executable,
                python_executable=python_executable,
                repository=repository,
            )
        )
        results.append(
            compare_case(
                "validation-failure",
                [
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(fixture_response),
                    "--diff",
                    str(repository / "missing-review-sensei-diff.patch"),
                ],
                executable=executable,
                python_executable=python_executable,
                repository=repository,
                expected_returncode=2,
            )
        )
    results.extend(
        _local_review_cases(
            executable=executable,
            python_executable=python_executable,
            repository=repository,
        )
    )
    if base_ref is not None and head_ref is not None:
        # Use a fresh repository-local name so the smoke oracle cannot
        # overwrite or delete a caller's pre-existing output file.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".review-sensei-smoke-", suffix=".patch", dir=repository
        )
        os.close(descriptor)
        output_path = Path(temporary_name)
        output_path.unlink()
        output_name = output_path.name
        try:
            results.append(
                compare_case(
                    "prepare-diff",
                    [
                        "prepare-diff",
                        "--repository",
                        str(repository),
                        "--base-ref",
                        base_ref,
                        "--head-ref",
                        head_ref,
                        "--output",
                        output_name,
                    ],
                    executable=executable,
                    python_executable=python_executable,
                    repository=repository,
                    output_path=output_path,
                )
            )
        finally:
            output_path.unlink(missing_ok=True)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--diff", type=Path)
    parser.add_argument("--fixture-response", type=Path)
    parser.add_argument("--base-ref")
    parser.add_argument("--head-ref")
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument(
        "--check", action="store_true", help="Validate executable input only"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        executable = validate_executable(args.executable)
        if args.check:
            print(json.dumps({"executable": str(executable), "check": "passed"}))
            return 0
        if (args.diff is None) != (args.fixture_response is None):
            raise SmokeError("--diff and --fixture-response must be supplied together")
        if (args.base_ref is None) != (args.head_ref is None):
            raise SmokeError("--base-ref and --head-ref must be supplied together")
        report = run_smoke(
            executable,
            repository=args.repository,
            diff=args.diff,
            fixture_response=args.fixture_response,
            base_ref=args.base_ref,
            head_ref=args.head_ref,
            python_executable=args.python_executable,
        )
    except SmokeError as exc:
        print(f"standalone smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"cases": report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
