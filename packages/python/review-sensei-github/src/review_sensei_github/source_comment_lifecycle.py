"""Source-comment edit/delete lifecycle classification and recovery routing (G27).

Classifies live GitHub comment mutations (unchanged, edited text, restored original
body text at a newer ``updated_at``, deleted comment) and webhook delivery anomalies
(duplicate delivery, late/stale revision delivery) across all five action lifecycle
phases:

1. ``before_admission`` — before ``admit_action`` persists durable pending intent.
2. ``unstarted_intent`` — ``action.phase == "pending"``, ``execution_started is False``,
   zero uncertain caller requests, and no published pending receipt.
3. ``started_work`` — ``action.phase == "pending"`` with ``execution_started is True``
   or unresolved caller request uncertainty.
4. ``pending_receipt`` — ``action.phase == "pending"`` after a durable pending receipt
   or unfinished reassessment state exists.
5. ``terminal_completed`` — ``action.phase == "terminal"`` with an authenticated
   immutable ``caller-action-result-v1`` receipt.

Invariants enforced:
- Every comment revision is identified by the full tuple
  ``(kind, id, updated_at, body_sha256)``. Editing a comment and later restoring the
  exact original body text produces ``mutation_state == "edit_restored_body"`` because
  ``updated_at`` advanced; it is never accepted as an unchanged replay of the original
  revision.
- Narrow ``G32`` cancellation/supersession applies strictly to ``unstarted_intent``
  when live source mutation is proven AND positive writer exclusion/cessation is
  authenticated. Without witness proof, ``unstarted_intent`` holds fail-closed.
- ``started_work`` and ``pending_receipt`` never silently settle and never enter ``G32``;
  prior charges and durable receipts remain held with bounded recovery guidance.
- ``terminal_completed`` preserves immutable receipts, dispositions, and approvals
  with zero replay across source edits, deletions, and duplicate/late deliveries.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    FrozenApiConfiguration,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator,
    IssuedWriterToken,
)

from .api_run_history import read_action_result
from .api_run_owner import ApiRunActionsEvidence
from .api_run_requests import json_copy
from .unstarted_action_recovery import (
    NoBusinessDispatchEvidence,
    authenticate_unstarted_cancellation,
)

if TYPE_CHECKING:
    from .api_run_requests import ApiRunRequestTransport
    from .session_ledger import GitHubIssueCommentSessionLedger

_ISO_STAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

LIFECYCLE_PHASES: Final[frozenset[str]] = frozenset(
    {
        "before_admission",
        "unstarted_intent",
        "started_work",
        "pending_receipt",
        "terminal_completed",
    }
)
SOURCE_MUTATION_STATES: Final[frozenset[str]] = frozenset(
    {
        "unchanged",
        "source_edited",
        "edit_restored_body",
        "source_deleted",
    }
)
DELIVERY_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {
        "current_revision",
        "duplicate_delivery",
        "late_stale_delivery",
        "mutated_revision",
        "deleted_source",
    }
)
RECOVERY_ROUTES: Final[frozenset[str]] = frozenset(
    {
        "admit_current_revision",
        "refuse_before_admission",
        "execute_same_source",
        "refuse_stale_delivery",
        "g32_unstarted_cancellation",
        "hold_started_work",
        "hold_pending_receipt",
        "preserve_terminal_no_replay",
    }
)


@dataclass(frozen=True)
class SourceRevisionObservation:
    """Metered live GitHub comment observation compared against bound source identity."""

    comment_id: int
    kind: str
    status: int
    observed_updated_at: str | None
    observed_body_sha256: str | None
    mutation_state: str
    delivery_disposition: str


@dataclass(frozen=True)
class SourceLifecycleClassification:
    """Deterministic lifecycle classification and bounded recovery routing decision."""

    identity: SessionIdentity
    phase: str
    original_source: Mapping[str, Any]
    observation: SourceRevisionObservation
    route: str
    g32_eligible: bool
    replay_permitted: bool
    prior_charges_preserved: bool
    guidance: str


def _validate_source_descriptor(
    source: object, *, label: str = "source"
) -> dict[str, Any]:
    if not isinstance(source, Mapping) or set(source) != {
        "kind",
        "id",
        "updated_at",
        "body_sha256",
    }:
        raise ReviewInputError(f"{label} descriptor shape is invalid")
    kind = source["kind"]
    comment_id = source["id"]
    updated_at = source["updated_at"]
    body_sha256 = source["body_sha256"]
    if (
        kind not in {"issue_comment", "review_comment"}
        or type(comment_id) is not int
        or comment_id <= 0
        or not isinstance(updated_at, str)
        or _ISO_STAMP_RE.fullmatch(updated_at) is None
        or not isinstance(body_sha256, str)
        or _SHA256_RE.fullmatch(body_sha256) is None
    ):
        raise ReviewInputError(f"{label} descriptor fields are invalid")
    return {
        "kind": kind,
        "id": comment_id,
        "updated_at": updated_at,
        "body_sha256": body_sha256,
    }


def build_source_comment_descriptor(
    *,
    kind: str,
    comment_id: int,
    updated_at: str,
    body: str,
) -> dict[str, Any]:
    """Build and validate a canonical ``(kind, id, updated_at, body_sha256)`` descriptor."""

    if not isinstance(body, str):
        raise ReviewInputError("source comment body must be a string")
    return _validate_source_descriptor(
        {
            "kind": kind,
            "id": comment_id,
            "updated_at": updated_at,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        },
        label="source comment",
    )


def admit_event_comment_source(
    event_payload: object,
    *,
    pull_request: int,
    source_comment_id: int,
    purpose: str,
    event_name: str,
    expected_body: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Validate an incoming runner/webhook comment event before action admission."""

    if event_name not in {"issue_comment", "pull_request_review_comment"}:
        raise ReviewInputError("unsupported source comment event")
    if not isinstance(event_payload, Mapping):
        raise ReviewInputError("action source does not match the original event")
    raw_action = event_payload.get("action")
    if raw_action is not None:
        if not isinstance(raw_action, str) or not raw_action.strip():
            raise ReviewInputError("action source event action is invalid")
        normalized_action = raw_action.strip()
        if normalized_action == "deleted":
            raise ReviewInputError(
                "pre-admission source check refused (source_deleted/deleted_source): "
                "deleted comment event cannot admit an action"
            )
        if normalized_action == "edited":
            raise ReviewInputError(
                "pre-admission source check refused (source_edited/mutated_revision): "
                "edited comment event cannot admit or replay an action"
            )
        if normalized_action != "created":
            raise ReviewInputError(
                f"pre-admission source check refused: unsupported comment action {normalized_action!r}"
            )
    if isinstance(event_payload.get("changes"), Mapping) and (
        "body" in event_payload["changes"]
    ):
        raise ReviewInputError(
            "pre-admission source check refused (source_edited/mutated_revision): "
            "comment mutation payload cannot admit an action"
        )
    comment = event_payload.get("comment")
    pull = event_payload.get("issue") or event_payload.get("pull_request")
    if (
        not isinstance(comment, Mapping)
        or not isinstance(pull, Mapping)
        or pull.get("number") != pull_request
        or comment.get("id") != source_comment_id
        or not isinstance(comment.get("body"), str)
        or not isinstance(comment.get("updated_at"), str)
    ):
        raise ReviewInputError("action source does not match the original event")
    if purpose == "command" and comment["body"] != expected_body:
        raise ReviewInputError("command body differs from the original source")
    if purpose == "reply" and comment["updated_at"] != expected_updated_at:
        raise ReviewInputError("reply source version differs from the original event")
    kind = (
        "review_comment"
        if event_name == "pull_request_review_comment"
        else "issue_comment"
    )
    return build_source_comment_descriptor(
        kind=kind,
        comment_id=comment["id"],
        updated_at=comment["updated_at"],
        body=comment["body"],
    )


