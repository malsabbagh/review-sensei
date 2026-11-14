"""Process-local capacity observations. These never authorize native effects."""

from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import encode_lifecycle_state

from .http import GitHubHttp

_ISSUED: WeakSet = WeakSet()
_EXECUTED: WeakSet = WeakSet()
_EXECUTION_JOURNALS: WeakKeyDictionary = WeakKeyDictionary()
_PREVIEW: WeakKeyDictionary = WeakKeyDictionary()
_PENDING: WeakKeyDictionary = WeakKeyDictionary()
_REQUESTS: WeakKeyDictionary = WeakKeyDictionary()
_RETAINED: WeakKeyDictionary = WeakKeyDictionary()
_DICTIONARY: WeakKeyDictionary = WeakKeyDictionary()
_CHECK_CREDENTIALS: WeakKeyDictionary = WeakKeyDictionary()
_JOURNAL_TRANSPORTS: WeakKeyDictionary = WeakKeyDictionary()
_CLEANUP: WeakKeyDictionary = WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class CommandFinalizationPlan:
    journal: Any
    record: Any
    root: bytes
    operation_id: str
    head_sha: str
    http: Any
    budget: Any
    payload: bytes
    states: tuple[Any, ...]
    state_bytes: bytes

    def require(self, journal, *, http, budget):
        if (
            self not in _ISSUED
            or self.journal is not journal
            or self.record is not journal._record
            or self.root != canonical_bytes(journal._record.to_dict())
            or self.operation_id != journal.operation_id
            or self.head_sha != journal.head_sha
            or self.http is not http
            or self.budget is not budget
            or self.state_bytes
            != canonical_bytes([encode_lifecycle_state(state) for state in self.states])
        ):
            raise ReviewInputError("command finalization capacity observation changed")

    def can_reuse_retained_lifecycle(self, preview_record):
        """Pure sizing predicate; runtime still requires the actual owned CAS root."""
        from .api_run_requests import ApiRunRequestTransport

        caller = getattr(self.journal.ledger, "caller", None)
        return (
            self in _ISSUED
            and self in _RETAINED
            and type(caller) is ApiRunRequestTransport
            and preview_record.identity() == self.record.identity()
            and canonical_bytes(preview_record.finding_lifecycle)
            == canonical_bytes(self.record.finding_lifecycle)
        )

    def reuse_retained_lifecycle(self, ledger, record, *, dispositions):
        """Reuse exact committed bytes only across this caller's own command CAS."""
        import json

        from review_sensei.session import SessionRecord
        from review_sensei.terminal_representability import action_intent_sha256

        self.require(self.journal, http=self.http, budget=self.budget)
        caller = getattr(ledger, "caller", None)
        preview = _PREVIEW.get(self)
        owns_record = record is getattr(caller, "_effect_record", None)
        if (
            ledger is not self.journal.ledger
            or not self.can_reuse_retained_lifecycle(record)
            or not isinstance(record, SessionRecord)
            or not owns_record
            or record.record_sha256 != getattr(caller, "_root", None)
            or canonical_bytes(SessionRecord.from_dict(record.to_dict()).to_dict())
            != canonical_bytes(record.to_dict())
            or preview is None
            or canonical_bytes([item.to_dict() for item in dispositions]) != preview[1]
            or canonical_bytes(self.journal._document()) != _RETAINED[self]
        ):
            raise ReviewInputError("command retained lifecycle custody changed")
        action = (record.api_run_accounting or {}).get("action") or {}
        if (
            not isinstance(action, dict)
            or action.get("phase") != "pending"
            or action_intent_sha256(action.get("intent", {})) != preview[2]
        ):
            raise ReviewInputError("command retained lifecycle admitted intent changed")
        expected = json.loads(preview[0])
        actual = record.to_dict()
        for field in (
            "api_run_accounting",
            "generation",
            "updated_at",
            "record_sha256",
            "dispositions",
            "disposition_snapshot",
        ):
            expected.pop(field, None)
            actual.pop(field, None)
        if canonical_bytes(expected) != canonical_bytes(actual):
            raise ReviewInputError(
                "command retained lifecycle business custody changed"
            )
        return record, json.loads(_RETAINED[self])

    def bind_dictionary_base(self, base):
        """Pin existing authenticated full bytes, never a new leaf or authority."""
        import json
        from hashlib import sha256

        from review_sensei.finding_lifecycle_store import (
            DOCUMENT_VERSION,
            normalize_reference,
        )

        from .finding_lifecycle import _pack_journal
        from .finding_lifecycle_dictionary import pack

        self.require(self.journal, http=self.http, budget=self.budget)
        retained = _RETAINED.get(self)
        reference = normalize_reference(self.record.finding_lifecycle)
        if retained is None or reference is None or self in _DICTIONARY:
            raise ReviewInputError("command dictionary base custody is unavailable")
        expected = {
            "schema_version": DOCUMENT_VERSION,
            "lifecycle": _pack_journal(json.loads(retained)),
        }
        raw = canonical_bytes(base)
        manifest = reference["manifest"]
        if (
            raw != canonical_bytes(expected)
            or sha256(raw).hexdigest() != manifest["sha256"]
        ):
            raise ReviewInputError(
                "command dictionary base differs from selected evidence"
            )
        # Run the closed codec's independent base/scope validation while still
        # at the original authenticated root, before retaining any context.
        pack(
            json.loads(retained),
            base=base,
            manifest=manifest,
            current_binding=manifest["binding"],
        )
        _DICTIONARY[self] = (
            raw,
            canonical_bytes(manifest),
            _dictionary_plan_scope(self),
            self.journal.ledger,
        )

    def bind_preview_capacity(self, preview_record, details, *, pending_documents=None):
        import json

        from review_sensei.disposition import decode_session_dispositions
        from review_sensei.terminal_representability import action_intent_sha256

        self.require(self.journal, http=self.http, budget=self.budget)
        original = self.record.api_run_accounting or {}
        action = original.get("action") or {}
        if (
            details.get("original_root") != self.record.record_sha256
            or details.get("preview_root") != preview_record.record_sha256
            or details.get("intent_sha256")
            != action_intent_sha256(action.get("intent", {}))
            or not isinstance(details.get("dispositions"), bytes)
            or preview_record.identity() != self.record.identity()
        ):
            raise ReviewInputError("command preview capacity commitment changed")
        commitment = (
            canonical_bytes(preview_record.to_dict()),
            canonical_bytes(
                [
                    item.to_dict()
                    for item in decode_session_dispositions(
                        json.loads(details["dispositions"])
                    )
                ]
            ),
            details["intent_sha256"],
            details["dispositions"],
        )
        if self in _PREVIEW and _PREVIEW[self] != commitment:
            raise ReviewInputError("command preview capacity commitment was replaced")
        if pending_documents is not None:
            from review_sensei.bounded_evidence import MAX_STORED_PART_BYTES

            from .finding_lifecycle import MAX_EFFECTS, _digest, _request_identity

            payload = json.loads(self.payload)
            requests = list(payload["cleanup_requests"])
            requests.extend(
                request
                for request in (
                    payload.get("review_update"),
                    payload.get("check_request"),
                    payload.get("conditional_approval"),
                )
                if request is not None
            )
            allowed = {
                _digest(
                    {"method": method, "path": path, "request": _request_identity(body)}
                )
                for method, path, body in requests
            }
            if (
                not isinstance(pending_documents, dict)
                or len(pending_documents) > MAX_EFFECTS
                or set(pending_documents) - allowed
                or any(
                    not isinstance(value, bytes) or len(value) > MAX_STORED_PART_BYTES
                    for value in pending_documents.values()
                )
            ):
                raise ReviewInputError(
                    "command pending-document capacity commitment is invalid"
                )
            pending = tuple(sorted(pending_documents.items()))
            if self in _PENDING and _PENDING[self] != pending:
                raise ReviewInputError(
                    "command pending-document capacity commitment changed"
                )
            _PENDING[self] = pending
            _REQUESTS[self] = frozenset(allowed)
        _PREVIEW[self] = commitment


