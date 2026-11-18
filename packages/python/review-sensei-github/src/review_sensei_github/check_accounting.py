"""Live check-rejection accounting evidence; never restored recovery authority.

Only the original caller's physical response can qualify a rejection. This
proof does not confirm a check, reconcile another effect, refund a request,
or bypass the caller's global unresolved-mutation/deadline/funding guards.
"""

from __future__ import annotations

import hashlib
import json
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .action_completion import FinalizationSubject, _fingerprint, approval_confirmed
from .check_diagnostics import CheckFailure

_INSPECTION_BYTES = 8192
_ACTIVE: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_FAILURES: dict[int, tuple[Any, Any]] = {}
_CAPTURES: dict[int, tuple[Any, Any]] = {}
_CONTEXTS: dict[int, tuple[Any, Any]] = {}
_PUBLICATIONS: dict[int, tuple[Any, Any]] = {}
_CLEANUPS: dict[int, tuple[Any, Any]] = {}
_PROJECTIONS: dict[int, tuple[Any, Any]] = {}


def _remember(table: dict[int, tuple[Any, Any]], value: Any, proof: Any) -> None:
    key = id(value)
    table[key] = (weakref.ref(value, lambda _ref: table.pop(key, None)), proof)


def _lookup(table: dict[int, tuple[Any, Any]], value: Any) -> Any:
    item = table.get(id(value))
    return item[1] if item is not None and item[0]() is value else None


def _snapshot(caller: Any) -> tuple[Any, ...]:
    state = caller.accounting
    return state.identity, state.owner, state.configuration, state.startup


@dataclass(frozen=True)
class _SourceContext:
    caller: Any
    snapshot: Any
    source_result: Any
    binding: tuple
    root_sha256: str
    caller_root_sha256: str
    source_id: int

    def seal(self) -> tuple[Any, ...]:
        return (
            id(self.caller()),
            self.snapshot,
            id(self.source_result),
            self.source_id,
            self.binding,
            self.root_sha256,
            self.caller_root_sha256,
        )

    def valid(self, caller: Any) -> bool:
        return (
            self.caller() is caller
            and caller._state is not None
            and self.snapshot == _snapshot(caller)
            and _lookup(_CONTEXTS, self) == self.seal()
        )


def source_context(
    http: Any, source_result: Any, record: Any, identity: Any
) -> _SourceContext | None:
    from review_sensei.models import ReviewResult

    from .caller_operation import caller_for_http

    caller = caller_for_http(http)
    if (
        caller is None
        or caller._state is None
        or not caller._operation_entry_started
        or caller.accounting.owner.run_attempt != 1
        or type(source_result) is not ReviewResult
        or record is None
    ):
        return None
    tx = source_result.transaction
    current = record.transaction
    if (
        tx is None
        or current is None
        or current.phase not in {"publication_pending", "publication_failed"}
        or current.transaction_id != tx.transaction_id
        or current.result_sha256 != source_result.content_digest()
        or record.repository != identity.repository
        or record.pull_request != identity.pull_request
        or caller.run.repository_id != identity.repository_id
        or caller._effect_record is None
        or caller._root != caller._effect_record.record_sha256
    ):
        return None
    binding = (
        identity.repository,
        identity.pull_request,
        tx.base_sha,
        tx.head_sha,
        tx.transaction_id,
        source_result.content_digest(),
    )
    context = _SourceContext(
        weakref.ref(caller),
        _snapshot(caller),
        weakref.ref(source_result),
        binding,
        record.record_sha256,
        caller._root,
        id(source_result),
    )
    _remember(_CONTEXTS, context, context.seal())
    return context


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass
class _Capture:
    caller: Any
    snapshot: Any
    operation: str
    method: str
    url_digest: str
    body_digest: str | None
    target: tuple[str, int | None, str] | None = None
    broker: Any = None
    observation: Any = None
    request_id: str | None = None
    deadline: int | None = None
    matches: int = 0
    status: int | None = None
    closed: bool = False
    transaction: Any = None
    context: Any = None

    def bind(self, failure: CheckFailure) -> CheckFailure:
        if (
            self.closed
            and self.matches == 1
            and self.target is not None
            and type(failure) is CheckFailure
            and failure.operation == self.operation
            and failure.code == "check_access_denied"
            and failure.status == self.status
            and self.status in {401, 403}
            and self.valid()
        ):
            _remember(
                _FAILURES,
                failure,
                (self, (failure.code, failure.operation, failure.status)),
            )
        return failure

    def seal(self) -> tuple[Any, ...]:
        return (
            id(self.caller()) if self.caller else None,
            self.snapshot,
            self.operation,
            self.method,
            self.url_digest,
            self.body_digest,
            self.target,
            id(self.observation),
            self.request_id,
            self.deadline,
            self.matches,
            self.status,
            self.transaction,
            id(self.context),
        )

    def valid(self) -> bool:
        if not self.closed or _lookup(_CAPTURES, self) != self.seal():
            return False
        caller = self.caller() if self.caller is not None else None
        if (
            caller is None
            or caller._state is None
            or self.snapshot != _snapshot(caller)
        ):
            return False
        observation = self.observation
        context = self.context
        binding = None if context is None else context.binding
        if (
            context is None
            or not context.valid(caller)
            or binding != self.transaction
            or self.target
            not in {
                (binding[0], binding[1], binding[3]),
                (binding[0], None, binding[3]),
            }
        ):
            return False
        return (
            observation is not None
            and any(item is observation for item in caller.observations)
            and observation.get("request_id") == self.request_id
            and observation.get("deadline_unix_ms") == self.deadline
            and observation.get("outcome") == "response"
            and not observation.get("uncertain_effect")
            and any(
                item.request_id == self.request_id
                and item.deadline_unix_ms == self.deadline
                for item in caller.accounting.completed
            )
        )


