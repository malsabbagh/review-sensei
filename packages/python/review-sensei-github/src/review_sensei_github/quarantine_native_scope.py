"""V3 native namespace from complete process-authenticated lineage only."""

from __future__ import annotations

import json
import weakref
from dataclasses import dataclass
from typing import Any

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_host import review_startup_plan
from .caller_operation import caller_for_http
from .default_human_reader import transaction_publication_digest

_ISSUED: weakref.WeakSet[NativeScope] = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class NativeScope:
    caller: Any
    identity: SessionIdentity
    base: str
    head: str
    scope: str
    producer: int
    prior: frozenset

    def filter_reviews(self, reviews):
        from .publication import parse_finding_index

        if self.identity.repository_id is None:
            raise ReviewInputError("quarantine numeric repository identity is absent")
        selected = []
        for review in reviews:
            user = review.get("user") if isinstance(review, dict) else None
            if (
                isinstance(user, dict)
                and user.get("type") == "Bot"
                and user.get("id") == self.producer
                and review.get("commit_id") == self.head
                and isinstance(review.get("body"), str)
            ):
                index = parse_finding_index(
                    review["body"],
                    repository_id=self.identity.repository_id,
                    pull_request=self.identity.pull_request,
                    head_sha=self.head,
                )
                if (
                    index is not None
                    and (self.head, index.base_sha, index.result_digest) in self.prior
                ):
                    continue
            selected.append(review)
        return selected


def bind_native_scope(ledger, identity, authenticated):
    from .quarantine_origin import _AUTHENTICATED, AuthenticatedQuarantine

    caller = caller_for_http(ledger.http)
    if caller is None or caller.plan != review_startup_plan(
        caller.job, quarantine_v3=True
    ):
        return
    if (
        type(authenticated) is not AuthenticatedQuarantine
        or authenticated not in _AUTHENTICATED
        or authenticated.archive.get("publication_scope") is None
        or authenticated.original_caller is not caller
        or authenticated.original_identity != identity
    ):
        raise ReviewInputError("quarantine native scope lacks authenticated origin")
    reader = ledger._quarantine_origin_reader
    prior = []
    for source in json.loads(authenticated.source_documents_bytes).values():
        record = SessionRecord.from_dict(source["record"])
        if record.transaction is None:
            raise ReviewInputError("quarantine native scope publication is absent")
        prior.append(
            (
                record.transaction.head_sha,
                record.transaction.base_sha,
                transaction_publication_digest(record.transaction),
            )
        )
    scope = NativeScope(
        caller,
        identity,
        reader.current_scope.base_sha,
        reader.current_scope.head_sha,
        authenticated.lineage["scope_sha256"],
        ledger._evidence_author_id,
        frozenset(prior),
    )
    _ISSUED.add(scope)
    setattr(caller, "_quarantine_v3_native_scope", scope)


def native_scope(http, *, repository=None, head_sha=None):
    caller = caller_for_http(http)
    if caller is None or caller.plan != review_startup_plan(
        caller.job, quarantine_v3=True
    ):
        return None
    scope = getattr(caller, "_quarantine_v3_native_scope", None)
    if (
        type(scope) is not NativeScope
        or scope not in _ISSUED
        or scope.caller is not caller
        or repository is not None
        and repository != scope.identity.repository
        or head_sha is not None
        and head_sha != scope.head
    ):
        raise ReviewInputError("quarantine native scope is foreign or unavailable")
    return scope


def filter_reviews(http, reviews, *, repository, head_sha=None):
    scope = native_scope(http, repository=repository, head_sha=head_sha)
    return reviews if scope is None else scope.filter_reviews(reviews)
