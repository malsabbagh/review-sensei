"""OpenRouter qualification harness and support-record validation.

This module binds the generic promotion-record contract to an explicit
OpenRouter model, upstream routing policy, and remote endpoint scope.
Fixture-only or incomplete evidence cannot mint ``supported`` status.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ReviewInputError
from .evaluation import (
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
_OPENROUTER_PROVIDER = "openrouter"
_INITIAL_MODEL = "anthropic/claude-3.5-haiku"
_INITIAL_UPSTREAM = "anthropic"
_DEFAULT_LIMITATIONS = (
    "Qualified only for the declared model, upstream provider, and routing "
    "policy; not the full OpenRouter catalog.",
    "Synthetic corpus regression only; not broad model superiority.",
    "Mutable model aliases and unavailable revision metadata must be recorded "
    "honestly in observed_revision.",
)


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
            raise ReviewInputError("OpenRouter qualification target fields are required")
        if not is_allowlisted_openrouter_endpoint(self.base_url):
            raise ReviewInputError("OpenRouter qualification base_url is not allowlisted")

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
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _digest_list(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    digests: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ReviewInputError(f"{label}[{index}] must be a SHA-256 digest")
        digests.append(value)
    return tuple(digests)


@dataclass(frozen=True)
class OpenRouterQualificationRecord:
    """Machine-readable OpenRouter support metadata bound to promotion evidence."""

    promotion: PromotionRecord
    qualification_target: OpenRouterQualificationTarget
    evidence_references: tuple[str, ...]
    limitations: tuple[str, ...]

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
            raise ReviewInputError("OpenRouter qualification record requires limitations")
        if any(not isinstance(item, str) or not item.strip() for item in self.limitations):
            raise ReviewInputError("OpenRouter qualification limitations must be non-empty")
        if self.promotion.status == "supported":
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
        value = {
            "schema_version": "1.0",
            **self.promotion.to_dict(),
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
    """Parse and validate one OpenRouter qualification support record."""

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
    if fields["mode"] != "live":
        return
    if fields["model"] != target.model:
        raise ReviewInputError("OpenRouter qualification report model does not match target")
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
    evidence_references: Sequence[str] = (),
    limitations: Sequence[str] = _DEFAULT_LIMITATIONS,
    rollback_decision: str = "revert-to-baseline",
    status: str | None = None,
) -> OpenRouterQualificationRecord:
    """Build an OpenRouter qualification record from evaluation reports."""

    try:
        documents = tuple(reports)
    except TypeError as exc:
        raise ReviewInputError("OpenRouter qualification reports must be iterable") from exc
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
    references = tuple(evidence_references)
    if not references and documents:
        references = tuple(
            hashlib.sha256(
                json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                    "utf-8"
                )
            ).hexdigest()
            for report in documents
        )
    return OpenRouterQualificationRecord(
        promotion=promotion,
        qualification_target=target,
        evidence_references=references,
        limitations=tuple(limitations),
    )


def validate_openrouter_qualification_against_report(
    record: OpenRouterQualificationRecord,
    report: Mapping[str, Any],
) -> None:
    """Bind one qualification record to one evaluation report, failing closed."""

    validate_promotion_against_report(record.promotion, report)
    _validate_openrouter_live_report(report, record.qualification_target)


def require_supported_openrouter_qualification(
    record: OpenRouterQualificationRecord | Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
) -> OpenRouterQualificationRecord:
    """Fail-closed gate for OpenRouter support claims and approval eligibility."""

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
    minted = qualification_record_from_reports(
        reports,
        target=parsed.qualification_target,
        observed_revision=parsed.promotion.observed_revision,
        reproducibility=parsed.promotion.reproducibility,
        evaluated_at=parsed.promotion.evaluated_at,
        evidence_references=parsed.evidence_references,
        limitations=parsed.limitations,
        rollback_decision=parsed.promotion.rollback_decision,
        status="supported",
    )
    for name in (
        "engine_digest",
        "prompt_digest",
        "configuration_digest",
        "corpus_digest",
        "provider",
        "model",
        "observed_revision",
        "run_count",
        "status",
        "rollback_decision",
    ):
        if getattr(minted.promotion, name) != getattr(parsed.promotion, name):
            raise ReviewInputError(
                f"OpenRouter qualification {name} does not match live evaluation evidence"
            )
    if minted.evidence_references != parsed.evidence_references:
        raise ReviewInputError(
            "OpenRouter qualification evidence_references do not match live reports"
        )
    for report in reports:
        validate_openrouter_qualification_against_report(parsed, report)
    return parsed


__all__ = [
    "INITIAL_OPENROUTER_QUALIFICATION_TARGET",
    "OpenRouterQualificationRecord",
    "OpenRouterQualificationTarget",
    "qualification_record_from_reports",
    "require_supported_openrouter_qualification",
    "routing_policy_digest",
    "validate_openrouter_qualification_against_report",
    "validate_openrouter_qualification_record",
]
