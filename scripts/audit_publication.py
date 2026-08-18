#!/usr/bin/env python3
"""Audit an exact source commit before public publication.

The audit resolves an explicit commit SHA, enumerates its exact Git tree,
applies the publication exclusion manifest before reading any blob, rejects
unsafe publishable non-blob entries, scans publishable blobs for high-confidence
credentials, private repository references, private-network endpoints, and
unallowlisted identity metadata, and writes a deterministic machine-readable
report.  The audit fails closed: any blocking finding aborts publication
without echoing the matched value.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_publication_boundary import (
    DEFAULT_EXCLUSIONS,
    PublicationBoundaryError,
    git_blob_bytes,
    git_blob_size,
    git_tree_entries,
    parse_exclusions,
    source_tree_hash_from_blobs,
)

DEFAULT_POLICY = ".publication/audit-policy.json"
REPORT_SCHEMA_VERSION = 1

_PEM_DASHES = b"-" * 5
PRIVATE_KEY_HEADERS = (
    re.compile(
        _PEM_DASHES
        + rb"BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"
        + _PEM_DASHES
    ),
    re.compile(_PEM_DASHES + rb"BEGIN PGP PRIVATE KEY BLOCK" + _PEM_DASHES),
    re.compile(_PEM_DASHES + rb"BEGIN ENCRYPTED PRIVATE KEY" + _PEM_DASHES),
)

_TOKEN_PREFIXES = ("AKIA", "ghp_", "github_pat_", "xox", "sk-", "AIza")
TOKEN_PATTERNS = tuple(
    re.compile(rb"\b" + prefix.encode("ascii") + rb"[A-Za-z0-9_-]{16,}\b")
    for prefix in _TOKEN_PREFIXES
)

_PRIVATE_OWNER = "malsabbagh"
_PRIVATE_REPO = "code-sensei"
PRIVATE_REPO_PATTERNS = (
    re.compile(
        rb"github\.com/"
        + _PRIVATE_OWNER.encode("ascii")
        + rb"/"
        + _PRIVATE_REPO.encode("ascii")
    ),
    re.compile(
        rb"git@github\.com:"
        + _PRIVATE_OWNER.encode("ascii")
        + rb"/"
        + _PRIVATE_REPO.encode("ascii")
    ),
    re.compile(
        rb"\b"
        + _PRIVATE_OWNER.encode("ascii")
        + rb"/"
        + _PRIVATE_REPO.encode("ascii")
        + rb"\b"
    ),
)

PRIVATE_NETWORK_PATTERNS = (
    re.compile(rb"https?://(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}", re.IGNORECASE),
    re.compile(rb"https?://192\.168\.\d{1,3}\.\d{1,3}", re.IGNORECASE),
    re.compile(rb"https?://172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}", re.IGNORECASE),
    re.compile(rb"https?://169\.254\.\d{1,3}\.\d{1,3}", re.IGNORECASE),
    re.compile(
        rb"https?://[a-z0-9-]+\.(?:internal|corp|lan|local|home)(?:\.|/|:|$)",
        re.IGNORECASE,
    ),
)

EMAIL_PATTERN = re.compile(rb"[A-Za-z0-9._%+\[\]-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HANDLE_PATTERN = re.compile(rb"@[A-Za-z0-9][A-Za-z0-9-]{0,38}")
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net"}
PLACEHOLDER_HANDLES = {"example", "yourname", "username", "owner", "user"}
JSON_LD_KEY_HANDLES = {"@context", "@type"}
CSS_AT_RULE_HANDLES = {
    "@charset",
    "@container",
    "@counter-style",
    "@document",
    "@font-face",
    "@import",
    "@keyframes",
    "@layer",
    "@media",
    "@namespace",
    "@page",
    "@property",
    "@scope",
    "@starting-style",
    "@supports",
}
REQUIRED_PROHIBITED_CONTENT_CLASSES = {
    "private-keys",
    "tokens",
    "private-repo-urls",
    "private-network-endpoints",
    "identity-metadata",
}
HANDLE_SCAN_EXTENSIONS = {
    ".md",
    ".txt",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".cfg",
    ".ini",
    ".sh",
    ".bash",
    ".html",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".sql",
    ".conf",
}


class PublicationAuditError(ValueError):
    """Raised when the audit cannot proceed safely."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_policy(data: object, source: str) -> dict[str, Any]:
    """Validate decoded publication policy data fail-closed."""
    try:
        data = dict(data)
    except (TypeError, ValueError) as exc:
        raise PublicationAuditError(
            f"audit policy must be a JSON object: {source}"
        ) from exc
    if not isinstance(data, dict):
        raise PublicationAuditError(f"audit policy must be a JSON object: {source}")
    if data.get("version") != 1:
        raise PublicationAuditError("audit policy version must be 1")
    if data.get("history_strategy") != "clean-root":
        raise PublicationAuditError("audit policy history_strategy must be clean-root")
    identity = data.get("public_identity")
    if not isinstance(identity, dict):
        raise PublicationAuditError("audit policy public_identity must be an object")
    for key in ("emails", "names"):
        value = identity.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise PublicationAuditError(
                f"audit policy public_identity.{key} must be a string list"
            )
    classifications = data.get("path_classifications")
    if not isinstance(classifications, dict):
        raise PublicationAuditError(
            "audit policy path_classifications must be an object"
        )
    for key in ("synthetic", "public"):
        value = classifications.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise PublicationAuditError(
                f"audit policy path_classifications.{key} must be a string list"
            )
    prohibited_paths = data.get("prohibited_path_classes")
    if not isinstance(prohibited_paths, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("class"), str)
        and isinstance(item.get("patterns"), list)
        and all(isinstance(pattern, str) for pattern in item["patterns"])
        for item in prohibited_paths
    ):
        raise PublicationAuditError(
            "audit policy prohibited_path_classes must be a list of class/pattern objects"
        )
    prohibited_content = data.get("prohibited_content_classes")
    if not isinstance(prohibited_content, list) or not all(
        isinstance(item, str) for item in prohibited_content
    ):
        raise PublicationAuditError(
            "audit policy prohibited_content_classes must be a string list"
        )
    prohibited_set = set(prohibited_content)
    if len(prohibited_content) != len(prohibited_set):
        raise PublicationAuditError(
            "audit policy prohibited_content_classes must not contain duplicates"
        )
    missing = sorted(REQUIRED_PROHIBITED_CONTENT_CLASSES - prohibited_set)
    unknown = sorted(prohibited_set - REQUIRED_PROHIBITED_CONTENT_CLASSES)
    if missing or unknown:
        if missing:
            missing_text = f"missing: {', '.join(missing)}"
        else:
            missing_text = "missing: none"
        if unknown:
            unknown_text = f"unknown: {', '.join(unknown)}"
        else:
            unknown_text = "unknown: none"
        raise PublicationAuditError(
            "audit policy prohibited_content_classes must contain exactly required "
            "categories; " + f"{missing_text}; {unknown_text}"
        )
    redacted = data.get("redacted_report")
    if not isinstance(redacted, dict):
        raise PublicationAuditError("audit policy redacted_report must be an object")
    for key in ("include_paths", "include_rule", "include_category", "include_value"):
        if not isinstance(redacted.get(key), bool):
            raise PublicationAuditError(
                f"audit policy redacted_report.{key} must be a boolean"
            )
    allowed = data.get("allowed_endpoints")
    if not isinstance(allowed, list) or not all(
        isinstance(item, str) for item in allowed
    ):
        raise PublicationAuditError(
            "audit policy allowed_endpoints must be a string list"
        )
    return data


