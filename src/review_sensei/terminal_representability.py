"""Pre-effect terminal action-result representability and capacity guard (G34).

Every admitted maintainer command or reply effect must have a representable,
funded terminal settlement route under the active ``caller-action-result-v1``
inline carrier ceiling (``8,192`` bytes), ``SessionRecord`` root ceiling
(``32,768`` record bytes / ``65,536`` GitHub comment bytes), and original
physical API request allowance before any business ledger mutation, native
review/check refinalization, or command receipt comment is dispatched.

``caller-requests-v3`` remains reader-only by default per Section 8.3; read-only
current-root (``#431``) and ancestry (``#436``) observations never confer
writer or settlement authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .api_work import (
    ACCOUNTING_VERSION,
    LEGACY_ACCOUNTING_VERSION,
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
    ApiRunAccounting,
)
from .bounded_evidence import (
    INLINE_ENCODING,
    MAX_MANIFEST_BYTES,
    MAX_PART_BYTES,
    MAX_PART_READS,
    MAX_PARTITION_DECODED_BYTES,
    PARTITION_ENCODING,
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
    encode_evidence,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
    read_partitioned_evidence,
)
from .disposition import (
    FindingIdentity,
    MaintainerCommand,
    MaintainerCommandResult,
    apply_session_command,
)
from .errors import ReviewInputError
from .provenance_manifest import (
    RecoveryPrescription,
    _exact_bool,
    _exact_false_authority,
    _non_negative_int,
    prescribe_recovery,
)
from .recovery_context import (
    RecoveryRefusalError,
    ensure_not_observation_only_evidence,
)
from .session import (
    MAX_DISPOSITION_BYTES,
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    SessionIdentity,
    SessionLoadResult,
    SessionRecord,
    disposition_array_bytes,
)

ACTION_RESULT_ENVELOPE_VERSION = "caller-action-result-v1"
MAX_COVERAGE_TOKEN_BYTES = 256
MAX_COVERAGE_ENTRY_COUNT = 256
SUPPORTED_ACTION_WRITER_VERSIONS = frozenset(
    {LEGACY_ACCOUNTING_VERSION, ACCOUNTING_VERSION}
)

TERMINAL_REFUSAL_CODES = frozenset(
    {
        "observation_only_not_authority",
        "unsupported_v3_writer",
        "disposition_token_bytes_exceeded",
        "disposition_entry_cap_exceeded",
        "disposition_array_bytes_exceeded",
        "carrier_inline_bound_exceeded",
        "session_root_bytes_exceeded",
        "request_budget_exhausted_before_effect",
        "snapshot_parts_unavailable_for_preview",
    }
)

_PART_HEADER = "ReviewSensei immutable evidence part v1\n```json\n"
_PART_FOOTER = "\n```\n<!-- reviewsensei:evidence-part:v1 -->"

_SESSION_COMMENT_INTRO = "ReviewSensei session ledger (round counters only; no source)."
_DISPOSITION_ARRAY_OVERFLOW_MESSAGES = frozenset(
    {
        "session disposition limit reached",
        "session dispositions exceed the configured bound",
    }
)
_SESSION_ROOT_OVERFLOW_MESSAGES = frozenset(
    {
        "session record exceeds the configured size limit",
        "disposition snapshot has no future root capacity",
        "partition manifest exceeds actual lifecycle allocation",
    }
)


def _json_clone(value: object) -> Any:
    return json.loads(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def build_action_result_binding(
    *,
    identity: SessionIdentity,
    policy_commit: str,
    head_sha: str,
    intent_sha256: str,
    configuration_digest: str,
    owner_generation: int,
    producer: str,
) -> dict[str, object]:
    """Construct the exact ``caller-action-result-v1`` inline manifest binding."""

    if identity.repository_id is None:
        raise ReviewInputError("action result binding requires repository_id")
    return {
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "pull_request": identity.pull_request,
        "base_sha": policy_commit,
        "head_sha": head_sha,
        "policy_digest": intent_sha256,
        "configuration_digest": configuration_digest,
        "generation": _non_negative_int(owner_generation, field="owner_generation"),
        "producer": producer,
        "purpose": "evidence",
        "schema_version": "1.0",
    }


def estimate_session_record_bytes(record: SessionRecord | Mapping[str, object]) -> int:
    """Exact UTF-8 byte count of ``SessionRecord.to_dict()`` under canonical root JSON."""

    payload = record.to_dict() if isinstance(record, SessionRecord) else dict(record)
    return len(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def estimate_session_comment_bytes(
    record: SessionRecord,
    *,
    repository_id: int,
    pull_request: int,
    consumed_grants: Sequence[str] = (),
) -> int:
    """Exact UTF-8 byte count of the rendered GitHub session marker comment."""

    document = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
    grant_lines = "".join(
        f"<!-- reviewsensei:consumed-grant:v1 sha256={digest} -->\n"
        for digest in consumed_grants
    )
    marker_prefix = (
        "<!-- reviewsensei:session:v1"
        if record.quarantine_lineage is None
        else "<!-- reviewsensei:session:v2"
    )
    marker = (
        f"{marker_prefix} repo={repository_id} pr={pull_request} "
        f"gen={record.generation} digest={record.record_sha256} -->"
    )
    body = (
        f"{grant_lines}{_SESSION_COMMENT_INTRO}\n\n```json\n{document}\n```\n\n{marker}"
    )
    return len(body.encode("utf-8"))


@dataclass(frozen=True)
class ActionSettlementPricing:
    """Physical API request pricing for pre-effect action admission and settlement."""

    cold_census_requests: int = 0
    business_mutation_requests: int = 0
    refinalization_requests: int = 0
    receipt_publication_requests: int = 0
    part_write_and_readback_requests: int = 0
    terminal_close_requests: int = 1
    committed_requests: int = 0
    original_api_limit: int = 512
    startup_requests: int = 0
    ancestry_read_requests: int = 0
    snapshot_read_requests: int = 0
    retry_reconcile_requests: int = 0
    ambiguous_write_charges: int = 0
    ordinary_evidence_reads: int = 0
    protected_fence_reads: int = 4
    evidence_read_limit: int = MAX_PART_READS

    def __post_init__(self) -> None:
        for attr in (
            "cold_census_requests",
            "business_mutation_requests",
            "refinalization_requests",
            "receipt_publication_requests",
            "part_write_and_readback_requests",
            "terminal_close_requests",
            "committed_requests",
            "startup_requests",
            "ancestry_read_requests",
            "snapshot_read_requests",
            "retry_reconcile_requests",
            "ambiguous_write_charges",
            "ordinary_evidence_reads",
            "protected_fence_reads",
        ):
            object.__setattr__(
                self, attr, _non_negative_int(getattr(self, attr), field=attr)
            )
        limit = _non_negative_int(self.original_api_limit, field="original_api_limit")
        if limit <= 0:
            raise ReviewInputError("original_api_limit must be positive")
        object.__setattr__(self, "original_api_limit", limit)
        read_limit = _non_negative_int(
            self.evidence_read_limit, field="evidence_read_limit"
        )
        if read_limit <= 0 or self.protected_fence_reads >= read_limit:
            raise ReviewInputError(
                "evidence_read_limit must exceed protected_fence_reads"
            )
        object.__setattr__(self, "evidence_read_limit", read_limit)

    @property
    def ordinary_evidence_read_cap(self) -> int:
        return self.evidence_read_limit - self.protected_fence_reads

    def evidence_tail_fits(self) -> bool:
        return (
            self.ordinary_evidence_reads + self.snapshot_read_requests
        ) <= self.ordinary_evidence_read_cap

    @property
    def required_requests(self) -> int:
        return (
            self.startup_requests
            + self.ancestry_read_requests
            + self.snapshot_read_requests
            + self.retry_reconcile_requests
            + self.ambiguous_write_charges
            + self.cold_census_requests
            + self.business_mutation_requests
            + self.refinalization_requests
            + self.receipt_publication_requests
            + self.part_write_and_readback_requests
            + self.terminal_close_requests
        )

    @property
    def total_projected_requests(self) -> int:
        return self.committed_requests + self.required_requests

    @property
    def remaining_headroom(self) -> int:
        return self.original_api_limit - self.total_projected_requests

    def fits(self) -> bool:
        return (
            self.total_projected_requests <= self.original_api_limit
            and self.evidence_tail_fits()
        )

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "cold_census_requests": self.cold_census_requests,
            "business_mutation_requests": self.business_mutation_requests,
            "refinalization_requests": self.refinalization_requests,
            "receipt_publication_requests": self.receipt_publication_requests,
            "part_write_and_readback_requests": self.part_write_and_readback_requests,
            "terminal_close_requests": self.terminal_close_requests,
            "committed_requests": self.committed_requests,
            "startup_requests": self.startup_requests,
            "ancestry_read_requests": self.ancestry_read_requests,
            "snapshot_read_requests": self.snapshot_read_requests,
            "retry_reconcile_requests": self.retry_reconcile_requests,
            "ambiguous_write_charges": self.ambiguous_write_charges,
            "ordinary_evidence_reads": self.ordinary_evidence_reads,
            "protected_fence_reads": self.protected_fence_reads,
            "evidence_read_limit": self.evidence_read_limit,
            "ordinary_evidence_read_cap": self.ordinary_evidence_read_cap,
            "evidence_tail_fits": self.evidence_tail_fits(),
            "required_requests": self.required_requests,
            "total_projected_requests": self.total_projected_requests,
            "original_api_limit": self.original_api_limit,
            "remaining_headroom": self.remaining_headroom,
            "fits": self.fits(),
        }


@dataclass(frozen=True)
class TerminalRepresentabilityReport:
    """Structured pre-effect evaluation of terminal action representability.

    ``carrier_bytes`` measures the canonical JSON byte length of the
    ``caller-action-result-v1`` carrier object, whereas ``manifest_bytes``
    measures the canonical JSON byte length of the outer ``inline_manifest``
    wrapper (including ``binding``) or ``None`` when the inline manifest
    exceeds ``MAX_MANIFEST_BYTES`` (``8,192`` bytes).
    """

    representable: bool
    refusal_code: str | None
    reason: str
    disposition_entry_count: int
    max_disposition_token_bytes: int
    disposition_bytes: int
    envelope_bytes: int
    carrier_bytes: int
    payload_bytes: int
    manifest_bytes: int | None
    projected_record_bytes: int
    projected_comment_bytes: int
    future_reserve_bytes: int
    accounting_version: str
    v3_writer_enabled: bool
    pricing: ActionSettlementPricing
    recovery: RecoveryPrescription
    authority: bool = False
    max_intermediate_record_bytes: int = 0
    max_intermediate_comment_bytes: int = 0

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        object.__setattr__(
            self,
            "representable",
            _exact_bool(self.representable, field="representable"),
        )
        object.__setattr__(
            self,
            "v3_writer_enabled",
            _exact_bool(self.v3_writer_enabled, field="v3_writer_enabled"),
        )
        object.__setattr__(
            self,
            "max_intermediate_record_bytes",
            _non_negative_int(
                self.max_intermediate_record_bytes,
                field="max_intermediate_record_bytes",
            ),
        )
        object.__setattr__(
            self,
            "max_intermediate_comment_bytes",
            _non_negative_int(
                self.max_intermediate_comment_bytes,
                field="max_intermediate_comment_bytes",
            ),
        )
        if self.representable:
            if self.refusal_code is not None:
                raise ReviewInputError(
                    "representable terminal report cannot set refusal_code"
                )
        elif self.refusal_code not in TERMINAL_REFUSAL_CODES:
            raise ReviewInputError(f"unsupported refusal_code: {self.refusal_code!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "representable": self.representable,
            "refusal_code": self.refusal_code,
            "reason": self.reason,
            "disposition_entry_count": self.disposition_entry_count,
            "max_disposition_token_bytes": self.max_disposition_token_bytes,
            "disposition_bytes": self.disposition_bytes,
            "envelope_bytes": self.envelope_bytes,
            "carrier_bytes": self.carrier_bytes,
            "payload_bytes": self.payload_bytes,
            "manifest_bytes": self.manifest_bytes,
            "projected_record_bytes": self.projected_record_bytes,
            "projected_comment_bytes": self.projected_comment_bytes,
            "max_intermediate_record_bytes": self.max_intermediate_record_bytes,
            "max_intermediate_comment_bytes": self.max_intermediate_comment_bytes,
            "future_reserve_bytes": self.future_reserve_bytes,
            "accounting_version": self.accounting_version,
            "v3_writer_enabled": self.v3_writer_enabled,
            "pricing": self.pricing.to_dict(),
            "recovery": self.recovery.to_dict(),
            "authority": False,
        }


class TerminalRepresentabilityError(ReviewInputError):
    """Raised when pre-effect terminal representability refuses an unrecoverable write."""

    def __init__(self, report: TerminalRepresentabilityReport) -> None:
        self.report = report
        super().__init__(format_terminal_refusal_summary(report))


def format_terminal_refusal_summary(report: TerminalRepresentabilityReport) -> str:
    """Format a bounded user-safe refusal message with an actionable recovery path."""

    return (
        f"Terminal result representability refused ({report.refusal_code}): "
        f"{report.reason}; recovery={report.recovery.action} "
        f"via '{report.recovery.command}'."
    )


def _prescribe_terminal_recovery(
    refusal_code: str, *, action_name: str | None = None
) -> RecoveryPrescription:
    if refusal_code in {
        "carrier_inline_bound_exceeded",
        "disposition_entry_cap_exceeded",
        "disposition_token_bytes_exceeded",
        "disposition_array_bytes_exceeded",
    }:
        if action_name == "accept-coverage-uncertainty":
            return RecoveryPrescription(
                action="review_remaining",
                command="@reviewsensei review remaining",
                requires_fresh_inference=True,
                steps=(
                    "Run '@reviewsensei review remaining' to analyze uncovered files and shrink incomplete_coverage within the 8192-byte inline terminal manifest ceiling.",
                    "Alternatively run '@reviewsensei rescan' on the pinned head if original partial scope cannot be narrowed.",
                    "Once incomplete_coverage fits, re-evaluate convergence via '@reviewsensei verify' without losing prior obligations.",
                ),
            )
        return RecoveryPrescription(
            action="verify_convergence",
            command="@reviewsensei verify",
            requires_fresh_inference=False,
            steps=(
                "Apply overrides in smaller batches (for example 5-10 finding IDs per command) so each action result fits the 8192-byte inline ceiling.",
                "Run '@reviewsensei verify' after bounded batches to re-evaluate approval without new inference.",
            ),
        )
    if refusal_code == "session_root_bytes_exceeded":
        return RecoveryPrescription(
            action="rescan_under_pinned_provenance",
            command="@reviewsensei rescan",
            requires_fresh_inference=True,
            steps=(
                "Enable disposition snapshot staging or run '@reviewsensei rescan' on a clean head when cumulative root state reaches 32768 record / 65536 comment bytes.",
                "Do not manually delete session markers; preserve prior obligations and reservations.",
            ),
        )
    if refusal_code == "request_budget_exhausted_before_effect":
        return RecoveryPrescription(
            action="reconcile_owned_readback_then_hold_or_dispose",
            command="reconcile-owned-readback",
            requires_fresh_inference=False,
            steps=(
                "Do not dispatch partial mutations when physical API headroom cannot fund business, receipt, and terminal close writes.",
                "Issue an authorized fresh command run on the current head to execute within its own bounded caller allowance.",
            ),
        )
    if refusal_code == "unsupported_v3_writer":
        return RecoveryPrescription(
            action="rescan_under_pinned_provenance",
            command="@reviewsensei rescan",
            requires_fresh_inference=True,
            steps=(
                "Keep caller-requests-v3 writer activation disabled per Section 8.3 until all-writer cutover is authorized.",
                "Use supported caller-requests-v1 or caller-requests-v2 writers within the 8192-byte inline bound.",
            ),
        )
    if refusal_code == "snapshot_parts_unavailable_for_preview":
        return RecoveryPrescription(
            action="supply_original_context_or_rescan",
            command="supply-original-recovery-context",
            requires_fresh_inference=False,
            steps=(
                "Supply the owning session ledger or pre-authenticated disposition snapshot parts so preview can evaluate existing decisions without discarding snapshot state.",
                "Never strip an authenticated disposition_snapshot during pre-effect admission.",
            ),
        )
    return RecoveryPrescription(
        action="supply_original_context_or_rescan",
        command="supply-original-recovery-context",
        requires_fresh_inference=False,
        steps=(
            "Do not pass read-only action result or ancestry observations as writer or settlement authority.",
            "Load the authenticated current SessionRecord from the owning session ledger before evaluating settlement.",
        ),
    )


def _try_decode_inline_snapshot_dispositions(
    snapshot: Mapping[str, object] | None,
) -> tuple[Mapping[str, object], ...] | None:
    if snapshot is None:
        return None
    from .disposition_snapshot import (
        normalize_snapshot,
        snapshot_document,
    )

    try:
        root = normalize_snapshot(snapshot)
        if root is None or root["schema_version"] != "1.0":
            return None
        manifest = root["state_manifest"]
        binding = manifest["binding"]
        if manifest.get("encoding") != INLINE_ENCODING:
            return None
        document = read_inline_evidence(manifest, expected_binding=binding)
        return snapshot_document(
            document,
            result_digest=root["result_digest"],
            item_count=manifest["item_count"],
        )
    except ReviewInputError:
        return None


def evaluate_terminal_action_representability(
    *,
    identity: SessionIdentity,
    intent_sha256: str,
    outcome: Mapping[str, object],
    result_document: Mapping[str, object],
    binding: Mapping[str, object],
    current_record: object | None = None,
    accounting_version: str = ACCOUNTING_VERSION,
    pricing: ActionSettlementPricing | None = None,
    future_reserve_bytes: int = 0,
    consumed_grants: Sequence[str] = (),
    allow_v3_writer: bool = False,
    disposition_entry_count: int = 0,
    max_disposition_token_bytes: int = 0,
    action_name: str | None = None,
    resolved_dispositions: Sequence[Mapping[str, object]] | None = None,
    intermediate_record: SessionRecord | None = None,
) -> TerminalRepresentabilityReport:
    """Evaluate whether a candidate action result and root close are representable pre-effect."""

    effective_pricing = pricing or ActionSettlementPricing()
    reserve = _non_negative_int(future_reserve_bytes, field="future_reserve_bytes")
    v3_enabled = _exact_bool(allow_v3_writer, field="allow_v3_writer")

    for candidate in (current_record, result_document, outcome):
        if candidate is not None:
            try:
                ensure_not_observation_only_evidence(
                    candidate, check_writer_enrollment=not v3_enabled
                )
            except ReviewInputError as exc:
                candidate_code = (
                    exc.refusal_code
                    if isinstance(exc, RecoveryRefusalError)
                    and exc.refusal_code in TERMINAL_REFUSAL_CODES
                    else "observation_only_not_authority"
                )
                return TerminalRepresentabilityReport(
                    representable=False,
                    refusal_code=candidate_code,
                    reason=str(exc),
                    disposition_entry_count=disposition_entry_count,
                    max_disposition_token_bytes=max_disposition_token_bytes,
                    disposition_bytes=0,
                    envelope_bytes=0,
                    carrier_bytes=0,
                    payload_bytes=0,
                    manifest_bytes=None,
                    projected_record_bytes=0,
                    projected_comment_bytes=0,
                    future_reserve_bytes=reserve,
                    accounting_version=accounting_version,
                    v3_writer_enabled=v3_enabled,
                    pricing=effective_pricing,
                    recovery=_prescribe_terminal_recovery(
                        candidate_code, action_name=action_name
                    ),
                )

    record_obj = current_record if isinstance(current_record, SessionRecord) else None
    if resolved_dispositions is not None:
        disp_bytes = disposition_array_bytes(resolved_dispositions)
    elif record_obj is not None:
        if record_obj.dispositions:
            disp_bytes = disposition_array_bytes(record_obj.dispositions)
        else:
            inline_disps = _try_decode_inline_snapshot_dispositions(
                record_obj.disposition_snapshot
            )
            disp_bytes = (
                disposition_array_bytes(inline_disps)
                if inline_disps is not None
                else disposition_array_bytes(record_obj.dispositions)
            )
    else:
        disp_bytes = 0

    if accounting_version == PARTITIONED_ACTION_ACCOUNTING_VERSION and not v3_enabled:
        return TerminalRepresentabilityReport(
            representable=False,
            refusal_code="unsupported_v3_writer",
            reason="caller-requests-v3 writer activation is disabled by default",
            disposition_entry_count=disposition_entry_count,
            max_disposition_token_bytes=max_disposition_token_bytes,
            disposition_bytes=disp_bytes,
            envelope_bytes=0,
            carrier_bytes=0,
            payload_bytes=0,
            manifest_bytes=None,
            projected_record_bytes=(
                estimate_session_record_bytes(record_obj)
                if record_obj is not None
                else 0
            ),
            projected_comment_bytes=0,
            future_reserve_bytes=reserve,
            accounting_version=accounting_version,
            v3_writer_enabled=v3_enabled,
            pricing=effective_pricing,
            recovery=_prescribe_terminal_recovery(
                "unsupported_v3_writer", action_name=action_name
            ),
        )
    if (
        accounting_version not in SUPPORTED_ACTION_WRITER_VERSIONS
        and accounting_version != PARTITIONED_ACTION_ACCOUNTING_VERSION
    ):
        raise ReviewInputError(
            f"unsupported action accounting version: {accounting_version!r}"
        )

    envelope = {
        "schema_version": ACTION_RESULT_ENVELOPE_VERSION,
        "intent_sha256": intent_sha256,
        "outcome": _json_clone(dict(outcome)),
        "result": _json_clone(dict(result_document)),
    }
    envelope_bytes = len(canonical_bytes(envelope))
    payload = encode_evidence(envelope, max_decoded_bytes=MAX_PARTITION_DECODED_BYTES)
    payload_bytes = len(canonical_bytes(payload))
    carrier = {
        "schema_version": ACTION_RESULT_ENVELOPE_VERSION,
        "payload": payload,
    }
    carrier_bytes = len(canonical_bytes(carrier))
    normalized_binding = dict(binding)
    manifest = inline_manifest(carrier, binding=normalized_binding, item_count=1)
    manifest_bytes = len(canonical_bytes(manifest)) if manifest is not None else None

    if not effective_pricing.fits():
        budget_reason = (
            f"projected physical requests {effective_pricing.total_projected_requests} "
            f"exceed original limit {effective_pricing.original_api_limit}"
            if effective_pricing.total_projected_requests
            > effective_pricing.original_api_limit
            else (
                f"ordinary evidence reads "
                f"{effective_pricing.ordinary_evidence_reads + effective_pricing.snapshot_read_requests} "
                f"exceed cap {effective_pricing.ordinary_evidence_read_cap} "
                f"preserving {effective_pricing.protected_fence_reads} fence reads"
            )
        )
        return TerminalRepresentabilityReport(
            representable=False,
            refusal_code="request_budget_exhausted_before_effect",
            reason=budget_reason,
            disposition_entry_count=disposition_entry_count,
            max_disposition_token_bytes=max_disposition_token_bytes,
            disposition_bytes=disp_bytes,
            envelope_bytes=envelope_bytes,
            carrier_bytes=carrier_bytes,
            payload_bytes=payload_bytes,
            manifest_bytes=manifest_bytes,
            projected_record_bytes=(
                estimate_session_record_bytes(record_obj)
                if record_obj is not None
                else 0
            ),
            projected_comment_bytes=0,
            future_reserve_bytes=reserve,
            accounting_version=accounting_version,
            v3_writer_enabled=v3_enabled,
            pricing=effective_pricing,
            recovery=_prescribe_terminal_recovery(
                "request_budget_exhausted_before_effect", action_name=action_name
            ),
        )

    if (
        manifest is None
        or manifest_bytes is None
        or manifest_bytes > MAX_MANIFEST_BYTES
        or carrier_bytes > MAX_MANIFEST_BYTES
        or payload_bytes > MAX_MANIFEST_BYTES
    ):
        return TerminalRepresentabilityReport(
            representable=False,
            refusal_code="carrier_inline_bound_exceeded",
            reason=(
                f"encoded action carrier/inline manifest "
                f"(carrier={carrier_bytes}B, manifest={manifest_bytes}B, "
                f"envelope={envelope_bytes}B, disposition={disp_bytes}B) "
                f"exceeds MAX_MANIFEST_BYTES={MAX_MANIFEST_BYTES}"
            ),
            disposition_entry_count=disposition_entry_count,
            max_disposition_token_bytes=max_disposition_token_bytes,
            disposition_bytes=disp_bytes,
            envelope_bytes=envelope_bytes,
            carrier_bytes=carrier_bytes,
            payload_bytes=payload_bytes,
            manifest_bytes=manifest_bytes,
            projected_record_bytes=(
                estimate_session_record_bytes(record_obj)
                if record_obj is not None
                else 0
            ),
            projected_comment_bytes=0,
            future_reserve_bytes=reserve,
            accounting_version=accounting_version,
            v3_writer_enabled=v3_enabled,
            pricing=effective_pricing,
            recovery=_prescribe_terminal_recovery(
                "carrier_inline_bound_exceeded", action_name=action_name
            ),
        )

    decoded_carrier = read_inline_evidence(
        manifest, expected_binding=normalized_binding
    )
    if decoded_carrier != carrier:
        raise ReviewInputError(
            "inline action result manifest failed binding/carrier round-trip"
        )

    projected_record_bytes = 0
    projected_comment_bytes = 0
    max_intermediate_record_bytes = 0
    max_intermediate_comment_bytes = 0
    if record_obj is not None:
        repo_id = identity.repository_id or record_obj.repository_id or 1
        inter_rec = (
            intermediate_record if intermediate_record is not None else record_obj
        )
        inter_record_bytes = estimate_session_record_bytes(inter_rec) + reserve
        inter_comment_bytes = (
            estimate_session_comment_bytes(
                inter_rec,
                repository_id=repo_id,
                pull_request=identity.pull_request,
                consumed_grants=consumed_grants,
            )
            + reserve
        )
        raw_payload = copy.deepcopy(record_obj.to_dict())
        raw_payload["generation"] = record_obj.generation + 1
        accounting_doc = raw_payload.get("api_run_accounting")
        if isinstance(accounting_doc, dict) and isinstance(
            accounting_doc.get("action"), dict
        ):
            updated_accounting = copy.deepcopy(accounting_doc)
            updated_accounting["action"] = {
                **updated_accounting["action"],
                "execution_started": True,
                "phase": "terminal",
                "result": manifest,
                "outcome": _json_clone(dict(outcome)),
            }
            raw_payload["api_run_accounting"] = updated_accounting
        raw_record_bytes = estimate_session_record_bytes(raw_payload)
        projected_record_bytes = raw_record_bytes + reserve
        max_intermediate_record_bytes = max(inter_record_bytes, projected_record_bytes)
        if max_intermediate_record_bytes > MAX_SESSION_RECORD_BYTES:
            return TerminalRepresentabilityReport(
                representable=False,
                refusal_code="session_root_bytes_exceeded",
                reason=(
                    f"projected/intermediate SessionRecord size {max_intermediate_record_bytes} bytes "
                    f"(terminal={raw_record_bytes}B + reserve={reserve}B) "
                    f"exceeds MAX_SESSION_RECORD_BYTES={MAX_SESSION_RECORD_BYTES}"
                ),
                disposition_entry_count=disposition_entry_count,
                max_disposition_token_bytes=max_disposition_token_bytes,
                disposition_bytes=disp_bytes,
                envelope_bytes=envelope_bytes,
                carrier_bytes=carrier_bytes,
                payload_bytes=payload_bytes,
                manifest_bytes=manifest_bytes,
                projected_record_bytes=projected_record_bytes,
                projected_comment_bytes=0,
                future_reserve_bytes=reserve,
                accounting_version=accounting_version,
                v3_writer_enabled=v3_enabled,
                pricing=effective_pricing,
                recovery=_prescribe_terminal_recovery(
                    "session_root_bytes_exceeded", action_name=action_name
                ),
                max_intermediate_record_bytes=max_intermediate_record_bytes,
                max_intermediate_comment_bytes=inter_comment_bytes,
            )
        evolved = record_obj.evolve(
            generation=record_obj.generation + 1,
            api_run_accounting=cast(
                Mapping[str, object] | None, raw_payload.get("api_run_accounting")
            ),
        )
        raw_comment_bytes = estimate_session_comment_bytes(
            evolved,
            repository_id=repo_id,
            pull_request=identity.pull_request,
            consumed_grants=consumed_grants,
        )
        projected_comment_bytes = raw_comment_bytes + reserve
        max_intermediate_comment_bytes = max(
            inter_comment_bytes, projected_comment_bytes
        )
        if max_intermediate_comment_bytes > MAX_SESSION_COMMENT_BYTES:
            return TerminalRepresentabilityReport(
                representable=False,
                refusal_code="session_root_bytes_exceeded",
                reason=(
                    f"projected/intermediate session comment size {max_intermediate_comment_bytes} bytes "
                    f"(comment={raw_comment_bytes}B + reserve={reserve}B) "
                    f"exceeds MAX_SESSION_COMMENT_BYTES={MAX_SESSION_COMMENT_BYTES}"
                ),
                disposition_entry_count=disposition_entry_count,
                max_disposition_token_bytes=max_disposition_token_bytes,
                disposition_bytes=disp_bytes,
                envelope_bytes=envelope_bytes,
                carrier_bytes=carrier_bytes,
                payload_bytes=payload_bytes,
                manifest_bytes=manifest_bytes,
                projected_record_bytes=projected_record_bytes,
                projected_comment_bytes=projected_comment_bytes,
                future_reserve_bytes=reserve,
                accounting_version=accounting_version,
                v3_writer_enabled=v3_enabled,
                pricing=effective_pricing,
                recovery=_prescribe_terminal_recovery(
                    "session_root_bytes_exceeded", action_name=action_name
                ),
                max_intermediate_record_bytes=max_intermediate_record_bytes,
                max_intermediate_comment_bytes=max_intermediate_comment_bytes,
            )

    return TerminalRepresentabilityReport(
        representable=True,
        refusal_code=None,
        reason="terminal action carrier, root envelope, and request budget fit",
        disposition_entry_count=disposition_entry_count,
        max_disposition_token_bytes=max_disposition_token_bytes,
        disposition_bytes=disp_bytes,
        envelope_bytes=envelope_bytes,
        carrier_bytes=carrier_bytes,
        payload_bytes=payload_bytes,
        manifest_bytes=manifest_bytes,
        projected_record_bytes=projected_record_bytes,
        projected_comment_bytes=projected_comment_bytes,
        future_reserve_bytes=reserve,
        accounting_version=accounting_version,
        v3_writer_enabled=v3_enabled,
        pricing=effective_pricing,
        recovery=prescribe_recovery("supported_read_only_observation"),
        max_intermediate_record_bytes=max_intermediate_record_bytes,
        max_intermediate_comment_bytes=max_intermediate_comment_bytes,
    )


class _PassiveBudgetCheck:
    @staticmethod
    def check() -> None:
        return None


def _verify_partitioned_manifest_without_consume(
    manifest: Mapping[str, object],
    *,
    reader: Callable[[str], AuthenticatedPart],
    expected_binding: Mapping[str, object],
    budget: Any = None,
) -> object:
    check_target = (
        budget
        if callable(getattr(budget, "check", None))
        else cast(Any, _PassiveBudgetCheck())
    )
    return read_partitioned_evidence(
        dict(manifest),
        reader=reader,
        expected_binding=expected_binding,
        budget=cast(EvidenceReadBudget, check_target),
    )


def _extract_part_from_source_ledger(
    source_ledger: object,
    identity: SessionIdentity,
    storage_id: str,
    *,
    producer: str,
) -> AuthenticatedPart | None:
    verified = getattr(source_ledger, "_verified_parts", None)
    if isinstance(verified, Mapping):
        cached_part = verified.get(storage_id)
        if isinstance(cached_part, AuthenticatedPart):
            return AuthenticatedPart(
                copy.deepcopy(cached_part.document), cached_part.producer
            )

    if storage_id.isdigit() and 1 <= int(storage_id) < 10**19:
        comment_sources: list[Mapping[Any, Any]] = []
        comments_by_id = getattr(source_ledger, "_comments_by_id", None)
        if isinstance(comments_by_id, Mapping):
            comment_sources.append(comments_by_id)
        startup_snap = getattr(source_ledger, "_quarantine_startup_snapshot", None)
        startup_comments = (
            getattr(startup_snap, "comments_by_id", None)
            if startup_snap is not None
            else None
        )
        if isinstance(startup_comments, Mapping):
            comment_sources.append(startup_comments)
        comment_id = int(storage_id)
        for source_map in comment_sources:
            item = source_map.get(comment_id)
            if not isinstance(item, dict):
                continue
            part_owner = getattr(source_ledger, "_part_owner", None)
            owned = part_owner(item, identity) if callable(part_owner) else item
            if (
                not isinstance(owned, Mapping)
                or type(owned.get("id")) is not int
                or owned.get("id") != comment_id
            ):
                raise ReviewInputError("GitHub partition identity does not match")
            body = owned.get("body")
            if (
                not isinstance(body, str)
                or len(body.encode("utf-8")) > MAX_PART_BYTES
                or not body.startswith(_PART_HEADER)
                or not body.endswith(_PART_FOOTER)
            ):
                raise ReviewInputError("GitHub partition framing is invalid")
            raw_text = body.split("\n```json\n", 1)[1].rsplit("\n```\n", 1)[0]
            try:
                doc = json.loads(raw_text)
                if canonical_bytes(doc).decode("utf-8") != raw_text:
                    raise ValueError("noncanonical part")
            except (ValueError, UnicodeError) as exc:
                raise ReviewInputError("GitHub partition JSON is invalid") from exc
            return AuthenticatedPart(doc, producer)

    part_docs = getattr(source_ledger, "_part_documents", None)
    if isinstance(part_docs, Mapping):
        local_cached = part_docs.get((identity, storage_id))
        if isinstance(local_cached, tuple) and len(local_cached) == 2:
            raw_bytes = local_cached[1]
            if isinstance(raw_bytes, (bytes, bytearray)):
                try:
                    doc = json.loads(bytes(raw_bytes))
                    if canonical_bytes(doc) == bytes(raw_bytes):
                        return AuthenticatedPart(doc, producer)
                except (ValueError, UnicodeError) as exc:
                    raise ReviewInputError("cached partition JSON is invalid") from exc

    root_dir = getattr(source_ledger, "root", None)
    if isinstance(root_dir, Path) and re.fullmatch(r"[a-f0-9]{64}", storage_id):
        safe_repo = identity.repository.replace("/", "%2F")
        evidence_dir = root_dir / ".evidence"
        part_dir = evidence_dir / safe_repo / str(identity.pull_request)
        part_path = part_dir / storage_id
        if (
            not any(
                p.is_symlink()
                for p in (root_dir, evidence_dir, part_dir.parent, part_dir, part_path)
            )
            and part_path.is_file()
        ):
            try:
                raw_file = part_path.read_bytes()
                if (
                    len(raw_file) <= MAX_PART_BYTES
                    and hashlib.sha256(raw_file).hexdigest() == storage_id
                ):
                    doc = json.loads(raw_file)
                    if canonical_bytes(doc) == raw_file:
                        return AuthenticatedPart(doc, producer)
            except (OSError, ValueError, UnicodeError) as exc:
                raise ReviewInputError("local partition is missing or invalid") from exc

    for attr_name in ("_parts", "_preloaded_parts"):
        mem_parts = getattr(source_ledger, attr_name, None)
        if isinstance(mem_parts, Mapping) and storage_id in mem_parts:
            entry = mem_parts[storage_id]
            if isinstance(entry, AuthenticatedPart):
                return AuthenticatedPart(copy.deepcopy(entry.document), entry.producer)
            if isinstance(entry, tuple) and len(entry) == 2:
                return AuthenticatedPart(copy.deepcopy(entry[0]), str(entry[1]))

    return None


def _preload_snapshot_parts_from_ledger(
    source_ledger: object | None,
    identity: SessionIdentity,
    record: SessionRecord | None,
    *,
    producer: str,
) -> dict[str, AuthenticatedPart]:
    if source_ledger is None or record is None or record.disposition_snapshot is None:
        return {}
    from .disposition_snapshot import normalize_snapshot

    root = normalize_snapshot(record.disposition_snapshot)
    if root is None:
        return {}
    manifest = root["state_manifest"]
    binding = manifest["binding"]
    preloaded: dict[str, AuthenticatedPart] = {}

    if manifest.get("encoding") == PARTITION_ENCODING:
        for ref in manifest.get("parts", ()):
            storage_id = str(ref["storage_id"])
            extracted = _extract_part_from_source_ledger(
                source_ledger, identity, storage_id, producer=producer
            )
            if extracted is None:
                return preloaded
            preloaded[storage_id] = extracted

    if root["schema_version"] == "2.0":
        try:
            index_doc = (
                read_inline_evidence(manifest, expected_binding=binding)
                if manifest.get("encoding") == INLINE_ENCODING
                else _verify_partitioned_manifest_without_consume(
                    manifest,
                    reader=lambda sid: preloaded[sid],
                    expected_binding=binding,
                )
            )
        except (KeyError, ReviewInputError):
            return preloaded
        if isinstance(index_doc, Mapping):
            for page_ref in index_doc.get("pages", ()):
                if (
                    isinstance(page_ref, Mapping)
                    and page_ref.get("encoding") == PARTITION_ENCODING
                ):
                    for ref in page_ref.get("parts", ()):
                        storage_id = str(ref["storage_id"])
                        if storage_id in preloaded:
                            continue
                        extracted = _extract_part_from_source_ledger(
                            source_ledger, identity, storage_id, producer=producer
                        )
                        if extracted is not None:
                            preloaded[storage_id] = extracted

    return preloaded


class _InMemoryPreviewLedger:
    """Pure in-memory sandbox ledger that never mutates real storage or touches I/O."""

    def __init__(
        self,
        record: SessionRecord | None,
        *,
        producer: str = "local-ledger",
        enable_disposition_snapshot_writes: bool = False,
        supports_paged_disposition_snapshot_writes: bool = False,
        evidence_budget: Any = None,
        preloaded_parts: Mapping[str, AuthenticatedPart] | None = None,
        preloaded_dispositions: Sequence[Mapping[str, object]] | None = None,
        disposition_page_cache: Mapping[str, Any] | None = None,
    ) -> None:
        self._record = record
        self._producer = producer
        self.enable_disposition_snapshot_writes = enable_disposition_snapshot_writes
        self.supports_paged_disposition_snapshot_writes = (
            supports_paged_disposition_snapshot_writes
        )
        self.evidence_budget = (
            evidence_budget
            if evidence_budget is not None
            else (
                EvidenceReadBudget()
                if record is not None and record.disposition_snapshot is not None
                else None
            )
        )
        self._parts: dict[str, tuple[object, str]] = {}
        self._preloaded_parts: dict[str, AuthenticatedPart] = {
            str(part_id): AuthenticatedPart(copy.deepcopy(part.document), part.producer)
            for part_id, part in (preloaded_parts or {}).items()
        }
        self._staged_documents: dict[str, tuple[dict[str, object], object]] = {}
        self._disposition_page_cache: dict[str, Any] = copy.deepcopy(
            dict(disposition_page_cache or {})
        )
        self._preloaded_snapshot_doc: (
            tuple[str, dict[str, object], dict[str, object]] | None
        ) = None
        if (
            preloaded_dispositions is not None
            and record is not None
            and record.disposition_snapshot is not None
        ):
            self._register_preloaded_dispositions(record, preloaded_dispositions)

    def _register_preloaded_dispositions(
        self,
        record: SessionRecord,
        preloaded_dispositions: Sequence[Mapping[str, object]],
    ) -> None:
        from .disposition_snapshot import normalize_snapshot
        from .session import _stored_dispositions

        root = normalize_snapshot(record.disposition_snapshot)
        if root is None or root["schema_version"] != "1.0":
            return
        manifest = root["state_manifest"]
        binding = dict(manifest["binding"])
        stored = _stored_dispositions(
            [dict(item) for item in preloaded_dispositions], snapshot=True
        )
        candidate_doc: dict[str, object] = {
            "schema_version": "1.0",
            "result_digest": root["result_digest"],
            "dispositions": list(stored),
        }
        raw = canonical_bytes(candidate_doc)
        if len(stored) != manifest.get("item_count"):
            raise ReviewInputError(
                "preloaded disposition snapshot item_count does not match authenticated root"
            )
        if manifest.get("encoding") == PARTITION_ENCODING and (
            len(raw) != manifest.get("decoded_bytes")
            or hashlib.sha256(raw).hexdigest() != manifest.get("sha256")
        ):
            raise ReviewInputError(
                "preloaded disposition snapshot digest does not match authenticated root"
            )
        key = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        self._preloaded_snapshot_doc = (key, binding, candidate_doc)

    def evidence_producer(self) -> str:
        return self._producer

    def _write_part(
        self, identity: SessionIdentity, part_id: str, payload: object
    ) -> str:
        self._parts[part_id] = (copy.deepcopy(payload), self.evidence_producer())
        return part_id

    def _read_part(
        self, identity: SessionIdentity, storage_id: str
    ) -> AuthenticatedPart:
        if storage_id in self._parts:
            doc, prod = self._parts[storage_id]
            return AuthenticatedPart(copy.deepcopy(doc), prod)
        preloaded = self._preloaded_parts.get(storage_id)
        if preloaded is not None:
            return AuthenticatedPart(
                copy.deepcopy(preloaded.document), preloaded.producer
            )
        raise ReviewInputError("preview part is unavailable")

    def stage_evidence(
        self,
        identity: SessionIdentity,
        *,
        binding: dict[str, object],
        document: object,
        item_count: int,
        max_manifest_bytes: int,
    ) -> dict[str, object]:
        if not self.enable_disposition_snapshot_writes:
            raise ReviewInputError(
                "partition writers require explicit reader-first enablement"
            )
        if binding.get("purpose") != "dispositions":
            raise ReviewInputError(
                f"unsupported preview partition purpose: {binding.get('purpose')!r}"
            )
        inlined = inline_manifest(document, binding=binding, item_count=item_count)
        if (
            inlined is not None
            and isinstance(max_manifest_bytes, int)
            and not isinstance(max_manifest_bytes, bool)
            and len(canonical_bytes(inlined)) <= max_manifest_bytes
        ):
            return inlined

        parts = partition_evidence(document, binding=binding, item_count=item_count)
        storage_ids = tuple(
            self._write_part(
                identity,
                hashlib.sha256(canonical_bytes(part)).hexdigest(),
                part,
            )
            for part in parts
        )
        manifest = evidence_manifest(
            document,
            binding=binding,
            item_count=item_count,
            parts=parts,
            storage_ids=storage_ids,
        )
        if len(canonical_bytes(manifest)) > max_manifest_bytes:
            raise ReviewInputError(
                "partition manifest exceeds actual lifecycle allocation"
            )
        key = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        self._staged_documents[key] = (dict(binding), copy.deepcopy(document))
        return manifest

    def stage_disposition_snapshot_batch(
        self,
        identity: SessionIdentity,
        *,
        record: SessionRecord,
        binding: dict[str, object],
        pages: Sequence[Mapping[str, Any]],
        values: tuple[Mapping[str, object], ...],
        result_digest: str,
        max_manifest_bytes: int,
    ) -> dict[str, object]:
        from .disposition_snapshot import (
            prepare_local_paged_snapshot,
            read_paged_snapshot,
        )
        from .session import _remember_disposition_pages

        if not self.enable_disposition_snapshot_writes:
            raise ReviewInputError("disposition snapshot writer is disabled")
        if (
            binding.get("repository") != identity.repository
            or binding.get("pull_request") != identity.pull_request
            or binding.get("repository_id") != identity.repository_id
            or binding.get("producer") != self.evidence_producer()
            or binding.get("purpose") != "dispositions"
        ):
            raise ReviewInputError("preview disposition batch binding does not match")
        prepared = prepare_local_paged_snapshot(
            binding=binding,
            pages=pages,
            values=values,
            result_digest=result_digest,
            max_manifest_bytes=max_manifest_bytes,
        )
        for part_id, part_doc in prepared.parts.items():
            if part_id not in self._preloaded_parts:
                self._write_part(identity, part_id, part_doc)
        document = self.read_evidence(
            identity, prepared.manifest, expected_binding=binding
        )
        root = {
            "schema_version": "2.0",
            "result_digest": result_digest,
            "state_manifest": prepared.manifest,
        }
        restored = read_paged_snapshot(
            document, root=root, reader=self.read_evidence, identity=identity
        )
        if restored != values:
            raise ReviewInputError("preview disposition batch readback differs")
        _remember_disposition_pages(self, root, document)
        return prepared.manifest

    def read_evidence(
        self,
        identity: SessionIdentity,
        manifest: dict[str, object],
        *,
        expected_binding: Mapping[str, object],
    ) -> object:
        if isinstance(manifest, dict) and manifest.get("encoding") == INLINE_ENCODING:
            return read_inline_evidence(manifest, expected_binding=expected_binding)
        key = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        staged = self._staged_documents.get(key)
        if staged is not None:
            if staged[0] != dict(expected_binding):
                raise ReviewInputError("preview partition binding or manifest mismatch")
            return copy.deepcopy(staged[1])
        if (
            self._preloaded_snapshot_doc is not None
            and self._preloaded_snapshot_doc[0] == key
        ):
            if self._preloaded_snapshot_doc[1] != dict(expected_binding):
                raise ReviewInputError("preview partition binding or manifest mismatch")
            return copy.deepcopy(self._preloaded_snapshot_doc[2])
        if (
            isinstance(manifest, dict)
            and manifest.get("encoding") == PARTITION_ENCODING
        ):
            parts = manifest.get("parts")
            if isinstance(parts, list) and all(
                isinstance(ref, Mapping)
                and (
                    str(ref.get("storage_id")) in self._parts
                    or str(ref.get("storage_id")) in self._preloaded_parts
                )
                for ref in parts
            ):
                return _verify_partitioned_manifest_without_consume(
                    manifest,
                    reader=lambda storage_id: self._read_part(identity, storage_id),
                    expected_binding=expected_binding,
                    budget=self.evidence_budget,
                )
            raise ReviewInputError("preview part is unavailable")
        raise ReviewInputError("preview partition binding or manifest mismatch")

    def read_disposition_snapshot(
        self,
        identity: SessionIdentity,
        record: SessionRecord | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Serve authenticated snapshot entries in-memory with 0 evidence_budget calls."""

        target = record if record is not None else self._record
        if target is None:
            return ()
        if target.disposition_snapshot is None:
            return target.dispositions
        from .disposition_snapshot import (
            normalize_snapshot,
            read_paged_snapshot,
            snapshot_document,
        )
        from .session import _remember_disposition_pages

        if isinstance(self.evidence_budget, EvidenceReadBudget):
            self.evidence_budget.check()
        root = normalize_snapshot(target.disposition_snapshot)
        assert root is not None
        manifest = root["state_manifest"]
        binding = manifest["binding"]
        if binding["producer"] != self.evidence_producer():
            raise ReviewInputError(
                "disposition snapshot producer does not match session"
            )
        document = self.read_evidence(identity, manifest, expected_binding=binding)
        if root["schema_version"] == "2.0":
            values = read_paged_snapshot(
                document, root=root, reader=self.read_evidence, identity=identity
            )
            _remember_disposition_pages(self, root, document)
            return values
        return snapshot_document(
            document,
            result_digest=root["result_digest"],
            item_count=manifest["item_count"],
        )

    def load(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionLoadResult:
        if self._record is None:
            return SessionLoadResult(status="missing", record=None)
        return SessionLoadResult(status="ok", record=self._record)

    def initialize(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionRecord:
        self._record = SessionRecord.create(identity, now=now)
        return self._record

    def initialize_with_mutation(
        self,
        identity: SessionIdentity,
        mutate: Any,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        self._record = mutate(SessionRecord.create(identity, now=now))
        return self._record

    def replace(
        self,
        identity: SessionIdentity,
        mutate: Any,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        if self._record is None:
            raise ReviewInputError("preview session record is missing")
        self._record = mutate(self._record)
        return self._record

    def reenroll(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionRecord:
        self._record = SessionRecord.create(identity, now=now)
        return self._record

    def continue_after_abandoned_analysis(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionRecord:
        if self._record is None:
            raise ReviewInputError("preview session record is missing")
        self._record = self._record.evolve(
            now=now,
            generation=self._record.generation + 1,
            reservation_id=None,
            reserved_slot=None,
            operator_paused=False,
            failed_attempts=self._record.failed_attempts + 1,
        )
        return self._record


def _resolve_preview_producer(
    *,
    current_record: SessionRecord | None,
    source_ledger: object | None,
    binding: Mapping[str, object],
) -> str:
    if source_ledger is not None:
        producer_fn = getattr(source_ledger, "evidence_producer", None)
        if callable(producer_fn):
            value = producer_fn()
            if isinstance(value, str) and value:
                return value
    if current_record is not None and isinstance(
        current_record.disposition_snapshot, Mapping
    ):
        state_manifest = current_record.disposition_snapshot.get("state_manifest")
        if isinstance(state_manifest, Mapping):
            snap_binding = state_manifest.get("binding")
            if isinstance(snap_binding, Mapping):
                snap_producer = snap_binding.get("producer")
                if isinstance(snap_producer, str) and snap_producer:
                    return snap_producer
    binding_producer = binding.get("producer")
    if isinstance(binding_producer, str) and binding_producer:
        return binding_producer
    return "local-ledger"


def preview_session_command_representability(
    *,
    identity: SessionIdentity,
    current_record: SessionRecord | None,
    command: MaintainerCommand,
    intent_sha256: str,
    binding: Mapping[str, object],
    finding_fingerprints: Sequence[str] | None = None,
    finding_inventory: Sequence[FindingIdentity] | None = None,
    prior_inventories: Sequence[tuple[str, Sequence[FindingIdentity]]] = (),
    actor_role: str | None = None,
    policy_digest: str | None = None,
    provenance: str | None = None,
    incomplete_coverage: Sequence[str] | None = None,
    enable_disposition_snapshot_writes: bool = False,
    supports_paged_disposition_snapshot_writes: bool | None = None,
    evidence_budget: Any = None,
    source_ledger: Any = None,
    preloaded_parts: Mapping[str, AuthenticatedPart] | None = None,
    preloaded_dispositions: Sequence[Mapping[str, object]] | None = None,
    accounting_version: str = ACCOUNTING_VERSION,
    pricing: ActionSettlementPricing | None = None,
    future_reserve_bytes: int = 0,
    consumed_grants: Sequence[str] = (),
    allow_v3_writer: bool = False,
    now: datetime | None = None,
) -> tuple[
    MaintainerCommandResult | None, SessionRecord | None, TerminalRepresentabilityReport
]:
    """Dry-run ``apply_session_command`` in-memory and evaluate G34 representability.

    Never mutates any remote or caller-owned session ledger and never triggers
    network requests. Preserves ``current_record.disposition_snapshot`` on the
    in-memory sandbox record and serves pre-loaded snapshot entries with 0 extra
    ``evidence_budget`` physical calls.
    """

    effective_pricing = pricing or ActionSettlementPricing()
    reserve = _non_negative_int(future_reserve_bytes, field="future_reserve_bytes")
    v3_enabled = _exact_bool(allow_v3_writer, field="allow_v3_writer")
    coverage_items = tuple(incomplete_coverage or ())
    entry_count = len(coverage_items)
    max_token_bytes = max(
        (len(token.encode("utf-8")) for token in coverage_items), default=0
    )

    if command.action == "accept-coverage-uncertainty" and coverage_items:
        if max_token_bytes > MAX_COVERAGE_TOKEN_BYTES:
            return (
                None,
                None,
                TerminalRepresentabilityReport(
                    representable=False,
                    refusal_code="disposition_token_bytes_exceeded",
                    reason=(
                        f"incomplete_coverage token is {max_token_bytes} bytes, "
                        f"exceeding {MAX_COVERAGE_TOKEN_BYTES}-byte limit"
                    ),
                    disposition_entry_count=entry_count,
                    max_disposition_token_bytes=max_token_bytes,
                    disposition_bytes=0,
                    envelope_bytes=0,
                    carrier_bytes=0,
                    payload_bytes=0,
                    manifest_bytes=None,
                    projected_record_bytes=(
                        estimate_session_record_bytes(current_record)
                        if current_record is not None
                        else 0
                    ),
                    projected_comment_bytes=0,
                    future_reserve_bytes=reserve,
                    accounting_version=accounting_version,
                    v3_writer_enabled=v3_enabled,
                    pricing=effective_pricing,
                    recovery=_prescribe_terminal_recovery(
                        "disposition_token_bytes_exceeded", action_name=command.action
                    ),
                ),
            )
        if entry_count > MAX_COVERAGE_ENTRY_COUNT:
            return (
                None,
                None,
                TerminalRepresentabilityReport(
                    representable=False,
                    refusal_code="disposition_entry_cap_exceeded",
                    reason=(
                        f"incomplete_coverage has {entry_count} entries, "
                        f"exceeding {MAX_COVERAGE_ENTRY_COUNT}-entry cap"
                    ),
                    disposition_entry_count=entry_count,
                    max_disposition_token_bytes=max_token_bytes,
                    disposition_bytes=0,
                    envelope_bytes=0,
                    carrier_bytes=0,
                    payload_bytes=0,
                    manifest_bytes=None,
                    projected_record_bytes=(
                        estimate_session_record_bytes(current_record)
                        if current_record is not None
                        else 0
                    ),
                    projected_comment_bytes=0,
                    future_reserve_bytes=reserve,
                    accounting_version=accounting_version,
                    v3_writer_enabled=v3_enabled,
                    pricing=effective_pricing,
                    recovery=_prescribe_terminal_recovery(
                        "disposition_entry_cap_exceeded", action_name=command.action
                    ),
                ),
            )

    preview_producer = _resolve_preview_producer(
        current_record=current_record,
        source_ledger=source_ledger,
        binding=binding,
    )
    merged_preloaded_parts = _preload_snapshot_parts_from_ledger(
        source_ledger,
        identity,
        current_record,
        producer=preview_producer,
    )
    if preloaded_parts is not None:
        merged_preloaded_parts.update(preloaded_parts)
    source_page_cache = (
        getattr(source_ledger, "_disposition_page_cache", None)
        if source_ledger is not None
        else None
    )
    paged_writes_enabled = (
        supports_paged_disposition_snapshot_writes
        if supports_paged_disposition_snapshot_writes is not None
        else bool(
            getattr(source_ledger, "supports_paged_disposition_snapshot_writes", False)
        )
    )

    # Preserve current_record (including any authenticated disposition_snapshot)
    # directly on sandbox_record so already-recorded, verify, and snapshot-append
    # paths evaluate against the true authenticated decision inventory.
    sandbox_record = current_record
    sandbox_ledger = _InMemoryPreviewLedger(
        sandbox_record,
        producer=preview_producer,
        enable_disposition_snapshot_writes=enable_disposition_snapshot_writes,
        supports_paged_disposition_snapshot_writes=paged_writes_enabled,
        evidence_budget=evidence_budget,
        preloaded_parts=merged_preloaded_parts,
        preloaded_dispositions=preloaded_dispositions,
        disposition_page_cache=(
            source_page_cache if isinstance(source_page_cache, Mapping) else None
        ),
    )
    try:
        preview_record, preview_result = apply_session_command(
            cast(Any, sandbox_ledger),
            identity,
            command,
            finding_fingerprints=finding_fingerprints,
            finding_inventory=finding_inventory,
            prior_inventories=prior_inventories,
            actor_role=actor_role,
            policy_digest=policy_digest,
            provenance=provenance,
            incomplete_coverage=incomplete_coverage,
            now=now,
        )
    except ReviewInputError as exc:
        text = str(exc)
        structured_code = getattr(exc, "refusal_code", None)
        if (
            structured_code == "disposition_array_bytes_exceeded"
            or text in _DISPOSITION_ARRAY_OVERFLOW_MESSAGES
        ):
            return (
                None,
                None,
                TerminalRepresentabilityReport(
                    representable=False,
                    refusal_code="disposition_array_bytes_exceeded",
                    reason=text,
                    disposition_entry_count=entry_count,
                    max_disposition_token_bytes=max_token_bytes,
                    disposition_bytes=MAX_DISPOSITION_BYTES + 1,
                    envelope_bytes=0,
                    carrier_bytes=0,
                    payload_bytes=0,
                    manifest_bytes=None,
                    projected_record_bytes=(
                        estimate_session_record_bytes(current_record)
                        if current_record is not None
                        else 0
                    ),
                    projected_comment_bytes=0,
                    future_reserve_bytes=reserve,
                    accounting_version=accounting_version,
                    v3_writer_enabled=v3_enabled,
                    pricing=effective_pricing,
                    recovery=_prescribe_terminal_recovery(
                        "disposition_array_bytes_exceeded",
                        action_name=command.action,
                    ),
                ),
            )
        if (
            structured_code == "session_root_bytes_exceeded"
            or text in _SESSION_ROOT_OVERFLOW_MESSAGES
        ):
            return (
                None,
                None,
                TerminalRepresentabilityReport(
                    representable=False,
                    refusal_code="session_root_bytes_exceeded",
                    reason=text,
                    disposition_entry_count=entry_count,
                    max_disposition_token_bytes=max_token_bytes,
                    disposition_bytes=0,
                    envelope_bytes=0,
                    carrier_bytes=0,
                    payload_bytes=0,
                    manifest_bytes=None,
                    projected_record_bytes=MAX_SESSION_RECORD_BYTES + 1,
                    projected_comment_bytes=0,
                    future_reserve_bytes=reserve,
                    accounting_version=accounting_version,
                    v3_writer_enabled=v3_enabled,
                    pricing=effective_pricing,
                    recovery=_prescribe_terminal_recovery(
                        "session_root_bytes_exceeded", action_name=command.action
                    ),
                ),
            )
        if (
            current_record is not None
            and current_record.disposition_snapshot is not None
            and text
            in {
                "preview partition binding or manifest mismatch",
                "preview part is unavailable",
            }
        ):
            return (
                None,
                None,
                TerminalRepresentabilityReport(
                    representable=False,
                    refusal_code="snapshot_parts_unavailable_for_preview",
                    reason=(
                        "authenticated disposition_snapshot references external parts "
                        "not pre-loaded for in-memory preview; refusing conservatively"
                    ),
                    disposition_entry_count=entry_count,
                    max_disposition_token_bytes=max_token_bytes,
                    disposition_bytes=0,
                    envelope_bytes=0,
                    carrier_bytes=0,
                    payload_bytes=0,
                    manifest_bytes=None,
                    projected_record_bytes=estimate_session_record_bytes(
                        current_record
                    ),
                    projected_comment_bytes=0,
                    future_reserve_bytes=reserve,
                    accounting_version=accounting_version,
                    v3_writer_enabled=v3_enabled,
                    pricing=effective_pricing,
                    recovery=_prescribe_terminal_recovery(
                        "snapshot_parts_unavailable_for_preview",
                        action_name=command.action,
                    ),
                ),
            )
        raise

    if preview_result is None:
        raise ReviewInputError("preview session command returned no result")

    staged_part_count = len(sandbox_ledger._parts)
    if (
        staged_part_count > 0
        and effective_pricing.part_write_and_readback_requests == 0
    ):
        effective_pricing = replace(
            effective_pricing,
            part_write_and_readback_requests=staged_part_count * 2,
        )

    resolved_dispositions: Sequence[Mapping[str, object]] | None = None
    if preview_record is not None:
        if preview_record.disposition_snapshot is not None:
            try:
                resolved_dispositions = sandbox_ledger.read_disposition_snapshot(
                    identity, preview_record
                )
            except ReviewInputError as exc:
                # Commands that do not read or change dispositions can price the
                # retained root without hydration. Never hide authentication or
                # binding errors, or unavailable data for a snapshot mutation.
                if str(exc) != "preview part is unavailable" or command.action in {
                    "accept-risk",
                    "dismiss",
                    "defer",
                    "accept-coverage-uncertainty",
                    "verify",
                }:
                    raise
        else:
            resolved_dispositions = preview_record.dispositions

    outcome = (
        {"kind": "applied", "reason": None}
        if preview_result.applied
        else {
            "kind": "explicit_noop",
            "reason": "status" if command.action == "status" else "no_change",
        }
    )
    report = evaluate_terminal_action_representability(
        identity=identity,
        intent_sha256=intent_sha256,
        outcome=outcome,
        result_document=asdict(preview_result),
        binding=binding,
        current_record=preview_record,
        accounting_version=accounting_version,
        pricing=effective_pricing,
        future_reserve_bytes=reserve,
        consumed_grants=consumed_grants,
        allow_v3_writer=v3_enabled,
        disposition_entry_count=entry_count,
        max_disposition_token_bytes=max_token_bytes,
        action_name=command.action,
        resolved_dispositions=resolved_dispositions,
        intermediate_record=current_record,
    )
    return preview_result, preview_record, report


def require_terminal_action_representable(
    report: TerminalRepresentabilityReport,
) -> TerminalRepresentabilityReport:
    """Raise ``TerminalRepresentabilityError`` when ``report`` is not representable."""

    if not report.representable:
        raise TerminalRepresentabilityError(report)
    return report


def action_intent_sha256(intent: Mapping[str, object]) -> str:
    """Compute the canonical SHA-256 digest of a caller action intent document."""

    return hashlib.sha256(canonical_bytes(dict(intent))).hexdigest()


def accounting_version_from_record(record: SessionRecord | None) -> str:
    """Extract the active caller accounting schema version from ``SessionRecord``."""

    if record is None or record.api_run_accounting is None:
        return ACCOUNTING_VERSION
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    return state.version


def with_terminal_action_manifest(
    accounting: ApiRunAccounting,
    *,
    manifest: Mapping[str, object],
    outcome: Mapping[str, object],
) -> ApiRunAccounting:
    """Project terminal ``action`` state onto an ``ApiRunAccounting`` snapshot."""

    if accounting.action is None:
        raise ReviewInputError("caller accounting has no admitted action")
    return replace(
        accounting,
        action={
            **accounting.action,
            "execution_started": True,
            "phase": "terminal",
            "result": dict(manifest),
            "outcome": dict(outcome),
        },
    )


__all__ = [
    "ACTION_RESULT_ENVELOPE_VERSION",
    "ActionSettlementPricing",
    "MAX_COVERAGE_ENTRY_COUNT",
    "MAX_COVERAGE_TOKEN_BYTES",
    "SUPPORTED_ACTION_WRITER_VERSIONS",
    "TERMINAL_REFUSAL_CODES",
    "TerminalRepresentabilityError",
    "TerminalRepresentabilityReport",
    "accounting_version_from_record",
    "action_intent_sha256",
    "build_action_result_binding",
    "estimate_session_comment_bytes",
    "estimate_session_record_bytes",
    "evaluate_terminal_action_representability",
    "format_terminal_refusal_summary",
    "preview_session_command_representability",
    "require_terminal_action_representable",
    "with_terminal_action_manifest",
]
