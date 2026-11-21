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

import review_sensei
from review_sensei.api_work import ACCOUNTING_VERSION
from review_sensei.baseline import ReviewBaseline, baseline_complete_document
from review_sensei.cli import (
    _CANONICAL_BACKEND_ADAPTERS,
    _configured_resource_budget,
    _transaction_provider_identity,
)
from review_sensei.configuration import (
    customization_directories,
    load_configuration,
    resolve_inference,
)
from review_sensei.context import (
    ContextSnapshot,
    RepositoryContextStore,
    ReviewContextCacheKey,
    SymbolAwareContextPolicy,
    build_review_context_cache_key,
    build_review_context_selection,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diff import analyze_diff
from review_sensei.errors import ContextLoadError, ReviewInputError
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
from review_sensei.stages import (
    ReviewCategory,
    Stage,
    category_catalog_for_configured_stages,
    load_stages_from_dir,
)
from review_sensei.validation import DEFAULT_REVIEW_LIMITS

_SUPPORTED_RECOVERY_ENV_OVERRIDES = ("REVIEWSENSEI_PROVIDER", "REVIEWSENSEI_MODEL")


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


def _resolve_trusted_subdir(source_root: Path, directory: Path, *, label: str) -> Path:
    """Validate that a custom configuration directory stays inside ``source_root``."""

    resolved_root = source_root.resolve()
    candidate = directory if directory.is_absolute() else (source_root / directory)
    if candidate.is_symlink():
        raise ReviewInputError(f"{label} directory must not be a symbolic link")
    try:
        resolved_candidate = candidate.resolve()
    except OSError as exc:
        raise ReviewInputError(f"{label} directory is unavailable") from exc
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ReviewInputError(
            f"{label} directory must be contained inside trusted source_root"
        )
    current = resolved_root
    for part in resolved_candidate.relative_to(resolved_root).parts:
        current = current / part
        if current.is_symlink():
            raise ReviewInputError(
                f"{label} directory must not traverse symbolic links"
            )
    if candidate.is_relative_to(source_root):
        raw_current = source_root
        for part in candidate.relative_to(source_root).parts:
            raw_current = raw_current / part
            if raw_current.is_symlink():
                raise ReviewInputError(
                    f"{label} directory must not traverse symbolic links"
                )
    if not resolved_candidate.exists() or not resolved_candidate.is_dir():
        raise RecoveryRefusalError(
            f"absent_context: {label} directory is unavailable",
            refusal_code="absent_context",
        )
    return resolved_candidate


def _load_trusted_stages(
    source_root: Path,
    configuration: Any,
    *,
    stages_dir: Path | None,
    categories_dir: Path | None,
    use_customization_directories: bool,
) -> tuple[tuple[Stage, ...], tuple[ReviewCategory, ...]]:
    """Load and validate effective stages and categories from trusted source."""

    if categories_dir is not None and stages_dir is None:
        raise ReviewInputError("--categories-dir requires --stages-dir")
    loaded: tuple[Stage, ...]
    if stages_dir is not None:
        trusted_stages_dir = _resolve_trusted_subdir(
            source_root, stages_dir, label="stages"
        )
        trusted_categories_dir = (
            _resolve_trusted_subdir(source_root, categories_dir, label="categories")
            if categories_dir is not None
            else None
        )
        loaded = tuple(
            load_stages_from_dir(
                trusted_stages_dir,
                category_catalog=category_catalog_for_configured_stages(
                    trusted_categories_dir
                ),
            )
        )
    elif use_customization_directories:
        dot_dir = source_root / ".reviewsensei"
        if dot_dir.is_symlink():
            raise ReviewInputError(".reviewsensei must not be a symbolic link")
        custom = customization_directories(configuration)
        if custom.stages is None and custom.categories is None:
            loaded = DEFAULT_STAGES
        else:
            if custom.stages is not None:
                trusted_stages_dir = _resolve_trusted_subdir(
                    source_root, custom.stages, label="stages"
                )
            else:
                packaged_file = getattr(review_sensei, "__file__", None)
                if not isinstance(packaged_file, str):
                    raise ReviewInputError("review_sensei package path is unavailable")
                trusted_stages_dir = Path(packaged_file).parent / "default_stages"
            trusted_categories_dir = (
                _resolve_trusted_subdir(
                    source_root, custom.categories, label="categories"
                )
                if custom.categories is not None
                else None
            )
            loaded = tuple(
                load_stages_from_dir(
                    trusted_stages_dir,
                    category_catalog=category_catalog_for_configured_stages(
                        trusted_categories_dir
                    ),
                )
            )
    else:
        loaded = DEFAULT_STAGES
    if not loaded:
        raise ReviewInputError("review must contain at least one configured stage")
    if any(stage.provider_profile is not None for stage in loaded):
        raise RecoveryRefusalError(
            "absent_context: stage provider_profile routing requires original context documents",
            refusal_code="absent_context",
        )
    category_definitions: dict[str, ReviewCategory] = {}
    for stage in loaded:
        for category in stage.categories:
            existing = category_definitions.setdefault(category.id, category)
            if existing != category:
                raise ReviewInputError(
                    "review category ids must have consistent definitions across stages"
                )
    return loaded, tuple(category_definitions.values())


def _resolve_recovery_inference(
    configuration: Any,
    *,
    cli_provider: str | None,
    cli_model: str | None,
    environ: Mapping[str, str] | None,
) -> tuple[ProviderSettings, dict[str, object], str | None]:
    """Resolve effective inference from trusted configuration and supported overrides."""

    if cli_provider is not None and (
        not isinstance(cli_provider, str) or not cli_provider.strip()
    ):
        raise ReviewInputError("--provider requires a non-empty value")
    if cli_model is not None and (
        not isinstance(cli_model, str) or not cli_model.strip()
    ):
        raise ReviewInputError("--model requires a non-empty value")
    if environ is not None and not isinstance(environ, Mapping):
        raise ReviewInputError("environ must be a mapping")
    filtered_environ: dict[str, str] = (
        {
            key: environ[key]
            for key in _SUPPORTED_RECOVERY_ENV_OVERRIDES
            if key in environ
        }
        if environ is not None
        else {}
    )
    resolved = resolve_inference(
        configuration,
        cli_provider=cli_provider,
        cli_model=cli_model,
        environ=filtered_environ,
    )
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
    return settings, provider, model


def _resolve_recovery_evidence(
    *,
    evidence_policy: str,
    snapshot_sha256: str | None,
    snapshot: Mapping[str, str] | None,
) -> dict[str, object]:
    """Validate evidence policy and snapshot digest binding fail-closed."""

    if not isinstance(evidence_policy, str) or evidence_policy not in {
        "legacy",
        "confirmed",
    }:
        raise ReviewInputError("evidence_policy must be legacy or confirmed")
    if evidence_policy == "legacy":
        if snapshot_sha256 is not None or snapshot is not None:
            raise RecoveryRefusalError(
                "snapshot_or_transaction_mismatch: legacy evidence_policy cannot include snapshot evidence",
                refusal_code="snapshot_or_transaction_mismatch",
            )
        return {"evidence_policy": "legacy", "snapshot_sha256": None}
    if (
        snapshot_sha256 is None
        or not isinstance(snapshot_sha256, str)
        or not snapshot_sha256.strip()
    ):
        raise RecoveryRefusalError(
            "absent_context: confirmed evidence_policy requires snapshot_sha256",
            refusal_code="absent_context",
        )
    if snapshot is not None:
        from review_sensei.verifier import _snapshot_digest

        computed = _snapshot_digest(snapshot, limits=DEFAULT_REVIEW_LIMITS)
        if computed != snapshot_sha256:
            raise RecoveryRefusalError(
                "snapshot_or_transaction_mismatch: snapshot_sha256 does not match trusted snapshot",
                refusal_code="snapshot_or_transaction_mismatch",
            )
    return {"evidence_policy": "confirmed", "snapshot_sha256": snapshot_sha256}


def _resolve_symbol_context_policy(
    configuration: Any,
    *,
    base_sha: str,
    head_sha: str,
    enable_symbol_context: bool,
    allowed_paths: Sequence[str] | None,
    max_files: int | None,
    max_bytes: int | None,
    max_depth: int | None,
) -> tuple[SymbolAwareContextPolicy, ContextSnapshot | None, str | None]:
    """Build trusted-base symbol-aware context policy or refuse if snapshot is unavailable."""

    if type(enable_symbol_context) is not bool:
        raise ReviewInputError("enable_symbol_context must be a boolean")
    symbol_cfg = configuration.advanced.context.symbol_context
    if not enable_symbol_context and not symbol_cfg.enabled:
        return SymbolAwareContextPolicy(enabled=False), None, None
    if not isinstance(base_sha, str) or not base_sha.strip():
        raise RecoveryRefusalError(
            "absent_context: symbol-aware context requires a trusted base snapshot",
            refusal_code="absent_context",
        )
    normalized_base = base_sha.strip().lower()
    normalized_head = (
        head_sha.strip().lower()
        if isinstance(head_sha, str) and head_sha.strip()
        else None
    )
    try:
        context_snapshot = ContextSnapshot(normalized_base, kind="base")
        resolved_allowed = (
            tuple(allowed_paths)
            if allowed_paths
            else (symbol_cfg.allowed_paths or ("**",))
        )
        policy = SymbolAwareContextPolicy(
            enabled=True,
            allowed_paths=resolved_allowed,
            max_files=max_files if max_files is not None else symbol_cfg.max_files,
            max_bytes=max_bytes if max_bytes is not None else symbol_cfg.max_bytes,
            max_depth=max_depth if max_depth is not None else symbol_cfg.max_depth,
        )
    except ContextLoadError as exc:
        raise RecoveryRefusalError(
            f"absent_context: {exc}", refusal_code="absent_context"
        ) from exc
    return policy, context_snapshot, normalized_head


def reconstruct_publication_contexts(
    *,
    source_root: Path,
    diff: str,
    ledger: SessionLedger,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    policy: ReviewConvergencePolicy,
    cli_provider: str | None = None,
    cli_model: str | None = None,
    environ: Mapping[str, str] | None = None,
    evidence_policy: str = "legacy",
    snapshot_sha256: str | None = None,
    snapshot: Mapping[str, str] | None = None,
    enable_symbol_context: bool = False,
    symbol_context_allowed_paths: Sequence[str] | None = None,
    symbol_context_max_files: int | None = None,
    symbol_context_max_bytes: int | None = None,
    symbol_context_max_depth: int | None = None,
    stages_dir: Path | None = None,
    categories_dir: Path | None = None,
    use_customization_directories: bool = True,
    orchestrate_large_changes: bool = False,
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
    settings, provider, model = _resolve_recovery_inference(
        configuration,
        cli_provider=cli_provider,
        cli_model=cli_model,
        environ=environ,
    )
    budget = _configured_resource_budget(
        ResourceBudget.for_limits(DEFAULT_REVIEW_LIMITS), configuration
    )
    work_policy = review_work_policy_digest(
        configuration.advanced.review_work, budget, decision=configuration.decision
    )
    effective_stages, catalog = _load_trusted_stages(
        source_root,
        configuration,
        stages_dir=stages_dir,
        categories_dir=categories_dir,
        use_customization_directories=use_customization_directories,
    )
    stages, categories = transaction_stage_identity(effective_stages)
    reconstructed_configuration = build_transaction_configuration_context(
        provider=provider,
        model=model,
        stages=stages,
        category_policy=categories,
        publication_mode=policy.mode,
        orchestration_enabled=(
            bool(orchestrate_large_changes)
            or configuration.advanced.review_work.mode == "unified"
        ),
        work_policy_digest=work_policy,
    )
    evidence = _resolve_recovery_evidence(
        evidence_policy=evidence_policy,
        snapshot_sha256=snapshot_sha256,
        snapshot=snapshot,
    )
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
    source_context_policy, context_snapshot, untrusted_head_sha = (
        _resolve_symbol_context_policy(
            configuration,
            base_sha=base_sha,
            head_sha=head_sha,
            enable_symbol_context=enable_symbol_context,
            allowed_paths=symbol_context_allowed_paths,
            max_files=symbol_context_max_files,
            max_bytes=symbol_context_max_bytes,
            max_depth=symbol_context_max_depth,
        )
    )
    try:
        selection = build_review_context_selection(
            catalog,
            changed_paths=analysis.changed_paths,
            learnings=learnings,
            context_store=RepositoryContextStore(source_root),
            source_context_policy=source_context_policy,
            snapshot=context_snapshot,
            changed_lines=analysis.changed_lines,
        )
    except ContextLoadError as exc:
        raise RecoveryRefusalError(
            f"absent_context: {exc}", refusal_code="absent_context"
        ) from exc
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
            untrusted_head_sha=untrusted_head_sha,
            base_sha=base_sha,
            head_sha=head_sha,
            work_policy_digest=work_policy,
        ),
        provider_name=settings.name,
        stages=effective_stages,
        profile="default",
        effective_model=model,
        stage_provider_bindings={
            stage.name: (settings.name, model) for stage in effective_stages
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
        evidence_context=reconstructed.evidence,
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
    cli_provider: str | None = None,
    cli_model: str | None = None,
    environ: Mapping[str, str] | None = None,
    evidence_policy: str = "legacy",
    snapshot_sha256: str | None = None,
    snapshot: Mapping[str, str] | None = None,
    enable_symbol_context: bool = False,
    symbol_context_allowed_paths: Sequence[str] | None = None,
    symbol_context_max_files: int | None = None,
    symbol_context_max_bytes: int | None = None,
    symbol_context_max_depth: int | None = None,
    stages_dir: Path | None = None,
    categories_dir: Path | None = None,
    use_customization_directories: bool = True,
    orchestrate_large_changes: bool = False,
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
            cli_provider=cli_provider,
            cli_model=cli_model,
            environ=environ,
            evidence_policy=evidence_policy,
            snapshot_sha256=snapshot_sha256,
            snapshot=snapshot,
            enable_symbol_context=enable_symbol_context,
            symbol_context_allowed_paths=symbol_context_allowed_paths,
            symbol_context_max_files=symbol_context_max_files,
            symbol_context_max_bytes=symbol_context_max_bytes,
            symbol_context_max_depth=symbol_context_max_depth,
            stages_dir=stages_dir,
            categories_dir=categories_dir,
            use_customization_directories=use_customization_directories,
            orchestrate_large_changes=orchestrate_large_changes,
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