def compare_source_comment_revisions(
    original_source: Mapping[str, Any],
    observed_payload: Mapping[str, Any] | None = None,
    *,
    deleted: bool = False,
    phase: str = "before_admission",
    event_delivery: Mapping[str, Any] | None = None,
) -> SourceRevisionObservation:
    """Compare a bound source descriptor against a live or delivered comment revision."""

    if phase not in LIFECYCLE_PHASES:
        raise ReviewInputError("source lifecycle phase is unsupported")
    bound = _validate_source_descriptor(original_source, label="original source")
    delivered = (
        None
        if event_delivery is None
        else _validate_source_descriptor(event_delivery, label="event delivery")
    )
    if delivered is not None and (
        delivered["kind"] != bound["kind"] or delivered["id"] != bound["id"]
    ):
        raise ReviewInputError("event delivery comment identity conflicts with source")
    if deleted:
        return SourceRevisionObservation(
            comment_id=bound["id"],
            kind=bound["kind"],
            status=404,
            observed_updated_at=None,
            observed_body_sha256=None,
            mutation_state="source_deleted",
            delivery_disposition="deleted_source",
        )
    if (
        not isinstance(observed_payload, Mapping)
        or type(observed_payload.get("id")) is not int
        or observed_payload["id"] != bound["id"]
        or not isinstance(observed_payload.get("updated_at"), str)
        or _ISO_STAMP_RE.fullmatch(observed_payload["updated_at"]) is None
    ):
        raise ReviewInputError("live source lifecycle payload is malformed")

    observed_updated_at = observed_payload["updated_at"]
    if isinstance(observed_payload.get("body"), str):
        observed_body_sha256 = hashlib.sha256(
            observed_payload["body"].encode("utf-8")
        ).hexdigest()
    elif (
        isinstance(observed_payload.get("body_sha256"), str)
        and _SHA256_RE.fullmatch(observed_payload["body_sha256"]) is not None
    ):
        observed_body_sha256 = observed_payload["body_sha256"]
    else:
        raise ReviewInputError("live source lifecycle payload is malformed")

    if (
        observed_updated_at == bound["updated_at"]
        and observed_body_sha256 == bound["body_sha256"]
    ):
        mutation_state = "unchanged"
        if delivered is None:
            disposition = (
                "duplicate_delivery"
                if phase == "terminal_completed"
                else "current_revision"
            )
        elif (
            delivered["updated_at"] == bound["updated_at"]
            and delivered["body_sha256"] == bound["body_sha256"]
        ):
            disposition = (
                "current_revision"
                if phase == "before_admission"
                else "duplicate_delivery"
            )
        else:
            disposition = "late_stale_delivery"
    elif (
        observed_body_sha256 == bound["body_sha256"]
        and observed_updated_at != bound["updated_at"]
    ):
        mutation_state = "edit_restored_body"
        if delivered is not None and (
            delivered["updated_at"] != observed_updated_at
            or delivered["body_sha256"] != observed_body_sha256
        ):
            disposition = "late_stale_delivery"
        else:
            disposition = "mutated_revision"
    else:
        mutation_state = "source_edited"
        if delivered is not None and (
            delivered["updated_at"] != observed_updated_at
            or delivered["body_sha256"] != observed_body_sha256
        ):
            disposition = "late_stale_delivery"
        else:
            disposition = "mutated_revision"

    return SourceRevisionObservation(
        comment_id=bound["id"],
        kind=bound["kind"],
        status=200,
        observed_updated_at=observed_updated_at,
        observed_body_sha256=observed_body_sha256,
        mutation_state=mutation_state,
        delivery_disposition=disposition,
    )


