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
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
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
    MAX_PARTITION_DECODED_BYTES,
    AuthenticatedPart,
    canonical_bytes,
    encode_evidence,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
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
    }
)

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

    def __post_init__(self) -> None:
        for attr in (
            "cold_census_requests",
            "business_mutation_requests",
            "refinalization_requests",
            "receipt_publication_requests",
            "part_write_and_readback_requests",
            "terminal_close_requests",
            "committed_requests",
        ):
            object.__setattr__(
                self, attr, _non_negative_int(getattr(self, attr), field=attr)
            )
        limit = _non_negative_int(self.original_api_limit, field="original_api_limit")
        if limit <= 0:
            raise ReviewInputError("original_api_limit must be positive")
        object.__setattr__(self, "original_api_limit", limit)

    @property
    def required_requests(self) -> int:
        return (
            self.cold_census_requests
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
        return self.total_projected_requests <= self.original_api_limit

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "cold_census_requests": self.cold_census_requests,
            "business_mutation_requests": self.business_mutation_requests,
            "refinalization_requests": self.refinalization_requests,
            "receipt_publication_requests": self.receipt_publication_requests,
            "part_write_and_readback_requests": self.part_write_and_readback_requests,
            "terminal_close_requests": self.terminal_close_requests,
            "committed_requests": self.committed_requests,
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
    return RecoveryPrescription(
        action="supply_original_context_or_rescan",
        command="supply-original-recovery-context",
        requires_fresh_inference=False,
        steps=(
            "Do not pass read-only action result or ancestry observations as writer or settlement authority.",
            "Load the authenticated current SessionRecord from the owning session ledger before evaluating settlement.",
        ),
    )


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
    disp_bytes = (
        disposition_array_bytes(record_obj.dispositions)
        if record_obj is not None
        else 0
    )

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
        return TerminalRepresentabilityReport(
            representable=False,
            refusal_code="request_budget_exhausted_before_effect",
            reason=(
                f"projected physical requests {effective_pricing.total_projected_requests} "
                f"exceed original limit {effective_pricing.original_api_limit}"
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
    if record_obj is not None:
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
        if projected_record_bytes > MAX_SESSION_RECORD_BYTES:
            return TerminalRepresentabilityReport(
                representable=False,
                refusal_code="session_root_bytes_exceeded",
                reason=(
                    f"projected SessionRecord size {projected_record_bytes} bytes "
                    f"(record={raw_record_bytes}B + reserve={reserve}B) "
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
            )
        evolved = record_obj.evolve(
            generation=record_obj.generation + 1,
            api_run_accounting=cast(
                Mapping[str, object] | None, raw_payload.get("api_run_accounting")
            ),
        )
        repo_id = identity.repository_id or evolved.repository_id or 1
        raw_comment_bytes = estimate_session_comment_bytes(
            evolved,
            repository_id=repo_id,
            pull_request=identity.pull_request,
            consumed_grants=consumed_grants,
        )
        projected_comment_bytes = raw_comment_bytes + reserve
        if projected_comment_bytes > MAX_SESSION_COMMENT_BYTES:
            return TerminalRepresentabilityReport(
                representable=False,
                refusal_code="session_root_bytes_exceeded",
                reason=(
                    f"projected session comment size {projected_comment_bytes} bytes "
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
    )


class _InMemoryPreviewLedger:
    """Pure in-memory sandbox ledger that never constructs a budget or touches I/O."""

    supports_paged_disposition_snapshot_writes = False

    def __init__(
        self,
        record: SessionRecord | None,
        *,
        enable_disposition_snapshot_writes: bool = False,
        evidence_budget: Any = None,
    ) -> None:
        self._record = record
        self.enable_disposition_snapshot_writes = enable_disposition_snapshot_writes
        self.evidence_budget = evidence_budget
        self._parts: dict[str, tuple[object, str]] = {}
        self._staged_documents: dict[str, tuple[dict[str, object], object]] = {}

    def evidence_producer(self) -> str:
        return "local-ledger"

    def _write_part(
        self, identity: SessionIdentity, part_id: str, payload: object
    ) -> str:
        self._parts[part_id] = (copy.deepcopy(payload), self.evidence_producer())
        return part_id

    def _read_part(
        self, identity: SessionIdentity, storage_id: str
    ) -> AuthenticatedPart:
        if storage_id not in self._parts:
            raise ReviewInputError("preview part is unavailable")
        doc, prod = self._parts[storage_id]
        return AuthenticatedPart(copy.deepcopy(doc), prod)

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
        if staged is None or staged[0] != dict(expected_binding):
            raise ReviewInputError("preview partition binding or manifest mismatch")
        return copy.deepcopy(staged[1])

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
    evidence_budget: Any = None,
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
    network requests. Early disposition token/cap/array overflows are classified
    into structured ``TerminalRepresentabilityReport`` refusals.
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

    existing_snapshot_bytes = (
        len(
            canonical_bytes(
                {"disposition_snapshot": current_record.disposition_snapshot}
            )
        )
        if current_record is not None
        and current_record.disposition_snapshot is not None
        else 0
    )
    sandbox_record = (
        current_record.evolve(disposition_snapshot=None)
        if existing_snapshot_bytes > 0 and current_record is not None
        else current_record
    )
    sandbox_ledger = _InMemoryPreviewLedger(
        sandbox_record,
        enable_disposition_snapshot_writes=enable_disposition_snapshot_writes,
        evidence_budget=evidence_budget,
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

    outcome = (
        {"kind": "applied", "reason": None}
        if preview_result.applied
        else {
            "kind": "explicit_noop",
            "reason": "status" if command.action == "status" else "no_change",
        }
    )
    # Strip existing snapshot during dry-run only to avoid reading remote parts;
    # re-add its root footprint only when the command neither staged a replacement
    # snapshot (preview_record.disposition_snapshot is not None) nor cleared the
    # ledger via reenrollment.
    effective_reserve = (
        reserve + existing_snapshot_bytes
        if existing_snapshot_bytes > 0
        and command.action != "reenroll"
        and preview_record is not None
        and preview_record.disposition_snapshot is None
        else reserve
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
        future_reserve_bytes=effective_reserve,
        consumed_grants=consumed_grants,
        allow_v3_writer=v3_enabled,
        disposition_entry_count=entry_count,
        max_disposition_token_bytes=max_token_bytes,
        action_name=command.action,
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