def load_policy(path: Path) -> dict[str, Any]:
    """Load and validate the publication audit policy fail-closed."""
    if not path.is_file():
        raise PublicationAuditError(f"audit policy not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublicationAuditError(f"audit policy is not valid JSON: {path}") from exc
    return _parse_policy(data, str(path))


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _classify_path(path: str, policy: dict[str, Any]) -> str:
    classifications = policy["path_classifications"]
    if _matches_any(path, classifications.get("synthetic", [])):
        return "synthetic"
    if _matches_any(path, classifications.get("public", [])):
        return "public"
    return "unclassified"


def _prohibited_path_class(path: str, policy: dict[str, Any]) -> str | None:
    for entry in policy["prohibited_path_classes"]:
        if _matches_any(path, entry["patterns"]):
            return entry["class"]
    return None


def _redact_findings(
    findings: list[dict[str, str]], policy: dict[str, Any]
) -> list[dict[str, str]]:
    redacted = policy["redacted_report"]
    keys: list[str] = []
    if redacted.get("include_paths"):
        keys.append("path")
    if redacted.get("include_rule"):
        keys.append("rule")
    if redacted.get("include_category"):
        keys.append("category")
    if redacted.get("include_value"):
        keys.append("value")
    return [
        {key: finding[key] for key in keys if key in finding} for finding in findings
    ]


def _host_from_match(match: bytes) -> str:
    text = match.decode("ascii", errors="replace")
    return text.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()


def _is_non_identity_handle(
    path: str,
    content: bytes,
    match: re.Match[bytes],
    email_spans: list[tuple[int, int]],
) -> bool:
    """Return whether a handle match is syntax rather than author metadata."""
    if content[match.end() : match.end() + 1] == b"/":
        # Scoped package names such as @cloudflare/workers-types.
        return True
    if any(start <= match.start() < end for start, end in email_spans):
        # Full email addresses are scanned separately; do not treat @domain as
        # a second identity finding.
        return True

    handle = match.group(0).decode("ascii", errors="replace").lower()
    suffix = content[match.end() :]
    extension = Path(path).suffix.lower()
    if (
        handle in JSON_LD_KEY_HANDLES
        and extension in {".html", ".json", ".js", ".jsx", ".ts", ".tsx"}
        and re.match(rb'"?\s*:', suffix)
    ):
        return True
    if handle in CSS_AT_RULE_HANDLES and extension in {".css", ".html"}:
        line_start = content.rfind(b"\n", 0, match.start()) + 1
        if not content[line_start : match.start()].strip():
            return True
    return False


def _scan_blob(
    path: str,
    content: bytes,
    policy: dict[str, Any],
    classification: str,
) -> list[dict[str, str]]:
    """Return redacted findings for one publishable blob."""
    findings: list[dict[str, str]] = []
    prohibited = set(policy["prohibited_content_classes"])

    if "private-keys" in prohibited:
        for pattern in PRIVATE_KEY_HEADERS:
            if pattern.search(content):
                findings.append(
                    {
                        "path": path,
                        "rule": "private-key-header",
                        "category": "private-keys",
                        "redacted": "true",
                    }
                )
                break

    if "tokens" in prohibited:
        for pattern in TOKEN_PATTERNS:
            if pattern.search(content):
                findings.append(
                    {
                        "path": path,
                        "rule": "high-confidence-token",
                        "category": "tokens",
                        "redacted": "true",
                    }
                )
                break

    if "private-repo-urls" in prohibited:
        for pattern in PRIVATE_REPO_PATTERNS:
            if pattern.search(content):
                findings.append(
                    {
                        "path": path,
                        "rule": "private-repo-reference",
                        "category": "private-repo-urls",
                        "redacted": "true",
                    }
                )
                break

    if "private-network-endpoints" in prohibited:
        allowed = {item.lower() for item in policy.get("allowed_endpoints", [])}
        for pattern in PRIVATE_NETWORK_PATTERNS:
            for match in pattern.finditer(content):
                host = _host_from_match(match.group(0))
                if host in allowed:
                    continue
                findings.append(
                    {
                        "path": path,
                        "rule": "private-network-endpoint",
                        "category": "private-network-endpoints",
                        "redacted": "true",
                    }
                )
                break

    if "identity-metadata" in prohibited and classification != "synthetic":
        identity = policy["public_identity"]
        allowed_emails = {item.lower() for item in identity.get("emails", [])}
        allowed_names = {item.lower() for item in identity.get("names", [])}
        for match in EMAIL_PATTERN.finditer(content):
            email = match.group(0).decode("ascii", errors="replace").lower()
            domain = email.rsplit("@", 1)[-1]
            if domain in PLACEHOLDER_DOMAINS:
                continue
            if email not in allowed_emails:
                findings.append(
                    {
                        "path": path,
                        "rule": "unallowlisted-email",
                        "category": "identity-metadata",
                        "redacted": "true",
                    }
                )
                break
        if Path(path).suffix.lower() in HANDLE_SCAN_EXTENSIONS:
            email_spans = [match.span() for match in EMAIL_PATTERN.finditer(content)]
            for match in HANDLE_PATTERN.finditer(content):
                handle = match.group(0).decode("ascii", errors="replace").lower()
                if _is_non_identity_handle(path, content, match, email_spans):
                    continue
                if re.match(r"@v\d+(?:\.\d+)*$", handle):
                    continue
                if re.fullmatch(r"@[0-9a-f]{20,40}", handle):
                    continue
                if handle.lstrip("@") in PLACEHOLDER_HANDLES:
                    continue
                if (
                    handle not in allowed_names
                    and handle.lstrip("@") not in allowed_names
                ):
                    findings.append(
                        {
                            "path": path,
                            "rule": "unallowlisted-handle",
                            "category": "identity-metadata",
                            "redacted": "true",
                        }
                    )
                    break

    return findings


def _resolve_commit(root: Path, source_sha: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_sha):
        raise PublicationAuditError(
            "source_sha must be a full 40-character commit SHA; symbolic refs are not allowed"
        )
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{source_sha}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _tree_sha(root: Path, source_sha: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", f"{source_sha}^{{tree}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _candidate_blob(
    root: Path,
    source_sha: str,
    entries_by_path: dict[str, dict[str, str]],
    path: Path,
    label: str,
) -> bytes:
    """Read a policy/manifest from the candidate tree, not mutable disk state."""
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise PublicationAuditError(f"{label} must be inside repository root") from exc
    entry = entries_by_path.get(rel)
    if entry is None or entry.get("type") != "blob":
        raise PublicationAuditError(
            f"{label} is not tracked in source commit {source_sha}"
        )
    blob = git_blob_bytes(root, entry["oid"])
    try:
        if path.read_bytes() != blob:
            raise PublicationAuditError(
                f"{label} differs from source commit {source_sha}; refusing mutable policy"
            )
    except OSError as exc:
        raise PublicationAuditError(f"{label} could not be read: {path}") from exc
    return blob


def _load_candidate_policy(
    root: Path,
    source_sha: str,
    entries_by_path: dict[str, dict[str, str]],
    path: Path,
) -> dict[str, Any]:
    blob = _candidate_blob(root, source_sha, entries_by_path, path, "audit policy")
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationAuditError(
            "audit policy in source commit is not valid UTF-8 JSON"
        ) from exc
    return _parse_policy(data, f"source commit {source_sha}")


def _load_candidate_exclusions(
    root: Path,
    source_sha: str,
    entries_by_path: dict[str, dict[str, str]],
    path: Path,
) -> list[dict[str, str]]:
    blob = _candidate_blob(
        root, source_sha, entries_by_path, path, "exclusions manifest"
    )
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationAuditError(
            "exclusions manifest in source commit is not valid UTF-8 JSON"
        ) from exc
    return parse_exclusions(data, f"source commit {source_sha} exclusions")


def _commit_identity_findings(
    root: Path, source_sha: str, policy: dict[str, Any]
) -> tuple[list[dict[str, str]], dict[str, bool]]:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", source_sha],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    values = result.stdout.rstrip("\n").split("\x00")
    if len(values) != 4:
        raise PublicationAuditError("source commit metadata could not be parsed")
    author_name, author_email, committer_name, committer_email = values
    allowed_names = {item.lower() for item in policy["public_identity"]["names"]}
    allowed_emails = {item.lower() for item in policy["public_identity"]["emails"]}
    checks = {
        "author_name_allowed": author_name.lower() in allowed_names,
        "author_email_allowed": author_email.lower() in allowed_emails,
        "committer_name_allowed": committer_name.lower() in allowed_names,
        "committer_email_allowed": committer_email.lower() in allowed_emails,
    }
    findings: list[dict[str, str]] = []
    for key, allowed in checks.items():
        if not allowed:
            findings.append(
                {
                    "path": "<commit metadata>",
                    "rule": f"unallowlisted-{key.removesuffix('_allowed')}",
                    "category": "identity-metadata",
                    "redacted": "true",
                }
            )
    return findings, checks


def audit_publication(
    root: Path,
    source_sha: str,
    policy_path: Path | None = None,
    exclusions_path: Path | None = None,
) -> dict[str, Any]:
    """Audit an exact source commit and return a machine-readable report."""
    if policy_path is None:
        policy_path = root / DEFAULT_POLICY
    if exclusions_path is None:
        exclusions_path = root / DEFAULT_EXCLUSIONS

    resolved_sha = _resolve_commit(root, source_sha)
    tree_sha = _tree_sha(root, resolved_sha)
    entries = git_tree_entries(root, resolved_sha)
    entries_by_path = {entry["path"]: entry for entry in entries}
    policy = _load_candidate_policy(root, resolved_sha, entries_by_path, policy_path)
    exclusions = _load_candidate_exclusions(
        root, resolved_sha, entries_by_path, exclusions_path
    )
    exclusion_patterns = [entry["pattern"] for entry in exclusions]

    excluded_paths: list[str] = []
    synthetic_paths: list[str] = []
    publishable_blobs: list[tuple[str, bytes]] = []
    findings: list[dict[str, str]] = []
    total_bytes = 0
    excluded_bytes = 0
    publishable_bytes = 0
    synthetic_bytes = 0

    for entry in entries:
        rel = entry["path"]
        if _matches_any(rel, exclusion_patterns):
            excluded_paths.append(rel)
            if entry["type"] == "blob":
                excluded_bytes += git_blob_size(root, entry["oid"])
            continue
        prohibited_class = _prohibited_path_class(rel, policy)
        if prohibited_class is not None:
            findings.append(
                {
                    "path": rel,
                    "rule": "prohibited-path-class",
                    "category": prohibited_class,
                    "redacted": "true",
                }
            )
            continue
        if entry["type"] != "blob":
            findings.append(
                {
                    "path": rel,
                    "rule": "unsafe-non-blob-entry",
                    "category": "unsafe-tree-entry",
                    "redacted": "true",
                }
            )
            continue
        classification = _classify_path(rel, policy)
        if classification == "synthetic":
            synthetic_paths.append(rel)
        content = git_blob_bytes(root, entry["oid"])
        content_bytes = len(content)
        publishable_bytes += content_bytes
        if classification == "synthetic":
            synthetic_bytes += content_bytes
        publishable_blobs.append((rel, content))
        findings.extend(_scan_blob(rel, content, policy, classification))

    total_bytes = excluded_bytes + publishable_bytes

    publishable_tree_hash = source_tree_hash_from_blobs(publishable_blobs)
    policy_digest = _sha256(_canonical_json(policy))

    redacted_findings = _redact_findings(findings, policy)
    identity_findings, identity_status = _commit_identity_findings(
        root, resolved_sha, policy
    )
    findings.extend(identity_findings)
    redacted_findings = _redact_findings(findings, policy)
    blocking_findings = redacted_findings

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_sha": resolved_sha,
        "source_tree": tree_sha,
        "publishable_tree_hash": publishable_tree_hash,
        "policy_digest": policy_digest,
        "history_strategy": policy["history_strategy"],
        "clean_root_history": policy["history_strategy"] == "clean-root",
        "metadata_status": "ok" if not blocking_findings else "blocked",
        "commit_metadata": identity_status,
        "counts": {
            "total_entries": len(entries),
            "excluded": len(excluded_paths),
            "publishable": len(publishable_blobs),
            "synthetic": len(synthetic_paths),
            "total_bytes": total_bytes,
            "excluded_bytes": excluded_bytes,
            "publishable_bytes": publishable_bytes,
            "synthetic_bytes": synthetic_bytes,
            "findings": len(findings),
            "blocking_findings": len(blocking_findings),
        },
        "exclusions": sorted(excluded_paths),
        "synthetic": sorted(synthetic_paths),
        "findings": redacted_findings,
    }
    report["report_digest"] = _sha256(
        _canonical_json(
            {
                key: value
                for key, value in report.items()
                if key not in {"report_digest", "generated_at"}
            }
        )
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit an exact source commit before public publication."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Repository root (default: current directory).",
    )
    parser.add_argument(
        "--source-sha",
        required=True,
        help="Explicit source commit SHA to audit.",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Path to audit policy (default: .publication/audit-policy.json).",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=None,
        help="Path to exclusions manifest (default: .publication/exclusions.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the machine-readable report to this path.",
    )
    args = parser.parse_args(argv)

    try:
        report = audit_publication(
            root=args.root,
            source_sha=args.source_sha,
            policy_path=args.policy,
            exclusions_path=args.exclusions,
        )
    except (
        PublicationAuditError,
        PublicationBoundaryError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    counts = report["counts"]
    print(f"OK  source commit: {report['source_sha']}")
    print(f"OK  source tree:   {report['source_tree']}")
    print(f"OK  publishable tree hash: {report['publishable_tree_hash']}")
    print(f"OK  policy digest: {report['policy_digest']}")
    print(
        f"OK  publishable files: {counts['publishable']} "
        f"(excluded {counts['excluded']}, synthetic {counts['synthetic']})"
    )
    if counts["blocking_findings"]:
        print(
            f"FAIL blocking findings: {counts['blocking_findings']}",
            file=sys.stderr,
        )
        return 1
    print("OK  publication audit passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
