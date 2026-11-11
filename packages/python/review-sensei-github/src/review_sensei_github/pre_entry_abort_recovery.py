"""Pre-entry `no-transaction` abort qualification and bounded recovery (G29).

Models the SiteVault `#142` predecessor boundary (`phase=no-transaction` in
`_accept_activation`):

1. **Missing transaction != no operation happened**: A predecessor session
   record with `record.transaction is None` can arise after `Provider.complete`
   already entered and released its reservation in `finally`, after a lost
   closure write (`closure-absent`), or after an unconfirmed crash. Only an
   authenticated `analysis_abort` receipt paired with ceased-owner proof, zero
   provider entry, confirmed reservation release, clean caller dispatch, and
   absence of late native effects proves that no operation started and qualifies
   ordinary rollover (`pre_entry_abort_rollover`).
2. **Conservative liability & fail-closed negative controls**: Whenever provider
   non-entry, clean dispatch, reservation release, owner cessation, or absence
   of late effects is unproven, qualification fails closed (`qualifies_rollover`
   is `False`), charges conservative caller and provider liability holds, and
   forbids fresh `reenroll`/reset abuse.
3. **Always-recoverable path**: Every fail-closed `no-transaction` predecessor
   with a ceased owner yields an authenticated, schema-validated recovery
   descriptor (`NoTransactionRecoveryDescriptor`, `authority=False`) binding
   the exact predecessor run IDs, immutable comment digests, conservative
   unknown-cost holds, and prior finding obligations into a designated
   `controlled-restart-registry-v1` registration so no PR is permanently wedged
   at `phase=no-transaction`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord

from . import controlled_restart
from .publication import (
    APPROVAL_MARKER_PREFIX,
    REVIEW_MARKER_PREFIX,
    finding_publication_scope,
    review_identity_marker,
)
from .session_ledger import parse_session_comment

_HEX40 = re.compile(r"[a-f0-9]{40}")
_HEX64 = re.compile(r"[a-f0-9]{64}")

NO_TRANSACTION_STATES = frozenset(
    {
        "qualified_pre_entry_abort",
        "pre_entry_abort_owner_active",
        "pre_entry_abort_late_effects",
        "pre_entry_abort_corrupt_receipt",
        "no_transaction_active_reservation",
        "no_transaction_provider_entered",
        "no_transaction_unknown_dispatch",
        "no_transaction_unconfirmed_predecessor",
        "not_no_transaction_boundary",
    }
)

NO_TRANSACTION_ROUTES = frozenset(
    {
        "pre_entry_abort_rollover",
        "wait_for_owner_cessation_then_rollover",
        "wait_for_owner_cessation_then_recover",
        "reservation_recovery_or_controlled_restart",
        "controlled_serialized_restart",
        "delegate_to_recovery_state_classifier",
    }
)

RECOVERY_CLASSIFIER_ID = "no-transaction-controlled-restart-v1"


@dataclass(frozen=True)
class NoTransactionAbortQualification:
    """Deterministic qualification of a `phase=no-transaction` predecessor."""

    state_code: str
    qualifies_rollover: bool
    missing_transaction_means_no_operation: bool
    owner_ceased: bool
    provider_entry_proven_absent: bool
    reservation_release_confirmed: bool
    dispatch_clean: bool
    late_effects_absent: bool
    conservative_nonstartup_caller_liability: int
    conservative_total_caller_liability: int
    conservative_provider_call_hold: int
    missing_predicates: tuple[str, ...]
    primary_route: str
    fallback_routes: tuple[str, ...]
    operator_action: str
    reenroll_permitted: bool = False
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "no-transaction qualification is diagnostic only and never grants authority"
            )
        if self.reenroll_permitted is not False:
            raise ReviewInputError(
                "enrolled no-transaction recovery never authorizes fresh reenroll reset"
            )
        if self.state_code not in NO_TRANSACTION_STATES:
            raise ReviewInputError(
                f"unknown no-transaction qualification state: {self.state_code}"
            )
        if self.primary_route not in NO_TRANSACTION_ROUTES or any(
            route not in NO_TRANSACTION_ROUTES for route in self.fallback_routes
        ):
            raise ReviewInputError("unknown no-transaction recovery route")
        if self.missing_transaction_means_no_operation and (
            self.state_code != "qualified_pre_entry_abort"
            or not self.qualifies_rollover
        ):
            raise ReviewInputError(
                "missing transaction cannot imply no-operation outside qualified pre-entry abort"
            )
        if self.qualifies_rollover and self.missing_predicates:
            raise ReviewInputError(
                "qualified pre-entry abort rollover cannot carry missing predicates"
            )
        if (
            self.conservative_nonstartup_caller_liability < 0
            or self.conservative_total_caller_liability
            < self.conservative_nonstartup_caller_liability
            or self.conservative_provider_call_hold < 0
        ):
            raise ReviewInputError("conservative liability bounds must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_code": self.state_code,
            "qualifies_rollover": self.qualifies_rollover,
            "missing_transaction_means_no_operation": (
                self.missing_transaction_means_no_operation
            ),
            "owner_ceased": self.owner_ceased,
            "provider_entry_proven_absent": self.provider_entry_proven_absent,
            "reservation_release_confirmed": self.reservation_release_confirmed,
            "dispatch_clean": self.dispatch_clean,
            "late_effects_absent": self.late_effects_absent,
            "conservative_nonstartup_caller_liability": (
                self.conservative_nonstartup_caller_liability
            ),
            "conservative_total_caller_liability": (
                self.conservative_total_caller_liability
            ),
            "conservative_provider_call_hold": self.conservative_provider_call_hold,
            "missing_predicates": list(self.missing_predicates),
            "primary_route": self.primary_route,
            "fallback_routes": list(self.fallback_routes),
            "operator_action": self.operator_action,
            "reenroll_permitted": False,
            "authority": False,
        }


@dataclass(frozen=True)
class NoTransactionRecoveryDescriptor:
    """Authenticated operator recovery descriptor for unconfirmed `no-transaction` runs.

    Retains conservative unknown-cost holds and prior finding obligations while
    producing a schema-validated `controlled-restart-registry-v1` registration
    row. Carrying this descriptor never grants runtime execution authority on
    its own (`authority=False`).
    """

    classifier_id: str
    qualification_state: str
    predecessor_run_id: int
    predecessor_root_sha256: str
    retained_nonstartup_caller_liability: int
    retained_total_caller_liability: int
    retained_provider_call_hold: int
    prior_finding_obligations_count: int
    controlled_restart_registration: dict[str, Any]
    operator_instructions: str
    reenroll_permitted: bool = False
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "no-transaction recovery descriptor never grants runtime authority"
            )
        if self.reenroll_permitted is not False:
            raise ReviewInputError(
                "no-transaction recovery descriptor forbids reenroll reset"
            )
        if self.classifier_id != RECOVERY_CLASSIFIER_ID:
            raise ReviewInputError("unsupported no-transaction recovery classifier")

    @property
    def request_id(self) -> str:
        return str(self.controlled_restart_registration["request_id"])

    def registry_document(self) -> dict[str, Any]:
        return {
            "schema_version": "controlled-restart-registry-v1",
            "registrations": [dict(self.controlled_restart_registration)],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifier_id": self.classifier_id,
            "qualification_state": self.qualification_state,
            "predecessor_run_id": self.predecessor_run_id,
            "predecessor_root_sha256": self.predecessor_root_sha256,
            "retained_nonstartup_caller_liability": (
                self.retained_nonstartup_caller_liability
            ),
            "retained_total_caller_liability": self.retained_total_caller_liability,
            "retained_provider_call_hold": self.retained_provider_call_hold,
            "prior_finding_obligations_count": self.prior_finding_obligations_count,
            "controlled_restart_registration": dict(
                self.controlled_restart_registration
            ),
            "operator_instructions": self.operator_instructions,
            "reenroll_permitted": False,
            "authority": False,
        }


def _conservative_bounds(old: ApiRunAccounting) -> tuple[int, int]:
    nonstartup = (
        old.prior_nonstartup_liability
        + old.confirmed
        + old.uncertain
        + old.reserved_undispatched
    )
    total = old.startup.liability + nonstartup
    return nonstartup, total


def _count_prior_obligations(record: SessionRecord) -> int:
    """Count open/retained prior finding obligations recorded on the session."""
    counts: list[int] = []
    history = record.convergence_history
    if isinstance(history, Mapping):
        progress = history.get("progress")
        if isinstance(progress, list):
            for entry in reversed(progress):
                if isinstance(entry, Mapping) and isinstance(
                    entry.get("blocker_count"), int
                ):
                    counts.append(max(0, int(entry["blocker_count"])))
                    break
        baseline = history.get("baseline")
        if isinstance(baseline, Mapping):
            if isinstance(baseline.get("item_count"), int):
                counts.append(max(0, int(baseline["item_count"])))
            elif isinstance(baseline.get("findings"), list):
                counts.append(len(baseline["findings"]))
            else:
                try:
                    from review_sensei.baseline import baseline_from_history_document

                    decoded_baseline = baseline_from_history_document(dict(baseline))
                    counts.append(len(decoded_baseline.findings))
                except Exception:
                    pass
    queue = record.assessment_queue
    if isinstance(queue, Mapping):
        manifest = queue.get("state_manifest")
        if isinstance(manifest, Mapping) and isinstance(
            manifest.get("item_count"), int
        ):
            counts.append(max(0, int(manifest["item_count"])))
    snapshot = record.disposition_snapshot
    if isinstance(snapshot, Mapping):
        manifest = snapshot.get("state_manifest")
        if isinstance(manifest, Mapping) and isinstance(
            manifest.get("item_count"), int
        ):
            counts.append(max(0, int(manifest["item_count"])))
    return max(counts, default=0)


def _validate_analysis_abort_receipt(
    record: SessionRecord, old: ApiRunAccounting
) -> tuple[bool, ReviewTransaction | None, tuple[str, ...]]:
    """Verify exact structural and cryptographic bindings of `old.analysis_abort`."""
    receipt = old.analysis_abort
    if not isinstance(receipt, Mapping):
        return False, None, ("authenticated_analysis_abort_receipt",)
    expected_keys = {
        "source_root",
        "cleanup_root",
        "transaction",
        "entry_state",
        "run_id",
        "owner",
    }
    failures: list[str] = []
    if set(receipt) != expected_keys:
        return False, None, ("valid_analysis_abort_schema",)
    source_root = receipt.get("source_root")
    cleanup_root = receipt.get("cleanup_root")
    if (
        not isinstance(source_root, str)
        or _HEX64.fullmatch(source_root) is None
        or not isinstance(cleanup_root, str)
        or _HEX64.fullmatch(cleanup_root) is None
        or source_root == cleanup_root
    ):
        failures.append("distinct_abort_source_and_cleanup_roots")
    if receipt.get("entry_state") != "not_started":
        failures.append("pre_entry_not_started_state")
    if receipt.get("run_id") != old.identity.run_id:
        failures.append("matching_abort_run_id")
    if receipt.get("owner") != asdict(old.owner):
        failures.append("matching_abort_owner_slot")
    raw_tx = receipt.get("transaction")
    abort_tx: ReviewTransaction | None = None
    if not isinstance(raw_tx, Mapping):
        failures.append("valid_aborted_analysis_transaction")
    else:
        try:
            abort_tx = ReviewTransaction.from_dict(dict(raw_tx))
        except Exception:
            failures.append("valid_aborted_analysis_transaction")
        else:
            if abort_tx.phase != "analysis":
                failures.append("analysis_phase_abort_transaction")
    if record.transaction is not None:
        failures.append("null_session_transaction_after_abort_cleanup")
    if record.reservation_id is not None or record.reserved_slot is not None:
        failures.append("confirmed_reservation_release")
    if not failures and (
        record.generation < 2 or record.record_sha256 in {source_root, cleanup_root}
    ):
        failures.append("verified_cleanup_root_lineage")
    return (len(failures) == 0), abort_tx, tuple(failures)


def verify_reenroll_forbidden(record: SessionRecord) -> None:
    """Reject unauthorized `reenroll` or fresh-enrollment reset on enrolled state."""
    if not isinstance(record, SessionRecord):
        raise ReviewInputError("session record is required")
    if record.api_run_accounting is not None:
        raise ReviewInputError(
            "fresh reenroll/reset is forbidden on an enrolled API-accounted PR; "
            "recover via pre-entry abort rollover or designated controlled serialized restart"
        )


def qualify_no_transaction_abort(
    record: SessionRecord,
    *,
    owner_ceased: bool = True,
    provider_entry_started: bool | None = None,
    unreconciled_mutation_present: bool = False,
    late_native_effects_present: bool = False,
) -> NoTransactionAbortQualification:
    """Evaluate exact G29 pre-entry `no-transaction` predicates on `record`."""
    if not isinstance(record, SessionRecord):
        raise ReviewInputError("qualification requires a valid SessionRecord")
    if record.api_run_accounting is None:
        raise ReviewInputError(
            "no-transaction qualification requires API-accounted session state"
        )
    old = ApiRunAccounting.from_document(record.api_run_accounting)
    nonstartup_liab, total_liab = _conservative_bounds(old)

    # Non-`no-transaction` boundaries (e.g., settled review or action, or
    # unclosed publication transaction) delegate to the G03 state classifier.
    if old.action is not None or (
        record.transaction is not None
        and old.analysis_abort is None
        and record.reservation_id is None
        and record.reserved_slot is None
    ):
        return NoTransactionAbortQualification(
            state_code="not_no_transaction_boundary",
            qualifies_rollover=False,
            missing_transaction_means_no_operation=False,
            owner_ceased=owner_ceased,
            provider_entry_proven_absent=False,
            reservation_release_confirmed=(
                record.reservation_id is None and record.reserved_slot is None
            ),
            dispatch_clean=(old.uncertain == 0 and not unreconciled_mutation_present),
            late_effects_absent=not late_native_effects_present,
            conservative_nonstartup_caller_liability=nonstartup_liab,
            conservative_total_caller_liability=total_liab,
            conservative_provider_call_hold=1,
            missing_predicates=("no_transaction_predecessor_boundary",),
            primary_route="delegate_to_recovery_state_classifier",
            fallback_routes=("controlled_serialized_restart",),
            operator_action=(
                "Record is outside the `no-transaction` abort boundary; "
                "use `classify_recovery_state` to select the exact recovery route."
            ),
        )

    dispatch_clean = old.uncertain == 0 and not unreconciled_mutation_present
    release_confirmed = (
        record.reservation_id is None
        and record.reserved_slot is None
        and record.transaction is None
    )

    # Case 1: `analysis_abort` receipt is present on `ApiRunAccounting`.
    if old.analysis_abort is not None:
        valid_receipt, _abort_tx, receipt_failures = _validate_analysis_abort_receipt(
            record, old
        )
        if not valid_receipt or provider_entry_started is True:
            missing_corrupt = list(receipt_failures)
            if provider_entry_started is True:
                missing_corrupt.append("zero_provider_entry")
            return NoTransactionAbortQualification(
                state_code="pre_entry_abort_corrupt_receipt",
                qualifies_rollover=False,
                missing_transaction_means_no_operation=False,
                owner_ceased=owner_ceased,
                provider_entry_proven_absent=False,
                reservation_release_confirmed=release_confirmed,
                dispatch_clean=dispatch_clean,
                late_effects_absent=not late_native_effects_present,
                conservative_nonstartup_caller_liability=nonstartup_liab,
                conservative_total_caller_liability=total_liab,
                conservative_provider_call_hold=1,
                missing_predicates=tuple(missing_corrupt),
                primary_route="controlled_serialized_restart",
                fallback_routes=(),
                operator_action=(
                    "Pre-entry `analysis_abort` receipt conflicts with session "
                    "state or provider entry; isolate via controlled serialized restart."
                ),
            )
        if not dispatch_clean:
            return NoTransactionAbortQualification(
                state_code="no_transaction_unknown_dispatch",
                qualifies_rollover=False,
                missing_transaction_means_no_operation=False,
                owner_ceased=owner_ceased,
                provider_entry_proven_absent=True,
                reservation_release_confirmed=True,
                dispatch_clean=False,
                late_effects_absent=not late_native_effects_present,
                conservative_nonstartup_caller_liability=nonstartup_liab,
                conservative_total_caller_liability=total_liab,
                conservative_provider_call_hold=0,
                missing_predicates=("zero_uncertain_caller_mutations",),
                primary_route="controlled_serialized_restart",
                fallback_routes=(),
                operator_action=(
                    "Caller accounting carries uncertain/unconfirmed mutations; "
                    "retain conservative caller liability and recover via controlled restart."
                ),
            )
        if late_native_effects_present:
            return NoTransactionAbortQualification(
                state_code="pre_entry_abort_late_effects",
                qualifies_rollover=False,
                missing_transaction_means_no_operation=False,
                owner_ceased=owner_ceased,
                provider_entry_proven_absent=True,
                reservation_release_confirmed=True,
                dispatch_clean=True,
                late_effects_absent=False,
                conservative_nonstartup_caller_liability=nonstartup_liab,
                conservative_total_caller_liability=total_liab,
                conservative_provider_call_hold=1,
                missing_predicates=("zero_late_native_effects",),
                primary_route="controlled_serialized_restart",
                fallback_routes=(),
                operator_action=(
                    "Late native review or thread effects exist on the aborted "
                    "scope; refuse rollover and isolate via controlled serialized restart."
                ),
            )
        if not owner_ceased:
            return NoTransactionAbortQualification(
                state_code="pre_entry_abort_owner_active",
                qualifies_rollover=False,
                missing_transaction_means_no_operation=False,
                owner_ceased=False,
                provider_entry_proven_absent=True,
                reservation_release_confirmed=True,
                dispatch_clean=True,
                late_effects_absent=True,
                conservative_nonstartup_caller_liability=nonstartup_liab,
                conservative_total_caller_liability=total_liab,
                conservative_provider_call_hold=0,
                missing_predicates=("predecessor_owner_cessation",),
                primary_route="wait_for_owner_cessation_then_rollover",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Wait for predecessor Actions run cessation, then run ordinary "
                    "`review-run` rollover."
                ),
            )
        return NoTransactionAbortQualification(
            state_code="qualified_pre_entry_abort",
            qualifies_rollover=True,
            missing_transaction_means_no_operation=True,
            owner_ceased=True,
            provider_entry_proven_absent=True,
            reservation_release_confirmed=True,
            dispatch_clean=True,
            late_effects_absent=True,
            conservative_nonstartup_caller_liability=nonstartup_liab,
            conservative_total_caller_liability=total_liab,
            conservative_provider_call_hold=0,
            missing_predicates=(),
            primary_route="pre_entry_abort_rollover",
            fallback_routes=(),
            operator_action=(
                "Eligible for ordinary `review-run` rollover: pre-entry abort "
                "closure, reservation release, clean dispatch, and ceased owner are proven."
            ),
        )

    # Case 2: Active/unreleased reservation (`cleanup-absent` or crashed during analysis).
    if record.reservation_id is not None or record.reserved_slot is not None:
        missing_res = [
            "confirmed_reservation_release",
            "authenticated_analysis_abort_receipt",
        ]
        if not owner_ceased:
            missing_res.append("predecessor_owner_cessation")
        if not dispatch_clean:
            missing_res.append("zero_uncertain_caller_mutations")
        return NoTransactionAbortQualification(
            state_code="no_transaction_active_reservation",
            qualifies_rollover=False,
            missing_transaction_means_no_operation=False,
            owner_ceased=owner_ceased,
            provider_entry_proven_absent=(provider_entry_started is False),
            reservation_release_confirmed=False,
            dispatch_clean=dispatch_clean,
            late_effects_absent=not late_native_effects_present,
            conservative_nonstartup_caller_liability=nonstartup_liab,
            conservative_total_caller_liability=total_liab,
            conservative_provider_call_hold=0 if provider_entry_started is False else 1,
            missing_predicates=tuple(missing_res),
            primary_route=(
                "reservation_recovery_or_controlled_restart"
                if owner_ceased
                else "wait_for_owner_cessation_then_recover"
            ),
            fallback_routes=("controlled_serialized_restart",),
            operator_action=(
                "Reservation cleanup was not confirmed (`cleanup-absent` or interrupted "
                "analysis); confirm owner cessation and recover via authenticated "
                "reservation recovery or controlled serialized restart."
            ),
        )

    # Case 3: `record.transaction is None`, `analysis_abort is None` (SiteVault #142 class).
    if provider_entry_started is True:
        missing_prov = [
            "zero_provider_entry",
            "authenticated_analysis_abort_receipt",
        ]
        if not owner_ceased:
            missing_prov.append("predecessor_owner_cessation")
        return NoTransactionAbortQualification(
            state_code="no_transaction_provider_entered",
            qualifies_rollover=False,
            missing_transaction_means_no_operation=False,
            owner_ceased=owner_ceased,
            provider_entry_proven_absent=False,
            reservation_release_confirmed=True,
            dispatch_clean=dispatch_clean,
            late_effects_absent=not late_native_effects_present,
            conservative_nonstartup_caller_liability=nonstartup_liab,
            conservative_total_caller_liability=total_liab,
            conservative_provider_call_hold=1,
            missing_predicates=tuple(missing_prov),
            primary_route=(
                "controlled_serialized_restart"
                if owner_ceased
                else "wait_for_owner_cessation_then_recover"
            ),
            fallback_routes=(),
            operator_action=(
                "Provider execution already entered before reservation cleanup "
                "cleared `record.transaction`; missing transaction does NOT mean "
                "no operation happened. Charge conservative liability and recover "
                "via designated controlled serialized restart after owner cessation."
            ),
        )

    if not dispatch_clean:
        missing_unk = [
            "zero_uncertain_caller_mutations",
            "authenticated_analysis_abort_receipt",
        ]
        if not owner_ceased:
            missing_unk.append("predecessor_owner_cessation")
        return NoTransactionAbortQualification(
            state_code="no_transaction_unknown_dispatch",
            qualifies_rollover=False,
            missing_transaction_means_no_operation=False,
            owner_ceased=owner_ceased,
            provider_entry_proven_absent=(provider_entry_started is False),
            reservation_release_confirmed=True,
            dispatch_clean=False,
            late_effects_absent=not late_native_effects_present,
            conservative_nonstartup_caller_liability=nonstartup_liab,
            conservative_total_caller_liability=total_liab,
            conservative_provider_call_hold=0 if provider_entry_started is False else 1,
            missing_predicates=tuple(missing_unk),
            primary_route=(
                "controlled_serialized_restart"
                if owner_ceased
                else "wait_for_owner_cessation_then_recover"
            ),
            fallback_routes=(),
            operator_action=(
                "Abort closure write or caller mutation had unknown/unconfirmed "
                "dispatch (`closure-absent`); retain conservative liability and "
                "recover via designated controlled serialized restart."
            ),
        )

    missing_unconf = [
        "authenticated_analysis_abort_receipt",
        "proven_provider_non_entry",
    ]
    if late_native_effects_present:
        missing_unconf.append("zero_late_native_effects")
    if not owner_ceased:
        missing_unconf.append("predecessor_owner_cessation")
    return NoTransactionAbortQualification(
        state_code="no_transaction_unconfirmed_predecessor",
        qualifies_rollover=False,
        missing_transaction_means_no_operation=False,
        owner_ceased=owner_ceased,
        provider_entry_proven_absent=False,
        reservation_release_confirmed=True,
        dispatch_clean=True,
        late_effects_absent=not late_native_effects_present,
        conservative_nonstartup_caller_liability=nonstartup_liab,
        conservative_total_caller_liability=total_liab,
        conservative_provider_call_hold=1,
        missing_predicates=tuple(missing_unconf),
        primary_route=(
            "controlled_serialized_restart"
            if owner_ceased
            else "wait_for_owner_cessation_then_recover"
        ),
        fallback_routes=(),
        operator_action=(
            "Session record has `phase=no-transaction` without an authenticated "
            "`analysis_abort` receipt (SiteVault #142 class). Do not reset or "
            "reenroll; retain unknown-cost holds and recover via designated "
            "controlled serialized restart."
        ),
    )


def build_no_transaction_recovery_descriptor(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    qualification: NoTransactionAbortQualification,
    old_comments: Sequence[Mapping[str, Any]],
    designated_run_id: str,
    base_sha: str,
    head_sha: str,
    policy_commit: str,
    workflow_sha: str,
    issuance_disabled_at: int,
    retire_after: int,
    job: str = "hosted",
    extra_old_runs: Sequence[int] = (),
) -> NoTransactionRecoveryDescriptor:
    """Build a validated `controlled-restart-registry-v1` recovery descriptor.

    Ensures a PR stuck at `phase=no-transaction` always has a concrete,
    deterministic, schema-checked operator recovery route while preserving
    unknown-cost holds, prior finding obligations, and opaque predecessor comments.
    """
    if not isinstance(identity, SessionIdentity) or not isinstance(
        record, SessionRecord
    ):
        raise ReviewInputError(
            "recovery descriptor requires valid identity and session record"
        )
    if record.api_run_accounting is None:
        raise ReviewInputError(
            "recovery descriptor requires API-accounted session state"
        )
    if qualification.qualifies_rollover:
        raise ReviewInputError(
            "qualified pre-entry abort recovers automatically via ordinary rollover"
        )
    if not qualification.owner_ceased:
        raise ReviewInputError(
            "cannot issue recovery descriptor before predecessor Actions owner ceases"
        )
    old = ApiRunAccounting.from_document(record.api_run_accounting)
    old_run_ids: list[int] = [old.identity.run_id]
    for run_id in extra_old_runs:
        if type(run_id) is not int or run_id <= 0:
            raise ReviewInputError(
                "extra_old_runs must contain positive integer run IDs"
            )
        if run_id not in old_run_ids:
            old_run_ids.append(run_id)

    normalized_comments = [dict(ref) for ref in old_comments]
    row_body: dict[str, Any] = {
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "pull_request": identity.pull_request,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "policy_commit": policy_commit,
        "workflow_sha": workflow_sha,
        "run_id": designated_run_id,
        "job": job,
        "issuance_disabled_at": issuance_disabled_at,
        "retire_after": retire_after,
        "old_runs": old_run_ids,
        "old_comments": normalized_comments,
    }
    row_body["request_id"] = hashlib.sha256(canonical_bytes(row_body)).hexdigest()
    registry_bytes = canonical_bytes(
        {
            "schema_version": "controlled-restart-registry-v1",
            "registrations": [row_body],
        }
    )
    validated_rows = controlled_restart.read_registry(registry_bytes)
    validated_row = validated_rows[0]

    obligations = _count_prior_obligations(record)
    return NoTransactionRecoveryDescriptor(
        classifier_id=RECOVERY_CLASSIFIER_ID,
        qualification_state=qualification.state_code,
        predecessor_run_id=old.identity.run_id,
        predecessor_root_sha256=record.record_sha256,
        retained_nonstartup_caller_liability=(
            qualification.conservative_nonstartup_caller_liability
        ),
        retained_total_caller_liability=(
            qualification.conservative_total_caller_liability
        ),
        retained_provider_call_hold=qualification.conservative_provider_call_hold,
        prior_finding_obligations_count=obligations,
        controlled_restart_registration=validated_row,
        operator_instructions=(
            f"Register request_id={validated_row['request_id']} in "
            "`controlled_restarts.json`, wait until retire_after="
            f"{validated_row['retire_after']}, and dispatch `restart-run "
            f"--restart-id {validated_row['request_id']}` on run_id="
            f"{validated_row['run_id']}. Do NOT reenroll or delete predecessor comments."
        ),
    )


def _probe_owner_ceased(
    ledger: Any,
    identity: SessionIdentity,
    old: ApiRunAccounting,
    actions_token: str | None,
) -> bool:
    http = getattr(ledger, "http", None)
    token = actions_token or getattr(ledger, "token", None)
    if http is not None and isinstance(token, str) and token.strip():
        run_path = http.repository_path(
            identity.repository, f"/actions/runs/{old.identity.run_id}"
        )
        status, run_payload = http.request("GET", run_path, token=token)
        if status == 200 and isinstance(run_payload, dict):
            repo = run_payload.get("repository")
            attempt = run_payload.get("run_attempt")
            if (
                run_payload.get("id") == old.identity.run_id
                and isinstance(repo, dict)
                and repo.get("id") == identity.repository_id
                and repo.get("full_name") == identity.repository
                and run_payload.get("status") == "completed"
                and isinstance(attempt, int)
                and not isinstance(attempt, bool)
                and attempt >= old.owner.run_attempt
            ):
                jobs_path = http.repository_path(
                    identity.repository,
                    f"/actions/runs/{old.identity.run_id}/attempts/{old.owner.run_attempt}/jobs?per_page=100&page=1",
                )
                j_status, j_payload = http.request("GET", jobs_path, token=token)
                if j_status == 200 and isinstance(j_payload, dict):
                    jobs = j_payload.get("jobs")
                    total_count = j_payload.get("total_count")
                    if (
                        isinstance(jobs, list)
                        and (
                            total_count is None
                            or (
                                isinstance(total_count, int)
                                and not isinstance(total_count, bool)
                                and total_count == len(jobs)
                            )
                        )
                        and all(
                            isinstance(j, dict) and j.get("status") == "completed"
                            for j in jobs
                        )
                    ):
                        return True
                return False
    run_reader = getattr(ledger, "_run_reader", None)
    ceased_fn = getattr(run_reader, "ceased_owner", None)
    if callable(ceased_fn):
        try:
            ceased_fn(identity, old.identity, old.owner)
            return True
        except Exception:
            return False
    return False


def _detect_late_native_effects(
    ledger: Any,
    identity: SessionIdentity,
    record: SessionRecord,
    old: ApiRunAccounting,
) -> bool:
    """Detect whether ReviewSensei reviews or threads exist for an aborted scope."""
    abort_head: str | None = None
    if isinstance(old.analysis_abort, Mapping):
        raw_tx = old.analysis_abort.get("transaction")
        if isinstance(raw_tx, Mapping):
            raw_head = raw_tx.get("head_sha")
            if isinstance(raw_head, str):
                abort_head = raw_head

    http = getattr(ledger, "http", None)
    token = getattr(ledger, "token", None)
    if (
        http is None
        or not isinstance(token, str)
        or not token.strip()
        or identity.repository_id is None
    ):
        return False

    reviews_path = http.repository_path(
        identity.repository,
        f"/pulls/{identity.pull_request}/reviews?per_page=100&page=1",
    )
    r_status, reviews = http.request("GET", reviews_path, token=token)
    if r_status == 200 and isinstance(reviews, list):
        expected_prefix = (
            review_identity_marker(
                repository_id=identity.repository_id,
                pull_request=identity.pull_request,
                head_sha=abort_head,
            )
            if abort_head is not None
            else f"{REVIEW_MARKER_PREFIX} repo={identity.repository_id} pr={identity.pull_request} "
        )
        approval_prefix = f"{APPROVAL_MARKER_PREFIX} repo={identity.repository_id} pr={identity.pull_request} "
        for item in reviews:
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            if not isinstance(body, str):
                continue
            if expected_prefix in body:
                if abort_head is not None or (
                    record.transaction is None and old.terminal_transaction is None
                ):
                    return True
            if (
                approval_prefix in body
                and record.transaction is None
                and old.terminal_transaction is None
            ):
                if abort_head is None or f"head={abort_head}" in body:
                    return True

    owner, _, name = identity.repository.partition("/")
    gql_status, gql_payload = http.request(
        "POST",
        "/graphql",
        body={
            "query": (
                "query LateAbortEffects($owner: String!, $name: String!, $number: Int!, $first: Int!) { "
                "repository(owner: $owner, name: $name) { pullRequest(number: $number) { "
                "reviewThreads(first: $first) { nodes { id isResolved comments(first: 1) { nodes { body } } } } "
                "} } }"
            ),
            "variables": {
                "owner": owner,
                "name": name,
                "number": identity.pull_request,
                "first": 100,
            },
        },
        token=token,
    )
    if gql_status == 200 and isinstance(gql_payload, dict):
        threads = (
            gql_payload.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes")
        )
        if isinstance(threads, list):
            for thread in threads:
                if not isinstance(thread, dict):
                    continue
                nodes = thread.get("comments", {}).get("nodes")
                if not isinstance(nodes, list) or not nodes:
                    continue
                first_body = (
                    nodes[0].get("body") if isinstance(nodes[0], dict) else None
                )
                if not isinstance(first_body, str):
                    continue
                f_scope = finding_publication_scope(first_body)
                if f_scope is not None:
                    repo_id, pr, head, _base, _blocking, _human = f_scope
                    if (repo_id, pr) == (
                        identity.repository_id,
                        identity.pull_request,
                    ):
                        if abort_head is not None and head == abort_head:
                            return True
                        if (
                            abort_head is None
                            and record.transaction is None
                            and old.terminal_transaction is None
                        ):
                            return True
    return False


def inspect_live_no_transaction_predecessor(
    *,
    ledger: Any,
    identity: SessionIdentity,
    actions_token: str | None = None,
    provider_entry_started: bool | None = None,
) -> tuple[NoTransactionAbortQualification, dict[str, Any]]:
    """Read-only inspection of a live `no-transaction` session comment.

    Returns `(qualification, comment_ref)` where `comment_ref` is the exact
    `{"id", "bytes", "sha256"}` archive reference required by
    `build_no_transaction_recovery_descriptor`.
    """
    http = getattr(ledger, "http", None)
    token = getattr(ledger, "token", None)
    if http is None or not isinstance(token, str) or not token.strip():
        raise ReviewInputError("live no-transaction inspection requires an HTTP ledger")

    comments_path = http.repository_path(
        identity.repository,
        f"/issues/{identity.pull_request}/comments?per_page=100&page=1",
    )
    status, comments = http.request("GET", comments_path, token=token)
    if status != 200 or not isinstance(comments, list):
        raise ReviewInputError(
            "unable to read issue comments for no-transaction inspection"
        )

    matched_comments: list[tuple[dict[str, Any], SessionRecord]] = []
    for item in comments:
        if not isinstance(item, dict) or not isinstance(item.get("body"), str):
            continue
        author = item.get("user")
        if isinstance(author, dict) and author.get("type") != "Bot":
            continue
        parsed = parse_session_comment(item["body"], identity=identity)
        if parsed is not None:
            matched_comments.append((item, parsed))
    if not matched_comments:
        raise ReviewInputError("no enrolled session comment found on pull request")
    if len(matched_comments) > 1:
        raise ReviewInputError(
            "multiple conflicting session comments found on pull request"
        )
    matched_comment, matched_record = matched_comments[0]
    comment_id = matched_comment.get("id")
    if type(comment_id) is not int or comment_id <= 0:
        raise ReviewInputError("session comment has an invalid numeric identifier")
    raw_bytes = str(matched_comment["body"]).encode("utf-8")
    comment_ref = {
        "id": comment_id,
        "bytes": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }
    if matched_record.api_run_accounting is None:
        raise ReviewInputError("session comment is not API-accounted")
    old = ApiRunAccounting.from_document(matched_record.api_run_accounting)
    ceased = _probe_owner_ceased(ledger, identity, old, actions_token)
    late_effects = _detect_late_native_effects(ledger, identity, matched_record, old)
    qualification = qualify_no_transaction_abort(
        matched_record,
        owner_ceased=ceased,
        provider_entry_started=provider_entry_started,
        late_native_effects_present=late_effects,
    )
    return qualification, comment_ref


__all__ = [
    "NO_TRANSACTION_ROUTES",
    "NO_TRANSACTION_STATES",
    "RECOVERY_CLASSIFIER_ID",
    "NoTransactionAbortQualification",
    "NoTransactionRecoveryDescriptor",
    "build_no_transaction_recovery_descriptor",
    "inspect_live_no_transaction_predecessor",
    "qualify_no_transaction_abort",
    "verify_reenroll_forbidden",
]
