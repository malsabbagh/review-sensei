"""Exact, source-issued guarded publication batch; never cold dispatch authority."""

from __future__ import annotations

import json
import re
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

# Only this module advances execution. No durable field asserts non-dispatch.
_EXECUTIONS: WeakKeyDictionary = WeakKeyDictionary()


def _shape(method, path, body):
    return canonical_bytes(
        [method, re.sub(r"/check-runs/[1-9][0-9]*$", "/check-runs/{id}", path), body]
    )


def _effect(method, path, body):
    from .finding_lifecycle import _digest, _request_identity

    request = _request_identity(body)
    return {
        "method": method,
        "path": path,
        "request": request,
        "request_sha256": _digest({"method": method, "path": path, "request": request}),
        "status": "pending",
        "response": None,
    }


def intercept_request(
    journal, http, method, path, *, token, body, kwargs, prepare_only=False
):
    """Return (handled, response); only a real planner can issue the context.

    First check uses the ordinary journal path. Completion and review intents
    share one checkpoint, which preserves the confirmed first-check receipt.
    An undispatched review remains pending after any stop or process loss.
    """
    from .finding_lifecycle import MAX_EFFECTS, _pack_journal, _require_effect_scope
    from .guarded_pending_publication import batch_context

    context = batch_context(journal)
    retained = _EXECUTIONS.get(journal)
    if context is None:
        if getattr(journal, "_guarded_publication_pair", None) is not None and any(
            effect["status"] != "confirmed" for effect in journal.effects[1:3]
        ):
            raise ReviewInputError("cold guarded publication pair cannot dispatch")
        if retained is not None:
            raise ReviewInputError("guarded publication batch custody disappeared")
        return False, None
    if prepare_only:
        raise ReviewInputError(
            "guarded publication batch cannot prepare separate effects"
        )
    payload_bytes = context.payload
    payload = json.loads(payload_bytes)
    if canonical_bytes(payload) != payload_bytes:
        raise ReviewInputError("guarded publication batch payload is not canonical")
    starts = payload.get("start_requests")
    completes = payload.get("complete_requests")
    review = payload.get("review_request")
    tail = payload.get("checkpoint_tail_calls")
    wire_bound = payload.get("future_wire_bound")
    if (
        not isinstance(starts, list)
        or not starts
        or not isinstance(completes, list)
        or not completes
        or not isinstance(review, list)
        or len(review) != 3
        or type(tail) is not int
        or tail < 2
        or type(wire_bound) is not int
        or wire_bound <= 0
    ):
        raise ReviewInputError("guarded publication batch plan is incomplete")
    for value in (*starts, *completes, review):
        if not isinstance(value, list) or len(value) != 3:
            raise ReviewInputError("guarded publication batch request is invalid")
    review_method, review_path, review_body = review
    if (
        review_method != "POST"
        or review_path
        != f"/repos/{journal.identity.repository}/pulls/{journal.identity.pull_request}/reviews"
        or not isinstance(review_body, dict)
        or set(review_body) != {"commit_id", "event", "body", "comments"}
        or review_body["commit_id"] != journal.head_sha
        or review_body["event"] not in {"COMMENT", "REQUEST_CHANGES"}
        or not isinstance(review_body["body"], str)
        or review_body["comments"] != []
    ):
        raise ReviewInputError("guarded publication batch review scope changed")
    shape = _shape(method, path, body)
    if retained is None:
        if journal.effects or shape not in {_shape(*value) for value in starts}:
            raise ReviewInputError("guarded publication batch start changed")
        if method not in {"POST", "PATCH"} or "/check-runs" not in path:
            raise ReviewInputError("guarded publication batch start is not a check")
        if method == "PATCH" and journal._check_precondition(path) is None:
            raise ReviewInputError(
                "guarded publication initial check lookup is missing"
            )
        _EXECUTIONS[journal] = {
            "context": context,
            "payload": payload_bytes,
            "phase": "start",
            "start": canonical_bytes(_effect(method, path, body)),
        }
        return False, None
    if retained["context"] is not context or retained["payload"] != payload_bytes:
        raise ReviewInputError("guarded publication batch context changed")
    if retained["phase"] == "start":
        # Failure/rejection/unknown of the first check never activates the batch.
        if len(journal.effects) != 1:
            raise ReviewInputError("guarded publication first check is not confirmed")
        first = journal.effects[0]
        expected = json.loads(retained["start"])
        if (
            first["status"] != "confirmed"
            or any(
                first[field] != expected[field]
                for field in expected
                if field not in {"status", "response"}
            )
            or shape not in {_shape(*value) for value in completes}
        ):
            retained["phase"] = "stopped"
            raise ReviewInputError("guarded publication first check is not confirmed")
        response = first["response"]
        identifier = (
            response[1].get("id")
            if isinstance(response, list)
            and len(response) == 2
            and isinstance(response[1], dict)
            else None
        )
        if (
            type(identifier) is not int
            or identifier <= 0
            or method != "PATCH"
            or path != f"/repos/{journal.identity.repository}/check-runs/{identifier}"
        ):
            retained["phase"] = "stopped"
            raise ReviewInputError(
                "guarded publication completed check identity changed"
            )
        precondition = journal._check_precondition(path)
        if precondition is None or precondition["check_run_id"] != identifier:
            retained["phase"] = "stopped"
            raise ReviewInputError(
                "guarded publication completed check lookup is missing"
            )
        complete = _effect(method, path, body)
        complete["check_update_precondition"] = precondition
        next_review = _effect(*review)
        if len(journal.effects) + 2 > MAX_EFFECTS:
            raise ReviewInputError("guarded publication batch exceeds effect capacity")
        # The planner priced this exact shape before the first business effect.
        # Do not invoke per-effect preflight, which cannot model two intents.
        journal.effects.extend((complete, next_review))
        retained["phase"] = "stopped"
        old_pair = getattr(journal, "_guarded_publication_pair", None)
        try:
            journal._guarded_publication_pair = describe_pair(journal.effects)
            document = journal._document()
            _require_effect_scope(document, journal.identity, journal.head_sha)
            if len(canonical_bytes(_pack_journal(document))) > wire_bound:
                raise ReviewInputError(
                    "guarded publication batch exceeds funded wire bound"
                )
        except BaseException:
            # Pure validation has not attempted persistence or either native
            # send. Once checkpoint begins, retain both intents on any error.
            del journal.effects[-2:]
            journal._guarded_publication_pair = old_pair
            raise
        journal._future_wire_bound = wire_bound
        journal.checkpoint(reserved_tail_calls=tail, pending_intent=True)
        retained["root"] = journal._record.record_sha256
        retained["review"] = next_review
        retained["complete"] = complete
        result = journal._dispatch_effect(
            http, complete, method, path, token=token, body=body, **kwargs
        )
        if complete["status"] == "confirmed":
            retained["phase"] = "review"
            retained["effects"] = canonical_bytes(journal.effects)
        return True, result
    if retained["phase"] == "review":
        if (
            canonical_bytes([method, path, body]) != canonical_bytes(review)
            or journal._record.record_sha256 != retained["root"]
            or canonical_bytes(journal.effects) != retained["effects"]
            or journal.effects[-1] is not retained["review"]
            or retained["review"]["status"] != "pending"
            or journal.effects[-2] is not retained["complete"]
            or retained["complete"]["status"] != "confirmed"
        ):
            retained["phase"] = "stopped"
            raise ReviewInputError("guarded publication precommitted review changed")
        retained["phase"] = "stopped"
        result = journal._dispatch_effect(
            http, retained["review"], method, path, token=token, body=body, **kwargs
        )
        if retained["review"]["status"] == "confirmed":
            retained["phase"] = "done"
        return True, result
    raise ReviewInputError(
        "guarded publication batch stopped; later native dispatch refused"
    )


