"""Lazy fresh-PR ReviewSensei analysis for v6 pilot runs under active write leases."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from review_sensei.configuration import (
    DEFAULT_CONFIG_FILENAME,
    default_configuration,
    load_configuration,
)
from review_sensei.errors import ReviewInputError
from review_sensei.hosted import plan_hosted_execution
from review_sensei.models import ReviewRequest, ReviewResult
from review_sensei.presentation import render_review_markdown
from review_sensei.providers.openai_compatible import (
    validate_openai_compatible_endpoint,
)
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.providers.registry import ProviderSettings, default_registry
from review_sensei.providers.routing import bind_stage_providers
from review_sensei.service import DEFAULT_STAGES, ReviewService

from .v6_credential_matrix import (
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
)
from .v6_recovery_ledger import MAX_MARKER_ENCODED_BYTES
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
_MAX_PR_FILES_PAGES = 30
_MAX_ENVELOPE_BYTES = MAX_MARKER_ENCODED_BYTES - 4096


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


def _reconstruct_diff_from_files(
    *,
    read_client: DualCredentialGitHubClient,
    owner: str,
    repo: str,
    pull_request: int,
    event: GitHubEventKind,
    visibility: RepositoryVisibility,
    granted_scopes: Collection[str],
    now_fn: Callable[[], float],
) -> str:
    """Reconstruct a unified diff from GitHub ``pulls/{pr}/files`` patch entries."""

    chunks: list[str] = []
    for page in range(1, _MAX_PR_FILES_PAGES + 1):
        resp = read_client.execute(
            "pr_files",
            method="GET",
            path=(
                f"/repos/{owner}/{repo}/pulls/{pull_request}/files"
                f"?per_page=100&page={page}"
            ),
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            now_epoch=now_fn(),
        )
        if resp.status != 200 or not isinstance(resp.body, list):
            raise ReviewInputError(
                f"failed to fetch PR files (status={resp.status})",
                diagnostic="v6_pr_diff_unavailable",
            )
        if not resp.body:
            break
        for entry in resp.body:
            if not isinstance(entry, Mapping):
                continue
            patch = entry.get("patch")
            filename = entry.get("filename")
            if not isinstance(filename, str) or not filename.strip():
                raise ReviewInputError(
                    "PR file entry missing filename",
                    diagnostic="v6_pr_diff_incomplete",
                )
            if not isinstance(patch, str) or not patch.strip():
                raise ReviewInputError(
                    f"PR diff incomplete for {filename}",
                    diagnostic="v6_pr_diff_incomplete",
                )
            patch_body = patch if patch.endswith("\n") else f"{patch}\n"
            if patch_body.startswith("diff --git "):
                chunks.append(patch_body)
                continue
            status = str(entry.get("status") or "modified")
            prev = entry.get("previous_filename")
            old_name = str(prev) if isinstance(prev, str) and prev.strip() else filename
            old_header = "/dev/null" if status == "added" else f"a/{old_name}"
            new_header = "/dev/null" if status == "removed" else f"b/{filename}"
            chunks.append(
                f"diff --git a/{old_name} b/{filename}\n"
                f"--- {old_header}\n"
                f"+++ {new_header}\n"
                f"{patch_body}"
            )
        if len(resp.body) < 100:
            break
    return "".join(chunks)


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
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str]]:
    """Convert ``ReviewResult`` into ``UnresolvedResultRecord`` payload and digests."""

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
        "provider": result.provider,
        "model": result.model,
        "review_status": result.review_status,
        "content_digest": result.content_digest(),
    }
    encoded_len = len(
        json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    if encoded_len > _MAX_ENVELOPE_BYTES:
        envelope["findings"] = []
        max_body_bytes = max(1024, _MAX_ENVELOPE_BYTES // 2)
        body_bytes = review_body.encode("utf-8")
        if len(body_bytes) > max_body_bytes:
            trimmed = body_bytes[:max_body_bytes].decode("utf-8", errors="ignore")
            envelope["review_body"] = (
                trimmed.rstrip() + "\n\n_(Review body truncated to ledger bound.)_"
            )
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
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str]]:
    """Fetch PR metadata + diff via ``GITHUB_TOKEN``, run ``ReviewService``, and return v6 payload."""

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
    head_info = pr_resp.body.get("head")
    if not isinstance(head_info, Mapping) or head_info.get("sha") != head_sha:
        raise ReviewInputError(
            f"PR head SHA mismatch: expected {head_sha}, got {head_info.get('sha') if isinstance(head_info, Mapping) else None}",
            diagnostic="v6_pr_head_mismatch",
        )
    raw_title = pr_resp.body.get("title")
    pr_title = raw_title if isinstance(raw_title, str) and raw_title.strip() else None
    base_info = pr_resp.body.get("base")
    raw_base_sha = base_info.get("sha") if isinstance(base_info, Mapping) else None
    base_sha = (
        raw_base_sha
        if isinstance(raw_base_sha, str) and len(raw_base_sha) == 40
        else None
    )

    diff_resp = read_client.execute(
        "pr_diff",
        method="GET",
        path=f"/repos/{owner}/{repo}/pulls/{pull_request}",
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
        diff = diff_resp.body
    else:
        diff = _reconstruct_diff_from_files(
            read_client=read_client,
            owner=owner,
            repo=repo,
            pull_request=pull_request,
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            now_fn=clock,
        )
    if not diff.strip():
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
    review_result = service.review(
        ReviewRequest(
            diff=diff,
            repository=f"{owner}/{repo}",
            pull_request_number=pull_request,
            title=pr_title,
            base_sha=base_sha,
            head_sha=head_sha,
            model=settings.model,
        )
    )
    diff_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return _review_result_to_unresolved_payload(review_result, diff_sha256=diff_sha256)
