"""Lazy fresh-PR ReviewSensei analysis for v6 pilot runs under active write leases."""

from __future__ import annotations

import base64
import difflib
import hashlib
import os
import time
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review_sensei.configuration import (
    DEFAULT_CONFIG_FILENAME,
    default_configuration,
    load_configuration,
)
from review_sensei.diff import analyze_diff
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceSnapshot
from review_sensei.hosted import plan_hosted_execution
from review_sensei.hunk_subdivision import subdivide_hunk
from review_sensei.models import ReviewComment, ReviewRequest, ReviewResult
from review_sensei.presentation import render_review_markdown
from review_sensei.providers.openai_compatible import (
    validate_openai_compatible_endpoint,
)
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.providers.registry import ProviderSettings, default_registry
from review_sensei.providers.routing import bind_stage_providers
from review_sensei.service import DEFAULT_STAGES, ReviewService
from review_sensei.validation import DEFAULT_REVIEW_LIMITS

from .errors import GitHubResponseTooLargeError
from .v6_credential_matrix import (
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
)
from .v6_transport import DualCredentialGitHubClient, DualCredentialTransport

_BACKEND_ADAPTERS: Mapping[str, str] = {
    "local-ollama": "ollama",
    "cloud-ollama": "ollama",
    "openrouter": "openrouter",
    "openai-compatible": "openai-compatible",
}
_ALLOWED_ENV_KEYS = (
    "REVIEWSENSEI_PROVIDER",
    "REVIEWSENSEI_MODEL",
    "OLLAMA_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
)
_COMPARE_MAX_FILES_THRESHOLD = 300
DEFAULT_BATCH_MAX_BYTES = 64 * 1024
DEFAULT_BATCH_MAX_FILES = 50
DEFAULT_BATCH_MAX_HUNKS = 200
_SPECIAL_GIT_MODES = frozenset({"120000", "160000"})
_HEX_CHARS = frozenset("0123456789abcdef")


def _is_40_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(ch in _HEX_CHARS for ch in value)
    )


class _ReadOnlyAppTransport:
    """Fail-closed placeholder ensuring fresh-PR reads never touch App credentials."""

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        raise ReviewInputError(
            f"refusing App broker call {method} {path} during read-only PR diff fetch",
            diagnostic="v6_no_active_write_lease",
        )


@dataclass(frozen=True)
class _TreeEntry:
    path: str
    sha: str
    mode: str
    entry_type: str


def verify_live_pr_head(
    *,
    owner: str,
    repo: str,
    pull_request: int,
    expected_head_sha: str,
    workflow_token_transport: DualCredentialTransport,
    event: GitHubEventKind = GitHubEventKind.PULL_REQUEST,
    visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC,
    granted_scopes: Collection[str] = (),
    is_ghec: bool = False,
    clock: Callable[[], float] = time.time,
) -> Mapping[str, Any]:
    """Re-read PR metadata via ``WORKFLOW_GITHUB_TOKEN`` and fail closed if closed or moved."""

    read_client = DualCredentialGitHubClient(
        workflow_token_transport=workflow_token_transport,
        broker_app_transport=_ReadOnlyAppTransport(),
        is_ghec=is_ghec,
    )
    pr_resp = read_client.execute(
        "pr_metadata",
        method="GET",
        path=f"/repos/{owner}/{repo}/pulls/{pull_request}",
        event=event,
        visibility=visibility,
        granted_scopes=granted_scopes,
        credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
        now_epoch=clock(),
    )
    if pr_resp.status != 200 or not isinstance(pr_resp.body, Mapping):
        raise ReviewInputError(
            f"failed to fetch PR metadata for {owner}/{repo}#{pull_request} (status={pr_resp.status})",
            diagnostic="v6_pr_metadata_invalid",
        )
    pr_state = pr_resp.body.get("state")
    if isinstance(pr_state, str) and pr_state != "open":
        raise ReviewInputError(
            f"PR {owner}/{repo}#{pull_request} is no longer open (state={pr_state!r})",
            diagnostic="v6_pr_not_open",
        )
    head_info = pr_resp.body.get("head")
    actual_head = head_info.get("sha") if isinstance(head_info, Mapping) else None
    if actual_head != expected_head_sha:
        raise ReviewInputError(
            f"PR head SHA mismatch: expected {expected_head_sha}, got {actual_head}",
            diagnostic="v6_pr_head_mismatch",
        )
    return pr_resp.body