class CapacityPrepared(Exception):
    def __init__(self, plan: CommandFinalizationPlan):
        self.plan = plan
        super().__init__("capacity-only command preparation complete")


def issue_plan(finalizer, *, payload, states, check_token, cleanup_instances=None):
    journal = finalizer._finding_lifecycle_journal
    plan = CommandFinalizationPlan(
        journal=journal,
        record=journal._record,
        root=canonical_bytes(journal._record.to_dict()),
        operation_id=journal.operation_id,
        head_sha=journal.head_sha,
        http=finalizer.http,
        budget=finalizer.evidence_budget or finalizer._operation_budget,
        payload=canonical_bytes(payload),
        states=tuple(states),
        state_bytes=canonical_bytes(
            [encode_lifecycle_state(state) for state in states]
        ),
    )
    _ISSUED.add(plan)
    if cleanup_instances is not None:
        _bind_cleanup_prefixes(plan, cleanup_instances)
    from hashlib import sha256

    _CHECK_CREDENTIALS[plan] = sha256(check_token.encode()).digest()
    # Independently bind supplied in-memory journal bytes to the committed
    # evidence envelope. Legacy representations that do not re-encode exactly
    # retain their ordinary priced restore path.
    from review_sensei.finding_lifecycle_store import (
        DOCUMENT_VERSION,
        normalize_reference,
    )

    from .finding_lifecycle import _pack_journal

    try:
        reference = normalize_reference(journal._record.finding_lifecycle)
        document = journal._document()
        envelope = {
            "schema_version": DOCUMENT_VERSION,
            "lifecycle": _pack_journal(document),
        }
        if (
            reference is not None
            and sha256(canonical_bytes(envelope)).hexdigest()
            == reference["manifest"]["sha256"]
        ):
            _RETAINED[plan] = canonical_bytes(document)
    except (ReviewInputError, AttributeError, TypeError):
        pass  # Unqualified reuse retains the original priced restore path.
    return plan


