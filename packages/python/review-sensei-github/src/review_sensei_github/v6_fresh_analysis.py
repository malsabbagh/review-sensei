"""Lazy fresh-PR ReviewSensei analysis for v6 pilot runs under active write leases."""

from __future__ import annotations

import base64
import difflib
import hashlib
import heapq
import os
import time
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from review_sensei.budgets import EffectiveWorkBudget
from review_sensei.configuration import (
    DEFAULT_CONFIG_FILENAME,
    ProductConfiguration,
    default_configuration,
    load_configuration,
)
from review_sensei.convergence import resolve_review_convergence_policy
from review_sensei.diff import DiffFileRecord, DiffHunk, analyze_diff
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceSnapshot
from review_sensei.hosted import HostedPlan, plan_hosted_execution
from review_sensei.hunk_subdivision import subdivide_hunk
from review_sensei.models import (
    ReviewComment,
    ReviewRequest,
    ReviewResult,
    ReviewTransaction,
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.presentation import render_review_markdown
from review_sensei.providers.base import (
    ProviderRequest,
    ProviderResponse,
    ReviewProvider,
)
from review_sensei.providers.openai_compatible import (
    validate_openai_compatible_endpoint,
)
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.providers.registry import ProviderSettings, default_registry
from review_sensei.providers.routing import bind_stage_providers
from review_sensei.service import (
    DEFAULT_STAGES,
    ReviewService,
    review_work_policy_digest,
)
from review_sensei.validation import (
    DEFAULT_REVIEW_LIMITS,
    DEFAULT_TOTAL_WORK_MAX_PROVIDER_CALLS,
)

from .errors import GitHubHTTPError, GitHubResponseTooLargeError
from .v6_credential_matrix import (
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
)
from .v6_recovery_ledger import canonical_json_bytes, canonical_sha256
from .v6_transport import DualCredentialGitHubClient, DualCredentialTransport

_REAL_REVIEW_SERVICE = ReviewService

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
_MAX_LCA_COMMIT_FETCHES = 256

_LCA_PARENT1 = 1
_LCA_PARENT2 = 2
_LCA_STALE = 4
_LCA_RESULT = 8


class V6FreshAnalysisError(ReviewInputError):
    """Fail-closed error raised during v6 fresh PR ancestry, diff, or batch planning."""

    def __init__(self, diagnostic: str, message: str = "") -> None:
        super().__init__(message or diagnostic, diagnostic=diagnostic)


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


class _NoCallSizingProvider(ReviewProvider):
    """Zero-network provider placeholder used only to run ``ReviewService._format_prompt``."""

    name = "sizing"
    model: str | None = None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("_NoCallSizingProvider must never execute model calls")


@dataclass(frozen=True)
class _TreeEntry:
    path: str
    sha: str
    mode: str
    entry_type: str


@dataclass(frozen=True)
class V6DiffUnit:
    """Single hunk or subdivided sub-hunk unit ready for prompt-budgeted batching."""

    file_path: str
    hunk_id: str
    parent_hunk_id: str
    sub_hunk_index: int
    sub_hunk_count: int
    header: str
    hunk_body: str
    patch_text: str
    is_fragment: bool
    group_key: tuple[str, str, int, str, int, int]


@dataclass(frozen=True)
class V6PlannedBatch:
    """One deterministic, prompt-budgeted batch covering exact primary hunk IDs."""

    batch_id: str
    diff_text: str
    diff_sha256: str
    prompt_bytes: int
    paths: tuple[str, ...]
    primary_hunk_ids: tuple[str, ...]
    parent_hunk_ids: tuple[str, ...]

    def to_plan_dict(self) -> dict[str, Any]:
        """Compact JSON-compatible representation stored inside ``BatchPlanRecord``."""

        return {
            "batch_id": self.batch_id,
            "primary_hunk_ids": list(self.primary_hunk_ids),
            "parent_hunk_ids": list(self.parent_hunk_ids),
            "paths": list(self.paths),
            "diff_sha256": self.diff_sha256,
            "prompt_bytes": self.prompt_bytes,
        }


@dataclass(frozen=True)
class V6FreshAnalysisContext:
    """Complete immutable PR diff context and restartable batch execution plan."""

    owner: str
    repo: str
    pull_request: int
    pr_title: str | None
    base_sha: str
    merge_base_sha: str
    head_sha: str
    inventory_sha256: str
    inventory: tuple[dict[str, Any], ...]
    non_text_changes: tuple[dict[str, Any], ...]
    policy_digest: str
    configuration_digest: str
    max_provider_calls: int
    planned_batches: tuple[V6PlannedBatch, ...]
    all_expected_hunk_ids: tuple[str, ...]
    sub_hunks_by_parent: Mapping[str, tuple[str, ...]]
    combined_diff_sha256: str
    provider_name: str
    model_name: str | None
    service: Any
    instructions: str | None = None
    has_unsupported: bool = False


def compute_trusted_policy_and_config_digests(
    configuration: ProductConfiguration,
    *,
    provider_name: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    allow_custom_endpoint: bool = False,
    upstream_provider: str | None = None,
    timeout_seconds: float | None = 120.0,
) -> tuple[str, str]:
    """Compute real 64-hex ``(policy_digest, configuration_digest)`` from trusted config."""

    resolved_plan: HostedPlan | None = None
    effective_model: str | None
    effective_base_url: str | None
    effective_timeout: float | None
    if provider_name is None:
        resolved_plan = plan_hosted_execution(configuration, environ={})
        effective_provider = _BACKEND_ADAPTERS.get(
            resolved_plan.backend, resolved_plan.backend
        )
        effective_model = model_name or resolved_plan.model
        effective_base_url = base_url or resolved_plan.base_url
        effective_allow_custom = (
            bool(allow_custom_endpoint) or resolved_plan.allow_custom_endpoint
        )
        effective_upstream = upstream_provider or resolved_plan.upstream_provider
        effective_timeout = resolved_plan.timeout_seconds
    else:
        effective_provider = provider_name
        effective_model = model_name
        effective_base_url = base_url
        effective_allow_custom = bool(allow_custom_endpoint)
        effective_upstream = upstream_provider
        effective_timeout = timeout_seconds

    mode_arg = "advisory" if configuration.github.reviews == "advisory" else None
    convergence_policy = resolve_review_convergence_policy(
        mode=mode_arg,
        quality=configuration.review.quality,
    )
    policy_payload: dict[str, Any] = {
        "convergence": convergence_policy.identity_fields(),
        "github": {
            "automatic_reviews": configuration.github.automatic_reviews,
            "writes": configuration.github.writes,
            "reviews": configuration.github.reviews,
            "mentions": configuration.github.mentions,
            "learning": configuration.github.learning,
            "artifacts": configuration.github.artifacts,
            "operation_entry": configuration.github.operation_entry,
            "publication_inventory": configuration.github.publication_inventory,
        },
        "review_quality": configuration.review.quality,
    }
    policy_digest = canonical_sha256(policy_payload)

    max_calls = (
        configuration.advanced.resources.max_provider_calls
        or DEFAULT_TOTAL_WORK_MAX_PROVIDER_CALLS
    )
    resource_budget = ResourceBudget.create(
        limits=DEFAULT_REVIEW_LIMITS,
        max_provider_calls=max_calls,
    )
    work_policy_digest = review_work_policy_digest(
        configuration.advanced.review_work,
        resource_budget,
        None,
        decision=configuration.decision,
        generated_mirrors=configuration.advanced.generated_mirrors,
    )
    stage_identity, category_policy = transaction_stage_identity(DEFAULT_STAGES)
    provider_identity: dict[str, object] = {
        "name": effective_provider,
        "profile": None,
        "base_url": effective_base_url,
        "timeout_seconds": (
            float(effective_timeout) if effective_timeout is not None else 120.0
        ),
        "max_output_tokens": None,
        "allow_custom_endpoint": effective_allow_custom,
        "openrouter_policy": (
            {"schema_version": 1, "upstream_provider": effective_upstream}
            if effective_provider == "openrouter" and effective_upstream is not None
            else None
        ),
    }
    config_context = build_transaction_configuration_context(
        provider=provider_identity,
        model=effective_model,
        stages=stage_identity,
        category_policy=category_policy,
        publication_mode=convergence_policy.mode,
        orchestration_enabled=True,
        work_policy_digest=work_policy_digest,
    )
    configuration_digest = ReviewTransaction.compute_configuration_digest(
        config_context
    )
    return policy_digest, configuration_digest


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


def _parse_commit_timestamp(body: Mapping[str, Any]) -> float:
    """Extract Git commit timestamp (committer date, then author date) as epoch seconds."""

    for section_key in ("committer", "author"):
        section = body.get(section_key)
        if not isinstance(section, Mapping):
            continue
        raw_date = section.get("date")
        if not isinstance(raw_date, str) or not raw_date.strip():
            continue
        normalized = raw_date.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return 0.0


def _resolve_merge_base_via_git_commits(
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
) -> str:
    """Resolve exact Git merge base via ``paint_down_to_common`` + ``remove_redundant``.

    Orders active traversal by commit timestamp descending, propagates
    ``PARENT1``/``PARENT2``/``STALE``/``RESULT`` flags, stops only once all
    active queue items are ``STALE``, and fails closed with
    ``V6FreshAnalysisError("v6_merge_base_ancestry_incomplete", ...)`` if any
    parent fetch fails or the fetch cap is exhausted.
    """

    if base_sha == head_sha:
        return base_sha

    parents_cache: dict[str, tuple[str, ...]] = {}
    commit_ts: dict[str, float] = {}
    fetches = 0

    def _get_commit(commit_sha: str) -> tuple[tuple[str, ...], float]:
        nonlocal fetches
        if commit_sha in parents_cache:
            return parents_cache[commit_sha], commit_ts[commit_sha]
        if fetches >= _MAX_LCA_COMMIT_FETCHES:
            raise V6FreshAnalysisError(
                "v6_merge_base_ancestry_incomplete",
                f"commit ancestry fetch cap ({_MAX_LCA_COMMIT_FETCHES}) exceeded at {commit_sha}",
            )
        fetches += 1
        try:
            resp = read_client.execute(
                "git_commit",
                method="GET",
                path=f"/repos/{owner}/{repo}/git/commits/{commit_sha}",
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                now_epoch=clock(),
            )
        except (GitHubResponseTooLargeError, GitHubHTTPError) as exc:
            raise V6FreshAnalysisError(
                "v6_merge_base_ancestry_incomplete",
                f"failed to fetch commit {commit_sha} during merge-base resolution: {exc}",
            ) from exc
        if resp.status != 200 or not isinstance(resp.body, Mapping):
            raise V6FreshAnalysisError(
                "v6_merge_base_ancestry_incomplete",
                f"invalid git commit response for {commit_sha} (status={resp.status})",
            )
        raw_parents = resp.body.get("parents")
        if not isinstance(raw_parents, Sequence) or isinstance(
            raw_parents, (str, bytes, bytearray)
        ):
            raise V6FreshAnalysisError(
                "v6_merge_base_ancestry_incomplete",
                f"git commit {commit_sha} missing valid 'parents' sequence",
            )
        parsed: list[str] = []
        for entry in raw_parents:
            psha = entry.get("sha") if isinstance(entry, Mapping) else entry
            if not _is_40_hex(psha):
                raise V6FreshAnalysisError(
                    "v6_merge_base_ancestry_incomplete",
                    f"git commit {commit_sha} contains invalid parent SHA {psha!r}",
                )
            parsed.append(str(psha))
        ts = _parse_commit_timestamp(resp.body)
        result = tuple(parsed)
        parents_cache[commit_sha] = result
        commit_ts[commit_sha] = ts
        return result, ts

    flags: dict[str, int] = {}
    heap: list[tuple[float, int, str]] = []
    in_queue: set[str] = set()
    push_seq = 0

    def _push(sha: str, ts: float) -> None:
        nonlocal push_seq
        if sha in in_queue:
            return
        push_seq += 1
        in_queue.add(sha)
        heapq.heappush(heap, (-ts, push_seq, sha))

    def _queue_has_non_stale() -> bool:
        return any(
            (flags.get(sha, 0) & _LCA_STALE) == 0
            for _, _, sha in heap
            if sha in in_queue
        )

    _, base_ts = _get_commit(base_sha)
    _, head_ts = _get_commit(head_sha)
    flags[base_sha] = _LCA_PARENT1
    flags[head_sha] = _LCA_PARENT2
    _push(base_sha, base_ts)
    _push(head_sha, head_ts)

    result_candidates: list[str] = []
    while _queue_has_non_stale():
        _, _, cur_sha = heapq.heappop(heap)
        if cur_sha not in in_queue:
            continue
        in_queue.remove(cur_sha)

        cur_flags = flags.get(cur_sha, 0)
        if (cur_flags & (_LCA_PARENT1 | _LCA_PARENT2)) == (_LCA_PARENT1 | _LCA_PARENT2):
            if not (cur_flags & _LCA_RESULT):
                flags[cur_sha] = cur_flags | _LCA_RESULT
                result_candidates.append(cur_sha)
            prop_flags = _LCA_PARENT1 | _LCA_PARENT2 | _LCA_STALE
        else:
            prop_flags = cur_flags & (_LCA_PARENT1 | _LCA_PARENT2 | _LCA_STALE)

        parents, _ = _get_commit(cur_sha)
        for psha in parents:
            pflags = flags.get(psha, 0)
            if (pflags & prop_flags) == prop_flags:
                continue
            flags[psha] = pflags | prop_flags
            _, pts = _get_commit(psha)
            _push(psha, pts)

    if not result_candidates:
        raise V6FreshAnalysisError(
            "v6_merge_base_unavailable",
            f"unable to resolve merge base commit for {base_sha}...{head_sha}",
        )

    # remove_redundant: filter out any common ancestor that is a strict ancestor of another.
    if len(result_candidates) == 1:
        return result_candidates[0]

    min_cand_ts = min(commit_ts.get(c, 0.0) for c in result_candidates)
    strict_ancestors: set[str] = set()
    for cand in result_candidates:
        cand_parents, _ = _get_commit(cand)
        walk: deque[str] = deque(cand_parents)
        seen_walk: set[str] = set()
        while walk:
            anc = walk.popleft()
            if anc in seen_walk:
                continue
            seen_walk.add(anc)
            strict_ancestors.add(anc)
            anc_parents, anc_ts = _get_commit(anc)
            if anc_ts >= min_cand_ts or anc_ts == 0.0:
                for gp in anc_parents:
                    if gp not in seen_walk:
                        walk.append(gp)

    maximal = [
        c for c in result_candidates if c not in strict_ancestors
    ] or result_candidates
    return max(maximal, key=lambda c: (commit_ts.get(c, 0.0), c))


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
    """Fetch compare JSON or resolve ``merge_base_sha`` via page=2 or commit-graph LCA."""

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
        if resp.status == 200 and isinstance(resp.body, Mapping):
            compare_json = resp.body
            merge_base_info = resp.body.get("merge_base_commit")
            raw_merge_base = (
                merge_base_info.get("sha")
                if isinstance(merge_base_info, Mapping)
                else None
            )
            if _is_40_hex(raw_merge_base):
                return str(raw_merge_base), compare_json, False
        compare_too_large = True
    except (GitHubResponseTooLargeError, GitHubHTTPError):
        compare_too_large = True

    # First fallback: Compare page=2 omits files[] while still returning merge_base_commit.sha.
    try:
        page2_resp = read_client.execute(
            "pr_compare",
            method="GET",
            path=f"{compare_path}?per_page=1&page=2",
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            now_epoch=clock(),
        )
        if page2_resp.status == 200 and isinstance(page2_resp.body, Mapping):
            mb2_info = page2_resp.body.get("merge_base_commit")
            raw_mb2 = mb2_info.get("sha") if isinstance(mb2_info, Mapping) else None
            if _is_40_hex(raw_mb2):
                return str(raw_mb2), None, compare_too_large
    except (GitHubResponseTooLargeError, GitHubHTTPError):
        pass

    # Second fallback: Git paint_down_to_common over immutable git_commit objects.
    merge_base_sha = _resolve_merge_base_via_git_commits(
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
    return merge_base_sha, None, compare_too_large


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
            raise V6FreshAnalysisError(
                "v6_diff_inventory_incomplete",
                f"failed to fetch git tree {root_sha} (status={resp.status})",
            )
        if bool(resp.body.get("truncated", False)):
            need_dir_walk = True
        else:
            raw_tree = resp.body.get("tree")
            if not isinstance(raw_tree, list):
                raise V6FreshAnalysisError(
                    "v6_diff_inventory_incomplete",
                    f"git tree {root_sha} missing 'tree' list",
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
    except GitHubHTTPError as exc:
        raise V6FreshAnalysisError(
            "v6_diff_inventory_incomplete",
            f"failed to fetch git tree {root_sha}: {exc}",
        ) from exc

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
        try:
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
        except (GitHubResponseTooLargeError, GitHubHTTPError) as exc:
            raise V6FreshAnalysisError(
                "v6_diff_inventory_incomplete",
                f"failed to fetch git directory tree {tree_sha}: {exc}",
            ) from exc
        if resp.status != 200 or not isinstance(resp.body, Mapping):
            raise V6FreshAnalysisError(
                "v6_diff_inventory_incomplete",
                f"failed to fetch git tree {tree_sha} (status={resp.status})",
            )
        raw_tree = resp.body.get("tree")
        if not isinstance(raw_tree, list):
            raise V6FreshAnalysisError(
                "v6_diff_inventory_incomplete",
                f"git tree {tree_sha} missing 'tree' list",
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
        raise V6FreshAnalysisError(
            "v6_diff_inventory_incomplete", "git blob payload must be a mapping"
        )
    content = body.get("content")
    encoding = str(body.get("encoding") or "base64").lower()
    if not isinstance(content, str):
        raise V6FreshAnalysisError(
            "v6_diff_inventory_incomplete", "git blob payload missing content"
        )
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=False)
        except Exception as exc:
            raise V6FreshAnalysisError(
                "v6_diff_inventory_incomplete", "git blob contains invalid base64"
            ) from exc
    if encoding == "utf-8":
        return content.encode("utf-8")
    raise V6FreshAnalysisError(
        "v6_diff_inventory_incomplete",
        f"unsupported git blob encoding {encoding!r}",
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
    except GitHubHTTPError as exc:
        raise V6FreshAnalysisError(
            "v6_diff_inventory_incomplete",
            f"failed to fetch git blob {sha}: {exc}",
        ) from exc
    if resp.status != 200:
        raise V6FreshAnalysisError(
            "v6_diff_inventory_incomplete",
            f"failed to fetch git blob {sha} (status={resp.status})",
        )
    raw_bytes = _decode_blob_bytes(resp.body)
    if b"\x00" in raw_bytes:
        return None, "binary_blob"
    try:
        return raw_bytes.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "binary_blob"


def _compute_hunk_id(path: str, hunk: DiffHunk) -> str:
    """Return deterministic stable identifier for one unified diff hunk."""

    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "path": path,
                "index": hunk.index,
                "old_start": hunk.old_start,
                "old_count": hunk.old_count,
                "new_start": hunk.new_start,
                "new_count": hunk.new_count,
                "text": hunk.text,
            }
        )
    ).hexdigest()[:16]
    return (
        f"{path}#h{hunk.index}:"
        f"{hunk.old_start},{hunk.old_count}+{hunk.new_start},{hunk.new_count}:{digest}"
    )


def _diff_reconstructs_head(
    base_text: str | None,
    head_text: str | None,
    file_rec: DiffFileRecord,
) -> bool:
    """Verify ``file_rec`` hunks reconstruct ``head_text`` line-for-line from ``base_text``."""

    if file_rec.binary:
        return False
    base_lines = base_text.splitlines() if base_text is not None else []
    head_lines = head_text.splitlines() if head_text is not None else []
    if base_text is None:
        base_lines = []
    if head_text is None:
        head_lines = []

    if not file_rec.hunks:
        return base_lines == head_lines

    reconstructed: list[str] = []
    old_cursor = 0  # 0-based index into base_lines
    for hunk in file_rec.hunks:
        target_start = (
            0 if hunk.old_count == 0 and hunk.old_start == 0 else hunk.old_start - 1
        )
        if hunk.old_count == 0 and hunk.old_start > 0:
            target_start = hunk.old_start
        if target_start < old_cursor or target_start > len(base_lines):
            return False
        reconstructed.extend(base_lines[old_cursor:target_start])
        old_cursor = target_start

        lines = hunk.text.split("\n")[1:]
        for line in lines:
            if not line:
                continue
            if line.startswith("\\"):
                continue
            prefix = line[0]
            content = line[1:]
            if prefix == " ":
                if old_cursor >= len(base_lines) or base_lines[old_cursor] != content:
                    return False
                reconstructed.append(content)
                old_cursor += 1
            elif prefix == "-":
                if old_cursor >= len(base_lines) or base_lines[old_cursor] != content:
                    return False
                old_cursor += 1
            elif prefix == "+":
                reconstructed.append(content)
            else:
                return False

    reconstructed.extend(base_lines[old_cursor:])
    return reconstructed == head_lines


def _build_authoritative_inventory_and_diffs(
    *,
    read_client: DualCredentialGitHubClient,
    owner: str,
    repo: str,
    merge_base_sha: str,
    head_sha: str,
    compare_files_by_path: Mapping[str, Mapping[str, Any]],
    compare_diff_records_by_path: Mapping[str, DiffFileRecord],
    event: GitHubEventKind,
    visibility: RepositoryVisibility,
    granted_scopes: Collection[str],
    clock: Callable[[], float],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Build authoritative tree/blob inventory and reconcile Compare fast-path against it."""

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

    pure_rename_pairs: dict[str, str] = {}
    suppressed_deleted: set[str] = set()
    for sha, deleted_list in deleted_by_sha.items():
        added_list = added_by_sha.get(sha, [])
        if len(deleted_list) == 1 and len(added_list) == 1:
            old_p = deleted_list[0]
            new_p = added_list[0]
            if base_blobs[old_p].mode == head_blobs[new_p].mode:
                pure_rename_pairs[new_p] = old_p
                suppressed_deleted.add(old_p)

    # Pair renames with edits only when trees prove old_p was removed and new_p was added.
    modified_rename_pairs: dict[str, str] = {}
    for new_p, compare_meta in compare_files_by_path.items():
        if str(compare_meta.get("status") or "") != "renamed":
            continue
        prev = compare_meta.get("previous_filename")
        if (
            isinstance(prev, str)
            and prev
            and prev in base_blobs
            and prev not in head_blobs
            and prev not in suppressed_deleted
            and new_p in head_blobs
            and new_p not in base_blobs
            and new_p not in pure_rename_pairs
        ):
            modified_rename_pairs[new_p] = prev
            suppressed_deleted.add(prev)

    text_diffs: list[str] = []
    inventory: list[dict[str, Any]] = []

    for path in all_paths:
        if path in suppressed_deleted:
            continue
        if path in pure_rename_pairs:
            inventory.append(
                {
                    "path": path,
                    "previous_path": pure_rename_pairs[path],
                    "status": "rename_only",
                    "disposition": "rename_only",
                    "reason": "pure_rename",
                    "hunk_ids": [],
                }
            )
            continue

        if path in modified_rename_pairs:
            prev_path = modified_rename_pairs[path]
            base_entry: _TreeEntry | None = base_blobs.get(prev_path)
            head_entry: _TreeEntry | None = head_blobs.get(path)
            expected_status = "renamed"
        else:
            prev_path = None
            base_entry = base_blobs.get(path)
            head_entry = head_blobs.get(path)
            if base_entry == head_entry:
                continue
            if base_entry is None:
                expected_status = "added"
            elif head_entry is None:
                expected_status = "removed"
            else:
                expected_status = "modified"

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
                    "status": "unsupported",
                    "disposition": "unsupported",
                    "reason": "non_blob_or_special_mode",
                    "hunk_ids": [],
                }
            )
            continue

        if (
            base_entry is not None
            and head_entry is not None
            and base_entry.sha == head_entry.sha
            and base_entry.mode != head_entry.mode
        ):
            inventory.append(
                {
                    "path": path,
                    "status": "unsupported",
                    "disposition": "unsupported",
                    "reason": "mode_change_only",
                    "hunk_ids": [],
                }
            )
            continue

        base_text: str | None = None
        base_err: str | None = None
        if base_entry is not None:
            base_text, base_err = _fetch_blob_text_or_binary(
                read_client=read_client,
                owner=owner,
                repo=repo,
                sha=base_entry.sha,
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                clock=clock,
            )
            if base_err == "binary_blob":
                inventory.append(
                    {
                        "path": path,
                        "status": "binary",
                        "disposition": "binary",
                        "reason": base_err,
                        "hunk_ids": [],
                        **({"previous_path": prev_path} if prev_path else {}),
                    }
                )
                continue

        head_text: str | None = None
        head_err: str | None = None
        if head_entry is not None and base_err is None:
            head_text, head_err = _fetch_blob_text_or_binary(
                read_client=read_client,
                owner=owner,
                repo=repo,
                sha=head_entry.sha,
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                clock=clock,
            )
            if head_err == "binary_blob":
                inventory.append(
                    {
                        "path": path,
                        "status": "binary",
                        "disposition": "binary",
                        "reason": head_err,
                        "hunk_ids": [],
                        **({"previous_path": prev_path} if prev_path else {}),
                    }
                )
                continue

        cmp_rec = compare_diff_records_by_path.get(path)
        cmp_entry: Mapping[str, Any] | None = compare_files_by_path.get(path)
        if base_err is not None or head_err is not None:
            # Oversized blob: allow verified Compare patch if available, otherwise account as unsupported.
            if cmp_rec is not None and not cmp_rec.binary and cmp_rec.hunks:
                hunk_ids = [_compute_hunk_id(path, h) for h in cmp_rec.hunks]
                text_diffs.append(cmp_rec.text)
                inventory.append(
                    {
                        "path": path,
                        "status": expected_status,
                        "disposition": "text",
                        "reason": "compare_patch_oversized_blob",
                        "hunk_ids": hunk_ids,
                        **({"previous_path": prev_path} if prev_path else {}),
                    }
                )
                continue
            err_reason = base_err or head_err or "blob_response_too_large"
            inventory.append(
                {
                    "path": path,
                    "status": "unsupported",
                    "disposition": "unsupported",
                    "reason": err_reason,
                    "hunk_ids": [],
                    **({"previous_path": prev_path} if prev_path else {}),
                }
            )
            continue

        # Reconcile Compare fast-path against authoritative base_text -> head_text.
        cmp_status_ok = True
        if cmp_entry is not None:
            raw_cmp_status = str(cmp_entry.get("status") or expected_status)
            if raw_cmp_status != expected_status:
                cmp_status_ok = False
        if (
            cmp_rec is not None
            and cmp_status_ok
            and _diff_reconstructs_head(base_text, head_text, cmp_rec)
        ):
            hunk_ids = [_compute_hunk_id(path, h) for h in cmp_rec.hunks]
            text_diffs.append(cmp_rec.text)
            inv_row: dict[str, Any] = {
                "path": path,
                "status": expected_status,
                "disposition": "text",
                "reason": "compare_reconciled",
                "hunk_ids": hunk_ids,
            }
            if prev_path:
                inv_row["previous_path"] = prev_path
            inventory.append(inv_row)
            continue

        old_lines = base_text.splitlines() if base_text is not None else []
        new_lines = head_text.splitlines() if head_text is not None else []
        old_name = prev_path or path
        fromfile = "/dev/null" if base_text is None else f"a/{old_name}"
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
                    "status": "unsupported",
                    "disposition": "unsupported",
                    "reason": "no_line_diff_content",
                    "hunk_ids": [],
                }
            )
            continue
        file_diff = f"diff --git a/{old_name} b/{path}\n" + "\n".join(diff_lines) + "\n"
        parsed_blob = analyze_diff(file_diff)
        if not parsed_blob.file_records or not parsed_blob.file_records[0].hunks:
            raise V6FreshAnalysisError(
                "v6_diff_inventory_incomplete",
                f"unable to parse authoritative unified diff for {path}",
            )
        blob_rec = parsed_blob.file_records[0]
        hunk_ids = [_compute_hunk_id(path, h) for h in blob_rec.hunks]
        text_diffs.append(blob_rec.text)
        inv_blob: dict[str, Any] = {
            "path": path,
            "status": expected_status,
            "disposition": "text",
            "reason": "blob_unified_diff",
            "hunk_ids": hunk_ids,
        }
        if prev_path:
            inv_blob["previous_path"] = prev_path
        inventory.append(inv_blob)

    return text_diffs, inventory


def _file_group_identity(path: str) -> tuple[str, str, int]:
    """Return ``(parent_dir, normalized_stem, is_test)`` for source/test pairing."""

    p = Path(path)
    parent = p.parent.as_posix()
    if parent == ".":
        parent = ""
    stem = p.stem
    is_test = 0
    if stem.startswith("test_") and len(stem) > 5:
        stem = stem[5:]
        is_test = 1
    elif stem.endswith("_test") and len(stem) > 5:
        stem = stem[:-5]
        is_test = 1
    elif stem.endswith(".test") and len(stem) > 5:
        stem = stem[:-5]
        is_test = 1
    elif stem.endswith(".spec") and len(stem) > 5:
        stem = stem[:-5]
        is_test = 1
    return parent, stem, is_test


def _build_group_keys(paths: Sequence[str]) -> dict[str, tuple[str, str, int]]:
    """Pair source + test files and cluster same-directory modules deterministically."""

    source_dirs_by_stem: dict[str, str] = {}
    identities: dict[str, tuple[str, str, int]] = {}
    for path in sorted(set(paths)):
        parent, stem, is_test = _file_group_identity(path)
        identities[path] = (parent, stem, is_test)
        if is_test == 0 and stem not in source_dirs_by_stem:
            source_dirs_by_stem[stem] = parent

    group_keys: dict[str, tuple[str, str, int]] = {}
    for path, (parent, stem, is_test) in identities.items():
        cluster_dir = source_dirs_by_stem.get(stem, parent) if is_test else parent
        group_keys[path] = (cluster_dir, stem, is_test)
    return group_keys


def _measure_prompt_bytes(
    service: Any,
    *,
    owner: str,
    repo: str,
    pull_request: int,
    pr_title: str | None,
    base_sha: str,
    head_sha: str,
    model_name: str | None,
    instructions: str | None,
    diff_text: str,
    max_prompt_bytes: int,
) -> int:
    """Measure full rendered model prompt UTF-8 byte size via ``ReviewService._format_prompt``."""

    formatter_service = service
    if not hasattr(service, "_format_prompt") or not hasattr(service, "stages"):
        formatter_service = _REAL_REVIEW_SERVICE(
            _NoCallSizingProvider(),
            stages=DEFAULT_STAGES,
        )

    req = ReviewRequest(
        diff=diff_text,
        repository=f"{owner}/{repo}",
        pull_request_number=pull_request,
        title=pr_title,
        base_sha=base_sha,
        head_sha=head_sha,
        model=model_name,
        instructions=instructions,
    )
    analysis = analyze_diff(diff_text, limits=req.limits) if diff_text else None
    max_rendered = 0
    for stage in formatter_service.stages:
        rendered = formatter_service._format_prompt(
            stage,
            req,
            active_categories=stage.categories,
            max_prompt_bytes=max_prompt_bytes,
            analysis=analysis,
        )
        max_rendered = max(max_rendered, len(rendered.encode("utf-8")))
    return max_rendered


def _split_and_group_diff_units(
    text_diffs: Sequence[str],
    *,
    snapshot: EvidenceSnapshot,
    batch_max_bytes: int,
    batch_max_hunks: int,
    prompt_ceiling_bytes: int,
    measure_prompt_fn: Callable[[str], int],
) -> tuple[list[V6DiffUnit], dict[str, tuple[str, ...]], set[str]]:
    """Split file diffs into bounded hunk/sub-hunk units and attach grouping keys."""

    parsed_records: list[DiffFileRecord] = []
    for file_diff_text in text_diffs:
        analysis = analyze_diff(file_diff_text)
        parsed_records.extend(analysis.file_records)

    all_paths = [rec.canonical_path or rec.old_path or "" for rec in parsed_records]
    group_map = _build_group_keys(all_paths)

    units: list[V6DiffUnit] = []
    sub_hunks_by_parent: dict[str, tuple[str, ...]] = {}
    oversized_paths: set[str] = set()

    for rec in parsed_records:
        file_key = rec.canonical_path or rec.old_path or ""
        cluster_dir, stem, is_test = group_map.get(file_key, ("", file_key, 0))

        if not rec.hunks:
            rec_bytes = len(rec.text.encode("utf-8"))
            if (
                rec_bytes <= DEFAULT_REVIEW_LIMITS.max_diff_bytes
                and rec_bytes <= batch_max_bytes
                and measure_prompt_fn(rec.text) <= prompt_ceiling_bytes
            ):
                synthetic_id = f"{file_key}#h0:0,0+0,0:{hashlib.sha256(rec.text.encode('utf-8')).hexdigest()[:16]}"
                units.append(
                    V6DiffUnit(
                        file_path=file_key,
                        hunk_id=synthetic_id,
                        parent_hunk_id=synthetic_id,
                        sub_hunk_index=0,
                        sub_hunk_count=1,
                        header=rec.header,
                        hunk_body="",
                        patch_text=rec.text,
                        is_fragment=False,
                        group_key=(cluster_dir, stem, is_test, file_key, 0, 0),
                    )
                )
                sub_hunks_by_parent[synthetic_id] = (synthetic_id,)
            elif rec.canonical_path is not None:
                oversized_paths.add(rec.canonical_path)
            continue

        for hunk in rec.hunks:
            parent_hunk_id = _compute_hunk_id(file_key, hunk)
            hunk_body = hunk.text if hunk.text.endswith("\n") else f"{hunk.text}\n"
            single_hunk_diff = f"{rec.header}{hunk_body}"
            single_bytes = len(single_hunk_diff.encode("utf-8"))
            if (
                single_bytes <= batch_max_bytes
                and measure_prompt_fn(single_hunk_diff) <= prompt_ceiling_bytes
            ):
                units.append(
                    V6DiffUnit(
                        file_path=file_key,
                        hunk_id=parent_hunk_id,
                        parent_hunk_id=parent_hunk_id,
                        sub_hunk_index=0,
                        sub_hunk_count=1,
                        header=rec.header,
                        hunk_body=hunk_body,
                        patch_text=single_hunk_diff,
                        is_fragment=False,
                        group_key=(cluster_dir, stem, is_test, file_key, hunk.index, 0),
                    )
                )
                sub_hunks_by_parent[parent_hunk_id] = (parent_hunk_id,)
                continue

            parts = subdivide_hunk(
                rec,
                hunk,
                snapshot,
                admit=lambda part: (
                    len(part.patch.encode("utf-8")) <= batch_max_bytes
                    and measure_prompt_fn(part.patch) <= prompt_ceiling_bytes
                ),
            )
            part_ids: list[str] = []
            part_failed = False
            for part_idx, part in enumerate(parts):
                part_bytes = len(part.patch.encode("utf-8"))
                if (
                    part_bytes > DEFAULT_REVIEW_LIMITS.max_diff_bytes
                    or part_bytes > batch_max_bytes
                    or measure_prompt_fn(part.patch) > prompt_ceiling_bytes
                ):
                    part_failed = True
                    break
                sub_id = f"{parent_hunk_id}#s{part_idx}"
                part_ids.append(sub_id)
                units.append(
                    V6DiffUnit(
                        file_path=file_key,
                        hunk_id=sub_id,
                        parent_hunk_id=parent_hunk_id,
                        sub_hunk_index=part_idx,
                        sub_hunk_count=len(parts),
                        header=rec.header,
                        hunk_body=part.patch,
                        patch_text=part.patch,
                        is_fragment=True,
                        group_key=(
                            cluster_dir,
                            stem,
                            is_test,
                            file_key,
                            hunk.index,
                            part_idx,
                        ),
                    )
                )
            if part_failed:
                # Remove any partial fragments for this oversized hunk and mark file unsupported.
                units = [u for u in units if u.parent_hunk_id != parent_hunk_id]
                if rec.canonical_path is not None:
                    oversized_paths.add(rec.canonical_path)
            else:
                sub_hunks_by_parent[parent_hunk_id] = tuple(part_ids)

    units.sort(key=lambda u: u.group_key)
    return units, sub_hunks_by_parent, oversized_paths


def _render_batch_diff_text(batch_units: Sequence[V6DiffUnit]) -> str:
    """Render grouped units cleanly, combining non-fragment hunks of the same file."""

    chunks: list[str] = []
    idx = 0
    while idx < len(batch_units):
        unit = batch_units[idx]
        if unit.is_fragment or not unit.hunk_body:
            chunks.append(unit.patch_text)
            idx += 1
            continue
        chunks.append(unit.header)
        chunks.append(unit.hunk_body)
        j = idx + 1
        while (
            j < len(batch_units)
            and not batch_units[j].is_fragment
            and batch_units[j].file_path == unit.file_path
            and batch_units[j].header == unit.header
            and batch_units[j].hunk_body
        ):
            chunks.append(batch_units[j].hunk_body)
            j += 1
        idx = j
    return "".join(chunks)


def _pack_units_into_planned_batches(
    units: Sequence[V6DiffUnit],
    *,
    batch_max_bytes: int,
    batch_max_files: int,
    batch_max_hunks: int,
    prompt_ceiling_bytes: int,
    measure_prompt_fn: Callable[[str], int],
) -> tuple[V6PlannedBatch, ...]:
    """Pack grouped units into prompt-budgeted batches with strict 1x hunk coverage."""

    if not units:
        return ()

    planned: list[V6PlannedBatch] = []
    cur_units: list[V6DiffUnit] = []
    cur_files: set[str] = set()

    def _flush(batch_idx: int) -> None:
        diff_text = _render_batch_diff_text(cur_units)
        prompt_bytes = measure_prompt_fn(diff_text)
        diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
        paths = tuple(dict.fromkeys(u.file_path for u in cur_units))
        primary_hunk_ids = tuple(u.hunk_id for u in cur_units)
        parent_hunk_ids = tuple(dict.fromkeys(u.parent_hunk_id for u in cur_units))
        planned.append(
            V6PlannedBatch(
                batch_id=f"batch-{batch_idx:03d}-{diff_sha[:12]}",
                diff_text=diff_text,
                diff_sha256=diff_sha,
                prompt_bytes=prompt_bytes,
                paths=paths,
                primary_hunk_ids=primary_hunk_ids,
                parent_hunk_ids=parent_hunk_ids,
            )
        )

    for unit in units:
        if cur_units:
            # Keep a fragmented file's tail separate from the next unrelated group so
            # source+test pairs start together cleanly instead of being split across batches.
            switching_from_fragment_group = (
                cur_units[-1].is_fragment
                and cur_units[-1].group_key[:2] != unit.group_key[:2]
            )
            candidate_units = [*cur_units, unit]
            candidate_files = cur_files | {unit.file_path}
            if (
                switching_from_fragment_group
                or len(candidate_files) > batch_max_files
                or len(candidate_units) > batch_max_hunks
            ):
                _flush(len(planned))
                cur_units = []
                cur_files = set()
            else:
                candidate_diff = _render_batch_diff_text(candidate_units)
                if (
                    len(candidate_diff.encode("utf-8")) > batch_max_bytes
                    or measure_prompt_fn(candidate_diff) > prompt_ceiling_bytes
                ):
                    _flush(len(planned))
                    cur_units = []
                    cur_files = set()
        cur_units.append(unit)
        cur_files.add(unit.file_path)

    if cur_units:
        _flush(len(planned))

    # Assert strict partition invariant: every expected unit appears in exactly one batch.
    seen_ids: list[str] = []
    for batch in planned:
        seen_ids.extend(batch.primary_hunk_ids)
    expected_ids = [u.hunk_id for u in units]
    if len(seen_ids) != len(expected_ids) or set(seen_ids) != set(expected_ids):
        raise V6FreshAnalysisError(
            "v6_batch_partition_invalid",
            "planned batches failed strict 1x primary hunk partition invariant",
        )
    return tuple(planned)


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


def _load_trusted_configuration(policy_root: Path | None) -> ProductConfiguration:
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
    binding_metadata: Mapping[str, Any] | None = None,
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
    digests: list[str] = [f"diff:sha256:{diff_sha256}"]
    if binding_metadata is not None:
        for key in (
            "base_sha",
            "merge_base_sha",
            "inventory_sha256",
            "policy_digest",
            "configuration_digest",
        ):
            val = binding_metadata.get(key)
            if isinstance(val, str) and val:
                digests.append(f"v6:{key}:{val}")
    return (envelope,), tuple(digests)


def prepare_v6_fresh_analysis_context(
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
    batch_prompt_bytes: int | None = None,
    instructions: str | None = None,
) -> V6FreshAnalysisContext:
    """Resolve immutable ancestry/inventory, reconcile Compare, and build prompt-budgeted batches."""

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

    compare_diff_records_by_path: dict[str, DiffFileRecord] = {}
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
                    key = rec.canonical_path or rec.old_path or ""
                    if key:
                        compare_diff_records_by_path[key] = rec
        except (GitHubResponseTooLargeError, GitHubHTTPError, ReviewInputError):
            compare_diff_records_by_path = {}

    text_diffs, file_inventory = _build_authoritative_inventory_and_diffs(
        read_client=read_client,
        owner=owner,
        repo=repo,
        merge_base_sha=merge_base_sha,
        head_sha=head_sha,
        compare_files_by_path=compare_files_by_path,
        compare_diff_records_by_path=compare_diff_records_by_path,
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

    max_provider_calls = (
        configuration.advanced.resources.max_provider_calls
        or DEFAULT_TOTAL_WORK_MAX_PROVIDER_CALLS
    )
    resource_budget = ResourceBudget.create(
        limits=DEFAULT_REVIEW_LIMITS,
        max_provider_calls=max_provider_calls,
    )
    effective_work_budget: EffectiveWorkBudget = (
        configuration.advanced.review_work.effective(
            limits=DEFAULT_REVIEW_LIMITS,
            resource=resource_budget,
            mode="discovery",
        )
    )
    ceiling_bytes, _ = effective_work_budget.effective_prompt_ceiling_bytes()
    prompt_ceiling_bytes = min(
        ceiling_bytes,
        effective_work_budget.batch_prompt_bytes,
        batch_prompt_bytes if batch_prompt_bytes is not None else ceiling_bytes,
    )

    policy_digest, configuration_digest = compute_trusted_policy_and_config_digests(
        configuration,
        provider_name=settings.name,
        model_name=settings.model,
        base_url=settings.base_url,
        allow_custom_endpoint=settings.allow_custom_endpoint,
        upstream_provider=plan.upstream_provider,
        timeout_seconds=settings.timeout_seconds,
    )

    def _measure_fn(candidate_diff: str) -> int:
        return _measure_prompt_bytes(
            service,
            owner=owner,
            repo=repo,
            pull_request=pull_request,
            pr_title=pr_title,
            base_sha=merge_base_sha,
            head_sha=head_sha,
            model_name=settings.model,
            instructions=instructions,
            diff_text=candidate_diff,
            max_prompt_bytes=prompt_ceiling_bytes,
        )

    snapshot = EvidenceSnapshot(
        repository=f"{owner}/{repo}",
        pull_request=pull_request,
        base_sha=merge_base_sha,
        head_sha=head_sha,
    )
    review_units, sub_hunks_by_parent, oversized_paths = _split_and_group_diff_units(
        text_diffs,
        snapshot=snapshot,
        batch_max_bytes=batch_max_bytes,
        batch_max_hunks=batch_max_hunks,
        prompt_ceiling_bytes=prompt_ceiling_bytes,
        measure_prompt_fn=_measure_fn,
    )

    if oversized_paths:
        for entry in file_inventory:
            if entry.get("path") in oversized_paths:
                entry["status"] = "unsupported"
                entry["disposition"] = "unsupported"
                entry["reason"] = "single_line_exceeds_review_limit"
                entry["hunk_ids"] = []

    planned_batches = _pack_units_into_planned_batches(
        review_units,
        batch_max_bytes=batch_max_bytes,
        batch_max_files=batch_max_files,
        batch_max_hunks=batch_max_hunks,
        prompt_ceiling_bytes=prompt_ceiling_bytes,
        measure_prompt_fn=_measure_fn,
    )

    inventory_tuple = tuple(dict(entry) for entry in file_inventory)
    inventory_sha256 = canonical_sha256(inventory_tuple)
    non_text_changes = tuple(
        dict(entry) for entry in inventory_tuple if entry.get("disposition") != "text"
    )
    all_expected_hunk_ids = tuple(u.hunk_id for u in review_units)
    combined_diff_sha256 = hashlib.sha256(
        "".join(text_diffs).encode("utf-8")
    ).hexdigest()
    has_unsupported = any(
        entry.get("disposition") == "unsupported" for entry in inventory_tuple
    )

    return V6FreshAnalysisContext(
        owner=owner,
        repo=repo,
        pull_request=pull_request,
        pr_title=pr_title,
        base_sha=base_sha,
        merge_base_sha=merge_base_sha,
        head_sha=head_sha,
        inventory_sha256=inventory_sha256,
        inventory=inventory_tuple,
        non_text_changes=non_text_changes,
        policy_digest=policy_digest,
        configuration_digest=configuration_digest,
        max_provider_calls=max_provider_calls,
        planned_batches=planned_batches,
        all_expected_hunk_ids=all_expected_hunk_ids,
        sub_hunks_by_parent=sub_hunks_by_parent,
        combined_diff_sha256=combined_diff_sha256,
        provider_name=settings.name,
        model_name=settings.model,
        service=service,
        instructions=instructions,
        has_unsupported=has_unsupported,
    )


def execute_v6_batch(
    context: V6FreshAnalysisContext,
    batch: V6PlannedBatch,
    *,
    remaining_provider_calls: int,
) -> tuple[ReviewResult, int]:
    """Execute one planned batch against the configured ``ReviewService`` within budget."""

    if remaining_provider_calls < 1:
        raise V6FreshAnalysisError(
            "review_provider_call_budget_exhausted",
            "review-wide provider call budget exhausted before batch dispatch",
        )
    req = ReviewRequest(
        diff=batch.diff_text,
        repository=f"{context.owner}/{context.repo}",
        pull_request_number=context.pull_request,
        title=context.pr_title,
        base_sha=context.merge_base_sha,
        head_sha=context.head_sha,
        model=context.model_name,
        instructions=context.instructions,
    )
    if hasattr(context.service, "run") and callable(context.service.run):
        budget = ResourceBudget.create(
            limits=DEFAULT_REVIEW_LIMITS,
            max_provider_calls=remaining_provider_calls,
        )
        run = context.service.run(req, budget=budget)
        calls_made = max(1, int(getattr(run.outcome, "provider_calls", 1) or 1))
        if run.result is None or run.outcome.status in {
            "provider_failed",
            "budget_exhausted",
            "failed",
        }:
            diag = (
                getattr(run.outcome, "diagnostic", None) or "v6_batch_inference_failed"
            )
            raise V6FreshAnalysisError(
                str(diag),
                f"batch {batch.batch_id} failed during provider execution ({run.outcome.status})",
            )
        return run.result, calls_made

    res = context.service.review(req)
    return res, 1


def finalize_v6_fresh_analysis_result(
    context: V6FreshAnalysisContext,
    batch_results: Sequence[ReviewResult],
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str]]:
    """Merge verified per-batch ``ReviewResult``s into an unresolved envelope payload."""

    merged_result = _merge_batch_results(
        batch_results,
        provider_name=context.provider_name,
        model_name=context.model_name,
        has_unsupported=context.has_unsupported,
    )
    return _review_result_to_unresolved_payload(
        merged_result,
        diff_sha256=context.combined_diff_sha256,
        file_inventory=context.inventory,
        binding_metadata={
            "base_sha": context.base_sha,
            "merge_base_sha": context.merge_base_sha,
            "inventory_sha256": context.inventory_sha256,
            "policy_digest": context.policy_digest,
            "configuration_digest": context.configuration_digest,
        },
    )


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
    batch_prompt_bytes: int | None = None,
    instructions: str | None = None,
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str]]:
    """Fetch immutable ``merge_base...head`` diff + complete inventory, batch review, and return v6 payload."""

    context = prepare_v6_fresh_analysis_context(
        owner=owner,
        repo=repo,
        pull_request=pull_request,
        head_sha=head_sha,
        workflow_token_transport=workflow_token_transport,
        event=event,
        visibility=visibility,
        granted_scopes=granted_scopes,
        policy_root=policy_root,
        provider=provider,
        model=model,
        provider_base_url=provider_base_url,
        allow_custom_endpoint=allow_custom_endpoint,
        is_ghec=is_ghec,
        clock=clock,
        batch_max_bytes=batch_max_bytes,
        batch_max_files=batch_max_files,
        batch_max_hunks=batch_max_hunks,
        batch_prompt_bytes=batch_prompt_bytes,
        instructions=instructions,
    )

    batch_results: list[ReviewResult] = []
    calls_used = 0
    for batch in context.planned_batches:
        remaining = context.max_provider_calls - calls_used
        result, batch_calls = execute_v6_batch(
            context, batch, remaining_provider_calls=remaining
        )
        calls_used += batch_calls
        batch_results.append(result)

    return finalize_v6_fresh_analysis_result(context, batch_results)