def _format_compare_file_diff(entry: Mapping[str, Any]) -> str:
    """Format a GitHub compare file ``patch`` verbatim into a unified diff file section."""

    filename = str(entry["filename"])
    patch = str(entry["patch"])
    patch_body = patch if patch.endswith("\n") else f"{patch}\n"
    if patch_body.startswith("diff --git "):
        return patch_body
    status = str(entry.get("status") or "modified")
    prev = entry.get("previous_filename")
    old_name = str(prev) if isinstance(prev, str) and prev.strip() else filename
    old_header = "/dev/null" if status == "added" else f"a/{old_name}"
    new_header = "/dev/null" if status == "removed" else f"b/{filename}"
    return (
        f"diff --git a/{old_name} b/{filename}\n"
        f"--- {old_header}\n"
        f"+++ {new_header}\n"
        f"{patch_body}"
    )


def _fetch_compare_and_merge_base(
    *,
    read_client: DualCredentialGitHubClient,
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    event: GitHubEventKind,
    visibility: RepositoryVisibility,
    granted_scopes: Collection[str],
    clock: Callable[[], float],
) -> tuple[str, Mapping[str, Any] | None, bool]:
    """Fetch compare JSON (falling back to ``?per_page=1``) and extract ``merge_base_sha``."""

    compare_path = f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    compare_json: Mapping[str, Any] | None = None
    compare_too_large = False
    try:
        resp = read_client.execute(
            "pr_compare",
            method="GET",
            path=compare_path,
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            now_epoch=clock(),
        )
    except GitHubResponseTooLargeError:
        compare_too_large = True
        resp = read_client.execute(
            "pr_compare",
            method="GET",
            path=f"{compare_path}?per_page=1",
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            now_epoch=clock(),
        )

    if resp.status != 200 or not isinstance(resp.body, Mapping):
        raise ReviewInputError(
            f"failed to fetch compare {base_sha}...{head_sha} (status={resp.status})",
            diagnostic="v6_compare_unavailable",
        )
    if not compare_too_large:
        compare_json = resp.body

    merge_base_info = resp.body.get("merge_base_commit")
    raw_merge_base = (
        merge_base_info.get("sha") if isinstance(merge_base_info, Mapping) else None
    )
    if _is_40_hex(raw_merge_base):
        merge_base_sha = str(raw_merge_base)
    else:
        merge_base_sha = base_sha
    return merge_base_sha, compare_json, compare_too_large


