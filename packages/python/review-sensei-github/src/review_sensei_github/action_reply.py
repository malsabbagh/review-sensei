"""Mention replies owned by one authenticated, durable caller-run action."""

from __future__ import annotations

import hashlib
import json
from copy import copy
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.request import Request

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.cli import (
    _configured_resource_budget,
    _work_recovery_from_cli,
    resolve_review_inference,
)
from review_sensei.configuration import customization_directories, load_configuration
from review_sensei.errors import ReviewInputError
from review_sensei.outcomes import ResourceBudget
from review_sensei.providers.registry import default_registry
from review_sensei.providers.routing import bind_stage_providers
from review_sensei.service import ReviewService
from review_sensei.stages import (
    category_catalog_for_configured_stages,
    load_stages_from_dir,
)

from .action_completion import require_reply_completion
from .action_run import application
from .api_run_budget import CallerRunBudget
from .api_run_host import policy_bytes
from .application import GitHubWriteOptions
from .conversation import ReplyResult
from .operation_comments import GitHubIssueCommentOperationPort, ResultPartStore
from .operation_host import GitHubCommentOperationStore
from .review_run import ActionRunLedger, ReviewRun
from .run_authority import VerifiedRunAction

_PUBLISHED = frozenset({"replied", "replied_and_resolved"})
_ALREADY_PUBLISHED = frozenset({"already_replied", "already_replied_and_resolved"})
_NOOPS = {
    "disabled": "policy_disabled",
    "skipped_no_mention": "not_mentioned",
    "skipped_unauthorized": "unauthorized",
}


def _configuration(run: ReviewRun) -> Any:
    if hashlib.sha256(policy_bytes(run.configuration_path)).hexdigest() != (
        run.caller.accounting.configuration.digest
    ):
        raise ReviewInputError("frozen reply policy configuration changed")
    return load_configuration(run.configuration_path)


def _providers(run: ReviewRun, args: Any, configuration: Any) -> tuple[Any, Any, Any]:
    """Reuse canonical CLI selection with the already frozen plan values."""
    if getattr(args, "profile", None) is not None:
        raise ReviewInputError("current replies require the frozen inference plan")
    if (
        args.provider != run.plan.backend
        or args.model != run.plan.model
        or args.base_url != run.plan.base_url
    ):
        raise ReviewInputError(
            "reply inference arguments conflict with the frozen plan"
        )
    selected = copy(args)
    selected.config = run.configuration_path
    selected.provider = run.plan.backend
    selected.model = run.plan.model
    selected.base_url = run.plan.base_url
    selected.allow_custom_endpoint = run.plan.allow_custom_endpoint
    selected.api_key_env = run.plan.credential_env
    selected.timeout_seconds = run.plan.timeout_seconds
    selected._explicit_cli_options = {"--provider", "--model", "--base-url"}
    if run.plan.credential_required:
        selected._explicit_cli_options.add("--api-key-env")
    if run.plan.timeout_seconds is not None:
        selected._explicit_cli_options.add("--timeout-seconds")
    settings, _key = resolve_review_inference(selected, [])
    registry = default_registry()
    provider = registry.create(settings)
    broader = None
    if getattr(args, "enable_broader_review", False):
        if (
            configuration.advanced.review_work.mode != "unified"
            or not run.plan.policy.automatic_reviews
            or not run.plan.policy.writes
        ):
            raise ReviewInputError(
                "broader reply review requires unified mode and trusted full-review/write policy"
            )
        import review_sensei

        custom = customization_directories(configuration)
        packaged = Path(review_sensei.__file__).parent
        categories = custom.categories
        if custom.stages is None and categories is None:
            categories = packaged / "default_categories"
        stages = load_stages_from_dir(
            custom.stages or packaged / "default_stages",
            category_catalog=category_catalog_for_configured_stages(categories),
        )
        full, stage_providers = bind_stage_providers(
            registry=registry, settings=settings, stages=stages
        )
        broader = ReviewService(
            full,
            stages=stages,
            stage_providers=stage_providers,
            work_budgets=configuration.advanced.review_work,
        )
    return provider, broader, _work_recovery_from_cli(selected, configuration)


