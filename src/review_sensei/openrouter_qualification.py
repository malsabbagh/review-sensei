"""OpenRouter qualification harness and support-record validation.

This module binds the generic promotion-record contract to an explicit
OpenRouter model, upstream routing policy, and remote endpoint scope.
Fixture-only or incomplete evidence cannot mint ``supported`` status.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import ReviewInputError
from .evaluation import (
    MAX_JSON_FILE_BYTES,
    PromotionRecord,
    _report_promotion_fields,
    promotion_record_from_reports,
    require_supported_promotion,
    validate_promotion_against_report,
    validate_promotion_record,
)
from .providers.openrouter import (
    DEFAULT_OPENROUTER_BASE_URL,
    OpenRouterRoutingPolicy,
    is_allowlisted_openrouter_endpoint,
)
from .schemas import validate_public_document

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_OPENROUTER_PROVIDER = "openrouter"
_SCHEMA_VERSION = "1.0"
_INITIAL_MODEL = "anthropic/claude-3.5-haiku"
_INITIAL_UPSTREAM = "anthropic"
_ACCEPTED_REPORT_MODES = frozenset({"fixture", "live"})
# A retained artifact is the same file ``load_evaluation_report`` reads, so it
# carries the evaluation contract's own JSON ceiling rather than a second,
# independently drifting bound (ADR 0007).
MAX_EVIDENCE_ARTIFACT_BYTES = MAX_JSON_FILE_BYTES
_DEFAULT_LIMITATIONS = (
    "Qualified only for the declared model, upstream provider, and routing "
    "policy; not the full OpenRouter catalog.",
    "Synthetic corpus regression only; not broad model superiority.",
    "Mutable model aliases and unavailable revision metadata must be recorded "
    "honestly in observed_revision.",
)

# Published qualification slices. A new model or upstream provider stays
# outside the qualified slice until its target and evidence set are published,
# so the harness refuses to mint records for unlisted combinations.
PUBLISHED_QUALIFICATION_SLICES = frozenset({(_INITIAL_MODEL, _INITIAL_UPSTREAM)})


@dataclass(frozen=True)
class OpenRouterQualificationTarget:
    """Explicit OpenRouter configuration slice eligible for qualification."""

    model: str
    upstream_provider: str
    base_url: str = DEFAULT_OPENROUTER_BASE_URL

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.model, self.upstream_provider, self.base_url)
        ):
            raise ReviewInputError(
                "OpenRouter qualification target fields are required"
            )
        if not is_allowlisted_openrouter_endpoint(self.base_url):
            raise ReviewInputError(
                "OpenRouter qualification base_url is not allowlisted"
            )
        if (self.model, self.upstream_provider) not in PUBLISHED_QUALIFICATION_SLICES:
            raise ReviewInputError(
                "OpenRouter qualification target model and upstream provider "
                "are outside the published qualification slice"
            )

    @property
    def provider(self) -> str:
        return _OPENROUTER_PROVIDER

    def routing_policy(self) -> OpenRouterRoutingPolicy:
        return OpenRouterRoutingPolicy(upstream_provider=self.upstream_provider)

    def routing_policy_digest(self) -> str:
        return routing_policy_digest(self.routing_policy())

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "upstream_provider": self.upstream_provider,
            "base_url": self.base_url,
            "routing_policy_digest": self.routing_policy_digest(),
        }


INITIAL_OPENROUTER_QUALIFICATION_TARGET = OpenRouterQualificationTarget(
    model=_INITIAL_MODEL,
    upstream_provider=_INITIAL_UPSTREAM,
)


def routing_policy_digest(policy: OpenRouterRoutingPolicy) -> str:
    """Return a SHA-256 digest of non-secret OpenRouter routing policy fields."""

    if not isinstance(policy, OpenRouterRoutingPolicy):
        raise ReviewInputError("OpenRouter routing policy is required")
    payload = dict(policy.identity_fields())
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()


def _digest_list(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    digests: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ReviewInputError(f"{label}[{index}] must be a SHA-256 digest")
        digests.append(value)
    if len(set(digests)) != len(digests):
        raise ReviewInputError(f"{label} must reference distinct artifacts")
    return tuple(digests)


def evidence_reference_digest(artifact: bytes) -> str:
    """Return the SHA-256 digest of one retained evaluation-report artifact.

    ``evidence_references`` are digests of the exact retained report bytes, so
    an auditor can reproduce them with ``sha256sum openrouter-live-1.json``.
    Digests of re-serialized in-memory documents are not interchangeable with
    these values and are never minted by this module.
    """

    if not isinstance(artifact, (bytes, bytearray)):
        raise ReviewInputError("evaluation report artifact must be bytes")
    raw = bytes(artifact)
    if len(raw) > MAX_EVIDENCE_ARTIFACT_BYTES:
        raise ReviewInputError(
            "evaluation report artifact exceeds the configured size limit"
        )
    return hashlib.sha256(raw).hexdigest()


def _evidence_references_from_artifacts(
    artifacts: Sequence[bytes],
    documents: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Hash retained report bytes and bind each one to its parsed report."""

    try:
        payloads = tuple(artifacts)
    except TypeError as exc:
        raise ReviewInputError(
            "OpenRouter qualification report artifacts must be iterable"
        ) from exc
    if len(payloads) != len(documents):
        raise ReviewInputError(
            "OpenRouter qualification requires one retained report artifact "
            "per evaluation report"
        )
    digests: list[str] = []
    for index, (payload, document) in enumerate(zip(payloads, documents)):
        if not isinstance(payload, (bytes, bytearray)):
            raise ReviewInputError(
                f"report_artifacts[{index}] must be the retained report bytes"
            )
        raw = bytes(payload)
        digest = evidence_reference_digest(raw)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReviewInputError(
                f"report_artifacts[{index}] is not a UTF-8 JSON document"
            ) from exc
        if parsed != dict(document):
            raise ReviewInputError(
                f"report_artifacts[{index}] does not match the supplied "
                "evaluation report; pass the bytes the report was parsed from "
                "rather than a re-serialized copy, because the digest is taken "
                "over the retained bytes"
            )
        digests.append(digest)
    return _digest_list(digests, label="evidence_references")


