"""Process-issued completion witnesses, never restored document authority.

Serialized witnesses are useful retained diagnostics. Only the original object
issued by a source success branch can settle the current process's action.
"""

from __future__ import annotations

import hashlib
import os
import weakref
from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, Mapping

from review_sensei.bounded_evidence import canonical_bytes


class CompletionState(str, Enum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"


class CompletionScope(str, Enum):
    APPROVAL = "approval"
    PUBLICATION = "publication"
    FINALIZATION = "finalization"
    UNNECESSARY = "unnecessary"


@dataclass(frozen=True)
class FinalizationSubject:
    repository: str
    pull_request: int
    head_sha: str
    result_digest: str


@dataclass(frozen=True)
class FinalizationCompletion:
    subject: FinalizationSubject
    scope: CompletionScope
    outcome_sha256: str
    state: CompletionState = CompletionState.UNCONFIRMED


@dataclass(frozen=True)
class ReplySource:
    repository: str
    pull_request: int
    head_sha: str
    kind: str
    id: int
    updated_at: str
    body_sha256: str
    run_id: str | None
    run_attempt: str | None


@dataclass(frozen=True)
class ReplyFinalizationCompletion:
    source: ReplySource
    scope: CompletionScope
    outcome_sha256: str
    finalization: FinalizationCompletion | None = None
    state: CompletionState = CompletionState.UNCONFIRMED


_ISSUED: dict[int, weakref.ReferenceType[Any]] = {}


def _issue(value: Any) -> Any:
    key = id(value)
    _ISSUED[key] = weakref.ref(value, lambda _ref: _ISSUED.pop(key, None))
    return value


def _issued(value: Any) -> bool:
    ref = _ISSUED.get(id(value))
    return (
        ref is not None
        and ref() is value
        and getattr(value, "state", None) is CompletionState.CONFIRMED
    )


def result_document(value: Any) -> dict[str, Any]:
    """The exact retained public result, excluding its completion witness."""
    return {
        item.name: asdict_value(getattr(value, item.name))
        for item in fields(value)
        if item.name != "finalization_completion"
    }


def asdict_value(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(result_document(value))).hexdigest()


def _confirmed_approval(
    result: Any, subject: FinalizationSubject
) -> FinalizationCompletion:
    return _issue(
        FinalizationCompletion(
            subject,
            CompletionScope.APPROVAL,
            _fingerprint(result),
            CompletionState.CONFIRMED,
        )
    )


def approval_confirmed(result: Any) -> bool:
    proof = getattr(result, "finalization_completion", None)
    return (
        type(proof) is FinalizationCompletion
        and _issued(proof)
        and proof.scope is CompletionScope.APPROVAL
        and proof.outcome_sha256 == _fingerprint(result)
    )


def _confirmed_finalization(
    approval: Any, result: Any
) -> FinalizationCompletion | None:
    """Called only after the selected native check and thread tails settled."""
    if not approval_confirmed(approval):
        return None
    return _issue(
        FinalizationCompletion(
            approval.finalization_completion.subject,
            CompletionScope.FINALIZATION,
            _fingerprint(result),
            CompletionState.CONFIRMED,
        )
    )


def finalization_confirmed(result: Any) -> bool:
    proof = getattr(result, "finalization_completion", None)
    return (
        type(proof) is FinalizationCompletion
        and _issued(proof)
        and proof.scope is CompletionScope.FINALIZATION
        and proof.outcome_sha256 == _fingerprint(result)
    )


def _confirmed_publication(approval: Any, result: Any) -> FinalizationCompletion | None:
    """Publication source confirmed its native review and selected check."""
    if not approval_confirmed(approval):
        return None
    return _issue(
        FinalizationCompletion(
            approval.finalization_completion.subject,
            CompletionScope.PUBLICATION,
            _fingerprint(result),
            CompletionState.CONFIRMED,
        )
    )


def _confirmed_cleanup(previous: Any, result: Any) -> FinalizationCompletion | None:
    proof = getattr(previous, "finalization_completion", None)
    if (
        type(proof) is not FinalizationCompletion
        or not _issued(proof)
        or proof.scope is not CompletionScope.PUBLICATION
        or proof.outcome_sha256 != _fingerprint(previous)
    ):
        return None
    return _issue(
        FinalizationCompletion(
            proof.subject,
            CompletionScope.FINALIZATION,
            _fingerprint(result),
            CompletionState.CONFIRMED,
        )
    )


def rebind_publication_metadata(
    previous: Any, result: Any
) -> FinalizationCompletion | None:
    """Only non-business metadata additions may carry a publication witness."""
    proof = getattr(previous, "finalization_completion", None)
    if (
        type(proof) is not FinalizationCompletion
        or not _issued(proof)
        or proof.outcome_sha256 != _fingerprint(previous)
    ):
        return None
    before, after = result_document(previous), result_document(result)
    for name in ("shadow", "transaction_id", "generation", "operation_diagnostics"):
        before.pop(name, None)
        after.pop(name, None)
    if before != after:
        return None
    return _issue(
        FinalizationCompletion(
            proof.subject,
            proof.scope,
            _fingerprint(result),
            CompletionState.CONFIRMED,
        )
    )


def reply_source(
    *,
    repository: str,
    pull_request: int,
    head_sha: str,
    kind: str,
    source_id: int,
    updated_at: str,
    body: str,
) -> ReplySource:
    return ReplySource(
        repository,
        pull_request,
        head_sha,
        "review_comment" if kind == "inline" else "issue_comment",
        source_id,
        updated_at,
        hashlib.sha256(body.encode()).hexdigest(),
        os.environ.get("GITHUB_RUN_ID"),
        os.environ.get("GITHUB_RUN_ATTEMPT"),
    )


def _reply_unnecessary(
    result: Any, source: ReplySource
) -> ReplyFinalizationCompletion | None:
    """Generic prose publisher explicitly selected no approval/check/thread work."""
    if any(
        getattr(result, name) is not None
        for name in (
            "approval_status",
            "approval_diagnostic",
            "assessment_status",
            "assessment_diagnostic",
            "resolution_status",
        )
    ):
        return None
    return _issue(
        ReplyFinalizationCompletion(
            source,
            CompletionScope.UNNECESSARY,
            _fingerprint(result),
            state=CompletionState.CONFIRMED,
        )
    )


def reply_completion_from_finalization(
    publication: Any, result: Any, source: ReplySource, *, result_digest: str
) -> ReplyFinalizationCompletion | None:
    if not finalization_confirmed(publication):
        return None
    proof = publication.finalization_completion
    if proof.subject != FinalizationSubject(
        source.repository, source.pull_request, source.head_sha, result_digest
    ):
        return None
    return _issue(
        ReplyFinalizationCompletion(
            source,
            CompletionScope.FINALIZATION,
            _fingerprint(result),
            proof,
            CompletionState.CONFIRMED,
        )
    )


def rebind_reply_completion(
    previous: Any, result: Any
) -> ReplyFinalizationCompletion | None:
    """Carry a source-issued result through its publisher's outer result shape."""
    proof = getattr(previous, "finalization_completion", None)
    if not isinstance(proof, ReplyFinalizationCompletion) or not _reply_confirmed(
        previous
    ):
        return None
    before, after = result_document(previous), result_document(result)
    for name in ("status", "comment_id"):
        before.pop(name, None)
        after.pop(name, None)
    if before != after:
        return None
    return _issue(
        ReplyFinalizationCompletion(
            proof.source,
            proof.scope,
            _fingerprint(result),
            proof.finalization,
            CompletionState.CONFIRMED,
        )
    )


def _reply_confirmed(result: Any) -> bool:
    proof = getattr(result, "finalization_completion", None)
    return (
        type(proof) is ReplyFinalizationCompletion
        and _issued(proof)
        and proof.outcome_sha256 == _fingerprint(result)
        and (
            proof.scope is CompletionScope.UNNECESSARY
            and proof.finalization is None
            or proof.scope is CompletionScope.FINALIZATION
            and type(proof.finalization) is FinalizationCompletion
            and _issued(proof.finalization)
            and proof.finalization.scope is CompletionScope.FINALIZATION
        )
    )


def require_reply_completion(
    result: Any, authority: Mapping[str, Any], *, run_id: str, run_attempt: str
) -> bool:
    if not _reply_confirmed(result):
        return False
    proof = result.finalization_completion
    source = authority["source"]
    return proof.source == ReplySource(
        authority["repository"],
        authority["pull_request"],
        authority["head_sha"],
        source["kind"],
        source["id"],
        source["updated_at"],
        source["body_sha256"],
        run_id,
        run_attempt,
    )