def observe_live_source_revision(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    original_source: Mapping[str, Any],
    phase: str,
    event_delivery: Mapping[str, Any] | None = None,
) -> SourceRevisionObservation:
    """Meter one live comment GET and classify mutation and delivery freshness."""

    if phase not in LIFECYCLE_PHASES:
        raise ReviewInputError("source lifecycle phase is unsupported")
    caller.observe_action_transport_state(ledger, identity)
    bound = _validate_source_descriptor(original_source, label="original source")
    if event_delivery is not None:
        delivered = _validate_source_descriptor(event_delivery, label="event delivery")
        if delivered["kind"] != bound["kind"] or delivered["id"] != bound["id"]:
            raise ReviewInputError(
                "event delivery comment identity conflicts with source"
            )

    suffix = (
        "/issues/comments/" if bound["kind"] == "issue_comment" else "/pulls/comments/"
    ) + str(bound["id"])
    path = ledger.http.repository_path(identity.repository, suffix)
    with caller.read_operation("source-comment-lifecycle-fence"):
        status, payload = ledger.http.request("GET", path, token=ledger.token)

    if status == 404:
        return compare_source_comment_revisions(
            bound,
            deleted=True,
            phase=phase,
            event_delivery=event_delivery,
        )
    if status != 200:
        raise ReviewInputError(
            f"live source lifecycle observation failed with HTTP {status}"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), str):
        raise ReviewInputError("live source lifecycle payload is malformed")
    return compare_source_comment_revisions(
        bound,
        payload,
        phase=phase,
        event_delivery=event_delivery,
    )