def _local_model(run: ReviewRun) -> None:
    if run.plan.runner_kind != "local":
        return
    request = Request(
        run.plan.base_url.rstrip("/") + "/tags",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with run.caller.read_operation("reply-model-discovery"):
        with run.caller.open(request, timeout=10) as response:
            raw = response.read(512 * 1024 + 1)
            if getattr(response, "status", None) != 200 or len(raw) > 512 * 1024:
                raise ReviewInputError("local reply model discovery is unavailable")
    try:
        models = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise ReviewInputError("local reply model inventory is invalid") from error
    if (
        not isinstance(models, dict)
        or not isinstance(models.get("models"), list)
        or not any(
            isinstance(model, dict) and model.get("name") == run.plan.model
            for model in models["models"]
        )
    ):
        raise ReviewInputError("planned local reply model is unavailable")


def _outcome(result: ReplyResult, run: ReviewRun | None = None) -> dict[str, object]:
    if result.status in _PUBLISHED | _ALREADY_PUBLISHED:
        if type(result.comment_id) is not int or result.comment_id <= 0:
            raise ReviewInputError(
                "reply publication has no confirmed comment identity"
            )
        if (
            run is None
            or run.session_authority is None
            or not require_reply_completion(
                result,
                run.session_authority,
                run_id=str(run.caller.run.run_id),
                run_attempt=str(run.caller.attempt),
            )
        ):
            raise ReviewInputError(
                "reply assessment or required finalization remains pending"
            )
        return {
            "kind": "applied" if result.status in _PUBLISHED else "already_applied",
            "reason": None,
        }
    if (
        result.status in _NOOPS
        and result.comment_id is None
        and result.finalization_completion is None
        and all(
            value is None
            for value in (
                result.approval_status,
                result.approval_diagnostic,
                result.assessment_status,
                result.assessment_diagnostic,
                result.resolution_status,
            )
        )
    ):
        return {"kind": "explicit_noop", "reason": _NOOPS[result.status]}
    raise ReviewInputError("reply effect remains pending: " + result.status)


def execute_reply(run: ReviewRun, args: Any) -> int:
    authority = VerifiedRunAction(run)
    if (
        not isinstance(run.ledger, ActionRunLedger)
        or authority.document["purpose"] != "reply"
    ):
        raise ReviewInputError("reply requires its current action ledger and purpose")
    if not isinstance(run.caller.budget_view, CallerRunBudget):
        raise ReviewInputError("reply requires the original caller-run allowance")
    source = authority.document["source"]
    source_kind = "inline" if source["kind"] == "review_comment" else "issue"
    if (
        args.source_comment_id != source["id"]
        or args.source_updated_at != source["updated_at"]
        or args.source_kind != source_kind
    ):
        raise ReviewInputError("reply arguments conflict with the authenticated source")
    run.ledger.business_guard = authority.fence
    authority.fence()
    configuration = _configuration(run)
    key = hashlib.sha256(
        canonical_bytes(
            {
                "purpose": "reply",
                "repository_id": run.identity.repository_id,
                "pull_request": run.identity.pull_request,
                "head_sha": run.head_sha,
                "source": source,
                "configuration": run.caller.accounting.configuration.digest,
            }
        )
    ).hexdigest()
    caller = run.caller
    intent = caller.admit_action(
        run.ledger,
        run.identity,
        purpose="reply",
        source=source,
        head_sha=run.head_sha,
        operation_key=key,
    )
    boundary = caller.begin_action_effect(run.ledger, run.identity, intent)
    if not run.plan.policy.writes or not run.plan.policy.mentions:
        result = ReplyResult(status="disabled")
    else:
        app_id = run.ledger.authenticated_app_id
        author_id = run.ledger._evidence_author_id
        if (
            type(app_id) is not int
            or app_id <= 0
            or type(author_id) is not int
            or author_id <= 0
        ):
            raise ReviewInputError(
                "reply operation journal App identity is unavailable"
            )
        provider, broader, recovery = _providers(run, args, configuration)
        _local_model(run)
        port = GitHubIssueCommentOperationPort(
            run.http,
            token=run.ledger.token,
            repository=run.identity.repository,
            pull_request=run.identity.pull_request,
            app_user_id=author_id,
            strict_association=True,
        )
        store = GitHubCommentOperationStore(port, app_user_id=author_id)
        parts = ResultPartStore(port, app_user_id=author_id)
        app = application(run)
        # Broader reassessment may call this same application's publish_review;
        # its prior budget must already be this run's original caller facade.
        app.reviewer.finalizer.evidence_budget = caller.budget_view
        result = app.generate_and_publish_reply(
            options=GitHubWriteOptions(
                github_writes=True,
                mention_replies=True,
                auto_review=broader is not None,
                reviews_policy=run.plan.policy.reviews,
                upload_artifacts=run.plan.policy.artifacts == "diagnostics",
                github_session_ledger=True,
                complete_inventory=True,
                operation_entry="enabled",
            ),
            oidc_token=run.oidc,
            read_token=run.ledger.token,
            repository=run.identity.repository,
            pull_request=run.identity.pull_request,
            source_comment_id=source["id"],
            source_updated_at=source["updated_at"],
            expected_head_sha=run.head_sha,
            reply_provider=provider,
            model=run.plan.model,
            app_slug=args.app_slug,
            root_comment_id=args.root_comment_id or None,
            source_kind=source_kind,
            work_budgets=configuration.advanced.review_work,
            budget=_configured_resource_budget(ResourceBudget.create(), configuration),
            work_recovery=recovery,
            broader_service=broader,
            operation_entry="enabled",
            repository_id=run.identity.repository_id,
            app_id=app_id,
            run_id=str(caller.run.run_id),
            run_attempt=str(caller.attempt),
            operation_store=store,
            operation_parts=parts,
            evidence_budget=caller.budget_view,
            current_action=authority,
        )
    if not isinstance(result, ReplyResult):
        raise ReviewInputError("reply result is not a confirmed current outcome")
    outcome = _outcome(result, run)
    _configuration(run)
    proof = caller.settle_action_effect(
        run.ledger,
        run.identity,
        boundary,
        outcome=outcome,
        result_document=asdict(result),
    )
    caller.begin_terminal("action-completion")
    caller.close_action(run.ledger, run.identity, proof)
    print(result.status)
    return 0