def register_journal_transport(transport, journal, delegate):
    from .finding_lifecycle import FindingLifecycleJournal, _JournalTransport

    if (
        type(journal) is not FindingLifecycleJournal
        or type(transport) is not _JournalTransport
        or transport.journal is not journal
        or transport.delegate is not delegate
        or getattr(transport.request, "__func__", None) is not _JournalTransport.request
        or getattr(transport.preflight_native_batch, "__func__", None)
        is not _JournalTransport.preflight_native_batch
        or getattr(journal.request, "__func__", None)
        is not FindingLifecycleJournal.request
        or getattr(journal.preflight_native_batch, "__func__", None)
        is not FindingLifecycleJournal.preflight_native_batch
    ):
        raise ReviewInputError("command capacity journal transport issuer is invalid")
    _JOURNAL_TRANSPORTS[transport] = (
        journal,
        delegate,
        _JournalTransport.request,
        _JournalTransport.preflight_native_batch,
        FindingLifecycleJournal.request,
        FindingLifecycleJournal.preflight_native_batch,
    )


def _underlying_http(http, *, budget):
    from .publication_recovery import PublicationBudgetHttp, _PaginationObservationHttp

    seen = set()
    for _ in range(16):
        if id(http) in seen:
            break
        seen.add(id(http))
        try:
            registered = _JOURNAL_TRANSPORTS.get(http)
        except TypeError as error:
            raise ReviewInputError(
                "command capacity transport source is unsupported"
            ) from error
        if registered is not None:
            if (
                vars(http).get("delegate") is not registered[1]
                or vars(http).get("journal") is not registered[0]
                or getattr(registered[0], "_native_budget", None) is not budget
                or getattr(registered[0].request, "__func__", None) is not registered[4]
                or getattr(registered[0].preflight_native_batch, "__func__", None)
                is not registered[5]
                or getattr(http.request, "__func__", None) is not registered[2]
                or getattr(http.preflight_native_batch, "__func__", None)
                is not registered[3]
            ):
                raise ReviewInputError("command capacity journal transport changed")
            http = registered[1]
        elif type(http) in (
            PublicationBudgetHttp,
            _PaginationObservationHttp,
            ClosedCommandReads,
        ):
            if (
                getattr(http.request, "__func__", None) is not type(http).request
                or type(http) is PublicationBudgetHttp
                and http.publication_budget is not budget
                or type(http) is _PaginationObservationHttp
                and http.observation_budget is not budget
            ):
                raise ReviewInputError("command capacity accounting transport changed")
            http = http.transport
        elif (
            type(http) is GitHubHttp
            and getattr(http.request, "__func__", None) is GitHubHttp.request
        ):
            return http
        else:
            raise ReviewInputError("command capacity transport source is unsupported")
    raise ReviewInputError("command capacity transport chain is unqualified")


