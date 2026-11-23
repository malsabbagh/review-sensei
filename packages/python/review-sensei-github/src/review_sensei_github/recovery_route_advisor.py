"""Deterministic recovery-state matrix and bounded recovery-route advisor (G03).

Classifies every terminal and unclosed predecessor state across head/base
relations, attempt counters, historical executor qualification, transaction
phases, partial/rescan flags, terminal actions, pre-entry aborts, and uncertain
writes. Outputs are strictly diagnostic (`authority=False`) and never authorize
live resets, forge historical witnesses, or mutate GitHub state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .review_completion_recovery import _ReadOnlyHttp

from review_sensei.api_work import (
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
    ApiExecutionOwner,
    ApiRunAccounting,
)
from review_sensei.bounded_evidence import (
    INLINE_ENCODING,
    EvidenceReadBudget,
    canonical_bytes,
    read_inline_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.historical_completion import QUALIFIED_EXECUTORS
from review_sensei.models import ReviewResult
from review_sensei.provenance_manifest import (
    UNKNOWN_PROVENANCE,
    InvocationProvenanceEvidence,
    ProvenanceManifest,
    build_provenance_manifest,
)
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_history import (
    RETAINED_CONFIRMATION_TAIL,
    _match_action_result,
    _read_bound_action_result,
    action_result_binding,
    decode_action_result_carrier,
    inherited_preprovider_reference,
    read_action_result,
)
from .api_run_owner import (
    MAX_LATER_ATTEMPT_INSPECTION,
    ApiRunActionsEvidence,
    is_terminal_job,
    later_attempt_proved_no_business_entry,
)
from .checkpoint_result_store import CheckpointResultStore
from .checks import REVIEW_CHECK_EXTERNAL_ID, REVIEW_CHECK_NAME
from .operation_comments import GitHubIssueCommentOperationPort
from .pre_entry_abort_recovery import (
    _detect_late_native_effects,
    _ReadOnlyNoTransactionHttp,
    _validate_analysis_abort_receipt,
    classify_abort_inline_effect,
    classify_abort_review_effect,
)
from .prior_rescan_publication import admitted_rescan
from .publication_failure_recovery import (
    _NO_EARLIER_EFFECTS,
    SETTLEMENT_WITNESS_MARKER,
    PreExternalWriteSettlementWitness,
    _earlier_settled_effects,
    _find_settlement_witness_for_producer,
    _is_authenticated_bot_comment,
    iter_step_down_pages,
    parse_settlement_witness_comment,
)
from .publication_recovery import _event_key, _payload
from .session_ledger import GitHubIssueCommentSessionLedger

SCHEMA_VERSION = "review-recovery-classification-v1"
PRE_482_LIFECYCLE_OVERFLOW_DIAGNOSTIC = (
    "finding lifecycle parts exceed original checkpoint operation"
)

SUPPORTED_RECOVERY_ROUTES: frozenset[str] = frozenset(
    {
        "ordinary_rollover",
        "operator_resume_then_ordinary_rollover",
        "operator_review_remaining_or_rollover",
        "operator_verify_convergence",
        "automatic_historical_completion",
        "same_run_attempt2_retained_replay",
        "pre_entry_abort_rollover",
        "pre_external_write_rollover",
        "pending_reassessment_continuation",
        "targeted_human_disposition",
        "quarantine_published_tail_isolation",
        "quarantine_prepublication_isolation",
        "controlled_serialized_restart",
    }
)

EXECUTOR_STATUSES: frozenset[str] = frozenset(
    {
        "qualified_success",
        "ceased",
        "failed",
        "unrecognized",
        "active_or_unconfirmed",
        "not_applicable",
    }
)

DISPATCH_STATUSES: frozenset[str] = frozenset(
    {
        "known_clean",
        "uncertain_terminal_write",
    }
)

MISSING_WITNESSES: frozenset[str] = frozenset(
    {
        "authenticated_session_root",
        "authenticated_api_run_accounting",
        "confirmed_reservation_release",
        "predecessor_owner_cessation",
        "authenticated_terminal_action_result",
        "operator_pause_cleared",
        "terminal_action_outcome",
        "completed_reassessment_decisions",
        "confirmed_pre_entry_analysis_abort",
        "confirmed_pre_entry_non_dispatch",
        "consistent_pre_transaction_abort_boundary",
        "provider_non_entry_witness",
        "mutation_non_dispatch_witness",
        "root_write_settlement_witness",
        "base_sha_invariance_for_same_run_replay",
        "head_advance_for_historical_completion",
        "unused_attempt2_startup_slot",
        "terminal_transaction_settlement",
        "confirmed_terminal_write_settlement",
        "deterministic_root_write_non_entry",
        "retained_review_result_and_context",
        "retained_review_result_and_frozen_policy",
        "confirmed_native_publication_effects",
        "initial_attempt1_hosted_or_local_owner",
        "qualified_executor_successful_exit",
        "qualified_historical_executor_contract",
        "complete_non_partial_review_result",
        "explicit_rescan_historical_tail_receipt",
        "confirmed_review_publication",
        "same_head_and_base_for_attempt2_replay",
        "confirmed_failure_before_write",
        "valid_pre_external_write_settlement_witness",
        "unpublished_zero_effect_finding_lifecycle",
        "clean_native_effect_census",
    }
)

RECOVERY_STATES: frozenset[str] = frozenset(
    {
        "unenrolled_ready",
        "unauthenticated_ledger_root",
        "legacy_or_corrupt_accounting",
        "active_reservation_unreleased",
        "terminal_action_operator_paused",
        "terminal_action_without_review_transaction",
        "terminal_action_settled",
        "terminal_action_result_unauthenticated",
        "pending_action_pre_entry",
        "pending_action_started_unfinished",
        "pre_entry_analysis_abort_settled",
        "pre_external_write_settlement_ready",
        "pre_external_write_settlement_unverified",
        "pre_482_lifecycle_operation_overflow_zero_effect",
        "pre_482_lifecycle_operation_overflow_effects_present",
        "no_transaction_abort_unconfirmed",
        "conflicting_abort_with_transaction",
        "no_transaction_unclosed",
        "inherited_preprovider_unsettled",
        "terminal_review_operator_paused",
        "terminal_review_coverage_only_partial",
        "terminal_review_settled",
        "same_head_unclosed_publication_succeeded",
        "same_head_base_drift_publication_succeeded",
        "same_head_publication_attempts_exhausted",
        "unclosed_publication_uncertain_terminal_write",
        "unclosed_publication_operator_paused",
        "unclosed_publication_non_initial_owner",
        "unclosed_publication_failed_executor",
        "unclosed_publication_unrecognized_executor",
        "unclosed_publication_owner_active",
        "unclosed_publication_coverage_only_partial",
        "unclosed_publication_explicit_rescan",
        "unclosed_publication_missing_historical_evidence",
        "historical_publication_completion_eligible",
        "publication_failed_same_head_replay_eligible",
        "publication_failed_isolation_required",
        "publication_pending_same_head_replay_eligible",
        "nonterminal_transaction_isolation_required",
    }
)

ATTEMPT_EVIDENCE_PACKET_SCHEMA = "attempt-evidence-packet-v1"
REVIEWSENSEI_443_CASE_ID = "reviewsensei-443-run-43074015855-gen-11"
REVIEWSENSEI_443_REPOSITORY = "malsabbagh/review-sensei"
REVIEWSENSEI_443_PULL_REQUEST = 443
REVIEWSENSEI_443_RUN_ID = 43074015855
REVIEWSENSEI_443_GENERATION = 11
REVIEWSENSEI_459_CASE_ID = "reviewsensei-459-run-43321546017-gen-5"
REVIEWSENSEI_459_REPOSITORY = "malsabbagh/review-sensei"
REVIEWSENSEI_459_PULL_REQUEST = 459
REVIEWSENSEI_459_RUN_ID = 43321546017
REVIEWSENSEI_459_GENERATION = 5

TERMINAL_SETTLEMENT_FORMS: frozenset[str] = frozenset(
    {
        "review_terminal_transaction",
        "authenticated_terminal_action_null_review_marker",
        "authenticated_terminal_action_with_prior_review_marker",
        "pre_entry_analysis_abort",
        "pre_external_write_settlement",
        "unenrolled_session",
        "unsettled",
    }
)

DIAGNOSTIC_CAUSES: frozenset[str] = frozenset(
    {
        "not_applicable_settled",
        "unknown",
        "confirmed_pre_write_overflow",
        "confirmed_response_loss",
        "confirmed_executor_step_failure",
        "confirmed_capability_expiry",
        "confirmed_source_edit_cancellation",
    }
)

THREAD_CLEANUP_RECEIPT_STATES: frozenset[str] = frozenset(
    {
        "not_required",
        "confirmed_clean",
        "unresolved_threads_retained",
    }
)

ATTEMPT_PACKET_MISSING_PROOFS: frozenset[str] = MISSING_WITNESSES | frozenset(
    {
        "missing_close_diagnostics",
        "installed_executor_provenance",
        "publication_receipt_id",
        "check_receipt_id",
        "thread_cleanup_receipt",
        "historical_live_settlement_receipt",
    }
)


@dataclass(frozen=True)
class RecoveryClassification:
    """Closed diagnostic classification and bounded operator recovery plan."""

    state_code: str
    terminal: bool
    automatic_zero_inference: bool
    head_relation: str
    base_relation: str
    run_attempt: int | None
    executor_status: str
    transaction_phase: str
    action_phase: str | None
    dispatch_status: str
    missing_witnesses: tuple[str, ...]
    primary_route: str
    fallback_routes: tuple[str, ...]
    operator_action: str
    authority: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.authority is not False or self.schema_version != SCHEMA_VERSION:
            raise ReviewInputError("recovery classification never grants authority")
        if (
            self.state_code not in RECOVERY_STATES
            or self.executor_status not in EXECUTOR_STATUSES
            or self.dispatch_status not in DISPATCH_STATUSES
            or self.head_relation
            not in {"same_head", "different_head", "no_transaction_head"}
            or self.base_relation
            not in {"same_base", "base_drift", "no_transaction_base"}
            or self.action_phase not in {None, "pending", "terminal"}
        ):
            raise ReviewInputError("recovery classification vocabulary is invalid")
        if self.primary_route not in SUPPORTED_RECOVERY_ROUTES:
            raise ReviewInputError("recovery primary route is unsupported")
        if (
            any(
                route not in SUPPORTED_RECOVERY_ROUTES for route in self.fallback_routes
            )
            or self.primary_route in self.fallback_routes
            or len(set(self.fallback_routes)) != len(self.fallback_routes)
        ):
            raise ReviewInputError("recovery fallback routes are invalid")
        if any(
            witness not in MISSING_WITNESSES for witness in self.missing_witnesses
        ) or len(set(self.missing_witnesses)) != len(self.missing_witnesses):
            raise ReviewInputError("recovery missing witnesses are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "authority": False,
            "state_code": self.state_code,
            "terminal": self.terminal,
            "automatic_zero_inference": self.automatic_zero_inference,
            "head_relation": self.head_relation,
            "base_relation": self.base_relation,
            "run_attempt": self.run_attempt,
            "executor_status": self.executor_status,
            "transaction_phase": self.transaction_phase,
            "action_phase": self.action_phase,
            "dispatch_status": self.dispatch_status,
            "missing_witnesses": list(self.missing_witnesses),
            "primary_route": self.primary_route,
            "fallback_routes": list(self.fallback_routes),
            "operator_action": self.operator_action,
        }


def _require_sha(value: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{40}", value) is None:
        raise ReviewInputError(f"recovery {label} must be a 40-hex commit SHA")


def _attempt2_slot_available(accounting: ApiRunAccounting) -> bool:
    if accounting.owner.run_attempt != 1 or accounting.owner.job not in {
        "hosted",
        "local",
    }:
        return False
    return (accounting.owner.job, 2) not in set(accounting.startup_used)


def _verify_inline_action_result(
    record: SessionRecord, accounting: ApiRunAccounting, evidence_producer: str
) -> bool:
    action = accounting.action
    if action is None or action.get("phase") != "terminal":
        return False
    manifest = action.get("result")
    if not isinstance(manifest, dict) or manifest.get("encoding") != INLINE_ENCODING:
        return False
    try:
        binding = action_result_binding(
            record.identity(), accounting, evidence_producer
        )
        decoded = decode_action_result_carrier(
            read_inline_evidence(manifest, expected_binding=binding)
        )
        _match_action_result(decoded, binding, action)
    except ReviewInputError:
        return False
    return True


def _is_pre_482_lifecycle_overflow(failure_diagnostic: str | None) -> bool:
    return (
        isinstance(failure_diagnostic, str)
        and PRE_482_LIFECYCLE_OVERFLOW_DIAGNOSTIC in failure_diagnostic.casefold()
    )


def _finding_lifecycle_dirty_for_tx(record: SessionRecord, tx: Any) -> bool:
    if record.publication_staging is not None:
        return True
    lifecycle = record.finding_lifecycle
    if lifecycle is None:
        return False
    if not isinstance(lifecycle, Mapping):
        return True
    op_id = lifecycle.get("operation_id")
    if tx is None:
        return op_id is not None
    return not isinstance(op_id, str) or op_id == tx.transaction_id


def _parse_settlement_witness(
    settlement_witness: Any,
) -> PreExternalWriteSettlementWitness | None:
    if isinstance(settlement_witness, PreExternalWriteSettlementWitness):
        return settlement_witness
    if isinstance(settlement_witness, Mapping):
        try:
            fields = {
                key: settlement_witness[key]
                for key in (
                    "schema_version",
                    "repository",
                    "repository_id",
                    "pull_request",
                    "run_id",
                    "owner",
                    "settled_root_sha256",
                    "settled_generation",
                    "settled_phase",
                    "boundary",
                    "transaction_id",
                    "base_sha",
                    "head_sha",
                    "producer",
                    "content_sha256",
                )
            }
            if len(settlement_witness) != len(fields):
                return None
            return PreExternalWriteSettlementWitness(**fields)
        except (KeyError, ReviewInputError, TypeError, ValueError):
            return None
    return None


def _validate_settlement_witness_for_record(
    settlement_witness: Any,
    *,
    record: SessionRecord,
    old: ApiRunAccounting,
    tx: Any,
    tx_phase: str,
    evidence_producer: str | None,
) -> bool:
    witness = _parse_settlement_witness(settlement_witness)
    if witness is None:
        return False
    expected_boundary = (
        "post_checkpoint_pre_write" if tx is not None else "pre_checkpoint_validation"
    )
    expected_tx_id = tx.transaction_id if tx is not None else None
    if (
        witness.repository != record.repository
        or witness.repository_id != record.repository_id
        or witness.pull_request != record.pull_request
        or witness.run_id != old.identity.run_id
        or witness.owner
        != {
            "run_attempt": old.owner.run_attempt,
            "job": old.owner.job,
            "generation": old.owner.generation,
        }
        or witness.settled_root_sha256 != record.record_sha256
        or witness.settled_generation != record.generation
        or witness.settled_phase != tx_phase
        or witness.boundary != expected_boundary
        or witness.transaction_id != expected_tx_id
        or (evidence_producer is not None and witness.producer != evidence_producer)
    ):
        return False
    if tx is not None:
        return witness.base_sha == tx.base_sha and witness.head_sha == tx.head_sha
    return (
        record.failed_attempts > 0
        and record.failed_attempts_head_sha == witness.head_sha
    )


def _classify_pre_write_or_pre_482_state(
    record: SessionRecord,
    old: ApiRunAccounting,
    *,
    tx: Any,
    tx_phase: str,
    head_relation: str,
    base_relation: str,
    attempt: int,
    executor_status: str,
    dispatch_status: str,
    evidence_producer: str | None,
    native_effects_confirmed: bool | None,
    settlement_witness: Any,
    failure_diagnostic: str | None,
) -> RecoveryClassification | None:
    if tx is not None and tx.phase not in {"publication_failed", "publication_pending"}:
        return None
    owner_ceased = executor_status in {"ceased", "failed", "qualified_success"}
    lifecycle_clean = not _finding_lifecycle_dirty_for_tx(record, tx)
    native_clean = native_effects_confirmed is True
    dispatch_clean = dispatch_status == "known_clean" and old.uncertain == 0

    if settlement_witness is not None and (
        tx is None or tx.phase == "publication_failed"
    ):
        witness_valid = (
            settlement_witness is not False
            and _validate_settlement_witness_for_record(
                settlement_witness,
                record=record,
                old=old,
                tx=tx,
                tx_phase=tx_phase,
                evidence_producer=evidence_producer,
            )
        )
        if (
            witness_valid
            and lifecycle_clean
            and owner_ceased
            and native_clean
            and dispatch_clean
            and not record.operator_paused
        ):
            return RecoveryClassification(
                state_code="pre_external_write_settlement_ready",
                terminal=True,
                automatic_zero_inference=True,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx_phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(),
                primary_route="pre_external_write_rollover",
                fallback_routes=("ordinary_rollover",),
                operator_action=(
                    "Verified `pre-external-write-settlement-v1` witness, "
                    "zero-effect finding lifecycle, clean native census, and "
                    "ceased owner; automatic pre-external-write rollover is ready "
                    "(read-only diagnosis, authority=False)."
                ),
            )
        missing_witness_reasons: list[str] = []
        if not witness_valid:
            missing_witness_reasons.append(
                "valid_pre_external_write_settlement_witness"
            )
        if not lifecycle_clean:
            missing_witness_reasons.append("unpublished_zero_effect_finding_lifecycle")
        if not owner_ceased:
            missing_witness_reasons.append("predecessor_owner_cessation")
        if not native_clean:
            missing_witness_reasons.append("clean_native_effect_census")
        if not dispatch_clean:
            missing_witness_reasons.append("confirmed_failure_before_write")
        if record.operator_paused:
            missing_witness_reasons.append("operator_pause_cleared")
        return RecoveryClassification(
            state_code="pre_external_write_settlement_unverified",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx_phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=tuple(missing_witness_reasons),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Pre-external-write settlement rollover lacks a verified witness, "
                "owner cessation, clean lifecycle, or clean native census; hold "
                "and recover via controlled serialized restart."
            ),
        )

    if _is_pre_482_lifecycle_overflow(failure_diagnostic):
        if (
            lifecycle_clean
            and owner_ceased
            and native_clean
            and dispatch_clean
            and not record.operator_paused
        ):
            return RecoveryClassification(
                state_code="pre_482_lifecycle_operation_overflow_zero_effect",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx_phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("valid_pre_external_write_settlement_witness",),
                primary_route="controlled_serialized_restart",
                fallback_routes=(),
                operator_action=(
                    "Pre-#482 `finding lifecycle parts exceed original checkpoint "
                    "operation` record verified unpublished zero-effect "
                    "`finding_lifecycle`, empty native census, and ceased exact/later "
                    "attempts; lacks runtime settlement witness, so use designated "
                    "controlled serialized restart."
                ),
            )
        missing_pre_482: list[str] = ["valid_pre_external_write_settlement_witness"]
        if not lifecycle_clean:
            missing_pre_482.append("unpublished_zero_effect_finding_lifecycle")
        if not native_clean:
            missing_pre_482.append("clean_native_effect_census")
        if not owner_ceased:
            missing_pre_482.append("predecessor_owner_cessation")
        if not dispatch_clean:
            missing_pre_482.append("confirmed_failure_before_write")
        if record.operator_paused:
            missing_pre_482.append("operator_pause_cleared")
        return RecoveryClassification(
            state_code="pre_482_lifecycle_operation_overflow_effects_present",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx_phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=tuple(missing_pre_482),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Pre-#482 lifecycle checkpoint overflow record retains recorded "
                "or uncertain native/lifecycle effects or active owner; isolate "
                "via controlled serialized restart."
            ),
        )
    return None


def classify_recovery_state(
    record: SessionRecord,
    *,
    current_base: str,
    current_head: str,
    executor_status: str = "not_applicable",
    dispatch_status: str = "known_clean",
    coverage_only_partial: bool = False,
    explicit_rescan: bool | None = None,
    retained_context_available: bool = True,
    native_effects_confirmed: bool | None = None,
    action_result_authenticated: bool | None = None,
    evidence_producer: str | None = None,
    settlement_witness: Any = None,
    failure_diagnostic: str | None = None,
) -> RecoveryClassification:
    """Classify a `SessionRecord` into an exact state, witness set, and route."""
    if not isinstance(record, SessionRecord):
        raise ReviewInputError("recovery state requires a SessionRecord")
    if (
        action_result_authenticated is not None
        and type(action_result_authenticated) is not bool
    ):
        raise ReviewInputError("action_result_authenticated must be None or a bool")
    _require_sha(current_base, "current_base")
    _require_sha(current_head, "current_head")
    if executor_status not in EXECUTOR_STATUSES:
        raise ReviewInputError("recovery executor_status is unsupported")
    if dispatch_status not in DISPATCH_STATUSES:
        raise ReviewInputError("recovery dispatch_status is unsupported")

    tx = record.transaction
    tx_phase = tx.phase if tx is not None else "no-transaction"
    head_relation = (
        "no_transaction_head"
        if tx is None
        else ("same_head" if tx.head_sha == current_head else "different_head")
    )
    base_relation = (
        "no_transaction_base"
        if tx is None
        else ("same_base" if tx.base_sha == current_base else "base_drift")
    )

    if record.api_run_accounting is None:
        return RecoveryClassification(
            state_code="legacy_or_corrupt_accounting",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=None,
            executor_status=executor_status,
            transaction_phase=tx_phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=("authenticated_api_run_accounting",),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Archive the legacy or unauthenticated session root via one "
                "designated controlled serialized restart after capability expiry."
            ),
        )

    try:
        old = ApiRunAccounting.from_document(record.api_run_accounting)
    except ReviewInputError:
        return RecoveryClassification(
            state_code="legacy_or_corrupt_accounting",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=None,
            executor_status=executor_status,
            transaction_phase=tx_phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=("authenticated_api_run_accounting",),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Archive the unsupported caller accounting document via one "
                "designated controlled serialized restart after capability expiry."
            ),
        )

    classification = _classify_accounted_recovery_state(
        record,
        old,
        tx=tx,
        tx_phase=tx_phase,
        current_head=current_head,
        head_relation=head_relation,
        base_relation=base_relation,
        executor_status=executor_status,
        dispatch_status=dispatch_status,
        coverage_only_partial=coverage_only_partial,
        explicit_rescan=explicit_rescan,
        retained_context_available=retained_context_available,
        native_effects_confirmed=native_effects_confirmed,
        action_result_authenticated=action_result_authenticated,
        evidence_producer=evidence_producer,
        settlement_witness=settlement_witness,
        failure_diagnostic=failure_diagnostic,
    )
    if old.version == PARTITIONED_ACTION_ACCOUNTING_VERSION and (
        classification.primary_route != "controlled_serialized_restart"
        or classification.fallback_routes
        or classification.automatic_zero_inference
    ):
        return replace(
            classification,
            automatic_zero_inference=False,
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                classification.operator_action
                + " (`caller-requests-v3` is reader-only; recover via "
                "controlled serialized restart.)"
            ),
        )
    return classification


def _classify_accounted_recovery_state(
    record: SessionRecord,
    old: ApiRunAccounting,
    *,
    tx: Any,
    tx_phase: str,
    current_head: str,
    head_relation: str,
    base_relation: str,
    executor_status: str,
    dispatch_status: str,
    coverage_only_partial: bool,
    explicit_rescan: bool | None,
    retained_context_available: bool,
    native_effects_confirmed: bool | None,
    action_result_authenticated: bool | None,
    evidence_producer: str | None,
    settlement_witness: Any = None,
    failure_diagnostic: str | None = None,
) -> RecoveryClassification:

    attempt = old.owner.run_attempt
    action_phase = (
        str(old.action.get("phase"))
        if isinstance(old.action, dict)
        and old.action.get("phase") in {"pending", "terminal"}
        else None
    )

    # 1. Active reservation boundary: blocks ordinary rollover until released/replayed.
    if record.reservation_id is not None or record.reserved_slot is not None:
        can_attempt2 = (
            tx is not None
            and head_relation == "same_head"
            and base_relation == "same_base"
            and _attempt2_slot_available(old)
            and executor_status != "active_or_unconfirmed"
            and dispatch_status == "known_clean"
            and retained_context_available
        )
        if can_attempt2:
            return RecoveryClassification(
                state_code="active_reservation_unreleased",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx_phase,
                action_phase=action_phase,
                dispatch_status=dispatch_status,
                missing_witnesses=(
                    "confirmed_reservation_release",
                    "terminal_transaction_settlement",
                ),
                primary_route="same_run_attempt2_retained_replay",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Rerun attempt 2 of the ceased Actions run on the unchanged "
                    "head and base, or use controlled serialized restart after expiry."
                ),
            )
        return RecoveryClassification(
            state_code="active_reservation_unreleased",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx_phase,
            action_phase=action_phase,
            dispatch_status=dispatch_status,
            missing_witnesses=(
                "confirmed_reservation_release",
                "predecessor_owner_cessation",
            ),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Confirm predecessor cessation and capability expiry, then "
                "execute one designated controlled serialized restart."
            ),
        )

    # 2. Explicit command / reply action states (`old.action is not None`).
    if old.action is not None:
        if action_phase == "terminal":
            verified_action = action_result_authenticated
            if verified_action is None:
                if (
                    evidence_producer is not None
                    and isinstance(old.action.get("result"), dict)
                    and old.action["result"].get("encoding") == INLINE_ENCODING
                ):
                    verified_action = _verify_inline_action_result(
                        record, old, evidence_producer
                    )
                else:
                    verified_action = False
            if not verified_action:
                fallbacks: tuple[str, ...] = (
                    ("quarantine_published_tail_isolation",)
                    if tx is not None and tx.phase == "publication_succeeded"
                    else ()
                )
                return RecoveryClassification(
                    state_code="terminal_action_result_unauthenticated",
                    terminal=False,
                    automatic_zero_inference=False,
                    head_relation=head_relation,
                    base_relation=base_relation,
                    run_attempt=attempt,
                    executor_status=executor_status,
                    transaction_phase=tx_phase,
                    action_phase="terminal",
                    dispatch_status=dispatch_status,
                    missing_witnesses=("authenticated_terminal_action_result",),
                    primary_route="controlled_serialized_restart",
                    fallback_routes=fallbacks,
                    operator_action=(
                        "Do not roll over an unauthenticated action carrier; "
                        "isolate via controlled serialized restart after expiry."
                    ),
                )
            if record.operator_paused:
                return RecoveryClassification(
                    state_code="terminal_action_operator_paused",
                    terminal=True,
                    automatic_zero_inference=False,
                    head_relation=head_relation,
                    base_relation=base_relation,
                    run_attempt=attempt,
                    executor_status=executor_status,
                    transaction_phase=tx_phase,
                    action_phase="terminal",
                    dispatch_status=dispatch_status,
                    missing_witnesses=("operator_pause_cleared",),
                    primary_route="operator_resume_then_ordinary_rollover",
                    fallback_routes=("ordinary_rollover",),
                    operator_action=(
                        "Terminal command action is valid (`terminal_transaction=null`); "
                        "issue `@reviewsensei review continue` to resume review runs."
                    ),
                )
            if tx is None:
                return RecoveryClassification(
                    state_code="terminal_action_without_review_transaction",
                    terminal=True,
                    automatic_zero_inference=True,
                    head_relation=head_relation,
                    base_relation=base_relation,
                    run_attempt=attempt,
                    executor_status=executor_status,
                    transaction_phase="no-transaction",
                    action_phase="terminal",
                    dispatch_status=dispatch_status,
                    missing_witnesses=(),
                    primary_route="ordinary_rollover",
                    fallback_routes=(),
                    operator_action=(
                        "No recovery needed: authenticated terminal command action "
                        "with `terminal_transaction=null` and null review marker is terminal."
                    ),
                )
            settled_fallbacks: list[str] = []
            if coverage_only_partial:
                settled_fallbacks.append("operator_review_remaining_or_rollover")
            if head_relation == "same_head" and base_relation == "same_base":
                settled_fallbacks.append("operator_verify_convergence")
            return RecoveryClassification(
                state_code="terminal_action_settled",
                terminal=True,
                automatic_zero_inference=True,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx_phase,
                action_phase="terminal",
                dispatch_status=dispatch_status,
                missing_witnesses=(),
                primary_route="ordinary_rollover",
                fallback_routes=tuple(settled_fallbacks),
                operator_action=(
                    "No recovery needed: authenticated terminal action settles "
                    "the predecessor (`terminal_transaction=null` is valid per #430)."
                ),
            )

        # Non-terminal (`action.pending`) state: older `publication_succeeded` never
        # settles a later pending reply or command action (#408 / G30).
        started = bool(old.action.get("execution_started"))
        intent = old.action.get("intent")
        intent_head = intent.get("head_sha") if isinstance(intent, dict) else None
        if not started:
            pending_fallbacks: tuple[str, ...] = (
                ("quarantine_published_tail_isolation",)
                if tx is not None and tx.phase == "publication_succeeded"
                else ()
            )
            return RecoveryClassification(
                state_code="pending_action_pre_entry",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx_phase,
                action_phase="pending",
                dispatch_status=dispatch_status,
                missing_witnesses=(
                    "terminal_action_outcome",
                    "predecessor_owner_cessation",
                ),
                primary_route="controlled_serialized_restart",
                fallback_routes=pending_fallbacks,
                operator_action=(
                    "Unstarted pending action never closed; confirm owner "
                    "cessation and recover via controlled serialized restart."
                ),
            )
        if intent_head == current_head:
            return RecoveryClassification(
                state_code="pending_action_started_unfinished",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx_phase,
                action_phase="pending",
                dispatch_status=dispatch_status,
                missing_witnesses=(
                    "terminal_action_outcome",
                    "completed_reassessment_decisions",
                ),
                primary_route="pending_reassessment_continuation",
                fallback_routes=(
                    "targeted_human_disposition",
                    "controlled_serialized_restart",
                ),
                operator_action=(
                    "Continue remaining pending reassessment items under bounded "
                    "funding without duplicating the pending receipt, or apply "
                    "explicit human disposition (`override`/`accept-risk`/`rescan`)."
                ),
            )
        return RecoveryClassification(
            state_code="pending_action_started_unfinished",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx_phase,
            action_phase="pending",
            dispatch_status=dispatch_status,
            missing_witnesses=(
                "terminal_action_outcome",
                "completed_reassessment_decisions",
            ),
            primary_route="controlled_serialized_restart",
            fallback_routes=("targeted_human_disposition",),
            operator_action=(
                "PR head advanced while pending reassessment remained open; "
                "recover via controlled serialized restart and re-evaluate on new head."
            ),
        )

    # 3. Pre-entry `analysis_abort` and `no-transaction` boundaries (G29).
    if old.analysis_abort is not None:
        if tx is not None:
            return RecoveryClassification(
                state_code="conflicting_abort_with_transaction",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx_phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("consistent_pre_transaction_abort_boundary",),
                primary_route="controlled_serialized_restart",
                fallback_routes=(),
                operator_action=(
                    "Conflicting `analysis_abort` and review transaction markers; "
                    "archive via controlled serialized restart after expiry."
                ),
            )
        abort_valid, _abort_tx, _abort_failures = _validate_analysis_abort_receipt(
            record, old
        )
        owner_ceased = executor_status in {"ceased", "failed", "qualified_success"}
        late_effects_clean = native_effects_confirmed is True
        if (
            abort_valid
            and owner_ceased
            and late_effects_clean
            and old.uncertain == 0
            and dispatch_status == "known_clean"
        ):
            return RecoveryClassification(
                state_code="pre_entry_analysis_abort_settled",
                terminal=True,
                automatic_zero_inference=True,
                head_relation="no_transaction_head",
                base_relation="no_transaction_base",
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase="no-transaction",
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(),
                primary_route="pre_entry_abort_rollover",
                fallback_routes=("ordinary_rollover",),
                operator_action=(
                    "Confirmed pre-entry `analysis_abort` (`phase=no-transaction`) "
                    "is terminal; ordinary rollover proceeds once owner has ceased."
                ),
            )
        missing_abort: list[str] = []
        if not abort_valid:
            missing_abort.append("confirmed_pre_entry_analysis_abort")
        if not owner_ceased:
            missing_abort.append("predecessor_owner_cessation")
        if (
            old.uncertain != 0
            or dispatch_status != "known_clean"
            or not late_effects_clean
        ):
            missing_abort.append("confirmed_pre_entry_non_dispatch")
        return RecoveryClassification(
            state_code="no_transaction_abort_unconfirmed",
            terminal=False,
            automatic_zero_inference=False,
            head_relation="no_transaction_head",
            base_relation="no_transaction_base",
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase="no-transaction",
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=tuple(missing_abort),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Pre-entry abort lacks non-dispatch or zero-provider proof; keep "
                "conservative holds and recover via controlled serialized restart."
            ),
        )

    if tx is None:
        pre_write_classification = _classify_pre_write_or_pre_482_state(
            record,
            old,
            tx=None,
            tx_phase="no-transaction",
            head_relation="no_transaction_head",
            base_relation="no_transaction_base",
            attempt=attempt,
            executor_status=executor_status,
            dispatch_status=dispatch_status,
            evidence_producer=evidence_producer,
            native_effects_confirmed=native_effects_confirmed,
            settlement_witness=settlement_witness,
            failure_diagnostic=failure_diagnostic,
        )
        if pre_write_classification is not None:
            return pre_write_classification
        return RecoveryClassification(
            state_code="no_transaction_unclosed",
            terminal=False,
            automatic_zero_inference=False,
            head_relation="no_transaction_head",
            base_relation="no_transaction_base",
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase="no-transaction",
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=(
                "confirmed_pre_entry_analysis_abort",
                "provider_non_entry_witness",
                "mutation_non_dispatch_witness",
            ),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Unclosed `phase=no-transaction` lacks pre-entry `analysis_abort` "
                "proof; retain unknown-cost liability and use controlled serialized restart."
            ),
        )

    # 4. Inherited v2 pre-provider reference boundary.
    if inherited_preprovider_reference(record, old) is not None:
        return RecoveryClassification(
            state_code="inherited_preprovider_unsettled",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx_phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=(
                "provider_non_entry_witness",
                "root_write_settlement_witness",
            ),
            primary_route="controlled_serialized_restart",
            fallback_routes=("quarantine_published_tail_isolation",),
            operator_action=(
                "Historical rollover enrolled a successor that stopped before "
                "settling its transaction; isolate via controlled serialized restart."
            ),
        )

    # 5. Terminal review transactions (`publication_succeeded` / `publication_suppressed`).
    if (
        tx.phase in {"publication_succeeded", "publication_suppressed"}
        and old.terminal_transaction == tx.transaction_id
    ):
        if record.operator_paused:
            return RecoveryClassification(
                state_code="terminal_review_operator_paused",
                terminal=True,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("operator_pause_cleared",),
                primary_route="operator_resume_then_ordinary_rollover",
                fallback_routes=("ordinary_rollover",),
                operator_action=(
                    "Review transaction is terminal, but PR is paused; dispatch "
                    "`@reviewsensei review continue` before ordinary review runs."
                ),
            )
        if coverage_only_partial:
            if head_relation == "same_head" and base_relation == "same_base":
                return RecoveryClassification(
                    state_code="terminal_review_coverage_only_partial",
                    terminal=True,
                    automatic_zero_inference=True,
                    head_relation=head_relation,
                    base_relation=base_relation,
                    run_attempt=attempt,
                    executor_status=executor_status,
                    transaction_phase=tx.phase,
                    action_phase=None,
                    dispatch_status=dispatch_status,
                    missing_witnesses=(),
                    primary_route="operator_review_remaining_or_rollover",
                    fallback_routes=("ordinary_rollover",),
                    operator_action=(
                        "Partial review closed cleanly; dispatch "
                        "`@reviewsensei review remaining` on same head or push a new commit."
                    ),
                )
            return RecoveryClassification(
                state_code="terminal_review_coverage_only_partial",
                terminal=True,
                automatic_zero_inference=True,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(),
                primary_route="ordinary_rollover",
                fallback_routes=("operator_review_remaining_or_rollover",),
                operator_action=(
                    "Partial predecessor review closed cleanly; ordinary new-run "
                    "rollover proceeds automatically."
                ),
            )
        review_fallbacks: tuple[str, ...] = (
            ("operator_verify_convergence",)
            if head_relation == "same_head" and base_relation == "same_base"
            else ()
        )
        return RecoveryClassification(
            state_code="terminal_review_settled",
            terminal=True,
            automatic_zero_inference=True,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx.phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=(),
            primary_route="ordinary_rollover",
            fallback_routes=review_fallbacks,
            operator_action=(
                "No recovery needed: predecessor review transaction is settled; "
                "use `@reviewsensei verify` on same head if native convergence changed."
            ),
        )

    # Determine explicit rescan status when not pre-computed by caller.
    rescan_ambiguous = False
    if explicit_rescan is None:
        try:
            explicit_rescan = admitted_rescan(record) is not None
        except ReviewInputError:
            explicit_rescan = True
            rescan_ambiguous = True

    # 6. Unclosed `publication_succeeded` matrix (G03 core).
    if tx.phase == "publication_succeeded":
        if record.operator_paused:
            return RecoveryClassification(
                state_code="unclosed_publication_operator_paused",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(
                    "operator_pause_cleared",
                    "terminal_transaction_settlement",
                ),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Paused unclosed `publication_succeeded` is outside automatic "
                    "historical completion; isolate via published-tail quarantine "
                    "or controlled serialized restart."
                ),
            )

        if dispatch_status != "known_clean":
            return RecoveryClassification(
                state_code="unclosed_publication_uncertain_terminal_write",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(
                    "confirmed_terminal_write_settlement",
                    "deterministic_root_write_non_entry",
                ),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Terminal close response was lost or uncertain; do not assume "
                    "non-dispatch. Recover via published-tail quarantine or "
                    "controlled serialized restart after capability expiry."
                ),
            )

        if head_relation == "same_head":
            if base_relation != "same_base":
                return RecoveryClassification(
                    state_code="same_head_base_drift_publication_succeeded",
                    terminal=False,
                    automatic_zero_inference=False,
                    head_relation="same_head",
                    base_relation="base_drift",
                    run_attempt=attempt,
                    executor_status=executor_status,
                    transaction_phase=tx.phase,
                    action_phase=None,
                    dispatch_status=dispatch_status,
                    missing_witnesses=(
                        "base_sha_invariance_for_same_run_replay",
                        "head_advance_for_historical_completion",
                    ),
                    primary_route="quarantine_published_tail_isolation",
                    fallback_routes=("controlled_serialized_restart",),
                    operator_action=(
                        "Same-head base drift blocks attempt-2 replay and same-head "
                        "blocks narrow historical completion; isolate via same-head "
                        "`quarantine-published-activation-v3` or push a new head commit."
                    ),
                )
            if not _attempt2_slot_available(old):
                return RecoveryClassification(
                    state_code="same_head_publication_attempts_exhausted",
                    terminal=False,
                    automatic_zero_inference=False,
                    head_relation="same_head",
                    base_relation="same_base",
                    run_attempt=attempt,
                    executor_status=executor_status,
                    transaction_phase=tx.phase,
                    action_phase=None,
                    dispatch_status=dispatch_status,
                    missing_witnesses=(
                        "unused_attempt2_startup_slot",
                        "terminal_transaction_settlement",
                    ),
                    primary_route="quarantine_published_tail_isolation",
                    fallback_routes=("controlled_serialized_restart",),
                    operator_action=(
                        "Same-run attempt-2 slot is already consumed or unavailable; "
                        "isolate via same-head `quarantine-published-activation-v3` "
                        "or controlled serialized restart."
                    ),
                )
            missing_same_head: list[str] = [
                "head_advance_for_historical_completion",
                "terminal_transaction_settlement",
            ]
            if executor_status == "active_or_unconfirmed":
                missing_same_head.append("predecessor_owner_cessation")
            if not retained_context_available or rescan_ambiguous:
                missing_same_head.append("retained_review_result_and_context")
            if native_effects_confirmed is False:
                missing_same_head.append("confirmed_native_publication_effects")
            return RecoveryClassification(
                state_code="same_head_unclosed_publication_succeeded",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="same_head",
                base_relation="same_base",
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=tuple(missing_same_head),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Unclosed `publication_succeeded` cannot replay on same-run "
                    "attempt 2 (`retained_analysis` refuses unverified published "
                    "tails); isolate via same-head `quarantine-published-activation-v3` "
                    "preserving obligations, push a new head commit if qualified, "
                    "or use controlled serialized restart."
                ),
            )

        # Different-head unclosed `publication_succeeded`: check narrow historical
        # completion rules in exact deterministic precedence.
        if (
            old.owner.run_attempt != 1
            or old.owner.generation != 0
            or old.owner.job not in {"hosted", "local"}
        ):
            return RecoveryClassification(
                state_code="unclosed_publication_non_initial_owner",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="different_head",
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("initial_attempt1_hosted_or_local_owner",),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Predecessor owner is not initial `(attempt=1, generation=0)` "
                    "hosted/local; isolate via published-tail quarantine or "
                    "controlled serialized restart."
                ),
            )
        if executor_status == "active_or_unconfirmed":
            return RecoveryClassification(
                state_code="unclosed_publication_owner_active",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="different_head",
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(
                    "predecessor_owner_cessation",
                    "qualified_executor_successful_exit",
                ),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Wait for predecessor Actions run cessation before attempting "
                    "historical completion or quarantine isolation."
                ),
            )
        if executor_status == "failed":
            return RecoveryClassification(
                state_code="unclosed_publication_failed_executor",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="different_head",
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("qualified_executor_successful_exit",),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Failed historical executor never proves root-write non-entry; "
                    "isolate via published-tail quarantine preserving obligations, "
                    "or use controlled serialized restart."
                ),
            )
        if executor_status != "qualified_success":
            return RecoveryClassification(
                state_code="unclosed_publication_unrecognized_executor",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="different_head",
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("qualified_historical_executor_contract",),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Historical workflow SHA is not in `QUALIFIED_EXECUTORS`; "
                    "isolate via published-tail quarantine or controlled restart."
                ),
            )
        if coverage_only_partial:
            return RecoveryClassification(
                state_code="unclosed_publication_coverage_only_partial",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="different_head",
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("complete_non_partial_review_result",),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Coverage-only partial review is outside automatic historical "
                    "completion; isolate via published-tail quarantine or "
                    "controlled serialized restart."
                ),
            )
        if explicit_rescan:
            return RecoveryClassification(
                state_code="unclosed_publication_explicit_rescan",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="different_head",
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("explicit_rescan_historical_tail_receipt",),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Explicit rescan publication requires its separate tail "
                    "receipt; isolate via published-tail quarantine or controlled restart."
                ),
            )
        missing_hist: list[str] = []
        if not retained_context_available:
            missing_hist.append("retained_review_result_and_frozen_policy")
        if native_effects_confirmed is False:
            missing_hist.append("confirmed_native_publication_effects")
        if missing_hist:
            return RecoveryClassification(
                state_code="unclosed_publication_missing_historical_evidence",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="different_head",
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=tuple(missing_hist),
                primary_route="quarantine_published_tail_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Missing retained review result, frozen policy, or confirmed "
                    "native effects; isolate via published-tail quarantine or "
                    "controlled serialized restart."
                ),
            )
        return RecoveryClassification(
            state_code="historical_publication_completion_eligible",
            terminal=False,
            automatic_zero_inference=True,
            head_relation="different_head",
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status="qualified_success",
            transaction_phase=tx.phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=(),
            primary_route="automatic_historical_completion",
            fallback_routes=(
                "quarantine_published_tail_isolation",
                "controlled_serialized_restart",
            ),
            operator_action=(
                "Eligible for automatic zero-inference historical completion on "
                "the next authorized new-head `review-run`."
            ),
        )

    # 7. Unclosed `publication_failed` matrix (G03 / G31).
    if tx.phase == "publication_failed":
        pre_write_classification = _classify_pre_write_or_pre_482_state(
            record,
            old,
            tx=tx,
            tx_phase=tx.phase,
            head_relation=head_relation,
            base_relation=base_relation,
            attempt=attempt,
            executor_status=executor_status,
            dispatch_status=dispatch_status,
            evidence_producer=evidence_producer,
            native_effects_confirmed=native_effects_confirmed,
            settlement_witness=settlement_witness,
            failure_diagnostic=failure_diagnostic,
        )
        if pre_write_classification is not None:
            return pre_write_classification
        if (
            head_relation == "same_head"
            and base_relation == "same_base"
            and _attempt2_slot_available(old)
            and not record.operator_paused
            and executor_status != "active_or_unconfirmed"
            and dispatch_status == "known_clean"
            and retained_context_available
        ):
            return RecoveryClassification(
                state_code="publication_failed_same_head_replay_eligible",
                terminal=False,
                automatic_zero_inference=True,
                head_relation="same_head",
                base_relation="same_base",
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(
                    "confirmed_review_publication",
                    "terminal_transaction_settlement",
                ),
                primary_route="same_run_attempt2_retained_replay",
                fallback_routes=(
                    "quarantine_prepublication_isolation",
                    "controlled_serialized_restart",
                ),
                operator_action=(
                    "Rerun attempt 2 of the same Actions run on unchanged head "
                    "and base to replay retained publication (including confirmed "
                    "narrative archive when needed) with zero new inference."
                ),
            )
        missing_failed: list[str] = [
            "confirmed_review_publication",
            "terminal_transaction_settlement",
        ]
        if head_relation != "same_head" or base_relation != "same_base":
            missing_failed.append("same_head_and_base_for_attempt2_replay")
        elif not _attempt2_slot_available(old):
            missing_failed.append("unused_attempt2_startup_slot")
        if dispatch_status != "known_clean":
            missing_failed.append("confirmed_failure_before_write")
        if not retained_context_available:
            missing_failed.append("retained_review_result_and_context")
        if record.operator_paused:
            missing_failed.append("operator_pause_cleared")
        if executor_status == "active_or_unconfirmed":
            missing_failed.append("predecessor_owner_cessation")

        quarantine_structural_ok = (
            head_relation == "different_head"
            and old.owner.run_attempt == 1
            and old.owner.generation == 0
            and old.owner.job in {"hosted", "local"}
            and not explicit_rescan
            and record.quarantine_lineage is None
            and dispatch_status == "known_clean"
            and retained_context_available
        )
        can_prepub_quarantine = quarantine_structural_ok and not record.operator_paused
        if can_prepub_quarantine:
            return RecoveryClassification(
                state_code="publication_failed_isolation_required",
                terminal=False,
                automatic_zero_inference=False,
                head_relation=head_relation,
                base_relation=base_relation,
                run_attempt=attempt,
                executor_status=executor_status,
                transaction_phase=tx.phase,
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=tuple(missing_failed),
                primary_route="quarantine_prepublication_isolation",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Isolate prepublication failure on the new head via "
                    "authenticated `literal-blocker-path-prepublication-v1` or "
                    "`body-limit-prepublication-v1` quarantine preserving all findings."
                ),
            )
        fallbacks_failed: tuple[str, ...] = (
            ("quarantine_prepublication_isolation",)
            if quarantine_structural_ok and record.operator_paused
            else ()
        )
        return RecoveryClassification(
            state_code="publication_failed_isolation_required",
            terminal=False,
            automatic_zero_inference=False,
            head_relation=head_relation,
            base_relation=base_relation,
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx.phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=tuple(missing_failed),
            primary_route="controlled_serialized_restart",
            fallback_routes=fallbacks_failed,
            operator_action=(
                "Prepublication failure is outside same-head replay and "
                "prepublication quarantine predicates; recover via controlled "
                "serialized restart after capability expiry."
            ),
        )

    # 8. Remaining non-terminal phases (`analysis`, `publication_pending`, unclosed `publication_suppressed`).
    if tx.phase == "publication_pending":
        pre_write_pending = _classify_pre_write_or_pre_482_state(
            record,
            old,
            tx=tx,
            tx_phase=tx.phase,
            head_relation=head_relation,
            base_relation=base_relation,
            attempt=attempt,
            executor_status=executor_status,
            dispatch_status=dispatch_status,
            evidence_producer=evidence_producer,
            native_effects_confirmed=native_effects_confirmed,
            settlement_witness=None,
            failure_diagnostic=failure_diagnostic,
        )
        if pre_write_pending is not None:
            return pre_write_pending
    same_run_replay_ok = (
        head_relation == "same_head"
        and base_relation == "same_base"
        and _attempt2_slot_available(old)
        and not record.operator_paused
        and executor_status != "active_or_unconfirmed"
        and dispatch_status == "known_clean"
        and retained_context_available
    )
    if tx.phase == "publication_pending" and same_run_replay_ok:
        return RecoveryClassification(
            state_code="publication_pending_same_head_replay_eligible",
            terminal=False,
            automatic_zero_inference=True,
            head_relation="same_head",
            base_relation="same_base",
            run_attempt=attempt,
            executor_status=executor_status,
            transaction_phase=tx.phase,
            action_phase=None,
            dispatch_status=dispatch_status,
            missing_witnesses=(
                "confirmed_review_publication",
                "terminal_transaction_settlement",
            ),
            primary_route="same_run_attempt2_retained_replay",
            fallback_routes=("controlled_serialized_restart",),
            operator_action=(
                "Rerun attempt 2 of the same Actions run on unchanged head and "
                "base to reconcile or replay publication from retained result."
            ),
        )
    return RecoveryClassification(
        state_code="nonterminal_transaction_isolation_required",
        terminal=False,
        automatic_zero_inference=False,
        head_relation=head_relation,
        base_relation=base_relation,
        run_attempt=attempt,
        executor_status=executor_status,
        transaction_phase=tx.phase,
        action_phase=None,
        dispatch_status=dispatch_status,
        missing_witnesses=("terminal_transaction_settlement",),
        primary_route="controlled_serialized_restart",
        fallback_routes=(),
        operator_action=(
            "Unsettled transaction cannot close automatically; confirm owner "
            "cessation and recover via controlled serialized restart after expiry."
        ),
    )


_HEX40_RE = re.compile(r"[a-f0-9]{40}")
_HEX64_RE = re.compile(r"[a-f0-9]{64}")
_VERSION_TOKEN_RE = re.compile(r"[A-Za-z0-9_.+-]{1,64}")


def _require_sha_or_unknown(value: str, label: str) -> None:
    if not isinstance(value, str) or (
        value != "unknown" and _HEX40_RE.fullmatch(value) is None
    ):
        raise ReviewInputError(
            f"attempt evidence {label} must be 'unknown' or a 40-hex SHA"
        )


def _require_version_or_unknown(value: str, label: str) -> None:
    if not isinstance(value, str) or (
        value != "unknown" and _VERSION_TOKEN_RE.fullmatch(value) is None
    ):
        raise ReviewInputError(
            f"attempt evidence {label} must be 'unknown' or a bounded version token"
        )


def _require_optional_sha256(value: str | None, label: str) -> None:
    if value is not None and (
        not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None
    ):
        raise ReviewInputError(
            f"attempt evidence {label} must be None or a 64-hex SHA-256"
        )


def _require_optional_pos_int(value: int | None, label: str) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise ReviewInputError(
            f"attempt evidence {label} must be None or a positive integer"
        )


def _require_optional_nat_int(value: int | None, label: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ReviewInputError(
            f"attempt evidence {label} must be None or a non-negative integer"
        )


@dataclass(frozen=True)
class AttemptEvidencePacket:
    """Immutable read-only historical attempt-evidence packet (`authority=False`).

    Binds original attempt identities, installed executor provenance, frozen
    context, caller funding/reservation liability, intent/execution markers,
    retained result digests, native publication/check/cleanup/terminal receipts,
    uncertainty/missing-diagnostics state, and explicit missing-proof holds
    without mutating session ledgers or forging live settlement receipts.
    """

    case_id: str
    repository: str
    repository_id: int
    pull_request: int
    session_generation: int | None
    record_sha256: str | None
    run_id: int | None
    run_attempt: int | None
    owner_job: str | None
    owner_generation: int | None
    transaction_id: str | None
    base_sha: str | None
    head_sha: str | None
    current_base_sha: str
    current_head_sha: str
    executor_sha: str
    executor_commit: str
    workflow_blob: str
    installed_core_version: str
    installed_hosted_version: str
    executor_status: str
    policy_commit: str | None
    configuration_digest: str | None
    policy_digest: str | None
    evidence_digest: str | None
    retained_context_available: bool
    explicit_rescan: bool
    operator_paused: bool
    caller_allowance_limit: int | None
    startup_liability: int | None
    prior_nonstartup_liability: int | None
    confirmed_requests: int | None
    uncertain_requests: int | None
    reserved_undispatched: int | None
    nonstartup_liability_upper_bound: int | None
    total_liability_upper_bound: int | None
    remaining_caller_allowance: int | None
    attempt2_slot_available: bool
    active_reservation_id: str | None
    reserved_slot: str | None
    transaction_phase: str
    action_phase: str | None
    action_purpose: str | None
    action_execution_started: bool | None
    action_operation_key: str | None
    action_intent_sha256: str | None
    terminal_transaction: str | None
    terminal_action_authenticated: bool | None
    abort_cleanup_entry_state: str | None
    abort_cleanup_root_sha256: str | None
    result_sha256: str | None
    retained_result_verified: bool
    coverage_only_partial: bool
    publication_receipt_id: int | None
    check_receipt_id: int | None
    thread_cleanup_receipt_state: str | None
    native_approval_receipt_id: int | None
    terminal_settlement_form: str
    historical_settlement_receipt_supplied: bool
    dispatch_status: str
    diagnostics_available: bool
    inferred_cause: str
    missing_proofs: tuple[str, ...]
    classification: RecoveryClassification
    null_marker_universal_failure: bool = False
    authority: bool = False
    schema_version: str = ATTEMPT_EVIDENCE_PACKET_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.authority is not False
            or self.null_marker_universal_failure is not False
            or self.schema_version != ATTEMPT_EVIDENCE_PACKET_SCHEMA
        ):
            raise ReviewInputError(
                "attempt evidence packet is strictly diagnostic (authority=False) "
                "and never treats a null review marker as a universal failure rule"
            )
        if not isinstance(self.classification, RecoveryClassification):
            raise ReviewInputError(
                "attempt evidence packet requires a RecoveryClassification"
            )
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ReviewInputError("attempt evidence case_id must be non-empty")
        if type(self.repository_id) is not int or self.repository_id <= 0:
            raise ReviewInputError(
                "attempt evidence repository_id must be a positive integer"
            )
        SessionIdentity(
            repository=self.repository,
            pull_request=self.pull_request,
            repository_id=self.repository_id,
        )
        _require_optional_nat_int(self.session_generation, "session_generation")
        _require_optional_sha256(self.record_sha256, "record_sha256")
        _require_optional_pos_int(self.run_id, "run_id")
        if self.run_attempt not in {None, 1, 2}:
            raise ReviewInputError("attempt evidence run_attempt must be None, 1, or 2")
        _require_optional_nat_int(self.owner_generation, "owner_generation")
        _require_optional_sha256(self.transaction_id, "transaction_id")
        if self.base_sha is not None:
            _require_sha(self.base_sha, "base_sha")
        if self.head_sha is not None:
            _require_sha(self.head_sha, "head_sha")
        _require_sha(self.current_base_sha, "current_base_sha")
        _require_sha(self.current_head_sha, "current_head_sha")
        _require_sha_or_unknown(self.executor_sha, "executor_sha")
        _require_sha_or_unknown(self.executor_commit, "executor_commit")
        _require_sha_or_unknown(self.workflow_blob, "workflow_blob")
        _require_version_or_unknown(
            self.installed_core_version, "installed_core_version"
        )
        _require_version_or_unknown(
            self.installed_hosted_version, "installed_hosted_version"
        )
        if self.policy_commit is not None:
            _require_sha(self.policy_commit, "policy_commit")
        _require_optional_sha256(self.configuration_digest, "configuration_digest")
        _require_optional_sha256(self.policy_digest, "policy_digest")
        _require_optional_sha256(self.evidence_digest, "evidence_digest")
        _require_optional_pos_int(self.caller_allowance_limit, "caller_allowance_limit")
        _require_optional_nat_int(self.startup_liability, "startup_liability")
        _require_optional_nat_int(
            self.prior_nonstartup_liability, "prior_nonstartup_liability"
        )
        _require_optional_nat_int(self.confirmed_requests, "confirmed_requests")
        _require_optional_nat_int(self.uncertain_requests, "uncertain_requests")
        _require_optional_nat_int(self.reserved_undispatched, "reserved_undispatched")
        _require_optional_nat_int(
            self.nonstartup_liability_upper_bound,
            "nonstartup_liability_upper_bound",
        )
        _require_optional_nat_int(
            self.total_liability_upper_bound, "total_liability_upper_bound"
        )
        _require_optional_nat_int(
            self.remaining_caller_allowance, "remaining_caller_allowance"
        )
        _require_optional_sha256(self.action_operation_key, "action_operation_key")
        _require_optional_sha256(self.action_intent_sha256, "action_intent_sha256")
        _require_optional_sha256(self.terminal_transaction, "terminal_transaction")
        _require_optional_sha256(
            self.abort_cleanup_root_sha256, "abort_cleanup_root_sha256"
        )
        _require_optional_sha256(self.result_sha256, "result_sha256")
        _require_optional_pos_int(self.publication_receipt_id, "publication_receipt_id")
        _require_optional_pos_int(self.check_receipt_id, "check_receipt_id")
        _require_optional_pos_int(
            self.native_approval_receipt_id, "native_approval_receipt_id"
        )
        if (
            self.thread_cleanup_receipt_state is not None
            and self.thread_cleanup_receipt_state not in THREAD_CLEANUP_RECEIPT_STATES
        ):
            raise ReviewInputError(
                "attempt evidence thread_cleanup_receipt_state is invalid"
            )
        if (
            self.executor_status not in EXECUTOR_STATUSES
            or self.dispatch_status not in DISPATCH_STATUSES
            or self.terminal_settlement_form not in TERMINAL_SETTLEMENT_FORMS
            or self.inferred_cause not in DIAGNOSTIC_CAUSES
        ):
            raise ReviewInputError("attempt evidence packet vocabulary is invalid")
        if not self.diagnostics_available and self.inferred_cause not in {
            "unknown",
            "not_applicable_settled",
        }:
            raise ReviewInputError(
                "attempt evidence packet cannot infer a cause from missing diagnostics"
            )
        if self.classification.terminal and (
            self.inferred_cause != "not_applicable_settled"
            or self.terminal_settlement_form == "unsettled"
        ):
            raise ReviewInputError(
                "settled attempt evidence packet must record its settlement form"
            )
        if not self.classification.terminal and (
            self.terminal_settlement_form != "unsettled"
        ):
            raise ReviewInputError(
                "nonterminal attempt evidence packet cannot claim terminal settlement"
            )
        if (
            self.terminal_settlement_form
            in {
                "authenticated_terminal_action_null_review_marker",
                "authenticated_terminal_action_with_prior_review_marker",
            }
            and self.terminal_action_authenticated is not True
        ):
            raise ReviewInputError(
                "terminal action settlement requires positive result authentication"
            )
        if any(
            proof not in ATTEMPT_PACKET_MISSING_PROOFS for proof in self.missing_proofs
        ) or len(set(self.missing_proofs)) != len(self.missing_proofs):
            raise ReviewInputError("attempt evidence missing_proofs are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "authority": False,
            "null_marker_universal_failure": False,
            "case_id": self.case_id,
            "identities": {
                "repository": self.repository,
                "repository_id": self.repository_id,
                "pull_request": self.pull_request,
                "session_generation": self.session_generation,
                "record_sha256": self.record_sha256,
                "run_id": self.run_id,
                "run_attempt": self.run_attempt,
                "owner_job": self.owner_job,
                "owner_generation": self.owner_generation,
                "transaction_id": self.transaction_id,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "current_base_sha": self.current_base_sha,
                "current_head_sha": self.current_head_sha,
            },
            "installed_executor": {
                "executor_sha": self.executor_sha,
                "executor_commit": self.executor_commit,
                "workflow_blob": self.workflow_blob,
                "installed_core_version": self.installed_core_version,
                "installed_hosted_version": self.installed_hosted_version,
                "executor_status": self.executor_status,
            },
            "context": {
                "policy_commit": self.policy_commit,
                "configuration_digest": self.configuration_digest,
                "policy_digest": self.policy_digest,
                "evidence_digest": self.evidence_digest,
                "retained_context_available": self.retained_context_available,
                "explicit_rescan": self.explicit_rescan,
                "operator_paused": self.operator_paused,
            },
            "funding": {
                "caller_allowance_limit": self.caller_allowance_limit,
                "startup_liability": self.startup_liability,
                "prior_nonstartup_liability": self.prior_nonstartup_liability,
                "confirmed_requests": self.confirmed_requests,
                "uncertain_requests": self.uncertain_requests,
                "reserved_undispatched": self.reserved_undispatched,
                "nonstartup_liability_upper_bound": self.nonstartup_liability_upper_bound,
                "total_liability_upper_bound": self.total_liability_upper_bound,
                "remaining_caller_allowance": self.remaining_caller_allowance,
                "attempt2_slot_available": self.attempt2_slot_available,
                "active_reservation_id": self.active_reservation_id,
                "reserved_slot": self.reserved_slot,
            },
            "intent_execution": {
                "transaction_phase": self.transaction_phase,
                "action_phase": self.action_phase,
                "action_purpose": self.action_purpose,
                "action_execution_started": self.action_execution_started,
                "action_operation_key": self.action_operation_key,
                "action_intent_sha256": self.action_intent_sha256,
                "terminal_transaction": self.terminal_transaction,
                "terminal_action_authenticated": self.terminal_action_authenticated,
                "abort_cleanup_entry_state": self.abort_cleanup_entry_state,
                "abort_cleanup_root_sha256": self.abort_cleanup_root_sha256,
            },
            "retained_results": {
                "result_sha256": self.result_sha256,
                "retained_result_verified": self.retained_result_verified,
                "coverage_only_partial": self.coverage_only_partial,
            },
            "receipts": {
                "publication_receipt_id": self.publication_receipt_id,
                "check_receipt_id": self.check_receipt_id,
                "thread_cleanup_receipt_state": self.thread_cleanup_receipt_state,
                "native_approval_receipt_id": self.native_approval_receipt_id,
                "terminal_settlement_form": self.terminal_settlement_form,
                "historical_settlement_receipt_supplied": (
                    self.historical_settlement_receipt_supplied
                ),
            },
            "uncertainty": {
                "dispatch_status": self.dispatch_status,
                "diagnostics_available": self.diagnostics_available,
                "inferred_cause": self.inferred_cause,
            },
            "missing_proofs": list(self.missing_proofs),
            "classification": self.classification.to_dict(),
        }


def _derive_terminal_settlement_form(
    classification: RecoveryClassification,
    *,
    has_prior_review_transaction: bool | None = None,
) -> str:
    if classification.state_code == "unenrolled_ready":
        return "unenrolled_session"
    if classification.state_code == "terminal_action_without_review_transaction":
        return "authenticated_terminal_action_null_review_marker"
    if classification.state_code == "terminal_action_operator_paused":
        if has_prior_review_transaction is False or (
            has_prior_review_transaction is None
            and classification.transaction_phase == "no-transaction"
        ):
            return "authenticated_terminal_action_null_review_marker"
        return "authenticated_terminal_action_with_prior_review_marker"
    if classification.state_code == "terminal_action_settled":
        return "authenticated_terminal_action_with_prior_review_marker"
    if classification.state_code == "pre_entry_analysis_abort_settled":
        return "pre_entry_analysis_abort"
    if classification.state_code == "pre_external_write_settlement_ready":
        return "pre_external_write_settlement"
    if classification.state_code in {
        "terminal_review_settled",
        "terminal_review_operator_paused",
        "terminal_review_coverage_only_partial",
    }:
        return "review_terminal_transaction"
    return "unsettled"


def _assemble_attempt_evidence_packet(
    *,
    identity: SessionIdentity,
    record: SessionRecord | None,
    classification: RecoveryClassification,
    current_base: str,
    current_head: str,
    case_id: str | None = None,
    executor_sha: str = "unknown",
    executor_commit: str = "unknown",
    workflow_blob: str = "unknown",
    installed_core_version: str = "unknown",
    installed_hosted_version: str = "unknown",
    retained_context_available: bool = False,
    explicit_rescan: bool | None = None,
    coverage_only_partial: bool = False,
    action_result_authenticated: bool | None = None,
    evidence_producer: str | None = None,
    publication_receipt_id: int | None = None,
    check_receipt_id: int | None = None,
    thread_cleanup_receipt_state: str | None = None,
    native_approval_receipt_id: int | None = None,
    historical_settlement_receipt_supplied: bool = False,
    diagnostics_available: bool = False,
    diagnostic_cause: str | None = None,
) -> AttemptEvidencePacket:
    if not isinstance(historical_settlement_receipt_supplied, bool) or not isinstance(
        diagnostics_available, bool
    ):
        raise ReviewInputError("attempt evidence boolean flags must be bool")
    if diagnostic_cause is not None and diagnostic_cause not in DIAGNOSTIC_CAUSES:
        raise ReviewInputError("attempt evidence diagnostic_cause is unsupported")

    old: ApiRunAccounting | None = None
    if record is not None and record.api_run_accounting is not None:
        try:
            old = ApiRunAccounting.from_document(record.api_run_accounting)
        except ReviewInputError:
            old = None

    tx = record.transaction if record is not None else None
    resolved_rescan = False
    if explicit_rescan is not None:
        resolved_rescan = bool(explicit_rescan)
    elif record is not None:
        try:
            resolved_rescan = admitted_rescan(record) is not None
        except ReviewInputError:
            resolved_rescan = True

    action_purpose: str | None = None
    action_started: bool | None = None
    action_op_key: str | None = None
    action_intent_digest: str | None = None
    verified_action: bool | None = None
    if old is not None and isinstance(old.action, dict):
        raw_started = old.action.get("execution_started")
        if isinstance(raw_started, bool):
            action_started = raw_started
        intent = old.action.get("intent")
        if isinstance(intent, dict):
            raw_purpose = intent.get("purpose")
            if isinstance(raw_purpose, str):
                action_purpose = raw_purpose
            raw_op = intent.get("operation_key")
            if isinstance(raw_op, str):
                action_op_key = raw_op
            action_intent_digest = hashlib.sha256(canonical_bytes(intent)).hexdigest()
        if old.action.get("phase") == "terminal":
            if action_result_authenticated is not None:
                verified_action = bool(action_result_authenticated)
            elif (
                record is not None
                and evidence_producer is not None
                and isinstance(old.action.get("result"), dict)
                and old.action["result"].get("encoding") == INLINE_ENCODING
            ):
                verified_action = _verify_inline_action_result(
                    record, old, evidence_producer
                )
            else:
                verified_action = False

    abort_entry_state: str | None = None
    abort_cleanup_root: str | None = None
    if old is not None and isinstance(old.analysis_abort, dict):
        raw_entry = old.analysis_abort.get("entry_state")
        if isinstance(raw_entry, str):
            abort_entry_state = raw_entry
        raw_cleanup = old.analysis_abort.get("cleanup_root")
        if isinstance(raw_cleanup, str):
            abort_cleanup_root = raw_cleanup

    nonstartup_upper: int | None = None
    total_upper: int | None = None
    if old is not None:
        nonstartup_upper = (
            old.prior_nonstartup_liability
            + old.confirmed
            + old.uncertain
            + old.reserved_undispatched
        )
        total_upper = old.startup.liability + nonstartup_upper

    run_id = old.identity.run_id if old is not None else None
    session_gen = record.generation if record is not None else None
    resolved_case_id = case_id
    if resolved_case_id is None:
        if (
            identity.repository.casefold() == REVIEWSENSEI_443_REPOSITORY.casefold()
            and identity.pull_request == REVIEWSENSEI_443_PULL_REQUEST
            and run_id == REVIEWSENSEI_443_RUN_ID
            and session_gen == REVIEWSENSEI_443_GENERATION
        ):
            resolved_case_id = REVIEWSENSEI_443_CASE_ID
        elif (
            identity.repository.casefold() == REVIEWSENSEI_459_REPOSITORY.casefold()
            and identity.pull_request == REVIEWSENSEI_459_PULL_REQUEST
            and run_id == REVIEWSENSEI_459_RUN_ID
            and session_gen == REVIEWSENSEI_459_GENERATION
        ):
            resolved_case_id = REVIEWSENSEI_459_CASE_ID
        else:
            gen_token = str(session_gen) if session_gen is not None else "none"
            run_token = str(run_id) if run_id is not None else "none"
            resolved_case_id = (
                f"{identity.repository}#{identity.pull_request}"
                f":gen-{gen_token}:run-{run_token}"
            )

    settlement_form = _derive_terminal_settlement_form(
        classification,
        has_prior_review_transaction=(tx is not None),
    )
    if diagnostic_cause is not None:
        if not diagnostics_available and diagnostic_cause not in {
            "unknown",
            "not_applicable_settled",
        }:
            raise ReviewInputError(
                "attempt evidence packet cannot infer a cause from missing diagnostics"
            )
        if classification.terminal and diagnostic_cause != "not_applicable_settled":
            raise ReviewInputError(
                "settled attempt evidence packet cannot carry a failure cause"
            )
        inferred_cause = diagnostic_cause
    else:
        inferred_cause = (
            "not_applicable_settled" if classification.terminal else "unknown"
        )

    missing: list[str] = list(classification.missing_witnesses)
    if not classification.terminal:
        if tx is not None and (
            old is None or old.terminal_transaction != tx.transaction_id
        ):
            missing.append("terminal_transaction_settlement")
        if not diagnostics_available:
            missing.append("missing_close_diagnostics")
        if executor_sha == "unknown":
            missing.append("installed_executor_provenance")
        if tx is not None and tx.phase == "publication_succeeded":
            if publication_receipt_id is None:
                missing.append("publication_receipt_id")
            if check_receipt_id is None:
                missing.append("check_receipt_id")
            if thread_cleanup_receipt_state is None:
                missing.append("thread_cleanup_receipt")
        if not historical_settlement_receipt_supplied:
            missing.append("historical_live_settlement_receipt")
    deduped_missing = tuple(dict.fromkeys(missing))

    repo_id = identity.repository_id
    if type(repo_id) is not int or repo_id <= 0:
        raise ReviewInputError(
            "attempt evidence packet requires a positive integer repository_id"
        )
    retained_verified = bool(
        tx is not None and tx.result_sha256 is not None and retained_context_available
    )

    return AttemptEvidencePacket(
        case_id=resolved_case_id,
        repository=identity.repository,
        repository_id=repo_id,
        pull_request=identity.pull_request,
        session_generation=session_gen,
        record_sha256=record.record_sha256 if record is not None else None,
        run_id=run_id,
        run_attempt=old.owner.run_attempt if old is not None else None,
        owner_job=old.owner.job if old is not None else None,
        owner_generation=old.owner.generation if old is not None else None,
        transaction_id=tx.transaction_id if tx is not None else None,
        base_sha=tx.base_sha if tx is not None else None,
        head_sha=tx.head_sha if tx is not None else None,
        current_base_sha=current_base,
        current_head_sha=current_head,
        executor_sha=executor_sha,
        executor_commit=executor_commit,
        workflow_blob=workflow_blob,
        installed_core_version=installed_core_version,
        installed_hosted_version=installed_hosted_version,
        executor_status=classification.executor_status,
        policy_commit=old.configuration.commit if old is not None else None,
        configuration_digest=(
            tx.configuration_digest
            if tx is not None
            else (old.configuration.digest if old is not None else None)
        ),
        policy_digest=tx.policy_digest if tx is not None else None,
        evidence_digest=tx.evidence_digest if tx is not None else None,
        retained_context_available=bool(retained_context_available),
        explicit_rescan=resolved_rescan,
        operator_paused=bool(record.operator_paused) if record is not None else False,
        caller_allowance_limit=(old.configuration.limit if old is not None else None),
        startup_liability=old.startup.liability if old is not None else None,
        prior_nonstartup_liability=(
            old.prior_nonstartup_liability if old is not None else None
        ),
        confirmed_requests=old.confirmed if old is not None else None,
        uncertain_requests=old.uncertain if old is not None else None,
        reserved_undispatched=(old.reserved_undispatched if old is not None else None),
        nonstartup_liability_upper_bound=nonstartup_upper,
        total_liability_upper_bound=total_upper,
        remaining_caller_allowance=old.remaining if old is not None else None,
        attempt2_slot_available=(
            _attempt2_slot_available(old) if old is not None else False
        ),
        active_reservation_id=(record.reservation_id if record is not None else None),
        reserved_slot=record.reserved_slot if record is not None else None,
        transaction_phase=classification.transaction_phase,
        action_phase=classification.action_phase,
        action_purpose=action_purpose,
        action_execution_started=action_started,
        action_operation_key=action_op_key,
        action_intent_sha256=action_intent_digest,
        terminal_transaction=(old.terminal_transaction if old is not None else None),
        terminal_action_authenticated=verified_action,
        abort_cleanup_entry_state=abort_entry_state,
        abort_cleanup_root_sha256=abort_cleanup_root,
        result_sha256=tx.result_sha256 if tx is not None else None,
        retained_result_verified=retained_verified,
        coverage_only_partial=bool(coverage_only_partial),
        publication_receipt_id=publication_receipt_id,
        check_receipt_id=check_receipt_id,
        thread_cleanup_receipt_state=thread_cleanup_receipt_state,
        native_approval_receipt_id=native_approval_receipt_id,
        terminal_settlement_form=settlement_form,
        historical_settlement_receipt_supplied=historical_settlement_receipt_supplied,
        dispatch_status=classification.dispatch_status,
        diagnostics_available=diagnostics_available,
        inferred_cause=inferred_cause,
        missing_proofs=deduped_missing,
        classification=classification,
    )


def build_attempt_evidence_packet(
    record: SessionRecord,
    *,
    current_base: str,
    current_head: str,
    case_id: str | None = None,
    executor_status: str = "not_applicable",
    executor_sha: str = "unknown",
    executor_commit: str = "unknown",
    workflow_blob: str = "unknown",
    installed_core_version: str = "unknown",
    installed_hosted_version: str = "unknown",
    dispatch_status: str = "known_clean",
    coverage_only_partial: bool = False,
    explicit_rescan: bool | None = None,
    retained_context_available: bool = False,
    native_effects_confirmed: bool | None = None,
    action_result_authenticated: bool | None = None,
    evidence_producer: str | None = None,
    publication_receipt_id: int | None = None,
    check_receipt_id: int | None = None,
    thread_cleanup_receipt_state: str | None = None,
    native_approval_receipt_id: int | None = None,
    historical_settlement_receipt_supplied: bool = False,
    diagnostics_available: bool = False,
    diagnostic_cause: str | None = None,
    settlement_witness: Any = None,
    failure_diagnostic: str | None = None,
) -> AttemptEvidencePacket:
    """Build an immutable offline `AttemptEvidencePacket` (`authority=False`).

    Classifies the complete actual `SessionRecord` (preserving valid terminal
    command actions whose review `terminal_transaction` marker is `None`, while
    holding nonterminal `publication_succeeded` records such as ReviewSensei
    `#443` run `43074015855` generation `11` on explicit missing proofs and
    `inferred_cause="unknown"` when close diagnostics are absent).
    """
    classification = classify_recovery_state(
        record,
        current_base=current_base,
        current_head=current_head,
        executor_status=executor_status,
        dispatch_status=dispatch_status,
        coverage_only_partial=coverage_only_partial,
        explicit_rescan=explicit_rescan,
        retained_context_available=retained_context_available,
        native_effects_confirmed=native_effects_confirmed,
        action_result_authenticated=action_result_authenticated,
        evidence_producer=evidence_producer,
        settlement_witness=settlement_witness,
        failure_diagnostic=failure_diagnostic,
    )
    if type(record.repository_id) is not int or record.repository_id <= 0:
        raise ReviewInputError(
            "attempt evidence packet requires a repository_id-bound SessionRecord"
        )
    identity = SessionIdentity(
        repository=record.repository,
        pull_request=record.pull_request,
        repository_id=record.repository_id,
    )
    return _assemble_attempt_evidence_packet(
        identity=identity,
        record=record,
        classification=classification,
        current_base=current_base,
        current_head=current_head,
        case_id=case_id,
        executor_sha=executor_sha,
        executor_commit=executor_commit,
        workflow_blob=workflow_blob,
        installed_core_version=installed_core_version,
        installed_hosted_version=installed_hosted_version,
        retained_context_available=retained_context_available,
        explicit_rescan=explicit_rescan,
        coverage_only_partial=coverage_only_partial,
        action_result_authenticated=action_result_authenticated,
        evidence_producer=evidence_producer,
        publication_receipt_id=publication_receipt_id,
        check_receipt_id=check_receipt_id,
        thread_cleanup_receipt_state=thread_cleanup_receipt_state,
        native_approval_receipt_id=native_approval_receipt_id,
        historical_settlement_receipt_supplied=historical_settlement_receipt_supplied,
        diagnostics_available=diagnostics_available,
        diagnostic_cause=diagnostic_cause,
    )


def _probe_executor_details_readonly(
    ro_http: _ReadOnlyHttp,
    *,
    actions_token: str,
    identity: SessionIdentity,
    run_id: int,
    owner: ApiExecutionOwner,
    tx_head_sha: str | None,
) -> tuple[str, str, str, str]:
    """Return `(executor_status, executor_sha, executor_commit, workflow_blob)` read-only."""
    if type(run_id) is not int or run_id < 1:
        return ("active_or_unconfirmed", "unknown", "unknown", "unknown")
    run_reader = ApiRunActionsEvidence(
        ro_http, token=actions_token, command_run_id=str(run_id), job=owner.job
    )
    if (
        tx_head_sha is not None
        and owner.run_attempt == 1
        and owner.generation == 0
        and owner.job in {"hosted", "local"}
    ):
        try:
            stopped = run_reader.qualified_publication_cessation(
                identity, run_id, owner, head_sha=tx_head_sha
            )
            exec_sha = (
                stopped["executor_sha"]
                if isinstance(stopped.get("executor_sha"), str)
                and _HEX40_RE.fullmatch(stopped["executor_sha"]) is not None
                else "unknown"
            )
            exec_commit = (
                stopped["executor_commit"]
                if isinstance(stopped.get("executor_commit"), str)
                and _HEX40_RE.fullmatch(stopped["executor_commit"]) is not None
                else "unknown"
            )
            wf_blob = (
                stopped["workflow_blob"]
                if isinstance(stopped.get("workflow_blob"), str)
                and _HEX40_RE.fullmatch(stopped["workflow_blob"]) is not None
                else "unknown"
            )
            return ("qualified_success", exec_sha, exec_commit, wf_blob)
        except ReviewInputError:
            pass

    try:
        _job_id, _stamp, job_conclusion = run_reader.ceased_owner(identity, owner)
    except ReviewInputError:
        return ("active_or_unconfirmed", "unknown", "unknown", "unknown")

    try:
        run_doc = run_reader._get(identity, f"/actions/runs/{run_id}")
    except ReviewInputError:
        # Fail closed as active_or_unconfirmed when a failed attempt's run doc is
        # unreadable (a later attempt may be active); preserve ceased on success.
        if job_conclusion != "success":
            return ("active_or_unconfirmed", "unknown", "unknown", "unknown")
        return ("ceased", "unknown", "unknown", "unknown")

    repository = run_doc.get("repository")
    run_attempt = run_doc.get("run_attempt")
    if (
        type(run_doc.get("id")) is not int
        or run_doc.get("id") != run_id
        or not isinstance(repository, dict)
        or repository.get("id") != identity.repository_id
        or repository.get("full_name") != identity.repository
        or type(run_attempt) is not int
        or run_attempt < owner.run_attempt
    ):
        return ("active_or_unconfirmed", "unknown", "unknown", "unknown")

    if run_doc.get("status") != "completed":
        return ("active_or_unconfirmed", "unknown", "unknown", "unknown")

    if run_attempt > owner.run_attempt:
        if (
            not is_terminal_job(run_doc)
            or run_attempt - owner.run_attempt > MAX_LATER_ATTEMPT_INSPECTION
        ):
            return ("active_or_unconfirmed", "unknown", "unknown", "unknown")
        for later_attempt in range(owner.run_attempt + 1, run_attempt + 1):
            try:
                later_jobs = run_reader._jobs(identity, later_attempt)
            except ReviewInputError:
                return ("active_or_unconfirmed", "unknown", "unknown", "unknown")
            if not later_attempt_proved_no_business_entry(later_jobs):
                return ("active_or_unconfirmed", "unknown", "unknown", "unknown")

    if job_conclusion != "success":
        return ("failed", "unknown", "unknown", "unknown")

    refs = run_doc.get("referenced_workflows")
    observed_sha = "unknown"
    if isinstance(refs, list) and len(refs) == 1 and isinstance(refs[0], dict):
        raw_sha = refs[0].get("sha")
        if isinstance(raw_sha, str) and _HEX40_RE.fullmatch(raw_sha) is not None:
            observed_sha = raw_sha

    if run_attempt > owner.run_attempt:
        return ("ceased", observed_sha, "unknown", "unknown")

    if (
        tx_head_sha is None
        or owner.job == "command"
        or run_doc.get("conclusion") != "success"
    ):
        return ("ceased", observed_sha, "unknown", "unknown")

    if observed_sha in QUALIFIED_EXECUTORS:
        # Workflow SHA is catalogued, so cessation failed on step/chronology/head.
        return ("failed", observed_sha, "unknown", "unknown")
    return ("unrecognized", observed_sha, "unknown", "unknown")


def _probe_executor_status_readonly(
    ro_http: _ReadOnlyHttp,
    *,
    actions_token: str,
    identity: SessionIdentity,
    run_id: int,
    owner: ApiExecutionOwner,
    tx_head_sha: str | None,
) -> str:
    """Inspect predecessor Actions run cessation and executor contract read-only.

    Uses a single `ApiRunActionsEvidence` anchored to `str(run_id)` (avoiding
    any synthetic `command_run_id` offset) and separates job-level owner
    cessation (`ceased`) from failed job exit (`failed`) and uncatalogued
    workflow source (`unrecognized`).
    """
    return _probe_executor_details_readonly(
        ro_http,
        actions_token=actions_token,
        identity=identity,
        run_id=run_id,
        owner=owner,
        tx_head_sha=tx_head_sha,
    )[0]


def _inspect_pre_write_witness_readonly(
    ro_ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    old: ApiRunAccounting,
    *,
    author_id: int | None,
    producer: str | None,
    budget: EvidenceReadBudget,
    force_scan: bool,
) -> PreExternalWriteSettlementWitness | bool | None:
    if author_id is None or producer is None or not ro_ledger.app_slug:
        return False if force_scan else None
    if force_scan:
        try:
            return _find_settlement_witness_for_producer(
                http=ro_ledger.http,
                token=ro_ledger.token,
                app_slug=ro_ledger.app_slug,
                producer_id=author_id,
                producer=producer,
                budget=budget,
                identity=identity,
                record=record,
                old=old,
            )
        except ReviewInputError:
            return False
    cached = getattr(ro_ledger, "_cached_comments", lambda: ())()
    matches: list[PreExternalWriteSettlementWitness] = []
    saw_bot_marker = False
    for row in cached:
        if not isinstance(row, dict) or not _is_authenticated_bot_comment(
            row, producer_id=author_id, app_slug=ro_ledger.app_slug
        ):
            continue
        body = row.get("body")
        if isinstance(body, str) and SETTLEMENT_WITNESS_MARKER in body:
            saw_bot_marker = True
            try:
                parsed = parse_settlement_witness_comment(body)
            except ReviewInputError:
                return False
            if (
                parsed is not None
                and parsed.settled_root_sha256 == record.record_sha256
            ):
                matches.append(parsed)
    if not matches:
        return False if saw_bot_marker else None
    if len(matches) != 1:
        return False
    return matches[0]


def _inspect_pre_write_census_readonly(
    ro_ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    old: ApiRunAccounting,
    *,
    author_id: int | None,
    budget: EvidenceReadBudget,
) -> bool | None:
    app_slug = ro_ledger.app_slug
    if author_id is None or not app_slug:
        return None
    tx = record.transaction
    head_sha = tx.head_sha if tx is not None else record.failed_attempts_head_sha
    if not isinstance(head_sha, str) or not head_sha:
        return None
    http = ro_ledger.http
    deadline = http.evidence_monotonic() + 30
    earlier: Any = None
    earlier_review_ids: set[int] = set()

    def ensure_earlier() -> Any:
        nonlocal earlier
        if earlier is None:
            earlier = (
                _earlier_settled_effects(
                    ro_ledger, identity, record, old, head_sha, author_id, budget
                )
                if tx is not None and record.finding_lifecycle is not None
                else _NO_EARLIER_EFFECTS
            )
        return earlier

    def hold(msg: str) -> ReviewInputError:
        return ReviewInputError(msg)

    try:
        for resource, inline in (("reviews", False), ("comments", True)):
            path = http.repository_path(
                identity.repository, f"/pulls/{identity.pull_request}/{resource}"
            )
            seen: set[int] = set()
            for rows in iter_step_down_pages(
                http=http,
                token=ro_ledger.token,
                path=path,
                deadline=deadline,
                protect_request=lambda: budget.preflight(
                    RETAINED_CONFIRMATION_TAIL + 1
                ),
                hold=hold,
                unavailable_message="pre-write readonly census is unavailable",
                incomplete_message="pre-write readonly census is incomplete",
                deadline_message="pre-write readonly census reached its deadline",
                pagination_bound_message="pre-write readonly census reached pagination bound",
            ):
                for row in rows:
                    row_id = row.get("id") if isinstance(row, dict) else None
                    if type(row_id) is not int or row_id <= 0 or row_id in seen:
                        return None
                    seen.add(row_id)
                    effect = (
                        classify_abort_inline_effect(
                            author=row.get("user"),
                            native_head=row.get("original_commit_id"),
                            current_head=row.get("commit_id"),
                            body=row.get("body"),
                            identity=identity,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            app_user_id=author_id,
                        )
                        if inline
                        else classify_abort_review_effect(
                            row,
                            identity=identity,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            app_user_id=author_id,
                        )
                    )
                    if effect is None:
                        return None
                    if effect is True:
                        ancestor = ensure_earlier()
                        if inline:
                            parent = row.get("pull_request_review_id")
                            if (
                                type(parent) is not int
                                or parent not in earlier_review_ids
                            ):
                                return False
                        if not ancestor.explains(row, inline=inline):
                            return False
                        if not inline:
                            earlier_review_ids.add(row_id)

        check_path = http.repository_path(
            identity.repository, f"/commits/{head_sha}/check-runs"
        )
        seen_checks: set[int] = set()
        total_checks: int | None = None
        expected_slug = app_slug.removesuffix("[bot]").casefold()

        def extract_checks(status: int, value: Any, page_size: int) -> list[Any] | None:
            nonlocal total_checks
            if status != 200 or not isinstance(value, dict):
                return None
            rows, count = value.get("check_runs"), value.get("total_count")
            if (
                not isinstance(rows, list)
                or len(rows) > page_size
                or type(count) is not int
                or count < 0
                or (total_checks is not None and total_checks != count)
            ):
                return None
            total_checks = count
            return rows

        def verify_checks() -> None:
            if len(seen_checks) != total_checks:
                raise ReviewInputError("pre-write readonly check-run census incomplete")

        for rows in iter_step_down_pages(
            http=http,
            token=ro_ledger.token,
            path=check_path,
            deadline=deadline,
            protect_request=lambda: budget.preflight(RETAINED_CONFIRMATION_TAIL + 1),
            hold=hold,
            unavailable_message="pre-write readonly check census is unavailable",
            incomplete_message="pre-write readonly check census is incomplete",
            deadline_message="pre-write readonly check census reached its deadline",
            pagination_bound_message="pre-write readonly check census reached pagination bound",
            extra_query=f"check_name={REVIEW_CHECK_NAME}",
            extract_rows=extract_checks,
            verify_complete=verify_checks,
        ):
            for row in rows:
                row_id = row.get("id") if isinstance(row, dict) else None
                if type(row_id) is not int or row_id <= 0 or row_id in seen_checks:
                    return None
                seen_checks.add(row_id)
                app = row.get("app")
                if row.get("name") == REVIEW_CHECK_NAME:
                    if (
                        not isinstance(app, dict)
                        or not isinstance(app.get("slug"), str)
                        or row.get("head_sha") not in {head_sha, None}
                    ):
                        return None
                    if app["slug"].casefold() == expected_slug:
                        if row.get("external_id") not in {
                            None,
                            REVIEW_CHECK_EXTERNAL_ID,
                        }:
                            return False
                        if row.get("head_sha") != head_sha:
                            return None
                        ancestor = ensure_earlier()
                        if not ancestor.explains_check_run(row):
                            return False
    except ReviewInputError:
        return None
    return True


def _inspect_live_ledger_readonly(
    *,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    current_base: str,
    current_head: str,
    actions_token: str | None = None,
    app_id: int | None = None,
    dispatch_status: str = "known_clean",
    native_effects_confirmed: bool | None = None,
    budget: EvidenceReadBudget | None = None,
    inspect_settlement_witness: bool = False,
    failure_diagnostic: str | None = None,
) -> tuple[
    SessionRecord | None,
    RecoveryClassification,
    str,
    str,
    str,
    bool,
    bool,
    bool | None,
    str | None,
]:
    if not isinstance(ledger, GitHubIssueCommentSessionLedger):
        raise ReviewInputError("live recovery classification requires native ledger")
    _require_sha(current_base, "current_base")
    _require_sha(current_head, "current_head")
    if dispatch_status not in DISPATCH_STATUSES:
        raise ReviewInputError("recovery dispatch_status is unsupported")
    effective_app_id = app_id if app_id is not None else ledger.authenticated_app_id
    if effective_app_id is not None and (
        type(effective_app_id) is not int or effective_app_id <= 0
    ):
        raise ReviewInputError("live recovery app_id must be a positive integer")

    from .review_completion_recovery import _ReadOnlyHttp

    read_budget = (
        budget
        if budget is not None
        else (
            ledger.evidence_budget
            if ledger.evidence_budget is not None
            else EvidenceReadBudget()
        )
    )
    ro_http = _ReadOnlyHttp(ledger.http)
    ro_ledger = GitHubIssueCommentSessionLedger(
        http=ro_http,
        token=ledger.token,
        app_slug=ledger.app_slug,
        evidence_budget=read_budget,
    )
    loaded = ro_ledger.load(identity)
    if loaded.status == "missing":
        return (
            None,
            RecoveryClassification(
                state_code="unenrolled_ready",
                terminal=True,
                automatic_zero_inference=True,
                head_relation="no_transaction_head",
                base_relation="no_transaction_base",
                run_attempt=None,
                executor_status="not_applicable",
                transaction_phase="no-transaction",
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=(),
                primary_route="ordinary_rollover",
                fallback_routes=(),
                operator_action=(
                    "No session root exists yet; ordinary initial enrollment is ready."
                ),
            ),
            "unknown",
            "unknown",
            "unknown",
            True,
            False,
            None,
            None,
        )
    if loaded.status != "ok" or loaded.record is None:
        return (
            None,
            RecoveryClassification(
                state_code="unauthenticated_ledger_root",
                terminal=False,
                automatic_zero_inference=False,
                head_relation="no_transaction_head",
                base_relation="no_transaction_base",
                run_attempt=None,
                executor_status="not_applicable",
                transaction_phase="no-transaction",
                action_phase=None,
                dispatch_status=dispatch_status,
                missing_witnesses=("authenticated_session_root",),
                primary_route="controlled_serialized_restart",
                fallback_routes=(),
                operator_action=(
                    "Session ledger root failed integrity or ownership checks; "
                    "recover via one designated controlled serialized restart."
                ),
            ),
            "unknown",
            "unknown",
            "unknown",
            False,
            False,
            None,
            None,
        )

    record = loaded.record
    if record.api_run_accounting is None:
        return (
            record,
            classify_recovery_state(
                record,
                current_base=current_base,
                current_head=current_head,
                dispatch_status=dispatch_status,
            ),
            "unknown",
            "unknown",
            "unknown",
            False,
            False,
            None,
            None,
        )

    try:
        old = ApiRunAccounting.from_document(record.api_run_accounting)
    except ReviewInputError:
        return (
            record,
            classify_recovery_state(
                record,
                current_base=current_base,
                current_head=current_head,
                dispatch_status=dispatch_status,
            ),
            "unknown",
            "unknown",
            "unknown",
            False,
            False,
            None,
            None,
        )

    action_authenticated: bool | None = None
    if old.action is not None and old.action.get("phase") == "terminal":
        try:
            if old.version == PARTITIONED_ACTION_ACCOUNTING_VERSION:
                _read_bound_action_result(
                    ro_ledger, identity, record, budget=read_budget
                )
            else:
                read_action_result(ro_ledger, identity, record)
            action_authenticated = True
        except ReviewInputError:
            action_authenticated = False

    producer: str | None = None
    author_id: int | None = None
    try:
        candidate_producer = ro_ledger.evidence_producer()
    except ReviewInputError:
        candidate_producer = None
    if candidate_producer is not None and candidate_producer.startswith("github-bot:"):
        raw_author = candidate_producer.removeprefix("github-bot:")
        if raw_author.isdigit() and int(raw_author) > 0:
            producer = candidate_producer
            author_id = int(raw_author)

    tx = record.transaction
    retained_ok = True
    coverage_only_partial = False
    if tx is not None:
        if author_id is None:
            retained_ok = False
        else:
            port = GitHubIssueCommentOperationPort(
                ro_http,
                token=ro_ledger.token,
                repository=identity.repository,
                pull_request=identity.pull_request,
                app_user_id=author_id,
                strict_association=True,
            )
            store = CheckpointResultStore(
                port,
                app_user_id=author_id,
                http=ro_http,
                identity=identity,
                transaction=tx,
                app_id=effective_app_id,
                compact_write=False,
            )
            try:
                available = store.read_available(event_key=_event_key(identity, tx))
            except ReviewInputError:
                available = None
            if available is None:
                retained_ok = False
            else:
                try:
                    parsed_result = ReviewResult.from_dict(json.loads(available[1]))
                    _payload(parsed_result, projected=True)
                except (ValueError, TypeError, ReviewInputError):
                    retained_ok = False
                else:
                    if parsed_result.content_digest() != tx.result_sha256 or (
                        parsed_result.transaction is not None
                        and parsed_result.transaction.transaction_id
                        != tx.transaction_id
                    ):
                        retained_ok = False
                    else:
                        coverage_only_partial = bool(
                            parsed_result.coverage_only_partial
                        )

    executor_status = "not_applicable"
    executor_sha = "unknown"
    executor_commit = "unknown"
    workflow_blob = "unknown"
    if actions_token is not None and old.owner.job in {"hosted", "local", "command"}:
        (
            executor_status,
            executor_sha,
            executor_commit,
            workflow_blob,
        ) = _probe_executor_details_readonly(
            ro_http,
            actions_token=actions_token,
            identity=identity,
            run_id=old.identity.run_id,
            owner=old.owner,
            tx_head_sha=tx.head_sha if tx is not None else None,
        )

    effective_native_effects_confirmed = native_effects_confirmed
    if (
        tx is None
        and old.analysis_abort is not None
        and effective_native_effects_confirmed is None
    ):
        abort_ledger = GitHubIssueCommentSessionLedger(
            http=_ReadOnlyNoTransactionHttp(ledger.http),
            token=ledger.token,
            app_slug=ledger.app_slug,
            evidence_budget=read_budget,
        )
        late_effects = _detect_late_native_effects(
            abort_ledger,
            identity,
            record,
            old,
            app_slug=ledger.app_slug,
            app_user_id=author_id,
        )
        effective_native_effects_confirmed = late_effects is False

    settlement_witness_val: PreExternalWriteSettlementWitness | bool | None = None
    if (tx is None and old.analysis_abort is None) or (
        tx is not None and tx.phase == "publication_failed"
    ):
        settlement_witness_val = _inspect_pre_write_witness_readonly(
            ro_ledger,
            identity,
            record,
            old,
            author_id=author_id,
            producer=producer,
            budget=read_budget,
            force_scan=inspect_settlement_witness,
        )

    if effective_native_effects_confirmed is None and (
        settlement_witness_val is not None
        or (
            _is_pre_482_lifecycle_overflow(failure_diagnostic)
            and (
                (tx is None and old.analysis_abort is None)
                or (
                    tx is not None
                    and tx.phase in {"publication_failed", "publication_pending"}
                )
            )
        )
    ):
        census_result = _inspect_pre_write_census_readonly(
            ro_ledger,
            identity,
            record,
            old,
            author_id=author_id,
            budget=read_budget,
        )
        if census_result is not None:
            effective_native_effects_confirmed = census_result

    classification = classify_recovery_state(
        record,
        current_base=current_base,
        current_head=current_head,
        executor_status=executor_status,
        dispatch_status=dispatch_status,
        coverage_only_partial=coverage_only_partial,
        retained_context_available=retained_ok,
        native_effects_confirmed=effective_native_effects_confirmed,
        action_result_authenticated=action_authenticated,
        evidence_producer=producer,
        settlement_witness=settlement_witness_val,
        failure_diagnostic=failure_diagnostic,
    )
    return (
        record,
        classification,
        executor_sha,
        executor_commit,
        workflow_blob,
        retained_ok,
        coverage_only_partial,
        action_authenticated,
        producer,
    )


def classify_live_ledger_recovery(
    *,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    current_base: str,
    current_head: str,
    actions_token: str | None = None,
    app_id: int | None = None,
    dispatch_status: str = "known_clean",
    native_effects_confirmed: bool | None = None,
    budget: EvidenceReadBudget | None = None,
    inspect_settlement_witness: bool = False,
    failure_diagnostic: str | None = None,
) -> RecoveryClassification:
    """Inspect a live ledger strictly read-only (`_ReadOnlyHttp`) and classify."""
    return _inspect_live_ledger_readonly(
        ledger=ledger,
        identity=identity,
        current_base=current_base,
        current_head=current_head,
        actions_token=actions_token,
        app_id=app_id,
        dispatch_status=dispatch_status,
        native_effects_confirmed=native_effects_confirmed,
        budget=budget,
        inspect_settlement_witness=inspect_settlement_witness,
        failure_diagnostic=failure_diagnostic,
    )[1]


def build_live_ledger_attempt_evidence_packet(
    *,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    current_base: str,
    current_head: str,
    case_id: str | None = None,
    actions_token: str | None = None,
    app_id: int | None = None,
    dispatch_status: str = "known_clean",
    native_effects_confirmed: bool | None = None,
    installed_core_version: str = "unknown",
    installed_hosted_version: str = "unknown",
    publication_receipt_id: int | None = None,
    check_receipt_id: int | None = None,
    thread_cleanup_receipt_state: str | None = None,
    native_approval_receipt_id: int | None = None,
    historical_settlement_receipt_supplied: bool = False,
    diagnostics_available: bool = False,
    diagnostic_cause: str | None = None,
    budget: EvidenceReadBudget | None = None,
    inspect_settlement_witness: bool = False,
    failure_diagnostic: str | None = None,
) -> AttemptEvidencePacket:
    """Inspect a live ledger strictly read-only (`_ReadOnlyHttp`) into an `AttemptEvidencePacket`."""
    (
        record,
        classification,
        executor_sha,
        executor_commit,
        workflow_blob,
        retained_ok,
        coverage_only_partial,
        action_authenticated,
        producer,
    ) = _inspect_live_ledger_readonly(
        ledger=ledger,
        identity=identity,
        current_base=current_base,
        current_head=current_head,
        actions_token=actions_token,
        app_id=app_id,
        dispatch_status=dispatch_status,
        native_effects_confirmed=native_effects_confirmed,
        budget=budget,
        inspect_settlement_witness=inspect_settlement_witness,
        failure_diagnostic=failure_diagnostic,
    )
    return _assemble_attempt_evidence_packet(
        identity=identity,
        record=record,
        classification=classification,
        current_base=current_base,
        current_head=current_head,
        case_id=case_id,
        executor_sha=executor_sha,
        executor_commit=executor_commit,
        workflow_blob=workflow_blob,
        installed_core_version=installed_core_version,
        installed_hosted_version=installed_hosted_version,
        retained_context_available=retained_ok,
        coverage_only_partial=coverage_only_partial,
        action_result_authenticated=action_authenticated,
        evidence_producer=producer,
        publication_receipt_id=publication_receipt_id,
        check_receipt_id=check_receipt_id,
        thread_cleanup_receipt_state=thread_cleanup_receipt_state,
        native_approval_receipt_id=native_approval_receipt_id,
        historical_settlement_receipt_supplied=historical_settlement_receipt_supplied,
        diagnostics_available=diagnostics_available,
        diagnostic_cause=diagnostic_cause,
    )


def provenance_manifest_from_attempt_packet(
    packet: AttemptEvidencePacket,
) -> ProvenanceManifest:
    """Convert a historical advisor ``AttemptEvidencePacket`` to a sanitized ``ProvenanceManifest``."""

    if not isinstance(packet, AttemptEvidencePacket):
        raise ReviewInputError("packet must be an AttemptEvidencePacket")
    selected_executor = UNKNOWN_PROVENANCE
    if packet.executor_sha != UNKNOWN_PROVENANCE:
        selected_executor = f"github-actions:{packet.executor_sha}"
    elif packet.executor_commit != UNKNOWN_PROVENANCE:
        selected_executor = f"github-actions:{packet.executor_commit}"
    return build_provenance_manifest(
        repository=packet.repository,
        repository_id=packet.repository_id,
        pull_request=packet.pull_request,
        base_sha=packet.base_sha if packet.base_sha is not None else UNKNOWN_PROVENANCE,
        head_sha=packet.head_sha if packet.head_sha is not None else UNKNOWN_PROVENANCE,
        reusable_workflow_tag_object=packet.executor_sha,
        reusable_workflow_peeled_commit=packet.executor_commit,
        installed_core_version=packet.installed_core_version,
        installed_hosted_version=packet.installed_hosted_version,
        selected_executor=selected_executor,
        frozen_policy_commit=(
            packet.policy_commit
            if packet.policy_commit is not None
            else UNKNOWN_PROVENANCE
        ),
        frozen_policy_sha256=(
            packet.policy_digest
            if packet.policy_digest is not None
            else UNKNOWN_PROVENANCE
        ),
        invocation_evidence=InvocationProvenanceEvidence(
            observation_scope="retained_attempt"
        ),
        auto_inspect_local_packages=False,
        inspect_module_origins=False,
    )