def _resolve_evidence_references(
    documents: Sequence[Mapping[str, Any]],
    *,
    report_artifacts: Sequence[bytes] | None,
    evidence_references: Sequence[str],
    status: str,
) -> tuple[tuple[str, ...], _EvidenceProvenance]:
    declared = _digest_list(tuple(evidence_references), label="evidence_references")
    if report_artifacts is None:
        if status == "supported":
            raise ReviewInputError(
                "supported OpenRouter qualification requires the retained live "
                "report artifacts; evidence_references cannot be accepted "
                "without bytes the harness can hash itself"
            )
        return declared, _EvidenceProvenance.UNVERIFIED
    derived = _evidence_references_from_artifacts(report_artifacts, documents)
    if declared and declared != derived:
        raise ReviewInputError(
            "OpenRouter qualification evidence_references do not match the "
            "retained live report artifacts"
        )
    return derived, _EvidenceProvenance.ARTIFACT_DIGEST


class _EvidenceProvenance(enum.Enum):
    """Where a record's ``evidence_references`` came from.

    The members are module-private so a caller outside this module cannot name
    anything but the default, which keeps ``supported`` unconstructible by
    hand: only the mint path (which hashed retained bytes) and the parse path
    (which is reading an already-published record for verification) can claim
    a stronger provenance.
    """

    UNVERIFIED = "unverified"
    PUBLISHED = "published"
    ARTIFACT_DIGEST = "artifact-digest"


