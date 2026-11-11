"""Bounded, content-addressed original recovery-context retention and handoff (G17).

This module censuses and authenticates the minimum non-sensitive digests and
bindings required to recover publication or continuation in a fresh process
without re-running model inference, while strictly excluding raw prompts,
unified diffs, provider outputs, and credentials per ``docs/data-handling.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .api_work import (
    ACCOUNTING_VERSION,
    LEGACY_ACCOUNTING_VERSION,
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
)
from .errors import ReviewInputError
from .outcomes import DEFAULT_RECOVERY_TTL_SECONDS, MAX_RECOVERY_TTL_SECONDS
from .provenance_manifest import (
    RecoveryPrescription,
    _exact_bool,
    _exact_false_authority,
    _non_negative_int,
    _reject_secret_or_control,
    prescribe_recovery,
)

RECOVERY_CONTEXT_SCHEMA_VERSION = "original-recovery-context-v1"
RECOVERY_HANDOFF_ENVELOPE_VERSION = "original-recovery-handoff-v1"
MAX_RECOVERY_CONTEXT_BUNDLE_BYTES = 8_192
MAX_RECOVERY_HANDOFF_FILES = 8
MAX_RECOVERY_HANDOFF_TOTAL_BYTES = 65_536
MAX_RECOVERY_ANCESTOR_DEPTH = 32
MAX_RECOVERY_RESULT_PARTS = 32
MAX_SESSION_ROOT_BYTES = 65_536
MAX_DECODED_RESULT_BYTES = 2 * 1024 * 1024

SUPPORTED_WRITER_ACCOUNTING_VERSIONS = frozenset(
    {LEGACY_ACCOUNTING_VERSION, ACCOUNTING_VERSION}
)
PLACEMENT_MODES = frozenset(
    {"complete", "bounded", "advisory", "merge_focused", "merge-focused"}
)
RUNTIME_ONLY_FIELD_LABELS = frozenset(
    {
        "stage_provider_routing",
        "legacy_cache_key",
        "overwritten_verification_baseline",
        "custom_stages",
        "cli_overrides",
        "carried_eligibility_holds",
    }
)
OBSERVATION_ONLY_REFUSAL_CODE = "observation_only_not_authority"
UNSUPPORTED_V3_WRITER_REFUSAL_CODE = "unsupported_v3_writer"

REFUSAL_CODES = frozenset(
    {
        "absent_context",
        "truncated_or_corrupt_context",
        "tampered_authentication_or_digest",
        "expired_context",
        "overwritten_baseline_or_context",
        "snapshot_or_transaction_mismatch",
        "circular_ancestor_evidence",
        "ancestor_depth_exceeded",
        "root_capacity_exceeded",
        "budget_exhausted_before_effect",
        UNSUPPORTED_V3_WRITER_REFUSAL_CODE,
        OBSERVATION_ONLY_REFUSAL_CODE,
    }
)


class RecoveryRefusalError(ReviewInputError):
    """Structured ReviewInputError carrying a closed-vocabulary refusal_code."""

    def __init__(self, message: str, *, refusal_code: str) -> None:
        if refusal_code not in REFUSAL_CODES:
            raise ReviewInputError(f"invalid refusal_code: {refusal_code}")
        self.refusal_code = refusal_code
        super().__init__(message)


_HEX40_RE = re.compile(r"^[a-f0-9]{40}$")
_HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_REPO_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")


def canonical_recovery_digest(value: object) -> str:
    """Compute a deterministic SHA-256 digest over canonical JSON bytes."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hex40(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{field} must be a 40-char lowercase Git SHA")
    _reject_secret_or_control(value, field=field)
    if _HEX40_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{field} must be a 40-char lowercase Git SHA")
    return value