def _fetch_complete_tree(
    *,
    read_client: DualCredentialGitHubClient,
    owner: str,
    repo: str,
    root_sha: str,
    event: GitHubEventKind,
    visibility: RepositoryVisibility,
    granted_scopes: Collection[str],
    clock: Callable[[], float],
) -> dict[str, _TreeEntry]:
    """Fetch complete immutable tree entries for ``root_sha`` via recursive or directory walk."""

    blobs: dict[str, _TreeEntry] = {}
    need_dir_walk = False
    try:
        resp = read_client.execute(
            "git_tree",
            method="GET",
            path=f"/repos/{owner}/{repo}/git/trees/{root_sha}?recursive=1",
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            now_epoch=clock(),
        )
        if resp.status != 200 or not isinstance(resp.body, Mapping):
            raise ReviewInputError(
                f"failed to fetch git tree {root_sha} (status={resp.status})",
                diagnostic="v6_git_tree_unavailable",
            )
        if bool(resp.body.get("truncated", False)):
            need_dir_walk = True
        else:
            raw_tree = resp.body.get("tree")
            if not isinstance(raw_tree, list):
                raise ReviewInputError(
                    f"git tree {root_sha} missing 'tree' list",
                    diagnostic="v6_git_tree_unavailable",
                )
            for item in raw_tree:
                if not isinstance(item, Mapping):
                    continue
                entry_type = str(item.get("type") or "")
                if entry_type == "tree":
                    continue
                path = item.get("path")
                sha = item.get("sha")
                mode = str(item.get("mode") or "100644")
                if isinstance(path, str) and path and _is_40_hex(sha):
                    blobs[path] = _TreeEntry(
                        path=path,
                        sha=str(sha),
                        mode=mode,
                        entry_type=entry_type or "blob",
                    )
            return blobs
    except GitHubResponseTooLargeError:
        need_dir_walk = True

    if not need_dir_walk:
        return blobs

    blobs.clear()
    queue: deque[tuple[str, str]] = deque([("", root_sha)])
    visited_trees: set[tuple[str, str]] = set()
    while queue:
        prefix, tree_sha = queue.popleft()
        if (prefix, tree_sha) in visited_trees:
            continue
        visited_trees.add((prefix, tree_sha))
        resp = read_client.execute(
            "git_tree",
            method="GET",
            path=f"/repos/{owner}/{repo}/git/trees/{tree_sha}",
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            now_epoch=clock(),
        )
        if resp.status != 200 or not isinstance(resp.body, Mapping):
            raise ReviewInputError(
                f"failed to fetch git tree {tree_sha} (status={resp.status})",
                diagnostic="v6_git_tree_unavailable",
            )
        raw_tree = resp.body.get("tree")
        if not isinstance(raw_tree, list):
            raise ReviewInputError(
                f"git tree {tree_sha} missing 'tree' list",
                diagnostic="v6_git_tree_unavailable",
            )
        for item in raw_tree:
            if not isinstance(item, Mapping):
                continue
            entry_type = str(item.get("type") or "")
            name = item.get("path")
            sha = item.get("sha")
            mode = str(item.get("mode") or "100644")
            if not isinstance(name, str) or not name or not _is_40_hex(sha):
                continue
            full_path = f"{prefix}/{name}" if prefix else name
            if entry_type == "tree":
                queue.append((full_path, str(sha)))
            else:
                blobs[full_path] = _TreeEntry(
                    path=full_path,
                    sha=str(sha),
                    mode=mode,
                    entry_type=entry_type or "blob",
                )
    return blobs


def _decode_blob_bytes(body: Any) -> bytes:
    if not isinstance(body, Mapping):
        raise ReviewInputError(
            "git blob payload must be a mapping", diagnostic="v6_git_blob_invalid"
        )
    content = body.get("content")
    encoding = str(body.get("encoding") or "base64").lower()
    if not isinstance(content, str):
        raise ReviewInputError(
            "git blob payload missing content", diagnostic="v6_git_blob_invalid"
        )
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=False)
        except Exception as exc:
            raise ReviewInputError(
                "git blob contains invalid base64", diagnostic="v6_git_blob_invalid"
            ) from exc
    if encoding == "utf-8":
        return content.encode("utf-8")
    raise ReviewInputError(
        f"unsupported git blob encoding {encoding!r}",
        diagnostic="v6_git_blob_invalid",
    )


def _fetch_blob_text_or_binary(
    *,
    read_client: DualCredentialGitHubClient,
    owner: str,
    repo: str,
    sha: str,
    event: GitHubEventKind,
    visibility: RepositoryVisibility,
    granted_scopes: Collection[str],
    clock: Callable[[], float],
) -> tuple[str | None, str | None]:
    """Return ``(decoded_text, None)`` or ``(None, reason)`` for binary/oversized blobs."""

    try:
        resp = read_client.execute(
            "git_blob",
            method="GET",
            path=f"/repos/{owner}/{repo}/git/blobs/{sha}",
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            now_epoch=clock(),
        )
    except GitHubResponseTooLargeError:
        return None, "blob_response_too_large"
    if resp.status != 200:
        raise ReviewInputError(
            f"failed to fetch git blob {sha} (status={resp.status})",
            diagnostic="v6_git_blob_unavailable",
        )
    raw_bytes = _decode_blob_bytes(resp.body)
    if b"\x00" in raw_bytes:
        return None, "binary_blob"
    try:
        return raw_bytes.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "binary_blob"