def _has_durable_pending_receipt(
    record: SessionRecord,
    *,
    explicit_pending_receipt: bool,
    action_started: bool,
) -> bool:
    if explicit_pending_receipt or record.publication_staging is not None:
        return True
    if record.assessment_queue is not None:
        if record.assessment_queue.get("active_operation") is not None:
            return True
        manifest = record.assessment_queue.get("state_manifest")
        if (
            action_started
            and isinstance(manifest, Mapping)
            and int(manifest.get("item_count", 0)) > 0
        ):
            return True
    return False


def _determine_phase_and_source(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord | None,
    candidate_source: Mapping[str, Any] | None,
    has_pending_receipt: bool,
) -> tuple[str, dict[str, Any]]:
    transport_state = caller.observe_action_transport_state(ledger, identity)
    if record is None:
        if candidate_source is None:
            raise ReviewInputError(
                "pre-admission source lifecycle classification requires candidate_source"
            )
        return (
            "before_admission",
            _validate_source_descriptor(candidate_source, label="candidate source"),
        )
    if not isinstance(record, SessionRecord) or record.identity() != identity:
        raise ReviewInputError("source lifecycle record scope conflicts")
    if record.api_run_accounting is None:
        raise ReviewInputError("source lifecycle record lacks caller accounting")
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    state.require_writer_version()
    action = state.action
    if action is None:
        if candidate_source is None:
            raise ReviewInputError(
                "pre-admission source lifecycle classification requires candidate_source"
            )
        return (
            "before_admission",
            _validate_source_descriptor(candidate_source, label="candidate source"),
        )
    bound_source = _validate_source_descriptor(
        action["intent"]["source"], label="admitted source"
    )
    if candidate_source is not None:
        validated_candidate = _validate_source_descriptor(
            candidate_source, label="candidate source"
        )
        if (
            validated_candidate["kind"] != bound_source["kind"]
            or validated_candidate["id"] != bound_source["id"]
        ):
            raise ReviewInputError(
                "candidate source comment identity conflicts with admitted action"
            )
    if action["phase"] == "terminal":
        return "terminal_completed", bound_source
    same_caller_unresolved = (
        transport_state.is_activated
        and state.identity == caller.run
        and (
            transport_state.has_unresolved_or_started_work
            or transport_state.has_live_started_accounting
        )
    )
    has_conflicting_review_work = (
        record.reservation_id is not None
        or record.reserved_slot is not None
        or (
            record.transaction is not None
            and record.transaction.phase
            not in {"publication_succeeded", "publication_suppressed"}
        )
        or state.analysis_abort is not None
        or state.terminal_transaction is not None
    )
    action_started = (
        bool(action["execution_started"])
        or state.uncertain > 0
        or same_caller_unresolved
        or has_conflicting_review_work
    )
    if _has_durable_pending_receipt(
        record,
        explicit_pending_receipt=has_pending_receipt,
        action_started=action_started,
    ):
        return "pending_receipt", bound_source
    if action_started:
        return "started_work", bound_source
    return "unstarted_intent", bound_source


def _require_still_unstarted_before_g32(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    candidate_source: Mapping[str, Any] | None,
    has_pending_receipt: bool,
) -> None:
    current_phase, _ = _determine_phase_and_source(
        caller=caller,
        ledger=ledger,
        identity=identity,
        record=record,
        candidate_source=candidate_source,
        has_pending_receipt=has_pending_receipt,
    )
    if current_phase != "unstarted_intent":
        raise ReviewInputError(
            f"action transitioned to {current_phase} before G32 cancellation; "
            "hold fail-closed with prior charges preserved"
        )