@contextmanager
def _capture(
    caller, operation, method, url, body_digest, target, broker=None, context=None
):
    capture = _Capture(
        None,
        None,
        operation,
        method,
        _digest(url.encode()),
        body_digest,
        target,
        broker,
    )
    if caller is not None and caller._state is not None and caller not in _ACTIVE:
        capture.caller = weakref.ref(caller)
        capture.snapshot = _snapshot(caller)
        capture.context = context
        capture.transaction = None if context is None else context.binding
        if context is not None and context.valid(caller):
            _ACTIVE[caller] = capture
    try:
        yield capture
    finally:
        capture.closed = True
        capture.broker = None
        if caller is not None and _ACTIVE.get(caller) is capture:
            _remember(_CAPTURES, capture, capture.seal())
            del _ACTIVE[caller]


def broker_check_call(broker: Any, context: _SourceContext | None = None):
    from .api_run_requests import ApiRunRequestTransport

    if type(context) is not _SourceContext or _lookup(_CONTEXTS, context) is None:
        return _capture(None, "capability", "POST", "", None, None)
    owner = getattr(getattr(broker, "opener", None), "__self__", None)
    caller = (
        owner
        if isinstance(owner, ApiRunRequestTransport)
        and getattr(getattr(broker, "opener", None), "__func__", None)
        is ApiRunRequestTransport.open
        else None
    )
    return _capture(
        caller,
        "capability",
        "POST",
        getattr(broker, "broker_url", ""),
        None,
        None,
        broker,
        context,
    )


def native_check_call(
    http, operation, method, path, *, repository, head_sha, body=None, context=None
):
    if type(context) is not _SourceContext or _lookup(_CONTEXTS, context) is None:
        return _capture(None, operation, method, "", None, None)
    from .caller_operation import caller_for_http
    from .publication_recovery import _publication_transport

    transport = _publication_transport(http)
    url = getattr(transport, "api_url", "") + path
    raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
    return _capture(
        caller_for_http(http),
        operation,
        method,
        url,
        _digest(raw),
        (repository, None, head_sha),
        context=context,
    )


def _dispatch(caller: Any, request: Any, observation: dict[str, Any]) -> None:
    """Called after the SAME original dispatch debit, before physical I/O."""
    capture = _ACTIVE.get(caller)
    if capture is None or capture.closed or request.get_method() != capture.method:
        return
    if _digest(request.full_url.encode()) != capture.url_digest:
        return
    raw = request.data or b""
    if not isinstance(raw, bytes) or len(raw) > _INSPECTION_BYTES:
        return
    if capture.broker is not None:
        try:
            body = json.loads(raw)
            authority = body["session_attestation"]
            if (
                body.get("capability") != "check_publish"
                or authority != capture.broker.authority
            ):
                return
            target = (
                authority["repository"],
                authority["pull_request"],
                authority["head_sha"],
            )
            if (
                not isinstance(target[0], str)
                or type(target[1]) is not int
                or not isinstance(target[2], str)
            ):
                return
        except (KeyError, TypeError, ValueError):
            return
        capture.target = target
    elif _digest(raw) != capture.body_digest:
        return
    capture.matches += 1
    capture.observation = observation
    capture.request_id = observation["request_id"]
    capture.deadline = observation["deadline_unix_ms"]


def _response(caller: Any, observation: dict[str, Any], status: Any) -> None:
    """Called only after existing response and original deadline validation."""
    capture = _ACTIVE.get(caller)
    if (
        capture is not None
        and not capture.closed
        and capture.observation is observation
    ):
        capture.status = (
            status if type(status) is int and status in {401, 403} else None
        )


