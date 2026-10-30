"""Deterministic, fail-closed approval policy for GitHub review publication."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.coverage import coverage_approval_state
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import HumanInventoryResolution, PendingHumanReview
from review_sensei.models import ReviewResult
from review_sensei.review_verdict import ReviewVerdict
from review_sensei.schemas import validate_public_document

if TYPE_CHECKING:
    from .default_human_inventory import HumanInventoryReference
    from .eligibility_parts import (
        PartitionedEligibilityContext,
        PartitionedEligibilityRoot,
        PartitionReader,
        VisibleReader,
    )
    from .native_union import NativeUnionReader, NativeUnionReceipt

APPROVAL_ELIGIBILITY_SCHEMA_VERSION = "1"
APPROVAL_FACTS_SCHEMA_VERSION = "2"
APPROVAL_VERDICT_FACTS_SCHEMA_VERSION = "3"
APPROVAL_ELIGIBILITY_CROSS_FILE_VERSION = "2.0"
DEFAULT_HUMAN_ELIGIBILITY_VERSION = "5"
MAX_REFERENCED_ELIGIBILITY_BYTES = 32 * 1024
_MAX_ELIGIBILITY_MARKER_BYTES = 65_536
_ELIGIBILITY_MARKER_PREFIX = "<!-- reviewsensei:eligibility:v1"
REVIEW_STATUSES = frozenset({"complete", "partial", "incomplete", "summary-only"})
EVIDENCE_POLICIES = frozenset({"legacy", "confirmed"})
# ``not-required`` covers backends without a published qualification slice.
# ``qualified`` requires a verified supported record for the trusted effective
# configuration; ``missing`` and ``unverified`` both withhold approval but stay
# distinguishable so operators can see whether evidence was absent or wrong.
QUALIFICATION_STATES = frozenset({"not-required", "qualified", "missing", "unverified"})
_COVERAGE_BLOCKER_STATES = frozenset(
    {
        "reviewed",
        "partial",
        "incomplete",
        "unknown",
    }
)
MAINTAINER_COVERAGE_EXCEPTION_SATISFIED_GATES = frozenset(
    {
        "review-partial",
        "review-unverified",
        "coverage-partial",
    }
)


@dataclass(frozen=True)
class AutoApprovalDecision:
    """Explain whether a validated review may use GitHub's APPROVE event."""

    approved: bool
    blockers: tuple[str, ...] = ()
    outcome: str = "withheld"
    satisfied_gates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome == "withheld" and self.approved:
            object.__setattr__(self, "outcome", "approved")


