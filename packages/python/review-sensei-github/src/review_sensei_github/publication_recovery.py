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
from copy import copy
from dataclasses import replace
from typing import Any, Mapping
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
    complete_review_publication,
    load_review_transaction_for_publication,
)

from .errors import GitHubHTTPResponseTooLargeError, GitHubPublicationError
from .http import GitHubHttp
from .operation_comments import (
    GitHubIssueCommentOperationPort,
    ResultPartStore,
    ensure_result_part_capacity,
)

# Metadata dies with the original budget. No response bytes, credentials or
# authority decisions are retained; distinct physical transports stay isolated.
_PAGINATION_OBSERVATIONS: WeakKeyDictionary = WeakKeyDictionary()


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

    def __init__(self, http: GitHubHttp, budget: EvidenceReadBudget) -> None:
        from .human_assessment import FeedbackBudgetHttp

        super().__init__(api_url=http.api_url, timeout=getattr(http, "timeout", 30))
        self.publication_budget = budget
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
        budget.check()
        remaining = (
            budget._remaining_seconds() if self._already_charged else budget.consume()
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
        budget.check()
        return response


def publication_http(
    http: GitHubHttp, budget: EvidenceReadBudget
) -> PublicationBudgetHttp:
    if type(http) is PublicationBudgetHttp:
        if http.publication_budget is not budget:
            raise GitHubPublicationError("publication budget identity mismatches")
        return http
    return PublicationBudgetHttp(http, budget)


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


def _payload(result: ReviewResult) -> bytes:
    document = result.to_dict()
    validate_public_document(document, "review-result")
    restored = ReviewResult.from_dict(document)
    if restored.to_dict() != document:
        raise ReviewInputError("retained result is not canonical")
    payload = canonical_bytes(document)
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
    return ResultPartStore(port, app_user_id=user_id)


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
) -> ReviewResult:
    _payload(result)
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
) -> ReviewResult:
    """Recover exact owned bytes using an independently selected durable checkpoint."""
    try:
        budget = budget or EvidenceReadBudget()
        budget.check()
        ledger = publication_session_ledger(ledger, budget)
        identity = SessionIdentity(
            repository, pull_request, repository_id=repository_id
        )
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
        )
        available = store.read_available(event_key=_event_key(identity, transaction))
        if available is None:
            raise ReviewInputError("retained result is missing")
        _digest, payload = available
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ReviewInputError("retained result is invalid")
        result = ReviewResult.from_dict(document)
        if _payload(result) != payload:
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
        )
        assert validated.transaction is not None
        event_key = _event_key(identity, validated.transaction)
        store = _store(
            http,
            token=token,
            identity=identity,
            app_slug=app_slug,
            base_sha=base_sha,
            head_sha=head_sha,
        )
        available = store.read_available(event_key=event_key)
        if available is None:
            if validated.transaction.phase != "publication_pending":
                raise ReviewInputError(
                    "retained result missing after unconfirmed publication; "
                    "do not recreate uncertain writes; use the original result "
                    "with the explicit operation-entry opt-out"
                )
            payload = _payload(validated)
            budget = getattr(http, "publication_budget", None)
            if isinstance(budget, EvidenceReadBudget):
                # Before any part POST, admit every bounded listing/readback
                # and a possible lost-response reconciliation, without refill.
                liability = 2 * ensure_result_part_capacity(len(payload)) + 30
                if budget.calls + liability > 60:
                    raise ReviewInputError(
                        "retention request liability exceeds the original budget"
                    )
            # A fresh owner may only reconcile this attempt. The existing
            # unconfirmed-publication phase is durable before the first POST,
            # including a crash that bypasses application exception cleanup.
            complete_review_publication(
                ledger, identity, validated.transaction, published=False
            )
            try:
                digest = store.write(event_key=event_key, result=payload)
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
                _payload(prior) != payload
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
    except (ValueError, ReviewInputError) as exc:
        raise GitHubPublicationError(
            "publication result not retained: " + str(exc)
        ) from exc
