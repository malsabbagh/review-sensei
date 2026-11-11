"""Deterministic recovery-state matrix and bounded recovery-route advisor (G03).

Classifies every terminal and unclosed predecessor state across head/base
relations, attempt counters, historical executor qualification, transaction
phases, partial/rescan flags, terminal actions, pre-entry aborts, and uncertain
writes. Outputs are strictly diagnostic (`authority=False`) and never authorize
live resets, forge historical witnesses, or mutate GitHub state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
    read_inline_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.historical_completion import QUALIFIED_EXECUTORS
from review_sensei.models import ReviewResult
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_history import (
    _match_action_result,
    _read_bound_action_result,
    action_result_binding,
    decode_action_result_carrier,
    inherited_preprovider_reference,
    read_action_result,
)
from .api_run_owner import ApiRunActionsEvidence
from .checkpoint_result_store import CheckpointResultStore
from .operation_comments import GitHubIssueCommentOperationPort
from .prior_rescan_publication import admitted_rescan
from .publication_recovery import _event_key, _payload
from .session_ledger import GitHubIssueCommentSessionLedger

SCHEMA_VERSION = "review-recovery-classification-v1"

SUPPORTED_RECOVERY_ROUTES: frozenset[str] = frozenset(
    {
        "ordinary_rollover",
        "operator_resume_then_ordinary_rollover",
        "operator_review_remaining_or_rollover",
        "operator_verify_convergence",
        "automatic_historical_completion",
        "same_run_attempt2_retained_replay",
        "pre_entry_abort_rollover",
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
) -> RecoveryClassification:
    """Classify a `SessionRecord` into an exact state, witness set, and route."""
    if not isinstance(record, SessionRecord):
        raise ReviewInputError("recovery state requires a SessionRecord")
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
        old.require_writer_version()
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
                    verified_action = True
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
        if (
            old.analysis_abort.get("entry_state") == "not_started"
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
            missing_witnesses=("confirmed_pre_entry_non_dispatch",),
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Pre-entry abort lacks non-dispatch or zero-provider proof; keep "
                "conservative holds and recover via controlled serialized restart."
            ),
        )

    if tx is None:
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
    if type(run_id) is not int or run_id < 1:
        return "active_or_unconfirmed"
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
            run_reader.qualified_publication_cessation(
                identity, run_id, owner, head_sha=tx_head_sha
            )
            return "qualified_success"
        except ReviewInputError:
            pass

    try:
        _job_id, _stamp, job_conclusion = run_reader.ceased_owner(identity, owner)
    except ReviewInputError:
        return "active_or_unconfirmed"

    if job_conclusion != "success":
        return "failed"

    try:
        run_doc = run_reader._get(identity, f"/actions/runs/{run_id}")
    except ReviewInputError:
        return "ceased"

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
        return "active_or_unconfirmed"

    if run_attempt > owner.run_attempt:
        return "ceased"

    if run_doc.get("status") != "completed":
        return "active_or_unconfirmed"

    if (
        tx_head_sha is None
        or owner.job == "command"
        or run_doc.get("conclusion") != "success"
    ):
        return "ceased"

    refs = run_doc.get("referenced_workflows")
    if isinstance(refs, list) and len(refs) == 1 and isinstance(refs[0], dict):
        sha = refs[0].get("sha")
        if isinstance(sha, str) and sha in QUALIFIED_EXECUTORS:
            # Workflow SHA is catalogued, so cessation failed on step/chronology/head.
            return "failed"
    return "unrecognized"


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
) -> RecoveryClassification:
    """Inspect a live ledger strictly read-only (`_ReadOnlyHttp`) and classify."""
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
        return RecoveryClassification(
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
        )
    if loaded.status != "ok" or loaded.record is None:
        return RecoveryClassification(
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
        )

    record = loaded.record
    if record.api_run_accounting is None:
        return classify_recovery_state(
            record,
            current_base=current_base,
            current_head=current_head,
            dispatch_status=dispatch_status,
        )

    try:
        old = ApiRunAccounting.from_document(record.api_run_accounting)
        old.require_writer_version()
    except ReviewInputError:
        return classify_recovery_state(
            record,
            current_base=current_base,
            current_head=current_head,
            dispatch_status=dispatch_status,
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
    if actions_token is not None and old.owner.job in {"hosted", "local", "command"}:
        executor_status = _probe_executor_status_readonly(
            ro_http,
            actions_token=actions_token,
            identity=identity,
            run_id=old.identity.run_id,
            owner=old.owner,
            tx_head_sha=tx.head_sha if tx is not None else None,
        )

    return classify_recovery_state(
        record,
        current_base=current_base,
        current_head=current_head,
        executor_status=executor_status,
        dispatch_status=dispatch_status,
        coverage_only_partial=coverage_only_partial,
        retained_context_available=retained_ok,
        native_effects_confirmed=native_effects_confirmed,
        action_result_authenticated=action_authenticated,
        evidence_producer=producer,
    )
