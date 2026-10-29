"""Read-only reconstruction of the c7 complete-profile framing failure.

This classifies the old formatter; the live publisher's archive fallback must
not change the classification of an immutable failed result. No HTTP request,
retention, inventory staging or provider call is permitted here.
"""

from __future__ import annotations

import hashlib
import weakref
from dataclasses import replace

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

_ISSUED: weakref.WeakValueDictionary[int, ReconstructedFailedSource] = (
    weakref.WeakValueDictionary()
)


class ReconstructedFailedSource(dict):
    """Original-context reader's process-bound result, not serialized authority."""

    _http: object
    _record: str
    _result: str
    _bytes: bytes
    _failure: dict | None

    def require_body_failure(self, *, http, previous, result):
        if (
            _ISSUED.get(id(self)) is not self
            or self._http is not http
            or self._record != previous.record_sha256
            or self._result != result.content_digest()
            or self._bytes != canonical_bytes(self)
            or self._failure is None
        ):
            raise ReviewInputError(
                "quarantine body-limit original reconstruction is unavailable or changed"
            )
        return dict(self._failure)


def reconstructed_source(document, *, ledger, previous, result, admitted, diff, policy):
    """Called after exact immutable result/context and admission verification."""
    value = ReconstructedFailedSource(document)
    value._http, value._record = ledger.http, previous.record_sha256
    value._result, value._bytes = result.content_digest(), canonical_bytes(document)
    value._failure = None
    if not any(any(c in item.path for c in "*?[") for item in result.comments):
        value._failure = reproduce_body_limit(
            previous=previous, result=admitted.result, diff=diff, policy=policy
        )
    _ISSUED[id(value)] = value
    return value


def reproduce_body_limit(*, previous, result, diff, policy):
    """Reassemble the old most compact complete frame with every explanation.

    The fixed-width inventory placeholder is the old pre-staging representation.
    If that minimum still fails, neither legacy inventory nor larger framing can
    publish. Merge-focused placement needs no mutable host facts. Narrative
    confirmation is deliberately unavailable when classifying the old source.
    """
    from .approval import approval_eligibility_from_result
    from .default_human_inventory import HumanInventoryReference
    from .publication import (
        PublicationLimitError,
        ReviewPublisher,
        _approval_eligible,
        _disposition_frame_reserve,
        _published_summary_byte_limit,
        approval_eligibility_marker,
        fit_assembled_review,
        review_marker,
        review_result_digest,
    )

    if policy.mode != "merge-focused" or policy.inline_advisory_threads:
        raise ReviewInputError("quarantine body-limit placement contract changed")
    tx, identity = previous.transaction, previous.identity()
    from .http import GitHubHttp

    def no_http(*args, **kwargs):
        raise ReviewInputError("quarantine body-limit reconstruction permits no HTTP")

    publisher = ReviewPublisher(
        http=GitHubHttp(api_url="https://quarantine.invalid", opener=no_http)
    )

    def no_host_facts():
        raise ReviewInputError("quarantine body-limit requires mutable placement facts")

    assembled = publisher._assemble_review_body(
        result=result,
        analysis=publisher._validate_locations(result, diff),
        convergence_policy=policy,
        read_facts=no_host_facts,
        auto_approve=_approval_eligible(result),
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=tx.head_sha,
        base_sha=tx.base_sha,
    )
    assembled = replace(assembled, compact_references=True)
    eligibility = approval_eligibility_from_result(
        result,
        head_sha=tx.head_sha,
        base_sha=tx.base_sha,
        enabled=True,
        app_authored=False,
        check_published=False,
    )
    if eligibility.human_review is not None:
        eligibility = replace(
            eligibility,
            human_inventory_reference=HumanInventoryReference(
                "0" * 64 + "\t" + "0" * 64, "0" * 64
            ),
        )
    eligibility = replace(
        eligibility,
        facts=replace(
            eligibility.facts,
            has_blocking_findings=any(
                e.blocking and e.body_placed for e in assembled.index_entries
            ),
        ),
    )
    reserve = _disposition_frame_reserve(eligibility, assembled.index_entries)
    try:
        fit_assembled_review(
            assembled,
            review_marker=review_marker(
                repository_id=identity.repository_id,
                pull_request=identity.pull_request,
                head_sha=tx.head_sha,
                result=result,
            ),
            eligibility_marker=approval_eligibility_marker(eligibility),
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=tx.head_sha,
            base_sha=tx.base_sha,
            result_digest=review_result_digest(result),
            summary_limit=_published_summary_byte_limit(result),
            complete_inventory=True,
            reserved_bytes=reserve,
            preserve_explanations=eligibility.human_review is not None,
        )
    except PublicationLimitError as exc:
        if exc.check != "framed-review-body":
            return None
        return {
            "contract": "body-limit-prepublication-v1",
            "check": exc.check,
            "result_sha256": result.content_digest(),
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "retained_review_status": result.review_status,
            "raw_candidate_count": len(result.comments),
        }
    return None
