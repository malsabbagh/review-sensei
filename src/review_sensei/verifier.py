"""Deterministic candidate-finding evidence checks.

This pass never asks a model to adjudicate itself.  It verifies that bounded
evidence references exist in the exact snapshot reviewed and that candidates
are actionable before a publisher is allowed to consider them.

Snapshot bounds intentionally reuse ``ReviewLimits`` diff ceilings
(``max_diff_files`` and ``max_diff_bytes``) so verification stays aligned with
the same bounded reviewed-input profile used to admit review inputs.  The
snapshot is the reviewed source mapping passed into verification, not an
unbounded repository checkout.  Embedders may tighten limits by passing a lower
``ReviewLimits`` profile to the verifier helpers.

Public documents in this module use ``schema_version: "1.0"``, matching the
existing v1 schema family identified by ``$id`` URLs under ``/schemas/v1/``.
Evidence excerpts are single-line substrings matched against one reviewed line.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from collections import Counter

from .errors import ReviewInputError
from .models import ReviewComment, ReviewResult
from .presentation import _escape_markdown_label
from .schemas import validate_public_document
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    ReviewLimits,
    utf8_size,
    validate_repository_path,
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
DISPOSITIONS = frozenset({"confirmed", "rejected", "insufficient-evidence"})
EVIDENCE_POLICIES = frozenset({"legacy", "confirmed"})
_EVIDENCE_FIELDS = frozenset({"path", "line", "snapshot_sha256", "excerpt"})
PUBLIC_SCHEMA_VERSION = "1.0"
_COVERAGE_NOTE = (
    "Unpublished candidates are not findings. Incomplete verification is not "
    "a clean review."
)


@dataclass(frozen=True)
class EvidenceReference:
    """One bounded line reference into the reviewed snapshot.

    ``excerpt``, when present, must be a single-line string at construction time.
    Whether it is a substring of the referenced snapshot line is validated only
    during ``verify_candidate`` / ``verify_candidates``, not here.
    """

    path: str
    line: int
    snapshot_sha256: str
    excerpt: str | None = None

    def __post_init__(self) -> None:
        validate_repository_path(self.path, label="evidence path")
        if (
            isinstance(self.line, bool)
            or not isinstance(self.line, int)
            or self.line < 1
        ):
            raise ReviewInputError("evidence line must be positive")
        if not isinstance(self.snapshot_sha256, str) or not _SHA256.fullmatch(
            self.snapshot_sha256
        ):
            raise ReviewInputError("evidence snapshot_sha256 must be a SHA-256 digest")
        if self.excerpt is not None and (
            not isinstance(self.excerpt, str)
            or not self.excerpt
            or len(self.excerpt) > 512
            or "\n" in self.excerpt
            or "\r" in self.excerpt
        ):
            raise ReviewInputError("evidence excerpt must be a single line")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "snapshot_sha256": self.snapshot_sha256,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class CandidateFinding:
    claim: str
    triggering_conditions: str
    impacted_path: str
    evidence: tuple[EvidenceReference, ...]
    severity_rationale: str
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "claim",
            "triggering_conditions",
            "impacted_path",
            "severity_rationale",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ReviewInputError(
                    f"candidate {name} must be a bounded non-empty string"
                )
        validate_repository_path(self.impacted_path, label="candidate impacted_path")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(reference, EvidenceReference) for reference in self.evidence
        ):
            raise ReviewInputError(
                "candidate evidence must be EvidenceReference values"
            )
        if not self.evidence:
            raise ReviewInputError("candidate must include evidence")
        if len(self.evidence) > 8:
            raise ReviewInputError("candidate has too many evidence references")
        if not isinstance(self.assumptions, tuple) or any(
            not isinstance(assumption, str)
            or not assumption.strip()
            or len(assumption) > 1024
            for assumption in self.assumptions
        ):
            raise ReviewInputError(
                "candidate assumptions must be bounded non-empty strings"
            )
        if len(self.assumptions) > 16:
            raise ReviewInputError("candidate has too many assumptions")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "claim": self.claim,
            "triggering_conditions": self.triggering_conditions,
            "impacted_path": self.impacted_path,
            "evidence": [reference.to_dict() for reference in self.evidence],
            "severity_rationale": self.severity_rationale,
        }
        if self.assumptions:
            value["assumptions"] = list(self.assumptions)
        validate_public_document(value, "candidate-finding")
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CandidateFinding":
        if not isinstance(value, Mapping):
            raise ReviewInputError("candidate must be an object")
        try:
            required_fields = (
                "claim",
                "triggering_conditions",
                "impacted_path",
                "severity_rationale",
            )
            for field in required_fields:
                if field not in value:
                    raise KeyError(field)
                if not isinstance(value[field], str):
                    raise TypeError(f"candidate {field} must be a string")
            if "evidence" not in value:
                raise KeyError("evidence")
            raw_evidence = value["evidence"]
            if not isinstance(raw_evidence, (list, tuple)):
                raise TypeError("evidence must be iterable")
            evidence_values: list[EvidenceReference] = []
            for item in raw_evidence:
                if not isinstance(item, Mapping):
                    raise TypeError("evidence reference must be an object")
                unknown = set(item) - _EVIDENCE_FIELDS
                if unknown:
                    raise TypeError("evidence reference contains unknown fields")
                try:
                    evidence_values.append(EvidenceReference(**item))  # type: ignore[arg-type]
                except ReviewInputError as exc:
                    raise ReviewInputError("candidate finding is malformed") from exc
            evidence = tuple(evidence_values)
            raw_assumptions = value.get("assumptions", ())
            if not isinstance(raw_assumptions, (list, tuple)):
                raise TypeError("assumptions must be iterable")
            if any(not isinstance(item, str) for item in raw_assumptions):
                raise TypeError("assumptions must contain only strings")
            assumptions = tuple(raw_assumptions)
            return cls(
                value["claim"],  # type: ignore[arg-type]
                value["triggering_conditions"],  # type: ignore[arg-type]
                value["impacted_path"],  # type: ignore[arg-type]
                evidence,
                value["severity_rationale"],  # type: ignore[arg-type]
                assumptions,
            )
        except KeyError as exc:
            field = exc.args[0]
            raise ReviewInputError(
                f"candidate finding is missing field '{field}'"
            ) from exc
        except ReviewInputError as exc:
            if str(exc) == "candidate finding is malformed":
                raise
            raise ReviewInputError("candidate finding is malformed") from exc
        except (TypeError, ValueError) as exc:
            raise ReviewInputError("candidate finding is malformed") from exc


@dataclass(frozen=True)
class VerificationResult:
    disposition: str
    reasons: tuple[str, ...]
    evidence_valid: bool
    actionable: bool

    def __post_init__(self) -> None:
        if self.disposition not in DISPOSITIONS:
            raise ReviewInputError("verification disposition is unsupported")

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "evidence_valid": self.evidence_valid,
            "actionable": self.actionable,
        }
        validate_public_document(value, "verification-result")
        return value


def _check_snapshot_bounds(
    snapshot: Mapping[str, str],
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> None:
    """Bound snapshot size using the public diff profile.

    ``max_diff_bytes`` applies to the aggregate UTF-8 size of all paths and
    contents.  Each individual file is also capped at the same byte ceiling as
    a fail-fast guard so no single path can exceed the public diff limit alone.
    """
    if not isinstance(snapshot, Mapping):
        raise ReviewInputError("snapshot must be a mapping")
    if len(snapshot) > limits.max_diff_files:
        raise ReviewInputError("snapshot contains too many files")
    total_bytes = 0
    for path, content in snapshot.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise ReviewInputError("snapshot paths and contents must be strings")
        validate_repository_path(path, label="snapshot path")
        path_bytes = utf8_size(path, label="snapshot path")
        content_bytes = utf8_size(content, label="snapshot content")
        if path_bytes + content_bytes > limits.max_diff_bytes:
            raise ReviewInputError(
                "snapshot file exceeds the configured per-file byte limit"
            )
        total_bytes += path_bytes + content_bytes
        if total_bytes > limits.max_diff_bytes:
            raise ReviewInputError(
                "snapshot exceeds the configured aggregate byte limit"
            )


def _canonical_snapshot_bytes(snapshot: Mapping[str, str]) -> bytes:
    """Return the canonical UTF-8 bytes used for snapshot digests.

    Callers must hash this exact representation: sorted object keys,
    ``sort_keys=True``, compact separators ``(",", ":")``, and
    ``ensure_ascii=True`` (non-ASCII code points are escaped).
    """

    return json.dumps(
        dict(sorted(snapshot.items())),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _snapshot_digest(
    snapshot: Mapping[str, str],
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> str:
    _check_snapshot_bounds(snapshot, limits=limits)
    return hashlib.sha256(_canonical_snapshot_bytes(snapshot)).hexdigest()


def _snapshot_lines(content: str) -> list[str]:
    """Split reviewed snapshot text on normalized Unix line boundaries."""

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def _candidate_dedup_key(candidate: CandidateFinding) -> tuple[str, str, str]:
    evidence_payload = json.dumps(
        [reference.to_dict() for reference in candidate.evidence],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    evidence_digest = hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()
    return (candidate.impacted_path, candidate.claim, evidence_digest)


def _verify_candidate_evidence(
    candidate: CandidateFinding,
    snapshot: Mapping[str, str],
    *,
    reviewed_snapshot_sha256: str,
) -> VerificationResult:
    reasons: list[str] = []
    evidence_valid = True
    for reference in candidate.evidence:
        if reference.snapshot_sha256 != reviewed_snapshot_sha256:
            evidence_valid = False
            reasons.append("evidence snapshot does not match reviewed snapshot")
            continue
        snapshot_content = snapshot.get(reference.path)
        if snapshot_content is None:
            evidence_valid = False
            reasons.append("evidence path is absent from reviewed snapshot")
            continue
        lines = _snapshot_lines(snapshot_content)
        if reference.line < 1 or reference.line > len(lines):
            evidence_valid = False
            reasons.append("evidence line is outside reviewed snapshot")
            continue
        if (
            reference.excerpt is not None
            and reference.excerpt not in lines[reference.line - 1]
        ):
            evidence_valid = False
            reasons.append("evidence excerpt does not match reviewed snapshot")
    actionable = bool(
        candidate.claim.strip()
        and candidate.triggering_conditions.strip()
        and candidate.impacted_path.strip()
    )
    if not actionable:
        reasons.append("candidate lacks actionable triggering conditions")
    if not evidence_valid:
        return VerificationResult(
            "rejected", tuple(dict.fromkeys(reasons)), False, False
        )
    if not actionable:
        return VerificationResult(
            "insufficient-evidence", tuple(dict.fromkeys(reasons)), True, False
        )
    return VerificationResult("confirmed", (), True, True)


def verify_candidate(
    candidate: CandidateFinding,
    snapshot: Mapping[str, str],
    *,
    snapshot_sha256: str,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> VerificationResult:
    """Verify evidence paths/lines and excerpts against a reviewed snapshot.

    ``snapshot`` maps canonical repository paths to source text.  It is treated
    as data only; no instructions in source or candidate fields are executed.
    """
    if not isinstance(candidate, CandidateFinding):
        raise ReviewInputError("candidate must be a CandidateFinding")
    if not isinstance(snapshot_sha256, str) or not _SHA256.fullmatch(snapshot_sha256):
        raise ReviewInputError("snapshot_sha256 must be a SHA-256 digest")
    reviewed_digest = _snapshot_digest(snapshot, limits=limits)
    if reviewed_digest != snapshot_sha256:
        raise ReviewInputError("snapshot_sha256 does not match reviewed snapshot")
    return _verify_candidate_evidence(
        candidate, snapshot, reviewed_snapshot_sha256=reviewed_digest
    )


def verify_candidates(
    candidates: Sequence[CandidateFinding],
    snapshot: Mapping[str, str],
    *,
    snapshot_sha256: str,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> tuple[VerificationResult, ...]:
    """Verify candidates independently, de-duplicating identical claims.

    De-duplication keys on ``(impacted_path, claim, evidence_digest)`` so the
    same logical finding with distinct evidence remains distinct.  Actionable
    checks require bounded non-empty claim, triggering conditions, and path;
    semantic quality gates belong upstream of this verifier.

    Snapshot bounds and digest work happen once per batch; each candidate then
    reuses the validated snapshot mapping for evidence checks.
    """
    if not isinstance(snapshot_sha256, str) or not _SHA256.fullmatch(snapshot_sha256):
        raise ReviewInputError("snapshot_sha256 must be a SHA-256 digest")
    digest = _snapshot_digest(snapshot, limits=limits)
    if digest != snapshot_sha256:
        raise ReviewInputError("snapshot_sha256 does not match reviewed snapshot")
    seen: set[tuple[str, str, str]] = set()
    results: list[VerificationResult] = []
    for candidate in candidates:
        if not isinstance(candidate, CandidateFinding):
            raise ReviewInputError("candidates must contain CandidateFinding values")
        key = _candidate_dedup_key(candidate)
        if key in seen:
            results.append(
                VerificationResult("rejected", ("duplicate candidate",), False, False)
            )
            continue
        seen.add(key)
        results.append(
            _verify_candidate_evidence(
                candidate, snapshot, reviewed_snapshot_sha256=digest
            )
        )
    return tuple(results)


@dataclass(frozen=True)
class PublishableReview:
    """A review whose comments are safe for a publisher to emit as findings."""

    result: ReviewResult
    verifications: tuple[VerificationResult, ...]
    evidence_policy: str
    unpublished: int


def _escape_published_text(value: str) -> str:
    """Escape untrusted candidate text before it becomes a published comment body."""

    return _escape_markdown_label(value.strip()).replace("@", "\\@")


def _escape_inline_excerpt(value: str) -> str:
    """Keep excerpt text inside one inline-code span."""

    return value.replace("\\", "\\\\").replace("`", "\\`")


def format_candidate_finding(candidate: CandidateFinding) -> str:
    """Render a candidate as concise published reasoning, never a raw transcript."""

    if not isinstance(candidate, CandidateFinding):
        raise ReviewInputError("candidate must be a CandidateFinding")
    evidence_parts: list[str] = []
    for reference in candidate.evidence:
        location = f"{reference.path}:{reference.line}"
        if reference.excerpt:
            excerpt = _escape_inline_excerpt(reference.excerpt)
            evidence_parts.append(f"{location} (`{excerpt}`)")
        else:
            evidence_parts.append(location)
    lines = [
        _escape_published_text(candidate.claim),
        "",
        f"Trigger: {_escape_published_text(candidate.triggering_conditions)}",
        f"Why it matters: {_escape_published_text(candidate.severity_rationale)}",
        f"Evidence: {'; '.join(evidence_parts)}",
    ]
    if candidate.assumptions:
        escaped = "; ".join(_escape_published_text(assumption) for assumption in candidate.assumptions)
        lines.append(f"Assumptions: {escaped}")
    return "\n".join(lines)


def _candidate_to_comment(candidate: CandidateFinding) -> ReviewComment:
    # Anchor the finding at the first evidence reference on impacted_path when
    # present; otherwise fall back to the primary reference. Publishers may
    # still reject the location against the diff if it is outside hunks.
    located = next(
        (
            reference
            for reference in candidate.evidence
            if reference.path == candidate.impacted_path
        ),
        candidate.evidence[0],
    )
    return ReviewComment(
        path=located.path,
        line=located.line,
        body=format_candidate_finding(candidate),
    )


def _verification_coverage(verifications: Sequence[VerificationResult]) -> str:
    counts = {"confirmed": 0, "rejected": 0, "insufficient-evidence": 0}
    reason_counts: Counter[str] = Counter()
    for item in verifications:
        counts[item.disposition] += 1
        if item.disposition != "confirmed":
            for reason in item.reasons:
                reason_counts[reason] += 1
    parts = [
        "Verification coverage: "
        f"confirmed={counts['confirmed']}, "
        f"rejected={counts['rejected']}, "
        f"insufficient-evidence={counts['insufficient-evidence']}."
    ]
    if reason_counts:
        breakdown = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(reason_counts.items())
        )
        parts.append(f"Rejection reasons: {breakdown}.")
    parts.append(_COVERAGE_NOTE)
    return " ".join(parts)


def _downgrade_incomplete_status(status: str) -> str:
    if status in {"complete", "summary-only"}:
        return "partial"
    return status


def prepare_publishable_review(
    result: ReviewResult,
    *,
    candidates: Sequence[CandidateFinding] | None = None,
    snapshot: Mapping[str, str] | None = None,
    snapshot_sha256: str | None = None,
    evidence_policy: str = "legacy",
    limits: ReviewLimits | None = None,
) -> PublishableReview:
    """Gate findings before publication using the configured evidence policy.

    ``legacy`` is the compatible single-pass mode: existing comments publish
    unchanged and are identified by ``evidence_policy="legacy"``. ``confirmed``
    publishes only candidates whose evidence exists in the exact reviewed
    snapshot. Rejected, duplicate, malformed, and insufficient-evidence
    candidates never become findings, and incomplete coverage cannot be a
    clean review.
    """

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("review result is invalid")
    if evidence_policy not in EVIDENCE_POLICIES:
        raise ReviewInputError("evidence policy is unsupported")
    if evidence_policy == "legacy":
        if result.evidence_policy != "legacy":
            result = ReviewResult(
                summary=result.summary,
                comments=result.comments,
                provider=result.provider,
                model=result.model,
                learning_proposals=result.learning_proposals,
                review_status=result.review_status,
                limits=result.limits,
                source_context_coverage=result.source_context_coverage,
                evidence_policy="legacy",
            )
        return PublishableReview(result, (), "legacy", 0)

    if candidates is None:
        candidates = ()
    if snapshot is None or snapshot_sha256 is None:
        raise ReviewInputError("confirmed evidence policy requires a reviewed snapshot")
    review_limits = limits if limits is not None else result.limits
    verifications = verify_candidates(
        candidates,
        snapshot,
        snapshot_sha256=snapshot_sha256,
        limits=review_limits,
    )
    published: list[ReviewComment] = []
    unpublished = 0
    for candidate, verification in zip(candidates, verifications, strict=True):
        if verification.disposition == "confirmed":
            published.append(_candidate_to_comment(candidate))
        else:
            unpublished += 1
    if result.comments and not candidates:
        unpublished += len(result.comments)
    incomplete = unpublished > 0
    summary = result.summary
    status = result.review_status
    if incomplete:
        status = _downgrade_incomplete_status(status)
        coverage = _verification_coverage(verifications)
        summary = f"{summary}\n\n{coverage}" if summary else coverage
    prepared = ReviewResult(
        summary=summary,
        comments=tuple(published),
        provider=result.provider,
        model=result.model,
        learning_proposals=result.learning_proposals,
        review_status=status,
        limits=review_limits,
        source_context_coverage=result.source_context_coverage,
        evidence_policy="confirmed",
    )
    return PublishableReview(prepared, verifications, "confirmed", unpublished)