def _hex64(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{field} must be a 64-char lowercase SHA-256")
    _reject_secret_or_control(value, field=field)
    if _HEX64_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{field} must be a 64-char lowercase SHA-256")
    return value


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or not 1 <= value <= (1 << 53) - 1:
        raise ReviewInputError(f"{field} must be a positive integer")
    return value


def _safe_token(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{field} must be a non-empty bounded identifier")
    _reject_secret_or_control(value, field=field)
    if _SAFE_TOKEN_RE.fullmatch(value) is None or ".." in value or "//" in value:
        raise ReviewInputError(f"{field} must be a non-empty bounded identifier")
    return value


def classify_recovery_input_origins(
    *,
    has_stage_provider_routing: bool = False,
    has_legacy_cache_key: bool = False,
    has_overwritten_verification_baseline: bool = False,
    has_custom_stages: bool = False,
    has_cli_overrides: bool = False,
    has_carried_eligibility_holds: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Separate default-reconstructible context from runtime-only inputs."""

    flags = (
        ("stage_provider_routing", has_stage_provider_routing),
        ("legacy_cache_key", has_legacy_cache_key),
        ("overwritten_verification_baseline", has_overwritten_verification_baseline),
        ("custom_stages", has_custom_stages),
        ("cli_overrides", has_cli_overrides),
        ("carried_eligibility_holds", has_carried_eligibility_holds),
    )
    runtime_only: list[str] = []
    for label, flag in flags:
        checked = _exact_bool(flag, field=label)
        if checked:
            runtime_only.append(label)
    ordered = tuple(sorted(runtime_only))
    return (len(ordered) == 0, ordered)


def ensure_supported_writer_accounting(
    candidate: object, *, allow_v3_writer: bool = False
) -> None:
    """Refuse reader-only caller-requests-v3 documents on writer enrollment paths."""

    if allow_v3_writer or not isinstance(candidate, Mapping):
        return
    if (
        candidate.get("accounting_version") == PARTITIONED_ACTION_ACCOUNTING_VERSION
        or candidate.get("version") == PARTITIONED_ACTION_ACCOUNTING_VERSION
        or candidate.get("schema_version") == PARTITIONED_ACTION_ACCOUNTING_VERSION
    ):
        raise RecoveryRefusalError(
            "unsupported_v3_writer: caller-requests-v3 is reader-only and cannot enroll recovery writes",
            refusal_code=UNSUPPORTED_V3_WRITER_REFUSAL_CODE,
        )


def ensure_not_observation_only_evidence(
    candidate: object, *, check_writer_enrollment: bool = True
) -> None:
    """Refuse read-only #431/#436 observations when passed as recovery context.

    Observation-only checks always run first and raise
    ``observation_only_not_authority``. When ``check_writer_enrollment=True``
    (default for writer/recovery bundle enrollment), ``caller-requests-v3``
    documents also raise ``unsupported_v3_writer``; read-side callers pass
    ``check_writer_enrollment=False`` so valid v3 reader observations are never
    misclassified as writer refusals.
    """

    if candidate is None:
        return
    if hasattr(candidate, "anchor_document") and hasattr(candidate, "ancestors"):
        raise RecoveryRefusalError(
            "observation_only_not_authority: ancestry observation confers no recovery authority",
            refusal_code=OBSERVATION_ONLY_REFUSAL_CODE,
        )
    if hasattr(candidate, "root_document") and hasattr(candidate, "result_document"):
        raise RecoveryRefusalError(
            "observation_only_not_authority: archived action result observation confers no recovery authority",
            refusal_code=OBSERVATION_ONLY_REFUSAL_CODE,
        )
    if (
        isinstance(candidate, Mapping)
        and candidate.get("schema_version") == "caller-action-result-v1"
    ):
        raise RecoveryRefusalError(
            "observation_only_not_authority: read-only action result is not original recovery context",
            refusal_code=OBSERVATION_ONLY_REFUSAL_CODE,
        )
    if check_writer_enrollment:
        ensure_supported_writer_accounting(candidate)


@dataclass(frozen=True)
class OriginalRecoveryContextBundle:
    """Minimum sanitized digest census for zero-inference fresh-process recovery."""

    repository: str
    repository_id: int | None
    pull_request: int
    base_ref: str
    base_sha: str
    head_sha: str
    policy_commit: str
    transaction_id: str
    generation: int
    reservation_id: str
    accepted_result_sha256: str
    configuration_digest: str
    evidence_digest: str
    convergence_policy_digest: str
    work_policy_digest: str
    decision_policy_digest: str
    provider_identity: str
    selected_context_digest: str
    learnings_digest: str
    baseline_digest: str | None
    diff_sha256: str
    placement_mode: str
    accounting_version: str = ACCOUNTING_VERSION
    producer: str = "local-cli"
    run_id: int = 1
    attempt: int = 1
    reconstructible_from_defaults: bool = True
    runtime_only_fields: tuple[str, ...] = ()
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        if not isinstance(self.repository, str):
            raise ReviewInputError("repository must be 'owner/repo'")
        _reject_secret_or_control(self.repository, field="repository")
        if _SAFE_REPO_RE.fullmatch(self.repository) is None or ".." in self.repository:
            raise ReviewInputError("repository must be 'owner/repo'")
        if self.repository_id is not None:
            _positive_int(self.repository_id, field="repository_id")
        _positive_int(self.pull_request, field="pull_request")
        if not isinstance(self.base_ref, str):
            raise ReviewInputError("base_ref must be a safe branch ref")
        _reject_secret_or_control(self.base_ref, field="base_ref")
        if (
            _SAFE_REF_RE.fullmatch(self.base_ref) is None
            or ".." in self.base_ref
            or "//" in self.base_ref
        ):
            raise ReviewInputError("base_ref must be a safe branch ref")
        _hex40(self.base_sha, field="base_sha")
        _hex40(self.head_sha, field="head_sha")
        _hex40(self.policy_commit, field="policy_commit")
        _hex64(self.transaction_id, field="transaction_id")
        _non_negative_int(self.generation, field="generation")
        _safe_token(self.reservation_id, field="reservation_id")
        for digest_attr in (
            "accepted_result_sha256",
            "configuration_digest",
            "evidence_digest",
            "convergence_policy_digest",
            "work_policy_digest",
            "decision_policy_digest",
            "selected_context_digest",
            "learnings_digest",
            "diff_sha256",
        ):
            _hex64(getattr(self, digest_attr), field=digest_attr)
        if self.baseline_digest is not None:
            _hex64(self.baseline_digest, field="baseline_digest")
        _safe_token(self.provider_identity, field="provider_identity")
        if self.placement_mode not in PLACEMENT_MODES:
            raise ReviewInputError(
                f"placement_mode must be one of {sorted(PLACEMENT_MODES)}"
            )
        if self.accounting_version == PARTITIONED_ACTION_ACCOUNTING_VERSION:
            raise RecoveryRefusalError(
                "unsupported_v3_writer: caller-requests-v3 is reader-only and cannot enroll recovery writes",
                refusal_code=UNSUPPORTED_V3_WRITER_REFUSAL_CODE,
            )
        if self.accounting_version not in SUPPORTED_WRITER_ACCOUNTING_VERSIONS:
            raise ReviewInputError("unsupported accounting_version for recovery bundle")
        _safe_token(self.producer, field="producer")
        _positive_int(self.run_id, field="run_id")
        if type(self.attempt) is not int or self.attempt not in {1, 2}:
            raise ReviewInputError("attempt must be 1 or 2")
        _exact_bool(
            self.reconstructible_from_defaults, field="reconstructible_from_defaults"
        )
        if isinstance(self.runtime_only_fields, (str, bytes)) or not isinstance(
            self.runtime_only_fields, Sequence
        ):
            raise ReviewInputError("runtime_only_fields must be a sequence")
        normalized_runtime = tuple(sorted(set(self.runtime_only_fields)))
        if len(normalized_runtime) != len(tuple(self.runtime_only_fields)):
            raise ReviewInputError("runtime_only_fields must not contain duplicates")
        for label in normalized_runtime:
            if label not in RUNTIME_ONLY_FIELD_LABELS:
                raise ReviewInputError(f"unknown runtime_only_fields label: {label}")
        if self.reconstructible_from_defaults and normalized_runtime:
            raise ReviewInputError(
                "reconstructible_from_defaults cannot be True when runtime_only_fields is non-empty"
            )
        object.__setattr__(self, "runtime_only_fields", normalized_runtime)
        if len(_canonical_bytes(self.to_dict())) > MAX_RECOVERY_CONTEXT_BUNDLE_BYTES:
            raise ReviewInputError("recovery context bundle exceeds 8,192-byte ceiling")

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECOVERY_CONTEXT_SCHEMA_VERSION,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "policy_commit": self.policy_commit,
            "transaction_id": self.transaction_id,
            "generation": self.generation,
            "reservation_id": self.reservation_id,
            "accepted_result_sha256": self.accepted_result_sha256,
            "configuration_digest": self.configuration_digest,
            "evidence_digest": self.evidence_digest,
            "convergence_policy_digest": self.convergence_policy_digest,
            "work_policy_digest": self.work_policy_digest,
            "decision_policy_digest": self.decision_policy_digest,
            "provider_identity": self.provider_identity,
            "selected_context_digest": self.selected_context_digest,
            "learnings_digest": self.learnings_digest,
            "baseline_digest": self.baseline_digest,
            "diff_sha256": self.diff_sha256,
            "placement_mode": self.placement_mode,
            "accounting_version": self.accounting_version,
            "producer": self.producer,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "reconstructible_from_defaults": self.reconstructible_from_defaults,
            "runtime_only_fields": list(self.runtime_only_fields),
            "contains_raw_diff_or_prompt": False,
            "authority": False,
        }

    @property
    def bundle_sha256(self) -> str:
        return hashlib.sha256(_canonical_bytes(self._body_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = self._body_dict()
        payload["bundle_sha256"] = self.bundle_sha256
        return payload

    @classmethod
    def from_dict(cls, data: object) -> OriginalRecoveryContextBundle:
        ensure_not_observation_only_evidence(data)
        if not isinstance(data, Mapping):
            raise ReviewInputError("recovery context bundle must be a JSON object")
        required = set(cls.__dataclass_fields__.keys()) | {
            "schema_version",
            "contains_raw_diff_or_prompt",
        }
        optional = {"bundle_sha256"}
        keys = set(data.keys())
        if required - keys:
            raise ReviewInputError(
                f"recovery context bundle missing fields: {sorted(required - keys)}"
            )
        if keys - required - optional:
            raise ReviewInputError(
                f"recovery context bundle has unexpected fields: {sorted(keys - required - optional)}"
            )
        if data["schema_version"] != RECOVERY_CONTEXT_SCHEMA_VERSION:
            raise ReviewInputError("unsupported recovery context schema_version")
        if data["contains_raw_diff_or_prompt"] is not False:
            raise ReviewInputError("contains_raw_diff_or_prompt must be False")
        _exact_false_authority(data["authority"], field="authority")
        runtime_raw = data["runtime_only_fields"]
        if not isinstance(runtime_raw, list):
            raise ReviewInputError("runtime_only_fields must be a JSON array")
        bundle = cls(
            repository=data["repository"],
            repository_id=data["repository_id"],
            pull_request=data["pull_request"],
            base_ref=data["base_ref"],
            base_sha=data["base_sha"],
            head_sha=data["head_sha"],
            policy_commit=data["policy_commit"],
            transaction_id=data["transaction_id"],
            generation=data["generation"],
            reservation_id=data["reservation_id"],
            accepted_result_sha256=data["accepted_result_sha256"],
            configuration_digest=data["configuration_digest"],
            evidence_digest=data["evidence_digest"],
            convergence_policy_digest=data["convergence_policy_digest"],
            work_policy_digest=data["work_policy_digest"],
            decision_policy_digest=data["decision_policy_digest"],
            provider_identity=data["provider_identity"],
            selected_context_digest=data["selected_context_digest"],
            learnings_digest=data["learnings_digest"],
            baseline_digest=data["baseline_digest"],
            diff_sha256=data["diff_sha256"],
            placement_mode=data["placement_mode"],
            accounting_version=data["accounting_version"],
            producer=data["producer"],
            run_id=data["run_id"],
            attempt=data["attempt"],
            reconstructible_from_defaults=data["reconstructible_from_defaults"],
            runtime_only_fields=tuple(runtime_raw),
            authority=False,
        )
        if "bundle_sha256" in data:
            expected = _hex64(data["bundle_sha256"], field="bundle_sha256")
            if expected != bundle.bundle_sha256:
                raise RecoveryRefusalError(
                    "tampered_authentication_or_digest: bundle_sha256 mismatch",
                    refusal_code="tampered_authentication_or_digest",
                )
        return bundle


def build_bundle_from_context_documents(
    *,
    transaction: Any,
    configuration_context: Mapping[str, Any],
    admission_context: Mapping[str, Any],
    decision_policy_digest: str,
    diff_sha256: str,
    policy_commit: str,
    evidence_context: Mapping[str, Any] | None = None,
    base_ref: str = "main",
    repository_id: int | None = None,
    placement_mode: str = "complete",
    accounting_version: str = ACCOUNTING_VERSION,
    producer: str = "local-cli",
    run_id: int = 1,
    attempt: int = 1,
    reconstructible_from_defaults: bool = True,
    runtime_only_fields: Sequence[str] = (),
) -> OriginalRecoveryContextBundle:
    """Build a sanitized recovery bundle from original context documents without raw diffs."""

    ensure_not_observation_only_evidence(configuration_context)
    ensure_not_observation_only_evidence(admission_context)
    if evidence_context is not None:
        ensure_not_observation_only_evidence(evidence_context)
    if not isinstance(configuration_context, Mapping) or not isinstance(
        admission_context, Mapping
    ):
        raise ReviewInputError("configuration and admission contexts must be mappings")
    if "diff" in admission_context or "prompt" in admission_context:
        raise ReviewInputError(
            "raw diff or prompt content is forbidden in recovery context inputs"
        )
    if getattr(transaction, "result_sha256", None) is None:
        raise ReviewInputError("transaction must bind an accepted result_sha256")

    from .models import ReviewTransaction

    computed_config_digest = ReviewTransaction.compute_configuration_digest(
        dict(configuration_context)
    )
    resolved_evidence: Mapping[str, Any]
    if evidence_context is not None:
        resolved_evidence = evidence_context
    elif isinstance(admission_context.get("evidence"), Mapping):
        resolved_evidence = admission_context["evidence"]
    else:
        resolved_evidence = {"evidence_policy": "legacy", "snapshot_sha256": None}
    computed_evidence_digest = ReviewTransaction.compute_evidence_digest(
        dict(resolved_evidence)
    )
    if computed_config_digest != transaction.configuration_digest:
        raise RecoveryRefusalError(
            "configuration_context digest does not match committed transaction",
            refusal_code="snapshot_or_transaction_mismatch",
        )
    if computed_evidence_digest != transaction.evidence_digest:
        raise RecoveryRefusalError(
            "admission_context evidence digest does not match committed transaction",
            refusal_code="snapshot_or_transaction_mismatch",
        )

    raw_provider = configuration_context.get("provider")
    if isinstance(raw_provider, Mapping):
        provider = str(raw_provider.get("name") or "unknown")
    else:
        provider = str(raw_provider or "unknown")
    model = str(configuration_context.get("model") or "default")

    orchestration = configuration_context.get("orchestration")
    raw_work_digest = configuration_context.get("work_policy_digest")
    if raw_work_digest is None and isinstance(orchestration, Mapping):
        raw_work_digest = orchestration.get("work_policy_digest")
    if isinstance(raw_work_digest, str) and _HEX64_RE.fullmatch(raw_work_digest):
        work_policy_digest = raw_work_digest
    else:
        work_policy_digest = canonical_recovery_digest(orchestration)

    current_key = admission_context.get("current_key")
    if (
        "active_category_ids" in admission_context
        or "lens_contexts" in admission_context
    ):
        selected_context_digest = canonical_recovery_digest(
            {
                "active_category_ids": admission_context.get("active_category_ids", []),
                "lens_contexts": admission_context.get("lens_contexts", []),
            }
        )
    else:
        selected_context_digest = canonical_recovery_digest(current_key)

    if "learnings" in admission_context:
        learnings_digest = canonical_recovery_digest(
            admission_context.get("learnings", [])
        )
    elif (
        isinstance(current_key, Mapping)
        and isinstance(current_key.get("learnings_digest"), str)
        and _HEX64_RE.fullmatch(current_key["learnings_digest"])
    ):
        learnings_digest = current_key["learnings_digest"]
    else:
        learnings_digest = canonical_recovery_digest([])

    baseline_raw = admission_context.get("baseline")
    baseline_digest = (
        canonical_recovery_digest(baseline_raw) if baseline_raw is not None else None
    )
    return OriginalRecoveryContextBundle(
        repository=transaction.repository,
        repository_id=repository_id,
        pull_request=transaction.pull_request,
        base_ref=base_ref,
        base_sha=transaction.base_sha,
        head_sha=transaction.head_sha,
        policy_commit=policy_commit,
        transaction_id=transaction.transaction_id,
        generation=transaction.generation,
        reservation_id=transaction.reservation_id,
        accepted_result_sha256=transaction.result_sha256,
        configuration_digest=computed_config_digest,
        evidence_digest=computed_evidence_digest,
        convergence_policy_digest=transaction.policy_digest,
        work_policy_digest=work_policy_digest,
        decision_policy_digest=decision_policy_digest,
        provider_identity=f"{provider}:{model}",
        selected_context_digest=selected_context_digest,
        learnings_digest=learnings_digest,
        baseline_digest=baseline_digest,
        diff_sha256=diff_sha256,
        placement_mode=placement_mode,
        accounting_version=accounting_version,
        producer=producer,
        run_id=run_id,
        attempt=attempt,
        reconstructible_from_defaults=reconstructible_from_defaults,
        runtime_only_fields=tuple(runtime_only_fields),
        authority=False,
    )


@dataclass(frozen=True)
class RecoveryBudgetPricing:
    """Pre-effect request, root, and ancestor-chain pricing under original budget."""

    startup_read_requests: int
    ancestor_read_requests: int
    result_part_read_requests: int
    publication_write_requests: int
    readback_requests: int
    protected_tail_requests: int
    prior_committed_requests: int
    original_api_limit: int
    current_root_bytes: int
    intermediate_root_bytes: int
    total_decoded_bytes: int

    @property
    def required_new_requests(self) -> int:
        return (
            self.startup_read_requests
            + self.ancestor_read_requests
            + self.result_part_read_requests
            + self.publication_write_requests
            + self.readback_requests
            + self.protected_tail_requests
        )

    @property
    def total_projected_requests(self) -> int:
        return self.prior_committed_requests + self.required_new_requests

    @property
    def remaining_headroom_requests(self) -> int:
        return self.original_api_limit - self.total_projected_requests

    def fits(self) -> bool:
        return (
            self.total_projected_requests <= self.original_api_limit
            and self.result_part_read_requests <= MAX_RECOVERY_RESULT_PARTS
            and self.current_root_bytes <= MAX_SESSION_ROOT_BYTES
            and self.intermediate_root_bytes <= MAX_SESSION_ROOT_BYTES
            and self.total_decoded_bytes <= MAX_DECODED_RESULT_BYTES
        )


@dataclass(frozen=True)
class RecoveryContextDecision:
    """Outcome of validating original recovery context before admitting effects."""

    admitted: bool
    bundle: OriginalRecoveryContextBundle | None
    refusal_code: str | None
    missing_or_invalid: tuple[str, ...]
    pricing: RecoveryBudgetPricing | None
    recovery: RecoveryPrescription
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        _exact_bool(self.admitted, field="admitted")
        if self.admitted:
            if self.bundle is None or self.refusal_code is not None:
                raise ReviewInputError(
                    "admitted recovery decision requires bundle and null refusal_code"
                )
        else:
            if self.refusal_code not in REFUSAL_CODES:
                raise ReviewInputError(f"invalid refusal_code: {self.refusal_code}")


def _refusal_decision(
    code: str,
    details: Sequence[str],
    *,
    pricing: RecoveryBudgetPricing | None = None,
    ledger_expired: bool = False,
) -> RecoveryContextDecision:
    if ledger_expired:
        prescription = prescribe_recovery("missing_evidence", ledger_expired=True)
    elif code in {
        "absent_context",
        "truncated_or_corrupt_context",
        "tampered_authentication_or_digest",
        "expired_context",
        "overwritten_baseline_or_context",
        "observation_only_not_authority",
    }:
        prescription = prescribe_recovery("missing_evidence")
    elif code == "snapshot_or_transaction_mismatch":
        prescription = prescribe_recovery("provenance_mismatch")
    elif code in {"ancestor_depth_exceeded", "circular_ancestor_evidence"}:
        prescription = prescribe_recovery("explicit_human_decision_required")
    else:
        prescription = prescribe_recovery("unsupported_legacy_state")
    return RecoveryContextDecision(
        admitted=False,
        bundle=None,
        refusal_code=code,
        missing_or_invalid=tuple(details),
        pricing=pricing,
        recovery=prescription,
        authority=False,
    )


def price_recovery_context_admission(
    bundle: OriginalRecoveryContextBundle,
    *,
    current_run_id: int,
    current_root_sha256: str | None = None,
    ancestor_record_sha256s: Sequence[str] = (),
    ancestor_run_ids: Sequence[int] = (),
    current_root_bytes: int = 4_096,
    intermediate_root_bytes: int = 4_096,
    result_part_count: int = 1,
    total_decoded_bytes: int = 16_384,
    startup_read_requests: int = 4,
    publication_write_requests: int = 4,
    readback_requests: int = 2,
    protected_tail_requests: int = 6,
    original_api_limit: int = 512,
    already_consumed_requests: int = 0,
    unknown_usage_requests: int = 0,
    active_reserved_requests: int = 0,
) -> RecoveryContextDecision:
    """Validate ancestor cycles, storage bounds, and request pricing pre-effect."""

    _positive_int(current_run_id, field="current_run_id")
    _positive_int(original_api_limit, field="original_api_limit")
    for field_name, num in (
        ("current_root_bytes", current_root_bytes),
        ("intermediate_root_bytes", intermediate_root_bytes),
        ("result_part_count", result_part_count),
        ("total_decoded_bytes", total_decoded_bytes),
        ("startup_read_requests", startup_read_requests),
        ("publication_write_requests", publication_write_requests),
        ("readback_requests", readback_requests),
        ("protected_tail_requests", protected_tail_requests),
        ("already_consumed_requests", already_consumed_requests),
        ("unknown_usage_requests", unknown_usage_requests),
        ("active_reserved_requests", active_reserved_requests),
    ):
        _non_negative_int(num, field=field_name)

    pricing = RecoveryBudgetPricing(
        startup_read_requests=startup_read_requests,
        ancestor_read_requests=len(ancestor_record_sha256s),
        result_part_read_requests=result_part_count,
        publication_write_requests=publication_write_requests,
        readback_requests=readback_requests,
        protected_tail_requests=protected_tail_requests,
        prior_committed_requests=(
            already_consumed_requests
            + unknown_usage_requests
            + active_reserved_requests
        ),
        original_api_limit=original_api_limit,
        current_root_bytes=current_root_bytes,
        intermediate_root_bytes=intermediate_root_bytes,
        total_decoded_bytes=total_decoded_bytes,
    )

    seen_hashes: set[str] = set()
    if current_root_sha256 is not None:
        seen_hashes.add(_hex64(current_root_sha256, field="current_root_sha256"))

    if (
        len(ancestor_record_sha256s) > MAX_RECOVERY_ANCESTOR_DEPTH
        or len(ancestor_run_ids) > MAX_RECOVERY_ANCESTOR_DEPTH
    ):
        return _refusal_decision(
            "ancestor_depth_exceeded",
            ("ancestor_depth_exceeds_32",),
            pricing=pricing,
        )

    for anc_hash in ancestor_record_sha256s:
        validated_hash = _hex64(anc_hash, field="ancestor_record_sha256")
        if validated_hash in seen_hashes:
            return _refusal_decision(
                "circular_ancestor_evidence",
                ("ancestor_record_sha256_cycle",),
                pricing=pricing,
            )
        seen_hashes.add(validated_hash)

    seen_runs: set[int] = {current_run_id}
    for anc_run in ancestor_run_ids:
        validated_run = _positive_int(anc_run, field="ancestor_run_id")
        if validated_run in seen_runs:
            return _refusal_decision(
                "circular_ancestor_evidence",
                ("ancestor_run_id_cycle",),
                pricing=pricing,
            )
        seen_runs.add(validated_run)

    if (
        result_part_count > MAX_RECOVERY_RESULT_PARTS
        or current_root_bytes > MAX_SESSION_ROOT_BYTES
        or intermediate_root_bytes > MAX_SESSION_ROOT_BYTES
        or total_decoded_bytes > MAX_DECODED_RESULT_BYTES
    ):
        return _refusal_decision(
            "root_capacity_exceeded",
            ("root_or_part_storage_ceiling_exceeded",),
            pricing=pricing,
        )

    if pricing.total_projected_requests > original_api_limit:
        return _refusal_decision(
            "budget_exhausted_before_effect",
            ("projected_physical_requests_exceed_original_api_limit",),
            pricing=pricing,
        )

    return RecoveryContextDecision(
        admitted=True,
        bundle=bundle,
        refusal_code=None,
        missing_or_invalid=(),
        pricing=pricing,
        recovery=prescribe_recovery(
            "eligible_recovery_awaiting_authorization",
            has_retained_result=True,
        ),
        authority=False,
    )


class RecoveryContextStore:
    """Private, HMAC-authenticated handoff store for original recovery bundles."""

    def __init__(
        self,
        directory: Path,
        *,
        key: bytes,
        ttl_seconds: int = DEFAULT_RECOVERY_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(key, bytes)
            or len(key) < 32
            or type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= MAX_RECOVERY_TTL_SECONDS
        ):
            raise ReviewInputError(
                "recovery context handoff key (>=32 bytes) or TTL is invalid"
            )
        self.directory = Path(directory)
        self._key = key
        self.ttl_seconds = ttl_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _signature(self, document: Mapping[str, Any]) -> str:
        return hmac.new(
            self._key,
            b"reviewsensei:original-recovery-context:v1\x00"
            + _canonical_bytes(document),
            hashlib.sha256,
        ).hexdigest()

    def _validate_directory(self) -> None:
        if self.directory.is_symlink():
            raise ReviewInputError("recovery context directory must not be a symlink")
        if self.directory.exists() and os.name == "posix":
            stat_info = self.directory.stat()
            if stat_info.st_uid != os.getuid() or stat_info.st_mode & 0o077:
                raise ReviewInputError("recovery context directory must be private")

    def save(self, bundle: OriginalRecoveryContextBundle) -> Path:
        if not isinstance(bundle, OriginalRecoveryContextBundle):
            raise ReviewInputError("bundle must be an OriginalRecoveryContextBundle")
        self._validate_directory()
        now = self.now()
        if now.tzinfo is None:
            raise ReviewInputError("recovery context clock must be timezone-aware")
        expires = now + timedelta(seconds=self.ttl_seconds)
        document = {
            "envelope_version": RECOVERY_HANDOFF_ENVELOPE_VERSION,
            "saved_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "bundle": bundle.to_dict(),
        }
        envelope = {
            "document": document,
            "authentication": self._signature(document),
        }
        encoded = _canonical_bytes(envelope)
        if len(encoded) > MAX_RECOVERY_CONTEXT_BUNDLE_BYTES:
            raise ReviewInputError("recovery context handoff envelope exceeds size cap")

        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.directory / f"{bundle.transaction_id}.json"
        if target.is_symlink():
            raise ReviewInputError("recovery context target must not be a symlink")

        existing_count = 1
        existing_bytes = len(encoded)
        for index, entry in enumerate(self.directory.iterdir()):
            if index >= 32:
                raise ReviewInputError("recovery context directory entry cap exceeded")
            if entry == target:
                continue
            if entry.suffix == ".json":
                if entry.is_symlink() or not entry.is_file():
                    raise ReviewInputError(
                        "recovery context directory entry is invalid"
                    )
                existing_count += 1
                existing_bytes += entry.stat().st_size
            if (
                existing_count > MAX_RECOVERY_HANDOFF_FILES
                or existing_bytes > MAX_RECOVERY_HANDOFF_TOTAL_BYTES
            ):
                raise ReviewInputError(
                    "recovery context handoff aggregate storage budget exhausted"
                )

        fd, temp_name = tempfile.mkstemp(prefix=".rcx-", dir=self.directory)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(target)
        finally:
            temp_path.unlink(missing_ok=True)
        return target

    def load_for_fresh_process(
        self,
        transaction_id: str,
        *,
        expected_repository: str,
        expected_pull_request: int,
        expected_base_sha: str,
        expected_head_sha: str,
        expected_policy_commit: str,
        expected_result_sha256: str,
        expected_baseline_digest: str | None = None,
        verify_baseline_match: bool = False,
        current_run_id: int = 1,
        current_root_sha256: str | None = None,
        ancestor_record_sha256s: Sequence[str] = (),
        ancestor_run_ids: Sequence[int] = (),
        current_root_bytes: int = 4_096,
        intermediate_root_bytes: int = 4_096,
        result_part_count: int = 1,
        total_decoded_bytes: int = 16_384,
        original_api_limit: int = 512,
        already_consumed_requests: int = 0,
        unknown_usage_requests: int = 0,
        active_reserved_requests: int = 0,
        ledger_expired: bool = False,
    ) -> RecoveryContextDecision:
        """Load, authenticate, and price original recovery context in a fresh process."""

        _hex64(transaction_id, field="transaction_id")
        self._validate_directory()
        path = self.directory / f"{transaction_id}.json"
        if not path.exists():
            return _refusal_decision(
                "absent_context",
                ("original_recovery_context_bundle",),
                ledger_expired=ledger_expired,
            )
        if path.is_symlink():
            return _refusal_decision(
                "tampered_authentication_or_digest", ("symlink_handoff_rejected",)
            )
        try:
            raw = path.read_bytes()
            if not raw or len(raw) > MAX_RECOVERY_CONTEXT_BUNDLE_BYTES:
                return _refusal_decision(
                    "truncated_or_corrupt_context", ("invalid_envelope_size",)
                )
            envelope = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _refusal_decision(
                "truncated_or_corrupt_context", ("truncated_or_invalid_json",)
            )

        if (
            not isinstance(envelope, Mapping)
            or set(envelope.keys()) != {"document", "authentication"}
            or not isinstance(envelope.get("authentication"), str)
            or not isinstance(envelope.get("document"), Mapping)
        ):
            return _refusal_decision(
                "truncated_or_corrupt_context", ("malformed_handoff_envelope",)
            )

        document = envelope["document"]
        expected_sig = self._signature(document)
        if not hmac.compare_digest(envelope["authentication"], expected_sig):
            return _refusal_decision(
                "tampered_authentication_or_digest", ("hmac_signature_mismatch",)
            )

        if (
            set(document.keys())
            != {
                "envelope_version",
                "saved_at",
                "expires_at",
                "bundle",
            }
            or document.get("envelope_version") != RECOVERY_HANDOFF_ENVELOPE_VERSION
        ):
            return _refusal_decision(
                "truncated_or_corrupt_context", ("unsupported_envelope_version",)
            )

        try:
            saved_at = datetime.fromisoformat(str(document["saved_at"]))
            expires_at = datetime.fromisoformat(str(document["expires_at"]))
        except ValueError:
            return _refusal_decision(
                "truncated_or_corrupt_context", ("invalid_timestamp",)
            )

        now = self.now()
        if (
            saved_at.tzinfo is None
            or expires_at.tzinfo is None
            or now.tzinfo is None
            or not saved_at <= now < expires_at
            or not 0
            < (expires_at - saved_at).total_seconds()
            <= MAX_RECOVERY_TTL_SECONDS
        ):
            return _refusal_decision(
                "expired_context",
                ("recovery_context_ttl_expired",),
                ledger_expired=ledger_expired,
            )

        try:
            bundle = OriginalRecoveryContextBundle.from_dict(document["bundle"])
        except RecoveryRefusalError as exc:
            detail_map = {
                UNSUPPORTED_V3_WRITER_REFUSAL_CODE: "caller-requests-v3",
                OBSERVATION_ONLY_REFUSAL_CODE: "read_only_observation",
                "tampered_authentication_or_digest": "bundle_sha256_mismatch",
            }
            return _refusal_decision(
                exc.refusal_code,
                (detail_map.get(exc.refusal_code, "invalid_bundle_fields"),),
            )
        except ReviewInputError:
            return _refusal_decision(
                "truncated_or_corrupt_context", ("invalid_bundle_fields",)
            )

        mismatches: list[str] = []
        if bundle.transaction_id != transaction_id:
            mismatches.append("transaction_id")
        if bundle.repository != expected_repository:
            mismatches.append("repository")
        if bundle.pull_request != expected_pull_request:
            mismatches.append("pull_request")
        if bundle.base_sha != expected_base_sha:
            mismatches.append("base_sha")
        if bundle.head_sha != expected_head_sha:
            mismatches.append("head_sha")
        if bundle.policy_commit != expected_policy_commit:
            mismatches.append("policy_commit")
        if bundle.accepted_result_sha256 != expected_result_sha256:
            mismatches.append("accepted_result_sha256")
        if mismatches:
            return _refusal_decision(
                "snapshot_or_transaction_mismatch", tuple(mismatches)
            )

        if verify_baseline_match and bundle.baseline_digest != expected_baseline_digest:
            return _refusal_decision(
                "overwritten_baseline_or_context", ("baseline_digest_overwritten",)
            )

        return price_recovery_context_admission(
            bundle,
            current_run_id=current_run_id,
            current_root_sha256=current_root_sha256,
            ancestor_record_sha256s=ancestor_record_sha256s,
            ancestor_run_ids=ancestor_run_ids,
            current_root_bytes=current_root_bytes,
            intermediate_root_bytes=intermediate_root_bytes,
            result_part_count=result_part_count,
            total_decoded_bytes=total_decoded_bytes,
            original_api_limit=original_api_limit,
            already_consumed_requests=already_consumed_requests,
            unknown_usage_requests=unknown_usage_requests,
            active_reserved_requests=active_reserved_requests,
        )