@dataclass(frozen=True)
class ApprovalFacts:
    """Everything one approval decision depends on, besides the live thread scan.

    The result-derived fields are frozen at publication time so a delayed
    finalization cannot re-derive eligibility from a different result. The
    unresolved-blocking-thread state is deliberately excluded: the finalizer
    reads it live from the reviewed head instead of trusting a persisted value.
    ``check_published`` is retained as a diagnostic fact; inability to publish a
    merge-gate check does not make an otherwise eligible review ineligible for
    approval.

    Provider identity is bound by the enclosing result/configuration identity.
    A verdict's policy digest identifies convergence policy, not a provider;
    parsing these facts validates shape and does not authenticate their source.
    """

    enabled: bool
    app_authored: bool
    review_status: str
    evidence_policy: str
    has_blocking_findings: bool
    has_human_adjudication_findings: bool
    coverage_blocker: str | None
    qualification: str = "not-required"
    check_published: bool = True
    has_open_review_threads: bool | None = None
    persistence_status: str | None = None
    provider: str | None = None
    qualification_binding: str | None = None
    review_verdict: ReviewVerdict | None = None

    def __post_init__(self) -> None:
        import re

        if self.review_verdict is not None and (
            type(self.review_verdict) is not ReviewVerdict or self.provider is None
        ):
            raise ReviewInputError("approval review verdict is invalid")
        if self.provider is not None and (
            not isinstance(self.provider, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", self.provider) is None
        ):
            raise ReviewInputError("approval facts provider is invalid")
        if self.qualification_binding is not None and (
            self.provider != "openrouter"
            or not isinstance(self.qualification_binding, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.qualification_binding) is None
        ):
            raise ReviewInputError("approval qualification binding is invalid")
        if self.persistence_status is not None and (
            self.persistence_status != "capacity-exceeded"
            or self.review_status != "partial"
        ):
            raise ReviewInputError("approval facts persistence status is invalid")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": APPROVAL_VERDICT_FACTS_SCHEMA_VERSION
            if self.review_verdict is not None
            else APPROVAL_FACTS_SCHEMA_VERSION
            if self.provider is not None
            else APPROVAL_ELIGIBILITY_SCHEMA_VERSION,
            "enabled": self.enabled,
            "app_authored": self.app_authored,
            "review_status": self.review_status,
            "evidence_policy": self.evidence_policy,
            "has_blocking_findings": self.has_blocking_findings,
            "has_human_adjudication_findings": self.has_human_adjudication_findings,
            "coverage_blocker": self.coverage_blocker,
            "qualification": self.qualification,
            "check_published": self.check_published,
        }
        if self.review_verdict is not None:
            value["review_verdict"] = self.review_verdict.to_dict()
        if self.persistence_status is not None:
            value["persistence_status"] = self.persistence_status
        if self.provider is not None:
            value["provider"] = self.provider
        if self.qualification_binding is not None:
            value["qualification_binding"] = self.qualification_binding
        return value

    @classmethod
    def from_dict(cls, value: object) -> "ApprovalFacts":
        """Parse the persisted result-side facts; the thread state is not stored."""

        if not isinstance(value, Mapping):
            raise ReviewInputError("approval facts must be an object")
        expected = {
            "schema_version",
            "enabled",
            "app_authored",
            "review_status",
            "evidence_policy",
            "has_blocking_findings",
            "has_human_adjudication_findings",
            "coverage_blocker",
            "qualification",
            "check_published",
        }
        if not expected <= set(value) or set(value) - expected - {
            "persistence_status",
            "provider",
            "qualification_binding",
            "review_verdict",
        }:
            raise ReviewInputError("approval facts must contain the documented fields")
        version = value.get("schema_version")
        if version not in {
            APPROVAL_ELIGIBILITY_SCHEMA_VERSION,
            APPROVAL_FACTS_SCHEMA_VERSION,
            APPROVAL_VERDICT_FACTS_SCHEMA_VERSION,
        }:
            raise ReviewInputError("approval facts schema_version is invalid")
        if (version == APPROVAL_VERDICT_FACTS_SCHEMA_VERSION) != (
            "review_verdict" in value
        ):
            raise ReviewInputError("approval review verdict version is invalid")
        if (
            version
            in {
                APPROVAL_FACTS_SCHEMA_VERSION,
                APPROVAL_VERDICT_FACTS_SCHEMA_VERSION,
            }
            and value.get("provider") is None
        ) or (
            version == APPROVAL_ELIGIBILITY_SCHEMA_VERSION
            and ("provider" in value or "qualification_binding" in value)
        ):
            raise ReviewInputError("approval facts provider version is invalid")
        for label in (
            "enabled",
            "app_authored",
            "has_blocking_findings",
            "has_human_adjudication_findings",
            "check_published",
        ):
            if not isinstance(value.get(label), bool):
                raise ReviewInputError(f"approval facts {label} must be a boolean")
        if value.get("review_status") not in REVIEW_STATUSES:
            raise ReviewInputError("approval facts review_status is invalid")
        if value.get("evidence_policy") not in EVIDENCE_POLICIES:
            raise ReviewInputError("approval facts evidence_policy is invalid")
        persistence_status = value.get("persistence_status")
        if persistence_status is not None and (
            persistence_status != "capacity-exceeded"
            or value.get("review_status") != "partial"
        ):
            raise ReviewInputError("approval facts persistence status is invalid")
        coverage_blocker = value.get("coverage_blocker")
        if coverage_blocker is not None and coverage_blocker not in (
            _COVERAGE_BLOCKER_STATES
        ):
            raise ReviewInputError("approval facts coverage_blocker is invalid")
        if value.get("qualification") not in QUALIFICATION_STATES:
            raise ReviewInputError("approval facts qualification is invalid")
        return cls(
            enabled=value["enabled"],
            app_authored=value["app_authored"],
            review_status=value["review_status"],
            evidence_policy=value["evidence_policy"],
            has_blocking_findings=value["has_blocking_findings"],
            has_human_adjudication_findings=value["has_human_adjudication_findings"],
            coverage_blocker=coverage_blocker,
            qualification=value["qualification"],
            check_published=value["check_published"],
            persistence_status=persistence_status,
            provider=value.get("provider"),
            qualification_binding=value.get("qualification_binding"),
            review_verdict=ReviewVerdict.from_dict(value["review_verdict"])
            if "review_verdict" in value
            else None,
        )


