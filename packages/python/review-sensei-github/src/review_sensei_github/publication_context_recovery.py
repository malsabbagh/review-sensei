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
    RUNTIME_ONLY_FIELD_LABELS,
    OriginalRecoveryContextBundle,
    RecoveryContextDecision,
    RecoveryContextStore,
    RecoveryRefusalError,
    build_bundle_from_context_documents,
    classify_recovery_input_origins,
    price_recovery_context_admission,
)
from review_sensei.service import DEFAULT_STAGES, review_work_policy_digest
from review_sensei.session import (
    SessionIdentity,
    SessionLedger,
    SessionRecord,
    read_session_baseline,
    session_reservation_id,
)
from review_sensei.validation import DEFAULT_REVIEW_LIMITS


def resolve_recovery_placement_mode(
    *, publication_inventory: str, review_mode: str
) -> str:
    """Resolve the canonical recovery placement mode from inventory and convergence policy."""

    if review_mode == "advisory":
        return "advisory"
    if publication_inventory == "complete":
        return "complete"
    return "bounded"


def capture_recovery_context_bundle(
    store: RecoveryContextStore,
    *,
    transaction: ReviewTransaction,
    configuration_context: Mapping[str, Any],
    admission_context: Mapping[str, Any],
    decision_policy_digest: str,
    diff: str,
    policy_commit: str,
    evidence_context: Mapping[str, Any] | None = None,
    base_ref: str = "main",
    repository_id: int | None = None,
    placement_mode: str = "complete",
    accounting_version: str = ACCOUNTING_VERSION,
    producer: str = "local-cli",
    run_id: int = 1,
    attempt: int = 1,
    runtime_only_flags: Mapping[str, bool] | None = None,
) -> Path:
    """Persist a sanitized OriginalRecoveryContextBundle during ordinary entry."""

    reconstructible, runtime_only_fields = classify_recovery_input_origins(
        **_normalize_runtime_only_flags(runtime_only_flags)
    )
    diff_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    bundle = build_bundle_from_context_documents(
        transaction=transaction,
        configuration_context=configuration_context,
        admission_context=admission_context,
        evidence_context=evidence_context,
        decision_policy_digest=decision_policy_digest,
        diff_sha256=diff_sha256,
        policy_commit=policy_commit,
        base_ref=base_ref,
        repository_id=repository_id,
        placement_mode=placement_mode,
        accounting_version=accounting_version,
        producer=producer,
        run_id=run_id,
        attempt=attempt,
        reconstructible_from_defaults=reconstructible,
        runtime_only_fields=runtime_only_fields,
    )
    store.prune_expired()
    return store.save(bundle)


