"""Reconstruct canonical caller context from independently frozen trusted source.

This is not analysis admission. It neither reserves a round nor creates a
provider; every expected identity is recomputed before retained bytes are read.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from review_sensei.api_work import ACCOUNTING_VERSION
from review_sensei.baseline import ReviewBaseline, baseline_complete_document
from review_sensei.cli import (
    _CANONICAL_BACKEND_ADAPTERS,
    _configured_resource_budget,
    _transaction_provider_identity,
)
from review_sensei.configuration import load_configuration, resolve_inference
from review_sensei.context import (
    RepositoryContextStore,
    ReviewContextCacheKey,
    SymbolAwareContextPolicy,
    build_review_context_cache_key,
    build_review_context_selection,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diff import analyze_diff
from review_sensei.errors import ReviewInputError
from review_sensei.learnings import load_repository_learnings
from review_sensei.models import (
    ReviewRequest,
    ReviewTransaction,
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.provenance_manifest import prescribe_recovery
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.providers.registry import ProviderSettings
from review_sensei.recovery_context import (
    OriginalRecoveryContextBundle,
    RecoveryContextDecision,
    build_bundle_from_context_documents,
    classify_recovery_input_origins,
    price_recovery_context_admission,
)
from review_sensei.service import DEFAULT_STAGES, review_work_policy_digest
from review_sensei.session import (
    SessionIdentity,
    SessionLedger,
    read_session_baseline,
    session_reservation_id,
)
from review_sensei.validation import DEFAULT_REVIEW_LIMITS


@dataclass(frozen=True)
class ReconstructedPublicationContexts:
    configuration: dict[str, object]
    evidence: dict[str, object]
    baseline: ReviewBaseline | None
    current_key: ReviewContextCacheKey
    admission_context: dict[str, object] | None = None


def reconstruct_publication_contexts(
    *,
    source_root: Path,
    diff: str,
    ledger: SessionLedger,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    policy: ReviewConvergencePolicy,
) -> ReconstructedPublicationContexts:
    """Prove the original default reusable caller's context and reservation.

    The host authenticates this frozen source independently (the canonical
    workflow requires an ancestor of its trusted default branch). No expected
    configuration is copied from a result or its transaction. Changed defaults,
    original overrides, custom stages and unavailable prior baseline refuse
    rather than guessing. Operators can still supply original context documents
    to the existing publication command for those cases.
    """

    if not source_root.is_dir() or not (source_root / ".reviewsensei.yml").is_file():
        raise ReviewInputError("original trusted context source is unavailable")
    configuration = load_configuration(source_root / ".reviewsensei.yml")
    resolved = resolve_inference(configuration, environ={})
    if resolved.backend == "fixture":
        raise ReviewInputError("original hosted context cannot use fixture inference")
    settings = ProviderSettings(
        name=_CANONICAL_BACKEND_ADAPTERS[resolved.backend],
        model=resolved.model,
        base_url=resolved.base_url,
        timeout_seconds=resolved.timeout_seconds,
        allow_custom_endpoint=resolved.allow_custom_endpoint,
        openrouter_policy=(
            OpenRouterRoutingPolicy(upstream_provider=resolved.upstream_provider)
            if resolved.backend == "openrouter" and resolved.upstream_provider
            else None
        ),
    )
    provider, model = _transaction_provider_identity(settings)
    budget = _configured_resource_budget(
        ResourceBudget.for_limits(DEFAULT_REVIEW_LIMITS), configuration
    )
    work_policy = review_work_policy_digest(
        configuration.advanced.review_work, budget, decision=configuration.decision
    )
    stages, categories = transaction_stage_identity(DEFAULT_STAGES)
    reconstructed_configuration = build_transaction_configuration_context(
        provider=provider,
        model=model,
        stages=stages,
        category_policy=categories,
        publication_mode=policy.mode,
        orchestration_enabled=configuration.advanced.review_work.mode == "unified",
        work_policy_digest=work_policy,
    )
    evidence: dict[str, object] = {"evidence_policy": "legacy", "snapshot_sha256": None}
    analysis = analyze_diff(
        diff,
        limits=DEFAULT_REVIEW_LIMITS,
        allow_incomplete=True,
        max_bytes=1_048_576,
        max_lines=50_000,
        max_files=500,
        max_hunks=5_000,
    )
    learnings = load_repository_learnings(
        source_root, directory=".github/review-sensei/learnings"
    ).for_paths(analysis.changed_paths)
    catalog = tuple({c.id: c for s in DEFAULT_STAGES for c in s.categories}.values())
    selection = build_review_context_selection(
        catalog,
        changed_paths=analysis.changed_paths,
        learnings=learnings,
        context_store=RepositoryContextStore(source_root),
        source_context_policy=SymbolAwareContextPolicy(enabled=False),
        snapshot=None,
        changed_lines=analysis.changed_lines,
    )
    key = build_review_context_cache_key(
        ReviewRequest(
            diff=diff,
            repository=identity.repository,
            pull_request_number=identity.pull_request,
            model=model,
            learnings=learnings,
            active_category_ids=selection.active_category_ids,
            lens_contexts=selection.lens_contexts,
            source_context=selection.source_context,
            base_sha=base_sha,
            head_sha=head_sha,
            work_policy_digest=work_policy,
        ),
        provider_name=settings.name,
        stages=DEFAULT_STAGES,
        profile="default",
        effective_model=model,
        stage_provider_bindings={
            stage.name: (settings.name, model) for stage in DEFAULT_STAGES
        }
        if model is not None
        else None,
    )
    if key is None:
        raise ReviewInputError("original admission context is unavailable")
    loaded = ledger.load(identity)
    if loaded.status not in {"ok", "migrated"} or loaded.record is None:
        raise ReviewInputError("original context ledger is unavailable")
    record = loaded.record
    transaction = record.transaction
    configuration_digest = ReviewTransaction.compute_configuration_digest(
        reconstructed_configuration
    )
    evidence_digest = ReviewTransaction.compute_evidence_digest(evidence)
    reservation = session_reservation_id(
        repository=identity.repository,
        pull_request=identity.pull_request,
        head_sha=head_sha,
        kind="publish:"
        + key.digest()
        + ":"
        + policy.digest()
        + ":"
        + configuration_digest
        + ":"
        + evidence_digest,
    )
    quarantine_full_analysis = False
    if record.quarantine_lineage is not None and transaction is not None:
        from review_sensei.quarantine_lineage import full_analysis_reservation

        full_reservation = full_analysis_reservation(
            record.quarantine_lineage["scope_sha256"], reservation
        )
        if transaction.reservation_id == full_reservation:
            reservation = full_reservation
            quarantine_full_analysis = True
    if record.rescan_state is not None and transaction is not None:
        # The consumed request derives a distinct transaction reservation from
        # this independently reconstructed ordinary context. A successor may
        # recover that exact recorded binding, never infer a new rescan grant.
        receipts = [
            item
            for item in cast(list[dict[str, Any]], record.rescan_state["receipts"])
            if item["ordinary_reservation"] == reservation
            and item["reservation"] == transaction.reservation_id
        ]
        if len(receipts) == 1:
            reservation = transaction.reservation_id
    if (
        record.repository_id not in {None, identity.repository_id}
        or transaction is None
        or transaction.phase
        not in {"publication_pending", "publication_failed", "publication_succeeded"}
        or transaction.result_sha256 is None
        or record.reservation_id is not None
        or record.last_committed_reservation_id != reservation
        or transaction.reservation_id != reservation
        or not transaction.identity_matches(
            repository=identity.repository,
            pull_request=identity.pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            policy_digest=policy.digest(),
            configuration_digest=configuration_digest,
            evidence_digest=evidence_digest,
        )
    ):
        raise ReviewInputError(
            "reconstructed context does not match the original committed transaction; "
            "supply the original configuration and admission context documents"
        )
    baseline = read_session_baseline(ledger, record)
    if quarantine_full_analysis:
        # The authenticated transaction commits to the independently rebuilt
        # ordinary context plus the immutable lineage. It selected a full pass;
        # the predecessor baseline remains retained, never interpreted as fresh.
        baseline = None
    elif baseline is not None and baseline.cache_key == key:
        if (
            record.completed_verification_rounds != 0
            or record.completed_initial_reviews != 1
        ):
            raise ReviewInputError(
                "original prior baseline is unavailable; supply the original admission context"
            )
        baseline = None
    elif baseline is not None and (
        baseline.cache_key.repository != identity.repository
        or baseline.cache_key.pull_request != identity.pull_request
        or baseline.generation >= transaction.generation
    ):
        raise ReviewInputError("original prior baseline context is unverifiable")
    elif baseline is None and (
        record.completed_initial_reviews or record.completed_verification_rounds
    ):
        raise ReviewInputError(
            "original prior baseline is unavailable; supply the original admission context"
        )
    return ReconstructedPublicationContexts(
        reconstructed_configuration,
        evidence,
        baseline,
        key,
        {
            "schema_version": "quarantine-original-admission-context-v1",
            "configuration": reconstructed_configuration,
            "evidence": evidence,
            "active_category_ids": list(selection.active_category_ids),
            "lens_contexts": [
                lens.to_prompt_dict() for lens in selection.lens_contexts
            ],
            "learnings": [learning.to_dict() for learning in learnings],
            "baseline": baseline_complete_document(baseline)
            if baseline is not None
            else None,
            "current_key": dict(key.__dict__),
        },
    )


def bundle_from_reconstructed_contexts(
    reconstructed: ReconstructedPublicationContexts,
    *,
    source_root: Path,
    diff: str,
    ledger: SessionLedger,
    identity: SessionIdentity,
    base_ref: str,
    policy_commit: str,
    placement_mode: str = "complete",
    producer: str = "local-cli",
    run_id: int = 1,
    attempt: int = 1,
    runtime_only_flags: Mapping[str, bool] | None = None,
) -> OriginalRecoveryContextBundle:
    """Convert reconstructed publication contexts into a sanitized G17 recovery bundle."""

    if not source_root.is_dir() or not (source_root / ".reviewsensei.yml").is_file():
        raise ReviewInputError("original trusted context source is unavailable")
    configuration = load_configuration(source_root / ".reviewsensei.yml")
    loaded = ledger.load(identity)
    if loaded.status not in {"ok", "migrated"} or loaded.record is None:
        raise ReviewInputError("original context ledger is unavailable")
    record = loaded.record
    if record.transaction is None:
        raise ReviewInputError("original context transaction is unavailable")
    if reconstructed.admission_context is None:
        raise ReviewInputError("reconstructed admission_context is unavailable")

    accounting_version = ACCOUNTING_VERSION
    if isinstance(record.api_run_accounting, Mapping):
        raw_version = record.api_run_accounting.get(
            "version", record.api_run_accounting.get("schema_version")
        )
        if isinstance(raw_version, str):
            accounting_version = raw_version

    reconstructible, runtime_only_fields = classify_recovery_input_origins(
        **(dict(runtime_only_flags) if runtime_only_flags is not None else {})
    )
    diff_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return build_bundle_from_context_documents(
        transaction=record.transaction,
        configuration_context=reconstructed.configuration,
        admission_context=reconstructed.admission_context,
        decision_policy_digest=configuration.decision.digest(),
        diff_sha256=diff_sha256,
        policy_commit=policy_commit,
        base_ref=base_ref,
        repository_id=identity.repository_id,
        placement_mode=placement_mode,
        accounting_version=accounting_version,
        producer=producer,
        run_id=run_id,
        attempt=attempt,
        reconstructible_from_defaults=reconstructible,
        runtime_only_fields=runtime_only_fields,
    )


def reconstruct_or_prescribe_publication_contexts(
    *,
    source_root: Path,
    diff: str,
    ledger: SessionLedger,
    identity: SessionIdentity,
    base_ref: str,
    base_sha: str,
    head_sha: str,
    policy_commit: str,
    policy: ReviewConvergencePolicy,
    placement_mode: str = "complete",
    producer: str = "local-cli",
    current_run_id: int = 1,
    attempt: int = 1,
    ancestor_record_sha256s: Sequence[str] = (),
    ancestor_run_ids: Sequence[int] = (),
    current_root_bytes: int = 4_096,
    intermediate_root_bytes: int = 4_096,
    result_part_count: int = 1,
    total_decoded_bytes: int = 16_384,
    original_api_limit: int = 512,
    already_consumed_requests: int = 0,
    unknown_usage_requests: int = 0,
    active_reserved_requests: int = 0,
    ledger_expired: bool = False,
) -> tuple[ReconstructedPublicationContexts | None, RecoveryContextDecision]:
    """Reconstruct and price original recovery context or return a bounded recovery prescription."""

    try:
        reconstructed = reconstruct_publication_contexts(
            source_root=source_root,
            diff=diff,
            ledger=ledger,
            identity=identity,
            base_sha=base_sha,
            head_sha=head_sha,
            policy=policy,
        )
        bundle = bundle_from_reconstructed_contexts(
            reconstructed,
            source_root=source_root,
            diff=diff,
            ledger=ledger,
            identity=identity,
            base_ref=base_ref,
            policy_commit=policy_commit,
            placement_mode=placement_mode,
            producer=producer,
            run_id=current_run_id,
            attempt=attempt,
        )
    except ReviewInputError as exc:
        message = str(exc)
        if "unsupported_v3_writer" in message:
            code = "unsupported_v3_writer"
            prescription = prescribe_recovery("unsupported_legacy_state")
        elif "observation_only_not_authority" in message:
            code = "observation_only_not_authority"
            prescription = prescribe_recovery(
                "missing_evidence", ledger_expired=ledger_expired
            )
        elif "prior baseline" in message:
            code = "overwritten_baseline_or_context"
            prescription = prescribe_recovery(
                "missing_evidence", ledger_expired=ledger_expired
            )
        elif "original committed transaction" in message or "does not match" in message:
            code = "snapshot_or_transaction_mismatch"
            prescription = prescribe_recovery("provenance_mismatch")
        elif "unavailable" in message:
            code = "absent_context"
            prescription = prescribe_recovery(
                "missing_evidence", ledger_expired=ledger_expired
            )
        else:
            code = "truncated_or_corrupt_context"
            prescription = prescribe_recovery(
                "missing_evidence", ledger_expired=ledger_expired
            )
        return (
            None,
            RecoveryContextDecision(
                admitted=False,
                bundle=None,
                refusal_code=code,
                missing_or_invalid=(message,),
                pricing=None,
                recovery=prescription,
                authority=False,
            ),
        )

    decision = price_recovery_context_admission(
        bundle,
        current_run_id=current_run_id,
        ancestor_record_sha256s=ancestor_record_sha256s,
        ancestor_run_ids=ancestor_run_ids,
        current_root_bytes=current_root_bytes,
        intermediate_root_bytes=intermediate_root_bytes,
        result_part_count=result_part_count,
        total_decoded_bytes=total_decoded_bytes,
        original_api_limit=original_api_limit,
        already_consumed_requests=already_consumed_requests,
        unknown_usage_requests=unknown_usage_requests,
        active_reserved_requests=active_reserved_requests,
    )
    if not decision.admitted:
        return (None, decision)
    return (reconstructed, decision)