@dataclass(frozen=True)
class ReviewApprovalEligibility:
    """Trusted, exact-head eligibility carried from publication to finalization.

    The document is durable: it is persisted beside the published review and
    parsed back before a delayed finalization, so a missing, malformed, or
    stale document withholds approval instead of implying it.
    """

    head_sha: str
    result_digest: str
    facts: ApprovalFacts
    human_review: PendingHumanReview | None = None
    partitioned_root: PartitionedEligibilityRoot | None = None
    human_inventory_reference: HumanInventoryReference | None = None
    quarantine_assessments: dict[str, Any] | None = None
    native_union: NativeUnionReceipt | None = None

    def to_dict(self) -> dict[str, object]:
        if self.native_union is not None:
            from .native_union import NativeUnionReceipt

            if (
                type(self.native_union) is not NativeUnionReceipt
                or self.native_union.head_sha != self.head_sha
                or self.native_union.result_digest != self.result_digest
            ):
                raise ReviewInputError("native union eligibility identity conflicts")
            return {
                "schema_version": "8",
                "current": replace(self, native_union=None).to_dict(),
                "native_union": self.native_union.to_document(),
            }
        if self.quarantine_assessments is not None:
            from review_sensei.quarantine_assessments import validate_view

            view = validate_view(self.quarantine_assessments)
            if self.human_review is not None and view["fresh_human_pending"] != bool(
                self.human_review.pending
            ):
                raise ReviewInputError("quarantine fresh assessment facts conflict")
            if self.facts.has_human_adjudication_findings != (
                view["fresh_human_pending"] or bool(view["pending"])
            ):
                raise ReviewInputError("quarantine combined assessment facts conflict")
            current = replace(
                self,
                quarantine_assessments=None,
                facts=replace(
                    self.facts,
                    has_human_adjudication_findings=view["fresh_human_pending"],
                ),
            )
            return {
                "schema_version": "7",
                "current": current.to_dict(),
                "quarantine_assessments": view,
            }
        if self.human_inventory_reference is not None:
            return self._referenced_document()
        if self.partitioned_root is not None:
            return self.partitioned_root.to_document(self)
        value: dict[str, object] = {
            "schema_version": APPROVAL_ELIGIBILITY_SCHEMA_VERSION,
            "head_sha": self.head_sha,
            "result_digest": self.result_digest,
            "facts": self.facts.to_dict(),
        }
        if self.human_review is not None:
            human_document = self.human_review.to_dict()
            value["human_review"] = human_document
            if "inventory" in human_document:
                value["schema_version"] = "3"
            elif any(item.required_paths for item in self.human_review.findings):
                value["schema_version"] = APPROVAL_ELIGIBILITY_CROSS_FILE_VERSION
        return value

    def _referenced_document(self) -> dict[str, object]:
        from .default_human_inventory import HumanInventoryReference

        reference = self.human_inventory_reference
        inventory = self.human_review
        if (
            not isinstance(reference, HumanInventoryReference)
            or not isinstance(inventory, PendingHumanReview)
            or self.partitioned_root is not None
            or self.facts.has_human_adjudication_findings != bool(inventory.pending)
        ):
            raise ReviewInputError("human inventory reference eligibility conflicts")
        _validate_eligibility_identity(self.head_sha, self.result_digest)
        value: dict[str, object] = {
            "schema_version": DEFAULT_HUMAN_ELIGIBILITY_VERSION,
            "head_sha": self.head_sha,
            "result_digest": self.result_digest,
            "facts": self.facts.to_dict(),
            "human_inventory_reference": reference.to_dict(),
            "human_resolution": HumanInventoryResolution.from_inventory(
                inventory
            ).to_dict(),
        }
        validate_public_document(value, "eligibility-v5")
        # The initial marker must support every future resolved full ID. This
        # bounds the complete marker; the publisher also reserves surrounding
        # visible prose, finding index and lifecycle framing before any write.
        maximum = dict(
            value,
            human_resolution=HumanInventoryResolution(
                inventory.inventory_digest,
                tuple(item.fingerprint for item in inventory.findings),
            ).to_dict(),
            facts=replace(self.facts, has_human_adjudication_findings=False).to_dict(),
        )
        wire = canonical_bytes(maximum)
        marker_bytes = (
            len(_ELIGIBILITY_MARKER_PREFIX)
            + 1
            + len(base64.urlsafe_b64encode(wire))
            + len(" -->")
        )
        if (
            len(wire) > MAX_REFERENCED_ELIGIBILITY_BYTES
            or marker_bytes > _MAX_ELIGIBILITY_MARKER_BYTES
        ):
            raise ReviewInputError(
                "human inventory eligibility exceeds future capacity"
            )
        return value

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        human_inventory_reader: Callable[[HumanInventoryReference], PendingHumanReview]
        | None = None,
        partition_reader: PartitionReader | None = None,
        expected_context: PartitionedEligibilityContext | None = None,
        visible_reader: VisibleReader | None = None,
        native_union_reader: NativeUnionReader | None = None,
    ) -> "ReviewApprovalEligibility":
        if isinstance(value, Mapping) and value.get("schema_version") == "8":
            from .native_union import NativeUnionReceipt

            if (
                set(value) != {"schema_version", "current", "native_union"}
                or not isinstance(value["current"], Mapping)
                or value["current"].get("schema_version")
                not in {"1", "2.0", "3", "4", "5", "7"}
            ):
                raise ReviewInputError("native union eligibility envelope is invalid")
            validate_public_document(value, "eligibility-v8")
            receipt = NativeUnionReceipt.from_document(value["native_union"])
            current = cls.from_dict(
                value["current"],
                human_inventory_reader=human_inventory_reader,
                partition_reader=partition_reader,
                expected_context=expected_context,
                visible_reader=visible_reader,
            )
            if (
                receipt.head_sha != current.head_sha
                or receipt.result_digest != current.result_digest
                or native_union_reader is None
            ):
                raise ReviewInputError(
                    "native union requires its complete scoped reader"
                )
            from review_sensei.bounded_evidence import canonical_bytes

            receipt.require_evidence(
                native_union_reader(receipt),
                document_sha256=hashlib.sha256(canonical_bytes(value)).hexdigest(),
            )
            return replace(current, native_union=receipt)
        if isinstance(value, Mapping) and value.get("schema_version") == "7":
            from review_sensei.quarantine_assessments import validate_view

            if (
                set(value) != {"schema_version", "current", "quarantine_assessments"}
                or not isinstance(value["current"], Mapping)
                or value["current"].get("schema_version")
                not in {"1", "2.0", "3", "4", "5"}
            ):
                raise ReviewInputError("quarantine eligibility envelope is invalid")
            validate_public_document(value, "eligibility-v7")
            view = validate_view(value["quarantine_assessments"])
            current = cls.from_dict(
                value["current"],
                human_inventory_reader=human_inventory_reader,
                partition_reader=partition_reader,
                expected_context=expected_context,
                visible_reader=visible_reader,
            )
            if (
                current.facts.has_human_adjudication_findings
                != view["fresh_human_pending"]
            ):
                raise ReviewInputError("quarantine fresh assessment facts conflict")
            combined = replace(
                current,
                quarantine_assessments=view,
                facts=replace(
                    current.facts,
                    has_human_adjudication_findings=(
                        view["fresh_human_pending"] or bool(view["pending"])
                    ),
                ),
            )
            combined.to_dict()
            return combined
        if (
            isinstance(value, Mapping)
            and value.get("schema_version") == DEFAULT_HUMAN_ELIGIBILITY_VERSION
        ):
            return cls._from_referenced_document(
                value, human_inventory_reader=human_inventory_reader
            )
        if isinstance(value, Mapping) and value.get("schema_version") == "4":
            from .eligibility_parts import parse_partitioned_eligibility

            return parse_partitioned_eligibility(
                value,
                partition_reader=partition_reader,
                expected_context=expected_context,
                visible_reader=visible_reader,
            )
        if not isinstance(value, Mapping):
            raise ReviewInputError("approval eligibility must be an object")
        if set(value) not in (
            {"schema_version", "head_sha", "result_digest", "facts"},
            {"schema_version", "head_sha", "result_digest", "facts", "human_review"},
        ):
            raise ReviewInputError(
                "approval eligibility must contain the documented fields"
            )
        if value.get("schema_version") not in (
            APPROVAL_ELIGIBILITY_SCHEMA_VERSION,
            APPROVAL_ELIGIBILITY_CROSS_FILE_VERSION,
            "3",
        ):
            raise ReviewInputError("approval eligibility schema_version is invalid")
        head_sha = value.get("head_sha")
        result_digest = value.get("result_digest")
        _validate_eligibility_identity(head_sha, result_digest)
        assert isinstance(head_sha, str) and isinstance(result_digest, str)
        facts = ApprovalFacts.from_dict(value.get("facts"))
        human_review = (
            PendingHumanReview.from_dict(value["human_review"])
            if "human_review" in value
            else None
        )
        cross_file = human_review is not None and any(
            item.required_paths for item in human_review.findings
        )
        encoded = (
            isinstance(value.get("human_review"), dict)
            and "inventory" in value["human_review"]
        )
        if encoded != (value.get("schema_version") == "3"):
            raise ReviewInputError("approval eligibility encoding version is invalid")
        if not encoded and cross_file != (
            value.get("schema_version") == APPROVAL_ELIGIBILITY_CROSS_FILE_VERSION
        ):
            raise ReviewInputError(
                "approval eligibility requirements version is invalid"
            )
        if human_review is not None and facts.has_human_adjudication_findings != bool(
            human_review.pending
        ):
            raise ReviewInputError("human review facts disagree with the inventory")
        return cls(
            head_sha=head_sha,
            result_digest=result_digest,
            facts=facts,
            human_review=human_review,
        )

    @classmethod
    def _from_referenced_document(
        cls,
        value: Mapping[str, object],
        *,
        human_inventory_reader: Callable[[HumanInventoryReference], PendingHumanReview]
        | None,
    ) -> ReviewApprovalEligibility:
        if set(value) != {
            "schema_version",
            "head_sha",
            "result_digest",
            "facts",
            "human_inventory_reference",
            "human_resolution",
        }:
            raise ReviewInputError("human inventory eligibility fields are invalid")
        if human_inventory_reader is None:
            raise ReviewInputError(
                "human inventory eligibility requires a trusted reader"
            )
        from .default_human_inventory import HumanInventoryReference

        head_sha, result_digest = value["head_sha"], value["result_digest"]
        _validate_eligibility_identity(head_sha, result_digest)
        assert isinstance(head_sha, str) and isinstance(result_digest, str)
        reference = HumanInventoryReference.from_dict(
            value["human_inventory_reference"]
        )
        resolution = HumanInventoryResolution.from_dict(value["human_resolution"])
        facts = ApprovalFacts.from_dict(value["facts"])
        validate_public_document(value, "eligibility-v5")
        inventory = human_inventory_reader(reference)
        if not isinstance(inventory, PendingHumanReview):
            raise ReviewInputError("human inventory reader returned invalid inventory")
        restored = resolution.restore(inventory)
        eligibility = cls(
            head_sha=head_sha,
            result_digest=result_digest,
            facts=facts,
            human_review=restored,
            human_inventory_reference=reference,
        )
        # Includes pending/facts agreement and the full future resolution budget.
        eligibility.to_dict()
        return eligibility

    def evaluate(
        self,
        *,
        app_authored: bool,
        has_open_review_threads: bool | None,
        coverage_uncertainty_accepted: bool = False,
    ) -> AutoApprovalDecision:
        """Evaluate the persisted facts with the live exact-head thread state."""

        if self.quarantine_assessments is not None:
            self.to_dict()
        return evaluate_approval_facts(
            replace(
                self.facts,
                app_authored=app_authored,
                has_open_review_threads=has_open_review_threads,
            ),
            coverage_uncertainty_accepted=coverage_uncertainty_accepted,
        )