class ClosedCommandReads(GitHubHttp):
    """Negative-only single-complete-page restriction; never synthesizes data."""

    def __init__(self, transport):
        self.transport = transport

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def request(self, method, path, *, token, body=None, **kwargs):
        from .errors import GitHubHTTPError

        query = (
            method == "POST"
            and path == "/graphql"
            and isinstance(body, dict)
            and str(body.get("query", "")).lstrip().startswith("query")
        )
        try:
            result = self.transport.request(
                method, path, token=token, body=body, **kwargs
            )
        except GitHubHTTPError as error:
            if method == "GET" or query:
                raise ReviewInputError(
                    "command capacity bounded read failed without retry"
                ) from error
            raise
        if query:
            if (
                result[0] != 200
                or not isinstance(result[1], dict)
                or result[1].get("errors") not in (None, [])
            ):
                raise ReviewInputError(
                    "command capacity bounded query failed without retry"
                )

            def incomplete(value):
                if isinstance(value, dict):
                    page = value.get("pageInfo")
                    if isinstance(page, dict) and page.get("hasNextPage") is not False:
                        return True
                    return any(incomplete(item) for item in value.values())
                if isinstance(value, list):
                    return any(incomplete(item) for item in value)
                return False

            if incomplete(result[1]):
                raise ReviewInputError(
                    "command capacity frontier grew beyond one complete page"
                )
        return result

    def paginate(self, *, path, token, timeout_seconds=None, **kwargs):
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        if set(kwargs) - {"page_sizes", "max_requests"}:
            raise ReviewInputError(
                "command capacity pagination options are unsupported"
            )
        sizes = kwargs.get("page_sizes")
        maximum = kwargs.get("max_requests")
        if (
            sizes is not None
            and (not isinstance(sizes, tuple) or not sizes or sizes[0] != 100)
            or maximum is not None
            and (type(maximum) is not int or maximum < 1)
        ):
            raise ReviewInputError("command capacity pagination bound is unsupported")
        parsed = urlsplit(path)
        supplied = dict(parse_qsl(parsed.query))
        if supplied.get("per_page", "100") != "100" or supplied.get("page", "1") != "1":
            raise ReviewInputError("command capacity pagination scope is unsupported")
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query)
            if key not in {"page", "per_page"}
        ]
        query.extend((("per_page", "100"), ("page", "1")))
        bounded = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query),
                parsed.fragment,
            )
        )
        status, payload = self.request(
            "GET", bounded, token=token, timeout_seconds=timeout_seconds
        )
        if status != 200 or not isinstance(payload, list) or len(payload) >= 100:
            raise ReviewInputError("command capacity frontier is not one complete page")
        return payload


def attach_execution_reads(plan, finalizer, journal, *, dispositions, check_token=None):
    """Transfer only a negative read bound across the same source operation."""
    import json
    from copy import copy
    from hashlib import sha256

    from .publication import ReviewApprovalFinalizer

    if (
        type(plan) is CommandFinalizationPlan
        and json.loads(plan.payload).get("read_contract")
        == "quarantine-sizing-observation-only-v1"
    ):
        raise ReviewInputError(
            "quarantine sizing observation cannot authorize execution"
        )
    if (
        type(plan) is not CommandFinalizationPlan
        or plan not in _ISSUED
        or plan in _EXECUTED
        or not isinstance(check_token, str)
        or sha256(check_token.encode()).digest() != _CHECK_CREDENTIALS.get(plan)
        or type(finalizer) is not ReviewApprovalFinalizer
        or _underlying_http(finalizer.http, budget=plan.budget)
        is not _underlying_http(plan.http, budget=plan.budget)
        or (finalizer.evidence_budget or finalizer._operation_budget) is not plan.budget
        or journal.identity != plan.journal.identity
        or journal.operation_id != plan.operation_id
        or journal.head_sha != plan.head_sha
    ):
        raise ReviewInputError("command capacity execution read scope changed")
    if (
        getattr(finalizer._require_closed_capacity_source, "__func__", None)
        is not ReviewApprovalFinalizer._require_closed_capacity_source
    ):
        raise ReviewInputError("command capacity execution source changed")
    finalizer._require_closed_capacity_source()
    plan.require(plan.journal, http=plan.http, budget=plan.budget)
    import json

    from review_sensei.terminal_representability import action_intent_sha256

    preview = _PREVIEW.get(plan)
    if preview is None:
        raise ReviewInputError("command execution has no admitted preview commitment")
    expected = json.loads(preview[0])
    actual = journal._record.to_dict()
    action = (journal._record.api_run_accounting or {}).get("action") or {}
    if (
        canonical_bytes([item.to_dict() for item in dispositions]) != preview[1]
        or action_intent_sha256(action.get("intent", {})) != preview[2]
        or journal._record.generation < plan.record.generation
    ):
        raise ReviewInputError("command execution differs from its admitted decisions")
    for field in (
        "api_run_accounting",
        "generation",
        "updated_at",
        "record_sha256",
        "dispositions",
        "disposition_snapshot",
    ):
        expected.pop(field, None)
        actual.pop(field, None)
    if canonical_bytes(expected) != canonical_bytes(actual):
        raise ReviewInputError("command execution changed an unplanned business field")
    from types import MappingProxyType

    pending = _PENDING.get(plan)
    if pending is None:
        raise ReviewInputError(
            "command execution pending-document commitment is absent"
        )
    journal._command_prepriced_pending_documents = MappingProxyType(dict(pending))
    journal._command_prepriced_request_keys = _REQUESTS[plan]
    operation = copy(finalizer)
    operation.http = ClosedCommandReads(finalizer.http)
    operation._closed_command_reads = True
    operation._execution_capacity_plan = plan
    _EXECUTED.add(plan)
    _EXECUTION_JOURNALS[journal] = plan
    return operation


