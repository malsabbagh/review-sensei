"""Exact validated publication result retention, independent of inference receipts.

Only the closed ReviewResult document is stored. Raw diffs, prompts, provider
transcripts and configuration are never included. Part hashes are transport
integrity, not authority: every load requires the independently selected ledger
and caller's trusted scope, policy, configuration and evidence expectations.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.schemas import validate_public_document
from review_sensei.session import (
    SessionIdentity,
    SessionLedger,
    load_review_transaction_for_publication,
    mark_review_publication_unconfirmed,
)
from review_sensei.validation import DEFAULT_REVIEW_LIMITS

from .errors import (
    GitHubHTTPError,
    GitHubHTTPResponseTooLargeError,
    GitHubPublicationError,
)
from .http import GitHubHttp
from .operation_comments import (
    GitHubIssueCommentOperationPort,
    ResultPartStore,
    ensure_result_part_capacity,
)
from .operation_diagnostics import OperationObserver

# Metadata dies with the original budget. No response bytes, credentials or
# authority decisions are retained; distinct physical transports stay isolated.
_PAGINATION_OBSERVATIONS: WeakKeyDictionary = WeakKeyDictionary()
_NARRATIVE_CONFIRMATION = object()


@dataclass(frozen=True)
class ConfirmedNarrativeArchive:
    """Read-only confirmation of original narrative retention, not a grant."""

    identity: SessionIdentity
    base_sha: str
    head_sha: str
    transaction_id: str
    content_digest: str
    payload_digest: str
    summary_digest: str
    carrier_ids: tuple[int, ...]
    _confirmation: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._confirmation is _NARRATIVE_CONFIRMATION:
            object.__setattr__(
                self, "_confirmation", (_NARRATIVE_CONFIRMATION, self._fields_digest())
            )

    def _fields_digest(self) -> str:
        return hashlib.sha256(
            canonical_bytes(
                {
                    "repository": self.identity.repository,
                    "repository_id": self.identity.repository_id,
                    "pull_request": self.identity.pull_request,
                    "base_sha": self.base_sha,
                    "head_sha": self.head_sha,
                    "transaction_id": self.transaction_id,
                    "content_digest": self.content_digest,
                    "payload_digest": self.payload_digest,
                    "summary_digest": self.summary_digest,
                    "carrier_ids": list(self.carrier_ids),
                }
            )
        ).hexdigest()

    def excerpt(
        self,
        result: ReviewResult,
        *,
        repository: str,
        repository_id: int,
        pull_request: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        """Render a labelled UTF-8 excerpt with every confirmed carrier link."""
        transaction = result.transaction
        if (
            self._confirmation != (_NARRATIVE_CONFIRMATION, self._fields_digest())
            or self.identity != SessionIdentity(repository, pull_request, repository_id)
            or self.base_sha != base_sha
            or self.head_sha != head_sha
            or transaction is None
            or transaction.transaction_id != self.transaction_id
            or result.content_digest() != self.content_digest
            or hashlib.sha256(result.summary.encode("utf-8")).hexdigest()
            != self.summary_digest
            or not self.carrier_ids
            or len(self.carrier_ids) > 32
            or len(set(self.carrier_ids)) != len(self.carrier_ids)
            or any(
                type(value) is not int or not 0 < value < 2**63
                for value in self.carrier_ids
            )
        ):
            raise ReviewInputError("retained narrative confirmation conflicts")
        links = ", ".join(
            f"[part {index + 1}]"
            f"(https://github.com/{repository}/pull/{pull_request}#issuecomment-{identifier})"
            for index, identifier in enumerate(self.carrier_ids)
        )
        prefix = (
            "Excerpt; the complete original provider narrative remains in the "
            "authenticated immutable result parts. Finding explanations and "
            "approval requirements are unchanged.\n\n"
        )
        suffix = (
            "…\n\n"
            f"Retained content SHA-256: `{self.content_digest}`. "
            f"Carrier payload SHA-256: `{self.payload_digest}`.\n\n{links}"
        )
        original = result.summary.encode("utf-8")
        available = len(original) - len((prefix + suffix).encode("utf-8")) - 1
        if available < 1:
            raise ReviewInputError("retained narrative framing cannot shrink overview")
        excerpt = original[: min(1024, available)].decode("utf-8", errors="ignore")
        return prefix + excerpt + suffix


def confirm_retained_narrative(
    *,
    http: GitHubHttp,
    token: str,
    result: ReviewResult,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    app_slug: str,
    ledger: SessionLedger,
    configuration_context: Mapping[str, object],
    evidence_context: Mapping[str, object],
    policy: ReviewConvergencePolicy,
    budget: EvidenceReadBudget,
) -> ConfirmedNarrativeArchive:
    """Confirm existing exact result parts without writing or reopening work.

    Use the caller's original shared budget. A changed ledger, missing part,
    foreign producer or unconfirmed payload cannot authorize compaction.
    """
    if not isinstance(budget, EvidenceReadBudget):
        raise ReviewInputError("retained narrative requires the original read budget")
    budget.check()
    before = ledger.load(identity)
    if before.status != "ok" or before.record is None:
        raise ReviewInputError("retained narrative ledger is unavailable")
    validated = _validate(
        result,
        ledger=ledger,
        identity=identity,
        base_sha=base_sha,
        head_sha=head_sha,
        configuration_context=configuration_context,
        evidence_context=evidence_context,
        policy=policy,
        loaded=before,
    )
    assert validated.transaction is not None
    store = _store(
        publication_http(http, budget),
        token=token,
        identity=identity,
        app_slug=app_slug,
        base_sha=base_sha,
        head_sha=head_sha,
        transaction=validated.transaction,
        source_app_id=getattr(ledger, "authenticated_app_id", None),
    )
    available = store.read_available_with_carriers(
        event_key=_event_key(identity, validated.transaction)
    )
    if available is None:
        raise ReviewInputError("retained narrative original parts are missing")
    digest, payload, carriers = available
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ReviewInputError("retained narrative result is invalid")
    retained = ReviewResult.from_dict(document)
    if (
        _payload(retained, projected=store.last_read_was_projection) != payload
        or retained.content_digest() != validated.content_digest()
        or retained.summary != result.summary
        or retained.transaction is None
        or retained.transaction.transaction_id != validated.transaction.transaction_id
    ):
        raise ReviewInputError("retained narrative original result conflicts")
    after = ledger.load(identity)
    if (
        after.status != "ok"
        or after.record is None
        or after.record.record_sha256 != before.record.record_sha256
    ):
        raise ReviewInputError("retained narrative ledger changed during confirmation")
    budget.check()
    return ConfirmedNarrativeArchive(
        identity=identity,
        base_sha=base_sha,
        head_sha=head_sha,
        transaction_id=validated.transaction.transaction_id,
        content_digest=validated.content_digest(),
        payload_digest=digest,
        summary_digest=hashlib.sha256(result.summary.encode("utf-8")).hexdigest(),
        carrier_ids=carriers,
        _confirmation=_NARRATIVE_CONFIRMATION,
    )


def _page_scope(path: str) -> tuple[str, int, int] | None:
    parsed = urlsplit(path)
    query = parse_qs(parsed.query)
    sizes, pages = query.pop("per_page", []), query.pop("page", [])
    if len(sizes) != 1 or len(pages) != 1:
        return None
    if not sizes[0].isdigit() or not pages[0].isdigit():
        return None
    size, page = int(sizes[0]), int(pages[0])
    if not 1 <= size <= 100 or not 1 <= page <= 1000:
        return None
    scope = parsed.path + "?" + urlencode(sorted(query.items()), doseq=True)
    return scope, (page - 1) * size, size


def _publication_transport(http: GitHubHttp) -> GitHubHttp:
    """Identity of an audited wrapper stack; never a response or authority proof."""
    from .human_assessment import FeedbackBudgetHttp

    while type(http) in {
        PublicationBudgetHttp,
        _PaginationObservationHttp,
        FeedbackBudgetHttp,
    }:
        http = getattr(http, "transport")
    return http


class _PaginationObservationHttp(GitHubHttp):
    """Observe oversized pages without owning request accounting."""

    def __init__(self, http: GitHubHttp, budget: EvidenceReadBudget) -> None:
        super().__init__(api_url=http.api_url, timeout=getattr(http, "timeout", 30))
        self.transport = http
        # Preserve the existing one-shot operation-entry handoff. This adapter
        # adds no response cache and must not silently discard an admitted list.
        self.primed_issue_comments = http.primed_issue_comments
        self.observation_budget = budget
        from .human_assessment import FeedbackBudgetHttp

        base = http
        while type(base) in {
            PublicationBudgetHttp,
            _PaginationObservationHttp,
            FeedbackBudgetHttp,
        }:
            base = getattr(base, "transport")
        operation = _PAGINATION_OBSERVATIONS.setdefault(budget, WeakKeyDictionary())
        self._oversized_page_ceilings = operation.setdefault(base, {})

    def pagination_page_size(self, path: str, offset: int = 0) -> int:
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        query.pop("per_page", None)
        query.pop("page", None)
        scope = parsed.path + "?" + urlencode(sorted(query.items()), doseq=True)
        return self._oversized_page_ceilings.get((self.api_url, scope, offset), 100)

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        scope = _page_scope(path) if method == "GET" else None
        key = (self.api_url, *scope[:2]) if scope is not None else None
        try:
            response = self.transport.request(
                method, path, token=token, body=body, timeout_seconds=timeout_seconds
            )
        except GitHubHTTPResponseTooLargeError:
            if key is not None and scope is not None:
                self._oversized_page_ceilings[key] = min(
                    max(1, scope[2] - 1), self._oversized_page_ceilings.get(key, 100)
                )
            raise
        return response


class PublicationBudgetHttp(GitHubHttp):
    """Charge each physical publication request once against one original budget."""

    def __init__(
        self,
        http: GitHubHttp,
        budget: EvidenceReadBudget,
        *,
        observer: OperationObserver | None = None,
    ) -> None:
        from .human_assessment import FeedbackBudgetHttp

        super().__init__(api_url=http.api_url, timeout=getattr(http, "timeout", 30))
        self.primed_issue_comments = http.primed_issue_comments
        self.publication_budget = budget
        self.operation_observer = observer
        accounting_http = http
        while type(accounting_http) is _PaginationObservationHttp:
            if accounting_http.observation_budget is not budget:
                raise GitHubPublicationError("publication budget identity mismatches")
            accounting_http = accounting_http.transport
        callback = getattr(accounting_http, "before_read", None)
        known_wrapper = type(accounting_http) is PublicationBudgetHttp
        wrapped_budget = (
            getattr(accounting_http, "publication_budget", None)
            if known_wrapper
            else None
        )
        known_feedback = type(accounting_http) is FeedbackBudgetHttp
        if (known_wrapper and wrapped_budget is not budget) or (
            known_feedback and callback != budget.consume
        ):
            raise GitHubPublicationError("publication budget identity mismatches")
        self._already_charged = known_wrapper or known_feedback
        self.transport = _PaginationObservationHttp(http, budget)

    def pagination_page_size(self, path: str, offset: int = 0) -> int:
        """Reuse only oversized-request ceilings at this exact item offset."""
        return self.transport.pagination_page_size(path, offset)

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        budget = self.publication_budget
        observer = self.operation_observer
        if observer is not None:
            observer.request(method, path)
        try:
            budget.check()
            remaining = (
                budget._remaining_seconds()
                if self._already_charged
                else budget.consume()
            )
            response = self.transport.request(
                method,
                path,
                token=token,
                body=body,
                timeout_seconds=min(remaining, timeout_seconds)
                if timeout_seconds is not None
                else remaining,
            )
            if observer is not None:
                observer.response(*response)
            budget.check()
            return response
        except (ReviewInputError, GitHubHTTPError, GitHubPublicationError) as exc:
            if observer is not None:
                observer.exception(exc)
            raise


def publication_http(
    http: GitHubHttp,
    budget: EvidenceReadBudget,
    *,
    observer: OperationObserver | None = None,
) -> PublicationBudgetHttp:
    if type(http) is PublicationBudgetHttp:
        if http.publication_budget is not budget:
            raise GitHubPublicationError("publication budget identity mismatches")
        if observer is None:
            return http
        observed = copy(http)
        observed.operation_observer = observer
        return observed
    return PublicationBudgetHttp(http, budget, observer=observer)


def publication_session_ledger(
    ledger: SessionLedger, budget: EvidenceReadBudget
) -> SessionLedger:
    """Bind actual remote ledger dispatches to the same publication allowance.

    This ledger already charges physical requests itself. Remove only audited
    wrappers charging that exact budget, without mutating the caller's ledger.
    """
    from .human_assessment import FeedbackBudgetHttp
    from .session_ledger import GitHubIssueCommentSessionLedger

    if not isinstance(ledger, GitHubIssueCommentSessionLedger):
        return ledger
    if ledger.evidence_budget is not None and ledger.evidence_budget is not budget:
        raise GitHubPublicationError("publication ledger budget identity mismatches")
    if (
        type(ledger.http) is _PaginationObservationHttp
        and ledger.http.observation_budget is budget
        and ledger.evidence_budget is budget
        and type(ledger.http.transport)
        not in {
            PublicationBudgetHttp,
            FeedbackBudgetHttp,
            _PaginationObservationHttp,
        }
    ):
        return ledger
    copied = copy(ledger)
    transport = copied.http
    while type(transport) in {
        PublicationBudgetHttp,
        FeedbackBudgetHttp,
        _PaginationObservationHttp,
    }:
        if type(transport) is PublicationBudgetHttp:
            if transport.publication_budget is not budget:
                raise GitHubPublicationError(
                    "publication ledger budget identity mismatches"
                )
        elif type(transport) is _PaginationObservationHttp:
            if transport.observation_budget is not budget:
                raise GitHubPublicationError(
                    "publication ledger budget identity mismatches"
                )
        elif getattr(transport, "before_read") != budget.consume:
            raise GitHubPublicationError(
                "publication ledger budget identity mismatches"
            )
        transport = getattr(transport, "transport")
    copied.http = _PaginationObservationHttp(transport, budget)
    copied.evidence_budget = budget
    return copied


class _RecoveryReadHttp(GitHubHttp):
    """Only metadata GETs are permitted while the explicit phase is active."""

    def __init__(self, transport: GitHubHttp, phase: PublicationRecoveryReadPhase):
        super().__init__(api_url=transport.api_url, timeout=transport.timeout)
        self.transport, self.phase = transport, phase

    def request(self, method, path, *, token, body=None, timeout_seconds=None):
        if not self.phase.active or method != "GET" or token != self.phase._token:
            raise ReviewInputError("publication recovery phase is read-only or closed")
        self.phase._budget.check()
        return self.transport.request(
            method, path, token=token, body=body, timeout_seconds=timeout_seconds
        )


class PublicationRecoveryReadPhase:
    """One lossless complete list for context selection and raw result parsing.

    This is a short read-only phase, not publication authority. Its list is never
    obtained from the ledger's filtered/deduplicated cache and is destroyed on
    exit. Ordinary mutable ledger fences remain fresh; later publication uses
    its separately selected grant/token and fresh storage scans.
    """

    def __init__(
        self,
        *,
        http: GitHubHttp,
        token: str,
        ledger: SessionLedger,
        identity: SessionIdentity,
        base_sha: str,
        head_sha: str,
        app_slug: str,
        budget: EvidenceReadBudget,
    ) -> None:
        from .session_ledger import GitHubIssueCommentSessionLedger

        if type(ledger) is not GitHubIssueCommentSessionLedger:
            raise ReviewInputError(
                "publication recovery phase requires a fresh GitHub ledger"
            )
        if (
            not isinstance(budget, EvidenceReadBudget)
            or ledger.evidence_budget not in (None, budget)
            or ledger.token != token
            or not isinstance(token, str)
            or not token.strip()
            or _publication_transport(ledger.http) is not _publication_transport(http)
            or ledger.app_slug is None
            or ledger.app_slug.casefold() != app_slug.casefold()
            or ledger._broker is not None
            or ledger._request_started
            or ledger._inventory_complete
            or ledger._comments_by_id
            or ledger._verified_parts
            or ledger._activation_ticket is not None
            or ledger._tail_preparing
            or ledger._history_reading
            or identity.repository_id is None
        ):
            raise ReviewInputError(
                "publication recovery phase ledger or scope conflicts"
            )
        self._path = http.repository_path(
            identity.repository, f"/issues/{identity.pull_request}/comments"
        )
        if any(
            self._path in owner.primed_issue_comments
            for owner in (http, ledger.http, _publication_transport(http))
        ):
            raise ReviewInputError(
                "publication recovery phase cannot use primed comments"
            )
        if (
            re.fullmatch(r"[a-f0-9]{40}", base_sha) is None
            or re.fullmatch(r"[a-f0-9]{40}", head_sha) is None
            or re.fullmatch(r"[A-Za-z0-9_-]+\[bot\]", app_slug, re.I | re.ASCII) is None
        ):
            raise ReviewInputError("publication recovery phase scope is invalid")
        self._budget, self._token = budget, token
        self._transport = _publication_transport(http)
        self._scope = (identity, base_sha, head_sha, app_slug.casefold())
        self._accounted_http = publication_http(http, budget)
        adapted = publication_session_ledger(ledger, budget)
        assert isinstance(adapted, GitHubIssueCommentSessionLedger)
        self.ledger = adapted
        self._normal_http = self.ledger.http
        self._snapshot: bytes | None = None
        self._claimed = False
        self.active = True
        self._read_http = _RecoveryReadHttp(self._transport, self)
        self.ledger.http = self._read_http

    def _acquire(self) -> None:
        self._budget.check()
        records = self._accounted_http.paginate(
            path=self._path,
            token=self._token,
            page_sizes=(100, 50, 20, 10, 5, 1),
            max_requests=10,
            timeout_seconds=60,
        )
        # Preserve every original item and duplicate identity. No ledger cache
        # projection, partial prefix, or ID-based normalization supplies this.
        self._snapshot = canonical_bytes(records)
        self._read_http.primed_issue_comments[self._path] = json.loads(self._snapshot)

    def _claim(
        self,
        *,
        http: GitHubHttp,
        token: str,
        ledger: SessionLedger,
        identity: SessionIdentity,
        base_sha: str,
        head_sha: str,
        app_slug: str,
        budget: EvidenceReadBudget,
    ) -> list[Any]:
        self._budget.check()
        if (
            not self.active
            or self._snapshot is None
            or self._claimed
            or budget is not self._budget
            or token != self._token
            or ledger is not self.ledger
            or self.ledger.http is not self._read_http
            or _publication_transport(http) is not self._transport
            or (identity, base_sha, head_sha, app_slug.casefold()) != self._scope
            or getattr(ledger, "evidence_budget", None) is not budget
            or getattr(ledger, "token", None) != token
        ):
            raise ReviewInputError("publication recovery phase is spent or conflicts")
        self._claimed = True
        return json.loads(self._snapshot)

    def close(self) -> None:
        self.active = False
        self._snapshot = None
        self._read_http.primed_issue_comments.clear()
        # Existing parser state may survive only through the ordinary metadata
        # ledger. Its next load performs normal fresh root/tail validation.
        self.ledger.http = self._normal_http


@contextmanager
def publication_recovery_read_phase(
    *,
    http: GitHubHttp,
    token: str,
    ledger: SessionLedger,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    app_slug: str,
    budget: EvidenceReadBudget,
) -> Iterator[PublicationRecoveryReadPhase]:
    """Explicit supported phase; generic/local ledgers retain their usual path."""
    phase = PublicationRecoveryReadPhase(
        http=http,
        token=token,
        ledger=ledger,
        identity=identity,
        base_sha=base_sha,
        head_sha=head_sha,
        app_slug=app_slug,
        budget=budget,
    )
    try:
        phase._acquire()
        yield phase
    finally:
        phase.close()


def _payload(result: ReviewResult, *, projected: bool = False) -> bytes:
    document = result.to_dict()
    validate_public_document(document, "review-result")
    restored = ReviewResult.from_dict(document)
    if restored.to_dict() != document:
        raise ReviewInputError("retained result is not canonical")
    payload = canonical_bytes(document)
    if projected:
        if len(payload) > DEFAULT_REVIEW_LIMITS.max_result_bytes:
            raise ReviewInputError(
                "retained logical result exceeds the core result limit"
            )
    else:
        ensure_result_part_capacity(len(payload))
    return payload


def _event_key(identity: SessionIdentity, transaction: ReviewTransaction) -> str:
    scope = hashlib.sha256(
        canonical_bytes(
            {
                "kind": "publication-result-v1",
                "repository": identity.repository,
                "repository_id": identity.repository_id,
                "pull_request": identity.pull_request,
            }
        )
    ).hexdigest()
    event = hashlib.sha256(
        canonical_bytes(
            {
                "transaction_id": transaction.transaction_id,
                "result_sha256": transaction.result_sha256,
            }
        )
    ).hexdigest()
    return f"{scope}\t{event}"


def _store(
    http: GitHubHttp,
    *,
    token: str,
    identity: SessionIdentity,
    app_slug: str,
    base_sha: str,
    head_sha: str,
    primed_comments: list[Any] | None = None,
    transaction: ReviewTransaction | None = None,
    source_app_id: int | None = None,
    compact_write: bool = False,
) -> ResultPartStore:
    if (
        not isinstance(app_slug, str)
        or re.fullmatch(
            r"[A-Za-z0-9_-]+\[bot\]", app_slug, flags=re.IGNORECASE | re.ASCII
        )
        is None
    ):
        raise ReviewInputError("retention requires the configured Bot login")
    status, user = http.request(
        "GET", "/users/" + quote(app_slug, safe=""), token=token
    )
    user_id = user.get("id") if isinstance(user, dict) else None
    if (
        status != 200
        or not isinstance(user, dict)
        or not isinstance(user.get("login"), str)
        or user["login"].casefold() != app_slug.casefold()
        or user.get("type") != "Bot"
        or type(user_id) is not int
        or user_id <= 0
    ):
        raise ReviewInputError("retention Bot identity is unavailable")
    status, pr = http.request(
        "GET",
        http.repository_path(identity.repository, f"/pulls/{identity.pull_request}"),
        token=token,
    )
    base = pr.get("base") if isinstance(pr, dict) else None
    head = pr.get("head") if isinstance(pr, dict) else None
    repo = base.get("repo") if isinstance(base, dict) else None
    if (
        status != 200
        or not isinstance(pr, dict)
        or pr.get("state") != "open"
        or not isinstance(base, dict)
        or base.get("sha") != base_sha
        or not isinstance(head, dict)
        or head.get("sha") != head_sha
        or not isinstance(repo, dict)
        or type(repo.get("id")) is not int
        or repo.get("id") != identity.repository_id
        or repo.get("full_name") != identity.repository
    ):
        raise ReviewInputError("retention current pull request identity mismatches")
    port = GitHubIssueCommentOperationPort(
        http,
        token=token,
        repository=identity.repository,
        pull_request=identity.pull_request,
        app_user_id=user_id,
        strict_association=True,
    )
    if primed_comments is not None:
        port.prime(primed_comments)
    from .checkpoint_result_store import CheckpointResultStore

    return CheckpointResultStore(
        port,
        app_user_id=user_id,
        http=http,
        identity=identity,
        transaction=transaction,
        app_id=source_app_id,
        compact_write=compact_write,
    )


def _validate(
    result: ReviewResult,
    *,
    ledger: SessionLedger,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    configuration_context: Mapping[str, object],
    evidence_context: Mapping[str, object],
    policy: ReviewConvergencePolicy,
    loaded: Any = None,
) -> ReviewResult:
    _payload(
        result, projected=bool(getattr(ledger, "_quarantine_capacity_cursor", False))
    )
    record = load_review_transaction_for_publication(
        ledger,
        identity,
        result,
        base_sha=base_sha,
        head_sha=head_sha,
        policy_digest=policy.digest(),
        configuration_digest=ReviewTransaction.compute_configuration_digest(
            configuration_context
        ),
        evidence_digest=ReviewTransaction.compute_evidence_digest(evidence_context),
        loaded=loaded,
    )
    if (
        record.transaction is None
        or record.transaction.result_sha256 != result.content_digest()
    ):
        raise ReviewInputError("retained result has no durable checkpoint")
    if record.repository_id != identity.repository_id:
        raise ReviewInputError("retention ledger repository identity mismatches")
    return replace(result, transaction=record.transaction)


def recover_retained_review_result(
    *,
    http: GitHubHttp,
    token: str,
    repository: str,
    repository_id: int,
    pull_request: int,
    base_sha: str,
    head_sha: str,
    app_slug: str,
    ledger: SessionLedger,
    configuration_context: Mapping[str, object],
    evidence_context: Mapping[str, object],
    convergence_policy: ReviewConvergencePolicy | None = None,
    budget: EvidenceReadBudget | None = None,
    recovery_phase: PublicationRecoveryReadPhase | None = None,
) -> ReviewResult:
    """Recover exact owned bytes using an independently selected durable checkpoint."""
    try:
        budget = budget or EvidenceReadBudget()
        budget.check()
        identity = SessionIdentity(
            repository, pull_request, repository_id=repository_id
        )
        primed_comments = None
        if recovery_phase is not None:
            if type(recovery_phase) is not PublicationRecoveryReadPhase:
                raise ReviewInputError("publication recovery phase is invalid")
            primed_comments = recovery_phase._claim(
                http=http,
                token=token,
                ledger=ledger,
                identity=identity,
                base_sha=base_sha,
                head_sha=head_sha,
                app_slug=app_slug,
                budget=budget,
            )
        ledger = publication_session_ledger(ledger, budget)
        loaded = ledger.load(identity)
        if loaded.status not in {"ok", "migrated"} or loaded.record is None:
            raise ReviewInputError("retention ledger is unavailable")
        transaction = loaded.record.transaction
        if (
            transaction is None
            or transaction.result_sha256 is None
            or transaction.phase == "analysis"
        ):
            raise ReviewInputError("retention requires a durable result checkpoint")
        transport = publication_http(http, budget)
        store = _store(
            transport,
            token=token,
            identity=identity,
            app_slug=app_slug,
            base_sha=base_sha,
            head_sha=head_sha,
            primed_comments=primed_comments,
            transaction=transaction,
            source_app_id=getattr(ledger, "authenticated_app_id", None),
        )
        available = store.read_available(event_key=_event_key(identity, transaction))
        if available is None:
            raise ReviewInputError(
                "retained result is missing; historical result bytes do not exist for "
                f"transaction {transaction.transaction_id}; do not reconstruct from an "
                "old head; an authorized maintainer (maintain/admin) must comment "
                "'@reviewsensei review reenroll' to record an irrecoverable-transaction "
                "successor transition"
            )
        _digest, payload = available
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ReviewInputError("retained result is invalid")
        result = ReviewResult.from_dict(document)
        if _payload(result, projected=store.last_read_was_projection) != payload:
            raise ReviewInputError("retained result is not canonical")
        return _validate(
            result,
            ledger=ledger,
            identity=identity,
            base_sha=base_sha,
            head_sha=head_sha,
            configuration_context=configuration_context,
            evidence_context=evidence_context,
            policy=convergence_policy or ReviewConvergencePolicy(),
        )
    except (ValueError, ReviewInputError) as exc:
        raise GitHubPublicationError(
            "publication result recovery refused: " + str(exc)
        ) from exc


def retain_checkpoint_review_result(
    *,
    http: GitHubHttp,
    token: str,
    result: ReviewResult,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    app_slug: str,
    pending_transaction: ReviewTransaction,
    configuration_context: Mapping[str, object] | None = None,
    evidence_context: Mapping[str, object] | None = None,
    policy: ReviewConvergencePolicy | None = None,
    source_app_id: int | None = None,
) -> ReviewResult:
    """Durably retain and readback-verify canonical result bytes before committing publication_pending."""
    try:
        effective_policy = policy or ReviewConvergencePolicy()
        expected_config_digest = (
            ReviewTransaction.compute_configuration_digest(configuration_context)
            if configuration_context is not None
            else pending_transaction.configuration_digest
        )
        expected_evidence_digest = (
            ReviewTransaction.compute_evidence_digest(evidence_context)
            if evidence_context is not None
            else pending_transaction.evidence_digest
        )
        if not pending_transaction.identity_matches(
            repository=identity.repository,
            pull_request=identity.pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            policy_digest=effective_policy.digest(),
            configuration_digest=expected_config_digest,
            evidence_digest=expected_evidence_digest,
        ):
            raise ReviewInputError(
                "review transaction identity or trusted context mismatches"
            )
        result_digest = result.content_digest()
        if pending_transaction.result_sha256 != result_digest:
            raise ReviewInputError("review result digest does not match transaction")
        bound = replace(result, transaction=pending_transaction)
        projected = (
            getattr(_publication_transport(http), "_quarantine_capacity_guard", None)
            is not None
        )
        payload = _payload(bound, projected=projected)
        event_key = _event_key(identity, pending_transaction)
        store = _store(
            http,
            token=token,
            identity=identity,
            app_slug=app_slug,
            base_sha=base_sha,
            head_sha=head_sha,
            transaction=pending_transaction,
            source_app_id=source_app_id,
            compact_write=True,
        )
        available = store.read_available(event_key=event_key)
        if available is None:
            parts = store.physical_part_count(payload)
            budget = getattr(http, "publication_budget", None)
            if isinstance(budget, EvidenceReadBudget):
                from .api_run_budget import CallerRunBudget
                from .api_run_plan import READ_OPERATION_REQUESTS

                liability = 2 * parts + (
                    READ_OPERATION_REQUESTS
                    if isinstance(budget, CallerRunBudget)
                    else 30
                )
                if liability > budget.ordinary_remaining():
                    raise ReviewInputError(
                        "retention request liability exceeds the original budget"
                    )
            try:
                digest = store.write(
                    event_key=event_key, result=payload, _check_existing=False
                )
            except Exception:
                available = store.read_available(event_key=event_key)
                if available is None or available[1] != payload:
                    raise ReviewInputError(
                        "retention write outcome unknown; recover the existing exact parts"
                    ) from None
                digest = available[0]
            if store.read(event_key=event_key, payload_digest=digest) != payload:
                raise ReviewInputError("retention readback mismatches")
        else:
            _digest, existing_payload = available
            document = json.loads(existing_payload)
            if not isinstance(document, dict):
                raise ReviewInputError("retained result is invalid")
            prior = ReviewResult.from_dict(document)
            if (
                _payload(prior, projected=store.last_read_was_projection)
                != existing_payload
                or prior.content_digest() != bound.content_digest()
            ):
                raise ReviewInputError("retained result differs from checkpoint")
        return bound
    except (ValueError, ReviewInputError) as exc:
        raise GitHubPublicationError(
            "publication result not retained: " + str(exc)
        ) from exc


def retain_review_result(
    *,
    http: GitHubHttp,
    token: str,
    result: ReviewResult,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    app_slug: str,
    ledger: SessionLedger,
    configuration_context: Mapping[str, object],
    evidence_context: Mapping[str, object],
    policy: ReviewConvergencePolicy,
    already_retained: bool = False,
    _loaded: Any = None,
) -> None:
    """Persist a prior independently validated checkpoint before publication writes."""
    try:
        validated = _validate(
            result,
            ledger=ledger,
            identity=identity,
            base_sha=base_sha,
            head_sha=head_sha,
            configuration_context=configuration_context,
            evidence_context=evidence_context,
            policy=policy,
            loaded=_loaded,
        )
        assert validated.transaction is not None
        if already_retained:
            if validated.transaction.phase == "publication_pending":
                mark_review_publication_unconfirmed(
                    ledger, identity, validated.transaction
                )
            return
        event_key = _event_key(identity, validated.transaction)
        store = _store(
            http,
            token=token,
            identity=identity,
            app_slug=app_slug,
            base_sha=base_sha,
            head_sha=head_sha,
            transaction=validated.transaction,
            source_app_id=getattr(ledger, "authenticated_app_id", None),
        )
        available = store.read_available(event_key=event_key)
        if available is None:
            if validated.transaction.phase != "publication_pending":
                raise ReviewInputError(
                    "retained result missing after unconfirmed publication; "
                    "do not recreate uncertain writes; use the original result "
                    "with the explicit operation-entry opt-out"
                )
            projected = (
                getattr(
                    _publication_transport(http), "_quarantine_capacity_guard", None
                )
                is not None
            )
            payload = _payload(validated, projected=projected)
            parts = store.physical_part_count(payload)
            budget = getattr(http, "publication_budget", None)
            if isinstance(budget, EvidenceReadBudget):
                # Before any part POST, admit every bounded listing/readback
                # and a possible lost-response reconciliation, without refill.
                from .api_run_budget import CallerRunBudget
                from .api_run_plan import READ_OPERATION_REQUESTS

                liability = 2 * parts + (
                    READ_OPERATION_REQUESTS
                    if isinstance(budget, CallerRunBudget)
                    else 30
                )
                if liability > budget.ordinary_remaining():
                    raise ReviewInputError(
                        "retention request liability exceeds the original budget"
                    )
            # A fresh owner may only reconcile this attempt. The existing
            # unconfirmed-publication phase is durable before the first POST,
            # including a crash that bypasses application exception cleanup.
            mark_review_publication_unconfirmed(ledger, identity, validated.transaction)
            try:
                digest = store.write(
                    event_key=event_key, result=payload, _check_existing=False
                )
            except Exception:
                # An uncertain POST may have succeeded. Read the complete owned
                # set once; never repeat the POST or create a new event identity.
                available = store.read_available(event_key=event_key)
                if available is None or available[1] != payload:
                    raise ReviewInputError(
                        "retention write outcome unknown; recover the existing exact parts"
                    ) from None
                digest = available[0]
            if store.read(event_key=event_key, payload_digest=digest) != payload:
                raise ReviewInputError("retention readback mismatches")
        else:
            _digest, payload = available
            document = json.loads(payload)
            if not isinstance(document, dict):
                raise ReviewInputError("retained result is invalid")
            prior = ReviewResult.from_dict(document)
            if (
                _payload(prior, projected=store.last_read_was_projection) != payload
                or prior.content_digest() != validated.content_digest()
            ):
                raise ReviewInputError("retained result differs from checkpoint")
            _validate(
                prior,
                ledger=ledger,
                identity=identity,
                base_sha=base_sha,
                head_sha=head_sha,
                configuration_context=configuration_context,
                evidence_context=evidence_context,
                policy=policy,
            )
            if validated.transaction.phase == "publication_pending":
                mark_review_publication_unconfirmed(
                    ledger, identity, validated.transaction
                )
    except (ValueError, ReviewInputError) as exc:
        raise GitHubPublicationError(
            "publication result not retained: " + str(exc)
        ) from exc
