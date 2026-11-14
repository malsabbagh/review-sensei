"""Pure v4 grouped-inline ordering data, never a dispatch/replay capability."""

import copy
import json

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import MAX_LIFECYCLE_BYTES

from .finding_lifecycle import (
    _digest,
    _request_identity,
    _require_effect_scope,
    _validate,
)
from .guarded_publication_batch import describe_pair
from .native_review_groups import NativeReviewPlan
from .original_admission_projection import restore_original_admission

VARIANT = "grouped-inline-pair-v1"
FIELDS = frozenset(
    {
        "schema_version",
        "base_schema_version",
        "lifecycle",
        "effects",
        "unknown_trust_effect",
        "native_receipts",
        "accepted_semantic_history",
        "original_admission",
        "grouped_inline_pair",
    }
)


def _descriptor(document, source, identity, plan):
    restored, _ = restore_original_admission(
        document["original_admission"], source, identity=identity
    )
    tx = restored.transaction
    if type(plan) is not NativeReviewPlan or tx is None:
        raise ReviewInputError("grouped inline plan is unavailable")
    rebuilt = NativeReviewPlan.prepare(
        repository_id=plan.repository_id,
        pull_request=plan.pull_request,
        base_sha=plan.base_sha,
        head_sha=plan.head_sha,
        transaction_id=plan.transaction_id,
        result_sha256=plan.result_sha256,
        items=plan.items,
    )
    if (
        rebuilt != plan
        or len(plan.ordinals) != 1
        or not plan.items
        or (
            plan.repository_id,
            plan.pull_request,
            plan.base_sha,
            plan.head_sha,
            plan.transaction_id,
            plan.result_sha256,
        )
        != (
            identity.repository_id,
            identity.pull_request,
            tx.base_sha,
            tx.head_sha,
            tx.transaction_id,
            tx.result_sha256,
        )
    ):
        raise ReviewInputError("grouped inline plan binding changed")
    effects = document["effects"]
    if not isinstance(effects, list) or len(effects) not in (3, 4):
        raise ReviewInputError("grouped inline effect sequence is incomplete")
    # Validate every exact effect and the common lifecycle/receipt envelope using
    # the unchanged old parser. Cross-effect ordering is separately closed below.
    base = {
        key: document[key]
        for key in (
            "lifecycle",
            "unknown_trust_effect",
            "native_receipts",
            "accepted_semantic_history",
        )
    }
    base["schema_version"] = "finding-lifecycle-journal-v2"
    for effect in effects:
        _validate({**base, "effects": [effect]})
    for index, effect in enumerate(effects):
        response = effect["response"]
        if effect["status"] == "rejected":
            if (
                response[0] not in {400, 401, 403, 404, 405, 410}
                or response[1] is not None
            ):
                raise ReviewInputError(
                    "grouped inline rejection is not original compact data"
                )
        elif effect["status"] == "confirmed":
            receipt = response[1]
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"id", "producer_id", "app_id", "mutation"}
                or receipt["mutation"] is not None
                or any(
                    value is not None
                    and (type(value) is not int or not 0 < value <= 2**63 - 1)
                    for value in (
                        receipt["id"],
                        receipt["producer_id"],
                        receipt["app_id"],
                    )
                )
                or (
                    receipt["producer_id"] is not None
                    if index < 2
                    else receipt["app_id"] is not None
                )
            ):
                raise ReviewInputError(
                    "grouped inline receipt is not original compact data"
                )
    _require_effect_scope(document, identity, tx.head_sha)
    if len({e["request_sha256"] for e in effects}) != len(effects):
        raise ReviewInputError("grouped inline intent duplicated")
    part = effects[2]
    if (
        part["method"] != "POST"
        or part["path"]
        != f"/repos/{identity.repository}/pulls/{identity.pull_request}/reviews"
        or canonical_bytes(part["request"])
        != canonical_bytes(_request_identity(plan.part_payload(0)))
    ):
        raise ReviewInputError("grouped inline complete request changed")
    # Reuse ONLY the old check-order predicates on a detached body-only view.
    # The actual inline request above is retained verbatim and never submitted
    # to an old writer/parser as body-only data. Its actual hash stays committed.
    check_view = copy.deepcopy(effects)
    check_view[2]["request"]["comments"] = []
    ordering = describe_pair(check_view)
    for native in effects[2:]:
        if native["status"] == "confirmed":
            response = native["response"]
            if (
                response[0] not in (200, 201)
                or not isinstance(response[1], dict)
                or any(
                    type(response[1].get(key)) is not int
                    or not 0 < response[1][key] <= 2**63 - 1
                    for key in ("id", "producer_id")
                )
            ):
                raise ReviewInputError(
                    "grouped inline native receipt identity is invalid"
                )
    if len(effects) == 4:
        final = effects[3]
        request = final["request"]
        if (
            final["method"] != "POST"
            or final["path"] != part["path"]
            or set(request) != {"commit_id", "event", "body", "comments"}
            or request["commit_id"] != tx.head_sha
            or request["event"] not in {"COMMENT", "REQUEST_CHANGES"}
            or request["comments"] != []
            or not isinstance(request["body"], dict)
            or set(request["body"]) != {"$body_sha256", "utf8_bytes"}
            or request["body"]["utf8_bytes"] > 65536
        ):
            raise ReviewInputError("grouped inline final intent changed")
    return {
        "schema_version": VARIANT,
        "transaction_id": tx.transaction_id,
        "result_sha256": tx.result_sha256,
        "plan_sha256": plan.sha256,
        "part_ordinal": 0,
        "original_admission_sha256": _digest(document["original_admission"]),
        "ordering": ordering,
        "part_request": copy.deepcopy(part["request"]),
    }