@dataclass(frozen=True)
class OpenRouterQualificationRecord:
    """Machine-readable OpenRouter support metadata bound to promotion evidence."""

    promotion: PromotionRecord
    qualification_target: OpenRouterQualificationTarget
    evidence_references: tuple[str, ...]
    limitations: tuple[str, ...]
    evidence_provenance: _EvidenceProvenance = _EvidenceProvenance.UNVERIFIED

    def __post_init__(self) -> None:
        if self.promotion.provider != _OPENROUTER_PROVIDER:
            raise ReviewInputError(
                "OpenRouter qualification record requires provider 'openrouter'"
            )
        if self.promotion.model != self.qualification_target.model:
            raise ReviewInputError(
                "OpenRouter qualification model does not match the declared target"
            )
        object.__setattr__(
            self,
            "evidence_references",
            _digest_list(self.evidence_references, label="evidence_references"),
        )
        if not self.limitations:
            raise ReviewInputError(
                "OpenRouter qualification record requires limitations"
            )
        if any(
            not isinstance(item, str) or not item.strip() for item in self.limitations
        ):
            raise ReviewInputError(
                "OpenRouter qualification limitations must be non-empty"
            )
        if self.promotion.status == "supported":
            if self.evidence_provenance is _EvidenceProvenance.UNVERIFIED:
                raise ReviewInputError(
                    "supported OpenRouter qualification records cannot be "
                    "constructed directly; mint one from retained live report "
                    "artifacts with qualification_record_from_reports"
                )
            if len(self.evidence_references) < 3:
                raise ReviewInputError(
                    "supported OpenRouter qualification requires at least three "
                    "evidence references"
                )
            if self.promotion.run_count < 3:
                raise ReviewInputError(
                    "supported OpenRouter qualification requires at least three runs"
                )

    @property
    def status(self) -> str:
        return self.promotion.status

    def to_dict(self) -> dict[str, Any]:
        promotion_fields = self.promotion.to_dict()
        if promotion_fields.get("schema_version") != _SCHEMA_VERSION:
            raise ReviewInputError(
                "OpenRouter qualification record requires promotion-record "
                f"schema_version {_SCHEMA_VERSION}"
            )
        value = {
            **promotion_fields,
            "qualification_target": self.qualification_target.to_dict(),
            "evidence_references": list(self.evidence_references),
            "limitations": list(self.limitations),
        }
        validate_public_document(value, "openrouter-qualification")
        return value


_PROMOTION_RECORD_KEYS = (
    "engine_digest",
    "prompt_digest",
    "configuration_digest",
    "corpus_digest",
    "provider",
    "model",
    "observed_revision",
    "run_count",
    "evaluated_at",
    "reproducibility",
    "status",
    "rollback_decision",
)


def validate_openrouter_qualification_record(
    value: Mapping[str, Any],
) -> OpenRouterQualificationRecord:
    """Parse one published OpenRouter qualification record structurally.

    This is a schema and self-consistency check only: it validates the document
    against the ``openrouter-qualification`` contract, rebuilds the promotion
    record, and confirms the declared ``routing_policy_digest`` matches the
    target. It cannot verify evidence provenance, because a published document
    carries digests rather than the retained bytes that produced them. A record
    parsed here is marked ``PUBLISHED``, not ``ARTIFACT_DIGEST``.

    Callers that need an evidence-bound answer, rather than a well-formed
    document, must use :func:`require_supported_openrouter_qualification` with
    the reports and retained artifacts.
    """

    if not isinstance(value, dict):
        raise ReviewInputError("OpenRouter qualification record must be a JSON object")
    validate_public_document(value, "openrouter-qualification")
    try:
        promotion = validate_promotion_record(
            {
                "schema_version": value["schema_version"],
                **{key: value[key] for key in _PROMOTION_RECORD_KEYS},
            }
        )
        target_payload = value["qualification_target"]
        target = OpenRouterQualificationTarget(
            model=target_payload["model"],
            upstream_provider=target_payload["upstream_provider"],
            base_url=target_payload.get("base_url", DEFAULT_OPENROUTER_BASE_URL),
        )
        declared_digest = target_payload["routing_policy_digest"]
        if declared_digest != target.routing_policy_digest():
            raise ReviewInputError(
                "OpenRouter qualification routing_policy_digest does not match target"
            )
        return OpenRouterQualificationRecord(
            promotion=promotion,
            qualification_target=target,
            evidence_references=tuple(value.get("evidence_references") or ()),
            limitations=tuple(value.get("limitations") or ()),
            evidence_provenance=_EvidenceProvenance.PUBLISHED,
        )
    except KeyError as exc:
        raise ReviewInputError("OpenRouter qualification record is incomplete") from exc


def _validate_openrouter_live_report(
    report: Mapping[str, Any],
    target: OpenRouterQualificationTarget,
) -> None:
    fields = _report_promotion_fields(report)
    if fields["provider"] != _OPENROUTER_PROVIDER:
        raise ReviewInputError("OpenRouter qualification requires openrouter reports")
    if fields["model"] != target.model:
        raise ReviewInputError(
            "OpenRouter qualification report model does not match target"
        )
    if fields["mode"] not in _ACCEPTED_REPORT_MODES:
        raise ReviewInputError("OpenRouter qualification report mode is not recognized")
    if fields["mode"] != "live":
        return
    run = report.get("run")
    if not isinstance(run, Mapping):
        raise ReviewInputError("evaluation report is incomplete")
    scope = run.get("endpoint_scope")
    if scope != "remote":
        raise ReviewInputError(
            "OpenRouter qualification requires remote endpoint_scope in live reports"
        )