def stop_command_cleanup(journal):
    """Stop later native writes only for a transferred source-issued command."""
    try:
        plan = _EXECUTION_JOURNALS.get(journal)
    except TypeError:
        return False
    if (
        plan is None
        or plan not in _EXECUTED
        or journal.operation_id != plan.operation_id
        or journal.head_sha != plan.head_sha
        or journal.identity != plan.journal.identity
    ):
        return False
    journal._command_native_stopped = True
    return True


def _dictionary_plan_scope(plan):
    return (
        id(plan.journal),
        id(plan.record),
        plan.root,
        plan.operation_id,
        plan.head_sha,
        id(plan.http),
        id(plan.budget),
        plan.payload,
        plan.state_bytes,
        canonical_bytes([encode_lifecycle_state(state) for state in plan.states]),
    )


def pack_authorized_pending(journal, logical_document):
    """Represent only exact admitted pending bytes under transferred custody."""
    import json

    from .finding_lifecycle import _pack_journal
    from .finding_lifecycle_dictionary import pack

    try:
        plan = _EXECUTION_JOURNALS.get(journal)
    except TypeError:
        return None
    if plan is None:
        return None
    context = _DICTIONARY.get(plan)
    if context is None:
        return None
    if (
        plan not in _ISSUED
        or plan not in _EXECUTED
        or context[2] != _dictionary_plan_scope(plan)
        or context[3] is not journal.ledger
        or canonical_bytes(plan.record.to_dict()) != plan.root
        or journal.operation_id != plan.operation_id
        or journal.head_sha != plan.head_sha
        or journal.identity != plan.record.identity()
        or journal._record.identity() != plan.record.identity()
    ):
        raise ReviewInputError("command dictionary execution custody changed")
    pending = _PENDING.get(plan)
    if pending is None:
        raise ReviewInputError("command dictionary pending commitments are absent")
    if canonical_bytes(_pack_journal(logical_document)) not in dict(pending).values():
        return None  # Confirmed/final snapshots stay ordinary self-contained v3.
    return pack(
        logical_document,
        base=json.loads(context[0]),
        manifest=json.loads(context[1]),
        current_binding=journal._record.finding_lifecycle["manifest"]["binding"],
    )