def _failure_capture(failure: Any) -> _Capture | None:
    proof = _lookup(_FAILURES, failure)
    if proof is None or type(failure) is not CheckFailure:
        return None
    capture, fields = proof
    return (
        capture
        if fields == (failure.code, failure.operation, failure.status)
        and capture.valid()
        else None
    )


def known_failure(failure: Any) -> bool:
    return _failure_capture(failure) is not None


def confirm_cleanup(publication: Any) -> None:
    # Called only by the source's completed/no-selected-work cleanup branch.
    _remember(_CLEANUPS, publication, _fingerprint(publication))


def _same_business(before: Any, after: Any) -> bool:
    from .action_completion import result_document

    left, right = result_document(before), result_document(after)
    left.pop("shadow", None)
    right.pop("shadow", None)
    return left == right


def _confirm_publication_projection(
    source_result: Any,
    projected_result: Any,
    *,
    repository: str,
    pull_request: int,
    head_sha: str,
) -> None:
    """Bind the trusted publisher's display projection to its exact input object.

    This is process-local evidence only. It does not authenticate caller-supplied
    serialized results, confer approval, or replace physical rejection receipts.
    The caller is the real publisher immediately after its validated projection.
    """
    from review_sensei.models import ReviewResult

    from .publication import review_result_digest

    if (
        type(source_result) is not ReviewResult
        or type(projected_result) is not ReviewResult
    ):
        return
    transaction = source_result.transaction
    if (
        transaction is None
        or projected_result.transaction not in (None, transaction)
        or transaction.repository != repository
        or transaction.pull_request != pull_request
        or transaction.head_sha != head_sha
    ):
        return
    _remember(
        _PROJECTIONS,
        source_result,
        (
            source_result.content_digest(),
            FinalizationSubject(
                repository,
                pull_request,
                head_sha,
                review_result_digest(projected_result),
            ),
        ),
    )


def _publication_subject(source_result, repository, pull_request, head_sha):
    from .publication import review_result_digest

    projection = _lookup(_PROJECTIONS, source_result)
    if projection is not None:
        digest, subject = projection
        if digest == source_result.content_digest() and (
            subject.repository,
            subject.pull_request,
            subject.head_sha,
        ) == (repository, pull_request, head_sha):
            return subject
    return FinalizationSubject(
        repository, pull_request, head_sha, review_result_digest(source_result)
    )


def confirm_denied_publication(
    publication,
    *,
    approval,
    failure,
    cleanup,
    review_id,
    http,
    source_result,
    repository,
    pull_request,
    head_sha: str,
) -> None:
    from review_sensei.models import ReviewResult

    from .caller_operation import caller_for_http

    if type(source_result) is not ReviewResult:
        return
    capture = _failure_capture(failure)
    caller = caller_for_http(http)
    transaction = source_result.transaction
    if (
        capture is None
        or caller is None
        or capture.caller() is not caller
        or not capture.valid()
        or transaction is None
        or caller.accounting.owner.run_attempt != 1
        or capture.context.source_result() is not source_result
        or publication.status != "published"
        or publication.diagnostic != failure.code
        or publication.thread_resolution is not None
        or not approval_confirmed(approval)
        or cleanup is None
        or _lookup(_CLEANUPS, cleanup) != _fingerprint(cleanup)
        or not _same_business(cleanup, publication)
        or type(review_id) is not int
        or review_id <= 0
        or publication.review_id != review_id
        or approval.finalization_completion.subject
        != _publication_subject(source_result, repository, pull_request, head_sha)
        or capture.target
        not in {(repository, pull_request, head_sha), (repository, None, head_sha)}
        or capture.transaction
        != (
            repository,
            pull_request,
            transaction.base_sha,
            head_sha,
            transaction.transaction_id,
            source_result.content_digest(),
        )
    ):
        return
    subject = (
        repository,
        pull_request,
        head_sha,
        transaction.transaction_id,
        source_result.content_digest(),
    )
    _remember(_PUBLICATIONS, publication, (capture, subject, _fingerprint(publication)))


def denied_accounting_details(
    publication: Any, caller: Any, identity: Any, source_result: Any
) -> dict[str, Any] | None:
    from review_sensei.models import ReviewResult

    if (
        type(source_result) is not ReviewResult
        or identity.repository_id != caller.run.repository_id
    ):
        return None
    proof = _lookup(_PUBLICATIONS, publication)
    transaction = source_result.transaction
    if proof is None or transaction is None:
        return None
    capture, subject, fingerprint = proof
    if (
        capture.caller() is not caller
        or not capture.valid()
        or fingerprint != _fingerprint(publication)
        or subject
        != (
            identity.repository,
            identity.pull_request,
            transaction.head_sha,
            transaction.transaction_id,
            source_result.content_digest(),
        )
    ):
        return None
    return {
        "code": "check_access_denied",
        "operation": capture.operation,
        "status": capture.status,
    }
