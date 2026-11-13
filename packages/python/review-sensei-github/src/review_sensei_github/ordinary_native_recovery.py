"""Source-bound ordinary native readback, without refunding lost HTTP charges."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

_SOURCE: Any = None
_ACTIVE: WeakKeyDictionary = WeakKeyDictionary()
_CHECK_RESULTS: WeakKeyDictionary = WeakKeyDictionary()
_SEALS: WeakKeyDictionary = WeakKeyDictionary()
_READS: WeakKeyDictionary = WeakKeyDictionary()
_MUTATIONS: WeakKeyDictionary = WeakKeyDictionary()
_INVALID: WeakKeyDictionary = WeakKeyDictionary()
_CLOSED: WeakKeyDictionary = WeakKeyDictionary()
_PHYSICAL: WeakKeyDictionary = WeakKeyDictionary()


def _physical_identity(observation):
    return tuple(
        observation.get(key)
        for key in (
            "phase",
            "lifecycle_phase",
            "request_id",
            "deadline_unix_ms",
            "mutation",
        )
    )


class ReconciledCheck(Exception):
    """An opaque logical outcome, never an HTTP status or caller-created proof."""


def install_sources(caller_class, journal_class, check_class):
    global _SOURCE
    from .api_run_requests import ApiRunRequestTransport
    from .checks import ReviewCheckPublisher
    from .finding_lifecycle import FindingLifecycleJournal
    from .publication import _REVIEW_THREADS_QUERY

    if _SOURCE is not None or (caller_class, journal_class, check_class) != (
        ApiRunRequestTransport,
        FindingLifecycleJournal,
        ReviewCheckPublisher,
    ):
        raise ReviewInputError(
            "ordinary native recovery source is already installed or foreign"
        )
    _SOURCE = (
        caller_class,
        caller_class.open,
        journal_class,
        {
            name: getattr(journal_class, name)
            for name in ("_dispatch_effect", "_readback", "checkpoint")
        },
        check_class,
        {
            name: getattr(check_class, name)
            for name in ("_create", "_update", "_request")
        },
        _REVIEW_THREADS_QUERY,
    )


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _scope(caller):
    state = caller.accounting
    return state.identity, state.owner, state.configuration, state.startup


@dataclass(eq=False)
class _Capture:
    journal: Any
    caller: Any
    witness: Any
    effect: Any
    root: str
    scope: Any
    transaction: Any
    method: str
    path: str
    body_digest: str
    credential: str
    api_url: str
    mutation: Any = None
    reads: list[Any] = field(default_factory=list)
    invalid: bool = False
    readback_closed: bool = False
    result_id: int | None = None
    committed_root: str | None = None

    def seal(self):
        return (
            self.journal,
            self.caller,
            self.witness,
            id(self.effect),
            self.root,
            self.scope,
            self.transaction,
            self.method,
            self.path,
            self.body_digest,
            self.credential,
            self.api_url,
            canonical_bytes(
                {
                    key: value
                    for key, value in self.effect.items()
                    if key not in {"status", "response"}
                }
            ),
        )

    def require(self, *, committed=False):
        if _SOURCE is None or _SEALS.get(self) != self.seal():
            raise ReviewInputError(
                "ordinary native recovery source unavailable or changed"
            )
        (
            caller_type,
            opened,
            journal_type,
            journal_methods,
            check_type,
            check_methods,
            _thread_query,
        ) = _SOURCE
        journal, caller = self.journal, self.caller
        if (
            type(caller) is not caller_type
            or caller_type.open is not opened
            or getattr(caller.open, "__func__", None) is not opened
            or type(journal) is not journal_type
            or any(
                getattr(journal_type, name) is not method or name in journal.__dict__
                for name, method in journal_methods.items()
            )
            or type(journal.reviewer.checks) is not check_type
            or any(
                getattr(check_type, name) is not method
                or name in journal.reviewer.checks.__dict__
                for name, method in check_methods.items()
            )
            or journal.ledger.caller is not caller
            or journal._record.transaction != self.transaction
            or _scope(caller) != self.scope
            or not any(effect is self.effect for effect in journal.effects)
            or caller._root != journal._record.record_sha256
            or caller._root != (self.committed_root if committed else self.root)
            or any(
                _physical_identity(observation) != identity
                for observation, identity in _PHYSICAL.get(self, ())
            )
            or self.invalid
            or self in _INVALID
            or self.mutation is not _MUTATIONS.get(self)
            or tuple(self.reads) != _READS.get(self, ())
            or self.readback_closed != bool(_CLOSED.get(self))
        ):
            raise ReviewInputError("ordinary native recovery custody changed")
        self.witness.require(journal, journal.reviewer)


@contextmanager
def native_attempt(journal, effect, method, path, body, token):
    from .ordinary_publication_reads import issued_witness

    witness = issued_witness(journal)
    caller = getattr(journal.ledger, "caller", None)
    if witness is None or caller is None:
        yield None
        return
    if caller in _ACTIVE:
        raise ReviewInputError("ordinary native recovery capture is nested")
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    capture = _Capture(
        journal,
        caller,
        witness,
        effect,
        journal._record.record_sha256,
        _scope(caller),
        journal._record.transaction,
        method,
        path,
        _digest(raw),
        _digest(("Bearer " + token).encode()),
        journal.reviewer.http.api_url,
    )
    _SEALS[capture] = capture.seal()
    _READS[capture] = ()
    capture.require()
    _ACTIVE[caller] = capture
    try:
        yield capture
    finally:
        _ACTIVE.pop(caller, None)


def dispatch(caller, request, observation):
    """Observe only the same original debit, before its physical dispatch."""
    capture = _ACTIVE.get(caller)
    if capture is None or _CLOSED.get(capture):
        return
    capture.require()
    parsed = urlsplit(request.full_url)
    credential = _digest(str(request.get_header("Authorization")).encode())
    if observation.get("phase") != "ordinary" or credential != capture.credential:
        capture.invalid = True
        _INVALID[capture] = True
        raise ReviewInputError("ordinary native recovery physical request changed")
    method = request.get_method()
    if capture.mutation is None:
        if (
            method != capture.method
            or request.full_url != capture.api_url + capture.path
            or _digest(request.data or b"") != capture.body_digest
            or not observation.get("mutation")
        ):
            capture.invalid = True
            _INVALID[capture] = True
            raise ReviewInputError("ordinary native recovery physical request changed")
        capture.mutation = observation
        _MUTATIONS[capture] = observation
        _PHYSICAL[capture] = ((observation, _physical_identity(observation)),)
        return
    graph_read = False
    if method == "POST" and parsed.path == urlsplit(capture.api_url).path + "/graphql":
        try:
            body = json.loads(request.data or b"{}")
        except (ValueError, TypeError):
            body = {}
        owner, name = capture.journal.identity.repository.split("/", 1)
        graph_read = (
            isinstance(body, dict)
            and set(body) == {"operationName", "query", "variables"}
            and body.get("operationName") == "ReviewThreads"
            and body.get("query") == _SOURCE[-1]
            and body.get("variables")
            == {
                "owner": owner,
                "name": name,
                "number": capture.journal.identity.pull_request,
                "after": None,
                "first": 100,
            }
            and bool(capture.effect["request"].get("comments"))
        )
    if (
        (method != "GET" and not graph_read)
        or (observation.get("mutation") and not graph_read)
        or (parsed.scheme, parsed.netloc)
        != (urlsplit(capture.api_url).scheme, urlsplit(capture.api_url).netloc)
        or len(capture.reads) >= (3 if capture.effect["request"].get("comments") else 2)
    ):
        capture.invalid = True
        _INVALID[capture] = True
        raise ReviewInputError("ordinary native recovery physical request changed")
    captured = (parsed.path, parse_qs(parsed.query), observation)
    _PHYSICAL[capture] = (
        *_PHYSICAL[capture],
        (observation, _physical_identity(observation)),
    )
    capture.reads.append(captured)
    _READS[capture] = (*_READS[capture], captured)


def recover(capture, *, token):
    """Confirm an exact source readback and durably retain it before resuming."""
    if capture is None:
        return None
    capture.require()
    journal, caller, effect = capture.journal, capture.caller, capture.effect
    mutation = capture.mutation
    if (
        mutation is None
        or mutation.get("outcome") not in {"unknown", "expired"}
        or not any(item is mutation for item in caller.observations)
        or effect["status"] != "pending"
    ):
        return None
    response = journal._readback(journal._readback_http, effect, token=token)
    if response is None:
        return None
    capture.require()
    status, value = response
    identifier = value.get("id") if isinstance(value, dict) else None
    if status != 200 or type(identifier) is not int or identifier <= 0:
        return None
    api_path = urlsplit(capture.api_url).path
    prefix = api_path + f"/repos/{journal.identity.repository}"
    expected: tuple[tuple[str, dict[str, list[str]]], ...]
    if capture.path.endswith("/reviews"):
        expected = (
            (api_path + capture.path, {"per_page": ["100"], "page": ["1"]}),
            (
                api_path + capture.path + f"/{identifier}/comments",
                {"per_page": ["100"], "page": ["1"]},
            ),
        )
        if effect["request"].get("comments"):
            expected = (*expected, (api_path + "/graphql", {}))
    elif capture.method == "POST" and capture.path.endswith("/check-runs"):
        expected = (
            (
                prefix + f"/commits/{journal.head_sha}/check-runs",
                {"check_name": ["ReviewSensei"], "per_page": ["100"]},
            ),
        )
    elif capture.method == "PATCH" and "/check-runs/" in capture.path:
        expected = ((api_path + capture.path, {}),)
    else:
        return None
    if (
        tuple(capture.reads) != _READS.get(capture)
        or tuple((path, query) for path, query, _ in capture.reads) != expected
    ) or any(
        observation.get("outcome") != "response"
        or observation.get("uncertain_effect")
        or not any(item is observation for item in caller.observations)
        or int(caller._clock() * 1000) > observation["deadline_unix_ms"]
        for _, _, observation in capture.reads
    ):
        return None
    # The physical mutation remains unknown and charged. Only its source-owned
    # effect is confirmed by independently charged exact native readbacks.
    receipt = journal._compact_response(value, effect=effect)
    capture.readback_closed = True
    _CLOSED[capture] = True
    effect["status"], effect["response"] = "confirmed", [status, receipt]
    journal.checkpoint()
    capture.committed_root = journal._record.record_sha256
    capture.require(committed=True)
    caller._reconciled_mutations.add(str(mutation["request_id"]))
    if "/check-runs" in capture.path:
        capture.result_id = identifier
        proof = ReconciledCheck("source-confirmed check outcome")
        _CHECK_RESULTS[proof] = (
            capture,
            capture.committed_root,
            identifier,
            journal._record,
        )
        raise proof
    return response


def consume_check(proof, checks, *, method, path, body):
    issued = _CHECK_RESULTS.pop(proof, None)
    if issued is None:
        raise ReviewInputError(
            "ordinary reconciled check proof is unissued or consumed"
        )
    capture, root, identifier, record = issued
    if (
        capture.committed_root != root
        or capture.result_id != identifier
        or capture.journal._record is not record
    ):
        raise ReviewInputError("ordinary reconciled check proof custody changed")
    capture.require(committed=True)
    if (
        checks is not capture.journal.reviewer.checks
        or method != capture.method
        or path != capture.path
        or _digest(json.dumps(body, separators=(",", ":")).encode())
        != capture.body_digest
        or type(capture.result_id) is not int
        or capture.effect["status"] != "confirmed"
    ):
        raise ReviewInputError("ordinary reconciled check consumer changed")
    return capture.result_id