BATCH_VERSION = "finding-lifecycle-journal-v3"


def describe_pair(effects, complete_index=1, review_index=2):
    """Data-only closed pair descriptor. This never issues dispatch authority."""
    from .finding_lifecycle import _digest

    def fail():
        raise ReviewInputError("guarded publication durable pair is invalid")

    if (
        type(complete_index) is not int
        or type(review_index) is not int
        or complete_index != 1
        or review_index != 2
        or len(effects) < 3
    ):
        fail()
    first, complete, review = effects[:3]
    if not all(isinstance(e, dict) for e in (first, complete, review)):
        fail()
    receipt = first.get("response")
    if (
        first.get("status") != "confirmed"
        or not isinstance(receipt, list)
        or len(receipt) != 2
        or receipt[0] not in (200, 201)
        or not isinstance(receipt[1], dict)
    ):
        fail()
    identifier, app_id = receipt[1].get("id"), receipt[1].get("app_id")
    if any(
        type(v) is not int or not 0 < v <= 9_223_372_036_854_775_807
        for v in (identifier, app_id)
    ):
        fail()
    precondition = complete.get("check_update_precondition")
    if not isinstance(precondition, dict):
        fail()
    head = precondition.get("head_sha")
    slug = precondition.get("app_slug")
    if (
        set(precondition) != {"check_run_id", "head_sha", "app_id", "app_slug"}
        or precondition.get("check_run_id") != identifier
        or precondition.get("app_id") != app_id
        or not isinstance(head, str)
        or re.fullmatch(r"[a-f0-9]{40}", head) is None
        or not isinstance(slug, str)
        or re.fullmatch(r"[a-z0-9-]{1,100}", slug) is None
    ):
        fail()
    match = re.fullmatch(
        r"(/repos/[^/]+/[^/]+)/check-runs/[1-9][0-9]*", complete.get("path", "")
    )
    if match is None:
        fail()
    repo = match[1]
    start_request, complete_request, review_request = (
        e.get("request") for e in (first, complete, review)
    )
    if not all(
        isinstance(r, dict) for r in (start_request, complete_request, review_request)
    ):
        fail()
    if (
        complete.get("method") != "PATCH"
        or complete["path"] != f"{repo}/check-runs/{identifier}"
        or complete_request.get("status") != "completed"
        or start_request.get("status") != "in_progress"
        or set(complete_request) != {"status", "output", "conclusion"}
        or first.get("method") not in {"POST", "PATCH"}
    ):
        fail()
    if first["method"] == "POST":
        if (
            first.get("path") != f"{repo}/check-runs"
            or start_request.get("head_sha") != head
            or start_request.get("name") != "ReviewSensei"
            or set(start_request)
            != {"status", "output", "name", "head_sha", "external_id"}
        ):
            fail()
    elif (
        set(start_request) != {"status", "output"}
        or first.get("path") != complete["path"]
        or first.get("check_update_precondition") != precondition
    ):
        fail()
    if (
        review.get("method") != "POST"
        or re.fullmatch(
            re.escape(repo) + r"/pulls/[1-9][0-9]*/reviews", review.get("path", "")
        )
        is None
        or set(review_request) != {"commit_id", "event", "body", "comments"}
        or review_request["commit_id"] != head
        or review_request["event"] not in {"COMMENT", "REQUEST_CHANGES"}
        or review_request["comments"] != []
        or not isinstance(review_request["body"], dict)
        or set(review_request["body"]) != {"$body_sha256", "utf8_bytes"}
    ):
        fail()
    if complete.get("status") != "confirmed" and review.get("status") != "pending":
        fail()
    if complete.get("status") == "confirmed":
        confirmed = complete.get("response")
        if (
            not isinstance(confirmed, list)
            or len(confirmed) != 2
            or confirmed[0] != 200
            or not isinstance(confirmed[1], dict)
            or confirmed[1].get("id") != identifier
            or confirmed[1].get("app_id") != app_id
        ):
            fail()
    if review.get("status") == "confirmed":
        confirmed = review.get("response")
        if (
            not isinstance(confirmed, list)
            or len(confirmed) != 2
            or confirmed[0] not in (200, 201)
            or not isinstance(confirmed[1], dict)
            or any(
                type(confirmed[1].get(k)) is not int or confirmed[1][k] <= 0
                for k in ("id", "producer_id")
            )
        ):
            fail()
    if len(effects) > 3 and not all(
        e.get("status") == "confirmed" for e in (complete, review)
    ):
        fail()
    return {
        "schema_version": "guarded-publication-pair-v1",
        "start_index": 0,
        "complete_index": 1,
        "review_index": 2,
        "start_request_sha256": first["request_sha256"],
        "start_receipt_sha256": _digest(receipt),
        "complete_request_sha256": complete["request_sha256"],
        "review_request_sha256": review["request_sha256"],
    }