def classify_source_comment_lifecycle(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord | None,
    candidate_source: Mapping[str, Any] | None = None,
    event_delivery: Mapping[str, Any] | None = None,
    has_pending_receipt: bool = False,
) -> SourceLifecycleClassification:
    """Classify source-comment mutation across all five action lifecycle phases."""

    phase, original_source = _determine_phase_and_source(
        caller=caller,
        ledger=ledger,
        identity=identity,
        record=record,
        candidate_source=candidate_source,
        has_pending_receipt=has_pending_receipt,
    )
    if phase == "terminal_completed":
        if record is None:
            raise ReviewInputError("terminal source lifecycle requires a record")
        read_action_result(ledger, identity, record)

    observation = observe_live_source_revision(
        caller=caller,
        ledger=ledger,
        identity=identity,
        original_source=original_source,
        phase=phase,
        event_delivery=event_delivery,
    )
    if record is not None and phase == "unstarted_intent":
        phase, _ = _determine_phase_and_source(
            caller=caller,
            ledger=ledger,
            identity=identity,
            record=record,
            candidate_source=candidate_source,
            has_pending_receipt=has_pending_receipt,
        )
    mut = observation.mutation_state
    disp = observation.delivery_disposition

    if phase == "before_admission":
        if mut == "unchanged" and disp == "current_revision":
            return SourceLifecycleClassification(
                identity=identity,
                phase=phase,
                original_source=cast(Mapping[str, Any], json_copy(original_source)),
                observation=observation,
                route="admit_current_revision",
                g32_eligible=False,
                replay_permitted=False,
                prior_charges_preserved=True,
                guidance=(
                    "live source matches requested revision; admit action once under "
                    "exact (kind, id, updated_at, body_sha256) binding"
                ),
            )
        return SourceLifecycleClassification(
            identity=identity,
            phase=phase,
            original_source=cast(Mapping[str, Any], json_copy(original_source)),
            observation=observation,
            route="refuse_before_admission",
            g32_eligible=False,
            replay_permitted=False,
            prior_charges_preserved=True,
            guidance=(
                f"pre-admission source check refused ({mut}/{disp}): zero session "
                "mutation allowed; submit or dispatch a fresh event bound to the "
                "current live comment revision"
            ),
        )

    if phase == "unstarted_intent":
        if mut == "unchanged":
            if disp == "late_stale_delivery":
                return SourceLifecycleClassification(
                    identity=identity,
                    phase=phase,
                    original_source=cast(Mapping[str, Any], json_copy(original_source)),
                    observation=observation,
                    route="refuse_stale_delivery",
                    g32_eligible=False,
                    replay_permitted=False,
                    prior_charges_preserved=True,
                    guidance=(
                        "late or forged delivery tuple disagrees with bound unstarted "
                        "intent; keep pending intent unchanged"
                    ),
                )
            return SourceLifecycleClassification(
                identity=identity,
                phase=phase,
                original_source=cast(Mapping[str, Any], json_copy(original_source)),
                observation=observation,
                route="execute_same_source",
                g32_eligible=False,
                replay_permitted=False,
                prior_charges_preserved=True,
                guidance=(
                    "live source is unchanged; same-source owner succession executes "
                    "normally without G32 cancellation"
                ),
            )
        return SourceLifecycleClassification(
            identity=identity,
            phase=phase,
            original_source=cast(Mapping[str, Any], json_copy(original_source)),
            observation=observation,
            route="g32_unstarted_cancellation",
            g32_eligible=True,
            replay_permitted=False,
            prior_charges_preserved=True,
            guidance=(
                f"unstarted pending intent observed {mut} ({disp}); default execution "
                "and rollover hold fail-closed; recover once via authenticated G32 "
                "UnstartedCancellationWitness preserving prior charges and history"
            ),
        )

    if phase == "started_work":
        return SourceLifecycleClassification(
            identity=identity,
            phase=phase,
            original_source=cast(Mapping[str, Any], json_copy(original_source)),
            observation=observation,
            route="hold_started_work",
            g32_eligible=False,
            replay_permitted=False,
            prior_charges_preserved=True,
            guidance=(
                f"action has started or uncertain caller work ({mut}/{disp}); G32 "
                "unstarted cancellation is forbidden; pending work requires exact result recovery; "
                "hold fail-closed with prior "
                "charges preserved until authentic effect readback or terminal closure"
            ),
        )

    if phase == "pending_receipt":
        return SourceLifecycleClassification(
            identity=identity,
            phase=phase,
            original_source=cast(Mapping[str, Any], json_copy(original_source)),
            observation=observation,
            route="hold_pending_receipt",
            g32_eligible=False,
            replay_permitted=False,
            prior_charges_preserved=True,
            guidance=(
                f"durable pending receipt exists ({mut}/{disp}); source edit or "
                "deletion cannot silently settle reassessment or invoke G32; hold "
                "pending receipt and original source attribution intact"
            ),
        )

    return SourceLifecycleClassification(
        identity=identity,
        phase=phase,
        original_source=cast(Mapping[str, Any], json_copy(original_source)),
        observation=observation,
        route="preserve_terminal_no_replay",
        g32_eligible=False,
        replay_permitted=False,
        prior_charges_preserved=True,
        guidance=(
            f"action is already terminal ({mut}/{disp}); preserve immutable receipt, "
            "dispositions, and approvals with zero replay"
        ),
    )


