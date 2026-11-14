"""Invocation-local stopped drift execution, never a transferable capability.

The only issuer executes the original paid preparation inside a live original
publication observer. It does not settle effects: the existing journal owns all
native receipts, uncertain intents, physical debits, and final checkpoints.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from contextlib import contextmanager
from dataclasses import asdict, fields, is_dataclass
from functools import wraps
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal
from review_sensei.source_registry import seal_module as _seal_source_module

from . import ordinary_drift_read_contract
from .ordinary_drift_read_contract import (
    DriftReadScope,
    classify_read,
    read_limits,
    require_complete_read,
)

_SOURCE = None
_CHECKPOINT_SOURCE = None
_MODES: WeakKeyDictionary = WeakKeyDictionary()
_CLEANUP: WeakKeyDictionary = WeakKeyDictionary()


def _record_invariants(record):
    """Fields not owned by a lifecycle/accounting checkpoint remain exact."""
    from .ordinary_drift_plan import _require_data

    _require_data(record)
    return tuple(
        (field.name, _snapshot(getattr(record, field.name)))
        for field in fields(record)
        if field.name
        not in {
            "generation",
            "updated_at",
            "record_sha256",
            "finding_lifecycle",
            "api_run_accounting",
        }
    )


def _install_checkpoint_source(source_class):
    """Capture only the actual original defining-module checkpoint method."""
    global _CHECKPOINT_SOURCE
    from . import finding_lifecycle

    current = inspect.currentframe()
    frame = current.f_back if current is not None else None
    spec = finding_lifecycle.__spec__
    get_code = getattr(getattr(spec, "loader", None), "get_code", None)
    if (
        _CHECKPOINT_SOURCE is not None
        or frame is None
        or frame.f_globals is not vars(finding_lifecycle)
        or frame.f_locals is not frame.f_globals
        or frame.f_code.co_name != "<module>"
        or not getattr(spec, "_initializing", False)
        or not callable(get_code)
        or get_code(finding_lifecycle.__name__) != frame.f_code
        or source_class is not finding_lifecycle.FindingLifecycleJournal
    ):
        raise _hold("checkpoint source installation is unqualified")
    original = source_class.checkpoint
    original_document = source_class._document
    original_pending = source_class._pending_wire
    original_pack = finding_lifecycle._pack_journal

    @wraps(original)
    def checkpoint(journal, *, reserved_tail_calls=0, pending_intent=False):
        mode = next(
            (value for value in _MODES.values() if value.journal is journal), None
        )
        if mode is None:
            return original(
                journal,
                reserved_tail_calls=reserved_tail_calls,
                pending_intent=pending_intent,
            )
        try:
            mode.require()
            if source_class.checkpoint is not checkpoint or "checkpoint" in vars(
                journal
            ):
                raise _hold("checkpoint source method changed")
            before = journal._record
            before_root = before.record_sha256
            document = original_document(journal)
            packed = (
                original_pending(journal, document)
                if pending_intent
                else original_pack(document)
            )
            envelope = {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": packed,
            }
            expected_digest = hashlib.sha256(canonical_bytes(envelope)).hexdigest()
            output = original(
                journal,
                reserved_tail_calls=reserved_tail_calls,
                pending_intent=pending_intent,
            )
            after = journal._record
            reference = after.finding_lifecycle
            if (
                type(reference) is not dict
                or reference.get("previous_root_sha256") != before_root
                or reference.get("operation_id") != journal.operation_id
                or reference.get("manifest", {}).get("sha256") != expected_digest
                or after.generation != before.generation + 1
                or _record_invariants(after) != mode.record_invariants
                or mode.caller._root != after.record_sha256
                or mode.caller._effect_record is not after
                or mode.caller._checkpoint is not None
            ):
                raise _hold(
                    "checkpoint successful return is not the exact owned source root"
                )
            mode.current_record = after
            mode.current_record_bytes = canonical_bytes(after.to_dict())
            mode.require()
            return output
        except BaseException:
            mode.stopped = True
            raise

    setattr(source_class, "checkpoint", checkpoint)
    _CHECKPOINT_SOURCE = (source_class, original, checkpoint)


def _install_sources(source_class, thread_query):
    """One defining-module installation; a supplied class/query cannot enroll."""
    global _SOURCE
    from . import (
        finding_lifecycle,
        ordinary_drift_plan,
        ordinary_publication_reads,
        publication,
    )

    current = inspect.currentframe()
    frame = current.f_back if current is not None else None
    spec = publication.__spec__
    get_code = getattr(getattr(spec, "loader", None), "get_code", None)
    if (
        _SOURCE is not None
        or frame is None
        or frame.f_globals is not vars(publication)
        or frame.f_locals is not frame.f_globals
        or frame.f_code.co_name != "<module>"
        or not getattr(spec, "_initializing", False)
        or not callable(get_code)
        or get_code(publication.__name__) != frame.f_code
        or source_class is not publication.ReviewPublisher
        or thread_query is not publication._REVIEW_THREADS_QUERY
    ):
        raise ReviewInputError("ordinary drift source installation is unqualified")
    _SOURCE = (
        source_class,
        thread_query,
        ordinary_drift_plan._observe_source,
        ordinary_publication_reads._Observation,
        ordinary_publication_reads._Observation._require,
        finding_lifecycle._request_identity,
        finding_lifecycle._body_matches,
    )


def _require_observer(observer):
    from . import ordinary_drift_plan, ordinary_publication_reads

    if (
        _SOURCE is None
        or type(observer) is not _SOURCE[3]
        or getattr(observer, "family", None) != "drift"
        or ordinary_publication_reads._Observation._require is not _SOURCE[4]
        or ordinary_drift_plan._observe_source is not _SOURCE[2]
    ):
        raise ReviewInputError("ordinary drift original observer is unavailable")
    require_module_seal(ordinary_drift_plan)
    require_module_seal(ordinary_drift_read_contract)
    require_module_seal(sys.modules[__name__])
    _SOURCE[4](observer, observer.journal, observer.publisher)


def _hold(message):
    return ReviewInputError("ordinary drift stopped: " + message)


def _snapshot(value):
    """Freeze closed producer data without invoking arbitrary codecs/callbacks."""
    if type(value) in (type(None), bool, int, float, str, bytes):
        return (type(value), value)
    if type(value) in (tuple, list):
        return (type(value), tuple(_snapshot(item) for item in value))
    if type(value) in (set, frozenset):
        return (type(value), frozenset(_snapshot(item) for item in value))
    if not isinstance(value, type) and is_dataclass(value):
        from .ordinary_drift_plan import _require_data

        _require_data(value)
        return (
            type(value),
            tuple(
                (field.name, _snapshot(getattr(value, field.name)))
                for field in fields(value)
            ),
        )
    if type(value) is dict:
        return (
            dict,
            tuple((_snapshot(key), _snapshot(item)) for key, item in value.items()),
        )
    raise _hold("source preparation contains non-closed values")


class _Mode:
    def __init__(self, observer, original_result, planned, prepared, inputs):
        self.observer = observer
        self.journal = observer.journal
        self.publisher = observer.publisher
        self.caller = self.journal.ledger.caller
        self.caller_scope = (
            _snapshot(asdict(self.caller.run)),
            self.caller.attempt,
            self.caller.job,
        )
        self.current_record = self.journal._record
        self.current_record_bytes = canonical_bytes(self.current_record.to_dict())
        self.record_invariants = _record_invariants(self.current_record)
        self.original_budget = self.caller.budget_view
        self.inputs = inputs
        self.input_bytes = _snapshot(inputs)
        self.original_result = original_result
        self.result_bytes = canonical_bytes(original_result.to_dict())
        self.planned = planned
        self.inventory = canonical_bytes(asdict(planned.inventory))
        self.prepared = prepared
        self.prepared_bytes = _snapshot(prepared)
        self.branches = tuple(row[0] for row in prepared["projections"])
        self.remaining = read_limits()
        self.index = 0
        self.chosen = None
        self.confirmed: tuple[bytes, ...] = ()
        self.check_id = None
        self.review_id = None
        self.stopped = False
        self.finishing = False
        self.suffix_checked = False
        self.finish_used = False
        self.in_request = False
        self.attempt = None

    def require(self):
        from .ordinary_drift_plan import _require_preparation_source

        _require_observer(self.observer)
        if (
            _MODES.get(self.observer) is not self
            or self.observer.journal is not self.journal
            or self.observer.publisher is not self.publisher
            or self.journal.ledger.caller is not self.caller
            or self.caller.budget_view is not self.original_budget
            or self.caller_scope
            != (
                _snapshot(asdict(self.caller.run)),
                self.caller.attempt,
                self.caller.job,
            )
        ):
            raise _hold("invocation custody changed")
        _require_preparation_source(
            self.publisher, self.journal, self.planned, *self.inputs
        )
        record = self.journal._record
        if (
            _snapshot(self.inputs) != self.input_bytes
            or record is not self.current_record
            or canonical_bytes(record.to_dict()) != self.current_record_bytes
            or _record_invariants(record) != self.record_invariants
            or canonical_bytes(self.original_result.to_dict()) != self.result_bytes
            or canonical_bytes(asdict(self.planned.inventory)) != self.inventory
            or _snapshot(self.prepared) != self.prepared_bytes
            or record.transaction is None
            or self.original_result.transaction is None
            or not self.original_result.transaction.logical_identity_matches(
                record.transaction
            )
            or self.original_result.content_digest() != record.transaction.result_sha256
            or self.caller._root != record.record_sha256
            or record.quarantine_lineage is not None
            or record.historical_assessments is not None
        ):
            raise _hold("original result, criterion inventory, or root changed")

    def scope(self):
        requests = self.branches[0]
        return (
            DriftReadScope(
                repository=self.journal.identity.repository,
                pull_request=self.journal.identity.pull_request,
                head_sha=self.journal.head_sha,
                thread_query=_SOURCE[1],
                inline_comment_ids=frozenset(
                    int(path.rsplit("/", 1)[1]) for _, path, _, _ in requests[2:-1]
                ),
                check_run_id=self.check_id,
                review_id=self.review_id,
                review_comment_count=len(requests[-1][2]["comments"]),
            )
            if self.review_id is not None
            else DriftReadScope(
                repository=self.journal.identity.repository,
                pull_request=self.journal.identity.pull_request,
                head_sha=self.journal.head_sha,
                thread_query=_SOURCE[1],
                inline_comment_ids=frozenset(
                    int(path.rsplit("/", 1)[1]) for _, path, _, _ in requests[2:-1]
                ),
                check_run_id=self.check_id,
            )
        )

    def prefix(self):
        effects = self.journal.effects
        if len(effects) != self.index or any(
            e["status"] != "confirmed" for e in effects
        ):
            raise _hold("native prefix is not exactly confirmed")
        if tuple(canonical_bytes(e) for e in effects) != self.confirmed:
            raise _hold("confirmed native prefix changed")

    def native(self, method, path, body):
        self.prefix()
        candidates = self.branches if self.chosen is None else (self.chosen,)
        selected = []
        for branch in candidates:
            if self.index >= len(branch):
                continue
            expected_method, expected_path, expected_body, _ = branch[self.index]
            if method != expected_method or canonical_bytes(body) != canonical_bytes(
                expected_body
            ):
                continue
            if self.index < 2 and method == "PATCH":
                condition = self.journal._check_precondition(path)
                if condition is None or (
                    self.check_id is not None
                    and condition["check_run_id"] != self.check_id
                ):
                    continue
                actual_path = f"/repos/{self.journal.identity.repository}/check-runs/{condition['check_run_id']}"
                if path != actual_path:
                    continue
            elif path != expected_path:
                continue
            selected.append(branch)
        if len(selected) != 1:
            raise _hold("native request differs from the source sequence")
        if self.index >= 2 and not self.suffix_checked:
            raise _hold("body-update suffix has not passed its original preflight")
        self.chosen = selected[0]
        self.attempt = (method, path, _SOURCE[5](body))

    def accept_native(self, response):
        status, payload = response
        if (
            type(status) is not int
            or not 200 <= status < 300
            or type(payload) is not dict
        ):
            raise _hold("native response is unsuccessful or malformed")
        effects = self.journal.effects
        if len(effects) != self.index + 1 or effects[-1]["status"] != "confirmed":
            raise _hold("native response lacks the original confirmed journal receipt")
        if tuple(canonical_bytes(e) for e in effects[:-1]) != self.confirmed:
            raise _hold("native attempt changed its confirmed prefix")
        effect = effects[-1]
        if self.attempt != (effect["method"], effect["path"], effect["request"]):
            raise _hold("native receipt changed the exact attempted request")
        if self.chosen is None:
            raise _hold("native receipt has no selected branch")
        method, path, body, _ = self.chosen[self.index]
        if effect["method"] != method or effect["request"] != _SOURCE[5](body):
            raise _hold("native receipt request differs from issued source")
        if self.index < 2:
            commitment = self.journal._check_commitment(payload)
            if commitment is None or (
                self.check_id is not None
                and commitment["check_run_id"] != self.check_id
            ):
                raise _hold("check receipt is not the original exact owned check")
            self.check_id = commitment["check_run_id"]
        elif self.index == len(self.chosen) - 1:
            if type(payload.get("id")) is not int or payload["id"] <= 0:
                raise _hold("main review receipt has no numeric identity")
            self.review_id = payload["id"]
        self.attempt = None
        self.index += 1
        self.confirmed = tuple(canonical_bytes(e) for e in effects)

    def pending_review(self, values):
        """Constrain an upcoming read; the original journal still validates it."""
        pending = [e for e in self.journal.effects if e["status"] == "pending"]
        if len(pending) != 1:
            return
        effect = pending[0]
        if effect["method"] != "POST" or not effect["path"].endswith("/reviews"):
            return
        request = effect["request"]
        candidates = [
            row
            for row in values
            if type(row) is dict
            and _SOURCE[6](row.get("body"), request.get("body"))
            and row.get("commit_id") == request.get("commit_id")
            and row.get("state")
            == {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
                request.get("event")
            )
        ]
        if len(candidates) != 1:
            raise _hold("pending main review does not have one exact readback")
        row = candidates[0]
        producer = self.journal.ledger.evidence_producer()
        user = row.get("user")
        if (
            not producer.startswith("github-bot:")
            or type(user) is not dict
            or type(user.get("id")) is not int
            or str(user["id"]) != producer.split(":", 1)[1]
            or row.get("edited_by_human") is True
            or type(row.get("id")) is not int
            or row["id"] <= 0
        ):
            raise _hold("pending main review ownership is unconfirmed")
        self.review_id = row["id"]


def prepare_current(
    publisher,
    observer,
    journal,
    *,
    original_result,
    result,
    assembled,
    eligibility,
    marker,
    outcome,
    policy,
    complete_inventory,
    checks_enabled,
    grouped,
    planned_window,
    planned_drift,
    convergence_policy,
    auto_approval,
    preserve_explanations,
    window_non_inline_blocking,
):
    """Execute paid original preparation; never accepts a caller-created proof."""
    _require_observer(observer)
    if observer.journal is not journal or observer.publisher is not publisher:
        raise _hold("preparation observer owner changed")
    if observer in _MODES:
        raise _hold("preparation cannot be repeated")
    from .ordinary_drift_plan import _require_preparation_source

    _require_preparation_source(
        publisher,
        journal,
        planned_drift,
        assembled,
        eligibility,
        original_result,
        result,
        outcome,
        policy,
        convergence_policy,
        auto_approval,
        preserve_explanations,
    )
    if type(preserve_explanations) is not bool:
        raise _hold("explanation rendering option is unqualified")
    # Only initial unsupported shape may retain the conservative route.
    if (
        planned_drift is None
        or planned_window is not None
        or grouped
        or checks_enabled is not True
        or window_non_inline_blocking
        or complete_inventory is not True
        or journal.effects
        or journal._record.quarantine_lineage is not None
        or journal._record.historical_assessments is not None
        or eligibility.human_inventory_reference is not None
    ):
        return None
    from .ordinary_drift_plan import _require_preparation_source

    _require_preparation_source(
        publisher,
        journal,
        original_result,
        result,
        planned_drift,
        assembled,
        eligibility,
    )
    record = journal._record
    if (
        planned_drift is not journal._planned
        or original_result.transaction is None
        or record.transaction is None
        or not original_result.transaction.logical_identity_matches(record.transaction)
        or original_result.content_digest() != record.transaction.result_sha256
    ):
        raise _hold("preparation is not the retained original transaction")
    prepared = _SOURCE[2](
        publisher,
        journal,
        planned=planned_drift,
        result=result,
        assembled=assembled,
        eligibility=eligibility,
        marker=marker,
        outcome=outcome,
        policy=policy,
        complete_inventory=complete_inventory,
        convergence_policy=convergence_policy,
        auto_approval=auto_approval,
        preserve_explanations=preserve_explanations,
    )
    if prepared is None:
        # Trusted unsupported shapes decline before mode/discount issuance.
        # Paid preparation remains charged; original generic preflights apply.
        return None
    _require_observer(observer)
    mode = _Mode(
        observer,
        original_result,
        planned_drift,
        prepared,
        (
            assembled,
            eligibility,
            original_result,
            result,
            outcome,
            policy,
            convergence_policy,
            auto_approval,
            preserve_explanations,
        ),
    )
    _MODES[observer] = mode
    try:
        mode.require()
        # Preparation must explicitly include the source-produced success frame.
        # A sizing placeholder or empirical string replacement is not authority.
        if prepared.get("success_frame_bound") is not True:
            raise _hold("source success frame is not bound")
        if len(mode.branches) != 2 or any(len(branch) < 4 for branch in mode.branches):
            raise _hold("source sequence is not the closed retained-update family")
    except BaseException:
        mode.stopped = True
        raise
    return prepared["quote"]


def _mode_for(observer):
    # Optional generic callers pass None; unissued objects cannot acquire a
    # profile by shape and must not fail WeakKeyDictionary's weakref coercion.
    if _SOURCE is None or type(observer) is not _SOURCE[3]:
        return None
    return _MODES.get(observer)


def handle_request(observer, transport, method, path, kwargs):
    require_module_seal(sys.modules[__name__])
    mode = _mode_for(observer)
    if mode is None:
        return False, None
    try:
        mode.require()
        if not any(transport is old for _, _, old, _, _ in observer.bindings):
            raise _hold("transport is not bound to the original observer")
        if mode.in_request:
            raise _hold("reentrant request is unsupported")
        is_read = method == "GET" or (method == "POST" and path == "/graphql")
        if mode.stopped and not (mode.finishing and is_read):
            raise _hold("a previous attempt failed; later work is forbidden")
        target = None
        if is_read:
            target = classify_read(mode.scope(), method, path, kwargs.get("body"))
            if mode.remaining[target.role] <= 0:
                raise _hold("bounded read role exhausted")
            if mode.finishing and not any(
                e["status"] == "pending" for e in mode.journal.effects
            ):
                raise _hold("finish has no uncertain native intent to read")
            mode.remaining[target.role] -= 1
        else:
            if mode.finishing:
                raise _hold("finish cannot dispatch another business effect")
            mode.native(method, path, kwargs.get("body"))
        mode.in_request = True
        try:
            response = transport.request(method, path, **kwargs)
        finally:
            mode.in_request = False
        mode.require()
        if target is not None:
            require_complete_read(target, response[0], response[1])
            if target.role == "reviews" and mode.finishing:
                mode.pending_review(response[1])
        else:
            mode.accept_native(response)
        return True, response
    except BaseException as error:
        mode.stopped = True
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise _hold(
            "request or receipt failed; original journal retains its outcome"
        ) from error


def suffix_native_calls(observer, *, planned, prepared, requests):
    require_module_seal(sys.modules[__name__])
    mode = _mode_for(observer)
    if mode is None:
        return None
    try:
        mode.require()
        mode.prefix()
        if (
            mode.stopped
            or mode.index != 2
            or mode.suffix_checked
            or planned is not mode.planned
        ):
            raise _hold("body-update suffix has changed or already started")
        updates = mode.publisher._prepare_existing_drift_updates(
            planned=planned, prepared=prepared
        )
        if canonical_bytes(updates) != mode.prepared["updates"]:
            raise _hold("prepared bodies or native bindings changed")
        expected = tuple(
            (method, path, body) for method, path, body, _ in mode.chosen[2:-1]
        )
        if canonical_bytes(requests) != canonical_bytes(expected):
            raise _hold("body-update requests differ from the prepared suffix")
        checkpoints = mode.prepared["quote"]["checkpoints"]
        extra = max(row["conditional_calls"] for row in checkpoints)
        # Existing batch preflight prices U pending roots + final receipt. Add
        # the separately reachable main-review pending root, never skip it.
        calls = sum(mode.remaining.values()) + len(expected) + 1 + extra
        mode.suffix_checked = True
        return calls
    except BaseException:
        mode.stopped = True
        raise


def stop(observer, error=None):
    """Negative-only latch, including errors before any native dispatch."""
    require_module_seal(sys.modules[__name__])
    mode = _mode_for(observer)
    if mode is not None:
        mode.stopped = True


@contextmanager
def finishing(observer):
    require_module_seal(sys.modules[__name__])
    mode = _mode_for(observer)
    if mode is None:
        yield
        return
    mode.require()
    if mode.finishing or mode.finish_used:
        mode.stopped = True
        raise _hold("finish cannot be repeated")
    mode.finish_used = True
    mode.finishing = True
    try:
        yield
    except BaseException:
        mode.stopped = True
        raise
    finally:
        mode.finishing = False
        mode.require()


def _matching_snapshot(publisher, kwargs):
    from .ordinary_drift_plan import _require_data

    key = (kwargs["repository_id"], kwargs["pull_request"])
    value = (
        kwargs,
        publisher._drift_states.get(key),
        publisher._active_window_states.get(key),
        {k: v for k, v in publisher._drift_concern_history.items() if k[:2] == key},
        publisher._native_finding_receipt_records.get(key),
    )
    _require_data(value)
    return _snapshot(value)


def empty_cleanup(observer, **matcher_kwargs):
    """Only the selected confirmed route may elide its exact empty matcher."""
    require_module_seal(sys.modules[__name__])
    mode = _mode_for(observer)
    if mode is None:
        return False
    try:
        mode.require()
        mode.prefix()
        if (
            mode.stopped
            or mode.chosen is None
            or mode.index != len(mode.chosen)
            or mode.publisher._pending_thread_cleanup is not None
            or set(matcher_kwargs)
            != {"threads", "repository_id", "pull_request", "head_sha", "replacements"}
            or type(matcher_kwargs["threads"]) not in (list, tuple)
            or matcher_kwargs["repository_id"] != mode.journal.identity.repository_id
            or matcher_kwargs["pull_request"] != mode.journal.identity.pull_request
            or matcher_kwargs["head_sha"] != mode.journal.head_sha
        ):
            raise _hold("cleanup source is not the completed selected route")
        snapshot = _matching_snapshot(mode.publisher, matcher_kwargs)
        if mode.publisher._matching_superseded_threads(**matcher_kwargs):
            raise _hold("selected cleanup requires unpriced native work")
        if _matching_snapshot(mode.publisher, matcher_kwargs) != snapshot:
            raise _hold("cleanup matcher changed its immutable source")
        _CLEANUP[mode.publisher] = (mode.journal, matcher_kwargs, snapshot)
        return True
    except BaseException:
        mode.stopped = True
        raise


def require_cleanup(publisher):
    """Validate after observer teardown and before any generic cleanup callback."""
    require_module_seal(sys.modules[__name__])
    guard = _CLEANUP.get(publisher)
    if guard is None:
        return
    journal, kwargs, snapshot = guard
    from .ordinary_drift_plan import _require_preparation_source

    _require_preparation_source(publisher, journal, kwargs)
    if (
        publisher._pending_thread_cleanup is not None
        or _matching_snapshot(publisher, kwargs) != snapshot
        or publisher._matching_superseded_threads(**kwargs)
    ):
        raise _hold("cleanup source changed before terminal completion")
    if _matching_snapshot(publisher, kwargs) != snapshot:
        raise _hold("cleanup source changed during final validation")
    _CLEANUP.pop(publisher, None)


def close(observer):
    if _mode_for(observer) is not None:
        _MODES.pop(observer, None)


_seal_source_module()
