"""One finite transport envelope for a complete pending-receipt publication."""

from __future__ import annotations

import time
from contextlib import contextmanager
from functools import wraps
from typing import Any

from .errors import GitHubConversationError
from .http import GitHubHttp

MAX_REPLY_PUBLICATION_REQUESTS = 128
MAX_REPLY_PUBLICATION_WRITES = 32
MAX_REPLY_PUBLICATION_SECONDS = 60


class ReplyPublicationHttp(GitHubHttp):
    """Charge physical attempts across pages, readbacks and authority fences.

    Delegation preserves an existing feedback/operation allowance: this adapter
    adds a finite publication ceiling and never replaces or replenishes it.
    """

    def __init__(self, transport: GitHubHttp) -> None:
        super().__init__(api_url=transport.api_url, timeout=transport.timeout)
        self.transport = transport
        self.requests = 0
        self.writes = 0
        self.deadline = time.monotonic() + MAX_REPLY_PUBLICATION_SECONDS

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        remaining = self.deadline - time.monotonic()
        write = method.upper() not in {"GET", "HEAD"}
        if (
            self.requests >= MAX_REPLY_PUBLICATION_REQUESTS
            or (write and self.writes >= MAX_REPLY_PUBLICATION_WRITES)
            or remaining <= 0
        ):
            raise GitHubConversationError("pending reply publication budget exhausted")
        self.requests += 1
        self.writes += int(write)
        if timeout_seconds is not None:
            remaining = min(remaining, timeout_seconds)
        result = self.transport.request(
            method,
            path,
            token=token,
            body=body,
            timeout_seconds=remaining,
        )
        if time.monotonic() >= self.deadline:
            raise GitHubConversationError("pending reply publication budget exhausted")
        return result


@contextmanager
def reply_publication_budget(*owners):
    """Share one wrapper across the publisher, its reader and its finalizer."""
    original = [owner.http for owner in owners]
    wrapped = original[0]
    if not isinstance(wrapped, ReplyPublicationHttp):
        wrapped = ReplyPublicationHttp(wrapped)
    try:
        for owner in owners:
            owner.http = wrapped
        yield
    finally:
        for owner, http in zip(owners, original, strict=True):
            owner.http = http


def bounded_reply_publication(method):
    @wraps(method)
    def bounded(self, *args, **kwargs):
        owners = [self, self.finalizer]
        conversation = getattr(self, "conversation", None)
        if conversation is not None:
            owners.extend([conversation, conversation.finalizer])
        with reply_publication_budget(*owners):
            return method(self, *args, **kwargs)

    return bounded