def execute_source_lifecycle_route(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord | None,
    candidate_source: Mapping[str, Any] | None = None,
    event_delivery: Mapping[str, Any] | None = None,
    has_pending_receipt: bool = False,
    mode: str = "same_run",
    actions_evidence: ApiRunActionsEvidence | None = None,
    canonical_coordinator: ContainedForwardingCoordinator | None = None,
    canonical_token: IssuedWriterToken | None = None,
    no_business_dispatch_evidence: NoBusinessDispatchEvidence | None = None,
    require_positive_dispatch_proof: bool = False,
    rollover_configuration: FrozenApiConfiguration | None = None,
    rollover_owner: ApiExecutionOwner | None = None,
) -> tuple[SourceLifecycleClassification, SessionRecord | ApiRunAccounting | None]:
    """Execute the bounded action/recovery decision for a classified source comment."""

    classification = classify_source_comment_lifecycle(
        caller=caller,
        ledger=ledger,
        identity=identity,
        record=record,
        candidate_source=candidate_source,
        event_delivery=event_delivery,
        has_pending_receipt=has_pending_receipt,
    )
    route = classification.route

    if route in {"admit_current_revision", "execute_same_source"}:
        return classification, record

    if route == "preserve_terminal_no_replay":
        return classification, record

    if route == "g32_unstarted_cancellation":
        if record is None:
            raise ReviewInputError(
                "unstarted cancellation route requires a durable session record"
            )
        if (
            actions_evidence is None
            and canonical_coordinator is None
            and canonical_token is None
        ):
            raise ReviewInputError(classification.guidance)
        _require_still_unstarted_before_g32(
            caller=caller,
            ledger=ledger,
            identity=identity,
            record=record,
            candidate_source=candidate_source,
            has_pending_receipt=has_pending_receipt,
        )
        if mode == "same_run":
            witness = authenticate_unstarted_cancellation(
                caller=caller,
                ledger=ledger,
                identity=identity,
                record=record,
                actions_evidence=actions_evidence,
                canonical_coordinator=canonical_coordinator,
                canonical_token=canonical_token,
                no_business_dispatch_evidence=no_business_dispatch_evidence,
                require_positive_dispatch_proof=require_positive_dispatch_proof,
                mode="same_run",
            )
            _require_still_unstarted_before_g32(
                caller=caller,
                ledger=ledger,
                identity=identity,
                record=record,
                candidate_source=candidate_source,
                has_pending_receipt=has_pending_receipt,
            )
            settled = caller.cancel_unstarted_action(ledger, identity, witness)
            return classification, settled
        if mode == "cross_run":
            if rollover_configuration is None or rollover_owner is None:
                raise ReviewInputError(
                    "cross-run unstarted recovery requires configuration and owner"
                )

            def _authenticate_current(current: SessionRecord):
                _require_still_unstarted_before_g32(
                    caller=caller,
                    ledger=ledger,
                    identity=identity,
                    record=current,
                    candidate_source=candidate_source,
                    has_pending_receipt=has_pending_receipt,
                )
                return authenticate_unstarted_cancellation(
                    caller=caller,
                    ledger=ledger,
                    identity=identity,
                    record=current,
                    actions_evidence=actions_evidence,
                    canonical_coordinator=canonical_coordinator,
                    canonical_token=canonical_token,
                    no_business_dispatch_evidence=no_business_dispatch_evidence,
                    require_positive_dispatch_proof=require_positive_dispatch_proof,
                    mode="cross_run",
                )

            activated = caller.rollover_unstarted_and_activate(
                ledger,
                identity,
                rollover_configuration,
                rollover_owner,
                record,
                _authenticate_current,
            )
            return classification, activated
        raise ReviewInputError("unstarted recovery mode is unsupported")

    raise ReviewInputError(classification.guidance)
