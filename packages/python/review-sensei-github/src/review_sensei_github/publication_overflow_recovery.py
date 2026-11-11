"""Historical publication overflow and nonterminal failure qualification (G31).

Models TaskAlong `#41`-class (`be1` executor) prepublication body-limit overflows
and historical nonterminal publication states (`publication_failed` and
`publication_pending`) without mutating the retained `ReviewResult`, frozen
`ReviewConvergencePolicy`, or caller/frame allowances.

Core safety invariants enforced by this module:
1. **Original Result & Policy Immutability**: Formatter output comparison and
   failure qualification verify before-and-after digests of `ReviewResult` and
   `ReviewConvergencePolicy` (`authority=False`). Neither the original summary,
   finding prose, coverage manifest, nor policy/configuration digests are ever
   mutated, truncated in storage, or dropped.
2. **Zero Allowance Enlargement**: GitHub's `MAX_PUBLISHED_REVIEW_BODY_BYTES`
   (`65,536`), `EvidenceReadBudget` (`64` total / `60` ordinary calls),
   `MAX_RESULT_PARTS` (`32`), and the predecessor's `FrozenApiConfiguration.limit`
   (`512`) are enforced unchanged. If finding prose still exceeds `65,536` bytes
   after the 1,024-byte `ConfirmedNarrativeArchive` excerpt, framing fails closed.
3. **Failure-Before-Write vs Response-Loss Separation**:
   - Deterministic local framing refusal (`PublicationLimitError`, `tx.phase ==
     "publication_failed"`, `uncertain == 0`, zero App-authored exact-head review
     on GitHub) qualifies as `confirmed_prepublication_overflow_before_write`.
   - Lost HTTP acknowledgement (`tx.phase == "publication_pending"` or
     `uncertain > 0`) is NEVER misclassified as failure-before-write or `c7`
     prepublication quarantine: live exact-head review readback distinguishes
     `response_loss_echo_observed_reconcilable` (review landed; reconcile without
     re-POSTing) from `response_loss_unobserved_remote_hold` (uncertain write;
     hold and recover via `controlled_serialized_restart`).
4. **No Silent `c7` Catalog Expansion**: An executor outside `EXECUTOR`
   (`c7f13b8cfe28e89ec82ad03bd2f34aed5b40cbb1`), such as TaskAlong `#41`'s `be1`
   executor, is marked `is_c7_body_limit_qualified=False` and routed to same-run
   retained replay (when same-head/slot predicates hold) or
   `controlled_serialized_restart`, never silently appended to `c7`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import (
    MAX_PART_READS,
    canonical_bytes,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.session import SessionIdentity, SessionRecord

from . import controlled_restart
from .approval import approval_eligibility_from_result
from .default_human_inventory import HumanInventoryReference
from .http import GitHubHttp
from .operation_comments import MAX_RESULT_PARTS
from .publication import (
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    ORDINARY_EVIDENCE_READ_LIMIT,
    PublicationLimitError,
    ReviewPublisher,
    _approval_eligible,
    _disposition_frame_reserve,
    _framed_review_body,
    _published_summary_byte_limit,
    _render_assembled_summary,
    approval_eligibility_from_body,
    approval_eligibility_marker,
    finding_index_marker,
    fit_assembled_review,
    parse_finding_index,
    review_marker,
    review_result_digest,
)
from .publication_recovery import ConfirmedNarrativeArchive
from .quarantine_published_tail import BODY_CALLER_BLOB, EXECUTOR

_HEX40 = re.compile(r"[a-f0-9]{40}")
_HEX64 = re.compile(r"[a-f0-9]{64}")

PUBLICATION_OVERFLOW_CLASSIFIER_ID = "nonterminal-publication-overflow-v1"

OVERFLOW_FAILURE_STATES: frozenset[str] = frozenset(
    {
        "confirmed_prepublication_overflow_before_write",
        "confirmed_prepublication_failure_before_write",
        "response_loss_echo_observed_reconcilable",
        "response_loss_unobserved_remote_hold",
        "predecessor_owner_active_hold",
    }
)

FAILURE_BOUNDARIES: frozenset[str] = frozenset(
    {
        "before_write",
        "response_loss_committed_echo",
        "response_loss_uncertain",
        "active_owner_unknown",
    }
)

OVERFLOW_RECOVERY_ROUTES: frozenset[str] = frozenset(
    {
        "same_run_attempt2_retained_replay",
        "reconcile_observed_exact_review",
        "quarantine_prepublication_isolation",
        "controlled_serialized_restart",
        "wait_for_owner_cessation",
    }
)


def _policy_digest(policy: ReviewConvergencePolicy) -> str:
    return policy.digest()


@dataclass(frozen=True)
class FormatterComparisonReport:
    """Exact comparison of pre-archive vs archive-backed review formatter outputs.

    `authority` is unconditionally `False`: comparing formatter outputs never
    grants publication authority, mutates the retained result, or enlarges byte
    or API allowances.
    """

    result_content_digest: str
    result_digest: str
    policy_digest: str
    diff_sha256: str
    review_body_byte_ceiling: int
    summary_byte_limit: int
    disposition_reserve_bytes: int
    original_narrative_bytes: int
    raw_candidate_count: int
    retained_review_status: str
    historical_exceeded_ceiling: bool
    historical_failure_check: str | None
    historical_uncompacted_frame_bytes: int
    historical_framed_body: str | None
    archive_excerpt_fits_ceiling: bool
    archive_failure_check: str | None
    archive_framed_body: str | None
    archive_framed_bytes: int | None
    bytes_saved_by_excerpt: int | None
    finding_index_identical: bool
    eligibility_digest_identical: bool
    finding_explanations_preserved: bool
    carrier_links_verified: bool
    result_immutable_verified: bool
    policy_immutable_verified: bool
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "FormatterComparisonReport is diagnostic and must not carry authority"
            )
        for name, val in (
            ("result_content_digest", self.result_content_digest),
            ("result_digest", self.result_digest),
            ("policy_digest", self.policy_digest),
            ("diff_sha256", self.diff_sha256),
        ):
            if not isinstance(val, str) or _HEX64.fullmatch(val) is None:
                raise ReviewInputError(f"invalid {name}")
        if self.review_body_byte_ceiling != MAX_PUBLISHED_REVIEW_BODY_BYTES:
            raise ReviewInputError(
                "review_body_byte_ceiling must equal MAX_PUBLISHED_REVIEW_BODY_BYTES (65536)"
            )
        if not self.result_immutable_verified or not self.policy_immutable_verified:
            raise ReviewInputError(
                "original ReviewResult and ReviewConvergencePolicy must remain immutable"
            )
        if self.archive_excerpt_fits_ceiling:
            if (
                self.archive_framed_body is None
                or self.archive_framed_bytes is None
                or self.archive_framed_bytes > MAX_PUBLISHED_REVIEW_BODY_BYTES
                or not self.finding_index_identical
                or not self.eligibility_digest_identical
                or not self.finding_explanations_preserved
                or not self.carrier_links_verified
            ):
                raise ReviewInputError(
                    "fitting archive formatter output failed integrity or byte ceiling check"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_content_digest": self.result_content_digest,
            "result_digest": self.result_digest,
            "policy_digest": self.policy_digest,
            "diff_sha256": self.diff_sha256,
            "review_body_byte_ceiling": self.review_body_byte_ceiling,
            "summary_byte_limit": self.summary_byte_limit,
            "disposition_reserve_bytes": self.disposition_reserve_bytes,
            "original_narrative_bytes": self.original_narrative_bytes,
            "raw_candidate_count": self.raw_candidate_count,
            "retained_review_status": self.retained_review_status,
            "historical_exceeded_ceiling": self.historical_exceeded_ceiling,
            "historical_failure_check": self.historical_failure_check,
            "historical_uncompacted_frame_bytes": self.historical_uncompacted_frame_bytes,
            "archive_excerpt_fits_ceiling": self.archive_excerpt_fits_ceiling,
            "archive_failure_check": self.archive_failure_check,
            "archive_framed_bytes": self.archive_framed_bytes,
            "bytes_saved_by_excerpt": self.bytes_saved_by_excerpt,
            "finding_index_identical": self.finding_index_identical,
            "eligibility_digest_identical": self.eligibility_digest_identical,
            "finding_explanations_preserved": self.finding_explanations_preserved,
            "carrier_links_verified": self.carrier_links_verified,
            "result_immutable_verified": self.result_immutable_verified,
            "policy_immutable_verified": self.policy_immutable_verified,
            "authority": False,
        }


@dataclass(frozen=True)
class NonterminalPublicationQualification:
    """Fail-closed qualification of a historical nonterminal publication failure."""

    classifier_id: str
    state_code: str
    failure_boundary: str
    transaction_phase: str
    remote_effect_uncertain: bool
    confirmed_failure_before_write: bool
    observed_review_id: int | None
    executor_sha: str | None
    caller_blob: str | None
    is_c7_body_limit_qualified: bool
    head_relation: str
    base_relation: str
    caller_allowance_limit: int
    evidence_read_ceiling: int
    ordinary_read_limit: int
    result_part_ceiling: int
    review_body_byte_ceiling: int
    result_content_digest: str
    policy_digest: str
    formatter_comparison: FormatterComparisonReport | None
    primary_route: str
    fallback_routes: tuple[str, ...]
    operator_action: str
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "NonterminalPublicationQualification must never carry authority"
            )
        if self.classifier_id != PUBLICATION_OVERFLOW_CLASSIFIER_ID:
            raise ReviewInputError("unknown overflow classifier_id")
        if self.state_code not in OVERFLOW_FAILURE_STATES:
            raise ReviewInputError(f"unknown overflow state_code: {self.state_code!r}")
        if self.failure_boundary not in FAILURE_BOUNDARIES:
            raise ReviewInputError(
                f"unknown failure_boundary: {self.failure_boundary!r}"
            )
        if self.primary_route not in OVERFLOW_RECOVERY_ROUTES or any(
            r not in OVERFLOW_RECOVERY_ROUTES for r in self.fallback_routes
        ):
            raise ReviewInputError("unknown overflow recovery route")
        if self.review_body_byte_ceiling != MAX_PUBLISHED_REVIEW_BODY_BYTES:
            raise ReviewInputError("review_body_byte_ceiling was altered")
        if (
            self.evidence_read_ceiling != MAX_PART_READS
            or self.ordinary_read_limit != ORDINARY_EVIDENCE_READ_LIMIT
            or self.result_part_ceiling != MAX_RESULT_PARTS
        ):
            raise ReviewInputError("read or part allowance constants were altered")
        if self.confirmed_failure_before_write and self.remote_effect_uncertain:
            raise ReviewInputError(
                "confirmed_failure_before_write cannot coexist with remote_effect_uncertain"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifier_id": self.classifier_id,
            "state_code": self.state_code,
            "failure_boundary": self.failure_boundary,
            "transaction_phase": self.transaction_phase,
            "remote_effect_uncertain": self.remote_effect_uncertain,
            "confirmed_failure_before_write": self.confirmed_failure_before_write,
            "observed_review_id": self.observed_review_id,
            "executor_sha": self.executor_sha,
            "caller_blob": self.caller_blob,
            "is_c7_body_limit_qualified": self.is_c7_body_limit_qualified,
            "head_relation": self.head_relation,
            "base_relation": self.base_relation,
            "caller_allowance_limit": self.caller_allowance_limit,
            "evidence_read_ceiling": self.evidence_read_ceiling,
            "ordinary_read_limit": self.ordinary_read_limit,
            "result_part_ceiling": self.result_part_ceiling,
            "review_body_byte_ceiling": self.review_body_byte_ceiling,
            "result_content_digest": self.result_content_digest,
            "policy_digest": self.policy_digest,
            "formatter_comparison": (
                self.formatter_comparison.to_dict()
                if self.formatter_comparison is not None
                else None
            ),
            "primary_route": self.primary_route,
            "fallback_routes": list(self.fallback_routes),
            "operator_action": self.operator_action,
            "authority": False,
        }


@dataclass(frozen=True)
class PublicationOverflowRecoveryDescriptor:
    """Immutable recovery descriptor for nonterminal publication failures (G31)."""

    classifier_id: str
    qualification_state: str
    failure_boundary: str
    predecessor_run_id: int
    predecessor_root_sha256: str
    result_content_digest: str
    policy_digest: str
    caller_allowance_limit: int
    review_body_byte_ceiling: int
    retained_nonstartup_caller_liability: int
    retained_total_caller_liability: int
    is_c7_body_limit_qualified: bool
    controlled_restart_registration: dict[str, Any]
    operator_instructions: str
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "PublicationOverflowRecoveryDescriptor must never carry authority"
            )
        if self.review_body_byte_ceiling != MAX_PUBLISHED_REVIEW_BODY_BYTES:
            raise ReviewInputError(
                "PublicationOverflowRecoveryDescriptor forbids enlarged body ceiling"
            )
        if (
            type(self.caller_allowance_limit) is not int
            or self.caller_allowance_limit <= 0
        ):
            raise ReviewInputError("invalid caller_allowance_limit")

    def registry_document(self) -> dict[str, Any]:
        return {
            "schema_version": "controlled-restart-registry-v1",
            "registrations": [dict(self.controlled_restart_registration)],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifier_id": self.classifier_id,
            "qualification_state": self.qualification_state,
            "failure_boundary": self.failure_boundary,
            "predecessor_run_id": self.predecessor_run_id,
            "predecessor_root_sha256": self.predecessor_root_sha256,
            "result_content_digest": self.result_content_digest,
            "policy_digest": self.policy_digest,
            "caller_allowance_limit": self.caller_allowance_limit,
            "review_body_byte_ceiling": self.review_body_byte_ceiling,
            "retained_nonstartup_caller_liability": (
                self.retained_nonstartup_caller_liability
            ),
            "retained_total_caller_liability": self.retained_total_caller_liability,
            "is_c7_body_limit_qualified": self.is_c7_body_limit_qualified,
            "controlled_restart_registration": dict(
                self.controlled_restart_registration
            ),
            "operator_instructions": self.operator_instructions,
            "authority": False,
        }


def compare_publication_formatter_outputs(
    *,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    result: ReviewResult,
    diff: str,
    policy: ReviewConvergencePolicy,
    narrative_archive: ConfirmedNarrativeArchive | None = None,
) -> FormatterComparisonReport:
    """Compare historical pre-archive vs archive-backed formatter outputs offline.

    Executes zero HTTP calls and verifies that `result` and `policy` remain
    bit-for-bit unchanged before and after formatting under the fixed
    `MAX_PUBLISHED_REVIEW_BODY_BYTES` (`65,536`) ceiling.
    """
    if not isinstance(identity, SessionIdentity) or identity.repository_id is None:
        raise ReviewInputError(
            "formatter comparison requires SessionIdentity with repository_id"
        )
    if not isinstance(result, ReviewResult) or not isinstance(
        policy, ReviewConvergencePolicy
    ):
        raise ReviewInputError("formatter comparison requires ReviewResult and policy")
    if _HEX40.fullmatch(base_sha) is None or _HEX40.fullmatch(head_sha) is None:
        raise ReviewInputError("formatter comparison requires 40-hex base/head SHAs")
    if policy.mode != "merge-focused" or policy.inline_advisory_threads:
        raise ReviewInputError(
            "formatter comparison requires merge-focused non-advisory placement policy"
        )

    before_result_bytes = canonical_bytes(result.to_dict())
    before_result_digest = result.content_digest()
    before_policy_digest = _policy_digest(policy)

    def _no_http(*_args: Any, **_kwargs: Any) -> Any:
        raise ReviewInputError("offline formatter comparison permits no HTTP")

    def _no_host_facts() -> Any:
        raise ReviewInputError(
            "offline formatter comparison permits no mutable host placement reads"
        )

    publisher = ReviewPublisher(
        http=GitHubHttp(api_url="https://offline-formatter.invalid", opener=_no_http)
    )
    assembled = publisher._assemble_review_body(
        result=result,
        analysis=publisher._validate_locations(result, diff),
        convergence_policy=policy,
        read_facts=_no_host_facts,
        auto_approve=_approval_eligible(result),
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=head_sha,
        base_sha=base_sha,
    )
    assembled = replace(assembled, compact_references=True)

    eligibility = approval_eligibility_from_result(
        result,
        head_sha=head_sha,
        base_sha=base_sha,
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
                entry.blocking and entry.body_placed
                for entry in assembled.index_entries
            ),
        ),
    )

    rev_marker = review_marker(
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=head_sha,
        result=result,
    )
    elig_marker = approval_eligibility_marker(eligibility)
    res_digest = review_result_digest(result)
    summary_limit = _published_summary_byte_limit(result)
    reserve = _disposition_frame_reserve(eligibility, assembled.index_entries)
    preserve_explanations = eligibility.human_review is not None

    expected_idx_marker = finding_index_marker(
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=head_sha,
        base_sha=base_sha,
        result_digest=res_digest,
        entries=assembled.index_entries,
        complete_inventory=True,
    )
    uncompacted_summary = _render_assembled_summary(
        assembled,
        omitted_identifiers=frozenset(),
        kept_explanations=assembled.body_explanations,
    )
    uncompacted_frame = _framed_review_body(
        uncompacted_summary,
        review_marker=rev_marker,
        index_marker=expected_idx_marker,
        eligibility_marker=elig_marker,
    )
    uncompacted_frame_bytes = len(uncompacted_frame.encode("utf-8"))

    # 1. Historical pre-archive formatter (`narrative_archive=None`):
    hist_exceeded = False
    hist_check: str | None = None
    hist_framed_body: str | None = None
    try:
        hist_summary, hist_idx = fit_assembled_review(
            assembled,
            review_marker=rev_marker,
            eligibility_marker=elig_marker,
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=head_sha,
            base_sha=base_sha,
            result_digest=res_digest,
            summary_limit=summary_limit,
            complete_inventory=True,
            reserved_bytes=reserve,
            preserve_explanations=preserve_explanations,
        )
        hist_framed_body = _framed_review_body(
            hist_summary,
            review_marker=rev_marker,
            index_marker=hist_idx,
            eligibility_marker=elig_marker,
        )
    except PublicationLimitError as exc:
        hist_exceeded = True
        hist_check = exc.check

    # 2. Archive-backed excerpt formatter (`narrative_archive` when supplied):
    arch_fits = False
    arch_check: str | None = None
    arch_framed_body: str | None = None
    arch_framed_bytes: int | None = None
    bytes_saved: int | None = None
    idx_identical = False
    elig_identical = False
    explanations_preserved = False
    carrier_links_ok = False

    if narrative_archive is not None and len(result.summary.encode("utf-8")) > 1024:
        if type(narrative_archive) is not ConfirmedNarrativeArchive:
            raise ReviewInputError(
                "narrative_archive must be ConfirmedNarrativeArchive"
            )
        excerpt_overview = narrative_archive.excerpt(
            result,
            repository=identity.repository,
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
        )
        compacted_assembled = replace(assembled, overview=excerpt_overview)
        try:
            arch_summary, arch_idx = fit_assembled_review(
                compacted_assembled,
                review_marker=rev_marker,
                eligibility_marker=elig_marker,
                repository_id=identity.repository_id,
                pull_request=identity.pull_request,
                head_sha=head_sha,
                base_sha=base_sha,
                result_digest=res_digest,
                summary_limit=summary_limit,
                complete_inventory=True,
                reserved_bytes=reserve,
                preserve_explanations=preserve_explanations,
            )
            arch_framed_body = _framed_review_body(
                arch_summary,
                review_marker=rev_marker,
                index_marker=arch_idx,
                eligibility_marker=elig_marker,
            )
            arch_framed_bytes = len(arch_framed_body.encode("utf-8"))
            arch_fits = arch_framed_bytes <= MAX_PUBLISHED_REVIEW_BODY_BYTES - reserve
            bytes_saved = uncompacted_frame_bytes - arch_framed_bytes

            parsed_idx = parse_finding_index(
                arch_framed_body,
                repository_id=identity.repository_id,
                pull_request=identity.pull_request,
                head_sha=head_sha,
            )
            idx_identical = (
                arch_idx == expected_idx_marker
                and parsed_idx is not None
                and parsed_idx.entries == tuple(assembled.index_entries)
                and parsed_idx.result_digest == res_digest
            )
            expected_human_review = eligibility.human_review
            parsed_elig = approval_eligibility_from_body(
                arch_framed_body,
                human_inventory_reader=(
                    (lambda _ref: expected_human_review)
                    if eligibility.human_inventory_reference is not None
                    and expected_human_review is not None
                    else None
                ),
            )
            elig_identical = (
                parsed_elig is not None
                and parsed_elig == eligibility
                and parsed_elig.result_digest == eligibility.result_digest
                and parsed_elig.facts == eligibility.facts
            )
            required_prose_details = [
                exp.view.detail
                for exp in assembled.body_explanations
                if exp.obligatory
                and isinstance(exp.view.detail, str)
                and exp.view.detail
            ]
            explanations_preserved = all(
                detail in arch_framed_body for detail in required_prose_details
            )
            carrier_links_ok = (
                f"Retained content SHA-256: `{narrative_archive.content_digest}`"
                in arch_framed_body
                and f"Carrier payload SHA-256: `{narrative_archive.payload_digest}`"
                in arch_framed_body
                and all(
                    f"#issuecomment-{cid})" in arch_framed_body
                    for cid in narrative_archive.carrier_ids
                )
            )
        except PublicationLimitError as exc:
            arch_fits = False
            arch_check = exc.check

    after_result_bytes = canonical_bytes(result.to_dict())
    after_result_digest = result.content_digest()
    after_policy_digest = _policy_digest(policy)

    return FormatterComparisonReport(
        result_content_digest=before_result_digest,
        result_digest=res_digest,
        policy_digest=before_policy_digest,
        diff_sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
        summary_byte_limit=summary_limit,
        disposition_reserve_bytes=reserve,
        original_narrative_bytes=len(result.summary.encode("utf-8")),
        raw_candidate_count=len(result.comments),
        retained_review_status=result.review_status,
        historical_exceeded_ceiling=hist_exceeded,
        historical_failure_check=hist_check,
        historical_uncompacted_frame_bytes=uncompacted_frame_bytes,
        historical_framed_body=hist_framed_body,
        archive_excerpt_fits_ceiling=arch_fits,
        archive_failure_check=arch_check,
        archive_framed_body=arch_framed_body,
        archive_framed_bytes=arch_framed_bytes,
        bytes_saved_by_excerpt=bytes_saved,
        finding_index_identical=idx_identical,
        eligibility_digest_identical=elig_identical,
        finding_explanations_preserved=explanations_preserved,
        carrier_links_verified=carrier_links_ok,
        result_immutable_verified=(
            before_result_bytes == after_result_bytes
            and before_result_digest == after_result_digest
        ),
        policy_immutable_verified=(before_policy_digest == after_policy_digest),
    )


def _find_exact_app_review_id(
    reviews: Sequence[Any],
    *,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    app_slug: str,
    result: ReviewResult,
) -> int | None:
    expected_marker = review_marker(
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head_sha,
        result=result,
    )
    expected_login = app_slug.casefold()
    expected_logins = {expected_login, f"{expected_login}[bot]"}
    for review in reviews:
        if not isinstance(review, Mapping):
            continue
        if review.get("commit_id") != head_sha:
            continue
        user = review.get("user")
        if not isinstance(user, Mapping) or user.get("type") != "Bot":
            continue
        login = user.get("login")
        if not isinstance(login, str) or login.casefold() not in expected_logins:
            continue
        body = review.get("body")
        if not isinstance(body, str) or expected_marker not in body:
            continue
        rid = review.get("id")
        if type(rid) is int and rid > 0:
            return rid
    return None


def qualify_nonterminal_publication_failure(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    result: ReviewResult,
    policy: ReviewConvergencePolicy,
    current_base_sha: str,
    current_head_sha: str,
    live_reviews: Sequence[Any],
    app_slug: str,
    owner_ceased: bool,
    formatter_comparison: FormatterComparisonReport | None = None,
    executor_sha: str | None = None,
    caller_blob: str | None = None,
    retained_context_available: bool = True,
) -> NonterminalPublicationQualification:
    """Qualify a historical `publication_failed` or `publication_pending` state.

    Enforces strict separation between confirmed failure-before-write (`tx.phase ==
    "publication_failed"`, `uncertain == 0`, zero remote review write) and
    response-loss / uncertain remote write (`tx.phase == "publication_pending"` or
    `uncertain > 0`).
    """
    if not isinstance(identity, SessionIdentity) or identity.repository_id is None:
        raise ReviewInputError("valid SessionIdentity with repository_id is required")
    if not isinstance(record, SessionRecord) or record.transaction is None:
        raise ReviewInputError(
            "SessionRecord with an active nonterminal ReviewTransaction is required"
        )
    if record.api_run_accounting is None:
        raise ReviewInputError(
            "nonterminal publication qualification requires API-accounted record"
        )
    tx = record.transaction
    if tx.phase not in {"publication_failed", "publication_pending"}:
        raise ReviewInputError(
            f"expected nonterminal publication phase, got {tx.phase!r}"
        )
    if tx.result_sha256 != result.content_digest():
        raise ReviewInputError(
            "retained ReviewResult digest does not match transaction.result_sha256"
        )
    if tx.policy_digest != _policy_digest(policy):
        raise ReviewInputError(
            "ReviewConvergencePolicy digest does not match transaction.policy_digest"
        )

    old = ApiRunAccounting.from_document(record.api_run_accounting)
    caller_limit = old.configuration.limit
    pol_digest = _policy_digest(policy)

    head_rel = "same_head" if current_head_sha == tx.head_sha else "different_head"
    base_rel = "same_base" if current_base_sha == tx.base_sha else "different_base"

    is_c7_qualified = (
        executor_sha == EXECUTOR
        and caller_blob == BODY_CALLER_BLOB
        and tx.phase == "publication_failed"
        and old.uncertain == 0
        and formatter_comparison is not None
        and formatter_comparison.historical_exceeded_ceiling
        and formatter_comparison.historical_failure_check == "framed-review-body"
        and not any(any(c in item.path for c in "*?[") for item in result.comments)
    )

    observed_review_id = _find_exact_app_review_id(
        live_reviews,
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=tx.head_sha,
        app_slug=app_slug,
        result=result,
    )

    # 1. Predecessor owner still active/unconfirmed -> hold all recovery.
    if not owner_ceased:
        return NonterminalPublicationQualification(
            classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
            state_code="predecessor_owner_active_hold",
            failure_boundary="active_owner_unknown",
            transaction_phase=tx.phase,
            remote_effect_uncertain=True,
            confirmed_failure_before_write=False,
            observed_review_id=observed_review_id,
            executor_sha=executor_sha,
            caller_blob=caller_blob,
            is_c7_body_limit_qualified=False,
            head_relation=head_rel,
            base_relation=base_rel,
            caller_allowance_limit=caller_limit,
            evidence_read_ceiling=MAX_PART_READS,
            ordinary_read_limit=ORDINARY_EVIDENCE_READ_LIMIT,
            result_part_ceiling=MAX_RESULT_PARTS,
            review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
            result_content_digest=result.content_digest(),
            policy_digest=pol_digest,
            formatter_comparison=formatter_comparison,
            primary_route="wait_for_owner_cessation",
            fallback_routes=("controlled_serialized_restart",),
            operator_action=(
                "Predecessor workflow run is still active or unconfirmed; wait "
                "for owner cessation and capability expiry before recovery."
            ),
        )

    attempt2_available = (
        old.owner.run_attempt == 1
        and old.owner.job in {"hosted", "local"}
        and (old.owner.job, 2) not in set(old.startup_used)
        and not record.operator_paused
        and retained_context_available
    )

    # 2. Response-loss / uncertain remote write (`publication_pending` or `uncertain > 0`
    #    or unexpected live review echo under `publication_failed`).
    if (
        tx.phase == "publication_pending"
        or old.uncertain > 0
        or observed_review_id is not None
    ):
        if observed_review_id is not None:
            can_reconcile_same_head = (
                head_rel == "same_head"
                and base_rel == "same_base"
                and attempt2_available
                and old.uncertain == 0
            )
            primary = (
                "reconcile_observed_exact_review"
                if can_reconcile_same_head
                else "controlled_serialized_restart"
            )
            fallbacks = (
                ("controlled_serialized_restart",) if can_reconcile_same_head else ()
            )
            return NonterminalPublicationQualification(
                classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
                state_code="response_loss_echo_observed_reconcilable",
                failure_boundary="response_loss_committed_echo",
                transaction_phase=tx.phase,
                remote_effect_uncertain=old.uncertain > 0,
                confirmed_failure_before_write=False,
                observed_review_id=observed_review_id,
                executor_sha=executor_sha,
                caller_blob=caller_blob,
                is_c7_body_limit_qualified=False,
                head_relation=head_rel,
                base_relation=base_rel,
                caller_allowance_limit=caller_limit,
                evidence_read_ceiling=MAX_PART_READS,
                ordinary_read_limit=ORDINARY_EVIDENCE_READ_LIMIT,
                result_part_ceiling=MAX_RESULT_PARTS,
                review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
                result_content_digest=result.content_digest(),
                policy_digest=pol_digest,
                formatter_comparison=formatter_comparison,
                primary_route=primary,
                fallback_routes=fallbacks,
                operator_action=(
                    f"Exact App-authored review (id={observed_review_id}) is already "
                    "persisted on GitHub despite lost HTTP acknowledgement; reconcile "
                    "the existing review without posting a duplicate review."
                ),
            )
        return NonterminalPublicationQualification(
            classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
            state_code="response_loss_unobserved_remote_hold",
            failure_boundary="response_loss_uncertain",
            transaction_phase=tx.phase,
            remote_effect_uncertain=True,
            confirmed_failure_before_write=False,
            observed_review_id=None,
            executor_sha=executor_sha,
            caller_blob=caller_blob,
            is_c7_body_limit_qualified=False,
            head_relation=head_rel,
            base_relation=base_rel,
            caller_allowance_limit=caller_limit,
            evidence_read_ceiling=MAX_PART_READS,
            ordinary_read_limit=ORDINARY_EVIDENCE_READ_LIMIT,
            result_part_ceiling=MAX_RESULT_PARTS,
            review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
            result_content_digest=result.content_digest(),
            policy_digest=pol_digest,
            formatter_comparison=formatter_comparison,
            primary_route="controlled_serialized_restart",
            fallback_routes=(),
            operator_action=(
                "Publication stopped with response-loss or uncertain mutation "
                "outcome (`publication_pending` or `uncertain > 0`) and no "
                "persisted review echo on GitHub. Do not classify as prepublication "
                "failure-before-write or blind-retry; recover via controlled "
                "serialized restart after capability expiry."
            ),
        )

    # 3. Confirmed failure-before-write (`tx.phase == "publication_failed"`,
    #    `old.uncertain == 0`, zero remote review write).
    is_overflow = (
        formatter_comparison is not None
        and formatter_comparison.historical_exceeded_ceiling
    )
    archive_can_fit = (
        formatter_comparison is None
        or formatter_comparison.archive_excerpt_fits_ceiling
    )
    state_code = (
        "confirmed_prepublication_overflow_before_write"
        if is_overflow
        else "confirmed_prepublication_failure_before_write"
    )

    if (
        head_rel == "same_head"
        and base_rel == "same_base"
        and attempt2_available
        and old.reserved_undispatched == 0
        and archive_can_fit
    ):
        fallbacks_same: tuple[str, ...] = (
            ("quarantine_prepublication_isolation", "controlled_serialized_restart")
            if is_c7_qualified
            else ("controlled_serialized_restart",)
        )
        return NonterminalPublicationQualification(
            classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
            state_code=state_code,
            failure_boundary="before_write",
            transaction_phase=tx.phase,
            remote_effect_uncertain=False,
            confirmed_failure_before_write=True,
            observed_review_id=None,
            executor_sha=executor_sha,
            caller_blob=caller_blob,
            is_c7_body_limit_qualified=is_c7_qualified,
            head_relation=head_rel,
            base_relation=base_rel,
            caller_allowance_limit=caller_limit,
            evidence_read_ceiling=MAX_PART_READS,
            ordinary_read_limit=ORDINARY_EVIDENCE_READ_LIMIT,
            result_part_ceiling=MAX_RESULT_PARTS,
            review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
            result_content_digest=result.content_digest(),
            policy_digest=pol_digest,
            formatter_comparison=formatter_comparison,
            primary_route="same_run_attempt2_retained_replay",
            fallback_routes=fallbacks_same,
            operator_action=(
                "Confirmed prepublication failure before any remote review write; "
                "rerun attempt 2 on unchanged head/base to replay the immutable "
                "retained result with confirmed narrative archive under original "
                "65,536-byte and 512-call allowances."
            ),
        )

    if (
        head_rel == "different_head"
        and is_c7_qualified
        and old.owner.run_attempt == 1
        and old.owner.generation == 0
        and not record.operator_paused
        and record.quarantine_lineage is None
        and retained_context_available
    ):
        return NonterminalPublicationQualification(
            classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
            state_code=state_code,
            failure_boundary="before_write",
            transaction_phase=tx.phase,
            remote_effect_uncertain=False,
            confirmed_failure_before_write=True,
            observed_review_id=None,
            executor_sha=executor_sha,
            caller_blob=caller_blob,
            is_c7_body_limit_qualified=True,
            head_relation=head_rel,
            base_relation=base_rel,
            caller_allowance_limit=caller_limit,
            evidence_read_ceiling=MAX_PART_READS,
            ordinary_read_limit=ORDINARY_EVIDENCE_READ_LIMIT,
            result_part_ceiling=MAX_RESULT_PARTS,
            review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
            result_content_digest=result.content_digest(),
            policy_digest=pol_digest,
            formatter_comparison=formatter_comparison,
            primary_route="quarantine_prepublication_isolation",
            fallback_routes=("controlled_serialized_restart",),
            operator_action=(
                "Confirmed `c7` prepublication body-limit failure on superseded "
                "head; isolate via `body-limit-prepublication-v1` quarantine "
                "preserving all historical findings."
            ),
        )

    return NonterminalPublicationQualification(
        classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
        state_code=state_code,
        failure_boundary="before_write",
        transaction_phase=tx.phase,
        remote_effect_uncertain=False,
        confirmed_failure_before_write=True,
        observed_review_id=None,
        executor_sha=executor_sha,
        caller_blob=caller_blob,
        is_c7_body_limit_qualified=is_c7_qualified,
        head_relation=head_rel,
        base_relation=base_rel,
        caller_allowance_limit=caller_limit,
        evidence_read_ceiling=MAX_PART_READS,
        ordinary_read_limit=ORDINARY_EVIDENCE_READ_LIMIT,
        result_part_ceiling=MAX_RESULT_PARTS,
        review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
        result_content_digest=result.content_digest(),
        policy_digest=pol_digest,
        formatter_comparison=formatter_comparison,
        primary_route="controlled_serialized_restart",
        fallback_routes=(),
        operator_action=(
            "Prepublication overflow is outside same-head archive replay or `c7` "
            "quarantine catalog (for example, TaskAlong `#41` `be1` executor on "
            "superseded head or finding prose exceeding 65,536 bytes after "
            "narrative compaction); recover via controlled serialized restart "
            "without enlarging allowances."
        ),
    )


def build_overflow_recovery_descriptor(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    qualification: NonterminalPublicationQualification,
    old_comments: Sequence[Mapping[str, Any]],
    designated_run_id: str,
    base_sha: str,
    head_sha: str,
    policy_commit: str,
    workflow_sha: str,
    issuance_disabled_at: int,
    retire_after: int,
    job: str = "hosted",
    requested_caller_limit: int | None = None,
    requested_body_ceiling: int = MAX_PUBLISHED_REVIEW_BODY_BYTES,
    extra_old_runs: Sequence[int] = (),
) -> PublicationOverflowRecoveryDescriptor:
    """Build a bounded `controlled-restart-registry-v1` descriptor for G31 recovery.

    Refuses any attempt to enlarge `requested_caller_limit` beyond the predecessor's
    frozen `old.configuration.limit` or `requested_body_ceiling` beyond `65,536`.
    """
    if not isinstance(identity, SessionIdentity) or not isinstance(
        record, SessionRecord
    ):
        raise ReviewInputError(
            "overflow recovery descriptor requires valid identity and record"
        )
    if record.api_run_accounting is None:
        raise ReviewInputError(
            "overflow recovery descriptor requires API-accounted predecessor"
        )
    if qualification.state_code == "predecessor_owner_active_hold":
        raise ReviewInputError(
            "cannot issue overflow recovery descriptor while predecessor owner is active"
        )
    if requested_body_ceiling != MAX_PUBLISHED_REVIEW_BODY_BYTES:
        raise ReviewInputError(
            "overflow recovery forbids enlarging MAX_PUBLISHED_REVIEW_BODY_BYTES (65536)"
        )

    old = ApiRunAccounting.from_document(record.api_run_accounting)
    if (
        requested_caller_limit is not None
        and requested_caller_limit != old.configuration.limit
    ):
        raise ReviewInputError(
            "overflow recovery forbids enlarging or altering caller API allowance"
        )

    nonstartup = (
        old.prior_nonstartup_liability
        + old.confirmed
        + old.uncertain
        + old.reserved_undispatched
    )
    total = old.startup.liability + nonstartup

    old_run_ids: list[int] = [old.identity.run_id]
    for run_id in extra_old_runs:
        if type(run_id) is not int or run_id <= 0:
            raise ReviewInputError("extra_old_runs must contain positive integer IDs")
        if run_id not in old_run_ids:
            old_run_ids.append(run_id)

    row_body: dict[str, Any] = {
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "pull_request": identity.pull_request,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "policy_commit": policy_commit,
        "workflow_sha": workflow_sha,
        "run_id": designated_run_id,
        "job": job,
        "issuance_disabled_at": issuance_disabled_at,
        "retire_after": retire_after,
        "old_runs": old_run_ids,
        "old_comments": [dict(ref) for ref in old_comments],
    }
    row_body["request_id"] = hashlib.sha256(canonical_bytes(row_body)).hexdigest()
    registry_bytes = canonical_bytes(
        {
            "schema_version": "controlled-restart-registry-v1",
            "registrations": [row_body],
        }
    )
    validated_row = controlled_restart.read_registry(registry_bytes)[0]

    return PublicationOverflowRecoveryDescriptor(
        classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
        qualification_state=qualification.state_code,
        failure_boundary=qualification.failure_boundary,
        predecessor_run_id=old.identity.run_id,
        predecessor_root_sha256=record.record_sha256,
        result_content_digest=qualification.result_content_digest,
        policy_digest=qualification.policy_digest,
        caller_allowance_limit=old.configuration.limit,
        review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
        retained_nonstartup_caller_liability=nonstartup,
        retained_total_caller_liability=total,
        is_c7_body_limit_qualified=qualification.is_c7_body_limit_qualified,
        controlled_restart_registration=validated_row,
        operator_instructions=(
            "Archive predecessor nonterminal publication evidence unmodified into "
            "`abandoned-history.json`, preserve immutable result/policy digests "
            "and conservative caller liability, and execute one serialized "
            f"designated recovery run (`run_id={designated_run_id}`) without "
            "enlarged byte or API allowances."
        ),
    )
