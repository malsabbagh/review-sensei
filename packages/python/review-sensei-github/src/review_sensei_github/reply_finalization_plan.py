"""Source-owned reply capacity observations, never resolution authority."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import encode_lifecycle_state

_ISSUED: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_APPROVED: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_DOCUMENTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_REUSE: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_RESTORE_QUOTE: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_TRANSPORTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_OWNERS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_EXECUTING: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_SOURCE: dict[object, Any] = {}


def source_reply_capacity(method):
    from functools import wraps

    @wraps(method)
    def wrapped(publisher, *args, **kwargs):
        import inspect

        from .human_assessment import HumanAssessmentPublisher

        if inspect.unwrap(HumanAssessmentPublisher.publish) is not method:
            raise ReviewInputError("reply capacity issuer is unsupported")
        if "_capacity_source" in kwargs:
            raise ReviewInputError("reply capacity source cannot be supplied")
        source = object()
        _SOURCE[source] = publisher
        try:
            return method(publisher, *args, _capacity_source=source, **kwargs)
        finally:
            _SOURCE.pop(source, None)

    return wrapped


@dataclass(frozen=True, eq=False)
class ReplyFinalizationPlan:
    journal: Any
    record: Any
    root: bytes
    http: Any
    budget: Any
    payload: bytes
    states: tuple[Any, ...]
    state_bytes: bytes

    def _signature(self):
        return (
            id(self.journal),
            id(self.record),
            self.root,
            id(self.http),
            id(self.budget),
            self.payload,
            self.state_bytes,
        )

    def require(self, journal, *, http, budget):
        if (
            self._signature() != _ISSUED.get(self)
            or journal is not self.journal
            or journal._record is not self.record
            or canonical_bytes(self.record.to_dict()) != self.root
            or http is not self.http
            or budget is not self.budget
            or canonical_bytes([encode_lifecycle_state(s) for s in self.states])
            != self.state_bytes
            or _source_http(self.http, self.budget) is not _TRANSPORTS.get(self)
        ):
            raise ReviewInputError("reply finalization capacity custody changed")

    def bind_capacity(self, *, pending_documents):
        self.require(self.journal, http=self.http, budget=self.budget)
        if self in _APPROVED:
            raise ReviewInputError("reply finalization capacity was already admitted")
        if self not in _RESTORE_QUOTE:
            raise ReviewInputError("reply restore reservation is absent")
        reuse = _RESTORE_QUOTE[self]
        if reuse and not self.can_reuse_retained_lifecycle():
            raise ReviewInputError("reply retained custody changed after pricing")
        _APPROVED[self] = tuple(sorted(pending_documents.items()))
        _REUSE[self] = reuse

    def price_restore_calls(self):
        self.require(self.journal, http=self.http, budget=self.budget)
        if self in _RESTORE_QUOTE:
            raise ReviewInputError("reply restore reservation was already quoted")
        reuse = self.can_reuse_retained_lifecycle()
        _RESTORE_QUOTE[self] = reuse
        if reuse:
            return 0
        reference = self.record.finding_lifecycle["manifest"]
        return (
            10
            if reference["encoding"] == "inline-json-v1"
            else 20 + len(reference["parts"])
        )

    def can_reuse_retained_lifecycle(self):
        self.require(self.journal, http=self.http, budget=self.budget)
        caller = getattr(self.journal.ledger, "caller", None)
        owned = getattr(caller, "_effect_record", None)
        if owned is None or self.record is None:
            return False
        return (
            self.record is owned
            and self.record.record_sha256 == getattr(caller, "_root", None)
            and canonical_bytes(self.record.to_dict()) == self.root
            and self.journal._record is self.record
        )

    def reuses_retained_lifecycle(self):
        if self not in _APPROVED:
            raise ReviewInputError("reply capacity admission is absent")
        return _REUSE[self]

    def reuse_retained_lifecycle(self, consumer):
        owners = _OWNERS.get(self)
        if owners is None or owners[1] is not consumer:
            raise ReviewInputError("reply retained lifecycle consumer changed")
        _require_owner_identity(self, owners[0])
        if (
            self not in _APPROVED
            or not self.can_reuse_retained_lifecycle()
            or consumer.journal is not self.journal
            or consumer.record is not self.record
            or consumer.ledger is not self.journal.ledger
            or consumer.identity != self.journal.identity
            or consumer.head_sha != self.journal.head_sha
            or canonical_bytes(self.journal._document()) != _DOCUMENTS.get(self)
        ):
            raise ReviewInputError("reply retained lifecycle custody changed")
        return self.journal

    def transfer(self, consumer, journal):
        owners = _OWNERS.get(self)
        if owners is None or owners[1] is not consumer:
            raise ReviewInputError("reply finalization consumer changed")
        _require_owner_identity(self, owners[0])
        self.require(self.journal, http=self.http, budget=self.budget)
        payload = json.loads(self.payload)
        if (
            self not in _APPROVED
            or self in _EXECUTING.values()
            or consumer.ledger is not self.journal.ledger
            or journal.identity != self.journal.identity
            or journal.operation_id != self.journal.operation_id
            or canonical_bytes(journal._record.to_dict()) != self.root
            or journal.head_sha != payload["head_sha"]
            or canonical_bytes(journal._document())
            != canonical_bytes(self.journal._document())
        ):
            raise ReviewInputError("reply finalization restore custody changed")
        journal._finalizer_capacity_states = self.states
        _EXECUTING[journal] = self


def executing_plan(journal):
    try:
        plan = _EXECUTING.get(journal)
    except TypeError:
        return None  # Legacy/non-journal callers have no reply sizing scope.
    if plan is None:
        return None
    if (
        plan._signature() != _ISSUED.get(plan)
        or canonical_bytes([encode_lifecycle_state(s) for s in plan.states])
        != plan.state_bytes
        or journal.ledger is not plan.journal.ledger
        or journal.identity != plan.journal.identity
        or journal.operation_id != plan.journal.operation_id
        or journal.head_sha != plan.journal.head_sha
    ):
        raise ReviewInputError("reply finalization execution custody changed")
    return plan


def _source_http(http, budget):
    from .command_finalization_plan import _underlying_http
    from .reply_budget import ReplyPublicationHttp

    if (
        type(http) is not ReplyPublicationHttp
        or getattr(http.request, "__func__", None) is not ReplyPublicationHttp.request
    ):
        raise ReviewInputError("reply capacity transport source is unsupported")
    return _underlying_http(http.transport, budget=budget)


def _require_source_methods(owner, cls):
    if type(owner) is not cls:
        raise ReviewInputError("reply capacity source class is unsupported")
    for name in vars(cls):
        if name.startswith("__"):
            continue
        expected = getattr(cls, name)
        if callable(expected):
            actual = getattr(owner, name)
            if getattr(actual, "__func__", actual) is not getattr(
                expected, "__func__", expected
            ):
                raise ReviewInputError(
                    "reply capacity source method is unsupported: " + name
                )


def _require_owner_identity(plan, publisher):
    from .conversation import ConversationPublisher
    from .human_assessment import HumanAssessmentPublisher
    from .publication import ReviewApprovalFinalizer

    actual = (
        publisher,
        getattr(publisher.finalizer, "_finding_lifecycle_consumer", None),
        publisher.conversation,
        publisher.finalizer,
        publisher.conversation.finalizer,
    )
    expected = _OWNERS.get(plan)
    if expected is None or any(
        a is not b for a, b in zip(actual, expected, strict=True)
    ):
        raise ReviewInputError("reply capacity execution owners changed")
    _require_source_methods(publisher, HumanAssessmentPublisher)
    _require_source_methods(publisher.conversation, ConversationPublisher)
    for finalizer in (publisher.finalizer, publisher.conversation.finalizer):
        _require_source_methods(finalizer, ReviewApprovalFinalizer)
        if (
            finalizer.evidence_budget is not plan.budget
            or finalizer._operation_observer is not None
            or finalizer._thread_cleanup_token_provider is not None
        ):
            raise ReviewInputError(
                "reply capacity execution callback or budget changed"
            )


def require_closed_reply_check_tail(reviews_policy, eligibility, index):
    """Negative-only sizing gate; an index projection grants no authority."""
    if not (reviews_policy == "auto-approve" and eligibility.facts.enabled) and any(
        entry.blocking and not entry.body_placed for entry in index.entries
    ):
        raise ReviewInputError("reply_capacity_inline_blocking_tail_unqualified")


def prepare_reply_capacity(
    publisher,
    *,
    prepared,
    refreshed,
    body,
    reply,
    token,
    check_token,
    repository,
    pull_request,
    app_slug,
    reviews_policy,
    layout,
    source,
):
    """Observe a closed ordinary source path before the first visible reply."""
    from .checks import ReviewCheckPublisher, review_check_outcome
    from .command_finalization_plan import ClosedCommandReads
    from .conversation import apply_authenticated_finding_resolution
    from .finding_lifecycle_consumers import project_confirmed_slot_release
    from .publication import ReviewApprovalFinalizer, _approval_body, approval_marker
    from .thread_resolution import FindingThreadResolver

    if source not in _SOURCE or _SOURCE[source] is not publisher:
        raise ReviewInputError("reply capacity source is unissued")
    consumer = getattr(publisher.finalizer, "_finding_lifecycle_consumer", None)
    inventory = refreshed.human_review
    if consumer is None or not consumer.enrolled or inventory is None:
        return None
    from .api_run_requests import ApiRunSessionLedger
    from .review_run import ActionRunLedger

    if type(consumer.ledger) not in {ApiRunSessionLedger, ActionRunLedger}:
        return None
    original = prepared.eligibility
    # Only the new multiple-target protocol is selected here. Other existing
    # paths retain their established readers and conservative guards.
    newly_resolved = set(inventory.resolved) - set(original.human_review.resolved)
    if len(newly_resolved) < 2:
        return None
    if (
        prepared.feedback is not None
        or refreshed.partitioned_root is not None
        or refreshed.native_union is not None
        or refreshed.human_inventory_reference is not None
        or body is None
        or layout != "legacy"
        or prepared.conversation.source_kind != "issue"
    ):
        return None
    from .conversation import ConversationPublisher
    from .human_assessment import HumanAssessmentPublisher

    if (
        type(publisher) is not HumanAssessmentPublisher
        or type(publisher.conversation) is not ConversationPublisher
        or any(
            getattr(getattr(publisher, name), "__func__", getattr(publisher, name))
            is not getattr(HumanAssessmentPublisher, name)
            for name in (
                "source_fence",
                "_source",
                "source_matches",
                "_source_metadata_digest",
                "_finalize_lifecycle_decisions",
                "_completed_reply",
            )
        )
        or any(
            getattr(
                getattr(publisher.conversation, name),
                "__func__",
                getattr(publisher.conversation, name),
            )
            is not getattr(ConversationPublisher, name)
            for name in (
                "publish_pending",
                "publish",
                "_publish",
                "_find_existing",
                "_completed_pending_report",
                "_pending_report_parts",
                "_require_comment_association",
                "_can_record_resolution",
            )
        )
    ):
        raise ReviewInputError("reply capacity source methods are unsupported")
    _require_source_methods(publisher, HumanAssessmentPublisher)
    _require_source_methods(publisher.conversation, ConversationPublisher)
    _require_source_methods(publisher.finalizer, ReviewApprovalFinalizer)
    _require_source_methods(publisher.conversation.finalizer, ReviewApprovalFinalizer)
    if any(
        getattr(owner, name) is not None
        for owner in (publisher.finalizer, publisher.conversation.finalizer)
        for name in ("_thread_cleanup_token_provider", "_operation_observer")
    ):
        raise ReviewInputError("reply capacity callback source is unsupported")
    journal = consumer.journal
    if any(
        getattr(owner, "_finding_lifecycle_journal", None) is not None
        and getattr(owner, "_finding_lifecycle_journal") is not journal
        for owner in (publisher.finalizer, publisher.conversation.finalizer)
    ):
        raise ReviewInputError("reply capacity existing journal scope conflicts")
    action = journal.ledger.caller.accounting.action
    expected_source = {
        "kind": "issue_comment",
        "id": prepared.conversation.source_comment_id,
        "updated_at": prepared.conversation.source_updated_at,
        "body_sha256": hashlib.sha256(prepared.source_body.encode()).hexdigest(),
    }
    if (
        action is None
        or action["phase"] != "pending"
        or action["intent"]["purpose"] != "reply"
        or action["intent"]["source"] != expected_source
    ):
        raise ReviewInputError("reply capacity admitted source changed")
    budget = publisher.finalizer.evidence_budget
    _source_http(publisher.http, budget)
    operation = copy.copy(publisher.finalizer)
    setattr(operation, "_finding_lifecycle_journal", journal)
    ReviewApprovalFinalizer._require_closed_capacity_source(operation)
    operation.http = ClosedCommandReads(publisher.http)
    operation._operation_observer = None
    for name in (
        "_active_window_states",
        "_drift_states",
        "_artifact_decisions",
        "_response_loss_holds",
        "_trust_snapshots",
        "_trust_decisions",
        "_unknown_effect_holds",
    ):
        setattr(operation, name, copy.deepcopy(getattr(operation, name)))
    head = prepared.conversation.head_sha
    live = operation._preflight(
        token=token,
        repository=repository,
        pull_request=pull_request,
        head_sha=head,
        app_slug=app_slug,
    )
    if (
        live.result is not None
        or live.app_authored
        or live.base_sha != inventory.base_sha
    ):
        raise ReviewInputError("reply capacity live source changed")
    assert live.repository_id is not None and live.base_sha is not None
    reviews = operation._load_head_reviews(
        token=token, repository=repository, pull_request=pull_request
    )
    if (
        operation._eligibility_from_reviews(
            reviews,
            head_sha=head,
            app_slug=app_slug,
            token=token,
            repository=repository,
            pull_request=pull_request,
        )
        != original
    ):
        raise ReviewInputError("reply capacity original authority changed")
    index = operation.finding_index_from_reviews(
        reviews,
        repository_id=live.repository_id,
        pull_request=pull_request,
        head_sha=head,
        app_slug=app_slug,
        eligibility=original,
    )
    records, instances = operation._decided_cleanup_inputs(
        eligibility=refreshed,
        index=index,
        reviews=reviews,
        app_slug=app_slug,
        repository_id=live.repository_id,
        pull_request=pull_request,
        head_sha=head,
        honored_instances=frozenset(),
    )
    if records or index is None:
        raise ReviewInputError("reply capacity legacy cleanup source is unsupported")
    require_closed_reply_check_tail(reviews_policy, refreshed, index)
    resolver = FindingThreadResolver(operation.http)
    discovery = resolver._discover_candidates(
        token=token,
        repository=repository,
        repository_id=live.repository_id,
        pull_request=pull_request,
        head_sha=head,
        base_sha=live.base_sha,
        app_slug=app_slug,
        instances=instances,
    )
    receipts = {
        r["instance"]: r
        for r in journal.reviewer.native_finding_receipts(
            repository_id=live.repository_id, pull_request=pull_request
        )
    }
    for node, root, instance, legacy in discovery:
        receipt = receipts.get(instance)
        if (
            legacy
            or receipt is None
            or receipt["thread_id"] != node["id"]
            or receipt["comment_id"] != root["databaseId"]
            or hashlib.sha256(root["body"].encode()).hexdigest()
            != receipt["body_sha256"]
        ):
            raise ReviewInputError("reply cleanup retained criterion binding changed")
    # Reuse only these source-discovered native nodes for the pure cost walk.
    # No reader callback or extra discovery request is introduced.
    requests, cleanup_calls = resolver.plan_capacity(
        token=token,
        repository=repository,
        repository_id=live.repository_id,
        pull_request=pull_request,
        head_sha=head,
        base_sha=live.base_sha,
        app_slug=app_slug,
        instances=instances,
        batch_fence_calls=2,
        cleanup_capability_required=True,
        root_reader=lambda: tuple(item[0] for item in discovery),
    )
    key = (live.repository_id, pull_request)
    initial = operation._drift_states.get(key) or operation._active_window_states.get(
        key
    )
    apply_authenticated_finding_resolution(
        operation,
        repository_id=live.repository_id,
        pull_request=pull_request,
        head_sha=head,
        instances=inventory.resolved,
        evidence_sha256=hashlib.sha256(
            canonical_bytes(inventory.to_dict())
        ).hexdigest(),
    )
    projected = operation._drift_states.get(key) or operation._active_window_states.get(
        key
    )
    receipts = {
        r["instance"]: r
        for r in journal.reviewer.native_finding_receipts(
            repository_id=live.repository_id, pull_request=pull_request
        )
    }
    ordered = []
    for _, _, request in requests:
        target = request["variables"]["input"]["threadId"]
        matches = [
            i for i in instances if receipts.get(i, {}).get("thread_id") == target
        ]
        if len(matches) != 1:
            raise ReviewInputError("reply cleanup native binding is ambiguous")
        ordered.append(matches[0])
    preconfirmed = frozenset(instances) - frozenset(ordered)
    prefixes = [preconfirmed | frozenset(ordered[:n]) for n in range(len(ordered) + 1)]
    state_candidates = [initial, projected]
    for prefix in prefixes:
        for selected in (prefix, prefix - preconfirmed):
            state_candidates.append(
                project_confirmed_slot_release(
                    projected,
                    key=key,
                    receipts=receipts,
                    instances=selected,
                    head_sha=head,
                )
            )
    unique = {
        canonical_bytes(encode_lifecycle_state(s)): s
        for s in state_candidates
        if s is not None
    }
    if initial is None or projected is None:
        raise ReviewInputError("reply capacity lifecycle projection is unavailable")
    # These two positional states are a source protocol, not just a set.
    states = (
        initial,
        projected,
        *(
            state
            for encoded, state in unique.items()
            if encoded
            not in {
                canonical_bytes(encode_lifecycle_state(initial)),
                canonical_bytes(encode_lifecycle_state(projected)),
            }
        ),
    )
    outcome = review_check_outcome(
        policy=reviews_policy,
        review_status=refreshed.facts.review_status,
        required_fixes=refreshed.facts.has_blocking_findings,
        required_decisions=refreshed.facts.has_human_adjudication_findings
        or bool(inventory.pending),
        review_verdict=refreshed.facts.review_verdict,
    )
    check = ReviewCheckPublisher(http=operation.http).plan_completion(
        token=check_token,
        repository=repository,
        head_sha=head,
        app_slug=app_slug,
        outcome=outcome,
    )
    possible = refreshed.evaluate(app_authored=False, has_open_review_threads=False)
    approval = (
        (
            "POST",
            operation.http.repository_path(
                repository, f"/pulls/{pull_request}/reviews"
            ),
            {
                "commit_id": head,
                "event": "APPROVE",
                "body": _approval_body(
                    approval_marker(
                        repository_id=live.repository_id,
                        pull_request=pull_request,
                        head_sha=head,
                        base_sha=live.base_sha,
                    ),
                    refreshed,
                    possible,
                ),
            },
        )
        if reviews_policy == "auto-approve" and possible.approved
        else None
    )
    authority = (
        "POST",
        operation.http.repository_path(repository, f"/pulls/{pull_request}/reviews"),
        {"commit_id": head, "event": "COMMENT", "body": body},
    )
    payload = {
        "read_contract": "closed-inline-reply-single-complete-page-v1",
        "repository": repository,
        "pull_request": pull_request,
        "head_sha": head,
        "source": prepared.authority_digest,
        "intent": action["intent"],
        "authority_body": body,
        "original_reviews": reviews,
        "cleanup_instances": sorted(instances),
        "cleanup_order": ordered,
        "preconfirmed": sorted(preconfirmed),
        "cleanup_requests": requests,
        "cleanup_native_calls": cleanup_calls,
        "ordered_requests": [
            authority,
            *requests,
            *([approval] if approval else []),
            check,
        ],
        "execution_read_tail": {
            "post_report_fences": 3,
            "finalizer_opening": 2,
            "cleanup_discovery": 1,
            "approval_and_loss": 5 if approval else 0,
            "check_lookup": 1,
            "final_uncertain_native_readback": 2,
        },
        "execution_capability_tail": {},
        "report_native_calls": 12,
    }
    from .pending import pending_reply_pages

    pages = pending_reply_pages(
        body=reply.body,
        head_sha=head,
        eligibility=refreshed,
        authority_digest=hashlib.sha256(
            json.dumps(
                [
                    prepared.source_body,
                    prepared.source_actor,
                    prepared.source_metadata_digest,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        diagnostic=prepared.evidence_diagnostic,
        decision_reasons={item.fingerprint: item.rationale for item in reply.decisions},
        assessment_digest=hashlib.sha256(
            canonical_bytes([item.to_dict() for item in reply.decisions])
        ).hexdigest(),
        layout=layout,
    )
    if len(pages) != 1:
        return None  # Existing multi-page protocol is not a closed single page.
    # One report scan + two complete 3-call fences + source/existing/PR/POST/readback.
    payload["report_pages"] = [page.body for page in pages]
    if getattr(journal.ledger, "evidence_budget", None) is not budget:
        raise ReviewInputError("reply original ledger budget changed")
    journal._native_budget = budget
    plan = ReplyFinalizationPlan(
        journal,
        journal._record,
        canonical_bytes(journal._record.to_dict()),
        publisher.http,
        budget,
        canonical_bytes(payload),
        states,
        canonical_bytes([encode_lifecycle_state(s) for s in states]),
    )
    _ISSUED[plan] = plan._signature()
    _DOCUMENTS[plan] = canonical_bytes(journal._document())
    _TRANSPORTS[plan] = _source_http(publisher.http, budget)
    _OWNERS[plan] = (
        publisher,
        consumer,
        publisher.conversation,
        publisher.finalizer,
        publisher.conversation.finalizer,
    )
    journal.preflight_reply_finalization(plan, http=publisher.http, budget=budget)
    return plan


@contextmanager
def execute_reply_capacity(plan, publisher):
    if plan is None:
        yield
        return
    from .command_finalization_plan import ClosedCommandReads

    plan.require(
        plan.journal, http=publisher.http, budget=publisher.finalizer.evidence_budget
    )
    _require_owner_identity(plan, publisher)
    consumer = publisher.finalizer._finding_lifecycle_consumer
    if plan.reuses_retained_lifecycle():
        plan.reuse_retained_lifecycle(consumer)
    owners = {
        id(owner): owner
        for owner in (
            publisher,
            publisher.finalizer,
            publisher.conversation,
            publisher.conversation.finalizer,
        )
    }
    prior = [(owner, owner.http) for owner in owners.values()]
    if any(http is not plan.http for _, http in prior):
        raise ReviewInputError("reply capacity execution transport changed")
    consumer._reply_capacity_plan = plan
    bounded = ClosedCommandReads(plan.http)
    try:
        for owner, _ in prior:
            owner.http = bounded
        yield
    finally:
        consumer._reply_capacity_plan = None
        for journal, issued in list(_EXECUTING.items()):
            if issued is plan:
                del _EXECUTING[journal]
        _APPROVED.pop(plan, None)
        _ISSUED.pop(plan, None)
        _DOCUMENTS.pop(plan, None)
        _REUSE.pop(plan, None)
        _RESTORE_QUOTE.pop(plan, None)
        _TRANSPORTS.pop(plan, None)
        _OWNERS.pop(plan, None)
        for owner, http in prior:
            owner.http = http


def stop_reply_cleanup(journal):
    if executing_plan(journal) is None:
        return False
    journal._reply_native_stopped = True
    return True


def restrict_reply_release(journal, instances):
    plan = executing_plan(journal)
    if plan is None:
        return instances
    from .finding_lifecycle import _digest, _request_identity

    payload = json.loads(plan.payload)
    preconfirmed = set(payload["preconfirmed"])
    allowed = set(preconfirmed) if preconfirmed <= set(instances) else set()
    for instance, (method, path, raw) in zip(
        payload["cleanup_order"], payload["cleanup_requests"], strict=True
    ):
        key = _digest(
            {"method": method, "path": path, "request": _request_identity(raw)}
        )
        retained = [e for e in journal.effects if e["request_sha256"] == key]
        if len(retained) != 1 or retained[0]["status"] != "confirmed":
            break
        allowed.add(instance)
    return set(instances) & allowed


def require_reply_native_request(journal, method, path, body):
    plan = executing_plan(journal)
    if plan is None:
        return
    payload = json.loads(plan.payload)
    if canonical_bytes([method, path, body]) not in {
        canonical_bytes(request) for request in payload["ordered_requests"]
    }:
        raise ReviewInputError("reply finalization native request was not priced")
    from .finding_lifecycle import _digest, _pack_journal, _request_identity

    request = _request_identity(body)
    digest = _digest({"method": method, "path": path, "request": request})
    expected = dict(_APPROVED[plan]).get(digest)
    if expected is not None and not any(
        e["request_sha256"] == digest for e in journal.effects
    ):
        document = copy.deepcopy(journal._document())
        document["effects"].append(
            {
                "method": method,
                "path": path,
                "request": request,
                "request_sha256": digest,
                "status": "pending",
                "response": None,
            }
        )
        if canonical_bytes(_pack_journal(document)) != expected:
            raise ReviewInputError("reply initial pending document changed")
    key = (journal.identity.repository_id, journal.identity.pull_request)
    state = (
        journal._planned
        or journal.reviewer._drift_states.get(key)
        or journal.reviewer._active_window_states.get(key)
    )
    if state is not None and canonical_bytes(encode_lifecycle_state(state)) not in {
        canonical_bytes(encode_lifecycle_state(item)) for item in plan.states
    }:
        raise ReviewInputError("reply finalization state was not priced")


def require_reply_cleanup_batch(journal, requests):
    plan = executing_plan(journal)
    if plan is not None and canonical_bytes(requests) != canonical_bytes(
        json.loads(plan.payload)["cleanup_requests"]
    ):
        raise ReviewInputError("reply cleanup source order changed before dispatch")