def validate_pair(effects, descriptor):
    if canonical_bytes(descriptor) != canonical_bytes(describe_pair(effects)):
        raise ReviewInputError("guarded publication durable descriptor changed")


def require_cold_pair_order(journal, index):
    """A rejected/unknown completed check cannot license review adoption."""
    if getattr(journal, "_guarded_publication_pair", None) is not None and index >= 2:
        if journal.effects[1]["status"] != "confirmed":
            raise ReviewInputError(
                "guarded publication completed check remains unconfirmed"
            )


def validate_review_receipt(journal, effect, receipt):
    """Require the ledger's exact numeric Bot owner before adopting pair review.

    The durable descriptor is an ordering commitment, not an ownership proof.
    This check applies equally to a hot response and a cold native readback.
    """
    descriptor = getattr(journal, "_guarded_publication_pair", None)
    if descriptor is None:
        return
    if len(journal.effects) < 3:
        raise ReviewInputError("guarded publication pair receipt scope is invalid")
    review = journal.effects[2]
    if effect is not review:
        return
    if review.get("request_sha256") != descriptor.get("review_request_sha256"):
        raise ReviewInputError("guarded publication review intent changed")
    producer = journal.ledger.evidence_producer()
    matched = (
        re.fullmatch(r"github-bot:([1-9][0-9]*)", producer)
        if isinstance(producer, str)
        else None
    )
    if (
        matched is None
        or not isinstance(receipt, dict)
        or type(receipt.get("producer_id")) is not int
        or receipt["producer_id"] != int(matched[1])
    ):
        raise ReviewInputError(
            "guarded publication review numeric producer is unconfirmed"
        )
