"""Spend the original reserved tail only on final media check reconciliation."""

from __future__ import annotations

import re
from copy import copy
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener, urlopen

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError

from .errors import GitHubPublicationError
from .http import GitHubHttp
from .publication_recovery import _publication_transport, publication_http


class _RefuseMediaRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise GitHubPublicationError("media GitHub redirects are refused")


def media_http(http: GitHubHttp) -> GitHubHttp:
    """Keep one physical request per guarded dispatch in the supported host.

    Explicitly injected transports retain their existing single-dispatch
    contract. Legacy callers outside media keep their existing opener.
    """
    if type(http) is GitHubHttp and http.opener is urlopen:
        http = copy(http)
        http.opener = build_opener(_RefuseMediaRedirects()).open
    return http


class MediaCheckFinalizationHttp(GitHubHttp):
    """At most four final check dispatches; no new count or deadline allowance.

    The original budget already reserves these calls for explicit finalization.
    This closed transport cannot read review evidence or exchange capabilities.
    """

    def __init__(
        self,
        http: GitHubHttp,
        budget: EvidenceReadBudget,
        *,
        repository: str,
        head_sha: str,
    ) -> None:
        # Validate any accounting wrapper before removing its ordinary debit.
        publication_http(http, budget)
        super().__init__(api_url=http.api_url, timeout=getattr(http, "timeout", 30))
        self.transport = media_http(_publication_transport(http))
        self.budget = budget
        self.prefix = self.repository_path(repository, "/check-runs")
        self.lookup = self.repository_path(
            repository, f"/commits/{head_sha}/check-runs"
        )
        self.dispatches = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        parsed = urlsplit(path)
        allowed = (
            (method == "GET" and parsed.path == self.lookup)
            or (method == "POST" and path == self.prefix)
            or (
                method == "PATCH"
                and re.fullmatch(re.escape(self.prefix) + r"/[1-9][0-9]*", path)
                is not None
            )
        )
        if not allowed or parsed.scheme or parsed.netloc or parsed.fragment:
            raise ReviewInputError(
                "media finalization transport refuses this operation"
            )
        if self.dispatches >= 4:
            raise ReviewInputError("media finalization tail is exhausted")
        remaining = self.budget.consume(fence=True)
        self.dispatches += 1
        response = self.transport.request(
            method,
            path,
            token=token,
            body=body,
            timeout_seconds=min(remaining, timeout_seconds)
            if timeout_seconds is not None
            else remaining,
        )
        self.budget.check()
        return response
