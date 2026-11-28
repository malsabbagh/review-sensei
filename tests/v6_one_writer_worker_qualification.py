"""Integrated acceptance qualification for ReviewSensei v6 one-writer coordinator.

Exercises the real Cloudflare Worker (`deploy/cloudflare/src/worker.ts` +
`V6PrCoordinator`) over HTTPS + SQLite Durable Object storage, the wheel-installed
`review-sensei-github` binary inside an isolated clean venv, and a local TLS
simulated GitHub + OIDC/JWKS server. Never touches live GitHub or live Worker.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import importlib.metadata as importlib_metadata
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from review_sensei_github.v6_recovery_ledger import (
    CompletionReceiptRecord,
    DurableHoldRecord,
    NonSubmissionProofRecord,
    OwnerMarkerRecord,
    ResultManifestRecord,
    ResultPartRecord,
    UnresolvedResultRecord,
    WriteIntentRecord,
    extract_v6_record_markers,
    format_v6_record_marker,
)

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "tests" / "v6_worker_http_bridge.mjs"

OWNER = "octo"
REPO = "v6-pilot-fixture"
REPO_FULL = f"{OWNER}/{REPO}"
REPO_ID = 987654321
APP_ID = 12345
APP_SLUG = "reviewsensei"
BOT_LOGIN = "reviewsensei[bot]"
BOT_USER_ID = 777001
INSTALLATION_ID = 67890
HEAD_SHA = "a" * 40
HEAD_SHA_B = "d" * 40
BASE_SHA = "0" * 40


def _workflow_ref_for_sha(workflow_sha: str) -> str:
    """Return the exact pinned pilot workflow ref for ``workflow_sha``."""

    return (
        "malsabbagh/review-sensei/.github/workflows/review-sensei-v6-pilot.yml@"
        f"{workflow_sha}"
    )


SIGNING_KEY = "v6-integrated-acceptance-signing-key-material-32bytes"
WF_READ_TOKEN = "ghp_wf_readonly_acceptance_token_0001"
PROVIDER_API_KEY = "sk-test-v6-provider-key-0001"
OIDC_REQUEST_PREFIX = "actions-oidc-req:"
DEFAULT_PR_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def pick(items, idx):\n"
    "+    # validate index\n"
    "     return items[idx]\n"
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _iso_utc(dt: datetime | None = None) -> str:
    current = dt if dt is not None else datetime.now(tz=timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(raw: str) -> datetime:
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _generate_tls_materials(target_dir: Path) -> tuple[Path, Path, Path]:
    """Generate an ephemeral CA and localhost/127.0.0.1 server certificate."""

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(tz=timezone.utc)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "ReviewSensei Test Ephemeral CA")]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = target_dir / "ca.pem"
    cert_pem = target_dir / "server-cert.pem"
    key_pem = target_dir / "server-key.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, cert_pem, key_pem


def _sign_rs256_jwt(
    private_key: rsa.RSAPrivateKey,
    *,
    headers: dict[str, Any],
    claims: dict[str, Any],
) -> str:
    head_b64 = _b64url(json.dumps(headers, separators=(",", ":")).encode("utf-8"))
    body_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{head_b64}.{body_b64}".encode("ascii")
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{head_b64}.{body_b64}.{_b64url(sig)}"


def _verify_rs256_jwt(public_key: rsa.RSAPublicKey, token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed_jwt")
    head_b64, body_b64, sig_b64 = parts
    padded_sig = sig_b64 + "=" * ((4 - len(sig_b64) % 4) % 4)
    sig = base64.urlsafe_b64decode(padded_sig.encode("ascii"))
    public_key.verify(
        sig,
        f"{head_b64}.{body_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    padded_body = body_b64 + "=" * ((4 - len(body_b64) % 4) % 4)
    return json.loads(
        base64.urlsafe_b64decode(padded_body.encode("ascii")).decode("utf-8")
    )


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_direct_dep_name(req_str: str) -> str | None:
    """Return normalized distribution name if ``req_str`` applies to current env without extras."""

    try:
        from packaging.requirements import Requirement
    except ImportError:
        Requirement = None  # type: ignore[assignment,misc]

    if Requirement is not None:
        req = Requirement(req_str)
        if req.marker is not None:
            if "extra" in str(req.marker) or not req.marker.evaluate():
                return None
        return _normalize_dist_name(req.name)

    head, sep, marker = req_str.partition(";")
    if sep and "extra" in marker:
        return None
    match = re.match(r"^\s*([A-Za-z0-9._-]+)", head)
    return _normalize_dist_name(match.group(1)) if match else None


def _collect_transitive_deps(roots: tuple[str, ...]) -> list[str]:
    """Recursively resolve non-extra runtime dependencies starting from ``roots``."""

    visited = {_normalize_dist_name(root) for root in roots}
    queue = list(roots)
    deps: list[str] = []
    while queue:
        current = queue.pop(0)
        dist = importlib_metadata.distribution(current)
        for req_str in dist.requires or ():
            dep_name = _parse_direct_dep_name(req_str)
            if dep_name is None or dep_name in visited:
                continue
            visited.add(dep_name)
            deps.append(dep_name)
            queue.append(dep_name)
    return deps


def _select_compatible_wheel_tag(tags: list[str]) -> str:
    """Choose the highest-priority compatible tag from ``WHEEL`` metadata."""

    if not tags:
        return "py3-none-any"
    try:
        from packaging.tags import sys_tags
    except ImportError:
        return tags[0]
    rank = {str(t): idx for idx, t in enumerate(sys_tags())}
    compatible = [t for t in tags if t in rank]
    if compatible:
        return min(compatible, key=lambda t: rank[t])
    return tags[0]


def _materialize_installed_dist_wheel(dist_name: str, wheel_dir: Path) -> None:
    """Repack an already-installed distribution into ``wheel_dir`` for offline --no-index install."""

    dist = importlib_metadata.distribution(dist_name)
    files = list(dist.files or ())
    if not files:
        raise RuntimeError(
            f"cannot locate files for installed distribution {dist_name!r}"
        )
    wheel_meta = dist.read_text("WHEEL") or ""
    tags = [
        line.split(":", 1)[1].strip()
        for line in wheel_meta.splitlines()
        if line.startswith("Tag:") and line.split(":", 1)[1].strip()
    ]
    tag = _select_compatible_wheel_tag(tags)
    norm_name = re.sub(r"[-_.]+", "_", dist.metadata["Name"])
    wheel_path = wheel_dir / f"{norm_name}-{dist.version}-{tag}.whl"
    if wheel_path.exists():
        return

    dist_info_prefix = f"{norm_name}-{dist.version}.dist-info"
    top_record_arc = f"{dist_info_prefix}/RECORD"
    record_rows: list[str] = []
    seen_arcs: set[str] = set()
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            if "__pycache__" in rel.parts or ".." in rel.parts:
                continue
            src_path = Path(str(dist.locate_file(rel)))
            if not src_path.is_file():
                continue
            arcname = rel.as_posix()
            if arcname == top_record_arc or (
                "/" not in arcname.rsplit("/", 1)[0]
                and arcname.endswith(".dist-info/RECORD")
            ):
                continue
            if arcname in seen_arcs:
                continue
            seen_arcs.add(arcname)
            data = src_path.read_bytes()
            zf.writestr(arcname, data)
            digest = _b64url(hashlib.sha256(data).digest())
            record_rows.append(f"{arcname},sha256={digest},{len(data)}")
            if "/" not in arcname.rsplit("/", 1)[0] and arcname.endswith(
                ".dist-info/METADATA"
            ):
                dist_info_prefix = arcname.rsplit("/", 1)[0]
        record_arc = f"{dist_info_prefix}/RECORD"
        record_rows.append(f"{record_arc},,")
        zf.writestr(record_arc, "\n".join(record_rows) + "\n")


def _verify_install_provenance(
    *,
    expected_commit_sha: str,
    wheel_dir: Path,
    stdout_text: str,
    step_summary_path: Path,
) -> dict[str, str]:
    """Verify workflow install stdout and GITHUB_STEP_SUMMARY match exact commit and built wheels."""

    built_wheels = sorted(wheel_dir.glob("*.whl"))
    wheel_sha256_by_name = {
        whl.name: hashlib.sha256(whl.read_bytes()).hexdigest() for whl in built_wheels
    }
    if len(wheel_sha256_by_name) != 2:
        raise RuntimeError(
            "expected 2 built wheels from workflow install step, "
            f"found {len(wheel_sha256_by_name)}: {sorted(wheel_sha256_by_name)}"
        )
    has_core_wheel = any(
        whl.name.startswith("review_sensei-") and whl.name.endswith(".whl")
        for whl in built_wheels
    )
    has_github_wheel = any(
        whl.name.startswith("review_sensei_github-") and whl.name.endswith(".whl")
        for whl in built_wheels
    )
    if not (has_core_wheel and has_github_wheel):
        raise RuntimeError(
            "expected both review_sensei-*.whl and review_sensei_github-*.whl, "
            f"found {sorted(wheel_sha256_by_name)}"
        )
    if not step_summary_path.is_file():
        raise RuntimeError(
            f"workflow install step missing GITHUB_STEP_SUMMARY file: {step_summary_path}"
        )

    stdout_lines = stdout_text.splitlines()
    summary_lines = step_summary_path.read_text(encoding="utf-8").splitlines()

    expected_commit_line = f"review_sensei_v6_workflow_commit={expected_commit_sha}"
    stdout_commit_lines = [
        line
        for line in stdout_lines
        if line.startswith("review_sensei_v6_workflow_commit=")
    ]
    if stdout_commit_lines != [expected_commit_line]:
        raise RuntimeError(
            "workflow install step stdout commit provenance mismatch: "
            f"expected [{expected_commit_line!r}], got {stdout_commit_lines!r}"
        )
    summary_commit_lines = [
        line
        for line in summary_lines
        if line.startswith("- `review_sensei_v6_workflow_commit=")
    ]
    if summary_commit_lines != [f"- `{expected_commit_line}`"]:
        raise RuntimeError(
            "workflow install step summary commit provenance mismatch: "
            f"expected ['- `{expected_commit_line}`'], got {summary_commit_lines!r}"
        )

    for whl_name, whl_digest in wheel_sha256_by_name.items():
        expected_wheel_line = f"review_sensei_v6_wheel_sha256 {whl_name} {whl_digest}"
        stdout_wheel_lines = [
            line
            for line in stdout_lines
            if line.startswith(f"review_sensei_v6_wheel_sha256 {whl_name} ")
        ]
        if stdout_wheel_lines != [expected_wheel_line]:
            raise RuntimeError(
                "workflow install step stdout wheel hash mismatch: "
                f"expected [{expected_wheel_line!r}], got {stdout_wheel_lines!r}"
            )
        summary_wheel_lines = [
            line
            for line in summary_lines
            if line.startswith(f"- `review_sensei_v6_wheel_sha256 {whl_name} ")
        ]
        if summary_wheel_lines != [f"- `{expected_wheel_line}`"]:
            raise RuntimeError(
                "workflow install step summary wheel hash mismatch: "
                f"expected ['- `{expected_wheel_line}`'], got {summary_wheel_lines!r}"
            )
    return wheel_sha256_by_name


def _build_offline_venv_and_install(
    temp_root: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    """Execute the actual `.github/workflows/review-sensei-v6-pilot.yml` install step via bash."""

    git_origin = temp_root / "workflow-origin"
    git_origin.mkdir(parents=True, exist_ok=True)
    ls_proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    allowed_prefixes = (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "src/",
        "packages/python/review-sensei-github/",
    )
    ignored_parts = {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "build",
        "dist",
    }
    for raw_rel in ls_proc.stdout.split(b"\x00"):
        if not raw_rel:
            continue
        rel_posix = raw_rel.decode("utf-8")
        if not (
            rel_posix in allowed_prefixes[:4]
            or rel_posix.startswith(allowed_prefixes[4])
            or rel_posix.startswith(allowed_prefixes[5])
        ):
            continue
        rel_path = Path(rel_posix)
        if (
            any(
                part in ignored_parts or part.endswith(".egg-info")
                for part in rel_path.parts
            )
            or rel_path.suffix == ".pyc"
        ):
            continue
        src_file = ROOT / rel_path
        if not src_file.is_file():
            continue
        dst_file = git_origin / rel_path
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)

    git_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(temp_root),
        "XDG_CONFIG_HOME": str(temp_root / "xdg"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "GIT_AUTHOR_NAME": "ReviewSensei Test",
        "GIT_AUTHOR_EMAIL": "test@reviewsensei.invalid",
        "GIT_COMMITTER_NAME": "ReviewSensei Test",
        "GIT_COMMITTER_EMAIL": "test@reviewsensei.invalid",
    }
    git_flags = [
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.autocrlf=false",
    ]
    subprocess.run(
        ["git", *git_flags, "init"],
        cwd=str(git_origin),
        env=git_env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", *git_flags, "add", "-A"],
        cwd=str(git_origin),
        env=git_env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        [
            "git",
            *git_flags,
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            "v6 workflow archive snapshot",
        ],
        cwd=str(git_origin),
        env=git_env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    commit_sha = subprocess.run(
        ["git", *git_flags, "rev-parse", "HEAD"],
        cwd=str(git_origin),
        env=git_env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()

    # Materialize offline wheelhouse containing PEP 517 build tools + runtime deps.
    wheelhouse = temp_root / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    seed_dists = ("pip", "setuptools", "review-sensei", "review-sensei-github")
    for base_dep in ("pip", "setuptools"):
        _materialize_installed_dist_wheel(base_dep, wheelhouse)
    for dep_name in _collect_transitive_deps(seed_dists):
        _materialize_installed_dist_wheel(dep_name, wheelhouse)

    # Extract & qualify actual Steps 1, 2, and 3 from .github/workflows/review-sensei-v6-pilot.yml.
    wf_doc = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "review-sensei-v6-pilot.yml").read_text(
            encoding="utf-8"
        )
    )
    wf_steps = [step for job in wf_doc["jobs"].values() for step in job["steps"]]
    validate_step = next(
        step
        for step in wf_steps
        if step.get("name")
        == "Validate trusted v6 inputs and reject fork head execution"
    )
    checkout_step = next(
        step
        for step in wf_steps
        if step.get("name") == "Check out trusted default-branch policy only"
    )
    if checkout_step["with"].get("path") != "trusted-policy":
        raise RuntimeError(
            "workflow checkout step must isolate policy under trusted-policy/"
        )
    install_step = next(
        step
        for step in wf_steps
        if step.get("name") == "Install ReviewSensei v6 pilot runner"
    )
    install_script = str(install_step["run"])

    shim_bin = temp_root / "shim-bin"
    shim_bin.mkdir(parents=True, exist_ok=True)
    python_shim = shim_bin / "python"
    if not python_shim.exists():
        os.symlink(sys.executable, python_shim)

    runner_temp = temp_root / "runner-temp"
    runner_temp.mkdir(parents=True, exist_ok=True)
    bash_env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"}
    }
    validate_env = {
        **bash_env,
        "REPOSITORY": REPO_FULL,
        "REPOSITORY_ID": str(REPO_ID),
        "PULL_REQUEST": "51",
        "HEAD_SHA": HEAD_SHA,
        "HEAD_REPOSITORY": REPO_FULL,
        "OPERATION_ID": "op-validate-1",
        "EVENT_KIND": "pull_request",
        "EFFECT_KIND": "pr_review_publish",
    }
    val_ok = subprocess.run(
        ["bash", "-c", str(validate_step["run"])],
        cwd=str(temp_root),
        env=validate_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if val_ok.returncode != 0:
        raise RuntimeError(f"workflow input validation step failed: {val_ok.stderr}")
    val_fork = subprocess.run(
        ["bash", "-c", str(validate_step["run"])],
        cwd=str(temp_root),
        env={**validate_env, "HEAD_REPOSITORY": "external-actor/fork-repo"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if val_fork.returncode == 0 or "fork" not in val_fork.stderr:
        raise RuntimeError(
            "workflow input validation failed to reject fork head repository"
        )

    # Step 2 equivalent: check out ONLY default branch policy snapshot into trusted-policy/.
    trusted_policy_checkout = temp_root / str(checkout_step["with"]["path"])
    subprocess.run(
        [
            "git",
            *git_flags,
            "clone",
            "--depth=1",
            str(git_origin),
            str(trusted_policy_checkout),
        ],
        check=True,
        env=git_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    trusted_policy_rev = subprocess.run(
        ["git", *git_flags, "rev-parse", "HEAD"],
        cwd=str(trusted_policy_checkout),
        env=git_env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    install_step_summary = runner_temp / "install-step-summary.md"
    bash_env.update(
        {
            "PATH": f"{shim_bin}:{os.environ.get('PATH', '')}",
            "HOME": str(temp_root),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_STEP_SUMMARY": str(install_step_summary),
            "REVIEW_SENSEI_WORKFLOW_SHA": commit_sha,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{git_origin.resolve().as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/malsabbagh/review-sensei.git",
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": str(wheelhouse),
        }
    )
    proc = subprocess.run(
        ["bash", "-c", install_script],
        cwd=str(temp_root),
        env=bash_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"actual workflow install step failed (exit {proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

    venv_dir = runner_temp / "review-sensei-v6-venv"
    built_wheel_dir = runner_temp / "review-sensei-v6-wheels"
    wheel_sha256_by_name = _verify_install_provenance(
        expected_commit_sha=commit_sha,
        wheel_dir=built_wheel_dir,
        stdout_text=proc.stdout,
        step_summary_path=install_step_summary,
    )
    worker_digest = hashlib.sha256()
    for ts_file in sorted((ROOT / "deploy" / "cloudflare" / "src").glob("*.ts")):
        worker_digest.update(ts_file.name.encode("utf-8"))
        worker_digest.update(ts_file.read_bytes())
    provenance: dict[str, Any] = {
        "workflow_commit_sha": commit_sha,
        "trusted_policy_revision": trusted_policy_rev,
        "wheel_sha256s": wheel_sha256_by_name,
        "wheel_dir": str(built_wheel_dir),
        "install_stdout": proc.stdout,
        "install_step_summary_path": str(install_step_summary),
        "worker_source_sha256": worker_digest.hexdigest(),
    }

    cli_bin = venv_dir / "bin" / "review-sensei-github"
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    help_proc = subprocess.run(
        [str(cli_bin), "v6-pilot-run", "--help"],
        check=True,
        cwd=str(temp_root),
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for required_flag in ("--analyze-fresh-pr", "--policy-root", "--operation-id"):
        if required_flag not in help_proc.stdout:
            raise RuntimeError(
                f"v6-pilot-run --help missing expected flag {required_flag}"
            )
    return venv_dir, cli_bin, provenance


@dataclass
class _CrashHook:
    method: str
    path: str
    pid: int
    body_contains: str | None = None
    kill_before_write: bool = True
    triggered: bool = False


@dataclass
class _DropReplyHook:
    method: str
    path: str
    triggered: bool = False


@dataclass
class _SimulatedGitHubState:
    oidc_key: rsa.RSAPrivateKey
    app_key: rsa.RSAPrivateKey
    workflow_sha: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests_log: list[tuple[str, str]] = field(default_factory=list)
    active_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    revoked_tokens: set[str] = field(default_factory=set)
    minted_tokens_history: list[str] = field(default_factory=list)
    runner_write_tokens: list[str] = field(default_factory=list)
    runner_mint_count: int = 0
    prs: dict[int, dict[str, Any]] = field(default_factory=dict)
    run_attempts: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    next_comment_id: int = 1000
    next_review_id: int = 5000
    crash_hook: _CrashHook | None = None
    drop_reply_hook: _DropReplyHook | None = None
    trees: dict[tuple[str, bool], Any] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    commits: dict[str, list[str]] = field(default_factory=dict)
    commit_dates: dict[str, str] = field(default_factory=dict)
    head_advance_after_comment_hook: tuple[int, str, str] | None = None
    reject_review_status: dict[int, tuple[int, dict[str, Any]]] = field(
        default_factory=dict
    )

    def ensure_pr(
        self,
        pr_number: int,
        *,
        head_sha: str = HEAD_SHA,
        base_sha: str = BASE_SHA,
        title: str = "Fix bounds check",
        diff: str = DEFAULT_PR_DIFF,
    ) -> dict[str, Any]:
        with self.lock:
            pr = self.prs.get(pr_number)
            if pr is None:
                pr = {
                    "number": pr_number,
                    "head_sha": head_sha,
                    "base_sha": base_sha,
                    "merge_base_sha": base_sha,
                    "title": title,
                    "diff": diff,
                    "state": "open",
                    "comments": [],
                    "reviews": [],
                }
                self.prs[pr_number] = pr
                if (base_sha, True) not in self.trees and (
                    base_sha,
                    False,
                ) not in self.trees:
                    base_blob_sha = hashlib.sha256(
                        f"base:{base_sha}".encode("utf-8")
                    ).hexdigest()[:40]
                    self.blobs.setdefault(
                        base_blob_sha, b"def pick(items, idx):\n    return items[idx]\n"
                    )
                    self.trees[(base_sha, True)] = {
                        "sha": base_sha,
                        "truncated": False,
                        "tree": [
                            {
                                "path": "src/app.py",
                                "type": "blob",
                                "sha": base_blob_sha,
                                "mode": "100644",
                            }
                        ],
                    }
                if (head_sha, True) not in self.trees and (
                    head_sha,
                    False,
                ) not in self.trees:
                    head_blob_sha = hashlib.sha256(
                        f"head:{head_sha}".encode("utf-8")
                    ).hexdigest()[:40]
                    self.blobs.setdefault(
                        head_blob_sha,
                        b"def pick(items, idx):\n    # validate index\n    return items[idx]\n",
                    )
                    self.trees[(head_sha, True)] = {
                        "sha": head_sha,
                        "truncated": False,
                        "tree": [
                            {
                                "path": "src/app.py",
                                "type": "blob",
                                "sha": head_blob_sha,
                                "mode": "100644",
                            }
                        ],
                    }
                self.commits.setdefault(base_sha, [])
                self.commits.setdefault(head_sha, [base_sha])
            return pr

    def set_run_attempt(
        self,
        run_id: str,
        attempt: int,
        *,
        status: str,
        conclusion: str | None,
        head_sha: str = HEAD_SHA,
    ) -> None:
        with self.lock:
            self.run_attempts[(str(run_id), attempt)] = {
                "id": int(run_id),
                "run_attempt": attempt,
                "status": status,
                "conclusion": conclusion,
                "head_sha": head_sha,
            }

    def add_app_comment(self, pr_number: int, body: str) -> dict[str, Any]:
        with self.lock:
            pr = self.prs.setdefault(
                pr_number,
                {
                    "number": pr_number,
                    "head_sha": HEAD_SHA,
                    "state": "open",
                    "comments": [],
                    "reviews": [],
                },
            )
            self.next_comment_id += 1
            cid = self.next_comment_id
            now_iso = _iso_utc()
            comment = {
                "id": cid,
                "body": body,
                "author_association": "NONE",
                "issue_url": f"https://api.github.com/repos/{OWNER}/{REPO}/issues/{pr_number}",
                "html_url": f"https://github.com/{REPO_FULL}/pull/{pr_number}#issuecomment-{cid}",
                "user": {"id": BOT_USER_ID, "login": BOT_LOGIN, "type": "Bot"},
                "performed_via_github_app": {"id": APP_ID, "slug": APP_SLUG},
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            pr["comments"].append(comment)
            return comment

    def oidc_jwks(self) -> dict[str, Any]:
        pub_nums = self.oidc_key.public_key().public_numbers()
        n_len = (pub_nums.n.bit_length() + 7) // 8
        e_len = (pub_nums.e.bit_length() + 7) // 8
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "test-oidc-key-1",
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url(pub_nums.n.to_bytes(n_len, "big")),
                    "e": _b64url(pub_nums.e.to_bytes(e_len, "big")),
                }
            ]
        }

    def issue_oidc_jwt(
        self,
        *,
        run_id: str,
        run_attempt: int,
        repository: str = REPO_FULL,
        repository_id: int = REPO_ID,
        workflow_sha: str | None = None,
        job_workflow_ref: str | None = None,
        audience: str = "sts.reviewsensei.dev",
    ) -> str:
        effective_workflow_sha = (
            workflow_sha if workflow_sha is not None else self.workflow_sha
        )
        effective_job_workflow_ref = (
            job_workflow_ref
            if job_workflow_ref is not None
            else _workflow_ref_for_sha(effective_workflow_sha)
        )
        now = int(time.time())
        repo_owner = repository.split("/", 1)[0]
        claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": audience,
            "exp": now + 300,
            "iat": now - 5,
            "nbf": now - 5,
            "sub": f"repo:{repository}:pull_request",
            "jti": secrets.token_hex(12),
            "repository": repository,
            "repository_owner": repo_owner,
            "repository_id": str(repository_id),
            "actor": "octocat",
            "actor_id": "100",
            "event_name": "pull_request",
            "workflow": "ReviewSensei v6",
            "workflow_ref": effective_job_workflow_ref,
            "workflow_sha": effective_workflow_sha,
            "job_workflow_ref": effective_job_workflow_ref,
            "job_workflow_sha": effective_workflow_sha,
            "ref": "refs/pull/42/merge",
            "sha": HEAD_SHA,
            "run_id": str(run_id),
            "run_number": "1",
            "run_attempt": str(run_attempt),
            "runner_environment": "github-hosted",
        }
        return _sign_rs256_jwt(
            self.oidc_key,
            headers={"alg": "RS256", "typ": "JWT", "kid": "test-oidc-key-1"},
            claims=claims,
        )


class _QuietThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Expected when SIGKILL kills a runner mid-request during crash tests.
        return


def _make_github_handler(
    state: _SimulatedGitHubState,
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(
            self,
            status: int,
            payload: Any,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            raw = json.dumps(payload).encode("utf-8")
            if extra_headers:
                for hk, hv in extra_headers.items():
                    if "\r" in hk or "\n" in hk or "\r" in hv or "\n" in hv:
                        raise ValueError("header injection rejected")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(raw)

        def _send_empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _bearer_token(self) -> str:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[len("Bearer ") :].strip()
            return ""

        def _require_app_jwt(self) -> bool:
            token = self._bearer_token()
            if not token:
                return False
            try:
                claims = _verify_rs256_jwt(state.app_key.public_key(), token)
                return str(claims.get("iss")) == str(APP_ID)
            except Exception:
                return False

        def _authenticate_rest(self, *, write: bool) -> tuple[bool, int, str]:
            token = self._bearer_token()
            if token == WF_READ_TOKEN:
                if write:
                    return False, 403, "workflow_token_read_only"
                return True, 200, "workflow"
            with state.lock:
                meta = state.active_tokens.get(token)
                if meta is None or meta["revoked"]:
                    return False, 401, "bad_credentials"
                expires_dt = _parse_iso_utc(str(meta["expires_at"]))
                if datetime.now(tz=timezone.utc) >= expires_dt:
                    return False, 401, "token_expired"
                return True, 200, "app"

        def _maybe_trigger_crash(
            self, method: str, path: str, body_text: str | None = None
        ) -> bool:
            with state.lock:
                hook = state.crash_hook
                if hook is None or hook.triggered:
                    return False
                if hook.method != method or hook.path != path:
                    return False
                if hook.body_contains is not None and (
                    body_text is None or hook.body_contains not in body_text
                ):
                    return False
                hook.triggered = True
                pid = hook.pid
            os.kill(pid, signal.SIGKILL)
            return True

        def _should_drop_reply(self, method: str, path: str) -> bool:
            with state.lock:
                hook = state.drop_reply_hook
                if hook is None or hook.triggered:
                    return False
                if hook.method != method or hook.path != path:
                    return False
                hook.triggered = True
                return True

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            with state.lock:
                state.requests_log.append(("GET", path))

            if path == "/.well-known/jwks":
                self._send_json(200, state.oidc_jwks())
                return

            if path == "/oidc/token":
                bearer = self._bearer_token()
                if not bearer.startswith(OIDC_REQUEST_PREFIX):
                    self._send_json(403, {"error": "unauthorized_oidc_request"})
                    return
                spec = bearer[len(OIDC_REQUEST_PREFIX) :]
                parts = dict(
                    item.split("=", 1) for item in spec.split(",") if "=" in item
                )
                aud = (query.get("audience") or ["sts.reviewsensei.dev"])[0]
                jwt_val = state.issue_oidc_jwt(
                    run_id=parts.get("run_id", "1"),
                    run_attempt=int(parts.get("run_attempt", "1")),
                    repository=parts.get("repository", REPO_FULL),
                    repository_id=int(parts.get("repository_id", str(REPO_ID))),
                    workflow_sha=parts.get("workflow_sha"),
                    job_workflow_ref=parts.get("job_workflow_ref"),
                    audience=aud,
                )
                self._send_json(200, {"value": jwt_val})
                return

            if path == "/app":
                if not self._require_app_jwt():
                    self._send_json(401, {"message": "Bad credentials"})
                    return
                self._send_json(
                    200, {"id": APP_ID, "slug": APP_SLUG, "name": "ReviewSensei"}
                )
                return

            if path == f"/repos/{REPO_FULL}/installation":
                if not self._require_app_jwt():
                    self._send_json(401, {"message": "Bad credentials"})
                    return
                self._send_json(
                    200,
                    {
                        "id": INSTALLATION_ID,
                        "repository_selection": "selected",
                        "permissions": {
                            "actions": "read",
                            "checks": "write",
                            "contents": "read",
                            "issues": "write",
                            "pull_requests": "write",
                        },
                    },
                )
                return

            ok, status, _ = self._authenticate_rest(write=False)
            if not ok:
                self._send_json(status, {"message": "Bad credentials"})
                return

            if path == f"/repos/{REPO_FULL}":
                self._send_json(
                    200,
                    {
                        "id": REPO_ID,
                        "name": REPO,
                        "full_name": REPO_FULL,
                        "fork": False,
                        "private": False,
                        "owner": {"id": 1, "login": OWNER, "type": "User"},
                    },
                )
                return

            pr_match = re.fullmatch(rf"/repos/{re.escape(REPO_FULL)}/pulls/(\d+)", path)
            if pr_match:
                pr_num = int(pr_match.group(1))
                pr = state.ensure_pr(pr_num)
                if "application/vnd.github.v3.diff" in self.headers.get("Accept", ""):
                    diff_bytes = str(pr.get("diff", DEFAULT_PR_DIFF)).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(diff_bytes)))
                    self.end_headers()
                    self.wfile.write(diff_bytes)
                    return
                self._send_json(
                    200,
                    {
                        "id": pr_num,
                        "number": pr_num,
                        "title": pr.get("title", "Fix bounds check"),
                        "state": pr["state"],
                        "head": {
                            "sha": pr["head_sha"],
                            "repo": {
                                "id": REPO_ID,
                                "full_name": REPO_FULL,
                                "fork": False,
                            },
                        },
                        "base": {
                            "sha": pr.get("base_sha", BASE_SHA),
                            "repo": {
                                "id": REPO_ID,
                                "full_name": REPO_FULL,
                                "fork": False,
                            },
                        },
                    },
                )
                return

            pr_files_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/pulls/(\d+)/files", path
            )
            if pr_files_match:
                self._send_json(200, [])
                return

            attempt_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/actions/runs/(\d+)/attempts/(\d+)",
                path,
            )
            if attempt_match:
                run_id, attempt = attempt_match.group(1), int(attempt_match.group(2))
                with state.lock:
                    info = state.run_attempts.get((run_id, attempt))
                if info is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, info)
                return

            run_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/actions/runs/(\d+)", path
            )
            if run_match:
                run_id = run_match.group(1)
                with state.lock:
                    candidates = [
                        v
                        for (rid, _att), v in state.run_attempts.items()
                        if rid == run_id
                    ]
                if not candidates:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, candidates[-1])
                return

            compare_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/compare/([0-9a-f]{{40}})\.\.\.([0-9a-f]{{40}})",
                path,
            )
            if compare_match:
                base_arg, head_arg = compare_match.group(1), compare_match.group(2)
                with state.lock:
                    pr = next(
                        (
                            p
                            for p in state.prs.values()
                            if p["head_sha"] == head_arg
                            or p.get("analysis_head_sha") == head_arg
                        ),
                        None,
                    )
                if pr is None:
                    pr = state.ensure_pr(1, head_sha=head_arg, base_sha=base_arg)
                merge_base = str(pr.get("merge_base_sha") or base_arg)
                if "application/vnd.github.v3.diff" in self.headers.get("Accept", ""):
                    diff_bytes = str(pr.get("diff", DEFAULT_PR_DIFF)).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(diff_bytes)))
                    self.end_headers()
                    self.wfile.write(diff_bytes)
                    return
                page = int((query.get("page") or ["1"])[0])
                per_page = int((query.get("per_page") or ["250"])[0])
                commits_list = list(pr.get("compare_commits") or [{"sha": head_arg}])
                start = max(0, (page - 1) * per_page)
                commits_slice = commits_list[start : start + per_page]
                if page >= 2:
                    page2_status = pr.get("compare_page2_status")
                    if page2_status is not None:
                        self._send_json(
                            int(page2_status),
                            {"message": "compare page 2 unavailable"},
                        )
                        return
                    self._send_json(
                        200,
                        {
                            "merge_base_commit": {"sha": merge_base},
                            "base_commit": {"sha": merge_base},
                            "status": "ahead",
                            "commits": commits_slice,
                        },
                    )
                    return
                default_patch = "@@ -1,2 +1,3 @@\n def pick(items, idx):\n+    # validate index\n     return items[idx]\n"
                files = pr.get("compare_files")
                if files is None:
                    files = [
                        {
                            "filename": "src/app.py",
                            "status": "modified",
                            "patch": default_patch,
                        }
                    ]
                if pr.get("compare_json_oversized"):
                    oversized_files = [
                        {
                            "filename": "src/oversized.py",
                            "status": "modified",
                            "patch": "+" + ("z" * (530 * 1024)),
                        }
                    ]
                    self._send_json(
                        200,
                        {
                            "merge_base_commit": {"sha": merge_base},
                            "base_commit": {"sha": merge_base},
                            "status": "ahead",
                            "commits": commits_slice,
                            "files": oversized_files,
                        },
                    )
                    return
                self._send_json(
                    200,
                    {
                        "merge_base_commit": {"sha": merge_base},
                        "base_commit": {"sha": merge_base},
                        "status": "ahead",
                        "commits": commits_slice,
                        "files": files,
                    },
                )
                return

            commit_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/git/commits/([0-9a-f]{{40}})", path
            )
            if commit_match:
                commit_sha = commit_match.group(1)
                with state.lock:
                    parent_shas = state.commits.get(commit_sha)
                    commit_date = state.commit_dates.get(
                        commit_sha, "2026-11-27T00:00:00Z"
                    )
                if parent_shas is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(
                    200,
                    {
                        "sha": commit_sha,
                        "tree": {"sha": commit_sha},
                        "committer": {"date": commit_date},
                        "parents": [{"sha": p} for p in parent_shas],
                    },
                )
                return

            tree_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/git/trees/([0-9a-f]{{40}})", path
            )
            if tree_match:
                tree_sha = tree_match.group(1)
                recursive = query.get("recursive", ["0"])[0] == "1"
                with state.lock:
                    payload = state.trees.get((tree_sha, recursive))
                    if payload is None and recursive:
                        payload = state.trees.get((tree_sha, False))
                if payload is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, payload)
                return

            blob_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/git/blobs/([0-9a-f]{{40}})", path
            )
            if blob_match:
                blob_sha = blob_match.group(1)
                with state.lock:
                    raw = state.blobs.get(blob_sha)
                if raw is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(
                    200,
                    {
                        "sha": blob_sha,
                        "size": len(raw),
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode("ascii"),
                    },
                )
                return

            comments_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/issues/(\d+)/comments", path
            )
            if comments_match:
                pr_num = int(comments_match.group(1))
                pr = state.ensure_pr(pr_num)
                since_list = query.get("since")
                since_dt = _parse_iso_utc(since_list[0]) if since_list else None
                per_page = int((query.get("per_page") or ["100"])[0])
                page = int((query.get("page") or ["1"])[0])
                with state.lock:
                    items = list(pr["comments"])
                if since_dt is not None:
                    items = [
                        c for c in items if _parse_iso_utc(c["updated_at"]) >= since_dt
                    ]
                start = (page - 1) * per_page
                slice_items = items[start : start + per_page]
                headers: dict[str, str] | None = None
                if start + per_page < len(items):
                    safe_comments_path = (
                        f"/repos/{REPO_FULL}/issues/{int(pr_num)}/comments"
                    )
                    headers = {
                        "Link": (
                            f"<https://api.github.com{safe_comments_path}"
                            f"?per_page={int(per_page)}&page={int(page) + 1}"
                            '&sort=created&direction=asc>; rel="next"'
                        )
                    }
                self._send_json(200, slice_items, extra_headers=headers)
                return

            single_comment_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/issues/comments/(\d+)", path
            )
            if single_comment_match:
                cid = int(single_comment_match.group(1))
                with state.lock:
                    found = next(
                        (
                            c
                            for pr in state.prs.values()
                            for c in pr["comments"]
                            if c["id"] == cid
                        ),
                        None,
                    )
                if found is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, found)
                return

            reviews_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/pulls/(\d+)/reviews", path
            )
            if reviews_match:
                pr_num = int(reviews_match.group(1))
                pr = state.ensure_pr(pr_num)
                per_page = int((query.get("per_page") or ["100"])[0])
                page = int((query.get("page") or ["1"])[0])
                with state.lock:
                    items = list(pr["reviews"])
                start = (page - 1) * per_page
                headers = None
                if start + per_page < len(items):
                    safe_reviews_path = (
                        f"/repos/{REPO_FULL}/pulls/{int(pr_num)}/reviews"
                    )
                    headers = {
                        "Link": (
                            f"<https://api.github.com{safe_reviews_path}"
                            f'?per_page={int(per_page)}&page={int(page) + 1}>; rel="next"'
                        )
                    }
                self._send_json(
                    200, items[start : start + per_page], extra_headers=headers
                )
                return

            single_review_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/pulls/(\d+)/reviews/(\d+)", path
            )
            if single_review_match:
                pr_num = int(single_review_match.group(1))
                rid = int(single_review_match.group(2))
                pr = state.ensure_pr(pr_num)
                with state.lock:
                    found = next((r for r in pr["reviews"] if r["id"] == rid), None)
                if found is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, found)
                return

            if re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/pulls/(\d+)/comments", path
            ):
                self._send_json(200, [])
                return

            if re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/commits/[0-9a-f]{{40}}/check-runs",
                path,
            ):
                self._send_json(200, {"total_count": 0, "check_runs": []})
                return

            self._send_json(404, {"message": "Not Found"})

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            with state.lock:
                state.requests_log.append(("POST", path))

            if path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
                if not self._require_app_jwt():
                    self._send_json(401, {"message": "Bad credentials"})
                    return
                body = self._read_json_body()
                perms = dict(body.get("permissions") or {})
                perms.setdefault("metadata", "read")
                tok = f"ghs_v6_accept_{secrets.token_hex(16)}"
                expires_at = _iso_utc(
                    datetime.now(tz=timezone.utc) + timedelta(hours=1)
                )
                with state.lock:
                    state.active_tokens[tok] = {
                        "permissions": perms,
                        "expires_at": expires_at,
                        "revoked": False,
                    }
                    state.minted_tokens_history.append(tok)
                    # Count write-scoped tokens minted for runners (excluding read-only edge metadata tokens).
                    if any(v == "write" for v in perms.values()):
                        state.runner_write_tokens.append(tok)
                        state.runner_mint_count += 1
                self._send_json(
                    201,
                    {
                        "token": tok,
                        "expires_at": expires_at,
                        "permissions": perms,
                    },
                )
                return

            ok, status, _ = self._authenticate_rest(write=True)
            if not ok:
                self._send_json(
                    status,
                    {
                        "message": "Resource not accessible by integration"
                        if status == 403
                        else "Bad credentials"
                    },
                )
                return

            body = self._read_json_body()
            body_str = json.dumps(body)

            comments_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/issues/(\d+)/comments", path
            )
            if comments_match:
                if self._maybe_trigger_crash("POST", path, body_str):
                    return
                pr_num = int(comments_match.group(1))
                created = state.add_app_comment(pr_num, str(body.get("body", "")))
                with state.lock:
                    hook = state.head_advance_after_comment_hook
                    if (
                        hook is not None
                        and hook[0] == pr_num
                        and hook[1] in str(body.get("body", ""))
                    ):
                        state.prs[pr_num]["head_sha"] = hook[2]
                        state.head_advance_after_comment_hook = None
                self._send_json(201, created)
                return

            reviews_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/pulls/(\d+)/reviews", path
            )
            if reviews_match:
                if self._maybe_trigger_crash("POST", path, body_str):
                    return
                pr_num = int(reviews_match.group(1))
                with state.lock:
                    rejection = state.reject_review_status.get(pr_num)
                if rejection is not None:
                    self._send_json(rejection[0], rejection[1])
                    return
                pr = state.ensure_pr(pr_num)
                with state.lock:
                    state.next_review_id += 1
                    rid = state.next_review_id
                    review = {
                        "id": rid,
                        "user": {"id": BOT_USER_ID, "login": BOT_LOGIN, "type": "Bot"},
                        "state": "COMMENTED",
                        "body": str(body.get("body", "")),
                        "commit_id": str(body.get("commit_id", HEAD_SHA)),
                        "pull_request_url": f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/{pr_num}",
                        "performed_via_github_app": {"id": APP_ID, "slug": APP_SLUG},
                    }
                    pr["reviews"].append(review)
                if self._should_drop_reply("POST", path):
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                self._send_json(200, review)
                return

            self._send_json(404, {"message": "Not Found"})

        def do_DELETE(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            with state.lock:
                state.requests_log.append(("DELETE", path))
            if path != "/installation/token":
                self._send_json(404, {"message": "Not Found"})
                return
            token = self._bearer_token()
            with state.lock:
                meta = state.active_tokens.get(token)
                if meta is None or meta["revoked"]:
                    self._send_json(401, {"message": "Bad credentials"})
                    return
                meta["revoked"] = True
                state.revoked_tokens.add(token)
            self._send_empty(204)

    return Handler


@dataclass
class _MockOpenAIProviderState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    calls: int = 0
    expected_api_key: str = PROVIDER_API_KEY
    model: str = "gpt-4o-mini"
    comment_path: str = "src/app.py"
    large_envelope_comments: int = 0


def _make_openai_provider_handler(
    provider_state: _MockOpenAIProviderState,
) -> type[http.server.BaseHTTPRequestHandler]:
    class ProviderHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(
            self,
            status: int,
            payload: Any,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            raw = json.dumps(payload).encode("utf-8")
            if extra_headers:
                for hk, hv in extra_headers.items():
                    if "\r" in hk or "\n" in hk or "\r" in hv or "\n" in hv:
                        raise ValueError("header injection rejected")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path != "/v1/chat/completions":
                self._send_json(404, {"error": "not_found"})
                return
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {provider_state.expected_api_key}":
                self._send_json(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b""
            try:
                req_json = json.loads(raw_body.decode("utf-8")) if raw_body else None
            except Exception:
                req_json = None
            if (
                not isinstance(req_json, dict)
                or not isinstance(req_json.get("model"), str)
                or not req_json["model"].strip()
                or not isinstance(req_json.get("messages"), list)
                or not req_json["messages"]
            ):
                self._send_json(400, {"error": "invalid_request"})
                return
            with provider_state.lock:
                provider_state.calls += 1
                call_num = provider_state.calls
            if provider_state.large_envelope_comments > 0:
                comments_list = []
                for idx in range(provider_state.large_envelope_comments):
                    entropy = "".join(
                        hashlib.sha256(f"{call_num}:{idx}:{blk}".encode()).hexdigest()
                        for blk in range(180)
                    )
                    comments_list.append(
                        {
                            "path": provider_state.comment_path,
                            "line": 2 + idx,
                            "body": f"Finding #{idx}: {entropy[:11500]}",
                            "blocking": False,
                            "severity": "medium",
                            "fix_effort": "small",
                            "category": "correctness",
                        }
                    )
            else:
                comments_list = [
                    {
                        "path": provider_state.comment_path,
                        "line": 2,
                        "body": "Ensure `idx` is within `[0, len(items))` before indexing.",
                        "blocking": False,
                        "severity": "medium",
                        "fix_effort": "small",
                        "category": "correctness",
                    }
                ]
            review_json = json.dumps(
                {
                    "summary": f"ReviewSensei analysis pass #{call_num}: validate index bounds.",
                    "comments": comments_list,
                    "learning_proposals": [],
                }
            )
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-v6-test-{call_num}",
                    "object": "chat.completion",
                    "model": provider_state.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": review_json},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

    return ProviderHandler


class V6OneWriterWorkerQualificationTests(unittest.TestCase):
    """End-to-end acceptance checks for ReviewSensei v6 coordinator + wheel runner."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="v6-worker-qual-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.venv_dir, self.cli_bin, self.provenance = _build_offline_venv_and_install(
            self.tmp_path
        )
        self.venv_python = self.venv_dir / "bin" / "python"
        self.workflow_sha = str(self.provenance["workflow_commit_sha"])
        self.workflow_ref = _workflow_ref_for_sha(self.workflow_sha)

        self.ca_pem, self.cert_pem, self.key_pem = _generate_tls_materials(
            self.tmp_path
        )
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ssl_ctx.load_verify_locations(cafile=str(self.ca_pem))

        self.oidc_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.app_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.app_key_pem = self.app_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

        self.gh_state = _SimulatedGitHubState(
            oidc_key=self.oidc_key,
            app_key=self.app_key,
            workflow_sha=self.workflow_sha,
        )
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        server_ctx.load_cert_chain(
            certfile=str(self.cert_pem), keyfile=str(self.key_pem)
        )
        self.gh_server = _QuietThreadingHTTPServer(
            ("127.0.0.1", 0), _make_github_handler(self.gh_state)
        )
        self.gh_server.socket = server_ctx.wrap_socket(
            self.gh_server.socket, server_side=True
        )
        self.gh_port = int(self.gh_server.server_address[1])
        self.gh_thread = threading.Thread(
            target=self.gh_server.serve_forever, daemon=True
        )
        self.gh_thread.start()
        self.addCleanup(self._stop_gh_server)

        self.worker_log = self.tmp_path / "worker.log"
        bridge_cfg = self.tmp_path / "bridge-config.json"
        bridge_cfg.write_text(
            json.dumps(
                {
                    "certPath": str(self.cert_pem),
                    "keyPath": str(self.key_pem),
                    "logPath": str(self.worker_log),
                    "githubPort": self.gh_port,
                    "env": {
                        "GITHUB_APP_ID": str(APP_ID),
                        "GITHUB_APP_PRIVATE_KEY": self.app_key_pem,
                        "REVIEWSENSEI_SIGNING_KEY": SIGNING_KEY,
                        "V6_PILOT_REPOSITORIES": REPO_FULL,
                        "V6_PILOT_WORKFLOW_REF": self.workflow_ref,
                    },
                }
            ),
            encoding="utf-8",
        )
        bridge_env = {
            **os.environ,
            "NODE_EXTRA_CA_CERTS": str(self.ca_pem),
        }
        self.bridge_proc = subprocess.Popen(
            ["node", "--experimental-sqlite", str(BRIDGE_PATH), str(bridge_cfg)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=bridge_env,
        )
        self.addCleanup(self._stop_bridge_proc)
        assert self.bridge_proc.stdout is not None
        ready_line = self.bridge_proc.stdout.readline()
        if not ready_line:
            stderr_text = (
                self.bridge_proc.stderr.read() if self.bridge_proc.stderr else ""
            )
            raise RuntimeError(f"worker bridge failed to start: {stderr_text}")
        self.worker_port = int(json.loads(ready_line)["port"])
        self.worker_url = f"https://127.0.0.1:{self.worker_port}"

    def _stop_gh_server(self) -> None:
        self.gh_server.shutdown()
        self.gh_server.server_close()
        self.gh_thread.join(timeout=5)

    def _stop_bridge_proc(self) -> None:
        if self.bridge_proc.poll() is None:
            if self.bridge_proc.stdin is not None and not self.bridge_proc.stdin.closed:
                self.bridge_proc.stdin.close()
            try:
                self.bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bridge_proc.terminate()
                self.bridge_proc.wait(timeout=5)
        for stream in (
            self.bridge_proc.stdin,
            self.bridge_proc.stdout,
            self.bridge_proc.stderr,
        ):
            if stream is not None and not stream.closed:
                stream.close()

    def _post_worker(
        self, route: str, payload: dict[str, Any]
    ) -> tuple[int, str, dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.worker_url}{route}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, raw, json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
            finally:
                exc.close()
            return exc.code, raw, json.loads(raw)

    def _runner_env(
        self,
        *,
        run_id: int,
        run_attempt: int = 1,
        repository: str = REPO_FULL,
        repository_id: int = REPO_ID,
        workflow_sha: str | None = None,
        job_workflow_ref: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        effective_workflow_sha = (
            workflow_sha if workflow_sha is not None else self.workflow_sha
        )
        effective_job_workflow_ref = (
            job_workflow_ref
            if job_workflow_ref is not None
            else _workflow_ref_for_sha(effective_workflow_sha)
        )
        env = {
            "PATH": f"{self.venv_dir / 'bin'}:{os.environ.get('PATH', '')}",
            "SSL_CERT_FILE": str(self.ca_pem),
            "GITHUB_TOKEN": WF_READ_TOKEN,
            "GITHUB_API_URL": f"https://127.0.0.1:{self.gh_port}",
            "ACTIONS_ID_TOKEN_REQUEST_URL": f"https://127.0.0.1:{self.gh_port}/oidc/token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": (
                f"{OIDC_REQUEST_PREFIX}run_id={run_id},run_attempt={run_attempt},"
                f"repository={repository},repository_id={repository_id},"
                f"workflow_sha={effective_workflow_sha},"
                f"job_workflow_ref={effective_job_workflow_ref}"
            ),
            "GITHUB_RUN_ID": str(run_id),
            "GITHUB_RUN_ATTEMPT": str(run_attempt),
            "REVIEW_SENSEI_WORKFLOW_SHA": effective_workflow_sha,
        }
        if extra_env:
            env.update(extra_env)
        return env

    def _runner_argv(
        self,
        *,
        pr_number: int,
        operation_id: str | None,
        run_id: int,
        run_attempt: int = 1,
        head_sha: str = HEAD_SHA,
        workflow_sha: str | None = None,
        outcome_path: Path,
        step_summary_path: Path,
        initial_findings_path: Path | None = None,
        analyze_fresh_pr: bool = False,
        policy_root: Path | None = None,
    ) -> list[str]:
        effective_workflow_sha = (
            workflow_sha if workflow_sha is not None else self.workflow_sha
        )
        argv = [
            str(self.cli_bin),
            "v6-pilot-run",
            "--repository",
            REPO_FULL,
            "--repository-id",
            str(REPO_ID),
            "--pull-request",
            str(pr_number),
            "--head-sha",
            head_sha,
            "--coordinator-url",
            self.worker_url,
            "--github-api-url",
            f"https://127.0.0.1:{self.gh_port}",
            "--run-id",
            str(run_id),
            "--run-attempt",
            str(run_attempt),
            "--workflow-sha",
            effective_workflow_sha,
            "--expected-bot-login",
            BOT_LOGIN,
            "--outcome",
            str(outcome_path),
            "--step-summary-path",
            str(step_summary_path),
        ]
        if operation_id is not None:
            argv.extend(["--operation-id", operation_id])
        if initial_findings_path is not None:
            argv.extend(["--initial-findings-json", str(initial_findings_path)])
        if analyze_fresh_pr:
            argv.append("--analyze-fresh-pr")
        if policy_root is not None:
            argv.extend(["--policy-root", str(policy_root)])
        return argv

    def _start_mock_provider(self) -> tuple[_MockOpenAIProviderState, int]:
        provider_state = _MockOpenAIProviderState()
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        server_ctx.load_cert_chain(
            certfile=str(self.cert_pem), keyfile=str(self.key_pem)
        )
        server = _QuietThreadingHTTPServer(
            ("127.0.0.1", 0), _make_openai_provider_handler(provider_state)
        )
        server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_cleanup)
        return provider_state, port

    def _write_trusted_policy(
        self,
        name: str,
        provider_port: int,
        *,
        writes: bool = True,
        automatic_reviews: bool = True,
    ) -> Path:
        policy_dir = self.tmp_path / name
        policy_dir.mkdir(parents=True, exist_ok=True)
        policy_file = policy_dir / ".reviewsensei.yml"
        policy_file.write_text(
            "schema: 1\n"
            "github:\n"
            f"  writes: {'true' if writes else 'false'}\n"
            f"  automatic_reviews: {'true' if automatic_reviews else 'false'}\n"
            "inference:\n"
            "  backend: openai-compatible\n"
            "  model: gpt-4o-mini\n"
            "advanced:\n"
            "  endpoint:\n"
            f"    base_url: https://127.0.0.1:{provider_port}/v1\n"
            "    allow_custom_endpoint: true\n",
            encoding="utf-8",
        )
        return policy_dir

    def _assert_no_secret_leaks(self, *paths_or_texts: Path | str) -> None:
        forbidden = [
            WF_READ_TOKEN,
            SIGNING_KEY,
            PROVIDER_API_KEY,
            "-----BEGIN PRIVATE KEY-----",
            "ghs_",
            "ghp_",
        ]
        with self.gh_state.lock:
            forbidden.extend(self.gh_state.minted_tokens_history)
        for item in paths_or_texts:
            if isinstance(item, Path):
                if not item.exists():
                    continue
                content = item.read_text(encoding="utf-8", errors="replace")
            else:
                content = item
            for needle in forbidden:
                self.assertNotIn(needle, content)
            # No raw OIDC JWTs (eyJ... . eyJ... . sig).
            self.assertIsNone(
                re.search(
                    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+",
                    content,
                )
            )

    def _assert_coordinator_slot_state(
        self,
        *,
        pr_number: int,
        run_id: int,
        expected_state: str,
        run_attempt: int = 1,
    ) -> dict[str, Any]:
        """Verify live coordinator slot state via ``POST /github/v6/lease/status``."""

        status_oidc = self.gh_state.issue_oidc_jwt(
            run_id=str(run_id), run_attempt=run_attempt
        )
        st_code, st_raw, st_body = self._post_worker(
            "/github/v6/lease/status",
            {"oidc_token": status_oidc, "pull_request": pr_number},
        )
        self.assertEqual(st_code, 200, st_raw)
        self.assertEqual(st_body["state"], expected_state, st_raw)
        self._assert_no_secret_leaks(st_raw)
        return st_body

    def test_integrated_worker_and_wheel_runner_acceptance_scenarios(self) -> None:
        # 0. Verify provenance hashes and installed packages inside venv (never repo checkout).
        provenance = self.provenance
        self.assertRegex(provenance["workflow_commit_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(provenance["workflow_commit_sha"], self.workflow_sha)
        self.assertEqual(self.workflow_ref, _workflow_ref_for_sha(self.workflow_sha))
        self.assertRegex(provenance["trusted_policy_revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(provenance["worker_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(provenance["wheel_sha256s"]), 2)
        wheel_names = sorted(provenance["wheel_sha256s"])
        self.assertTrue(
            any(
                name.startswith("review_sensei-") and name.endswith(".whl")
                for name in wheel_names
            ),
            wheel_names,
        )
        self.assertTrue(
            any(
                name.startswith("review_sensei_github-") and name.endswith(".whl")
                for name in wheel_names
            ),
            wheel_names,
        )
        for whl_sha in provenance["wheel_sha256s"].values():
            self.assertRegex(whl_sha, r"^[0-9a-f]{64}$")
        _verify_install_provenance(
            expected_commit_sha=self.workflow_sha,
            wheel_dir=Path(provenance["wheel_dir"]),
            stdout_text=str(provenance["install_stdout"]),
            step_summary_path=Path(provenance["install_step_summary_path"]),
        )

        pkg_paths = subprocess.run(
            [
                str(self.venv_python),
                "-c",
                (
                    "import json, review_sensei, review_sensei_github; "
                    "print(json.dumps([review_sensei.__file__, review_sensei_github.__file__]))"
                ),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self.tmp_path),
        )
        for pkg_file in json.loads(pkg_paths.stdout):
            self.assertTrue(
                Path(pkg_file).resolve().is_relative_to(self.venv_dir.resolve()),
                pkg_file,
            )

        # =====================================================================
        # Scenario 1: Clean --help, unauthorized OIDC refusal, authorized acquire + zero leaks
        # =====================================================================
        help_cwd = self.tmp_path / "help-cwd"
        help_cwd.mkdir()
        scrubbed_env = {"PATH": f"{self.venv_dir / 'bin'}:/usr/bin:/bin"}
        with self.gh_state.lock:
            req_before_help = len(self.gh_state.requests_log)
        help_proc = subprocess.run(
            [str(self.cli_bin), "v6-pilot-run", "--help"],
            cwd=str(help_cwd),
            env=scrubbed_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
        self.assertIn("--initial-findings-json", help_proc.stdout)
        with self.gh_state.lock:
            self.assertEqual(len(self.gh_state.requests_log), req_before_help)

        bad_oidc = self.gh_state.issue_oidc_jwt(
            run_id="900",
            run_attempt=1,
            workflow_sha="f" * 40,
            job_workflow_ref=f"{REPO_FULL}/.github/workflows/untrusted.yml@{'f' * 40}",
        )
        unauth_status, unauth_raw, unauth_body = self._post_worker(
            "/github/v6/lease/acquire",
            {
                "oidc_token": bad_oidc,
                "pull_request": 41,
                "head_sha": HEAD_SHA,
                "capabilities": ["review_publish"],
            },
        )
        self.assertEqual(unauth_status, 403)
        self.assertEqual(
            unauth_body["diagnostic"]["code"], "v6_lease_unauthorized_tuple"
        )
        self._assert_no_secret_leaks(unauth_raw, self.worker_log)

        findings_file = self.tmp_path / "findings-pr41.json"
        findings_file.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "path": "src/example.py",
                            "line": 12,
                            "body": "Check boundary condition.",
                        }
                    ],
                    "required_evidence_digests": [],
                }
            ),
            encoding="utf-8",
        )
        self.gh_state.ensure_pr(41)
        self.gh_state.set_run_attempt("901", 1, status="in_progress", conclusion=None)
        out_41 = self.tmp_path / "outcome-41.json"
        sum_41 = self.tmp_path / "summary-41.md"
        cwd_41 = self.tmp_path / "cwd-41"
        cwd_41.mkdir()
        res_41 = subprocess.run(
            self._runner_argv(
                pr_number=41,
                operation_id="op-accept-pr41",
                run_id=901,
                outcome_path=out_41,
                step_summary_path=sum_41,
                initial_findings_path=findings_file,
            ),
            cwd=str(cwd_41),
            env=self._runner_env(run_id=901),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_41.returncode, 0, res_41.stderr + res_41.stdout)
        parsed_41 = json.loads(out_41.read_text(encoding="utf-8"))
        self.assertEqual(parsed_41["status"], "completed")
        self.assertEqual(parsed_41["coordinator_state"], "IDLE")
        self.assertTrue(parsed_41["lease_released"])
        self.assertEqual(parsed_41["model_calls_started"], 1)
        self.assertEqual(len(self.gh_state.prs[41]["reviews"]), 1)
        # Ensure real ghs_ token was minted and then revoked on clean release.
        self.assertEqual(len(self.gh_state.revoked_tokens), 1)
        # Verify status route body reports slot back to IDLE and never echoes tokens.
        status_oidc = self.gh_state.issue_oidc_jwt(run_id="901", run_attempt=1)
        st_code, st_raw, st_body = self._post_worker(
            "/github/v6/lease/status",
            {"oidc_token": status_oidc, "pull_request": 41},
        )
        self.assertEqual(st_code, 200, st_raw)
        self.assertEqual(st_body["state"], "IDLE", st_raw)
        self._assert_no_secret_leaks(
            self.worker_log,
            st_raw,
            res_41.stdout,
            res_41.stderr,
            out_41,
            sum_41,
        )

        # =====================================================================
        # Scenario 2: Fresh PR #42 -> SIGKILL after review #1 before receipt -> cold successor adopts once
        # =====================================================================
        self.gh_state.ensure_pr(42)
        self.gh_state.set_run_attempt("1001", 1, status="in_progress", conclusion=None)
        findings_42 = self.tmp_path / "findings-pr42.json"
        findings_42.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "path": "src/service.py",
                            "line": 27,
                            "body": "Ensure idempotent state update.",
                        }
                    ],
                    "required_evidence_digests": [],
                }
            ),
            encoding="utf-8",
        )
        out_42_r1 = self.tmp_path / "outcome-42-r1.json"
        sum_42_r1 = self.tmp_path / "summary-42-r1.md"
        cwd_42_r1 = self.tmp_path / "cwd-42-r1"
        cwd_42_r1.mkdir()

        proc_42_r1 = subprocess.Popen(
            self._runner_argv(
                pr_number=42,
                operation_id="op-accept-pr42",
                run_id=1001,
                outcome_path=out_42_r1,
                step_summary_path=sum_42_r1,
                initial_findings_path=findings_42,
            ),
            cwd=str(cwd_42_r1),
            env=self._runner_env(run_id=1001),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self.gh_state.lock:
            self.gh_state.crash_hook = _CrashHook(
                method="POST",
                path=f"/repos/{REPO_FULL}/issues/42/comments",
                body_contains="reviewsensei:v6:receipt:v1",
                pid=proc_42_r1.pid,
            )
        rc_42_r1 = proc_42_r1.wait(timeout=30)
        stdout_42_r1 = ""
        stderr_42_r1 = ""
        if proc_42_r1.stdout is not None:
            stdout_42_r1 = proc_42_r1.stdout.read()
            proc_42_r1.stdout.close()
        if proc_42_r1.stderr is not None:
            stderr_42_r1 = proc_42_r1.stderr.read()
            proc_42_r1.stderr.close()
        self.assertEqual(rc_42_r1, -signal.SIGKILL)
        self.assertTrue(self.gh_state.crash_hook.triggered)

        # Mark Run 1 terminal failure and verify slot is still ACTIVE (Gen 1), 1 review, 0 receipts.
        self.gh_state.set_run_attempt(
            "1001", 1, status="completed", conclusion="failure"
        )
        oidc_1002 = self.gh_state.issue_oidc_jwt(run_id="1002", run_attempt=1)
        st42_code, _, st42_body = self._post_worker(
            "/github/v6/lease/status",
            {"oidc_token": oidc_1002, "pull_request": 42},
        )
        self.assertEqual(st42_code, 200)
        self.assertEqual(st42_body["state"], "ACTIVE")
        self.assertEqual(st42_body["generation"], 1)
        with self.gh_state.lock:
            pr42_reviews = list(self.gh_state.prs[42]["reviews"])
            pr42_comments = list(self.gh_state.prs[42]["comments"])
            gen1_token_42 = self.gh_state.runner_write_tokens[-1]
        self.assertEqual(len(pr42_reviews), 1)
        pr42_records = [
            rec
            for c in pr42_comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
        ]
        self.assertTrue(
            any(isinstance(r, UnresolvedResultRecord) for r in pr42_records)
        )
        self.assertTrue(any(isinstance(r, WriteIntentRecord) for r in pr42_records))
        self.assertFalse(
            any(isinstance(r, CompletionReceiptRecord) for r in pr42_records)
        )

        # Run 2: cold successor, clean cwd, NO local state, poisoned findings path (proves zero reads).
        poisoned_findings_42 = self.tmp_path / "poisoned-findings-42.json"
        poisoned_findings_42.write_text(
            "{NOT_VALID_JSON_MUST_NOT_READ", encoding="utf-8"
        )
        self.gh_state.set_run_attempt("1002", 1, status="in_progress", conclusion=None)
        out_42_r2 = self.tmp_path / "outcome-42-r2.json"
        sum_42_r2 = self.tmp_path / "summary-42-r2.md"
        cwd_42_r2 = self.tmp_path / "cwd-42-r2"
        cwd_42_r2.mkdir()
        res_42_r2 = subprocess.run(
            self._runner_argv(
                pr_number=42,
                operation_id="op-accept-pr42",
                run_id=1002,
                outcome_path=out_42_r2,
                step_summary_path=sum_42_r2,
                initial_findings_path=poisoned_findings_42,
            ),
            cwd=str(cwd_42_r2),
            env=self._runner_env(run_id=1002),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_42_r2.returncode, 0, res_42_r2.stderr + res_42_r2.stdout)
        parsed_42_r2 = json.loads(out_42_r2.read_text(encoding="utf-8"))
        self.assertEqual(parsed_42_r2["status"], "completed")
        self.assertEqual(parsed_42_r2["coordinator_state"], "IDLE")
        self.assertTrue(parsed_42_r2["lease_released"])
        self.assertEqual(parsed_42_r2["generation"], 2)
        self.assertEqual(parsed_42_r2["model_calls_started"], 0)
        self.assertTrue(parsed_42_r2["reconciled_from_existing_effect"])
        self._assert_coordinator_slot_state(
            pr_number=42, run_id=1002, expected_state="IDLE"
        )
        # Gen 1 token was revoked via DELETE (204) and returns 401 if used against GitHub.
        self.assertIn(gen1_token_42, self.gh_state.revoked_tokens)
        req_revoked = urllib.request.Request(
            f"https://127.0.0.1:{self.gh_port}/repos/{REPO_FULL}/pulls/42",
            headers={"Authorization": f"Bearer {gen1_token_42}"},
            method="GET",
        )
        with self.assertRaises(urllib.error.HTTPError) as rev_err:
            urllib.request.urlopen(req_revoked, context=self.ssl_ctx, timeout=5)
        self.assertEqual(rev_err.exception.code, 401)
        rev_err.exception.close()
        # Strictly 1 review total and 1 receipt for op-accept-pr42.
        self.assertEqual(len(self.gh_state.prs[42]["reviews"]), 1)
        pr42_receipts = [
            rec
            for c in self.gh_state.prs[42]["comments"]
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, CompletionReceiptRecord)
            and rec.operation_id == "op-accept-pr42"
        ]
        self.assertEqual(len(pr42_receipts), 1)
        self._assert_no_secret_leaks(
            self.worker_log,
            stdout_42_r1,
            stderr_42_r1,
            out_42_r1,
            sum_42_r1,
            res_42_r2.stdout,
            res_42_r2.stderr,
            out_42_r2,
            sum_42_r2,
        )

        # =====================================================================
        # Scenario 3: Fresh PR #43 -> SIGKILL on POST /pulls/43/reviews -> HOLD ->
        # resolve-hold(reconciled_clean) returns RECONCILING -> acquire 409 ->
        # Run 3 returns HOLD with 0 new tokens, 0 reviews, 0 model calls
        # =====================================================================
        self.gh_state.ensure_pr(43)
        self.gh_state.set_run_attempt("2001", 1, status="in_progress", conclusion=None)
        findings_43 = self.tmp_path / "findings-pr43.json"
        findings_43.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "path": "src/handler.py",
                            "line": 9,
                            "body": "Validate input bounds.",
                        }
                    ],
                    "required_evidence_digests": [],
                }
            ),
            encoding="utf-8",
        )
        out_43_r1 = self.tmp_path / "outcome-43-r1.json"
        sum_43_r1 = self.tmp_path / "summary-43-r1.md"
        cwd_43_r1 = self.tmp_path / "cwd-43-r1"
        cwd_43_r1.mkdir()

        proc_43_r1 = subprocess.Popen(
            self._runner_argv(
                pr_number=43,
                operation_id="op-accept-pr43",
                run_id=2001,
                outcome_path=out_43_r1,
                step_summary_path=sum_43_r1,
                initial_findings_path=findings_43,
            ),
            cwd=str(cwd_43_r1),
            env=self._runner_env(run_id=2001),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self.gh_state.lock:
            self.gh_state.crash_hook = _CrashHook(
                method="POST",
                path=f"/repos/{REPO_FULL}/pulls/43/reviews",
                pid=proc_43_r1.pid,
            )
        rc_43_r1 = proc_43_r1.wait(timeout=30)
        stdout_43_r1 = ""
        stderr_43_r1 = ""
        if proc_43_r1.stdout is not None:
            stdout_43_r1 = proc_43_r1.stdout.read()
            proc_43_r1.stdout.close()
        if proc_43_r1.stderr is not None:
            stderr_43_r1 = proc_43_r1.stderr.read()
            proc_43_r1.stderr.close()
        self.assertEqual(rc_43_r1, -signal.SIGKILL)
        self.assertTrue(self.gh_state.crash_hook.triggered)
        self.assertEqual(len(self.gh_state.prs[43]["reviews"]), 0)

        self.gh_state.set_run_attempt(
            "2001", 1, status="completed", conclusion="failure"
        )
        self.gh_state.set_run_attempt("2002", 1, status="in_progress", conclusion=None)
        with self.gh_state.lock:
            mints_before_43_r2 = self.gh_state.runner_mint_count
            gen1_token_43 = self.gh_state.runner_write_tokens[-1]

        out_43_r2 = self.tmp_path / "outcome-43-r2.json"
        sum_43_r2 = self.tmp_path / "summary-43-r2.md"
        cwd_43_r2 = self.tmp_path / "cwd-43-r2"
        cwd_43_r2.mkdir()
        res_43_r2 = subprocess.run(
            self._runner_argv(
                pr_number=43,
                operation_id="op-accept-pr43",
                run_id=2002,
                outcome_path=out_43_r2,
                step_summary_path=sum_43_r2,
            ),
            cwd=str(cwd_43_r2),
            env=self._runner_env(run_id=2002),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_43_r2.returncode, 1)
        parsed_43_r2 = json.loads(out_43_r2.read_text(encoding="utf-8"))
        self.assertEqual(parsed_43_r2["status"], "hold")
        self.assertEqual(parsed_43_r2["coordinator_state"], "HOLD")
        self.assertEqual(parsed_43_r2["model_calls_started"], 0)
        self.assertIn(gen1_token_43, self.gh_state.revoked_tokens)
        self.assertEqual(len(self.gh_state.prs[43]["reviews"]), 0)
        self.assertEqual(self.gh_state.runner_mint_count, mints_before_43_r2)

        # Locate PR 43 owner comment & slot_key, post App-authored hold:v1 with resolved_by_record_id.
        pr43_owner_cid: int | None = None
        pr43_slot_key = parsed_43_r2["slot_key"]
        for c in self.gh_state.prs[43]["comments"]:
            for rec in extract_v6_record_markers(c["body"], strict=True):
                if isinstance(rec, OwnerMarkerRecord) and rec.generation == 1:
                    pr43_owner_cid = int(c["id"])
        assert pr43_owner_cid is not None

        hold_rec = DurableHoldRecord(
            record_id="hold:op-accept-pr43:1",
            slot_key=pr43_slot_key,
            generation=1,
            lease_epoch=1,
            operation_id="op-accept-pr43",
            pr_head_sha=HEAD_SHA,
            reason_code="v6_lease_hold",
            missing_evidence=(),
            held_at=_iso_utc(),
            resolved_by_record_id="operator-audit-43",
        )
        hold_comment = self.gh_state.add_app_comment(
            43, format_v6_record_marker(hold_rec)
        )

        # Call real Worker POST /github/v6/lease/resolve-hold with resolution_kind="reconciled_clean".
        oidc_2003 = self.gh_state.issue_oidc_jwt(run_id="2003", run_attempt=1)
        rh_status, rh_raw, rh_body = self._post_worker(
            "/github/v6/lease/resolve-hold",
            {
                "oidc_token": oidc_2003,
                "slot_key": pr43_slot_key,
                "generation": 1,
                "hold_record_comment_id": hold_comment["id"],
                "resolution_kind": "reconciled_clean",
                "anchor_comment_id": pr43_owner_cid,
            },
        )
        self.assertEqual(rh_status, 200, rh_raw)
        self.assertEqual(rh_body["state"], "RECONCILING")

        # Immediate /acquire MUST fail closed with 409 v6_lease_reconciling (0 new tokens).
        acq43_status, acq43_raw, acq43_body = self._post_worker(
            "/github/v6/lease/acquire",
            {
                "oidc_token": oidc_2003,
                "pull_request": 43,
                "head_sha": HEAD_SHA,
                "capabilities": ["review_publish"],
                "operation_id": "op-accept-pr43",
            },
        )
        self.assertEqual(acq43_status, 409, acq43_raw)
        self.assertEqual(acq43_body["diagnostic"]["code"], "v6_lease_reconciling")
        self.assertEqual(self.gh_state.runner_mint_count, mints_before_43_r2)

        # Run 3: cold successor passes through Gate 2 reconciliation back to HOLD with 0 tokens, 0 reviews, 0 model calls.
        self.gh_state.set_run_attempt("2003", 1, status="in_progress", conclusion=None)
        out_43_r3 = self.tmp_path / "outcome-43-r3.json"
        sum_43_r3 = self.tmp_path / "summary-43-r3.md"
        cwd_43_r3 = self.tmp_path / "cwd-43-r3"
        cwd_43_r3.mkdir()
        res_43_r3 = subprocess.run(
            self._runner_argv(
                pr_number=43,
                operation_id="op-accept-pr43",
                run_id=2003,
                outcome_path=out_43_r3,
                step_summary_path=sum_43_r3,
            ),
            cwd=str(cwd_43_r3),
            env=self._runner_env(run_id=2003),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_43_r3.returncode, 1)
        parsed_43_r3 = json.loads(out_43_r3.read_text(encoding="utf-8"))
        self.assertEqual(parsed_43_r3["status"], "hold")
        self.assertEqual(parsed_43_r3["coordinator_state"], "HOLD")
        self.assertEqual(parsed_43_r3["model_calls_started"], 0)
        self.assertEqual(self.gh_state.runner_mint_count, mints_before_43_r2)
        self.assertEqual(len(self.gh_state.prs[43]["reviews"]), 0)
        self._assert_no_secret_leaks(
            self.worker_log,
            rh_raw,
            acq43_raw,
            stdout_43_r1,
            stderr_43_r1,
            out_43_r1,
            sum_43_r1,
            res_43_r2.stdout,
            res_43_r2.stderr,
            out_43_r2,
            sum_43_r2,
            res_43_r3.stdout,
            res_43_r3.stderr,
            out_43_r3,
            sum_43_r3,
        )

    def test_workflow_fresh_pr_analysis_and_cold_rerun_recovery(self) -> None:
        """Test 1: archive-installed CLI runs fresh PR analysis without --operation-id, crashes before receipt, and cold rerun recovers with 0 extra provider calls."""

        provider_state, provider_port = self._start_mock_provider()
        policy_dir = self._write_trusted_policy("trusted-policy-t1", provider_port)
        expected_op_id = f"v6:{REPO_ID}:51:{HEAD_SHA[:12]}:pr_review_publish"

        self.gh_state.ensure_pr(51, head_sha=HEAD_SHA)
        self.gh_state.set_run_attempt("9101", 1, status="in_progress", conclusion=None)
        out_r1 = self.tmp_path / "outcome-51-r1.json"
        sum_r1 = self.tmp_path / "summary-51-r1.md"
        cwd_r1 = self.tmp_path / "cwd-51-r1"
        cwd_r1.mkdir()

        provider_env = {
            "OPENAI_API_KEY": PROVIDER_API_KEY,
            "REVIEWSENSEI_PROVIDER": "openai-compatible",
            "REVIEWSENSEI_MODEL": "gpt-4o-mini",
        }
        proc_r1 = subprocess.Popen(
            self._runner_argv(
                pr_number=51,
                operation_id=None,
                run_id=9101,
                run_attempt=1,
                head_sha=HEAD_SHA,
                outcome_path=out_r1,
                step_summary_path=sum_r1,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_r1),
            env=self._runner_env(run_id=9101, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self.gh_state.lock:
            self.gh_state.crash_hook = _CrashHook(
                method="POST",
                path=f"/repos/{REPO_FULL}/issues/51/comments",
                body_contains="reviewsensei:v6:receipt:v1",
                pid=proc_r1.pid,
            )
        rc_r1 = proc_r1.wait(timeout=30)
        stdout_r1 = proc_r1.stdout.read() if proc_r1.stdout else ""
        stderr_r1 = proc_r1.stderr.read() if proc_r1.stderr else ""
        if proc_r1.stdout:
            proc_r1.stdout.close()
        if proc_r1.stderr:
            proc_r1.stderr.close()
        self.assertEqual(rc_r1, -signal.SIGKILL, stderr_r1)
        self.assertTrue(self.gh_state.crash_hook.triggered)

        with provider_state.lock:
            self.assertEqual(provider_state.calls, 1)
        with self.gh_state.lock:
            reviews_r1 = list(self.gh_state.prs[51]["reviews"])
            comments_r1 = list(self.gh_state.prs[51]["comments"])
            gen1_token = self.gh_state.runner_write_tokens[-1]
        self.assertEqual(len(reviews_r1), 1)
        self.assertIn("ReviewSensei analysis pass #1", reviews_r1[0]["body"])
        records_r1 = [
            rec
            for c in comments_r1
            for rec in extract_v6_record_markers(c["body"], strict=True)
        ]
        self.assertEqual(
            sum(1 for r in records_r1 if isinstance(r, UnresolvedResultRecord)), 1
        )
        self.assertEqual(
            sum(1 for r in records_r1 if isinstance(r, WriteIntentRecord)), 1
        )
        self.assertEqual(
            sum(1 for r in records_r1 if isinstance(r, CompletionReceiptRecord)), 0
        )

        # Run 2: cold successor, NO --operation-id, zero artifacts, must adopt review #1 with 0 model calls.
        self.gh_state.set_run_attempt(
            "9101", 1, status="completed", conclusion="failure"
        )
        self.gh_state.set_run_attempt("9102", 2, status="in_progress", conclusion=None)
        out_r2 = self.tmp_path / "outcome-51-r2.json"
        sum_r2 = self.tmp_path / "summary-51-r2.md"
        cwd_r2 = self.tmp_path / "cwd-51-r2"
        cwd_r2.mkdir()
        res_r2 = subprocess.run(
            self._runner_argv(
                pr_number=51,
                operation_id=None,
                run_id=9102,
                run_attempt=2,
                head_sha=HEAD_SHA,
                outcome_path=out_r2,
                step_summary_path=sum_r2,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_r2),
            env=self._runner_env(run_id=9102, run_attempt=2, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_r2.returncode, 0, res_r2.stderr + res_r2.stdout)
        parsed_r2 = json.loads(out_r2.read_text(encoding="utf-8"))
        self.assertEqual(parsed_r2["status"], "completed")
        self.assertEqual(parsed_r2["coordinator_state"], "IDLE")
        self.assertTrue(parsed_r2["lease_released"])
        self.assertEqual(parsed_r2["model_calls_started"], 0)
        self.assertTrue(parsed_r2["reconciled_from_existing_effect"])
        self._assert_coordinator_slot_state(
            pr_number=51, run_id=9102, run_attempt=2, expected_state="IDLE"
        )
        with provider_state.lock:
            self.assertEqual(provider_state.calls, 1)
        self.assertIn(gen1_token, self.gh_state.revoked_tokens)
        self.assertEqual(len(self.gh_state.prs[51]["reviews"]), 1)
        receipts_r2 = [
            rec
            for c in self.gh_state.prs[51]["comments"]
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, CompletionReceiptRecord)
        ]
        self.assertEqual(len(receipts_r2), 1)
        self.assertEqual(receipts_r2[0].operation_id, expected_op_id)
        self._assert_no_secret_leaks(
            self.worker_log,
            stdout_r1,
            stderr_r1,
            out_r1,
            sum_r1,
            res_r2.stdout,
            res_r2.stderr,
            out_r2,
            sum_r2,
        )

    def test_head_change_rebase_after_crash_reconciles_and_publishes_new_head(
        self,
    ) -> None:
        """Test 2: Run 1 crashes on HEAD_A after review #1; PR advances to HEAD_B; Run 2 reconciles HEAD_A and publishes review #2 for HEAD_B to IDLE."""

        provider_state, provider_port = self._start_mock_provider()
        policy_dir = self._write_trusted_policy("trusted-policy-t2", provider_port)
        op_a = f"v6:{REPO_ID}:52:{HEAD_SHA[:12]}:pr_review_publish"
        op_b = f"v6:{REPO_ID}:52:{HEAD_SHA_B[:12]}:pr_review_publish"

        self.gh_state.ensure_pr(52, head_sha=HEAD_SHA)
        self.gh_state.set_run_attempt(
            "9201", 1, status="in_progress", conclusion=None, head_sha=HEAD_SHA
        )
        out_r1 = self.tmp_path / "outcome-52-r1.json"
        sum_r1 = self.tmp_path / "summary-52-r1.md"
        cwd_r1 = self.tmp_path / "cwd-52-r1"
        cwd_r1.mkdir()

        provider_env = {
            "OPENAI_API_KEY": PROVIDER_API_KEY,
            "REVIEWSENSEI_PROVIDER": "openai-compatible",
            "REVIEWSENSEI_MODEL": "gpt-4o-mini",
        }
        proc_r1 = subprocess.Popen(
            self._runner_argv(
                pr_number=52,
                operation_id=None,
                run_id=9201,
                run_attempt=1,
                head_sha=HEAD_SHA,
                outcome_path=out_r1,
                step_summary_path=sum_r1,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_r1),
            env=self._runner_env(run_id=9201, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self.gh_state.lock:
            self.gh_state.crash_hook = _CrashHook(
                method="POST",
                path=f"/repos/{REPO_FULL}/issues/52/comments",
                body_contains="reviewsensei:v6:receipt:v1",
                pid=proc_r1.pid,
            )
        rc_r1 = proc_r1.wait(timeout=30)
        stdout_r1 = proc_r1.stdout.read() if proc_r1.stdout else ""
        stderr_r1 = proc_r1.stderr.read() if proc_r1.stderr else ""
        if proc_r1.stdout:
            proc_r1.stdout.close()
        if proc_r1.stderr:
            proc_r1.stderr.close()
        self.assertEqual(rc_r1, -signal.SIGKILL, stderr_r1)
        self.assertTrue(self.gh_state.crash_hook.triggered)

        # Mark Run 1 failed and advance PR head to HEAD_SHA_B (rebase / new push).
        self.gh_state.set_run_attempt(
            "9201", 1, status="completed", conclusion="failure", head_sha=HEAD_SHA
        )
        with self.gh_state.lock:
            self.gh_state.prs[52]["head_sha"] = HEAD_SHA_B
            head_b_blob = hashlib.sha256(
                f"head:{HEAD_SHA_B}".encode("utf-8")
            ).hexdigest()[:40]
            self.gh_state.blobs[head_b_blob] = (
                b"def pick(items, idx):\n    # validate index\n    return items[idx]\n"
            )
            self.gh_state.trees[(HEAD_SHA_B, True)] = {
                "sha": HEAD_SHA_B,
                "truncated": False,
                "tree": [
                    {
                        "path": "src/app.py",
                        "type": "blob",
                        "sha": head_b_blob,
                        "mode": "100644",
                    }
                ],
            }
            self.gh_state.commits[HEAD_SHA_B] = [BASE_SHA]
        self.gh_state.set_run_attempt(
            "9202", 1, status="in_progress", conclusion=None, head_sha=HEAD_SHA_B
        )
        out_r2 = self.tmp_path / "outcome-52-r2.json"
        sum_r2 = self.tmp_path / "summary-52-r2.md"
        cwd_r2 = self.tmp_path / "cwd-52-r2"
        cwd_r2.mkdir()
        res_r2 = subprocess.run(
            self._runner_argv(
                pr_number=52,
                operation_id=None,
                run_id=9202,
                run_attempt=1,
                head_sha=HEAD_SHA_B,
                outcome_path=out_r2,
                step_summary_path=sum_r2,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_r2),
            env=self._runner_env(run_id=9202, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_r2.returncode, 0, res_r2.stderr + res_r2.stdout)
        parsed_r2 = json.loads(out_r2.read_text(encoding="utf-8"))
        self.assertEqual(parsed_r2["status"], "completed")
        self.assertEqual(parsed_r2["coordinator_state"], "IDLE")
        self.assertTrue(parsed_r2["lease_released"])
        self.assertEqual(parsed_r2["generation"], 2)
        self.assertEqual(parsed_r2["model_calls_started"], 1)
        self._assert_coordinator_slot_state(
            pr_number=52, run_id=9202, expected_state="IDLE"
        )
        with provider_state.lock:
            self.assertEqual(provider_state.calls, 2)

        reviews = list(self.gh_state.prs[52]["reviews"])
        self.assertEqual(len(reviews), 2)
        self.assertEqual([r["commit_id"] for r in reviews], [HEAD_SHA, HEAD_SHA_B])
        receipts = [
            rec
            for c in self.gh_state.prs[52]["comments"]
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, CompletionReceiptRecord)
        ]
        self.assertEqual({r.operation_id for r in receipts}, {op_a, op_b})
        self._assert_no_secret_leaks(
            self.worker_log,
            stdout_r1,
            stderr_r1,
            out_r1,
            sum_r1,
            res_r2.stdout,
            res_r2.stderr,
            out_r2,
            sum_r2,
        )

    def test_same_run_lost_reply_after_fresh_analysis_reconciles_cleanly(self) -> None:
        """Test 3: Fresh PR analysis + GitHub accepts review #1 and drops HTTP reply -> same Run 1 reconciles to IDLE."""

        provider_state, provider_port = self._start_mock_provider()
        policy_dir = self._write_trusted_policy("trusted-policy-t3", provider_port)

        self.gh_state.ensure_pr(53, head_sha=HEAD_SHA)
        self.gh_state.set_run_attempt("9301", 1, status="in_progress", conclusion=None)
        with self.gh_state.lock:
            self.gh_state.drop_reply_hook = _DropReplyHook(
                method="POST",
                path=f"/repos/{REPO_FULL}/pulls/53/reviews",
            )

        out_r1 = self.tmp_path / "outcome-53-r1.json"
        sum_r1 = self.tmp_path / "summary-53-r1.md"
        cwd_r1 = self.tmp_path / "cwd-53-r1"
        cwd_r1.mkdir()
        provider_env = {
            "OPENAI_API_KEY": PROVIDER_API_KEY,
            "REVIEWSENSEI_PROVIDER": "openai-compatible",
            "REVIEWSENSEI_MODEL": "gpt-4o-mini",
        }
        res_r1 = subprocess.run(
            self._runner_argv(
                pr_number=53,
                operation_id=None,
                run_id=9301,
                run_attempt=1,
                head_sha=HEAD_SHA,
                outcome_path=out_r1,
                step_summary_path=sum_r1,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_r1),
            env=self._runner_env(run_id=9301, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_r1.returncode, 0, res_r1.stderr + res_r1.stdout)
        self.assertIsNotNone(self.gh_state.drop_reply_hook)
        assert self.gh_state.drop_reply_hook is not None
        self.assertTrue(self.gh_state.drop_reply_hook.triggered)

        parsed = json.loads(out_r1.read_text(encoding="utf-8"))
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["coordinator_state"], "IDLE")
        self.assertTrue(parsed["lease_released"])
        self.assertEqual(parsed["model_calls_started"], 1)
        self.assertTrue(parsed["reconciled_from_existing_effect"])
        self.assertTrue(parsed["reconciled_after_lost_write"])
        self._assert_coordinator_slot_state(
            pr_number=53, run_id=9301, expected_state="IDLE"
        )
        with provider_state.lock:
            self.assertEqual(provider_state.calls, 1)
        self.assertEqual(len(self.gh_state.prs[53]["reviews"]), 1)
        receipts = [
            rec
            for c in self.gh_state.prs[53]["comments"]
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, CompletionReceiptRecord)
        ]
        self.assertEqual(len(receipts), 1)
        self._assert_no_secret_leaks(
            self.worker_log,
            res_r1.stdout,
            res_r1.stderr,
            out_r1,
            sum_r1,
        )

    def test_large_pr_tree_split_partitioned_result_recovery_and_orphan_hold(
        self,
    ) -> None:
        """Test 4: >512 KiB PR splits compare -> trees -> blobs -> multi-batch -> multi-part ledger + cold recovery + orphan HOLD."""

        provider_state, provider_port = self._start_mock_provider()
        provider_state.comment_path = "src/big.py"
        provider_state.large_envelope_comments = 8
        policy_dir = self._write_trusted_policy("trusted-policy-t4", provider_port)
        policy_sha = hashlib.sha256(
            (policy_dir / ".reviewsensei.yml").read_bytes()
        ).hexdigest()
        self.assertRegex(policy_sha, r"^[0-9a-f]{64}$")

        base_sha = "1" * 40
        merge_base_sha = "2" * 40
        head_t4 = "e" * 40
        dir_base = "3" * 40
        dir_head = "4" * 40
        blob_big_base = "5" * 40
        blob_big_head = "6" * 40
        blob_bin = "7" * 40
        blob_ren = "8" * 40
        big_base_text = "\n".join(f"row_{i} = {i}" for i in range(6000))
        big_head_text = "\n".join(f"row_{i} = {i + 50000}" for i in range(6000))
        with self.gh_state.lock:
            self.gh_state.blobs[blob_big_base] = big_base_text.encode("utf-8")
            self.gh_state.blobs[blob_big_head] = big_head_text.encode("utf-8")
            self.gh_state.blobs[blob_bin] = b"\x00\x01binary-data\xff"
            self.gh_state.blobs[blob_ren] = b"renamed-unchanged\n"
            # ?recursive=1 returns truncated: true -> recursive directory walk
            self.gh_state.trees[(merge_base_sha, True)] = {
                "sha": merge_base_sha,
                "truncated": True,
                "tree": [],
            }
            self.gh_state.trees[(merge_base_sha, False)] = {
                "sha": merge_base_sha,
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree", "sha": dir_base, "mode": "040000"},
                    {
                        "path": "old.txt",
                        "type": "blob",
                        "sha": blob_ren,
                        "mode": "100644",
                    },
                ],
            }
            self.gh_state.trees[(dir_base, False)] = {
                "sha": dir_base,
                "truncated": False,
                "tree": [
                    {
                        "path": "big.py",
                        "type": "blob",
                        "sha": blob_big_base,
                        "mode": "100644",
                    },
                ],
            }
            self.gh_state.trees[(head_t4, True)] = {
                "sha": head_t4,
                "truncated": True,
                "tree": [],
            }
            self.gh_state.trees[(head_t4, False)] = {
                "sha": head_t4,
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree", "sha": dir_head, "mode": "040000"},
                    {
                        "path": "new.txt",
                        "type": "blob",
                        "sha": blob_ren,
                        "mode": "100644",
                    },
                    {
                        "path": "bin.dat",
                        "type": "blob",
                        "sha": blob_bin,
                        "mode": "100644",
                    },
                ],
            }
            self.gh_state.trees[(dir_head, False)] = {
                "sha": dir_head,
                "truncated": False,
                "tree": [
                    {
                        "path": "big.py",
                        "type": "blob",
                        "sha": blob_big_head,
                        "mode": "100644",
                    },
                ],
            }

        pr54 = self.gh_state.ensure_pr(54, head_sha=head_t4, base_sha=base_sha)
        pr54["merge_base_sha"] = merge_base_sha
        pr54["compare_json_oversized"] = True
        pr54["diff"] = "x" * (530 * 1024)
        self.gh_state.set_run_attempt(
            "9401", 1, status="in_progress", conclusion=None, head_sha=head_t4
        )

        provider_env = {
            "OPENAI_API_KEY": PROVIDER_API_KEY,
            "REVIEWSENSEI_PROVIDER": "openai-compatible",
            "REVIEWSENSEI_MODEL": "gpt-4o-mini",
        }
        out_r1 = self.tmp_path / "outcome-54-r1.json"
        sum_r1 = self.tmp_path / "summary-54-r1.md"
        cwd_r1 = self.tmp_path / "cwd-54-r1"
        cwd_r1.mkdir()
        proc_r1 = subprocess.Popen(
            self._runner_argv(
                pr_number=54,
                operation_id=None,
                run_id=9401,
                run_attempt=1,
                head_sha=head_t4,
                outcome_path=out_r1,
                step_summary_path=sum_r1,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_r1),
            env=self._runner_env(run_id=9401, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Crash right on intent:v1 AFTER all ResultPartRecord + ResultManifestRecord comments landed.
        with self.gh_state.lock:
            self.gh_state.crash_hook = _CrashHook(
                method="POST",
                path=f"/repos/{REPO_FULL}/issues/54/comments",
                body_contains="reviewsensei:v6:intent:v1",
                pid=proc_r1.pid,
            )
        rc_r1 = proc_r1.wait(timeout=60)
        stdout_r1 = proc_r1.stdout.read() if proc_r1.stdout else ""
        stderr_r1 = proc_r1.stderr.read() if proc_r1.stderr else ""
        if proc_r1.stdout:
            proc_r1.stdout.close()
        if proc_r1.stderr:
            proc_r1.stderr.close()
        self.assertEqual(rc_r1, -signal.SIGKILL, stderr_r1)
        with provider_state.lock:
            calls_after_r1 = provider_state.calls
        self.assertGreaterEqual(calls_after_r1, 2)

        recs_54 = [
            rec
            for c in self.gh_state.prs[54]["comments"]
            for rec in extract_v6_record_markers(c["body"], strict=True)
        ]
        parts_54 = [r for r in recs_54 if isinstance(r, ResultPartRecord)]
        manifests_54 = [r for r in recs_54 if isinstance(r, ResultManifestRecord)]
        self.assertGreaterEqual(len(parts_54), 2)
        self.assertEqual(len(manifests_54), 1)
        self.assertEqual(len(self.gh_state.prs[54]["reviews"]), 0)

        # Run 2: cold successor recovers partitioned manifest+parts with ZERO new model calls and publishes review!
        self.gh_state.set_run_attempt(
            "9401", 1, status="completed", conclusion="failure", head_sha=head_t4
        )
        self.gh_state.set_run_attempt(
            "9402", 1, status="in_progress", conclusion=None, head_sha=head_t4
        )
        out_r2 = self.tmp_path / "outcome-54-r2.json"
        sum_r2 = self.tmp_path / "summary-54-r2.md"
        cwd_r2 = self.tmp_path / "cwd-54-r2"
        cwd_r2.mkdir()
        res_r2 = subprocess.run(
            self._runner_argv(
                pr_number=54,
                operation_id=None,
                run_id=9402,
                run_attempt=1,
                head_sha=head_t4,
                outcome_path=out_r2,
                step_summary_path=sum_r2,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_r2),
            env=self._runner_env(run_id=9402, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_r2.returncode, 0, res_r2.stderr + res_r2.stdout)
        parsed_r2 = json.loads(out_r2.read_text(encoding="utf-8"))
        self.assertEqual(parsed_r2["status"], "completed")
        self.assertEqual(parsed_r2["coordinator_state"], "IDLE")
        self.assertEqual(parsed_r2["model_calls_started"], 0)
        self._assert_coordinator_slot_state(
            pr_number=54, run_id=9402, expected_state="IDLE"
        )
        with provider_state.lock:
            self.assertEqual(provider_state.calls, calls_after_r1)
        self.assertEqual(len(self.gh_state.prs[54]["reviews"]), 1)

        # Sub-test: Orphan result-part:v1 without result-manifest:v1 fails closed to HOLD (0 model calls)
        pr55 = self.gh_state.ensure_pr(55, head_sha=head_t4, base_sha=base_sha)
        pr55["merge_base_sha"] = merge_base_sha
        for c in self.gh_state.prs[54]["comments"]:
            recs = extract_v6_record_markers(c["body"], strict=True)
            final_parts = [
                r
                for r in recs
                if isinstance(r, ResultPartRecord)
                and r.record_id.startswith("result-part:")
            ]
            if final_parts:
                # Re-point operation_id for PR 55's operation_id so orphan part matches PR 55
                part = final_parts[0]
                orphan_part = ResultPartRecord(
                    record_id=part.record_id.replace(":54:", ":55:"),
                    operation_id=f"v6:{REPO_ID}:55:{head_t4[:12]}:pr_review_publish",
                    pr_head_sha=part.pr_head_sha,
                    payload_sha256=part.payload_sha256,
                    part_index=part.part_index,
                    part_count=part.part_count,
                    part_sha256=part.part_sha256,
                    part_payload=part.part_payload,
                )
                self.gh_state.add_app_comment(55, format_v6_record_marker(orphan_part))
                break
        self.gh_state.set_run_attempt(
            "9501", 1, status="in_progress", conclusion=None, head_sha=head_t4
        )
        out_55 = self.tmp_path / "outcome-55.json"
        sum_55 = self.tmp_path / "summary-55.md"
        cwd_55 = self.tmp_path / "cwd-55"
        cwd_55.mkdir()
        res_55 = subprocess.run(
            self._runner_argv(
                pr_number=55,
                operation_id=None,
                run_id=9501,
                run_attempt=1,
                head_sha=head_t4,
                outcome_path=out_55,
                step_summary_path=sum_55,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_55),
            env=self._runner_env(run_id=9501, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_55.returncode, 1)
        parsed_55 = json.loads(out_55.read_text(encoding="utf-8"))
        self.assertEqual(parsed_55["status"], "hold")
        self.assertEqual(
            parsed_55["diagnostic"], "incomplete_partitioned_result_missing_manifest"
        )
        self.assertEqual(parsed_55["model_calls_started"], 0)
        with provider_state.lock:
            self.assertEqual(provider_state.calls, calls_after_r1)

        # Sub-test: When Compare page=1 >512 KiB AND page=2 returns 500, git_commit LCA resolves diverged merge_base_sha != base_sha.
        head_59 = "9a" * 20
        mid_commit = "c" * 40
        older_commit = "f" * 40
        with self.gh_state.lock:
            self.gh_state.commits[base_sha] = [merge_base_sha]
            self.gh_state.commits[head_59] = [mid_commit]
            self.gh_state.commits[mid_commit] = [merge_base_sha]
            self.gh_state.commits[merge_base_sha] = [older_commit]
            self.gh_state.commits[older_commit] = []
            self.gh_state.trees[(head_59, True)] = {
                "sha": head_59,
                "truncated": True,
                "tree": [],
            }
            self.gh_state.trees[(head_59, False)] = self.gh_state.trees[
                (head_t4, False)
            ]
        pr59 = self.gh_state.ensure_pr(59, head_sha=head_59, base_sha=base_sha)
        pr59["merge_base_sha"] = merge_base_sha
        pr59["compare_json_oversized"] = True
        pr59["compare_page2_status"] = 500
        pr59["diff"] = "x" * (530 * 1024)
        self.gh_state.set_run_attempt(
            "9502", 1, status="in_progress", conclusion=None, head_sha=head_59
        )
        out_59 = self.tmp_path / "outcome-59.json"
        sum_59 = self.tmp_path / "summary-59.md"
        cwd_59 = self.tmp_path / "cwd-59"
        cwd_59.mkdir()
        res_59 = subprocess.run(
            self._runner_argv(
                pr_number=59,
                operation_id=None,
                run_id=9502,
                run_attempt=1,
                head_sha=head_59,
                outcome_path=out_59,
                step_summary_path=sum_59,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_59),
            env=self._runner_env(run_id=9502, run_attempt=1, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res_59.returncode, 0, res_59.stderr + res_59.stdout)
        parsed_59 = json.loads(out_59.read_text(encoding="utf-8"))
        self.assertEqual(parsed_59["status"], "completed")
        self.assertEqual(parsed_59["coordinator_state"], "IDLE")
        self._assert_coordinator_slot_state(
            pr_number=59, run_id=9502, expected_state="IDLE"
        )
        with self.gh_state.lock:
            self.assertIn(
                ("GET", f"/repos/{REPO_FULL}/git/commits/{head_59}"),
                self.gh_state.requests_log,
            )

        self._assert_no_secret_leaks(
            self.worker_log,
            stdout_r1,
            stderr_r1,
            out_r1,
            sum_r1,
            res_r2.stdout,
            res_r2.stderr,
            out_r2,
            sum_r2,
            res_55.stdout,
            res_55.stderr,
            out_55,
            sum_55,
            res_59.stdout,
            res_59.stderr,
            out_59,
            sum_59,
        )

    def test_trusted_policy_disabled_skips_before_coordinator(self) -> None:
        """Test 5: trusted policy github.writes=false / automatic_reviews=false skips cleanly with exit 0 and 0 coordinator calls."""

        provider_state, provider_port = self._start_mock_provider()
        policy_writes_off = self._write_trusted_policy(
            "policy-writes-off", provider_port, writes=False, automatic_reviews=True
        )
        policy_auto_off = self._write_trusted_policy(
            "policy-auto-off", provider_port, writes=True, automatic_reviews=False
        )
        self.gh_state.ensure_pr(56, head_sha=HEAD_SHA)
        with self.gh_state.lock:
            req_before = len(self.gh_state.requests_log)
            mint_before = self.gh_state.runner_mint_count

        for tag, pol_dir, expected_diag in (
            ("w", policy_writes_off, "v6_policy_writes_disabled"),
            ("a", policy_auto_off, "v6_policy_automatic_reviews_disabled"),
        ):
            out_p = self.tmp_path / f"outcome-56-{tag}.json"
            sum_p = self.tmp_path / f"summary-56-{tag}.md"
            cwd_p = self.tmp_path / f"cwd-56-{tag}"
            cwd_p.mkdir()
            res = subprocess.run(
                self._runner_argv(
                    pr_number=56,
                    operation_id=None,
                    run_id=9601,
                    head_sha=HEAD_SHA,
                    outcome_path=out_p,
                    step_summary_path=sum_p,
                    analyze_fresh_pr=True,
                    policy_root=pol_dir,
                ),
                cwd=str(cwd_p),
                env=self._runner_env(run_id=9601),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            parsed = json.loads(out_p.read_text(encoding="utf-8"))
            self.assertEqual(parsed["status"], "skipped")
            self.assertEqual(parsed["diagnostic"], expected_diag)
            self.assertEqual(parsed["model_calls_started"], 0)

        with provider_state.lock:
            self.assertEqual(provider_state.calls, 0)
        with self.gh_state.lock:
            self.assertEqual(len(self.gh_state.requests_log), req_before)
            self.assertEqual(self.gh_state.runner_mint_count, mint_before)

    def test_mid_run_head_movement_blocks_publish_and_persists_non_submission(
        self,
    ) -> None:
        """Test 6: PR head_sha moves right after intent:v1 -> live recheck blocks stale review and persists pre_dispatch_terminal_abort."""

        provider_state, provider_port = self._start_mock_provider()
        policy_dir = self._write_trusted_policy("trusted-policy-t6", provider_port)
        self.gh_state.ensure_pr(57, head_sha=HEAD_SHA)
        self.gh_state.prs[57]["analysis_head_sha"] = HEAD_SHA
        self.gh_state.set_run_attempt("9701", 1, status="in_progress", conclusion=None)
        with self.gh_state.lock:
            self.gh_state.head_advance_after_comment_hook = (
                57,
                "reviewsensei:v6:intent:v1",
                HEAD_SHA_B,
            )

        out_p = self.tmp_path / "outcome-57.json"
        sum_p = self.tmp_path / "summary-57.md"
        cwd_p = self.tmp_path / "cwd-57"
        cwd_p.mkdir()
        provider_env = {
            "OPENAI_API_KEY": PROVIDER_API_KEY,
            "REVIEWSENSEI_PROVIDER": "openai-compatible",
            "REVIEWSENSEI_MODEL": "gpt-4o-mini",
        }
        res = subprocess.run(
            self._runner_argv(
                pr_number=57,
                operation_id=None,
                run_id=9701,
                head_sha=HEAD_SHA,
                outcome_path=out_p,
                step_summary_path=sum_p,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_p),
            env=self._runner_env(run_id=9701, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        parsed = json.loads(out_p.read_text(encoding="utf-8"))
        self.assertEqual(parsed["status"], "hold")
        self.assertEqual(parsed["diagnostic"], "v6_pr_head_mismatch")
        self.assertEqual(len(self.gh_state.prs[57]["reviews"]), 0)
        self._assert_no_secret_leaks(
            self.worker_log, res.stdout, res.stderr, out_p, sum_p
        )

    def test_adaptive_pagination_and_413_size_refused(self) -> None:
        """Test 7: >512 KiB comment page triggers adaptive [100,12,5,1] step-down + 413 POST persists github_native_review_size_refused."""

        provider_state, provider_port = self._start_mock_provider()
        policy_dir = self._write_trusted_policy("trusted-policy-t7", provider_port)
        self.gh_state.ensure_pr(58, head_sha=HEAD_SHA)
        # Seed 15 unrelated noise comments where per_page=100 exceeds 512 KiB so Python scan_rest steps down 100 -> 12 -> 5.
        for idx in range(15):
            self.gh_state.add_app_comment(58, f"noise-comment-{idx} " + ("n" * 40000))
        with self.gh_state.lock:
            self.gh_state.reject_review_status[58] = (
                413,
                {"message": "Review payload too large"},
            )
        self.gh_state.set_run_attempt("9801", 1, status="in_progress", conclusion=None)

        out_p = self.tmp_path / "outcome-58.json"
        sum_p = self.tmp_path / "summary-58.md"
        cwd_p = self.tmp_path / "cwd-58"
        cwd_p.mkdir()
        provider_env = {
            "OPENAI_API_KEY": PROVIDER_API_KEY,
            "REVIEWSENSEI_PROVIDER": "openai-compatible",
            "REVIEWSENSEI_MODEL": "gpt-4o-mini",
        }
        res = subprocess.run(
            self._runner_argv(
                pr_number=58,
                operation_id=None,
                run_id=9801,
                head_sha=HEAD_SHA,
                outcome_path=out_p,
                step_summary_path=sum_p,
                analyze_fresh_pr=True,
                policy_root=policy_dir,
            ),
            cwd=str(cwd_p),
            env=self._runner_env(run_id=9801, extra_env=provider_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        parsed = json.loads(out_p.read_text(encoding="utf-8"))
        self.assertEqual(parsed["status"], "hold")
        self.assertEqual(parsed["diagnostic"], "github_native_review_size_refused")
        proofs = [
            rec
            for c in self.gh_state.prs[58]["comments"]
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, NonSubmissionProofRecord)
        ]
        self.assertEqual(len(proofs), 1)
        self.assertEqual(proofs[0].diagnostic, "github_native_review_size_refused")
        self.assertEqual(len(self.gh_state.prs[58]["reviews"]), 0)
        self._assert_no_secret_leaks(
            self.worker_log, res.stdout, res.stderr, out_p, sum_p
        )

    def test_mismatched_workflow_identity_and_provenance_rejected(self) -> None:
        """Test 8: Mismatched workflow_sha / job_workflow_ref returns 403 (0 tokens) and provenance mismatches fail closed."""

        mismatched_sha = "0" * 40 if self.workflow_sha != "0" * 40 else "1" * 40
        mismatched_pilot_ref = _workflow_ref_for_sha(mismatched_sha)
        untrusted_ref = (
            f"{REPO_FULL}/.github/workflows/untrusted.yml@{self.workflow_sha}"
        )
        self.gh_state.ensure_pr(60, head_sha=HEAD_SHA)
        self.gh_state.set_run_attempt("9901", 1, status="in_progress", conclusion=None)
        self.gh_state.set_run_attempt("9902", 1, status="in_progress", conclusion=None)

        with self.gh_state.lock:
            mints_before = self.gh_state.runner_mint_count

        for bad_run_id, oidc_sha, oidc_ref in (
            ("9901", mismatched_sha, mismatched_pilot_ref),
            ("9902", self.workflow_sha, untrusted_ref),
        ):
            bad_oidc = self.gh_state.issue_oidc_jwt(
                run_id=bad_run_id,
                run_attempt=1,
                workflow_sha=oidc_sha,
                job_workflow_ref=oidc_ref,
            )
            status, raw, body = self._post_worker(
                "/github/v6/lease/acquire",
                {
                    "oidc_token": bad_oidc,
                    "pull_request": 60,
                    "head_sha": HEAD_SHA,
                    "capabilities": ["review_publish"],
                },
            )
            self.assertEqual(status, 403, raw)
            self.assertEqual(
                body["diagnostic"]["code"], "v6_lease_unauthorized_tuple", raw
            )
            self._assert_no_secret_leaks(raw, self.worker_log)

        findings_60 = self.tmp_path / "findings-pr60.json"
        findings_60.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "path": "src/example.py",
                            "line": 5,
                            "body": "Check boundary.",
                        }
                    ],
                    "required_evidence_digests": [],
                }
            ),
            encoding="utf-8",
        )
        for idx, (run_id, runner_sha, runner_ref) in enumerate(
            (
                (9901, mismatched_sha, mismatched_pilot_ref),
                (9902, self.workflow_sha, untrusted_ref),
            ),
            start=1,
        ):
            out_60 = self.tmp_path / f"outcome-60-{idx}.json"
            sum_60 = self.tmp_path / f"summary-60-{idx}.md"
            cwd_60 = self.tmp_path / f"cwd-60-{idx}"
            cwd_60.mkdir()
            res = subprocess.run(
                self._runner_argv(
                    pr_number=60,
                    operation_id=f"op-mismatch-{idx}",
                    run_id=run_id,
                    workflow_sha=runner_sha,
                    outcome_path=out_60,
                    step_summary_path=sum_60,
                    initial_findings_path=findings_60,
                ),
                cwd=str(cwd_60),
                env=self._runner_env(
                    run_id=run_id,
                    workflow_sha=runner_sha,
                    job_workflow_ref=runner_ref,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(res.returncode, 1, res.stderr + res.stdout)
            parsed = json.loads(out_60.read_text(encoding="utf-8"))
            self.assertEqual(parsed["status"], "hold", parsed)
            self.assertEqual(
                parsed["diagnostic"], "v6_lease_unauthorized_tuple", parsed
            )
            self.assertEqual(parsed["model_calls_started"], 0)
            self._assert_no_secret_leaks(
                self.worker_log, res.stdout, res.stderr, out_60, sum_60
            )

        with self.gh_state.lock:
            self.assertEqual(self.gh_state.runner_mint_count, mints_before)
            self.assertEqual(len(self.gh_state.prs[60]["reviews"]), 0)

        # Provenance verification must fail closed on commit/wheel digest mismatches in stdout or $GITHUB_STEP_SUMMARY.
        wheel_dir = Path(self.provenance["wheel_dir"])
        install_stdout = str(self.provenance["install_stdout"])
        install_summary_text = Path(
            self.provenance["install_step_summary_path"]
        ).read_text(encoding="utf-8")
        first_whl_name, first_whl_digest = next(
            iter(sorted(self.provenance["wheel_sha256s"].items()))
        )
        tampered_digest = ("0" * 64) if first_whl_digest != ("0" * 64) else ("1" * 64)

        prov_tmp = self.tmp_path / "provenance-negatives"
        prov_tmp.mkdir()
        valid_summary = prov_tmp / "valid-summary.md"
        valid_summary.write_text(install_summary_text, encoding="utf-8")

        bad_stdout_commit = install_stdout.replace(
            f"review_sensei_v6_workflow_commit={self.workflow_sha}",
            f"review_sensei_v6_workflow_commit={mismatched_sha}",
        )
        bad_summary_commit = prov_tmp / "bad-commit-summary.md"
        bad_summary_commit.write_text(
            install_summary_text.replace(
                f"review_sensei_v6_workflow_commit={self.workflow_sha}",
                f"review_sensei_v6_workflow_commit={mismatched_sha}",
            ),
            encoding="utf-8",
        )
        bad_stdout_digest = install_stdout.replace(
            f"review_sensei_v6_wheel_sha256 {first_whl_name} {first_whl_digest}",
            f"review_sensei_v6_wheel_sha256 {first_whl_name} {tampered_digest}",
        )
        bad_summary_digest = prov_tmp / "bad-digest-summary.md"
        bad_summary_digest.write_text(
            install_summary_text.replace(
                f"review_sensei_v6_wheel_sha256 {first_whl_name} {first_whl_digest}",
                f"review_sensei_v6_wheel_sha256 {first_whl_name} {tampered_digest}",
            ),
            encoding="utf-8",
        )
        missing_summary = prov_tmp / "does-not-exist.md"

        for label, stdout_candidate, summary_candidate in (
            ("stdout_commit_mismatch", bad_stdout_commit, valid_summary),
            ("summary_commit_mismatch", install_stdout, bad_summary_commit),
            ("stdout_wheel_digest_mismatch", bad_stdout_digest, valid_summary),
            ("summary_wheel_digest_mismatch", install_stdout, bad_summary_digest),
            ("missing_step_summary", install_stdout, missing_summary),
        ):
            with self.subTest(case=label):
                with self.assertRaises(RuntimeError):
                    _verify_install_provenance(
                        expected_commit_sha=self.workflow_sha,
                        wheel_dir=wheel_dir,
                        stdout_text=stdout_candidate,
                        step_summary_path=summary_candidate,
                    )


if __name__ == "__main__":
    unittest.main()
