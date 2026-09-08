#!/usr/bin/env python3
"""Sync an approved internal commit to the public review-sensei repository.

The script:
  1. Validates the publication boundary (secrets, exclusions, tracked files).
  2. Computes a deterministic source tree hash.
  3. Checks out the target source commit into a temporary directory.
  4. Applies the exclusion list to produce the public file set.
  5. Commits the public file set to the public repository.
  6. Appends a provenance ledger entry recording both SHAs.

Re-running for the same approved source commit is idempotent: the script
checks the public ledger for the same source hash and exits without creating a
duplicate commit.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from validate_publication_boundary import (
    TREE_HASH_ALGORITHM,
    PublicationBoundaryError,
    load_exclusions,
    source_tree_hash,
    validate_boundary,
)

LEDGER_FILENAME = "publication-ledger.jsonl"
DEFAULT_LEDGER_DIR = ".publication"
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
PUBLIC_COMMIT_NAME = "malsabbagh"
PUBLIC_COMMIT_EMAIL = "malsabbagh@users.noreply.github.com"


class PublicationSyncError(ValueError):
    """Raised when the sync cannot proceed safely."""


def _git(
    root: Path, *args: str, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=capture,
        text=True,
        check=True,
    )


def _configure_public_identity(root: Path) -> None:
    """Configure a neutral, repository-local identity for publication commits."""
    _git(root, "config", "user.name", PUBLIC_COMMIT_NAME)
    _git(root, "config", "user.email", PUBLIC_COMMIT_EMAIL)


def _validate_branch_name(branch: str) -> None:
    """Reject branch names that can alter shell or Git command parsing."""
    if not branch:
        raise PublicationSyncError("public branch must not be empty")
    if "$(" in branch or "`" in branch:
        raise PublicationSyncError(f"invalid public branch value: {branch!r}")
    if ";" in branch or "&&" in branch or "||" in branch or "|" in branch:
        raise PublicationSyncError(f"invalid public branch value: {branch!r}")
    if (
        branch.startswith("-")
        or branch.endswith("/")
        or "/." in branch
        or ".." in branch
    ):
        raise PublicationSyncError(f"invalid public branch value: {branch!r}")
    if not _BRANCH_NAME_RE.fullmatch(branch):
        raise PublicationSyncError(f"invalid public branch value: {branch!r}")


def _export_source_commit(
    source_root: Path,
    source_sha: str,
    dest: Path,
) -> list[str]:
    """Export a specific commit's tracked files into `dest` and return file paths."""
    dest.mkdir(parents=True, exist_ok=True)
    result = _git(
        source_root,
        "ls-tree",
        "-r",
        "--full-tree",
        "--format=%(objectmode) %(path)",
        source_sha,
        capture=True,
    )
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        mode, rel = line.split(" ", 1)
        entries.append((rel, mode))

    for rel, mode in entries:
        blob_result = _git(
            source_root,
            "show",
            f"{source_sha}:{rel}",
            capture=True,
        )
        out_path = dest / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(blob_result.stdout.encode("utf-8"))
        if mode == "100755":
            current_mode = out_path.stat().st_mode
            out_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return [rel for rel, _ in entries]


def _checkout_source_tree(source_root: Path, source_sha: str, workdir: Path) -> Path:
    """Create a temporary detached worktree for `source_sha` and return its path."""
    workdir.mkdir(parents=True, exist_ok=True)
    _git(source_root, "worktree", "add", "--detach", str(workdir), source_sha)
    return workdir


def _find_ledger_entry(
    ledger_path: Path,
    source_sha: str,
    tree_hash: str,
) -> dict[str, str] | None:
    """Return an existing ledger entry matching source_sha or tree_hash."""
    if not ledger_path.is_file():
        return None
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if (
            entry.get("source_sha") == source_sha
            or entry.get("source_tree_hash") == tree_hash
        ):
            return entry
    return None


