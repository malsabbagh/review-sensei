#!/usr/bin/env python3
"""Install exact PyPI engine and hosted wheels and qualify them without credentials."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import venv
from pathlib import Path

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?\Z")
ENGINE = "review-sensei"
HOSTED = "review-sensei-github"
HOSTED_MANIFEST = "packages/python/review-sensei-github/pyproject.toml"
# Suites that exercise the hosted runtime also verify its exact version.
HOSTED_SUITES = frozenset({"qualify", "operations", "operation_state"})


def wheel_sha256(value):
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise argparse.ArgumentTypeError("an exact lowercase wheel SHA-256 is required")
    return value


def require_wheel_digest(actual, expected):
    if expected is not None and actual != expected:
        raise ValueError(
            "downloaded PyPI wheel differs from the verified release digest"
        )


def require_installed_distribution(identity, version, environment):
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("origin"), str)
        or not identity["origin"]
    ):
        raise ValueError("installed identity must include a non-empty origin path")
    origin = Path(identity["origin"]).resolve()
    if (
        identity.get("version") != version
        or "site-packages" not in origin.parts
        or not origin.is_relative_to(environment.resolve())
    ):
        raise ValueError(
            "installed version/origin differs from the isolated distribution"
        )


def require_hosted_pin(requires, version):
    """The hosted wheel must pin exactly the engine version being qualified."""
    pins = [
        r for r in requires or [] if re.match(r"review[-_.]sensei\s*[=<>!~(]", r, re.I)
    ]
    if pins != [ENGINE + "==" + version]:
        raise ValueError(HOSTED + " does not pin " + ENGINE + "==" + version)


def hosted_version(content, version):
    """Return the hosted version a released source manifest pins to the engine."""
    project = tomllib.loads(content)["project"]
    if project.get("name") != HOSTED:
        raise ValueError("hosted release manifest names a different distribution")
    require_hosted_pin(project.get("dependencies"), version)
    return exact_version(project["version"])


def wheel_for(wheels, project, version):
    identity = [project.replace("-", "_"), version]
    matching = [w for w in wheels if w.name.split("-")[:2] == identity]
    if len(matching) != 1:
        raise RuntimeError("expected exactly one " + project + " wheel")
    return matching[0]


def exact_version(value):
    if not VERSION.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "an exact X.Y.Z (or X.Y.ZrcN) version is required"
        )
    return value


def clean_environment(home):
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SSL_CERT_FILE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_STATE_HOME=str(home / "state"),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def download_command(python, version, destination, project=ENGINE):
    return [
        str(python),
        "-I",
        "-m",
        "pip",
        "--isolated",
        "--disable-pip-version-check",
        "download",
        "--no-cache-dir",
        "--no-deps",
        "--only-binary=:all:",
        "--index-url",
        "https://pypi.org/simple",
        "--retries",
        "0",
        "--timeout",
        "15",
        "--dest",
        str(destination),
        project + "==" + exact_version(version),
    ]


def propagation_download(
    command, cwd, env, attempts=6, delay=15, run=subprocess.run, sleep=time.sleep
):
    """At most six 45s attempts and five 15s pauses; never change the version."""
    receipts = []
    for attempt in range(1, attempts + 1):
        start = time.monotonic()
        try:
            proc = run(
                command, cwd=cwd, env=env, capture_output=True, text=True, timeout=45
            )
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = "TIMEOUT"
        receipts.append(
            {
                "attempt": attempt,
                "exit": code,
                "elapsed_seconds": round(time.monotonic() - start, 3),
            }
        )
        if code == 0:
            return True, receipts
        if attempt < attempts:
            sleep(delay)
    return False, receipts


def validate_receipt(data, version):
    """Reject empty, stale or internally inconsistent suite evidence."""
    if not isinstance(data, dict) or data.get("version") != version:
        raise ValueError("missing or wrong-version qualification receipt")
    for key in ("total", "pass", "fail"):
        if type(data.get(key)) is not int or data[key] < 0:
            raise ValueError("invalid qualification counts")
    rows = data.get("rows")
    if (
        data["total"] < 1
        or data["pass"] + data["fail"] != data["total"]
        or not isinstance(rows, list)
        or len(rows) != data["total"]
    ):
        raise ValueError("incomplete qualification receipt")
    if any(
        not isinstance(row, dict) or row.get("status") not in ("PASS", "FAIL")
        for row in rows
    ):
        raise ValueError("invalid qualification row")
    if sum(row["status"] == "PASS" for row in rows) != data["pass"]:
        raise ValueError("qualification rows disagree with counts")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, type=exact_version)
    parser.add_argument("--github-version", required=True, type=exact_version)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-wheel-sha256", type=wheel_sha256)
    parser.add_argument("--expected-github-wheel-sha256", type=wheel_sha256)
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        parser.error("output must be a new directory to prevent stale receipts")
    root.mkdir(parents=True)
    home = root / "isolated-home"
    home.mkdir()
    env = clean_environment(home)
    report = {
        "requested_version": args.version,
        "requested_github_version": args.github_version,
        "source": "exact PyPI wheels",
        "credential_free": True,
        "status": "NOT_EXECUTED",
        "suites": [],
        "gaps": [
            "No live providers or GitHub writes; loopback and fixture providers only",
            "Native/npm packaging is outside this PyPI lane",
            "Nine large total-work packing cases excluded from the default bounded lane",
            "Supplemental release-source reply/regression suites are outside this portable lane",
            "Windows cancellation and Linux/macOS architectures other than the runner are not proven",
        ],
    }
    exit_code = 1
    try:
        environment = root / "environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        wheels = root / "wheels"
        wheels.mkdir()
        for project, version, key in (
            (ENGINE, args.version, "propagation_attempts"),
            (HOSTED, args.github_version, "github_propagation_attempts"),
        ):
            ok, report[key] = propagation_download(
                download_command(python, version, wheels, project), root, env
            )
            if not ok:
                raise RuntimeError(
                    "exact PyPI " + project + " wheel unavailable after bounded "
                    "propagation attempts"
                )
        wheel_files = list(wheels.glob("*.whl"))
        if len(wheel_files) != 2:
            raise RuntimeError("expected exactly the two requested wheels")
        wheel = wheel_for(wheel_files, ENGINE, args.version)
        github_wheel = wheel_for(wheel_files, HOSTED, args.github_version)
        report["wheel"] = {
            "name": wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        }
        report["github_wheel"] = {
            "name": github_wheel.name,
            "sha256": hashlib.sha256(github_wheel.read_bytes()).hexdigest(),
        }
        require_wheel_digest(report["wheel"]["sha256"], args.expected_wheel_sha256)
        require_wheel_digest(
            report["github_wheel"]["sha256"], args.expected_github_wheel_sha256
        )
        report["expected_wheel_sha256"] = args.expected_wheel_sha256
        report["expected_github_wheel_sha256"] = args.expected_github_wheel_sha256
        command = [
            str(python),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-cache-dir",
            "--index-url",
            "https://pypi.org/simple",
            "--retries",
            "1",
            "--timeout",
            "15",
            str(wheel),
            str(github_wheel),
        ]
        installed = subprocess.run(
            command, cwd=root, env=env, capture_output=True, text=True, timeout=180
        )
        report["installation_exit"] = installed.returncode
        if installed.returncode:
            raise RuntimeError("isolated exact-wheel dependency installation failed")
        inspect = "import importlib.metadata,json,review_sensei;print(json.dumps({'version':importlib.metadata.version('review-sensei'),'origin':review_sensei.__file__,'dependencies':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}}))"
        identity = subprocess.check_output(
            [str(python), "-I", "-c", inspect], cwd=root, env=env, text=True, timeout=15
        )
        report["installed"] = json.loads(identity)
        require_installed_distribution(report["installed"], args.version, environment)
        inspect = "import importlib.metadata,json,review_sensei_github;print(json.dumps({'version':importlib.metadata.version('review-sensei-github'),'origin':review_sensei_github.__file__,'requires':importlib.metadata.requires('review-sensei-github')}))"
        identity = subprocess.check_output(
            [str(python), "-I", "-c", inspect], cwd=root, env=env, text=True, timeout=15
        )
        report["installed_github"] = json.loads(identity)
        require_installed_distribution(
            report["installed_github"], args.github_version, environment
        )
        require_hosted_pin(report["installed_github"].get("requires"), args.version)
        scripts = Path(__file__).resolve().parents[1] / "tests/published_cli"
        for name in (
            "qualify",
            "transports",
            "seam_boundaries",
            "operations",
            "operation_state",
        ):
            destination = root / name
            command = [
                str(python),
                str(scripts / (name + ".py")),
                "--expected-version",
                args.version,
                "--report",
                str(destination),
            ]
            if name in HOSTED_SUITES:
                command += ["--expected-github-version", args.github_version]
            try:
                proc = subprocess.run(
                    command, cwd=root, env=env, capture_output=True, timeout=300
                )
                code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired as error:
                code, stdout, stderr = (
                    "TIMEOUT",
                    error.stdout or b"",
                    error.stderr or b"",
                )
            (root / (name + ".stdout.txt")).write_bytes(stdout)
            (root / (name + ".stderr.txt")).write_bytes(stderr)
            result = destination / "results.json"
            data = validate_receipt(
                json.loads(result.read_text()) if result.exists() else {}, args.version
            )
            report["suites"].append(
                {
                    "name": name,
                    "exit": code,
                    "total": data.get("total", 0),
                    "pass": data.get("pass", 0),
                    "fail": data.get("fail", 0),
                }
            )
        passed = all(
            x["exit"] == 0 and x["total"] > 0 and x["fail"] == 0
            for x in report["suites"]
        )
        report["status"] = "PASS" if passed else "FAIL"
        exit_code = 0 if passed else 1
    except Exception as error:
        report["status"] = "ERROR"
        report["error"] = type(error).__name__ + ": " + str(error)
    finally:
        (root / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
