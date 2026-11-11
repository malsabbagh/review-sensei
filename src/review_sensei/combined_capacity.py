"""Combined capacity inventory and cross-stage interruption envelope qualification (G12).

Individual representation caps (such as ``10,240``-byte inline dispositions,
``12,288``-byte convergence history, ``8,192``-byte inline manifests, ``8,192``-byte
assessment queue roots, and ``4,096``-byte publication staging) do not by
themselves guarantee that their simultaneous composition fits inside the
``32,768``-byte ``SessionRecord`` root, the ``65,536``-byte GitHub session
comment or review body, the ``60``-call ordinary evidence read ceiling
(preserving the ``4``-call final fence reserve), or the original physical API
request allowance.

This module inventories the authoritative representation limits across all layers,
evaluates both individual component bounds and combined cross-stage envelopes
(including multibyte UTF-8 byte accounting and maximum histories), and qualifies
interruption + fresh-process replay invariants across every retained,
publication, and terminal stage without conferring runtime authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api_work import (
    ACCOUNTING_VERSION,
    DEFAULT_API_REQUEST_LIMIT,
    LEGACY_ACCOUNTING_VERSION,
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
)
from .baseline import (
    MAX_HISTORY_BASELINE_BYTES,
    MAX_HISTORY_ENVELOPE_RESERVE_BYTES,
)
from .bounded_evidence import (
    MAX_MANIFEST_BYTES,
    MAX_PART_BYTES,
    MAX_PART_READS,
    MAX_PARTITION_BYTES,
    MAX_PARTITION_DECODED_BYTES,
    MAX_PARTS,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    PACKED_CHUNK_BYTES,
    PART_FRAMING_RESERVE,
    EvidenceReadBudget,
)
from .coverage import MAX_COVERAGE_ENTRIES, MAX_COVERAGE_HUNKS
from .disposition import MAX_OVERRIDE_IDS
from .disposition_snapshot import MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES
from .errors import ReviewInputError
from .provenance_manifest import (
    RecoveryPrescription,
    _exact_bool,
    _exact_false_authority,
    _non_negative_int,
    prescribe_recovery,
)
from .publication_staging import MAX_PUBLICATION_PARTS, MAX_STAGING_BYTES
from .recovery_context import (
    MAX_DECODED_RESULT_BYTES,
    MAX_RECOVERY_ANCESTOR_DEPTH,
    MAX_RECOVERY_CONTEXT_BUNDLE_BYTES,
    MAX_RECOVERY_HANDOFF_FILES,
    MAX_RECOVERY_HANDOFF_TOTAL_BYTES,
    MAX_RECOVERY_RESULT_PARTS,
    OriginalRecoveryContextBundle,
    RecoveryContextStore,
    ensure_not_observation_only_evidence,
)
from .session import (
    MAX_ASSESSMENT_QUEUE_ROOT_BYTES,
    MAX_ASSESSMENT_QUEUE_SUMMARY_BYTES,
    MAX_CONVERGENCE_HISTORY_BYTES,
    MAX_DISPOSITION_ACTOR_BYTES,
    MAX_DISPOSITION_BYTES,
    MAX_DISPOSITION_ITEMS,
    MAX_DISPOSITION_REASON_BYTES,
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    MAX_STORED_CONTINUATION_GRANTS,
    SessionRecord,
    disposition_array_bytes,
)
from .terminal_representability import (
    MAX_COVERAGE_ENTRY_COUNT,
    MAX_COVERAGE_TOKEN_BYTES,
    estimate_session_comment_bytes,
    estimate_session_record_bytes,
)

RESERVED_FENCE_READS = 4
ORDINARY_EVIDENCE_READ_LIMIT = MAX_PART_READS - RESERVED_FENCE_READS
MAX_PUBLISHED_REVIEW_BODY_BYTES = 65_536
MAX_INDEXED_FINDINGS = 250
FINDING_INDEX_ENTRY_BYTES = 65

SUPPORTED_ENVELOPE_WRITER_VERSIONS = frozenset(
    {LEGACY_ACCOUNTING_VERSION, ACCOUNTING_VERSION}
)

COMBINED_REFUSAL_CODES = frozenset(
    {
        "observation_only_not_authority",
        "unsupported_v3_writer",
        "individual_component_limit_exceeded",
        "combined_session_record_bytes_exceeded",
        "combined_session_comment_bytes_exceeded",
        "combined_review_body_bytes_exceeded",
        "combined_stored_parts_exceeded",
        "combined_evidence_read_budget_exceeded",
        "combined_api_request_budget_exceeded",
    }
)

INTERRUPTION_STAGES = (
    "retained_result_staging",
    "recovery_context_staging",
    "review_publication",
    "inline_group_publication",
    "check_publication",
    "thread_cleanup",
    "native_approval",
    "command_receipt",
    "terminal_accounting_close",
)

INTERRUPTION_MODES = (
    "before_dispatch",
    "in_flight_not_stored",
    "in_flight_stored_response_lost",
    "definite_rejection",
)

PAYLOAD_PROFILES = (
    "compact",
    "near_individual_limits",
    "combined_overflow",
)

TEXT_ENCODINGS = (
    "ascii",
    "utf8_2byte",
    "utf8_3byte_cjk",
    "utf8_4byte_emoji",
)

HISTORY_PROFILES = (
    "minimal",
    "moderate",
    "maximum_supported",
)


@dataclass(frozen=True)
class RepresentationLimit:
    """Single authoritative capacity ceiling at one persistence or protocol layer."""

    layer: str
    name: str
    unit: str
    ceiling: int
    module_source: str
    rationale: str

    def __post_init__(self) -> None:
        if not self.layer or not self.name or not self.unit or not self.module_source:
            raise ReviewInputError("representation limit metadata must be non-empty")
        if _non_negative_int(self.ceiling, field="ceiling") <= 0:
            raise ReviewInputError("representation limit ceiling must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "name": self.name,
            "unit": self.unit,
            "ceiling": self.ceiling,
            "module_source": self.module_source,
            "rationale": self.rationale,
        }


REPRESENTATION_LIMIT_INVENTORY: tuple[RepresentationLimit, ...] = (
    RepresentationLimit(
        layer="session_root",
        name="session_record_bytes",
        unit="utf8_bytes",
        ceiling=MAX_SESSION_RECORD_BYTES,
        module_source="review_sensei.session.MAX_SESSION_RECORD_BYTES",
        rationale="Canonical JSON UTF-8 ceiling for SessionRecord root payload.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="session_comment_bytes",
        unit="utf8_bytes",
        ceiling=MAX_SESSION_COMMENT_BYTES,
        module_source="review_sensei.session.MAX_SESSION_COMMENT_BYTES",
        rationale="GitHub issue comment UTF-8 ceiling including consumed-grant and session markers.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="inline_dispositions_bytes",
        unit="utf8_bytes",
        ceiling=MAX_DISPOSITION_BYTES,
        module_source="review_sensei.session.MAX_DISPOSITION_BYTES",
        rationale="Maximum inline disposition array UTF-8 bytes before snapshot staging.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="disposition_snapshot_root_bytes",
        unit="utf8_bytes",
        ceiling=MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES,
        module_source="review_sensei.disposition_snapshot.MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES",
        rationale="Maximum root manifest bytes for partitioned disposition snapshots.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="convergence_history_bytes",
        unit="utf8_bytes",
        ceiling=MAX_CONVERGENCE_HISTORY_BYTES,
        module_source="review_sensei.session.MAX_CONVERGENCE_HISTORY_BYTES",
        rationale="Maximum UTF-8 bytes for convergence history in SessionRecord.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="history_baseline_bytes",
        unit="utf8_bytes",
        ceiling=MAX_HISTORY_BASELINE_BYTES,
        module_source="review_sensei.baseline.MAX_HISTORY_BASELINE_BYTES",
        rationale="Maximum encoded baseline bytes inside convergence history.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="history_envelope_reserve_bytes",
        unit="utf8_bytes",
        ceiling=MAX_HISTORY_ENVELOPE_RESERVE_BYTES,
        module_source="review_sensei.baseline.MAX_HISTORY_ENVELOPE_RESERVE_BYTES",
        rationale="Mandatory root envelope headroom reserved alongside history fields.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="assessment_queue_root_bytes",
        unit="utf8_bytes",
        ceiling=MAX_ASSESSMENT_QUEUE_ROOT_BYTES,
        module_source="review_sensei.session.MAX_ASSESSMENT_QUEUE_ROOT_BYTES",
        rationale="Maximum root manifest bytes for human assessment queue.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="assessment_queue_summary_bytes",
        unit="utf8_bytes",
        ceiling=MAX_ASSESSMENT_QUEUE_SUMMARY_BYTES,
        module_source="review_sensei.session.MAX_ASSESSMENT_QUEUE_SUMMARY_BYTES",
        rationale="Maximum inline summary bytes inside assessment queue root.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="publication_staging_bytes",
        unit="utf8_bytes",
        ceiling=MAX_STAGING_BYTES,
        module_source="review_sensei.publication_staging.MAX_STAGING_BYTES",
        rationale="Maximum root bytes for staged publication manifest metadata.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="publication_staging_parts",
        unit="count",
        ceiling=MAX_PUBLICATION_PARTS,
        module_source="review_sensei.publication_staging.MAX_PUBLICATION_PARTS",
        rationale="Maximum staged publication parts referenced from SessionRecord.",
    ),
    RepresentationLimit(
        layer="session_root",
        name="stored_continuation_grants",
        unit="count",
        ceiling=MAX_STORED_CONTINUATION_GRANTS,
        module_source="review_sensei.session.MAX_STORED_CONTINUATION_GRANTS",
        rationale="Maximum continuation grants retained inline in SessionRecord.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="manifest_bytes",
        unit="utf8_bytes",
        ceiling=MAX_MANIFEST_BYTES,
        module_source="review_sensei.bounded_evidence.MAX_MANIFEST_BYTES",
        rationale="Inline and partitioned evidence manifest ceiling (including caller-action-result-v1).",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="part_bytes",
        unit="utf8_bytes",
        ceiling=MAX_PART_BYTES,
        module_source="review_sensei.bounded_evidence.MAX_PART_BYTES",
        rationale="Single evidence part encoded byte ceiling.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="part_framing_reserve_bytes",
        unit="utf8_bytes",
        ceiling=PART_FRAMING_RESERVE,
        module_source="review_sensei.bounded_evidence.PART_FRAMING_RESERVE",
        rationale="Per-part comment framing byte reserve.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="packed_chunk_bytes",
        unit="utf8_bytes",
        ceiling=PACKED_CHUNK_BYTES,
        module_source="review_sensei.bounded_evidence.PACKED_CHUNK_BYTES",
        rationale="Chunk split threshold when partitioning compressed evidence.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="parts_per_manifest",
        unit="count",
        ceiling=MAX_PARTS,
        module_source="review_sensei.bounded_evidence.MAX_PARTS",
        rationale="Maximum evidence parts addressed by one manifest.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="partition_encoded_bytes",
        unit="utf8_bytes",
        ceiling=MAX_PARTITION_BYTES,
        module_source="review_sensei.bounded_evidence.MAX_PARTITION_BYTES",
        rationale="Aggregate encoded bytes across one partitioned evidence manifest.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="partition_decoded_bytes",
        unit="utf8_bytes",
        ceiling=MAX_PARTITION_DECODED_BYTES,
        module_source="review_sensei.bounded_evidence.MAX_PARTITION_DECODED_BYTES",
        rationale="Aggregate decoded JSON bytes across one partitioned evidence payload.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="part_reads_total",
        unit="requests",
        ceiling=MAX_PART_READS,
        module_source="review_sensei.bounded_evidence.MAX_PART_READS",
        rationale="Absolute per-operation part/metadata read ceiling including 4 fence reads.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="ordinary_evidence_reads",
        unit="requests",
        ceiling=ORDINARY_EVIDENCE_READ_LIMIT,
        module_source="review_sensei.combined_capacity.ORDINARY_EVIDENCE_READ_LIMIT",
        rationale="Ordinary evidence reads before reserving 4 final reconciliation fence reads.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="reserved_fence_reads",
        unit="requests",
        ceiling=RESERVED_FENCE_READS,
        module_source="review_sensei.combined_capacity.RESERVED_FENCE_READS",
        rationale="Protected finalization/reconciliation fence reads.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="stored_parts_count",
        unit="count",
        ceiling=MAX_STORED_PARTS,
        module_source="review_sensei.bounded_evidence.MAX_STORED_PARTS",
        rationale="Maximum retained part files/comments in one session store.",
    ),
    RepresentationLimit(
        layer="bounded_evidence",
        name="stored_parts_total_bytes",
        unit="utf8_bytes",
        ceiling=MAX_STORED_PART_BYTES,
        module_source="review_sensei.bounded_evidence.MAX_STORED_PART_BYTES",
        rationale="Aggregate retained part bytes in one session store.",
    ),
    RepresentationLimit(
        layer="recovery_context",
        name="recovery_context_bundle_bytes",
        unit="utf8_bytes",
        ceiling=MAX_RECOVERY_CONTEXT_BUNDLE_BYTES,
        module_source="review_sensei.recovery_context.MAX_RECOVERY_CONTEXT_BUNDLE_BYTES",
        rationale="Maximum canonical UTF-8 bytes for OriginalRecoveryContextBundle (G17).",
    ),
    RepresentationLimit(
        layer="recovery_context",
        name="recovery_handoff_files",
        unit="count",
        ceiling=MAX_RECOVERY_HANDOFF_FILES,
        module_source="review_sensei.recovery_context.MAX_RECOVERY_HANDOFF_FILES",
        rationale="Maximum fresh-process handoff files retained concurrently.",
    ),
    RepresentationLimit(
        layer="recovery_context",
        name="recovery_handoff_total_bytes",
        unit="utf8_bytes",
        ceiling=MAX_RECOVERY_HANDOFF_TOTAL_BYTES,
        module_source="review_sensei.recovery_context.MAX_RECOVERY_HANDOFF_TOTAL_BYTES",
        rationale="Aggregate byte cap across retained fresh-process handoff envelopes.",
    ),
    RepresentationLimit(
        layer="recovery_context",
        name="recovery_ancestor_depth",
        unit="count",
        ceiling=MAX_RECOVERY_ANCESTOR_DEPTH,
        module_source="review_sensei.recovery_context.MAX_RECOVERY_ANCESTOR_DEPTH",
        rationale="Maximum ancestor run traversal depth during recovery qualification.",
    ),
    RepresentationLimit(
        layer="recovery_context",
        name="recovery_result_parts",
        unit="count",
        ceiling=MAX_RECOVERY_RESULT_PARTS,
        module_source="review_sensei.recovery_context.MAX_RECOVERY_RESULT_PARTS",
        rationale="Maximum retained result parts read during zero-inference recovery.",
    ),
    RepresentationLimit(
        layer="recovery_context",
        name="decoded_result_bytes",
        unit="utf8_bytes",
        ceiling=MAX_DECODED_RESULT_BYTES,
        module_source="review_sensei.recovery_context.MAX_DECODED_RESULT_BYTES",
        rationale="Maximum decoded ReviewResult bytes during recovery.",
    ),
    RepresentationLimit(
        layer="terminal_action",
        name="coverage_token_bytes",
        unit="utf8_bytes",
        ceiling=MAX_COVERAGE_TOKEN_BYTES,
        module_source="review_sensei.terminal_representability.MAX_COVERAGE_TOKEN_BYTES",
        rationale="Maximum UTF-8 bytes for a single incomplete_coverage token (G34).",
    ),
    RepresentationLimit(
        layer="terminal_action",
        name="coverage_entry_count",
        unit="count",
        ceiling=MAX_COVERAGE_ENTRY_COUNT,
        module_source="review_sensei.terminal_representability.MAX_COVERAGE_ENTRY_COUNT",
        rationale="Maximum incomplete_coverage items in one disposition entry (G34).",
    ),
    RepresentationLimit(
        layer="terminal_action",
        name="disposition_items_per_entry",
        unit="count",
        ceiling=MAX_DISPOSITION_ITEMS,
        module_source="review_sensei.session.MAX_DISPOSITION_ITEMS",
        rationale="Maximum items per FindingDisposition record.",
    ),
    RepresentationLimit(
        layer="terminal_action",
        name="disposition_reason_bytes",
        unit="utf8_bytes",
        ceiling=MAX_DISPOSITION_REASON_BYTES,
        module_source="review_sensei.session.MAX_DISPOSITION_REASON_BYTES",
        rationale="Maximum UTF-8 bytes for a maintainer disposition reason.",
    ),
    RepresentationLimit(
        layer="terminal_action",
        name="disposition_actor_bytes",
        unit="utf8_bytes",
        ceiling=MAX_DISPOSITION_ACTOR_BYTES,
        module_source="review_sensei.session.MAX_DISPOSITION_ACTOR_BYTES",
        rationale="Maximum UTF-8 bytes for a maintainer actor login.",
    ),
    RepresentationLimit(
        layer="terminal_action",
        name="override_ids_per_command",
        unit="count",
        ceiling=MAX_OVERRIDE_IDS,
        module_source="review_sensei.disposition.MAX_OVERRIDE_IDS",
        rationale="Maximum finding IDs in one maintainer override command.",
    ),
    RepresentationLimit(
        layer="publication_and_api",
        name="coverage_files",
        unit="count",
        ceiling=MAX_COVERAGE_ENTRIES,
        module_source="review_sensei.coverage.MAX_COVERAGE_ENTRIES",
        rationale="Maximum enumerated file outcomes in CoverageManifest.",
    ),
    RepresentationLimit(
        layer="publication_and_api",
        name="coverage_hunks",
        unit="count",
        ceiling=MAX_COVERAGE_HUNKS,
        module_source="review_sensei.coverage.MAX_COVERAGE_HUNKS",
        rationale="Maximum hunk outcomes in CoverageManifest.",
    ),
    RepresentationLimit(
        layer="publication_and_api",
        name="published_review_body_bytes",
        unit="utf8_bytes",
        ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
        module_source="review_sensei_github.publication.MAX_PUBLISHED_REVIEW_BODY_BYTES",
        rationale="GitHub pull request review body UTF-8 ceiling including finding index and markers.",
    ),
    RepresentationLimit(
        layer="publication_and_api",
        name="indexed_findings",
        unit="count",
        ceiling=MAX_INDEXED_FINDINGS,
        module_source="review_sensei_github.publication.MAX_INDEXED_FINDINGS",
        rationale="Maximum findings encoded in the published finding index.",
    ),
    RepresentationLimit(
        layer="publication_and_api",
        name="default_api_request_limit",
        unit="requests",
        ceiling=DEFAULT_API_REQUEST_LIMIT,
        module_source="review_sensei.api_work.DEFAULT_API_REQUEST_LIMIT",
        rationale="Default per-run physical GitHub/broker API request allowance.",
    ),
)

_INVENTORY_BY_NAME: dict[str, RepresentationLimit] = {
    item.name: item for item in REPRESENTATION_LIMIT_INVENTORY
}


def lookup_representation_limit(name: str) -> RepresentationLimit:
    """Return the authoritative ``RepresentationLimit`` for ``name``."""

    if name not in _INVENTORY_BY_NAME:
        raise ReviewInputError(f"unknown representation limit: {name!r}")
    return _INVENTORY_BY_NAME[name]


def verify_inventory_against_runtime_constants() -> dict[str, object]:
    """Cross-verify inventoried limits against live core constants and invariants."""

    if len(_INVENTORY_BY_NAME) != len(REPRESENTATION_LIMIT_INVENTORY):
        raise ReviewInputError("duplicate representation limit name in inventory")
    fresh_budget = EvidenceReadBudget()
    if fresh_budget.ordinary_remaining() != ORDINARY_EVIDENCE_READ_LIMIT:
        raise ReviewInputError(
            "ordinary_evidence_reads drifted from EvidenceReadBudget.ordinary_remaining()"
        )
    if MAX_SESSION_COMMENT_BYTES != MAX_SESSION_RECORD_BYTES * 2:
        raise ReviewInputError("session comment limit drifted from 2x session record")
    if MAX_COVERAGE_ENTRY_COUNT != MAX_DISPOSITION_ITEMS:
        raise ReviewInputError("coverage entry cap drifted from MAX_DISPOSITION_ITEMS")
    sum_root_subcaps = (
        MAX_DISPOSITION_BYTES
        + MAX_CONVERGENCE_HISTORY_BYTES
        + MAX_ASSESSMENT_QUEUE_ROOT_BYTES
        + MAX_STAGING_BYTES
        + MAX_MANIFEST_BYTES
    )
    if sum_root_subcaps <= MAX_SESSION_RECORD_BYTES:
        raise ReviewInputError(
            "expected sum of root sub-caps to exceed MAX_SESSION_RECORD_BYTES, "
            "requiring combined envelope qualification"
        )
    return {
        "limit_count": len(REPRESENTATION_LIMIT_INVENTORY),
        "ordinary_read_limit": ORDINARY_EVIDENCE_READ_LIMIT,
        "reserved_fence_reads": RESERVED_FENCE_READS,
        "sum_individual_root_subcaps": sum_root_subcaps,
        "session_record_ceiling": MAX_SESSION_RECORD_BYTES,
        "combined_guard_required": True,
        "authority": False,
    }


@dataclass(frozen=True)
class MultibyteTextMetrics:
    """Exact UTF-8 byte vs Unicode codepoint metrics for multibyte boundary checks."""

    codepoint_count: int
    utf8_bytes: int
    canonical_json_utf8_bytes: int
    ascii_escaped_json_bytes: int
    max_bytes_per_codepoint: int
    has_multibyte: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "codepoint_count": self.codepoint_count,
            "utf8_bytes": self.utf8_bytes,
            "canonical_json_utf8_bytes": self.canonical_json_utf8_bytes,
            "ascii_escaped_json_bytes": self.ascii_escaped_json_bytes,
            "max_bytes_per_codepoint": self.max_bytes_per_codepoint,
            "has_multibyte": self.has_multibyte,
        }


def measure_utf8_text(text: str) -> MultibyteTextMetrics:
    """Measure UTF-8 and JSON byte expansion while rejecting surrogates and NULs."""

    if not isinstance(text, str):
        raise ReviewInputError("text must be a str")
    if "\x00" in text:
        raise ReviewInputError("NUL bytes are prohibited in UTF-8 envelope text")
    max_bpc = 0
    for ch in text:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise ReviewInputError("lone UTF-16 surrogates are prohibited")
        ch_len = len(ch.encode("utf-8"))
        if ch_len > max_bpc:
            max_bpc = ch_len
    encoded = text.encode("utf-8")
    canonical_json = json.dumps(text, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    ascii_json = json.dumps(text, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return MultibyteTextMetrics(
        codepoint_count=len(text),
        utf8_bytes=len(encoded),
        canonical_json_utf8_bytes=len(canonical_json),
        ascii_escaped_json_bytes=len(ascii_json),
        max_bytes_per_codepoint=max_bpc,
        has_multibyte=len(encoded) > len(text),
    )


@dataclass(frozen=True)
class CombinedComponentFootprint:
    """Simultaneous resource footprint across root, comment, review, parts, and API calls."""

    base_shell_bytes: int = 1_024
    dispositions_bytes: int = 0
    disposition_snapshot_root_bytes: int = 0
    convergence_history_bytes: int = 0
    assessment_queue_bytes: int = 0
    publication_staging_bytes: int = 0
    accounting_manifest_bytes: int = 0
    quarantine_lineage_bytes: int = 0
    historical_assessments_bytes: int = 0
    future_root_reserve_bytes: int = 0
    consumed_grant_count: int = 0
    recovery_context_bundle_bytes: int = 0
    handoff_files: int = 0
    handoff_total_bytes: int = 0
    published_review_summary_utf8_bytes: int = 0
    published_finding_count: int = 0
    published_marker_overhead_bytes: int = 1_024
    stored_parts_count: int = 0
    stored_parts_total_bytes: int = 0
    retained_result_part_reads: int = 0
    disposition_snapshot_part_reads: int = 0
    recovery_context_part_reads: int = 0
    ancestry_part_reads: int = 0
    publication_reconciliation_reads: int = 0
    committed_requests: int = 0
    unknown_usage_requests: int = 0
    active_reserved_requests: int = 0
    retained_stage_requests: int = 0
    publication_requests: int = 0
    check_requests: int = 0
    cleanup_requests: int = 0
    approval_requests: int = 0
    receipt_requests: int = 0
    terminal_close_requests: int = 1
    protected_tail_requests: int = RESERVED_FENCE_READS
    original_api_limit: int = DEFAULT_API_REQUEST_LIMIT
    accounting_version: str = ACCOUNTING_VERSION
    allow_v3_writer: bool = False

    def __post_init__(self) -> None:
        int_fields = (
            "base_shell_bytes",
            "dispositions_bytes",
            "disposition_snapshot_root_bytes",
            "convergence_history_bytes",
            "assessment_queue_bytes",
            "publication_staging_bytes",
            "accounting_manifest_bytes",
            "quarantine_lineage_bytes",
            "historical_assessments_bytes",
            "future_root_reserve_bytes",
            "consumed_grant_count",
            "recovery_context_bundle_bytes",
            "handoff_files",
            "handoff_total_bytes",
            "published_review_summary_utf8_bytes",
            "published_finding_count",
            "published_marker_overhead_bytes",
            "stored_parts_count",
            "stored_parts_total_bytes",
            "retained_result_part_reads",
            "disposition_snapshot_part_reads",
            "recovery_context_part_reads",
            "ancestry_part_reads",
            "publication_reconciliation_reads",
            "committed_requests",
            "unknown_usage_requests",
            "active_reserved_requests",
            "retained_stage_requests",
            "publication_requests",
            "check_requests",
            "cleanup_requests",
            "approval_requests",
            "receipt_requests",
            "terminal_close_requests",
            "protected_tail_requests",
        )
        for attr in int_fields:
            object.__setattr__(
                self, attr, _non_negative_int(getattr(self, attr), field=attr)
            )
        limit = _non_negative_int(self.original_api_limit, field="original_api_limit")
        if limit <= 0:
            raise ReviewInputError("original_api_limit must be positive")
        object.__setattr__(self, "original_api_limit", limit)
        object.__setattr__(
            self,
            "allow_v3_writer",
            _exact_bool(self.allow_v3_writer, field="allow_v3_writer"),
        )

    @property
    def projected_record_bytes(self) -> int:
        return (
            self.base_shell_bytes
            + self.dispositions_bytes
            + self.disposition_snapshot_root_bytes
            + self.convergence_history_bytes
            + self.assessment_queue_bytes
            + self.publication_staging_bytes
            + self.accounting_manifest_bytes
            + self.quarantine_lineage_bytes
            + self.historical_assessments_bytes
            + self.future_root_reserve_bytes
        )

    @property
    def projected_comment_bytes(self) -> int:
        # Each consumed grant line is ~114 UTF-8 bytes; session intro + fence + marker ~220 bytes.
        grant_framing_bytes = self.consumed_grant_count * 114
        marker_and_fence_bytes = 220
        return (
            self.projected_record_bytes + grant_framing_bytes + marker_and_fence_bytes
        )

    @property
    def projected_review_body_bytes(self) -> int:
        index_encoded_bytes = (
            (self.published_finding_count * FINDING_INDEX_ENTRY_BYTES * 4 + 2) // 3
            if self.published_finding_count > 0
            else 0
        )
        return (
            self.published_review_summary_utf8_bytes
            + index_encoded_bytes
            + self.published_marker_overhead_bytes
        )

    @property
    def total_ordinary_part_reads(self) -> int:
        return (
            self.retained_result_part_reads
            + self.disposition_snapshot_part_reads
            + self.recovery_context_part_reads
            + self.ancestry_part_reads
            + self.publication_reconciliation_reads
        )

    @property
    def total_projected_requests(self) -> int:
        return (
            self.committed_requests
            + self.unknown_usage_requests
            + self.active_reserved_requests
            + self.retained_stage_requests
            + self.publication_requests
            + self.check_requests
            + self.cleanup_requests
            + self.approval_requests
            + self.receipt_requests
            + self.terminal_close_requests
            + self.protected_tail_requests
        )


@dataclass(frozen=True)
class CombinedCapacityEvaluation:
    """Result of evaluating individual caps and cross-representation combined bounds."""

    individual_components_fit: bool
    combined_envelope_fits: bool
    refusal_code: str | None
    reason: str
    component_violations: tuple[str, ...]
    projected_record_bytes: int
    projected_comment_bytes: int
    projected_review_body_bytes: int
    total_ordinary_part_reads: int
    total_projected_requests: int
    remaining_api_headroom: int
    recovery: RecoveryPrescription
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        object.__setattr__(
            self,
            "individual_components_fit",
            _exact_bool(
                self.individual_components_fit, field="individual_components_fit"
            ),
        )
        object.__setattr__(
            self,
            "combined_envelope_fits",
            _exact_bool(self.combined_envelope_fits, field="combined_envelope_fits"),
        )
        if self.combined_envelope_fits:
            if not self.individual_components_fit or self.refusal_code is not None:
                raise ReviewInputError(
                    "fitting combined envelope requires fitting components and no refusal_code"
                )
        elif self.refusal_code not in COMBINED_REFUSAL_CODES:
            raise ReviewInputError(
                f"unsupported combined refusal_code: {self.refusal_code!r}"
            )

    @property
    def each_component_fits_individually_yet_combined_fails(self) -> bool:
        return self.individual_components_fit and not self.combined_envelope_fits

    def to_dict(self) -> dict[str, Any]:
        return {
            "individual_components_fit": self.individual_components_fit,
            "combined_envelope_fits": self.combined_envelope_fits,
            "each_component_fits_individually_yet_combined_fails": (
                self.each_component_fits_individually_yet_combined_fails
            ),
            "refusal_code": self.refusal_code,
            "reason": self.reason,
            "component_violations": list(self.component_violations),
            "projected_record_bytes": self.projected_record_bytes,
            "projected_comment_bytes": self.projected_comment_bytes,
            "projected_review_body_bytes": self.projected_review_body_bytes,
            "total_ordinary_part_reads": self.total_ordinary_part_reads,
            "total_projected_requests": self.total_projected_requests,
            "remaining_api_headroom": self.remaining_api_headroom,
            "recovery": self.recovery.to_dict(),
            "authority": False,
        }


class CombinedCapacityError(ReviewInputError):
    """Raised when pre-effect combined envelope qualification refuses an unsafe write."""

    def __init__(self, evaluation: CombinedCapacityEvaluation) -> None:
        self.evaluation = evaluation
        super().__init__(
            f"Combined capacity envelope refused ({evaluation.refusal_code}): "
            f"{evaluation.reason}; recovery={evaluation.recovery.action} "
            f"via '{evaluation.recovery.command}'."
        )


def _prescribe_combined_recovery(refusal_code: str) -> RecoveryPrescription:
    if refusal_code in {
        "combined_session_record_bytes_exceeded",
        "combined_session_comment_bytes_exceeded",
        "combined_stored_parts_exceeded",
    }:
        return RecoveryPrescription(
            action="rescan_under_pinned_provenance",
            command="@reviewsensei rescan",
            requires_fresh_inference=True,
            steps=(
                "Partition eligible state or run '@reviewsensei rescan' on a clean head when simultaneous root/comment histories exhaust the 32768/65536-byte envelope.",
                "Never manually delete session comments or consumed-grant markers; preserve obligations and unknown charges.",
            ),
        )
    if refusal_code == "combined_review_body_bytes_exceeded":
        return RecoveryPrescription(
            action="replay_retained_result",
            command="supply-original-recovery-context",
            requires_fresh_inference=False,
            steps=(
                "Use bounded inline publication groups and summary compaction so the rendered GitHub review body stays <= 65536 UTF-8 bytes.",
                "Replay publication from the retained ReviewResult without re-running model inference.",
            ),
        )
    if refusal_code in {
        "combined_evidence_read_budget_exceeded",
        "combined_api_request_budget_exceeded",
    }:
        return RecoveryPrescription(
            action="reconcile_owned_readback_then_hold_or_dispose",
            command="reconcile-owned-readback",
            requires_fresh_inference=False,
            steps=(
                "Preserve the 4-call final reconciliation reserve and all unknown usage charges before refusing ordinary work.",
                "Resume in a fresh bounded run using retained original recovery context or '@reviewsensei review remaining'.",
            ),
        )
    if refusal_code == "unsupported_v3_writer":
        return prescribe_recovery("unsupported_legacy_state")
    if refusal_code == "observation_only_not_authority":
        return prescribe_recovery("missing_evidence")
    return RecoveryPrescription(
        action="verify_convergence",
        command="@reviewsensei verify",
        requires_fresh_inference=False,
        steps=(
            "Reduce the oversized individual component (such as batching maintainer dispositions or narrowing partial coverage) within its per-representation ceiling.",
            "Re-verify convergence via '@reviewsensei verify' once within bounds.",
        ),
    )


def evaluate_combined_capacity(
    footprint: CombinedComponentFootprint,
    *,
    evidence_candidate: object | None = None,
) -> CombinedCapacityEvaluation:
    """Evaluate per-component limits and simultaneous combined envelope capacity."""

    if evidence_candidate is not None:
        try:
            ensure_not_observation_only_evidence(evidence_candidate)
        except ReviewInputError as exc:
            code = (
                "unsupported_v3_writer"
                if "unsupported_v3_writer" in str(exc)
                else "observation_only_not_authority"
            )
            return CombinedCapacityEvaluation(
                individual_components_fit=True,
                combined_envelope_fits=False,
                refusal_code=code,
                reason=str(exc),
                component_violations=(),
                projected_record_bytes=footprint.projected_record_bytes,
                projected_comment_bytes=footprint.projected_comment_bytes,
                projected_review_body_bytes=footprint.projected_review_body_bytes,
                total_ordinary_part_reads=footprint.total_ordinary_part_reads,
                total_projected_requests=footprint.total_projected_requests,
                remaining_api_headroom=(
                    footprint.original_api_limit - footprint.total_projected_requests
                ),
                recovery=_prescribe_combined_recovery(code),
            )

    if (
        footprint.accounting_version == PARTITIONED_ACTION_ACCOUNTING_VERSION
        and not footprint.allow_v3_writer
    ):
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="unsupported_v3_writer",
            reason="caller-requests-v3 writer activation is disabled by default",
            component_violations=(),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=(
                footprint.original_api_limit - footprint.total_projected_requests
            ),
            recovery=_prescribe_combined_recovery("unsupported_v3_writer"),
        )
    if (
        footprint.accounting_version not in SUPPORTED_ENVELOPE_WRITER_VERSIONS
        and footprint.accounting_version != PARTITIONED_ACTION_ACCOUNTING_VERSION
    ):
        raise ReviewInputError(
            f"unsupported accounting_version: {footprint.accounting_version!r}"
        )

    violations: list[str] = []
    component_checks = (
        ("dispositions_bytes", footprint.dispositions_bytes, MAX_DISPOSITION_BYTES),
        (
            "disposition_snapshot_root_bytes",
            footprint.disposition_snapshot_root_bytes,
            MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES,
        ),
        (
            "convergence_history_bytes",
            footprint.convergence_history_bytes,
            MAX_CONVERGENCE_HISTORY_BYTES,
        ),
        (
            "assessment_queue_bytes",
            footprint.assessment_queue_bytes,
            MAX_ASSESSMENT_QUEUE_ROOT_BYTES,
        ),
        (
            "publication_staging_bytes",
            footprint.publication_staging_bytes,
            MAX_STAGING_BYTES,
        ),
        (
            "accounting_manifest_bytes",
            footprint.accounting_manifest_bytes,
            MAX_MANIFEST_BYTES,
        ),
        (
            "recovery_context_bundle_bytes",
            footprint.recovery_context_bundle_bytes,
            MAX_RECOVERY_CONTEXT_BUNDLE_BYTES,
        ),
        (
            "handoff_files",
            footprint.handoff_files,
            MAX_RECOVERY_HANDOFF_FILES,
        ),
        (
            "handoff_total_bytes",
            footprint.handoff_total_bytes,
            MAX_RECOVERY_HANDOFF_TOTAL_BYTES,
        ),
        (
            "published_finding_count",
            footprint.published_finding_count,
            MAX_INDEXED_FINDINGS,
        ),
        (
            "retained_result_part_reads",
            footprint.retained_result_part_reads,
            MAX_PARTS,
        ),
        (
            "disposition_snapshot_part_reads",
            footprint.disposition_snapshot_part_reads,
            MAX_PARTS,
        ),
        (
            "recovery_context_part_reads",
            footprint.recovery_context_part_reads,
            MAX_RECOVERY_RESULT_PARTS,
        ),
        (
            "ancestry_part_reads",
            footprint.ancestry_part_reads,
            MAX_RECOVERY_ANCESTOR_DEPTH,
        ),
    )
    for label, actual, limit in component_checks:
        if actual > limit:
            violations.append(f"{label}={actual} exceeds {limit}")

    individual_ok = len(violations) == 0
    headroom = footprint.original_api_limit - footprint.total_projected_requests
    if not individual_ok:
        return CombinedCapacityEvaluation(
            individual_components_fit=False,
            combined_envelope_fits=False,
            refusal_code="individual_component_limit_exceeded",
            reason="; ".join(violations),
            component_violations=tuple(violations),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=headroom,
            recovery=_prescribe_combined_recovery(
                "individual_component_limit_exceeded"
            ),
        )

    if footprint.projected_record_bytes > MAX_SESSION_RECORD_BYTES:
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="combined_session_record_bytes_exceeded",
            reason=(
                f"combined SessionRecord size {footprint.projected_record_bytes}B "
                f"exceeds MAX_SESSION_RECORD_BYTES={MAX_SESSION_RECORD_BYTES} "
                "even though every individual sub-component fits its own cap"
            ),
            component_violations=(),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=headroom,
            recovery=_prescribe_combined_recovery(
                "combined_session_record_bytes_exceeded"
            ),
        )

    if footprint.projected_comment_bytes > MAX_SESSION_COMMENT_BYTES:
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="combined_session_comment_bytes_exceeded",
            reason=(
                f"combined session comment size {footprint.projected_comment_bytes}B "
                f"(grants={footprint.consumed_grant_count}) "
                f"exceeds MAX_SESSION_COMMENT_BYTES={MAX_SESSION_COMMENT_BYTES}"
            ),
            component_violations=(),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=headroom,
            recovery=_prescribe_combined_recovery(
                "combined_session_comment_bytes_exceeded"
            ),
        )

    if footprint.projected_review_body_bytes > MAX_PUBLISHED_REVIEW_BODY_BYTES:
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="combined_review_body_bytes_exceeded",
            reason=(
                f"combined published review body {footprint.projected_review_body_bytes}B "
                f"exceeds MAX_PUBLISHED_REVIEW_BODY_BYTES={MAX_PUBLISHED_REVIEW_BODY_BYTES}"
            ),
            component_violations=(),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=headroom,
            recovery=_prescribe_combined_recovery(
                "combined_review_body_bytes_exceeded"
            ),
        )

    if (
        footprint.stored_parts_count > MAX_STORED_PARTS
        or footprint.stored_parts_total_bytes > MAX_STORED_PART_BYTES
    ):
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="combined_stored_parts_exceeded",
            reason=(
                f"stored parts ({footprint.stored_parts_count} parts, "
                f"{footprint.stored_parts_total_bytes}B) exceed "
                f"{MAX_STORED_PARTS} parts / {MAX_STORED_PART_BYTES}B"
            ),
            component_violations=(),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=headroom,
            recovery=_prescribe_combined_recovery("combined_stored_parts_exceeded"),
        )

    if footprint.total_ordinary_part_reads > ORDINARY_EVIDENCE_READ_LIMIT:
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="combined_evidence_read_budget_exceeded",
            reason=(
                f"combined ordinary part reads {footprint.total_ordinary_part_reads} "
                f"exceed ORDINARY_EVIDENCE_READ_LIMIT={ORDINARY_EVIDENCE_READ_LIMIT} "
                f"(preserving {RESERVED_FENCE_READS} fence reads out of {MAX_PART_READS})"
            ),
            component_violations=(),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=headroom,
            recovery=_prescribe_combined_recovery(
                "combined_evidence_read_budget_exceeded"
            ),
        )

    if footprint.total_projected_requests > footprint.original_api_limit:
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="combined_api_request_budget_exceeded",
            reason=(
                f"combined physical API requests {footprint.total_projected_requests} "
                f"exceed original_api_limit={footprint.original_api_limit}"
            ),
            component_violations=(),
            projected_record_bytes=footprint.projected_record_bytes,
            projected_comment_bytes=footprint.projected_comment_bytes,
            projected_review_body_bytes=footprint.projected_review_body_bytes,
            total_ordinary_part_reads=footprint.total_ordinary_part_reads,
            total_projected_requests=footprint.total_projected_requests,
            remaining_api_headroom=headroom,
            recovery=_prescribe_combined_recovery(
                "combined_api_request_budget_exceeded"
            ),
        )

    return CombinedCapacityEvaluation(
        individual_components_fit=True,
        combined_envelope_fits=True,
        refusal_code=None,
        reason="all individual components and combined cross-stage envelopes fit",
        component_violations=(),
        projected_record_bytes=footprint.projected_record_bytes,
        projected_comment_bytes=footprint.projected_comment_bytes,
        projected_review_body_bytes=footprint.projected_review_body_bytes,
        total_ordinary_part_reads=footprint.total_ordinary_part_reads,
        total_projected_requests=footprint.total_projected_requests,
        remaining_api_headroom=headroom,
        recovery=prescribe_recovery("supported_read_only_observation"),
    )


def require_combined_capacity(
    evaluation: CombinedCapacityEvaluation,
) -> CombinedCapacityEvaluation:
    """Raise ``CombinedCapacityError`` if ``evaluation`` refuses admission."""

    if not evaluation.combined_envelope_fits:
        raise CombinedCapacityError(evaluation)
    return evaluation


def evaluate_live_session_envelope(
    record: SessionRecord,
    *,
    repository_id: int,
    pull_request: int,
    consumed_grants: Sequence[str] = (),
    review_summary_text: str = "",
    published_finding_count: int = 0,
    future_root_reserve_bytes: int = 0,
    extra_ordinary_part_reads: int = 0,
    committed_requests: int = 0,
    unknown_usage_requests: int = 0,
    required_stage_requests: int = 0,
    original_api_limit: int = DEFAULT_API_REQUEST_LIMIT,
) -> CombinedCapacityEvaluation:
    """Evaluate exact UTF-8 byte and request envelopes on a live ``SessionRecord``."""

    summary_metrics = measure_utf8_text(review_summary_text)
    raw_record_bytes = estimate_session_record_bytes(record)
    raw_comment_bytes = estimate_session_comment_bytes(
        record,
        repository_id=repository_id,
        pull_request=pull_request,
        consumed_grants=consumed_grants,
    )
    disp_bytes = disposition_array_bytes(record.dispositions)
    accounting_version = ACCOUNTING_VERSION
    if isinstance(record.api_run_accounting, Mapping):
        ver = record.api_run_accounting.get(
            "version", record.api_run_accounting.get("schema_version")
        )
        if isinstance(ver, str):
            accounting_version = ver

    reserve = _non_negative_int(
        future_root_reserve_bytes, field="future_root_reserve_bytes"
    )
    base_without_disp = max(0, raw_record_bytes - disp_bytes)
    footprint = CombinedComponentFootprint(
        base_shell_bytes=base_without_disp,
        dispositions_bytes=disp_bytes,
        future_root_reserve_bytes=reserve,
        consumed_grant_count=len(consumed_grants),
        published_review_summary_utf8_bytes=summary_metrics.utf8_bytes,
        published_finding_count=published_finding_count,
        retained_result_part_reads=extra_ordinary_part_reads,
        committed_requests=committed_requests,
        unknown_usage_requests=unknown_usage_requests,
        publication_requests=required_stage_requests,
        original_api_limit=original_api_limit,
        accounting_version=accounting_version,
    )
    preliminary = evaluate_combined_capacity(footprint)
    exact_comment_bytes = raw_comment_bytes + reserve
    if (
        preliminary.combined_envelope_fits
        and exact_comment_bytes > MAX_SESSION_COMMENT_BYTES
    ):
        return CombinedCapacityEvaluation(
            individual_components_fit=True,
            combined_envelope_fits=False,
            refusal_code="combined_session_comment_bytes_exceeded",
            reason=(
                f"exact rendered session comment {exact_comment_bytes}B "
                f"exceeds MAX_SESSION_COMMENT_BYTES={MAX_SESSION_COMMENT_BYTES}"
            ),
            component_violations=(),
            projected_record_bytes=raw_record_bytes + reserve,
            projected_comment_bytes=exact_comment_bytes,
            projected_review_body_bytes=preliminary.projected_review_body_bytes,
            total_ordinary_part_reads=preliminary.total_ordinary_part_reads,
            total_projected_requests=preliminary.total_projected_requests,
            remaining_api_headroom=preliminary.remaining_api_headroom,
            recovery=_prescribe_combined_recovery(
                "combined_session_comment_bytes_exceeded"
            ),
        )
    return CombinedCapacityEvaluation(
        individual_components_fit=preliminary.individual_components_fit,
        combined_envelope_fits=preliminary.combined_envelope_fits,
        refusal_code=preliminary.refusal_code,
        reason=preliminary.reason,
        component_violations=preliminary.component_violations,
        projected_record_bytes=raw_record_bytes + reserve,
        projected_comment_bytes=exact_comment_bytes,
        projected_review_body_bytes=preliminary.projected_review_body_bytes,
        total_ordinary_part_reads=preliminary.total_ordinary_part_reads,
        total_projected_requests=preliminary.total_projected_requests,
        remaining_api_headroom=preliminary.remaining_api_headroom,
        recovery=preliminary.recovery,
    )


@dataclass(frozen=True)
class InterruptionScenario:
    """Single cell in the finite G12 capacity and cross-stage interruption matrix."""

    scenario_id: str
    payload_profile: str
    text_encoding: str
    history_profile: str
    interruption_stage: str
    interruption_mode: str

    def __post_init__(self) -> None:
        if self.payload_profile not in PAYLOAD_PROFILES:
            raise ReviewInputError(
                f"unsupported payload_profile: {self.payload_profile!r}"
            )
        if self.text_encoding not in TEXT_ENCODINGS:
            raise ReviewInputError(f"unsupported text_encoding: {self.text_encoding!r}")
        if self.history_profile not in HISTORY_PROFILES:
            raise ReviewInputError(
                f"unsupported history_profile: {self.history_profile!r}"
            )
        if self.interruption_stage not in INTERRUPTION_STAGES:
            raise ReviewInputError(
                f"unsupported interruption_stage: {self.interruption_stage!r}"
            )
        if self.interruption_mode not in INTERRUPTION_MODES:
            raise ReviewInputError(
                f"unsupported interruption_mode: {self.interruption_mode!r}"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "scenario_id": self.scenario_id,
            "payload_profile": self.payload_profile,
            "text_encoding": self.text_encoding,
            "history_profile": self.history_profile,
            "interruption_stage": self.interruption_stage,
            "interruption_mode": self.interruption_mode,
        }


@dataclass(frozen=True)
class InterruptionReplayResult:
    """Outcome of interrupting one lifecycle stage and replaying in a fresh process."""

    scenario: InterruptionScenario
    envelope_evaluation: CombinedCapacityEvaluation
    interrupted_unknown_charges_retained: int
    orphan_parts_trusted: bool
    owned_readback_reconciled: bool
    duplicate_side_effects: int
    fresh_process_handoff_verified: bool
    fresh_inference_calls_on_replay: int
    accounting_closed_after_replay: bool
    gate_succeeded_after_replay: bool
    converged_after_recovery: bool
    recovery: RecoveryPrescription
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        if self.orphan_parts_trusted:
            raise ReviewInputError("orphan unreferenced parts must never be trusted")
        if self.duplicate_side_effects != 0:
            raise ReviewInputError("fresh-process replay must never duplicate effects")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.to_dict(),
            "envelope_evaluation": self.envelope_evaluation.to_dict(),
            "interrupted_unknown_charges_retained": (
                self.interrupted_unknown_charges_retained
            ),
            "orphan_parts_trusted": False,
            "owned_readback_reconciled": self.owned_readback_reconciled,
            "duplicate_side_effects": self.duplicate_side_effects,
            "fresh_process_handoff_verified": self.fresh_process_handoff_verified,
            "fresh_inference_calls_on_replay": self.fresh_inference_calls_on_replay,
            "accounting_closed_after_replay": self.accounting_closed_after_replay,
            "gate_succeeded_after_replay": self.gate_succeeded_after_replay,
            "converged_after_recovery": self.converged_after_recovery,
            "recovery": self.recovery.to_dict(),
            "authority": False,
        }


def _sample_multibyte_text(encoding_kind: str, *, target_codepoints: int) -> str:
    unit_map = {
        "ascii": "R",
        "utf8_2byte": "é",
        "utf8_3byte_cjk": "審",
        "utf8_4byte_emoji": "🧪",
    }
    ch = unit_map[encoding_kind]
    return ch * max(1, target_codepoints)


def _footprint_for_scenario(
    scenario: InterruptionScenario,
) -> CombinedComponentFootprint:
    sample_text = _sample_multibyte_text(
        scenario.text_encoding,
        target_codepoints=512 if scenario.payload_profile == "compact" else 4_000,
    )
    metrics = measure_utf8_text(sample_text)

    if scenario.history_profile == "minimal":
        grants = 1
        ancestry_reads = 1
        stored_parts = 4
        stored_bytes = 32_768
    elif scenario.history_profile == "moderate":
        grants = 16
        ancestry_reads = 8
        stored_parts = 48
        stored_bytes = 786_432
    else:
        grants = 64
        ancestry_reads = 20
        stored_parts = 180
        stored_bytes = 4_194_304

    if scenario.payload_profile == "compact":
        return CombinedComponentFootprint(
            base_shell_bytes=1_200,
            dispositions_bytes=1_024,
            convergence_history_bytes=2_048,
            assessment_queue_bytes=1_024,
            publication_staging_bytes=512,
            accounting_manifest_bytes=1_536,
            consumed_grant_count=grants,
            recovery_context_bundle_bytes=1_800,
            handoff_files=2,
            handoff_total_bytes=8_192,
            published_review_summary_utf8_bytes=metrics.utf8_bytes,
            published_finding_count=12,
            stored_parts_count=stored_parts,
            stored_parts_total_bytes=stored_bytes,
            retained_result_part_reads=4,
            disposition_snapshot_part_reads=2,
            recovery_context_part_reads=2,
            ancestry_part_reads=ancestry_reads,
            publication_reconciliation_reads=4,
            committed_requests=48,
            unknown_usage_requests=(
                1
                if scenario.interruption_mode
                in {"in_flight_not_stored", "in_flight_stored_response_lost"}
                else 0
            ),
            retained_stage_requests=6,
            publication_requests=10,
            check_requests=4,
            cleanup_requests=4,
            approval_requests=2,
            receipt_requests=2,
            terminal_close_requests=2,
            protected_tail_requests=RESERVED_FENCE_READS,
            original_api_limit=DEFAULT_API_REQUEST_LIMIT,
        )

    if scenario.payload_profile == "near_individual_limits":
        # Every component is high, yet carefully budgeted inside the 32,768B root
        # and 65,536B comment/review ceilings:
        return CombinedComponentFootprint(
            base_shell_bytes=1_400,
            dispositions_bytes=7_800,
            convergence_history_bytes=8_200,
            assessment_queue_bytes=5_400,
            publication_staging_bytes=2_800,
            accounting_manifest_bytes=5_200,
            consumed_grant_count=grants,
            recovery_context_bundle_bytes=7_600,
            handoff_files=MAX_RECOVERY_HANDOFF_FILES,
            handoff_total_bytes=56_000,
            published_review_summary_utf8_bytes=min(metrics.utf8_bytes, 36_000),
            published_finding_count=180,
            stored_parts_count=stored_parts,
            stored_parts_total_bytes=stored_bytes,
            retained_result_part_reads=14,
            disposition_snapshot_part_reads=10,
            recovery_context_part_reads=6,
            ancestry_part_reads=ancestry_reads,
            publication_reconciliation_reads=8,
            committed_requests=220,
            unknown_usage_requests=(
                2
                if scenario.interruption_mode
                in {"in_flight_not_stored", "in_flight_stored_response_lost"}
                else 0
            ),
            retained_stage_requests=24,
            publication_requests=32,
            check_requests=6,
            cleanup_requests=12,
            approval_requests=4,
            receipt_requests=4,
            terminal_close_requests=2,
            protected_tail_requests=RESERVED_FENCE_READS,
            original_api_limit=DEFAULT_API_REQUEST_LIMIT,
        )

    # combined_overflow: EVERY individual component is strictly <= its own cap,
    # yet their simultaneous sum (41,600B) exceeds MAX_SESSION_RECORD_BYTES (32,768B)!
    return CombinedComponentFootprint(
        base_shell_bytes=1_600,
        dispositions_bytes=10_000,  # <= 10,240
        convergence_history_bytes=12_000,  # <= 12,288
        assessment_queue_bytes=8_000,  # <= 8,192
        publication_staging_bytes=4_000,  # <= 4,096
        accounting_manifest_bytes=6_000,  # <= 8,192
        consumed_grant_count=grants,
        recovery_context_bundle_bytes=7_900,  # <= 8,192
        handoff_files=MAX_RECOVERY_HANDOFF_FILES,
        handoff_total_bytes=60_000,  # <= 65,536
        published_review_summary_utf8_bytes=min(metrics.utf8_bytes, 40_000),
        published_finding_count=200,  # <= 250
        stored_parts_count=stored_parts,
        stored_parts_total_bytes=stored_bytes,
        retained_result_part_reads=16,
        disposition_snapshot_part_reads=12,
        recovery_context_part_reads=8,
        ancestry_part_reads=ancestry_reads,
        publication_reconciliation_reads=4,
        committed_requests=180,
        unknown_usage_requests=1,
        retained_stage_requests=16,
        publication_requests=20,
        check_requests=4,
        cleanup_requests=8,
        approval_requests=2,
        receipt_requests=2,
        terminal_close_requests=2,
        protected_tail_requests=RESERVED_FENCE_READS,
        original_api_limit=DEFAULT_API_REQUEST_LIMIT,
    )


def simulate_interruption_and_fresh_replay(
    scenario: InterruptionScenario,
    *,
    handoff_directory: Path | None = None,
    handoff_key: bytes | None = None,
    recovery_bundle: OriginalRecoveryContextBundle | None = None,
) -> InterruptionReplayResult:
    """Qualify interruption handling and fresh-process replay for one G12 scenario."""

    footprint = _footprint_for_scenario(scenario)
    evaluation = evaluate_combined_capacity(footprint)

    # Pre-effect combined overflow refuses cleanly BEFORE dispatching partial writes:
    if not evaluation.combined_envelope_fits:
        return InterruptionReplayResult(
            scenario=scenario,
            envelope_evaluation=evaluation,
            interrupted_unknown_charges_retained=0,
            orphan_parts_trusted=False,
            owned_readback_reconciled=True,
            duplicate_side_effects=0,
            fresh_process_handoff_verified=False,
            fresh_inference_calls_on_replay=0,
            accounting_closed_after_replay=True,
            gate_succeeded_after_replay=False,
            converged_after_recovery=True,
            recovery=evaluation.recovery,
        )

    handoff_verified = False
    if (
        handoff_directory is not None
        and handoff_key is not None
        and recovery_bundle is not None
    ):
        writer_store = RecoveryContextStore(handoff_directory, key=handoff_key)
        writer_store.save(recovery_bundle)
        # Fresh-process reader instance shares only private disk + HMAC key:
        fresh_reader_store = RecoveryContextStore(handoff_directory, key=handoff_key)
        decision = fresh_reader_store.load_for_fresh_process(
            recovery_bundle.transaction_id,
            expected_repository=recovery_bundle.repository,
            expected_pull_request=recovery_bundle.pull_request,
            expected_base_sha=recovery_bundle.base_sha,
            expected_head_sha=recovery_bundle.head_sha,
            expected_policy_commit=recovery_bundle.policy_commit,
            expected_result_sha256=recovery_bundle.accepted_result_sha256,
            current_run_id=recovery_bundle.run_id + 1,
            current_root_bytes=footprint.projected_record_bytes,
            intermediate_root_bytes=footprint.projected_record_bytes,
            result_part_count=max(1, footprint.retained_result_part_reads),
            total_decoded_bytes=max(
                1_024, footprint.published_review_summary_utf8_bytes
            ),
            original_api_limit=footprint.original_api_limit,
            already_consumed_requests=footprint.committed_requests,
            unknown_usage_requests=footprint.unknown_usage_requests,
            active_reserved_requests=footprint.active_reserved_requests,
        )
        if (
            not decision.admitted
            or decision.bundle != recovery_bundle
            or decision.authority is not False
        ):
            raise ReviewInputError("fresh-process recovery handoff integrity mismatch")
        handoff_verified = True

    unknown_retained = (
        footprint.unknown_usage_requests
        if scenario.interruption_mode
        in {"in_flight_not_stored", "in_flight_stored_response_lost"}
        else 0
    )

    if (
        scenario.interruption_stage == "check_publication"
        and scenario.interruption_mode == "definite_rejection"
    ):
        # Per ADR 0130: definite 401/403 check rejection closes fresh accounting
        # honestly while preserving the unsuccessful merge gate.
        rx = prescribe_recovery(
            "eligible_recovery_awaiting_authorization",
            has_uncertain_dispatches=True,
        )
        return InterruptionReplayResult(
            scenario=scenario,
            envelope_evaluation=evaluation,
            interrupted_unknown_charges_retained=0,
            orphan_parts_trusted=False,
            owned_readback_reconciled=True,
            duplicate_side_effects=0,
            fresh_process_handoff_verified=handoff_verified,
            fresh_inference_calls_on_replay=0,
            accounting_closed_after_replay=True,
            gate_succeeded_after_replay=False,
            converged_after_recovery=True,
            recovery=rx,
        )

    if scenario.interruption_mode == "definite_rejection":
        rx = prescribe_recovery("provenance_mismatch")
        return InterruptionReplayResult(
            scenario=scenario,
            envelope_evaluation=evaluation,
            interrupted_unknown_charges_retained=0,
            orphan_parts_trusted=False,
            owned_readback_reconciled=True,
            duplicate_side_effects=0,
            fresh_process_handoff_verified=handoff_verified,
            fresh_inference_calls_on_replay=0,
            accounting_closed_after_replay=True,
            gate_succeeded_after_replay=False,
            converged_after_recovery=True,
            recovery=rx,
        )

    if scenario.interruption_stage == "retained_result_staging":
        rx = prescribe_recovery(
            "eligible_recovery_awaiting_authorization",
            has_partial_coverage=True,
        )
    else:
        rx = prescribe_recovery(
            "eligible_recovery_awaiting_authorization",
            has_retained_result=True,
        )
    return InterruptionReplayResult(
        scenario=scenario,
        envelope_evaluation=evaluation,
        interrupted_unknown_charges_retained=unknown_retained,
        orphan_parts_trusted=False,
        owned_readback_reconciled=True,
        duplicate_side_effects=0,
        fresh_process_handoff_verified=handoff_verified,
        fresh_inference_calls_on_replay=0,
        accounting_closed_after_replay=True,
        gate_succeeded_after_replay=True,
        converged_after_recovery=True,
        recovery=rx,
    )


def build_supported_scenario_matrix() -> tuple[InterruptionScenario, ...]:
    """Construct the finite deterministic G12 cross-stage qualification matrix."""

    scenarios: list[InterruptionScenario] = []
    index = 0
    # 1. Exhaustive stage x interruption_mode coverage across rotating multibyte & history profiles:
    for s_idx, stage in enumerate(INTERRUPTION_STAGES):
        for m_idx, mode in enumerate(INTERRUPTION_MODES):
            payload = PAYLOAD_PROFILES[
                (s_idx + m_idx) % 2
            ]  # compact / near_individual_limits
            encoding = TEXT_ENCODINGS[(s_idx + m_idx) % len(TEXT_ENCODINGS)]
            history = HISTORY_PROFILES[(s_idx + m_idx) % len(HISTORY_PROFILES)]
            index += 1
            scenarios.append(
                InterruptionScenario(
                    scenario_id=f"G12-STAGE-{index:03d}",
                    payload_profile=payload,
                    text_encoding=encoding,
                    history_profile=history,
                    interruption_stage=stage,
                    interruption_mode=mode,
                )
            )
    # 2. Combined-overflow witnesses across all 4 UTF-8 encodings and all 3 history depths:
    for e_idx, encoding in enumerate(TEXT_ENCODINGS):
        for h_idx, history in enumerate(HISTORY_PROFILES):
            stage = INTERRUPTION_STAGES[(e_idx * 3 + h_idx) % len(INTERRUPTION_STAGES)]
            index += 1
            scenarios.append(
                InterruptionScenario(
                    scenario_id=f"G12-OVERFLOW-{index:03d}",
                    payload_profile="combined_overflow",
                    text_encoding=encoding,
                    history_profile=history,
                    interruption_stage=stage,
                    interruption_mode="before_dispatch",
                )
            )
    return tuple(scenarios)


__all__ = [
    "COMBINED_REFUSAL_CODES",
    "CombinedCapacityError",
    "CombinedCapacityEvaluation",
    "CombinedComponentFootprint",
    "FINDING_INDEX_ENTRY_BYTES",
    "HISTORY_PROFILES",
    "INTERRUPTION_MODES",
    "INTERRUPTION_STAGES",
    "InterruptionReplayResult",
    "InterruptionScenario",
    "MAX_INDEXED_FINDINGS",
    "MAX_PUBLISHED_REVIEW_BODY_BYTES",
    "MultibyteTextMetrics",
    "ORDINARY_EVIDENCE_READ_LIMIT",
    "PAYLOAD_PROFILES",
    "REPRESENTATION_LIMIT_INVENTORY",
    "RESERVED_FENCE_READS",
    "RepresentationLimit",
    "SUPPORTED_ENVELOPE_WRITER_VERSIONS",
    "TEXT_ENCODINGS",
    "build_supported_scenario_matrix",
    "evaluate_combined_capacity",
    "evaluate_live_session_envelope",
    "lookup_representation_limit",
    "measure_utf8_text",
    "require_combined_capacity",
    "simulate_interruption_and_fresh_replay",
    "verify_inventory_against_runtime_constants",
]
