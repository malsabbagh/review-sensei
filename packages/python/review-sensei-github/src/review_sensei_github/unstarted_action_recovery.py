"""Narrow authenticated recovery for unstarted actions after source mutation (G32).

When an admitted command or mention-reply action leaves ``action.phase == "pending"``
with ``execution_started == False`` and zero uncertain dispatches because its live
GitHub source comment was edited or deleted before ``begin_action_effect``, default
execution and rollover remain held fail-closed.

This module provides a strictly opt-in, process-authenticated cancellation/supersession
witness (``UnstartedCancellationWitness``) that never reuses ``controlled_restart``
whole-history abandonment and never activates ``controlled_restarts.json``.
Every transition requires:
1. Stranded ``action.phase == "pending"`` with ``execution_started is False``,
   null result/outcome, zero uncertain requests, and no active review reservation.
2. Exact binding to the durable action intent, session identity, and root digest.
3. A fresh metered GitHub source read proving ``source_edited`` (changed ``updated_at``
   or ``body_sha256``) or ``source_deleted`` (HTTP 404). Unchanged sources refuse so
   ordinary same-source succession executes normally.
4. Positive writer exclusion/cessation proof via ``ApiRunActionsEvidence`` or an
   active ``ContainedForwardingCoordinator`` epoch capability.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Final, cast

from review_sensei.action_result_codec import encode_action_result_carrier
from review_sensei.api_work import (
    UNSTARTED_CANCELLATION_REASONS,
    ApiExecutionOwner,
    ApiRunAccounting,
)
from review_sensei.bounded_evidence import (
    canonical_bytes,
    inline_manifest,
    read_inline_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.recovery_authority import AuthorityRefusal
from review_sensei.session import (
    SessionIdentity,
    SessionRecord,
    _parse_aware_datetime,
)
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator,
    IssuedWriterToken,
    extract_unsettled_dispatches,
)

from .api_run_history import (
    action_result_binding,
    decode_action_result_carrier,
)
from .api_run_owner import ApiRunActionsEvidence
from .api_run_requests import json_copy
from .run_authority import validate_comment_url_binding

if TYPE_CHECKING:
    from .api_run_requests import ApiRunRequestTransport
    from .session_ledger import GitHubIssueCommentSessionLedger

_ISO_STAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)

SAME_RUN_AUTHORITY_MODES: Final[frozenset[str]] = frozenset(
    {
        "same_owner_active",
        "same_run_ceased_predecessor",
        "canonical_epoch_fenced",
    }
)
CROSS_RUN_AUTHORITY_MODES: Final[frozenset[str]] = frozenset(
    {
        "cross_run_ceased_predecessor",
        "canonical_epoch_fenced",
    }
)

_ISSUED_WITNESSES: weakref.WeakSet[UnstartedCancellationWitness] = weakref.WeakSet()
# Process-local capture provenance, never a persisted/cross-process recovery grant.
_ISSUED_DISPATCH_EVIDENCE: dict[int, tuple[Any, ...]] = {}
# Single-rollover cessation verification hooks keyed per freshly created evidence instance.
_ROLLOVER_CESSATION_HOOKS: weakref.WeakKeyDictionary[
    ApiRunActionsEvidence, Callable[[], None]
] = weakref.WeakKeyDictionary()


@contextmanager
def _scoped_rollover_cessation(
    actions_evidence: ApiRunActionsEvidence | None,
    require_cessation: Callable[[], None],
) -> Iterator[None]:
    if actions_evidence is None:
        yield
        return
    _ROLLOVER_CESSATION_HOOKS[actions_evidence] = require_cessation
    try:
        yield
    finally:
        _ROLLOVER_CESSATION_HOOKS.pop(actions_evidence, None)


def _verify_cross_run_cessation_once(
    actions_evidence: ApiRunActionsEvidence,
    *,
    identity: SessionIdentity,
    run_id: int,
    owner: ApiExecutionOwner,
    require_cessation: Callable[[], None] | None,
) -> tuple[object, ...]:
    """Verify complete-inventory cessation once and share its fresh payloads with ``require_cessation``."""
    if require_cessation is None:
        return actions_evidence.ceased_run_owner(
            identity,
            run_id,
            owner,
            complete_terminal_inventory=True,
        )
    http = getattr(actions_evidence, "http", None)
    http_dict = getattr(http, "__dict__", None)
    orig_bound = getattr(http, "request", None)
    if (
        http is None
        or not isinstance(http_dict, dict)
        or not callable(orig_bound)
        or not callable(getattr(http, "repository_path", None))
    ):
        ceased_run = actions_evidence.ceased_run_owner(
            identity,
            run_id,
            owner,
            complete_terminal_inventory=True,
        )
        require_cessation()
        return ceased_run

    had_own_request = "request" in http_dict
    prior_own_request = http_dict.get("request")
    owner_thread = threading.get_ident()
    captured: dict[tuple[str, str], tuple[int, Any]] = {}
    replay_only = False
    actions_prefix = http.repository_path(
        identity.repository, f"/actions/runs/{run_id}"
    )

    def scoped_request(
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Any]:
        if threading.get_ident() == owner_thread and method == "GET":
            key = (path, token)
            if replay_only:
                cached = captured.get(key)
                if cached is not None:
                    return cached[0], copy.deepcopy(cached[1])
            elif token == actions_evidence.token and (
                path == actions_prefix or path.startswith(actions_prefix + "/")
            ):
                status, payload = orig_bound(
                    method,
                    path,
                    token=token,
                    body=body,
                    timeout_seconds=timeout_seconds,
                )
                if status == 200 and isinstance(payload, dict):
                    captured[key] = (status, copy.deepcopy(payload))
                return status, payload
        return orig_bound(
            method,
            path,
            token=token,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    http_dict["request"] = scoped_request
    try:
        ceased_run = actions_evidence.ceased_run_owner(
            identity,
            run_id,
            owner,
            complete_terminal_inventory=True,
        )
        replay_only = True
        require_cessation()
        return ceased_run
    finally:
        if had_own_request:
            http_dict["request"] = prior_own_request
        else:
            http_dict.pop("request", None)


@dataclass(frozen=True)
class NoBusinessDispatchEvidence:
    """Explicit positive proof that a stranded unstarted owner dispatched zero business effects.

    Comment deletion, ``execution_started == False``, null result/outcome markers,
    and cancelled/completed Actions jobs are necessary preconditions but are NEVER
    sufficient settlement authority by themselves on ordinary caller routes.
    """

    identity: SessionIdentity
    target_run_id: int
    target_owner: ApiExecutionOwner
    intent_sha256: str
    observed_requests: tuple[Mapping[str, Any], ...]
    provider_dispatches: int = 0
    accepted_operation_present: bool = False
    native_effects_checked: bool = True

    @classmethod
    def from_stranded_caller(
        cls,
        caller: ApiRunRequestTransport,
        *,
        identity: SessionIdentity,
        record: SessionRecord,
        provider_dispatches: int = 0,
    ) -> NoBusinessDispatchEvidence:
        """Capture non-dispatch evidence from an authenticated stranded caller transport."""

        state, intent, intent_sha256 = _validate_unstarted_record(
            identity=identity,
            record=record,
            expected_intent=None,
        )
        original_owner = ApiExecutionOwner(**intent["owner"])
        if caller.run.run_id != int(intent["run_id"]):
            raise ReviewInputError(
                "stranded caller transport run_id conflicts with action intent"
            )
        if (
            caller._root != record.record_sha256
            or caller.accounting.identity.repository_id != identity.repository_id
            or caller.accounting.owner != original_owner
            or state.owner != original_owner
            or caller.accounting.action is None
            or caller.accounting.action["intent"] != intent
        ):
            raise ReviewInputError(
                "stranded transport source/root/owner binding conflicts"
            )
        accepted_present = bool(
            caller._operation_entry_started
            or caller._provider_effect_unresolved
            or caller._action_effect_boundary is not None
            or caller._action_result_proof is not None
        )
        captured = cls(
            identity=identity,
            target_run_id=caller.run.run_id,
            target_owner=caller.accounting.owner,
            intent_sha256=intent_sha256,
            observed_requests=tuple(
                cast(Mapping[str, Any], json_copy(obs)) for obs in caller.observations
            ),
            provider_dispatches=provider_dispatches,
            accepted_operation_present=accepted_present,
            native_effects_checked=True,
        )
        key = id(captured)
        _ISSUED_DISPATCH_EVIDENCE[key] = (
            weakref.ref(
                captured, lambda _ref: _ISSUED_DISPATCH_EVIDENCE.pop(key, None)
            ),
            _sha256_json(asdict(captured)),
            dict(caller._confirmed_control_writes),
            caller,
            frozenset(caller._physical_mutation_ids),
        )
        return captured


@dataclass(frozen=True, eq=False)
class UnstartedCancellationWitness:
    """Process-issued proof authorizing one narrow unstarted-action cancellation."""

    identity: SessionIdentity
    target_run_id: int
    target_root_sha256: str
    intent_sha256: str
    intent: Mapping[str, Any]
    reason: str
    authority_mode: str
    authority_proof_sha256: str
    live_source_observation: Mapping[str, Any]
    producer: str


def _issue_witness(
    witness: UnstartedCancellationWitness,
) -> UnstartedCancellationWitness:
    _ISSUED_WITNESSES.add(witness)
    return witness


def require_witness_issued(witness: object) -> UnstartedCancellationWitness:
    """Verify that ``witness`` is a live, process-issued cancellation proof."""

    if (
        type(witness) is not UnstartedCancellationWitness
        or witness not in _ISSUED_WITNESSES
    ):
        raise ReviewInputError(
            "unstarted action cancellation requires a process-issued witness"
        )
    return witness


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_unstarted_record(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    expected_intent: Mapping[str, Any] | None,
) -> tuple[ApiRunAccounting, dict[str, Any], str]:
    if not isinstance(record, SessionRecord) or record.identity() != identity:
        raise ReviewInputError("unstarted action record scope conflicts")
    if record.api_run_accounting is None:
        raise ReviewInputError("unstarted action record lacks caller accounting")
    if record.reservation_id is not None or record.reserved_slot is not None:
        raise ReviewInputError("unstarted action conflicts with a review reservation")
    if record.transaction is not None and record.transaction.phase not in {
        "publication_succeeded",
        "publication_suppressed",
    }:
        raise ReviewInputError("unstarted action conflicts with an unfinished review")
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    state.require_writer_version()
    if state.analysis_abort is not None or state.terminal_transaction is not None:
        raise ReviewInputError("unstarted action accounting state conflicts")
    if state.uncertain > 0:
        raise ReviewInputError(
            "action has uncertain caller requests and cannot be canceled as unstarted"
        )
    action = state.action
    if action is None:
        raise ReviewInputError("caller action intent is unavailable")
    if action["phase"] != "pending":
        raise ReviewInputError("caller action is already terminal")
    if action["execution_started"]:
        raise ReviewInputError("started action cannot be canceled as unstarted")
    if action["result"] is not None or action["outcome"] is not None:
        raise ReviewInputError("pending action cannot carry a completion result")
    intent = cast(dict[str, Any], json_copy(action["intent"]))
    if expected_intent is not None and json_copy(expected_intent) != intent:
        raise ReviewInputError("caller action intent does not match stranded record")
    intent_sha256 = _sha256_json(intent)
    return state, intent, intent_sha256


def _observe_mutated_source(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    source: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    if type(identity.repository_id) is not int or identity.repository_id <= 0:
        raise ReviewInputError(
            "live action source observation requires positive repository_id"
        )
    suffix = (
        "/issues/comments/" if source["kind"] == "issue_comment" else "/pulls/comments/"
    ) + str(source["id"])
    path = ledger.http.repository_path(identity.repository, suffix)
    with caller.read_operation("unstarted-action-source-fence"):
        status, payload = ledger.http.request("GET", path, token=ledger.token)
    if status == 404:
        return (
            "source_deleted",
            {
                "status": 404,
                "comment_id": int(source["id"]),
                "observed_updated_at": None,
                "observed_body_sha256": None,
            },
        )
    if status != 200:
        raise ReviewInputError(
            f"live action source observation failed with HTTP {status}"
        )
    if (
        not isinstance(payload, dict)
        or type(payload.get("id")) is not int
        or payload["id"] != source["id"]
        or not isinstance(payload.get("updated_at"), str)
        or _ISO_STAMP_RE.fullmatch(payload["updated_at"]) is None
        or not isinstance(payload.get("body"), str)
        or not validate_comment_url_binding(
            payload,
            kind=str(source["kind"]),
            pull_request=identity.pull_request,
            repository=identity.repository,
            expected_api_url=ledger.http.api_url,
            require_url=True,
        )
    ):
        raise ReviewInputError("live action source payload is malformed")
    observed_updated_at = payload["updated_at"]
    observed_body_sha256 = hashlib.sha256(payload["body"].encode("utf-8")).hexdigest()
    # Every admitted source revision is bound by the full tuple
    # `(kind, id, updated_at, body_sha256)`. Under G27, editing a comment and
    # later restoring its original text (`edit_restored_body`, where `updated_at`
    # advanced while `body_sha256` matches) is intentionally a distinct comment
    # revision rather than a replay of the original revision, so any divergence
    # in `(updated_at, body_sha256)` authorizes narrow G32 unstarted supersession.
    if (
        observed_updated_at == source["updated_at"]
        and observed_body_sha256 == source["body_sha256"]
    ):
        raise ReviewInputError(
            "live action source is unchanged; same-source succession must execute normally"
        )
    return (
        "source_edited",
        {
            "status": 200,
            "comment_id": int(source["id"]),
            "observed_updated_at": observed_updated_at,
            "observed_body_sha256": observed_body_sha256,
        },
    )


def _authenticate_authority_proof(
    *,
    caller: ApiRunRequestTransport,
    identity: SessionIdentity,
    state: ApiRunAccounting,
    intent: Mapping[str, Any],
    mode: str,
    actions_evidence: ApiRunActionsEvidence | None,
    canonical_coordinator: ContainedForwardingCoordinator | None,
    canonical_token: IssuedWriterToken | None,
) -> tuple[str, str]:
    if mode not in {"same_run", "cross_run"}:
        raise ReviewInputError("unstarted cancellation mode is unsupported")
    has_actions = actions_evidence is not None
    has_canonical = canonical_coordinator is not None or canonical_token is not None
    if not has_actions and not has_canonical:
        raise ReviewInputError(
            "unstarted action cancellation requires owner cessation or canonical fence proof"
        )
    original_owner = ApiExecutionOwner(**intent["owner"])
    proof_payload: dict[str, object] = {
        "mode": mode,
        "target_run_id": state.identity.run_id,
        "caller_run_id": caller.run.run_id,
        "caller_attempt": caller.attempt,
        "caller_job": caller.job,
    }
    selected_mode: str | None = None

    if has_actions:
        if not isinstance(actions_evidence, ApiRunActionsEvidence):
            raise ReviewInputError("actions cessation evidence is invalid")
        if (
            actions_evidence.command_run_id != str(caller.run.run_id)
            or actions_evidence.job != caller.job
        ):
            raise ReviewInputError("actions evidence owner scope conflicts")
        with caller.read_operation("unstarted-action-owner-fence"):
            if mode == "same_run":
                actions_evidence.current_owner(
                    identity,
                    attempt=state.owner.run_attempt,
                    generation=state.owner.generation,
                )
                if state.owner.run_attempt > original_owner.run_attempt:
                    ceased = actions_evidence.ceased_owner(identity, original_owner)
                    selected_mode = "same_run_ceased_predecessor"
                    proof_payload["ceased_owner"] = list(ceased)
                else:
                    if state.owner != original_owner:
                        raise ReviewInputError(
                            "unstarted action owner conflicts with original intent"
                        )
                    selected_mode = "same_owner_active"
            else:
                actions_evidence.current_owner(
                    identity,
                    attempt=caller.attempt,
                    generation=0,
                )
                require_cessation = _ROLLOVER_CESSATION_HOOKS.pop(
                    actions_evidence, None
                )
                ceased_run = _verify_cross_run_cessation_once(
                    actions_evidence,
                    identity=identity,
                    run_id=state.identity.run_id,
                    owner=state.owner,
                    require_cessation=require_cessation,
                )
                selected_mode = "cross_run_ceased_predecessor"
                proof_payload["ceased_run_owner"] = list(ceased_run)

    if has_canonical:
        if not isinstance(
            canonical_coordinator, ContainedForwardingCoordinator
        ) or not isinstance(canonical_token, IssuedWriterToken):
            raise ReviewInputError("canonical writer fence proof is incomplete")
        if type(identity.repository_id) is not int or identity.repository_id <= 0:
            raise ReviewInputError(
                "canonical writer fence requires a positive integer repository_id"
            )
        try:
            access = canonical_coordinator.authenticate(canonical_token)
            active_epoch = canonical_coordinator.active_epoch(
                base_store_id=canonical_token.base_store_id,
                repository_id=identity.repository_id,
                pull_request=identity.pull_request,
            )
            snapshot = canonical_coordinator.candidate.snapshot(canonical_token)
            unsettled = extract_unsettled_dispatches(snapshot)
        except AuthorityRefusal as exc:
            raise ReviewInputError(
                f"canonical writer fence refused unstarted cancellation: {exc}"
            ) from exc
        if (
            access.repository != identity.repository
            or access.repository_id != identity.repository_id
            or access.pull_request != identity.pull_request
            or not access.native_exclusive
            or active_epoch != canonical_token.epoch
        ):
            raise ReviewInputError("canonical writer fence scope or epoch conflicts")
        if unsettled:
            raise ReviewInputError(
                "canonical writer epoch has unsettled dispatches; cannot cancel as unstarted"
            )
        if (mode == "cross_run" or state.owner != original_owner) and (
            canonical_token.epoch < 2
        ):
            raise ReviewInputError(
                "cross-owner unstarted cancellation requires a fenced successor epoch"
            )
        selected_mode = "canonical_epoch_fenced"
        proof_payload["canonical_epoch"] = canonical_token.epoch
        proof_payload["canonical_grant_id"] = canonical_token.grant_id

    if selected_mode is None:
        raise ReviewInputError(
            "unstarted action cancellation requires owner cessation or canonical fence proof"
        )
    return selected_mode, _sha256_json(proof_payload)


def _owned_bot_artifact_body(
    item: object,
    *,
    app_author_id: object,
) -> str | None:
    if not isinstance(item, Mapping):
        return None
    user_obj = item.get("user")
    if not isinstance(user_obj, Mapping) or user_obj.get("type") != "Bot":
        return None
    if type(app_author_id) is not int or app_author_id <= 0:
        raise ReviewInputError(
            "native effect fence requires authenticated app author identity"
        )
    if user_obj.get("id") != app_author_id:
        return None
    body = item.get("body")
    return body if isinstance(body, str) else None


def _native_artifact_matches(body: str, *, source_id: int, run_id: int) -> bool:
    """Inspect owned, structured markers, never quoted JSON or numeric prefixes.

    Malformed recognized metadata cannot establish absence of an effect.
    Both legacy source= and the production reply comment= binding are accepted.
    """
    pattern = r"<!--\s*reviewsensei:(command-receipt:v\d+|reply:v\d+|operation:[^\s]+|disposition)\s+([\s\S]*?)-->"
    markers = list(re.finditer(pattern, body))
    recognized = list(
        re.finditer(
            r"<!--\s*reviewsensei:(?:command-receipt:|reply:|operation:|disposition\b)",
            body,
        )
    )
    if len(markers) != len(recognized):
        raise ReviewInputError("owned native artifact marker is malformed")

    def unique_fields(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("duplicate marker field")
            fields[key] = value
        return fields

    for match in markers:
        family, payload = match.groups()
        try:
            if payload.lstrip().startswith("{"):
                fields = json.loads(payload, object_pairs_hook=unique_fields)
                if not isinstance(fields, dict):
                    raise ValueError("marker must be an object")
            else:
                fields = {}
                for token in payload.split():
                    key, value = token.split("=", 1)
                    if not key or not value or key in fields:
                        raise ValueError("invalid marker field")
                    fields[key] = value
            keys = {"source", "source_comment_id", "command_id"}
            if family.startswith("reply:"):
                keys.add("comment")
            keys |= {"run_id"} if family.startswith("operation:") else set()
            if not keys.intersection(fields):
                raise ValueError("marker source identity is unavailable")
            for key in keys:
                if key not in fields:
                    continue
                value = fields[key]
                if type(value) is int:
                    identifier = value
                elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
                    identifier = int(value)
                else:
                    raise ValueError("invalid marker identifier")
                if identifier <= 0:
                    raise ValueError("invalid marker identifier")
                if identifier == (run_id if key == "run_id" else source_id):
                    return True
        except (ValueError, TypeError) as exc:
            raise ReviewInputError("owned native artifact marker is malformed") from exc
    return False


def verify_no_business_dispatch_evidence(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    state: ApiRunAccounting,
    intent: Mapping[str, Any],
    intent_sha256: str,
    evidence: NoBusinessDispatchEvidence | None,
) -> str:
    """Verify positive non-dispatch evidence and live absence of published native effects."""

    if not isinstance(evidence, NoBusinessDispatchEvidence):
        raise ReviewInputError(
            "ordinary unstarted action recovery requires explicit NoBusinessDispatchEvidence; "
            "comment deletion, execution_started=false, null markers, and cancelled jobs alone "
            "are insufficient authority"
        )
    original_owner = ApiExecutionOwner(**intent["owner"])
    if (
        evidence.identity != identity
        or type(evidence.target_run_id) is not int
        or evidence.target_run_id != state.identity.run_id
        or not isinstance(evidence.target_owner, ApiExecutionOwner)
        or evidence.target_owner != original_owner
        or evidence.target_owner != state.owner
        or evidence.intent_sha256 != intent_sha256
    ):
        raise ReviewInputError(
            "no-business-dispatch evidence owner, run, or intent binding conflicts"
        )
    if (
        type(evidence.provider_dispatches) is not int
        or evidence.provider_dispatches != 0
        or type(evidence.accepted_operation_present) is not bool
        or evidence.accepted_operation_present
        or type(evidence.native_effects_checked) is not bool
        or not evidence.native_effects_checked
    ):
        raise ReviewInputError(
            "no-business-dispatch evidence reports provider or accepted operation activity"
        )
    if (
        not isinstance(evidence.observed_requests, tuple)
        or len(evidence.observed_requests) == 0
    ):
        raise ReviewInputError(
            "no-business-dispatch evidence requires non-empty observed_requests; "
            "null or empty telemetry cannot prove non-dispatch"
        )

    ordinary_mutations = 0
    normalized_observations: list[dict[str, Any]] = []
    for item in evidence.observed_requests:
        if not isinstance(item, Mapping):
            raise ReviewInputError(
                "no-business-dispatch request observation is invalid"
            )
        phase = item.get("phase")
        lifecycle_phase = item.get("lifecycle_phase")
        outcome = item.get("outcome")
        mutation = item.get("mutation")
        uncertain_effect = bool(item.get("uncertain_effect", False))
        business_mutation = bool(item.get("business_mutation", False))
        if (
            phase not in {"startup", "ordinary"}
            or lifecycle_phase not in {"startup", "admission"}
            or type(mutation) is not bool
            or not isinstance(item.get("request_id"), str)
            or not str(item["request_id"]).strip()
        ):
            raise ReviewInputError(
                "no-business-dispatch evidence contains post-admission or malformed request phase"
            )
        if outcome != "response" or uncertain_effect:
            raise ReviewInputError(
                "no-business-dispatch evidence contains uncertain or unconfirmed request outcome"
            )
        if business_mutation:
            raise ReviewInputError(
                "no-business-dispatch evidence contains a business mutation dispatch"
            )
        if phase == "ordinary" and mutation:
            ordinary_mutations += 1
        normalized_observations.append(cast(dict[str, Any], json_copy(dict(item))))

    if ordinary_mutations > 2:
        raise ReviewInputError(
            "no-business-dispatch evidence exceeds pre-effect admission mutation count"
        )

    capture_digest = _sha256_json(asdict(evidence))
    issued = _ISSUED_DISPATCH_EVIDENCE.get(id(evidence))
    if issued is None or issued[0]() is not evidence or issued[1] != capture_digest:
        raise ReviewInputError(
            "no-business-dispatch evidence requires an unchanged process-issued transport capture; "
            "hold for exact result recovery"
        )
    assert isinstance(evidence, NoBusinessDispatchEvidence)
    if issued[3]._physical_mutation_ids != issued[4]:
        raise ReviewInputError(
            "no-business-dispatch evidence is stale after a later mutation; hold for exact result recovery"
        )
    observed_mutations = [
        str(item["request_id"]) for item in normalized_observations if item["mutation"]
    ]
    if (
        len(observed_mutations) != len(set(observed_mutations))
        or set(observed_mutations) != issued[4]
    ):
        raise ReviewInputError(
            "no-business-dispatch physical mutation inventory conflicts; hold for exact result recovery"
        )
    receipts = issued[2]
    for observation in normalized_observations:
        if observation["mutation"] and str(observation["request_id"]) not in receipts:
            raise ReviewInputError(
                "no-business-dispatch mutation lacks confirmed owned control-write receipt; "
                "hold for exact result recovery"
            )

    source_id = int(intent["source"]["id"])
    app_author_id = getattr(ledger, "_evidence_author_id", None)
    if type(app_author_id) is not int or app_author_id <= 0:
        raise ReviewInputError(
            "native effect fence requires authenticated app author identity"
        )
    comments_path = ledger.http.repository_path(
        identity.repository, f"/issues/{identity.pull_request}/comments"
    )
    with caller.read_operation("unstarted-action-native-effect-fence"):
        issue_comments = ledger.http.paginate(path=comments_path, token=ledger.token)
    for comment in issue_comments:
        body = _owned_bot_artifact_body(comment, app_author_id=app_author_id)
        if body is None:
            continue
        if _native_artifact_matches(
            body, source_id=source_id, run_id=state.identity.run_id
        ):
            raise ReviewInputError(
                "live GitHub native effect or accepted operation artifact exists for action; "
                "cannot cancel as unstarted"
            )

    if intent["source"]["kind"] == "review_comment":
        review_comments_path = ledger.http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}/comments"
        )
        with caller.read_operation("unstarted-action-native-effect-fence"):
            review_comments = ledger.http.paginate(
                path=review_comments_path, token=ledger.token
            )
        for item in review_comments:
            body = _owned_bot_artifact_body(item, app_author_id=app_author_id)
            if body is None:
                continue
            if _native_artifact_matches(
                body, source_id=source_id, run_id=state.identity.run_id
            ):
                raise ReviewInputError(
                    "live GitHub native effect or accepted operation artifact exists for action; "
                    "cannot cancel as unstarted"
                )

    if intent["purpose"] == "command":
        reviews_path = ledger.http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}/reviews"
        )
        with caller.read_operation("unstarted-action-native-effect-fence"):
            pr_reviews = ledger.http.paginate(path=reviews_path, token=ledger.token)
        for review_item in pr_reviews:
            body = _owned_bot_artifact_body(review_item, app_author_id=app_author_id)
            if body is None:
                continue
            if _native_artifact_matches(
                body, source_id=source_id, run_id=state.identity.run_id
            ):
                raise ReviewInputError(
                    "live GitHub native effect or accepted operation artifact exists for action; "
                    "cannot cancel as unstarted"
                )

    return _sha256_json(
        {
            "target_run_id": evidence.target_run_id,
            "target_owner": {
                "run_attempt": evidence.target_owner.run_attempt,
                "job": evidence.target_owner.job,
                "generation": evidence.target_owner.generation,
            },
            "intent_sha256": evidence.intent_sha256,
            "observation_count": len(normalized_observations),
            "ordinary_mutations": ordinary_mutations,
            "observations_sha256": _sha256_json(normalized_observations),
        }
    )


def authenticate_unstarted_cancellation(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    actions_evidence: ApiRunActionsEvidence | None = None,
    canonical_coordinator: ContainedForwardingCoordinator | None = None,
    canonical_token: IssuedWriterToken | None = None,
    no_business_dispatch_evidence: NoBusinessDispatchEvidence | None = None,
    require_positive_dispatch_proof: bool = True,
    expected_intent: Mapping[str, Any] | None = None,
    mode: str = "same_run",
) -> UnstartedCancellationWitness:
    """Authenticate live source mutation and writer exclusion for an unstarted action."""

    transport_state = caller.observe_action_transport_state(ledger, identity)
    if transport_state.has_unresolved_or_started_work:
        raise ReviewInputError(
            "caller transport has started or unresolved work; cannot cancel as unstarted"
        )
    state, intent, intent_sha256 = _validate_unstarted_record(
        identity=identity,
        record=record,
        expected_intent=expected_intent,
    )
    if mode == "same_run":
        if (
            not transport_state.is_activated
            or transport_state.root_sha256 != record.record_sha256
            or state.identity != caller.run
            or state.owner != transport_state.owner
        ):
            raise ReviewInputError(
                "same-run unstarted cancellation requires the active owning transport"
            )
        if transport_state.has_live_started_accounting:
            raise ReviewInputError("started action cannot be canceled as unstarted")
    elif mode == "cross_run":
        if (
            state.identity == caller.run
            or state.identity.repository_id != caller.run.repository_id
            or caller.attempt != 1
        ):
            raise ReviewInputError(
                "cross-run unstarted cancellation requires a new attempt-1 caller run"
            )
    else:
        raise ReviewInputError("unstarted cancellation mode is unsupported")

    authority_mode, authority_proof_sha256 = _authenticate_authority_proof(
        caller=caller,
        identity=identity,
        state=state,
        intent=intent,
        mode=mode,
        actions_evidence=actions_evidence,
        canonical_coordinator=canonical_coordinator,
        canonical_token=canonical_token,
    )
    dispatch_proof_sha256: str | None = None
    if (
        no_business_dispatch_evidence is not None
        or authority_mode != "canonical_epoch_fenced"
    ):
        dispatch_proof_sha256 = verify_no_business_dispatch_evidence(
            caller=caller,
            ledger=ledger,
            identity=identity,
            state=state,
            intent=intent,
            intent_sha256=intent_sha256,
            evidence=no_business_dispatch_evidence,
        )
    if dispatch_proof_sha256 is not None:
        authority_proof_sha256 = _sha256_json(
            {
                "authority_proof_sha256": authority_proof_sha256,
                "no_business_dispatch_proof_sha256": dispatch_proof_sha256,
            }
        )
    reason, live_observation = _observe_mutated_source(
        caller=caller,
        ledger=ledger,
        identity=identity,
        source=intent["source"],
    )
    producer = ledger.evidence_producer()
    return _issue_witness(
        UnstartedCancellationWitness(
            identity=identity,
            target_run_id=state.identity.run_id,
            target_root_sha256=record.record_sha256,
            intent_sha256=intent_sha256,
            intent=json_copy(intent),
            reason=reason,
            authority_mode=authority_mode,
            authority_proof_sha256=authority_proof_sha256,
            live_source_observation=json_copy(live_observation),
            producer=producer,
        )
    )


def settle_unstarted_accounting(
    *,
    identity: SessionIdentity,
    state: ApiRunAccounting,
    producer: str,
    witness: UnstartedCancellationWitness,
    prior_history: Mapping[str, Any] | None = None,
    has_finding_lifecycle: bool = False,
) -> ApiRunAccounting:
    """Build the terminal ``canceled_unstarted`` accounting value with prior charges intact."""

    verified = require_witness_issued(witness)
    state.require_writer_version()
    action = state.action
    if (
        verified.identity != identity
        or verified.target_run_id != state.identity.run_id
        or verified.producer != producer
        or verified.reason not in UNSTARTED_CANCELLATION_REASONS
        or state.uncertain > 0
        or action is None
        or action["phase"] != "pending"
        or action["execution_started"]
        or action["intent"] != verified.intent
        or _sha256_json(action["intent"]) != verified.intent_sha256
    ):
        raise ReviewInputError("unstarted cancellation witness binding conflicts")

    outcome = {"kind": "canceled_unstarted", "reason": verified.reason}
    carried_history = None if prior_history is None else json_copy(dict(prior_history))
    result_document: dict[str, Any] = {
        "status": "canceled_unstarted",
        "reason": verified.reason,
        "authority_mode": verified.authority_mode,
        "authority_proof_sha256": verified.authority_proof_sha256,
        "target_run_id": verified.target_run_id,
        "superseded_from_root_sha256": verified.target_root_sha256,
        "live_source_observation": json_copy(verified.live_source_observation),
    }
    if carried_history is not None:
        result_document["accepted_semantic_history"] = carried_history
    envelope = {
        "schema_version": "caller-action-result-v1",
        "intent_sha256": verified.intent_sha256,
        "outcome": outcome,
        "result": result_document,
    }
    carrier = encode_action_result_carrier(
        envelope,
        reference_history=has_finding_lifecycle,
    )
    manifest = inline_manifest(
        carrier,
        binding=action_result_binding(identity, state, producer),
        item_count=1,
    )
    if manifest is None:
        raise ReviewInputError(
            "canceled unstarted action result exceeds its inline retention bound"
        )
    if (
        decode_action_result_carrier(
            read_inline_evidence(
                manifest,
                expected_binding=cast(Mapping[str, object], manifest["binding"]),
            ),
            history=carried_history,
        )
        != envelope
    ):
        raise ReviewInputError("canceled unstarted action result codec differs")
    settled = replace(
        state,
        action={
            **action,
            "phase": "terminal",
            "execution_started": False,
            "result": manifest,
            "outcome": outcome,
        },
    )
    state.require_successor(settled)
    return settled


def settle_unstarted_record(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    producer: str,
    witness: UnstartedCancellationWitness,
) -> SessionRecord:
    """Deterministically materialize the settled archival record for cross-run rollover."""

    verified = require_witness_issued(witness)
    if (
        verified.authority_mode not in CROSS_RUN_AUTHORITY_MODES
        or record.identity() != identity
        or record.record_sha256 != verified.target_root_sha256
        or record.reservation_id is not None
        or record.reserved_slot is not None
    ):
        raise ReviewInputError(
            "cross-run unstarted cancellation record binding conflicts"
        )
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    prior_history = (
        record.finding_lifecycle.get("accepted_semantic_history")
        if isinstance(record.finding_lifecycle, Mapping)
        else None
    )
    settled_state = settle_unstarted_accounting(
        identity=identity,
        state=state,
        producer=producer,
        witness=verified,
        prior_history=(prior_history if isinstance(prior_history, Mapping) else None),
        has_finding_lifecycle=record.finding_lifecycle is not None,
    )
    preserved_now = _parse_aware_datetime(record.updated_at, label="updated_at")
    return record.evolve(
        generation=record.generation + 1,
        now=preserved_now,
        api_run_accounting=settled_state.to_document(),
    )