def validate_grouped_inline_journal(document, source, *, identity, plan):
    if (
        not isinstance(document, dict)
        or set(document) != FIELDS
        or document.get("schema_version") != "finding-lifecycle-journal-v4"
        or document.get("base_schema_version") != VARIANT
    ):
        raise ReviewInputError("grouped inline journal variant is unsupported")
    if len(canonical_bytes(document)) > MAX_LIFECYCLE_BYTES:
        raise ReviewInputError("grouped inline journal total byte capacity exceeded")
    expected = _descriptor(document, source, identity, plan)
    if canonical_bytes(document["grouped_inline_pair"]) != canonical_bytes(expected):
        raise ReviewInputError("grouped inline descriptor changed")
    return json.loads(canonical_bytes(document))


def grouped_inline_journal(base, projection, source, *, identity, plan):
    if not isinstance(base, dict) or set(base) != FIELDS - {
        "base_schema_version",
        "original_admission",
        "grouped_inline_pair",
    }:
        raise ReviewInputError("grouped inline base shape changed")
    if base["schema_version"] != "finding-lifecycle-journal-v2":
        raise ReviewInputError("grouped inline base version changed")
    document = {
        **base,
        "schema_version": "finding-lifecycle-journal-v4",
        "base_schema_version": VARIANT,
        "original_admission": projection,
    }
    document["grouped_inline_pair"] = _descriptor(document, source, identity, plan)
    return validate_grouped_inline_journal(
        document, source, identity=identity, plan=plan
    )


def cold_grouped_status(document, source, *, identity, plan):
    value = validate_grouped_inline_journal(
        document, source, identity=identity, plan=plan
    )
    return {
        "mode": "read-only",
        "unconfirmed": tuple(
            i
            for i, effect in enumerate(value["effects"])
            if effect["status"] != "confirmed"
        ),
    }


def require_grouped_inline_transition(before, after, source, *, identity, plan):
    """Pure monotone receipt/pending-intent relation, not source/CAS authority."""
    old = validate_grouped_inline_journal(before, source, identity=identity, plan=plan)
    new = validate_grouped_inline_journal(after, source, identity=identity, plan=plan)
    if (
        canonical_bytes(old["grouped_inline_pair"])
        != canonical_bytes(new["grouped_inline_pair"])
        or canonical_bytes(old["original_admission"])
        != canonical_bytes(new["original_admission"])
        or len(new["effects"]) < len(old["effects"])
        or old["unknown_trust_effect"]
        and not new["unknown_trust_effect"]
    ):
        raise ReviewInputError("grouped inline transition drops retained liabilities")
    for prior, current in zip(old["effects"], new["effects"]):
        if (
            any(
                canonical_bytes(prior.get(k)) != canonical_bytes(current.get(k))
                for k in (prior.keys() | current.keys()) - {"status", "response"}
            )
            or prior["status"] != "pending"
            and canonical_bytes(prior) != canonical_bytes(current)
        ):
            raise ReviewInputError("grouped inline transition changes retained intent")