def _validate_eligibility_identity(head_sha: object, result_digest: object) -> None:
    if (
        not isinstance(head_sha, str)
        or len(head_sha) != 40
        or any(character not in "0123456789abcdef" for character in head_sha)
        or not isinstance(result_digest, str)
        or len(result_digest) != 64
        or any(character not in "0123456789abcdef" for character in result_digest)
    ):
        raise ReviewInputError("approval eligibility identity is invalid")


def approval_facts_from_result(
    result: ReviewResult,
    *,
    enabled: bool,
    app_authored: bool,
    qualification: str = "not-required",
    check_published: bool = True,
    has_open_review_threads: bool | None = None,
) -> ApprovalFacts:
    """Freeze the result-derived approval inputs for one validated review."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("approval facts require a validated review result")
    qualification = restrict_result_qualification(result, qualification)
    coverage_state = (
        coverage_approval_state(result.coverage)
        if result.coverage is not None
        else None
    )
    return ApprovalFacts(
        enabled=enabled,
        app_authored=app_authored,
        review_status=result.review_status,
        persistence_status=result.persistence_status,
        evidence_policy=result.evidence_policy,
        has_blocking_findings=has_blocking_findings(result),
        has_human_adjudication_findings=has_human_adjudication_findings(result),
        review_verdict=result.review_verdict,
        coverage_blocker=coverage_state,
        qualification=qualification,
        provider=result.provider.strip().lower(),
        check_published=check_published,
        has_open_review_threads=has_open_review_threads,
    )


def approval_eligibility_from_result(
    result: ReviewResult,
    *,
    head_sha: str,
    enabled: bool,
    app_authored: bool,
    qualification: str = "not-required",
    check_published: bool = True,
    base_sha: str | None = None,
    retained_eligibility: ReviewApprovalEligibility | None = None,
) -> ReviewApprovalEligibility:
    """Build the persisted eligibility document for one published review."""

    from .publication import review_result_digest

    eligibility = ReviewApprovalEligibility(
        head_sha=head_sha,
        result_digest=review_result_digest(result),
        human_review=PendingHumanReview.from_result(result, base_sha),
        facts=approval_facts_from_result(
            result,
            enabled=enabled,
            app_authored=app_authored,
            qualification=qualification,
            check_published=check_published,
        ),
    )
    if retained_eligibility is None:
        return eligibility
    retained = retained_eligibility
    if (
        not isinstance(retained, ReviewApprovalEligibility)
        or retained.head_sha != head_sha
        or retained.human_review is None
        or retained.human_review.base_sha != base_sha
    ):
        raise ReviewInputError("broader review retained authority is invalid")
    # Discovery omission never retires an earlier human or blocking obligation.
    findings = {item.fingerprint: item for item in retained.human_review.findings}
    for item in eligibility.human_review.findings if eligibility.human_review else ():
        if item.fingerprint in findings:
            previous = findings[item.fingerprint]
            if previous.path != item.path or previous.body != item.body:
                raise ReviewInputError("broader review finding authority conflicts")
            # Stable display identity alone cannot transfer a resolution into
            # different metadata/requirements. Retain the original immutable
            # obligation, including unknown legacy metadata. New result facts
            # still contribute their independently stricter approval blockers.
            item = previous
        findings[item.fingerprint] = item
    inventory = PendingHumanReview(
        retained.human_review.base_sha,
        tuple(findings.values()),
        retained.human_review.resolved,
    )
    old, new = retained.facts, eligibility.facts
    verdict = new.review_verdict
    if old.review_verdict is not None:
        if verdict is None:
            verdict = old.review_verdict
        else:
            if verdict.policy_digest != old.review_verdict.policy_digest:
                raise ReviewInputError("broader review verdict policy conflicts")
            verdict = replace(
                verdict,
                unselected_blocking_defect=verdict.unselected_blocking_defect
                or old.review_verdict.unselected_blocking_defect,
                unselected_human_decision=verdict.unselected_human_decision
                or old.review_verdict.unselected_human_decision,
            )
    return replace(
        eligibility,
        human_review=inventory,
        facts=replace(
            new,
            review_verdict=verdict,
            persistence_status=new.persistence_status or old.persistence_status,
            enabled=new.enabled and old.enabled,
            app_authored=new.app_authored or old.app_authored,
            review_status=(
                "partial"
                if old.evidence_policy == "confirmed"
                and new.evidence_policy != "confirmed"
                else new.review_status
                if old.review_status == "complete"
                else old.review_status
            ),
            evidence_policy="confirmed"
            if old.evidence_policy == "confirmed"
            else new.evidence_policy,
            has_blocking_findings=new.has_blocking_findings
            or old.has_blocking_findings,
            has_human_adjudication_findings=bool(inventory.pending),
            coverage_blocker=old.coverage_blocker
            if old.coverage_blocker not in (None, "reviewed")
            else new.coverage_blocker,
            qualification=old.qualification
            if old.qualification not in ("qualified", "not-required")
            else new.qualification,
        ),
    )


def evaluate_approval_facts(
    facts: ApprovalFacts,
    *,
    coverage_uncertainty_accepted: bool = False,
) -> AutoApprovalDecision:
    """Apply the approval criteria to frozen facts.

    Check publication is reported separately because it controls merge-gate
    availability, not whether the review itself qualifies for approval.
    """

    blockers: list[str] = []
    satisfied_gates: list[str] = []
    coverage_exception_eligible = (
        coverage_uncertainty_accepted is True
        and facts.review_status == "partial"
        and facts.persistence_status != "capacity-exceeded"
        and facts.coverage_blocker in {None, "reviewed", "partial"}
    )
    if not isinstance(facts.enabled, bool):
        blockers.append("auto-approval-enabled-invalid")
    if not isinstance(facts.app_authored, bool):
        blockers.append("app-authored-flag-invalid")
    if facts.has_open_review_threads is not None and not isinstance(
        facts.has_open_review_threads, bool
    ):
        blockers.append("review-threads-invalid")
    # Approval remains the compatible default. A repository may explicitly
    # disable it while retaining the same review-publication and
    # marker/idempotency guarantees.
    if facts.enabled is not True:
        blockers.append("auto-approval-disabled")
    if facts.app_authored is True:
        blockers.append("app-authored-pull-request")
    if facts.review_verdict is not None:
        if facts.review_verdict.unselected_blocking_defect:
            blockers.append("review-verdict-blocking")
        if facts.review_verdict.unselected_human_decision:
            blockers.append("review-verdict-human")
    if facts.has_blocking_findings is True:
        blockers.append("blocking-findings-open")
    if facts.has_human_adjudication_findings is True:
        blockers.append("human-adjudication-open")
    if facts.has_open_review_threads is None:
        blockers.append("review-threads-incomplete")
    elif facts.has_open_review_threads is True:
        blockers.append("review-threads-open")
    status = facts.review_status
    if status in {"partial", "incomplete", "summary-only"}:
        status_blocker = (
            "baseline-capacity-exceeded"
            if facts.persistence_status == "capacity-exceeded"
            else f"review-{status}"
        )
        if (
            coverage_exception_eligible
            and status_blocker in MAINTAINER_COVERAGE_EXCEPTION_SATISFIED_GATES
        ):
            satisfied_gates.append(status_blocker)
        else:
            blockers.append(status_blocker)
    elif status != "complete":
        blockers.append("review-status-invalid")
    if facts.evidence_policy not in {"legacy", "confirmed"}:
        blockers.append("evidence-policy-invalid")
    # Confirmed reviews that are not fully verified keep the status blocker
    # above and add review-unverified so callers can distinguish evidence-
    # gated partial coverage from other partial reviews.
    elif facts.evidence_policy == "confirmed" and status != "complete":
        if (
            coverage_exception_eligible
            and "review-unverified" in MAINTAINER_COVERAGE_EXCEPTION_SATISFIED_GATES
        ):
            satisfied_gates.append("review-unverified")
        else:
            blockers.append("review-unverified")
    if facts.coverage_blocker is not None and facts.coverage_blocker != "reviewed":
        cov_blocker = f"coverage-{facts.coverage_blocker}"
        if (
            coverage_exception_eligible
            and cov_blocker in MAINTAINER_COVERAGE_EXCEPTION_SATISFIED_GATES
        ):
            satisfied_gates.append(cov_blocker)
        else:
            blockers.append(cov_blocker)
    if facts.qualification not in {"not-required", "qualified"}:
        blockers.append(f"qualification-{facts.qualification}")
    approved = not blockers
    outcome = (
        "maintainer-coverage-exception"
        if approved and satisfied_gates
        else ("approved" if approved else "withheld")
    )
    return AutoApprovalDecision(
        approved=approved,
        blockers=tuple(blockers),
        outcome=outcome,
        satisfied_gates=tuple(satisfied_gates),
    )


def restrict_result_qualification(result: ReviewResult, qualification: str) -> str:
    """A provider label or caller status cannot substitute for checked evidence.

    Only the publisher's independently configured retained-evidence authority
    may qualify OpenRouter facts. Neither accepting string carries that proof.
    Preserve diagnostic withholding states and other providers' policy.
    """

    if result.provider.strip().lower() == "openrouter" and qualification in {
        "not-required",
        "qualified",
    }:
        return "unverified"
    return qualification


def qualification_state_for_configuration(
    *,
    provider: str,
    model: str | None = None,
    upstream_provider: str | None = None,
    base_url: str | None = None,
    qualification_status: str = "unknown",
) -> str:
    """Map one effective provider configuration onto a qualification state.

    The qualification contract is scoped to the backends a published
    qualification slice covers, so every other backend is ``not-required``.
    For an OpenRouter deployment the slice key is the full model, upstream
    provider, and base URL combination: a configuration the published slices do
    not cover is ``unverified`` because no published evidence could apply to
    it, and a covered configuration is ``missing`` without checked evidence.
    ``qualification_status`` remains an accepted compatibility argument; a
    profile/deployment status string is not retained report-byte evidence and
    cannot authorize approval.
    """

    from review_sensei.openrouter_qualification import PUBLISHED_QUALIFICATION_SLICES

    if not isinstance(provider, str) or provider.strip().lower() != "openrouter":
        return "not-required"
    slice_key = (
        model if isinstance(model, str) else None,
        upstream_provider if isinstance(upstream_provider, str) else None,
        base_url if isinstance(base_url, str) else None,
    )
    if slice_key not in PUBLISHED_QUALIFICATION_SLICES:
        return "unverified"
    return "missing"


def qualification_state_for_publication(
    *,
    provider: str,
    model: str | None,
    provider_identity: Mapping[str, object] | None = None,
) -> str:
    """Derive the qualification verdict for one published review.

    ``provider_identity`` is the trusted provider document the reviewed run was
    bound to (``configuration_context['provider']``), which is the only source
    that carries the endpoint and upstream routing a qualification slice is
    keyed by. Without it the review's own provider and model are all that can
    be established: enough to clear a backend with no qualification contract,
    and never enough to claim qualification for an OpenRouter deployment.
    """

    identity = provider_identity if isinstance(provider_identity, Mapping) else {}
    base_url = identity.get("base_url")
    if not isinstance(base_url, str):
        base_url = None
    policy = identity.get("openrouter_policy")
    upstream_provider = (
        policy.get("upstream_provider") if isinstance(policy, Mapping) else None
    )
    return qualification_state_for_configuration(
        provider=provider,
        model=model,
        upstream_provider=upstream_provider,
        base_url=base_url,
    )


def evaluate_auto_approval(
    *,
    enabled: bool = True,
    app_authored: bool,
    result: ReviewResult,
    has_open_review_threads: bool | None = None,
    qualification: str = "not-required",
) -> AutoApprovalDecision:
    """Apply the repository's conservative approval criteria.

    A finding blocks when it is explicitly classified as blocking. When the
    optional classification is absent, only canonical critical/high severity
    blocks. Missing, lower-severity, and legacy free-form severity values are
    non-blocking. ``has_open_review_threads`` must describe only unresolved
    *blocking ReviewSensei findings*; non-blocking and human threads are not
    automatic-approval blockers. ``qualification`` carries the #115
    OpenRouter qualification verdict; a configuration outside the qualified
    slice or without verified evidence withholds approval.
    """

    if not isinstance(result, ReviewResult):
        # An unvalidated result keeps the original vocabulary: the caller can
        # still tell an invalid input apart from a disabled approval, and none
        # of the result-derived checks below can run.
        blockers: list[str] = []
        if not isinstance(enabled, bool):
            blockers.append("auto-approval-enabled-invalid")
        if not isinstance(app_authored, bool):
            blockers.append("app-authored-flag-invalid")
        if has_open_review_threads is not None and not isinstance(
            has_open_review_threads, bool
        ):
            blockers.append("review-threads-invalid")
        blockers.append("review-result-invalid")
        if enabled is not True:
            blockers.append("auto-approval-disabled")
        if app_authored is True:
            blockers.append("app-authored-pull-request")
        if has_open_review_threads is None:
            blockers.append("review-threads-incomplete")
        elif has_open_review_threads is True:
            blockers.append("review-threads-open")
        blockers.append("review-incomplete")
        return AutoApprovalDecision(approved=False, blockers=tuple(blockers))
    return evaluate_approval_facts(
        approval_facts_from_result(
            result,
            enabled=enabled,
            app_authored=app_authored,
            qualification=qualification,
            has_open_review_threads=has_open_review_threads,
        )
    )


def has_blocking_findings(result: ReviewResult) -> bool:
    """Whether a validated result contains a finding that blocks approval."""

    return any(comment.blocks_approval for comment in result.comments)


def has_human_adjudication_findings(result: ReviewResult) -> bool:
    """Whether a validated result still needs a human merge decision."""

    return any(comment.needs_human for comment in result.comments)


_WITHHELD_DIAGNOSTICS = {
    "auto-approval-disabled": "auto_approval_disabled",
    "auto-approval-enabled-invalid": "approval_withheld",
    "app-authored-pull-request": "app_authored",
    "app-authored-flag-invalid": "approval_withheld",
    "review-verdict-blocking": "required_fixes_open",
    "review-verdict-human": "human_adjudication_open",
    "blocking-findings-open": "required_fixes_open",
    "human-adjudication-open": "human_adjudication_open",
    "review-threads-incomplete": "review_threads_incomplete",
    "review-threads-invalid": "approval_withheld",
    "review-threads-open": "required_fixes_open",
    "review-partial": "review_incomplete",
    "baseline-capacity-exceeded": "baseline_capacity_exceeded",
    "review-incomplete": "review_incomplete",
    "review-summary-only": "review_incomplete",
    "review-status-invalid": "review_incomplete",
    "review-unverified": "review_incomplete",
    "evidence-policy-invalid": "review_incomplete",
    "coverage-partial": "review_incomplete",
    "baseline_capacity_exceeded": "baseline_capacity_exceeded",
    "coverage-incomplete": "review_incomplete",
    "coverage-unknown": "review_incomplete",
    "qualification-missing": "qualification_unverified",
    "qualification-unverified": "qualification_unverified",
}


def approval_withheld_diagnostic(decision: AutoApprovalDecision) -> str:
    """Map an approval decision's blockers onto one bounded public diagnostic."""

    for blocker in decision.blockers:
        mapped = _WITHHELD_DIAGNOSTICS.get(blocker)
        if mapped is not None:
            return mapped
    return "approval_withheld"


__all__ = [
    "APPROVAL_ELIGIBILITY_SCHEMA_VERSION",
    "ApprovalFacts",
    "AutoApprovalDecision",
    "ReviewApprovalEligibility",
    "approval_eligibility_from_result",
    "approval_facts_from_result",
    "approval_withheld_diagnostic",
    "evaluate_approval_facts",
    "evaluate_auto_approval",
    "has_blocking_findings",
    "has_human_adjudication_findings",
    "qualification_state_for_configuration",
    "qualification_state_for_publication",
]
