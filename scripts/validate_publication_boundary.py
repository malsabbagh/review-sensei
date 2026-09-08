#!/usr/bin/env python3
"""Validate that a source tree is safe to publish to the public repository.

Checks every tracked file against the exclusion manifest and a built-in
secret/credential heuristic.  The script fails closed: any file that matches
an exclusion pattern but is not tracked, or any tracked file that looks like
a secret, aborts publication before the sync step runs.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable

DEFAULT_EXCLUSIONS = ".publication/exclusions.json"
TREE_HASH_ALGORITHM = "sha256-path-mode-blob-v1"
DEFAULT_FILE_MODE = "100644"

# Patterns that indicate a likely secret or private key.  The check is
# conservative: it looks at file *paths* and a small set of file *extensions*
# rather than scanning file contents, so it is safe to run on a checkout that
# may contain private fixtures.
SECRET_PATH_PATTERNS = (
    re.compile(r"(^|/)\.env$", re.IGNORECASE),
    re.compile(r"(^|/)\.env\.", re.IGNORECASE),
    re.compile(r"(^|/)secrets?/", re.IGNORECASE),
    re.compile(r"(^|/)credentials?/", re.IGNORECASE),
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
    re.compile(r"\.pfx$", re.IGNORECASE),
    re.compile(r"\.p12$", re.IGNORECASE),
    re.compile(r"id_rsa", re.IGNORECASE),
    re.compile(r"id_ed25519$", re.IGNORECASE),
    re.compile(r"(^|/)\.ssh/", re.IGNORECASE),
)

# Content-level heuristics for private key material.  Only applied to files
# with text-like extensions to avoid false positives in binary artifacts.
PRIVATE_KEY_HEADER = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
)
TEXT_EXTENSIONS = {
    ".py",
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
    ".env",
    ".xml",
    ".html",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".sql",
    ".conf",
}


class PublicationBoundaryError(ValueError):
    """Raised when the source tree is not safe to publish."""


def parse_exclusions(
    data: object, source: str = "exclusions manifest"
) -> list[dict[str, str]]:
    """Validate decoded exclusion data and return normalized entries."""
    if not isinstance(data, list):
        raise PublicationBoundaryError(f"{source} must be a JSON array")
    for entry in data:
        if not isinstance(entry, dict):
            raise PublicationBoundaryError(
                f"each exclusion in {source} must be a JSON object"
            )
        if "pattern" not in entry or not isinstance(entry["pattern"], str):
            raise PublicationBoundaryError(
                f"each exclusion in {source} needs a string 'pattern'"
            )
        if "reason" not in entry or not isinstance(entry["reason"], str):
            raise PublicationBoundaryError(
                f"each exclusion in {source} needs a string 'reason'"
            )
    return [{"pattern": entry["pattern"], "reason": entry["reason"]} for entry in data]


def load_exclusions(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise PublicationBoundaryError(f"exclusions manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublicationBoundaryError(
            f"exclusions manifest is not valid JSON: {path}"
        ) from exc
    return parse_exclusions(data, str(path))


def _git_tracked_files(root: Path) -> list[str]:
    """Return repo-relative paths for every file tracked by git."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _git_tracked_modes(root: Path) -> dict[str, str]:
    """Return the Git index mode for each tracked file when available."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return {}

    modes: dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if not record.strip():
            continue
        metadata, separator, rel = record.partition("\t")
        if not separator:
            continue
        mode = metadata.split(" ", 1)[0]
        modes[rel] = mode
    return modes


def _filesystem_file_mode(path: Path) -> str:
    """Map a filesystem file mode to Git's regular-file mode values."""
    return "100755" if path.stat().st_mode & stat.S_IXUSR else DEFAULT_FILE_MODE


def git_tree_entries(root: Path, sha: str) -> list[dict[str, str]]:
    """Return exact tree entries for a commit, including mode/type/oid/path."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", sha],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    entries: list[dict[str, str]] = []
    for line in result.stdout.split("\0"):
        if not line:
            continue
        meta, _, path = line.partition("\t")
        mode, type_, oid = meta.split(" ", 2)
        entries.append({"mode": mode, "type": type_, "oid": oid, "path": path})
    return entries


def git_blob_bytes(root: Path, oid: str) -> bytes:
    """Return the exact bytes of a Git blob object."""
    result = subprocess.run(
        ["git", "cat-file", "blob", oid],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return result.stdout


def git_blob_size(root: Path, oid: str) -> int:
    """Return a Git blob's byte size without reading its content."""
    result = subprocess.run(
        ["git", "cat-file", "-s", oid],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _looks_like_secret_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in SECRET_PATH_PATTERNS)


def _contains_private_key(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return False
    try:
        content = path.read_bytes()
    except OSError:
        return False
    return bool(PRIVATE_KEY_HEADER.search(content))


def validate_boundary(
    root: Path,
    exclusions_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Validate the publication boundary and return (tracked, excluded) lists.

    Raises PublicationBoundaryError if any tracked file matches a secret
    pattern or contains private key material.
    """
    if exclusions_path is None:
        exclusions_path = root / DEFAULT_EXCLUSIONS
    exclusions = load_exclusions(exclusions_path)
    patterns = [e["pattern"] for e in exclusions]

    tracked = _git_tracked_files(root)
    excluded: list[str] = []
    publishable: list[str] = []

    for rel in tracked:
        if _matches_any(rel, patterns):
            excluded.append(rel)
            continue
        if _looks_like_secret_path(rel):
            raise PublicationBoundaryError(
                f"tracked file matches a secret path pattern: {rel}"
            )
        abs_path = root / rel
        if _contains_private_key(abs_path):
            raise PublicationBoundaryError(
                f"tracked file contains private key material: {rel}"
            )
        publishable.append(rel)

    return publishable, excluded


def source_tree_hash(root: Path, files: list[str]) -> str:
    """Return a deterministic SHA-256 hash of paths, modes, and blob contents."""
    tracked_modes = _git_tracked_modes(root)
    entries = [
        (
            rel,
            (root / rel).read_bytes(),
            tracked_modes.get(rel, _filesystem_file_mode(root / rel)),
        )
        for rel in files
    ]
    return source_tree_hash_from_blobs(entries)


def source_tree_hash_from_blobs(
    entries: list[tuple[str, bytes, str]],
) -> str:
    """Return a deterministic SHA-256 hash of paths, modes, and blobs."""
    hasher = hashlib.sha256()
    for rel, content, mode in sorted(entries, key=lambda item: item[0]):
        file_hash = hashlib.sha256(content).hexdigest()
        hasher.update(f"{rel}\0{mode}\0{file_hash}\n".encode("utf-8"))
    return hasher.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the publication boundary before a public sync."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Repository root (default: current directory).",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=None,
        help="Path to exclusions manifest (default: .publication/exclusions.json).",
    )
    parser.add_argument(
        "--print-files",
        action="store_true",
        help="Print every publishable file path.",
    )
    args = parser.parse_args(argv)

    try:
        publishable, excluded = validate_boundary(args.root, args.exclusions)
    except (PublicationBoundaryError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    tree_hash = source_tree_hash(args.root, publishable)
    print(f"OK  publishable files: {len(publishable)}")
    print(f"OK  excluded files:    {len(excluded)}")
    print(f"OK  source tree hash:  {tree_hash}")
    if args.print_files:
        for rel in sorted(publishable):
            print(f"  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