def qualification_record_from_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    target: OpenRouterQualificationTarget,
    observed_revision: str,
    reproducibility: Mapping[str, Any],
    evaluated_at: str,
    report_artifacts: Sequence[bytes] | None = None,
    evidence_references: Sequence[str] = (),
    limitations: Sequence[str] = _DEFAULT_LIMITATIONS,
    rollback_decision: str = "revert-to-baseline",
    status: str | None = None,
) -> OpenRouterQualificationRecord:
    """Build an OpenRouter qualification record from evaluation reports.

    ``report_artifacts`` holds the exact retained bytes of each report in
    ``reports``, in the same order. ``supported`` status requires them because
    ``evidence_references`` must be digests the harness computed itself from
    those bytes; caller-supplied digests are cross-checked against them and are
    otherwise only accepted for non-supported records.
    """

    try:
        documents = tuple(reports)
    except TypeError as exc:
        raise ReviewInputError(
            "OpenRouter qualification reports must be iterable"
        ) from exc
    for report in documents:
        _validate_openrouter_live_report(report, target)
    promotion = promotion_record_from_reports(
        documents,
        observed_revision=observed_revision,
        reproducibility=reproducibility,
        evaluated_at=evaluated_at,
        rollback_decision=rollback_decision,
        status=status,
    )
    if promotion.provider != _OPENROUTER_PROVIDER:
        raise ReviewInputError("OpenRouter qualification requires openrouter provider")
    if promotion.model != target.model:
        raise ReviewInputError("OpenRouter qualification model does not match target")
    references, provenance = _resolve_evidence_references(
        documents,
        report_artifacts=report_artifacts,
        evidence_references=evidence_references,
        status=promotion.status,
    )
    return OpenRouterQualificationRecord(
        promotion=promotion,
        qualification_target=target,
        evidence_references=references,
        limitations=tuple(limitations),
        evidence_provenance=provenance,
    )


def validate_openrouter_qualification_against_report(
    record: OpenRouterQualificationRecord,
    report: Mapping[str, Any],
) -> None:
    """Bind one qualification record to one evaluation report, failing closed."""

    validate_promotion_against_report(record.promotion, report)
    _validate_openrouter_live_report(report, record.qualification_target)


def _require_attested_evaluated_at(value: str, *, now: datetime | None = None) -> None:
    """Reject support-claim timestamps that are malformed or not yet observed."""

    if not isinstance(value, str) or not _RFC3339_UTC.fullmatch(value):
        raise ReviewInputError(
            "OpenRouter qualification evaluated_at must be an RFC 3339 UTC instant"
        )
    stamp = value[:-1]
    layout = "%Y-%m-%dT%H:%M:%S.%f" if "." in stamp else "%Y-%m-%dT%H:%M:%S"
    try:
        observed = datetime.strptime(stamp, layout).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ReviewInputError(
            "OpenRouter qualification evaluated_at must be an RFC 3339 UTC instant"
        ) from exc
    if observed > (now or datetime.now(timezone.utc)):
        raise ReviewInputError(
            "OpenRouter qualification evaluated_at is dated in the future"
        )


