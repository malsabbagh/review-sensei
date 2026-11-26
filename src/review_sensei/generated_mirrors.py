"""Isolated read-only 3-snapshot verifier for deterministic generated mirrors."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from .errors import ReviewInputError
from .evidence import evidence_digest

if TYPE_CHECKING:
    from .configuration import GeneratedMirrorRule

MAX_MIRROR_TREE_FILES = 20_000
MAX_MIRROR_SINGLE_FILE_BYTES = 16 * 1024 * 1024
MAX_MIRROR_TREE_TOTAL_BYTES = 128 * 1024 * 1024
_ALLOWED_GIT_MODES = frozenset({"100644", "100755"})


def mirror_path_matches(path: str, prefix: str) -> bool:
    """Return True when ``path`` equals or resides beneath ``prefix``."""
    normalized_path = path.replace("\\", "/").strip("/")
    normalized_prefix = prefix.replace("\\", "/").strip("/")
    if not normalized_path or not normalized_prefix:
        return False
    return normalized_path == normalized_prefix or normalized_path.startswith(
        normalized_prefix + "/"
    )


def _path_intersects_prefixes(path: str, prefixes: Sequence[str]) -> bool:
    return any(
        mirror_path_matches(path, prefix) or mirror_path_matches(prefix, path)
        for prefix in prefixes
    )


@dataclass(frozen=True)
class SnapshotMirrorVerification:
    """Verification outcome for one immutable git snapshot."""

    snapshot: str
    tree_oid: str
    verified: bool
    reason: str | None = None
    checked_files: tuple[str, ...] = ()
    mirror_digests: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.snapshot not in {"base", "head", "merge"}:
            raise ReviewInputError("snapshot mirror verification name is invalid")
        if not isinstance(self.verified, bool):
            raise ReviewInputError("snapshot mirror verified flag must be a boolean")


@dataclass(frozen=True)
class VerifiedMirrorAttestation:
    """Host-computed 3-snapshot deterministic mirror attestation."""

    rule_name: str
    policy_commit: str
    base_sha: str
    head_sha: str
    canonical_sources: tuple[str, ...]
    mirror_paths: tuple[str, ...]
    generator_sources: tuple[str, ...]
    snapshots: tuple[SnapshotMirrorVerification, ...]
    reason: str | None = None

    @property
    def verified(self) -> bool:
        return (
            self.reason is None
            and len(self.snapshots) == 3
            and {item.snapshot for item in self.snapshots} == {"base", "head", "merge"}
            and all(item.verified for item in self.snapshots)
        )

    def digest(self) -> str:
        return evidence_digest(
            {
                "rule_name": self.rule_name,
                "policy_commit": self.policy_commit,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "canonical_sources": list(self.canonical_sources),
                "mirror_paths": list(self.mirror_paths),
                "generator_sources": list(self.generator_sources),
                "reason": self.reason,
                "verified": self.verified,
                "snapshots": [
                    {
                        "snapshot": item.snapshot,
                        "tree_oid": item.tree_oid,
                        "verified": item.verified,
                        "reason": item.reason,
                        "checked_files": list(item.checked_files),
                        "mirror_digests": [list(pair) for pair in item.mirror_digests],
                    }
                    for item in self.snapshots
                ],
            }
        )


def _git_command(
    repo_root: Path,
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    # Every git invocation inspecting `merge_tree` OIDs MUST pass `env=merge_env`
    # (carrying GIT_OBJECT_DIRECTORY + GIT_ALTERNATE_OBJECT_DIRECTORIES) because
    # synthetic merge-tree objects live only in that ephemeral object directory.
    run_env = os.environ.copy() if env is None else dict(env)
    return subprocess.run(
        ["git", "-C", str(repo_root), "-c", "core.hooksPath=", *args],
        capture_output=True,
        env=run_env,
        check=False,
    )


def _git_objects_directory(repo_root: Path) -> Path | None:
    proc = _git_command(repo_root, ("rev-parse", "--git-path", "objects"))
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


def _resolve_tree_oid(
    repo_root: Path, ref: str, *, env: Mapping[str, str] | None = None
) -> str | None:
    proc = _git_command(
        repo_root, ("rev-parse", "--verify", f"{ref}^{{tree}}"), env=env
    )
    if proc.returncode != 0:
        return None
    tree_oid = proc.stdout.decode("utf-8", errors="replace").strip()
    return tree_oid or None


def _is_safe_relpath(raw_path: str) -> bool:
    if not raw_path or raw_path.startswith(("/", "\\")):
        return False
    normalized = raw_path.replace("\\", "/")
    parts = normalized.split("/")
    return not any(part in {"", ".", "..", ".git"} for part in parts)


def _inspect_tree_entries(
    repo_root: Path,
    tree_oid: str,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, str] | None, str | None]:
    proc = _git_command(
        repo_root,
        ("ls-tree", "-r", "-z", "-l", "--full-tree", tree_oid),
        env=env,
    )
    if proc.returncode != 0:
        return None, "tree-inspection-failed"
    entries: dict[str, str] = {}
    total_bytes = 0
    for raw_record in proc.stdout.split(b"\x00"):
        if not raw_record:
            continue
        header_bytes, sep, path_bytes = raw_record.partition(b"\t")
        if not sep:
            return None, "tree-inspection-failed"
        header_parts = header_bytes.decode("ascii", errors="strict").split()
        if len(header_parts) < 4:
            return None, "tree-inspection-failed"
        mode, obj_type, blob_oid, size_text = header_parts[:4]
        if mode not in _ALLOWED_GIT_MODES or obj_type != "blob":
            return None, "unsupported-tree-entry"
        if not size_text.isdigit():
            return None, "tree-inspection-failed"
        size = int(size_text)
        if size > MAX_MIRROR_SINGLE_FILE_BYTES:
            return None, "tree-size-limit-exceeded"
        total_bytes += size
        if total_bytes > MAX_MIRROR_TREE_TOTAL_BYTES:
            return None, "tree-size-limit-exceeded"
        if len(entries) >= MAX_MIRROR_TREE_FILES:
            return None, "tree-file-limit-exceeded"
        path = path_bytes.decode("utf-8", errors="strict")
        if not _is_safe_relpath(path):
            return None, "unsafe-tree-path"
        entries[path] = blob_oid
    return entries, None


def _extract_tree_safely(
    repo_root: Path,
    tree_oid: str,
    work_dir: Path,
    archive_path: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    with archive_path.open("wb") as handle:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "-c",
                "core.hooksPath=",
                "archive",
                "--format=tar",
                tree_oid,
            ],
            stdout=handle,
            stderr=subprocess.PIPE,
            env=os.environ.copy() if env is None else dict(env),
            check=False,
        )
    if proc.returncode != 0:
        return "tree-archive-failed"
    resolved_work = work_dir.resolve()
    try:
        with tarfile.open(archive_path, mode="r:") as tf:
            for member in tf.getmembers():
                if (
                    member.issym()
                    or member.islnk()
                    or member.isdev()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo()
                ):
                    return "unsupported-archive-entry"
                if not (member.isreg() or member.isdir()):
                    return "unsupported-archive-entry"
                rel = member.name.rstrip("/")
                if not rel:
                    continue
                if not _is_safe_relpath(rel):
                    return "unsafe-archive-path"
                destination = (work_dir / rel).resolve()
                if not destination.is_relative_to(resolved_work):
                    return "unsafe-archive-path"
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    return "tree-archive-failed"
                with extracted, destination.open("wb") as out_file:
                    while True:
                        chunk = extracted.read(65536)
                        if not chunk:
                            break
                        out_file.write(chunk)
                if member.mode & 0o111:
                    destination.chmod(0o755)
    except (tarfile.TarError, OSError, UnicodeError):
        return "tree-archive-failed"
    return None


def _hash_matching_files(
    work_dir: Path, prefixes: Sequence[str]
) -> tuple[dict[str, str] | None, str | None]:
    hashes: dict[str, str] = {}
    for root, dirs, files in os.walk(work_dir, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(work_dir)
        for dname in list(dirs):
            dpath = root_path / dname
            rel_dir = (rel_root / dname).as_posix() if rel_root != Path(".") else dname
            if dpath.is_symlink() and _path_intersects_prefixes(rel_dir, prefixes):
                return None, "symlink-detected"
            if dpath.is_symlink():
                dirs.remove(dname)
        for fname in files:
            fpath = root_path / fname
            rel_file = (rel_root / fname).as_posix() if rel_root != Path(".") else fname
            if not _path_intersects_prefixes(rel_file, prefixes):
                continue
            if fpath.is_symlink():
                return None, "symlink-detected"
            hashes[rel_file] = hashlib.sha256(fpath.read_bytes()).hexdigest()
    return hashes, None


def _clear_matching_files(work_dir: Path, prefixes: Sequence[str]) -> str | None:
    """Remove pre-existing mirror files before generation so outputs are proven fresh."""
    for root, dirs, files in os.walk(work_dir, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(work_dir)
        for dname in list(dirs):
            dpath = root_path / dname
            rel_dir = (rel_root / dname).as_posix() if rel_root != Path(".") else dname
            if dpath.is_symlink():
                if _path_intersects_prefixes(rel_dir, prefixes):
                    return "symlink-detected"
                dirs.remove(dname)
        for fname in files:
            fpath = root_path / fname
            rel_file = (rel_root / fname).as_posix() if rel_root != Path(".") else fname
            if not any(mirror_path_matches(rel_file, prefix) for prefix in prefixes):
                continue
            if fpath.is_symlink():
                return "symlink-detected"
            fpath.unlink()
    return None


def _resolve_generator_command(command: Sequence[str]) -> list[str]:
    """Resolve bare ``python3`` on Windows or when missing from PATH to ``sys.executable``."""
    resolved = list(command)
    if not resolved:
        return resolved
    exe_name = Path(resolved[0].replace("\\", "/")).name.lower().removesuffix(".exe")
    if exe_name == "python3" and (
        os.name == "nt"
        or resolved[0] == "python3"
        and shutil.which(resolved[0]) is None
    ):
        resolved[0] = sys.executable
    return resolved


def _generator_subprocess_env(work_dir: Path) -> dict[str, str]:
    """Build a minimal credential-free environment for isolated generator runs."""
    run_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(work_dir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "NO_COLOR": "1",
    }
    if os.name == "nt":
        for win_var in (
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
            "WINDIR",
            "OS",
            "TMP",
            "TEMP",
        ):
            value = os.environ.get(win_var)
            if value is not None:
                run_env[win_var] = value
    return run_env


def _verify_snapshot(
    repo_root: Path,
    *,
    snapshot_name: str,
    tree_oid: str,
    rule: GeneratedMirrorRule,
    git_env: Mapping[str, str] | None = None,
) -> SnapshotMirrorVerification:
    # `git_env` must be `merge_env` when inspecting the synthetic `merge` tree
    # so ls-tree, archive, and cat-file can read its ephemeral tree/blob OIDs.
    entries, inspect_error = _inspect_tree_entries(repo_root, tree_oid, env=git_env)
    if inspect_error is not None or entries is None:
        return SnapshotMirrorVerification(
            snapshot=snapshot_name,
            tree_oid=tree_oid,
            verified=False,
            reason=inspect_error or "tree-inspection-failed",
        )
    expected_blobs = {
        path: blob_oid
        for path, blob_oid in sorted(entries.items())
        if any(mirror_path_matches(path, prefix) for prefix in rule.mirror_paths)
    }
    protected_prefixes = (*rule.canonical_sources, *rule.generator_sources)
    with tempfile.TemporaryDirectory(prefix="rs-mirror-") as tmp_root:
        outer = Path(tmp_root)
        work_dir = outer / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        archive_path = outer / "tree.tar"
        extract_error = _extract_tree_safely(
            repo_root, tree_oid, work_dir, archive_path, env=git_env
        )
        if extract_error is not None:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason=extract_error,
            )
        pre_protected, pre_err = _hash_matching_files(work_dir, protected_prefixes)
        if pre_err is not None or pre_protected is None:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason=pre_err or "protected-input-error",
            )
        clear_err = _clear_matching_files(work_dir, rule.mirror_paths)
        if clear_err is not None:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason=clear_err,
            )
        run_env = _generator_subprocess_env(work_dir)
        command = _resolve_generator_command(rule.generator_command)
        try:
            gen_proc = subprocess.run(
                command,
                cwd=work_dir,
                env=run_env,
                shell=False,
                capture_output=True,
                timeout=rule.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason="generator-timeout",
            )
        except OSError:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason="generator-error",
            )
        if gen_proc.returncode != 0:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason="generator-failed",
            )
        post_protected, post_err = _hash_matching_files(work_dir, protected_prefixes)
        if post_err is not None or post_protected != pre_protected:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason=post_err or "canonical-or-generator-mutated",
            )
        actual_mirrors, mirror_err = _hash_matching_files(work_dir, rule.mirror_paths)
        if mirror_err is not None or actual_mirrors is None:
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason=mirror_err or "mirror-read-error",
            )
        if set(actual_mirrors) != set(expected_blobs):
            return SnapshotMirrorVerification(
                snapshot=snapshot_name,
                tree_oid=tree_oid,
                verified=False,
                reason="mirror-file-set-mismatch",
                checked_files=tuple(sorted(set(actual_mirrors) | set(expected_blobs))),
            )
        digests: list[tuple[str, str]] = []
        for path, blob_oid in sorted(expected_blobs.items()):
            blob_proc = _git_command(
                repo_root, ("cat-file", "blob", blob_oid), env=git_env
            )
            if blob_proc.returncode != 0:
                return SnapshotMirrorVerification(
                    snapshot=snapshot_name,
                    tree_oid=tree_oid,
                    verified=False,
                    reason="blob-read-failed",
                )
            generated_bytes = (work_dir / path).read_bytes()
            if generated_bytes != blob_proc.stdout:
                return SnapshotMirrorVerification(
                    snapshot=snapshot_name,
                    tree_oid=tree_oid,
                    verified=False,
                    reason="mirror-content-mismatch",
                    checked_files=tuple(sorted(expected_blobs)),
                )
            digests.append((path, hashlib.sha256(generated_bytes).hexdigest()))
        return SnapshotMirrorVerification(
            snapshot=snapshot_name,
            tree_oid=tree_oid,
            verified=True,
            checked_files=tuple(sorted(expected_blobs)),
            mirror_digests=tuple(digests),
        )


def verify_generated_mirrors(
    repo_root: Path,
    *,
    rules: Sequence[GeneratedMirrorRule],
    base_sha: str,
    head_sha: str,
    policy_commit: str,
    changed_paths: Sequence[str],
) -> tuple[VerifiedMirrorAttestation, ...]:
    """Verify configured deterministic generated-mirror rules across 3 snapshots."""
    if not rules:
        return ()
    relevant_rules = [
        rule
        for rule in rules
        if any(
            mirror_path_matches(changed, mirror)
            for changed in changed_paths
            for mirror in rule.mirror_paths
        )
    ]
    if not relevant_rules:
        return ()
    root_path = Path(repo_root)
    base_tree = _resolve_tree_oid(root_path, base_sha)
    head_tree = _resolve_tree_oid(root_path, head_sha)
    objects_dir = _git_objects_directory(root_path)

    attestations: list[VerifiedMirrorAttestation] = []
    for rule in relevant_rules:
        if any(
            mirror_path_matches(changed, gen_path)
            for changed in changed_paths
            for gen_path in rule.generator_sources
        ):
            attestations.append(
                VerifiedMirrorAttestation(
                    rule_name=rule.name,
                    policy_commit=policy_commit,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    canonical_sources=rule.canonical_sources,
                    mirror_paths=rule.mirror_paths,
                    generator_sources=rule.generator_sources,
                    snapshots=(),
                    reason="untrusted-generator-change",
                )
            )
            continue
        if base_tree is None or head_tree is None or objects_dir is None:
            attestations.append(
                VerifiedMirrorAttestation(
                    rule_name=rule.name,
                    policy_commit=policy_commit,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    canonical_sources=rule.canonical_sources,
                    mirror_paths=rule.mirror_paths,
                    generator_sources=rule.generator_sources,
                    snapshots=(),
                    reason="snapshot-unavailable",
                )
            )
            continue
        with tempfile.TemporaryDirectory(prefix="rs-merge-objs-") as merge_obj_dir:
            merge_env = {
                **os.environ,
                "GIT_OBJECT_DIRECTORY": merge_obj_dir,
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects_dir),
            }
            merge_proc = _git_command(
                root_path,
                ("merge-tree", "--write-tree", base_sha, head_sha),
                env=merge_env,
            )
            if merge_proc.returncode != 0:
                attestations.append(
                    VerifiedMirrorAttestation(
                        rule_name=rule.name,
                        policy_commit=policy_commit,
                        base_sha=base_sha,
                        head_sha=head_sha,
                        canonical_sources=rule.canonical_sources,
                        mirror_paths=rule.mirror_paths,
                        generator_sources=rule.generator_sources,
                        snapshots=(),
                        reason="merge-conflict",
                    )
                )
                continue
            merge_tree = (
                merge_proc.stdout.decode("utf-8", errors="replace")
                .splitlines()[0]
                .strip()
            )
            snapshots = (
                _verify_snapshot(
                    root_path,
                    snapshot_name="base",
                    tree_oid=base_tree,
                    rule=rule,
                ),
                _verify_snapshot(
                    root_path,
                    snapshot_name="head",
                    tree_oid=head_tree,
                    rule=rule,
                ),
                _verify_snapshot(
                    root_path,
                    snapshot_name="merge",
                    tree_oid=merge_tree,
                    rule=rule,
                    git_env=merge_env,
                ),
            )
        failure_reason = next(
            (snap.reason for snap in snapshots if not snap.verified), None
        )
        attestations.append(
            VerifiedMirrorAttestation(
                rule_name=rule.name,
                policy_commit=policy_commit,
                base_sha=base_sha,
                head_sha=head_sha,
                canonical_sources=rule.canonical_sources,
                mirror_paths=rule.mirror_paths,
                generator_sources=rule.generator_sources,
                snapshots=snapshots,
                reason=failure_reason,
            )
        )
    return tuple(attestations)


__all__ = [
    "MAX_MIRROR_SINGLE_FILE_BYTES",
    "MAX_MIRROR_TREE_FILES",
    "MAX_MIRROR_TREE_TOTAL_BYTES",
    "SnapshotMirrorVerification",
    "VerifiedMirrorAttestation",
    "mirror_path_matches",
    "verify_generated_mirrors",
]
