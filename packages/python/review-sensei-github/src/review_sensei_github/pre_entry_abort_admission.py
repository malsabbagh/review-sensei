"""Native negative evidence for ordinary rollover of a closed pre-entry abort."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_history import RETAINED_CONFIRMATION_TAIL, read_previous_run
from .errors import GitHubHTTPError, GitHubPublicationError
from .http import MAX_PAGINATION_PAGES
from .session_ledger import GitHubIssueCommentSessionLedger

_PAGE_SIZE = 100
_CENSUS_WALL_SECONDS = 30
_REVIEW_STATES = {
    "COMMENT": "COMMENTED",
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
}


def _hold(reason: str) -> ReviewInputError:
    return ReviewInputError(
        "previous caller pre-entry abort cannot roll over (phase=no-transaction): "
        + reason
    )


def _number(value: object) -> bool:
    return type(value) is int and 0 < value <= 9_223_372_036_854_775_807


def _protect_tail(budget: EvidenceReadBudget, reads: int) -> None:
    try:
        budget.preflight(RETAINED_CONFIRMATION_TAIL + reads)
    except EvidenceBudgetExhausted:
        raise _hold("native effect census is unfunded in caller startup") from None


@dataclass(frozen=True)
class _EarlierNativeRows:
    reviews: frozenset[tuple[int, str, str, int]]
    inline: frozenset[tuple[int, int, str, str]]

    def explains(self, row: dict[str, Any], *, inline: bool) -> bool:
        # Only called for a readable row independently attributed to this App
        # and the aborted scope. A receipt cannot explain an unreadable row.
        try:
            raw = row["body"].encode("utf-8")
        except UnicodeEncodeError:
            return False
        digest = hashlib.sha256(raw).hexdigest()
        if inline:
            parent = row.get("pull_request_review_id")
            head = row.get("commit_id")
            return (
                _number(parent)
                and isinstance(head, str)
                and head == row.get("original_commit_id")
                and row.get("in_reply_to_id") is None
                and (row["id"], parent, head, digest) in self.inline
            )
        state = row.get("state")
        return (
            isinstance(state, str)
            and (
                row["id"],
                state,
                digest,
                len(raw),
            )
            in self.reviews
        )


_NO_EARLIER_ROWS = _EarlierNativeRows(frozenset(), frozenset())


def _earlier_native_rows(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    current: SessionRecord,
    old: ApiRunAccounting,
    aborted: ReviewTransaction,
    producer_id: int,
    budget: EvidenceReadBudget,
) -> _EarlierNativeRows:
    """Explain only exact visible bytes proved by the already settled ancestor."""
    from review_sensei.finding_lifecycle_store import _decode_selected_lifecycle

    from .finding_lifecycle import _unpack_journal, _validate
    from .finding_lifecycle_dictionary import is_dictionary

    archive, reference = old.previous_record, current.finding_lifecycle
    if archive is None or reference is None:
        return _NO_EARLIER_ROWS
    manifest = reference.get("manifest")
    binding = manifest.get("binding") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or not isinstance(binding, dict)
        or binding["head_sha"] != aborted.head_sha
        or binding["generation"] >= aborted.generation
        or reference["operation_id"] == aborted.transaction_id
    ):
        return _NO_EARLIER_ROWS
    # The archive has one part. The selected lifecycle reference has only its
    # own parts. Both remain in the same already-authorized startup slot.
    _protect_tail(budget, 1 + len(manifest.get("parts", ())))
    try:
        prior = read_previous_run(ledger, identity, archive)
        prior_caller = ApiRunAccounting.from_document(prior.api_run_accounting)
        tx = prior.transaction
        if (
            tx is None
            or tx.phase != "publication_succeeded"
            or prior_caller.terminal_transaction != tx.transaction_id
            or prior.reservation_id is not None
            or prior.finding_lifecycle != reference
            or not tx.generation <= binding["generation"] <= prior.generation
            or prior.generation >= aborted.generation
            or reference["operation_id"] != tx.transaction_id
            or (
                binding["base_sha"],
                binding["head_sha"],
                binding["policy_digest"],
                binding["configuration_digest"],
            )
            != (tx.base_sha, tx.head_sha, tx.policy_digest, tx.configuration_digest)
        ):
            return _NO_EARLIER_ROWS
        # The closing predecessor CAS already discovered this exact current
        # root. Decode only its selected authenticated parts; that read path
        # enforces their digest, producer and repository/PR bindings.
        stored = _decode_selected_lifecycle(ledger, current)
        # Only the already priced selected manifest is in scope. A different
        # history codec or absent carrier is not a reason to widen this read.
        if stored is None or is_dictionary(stored):
            return _NO_EARLIER_ROWS
        journal = _validate(_unpack_journal(stored))
    except (GitHubHTTPError, GitHubPublicationError, ReviewInputError):
        raise _hold("earlier native effect evidence is unavailable") from None
    if journal["unknown_trust_effect"]:
        return _NO_EARLIER_ROWS

    reviews: dict[int, tuple[int, str, str, int]] = {}
    reviews_path = ledger.http.repository_path(
        identity.repository, f"/pulls/{identity.pull_request}/reviews"
    )
    for effect in journal["effects"]:
        if (
            effect["status"] != "confirmed"
            or effect["method"] != "POST"
            or effect["path"] != reviews_path
        ):
            continue
        status, response = effect["response"]
        request = effect["request"]
        event = request.get("event")
        body = request.get("body")
        if (
            status in {200, 201}
            and isinstance(response, dict)
            and _number(response.get("id"))
            and response.get("producer_id") == producer_id
            and request.get("commit_id") == aborted.head_sha
            and isinstance(event, str)
            and event in _REVIEW_STATES
            and isinstance(body, dict)
            and set(body) == {"$body_sha256", "utf8_bytes"}
        ):
            rid = response["id"]
            if rid in reviews:
                return _NO_EARLIER_ROWS
            reviews[rid] = (
                rid,
                _REVIEW_STATES[event],
                body["$body_sha256"],
                body["utf8_bytes"],
            )
    inline: dict[int, tuple[int, int, str, str]] = {}
    for r in journal["native_receipts"]:
        if (
            _number(r["comment_id"])
            and r["review_id"] in reviews
            and r["producer_id"] == producer_id
            and (r["repository_id"], r["pull_request"], r["head_sha"], r["base_sha"])
            == (identity.repository_id, identity.pull_request, tx.head_sha, tx.base_sha)
        ):
            cid = r["comment_id"]
            if cid in inline:
                return _NO_EARLIER_ROWS
            inline[cid] = (cid, r["review_id"], r["native_head_sha"], r["body_sha256"])
    return _EarlierNativeRows(frozenset(reviews.values()), frozenset(inline.values()))


def require_pre_entry_abort_admission(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    old: ApiRunAccounting,
) -> None:
    """Refuse on incomplete inventory before the ordinary archive/activation.

    Called inside the owning native CAS after exact root and Actions cessation
    checks. Use its existing installation token and caller startup meter. A
    clean census is only another required predicate, never rollover authority.
    """
    from .pre_entry_abort_recovery import (
        _validate_analysis_abort_receipt,
        classify_abort_inline_effect,
        classify_abort_review_effect,
    )
    from .prospective_abort_close import AbortCloseTarget, observe_exact_abort_close

    if old.uncertain == 1:
        if not ledger._mutation_active or ledger.app_slug is None:
            raise _hold("predecessor caller dispatch remains uncertain")
        selected = ledger.selected_native_comment(record)
        if selected is None:
            raise _hold("predecessor caller dispatch remains uncertain")
        comment_id, raw_row = selected
        try:
            producer_id = int(ledger.evidence_producer().removeprefix("github-bot:"))
            target = AbortCloseTarget(
                identity=identity,
                api_url=ledger.http.api_url,
                comment_id=comment_id,
                app_login=ledger.app_slug,
                producer_id=producer_id,
            )
            echo = observe_exact_abort_close(target, selected=record, row=raw_row)
        except (ReviewInputError, ValueError):
            raise _hold("predecessor caller dispatch remains uncertain") from None
        if (
            echo.root_sha256 != record.record_sha256
            or echo.run_id != old.identity.run_id
        ):
            raise _hold("predecessor caller dispatch remains uncertain")
    elif old.uncertain != 0:
        raise _hold("predecessor caller dispatch remains uncertain")
    valid, aborted, _ = _validate_analysis_abort_receipt(record, old)
    if (
        not valid
        or aborted is None
        or record.identity() != identity
        or old.identity.repository_id != identity.repository_id
        or (aborted.repository, aborted.pull_request)
        != (
            identity.repository,
            identity.pull_request,
        )
    ):
        raise _hold("abort receipt does not authenticate the retained scope")
    budget, app_slug = ledger.evidence_budget, ledger.app_slug
    if budget is None or not app_slug or not ledger._mutation_active:
        raise _hold("authenticated native census is unavailable")
    producer_id = int(ledger.evidence_producer().removeprefix("github-bot:"))
    http = ledger.http
    deadline = http.evidence_monotonic() + _CENSUS_WALL_SECONDS
    head_sha = aborted.head_sha

    earlier = None
    earlier_review_ids: set[int] = set()

    def inspect(
        resource: str,
        classifier: Callable[[dict[str, Any]], bool | None],
        *,
        inline: bool = False,
    ) -> None:
        nonlocal earlier
        path = http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}/{resource}"
        )
        seen: set[int] = set()
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            _protect_tail(budget, 1)
            remaining = deadline - http.evidence_monotonic()
            if remaining <= 0:
                raise _hold("native effect census reached its deadline")
            try:
                status, rows = http.request(
                    "GET",
                    f"{path}?per_page={_PAGE_SIZE}&page={page}",
                    token=ledger.token,
                    timeout_seconds=remaining,
                )
            except GitHubHTTPError:
                raise _hold("native effect census is unavailable") from None
            if status != 200 or not isinstance(rows, list) or len(rows) > _PAGE_SIZE:
                raise _hold("native effect census is incomplete")
            if http.evidence_monotonic() >= deadline:
                raise _hold("native effect census reached its deadline")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not _number(row.get("id"))
                    or row["id"] in seen
                ):
                    raise _hold("native effect census has an unreadable row")
                seen.add(row["id"])
                effect = classifier(row)
                if effect is None:
                    raise _hold("native effect scope is unconfirmed")
                if effect is True:
                    if earlier is None:
                        earlier = _earlier_native_rows(
                            ledger, identity, record, old, aborted, producer_id, budget
                        )
                    if inline:
                        parent = row.get("pull_request_review_id")
                        if not _number(parent) or parent not in earlier_review_ids:
                            raise _hold("late native effect on the aborted scope")
                    if not earlier.explains(row, inline=inline):
                        raise _hold("late native effect on the aborted scope")
                    if not inline:
                        earlier_review_ids.add(row["id"])
            if len(rows) < _PAGE_SIZE:
                if http.evidence_monotonic() >= deadline:
                    raise _hold("native effect census reached its deadline")
                return
        raise _hold("native effect census reached its pagination bound")

    def review(row: dict[str, Any]) -> bool | None:
        return classify_abort_review_effect(
            row,
            identity=identity,
            head_sha=head_sha,
            app_slug=app_slug,
            app_user_id=producer_id,
        )

    def inline_comment(row: dict[str, Any]) -> bool | None:
        return classify_abort_inline_effect(
            author=row.get("user"),
            native_head=row.get("original_commit_id"),
            current_head=row.get("commit_id"),
            body=row.get("body"),
            identity=identity,
            head_sha=head_sha,
            app_slug=app_slug,
            app_user_id=producer_id,
        )

    inspect("reviews", review)
    inspect("comments", inline_comment, inline=True)