def _build_complete_split_diff_and_inventory(
    *,
    read_client: DualCredentialGitHubClient,
    owner: str,
    repo: str,
    merge_base_sha: str,
    head_sha: str,
    compare_files_by_path: Mapping[str, Mapping[str, Any]],
    event: GitHubEventKind,
    visibility: RepositoryVisibility,
    granted_scopes: Collection[str],
    clock: Callable[[], float],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Diff complete immutable trees and build per-file diffs + sorted inventory."""

    base_blobs = _fetch_complete_tree(
        read_client=read_client,
        owner=owner,
        repo=repo,
        root_sha=merge_base_sha,
        event=event,
        visibility=visibility,
        granted_scopes=granted_scopes,
        clock=clock,
    )
    head_blobs = _fetch_complete_tree(
        read_client=read_client,
        owner=owner,
        repo=repo,
        root_sha=head_sha,
        event=event,
        visibility=visibility,
        granted_scopes=granted_scopes,
        clock=clock,
    )

    all_paths = sorted(set(base_blobs.keys()) | set(head_blobs.keys()))
    deleted_by_sha: dict[str, list[str]] = {}
    added_by_sha: dict[str, list[str]] = {}
    for path in all_paths:
        b = base_blobs.get(path)
        h = head_blobs.get(path)
        if b is not None and h is None and b.entry_type == "blob":
            deleted_by_sha.setdefault(b.sha, []).append(path)
        elif h is not None and b is None and h.entry_type == "blob":
            added_by_sha.setdefault(h.sha, []).append(path)

    rename_pairs: dict[str, str] = {}
    suppressed_deleted: set[str] = set()
    for sha, deleted_list in deleted_by_sha.items():
        added_list = added_by_sha.get(sha, [])
        if len(deleted_list) == 1 and len(added_list) == 1:
            old_p = deleted_list[0]
            new_p = added_list[0]
            if base_blobs[old_p].mode == head_blobs[new_p].mode:
                rename_pairs[new_p] = old_p
                suppressed_deleted.add(old_p)

    text_diffs: list[str] = []
    inventory: list[dict[str, Any]] = []

    for path in all_paths:
        if path in suppressed_deleted:
            continue
        if path in rename_pairs:
            inventory.append(
                {
                    "path": path,
                    "previous_path": rename_pairs[path],
                    "disposition": "rename_only",
                    "reason": "pure_rename",
                }
            )
            continue

        base_entry = base_blobs.get(path)
        head_entry = head_blobs.get(path)
        if base_entry == head_entry:
            continue

        cmp_entry = compare_files_by_path.get(path)
        if cmp_entry is not None:
            patch = cmp_entry.get("patch")
            if isinstance(patch, str) and patch.strip():
                text_diffs.append(_format_compare_file_diff(cmp_entry))
                inv_item: dict[str, Any] = {
                    "path": path,
                    "disposition": "text",
                    "reason": "compare_patch",
                }
                prev = cmp_entry.get("previous_filename")
                if isinstance(prev, str) and prev.strip():
                    inv_item["previous_path"] = prev
                inventory.append(inv_item)
                continue

        if (
            base_entry is not None
            and (
                base_entry.entry_type != "blob" or base_entry.mode in _SPECIAL_GIT_MODES
            )
        ) or (
            head_entry is not None
            and (
                head_entry.entry_type != "blob" or head_entry.mode in _SPECIAL_GIT_MODES
            )
        ):
            inventory.append(
                {
                    "path": path,
                    "disposition": "unsupported",
                    "reason": "non_blob_or_special_mode",
                }
            )
            continue

        base_text: str | None = None
        if base_entry is not None:
            base_text, err_reason = _fetch_blob_text_or_binary(
                read_client=read_client,
                owner=owner,
                repo=repo,
                sha=base_entry.sha,
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                clock=clock,
            )
            if err_reason is not None:
                disposition = "binary" if err_reason == "binary_blob" else "unsupported"
                inventory.append(
                    {"path": path, "disposition": disposition, "reason": err_reason}
                )
                continue

        head_text: str | None = None
        if head_entry is not None:
            head_text, err_reason = _fetch_blob_text_or_binary(
                read_client=read_client,
                owner=owner,
                repo=repo,
                sha=head_entry.sha,
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                clock=clock,
            )
            if err_reason is not None:
                disposition = "binary" if err_reason == "binary_blob" else "unsupported"
                inventory.append(
                    {"path": path, "disposition": disposition, "reason": err_reason}
                )
                continue

        old_lines = base_text.splitlines() if base_text is not None else []
        new_lines = head_text.splitlines() if head_text is not None else []
        fromfile = "/dev/null" if base_text is None else f"a/{path}"
        tofile = "/dev/null" if head_text is None else f"b/{path}"
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=fromfile,
                tofile=tofile,
                lineterm="",
            )
        )
        if not diff_lines:
            inventory.append(
                {
                    "path": path,
                    "disposition": "unsupported",
                    "reason": "no_line_diff_content",
                }
            )
            continue
        file_diff = f"diff --git a/{path} b/{path}\n" + "\n".join(diff_lines) + "\n"
        text_diffs.append(file_diff)
        inventory.append(
            {
                "path": path,
                "disposition": "text",
                "reason": "blob_unified_diff",
            }
        )

    return text_diffs, inventory


def _split_text_diff_into_review_units(
    file_diff_text: str,
    *,
    snapshot: EvidenceSnapshot,
    batch_max_bytes: int,
    batch_max_hunks: int,
) -> tuple[list[tuple[str, str, int, int]], list[str]]:
    """Split a file diff into bounded units via ``analyze_diff`` and ``subdivide_hunk``."""

    analysis = analyze_diff(file_diff_text)
    units: list[tuple[str, str, int, int]] = []
    oversized_paths: list[str] = []

    for rec in analysis.file_records:
        file_key = rec.canonical_path or rec.old_path or ""
        rec_bytes = len(rec.text.encode("utf-8"))
        if rec_bytes <= batch_max_bytes and len(rec.hunks) <= batch_max_hunks:
            units.append((file_key, rec.text, rec_bytes, max(1, len(rec.hunks))))
            continue

        if not rec.hunks:
            if rec_bytes <= DEFAULT_REVIEW_LIMITS.max_diff_bytes:
                units.append((file_key, rec.text, rec_bytes, 1))
            elif rec.canonical_path is not None:
                oversized_paths.append(rec.canonical_path)
            continue

        for hunk in rec.hunks:
            hunk_body = hunk.text if hunk.text.endswith("\n") else f"{hunk.text}\n"
            single_hunk_diff = f"{rec.header}{hunk_body}"
            single_bytes = len(single_hunk_diff.encode("utf-8"))
            if single_bytes <= batch_max_bytes:
                units.append((file_key, single_hunk_diff, single_bytes, 1))
                continue

            parts = subdivide_hunk(
                rec,
                hunk,
                snapshot,
                admit=lambda part: len(part.patch.encode("utf-8")) <= batch_max_bytes,
            )
            for part in parts:
                part_bytes = len(part.patch.encode("utf-8"))
                if part_bytes <= DEFAULT_REVIEW_LIMITS.max_diff_bytes:
                    units.append((file_key, part.patch, part_bytes, 1))
                elif rec.canonical_path is not None:
                    oversized_paths.append(rec.canonical_path)
    return units, oversized_paths


def _pack_diff_units_into_batches(
    units: Sequence[tuple[str, str, int, int]],
    *,
    batch_max_bytes: int,
    batch_max_files: int,
    batch_max_hunks: int,
) -> list[str]:
    """Greedily pack deterministic units into bounded batches without repeating file headers."""

    batches: list[str] = []
    cur_chunks: list[str] = []
    cur_keys: set[str] = set()
    cur_bytes = 0
    cur_files = 0
    cur_hunks = 0

    for file_key, text, size_bytes, hunk_count in units:
        would_exceed = bool(
            cur_chunks
            and (
                file_key in cur_keys
                or cur_bytes + size_bytes > batch_max_bytes
                or cur_files + 1 > batch_max_files
                or cur_hunks + hunk_count > batch_max_hunks
            )
        )
        if would_exceed:
            batches.append("".join(cur_chunks))
            cur_chunks = []
            cur_keys = set()
            cur_bytes = 0
            cur_files = 0
            cur_hunks = 0
        cur_chunks.append(text)
        cur_keys.add(file_key)
        cur_bytes += size_bytes
        cur_files += 1
        cur_hunks += hunk_count

    if cur_chunks:
        batches.append("".join(cur_chunks))
    return batches


def _merge_batch_results(
    batch_results: Sequence[ReviewResult],
    *,
    provider_name: str,
    model_name: str | None,
    has_unsupported: bool,
) -> ReviewResult:
    """Deterministically deduplicate comments and merge summaries across batches."""

    if not batch_results:
        return ReviewResult(
            summary="No reviewable text diff changes; complete file inventory recorded.",
            comments=(),
            provider=provider_name,
            model=model_name,
            review_status="partial" if has_unsupported else "complete",
        )

    seen_keys: set[tuple[str, int | None, str, str]] = set()
    merged_comments: list[ReviewComment] = []
    summaries: list[str] = []
    all_complete = not has_unsupported

    for res in batch_results:
        if res.review_status != "complete":
            all_complete = False
        trimmed_summary = res.summary.strip()
        if trimmed_summary:
            summaries.append(trimmed_summary)
        for comment in res.comments:
            key = (comment.path, comment.line, comment.side, comment.body.strip())
            if key not in seen_keys:
                seen_keys.add(key)
                merged_comments.append(comment)

    merged_summary = (
        summaries[0]
        if len(summaries) == 1
        else "\n\n".join(summaries)
        if summaries
        else "Review completed."
    )
    return ReviewResult(
        summary=merged_summary,
        comments=tuple(merged_comments),
        provider=provider_name,
        model=model_name,
        review_status="complete" if all_complete else "partial",
    )


def _load_trusted_configuration(policy_root: Path | None) -> Any:
    """Load repository policy strictly from ``policy_root`` (never untrusted PR head)."""

    if policy_root is None:
        return default_configuration()
    if policy_root.is_symlink() or not policy_root.is_dir():
        raise ReviewInputError(
            f"trusted policy root must be an existing non-symlink directory: {policy_root}",
            diagnostic="v6_untrusted_policy_root",
        )
    config_file = policy_root / DEFAULT_CONFIG_FILENAME
    if config_file.is_symlink():
        raise ReviewInputError(
            f"policy configuration must not be a symbolic link: {config_file}",
            diagnostic="v6_untrusted_policy_root",
        )
    if config_file.exists():
        return load_configuration(config_file)
    return default_configuration(root=policy_root)


def _review_result_to_unresolved_payload(
    result: ReviewResult,
    *,
    diff_sha256: str,
    file_inventory: Sequence[Mapping[str, Any]] = (),
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str]]:
    """Convert ``ReviewResult`` into lossless ``UnresolvedResultRecord`` payload and digests."""

    review_body = render_review_markdown(result).strip()
    inline_comments: list[dict[str, Any]] = []
    for comment in result.comments:
        if comment.line is not None and comment.side in {"LEFT", "RIGHT"}:
            inline_comments.append(
                {
                    "path": comment.path,
                    "line": comment.line,
                    "side": comment.side,
                    "body": comment.body,
                }
            )
    findings = [comment.to_dict() for comment in result.comments]
    envelope: dict[str, Any] = {
        "_v6_review_envelope": "reviewsensei:v6:review_envelope:v1",
        "review_body": review_body,
        "event": "COMMENT",
        "comments": inline_comments,
        "findings": findings,
        "file_inventory": [dict(entry) for entry in file_inventory],
        "provider": result.provider,
        "model": result.model,
        "review_status": result.review_status,
        "content_digest": result.content_digest(),
    }
    return (envelope,), (f"diff:sha256:{diff_sha256}",)


def run_v6_fresh_pr_analysis(
    *,
    owner: str,
    repo: str,
    pull_request: int,
    head_sha: str,
    workflow_token_transport: DualCredentialTransport,
    event: GitHubEventKind = GitHubEventKind.PULL_REQUEST,
    visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC,
    granted_scopes: Collection[str] = (),
    policy_root: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    provider_base_url: str | None = None,
    allow_custom_endpoint: bool = False,
    is_ghec: bool = False,
    clock: Callable[[], float] = time.time,
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES,
    batch_max_files: int = DEFAULT_BATCH_MAX_FILES,
    batch_max_hunks: int = DEFAULT_BATCH_MAX_HUNKS,
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str]]:
    """Fetch immutable ``merge_base...head`` diff + complete inventory, batch review, and return v6 payload."""

    read_client = DualCredentialGitHubClient(
        workflow_token_transport=workflow_token_transport,
        broker_app_transport=_ReadOnlyAppTransport(),
        is_ghec=is_ghec,
    )

    pr_body = verify_live_pr_head(
        owner=owner,
        repo=repo,
        pull_request=pull_request,
        expected_head_sha=head_sha,
        workflow_token_transport=workflow_token_transport,
        event=event,
        visibility=visibility,
        granted_scopes=granted_scopes,
        is_ghec=is_ghec,
        clock=clock,
    )
    raw_title = pr_body.get("title")
    pr_title = raw_title if isinstance(raw_title, str) and raw_title.strip() else None
    base_info = pr_body.get("base")
    raw_base_sha = base_info.get("sha") if isinstance(base_info, Mapping) else None
    if not _is_40_hex(raw_base_sha):
        raise ReviewInputError(
            f"PR metadata missing 40-hex base.sha for {owner}/{repo}#{pull_request}",
            diagnostic="v6_pr_metadata_invalid",
        )
    base_sha = str(raw_base_sha)

    merge_base_sha, compare_json, compare_too_large = _fetch_compare_and_merge_base(
        read_client=read_client,
        owner=owner,
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        event=event,
        visibility=visibility,
        granted_scopes=granted_scopes,
        clock=clock,
    )

    compare_files_by_path: dict[str, Mapping[str, Any]] = {}
    compare_files_complete = False
    if not compare_too_large and compare_json is not None:
        raw_files = compare_json.get("files")
        if (
            isinstance(raw_files, list)
            and len(raw_files) < _COMPARE_MAX_FILES_THRESHOLD
        ):
            all_have_patch = True
            for entry in raw_files:
                if not isinstance(entry, Mapping):
                    all_have_patch = False
                    continue
                fname = entry.get("filename")
                if isinstance(fname, str) and fname.strip():
                    compare_files_by_path[fname] = entry
                patch = entry.get("patch")
                if not isinstance(patch, str) or not patch.strip():
                    all_have_patch = False
            compare_files_complete = all_have_patch

    text_diffs: list[str] = []
    file_inventory: list[dict[str, Any]] = []
    used_fast_compare_diff = False

    if compare_files_complete:
        try:
            diff_resp = read_client.execute(
                "pr_compare",
                method="GET",
                path=f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
                headers={"Accept": "application/vnd.github.v3.diff"},
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                now_epoch=clock(),
            )
            if (
                diff_resp.status == 200
                and isinstance(diff_resp.body, str)
                and diff_resp.body.strip()
            ):
                parsed_fast = analyze_diff(diff_resp.body)
                for rec in parsed_fast.file_records:
                    text_diffs.append(rec.text)
                for fname in sorted(compare_files_by_path.keys()):
                    entry = compare_files_by_path[fname]
                    inv_row: dict[str, Any] = {
                        "path": fname,
                        "disposition": "text",
                        "reason": "compare_patch",
                    }
                    prev = entry.get("previous_filename")
                    if isinstance(prev, str) and prev.strip():
                        inv_row["previous_path"] = prev
                    file_inventory.append(inv_row)
                used_fast_compare_diff = True
        except GitHubResponseTooLargeError:
            used_fast_compare_diff = False

    if not used_fast_compare_diff:
        text_diffs, file_inventory = _build_complete_split_diff_and_inventory(
            read_client=read_client,
            owner=owner,
            repo=repo,
            merge_base_sha=merge_base_sha,
            head_sha=head_sha,
            compare_files_by_path=compare_files_by_path,
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            clock=clock,
        )

    if not text_diffs and not file_inventory:
        raise ReviewInputError(
            f"PR diff is empty or unreadable for {owner}/{repo}#{pull_request}",
            diagnostic="v6_empty_pr_diff",
        )

    configuration = _load_trusted_configuration(policy_root)
    filtered_env = {k: os.environ[k] for k in _ALLOWED_ENV_KEYS if k in os.environ}
    plan = plan_hosted_execution(
        configuration,
        cli_provider=provider,
        cli_model=model,
        environ=filtered_env,
    )
    adapter_name = _BACKEND_ADAPTERS.get(plan.backend)
    if adapter_name is None:
        raise ReviewInputError(
            f"unsupported backend {plan.backend!r} for v6 fresh analysis",
            diagnostic="v6_unsupported_provider_backend",
        )
    effective_base_url = (
        provider_base_url.strip()
        if isinstance(provider_base_url, str) and provider_base_url.strip()
        else plan.base_url
    )
    effective_allow_custom = bool(allow_custom_endpoint) or plan.allow_custom_endpoint
    if adapter_name == "openai-compatible":
        try:
            validate_openai_compatible_endpoint(
                effective_base_url, allow_custom_endpoint=effective_allow_custom
            )
        except ValueError as exc:
            raise ReviewInputError(
                str(exc), diagnostic="v6_invalid_provider_endpoint"
            ) from exc

    cred_env = plan.credential_env
    api_key = os.environ.get(cred_env, "").strip() or None if cred_env else None
    if plan.credential_required and not api_key:
        raise ReviewInputError(
            f"environment variable {cred_env} is unavailable",
            diagnostic="v6_missing_provider_credential",
        )
    if not plan.credential_required:
        api_key = None

    settings = ProviderSettings(
        name=adapter_name,
        model=plan.model,
        base_url=effective_base_url,
        api_key=api_key,
        timeout_seconds=plan.timeout_seconds,
        allow_custom_endpoint=effective_allow_custom,
        openrouter_policy=(
            OpenRouterRoutingPolicy(plan.upstream_provider)
            if plan.backend == "openrouter" and plan.upstream_provider is not None
            else None
        ),
    )
    review_provider, stage_providers = bind_stage_providers(
        registry=default_registry(),
        settings=settings,
        stages=DEFAULT_STAGES,
    )
    service = ReviewService(
        review_provider,
        stages=DEFAULT_STAGES,
        stage_providers=stage_providers,
        work_budgets=configuration.advanced.review_work,
    )

    snapshot = EvidenceSnapshot(
        repository=f"{owner}/{repo}",
        pull_request=pull_request,
        base_sha=merge_base_sha,
        head_sha=head_sha,
    )
    review_units: list[tuple[str, str, int, int]] = []
    oversized_paths: set[str] = set()
    for file_diff_text in text_diffs:
        units, unsplit = _split_text_diff_into_review_units(
            file_diff_text,
            snapshot=snapshot,
            batch_max_bytes=batch_max_bytes,
            batch_max_hunks=batch_max_hunks,
        )
        review_units.extend(units)
        oversized_paths.update(unsplit)

    if oversized_paths:
        for entry in file_inventory:
            if entry.get("path") in oversized_paths:
                entry["disposition"] = "unsupported"
                entry["reason"] = "single_line_exceeds_review_limit"

    batches = _pack_diff_units_into_batches(
        review_units,
        batch_max_bytes=batch_max_bytes,
        batch_max_files=batch_max_files,
        batch_max_hunks=batch_max_hunks,
    )
    batch_results: list[ReviewResult] = []
    for batch_diff in batches:
        batch_results.append(
            service.review(
                ReviewRequest(
                    diff=batch_diff,
                    repository=f"{owner}/{repo}",
                    pull_request_number=pull_request,
                    title=pr_title,
                    base_sha=merge_base_sha,
                    head_sha=head_sha,
                    model=settings.model,
                )
            )
        )

    has_unsupported = any(
        entry.get("disposition") == "unsupported" for entry in file_inventory
    )
    merged_result = _merge_batch_results(
        batch_results,
        provider_name=settings.name,
        model_name=settings.model,
        has_unsupported=has_unsupported,
    )
    combined_text_diff = "".join(text_diffs)
    diff_sha256 = hashlib.sha256(combined_text_diff.encode("utf-8")).hexdigest()
    return _review_result_to_unresolved_payload(
        merged_result,
        diff_sha256=diff_sha256,
        file_inventory=file_inventory,
    )
