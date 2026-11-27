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
import signal
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
import venv
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from review_sensei_github.v6_recovery_ledger import (
    CompletionReceiptRecord,
    DurableHoldRecord,
    OwnerMarkerRecord,
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
WORKFLOW_SHA = "b" * 40
WORKFLOW_REF = f"malsabbagh/review-sensei/.github/workflows/review-sensei-v6-pilot.yml@{WORKFLOW_SHA}"
SIGNING_KEY = "v6-integrated-acceptance-signing-key-material-32bytes"
WF_READ_TOKEN = "ghp_wf_readonly_acceptance_token_0001"
OIDC_REQUEST_PREFIX = "actions-oidc-req:"


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


def _materialize_installed_dist_wheel(dist_name: str, wheel_dir: Path) -> None:
    """Repack an already-installed distribution into ``wheel_dir`` for offline --no-index install."""

    dist = importlib_metadata.distribution(dist_name)
    files = list(dist.files or ())
    if not files:
        raise RuntimeError(
            f"cannot locate files for installed distribution {dist_name!r}"
        )
    wheel_meta = dist.read_text("WHEEL") or ""
    tag = "py3-none-any"
    for line in wheel_meta.splitlines():
        if line.startswith("Tag:"):
            tag = line.split(":", 1)[1].strip()
            break
    norm_name = re.sub(r"[-_.]+", "_", dist.metadata["Name"])
    wheel_path = wheel_dir / f"{norm_name}-{dist.version}-{tag}.whl"
    if wheel_path.exists():
        return

    dist_info_prefix = f"{norm_name}-{dist.version}.dist-info"
    record_rows: list[str] = []
    seen_arcs: set[str] = set()
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            if "__pycache__" in rel.parts or rel.name == "RECORD" or ".." in rel.parts:
                continue
            src_path = Path(str(dist.locate_file(rel)))
            if not src_path.is_file():
                continue
            arcname = rel.as_posix()
            if arcname.endswith(".dist-info/RECORD"):
                continue
            if arcname in seen_arcs:
                continue
            seen_arcs.add(arcname)
            data = src_path.read_bytes()
            zf.writestr(arcname, data)
            digest = _b64url(hashlib.sha256(data).digest())
            record_rows.append(f"{arcname},sha256={digest},{len(data)}")
            if arcname.endswith(".dist-info/METADATA"):
                dist_info_prefix = arcname.rsplit("/", 1)[0]
        record_arc = f"{dist_info_prefix}/RECORD"
        record_rows.append(f"{record_arc},,")
        zf.writestr(record_arc, "\n".join(record_rows) + "\n")


def _build_offline_venv_and_install(temp_root: Path) -> tuple[Path, Path]:
    """Build wheels from this checkout and install into an isolated clean venv."""

    wheelhouse = temp_root / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
            str(ROOT),
            str(ROOT / "packages" / "python" / "review-sensei-github"),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for dep_name in (
        "cryptography",
        "cffi",
        "pycparser",
        "certifi",
        "jsonschema",
        "jsonschema-specifications",
        "referencing",
        "rpds-py",
        "attrs",
    ):
        _materialize_installed_dist_wheel(dep_name, wheelhouse)

    venv_dir = temp_root / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    venv_python = venv_dir / "bin" / "python"
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "review-sensei",
            "review-sensei-github",
        ],
        check=True,
        cwd=str(temp_root),
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        [str(venv_python), "-m", "pip", "check"],
        check=True,
        cwd=str(temp_root),
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return venv_dir, venv_dir / "bin" / "review-sensei-github"


@dataclass
class _CrashHook:
    method: str
    path: str
    pid: int
    body_contains: str | None = None
    kill_before_write: bool = True
    triggered: bool = False


@dataclass
class _SimulatedGitHubState:
    oidc_key: rsa.RSAPrivateKey
    app_key: rsa.RSAPrivateKey
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

    def ensure_pr(self, pr_number: int) -> dict[str, Any]:
        with self.lock:
            pr = self.prs.get(pr_number)
            if pr is None:
                pr = {
                    "number": pr_number,
                    "head_sha": HEAD_SHA,
                    "state": "open",
                    "comments": [],
                    "reviews": [],
                }
                self.prs[pr_number] = pr
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
        workflow_sha: str = WORKFLOW_SHA,
        job_workflow_ref: str = WORKFLOW_REF,
        audience: str = "sts.reviewsensei.dev",
    ) -> str:
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
            "workflow_ref": job_workflow_ref,
            "workflow_sha": workflow_sha,
            "job_workflow_ref": job_workflow_ref,
            "job_workflow_sha": workflow_sha,
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

        def _send_json(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
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
                    workflow_sha=parts.get("workflow_sha", WORKFLOW_SHA),
                    job_workflow_ref=parts.get("job_workflow_ref", WORKFLOW_REF),
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
                self._send_json(
                    200,
                    {
                        "id": pr_num,
                        "number": pr_num,
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
                            "repo": {
                                "id": REPO_ID,
                                "full_name": REPO_FULL,
                                "fork": False,
                            }
                        },
                    },
                )
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
                self._send_json(200, items[start : start + per_page])
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
                with state.lock:
                    items = list(pr["reviews"])
                self._send_json(200, items)
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
                self._send_json(201, created)
                return

            reviews_match = re.fullmatch(
                rf"/repos/{re.escape(REPO_FULL)}/pulls/(\d+)/reviews", path
            )
            if reviews_match:
                if self._maybe_trigger_crash("POST", path, body_str):
                    return
                pr_num = int(reviews_match.group(1))
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