def _bind_cleanup_prefixes(plan, instances):
    """Price finite source-discovered releases; never install them as authority."""
    import json

    from .finding_lifecycle import MAX_EFFECTS
    from .finding_lifecycle_consumers import project_confirmed_slot_release

    if not isinstance(instances, frozenset):
        raise ReviewInputError("command cleanup inventory is not source bounded")
    if not instances:
        return  # No cleanup or release capability is needed for this plan.
    journal = plan.journal
    payload = json.loads(plan.payload)
    requests = payload["cleanup_requests"]
    if len(requests) > MAX_EFFECTS:
        raise ReviewInputError("command cleanup prefix exceeds effect capacity")
    receipts = journal.reviewer.native_finding_receipts(
        repository_id=journal.identity.repository_id,
        pull_request=journal.identity.pull_request,
    )
    selected = {r["instance"]: r for r in receipts if r["instance"] in instances}
    if set(selected) != set(instances):
        raise ReviewInputError("command cleanup has no complete native binding")
    ordered = []
    for method, path, body in requests:
        if (
            method != "POST"
            or path != "/graphql"
            or body.get("operationName") != "ResolveReviewThread"
        ):
            raise ReviewInputError("command cleanup prefix is not a resolve sequence")
        target = body["variables"]["input"]["threadId"]
        matches = [r["instance"] for r in selected.values() if r["thread_id"] == target]
        if len(matches) != 1 or matches[0] in ordered:
            raise ReviewInputError("command cleanup prefix binding is ambiguous")
        ordered.append(matches[0])
    preconfirmed = instances - frozenset(ordered)
    key = (journal.identity.repository_id, journal.identity.pull_request)
    initial = (
        journal._planned
        or journal.reviewer._drift_states.get(key)
        or journal.reviewer._active_window_states.get(key)
    )
    sources = tuple(state for state in (initial, *plan.states) if state is not None)
    projections = {}
    for count in range(len(ordered) + 1):
        prefix = frozenset(ordered[:count])
        for allowed in (prefix, prefix | preconfirmed):
            for state in sources:
                projected = project_confirmed_slot_release(
                    state,
                    key=key,
                    receipts=selected,
                    instances=allowed,
                    head_sha=journal.head_sha,
                )
                projections[canonical_bytes(encode_lifecycle_state(projected))] = (
                    projected
                )
    values = tuple(projections.values())
    _CLEANUP[plan] = (
        _dictionary_plan_scope(plan),
        instances,
        tuple(ordered),
        preconfirmed,
        values,
        canonical_bytes([encode_lifecycle_state(s) for s in values]),
    )


def cleanup_capacity_states(plan):
    context = _CLEANUP.get(plan)
    if context is None:
        return tuple(plan.states)
    if context[0] != _dictionary_plan_scope(plan) or context[5] != canonical_bytes(
        [encode_lifecycle_state(s) for s in context[4]]
    ):
        raise ReviewInputError("command cleanup capacity observation changed")
    return context[4]


def _executing_cleanup(journal):
    try:
        plan = _EXECUTION_JOURNALS.get(journal)
    except TypeError:
        return None
    if plan is None or plan not in _CLEANUP:
        return None
    if (
        plan not in _ISSUED
        or plan not in _EXECUTED
        or journal.ledger is not plan.journal.ledger
        or journal.identity != plan.journal.identity
        or journal.operation_id != plan.operation_id
        or journal.head_sha != plan.head_sha
    ):
        raise ReviewInputError("command cleanup execution custody changed")
    cleanup_capacity_states(plan)
    return plan


def command_cleanup_capacity_states(journal, states, instances):
    plan = _executing_cleanup(journal)
    if plan is None:
        return None
    context = _CLEANUP[plan]
    if (
        instances != context[1]
        or canonical_bytes([encode_lifecycle_state(s) for s in states])
        != plan.state_bytes
    ):
        raise ReviewInputError("command cleanup projection scope changed")
    return cleanup_capacity_states(plan)


def restrict_command_release(journal, instances):
    plan = _executing_cleanup(journal)
    if plan is None:
        return instances
    import json

    from .finding_lifecycle import _digest, _request_identity

    context = _CLEANUP[plan]
    allowed = set(context[3]) if context[3] <= set(instances) else set()
    for instance, (method, path, raw) in zip(
        context[2], json.loads(plan.payload)["cleanup_requests"], strict=True
    ):
        key = _digest(
            {"method": method, "path": path, "request": _request_identity(raw)}
        )
        retained = [
            effect for effect in journal.effects if effect["request_sha256"] == key
        ]
        if len(retained) != 1 or retained[0]["status"] != "confirmed":
            break
        allowed.add(instance)
    return set(instances) & allowed


def apply_command_confirmed_release(journal, instances):
    """Consume actual resolver observations, never the capacity projection."""
    if _executing_cleanup(journal) is None:
        return
    from .finding_lifecycle_consumers import project_confirmed_slot_release

    instances = restrict_command_release(journal, instances)
    key = (journal.identity.repository_id, journal.identity.pull_request)
    receipts = journal.reviewer._native_finding_receipt_records.get(key, {})
    for attr in ("_active_window_states", "_drift_states"):
        states = getattr(journal.reviewer, attr)
        state = states.get(key)
        if state is not None:
            states[key] = project_confirmed_slot_release(
                state,
                key=key,
                receipts=receipts,
                instances=instances,
                head_sha=journal.head_sha,
            )


