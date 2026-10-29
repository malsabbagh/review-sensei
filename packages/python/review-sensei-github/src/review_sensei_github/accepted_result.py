"""Original accepted-operation bytes for a compact checkpoint descriptor.

An in-process capture can select the writer's source. A reader instead checks
the complete owned event, its accepted packet/confirmed transition, exact carrier
identities and selected transaction. No serialized reference starts computation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

from .operation_comments import ResultPartStore
from .operation_host import (
    OPERATION_COMMENT_MARKER,
    AcceptedPacket,
    _decode_event,
    comment_payload,
)
from .publication_recovery import _publication_transport

_CAPTURED: WeakKeyDictionary = WeakKeyDictionary()
_VERSION = "accepted-operation-result-reference-v1"
_FIELDS = {
    "schema_version",
    "repository_id",
    "pull_request",
    "base_sha",
    "head_sha",
    "run_id",
    "run_attempt",
    "app_id",
    "producer_id",
    "reservation_id",
    "event_key",
    "event_comment_id",
    "event_body_sha256",
    "payload_sha256",
    "part_ids",
    "policy_digest",
    "configuration_digest",
    "evidence_digest",
}


def _sha(value):
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class CapturedAcceptedResult:
    reference_bytes: bytes
    payload: bytes
    native_format: str

    @property
    def reference(self):
        return json.loads(self.reference_bytes)


def remember_accepted_result(
    http,
    store,
    parts,
    admitted,
    payload,
    *,
    repository_id,
    pull_request,
    base_sha,
    head_sha,
    run_id,
    run_attempt,
    app_id,
    producer_id,
    reservation_id,
    policy_digest,
    configuration_digest,
    evidence_digest,
):
    handle = admitted.operation.handle
    key = handle.scope_digest, handle.event_id
    body = store._event_bodies.get(key)
    identifier = store._event_comment_ids.get(key)
    if not isinstance(body, str) or type(identifier) is not int:
        raise ReviewInputError("accepted result has no confirmed original event")
    if store.embedded_result(key) == payload:
        carriers = ()
    else:
        confirmed = parts._confirmed_read
        if confirmed is None or confirmed[:2] != (_sha(payload), payload):
            raise ReviewInputError("accepted result has no confirmed original parts")
        carriers = confirmed[2]
    reference = {
        "schema_version": _VERSION,
        "repository_id": repository_id,
        "pull_request": pull_request,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "app_id": app_id,
        "producer_id": producer_id,
        "reservation_id": reservation_id,
        "event_key": key[0] + "\t" + key[1],
        "event_comment_id": identifier,
        "event_body_sha256": _sha(body),
        "payload_sha256": _sha(payload),
        "part_ids": list(carriers),
        "policy_digest": policy_digest,
        "configuration_digest": configuration_digest,
        "evidence_digest": evidence_digest,
    }
    from .quarantine_capacity import NativeCapacityGuard
    from .retention_projection import GROUPED_NATIVE_RENDER_FORMAT, NATIVE_RENDER_FORMAT

    transport = _publication_transport(http)
    guard = getattr(transport, "_quarantine_capacity_guard", None)
    protected = (
        type(guard) is NativeCapacityGuard
        and guard.complete_tail
        and guard.identity.repository_id == repository_id
        and guard.identity.pull_request == pull_request
    )
    _CAPTURED[transport] = CapturedAcceptedResult(
        canonical_bytes(reference),
        payload,
        GROUPED_NATIVE_RENDER_FORMAT
        if protected and guard is not None and guard.native_grouped
        else NATIVE_RENDER_FORMAT
        if protected
        else "legacy",
    )


def selected_accepted_result(http, transaction, *, app_id):
    source = _CAPTURED.get(_publication_transport(http))
    if type(source) is not CapturedAcceptedResult:
        raise ReviewInputError(
            "compact checkpoint requires its original accepted operation"
        )
    _reference(source.reference, transaction, app_id=app_id)
    return source


def _reference(value, transaction, *, app_id):
    if (
        not isinstance(value, dict)
        or set(value) != _FIELDS
        or value["schema_version"] != _VERSION
        or type(value["app_id"]) is not int
        or value["app_id"] != app_id
        or type(app_id) is not int
        or app_id <= 0
        or value["pull_request"] != transaction.pull_request
        or value["base_sha"] != transaction.base_sha
        or value["head_sha"] != transaction.head_sha
        or value["reservation_id"] != transaction.reservation_id
        or value["policy_digest"] != transaction.policy_digest
        or value["configuration_digest"] != transaction.configuration_digest
        or value["evidence_digest"] != transaction.evidence_digest
        or any(
            type(value[k]) is not int or not 1 <= value[k] < 2**63
            for k in (
                "repository_id",
                "pull_request",
                "producer_id",
                "event_comment_id",
            )
        )
        or any(
            not isinstance(value[k], str)
            or re.fullmatch(r"[1-9][0-9]{0,18}", value[k]) is None
            for k in ("run_id", "run_attempt")
        )
        or any(
            not isinstance(value[k], str)
            or re.fullmatch(r"[a-f0-9]{64}", value[k]) is None
            for k in ("event_body_sha256", "payload_sha256")
        )
        or not isinstance(value["part_ids"], list)
        or len(value["part_ids"]) > 32
        or any(type(i) is not int or not 1 <= i < 2**63 for i in value["part_ids"])
        or len(set(value["part_ids"])) != len(value["part_ids"])
    ):
        raise ReviewInputError("accepted result original source scope conflicts")
    scope = _sha(f"{value['repository_id']}:{transaction.pull_request}")
    delivery = value["run_id"] + ":" + value["run_attempt"]
    event = _sha("pull_request\n" + delivery)
    if value["event_key"] != scope + "\t" + event:
        raise ReviewInputError("accepted result event identity conflicts")
    return scope, event, delivery


class _ReadSnapshot:
    def __init__(self, comments):
        self.comments = tuple(comments)
        self.requests = 0

    def list_comments(self):
        return self.comments

    def create(self, body):
        raise ReviewInputError("accepted source snapshot is read-only")

    def update(self, comment_id, body):
        raise ReviewInputError("accepted source snapshot is read-only")


def read_accepted_result(reference, transaction, store, *, repository_id, app_id):
    """Fresh complete native data, independently configured Bot/App and scope."""
    scope, event_id, delivery = _reference(reference, transaction, app_id=app_id)
    if (
        reference["repository_id"] != repository_id
        or reference["producer_id"] != store._app_user_id
    ):
        raise ReviewInputError(
            "accepted result repository or numeric producer conflicts"
        )
    comments = tuple(store._port.list_comments())
    matches = []
    for comment in comments:
        if (
            comment.user_id != store._app_user_id
            or comment.user_type != "Bot"
            or OPERATION_COMMENT_MARKER not in comment.body
        ):
            continue
        value = json.loads(comment_payload(comment.body))
        if (
            isinstance(value, dict)
            and value.get("kind") == "event"
            and value.get("key") == reference["event_key"]
        ):
            matches.append((comment, value))
    if len(matches) != 1:
        raise ReviewInputError("accepted result event is missing or duplicated")
    comment, value = matches[0]
    if (
        comment.comment_id != reference["event_comment_id"]
        or _sha(comment.body) != reference["event_body_sha256"]
    ):
        raise ReviewInputError("accepted result original event changed")
    key, event, transitions, _grants, _scope, embedded = _decode_event(value)
    packet = event["accepted"]
    owned = list(transitions.values())
    if (
        key != (scope, event_id)
        or not isinstance(packet, AcceptedPacket)
        or len(owned) != 1
        or not isinstance(owned[0], dict)
        or any(
            event[field] is not expected
            for field, expected in (
                ("provider_charged", True),
                ("provider_unknown", False),
                ("inference_blocked", False),
                ("quarantine", False),
                ("ack_attempted", False),
            )
        )
        or event.get("trigger") != "full-review"
    ):
        raise ReviewInputError("accepted result computation is not confirmed")
    transition = cast(dict[str, Any], owned[0])
    operation = _sha("operation\n" + transaction.reservation_id)
    target = _sha(
        f"computation\n{scope}\n{event_id}\n{transition['prior_root_sha']}\n{transition['prior_root_generation'] + 1}"
    )
    material = "\n".join(
        (
            scope,
            _sha(f"{repository_id}:{transaction.pull_request}:{transaction.head_sha}"),
            _sha(reference["run_id"] + "\n" + reference["run_attempt"])[:32],
            _sha(str(app_id)),
            _sha(transaction.reservation_id),
            event_id,
            operation,
            _sha(f"inventory\n{repository_id}:{transaction.pull_request}"),
            _sha("source\n" + delivery),
            transition["prior_root_sha"],
            str(transition["prior_root_generation"]),
            "full-review",
        )
    )
    if (
        event["binding"] != _sha(material)
        or event["operation_id"] != operation
        or event["root_sha"] != target
        or event["root_generation"] != transition["prior_root_generation"] + 1
        or transition["state"] != "confirmed"
        or transition["request_digest"] != event_id
        or transition["target_root_sha"] != target
        or transition["target_root_generation"] != event["root_generation"]
        or packet.request_digest != event_id
        or packet.payload_digest != reference["payload_sha256"]
        or packet.decisions_digest != reference["payload_sha256"]
    ):
        raise ReviewInputError("accepted result transaction/owner binding conflicts")
    parts = ResultPartStore(_ReadSnapshot(comments), app_user_id=store._app_user_id)
    available = parts.read_available_with_carriers(event_key=reference["event_key"])
    if reference["part_ids"]:
        if (
            available is None
            or available[0] != reference["payload_sha256"]
            or list(available[2]) != reference["part_ids"]
            or embedded is not None
        ):
            raise ReviewInputError(
                "accepted result original parts are incomplete or changed"
            )
        payload = available[1]
    else:
        if available is not None or embedded is None:
            raise ReviewInputError("accepted result embedded representation changed")
        payload = embedded
    if (
        _sha(payload) != packet.payload_digest
        or len(payload) != packet.known_output_bytes
    ):
        raise ReviewInputError("accepted result complete payload conflicts")
    return payload