class V6OneWriterWorkerQualificationTests(unittest.TestCase):
    """End-to-end acceptance checks for ReviewSensei v6 coordinator + wheel runner."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="v6-worker-qual-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.ca_pem, self.cert_pem, self.key_pem = _generate_tls_materials(
            self.tmp_path
        )
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ssl_ctx.load_verify_locations(cafile=str(self.ca_pem))

        self.oidc_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.app_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.app_key_pem = self.app_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

        self.gh_state = _SimulatedGitHubState(
            oidc_key=self.oidc_key, app_key=self.app_key
        )
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
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
                        "V6_PILOT_WORKFLOW_REF": WORKFLOW_REF,
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

        self.venv_dir, self.cli_bin = _build_offline_venv_and_install(self.tmp_path)
        self.venv_python = self.venv_dir / "bin" / "python"

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
        workflow_sha: str = WORKFLOW_SHA,
    ) -> dict[str, str]:
        env = {
            "PATH": f"{self.venv_dir / 'bin'}:{os.environ.get('PATH', '')}",
            "SSL_CERT_FILE": str(self.ca_pem),
            "GITHUB_TOKEN": WF_READ_TOKEN,
            "GITHUB_API_URL": f"https://127.0.0.1:{self.gh_port}",
            "ACTIONS_ID_TOKEN_REQUEST_URL": f"https://127.0.0.1:{self.gh_port}/oidc/token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": (
                f"{OIDC_REQUEST_PREFIX}run_id={run_id},run_attempt={run_attempt},"
                f"repository={repository},repository_id={repository_id},"
                f"workflow_sha={workflow_sha}"
            ),
            "GITHUB_RUN_ID": str(run_id),
            "GITHUB_RUN_ATTEMPT": str(run_attempt),
            "REVIEW_SENSEI_WORKFLOW_SHA": workflow_sha,
        }
        return env

    def _runner_argv(
        self,
        *,
        pr_number: int,
        operation_id: str,
        run_id: int,
        outcome_path: Path,
        step_summary_path: Path,
        initial_findings_path: Path | None = None,
    ) -> list[str]:
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
            HEAD_SHA,
            "--operation-id",
            operation_id,
            "--coordinator-url",
            self.worker_url,
            "--github-api-url",
            f"https://127.0.0.1:{self.gh_port}",
            "--run-id",
            str(run_id),
            "--run-attempt",
            "1",
            "--workflow-sha",
            WORKFLOW_SHA,
            "--expected-bot-login",
            BOT_LOGIN,
            "--outcome",
            str(outcome_path),
            "--step-summary-path",
            str(step_summary_path),
        ]
        if initial_findings_path is not None:
            argv.extend(["--initial-findings-json", str(initial_findings_path)])
        return argv

    def _assert_no_secret_leaks(self, *paths_or_texts: Path | str) -> None:
        forbidden = [
            WF_READ_TOKEN,
            SIGNING_KEY,
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

    def test_integrated_worker_and_wheel_runner_acceptance_scenarios(self) -> None:
        # 0. Verify installed packages inside venv resolve inside venv (not repo checkout).
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
        # Verify status route body never echoes tokens.
        status_oidc = self.gh_state.issue_oidc_jwt(run_id="901", run_attempt=1)
        st_code, st_raw, _ = self._post_worker(
            "/github/v6/lease/status",
            {"oidc_token": status_oidc, "pull_request": 41},
        )
        self.assertEqual(st_code, 200)
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
        if proc_42_r1.stdout is not None:
            proc_42_r1.stdout.close()
        if proc_42_r1.stderr is not None:
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
            self.worker_log, res_42_r2.stdout, res_42_r2.stderr, out_42_r2, sum_42_r2
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
        if proc_43_r1.stdout is not None:
            proc_43_r1.stdout.close()
        if proc_43_r1.stderr is not None:
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
            res_43_r2.stdout,
            res_43_r2.stderr,
            out_43_r2,
            sum_43_r2,
            res_43_r3.stdout,
            res_43_r3.stderr,
            out_43_r3,
            sum_43_r3,
        )


if __name__ == "__main__":
    unittest.main()