def retained_review_frontier(journal, reviews, *, producer, app_slug):
    """Select whole frames from authenticated native receipts, not matching text."""
    import hashlib

    path = f"/repos/{journal.identity.repository}/pulls/{journal.identity.pull_request}/reviews"
    expected = {}
    for effect in journal.effects:
        if not (
            effect["method"] == "POST"
            and effect["path"] == path
            or effect["method"] == "PUT"
            and effect["path"].startswith(path + "/")
        ):
            continue
        response = effect.get("response")
        if (
            effect["status"] != "confirmed"
            or not isinstance(response, (list, tuple))
            or len(response) != 2
            or not isinstance(response[1], dict)
        ):
            return None
        review_id = response[1].get("id")
        if (
            type(review_id) is not int
            or producer != f"github-bot:{response[1].get('producer_id')}"
        ):
            return None
        body = effect["request"].get("body")
        if not isinstance(body, dict) or set(body) != {"$body_sha256", "utf8_bytes"}:
            return None
        expected[review_id] = body
    if not expected:
        return None  # A two-frame proof needs actual retained native writes.
    originals = {
        r["review_id"]
        for r in journal.reviewer.native_finding_receipts(
            repository_id=journal.identity.repository_id,
            pull_request=journal.identity.pull_request,
        )
        if r["head_sha"] == journal.head_sha
    }
    if not originals or not originals <= set(expected):
        return None
    selected = []
    for review_id, body in expected.items():
        matches = [r for r in reviews if r.get("id") == review_id]
        if len(matches) != 1:
            return None
        review = matches[0]
        user, raw = review.get("user"), review.get("body")
        if (
            not isinstance(user, dict)
            or type(user.get("id")) is not int
            or producer != f"github-bot:{user['id']}"
            or user.get("type") != "Bot"
            or str(user.get("login", "")).casefold() != app_slug.casefold()
            or review.get("commit_id") != journal.head_sha
            or review.get("state") not in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
            or review.get("edited_by_human") is True
            or not isinstance(raw, str)
            or hashlib.sha256(raw.encode()).hexdigest() != body["$body_sha256"]
            or len(raw.encode()) != body["utf8_bytes"]
        ):
            return None
        if sum(r.get("body") == raw for r in reviews) != 1:
            return None
        selected.append(review)
    from .publication import APPROVAL_ELIGIBILITY_MARKER_PREFIX

    eligible_frames = [
        r
        for r in reviews
        if r.get("commit_id") == journal.head_sha
        and APPROVAL_ELIGIBILITY_MARKER_PREFIX in str(r.get("body", ""))
        and str((r.get("user") or {}).get("login", "")).casefold()
        == app_slug.casefold()
    ]
    if not eligible_frames or not any(eligible_frames[-1] is r for r in selected):
        return None
    return [r for r in reviews if any(r is selected_r for selected_r in selected)]


def command_preconfirmed_instances(plan):
    cleanup_capacity_states(plan)
    context = _CLEANUP.get(plan)
    return context[3] if context is not None else frozenset()


def checked_cleanup_discovery(journal, candidates, instances):
    """Negative full-body check over the existing complete source discovery."""
    import hashlib

    receipts = {
        r["instance"]: r
        for r in journal.reviewer.native_finding_receipts(
            repository_id=journal.identity.repository_id,
            pull_request=journal.identity.pull_request,
        )
    }
    found = set()
    for node, root, instance, old in candidates:
        receipt = receipts.get(instance)
        if (
            old
            or receipt is None
            or instance in found
            or receipt["head_sha"] != journal.head_sha
            or receipt["thread_id"] != node["id"]
            or receipt["comment_id"] != root["databaseId"]
            or hashlib.sha256(root["body"].encode()).hexdigest()
            != receipt["body_sha256"]
        ):
            raise ReviewInputError("command cleanup retained criterion binding changed")
        found.add(instance)
    if found != set(instances):
        raise ReviewInputError("command cleanup retained discovery is incomplete")
    return tuple(item[0] for item in candidates)


def execution_cleanup_roots(journal, resolver, *, root_reader, **scope):
    if _executing_cleanup(journal) is None:
        return root_reader()
    # This is the same discovery query previously performed by resolve. The
    # second candidate walk consumes these exact local nodes without another read.
    candidates = resolver._discover_candidates(root_reader=root_reader, **scope)
    return checked_cleanup_discovery(journal, candidates, scope["instances"])


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