def require_supported_openrouter_qualification(
    record: OpenRouterQualificationRecord | Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
    *,
    report_artifacts: Sequence[bytes] | None = None,
    expected_evaluated_at: str | None = None,
    expected_reproducibility: Mapping[str, Any] | None = None,
    allow_self_attested_inputs: bool = False,
    now: datetime | None = None,
) -> OpenRouterQualificationRecord:
    """Fail-closed gate for OpenRouter support claims and approval eligibility.

    The record is re-minted from ``reports`` and ``report_artifacts`` and then
    compared field by field, so every published value, including
    ``qualification_target``, ``evidence_references``, and ``limitations``,
    must agree with the evidence the gate can verify itself.

    ``evaluated_at`` and ``reproducibility`` are operator attestations that
    evaluation reports do not carry, so the gate cannot derive them from
    ``reports``. It always rejects an ``evaluated_at`` that is not an RFC 3339
    UTC instant or that is dated in the future.

    Beyond that, the caller must supply ``expected_evaluated_at`` and
    ``expected_reproducibility``: the comparison record is then minted from
    those values rather than from the record under test, so a stale or edited
    record fails instead of validating against itself. The weaker path, where
    the record attests to its own observation window, is available only by
    passing ``allow_self_attested_inputs=True``, which makes the reduced
    guarantee explicit at the call site rather than silently the default.

    ``now`` injects the clock used for the future-dating check so callers can
    reproduce gate decisions deterministically.
    """

    parsed = (
        record
        if isinstance(record, OpenRouterQualificationRecord)
        else validate_openrouter_qualification_record(record)
    )
    require_supported_promotion(parsed.promotion, reports)
    if parsed.qualification_target.model != parsed.promotion.model:
        raise ReviewInputError(
            "OpenRouter qualification target model does not match promotion record"
        )
    if not allow_self_attested_inputs and (
        expected_evaluated_at is None or expected_reproducibility is None
    ):
        raise ReviewInputError(
            "OpenRouter support gate requires expected_evaluated_at and "
            "expected_reproducibility; pass allow_self_attested_inputs=True to "
            "accept the record's own attestations and the weaker guarantee"
        )
    _require_attested_evaluated_at(parsed.promotion.evaluated_at, now=now)
    if expected_evaluated_at is not None:
        _require_attested_evaluated_at(expected_evaluated_at, now=now)
    minted = qualification_record_from_reports(
        reports,
        target=parsed.qualification_target,
        observed_revision=parsed.promotion.observed_revision,
        reproducibility=(
            parsed.promotion.reproducibility
            if expected_reproducibility is None
            else expected_reproducibility
        ),
        evaluated_at=(
            parsed.promotion.evaluated_at
            if expected_evaluated_at is None
            else expected_evaluated_at
        ),
        report_artifacts=report_artifacts,
        evidence_references=parsed.evidence_references,
        limitations=parsed.limitations,
        rollback_decision=parsed.promotion.rollback_decision,
        status="supported",
    )
    minted_promotion = minted.promotion.to_dict()
    parsed_promotion = parsed.promotion.to_dict()
    for name in _PROMOTION_RECORD_KEYS:
        if minted_promotion[name] != parsed_promotion[name]:
            raise ReviewInputError(
                f"OpenRouter qualification {name} does not match live evaluation evidence"
            )
    if minted.evidence_references != parsed.evidence_references:
        raise ReviewInputError(
            "OpenRouter qualification evidence_references do not match the "
            "retained live report artifacts"
        )
    if minted.to_dict() != parsed.to_dict():
        raise ReviewInputError(
            "OpenRouter qualification record does not match the record minted "
            "from live evaluation evidence"
        )
    for report in reports:
        validate_openrouter_qualification_against_report(parsed, report)
    return parsed


def load_supported_openrouter_qualification(
    document: Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
    *,
    report_artifacts: Sequence[bytes],
    expected_evaluated_at: str,
    expected_reproducibility: Mapping[str, Any],
    now: datetime | None = None,
) -> OpenRouterQualificationRecord:
    """Parse a published qualification document and gate it in one step.

    Prefer this over calling :func:`validate_openrouter_qualification_record`
    directly: that function is a structural parse, and a record it returns
    reports ``status == "supported"`` without any artifact check having run.
    This helper never returns a record that has not cleared the evidence-bound
    gate, so a downstream consumer cannot read a support claim it did not
    verify.
    """

    return require_supported_openrouter_qualification(
        validate_openrouter_qualification_record(document),
        reports,
        report_artifacts=report_artifacts,
        expected_evaluated_at=expected_evaluated_at,
        expected_reproducibility=expected_reproducibility,
        now=now,
    )


__all__ = [
    "INITIAL_OPENROUTER_QUALIFICATION_TARGET",
    "MAX_EVIDENCE_ARTIFACT_BYTES",
    "PUBLISHED_QUALIFICATION_SLICES",
    "OpenRouterQualificationRecord",
    "OpenRouterQualificationTarget",
    "evidence_reference_digest",
    "load_supported_openrouter_qualification",
    "qualification_record_from_reports",
    "require_supported_openrouter_qualification",
    "routing_policy_digest",
    "validate_openrouter_qualification_against_report",
    "validate_openrouter_qualification_record",
]