def verify_stored_recovery_context(
    store: RecoveryContextStore,
    *,
    expected_bundle: OriginalRecoveryContextBundle,
    current_run_id: int = 1,
    current_root_sha256: str | None = None,
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
) -> RecoveryContextDecision:
    """Authenticate, price, and cross-verify stored original recovery context fail-closed."""

    decision = store.load_for_fresh_process(
        expected_bundle.transaction_id,
        expected_repository=expected_bundle.repository,
        expected_pull_request=expected_bundle.pull_request,
        expected_base_sha=expected_bundle.base_sha,
        expected_head_sha=expected_bundle.head_sha,
        expected_policy_commit=expected_bundle.policy_commit,
        expected_result_sha256=expected_bundle.accepted_result_sha256,
        expected_baseline_digest=expected_bundle.baseline_digest,
        verify_baseline_match=True,
        current_run_id=current_run_id,
        current_root_sha256=current_root_sha256,
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
        ledger_expired=ledger_expired,
    )
    if not decision.admitted or decision.bundle is None:
        code = decision.refusal_code or "absent_context"
        details = ", ".join(decision.missing_or_invalid) or "recovery context refused"
        raise RecoveryRefusalError(f"{code}: {details}", refusal_code=code)

    stored = decision.bundle
    drifted: list[str] = []
    if stored.repository_id != expected_bundle.repository_id:
        drifted.append("repository_id")
    if expected_bundle.generation < stored.generation:
        drifted.append("generation")
    for field_name in (
        "base_ref",
        "reservation_id",
        "configuration_digest",
        "evidence_digest",
        "convergence_policy_digest",
        "work_policy_digest",
        "decision_policy_digest",
        "provider_identity",
        "selected_context_digest",
        "learnings_digest",
        "diff_sha256",
        "placement_mode",
        "accounting_version",
    ):
        if getattr(stored, field_name) != getattr(expected_bundle, field_name):
            drifted.append(field_name)
    if drifted:
        raise RecoveryRefusalError(
            f"snapshot_or_transaction_mismatch: stored recovery context drifted on {', '.join(drifted)}",
            refusal_code="snapshot_or_transaction_mismatch",
        )
    return decision


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
    recovery_store: RecoveryContextStore | None = None,
    policy_commit: str | None = None,
    base_ref: str = "main",
    placement_mode: str = "complete",
    producer: str = "local-cli",
    current_run_id: int = 1,
    attempt: int = 1,
    runtime_only_flags: Mapping[str, bool] | None = None,
    current_root_sha256: str | None = None,
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
) -> ReconstructedPublicationContexts:
    """Prove the original default reusable caller's context and reservation.

    The host authenticates this frozen source independently (the canonical
    workflow requires an ancestor of its trusted default branch). No expected
    configuration is copied from a result or its transaction. Changed defaults,
    original overrides, custom stages and unavailable prior baseline refuse
    rather than guessing. Operators can still supply original context documents
    to the existing publication command for those cases.
    """

    preloaded_record: SessionRecord | None = None
    if recovery_store is not None:
        if not policy_commit:
            raise RecoveryRefusalError(
                "absent_context: recovery context verification requires policy_commit",
                refusal_code="absent_context",
            )
        loaded_for_store = ledger.load(identity)
        if (
            loaded_for_store.status not in {"ok", "migrated"}
            or loaded_for_store.record is None
        ):
            raise RecoveryRefusalError(
                "original context ledger is unavailable",
                refusal_code="absent_context",
            )
        preloaded_record = loaded_for_store.record
        if (
            preloaded_record.transaction is None
            or preloaded_record.transaction.result_sha256 is None
        ):
            raise RecoveryRefusalError(
                "original context transaction is unavailable",
                refusal_code="absent_context",
            )
        pre_decision = recovery_store.load_for_fresh_process(
            preloaded_record.transaction.transaction_id,
            expected_repository=identity.repository,
            expected_pull_request=identity.pull_request,
            expected_base_sha=base_sha,
            expected_head_sha=head_sha,
            expected_policy_commit=policy_commit,
            expected_result_sha256=preloaded_record.transaction.result_sha256,
            current_run_id=current_run_id,
            current_root_sha256=current_root_sha256,
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
            ledger_expired=ledger_expired,
        )
        if not pre_decision.admitted or pre_decision.bundle is None:
            code = pre_decision.refusal_code or "absent_context"
            details = (
                ", ".join(pre_decision.missing_or_invalid) or "recovery context refused"
            )
            raise RecoveryRefusalError(f"{code}: {details}", refusal_code=code)

    if not source_root.is_dir() or not (source_root / ".reviewsensei.yml").is_file():
        raise RecoveryRefusalError(
            "original trusted context source is unavailable",
            refusal_code="absent_context",
        )
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
        raise RecoveryRefusalError(
            "original admission context is unavailable",
            refusal_code="absent_context",
        )
    if preloaded_record is not None:
        record = preloaded_record
    else:
        loaded = ledger.load(identity)
        if loaded.status not in {"ok", "migrated"} or loaded.record is None:
            raise RecoveryRefusalError(
                "original context ledger is unavailable",
                refusal_code="absent_context",
            )
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
        raise RecoveryRefusalError(
            "reconstructed context does not match the original committed transaction; "
            "supply the original configuration and admission context documents",
            refusal_code="snapshot_or_transaction_mismatch",
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
            raise RecoveryRefusalError(
                "original prior baseline is unavailable; supply the original admission context",
                refusal_code="overwritten_baseline_or_context",
            )
        baseline = None
    elif baseline is not None and (
        baseline.cache_key.repository != identity.repository
        or baseline.cache_key.pull_request != identity.pull_request
        or baseline.generation >= transaction.generation
    ):
        raise RecoveryRefusalError(
            "original prior baseline context is unverifiable",
            refusal_code="overwritten_baseline_or_context",
        )
    elif baseline is None and (
        record.completed_initial_reviews or record.completed_verification_rounds
    ):
        raise RecoveryRefusalError(
            "original prior baseline is unavailable; supply the original admission context",
            refusal_code="overwritten_baseline_or_context",
        )
    reconstructed_contexts = ReconstructedPublicationContexts(
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
    if recovery_store is not None:
        assert policy_commit is not None
        expected_bundle = bundle_from_reconstructed_contexts(
            reconstructed_contexts,
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
            runtime_only_flags=runtime_only_flags,
            _preloaded_record=record,
        )
        verify_stored_recovery_context(
            recovery_store,
            expected_bundle=expected_bundle,
            current_run_id=current_run_id,
            current_root_sha256=current_root_sha256,
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
            ledger_expired=ledger_expired,
        )
    return reconstructed_contexts


def _normalize_runtime_only_flags(
    runtime_only_flags: Mapping[str, bool] | None,
) -> dict[str, bool]:
    if runtime_only_flags is None:
        return {}
    if not isinstance(runtime_only_flags, Mapping):
        raise ReviewInputError("runtime_only_flags must be a mapping")
    normalized: dict[str, bool] = {}
    for raw_key, raw_val in runtime_only_flags.items():
        if not isinstance(raw_key, str):
            raise ReviewInputError("runtime_only_flags keys must be strings")
        label = raw_key.removeprefix("has_")
        if label not in RUNTIME_ONLY_FIELD_LABELS:
            raise ReviewInputError(f"unknown runtime_only_flags key: {raw_key}")
        kwarg_name = f"has_{label}"
        if kwarg_name in normalized and normalized[kwarg_name] != raw_val:
            raise ReviewInputError(f"conflicting runtime_only_flags for {label}")
        normalized[kwarg_name] = raw_val
    return normalized


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
    _preloaded_record: SessionRecord | None = None,
) -> OriginalRecoveryContextBundle:
    """Convert reconstructed publication contexts into a sanitized G17 recovery bundle."""

    if not source_root.is_dir() or not (source_root / ".reviewsensei.yml").is_file():
        raise RecoveryRefusalError(
            "original trusted context source is unavailable",
            refusal_code="absent_context",
        )
    configuration = load_configuration(source_root / ".reviewsensei.yml")
    if _preloaded_record is not None:
        record = _preloaded_record
    else:
        loaded = ledger.load(identity)
        if loaded.status not in {"ok", "migrated"} or loaded.record is None:
            raise RecoveryRefusalError(
                "original context ledger is unavailable",
                refusal_code="absent_context",
            )
        record = loaded.record
    if record.transaction is None:
        raise RecoveryRefusalError(
            "original context transaction is unavailable",
            refusal_code="absent_context",
        )
    if reconstructed.admission_context is None:
        raise RecoveryRefusalError(
            "reconstructed admission_context is unavailable",
            refusal_code="absent_context",
        )

    accounting_version = ACCOUNTING_VERSION
    if record.api_run_accounting is not None:
        if not isinstance(record.api_run_accounting, Mapping):
            raise RecoveryRefusalError(
                "record.api_run_accounting must be a mapping when present",
                refusal_code="truncated_or_corrupt_context",
            )
        raw_version = record.api_run_accounting.get(
            "version", record.api_run_accounting.get("schema_version")
        )
        if not isinstance(raw_version, str) or not raw_version.strip():
            raise RecoveryRefusalError(
                "record.api_run_accounting is missing a valid version string",
                refusal_code="truncated_or_corrupt_context",
            )
        accounting_version = raw_version

    reconstructible, runtime_only_fields = classify_recovery_input_origins(
        **_normalize_runtime_only_flags(runtime_only_flags)
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
    recovery_store: RecoveryContextStore | None = None,
    placement_mode: str = "complete",
    producer: str = "local-cli",
    current_run_id: int = 1,
    attempt: int = 1,
    runtime_only_flags: Mapping[str, bool] | None = None,
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
            recovery_store=recovery_store,
            policy_commit=policy_commit,
            base_ref=base_ref,
            placement_mode=placement_mode,
            producer=producer,
            current_run_id=current_run_id,
            attempt=attempt,
            runtime_only_flags=runtime_only_flags,
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
            ledger_expired=ledger_expired,
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
            runtime_only_flags=runtime_only_flags,
        )
    except ReviewInputError as exc:
        message = str(exc)
        code = (
            exc.refusal_code
            if isinstance(exc, RecoveryRefusalError)
            else "truncated_or_corrupt_context"
        )
        if code == "unsupported_v3_writer":
            prescription = prescribe_recovery("unsupported_legacy_state")
        elif code == "snapshot_or_transaction_mismatch":
            prescription = prescribe_recovery("provenance_mismatch")
        else:
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
