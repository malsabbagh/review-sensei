"""Cold physical-read certificates; neither a cache nor a larger allowance."""

from __future__ import annotations

import math
from urllib.parse import parse_qs, urlsplit

from review_sensei.errors import ReviewInputError

from .http import HISTORY_PAGE_SIZES, MAX_GITHUB_RESPONSE_BYTES, MAX_PAGINATION_PAGES

ENVELOPE_BYTES = 4096
PAGE_BYTES = MAX_GITHUB_RESPONSE_BYTES - ENVELOPE_BYTES


def cold_rest_requests(count, largest, sizes=HISTORY_PAGE_SIZES):
    """Upper bound the actual aligned algorithm, including every failed probe.

    Treat every remaining item as the largest admissible item. This is a
    conservative certificate, not a packing estimate. A short/empty final
    response is still a physical request; a full1000-item walk cannot omit it.
    """
    offset, index, calls = 0, 0, 0
    while calls <= MAX_PAGINATION_PAGES:
        calls += 1
        size = sizes[index]
        amount = min(size, max(0, count - offset))
        if amount * largest > PAGE_BYTES:
            candidates = [
                i for i in range(index + 1, len(sizes)) if offset % sizes[i] == 0
            ]
            if not candidates:
                return MAX_PAGINATION_PAGES + 1
            index = candidates[0]
            continue
        if amount < size:
            return calls
        offset += amount
        index = next(i for i, width in enumerate(sizes) if offset % width == 0)
    return calls


def cold_cursor_requests(count, size, largest):
    """Complete-node retry at each cold cursor, including its final page.

    Each non-count-limited complete page consumes more than PAGE_BYTES-largest
    encoded bytes. A bounded failed prefix chooses only nodes fully inside that
    byte envelope; the same cursor is then fetched as complete JSON. No prefix
    content is accepted as evidence. Concurrent edits can cause bounded refusal.
    """
    if count == 0 or count <= 100 and size <= PAGE_BYTES:
        return 1
    if largest > PAGE_BYTES:
        return MAX_PAGINATION_PAGES + 1
    if 100 * largest <= PAGE_BYTES:
        return math.ceil(count / 100)
    return 2 * (count // 100 + math.ceil(size / max(1, PAGE_BYTES - largest)))


def capacity_comment_reader(http, path):
    """The audited physical guard supplies one fixed-scope complete reader."""
    from .publication_recovery import _publication_transport

    transport = _publication_transport(http)
    guard = getattr(transport, "_quarantine_capacity_guard", None)
    reader = getattr(guard, "comment_reader", None)
    if guard is None or reader is None:
        return None
    parsed = urlsplit(path)
    expected = transport.repository_path(
        guard.identity.repository, f"/issues/{guard.identity.pull_request}/comments"
    )
    if parsed.path != expected or set(parse_qs(parsed.query)) - {"since"}:
        return None
    return reader


class CapacityCommentReader:
    """Issuer-owned transport/identity; every use re-reads complete native data."""

    def __init__(self, ledger, identity, transport):
        self.ledger, self.identity, self.transport = ledger, identity, transport

    def __call__(
        self,
        *,
        max_requests=None,
        timeout_seconds=None,
        before_read=None,
        expected_items=None,
        observe_request=None,
    ):
        from .caller_operation import caller_for_http
        from .publication_recovery import _publication_transport
        from .quarantine_census import fetch_comments

        if _publication_transport(self.ledger.http) is not self.transport:
            raise ReviewInputError("quarantine capacity census transport changed")
        caller = caller_for_http(self.ledger.http)
        if caller is None:
            raise ReviewInputError("quarantine capacity census caller is absent")
        caller._require_ledger(self.ledger, self.identity)
        caller._quarantine_census_api_url = self.ledger.http.api_url
        rows = fetch_comments(
            self.ledger,
            self.identity,
            max_requests=max_requests,
            timeout_seconds=timeout_seconds,
            before_read=before_read,
            observe_request=observe_request,
        )
        if expected_items is not None and len(rows) != expected_items:
            raise ReviewInputError("quarantine capacity census membership changed")
        return rows