def _append_ledger_entry(
    ledger_path: Path,
    entry: dict[str, str],
) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def sync_to_public(
    source_root: Path,
    source_sha: str,
    public_repo: str,
    public_remote: str,
    public_branch: str = "main",
    workdir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Sync an approved source commit to the public repository.

    Returns the provenance ledger entry. In dry-run mode the entry is returned but
    no commit is created.
    """
    exclusions_path = source_root / ".publication" / "exclusions.json"
    load_exclusions(exclusions_path)
    _validate_branch_name(public_branch)

    owns_workdir = workdir is None
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="review-sensei-publish-"))

    source_checkout = workdir / "source-checkout"
    source_tree = workdir / "source-tree"
    try:
        source_checkout = _checkout_source_tree(
            source_root, source_sha, source_checkout
        )
        publishable, _ = validate_boundary(source_checkout)
        _export_source_commit(source_checkout, source_sha, source_tree)
        tree_hash = source_tree_hash(source_tree, publishable)

        ledger_dir = workdir / DEFAULT_LEDGER_DIR
        ledger_path = ledger_dir / LEDGER_FILENAME
        if dry_run:
            local_ledger = source_root / DEFAULT_LEDGER_DIR / LEDGER_FILENAME
            if local_ledger.is_file():
                ledger_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_ledger, ledger_path)

        existing = _find_ledger_entry(ledger_path, source_sha, tree_hash)
        if existing is not None:
            print(
                f"SKIP: source commit {source_sha} already published as "
                f"{existing.get('public_sha', 'unknown')}"
            )
            return existing

        if dry_run:
            entry = {
                "source_sha": source_sha,
                "source_tree_hash": tree_hash,
                "source_tree_hash_algorithm": TREE_HASH_ALGORITHM,
                "public_repo": public_repo,
                "public_branch": public_branch,
                "public_sha": "(dry-run)",
                "files": str(len(publishable)),
            }
            print(f"DRY-RUN: would publish {len(publishable)} files to {public_repo}")
            print(f"DRY-RUN: source tree hash: {tree_hash}")
            return entry

        # Clone the public repo before duplicate detection so we can use the
        # canonical ledger history for idempotency checks.
        clone_dir = workdir / "public-clone"
        _git(
            Path("."),
            "clone",
            "--branch",
            public_branch,
            public_remote,
            str(clone_dir),
        )
        _configure_public_identity(clone_dir)

        public_ledger = clone_dir / DEFAULT_LEDGER_DIR / LEDGER_FILENAME
        existing = _find_ledger_entry(public_ledger, source_sha, tree_hash)
        if existing is not None:
            print(
                f"SKIP: source commit {source_sha} already published as "
                f"{existing.get('public_sha', 'unknown')}"
            )
            return existing

        # Remove all tracked files in the clone, then copy exported files.
        clone_tracked = _git(clone_dir, "ls-files", capture=True).stdout.splitlines()
        for rel in clone_tracked:
            target = clone_dir / rel
            if target.is_file():
                target.unlink()

        for rel in publishable:
            dest = clone_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_tree / rel, dest)

        _git(clone_dir, "add", "-A")
        diff = _git(clone_dir, "diff", "--cached", "--name-only", capture=True).stdout
        if diff.strip():
            _git(
                clone_dir,
                "commit",
                "-m",
                f"Publish from internal {source_sha[:12]}\n\nSource tree hash: {tree_hash}",
            )

        public_sha = _git(clone_dir, "rev-parse", "HEAD", capture=True).stdout.strip()

        entry = {
            "source_sha": source_sha,
            "source_tree_hash": tree_hash,
            "source_tree_hash_algorithm": TREE_HASH_ALGORITHM,
            "public_repo": public_repo,
            "public_branch": public_branch,
            "public_sha": public_sha,
            "files": str(len(publishable)),
        }
        _append_ledger_entry(public_ledger, entry)
        _git(clone_dir, "add", str(public_ledger))
        _git(
            clone_dir,
            "commit",
            "-m",
            f"Record provenance for {public_sha[:12]}",
        )
        _git(clone_dir, "push", "origin", public_branch)

        # Also copy the updated ledger back to the source repo.
        local_ledger = source_root / DEFAULT_LEDGER_DIR / LEDGER_FILENAME
        local_ledger.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(public_ledger, local_ledger)

        print(f"OK  published {len(publishable)} files to {public_repo}")
        print(f"OK  public commit: {public_sha}")
        print(f"OK  source tree hash: {tree_hash}")
        return entry
    finally:
        if source_checkout.is_dir():
            _git(source_root, "worktree", "remove", "--force", str(source_checkout))
        if owns_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync an approved internal commit to the public repository."
    )
    parser.add_argument(
        "--source-sha",
        required=True,
        help="Approved internal source commit SHA to publish.",
    )
    parser.add_argument(
        "--public-repo",
        default="malsabbagh/review-sensei",
        help="Public repository slug (default: malsabbagh/review-sensei).",
    )
    parser.add_argument(
        "--public-remote",
        default="",
        help="Git remote URL for the public repository. Required for non-dry-run.",
    )
    parser.add_argument(
        "--public-branch",
        default="main",
        help="Public repository branch to sync to (default: main).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Internal repository root (default: current directory).",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Temporary working directory (default: auto-created).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and compute hashes without pushing to the public repo.",
    )
    args = parser.parse_args(argv)

    if not args.dry_run and not args.public_remote:
        print(
            "FAIL: --public-remote is required unless --dry-run is set", file=sys.stderr
        )
        return 2

    try:
        sync_to_public(
            source_root=args.root,
            source_sha=args.source_sha,
            public_repo=args.public_repo,
            public_remote=args.public_remote,
            public_branch=args.public_branch,
            workdir=args.workdir,
            dry_run=args.dry_run,
        )
    except (
        PublicationBoundaryError,
        PublicationSyncError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
