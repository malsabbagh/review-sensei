"""Durable PR-wide review-session ledger for issue #136 C3/C5.

C1/C2 compute round admission from ``RoundSessionState``. That state does not
survive a fresh Actions job. C3 stores only bounded counters and CAS metadata.
It does not store source, prompts, findings, or provider output.

C5 enforces the C1 decision when a ledger is present: operator modes reserve
before inference and publication, skip unadmitted work, pause on a foreign
in-flight reservation, treat the same reservation as a duplicate, and charge
failed attempts. The cap never mints approval.

Session identity reuses the ADR 0042 / issue #38 pair ``repository`` +
``pull_request``. Head SHA, model, and policy digests do not name the session
and cannot reset the round limit.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import stat
import sys
import tempfile
from dataclasses import InitVar, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence, cast

from .bounded_evidence import (
    INLINE_ENCODING,
    MAX_PART_BYTES,
    MAX_PART_READS,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    PARTITION_ENCODING,
    ActivationTailPlan,
    AuthenticatedPart,
    EvidenceReadBudget,
    EvidenceTailTicket,
    NonResumableActivationError,
    TailDispatch,
    _operation_witness_matches,
    canonical_bytes,
    partition_evidence,
    read_inline_evidence,
    read_partitioned_evidence,
    stage_partitioned_evidence,
    validate_manifest,
)
from .convergence import (
    DEFAULT_MAX_FAILED_ATTEMPTS,
    DIAGNOSTIC_ROUND_CEILING,
    MAX_FAILED_ATTEMPTS,
    OPERATOR_REVIEW_MODES,
    ReviewConvergencePolicy,
    RoundAdmissionDecision,
    RoundSessionState,
    evaluate_round_admission,
)
from .coverage import FileCoverage, HunkCoverage
from .errors import ReviewInputError, ReviewSenseiError
from .history_association import (
    HistoryAssociation,
    _read_history_child,
    associate_history,
    history_payload,
    indexed_document,
    read_associated_history,
)
from .models import ReviewResult, ReviewTransaction
from .schemas import validate_public_document

if TYPE_CHECKING:
    from .baseline import ReviewBaseline
    from .rescan import AuthorizedRescan

PUBLIC_SCHEMA_VERSION = "1.0"
SESSION_LEDGER_ENV = "REVIEWSENSEI_SESSION_LEDGER"
DEFAULT_SESSION_TTL = timedelta(days=30)
# Keep a small lower bound when migrating an untrusted legacy expiry that is
# still in the future, so a writable v0.1 file cannot force an immediate reset.
MIN_SESSION_TTL = timedelta(minutes=1)
MAX_SESSION_TTL = timedelta(days=90)
# ADR 0066 compares 8 and 12 KiB against canonical checkpoint fixtures.
# Twelve KiB admits the 64-path repository-style fixture with duplicate path
# evidence, where eight does not. Keep the former 8 KiB record allowance for
# surrounding state; allocate less history for larger retained/escaped fields.
MAX_CONVERGENCE_HISTORY_BYTES = 12_288
# New caller accounting and retained rescan receipts share this root with
# decisions/history. Qualification covers the bounded 20-decision composition.
# This does not enlarge evidence parts, retention, or physical request limits.
MAX_SESSION_RECORD_BYTES = 32_768
MAX_ASSESSMENT_QUEUE_ROOT_BYTES = 8192
MAX_ASSESSMENT_QUEUE_SUMMARY_BYTES = 1024
MAX_CONVERGENCE_PROGRESS_ENTRIES = 3
SESSION_SHA256_PATTERN = r"^[a-f0-9]{64}$"
# This is a deliberately coarse structural ceiling, independent of the byte
# budgets above.  The publisher's review limits bound the real number of
# admitted findings; this envelope bound keeps an untrusted marker from
# driving an allocation without coupling the ledger to provider-specific
# limits or to the serialized record size.
MAX_DURABLE_BLOCKER_COUNT = 4_096
# Continuation grants are obsolete: no code path issues, consults, or consumes
# one.  Records written while the protocol existed still carry them, and an
# upgrade must keep that history and its integrity digest instead of failing
# the load, so the retained envelope stays bounded and readable.
MAX_STORED_CONTINUATION_GRANTS = 4
# GitHub comments include a bounded intro, JSON fence, and marker around the
# serialized record. Keep that framing allowance named and tied to the record
# bound so the two adapters cannot drift independently.
MAX_SESSION_COMMENT_BYTES = MAX_SESSION_RECORD_BYTES * 2
MAX_GENERATION = 2_147_483_647
RESERVATION_SLOTS = frozenset({"initial", "verification", "failed-attempt"})
_RESERVATION_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"
)
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_SHA256 = re.compile(SESSION_SHA256_PATTERN)
LOAD_STATUSES = frozenset(
    {"ok", "missing", "expired", "integrity-failed", "conflict", "migrated"}
)


class SessionLoadReason(str, Enum):
    """Stable reasons for a hosted session document load failure."""

    INTEGRITY_FAILED = "integrity-failed"
    CONFLICT = "conflict"


class SessionLoadError(ReviewInputError):
    """Typed load failure used by adapters instead of parsing error text."""

    def __init__(self, reason: SessionLoadReason, message: str) -> None:
        if not isinstance(reason, SessionLoadReason):
            raise ReviewInputError("session load reason is invalid")
        self.reason = reason
        super().__init__(message)


def _require_bool(value: object, *, label: str) -> None:
    if not isinstance(value, bool):
        raise ReviewInputError(f"{label} must be a boolean")


def _operator_paused(value: object) -> bool:
    _require_bool(value, label="operator_paused")
    return bool(value)


MAX_STORED_DISPOSITIONS = 4
MAX_DISPOSITIONS_PER_HEAD = 32
MAX_UNHEADED_DISPOSITIONS = 4
# Four maximum-size dispositions are about 9845 canonical bytes once non-ASCII
# reasons and actors are escaped. The bound stays above that historical record
# so an existing four-item ledger still loads. Shorter decisions can use the
# rest of the budget, up to the per-head and item caps.
MAX_DISPOSITION_BYTES = 10240
MAX_DISPOSITION_ITEMS = 256
MAX_DISPOSITION_REASON_BYTES = 512
MAX_DISPOSITION_ACTOR_BYTES = 256
_DISPOSITION_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{16,64}$")
_DISPOSITION_HEAD_RE = re.compile(r"^[a-f0-9]{40,64}$")
_DISPOSITION_ACTIONS = frozenset(
    {"dismiss", "defer", "accept-risk", "accept-coverage-uncertainty"}
)
_DISPOSITION_RESOLUTION_KINDS = frozenset(
    {
        "disputed",
        "deferred",
        "accepted_unresolved",
        "coverage_uncertainty_accepted",
    }
)
_DISPOSITION_TARGET_KINDS = frozenset({"finding", "coverage_uncertainty"})
_DIGEST_SHAPES = frozenset({"current", "operator-paused", "legacy"})
_CONVERGENCE_HISTORY_STATES = frozenset(
    {"completed", "invalidated", "recovery-required"}
)


def _stored_continuation_grants(value: object) -> tuple[dict[str, object], ...]:
    """Retain bounded, obsolete continuation-grant history verbatim.

    The grant protocol is gone: nothing issues, consults, or consumes a
    continuation grant.  A record written while it existed still carries one,
    and the re-serialized payload must stay byte-identical or its stored
    digest would no longer verify, so the history is kept as inert data.  The
    ``session-record`` schema owns the item shape; this helper only bounds the
    envelope and refuses anything that is not a plain string-keyed object.
    """

    if value is None:
        raise ReviewInputError("continuation grants must be an array")
    if (
        not isinstance(value, (list, tuple))
        or len(value) > MAX_STORED_CONTINUATION_GRANTS
    ):
        raise ReviewInputError("continuation grants exceed the configured bound")
    grants: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or any(
            not isinstance(key, str) for key in item
        ):
            raise ReviewInputError("continuation grant is invalid")
        grants.append(dict(item))
    return tuple(grants)


def _stored_assessment_queue(value: object) -> dict[str, object] | None:
    """Validate only C's dedicated root/summary, never queue payload semantics."""
    if value is None:
        return None
    normalized = json.loads(canonical_bytes(value))
    validate_public_document(normalized, "assessment-queue-root")
    if len(canonical_bytes(normalized)) > MAX_ASSESSMENT_QUEUE_ROOT_BYTES:
        raise ReviewInputError(
            "assessment queue root exceeds actual bounded allocation"
        )
    state_manifest = normalized["state_manifest"]
    if (
        isinstance(state_manifest, dict)
        and state_manifest.get("encoding") == INLINE_ENCODING
    ):
        read_inline_evidence(state_manifest, expected_binding=state_manifest["binding"])
        binding = cast(dict[str, object], state_manifest["binding"])
    else:
        manifest = validate_manifest(state_manifest)
        binding = cast(dict[str, object], manifest["binding"])
    if (
        binding["purpose"] != "queue"
        or binding["generation"] != normalized["inventory_generation"]
    ):
        raise ReviewInputError(
            "assessment queue inventory/domain binding does not match"
        )
    active = normalized["active_operation"]
    if active is not None:
        deadline = _parse_aware_datetime(
            active["deadline_at"], label="assessment deadline"
        )
        expires = _parse_aware_datetime(active["expires_at"], label="assessment expiry")
        if (
            deadline.utcoffset() != timedelta(0)
            or expires.utcoffset() != timedelta(0)
            or expires < deadline
        ):
            raise ReviewInputError(
                "assessment queue deadline/expiry must be ordered UTC instants"
            )
        if active["response_bytes"] + active["response_bytes_reserved"] > 1_048_576:
            raise ReviewInputError(
                "assessment actual and reserved response bytes exceed original ceiling"
            )
    summary = dict(normalized)
    summary["state_manifest"] = {}
    if len(canonical_bytes(summary)) > MAX_ASSESSMENT_QUEUE_SUMMARY_BYTES:
        raise ReviewInputError("assessment queue summary exceeds growth reserve")
    return normalized


def _stored_api_accounting(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    from .api_work import ApiRunAccounting

    return ApiRunAccounting.from_document(value).to_document()


def _stored_convergence_history(
    value: object,
) -> dict[str, object] | None:
    """Return one closed, bounded history envelope safe for durable storage."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReviewInputError("session convergence history is invalid")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        normalized = json.loads(encoded.decode("utf-8"))
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewInputError("session convergence history is invalid") from exc
    if len(encoded) > MAX_CONVERGENCE_HISTORY_BYTES:
        raise ReviewInputError(
            "session convergence history exceeds the configured bound"
        )
    if not isinstance(normalized, dict):
        raise ReviewInputError("session convergence history is invalid")
    allowed = {"state", "baseline", "progress", "provenance"}
    if set(normalized) - allowed or not {"state", "progress", "provenance"}.issubset(
        normalized
    ):
        raise ReviewInputError("session convergence history has unknown fields")
    state = normalized.get("state")
    if state not in _CONVERGENCE_HISTORY_STATES:
        raise ReviewInputError("session convergence history state is invalid")
    baseline = normalized.get("baseline")
    if baseline is not None:
        # Keep the in-process constructor as strict as the public schema.  A
        # record can be constructed without an untrusted-document round trip,
        # so schema validation alone cannot protect this boundary.
        from .baseline import baseline_from_history_document

        if (
            isinstance(baseline, dict)
            and baseline.get("encoding") == PARTITION_ENCODING
        ):
            validate_manifest(baseline)
        else:
            baseline_from_history_document(baseline)
    progress = normalized.get("progress")
    if (
        not isinstance(progress, list)
        or len(progress) > MAX_CONVERGENCE_PROGRESS_ENTRIES
    ):
        raise ReviewInputError("session history progress is invalid")
    for item in progress:
        if not isinstance(item, Mapping) or set(item) not in {
            frozenset({"event", "generation"}),
            frozenset({"event", "generation", "blocker_set_sha256", "blocker_count"}),
            frozenset(
                {
                    "event",
                    "generation",
                    "blocker_set_sha256",
                    "blocker_count",
                    "transaction_id",
                }
            ),
        }:
            raise ReviewInputError("session history progress item is invalid")
        if item.get("event") not in {"completed", "invalidated", "recovery-required"}:
            raise ReviewInputError("session history progress event is invalid")
        _require_bounded_int(
            item.get("generation"),
            label="session history progress generation",
            minimum=0,
            maximum=MAX_GENERATION,
        )
        if "blocker_set_sha256" in item:
            if item.get("event") != "completed":
                raise ReviewInputError("session history blocker set event is invalid")
            digest = item.get("blocker_set_sha256")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ReviewInputError("session history blocker set digest is invalid")
            _require_bounded_int(
                item.get("blocker_count"),
                label="session history blocker count",
                minimum=0,
                maximum=MAX_DURABLE_BLOCKER_COUNT,
            )
            if "transaction_id" in item:
                transaction_id = item.get("transaction_id")
                if not isinstance(transaction_id, str) or not _SHA256.fullmatch(
                    transaction_id
                ):
                    raise ReviewInputError(
                        "session history blocker transaction id is invalid"
                    )
    provenance = normalized.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {"ledger_digest"}:
        raise ReviewInputError("session history provenance is invalid")
    digest = provenance.get("ledger_digest")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ReviewInputError("session history ledger_digest is invalid")
    return cast(dict[str, object], normalized)


def blocker_set_digest(fingerprints: Sequence[str]) -> tuple[str, int]:
    """Return the digest and cardinality of admitted blocker fingerprints.

    ``fingerprints`` is a sequence of canonical finding-lifecycle identities:
    lowercase SHA-256 strings, not paths, prose, or provider output.  The
    strict input contract keeps persisted progress markers comparable across
    runtimes and prevents alternate encodings from creating false progress.
    """

    values = tuple(fingerprints)
    if any(
        not isinstance(value, str) or not _SHA256.fullmatch(value) for value in values
    ):
        raise ReviewInputError("blocker fingerprint is invalid")
    canonical = tuple(sorted(set(values)))
    return (
        hashlib.sha256(
            json.dumps(canonical, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        len(canonical),
    )


def convergence_progress_blocker_markers(
    history: Mapping[str, object] | None,
) -> tuple[tuple[str, int, str | None], ...]:
    """Return complete blocker markers, rejecting malformed metadata.

    Legacy lifecycle-only entries remain intentionally ignored, but an entry
    that starts claiming blocker metadata must be complete and well-typed.
    Failing closed here prevents a malformed in-memory or bypassed history
    from being converted into an empty window, which the no-progress detector
    treats as verified progress.
    """

    if not isinstance(history, Mapping):
        return ()
    if "progress" not in history:
        return ()
    progress = history.get("progress")
    if not isinstance(progress, list):
        raise ReviewInputError("session convergence progress is invalid")
    if len(progress) > MAX_CONVERGENCE_PROGRESS_ENTRIES:
        raise ReviewInputError(
            "session convergence progress exceeds the configured bound"
        )
    result: list[tuple[str, int, str | None]] = []
    for item in progress:
        if not isinstance(item, Mapping):
            raise ReviewInputError("session convergence progress item is invalid")
        has_digest = "blocker_set_sha256" in item
        has_count = "blocker_count" in item
        if not has_digest and not has_count:
            continue
        if not has_digest or not has_count:
            raise ReviewInputError("session convergence blocker marker is incomplete")
        if item.get("event") != "completed":
            raise ReviewInputError("session convergence blocker event is invalid")
        digest = item.get("blocker_set_sha256")
        count = item.get("blocker_count")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ReviewInputError("session convergence blocker digest is invalid")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ReviewInputError("session convergence blocker count is invalid")
        if count > MAX_DURABLE_BLOCKER_COUNT:
            raise ReviewInputError(
                "session convergence blocker count exceeds the configured bound"
            )
        transaction_id: str | None = None
        if "transaction_id" in item:
            transaction_id = item.get("transaction_id")
            if not isinstance(transaction_id, str) or not _SHA256.fullmatch(
                transaction_id
            ):
                raise ReviewInputError(
                    "session convergence blocker transaction id is invalid"
                )
        result.append((digest, count, transaction_id))
    return tuple(result)


def convergence_progress_blocker_sets(
    history: Mapping[str, object] | None,
) -> tuple[tuple[str, int], ...]:
    """Return complete blocker identities without dropping their order."""

    return tuple(
        (digest, count)
        for digest, count, _transaction_id in convergence_progress_blocker_markers(
            history
        )
    )


def _validate_convergence_history_baseline(
    value: Mapping[str, object] | None,
) -> None:
    """Enforce the state/baseline presence rule on an integrity-checked record.

    The rule runs after ``record_sha256`` verification so a tampered envelope
    still reports the integrity failure, while a forged-but-consistent
    document cannot smuggle a baseline through a non-completed state.
    """

    if value is None:
        return
    state = value.get("state")
    baseline = value.get("baseline")
    if state == "completed" and not isinstance(baseline, Mapping):
        raise ReviewInputError("completed session history requires a baseline")
    if state != "completed" and "baseline" in value:
        # The persisted history is closed: only a completed review may carry a
        # baseline, mirroring the public schema's presence rule so an
        # invalidated or recovery-required state cannot smuggle one through.
        raise ReviewInputError(
            "session history baseline is only valid for a completed state"
        )


def disposition_array_bytes(items: object) -> int:
    """Canonical JSON size of a dispositions array."""

    return len(
        json.dumps(
            list(items),  # type: ignore[call-overload]
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _stored_dispositions(
    value: object,
    *,
    snapshot: bool = False,
) -> tuple[dict[str, object], ...]:
    """Validate and copy persisted human finding dispositions."""

    if value is None:
        raise ReviewInputError("session dispositions must be an array")
    if not isinstance(value, (list, tuple)):
        raise ReviewInputError("session dispositions must be an array")
    if len(value) > (MAX_DURABLE_BLOCKER_COUNT if snapshot else MAX_DISPOSITION_ITEMS):
        raise ReviewInputError("session dispositions exceed the configured bound")
    normalized: list[dict[str, object]] = []
    allowed = {
        "fingerprint",
        "action",
        "reason",
        "actor",
        "head_sha",
        "expires_at",
        "actor_role",
        "finding_reference",
        "recorded_at",
        "policy_digest",
        "provenance",
        "resolution_kind",
        "target_kind",
        "incomplete_coverage",
    }
    for item in value:
        if not isinstance(item, Mapping) or set(item) - allowed:
            raise ReviewInputError("session disposition is invalid")
        fingerprint = item.get("fingerprint")
        if not isinstance(
            fingerprint, str
        ) or not _DISPOSITION_FINGERPRINT_RE.fullmatch(fingerprint):
            raise ReviewInputError("session disposition fingerprint is invalid")
        action = item.get("action")
        if not isinstance(action, str) or action not in _DISPOSITION_ACTIONS:
            raise ReviewInputError("session disposition action is invalid")
        reason = item.get("reason")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason.encode("utf-8")) > MAX_DISPOSITION_REASON_BYTES
            or not reason.isprintable()
        ):
            raise ReviewInputError("session disposition reason is invalid")
        actor = item.get("actor")
        if (
            not isinstance(actor, str)
            or not actor.strip()
            or len(actor.encode("utf-8")) > MAX_DISPOSITION_ACTOR_BYTES
            or not actor.isprintable()
        ):
            raise ReviewInputError("session disposition actor is invalid")
        head_sha = item.get("head_sha")
        if head_sha is not None and (
            not isinstance(head_sha, str)
            or not _DISPOSITION_HEAD_RE.fullmatch(head_sha)
        ):
            raise ReviewInputError("session disposition head_sha is invalid")
        expires_at = item.get("expires_at")
        if expires_at is not None:
            if not isinstance(expires_at, str):
                raise ReviewInputError("session disposition expires_at is invalid")
            _parse_aware_datetime(expires_at, label="session disposition expires_at")
        entry: dict[str, object] = {
            "fingerprint": fingerprint,
            "action": action,
            "reason": reason.strip(),
            "actor": actor,
            "head_sha": head_sha,
            "expires_at": expires_at,
        }
        actor_role = item.get("actor_role")
        if actor_role is not None:
            if (
                not isinstance(actor_role, str)
                or not actor_role.strip()
                or len(actor_role.encode("utf-8")) > 64
                or not actor_role.isprintable()
            ):
                raise ReviewInputError("session disposition actor_role is invalid")
            entry["actor_role"] = actor_role.strip()
        finding_reference = item.get("finding_reference")
        if finding_reference is not None:
            if not isinstance(finding_reference, str) or not re.fullmatch(
                r"^(?:RS-)?[A-Fa-f0-9]{6,64}$", finding_reference
            ):
                raise ReviewInputError(
                    "session disposition finding_reference is invalid"
                )
            entry["finding_reference"] = finding_reference
        recorded_at = item.get("recorded_at")
        if recorded_at is not None:
            if not isinstance(recorded_at, str):
                raise ReviewInputError("session disposition recorded_at is invalid")
            _parse_aware_datetime(recorded_at, label="session disposition recorded_at")
            entry["recorded_at"] = recorded_at
        policy_digest = item.get("policy_digest")
        if policy_digest is not None:
            if not isinstance(policy_digest, str) or not _SHA256.fullmatch(
                policy_digest
            ):
                raise ReviewInputError("session disposition policy_digest is invalid")
            entry["policy_digest"] = policy_digest
        provenance = item.get("provenance")
        if provenance is not None:
            if (
                not isinstance(provenance, str)
                or not provenance.strip()
                or len(provenance.encode("utf-8")) > 256
                or not provenance.isprintable()
            ):
                raise ReviewInputError("session disposition provenance is invalid")
            entry["provenance"] = provenance.strip()
        resolution_kind = item.get("resolution_kind")
        if resolution_kind is not None:
            if (
                not isinstance(resolution_kind, str)
                or resolution_kind not in _DISPOSITION_RESOLUTION_KINDS
            ):
                raise ReviewInputError("session disposition resolution_kind is invalid")
            entry["resolution_kind"] = resolution_kind
        target_kind = item.get("target_kind")
        if target_kind is not None:
            if (
                not isinstance(target_kind, str)
                or target_kind not in _DISPOSITION_TARGET_KINDS
            ):
                raise ReviewInputError("session disposition target_kind is invalid")
            entry["target_kind"] = target_kind
        if "incomplete_coverage" in item and item["incomplete_coverage"] is not None:
            raw_coverage = item["incomplete_coverage"]
            if not isinstance(raw_coverage, (list, tuple)) or len(raw_coverage) > 256:
                raise ReviewInputError(
                    "session disposition incomplete_coverage is invalid"
                )
            coverage_items: list[str] = []
            for cov in raw_coverage:
                if (
                    not isinstance(cov, str)
                    or not cov.strip()
                    or len(cov.encode("utf-8")) > 256
                    or not cov.isprintable()
                ):
                    raise ReviewInputError(
                        "session disposition incomplete_coverage is invalid"
                    )
                coverage_items.append(cov.strip())
            entry["incomplete_coverage"] = coverage_items
        normalized.append(entry)
    if not snapshot and disposition_array_bytes(normalized) > MAX_DISPOSITION_BYTES:
        raise ReviewInputError("session dispositions exceed the configured bound")
    seen: set[tuple[str, str | None]] = set()
    per_head: dict[str, int] = {}
    unheaded = 0
    for item in normalized:
        fingerprint = item["fingerprint"]
        head_sha = item["head_sha"]
        assert isinstance(fingerprint, str)
        key = (fingerprint, head_sha if isinstance(head_sha, str) else None)
        if key in seen:
            raise ReviewInputError("session disposition is duplicated")
        seen.add(key)
        if key[1] is None:
            unheaded += 1
        else:
            per_head[key[1]] = per_head.get(key[1], 0) + 1
    if unheaded > MAX_UNHEADED_DISPOSITIONS or (
        not snapshot
        and any(count > MAX_DISPOSITIONS_PER_HEAD for count in per_head.values())
    ):
        raise ReviewInputError("session dispositions exceed the configured bound")
    return tuple(normalized)


def _require_bounded_int(
    value: object, *, label: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewInputError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ReviewInputError(f"{label} is out of bounds")
    return value


def _aware_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_aware_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not _DATETIME_RE.fullmatch(value):
        raise ReviewInputError(f"{label} is invalid")
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReviewInputError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewInputError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _aware_now(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _digest_payload(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reservation_id(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _RESERVATION_ID_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{label} is invalid")
    return value


def migrate_session_document(value: Mapping[str, object]) -> dict[str, object]:
    """Rewrite a bounded legacy session document into v1 field names.

    The only supported legacy shape is ``schema_version="0.1"`` with
    ``pull_request_number`` and no integrity digest. Unknown versions fail
    closed. Callers rehash after migration; GitHub-backed loads never migrate.
    """

    if not isinstance(value, Mapping):
        raise ReviewInputError("session record is invalid")
    version = value.get("schema_version")
    if version == "1.0":
        return dict(value)
    if version != "0.1":
        raise ReviewInputError("session record schema_version is unsupported")
    if "record_sha256" in value:
        raise ReviewInputError("legacy session record must not include record_sha256")
    created_at = value.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ReviewInputError("legacy session record requires created_at")
    pull_request = value.get("pull_request", value.get("pull_request_number"))
    migrated = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "repository": value.get("repository"),
        "pull_request": pull_request,
        "repository_id": value.get("repository_id"),
        "completed_initial_reviews": value.get("completed_initial_reviews", 0),
        "completed_verification_rounds": value.get("completed_verification_rounds", 0),
        "failed_attempts": value.get("failed_attempts", 0),
        "generation": value.get("generation", 0),
        "reservation_id": value.get("reservation_id"),
        "reserved_slot": value.get("reserved_slot"),
        "last_committed_reservation_id": value.get("last_committed_reservation_id"),
        "created_at": created_at,
        "updated_at": value.get("updated_at", value.get("created_at")),
        "expires_at": value.get("expires_at"),
        "operator_paused": False,
        "dispositions": value.get("dispositions", []),
    }
    forbidden = set(value) - {
        "schema_version",
        "repository",
        "pull_request",
        "pull_request_number",
        "repository_id",
        "completed_initial_reviews",
        "completed_verification_rounds",
        "failed_attempts",
        "generation",
        "reservation_id",
        "reserved_slot",
        "last_committed_reservation_id",
        "created_at",
        "updated_at",
        "expires_at",
        "dispositions",
    }
    if forbidden:
        raise ReviewInputError("legacy session record has unknown fields")
    return migrated


def _stored_reservation_owner(value: object) -> Mapping[str, object] | None:
    """Bounded broker-authenticated ownership, omitted for older records."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"run_id", "head_sha"}:
        raise ReviewInputError("session reservation owner is invalid")
    run_id, head_sha = value["run_id"], value["head_sha"]
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", run_id) is None
        or not isinstance(head_sha, str)
        or re.fullmatch(r"[a-f0-9]{40}", head_sha) is None
    ):
        raise ReviewInputError("session reservation owner is invalid")
    return dict(value)


@dataclass(frozen=True)
class SessionIdentity:
    """PR-wide ledger identity. Head SHA is not part of the key."""

    repository: str
    pull_request: int
    repository_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not _REPOSITORY_RE.fullmatch(
            self.repository
        ):
            raise ReviewInputError("session repository is invalid")
        _require_bounded_int(
            self.pull_request, label="pull_request", minimum=1, maximum=2_147_483_647
        )
        if self.repository_id is not None:
            _require_bounded_int(
                self.repository_id,
                label="repository_id",
                minimum=1,
                maximum=2_147_483_647,
            )


@dataclass(frozen=True)
class SessionRecord:
    """Bounded durable counters for one pull-request session."""

    repository: str
    pull_request: int
    repository_id: int | None
    completed_initial_reviews: int
    completed_verification_rounds: int
    failed_attempts: int
    generation: int
    reservation_id: str | None
    reserved_slot: str | None
    last_committed_reservation_id: str | None
    created_at: str
    updated_at: str
    expires_at: str
    record_sha256: str
    operator_paused: bool = False
    failed_attempts_head_sha: str | None = None
    dispositions: tuple[Mapping[str, object], ...] = ()
    continuation_grants: tuple[Mapping[str, object], ...] = ()
    transaction: ReviewTransaction | None = None
    convergence_history: Mapping[str, object] | None = None
    reservation_owner: Mapping[str, object] | None = None
    rescan_state: Mapping[str, object] | None = None
    assessment_queue: Mapping[str, object] | None = None
    disposition_snapshot: Mapping[str, object] | None = None
    api_run_accounting: Mapping[str, object] | None = None
    historical_assessments: Mapping[str, object] | None = None
    # The shape is selected only while loading an existing untrusted document;
    # it is not part of the public record or its equality contract.
    _digest_shape_input: InitVar[str] = "current"
    _digest_shape: str = field(default="current", init=False, repr=False, compare=False)

    def __post_init__(self, _digest_shape_input: str) -> None:
        object.__setattr__(self, "_digest_shape", _digest_shape_input)
        SessionIdentity(
            repository=self.repository,
            pull_request=self.pull_request,
            repository_id=self.repository_id,
        )
        for label in (
            "completed_initial_reviews",
            "completed_verification_rounds",
        ):
            _require_bounded_int(
                getattr(self, label),
                label=label,
                minimum=0,
                maximum=DIAGNOSTIC_ROUND_CEILING,
            )
        _require_bounded_int(
            self.failed_attempts,
            label="failed_attempts",
            minimum=0,
            maximum=MAX_FAILED_ATTEMPTS,
        )
        if (
            self.failed_attempts_head_sha is not None
            and not _DISPOSITION_HEAD_RE.fullmatch(self.failed_attempts_head_sha)
        ):
            raise ReviewInputError("failed_attempts_head_sha is invalid")
        _require_bounded_int(
            self.generation, label="generation", minimum=0, maximum=MAX_GENERATION
        )
        object.__setattr__(
            self,
            "reservation_id",
            _reservation_id(self.reservation_id, label="reservation_id"),
        )
        object.__setattr__(
            self,
            "last_committed_reservation_id",
            _reservation_id(
                self.last_committed_reservation_id,
                label="last_committed_reservation_id",
            ),
        )
        if (
            self.reservation_id is not None
            and self.reservation_id == self.last_committed_reservation_id
        ):
            raise ReviewInputError(
                "reservation_id and last_committed_reservation_id must differ"
            )
        if (
            self.reserved_slot is not None
            and self.reserved_slot not in RESERVATION_SLOTS
        ):
            raise ReviewInputError("reserved_slot is invalid")
        if (self.reservation_id is None) != (self.reserved_slot is None):
            raise ReviewInputError("reservation_id and reserved_slot must be paired")
        object.__setattr__(
            self, "reservation_owner", _stored_reservation_owner(self.reservation_owner)
        )
        if self.reservation_owner is not None:
            if self.reservation_id is None or self._digest_shape != "current":
                raise ReviewInputError(
                    "reservation owner requires a current held reservation"
                )
            if (
                self.transaction is not None
                and self.transaction.head_sha != self.reservation_owner["head_sha"]
            ):
                raise ReviewInputError(
                    "reservation owner head does not match transaction"
                )
        created = _parse_aware_datetime(self.created_at, label="created_at")
        updated = _parse_aware_datetime(self.updated_at, label="updated_at")
        expires = _parse_aware_datetime(self.expires_at, label="expires_at")
        if updated < created:
            raise ReviewInputError("session updated_at precedes created_at")
        if expires <= created:
            raise ReviewInputError("session expires_at must be after created_at")
        if expires - created > MAX_SESSION_TTL:
            raise ReviewInputError("session ttl exceeds the configured bound")
        _require_bool(self.operator_paused, label="operator_paused")
        normalized_dispositions = _stored_dispositions(self.dispositions)
        object.__setattr__(self, "dispositions", normalized_dispositions)
        from .disposition_snapshot import normalize_snapshot, validate_snapshot_identity

        snapshot = normalize_snapshot(self.disposition_snapshot)
        object.__setattr__(self, "disposition_snapshot", snapshot)
        if snapshot is not None:
            if self.dispositions or self._digest_shape != "current":
                raise ReviewInputError(
                    "disposition snapshot requires exclusive current authority"
                )
            validate_snapshot_identity(
                snapshot,
                repository=self.repository,
                repository_id=self.repository_id,
                pull_request=self.pull_request,
                generation=self.generation,
            )
        object.__setattr__(
            self,
            "continuation_grants",
            _stored_continuation_grants(self.continuation_grants),
        )
        object.__setattr__(
            self,
            "convergence_history",
            _stored_convergence_history(self.convergence_history),
        )
        object.__setattr__(
            self, "api_run_accounting", _stored_api_accounting(self.api_run_accounting)
        )
        if self.api_run_accounting is not None:
            if self._digest_shape != "current":
                raise ReviewInputError(
                    "API accounting requires the current digest shape"
                )
            if (
                cast(Mapping[str, object], self.api_run_accounting["identity"])[
                    "repository_id"
                ]
                != self.repository_id
            ):
                raise ReviewInputError("API accounting repository identity conflicts")
            from .api_work import ApiRunAccounting

            terminal = ApiRunAccounting.from_document(
                self.api_run_accounting
            ).terminal_transaction
            if terminal is not None and (
                self.transaction is None
                or self.transaction.transaction_id != terminal
                or self.transaction.phase
                not in {"publication_succeeded", "publication_suppressed"}
            ):
                raise ReviewInputError("API accounting terminal transaction conflicts")
        if self.transaction is not None:
            if not isinstance(self.transaction, ReviewTransaction):
                raise ReviewInputError("session transaction is invalid")
            if self._digest_shape != "current":
                raise ReviewInputError(
                    "session transaction requires the current digest shape"
                )
            if (
                self.transaction.repository != self.repository
                or self.transaction.pull_request != self.pull_request
            ):
                raise ReviewInputError("session transaction identity does not match")
            if self.transaction.phase == "analysis" and (
                self.reservation_id != self.transaction.reservation_id
                or self.reserved_slot is None
            ):
                raise ReviewInputError("analysis transaction must own its reservation")
            if self.transaction.phase != "analysis" and (
                self.reservation_id is not None
                or self.last_committed_reservation_id != self.transaction.reservation_id
            ):
                raise ReviewInputError(
                    "completed transaction reservation state is invalid"
                )
        if self._digest_shape not in _DIGEST_SHAPES:
            raise ReviewInputError("session record digest shape is invalid")
        if self._digest_shape == "legacy" and (
            self.operator_paused
            or self.dispositions
            or self.continuation_grants
            or self.convergence_history is not None
        ):
            raise ReviewInputError("legacy session record has C6 fields")
        if self._digest_shape == "operator-paused" and (
            self.dispositions or self.continuation_grants
        ):
            raise ReviewInputError("operator-paused session record has C6 fields")
        if (
            self._digest_shape != "current"
            and self.failed_attempts_head_sha is not None
        ):
            # Only the current payload covers the per-head attempt scope, so a
            # record carrying one must never be written under an older shape.
            raise ReviewInputError(
                "session failed-attempt head requires the current digest shape"
            )
        if self._digest_shape != "current" and self.convergence_history is not None:
            # Only the current payload covers the convergence history, so a
            # record carrying one must never be written under the legacy or
            # operator-paused digest shape. Otherwise an in-place upgrade of an
            # older record would re-serialize the envelope while keeping a
            # digest computed without it.
            raise ReviewInputError(
                "session convergence history requires the current digest shape"
            )
        if self.rescan_state is not None:
            from .rescan import validate_rescan_state

            object.__setattr__(
                self, "rescan_state", validate_rescan_state(self.rescan_state)
            )
            receipts = self.rescan_state["receipts"]
            assert isinstance(receipts, list)
            if any(
                item["source"]["repository"] != self.repository
                or item["source"]["pull_request"] != self.pull_request
                for item in receipts
            ):
                raise ReviewInputError("rescan source does not match session identity")
            if self._digest_shape != "current":
                raise ReviewInputError("rescan state requires current digest shape")
        object.__setattr__(
            self, "assessment_queue", _stored_assessment_queue(self.assessment_queue)
        )
        if self.assessment_queue is not None:
            if self._digest_shape != "current":
                raise ReviewInputError(
                    "assessment queue requires current authenticated digest shape"
                )
            manifest = self.assessment_queue["state_manifest"]
            assert isinstance(manifest, dict)
            binding = manifest["binding"]
            assert isinstance(binding, dict)
            if (
                binding["repository"] != self.repository
                or binding["pull_request"] != self.pull_request
                or binding["repository_id"] != self.repository_id
                or binding["generation"] > self.generation
            ):
                raise ReviewInputError(
                    "assessment queue does not match session identity/generation"
                )
        from .historical_assessments import normalize_hold

        normalized_hold = normalize_hold(self.historical_assessments)
        object.__setattr__(self, "historical_assessments", normalized_hold)
        if normalized_hold is not None:
            receipt = normalized_hold["source"]["completion"]
            if self._digest_shape != "current" or (
                receipt["repository"],
                receipt["repository_id"],
                receipt["pull_request"],
            ) != (self.repository, self.repository_id, self.pull_request):
                raise ReviewInputError(
                    "historical assessment session identity conflicts"
                )
        expected_payload = {
            "current": self._payload,
            "operator-paused": self._payload_without_dispositions,
            "legacy": self._legacy_payload,
        }[self._digest_shape]()
        if self.record_sha256 != _digest_payload(expected_payload):
            raise ReviewInputError("session record integrity check failed")
        _validate_convergence_history_baseline(self.convergence_history)
        if (
            len(
                json.dumps(
                    self.to_dict(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            )
            > MAX_SESSION_RECORD_BYTES
        ):
            raise ReviewInputError("session record exceeds the configured size limit")

    def _payload_digest(self) -> str:
        payload = self._payload()
        return _digest_payload(payload)

    def _payload(self) -> dict[str, object]:
        payload = self._legacy_payload()
        payload["operator_paused"] = self.operator_paused
        payload["dispositions"] = list(self.dispositions)
        if self.failed_attempts_head_sha is not None:
            payload["failed_attempts_head_sha"] = self.failed_attempts_head_sha
        if self.continuation_grants:
            payload["continuation_grants"] = list(self.continuation_grants)
        if self.reservation_owner is not None:
            payload["reservation_owner"] = dict(self.reservation_owner)
        if self.transaction is not None:
            payload["transaction"] = self.transaction.to_dict()
        if self.convergence_history is not None:
            payload["convergence_history"] = dict(self.convergence_history)
        if self.rescan_state is not None:
            payload["rescan_state"] = dict(self.rescan_state)
        if self.assessment_queue is not None:
            payload["assessment_queue"] = dict(self.assessment_queue)
        if self.disposition_snapshot is not None:
            payload["disposition_snapshot"] = dict(self.disposition_snapshot)
        if self.api_run_accounting is not None:
            payload["api_run_accounting"] = dict(self.api_run_accounting)
        if self.historical_assessments is not None:
            payload["historical_assessments"] = dict(self.historical_assessments)
        return payload

    def _payload_without_dispositions(self) -> dict[str, object]:
        payload = self._legacy_payload()
        payload["operator_paused"] = self.operator_paused
        if self.transaction is not None:
            payload["transaction"] = self.transaction.to_dict()
        return payload

    def _legacy_payload(self) -> dict[str, object]:
        return {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "repository": self.repository,
            "pull_request": self.pull_request,
            "repository_id": self.repository_id,
            "completed_initial_reviews": self.completed_initial_reviews,
            "completed_verification_rounds": self.completed_verification_rounds,
            "failed_attempts": self.failed_attempts,
            "generation": self.generation,
            "reservation_id": self.reservation_id,
            "reserved_slot": self.reserved_slot,
            "last_committed_reservation_id": self.last_committed_reservation_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def _construct(
        cls,
        *,
        repository: str,
        pull_request: int,
        repository_id: int | None,
        completed_initial_reviews: int,
        completed_verification_rounds: int,
        failed_attempts: int,
        generation: int,
        reservation_id: str | None,
        reserved_slot: str | None,
        last_committed_reservation_id: str | None,
        created_at: str,
        updated_at: str,
        expires_at: str,
        operator_paused: bool = False,
        failed_attempts_head_sha: str | None = None,
        dispositions: Sequence[Mapping[str, object]] = (),
        continuation_grants: Sequence[Mapping[str, object]] = (),
        transaction: ReviewTransaction | None = None,
        convergence_history: Mapping[str, object] | None = None,
        reservation_owner: Mapping[str, object] | None = None,
        rescan_state: Mapping[str, object] | None = None,
        assessment_queue: Mapping[str, object] | None = None,
        disposition_snapshot: Mapping[str, object] | None = None,
        api_run_accounting: Mapping[str, object] | None = None,
        historical_assessments: Mapping[str, object] | None = None,
    ) -> "SessionRecord":
        normalized_dispositions = _stored_dispositions(dispositions)
        normalized_grants = _stored_continuation_grants(continuation_grants)
        payload = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "repository": repository,
            "pull_request": pull_request,
            "repository_id": repository_id,
            "completed_initial_reviews": completed_initial_reviews,
            "completed_verification_rounds": completed_verification_rounds,
            "failed_attempts": failed_attempts,
            "generation": generation,
            "reservation_id": reservation_id,
            "reserved_slot": reserved_slot,
            "last_committed_reservation_id": last_committed_reservation_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "expires_at": expires_at,
            "operator_paused": operator_paused,
            "dispositions": list(normalized_dispositions),
        }
        if failed_attempts_head_sha is not None:
            payload["failed_attempts_head_sha"] = failed_attempts_head_sha
        if normalized_grants:
            payload["continuation_grants"] = list(normalized_grants)
        normalized_owner = _stored_reservation_owner(reservation_owner)
        if normalized_owner is not None:
            payload["reservation_owner"] = dict(normalized_owner)
        if transaction is not None:
            payload["transaction"] = transaction.to_dict()
        normalized_history = _stored_convergence_history(convergence_history)
        if normalized_history is not None:
            payload["convergence_history"] = normalized_history
        if rescan_state is not None:
            payload["rescan_state"] = dict(rescan_state)
        normalized_queue = _stored_assessment_queue(assessment_queue)
        if normalized_queue is not None:
            payload["assessment_queue"] = normalized_queue
        from .disposition_snapshot import normalize_snapshot

        normalized_snapshot = normalize_snapshot(disposition_snapshot)
        normalized_api = _stored_api_accounting(api_run_accounting)
        from .historical_assessments import normalize_hold

        normalized_hold = normalize_hold(historical_assessments)
        if normalized_hold is not None:
            payload["historical_assessments"] = normalized_hold
        if normalized_api is not None:
            payload["api_run_accounting"] = normalized_api
        if normalized_snapshot is not None:
            payload["disposition_snapshot"] = normalized_snapshot
        if (
            len(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            > MAX_SESSION_RECORD_BYTES
        ):
            raise ReviewInputError("session record exceeds the configured size limit")
        return cls(
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
            completed_initial_reviews=completed_initial_reviews,
            completed_verification_rounds=completed_verification_rounds,
            failed_attempts=failed_attempts,
            generation=generation,
            reservation_id=reservation_id,
            reserved_slot=reserved_slot,
            last_committed_reservation_id=last_committed_reservation_id,
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
            record_sha256=_digest_payload(payload),
            operator_paused=operator_paused,
            failed_attempts_head_sha=failed_attempts_head_sha,
            dispositions=normalized_dispositions,
            continuation_grants=normalized_grants,
            transaction=transaction,
            convergence_history=normalized_history,
            reservation_owner=normalized_owner,
            rescan_state=rescan_state,
            assessment_queue=normalized_queue,
            disposition_snapshot=normalized_snapshot,
            api_run_accounting=normalized_api,
            historical_assessments=normalized_hold,
        )

    def to_dict(self) -> dict[str, object]:
        payload = {
            "current": self._payload,
            "operator-paused": self._payload_without_dispositions,
            "legacy": self._legacy_payload,
        }[self._digest_shape]()
        payload["record_sha256"] = self.record_sha256
        return payload

    def identity(self) -> SessionIdentity:
        return SessionIdentity(
            repository=self.repository,
            pull_request=self.pull_request,
            repository_id=self.repository_id,
        )

    def to_round_state(
        self,
        *,
        head_sha: str | None = None,
        policy_digest: str | None = None,
        **flags: bool,
    ) -> RoundSessionState:
        effective_failed = failed_attempts_for_head(self, head_sha)
        target_head = head_sha or self.failed_attempts_head_sha
        if effective_failed < MAX_FAILED_ATTEMPTS and (
            has_unconsumed_successor_grant(
                self, target_head, policy_digest=policy_digest
            )
            is not None
            or has_unconsumed_irrecoverable_transaction_grant(
                self, target_head, policy_digest=policy_digest
            )
            is not None
        ):
            effective_failed = 0
        return RoundSessionState(
            completed_initial_reviews=self.completed_initial_reviews,
            completed_verification_rounds=self.completed_verification_rounds,
            failed_attempts=effective_failed,
            same_head_duplicate=bool(flags.get("same_head_duplicate", False)),
            publication_recovery=bool(flags.get("publication_recovery", False)),
            transport_or_structural_retry=bool(
                flags.get("transport_or_structural_retry", False)
            ),
            no_progress=bool(flags.get("no_progress", False)),
            coverage_complete=bool(flags.get("coverage_complete", False)),
            independently_approval_eligible=bool(
                flags.get("independently_approval_eligible", False)
            ),
            latest_head_reviewed=bool(flags.get("latest_head_reviewed", False)),
            paused=bool(flags.get("paused", False)),
        )

    def expired(self, *, now: datetime | None = None) -> bool:
        return _parse_aware_datetime(self.expires_at, label="expires_at") <= _aware_now(
            now
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SessionRecord":
        if not isinstance(value, Mapping):
            raise ReviewInputError("session record is invalid")
        has_operator_paused = "operator_paused" in value
        has_dispositions = "dispositions" in value
        has_grants = "continuation_grants" in value
        has_transaction = "transaction" in value
        has_history = "convergence_history" in value
        has_owner = "reservation_owner" in value
        has_queue = "assessment_queue" in value
        has_snapshot = "disposition_snapshot" in value
        has_api_accounting = "api_run_accounting" in value
        if (
            "historical_assessments" in value
            and value["historical_assessments"] is None
        ):
            raise ReviewInputError(
                "historical assessments cannot be a null absence marker"
            )
        if has_api_accounting and value["api_run_accounting"] is None:
            raise ReviewInputError("API accounting cannot be a null absence marker")
        if has_snapshot and value["disposition_snapshot"] is None:
            raise ReviewInputError(
                "disposition snapshot cannot be a null absence marker"
            )
        if has_queue and value["assessment_queue"] is None:
            raise ReviewInputError("assessment queue cannot be a null absence marker")
        has_attempt_head = value.get("failed_attempts_head_sha") is not None
        if has_dispositions and not has_operator_paused:
            raise ReviewInputError(
                "session record dispositions require operator_paused"
            )
        digest_shape = (
            "current"
            if has_dispositions
            or has_grants
            or has_transaction
            or has_history
            or has_attempt_head
            or has_owner
            or has_queue
            or has_snapshot
            or has_api_accounting
            or "quarantine_lineage" in value
            or "publication_staging" in value
            or "rescan_state" in value
            or "historical_assessments" in value
            else "operator-paused"
            if has_operator_paused
            else "legacy"
        )
        transaction_value = value.get("transaction")
        transaction = (
            ReviewTransaction.from_dict(transaction_value)
            if isinstance(transaction_value, Mapping)
            else None
        )
        if transaction_value is not None and transaction is None:
            raise ReviewInputError("session transaction is invalid")
        record = cls(
            repository=str(value.get("repository", "")),
            pull_request=_require_bounded_int(
                value.get("pull_request"),
                label="pull_request",
                minimum=1,
                maximum=2_147_483_647,
            ),
            repository_id=(
                None
                if value.get("repository_id") is None
                else _require_bounded_int(
                    value.get("repository_id"),
                    label="repository_id",
                    minimum=1,
                    maximum=2_147_483_647,
                )
            ),
            completed_initial_reviews=_require_bounded_int(
                value.get("completed_initial_reviews", 0),
                label="completed_initial_reviews",
                minimum=0,
                maximum=DIAGNOSTIC_ROUND_CEILING,
            ),
            completed_verification_rounds=_require_bounded_int(
                value.get("completed_verification_rounds", 0),
                label="completed_verification_rounds",
                minimum=0,
                maximum=DIAGNOSTIC_ROUND_CEILING,
            ),
            failed_attempts=_require_bounded_int(
                value.get("failed_attempts", 0),
                label="failed_attempts",
                minimum=0,
                maximum=MAX_FAILED_ATTEMPTS,
            ),
            generation=_require_bounded_int(
                value.get("generation", 0),
                label="generation",
                minimum=0,
                maximum=MAX_GENERATION,
            ),
            reservation_id=value.get("reservation_id"),  # type: ignore[arg-type]
            reserved_slot=value.get("reserved_slot"),  # type: ignore[arg-type]
            last_committed_reservation_id=value.get("last_committed_reservation_id"),  # type: ignore[arg-type]
            created_at=str(value.get("created_at", "")),
            updated_at=str(value.get("updated_at", "")),
            expires_at=str(value.get("expires_at", "")),
            record_sha256=str(value.get("record_sha256", "")),
            operator_paused=_operator_paused(value.get("operator_paused", False)),
            failed_attempts_head_sha=(
                None
                if value.get("failed_attempts_head_sha") is None
                else str(value["failed_attempts_head_sha"])
            ),
            dispositions=_stored_dispositions(value.get("dispositions", [])),
            continuation_grants=_stored_continuation_grants(
                value.get("continuation_grants", [])
            ),
            transaction=transaction,
            convergence_history=_stored_convergence_history(
                value.get("convergence_history")
            ),
            reservation_owner=_stored_reservation_owner(value.get("reservation_owner")),
            rescan_state=value.get("rescan_state"),  # type: ignore[arg-type]
            assessment_queue=_stored_assessment_queue(value.get("assessment_queue")),
            disposition_snapshot=cast(
                Mapping[str, object] | None, value.get("disposition_snapshot")
            ),
            api_run_accounting=_stored_api_accounting(value.get("api_run_accounting")),
            historical_assessments=cast(
                Mapping[str, object] | None, value.get("historical_assessments")
            ),
            _digest_shape_input=digest_shape,
        )
        validate_public_document(record.to_dict(), "session-record")
        return record

    @classmethod
    def create(
        cls,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
        expires_at: datetime | str | None = None,
        ttl: timedelta = DEFAULT_SESSION_TTL,
        completed_initial_reviews: int = 0,
        completed_verification_rounds: int = 0,
        failed_attempts: int = 0,
        generation: int = 0,
        reservation_id: str | None = None,
        reserved_slot: str | None = None,
        last_committed_reservation_id: str | None = None,
        failed_attempts_head_sha: str | None = None,
        dispositions: Sequence[Mapping[str, object]] = (),
        continuation_grants: Sequence[Mapping[str, object]] = (),
        transaction: ReviewTransaction | None = None,
        convergence_history: Mapping[str, object] | None = None,
        reservation_owner: Mapping[str, object] | None = None,
        rescan_state: Mapping[str, object] | None = None,
        assessment_queue: Mapping[str, object] | None = None,
        disposition_snapshot: Mapping[str, object] | None = None,
        api_run_accounting: Mapping[str, object] | None = None,
        quarantine_lineage: Mapping[str, object] | None = None,
        publication_staging: Mapping[str, object] | None = None,
    ) -> "SessionRecord":
        created = _aware_now(now)
        if isinstance(expires_at, str):
            expires = _parse_aware_datetime(expires_at, label="expires_at")
        elif isinstance(expires_at, datetime):
            expires = _aware_now(expires_at)
        else:
            if (
                not isinstance(ttl, timedelta)
                or ttl <= timedelta(0)
                or ttl > MAX_SESSION_TTL
            ):
                raise ReviewInputError("session ttl is invalid")
            expires = created + ttl
        created_stamp = _format_datetime(created)
        return cls._construct(
            repository=identity.repository,
            pull_request=identity.pull_request,
            repository_id=identity.repository_id,
            completed_initial_reviews=completed_initial_reviews,
            completed_verification_rounds=completed_verification_rounds,
            failed_attempts=failed_attempts,
            generation=generation,
            reservation_id=reservation_id,
            reserved_slot=reserved_slot,
            last_committed_reservation_id=last_committed_reservation_id,
            created_at=created_stamp,
            updated_at=created_stamp,
            expires_at=_format_datetime(expires),
            operator_paused=False,
            failed_attempts_head_sha=failed_attempts_head_sha,
            dispositions=dispositions,
            continuation_grants=continuation_grants,
            transaction=transaction,
            convergence_history=convergence_history,
            reservation_owner=reservation_owner,
            rescan_state=rescan_state,
            assessment_queue=assessment_queue,
            disposition_snapshot=disposition_snapshot,
            api_run_accounting=api_run_accounting,
            quarantine_lineage=quarantine_lineage,
            publication_staging=publication_staging,
        )

    def rollover_api_accounting(
        self, accounting: Mapping[str, object]
    ) -> "SessionRecord":
        """Retain session state while changing a genuinely different run.

        This is a value invariant, not storage or owner authentication. The
        adapter must confirm the retained carrier and stopped predecessor first.
        Ordinary evolve() never allows an accounting identity replacement.
        """
        return self._rollover_api_accounting(accounting, historical=False)

    def rollover_historical_api_accounting(
        self, accounting: Mapping[str, object], *, assessments=()
    ) -> "SessionRecord":
        """Value constraints only; source adapter authenticates the transition."""
        return self._rollover_api_accounting(
            accounting, historical=True, assessments=assessments
        )

    def _rollover_api_accounting(
        self, accounting: Mapping[str, object], *, historical: bool, assessments=()
    ) -> "SessionRecord":
        from .api_work import ApiRunAccounting

        previous = ApiRunAccounting.from_document(self.api_run_accounting)
        candidate = ApiRunAccounting.from_document(accounting)
        ref = candidate.previous_record
        if historical:
            from .historical_completion import (
                REFERENCE_VERSION,
                require_receipt_matches,
            )

            if ref is None or ref.get("schema_version") != REFERENCE_VERSION:
                raise ReviewInputError(
                    "historical rollover requires a versioned receipt"
                )
            require_receipt_matches(self, candidate, ref["completion"])
        elif ref is not None and "completion" in ref:
            raise ReviewInputError(
                "ordinary rollover cannot consume historical completion"
            )
        if (
            previous.identity.repository_id != candidate.identity.repository_id
            or previous.identity.run_id == candidate.identity.run_id
            or candidate.owner.run_attempt != 1
            or candidate.owner.generation != 0
            or candidate.confirmed
            or candidate.uncertain
            or candidate.prior_nonstartup_liability
            or candidate.startup_used != ((candidate.owner.job, 1),)
            or ref is None
            or ref["run_id"] != previous.identity.run_id
            or ref["root_sha256"] != self.record_sha256
            or self.reservation_id is not None
            or self.reserved_slot is not None
            or (previous.action is not None and previous.action["phase"] != "terminal")
            or (
                previous.action is None
                and previous.analysis_abort is None
                and (
                    self.transaction is None
                    or self.transaction.phase
                    not in {"publication_succeeded", "publication_suppressed"}
                    or (
                        not historical
                        and previous.terminal_transaction
                        != self.transaction.transaction_id
                    )
                )
            )
            or (previous.analysis_abort is not None and self.transaction is not None)
        ):
            raise ReviewInputError(
                "caller run rollover is not terminal or correctly retained"
            )
        binding = cast(Mapping[str, Any], ref["manifest"])["binding"]
        if (
            binding["repository"] != self.repository
            or binding["repository_id"] != self.repository_id
            or binding["pull_request"] != self.pull_request
            or binding["generation"] != self.generation
        ):
            raise ReviewInputError("retained caller root scope conflicts")
        payload = self.to_dict()
        del payload["schema_version"], payload["record_sha256"]
        payload["generation"] = self.generation + 1
        payload["api_run_accounting"] = candidate.to_document()
        payload["transaction"] = self.transaction
        if historical:
            from .historical_assessments import initial_hold

            if self.historical_assessments is not None:
                raise ReviewInputError(
                    "historical transition cannot replace an existing assessment source"
                )
            hold = initial_hold(ref, list(assessments))
            if hold is not None:
                payload["historical_assessments"] = hold
        return type(self)._construct(**cast(dict[str, Any], payload))

    def quarantine_api_accounting(
        self,
        accounting: Mapping[str, object],
        lineage: Mapping[str, object],
        *,
        now: datetime | None = None,
    ) -> "SessionRecord":
        """Construct a separate unresolved lineage without resetting session state.

        This value operation grants no storage authority. The adapter must
        authenticate the complete retained origin/hold, finite isolated targets
        and issued one-shot slot, then create and confirm a DIFFERENT comment.
        Ordinary accounting rollover and evolve retain their terminal rules.
        """
        from .api_work import ApiRunAccounting
        from .quarantine_lineage import normalize_lineage

        previous = ApiRunAccounting.from_document(self.api_run_accounting)
        candidate = ApiRunAccounting.from_document(accounting)
        normalized = normalize_lineage(lineage)
        if (
            normalized is None
            or self.quarantine_lineage is not None
            or normalized["predecessor_record_sha256"] != self.record_sha256
            or normalized["creation_run_id"] != candidate.identity.run_id
            or normalized["assessment_state_manifest"]["item_count"] != 0
            or previous.identity.repository_id != candidate.identity.repository_id
            or previous.identity.run_id == candidate.identity.run_id
            or candidate.owner.run_attempt != 1
            or candidate.owner.generation != 0
            or candidate.confirmed
            or candidate.uncertain
            or candidate.prior_nonstartup_liability
            or candidate.previous_record is not None
            or candidate.action is not None
            or candidate.analysis_abort is not None
            or candidate.terminal_transaction is not None
            or candidate.startup_used != ((candidate.owner.job, 1),)
            or previous.terminal_transaction is not None
            or previous.action is not None
            or previous.analysis_abort is not None
            or self.transaction is None
            or self.transaction.phase
            != (
                "publication_succeeded"
                if normalized["schema_version"] == "quarantine-published-tail-v1"
                else "publication_failed"
            )
            or self.reservation_id is not None
            or self.reserved_slot is not None
            or self.operator_paused
        ):
            raise ReviewInputError(
                "quarantine requires a separately funded unresolved predecessor"
            )
        binding = normalized["origin_manifest"]["binding"]
        if (
            binding["repository"] != self.repository
            or binding["repository_id"] != self.repository_id
            or binding["pull_request"] != self.pull_request
            or binding["generation"] not in {self.generation, self.generation + 1}
        ):
            raise ReviewInputError("quarantine retained origin scope conflicts")
        payload = self.to_dict()
        del payload["schema_version"], payload["record_sha256"]
        payload["generation"] = self.generation + 1
        payload["updated_at"] = _format_datetime(_aware_now(now))
        payload["api_run_accounting"] = candidate.to_document()
        payload["quarantine_lineage"] = normalized
        payload["transaction"] = None
        payload["reservation_owner"] = None
        # Every other field, including limits/counters, decisions, TTL, previous
        # history references and grants, is retained verbatim. The old active
        # transaction/accounting remains inside the authenticated origin.
        return type(self)._construct(**cast(dict[str, Any], payload))

    def evolve(
        self,
        *,
        now: datetime | None = None,
        generation: int | None = None,
        completed_initial_reviews: int | None = None,
        completed_verification_rounds: int | None = None,
        failed_attempts: int | None = None,
        reservation_id: str | None | object = ...,
        reserved_slot: str | None | object = ...,
        last_committed_reservation_id: str | None | object = ...,
        operator_paused: bool | None = None,
        failed_attempts_head_sha: str | None = None,
        dispositions: Sequence[Mapping[str, object]] | None = None,
        continuation_grants: Sequence[Mapping[str, object]] | None = None,
        transaction: ReviewTransaction | None | object = ...,
        convergence_history: Mapping[str, object] | None | object = ...,
        reservation_owner: Mapping[str, object] | None | object = ...,
        rescan_state: Mapping[str, object] | None | object = ...,
        assessment_queue: Mapping[str, object] | None | object = ...,
        disposition_snapshot: Mapping[str, object] | None | object = ...,
        api_run_accounting: Mapping[str, object] | None | object = ...,
        historical_assessments: Mapping[str, object] | None | object = ...,
    ) -> "SessionRecord":
        from .quarantine_lineage import require_lineage_successor

        next_lineage = (
            self.quarantine_lineage if quarantine_lineage is ... else quarantine_lineage
        )
        require_lineage_successor(self.quarantine_lineage, next_lineage)
        from .publication_staging import require_staging_successor

        next_staging = (
            self.publication_staging
            if publication_staging is ...
            else publication_staging
        )
        require_staging_successor(self.publication_staging, next_staging)
        updated = _format_datetime(_aware_now(now))
        from .historical_assessments import require_hold_successor

        require_hold_successor(
            self.historical_assessments,
            self.historical_assessments
            if historical_assessments is ...
            else historical_assessments,
        )
        if self.api_run_accounting is not None and api_run_accounting is not ...:
            if api_run_accounting is None:
                raise ReviewInputError("API accounting cannot be removed")
            from .api_work import ApiRunAccounting

            ApiRunAccounting.from_document(self.api_run_accounting).require_successor(
                ApiRunAccounting.from_document(api_run_accounting)
            )
        return type(self)._construct(
            repository=self.repository,
            pull_request=self.pull_request,
            repository_id=self.repository_id,
            completed_initial_reviews=(
                self.completed_initial_reviews
                if completed_initial_reviews is None
                else completed_initial_reviews
            ),
            completed_verification_rounds=(
                self.completed_verification_rounds
                if completed_verification_rounds is None
                else completed_verification_rounds
            ),
            failed_attempts=(
                self.failed_attempts if failed_attempts is None else failed_attempts
            ),
            generation=self.generation if generation is None else generation,
            reservation_id=(
                self.reservation_id
                if reservation_id is ...
                else cast(str | None, reservation_id)
            ),
            reserved_slot=(
                self.reserved_slot
                if reserved_slot is ...
                else cast(str | None, reserved_slot)
            ),
            last_committed_reservation_id=(
                self.last_committed_reservation_id
                if last_committed_reservation_id is ...
                else cast(str | None, last_committed_reservation_id)
            ),
            created_at=self.created_at,
            updated_at=updated,
            expires_at=self.expires_at,
            operator_paused=(
                self.operator_paused if operator_paused is None else operator_paused
            ),
            failed_attempts_head_sha=(
                self.failed_attempts_head_sha
                if failed_attempts_head_sha is None
                else failed_attempts_head_sha
            ),
            dispositions=(self.dispositions if dispositions is None else dispositions),
            continuation_grants=(
                self.continuation_grants
                if continuation_grants is None
                else continuation_grants
            ),
            reservation_owner=(
                None
                if reservation_id is None
                else self.reservation_owner
                if reservation_owner is ...
                else cast(Mapping[str, object] | None, reservation_owner)
            ),
            transaction=(
                self.transaction
                if transaction is ...
                else cast(ReviewTransaction | None, transaction)
            ),
            rescan_state=(
                self.rescan_state
                if rescan_state is ...
                else cast(Mapping[str, object] | None, rescan_state)
            ),
            assessment_queue=(
                self.assessment_queue
                if assessment_queue is ...
                else cast(Mapping[str, object] | None, assessment_queue)
            ),
            disposition_snapshot=(
                self.disposition_snapshot
                if disposition_snapshot is ...
                else cast(Mapping[str, object] | None, disposition_snapshot)
            ),
            historical_assessments=(
                self.historical_assessments
                if historical_assessments is ...
                else cast(Mapping[str, object] | None, historical_assessments)
            ),
            api_run_accounting=(
                self.api_run_accounting
                if api_run_accounting is ...
                else cast(Mapping[str, object] | None, api_run_accounting)
            ),
            quarantine_lineage=cast(Mapping[str, object] | None, next_lineage),
            publication_staging=cast(Mapping[str, object] | None, next_staging),
            convergence_history=(
                self.convergence_history
                if convergence_history is ...
                else cast(Mapping[str, object] | None, convergence_history)
            ),
        )


def _reserve_publication_staging(
    shell: dict[str, object], record: SessionRecord
) -> None:
    if record.api_run_accounting is not None or record.publication_staging is not None:
        from .publication_staging import MAX_STAGING_BYTES

        # Sizing only: reserve the complete future value before any accepted
        # result, queue or decision can fill the root. This is not a state or
        # a receipt and is never persisted as a document.
        shell["publication_staging"] = "s" * MAX_STAGING_BYTES


def checkpoint_baseline_capacity(record: SessionRecord) -> int:
    """Allocate history within the record, including future lifecycle growth.

    Serialize a conservative superset of the analysis/publication shells.
    Existing dispositions, grants, identities and timestamps are kept exactly;
    fixed-width future counters, ownership, reservation and transaction fields
    are reserved at their largest supported encoding. The superset intentionally
    includes mutually exclusive fields, so no phase can exceed this allowance.
    New operator commands still validate their actual complete record before CAS.
    """

    from .baseline import (
        MAX_HISTORY_BASELINE_BYTES,
        MAX_HISTORY_ENVELOPE_RESERVE_BYTES,
    )

    shell = record.to_dict()
    _reserve_publication_staging(shell, record)
    shell.pop("convergence_history", None)
    shell.update(
        generation=MAX_GENERATION,
        completed_initial_reviews=DIAGNOSTIC_ROUND_CEILING,
        completed_verification_rounds=DIAGNOSTIC_ROUND_CEILING,
        failed_attempts=MAX_FAILED_ATTEMPTS,
        failed_attempts_head_sha="a" * 64,
        operator_paused=False,
        reservation_id="a" * 64,
        reserved_slot="failed-attempt",
        last_committed_reservation_id="b" * 64,
        reservation_owner={"run_id": "9" * 19, "head_sha": "a" * 40},
        convergence_history={},
    )
    # Internal timestamps are normalized by _format_datetime. Retained longer
    # legacy spellings are charged at their actual size rather than shortened.
    updated = "9999-12-31T23:59:59.999999Z"
    if len(updated) > len(str(shell["updated_at"])):
        shell["updated_at"] = updated
    transaction = ReviewTransaction.create(
        repository=record.repository,
        pull_request=record.pull_request,
        base_sha="a" * 40,
        head_sha="b" * 40,
        policy_digest="c" * 64,
        configuration_digest="d" * 64,
        evidence_digest="e" * 64,
        reservation_id="f" * 64,
        generation=MAX_GENERATION,
    ).to_dict()
    transaction.update(phase="publication_suppressed", result_sha256="a" * 64)
    shell["transaction"] = transaction
    if record.assessment_queue is not None:
        # Reserve the entire legal queue root across later summary/counter and
        # reference growth, rather than charging only today's short root.
        shell["assessment_queue"] = "q" * MAX_ASSESSMENT_QUEUE_ROOT_BYTES
    # Subtract the empty object's two bytes; key/colon/comma and the record
    # digest remain included. Nested JSON is an object, not an escaped string.
    outside = len(json.dumps(shell, sort_keys=True, separators=(",", ":")).encode()) - 2
    history_bytes = min(
        MAX_CONVERGENCE_HISTORY_BYTES, MAX_SESSION_RECORD_BYTES - outside
    )
    return max(
        0,
        min(
            MAX_HISTORY_BASELINE_BYTES,
            history_bytes - MAX_HISTORY_ENVELOPE_RESERVE_BYTES,
        ),
    )


def assessment_queue_manifest_capacity(record: SessionRecord) -> int:
    """Allocate the queue manifest beside retained baseline and future lifecycle.

    The 8 KiB queue cap includes its 1 KiB summary reserve. Preserve existing
    baseline bytes plus its 1 KiB history lifecycle reserve. The conservative
    shell includes mutually exclusive future transaction/ownership fields.
    """
    from .baseline import MAX_HISTORY_ENVELOPE_RESERVE_BYTES

    shell = record.to_dict()
    _reserve_publication_staging(shell, record)
    shell.pop("assessment_queue", None)
    history = shell.pop("convergence_history", None)
    shell.update(
        generation=MAX_GENERATION,
        completed_initial_reviews=DIAGNOSTIC_ROUND_CEILING,
        completed_verification_rounds=DIAGNOSTIC_ROUND_CEILING,
        failed_attempts=MAX_FAILED_ATTEMPTS,
        failed_attempts_head_sha="a" * 64,
        reservation_id="a" * 64,
        reserved_slot="failed-attempt",
        last_committed_reservation_id="b" * 64,
        reservation_owner={"run_id": "9" * 19, "head_sha": "a" * 40},
    )
    updated = "9999-12-31T23:59:59.999999Z"
    if len(updated) > len(str(shell["updated_at"])):
        shell["updated_at"] = updated
    transaction = ReviewTransaction.create(
        repository=record.repository,
        pull_request=record.pull_request,
        base_sha="a" * 40,
        head_sha="b" * 40,
        policy_digest="c" * 64,
        configuration_digest="d" * 64,
        evidence_digest="e" * 64,
        reservation_id="f" * 64,
        generation=MAX_GENERATION,
    ).to_dict()
    transaction.update(phase="publication_suppressed", result_sha256="a" * 64)
    shell["transaction"] = transaction
    history_bytes = (
        len(canonical_bytes(history)) if history is not None else 0
    ) + MAX_HISTORY_ENVELOPE_RESERVE_BYTES
    outside = (
        len(canonical_bytes(shell))
        + len(',"assessment_queue":')
        + len(',"convergence_history":')
        + history_bytes
    )
    return max(
        0,
        min(MAX_ASSESSMENT_QUEUE_ROOT_BYTES, MAX_SESSION_RECORD_BYTES - outside)
        - MAX_ASSESSMENT_QUEUE_SUMMARY_BYTES,
    )


@dataclass(frozen=True)
class SessionLoadResult:
    """Result of a fail-closed ledger load. Missing state is explicit."""

    status: str
    record: SessionRecord | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status not in LOAD_STATUSES:
            raise ReviewInputError("session load status is invalid")
        if self.detail is not None and (
            not isinstance(self.detail, str)
            or not self.detail
            or len(self.detail) > 160
            or not self.detail.isprintable()
        ):
            raise ReviewInputError("session load detail is invalid")
        if self.status in {"ok", "migrated"}:
            if not isinstance(self.record, SessionRecord):
                raise ReviewInputError("session load requires a record")
        elif self.record is not None:
            raise ReviewInputError("failed session load cannot include a record")


@dataclass(frozen=True)
class PreparedSessionRound:
    """Reservation plus C1 decision. C5 skips work when ``admit`` is false."""

    record: SessionRecord
    decision: RoundAdmissionDecision
    reservation_id: str | None
    transaction: ReviewTransaction | None = None


class SessionLedger(Protocol):
    """Durable store for one session identity."""

    def load(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionLoadResult: ...

    def initialize(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
        expires_at: datetime | str | None = None,
    ) -> SessionRecord: ...

    def replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord: ...

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        head_sha: str | None = None,
        rescan_request: AuthorizedRescan | None = None,
        now: datetime | None = None,
    ) -> SessionRecord: ...

    def commit(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord: ...

    def abort(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord: ...

    def reenroll(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Recover an expired or witness-only session so a new one can enroll."""
        ...


def _expected_after_slot(record: SessionRecord, slot: str) -> tuple[int, int, int]:
    """Return the full counter snapshot used to verify a committed replay."""

    written = _apply_slot(record, slot)
    return (
        written.get("completed_initial_reviews", record.completed_initial_reviews),
        written.get(
            "completed_verification_rounds", record.completed_verification_rounds
        ),
        written.get("failed_attempts", record.failed_attempts),
    )


def _apply_slot(record: SessionRecord, slot: str) -> dict[str, int]:
    """Return the bounded counter value written by the reserved slot."""

    if slot == "initial":
        return {
            "completed_initial_reviews": min(
                record.completed_initial_reviews + 1, DIAGNOSTIC_ROUND_CEILING
            )
        }
    if slot == "verification":
        return {
            "completed_verification_rounds": min(
                record.completed_verification_rounds + 1, DIAGNOSTIC_ROUND_CEILING
            )
        }
    if slot == "failed-attempt":
        # The stored count is also the maximum configurable per-head retry
        # budget, so further failure cleanups remain valid at the bound.
        return {"failed_attempts": min(record.failed_attempts + 1, MAX_FAILED_ATTEMPTS)}
    raise ReviewInputError("reserved_slot is invalid")


def _next_generation(record: SessionRecord) -> int:
    if record.generation >= MAX_GENERATION:
        raise ReviewInputError("session generation is exhausted")
    return record.generation + 1


def next_session_generation(record: SessionRecord) -> int:
    """Return the generation the next checkpoint will advance this record to.

    Callers that build a baseline envelope for a checkpoint need this value
    before the session layer verifies it. Recomputing ``generation + 1`` at the
    call site would duplicate an invariant this module owns, so the caller asks
    for it here instead.
    """

    return _next_generation(record)


def _failed_attempt_scope(
    record: SessionRecord, head_sha: str | None
) -> dict[str, Any]:
    """Return the failed-attempt bookkeeping a reservation for this head owes.

    The retry budget belongs to the head that burned it.  A changed head
    starts a fresh per-invocation budget instead of inheriting another head's
    failures, and a record written before per-head scoping carries no head at
    all, so an upgrade never keeps an obsolete cumulative count.
    """

    if head_sha is None or record.failed_attempts_head_sha == head_sha:
        return {}
    return {"failed_attempts": 0, "failed_attempts_head_sha": head_sha}


def failed_attempts_for_head(record: SessionRecord, head_sha: str | None) -> int:
    """Return the failed attempts an admission decision may hold against a head."""

    if record.failed_attempts_head_sha is None:
        return 0
    if head_sha is not None and record.failed_attempts_head_sha != head_sha:
        return 0
    return record.failed_attempts


def mutate_reserved(
    record: SessionRecord,
    *,
    slot: str,
    reservation_id: str,
    expected_generation: int,
    head_sha: str | None = None,
    policy_digest: str | None = None,
    rescan_request: AuthorizedRescan | None = None,
    now: datetime | None = None,
) -> SessionRecord:
    if record.last_committed_reservation_id == reservation_id:
        # A committed id is an idempotency key for the same logical attempt;
        # callers must not reuse it for a different reservation slot.
        return record
    if record.generation != expected_generation:
        raise ReviewInputError("session generation conflict")
    if record.reservation_id == reservation_id and record.reserved_slot == slot:
        return record
    if record.reservation_id is not None:
        raise ReviewInputError("session reservation is already held")
    if rescan_request is not None:
        from .rescan import AuthorizedRescan, consume_rescan_request

        if not isinstance(rescan_request, AuthorizedRescan):
            raise ReviewInputError("explicit rescan authority is invalid")
        record = consume_rescan_request(record, rescan_request, reservation_id)
    if record.transaction is not None:
        # A completed transaction is historical metadata.  A new admitted
        # head gets a fresh transaction; an active analysis cannot be silently
        # replaced by a competing reservation.
        if record.transaction.phase == "analysis":
            raise ReviewInputError("session analysis transaction is already active")
        record = record.evolve(transaction=None, now=now)
    if slot not in RESERVATION_SLOTS:
        raise ReviewInputError("reserved_slot is invalid")
    _reservation_id(reservation_id, label="reservation_id")
    next_generation = _next_generation(record)
    updated_grants = record.continuation_grants
    next_last_committed: str | None = record.last_committed_reservation_id
    target_head = head_sha or record.failed_attempts_head_sha
    active_successor = has_unconsumed_successor_grant(
        record, target_head, policy_digest=policy_digest
    )
    if active_successor is not None:
        grants_list = [dict(item) for item in record.continuation_grants]
        for idx in range(len(grants_list) - 1, -1, -1):
            item = grants_list[idx]
            if (
                item.get("command_id") == active_successor.get("command_id")
                and item.get("consumed_reservation_id") is None
            ):
                grants_list[idx] = {
                    **item,
                    "consumed_reservation_id": reservation_id,
                    "consumed_generation": next_generation,
                }
                updated_grants = tuple(grants_list)
                break
    elif (
        active_irrecoverable := has_unconsumed_irrecoverable_transaction_grant(
            record, target_head, policy_digest=policy_digest
        )
    ) is not None:
        retired_res = active_irrecoverable.get("consumed_reservation_id")
        if isinstance(retired_res, str) and retired_res:
            next_last_committed = hashlib.sha256(
                f"{retired_res}|irrecoverable-consumed".encode("utf-8")
            ).hexdigest()
    return record.evolve(
        now=now,
        generation=next_generation,
        reservation_id=reservation_id,
        reserved_slot=slot,
        continuation_grants=updated_grants,
        last_committed_reservation_id=next_last_committed,
        **_failed_attempt_scope(record, head_sha),
    )


def mutate_commit(
    record: SessionRecord,
    *,
    reservation_id: str,
    expected_generation: int,
    now: datetime | None = None,
) -> SessionRecord:
    if record.transaction is not None:
        raise ReviewInputError(
            "transaction reservations require an analysis checkpoint"
        )
    if (
        record.last_committed_reservation_id == reservation_id
        and record.reservation_id is None
    ):
        return record
    if record.generation != expected_generation:
        raise ReviewInputError("session generation conflict")
    if record.reservation_id != reservation_id or record.reserved_slot is None:
        raise ReviewInputError("session reservation does not match")
    increments = _apply_slot(record, record.reserved_slot)
    allowed_increments = {
        "completed_initial_reviews",
        "completed_verification_rounds",
        "failed_attempts",
    }
    unexpected_increments = set(increments) - allowed_increments
    if unexpected_increments:
        raise ReviewInputError("session counter increment is unsupported")
    return record.evolve(
        now=now,
        generation=_next_generation(record),
        reservation_id=None,
        reserved_slot=None,
        transaction=None,
        last_committed_reservation_id=reservation_id,
        completed_initial_reviews=increments.get("completed_initial_reviews"),
        completed_verification_rounds=increments.get("completed_verification_rounds"),
        failed_attempts=increments.get("failed_attempts"),
    )


def mutate_abort(
    record: SessionRecord,
    *,
    reservation_id: str,
    expected_generation: int,
    now: datetime | None = None,
) -> SessionRecord:
    """Release a reservation with CAS.

    An already-absent reservation is a no-op because there is no hold left to
    release. A live reservation owned by another id remains a conflict.
    """

    if (
        record.reservation_id is None
        and record.last_committed_reservation_id != reservation_id
    ):
        return record
    if (
        record.last_committed_reservation_id == reservation_id
        and record.reservation_id is None
    ):
        return record
    if record.generation != expected_generation:
        raise ReviewInputError("session generation conflict")
    if record.reservation_id != reservation_id:
        raise ReviewInputError("session reservation does not match")
    return record.evolve(
        now=now,
        generation=_next_generation(record),
        reservation_id=None,
        reserved_slot=None,
        transaction=None,
    )


def prepare_session_round(
    ledger: SessionLedger,
    identity: SessionIdentity,
    policy: ReviewConvergencePolicy,
    *,
    reservation_id: str,
    now: datetime | None = None,
    head_sha: str | None = None,
    rescan_request: AuthorizedRescan | None = None,
    **state_flags: bool,
) -> PreparedSessionRound:
    """Reserve a counted slot when C1 would admit the round.

    Any in-flight reservation pauses this job, including a retry carrying the
    same reservation id. The same ``reservation_id`` after commit is a
    same-head duplicate. Unadmitted operator rounds are not reserved so C5 can
    refuse inference and publication without consuming the budget.
    """

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    retired = sorted({"continuation_rounds"} & set(state_flags))
    if retired:
        raise ReviewInputError(
            "round count continuation is no longer supported: " + ", ".join(retired)
        )
    loaded = ledger.load(identity, now=now)
    if loaded.status == "missing":
        record = ledger.initialize(identity, now=now)
    elif loaded.status == "expired":
        # An expired budget stays authoritative instead of being silently
        # reinitialized. Name the operator recovery step so hosted and CLI
        # runs surface an actionable message rather than an internal error.
        raise ReviewInputError(
            "session ledger load failed: expired; authenticated recovery is "
            "required: re-enroll the session or explicitly replace the "
            "expired session marker"
        )
    elif loaded.status in {"ok", "migrated"} and loaded.record is not None:
        record = loaded.record
    else:
        raise ReviewInputError(f"session ledger load failed: {loaded.status}")
    from .rescan import (
        AuthorizedRescan,
        ordinary_rescan_duplicate,
        replayed_rescan,
        rescan_publication_recovery,
        rescan_reservation,
    )

    current_policy_digest = policy.digest()
    if isinstance(rescan_request, AuthorizedRescan):
        rescan_request = replace(rescan_request, ordinary_reservation=reservation_id)
    if rescan_request is not None:
        reservation_id = rescan_reservation(record, rescan_request, head_sha)
    elif (
        head_sha is not None
        and (
            irrecoverable_grant := has_irrecoverable_transaction_grant(
                record, head_sha, policy_digest=current_policy_digest
            )
        )
        is not None
    ):
        reservation_id = hashlib.sha256(
            f"{reservation_id}|{irrecoverable_grant['command_id']}:{irrecoverable_grant['consumed_generation']}".encode(
                "utf-8"
            )
        ).hexdigest()
    elif (
        successor_grant := has_unconsumed_successor_grant(
            record,
            head_sha or record.failed_attempts_head_sha,
            policy_digest=current_policy_digest,
        )
    ) is not None:
        reservation_id = hashlib.sha256(
            f"{reservation_id}|{successor_grant['command_id']}".encode("utf-8")
        ).hexdigest()
    flags = dict(state_flags)
    if (
        (
            isinstance(rescan_request, AuthorizedRescan)
            and replayed_rescan(record, rescan_request)
        )
        or ordinary_rescan_duplicate(record, reservation_id, head_sha)
        or rescan_publication_recovery(record, head_sha)
    ):
        flags.setdefault("same_head_duplicate", True)
    if record.operator_paused:
        flags.setdefault("paused", True)
    held = record.reservation_id
    if held is not None and not record.operator_paused:
        # Admission self-heal (ADR 0079): a leaked reservation from a lost
        # runner must not block this review until expiry when the owner
        # run provably completed. A maintainer pause is checked first and
        # is never cleared here. Any recovery failure keeps today's
        # blocked behavior exactly.
        recover = getattr(ledger, "recover_abandoned_analysis", None)
        if callable(recover):
            try:
                recovered = recover(identity, now=now)
            except (ReviewSenseiError, OSError):
                recovered = None
            if (
                recovered is not None
                and recovered.reservation_id is None
                and recovered.repository == identity.repository
                and recovered.pull_request == identity.pull_request
                and recovered.repository_id == identity.repository_id
            ):
                record = recovered
                held = None
    if held is not None:
        flags.setdefault("paused", True)
    elif record.last_committed_reservation_id == reservation_id and held is None:
        flags.setdefault("same_head_duplicate", True)
    decision = evaluate_round_admission(
        record.to_round_state(
            head_sha=head_sha,
            policy_digest=current_policy_digest,
            **flags,
        ),
        policy,
    )
    if (
        policy.mode not in OPERATOR_REVIEW_MODES
        or not decision.admit
        or not decision.count_as_completed_round
    ):
        return PreparedSessionRound(
            record=record, decision=decision, reservation_id=None
        )
    if decision.round_kind not in RESERVATION_SLOTS:
        raise ReviewInputError("admitted round kind cannot be reserved")
    # Reservation ids are deterministic for a repository, PR head, and slot.
    # Once that exact attempt committed, a retry is an idempotent no-op rather
    # than a second reservation that would alias last_committed_reservation_id.
    if (
        record.reservation_id is None
        and record.last_committed_reservation_id == reservation_id
    ):
        return PreparedSessionRound(
            record=record, decision=decision, reservation_id=None
        )
    reserved = ledger.reserve(
        identity,
        slot=decision.round_kind,
        reservation_id=reservation_id,
        expected_generation=record.generation,
        head_sha=head_sha,
        rescan_request=rescan_request,
        now=now,
    )
    return PreparedSessionRound(
        record=reserved, decision=decision, reservation_id=reservation_id
    )


def prepare_review_transaction(
    ledger: SessionLedger,
    identity: SessionIdentity,
    policy: ReviewConvergencePolicy,
    *,
    reservation_id: str,
    base_sha: str,
    head_sha: str,
    configuration_digest: str,
    evidence_digest: str,
    now: datetime | None = None,
    rescan_request: AuthorizedRescan | None = None,
    **state_flags: bool,
) -> PreparedSessionRound:
    """Reserve and bind one logical analysis transaction before inference."""

    for label, value in (
        ("base_sha", base_sha),
        ("head_sha", head_sha),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 40
            or not re.fullmatch(r"[a-f0-9]{40}", value)
        ):
            raise ReviewInputError(f"review transaction {label} is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", configuration_digest):
        raise ReviewInputError("review transaction configuration digest is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", evidence_digest):
        raise ReviewInputError("review transaction evidence digest is invalid")
    prepared = prepare_session_round(
        ledger,
        identity,
        policy,
        reservation_id=reservation_id,
        now=now,
        head_sha=head_sha,
        rescan_request=rescan_request,
        **state_flags,
    )
    if prepared.reservation_id is None:
        return prepared
    transaction = ReviewTransaction.create(
        repository=identity.repository,
        pull_request=identity.pull_request,
        base_sha=base_sha,
        head_sha=head_sha,
        policy_digest=policy.digest(),
        configuration_digest=configuration_digest,
        evidence_digest=evidence_digest,
        reservation_id=prepared.reservation_id,
        generation=prepared.record.generation,
    )

    def attach(record: SessionRecord) -> SessionRecord:
        if record.generation != prepared.record.generation:
            raise ReviewInputError("session generation conflict")
        if record.reservation_id != prepared.reservation_id:
            raise ReviewInputError("session reservation does not match")
        if record.transaction is not None:
            if record.transaction.transaction_id == transaction.transaction_id:
                return record
            raise ReviewInputError("session already has a different transaction")
        return record.evolve(transaction=transaction, now=now)

    attached = ledger.replace(identity, attach, now=now)
    return PreparedSessionRound(
        record=attached,
        decision=prepared.decision,
        reservation_id=prepared.reservation_id,
        transaction=transaction,
    )


def _checkpoint_transaction_record(
    record: SessionRecord,
    transaction: ReviewTransaction,
    result_digest: str,
    *,
    baseline: object | None = None,
    review_complete: bool = True,
    progress: Mapping[str, object] | None = None,
    now: datetime | None = None,
    partition_writer: Callable[[dict[str, object], int, int], dict[str, object]]
    | None = None,
) -> SessionRecord:
    if (
        record.transaction is None
        or record.transaction.transaction_id != transaction.transaction_id
    ):
        raise ReviewInputError("session transaction does not match")
    if record.transaction.phase in {
        "publication_pending",
        "publication_failed",
        "publication_succeeded",
        "publication_suppressed",
    }:
        if record.transaction.result_sha256 != result_digest:
            raise ReviewInputError("session transaction result digest does not match")
        return record
    if record.transaction.phase != "analysis":
        raise ReviewInputError("session transaction is not checkpointable")
    if not record.transaction.logical_identity_matches(transaction):
        raise ReviewInputError("session transaction identity does not match")
    if record.generation != transaction.generation:
        raise ReviewInputError("session generation conflict")
    if (
        record.reservation_id != transaction.reservation_id
        or record.reserved_slot
        not in {
            "initial",
            "verification",
        }
    ):
        raise ReviewInputError("session analysis reservation does not match")
    # Retained partial findings are publishable, but cannot complete a round
    # or replace its last complete baseline. Charge their bounded incomplete
    # attempt once, at the checkpoint, rather than again on publication retry.
    increments = (
        {}
        if not review_complete and progress is not None
        else _apply_slot(
            record, record.reserved_slot if review_complete else "failed-attempt"
        )
    )
    rescan_state = record.rescan_state
    if progress is not None:
        if review_complete or rescan_state is None:
            raise ReviewInputError("productive checkpoint has no remaining authority")
        rescan_state = json.loads(json.dumps(rescan_state))
        receipt = next(
            (
                item
                for item in rescan_state["receipts"]
                if item["reservation"] == transaction.reservation_id
            ),
            None,
        )
        if (
            receipt is None
            or receipt.get("mode") != "remaining"
            or progress.get("child_digest") != result_digest
            or progress.get("parent_digest") != receipt["source"]["result_sha256"]
        ):
            raise ReviewInputError("productive checkpoint authority differs")
        receipt["progress"] = dict(progress)
    next_generation = _next_generation(record)
    convergence_history: Mapping[str, object] | None = record.convergence_history
    if not review_complete and baseline is not None:
        raise ReviewInputError("a partial review cannot checkpoint a baseline")
    if baseline is not None:
        from .baseline import ReviewBaseline, baseline_history_document

        if not isinstance(baseline, ReviewBaseline) or not baseline.complete:
            raise ReviewInputError("only a complete baseline can be checkpointed")
        if (
            baseline.cache_key.repository != transaction.repository
            or baseline.cache_key.pull_request != transaction.pull_request
            or baseline.cache_key.base_sha != transaction.base_sha
            or baseline.cache_key.head_sha != transaction.head_sha
            or baseline.policy_digest != transaction.policy_digest
            or baseline.generation != next_generation
        ):
            raise ReviewInputError("baseline does not match the review transaction")
        prior_progress: list[object] = []
        if isinstance(convergence_history, Mapping):
            stored_progress = convergence_history.get("progress")
            if isinstance(stored_progress, list):
                # A checkpoint writes a markerless lifecycle placeholder for
                # the transaction that is about to be admitted.  Keep the
                # comparable blocker-bearing entries, rather than letting
                # that placeholder evict the immediately preceding F3
                # evidence from the bounded window.
                comparable_progress: list[object] = [
                    item
                    for item in stored_progress
                    if isinstance(item, Mapping)
                    and item.get("event") == "completed"
                    and "blocker_set_sha256" in item
                    and "blocker_count" in item
                ]
                prior_progress = comparable_progress[
                    -(MAX_CONVERGENCE_PROGRESS_ENTRIES - 1) :
                ]
        convergence_history = {
            "state": "completed",
            "baseline": baseline_history_document(
                baseline,
                max_bytes=checkpoint_baseline_capacity(record),
                require_complete=True,
                partition_writer=partition_writer,
            ),
            "progress": [
                *prior_progress,
                {"event": "completed", "generation": next_generation},
            ],
            # Bind the checkpoint to the integrity-protected ledger state it
            # advances.  It is not a self-reference to the newly computed
            # record digest.
            "provenance": {"ledger_digest": record.record_sha256},
        }
    pending = replace(
        transaction.with_result(result_digest),
        generation=next_generation,
    )
    return record.evolve(
        now=now,
        generation=next_generation,
        reservation_id=None,
        reserved_slot=None,
        last_committed_reservation_id=transaction.reservation_id,
        completed_initial_reviews=increments.get("completed_initial_reviews"),
        completed_verification_rounds=increments.get("completed_verification_rounds"),
        failed_attempts=increments.get("failed_attempts"),
        transaction=pending,
        convergence_history=convergence_history,
        rescan_state=rescan_state,
    )


def review_analysis_checkpoint_eligible(result: ReviewResult) -> bool:
    """Admit complete compatibility or explicitly covered validated partial work.

    Failed chunks may carry ``partially-reviewed`` without validated output;
    those entries alone cannot justify a checkpoint. Missing coverage and
    unknown enumeration also fail closed. Partial evidence never becomes a
    complete convergence baseline.
    """

    if result.review_status == "complete":
        return result.coverage is None or result.coverage.fully_reviewed
    coverage = result.coverage
    if (
        result.review_status != "partial"
        or coverage is None
        or not coverage.enumeration_complete
    ):
        return False
    entries: tuple[FileCoverage | HunkCoverage, ...] = (
        *coverage.files,
        *coverage.hunks,
    )
    return any(
        entry.outcome == "reviewed"
        or (
            entry.outcome == "partially-reviewed"
            and entry.reason == "cross-file-relationship"
        )
        for entry in entries
    )


def checkpoint_review_analysis(
    ledger: SessionLedger,
    identity: SessionIdentity,
    prepared: PreparedSessionRound,
    result: ReviewResult,
    *,
    baseline: object | None = None,
    now: datetime | None = None,
    retain_result: Callable[[ReviewResult], None] | None = None,
) -> ReviewResult:
    """Durably commit validated analysis once and return its bound artifact."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("review analysis result is invalid")
    if not review_analysis_checkpoint_eligible(result):
        raise ReviewInputError("review has no checkpointable validated coverage")
    if result.review_status != "complete" and baseline is not None:
        raise ReviewInputError("a partial review cannot checkpoint a baseline")
    transaction = prepared.transaction
    if transaction is None or prepared.reservation_id != transaction.reservation_id:
        raise ReviewInputError("prepared review transaction is invalid")
    result_digest = result.content_digest()
    if result.transaction is not None and (
        not result.transaction.logical_identity_matches(transaction)
        or result.transaction.result_sha256 not in {None, result_digest}
    ):
        raise ReviewInputError("review result transaction does not match")

    if retain_result is None:
        hook = getattr(ledger, "retain_checkpoint_result", None)
        if callable(hook):
            retain_result = hook
    if retain_result is not None:
        pending_preview = replace(
            transaction.with_result(result_digest),
            generation=transaction.generation + 1,
        )
        retain_result(replace(result, transaction=pending_preview))

    from .remaining_progress import checkpoint_remaining_progress

    updated = ledger.replace(
        identity,
        lambda record: _checkpoint_transaction_record(
            record,
            transaction,
            result_digest,
            baseline=baseline,
            review_complete=result.review_status == "complete",
            progress=checkpoint_remaining_progress(ledger, identity, record, result),
            now=now,
            partition_writer=(
                lambda document, count, allowance: getattr(ledger, "stage_evidence")(
                    identity,
                    binding={
                        "repository": transaction.repository,
                        "repository_id": identity.repository_id,
                        "pull_request": transaction.pull_request,
                        "base_sha": transaction.base_sha,
                        "head_sha": transaction.head_sha,
                        "policy_digest": transaction.policy_digest,
                        "configuration_digest": transaction.configuration_digest,
                        "generation": record.generation + 1,
                        "producer": getattr(ledger, "evidence_producer")(),
                        "purpose": "baseline",
                        "schema_version": "1.0",
                    },
                    document=document,
                    item_count=count,
                    max_manifest_bytes=allowance,
                )
            )
            if getattr(ledger, "enable_partition_writes", False)
            else None,
        ),
        now=now,
    )
    bound = updated.transaction
    if bound is None:
        raise ReviewInputError("session checkpoint did not persist a transaction")
    return replace(result, transaction=bound)


def load_review_transaction_for_publication(
    ledger: SessionLedger,
    identity: SessionIdentity,
    result: ReviewResult,
    *,
    base_sha: str,
    head_sha: str,
    policy_digest: str,
    configuration_digest: str,
    evidence_digest: str,
    baseline: object | None = None,
    now: datetime | None = None,
    commit_analysis: bool = True,
    loaded: SessionLoadResult | None = None,
) -> SessionRecord:
    """Validate a result against durable state, recovering a crash checkpoint.

    A retry may carry the transaction generation captured before publication
    advanced the durable phase.  The durable result digest and identity are
    authoritative in that case: publication rebinds the result to the current
    record, and the application short-circuits a terminal success as
    ``already_published``.  Possession of that older result artifact is not
    standalone authorization: the caller must still reach the identity-bound
    ledger selected for publication and pass the expected policy, configuration,
    evidence, head/base, and result-digest checks. The analysis-phase recovery
    path still requires an exact durable generation and reservation owner.
    """

    if result.transaction is None:
        raise ReviewInputError(
            "publication requires an identity-bound review transaction"
        )
    if not review_analysis_checkpoint_eligible(result):
        raise ReviewInputError("publication requires checkpointable validated coverage")
    transaction = result.transaction
    if not transaction.identity_matches(
        repository=identity.repository,
        pull_request=identity.pull_request,
        base_sha=base_sha,
        head_sha=head_sha,
        policy_digest=policy_digest,
        configuration_digest=configuration_digest,
        evidence_digest=evidence_digest,
    ):
        raise ReviewInputError(
            "review transaction identity or trusted context mismatches"
        )
    result_digest = result.content_digest()
    if transaction.result_sha256 not in {None, result_digest}:
        raise ReviewInputError("review result digest does not match transaction")
    if loaded is None:
        loaded = ledger.load(identity, now=now)
    if loaded.status not in {"ok", "migrated"} or loaded.record is None:
        raise ReviewInputError(f"session ledger load failed: {loaded.status}")
    record = loaded.record
    if (
        record.transaction is None
        or record.transaction.transaction_id != transaction.transaction_id
    ):
        raise ReviewInputError("durable transaction does not match result")
    if not record.transaction.logical_identity_matches(transaction):
        raise ReviewInputError("durable transaction identity does not match result")
    retained_preview = (
        record.transaction.phase == "analysis"
        and transaction.phase == "publication_pending"
        and transaction.generation == record.transaction.generation + 1
        and transaction.result_sha256 == result_digest
    )
    if transaction.generation > record.transaction.generation and not retained_preview:
        raise ReviewInputError("review transaction generation is stale")
    if record.transaction.phase == "analysis":
        if (
            transaction.generation != record.transaction.generation
            and not retained_preview
        ) or record.reservation_id != transaction.reservation_id:
            raise ReviewInputError("review transaction generation does not match")
        if not commit_analysis:
            return record
        from .remaining_progress import checkpoint_remaining_progress

        analysis_transaction = record.transaction
        return ledger.replace(
            identity,
            lambda current: _checkpoint_transaction_record(
                current,
                analysis_transaction,
                result_digest,
                baseline=baseline,
                review_complete=result.review_status == "complete",
                progress=checkpoint_remaining_progress(
                    ledger, identity, current, result
                ),
                now=now,
            ),
            now=now,
        )
    if record.transaction.result_sha256 != result_digest:
        raise ReviewInputError("durable result digest does not match")
    return record


def has_irrecoverable_transaction_grant(
    record: SessionRecord,
    head_sha: str | None,
    *,
    policy_digest: str | None = None,
) -> dict[str, object] | None:
    """Return the active irrecoverable-transaction successor grant for ``head_sha``."""

    if not isinstance(head_sha, str) or not head_sha:
        return None
    for item in reversed(record.continuation_grants):
        command_id = item.get("command_id")
        if (
            isinstance(command_id, str)
            and command_id.startswith("irrecoverable-tx:")
            and item.get("head_sha") == head_sha
            and isinstance(item.get("consumed_generation"), int)
            and (policy_digest is None or item.get("policy_digest") == policy_digest)
        ):
            return dict(item)
    return None


def has_unconsumed_irrecoverable_transaction_grant(
    record: SessionRecord,
    head_sha: str | None,
    *,
    policy_digest: str | None = None,
) -> dict[str, object] | None:
    """Return an irrecoverable-transaction grant whose successor slot is not yet consumed."""

    if failed_attempts_for_head(record, head_sha) >= MAX_FAILED_ATTEMPTS:
        return None
    existing = has_irrecoverable_transaction_grant(
        record, head_sha, policy_digest=policy_digest
    )
    if existing is None or record.reservation_id is not None:
        return None
    retired_res = existing.get("consumed_reservation_id")
    if not isinstance(retired_res, str) or not retired_res:
        return None
    expected_last = hashlib.sha256(
        f"{retired_res}|irrecoverable-retired".encode("utf-8")
    ).hexdigest()
    if record.last_committed_reservation_id == expected_last:
        return existing
    return None


def transition_irrecoverable_transaction(
    ledger: SessionLedger,
    identity: SessionIdentity,
    *,
    actor: str,
    head_sha: str | None = None,
    policy_digest: str | None = None,
    command_id: str | None = None,
    now: datetime | None = None,
) -> tuple[SessionRecord, bool]:
    """Retire an irrecoverable committed transaction and authorize one successor."""

    if not isinstance(actor, str) or not actor.strip():
        raise ReviewInputError("irrecoverable transaction transition requires an actor")
    effective_now = _aware_now(now)
    applied = False

    def mutate(record: SessionRecord) -> SessionRecord:
        nonlocal applied
        tx = record.transaction
        if tx is None:
            existing = has_unconsumed_irrecoverable_transaction_grant(
                record,
                head_sha or record.failed_attempts_head_sha,
                policy_digest=policy_digest,
            )
            if existing is not None:
                return record
            raise ReviewInputError(
                "session has no committed transaction awaiting publication recovery"
            )
        if tx.phase not in {"publication_pending", "publication_failed"}:
            raise ReviewInputError(
                f"transaction in phase {tx.phase!r} is not eligible for irrecoverable transition"
            )
        if head_sha is not None and tx.head_sha != head_sha:
            raise ReviewInputError(
                "irrecoverable transaction head does not match current head"
            )
        effective_policy_digest = policy_digest or tx.policy_digest
        next_generation = _next_generation(record)
        base_failed = (
            0
            if record.failed_attempts_head_sha not in {None, tx.head_sha}
            else record.failed_attempts
        )
        next_failed = min(base_failed + 1, MAX_FAILED_ATTEMPTS)
        grant_cmd_id = f"irrecoverable-tx:{tx.transaction_id[:32]}"
        if command_id:
            grant_cmd_id = (
                f"irrecoverable-tx:{command_id[:20]}:{tx.transaction_id[:24]}"[:64]
            )
        grant_entry: dict[str, object] = {
            "command_id": grant_cmd_id,
            "actor": actor.strip(),
            "head_sha": tx.head_sha,
            "policy_digest": effective_policy_digest,
            "issued_at": _format_datetime(effective_now),
            "expires_at": record.expires_at,
            "consumed_reservation_id": tx.reservation_id,
            "consumed_generation": next_generation,
        }
        prior_grants = list(
            record.continuation_grants[-(MAX_STORED_CONTINUATION_GRANTS - 1) :]
        )
        prior_grants.append(grant_entry)
        retired_marker = hashlib.sha256(
            f"{tx.reservation_id}|irrecoverable-retired".encode("utf-8")
        ).hexdigest()
        applied = True
        return record.evolve(
            now=effective_now,
            generation=next_generation,
            reservation_id=None,
            reserved_slot=None,
            reservation_owner=None,
            last_committed_reservation_id=retired_marker,
            failed_attempts=next_failed,
            failed_attempts_head_sha=tx.head_sha,
            transaction=None,
            operator_paused=False,
            continuation_grants=tuple(prior_grants),
        )

    updated = ledger.replace(identity, mutate, now=effective_now)
    return updated, applied


def has_unconsumed_successor_grant(
    record: SessionRecord,
    head_sha: str | None,
    *,
    policy_digest: str | None = None,
) -> dict[str, object] | None:
    """Return the active unconsumed successor continuation grant for ``head_sha``."""

    if not isinstance(head_sha, str) or not head_sha:
        return None
    for item in reversed(record.continuation_grants):
        command_id = item.get("command_id")
        if (
            isinstance(command_id, str)
            and command_id.startswith("successor:")
            and item.get("head_sha") == head_sha
            and item.get("consumed_reservation_id") is None
            and item.get("consumed_generation") is None
            and (policy_digest is None or item.get("policy_digest") == policy_digest)
        ):
            return dict(item)
    return None


def grant_successor_continuation(
    ledger: SessionLedger,
    identity: SessionIdentity,
    *,
    actor: str,
    actor_role: str | None = None,
    head_sha: str | None = None,
    policy_digest: str | None = None,
    command_id: str | None = None,
    expected_generation: int | None = None,
    now: datetime | None = None,
) -> tuple[SessionRecord, bool]:
    """Issue an explicit, bounded, idempotent successor continuation grant."""

    if not isinstance(actor, str) or not actor.strip():
        raise ReviewInputError("successor continuation grant requires an actor")
    if actor_role not in {"maintain", "admin", "OWNER", "MEMBER"}:
        raise ReviewInputError(
            "successor continuation grant requires maintain or admin permission"
        )
    effective_now = _aware_now(now)
    applied = False

    def mutate(record: SessionRecord) -> SessionRecord:
        nonlocal applied
        if record.reservation_id is not None:
            raise ReviewInputError(
                "cannot grant successor continuation while a session reservation is held"
            )
        if record.transaction is not None and record.transaction.phase in {
            "analysis",
            "publication_pending",
            "publication_failed",
        }:
            raise ReviewInputError(
                f"cannot grant successor continuation while transaction is in phase {record.transaction.phase!r}"
            )
        target_head = (
            head_sha
            or record.failed_attempts_head_sha
            or (record.transaction.head_sha if record.transaction is not None else None)
        )
        if not isinstance(target_head, str) or not re.fullmatch(
            r"[a-f0-9]{40}", target_head
        ):
            raise ReviewInputError(
                "successor continuation grant requires a valid 40-char hex head SHA"
            )
        if (
            record.failed_attempts_head_sha is not None
            and record.failed_attempts_head_sha != target_head
        ):
            raise ReviewInputError(
                "successor continuation grant head does not match failed-attempt head"
            )
        effective_policy_digest = (
            policy_digest
            or (
                record.transaction.policy_digest
                if record.transaction is not None
                else None
            )
            or ReviewConvergencePolicy().digest()
        )
        if not isinstance(effective_policy_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", effective_policy_digest
        ):
            raise ReviewInputError(
                "successor continuation grant requires a valid 64-char hex policy digest"
            )
        existing = has_unconsumed_successor_grant(
            record, target_head, policy_digest=effective_policy_digest
        )
        if existing is not None:
            if expected_generation is not None:
                existing_cmd = str(existing.get("command_id", ""))
                if (
                    record.generation != expected_generation
                    and not existing_cmd.startswith(f"successor:{expected_generation}:")
                ):
                    raise ReviewInputError(
                        "successor continuation grant generation does not match"
                    )
            if record.operator_paused:
                applied = True
                return record.evolve(
                    now=effective_now,
                    generation=_next_generation(record),
                    operator_paused=False,
                )
            return record

        if expected_generation is not None and record.generation != expected_generation:
            raise ReviewInputError(
                "successor continuation grant generation does not match"
            )
        current_failed = failed_attempts_for_head(record, target_head)
        if current_failed < DEFAULT_MAX_FAILED_ATTEMPTS:
            raise ReviewInputError(
                "failed-attempt allowance is not exhausted; successor grant is not needed"
            )
        if current_failed >= MAX_FAILED_ATTEMPTS:
            raise ReviewInputError(
                "hard failed-attempt ceiling is reached; push a new head to continue"
            )
        raw_token = (command_id or target_head[:16]).strip()
        safe_token = re.sub(r"[^A-Za-z0-9._:-]+", "-", raw_token).strip("-") or "grant"
        grant_cmd_id = f"successor:{record.generation}:{safe_token}"[:128]
        grant_entry: dict[str, object] = {
            "command_id": grant_cmd_id,
            "actor": actor.strip(),
            "head_sha": target_head,
            "policy_digest": effective_policy_digest,
            "issued_at": _format_datetime(effective_now),
            "expires_at": record.expires_at,
            "consumed_reservation_id": None,
            "consumed_generation": None,
        }
        prior_grants = list(
            record.continuation_grants[-(MAX_STORED_CONTINUATION_GRANTS - 1) :]
        )
        prior_grants.append(grant_entry)
        applied = True
        return record.evolve(
            now=effective_now,
            generation=_next_generation(record),
            operator_paused=False,
            continuation_grants=tuple(prior_grants),
        )

    updated = ledger.replace(identity, mutate, now=effective_now)
    return updated, applied


def _publication_state_mutation(
    transaction: ReviewTransaction,
    *,
    published: bool,
    now: datetime | None = None,
) -> Callable[[SessionRecord], SessionRecord]:
    """Advance publication state without charging the failed-attempt budget.

    Publication failures remain retryable.  The failed-attempt counter is
    reserved for inference or reservation failures, so a retry of a
    ``publication_failed`` transaction does not consume another analysis
    attempt.
    """

    if not isinstance(transaction, ReviewTransaction):
        raise ReviewInputError("review transaction is invalid")
    target = "publication_succeeded" if published else "publication_failed"

    def mutate(record: SessionRecord) -> SessionRecord:
        current = record.transaction
        if current is None or current.transaction_id != transaction.transaction_id:
            raise ReviewInputError("durable transaction does not match")
        if not current.logical_identity_matches(transaction):
            raise ReviewInputError("publication transaction identity does not match")
        # ``current`` is the durable ReviewTransaction, not the enclosing
        # SessionRecord. Older bound result artifacts are allowed to replay;
        # only a caller that claims a generation newer than the durable
        # transaction is stale. The marker-generation check below refers to
        # this same transaction generation and is skipped for an owned
        # terminal replay.
        if transaction.generation > current.generation:
            raise ReviewInputError("publication transaction generation is stale")
        # A durable success is terminal for the publication side of the
        # transaction. A later timeout or duplicate failure observation must
        # not downgrade it or raise while the caller is retrying at-least-once
        # delivery.
        if current.phase in {"publication_succeeded", "publication_suppressed"}:
            return record
        if current.phase == "publication_failed" and not published:
            return record
        if current.phase not in {"publication_pending", "publication_failed"}:
            raise ReviewInputError("publication phase transition is invalid")
        if (
            transaction.result_sha256 is None
            or current.result_sha256 != transaction.result_sha256
        ):
            raise ReviewInputError("publication result digest does not match")
        next_generation = _next_generation(record)
        updated_transaction = ReviewTransaction(
            transaction_id=current.transaction_id,
            repository=current.repository,
            pull_request=current.pull_request,
            base_sha=current.base_sha,
            head_sha=current.head_sha,
            policy_digest=current.policy_digest,
            configuration_digest=current.configuration_digest,
            evidence_digest=current.evidence_digest,
            reservation_id=current.reservation_id,
            # Caller ownership/accounting checkpoints advance the root without
            # advancing this business transaction. Preserve its own next step,
            # including the exact checkpoint-placeholder relationship checked
            # by blocker admission after a stopped-owner recovery.
            generation=(
                current.generation + 1
                if record.api_run_accounting is not None
                else next_generation
            ),
            phase=target,
            result_sha256=current.result_sha256,
        )
        return record.evolve(
            now=now, generation=next_generation, transaction=updated_transaction
        )

    return mutate


def complete_review_publication(
    ledger: SessionLedger,
    identity: SessionIdentity,
    transaction: ReviewTransaction,
    *,
    published: bool,
    now: datetime | None = None,
) -> SessionRecord:
    """The application's sole terminal publication completion/failed outcome."""
    mutation = _publication_state_mutation(transaction, published=published, now=now)
    claim = getattr(ledger, "claim_publication_completion", None)
    if callable(claim):
        claim(identity, transaction, published)
    return ledger.replace(identity, mutation, now=now)


def mark_review_publication_unconfirmed(
    ledger: SessionLedger,
    identity: SessionIdentity,
    transaction: ReviewTransaction,
    *,
    now: datetime | None = None,
) -> SessionRecord:
    """Fence a pre-publication write; never claim terminal completion authority.

    Retain the existing publication_failed uncertainty state and every exact
    transaction/result/generation check. This is the original write-intent
    mutation, not a finalizer or an extra state/allowance.
    """
    mutation = _publication_state_mutation(transaction, published=False, now=now)
    return ledger.replace(identity, mutation, now=now)


def suppress_review_publication(
    ledger: SessionLedger,
    identity: SessionIdentity,
    transaction: ReviewTransaction,
    *,
    now: datetime | None = None,
) -> SessionRecord:
    """Terminally record a policy handoff without making it retryable publish work."""

    if not isinstance(transaction, ReviewTransaction):
        raise ReviewInputError("review transaction is invalid")

    def mutate(record: SessionRecord) -> SessionRecord:
        current = record.transaction
        if current is None or current.transaction_id != transaction.transaction_id:
            raise ReviewInputError("durable transaction does not match")
        if not current.logical_identity_matches(transaction):
            raise ReviewInputError("publication transaction identity does not match")
        if current.phase == "publication_suppressed":
            return record
        if current.phase not in {"publication_pending", "publication_failed"}:
            raise ReviewInputError(
                "publication suppression phase transition is invalid"
            )
        if (
            transaction.result_sha256 is None
            or current.result_sha256 != transaction.result_sha256
        ):
            raise ReviewInputError("publication result digest does not match")
        return record.evolve(
            now=now,
            generation=_next_generation(record),
            transaction=current.with_phase("publication_suppressed"),
        )

    return ledger.replace(identity, mutate, now=now)


def record_admitted_blocker_progress(
    ledger: SessionLedger,
    identity: SessionIdentity,
    transaction: ReviewTransaction,
    *,
    blocker_set_sha256: str,
    blocker_count: int,
    suppress_publication: bool,
    now: datetime | None = None,
) -> SessionRecord:
    """Persist one post-admission blocker identity and optionally hand off.

    The only writer is the publication boundary after it has admitted the
    exact result it will hand to the GitHub publisher. A suppression and its
    evidence are consequently one compare-and-swap transition, rather than a
    retryable publication failure.
    """

    if not isinstance(transaction, ReviewTransaction):
        raise ReviewInputError("review transaction is invalid")
    if not isinstance(blocker_set_sha256, str) or not _SHA256.fullmatch(
        blocker_set_sha256
    ):
        raise ReviewInputError("blocker set digest is invalid")
    _require_bounded_int(
        blocker_count,
        label="blocker set count",
        minimum=0,
        maximum=MAX_DURABLE_BLOCKER_COUNT,
    )
    if not isinstance(suppress_publication, bool):
        raise ReviewInputError("publication suppression flag is invalid")

    def mutate(record: SessionRecord) -> SessionRecord:
        current = record.transaction
        if current is None or current.transaction_id != transaction.transaction_id:
            raise ReviewInputError("durable transaction does not match")
        if not current.logical_identity_matches(transaction):
            raise ReviewInputError("publication transaction identity does not match")
        if transaction.generation > current.generation:
            raise ReviewInputError("publication transaction generation is stale")
        if (
            transaction.result_sha256 is None
            or current.result_sha256 != transaction.result_sha256
        ):
            raise ReviewInputError("publication result digest does not match")
        history = record.convergence_history
        if not isinstance(history, Mapping) or history.get("state") != "completed":
            raise ReviewInputError("durable convergence history is unavailable")
        progress = history.get("progress")
        if not isinstance(progress, list) or not progress:
            raise ReviewInputError("durable convergence progress is unavailable")
        if len(progress) > MAX_CONVERGENCE_PROGRESS_ENTRIES:
            raise ReviewInputError(
                "durable convergence progress exceeds the configured bound"
            )
        last = progress[-1]
        if not isinstance(last, Mapping) or last.get("event") != "completed":
            raise ReviewInputError("durable convergence progress is invalid")
        item = {
            "event": "completed",
            "generation": current.generation,
            "blocker_set_sha256": blocker_set_sha256,
            "blocker_count": blocker_count,
            "transaction_id": current.transaction_id,
        }
        marker_matches = (
            last.get("blocker_set_sha256") == blocker_set_sha256
            and last.get("blocker_count") == blocker_count
            and last.get("transaction_id") == current.transaction_id
        )
        if (
            current.phase in {"publication_failed", "publication_succeeded"}
            and marker_matches
        ):
            # A retry can observe a terminal transaction whose record
            # generation advanced while the admitted progress marker still
            # carries the transaction generation from before publication. The
            # transaction id is the ownership witness: an older round with
            # the same blocker set must never be treated as this retry.
            if current.phase == "publication_failed" and suppress_publication:
                updated_history = dict(history)
                updated_history["progress"] = [*progress[:-1], item]
                return record.evolve(
                    now=now,
                    generation=_next_generation(record),
                    transaction=current.with_phase("publication_suppressed"),
                    convergence_history=updated_history,
                )
            # The exact marker is already durable; do not charge a second
            # generation for a retry that remains failed or already succeeded.
            return record
        if current.phase == "publication_suppressed":
            if dict(last) != item:
                raise ReviewInputError("durable blocker progress does not match")
            return record
        if (
            current.phase in {"publication_pending", "publication_failed"}
            and dict(last) == item
        ):
            # A retry can observe the same admitted result after the caller
            # crashed between this CAS and the GitHub write. The progress
            # evidence is already durable; do not charge another generation.
            return record
        failed_checkpoint_placeholder = False
        if (
            current.phase == "publication_failed"
            and set(last) == {"event", "generation"}
            and type(last.get("generation")) is int
            and last["generation"] + 1 == current.generation
            and record.last_committed_reservation_id == current.reservation_id
        ):
            # A publication can become unconfirmed before preparation admits
            # its blocker set (including the pre-POST retention crash fence).
            # Only the exact checkpoint baseline and reservation can bind this
            # markerless placeholder to the current durable transaction. An
            # older/foreign blocker-bearing marker never takes this path.
            checkpoint = read_session_baseline(ledger, record)
            failed_checkpoint_placeholder = (
                checkpoint is not None
                and checkpoint.complete
                and checkpoint.generation == last["generation"]
                and checkpoint.cache_key.repository == current.repository
                and checkpoint.cache_key.pull_request == current.pull_request
                and checkpoint.cache_key.base_sha == current.base_sha
                and checkpoint.cache_key.head_sha == current.head_sha
                and checkpoint.policy_digest == current.policy_digest
            )
        if (
            last.get("generation") != current.generation
            and not failed_checkpoint_placeholder
        ):
            raise ReviewInputError(
                "durable convergence progress does not match transaction"
            )
        if current.phase != "publication_pending" and not failed_checkpoint_placeholder:
            raise ReviewInputError("publication progress phase transition is invalid")
        if set(last) == set(item) and dict(last) != item:
            raise ReviewInputError("durable blocker progress does not match")
        # Admission replaces the transaction's final checkpoint placeholder;
        # it never appends a fourth lifecycle entry. The loader and this
        # explicit guard keep a bypassed in-memory record from silently
        # evicting or reordering older evidence.
        updated_history = dict(history)
        updated_history["progress"] = [*progress[:-1], item]
        next_transaction = (
            current.with_phase("publication_suppressed")
            if suppress_publication
            else current
        )
        return record.evolve(
            now=now,
            generation=_next_generation(record),
            transaction=next_transaction,
            convergence_history=updated_history,
        )

    return ledger.replace(identity, mutate, now=now)


def reclaim_abandoned_review_transaction(
    ledger: SessionLedger,
    identity: SessionIdentity,
    *,
    reservation_id: str,
    expected_generation: int,
    now: datetime | None = None,
) -> SessionRecord:
    """Release an explicitly owned abandoned analysis reservation.

    A crash can occur between the reservation CAS and transaction attachment,
    leaving a reservation-only record. The owner and generation checks still
    make that recovery bounded and safe, so reclaim both that state and a
    fully attached analysis transaction.
    """

    loaded = ledger.load(identity, now=now)
    if loaded.status not in {"ok", "migrated"} or loaded.record is None:
        raise ReviewInputError(f"session ledger load failed: {loaded.status}")
    record = loaded.record
    if (
        record.generation != expected_generation
        or record.reservation_id != reservation_id
        or record.reserved_slot not in {"initial", "verification"}
    ):
        raise ReviewInputError("abandoned transaction ownership or generation mismatch")
    if record.transaction is not None and (
        record.transaction.reservation_id != reservation_id
        or record.transaction.phase != "analysis"
    ):
        raise ReviewInputError("abandoned transaction ownership or generation mismatch")
    return ledger.replace(
        identity,
        lambda current: mutate_abort(
            current,
            reservation_id=reservation_id,
            expected_generation=expected_generation,
            now=now,
        ),
        now=now,
    )


def complete_session_round(
    ledger: SessionLedger,
    identity: SessionIdentity,
    prepared: PreparedSessionRound,
    *,
    published: bool,
    now: datetime | None = None,
) -> SessionRecord:
    """Commit a reservation after a successful write, or abort it."""

    if prepared.reservation_id is None:
        return prepared.record
    # A publisher retry can invoke completion after a previous commit already
    # advanced the durable generation. Only that exact reservation replay may
    # use the fresh record; otherwise retain the generation captured at reserve
    # so a competing writer still loses the CAS race instead of double-counting.
    loaded = ledger.load(identity, now=now)
    if (
        loaded.status in {"ok", "migrated"}
        and loaded.record is not None
        and loaded.record.reservation_id is None
        and loaded.record.last_committed_reservation_id == prepared.reservation_id
    ):
        if not published:
            raise ReviewInputError(
                "session completion outcome conflicts with committed reservation"
            )
        slot = prepared.record.reserved_slot
        if slot is None:
            raise ReviewInputError("prepared session reservation is invalid")
        expected_initial, expected_verification, expected_failed = _expected_after_slot(
            prepared.record, slot
        )
        if (
            loaded.record.generation == prepared.record.generation + 1
            and loaded.record.completed_initial_reviews == expected_initial
            and loaded.record.completed_verification_rounds == expected_verification
            and loaded.record.failed_attempts == expected_failed
        ):
            return loaded.record
        raise ReviewInputError("session completion replay is inconsistent")
    if published:
        return ledger.commit(
            identity,
            reservation_id=prepared.reservation_id,
            expected_generation=prepared.record.generation,
            now=now,
        )
    return ledger.abort(
        identity,
        reservation_id=prepared.reservation_id,
        expected_generation=prepared.record.generation,
        now=now,
    )


def session_reservation_id(
    *, repository: str, pull_request: int, head_sha: str, kind: str
) -> str:
    payload = f"{repository}|{pull_request}|{head_sha}|{kind}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def admission_diagnostic(decision: RoundAdmissionDecision) -> str:
    """Return a closed public diagnostic for a refused admission decision."""

    if not isinstance(decision, RoundAdmissionDecision):
        raise ReviewInputError("round admission decision is invalid")
    if decision.handoff_reason is not None:
        return decision.handoff_reason
    if decision.handoff:
        return "incomplete-coverage"
    return "already_published"


def should_skip_automation(
    decision: RoundAdmissionDecision, *, inference: bool
) -> bool:
    """Whether C5 must refuse inference or a new automated review event.

    Same-head duplicates, publication recovery, and transport retries do not
    infer again, but they may still publish an already-produced result.
    Handoff states refuse new REQUEST_CHANGES; ``may_emit_approve`` can still
    finalize an independently eligible last-round result.
    """

    if not isinstance(decision, RoundAdmissionDecision):
        raise ReviewInputError("round admission decision is invalid")
    if decision.admit:
        return False
    if inference:
        return True
    if decision.may_emit_approve:
        return False
    return decision.handoff


def record_session_failed_attempt(
    ledger: SessionLedger,
    identity: SessionIdentity,
    *,
    reservation_id: str,
    head_sha: str | None = None,
    expected_generation: int | None = None,
    now: datetime | None = None,
) -> SessionRecord:
    """Abort a held round reservation and charge the failed-attempt budget."""

    loaded = ledger.load(identity, now=now)
    if loaded.status in {"missing", "expired"} or loaded.record is None:
        record = ledger.initialize(identity, now=now)
    elif loaded.status in {"ok", "migrated"}:
        record = loaded.record
    else:
        raise ReviewInputError(f"session ledger load failed: {loaded.status}")
    if expected_generation is not None:
        if (
            record.generation != expected_generation
            or record.reservation_id != reservation_id
        ):
            raise ReviewInputError(
                "failed-attempt cleanup ownership or generation mismatch"
            )
    if record.reservation_id == reservation_id:
        record = ledger.abort(
            identity,
            reservation_id=reservation_id,
            expected_generation=record.generation,
            now=now,
        )
    fail_id = hashlib.sha256(
        f"{reservation_id}|failed-attempt".encode("utf-8")
    ).hexdigest()
    reserved = ledger.reserve(
        identity,
        slot="failed-attempt",
        reservation_id=fail_id,
        expected_generation=record.generation,
        head_sha=head_sha,
        now=now,
    )
    return ledger.commit(
        identity,
        reservation_id=fail_id,
        expected_generation=reserved.generation,
        now=now,
    )


def load_session_status(
    record: SessionRecord, *, now: datetime | None = None
) -> SessionLoadResult:
    if record.expired(now=now):
        return SessionLoadResult(status="expired")
    return SessionLoadResult(status="ok", record=record)


class InMemorySessionLedger:
    """Test and single-process ledger. Not a hosted database."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, int], SessionRecord] = {}
        self._retained_results: dict[tuple[str, int, str, str, str], ReviewResult] = {}
        self._latest_retained_results: dict[tuple[str, int], ReviewResult] = {}

    def retain_checkpoint_result(self, result: ReviewResult) -> None:
        tx = result.transaction
        if tx is None:
            return
        digest = tx.result_sha256 or result.content_digest()
        key = (tx.repository, tx.pull_request, tx.head_sha, digest, tx.transaction_id)
        self._retained_results[key] = result
        self._latest_retained_results[(tx.repository, tx.pull_request)] = result

    def load_retained_checkpoint_result(
        self,
        identity: SessionIdentity,
        *,
        transaction: ReviewTransaction | None = None,
        head_sha: str | None = None,
    ) -> ReviewResult | None:
        if transaction is not None and transaction.result_sha256 is not None:
            exact = self._retained_results.get(
                (
                    identity.repository,
                    identity.pull_request,
                    transaction.head_sha,
                    transaction.result_sha256,
                    transaction.transaction_id,
                )
            )
            if exact is not None:
                return exact
        latest = self._latest_retained_results.get(
            (identity.repository, identity.pull_request)
        )
        if latest is None:
            return None
        if head_sha is not None and latest.transaction is not None:
            if latest.transaction.head_sha != head_sha:
                return None
        return latest

    def load(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionLoadResult:
        record = self._records.get((identity.repository, identity.pull_request))
        if record is None:
            return SessionLoadResult(status="missing")
        if identity.repository_id is not None and record.repository_id not in {
            None,
            identity.repository_id,
        }:
            return SessionLoadResult(status="conflict")
        return load_session_status(record, now=now)

    def initialize(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
        expires_at: datetime | str | None = None,
    ) -> SessionRecord:
        current = self.load(identity, now=now)
        if current.status in {"ok", "migrated"}:
            raise ReviewInputError("session already exists")
        if current.status in {"integrity-failed", "conflict", "expired"}:
            raise ReviewInputError(f"session ledger load failed: {current.status}")
        record = SessionRecord.create(identity, now=now, expires_at=expires_at)
        self._records[(identity.repository, identity.pull_request)] = record
        return record

    def reenroll(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Recover an expired or absent in-memory session and enroll a fresh one."""

        loaded = self.load(identity, now=now)
        if loaded.status not in {"expired", "missing"}:
            raise ReviewInputError(
                "only an expired or witness-only session can be re-enrolled"
            )
        retained = self._records.get((identity.repository, identity.pull_request))
        if retained is not None and retained.quarantine_lineage is not None:
            raise ReviewInputError(
                "quarantine lineage requires retained authority and cannot be re-enrolled"
            )
        self._records.pop((identity.repository, identity.pull_request), None)
        return self.initialize(identity, now=now)

    def replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        loaded = self.load(identity, now=now)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("session record is missing")
        updated = mutate(loaded.record)
        from .quarantine_lineage import require_lineage_successor

        require_lineage_successor(
            loaded.record.quarantine_lineage, updated.quarantine_lineage
        )
        self._records[(identity.repository, identity.pull_request)] = updated
        return updated

    # Kept as a compatibility shim for older in-process callers. New code
    # must use the public CAS seam above.
    def _replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(identity, mutate, now=now)

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        head_sha: str | None = None,
        rescan_request: AuthorizedRescan | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
            identity,
            lambda record: mutate_reserved(
                record,
                slot=slot,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                head_sha=head_sha,
                rescan_request=rescan_request,
                now=now,
            ),
            now=now,
        )

    def commit(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
            identity,
            lambda record: mutate_commit(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )

    def abort(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
            identity,
            lambda record: mutate_abort(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )


def _safe_ledger_name(repository: str) -> str:
    """Return an injective, filesystem-safe name for a repository slug."""

    return repository.replace("/", "%2F")


def _trusted_evidence_resolver(
    ledger: object,
) -> tuple[Callable[[], str], Callable[..., object]]:
    producer = getattr(ledger, "evidence_producer", None)
    reader = getattr(ledger, "read_evidence", None)
    budget = getattr(ledger, "evidence_budget", None)
    if (
        not callable(producer)
        or not callable(reader)
        or not isinstance(budget, EvidenceReadBudget)
    ):
        raise ReviewInputError(
            "partition authority requires a trusted resolver and shared budget"
        )
    budget.check()
    return producer, reader


def read_session_dispositions(
    ledger: object, record: SessionRecord
) -> tuple[Mapping[str, object], ...]:
    """Read exactly one authenticated decision authority; never fall back."""
    if record.disposition_snapshot is None:
        return record.dispositions
    from .disposition_snapshot import normalize_snapshot, snapshot_document

    root = normalize_snapshot(record.disposition_snapshot)
    assert root is not None
    manifest = root["state_manifest"]
    binding = manifest["binding"]
    producer, reader = _trusted_evidence_resolver(ledger)
    if binding["producer"] != producer():
        raise ReviewInputError("disposition snapshot producer does not match session")
    identity = SessionIdentity(
        record.repository, record.pull_request, record.repository_id
    )
    document = reader(identity, manifest, expected_binding=binding)
    return snapshot_document(
        document, result_digest=root["result_digest"], item_count=manifest["item_count"]
    )


def disposition_snapshot_capacity(record: SessionRecord) -> int:
    """Reserve a decision root beside retained evidence and lifecycle growth."""
    from .baseline import MAX_HISTORY_ENVELOPE_RESERVE_BYTES
    from .disposition_snapshot import MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES

    shell = record.to_dict()
    _reserve_publication_staging(shell, record)
    shell.pop("disposition_snapshot", None)
    shell["dispositions"] = []
    shell.update(
        generation=MAX_GENERATION,
        completed_initial_reviews=DIAGNOSTIC_ROUND_CEILING,
        completed_verification_rounds=DIAGNOSTIC_ROUND_CEILING,
        failed_attempts=MAX_FAILED_ATTEMPTS,
        failed_attempts_head_sha="a" * 64,
        reservation_id="a" * 64,
        reserved_slot="failed-attempt",
        last_committed_reservation_id="b" * 64,
        reservation_owner={"run_id": "9" * 19, "head_sha": "a" * 40},
    )
    future = ReviewTransaction.create(
        repository=record.repository,
        pull_request=record.pull_request,
        base_sha="a" * 40,
        head_sha="b" * 40,
        policy_digest="c" * 64,
        configuration_digest="d" * 64,
        evidence_digest="e" * 64,
        reservation_id="f" * 64,
        generation=MAX_GENERATION,
    ).to_dict()
    future.update(phase="publication_suppressed", result_sha256="a" * 64)
    shell["transaction"] = future
    shell["updated_at"] = max(
        str(shell["updated_at"]), "9999-12-31T23:59:59.999999Z", key=len
    )
    if record.assessment_queue is not None:
        shell["assessment_queue"] = "q" * MAX_ASSESSMENT_QUEUE_ROOT_BYTES
    return max(
        0,
        min(
            MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES,
            MAX_SESSION_RECORD_BYTES
            - len(canonical_bytes(shell))
            - len(',"disposition_snapshot":')
            - MAX_HISTORY_ENVELOPE_RESERVE_BYTES,
        ),
    )


def stage_session_dispositions(
    ledger: object,
    record: SessionRecord,
    decisions: Sequence[Mapping[str, object]],
    *,
    head_sha: str | None,
    now: datetime | None,
) -> SessionRecord:
    """Stage one full snapshot inside the caller's serialized CAS mutation."""
    if getattr(ledger, "enable_disposition_snapshot_writes", False) is not True:
        raise ReviewInputError(
            "disposition snapshot writer is disabled; decisions retained"
        )
    transaction = record.transaction
    if (
        transaction is None
        or transaction.head_sha != head_sha
        or transaction.result_sha256 is None
    ):
        raise ReviewInputError(
            "disposition snapshot requires the current reviewed transaction"
        )
    values = _stored_dispositions(decisions, snapshot=True)
    producer, _reader = _trusted_evidence_resolver(ledger)
    stage = getattr(ledger, "stage_evidence", None)
    if not callable(stage):
        raise ReviewInputError("disposition snapshot staging is unavailable")
    document = {
        "schema_version": "1.0",
        "result_digest": transaction.result_sha256,
        "dispositions": list(values),
    }
    binding = {
        "repository": record.repository,
        "repository_id": record.repository_id,
        "pull_request": record.pull_request,
        "base_sha": transaction.base_sha,
        "head_sha": transaction.head_sha,
        "policy_digest": transaction.policy_digest,
        "configuration_digest": transaction.configuration_digest,
        "generation": record.generation + 1,
        "producer": producer(),
        "purpose": "dispositions",
        "schema_version": "1.0",
    }
    identity = SessionIdentity(
        record.repository, record.pull_request, record.repository_id
    )
    root = {"schema_version": "1.0", "result_digest": transaction.result_sha256}
    capacity = (
        disposition_snapshot_capacity(record)
        - len(canonical_bytes({**root, "state_manifest": {}}))
        + 2
    )
    if capacity <= 0:
        raise ReviewInputError("disposition snapshot has no future root capacity")
    manifest = stage(
        identity,
        binding=binding,
        document=document,
        item_count=len(values),
        max_manifest_bytes=capacity,
    )
    updated = record.evolve(
        now=now,
        generation=record.generation + 1,
        dispositions=(),
        disposition_snapshot={**root, "state_manifest": manifest},
    )
    if read_session_dispositions(ledger, updated) != values:
        raise ReviewInputError("disposition snapshot readback differs")
    return updated


def read_session_assessment_queue(
    ledger: object, record: SessionRecord
) -> object | None:
    """Reconstruct C's whole journal; C owns payload/count/receipt validation."""
    root = record.assessment_queue
    if root is None:
        return None
    manifest = root["state_manifest"]
    assert isinstance(manifest, dict)
    binding = manifest["binding"]
    assert isinstance(binding, dict)
    producer, reader = _trusted_evidence_resolver(ledger)
    if binding["producer"] != producer():
        raise ReviewInputError(
            "assessment queue producer does not match authenticated session"
        )
    identity = SessionIdentity(
        record.repository, record.pull_request, repository_id=record.repository_id
    )
    document = reader(identity, manifest, expected_binding=binding)
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != binding["schema_version"]
    ):
        raise ReviewInputError("assessment journal schema is unsupported")
    cache = getattr(ledger, "_history_documents", None)
    if indexed_document(document):
        if not getattr(ledger, "enable_history_graph", False) or cache is None:
            raise ReviewInputError(
                "indexed history requires explicit reader enablement"
            )
        payload = history_payload(record, document)
        encoded = canonical_bytes(payload)
        key = _history_cache_key(record)
        prospective = {**cache, key: encoded}
        if (
            len(prospective) > 64
            or sum(len(value) for value in prospective.values()) > 2_097_152
        ):
            raise ReviewInputError("history reader cache exceeds finite bound")
        cache[key] = encoded
        if getattr(ledger, "_activation_ticket", None) is not None:
            for row in payload["sources"]:
                _read_history_child(ledger, identity, payload, row)
    elif cache is not None and getattr(ledger, "enable_history_graph", False):
        if len(cache) >= 64 and _history_cache_key(record) not in cache:
            raise ReviewInputError("history reader cache exceeds finite bound")
        cache[_history_cache_key(record)] = b""
    return document


def _history_cache_key(record: SessionRecord) -> str:
    if record.assessment_queue is None:
        return ""
    return hashlib.sha256(
        canonical_bytes(record.assessment_queue["state_manifest"])
    ).hexdigest()


def _history_cached(ledger: Any, record: SessionRecord) -> dict[str, Any] | None:
    key = _history_cache_key(record)
    if not key:
        return None
    if key not in ledger._history_documents:
        raise ReviewInputError("complete queue reference inventory was not read")
    value = ledger._history_documents[key]
    return json.loads(value) if value else None


def _history_extra_ids(ledger: Any, record: SessionRecord) -> tuple[str, ...]:
    if not ledger.enable_history_graph:
        return ()
    payload = _history_cached(ledger, record)
    return (
        tuple(row["reference"]["storage_id"] for row in payload["sources"])
        if payload is not None
        else ()
    )


def _history_legacy_write_guard(ledger: Any, record: SessionRecord) -> None:
    if ledger.enable_history_graph and _history_cached(ledger, record) is not None:
        raise ReviewInputError(
            "indexed history mutation requires a sealed activation tail"
        )


def _history_preserve_sources(
    ledger: Any, before: SessionRecord, after: SessionRecord
) -> None:
    if not ledger.enable_history_graph:
        return
    old, new = _history_cached(ledger, before), _history_cached(ledger, after)
    if old is None:
        if before.assessment_queue is not None and new is not None:
            raise ReviewInputError(
                "indexed legacy migration requires complete original source proof"
            )
        if new is not None and new["sources"]:
            raise ReviewInputError(
                "initial indexed root cannot invent archived original accounting"
            )
        return
    if new is None:
        raise ReviewInputError(
            "indexed history cannot discard its complete source inventory"
        )

    def identities(
        payload: dict[str, Any], record: SessionRecord
    ) -> dict[str, tuple[str, object]]:
        from .assessment_history_index import IndexedAssessmentJournal

        journal = IndexedAssessmentJournal.from_document(payload["envelope"]["journal"])
        rows = {
            row["source_digest"]: (row["operation_id"], row["read_accounting"])
            for row in payload["sources"]
        }
        current = journal._current()
        if current is not None:
            active = cast(Mapping[str, Any], record.assessment_queue)[
                "active_operation"
            ]
            if (
                not isinstance(active, Mapping)
                or active["source_digest"] != current["source_digest"]
                or active["operation_id"] != current["operation_id"]
            ):
                raise ReviewInputError(
                    "indexed current original accounting carrier differs"
                )
            rows[current["source_digest"]] = (
                current["operation_id"],
                active["read_accounting"],
            )
        return rows

    previous, following = identities(old, before), identities(new, after)
    active = cast(Mapping[str, Any], after.assessment_queue)["active_operation"]
    changing = active["source_digest"] if isinstance(active, Mapping) else None
    for source, (operation, accounting) in previous.items():
        candidate = following.get(source)
        if candidate is None or candidate[0] != operation:
            raise ReviewInputError(
                "indexed history omitted or substituted an original source"
            )
        if source != changing and candidate[1] != accounting:
            raise ReviewInputError("indexed history changed original sealed accounting")
    old_archived = {row["source_digest"]: row for row in old["sources"]}
    new_archived = {row["source_digest"]: row for row in new["sources"]}
    for source, row in old_archived.items():
        if source != changing and new_archived.get(source) != row:
            raise ReviewInputError(
                "indexed history substituted a retained child reference"
            )
    for source, row in new_archived.items():
        if source in old_archived or source not in previous:
            continue
        manifest = cast(Mapping[str, Any], before.assessment_queue)["state_manifest"]
        if len(manifest["parts"]) != 1 or row["reference"] != {
            **manifest["parts"][0],
            "decoded_bytes": manifest["decoded_bytes"],
        }:
            raise ReviewInputError(
                "indexed archival requires exact prior owned current part"
            )


def _validate_queue_retention(before: SessionRecord, after: SessionRecord) -> None:
    from .historical_assessments import require_hold_successor

    require_hold_successor(before.historical_assessments, after.historical_assessments)
    if before.api_run_accounting is not None:
        if after.api_run_accounting is None:
            raise ReviewInputError("API accounting cannot be silently discarded")
        from .api_work import ApiRunAccounting

        previous = ApiRunAccounting.from_document(before.api_run_accounting)
        current = ApiRunAccounting.from_document(after.api_run_accounting)
        if previous.identity != current.identity:
            reference = current.previous_record
            expected = (
                before.rollover_historical_api_accounting(
                    after.api_run_accounting,
                    assessments=(after.historical_assessments or {}).get(
                        "assessments", ()
                    ),
                )
                if reference is not None
                and reference.get("schema_version") == "caller-run-reference-v2"
                else before.rollover_api_accounting(after.api_run_accounting)
            )
            if expected.record_sha256 != after.record_sha256:
                raise ReviewInputError("caller run rollover discarded session state")
        else:
            previous.require_successor(current)
    if before.disposition_snapshot is not None and after.disposition_snapshot is None:
        raise ReviewInputError("disposition snapshot cannot be silently discarded")
    if after.disposition_snapshot is not None and len(
        canonical_bytes(after.disposition_snapshot)
    ) > disposition_snapshot_capacity(after):
        raise ReviewInputError(
            "disposition snapshot exceeds future session growth allocation"
        )
    if before.assessment_queue is not None and after.assessment_queue is None:
        raise ReviewInputError(
            "assessment queue must retain an explicit tombstone, never absence"
        )
    if after.assessment_queue is not None:
        manifest = after.assessment_queue["state_manifest"]
        if len(canonical_bytes(manifest)) > assessment_queue_manifest_capacity(after):
            raise ReviewInputError(
                "assessment queue exceeds future session growth allocation"
            )


def _tail_operation_binding(value: Mapping[str, object]) -> dict[str, object]:
    fields = {
        "operation_id",
        "source_digest",
        "authority_digest",
        "execution_identity",
        "inventory_digest",
        "inventory_generation",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReviewInputError("activation operation binding is invalid")
    for name in fields - {"execution_identity", "inventory_generation"}:
        digest = value[name]
        if (
            not isinstance(digest, str)
            or re.fullmatch(SESSION_SHA256_PATTERN, digest) is None
        ):
            raise ReviewInputError("activation operation digest is invalid")
    if (
        not isinstance(value["execution_identity"], str)
        or re.fullmatch(r"[a-f0-9]{32}", value["execution_identity"]) is None
    ):
        raise ReviewInputError("activation execution identity is invalid")
    _require_bounded_int(
        value["inventory_generation"],
        label="inventory_generation",
        minimum=0,
        maximum=MAX_GENERATION,
    )
    return dict(value)


def _tail_attempt_scope(
    identity: SessionIdentity, reservation_id: str, operation: Mapping[str, object]
) -> str:
    _reservation_id(reservation_id, label="reservation_id")
    return hashlib.sha256(
        canonical_bytes(
            {
                "repository": identity.repository,
                "repository_id": identity.repository_id,
                "pull_request": identity.pull_request,
                "reservation_id": reservation_id,
                "operation": _tail_operation_binding(operation),
            }
        )
    ).hexdigest()


def _tail_part_ids(
    record: SessionRecord, *, history_ids: tuple[str, ...] = ()
) -> tuple[str, ...]:
    manifests = []
    history = record.convergence_history
    baseline = history.get("baseline") if isinstance(history, Mapping) else None
    if isinstance(baseline, dict) and baseline.get("encoding") == PARTITION_ENCODING:
        manifests.append(validate_manifest(baseline))
    if record.assessment_queue is not None:
        state_manifest = record.assessment_queue["state_manifest"]
        if (
            isinstance(state_manifest, dict)
            and state_manifest.get("encoding") == PARTITION_ENCODING
        ):
            manifests.append(validate_manifest(state_manifest))
    return (
        tuple(
            ref["storage_id"]
            for manifest in manifests
            for ref in cast(list[dict[str, Any]], manifest["parts"])
        )
        + history_ids
    )


def _tail_record_shape(record: SessionRecord) -> bytes:
    document = json.loads(canonical_bytes(record.to_dict()))
    document.pop("record_sha256", None)
    root = document.get("assessment_queue")
    if not isinstance(root, dict) or not isinstance(root.get("active_operation"), dict):
        raise ReviewInputError(
            "activation requires a retained operation accounting carrier"
        )
    root["active_operation"].pop("read_accounting", None)
    return canonical_bytes(document)


def _validate_tail_draft(
    before: SessionRecord,
    draft: SessionRecord,
    *,
    operation: Mapping[str, object],
    reservation_id: str,
    budget: EvidenceReadBudget,
    initial_slot: str | None = None,
) -> None:
    SessionRecord.from_dict(draft.to_dict())
    if (
        (draft.repository, draft.repository_id, draft.pull_request)
        != (before.repository, before.repository_id, before.pull_request)
        or (
            before.reservation_id != reservation_id
            if initial_slot is None
            else before.reservation_id is not None
            or before.last_committed_reservation_id == reservation_id
            or draft.reserved_slot != initial_slot
        )
        or draft.reservation_id != reservation_id
        or draft.generation != before.generation + 1
    ):
        raise ReviewInputError(
            "activation draft identity, reservation or generation changed"
        )
    for record in (before, draft):
        root = record.assessment_queue
        if root is None and record is before:
            continue
        if root is None or not isinstance(root["active_operation"], Mapping):
            raise ReviewInputError(
                "activation requires a retained operation accounting carrier"
            )
        active = root["active_operation"]
        binding = {
            name: root[name] if name.startswith("inventory_") else active[name]
            for name in operation
        }
        if (
            binding != operation
            or active["read_accounting"]["deadline_at_ms"] != budget.wall_deadline_ms
            or active["read_accounting"]["calls"] > budget.calls
        ):
            raise ReviewInputError(
                "activation original operation or accounting does not match"
            )
    _validate_queue_retention(before, draft)


def _tail_plan_scope(
    adapter: str,
    before: SessionRecord,
    draft: SessionRecord,
    *,
    operation: Mapping[str, object],
    budget: EvidenceReadBudget,
    scan_pages: int = 0,
    head_sha: str | None = None,
    root_id: int | None = None,
    grant_sha256: str | None = None,
    old_history_ids: tuple[str, ...] = (),
    new_history_ids: tuple[str, ...] = (),
) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "adapter": adapter,
                "current_root": before.record_sha256,
                "draft_shape": hashlib.sha256(_tail_record_shape(draft)).hexdigest(),
                "old_parts": _tail_part_ids(before, history_ids=old_history_ids),
                "new_parts": _tail_part_ids(draft, history_ids=new_history_ids),
                "operation": dict(operation),
                "original_deadline_ms": budget.wall_deadline_ms,
                "scan_pages": scan_pages,
                "head_sha": head_sha,
                "root_id": root_id,
                "grant_sha256": grant_sha256,
            }
        )
    ).hexdigest()


def _seal_tail_accounting(
    draft: SessionRecord,
    seal_accounting: Callable[[SessionRecord, Mapping[str, int]], SessionRecord],
    budget: EvidenceReadBudget,
) -> SessionRecord:
    accounting = {"calls": budget.calls, "deadline_at_ms": budget.wall_deadline_ms}
    sealed = seal_accounting(draft, dict(accounting))
    if not isinstance(sealed, SessionRecord):
        raise ReviewInputError(
            "activation accounting callback returned invalid authority"
        )
    SessionRecord.from_dict(sealed.to_dict())
    root = cast(Mapping[str, Any], sealed.assessment_queue)
    if (
        _tail_record_shape(sealed) != _tail_record_shape(draft)
        or root["active_operation"]["read_accounting"] != accounting
    ):
        raise ReviewInputError(
            "activation accounting callback changed sealed authority"
        )
    return sealed


def read_session_baseline(
    ledger: object, record: SessionRecord
) -> ReviewBaseline | None:
    """Read the complete inventory from this integrity-checked authority root.

    Immutable review generation may precede mutable receipt generation. Snapshot
    comparisons for the current operation still belong to the caller/planner.
    This seam never projects or rehashes a persisted record.
    """
    from .baseline import baseline_from_history_document

    if (
        record.convergence_history is None
        or "baseline" not in record.convergence_history
    ):
        return None

    value = record.convergence_history["baseline"]
    if not isinstance(value, dict) or value.get("encoding") != PARTITION_ENCODING:
        return baseline_from_history_document(value)
    manifest = validate_manifest(value)
    producer, reader = _trusted_evidence_resolver(ledger)
    binding = manifest["binding"]
    assert isinstance(binding, dict)
    if (
        binding["repository"] != record.repository
        or binding["repository_id"] != record.repository_id
        or binding["pull_request"] != record.pull_request
        or binding["generation"] > record.generation
        or binding["purpose"] != "baseline"
        or binding["producer"] != producer()
    ):
        raise ReviewInputError("partition root does not match authenticated session")
    identity = SessionIdentity(
        record.repository, record.pull_request, repository_id=record.repository_id
    )
    return baseline_from_history_document(
        value,
        reader=lambda root: reader(identity, root, expected_binding=binding),
    )


class LocalSessionLedger:
    """Filesystem ledger under an operator-supplied directory.

    This adapter assumes a single writer per repository/pull-request identity.
    Its atomic file replacement protects individual writes, but it does not
    claim inter-process locking; concurrent hosted jobs should use the
    GitHub-backed adapter instead, whose hosted checks are also best-effort
    unless the deployment serializes writers. Canonical content-addressed part
    bytes may be reused within this instance, but every use freshly checks local
    containment and inode metadata. Mutable roots and transactions are never
    cached. This is the same trusted local operator profile, not a filesystem
    adversary or distributed locking guarantee.

    Recovery from an expired session is explicit: ``reenroll`` is the only
    operation that may retire an expired record and its enrollment witness, and
    it refuses every other load status. No admission path re-creates a session
    that merely expired.
    """

    SINGLE_WRITER_PER_IDENTITY = True

    def __init__(
        self,
        root: Path,
        *,
        evidence_budget: EvidenceReadBudget | None = None,
        enable_partition_writes: bool = False,
        enable_history_graph: bool = False,
        enable_disposition_snapshot_writes: bool = False,
    ) -> None:
        if not isinstance(root, Path):
            raise ReviewInputError("session ledger root is invalid")
        self.root = root
        self._analysis_phases: Any | None = None
        self._metadata_phase_closed = False
        self.evidence_budget = evidence_budget or EvidenceReadBudget()
        self._explicit_evidence_budget = evidence_budget is not None
        self.enable_partition_writes = enable_partition_writes
        self.enable_history_graph = enable_history_graph
        self.enable_disposition_snapshot_writes = enable_disposition_snapshot_writes
        self._history_documents: dict[str, bytes] = {}
        self._history_associations: dict[object, Any] = {}
        self._shared_control_budget = evidence_budget is not None
        self._activation_ticket: EvidenceTailTicket | None = None
        self._activation_identity: SessionIdentity | None = None
        self._tail_preparing = False
        self._part_documents: dict[
            tuple[SessionIdentity, str], tuple[tuple[int, ...], bytes]
        ] = {}

    def _control_accounting_enabled(self) -> bool:
        if self._metadata_phase_closed:
            raise ReviewInputError("analysis metadata phase is closed")
        return (
            self._shared_control_budget
            or self.enable_partition_writes
            or self.enable_disposition_snapshot_writes
        )

    def _dispatch_evidence(self, label: str, *, fence: bool = False) -> None:
        if self._activation_ticket is not None:
            self._activation_ticket.consume(label, fence=fence)
        else:
            self.evidence_budget.consume(fence=fence)

    def _consume_control_io(
        self, *, fence: bool = False, label: str = "unplanned"
    ) -> None:
        if self._control_accounting_enabled():
            self._dispatch_evidence(label, fence=fence)

    def _observe_control_document(self, document: Mapping[str, object]) -> None:
        history = document.get("convergence_history")
        baseline = history.get("baseline") if isinstance(history, Mapping) else None
        if (
            "assessment_queue" in document
            or "disposition_snapshot" in document
            or (
                isinstance(baseline, Mapping)
                and baseline.get("encoding") == PARTITION_ENCODING
            )
        ):
            self._shared_control_budget = True

    def _reserve_local_io(self, calls: int, *, label: str = "unplanned") -> None:
        """Charge bounded physical work, including mandatory durable cleanup.

        One durable file-write dispatch includes its mandatory sync/cleanup;
        charges are never refunded after a fault. Metadata containment inspection
        is one bounded dispatch;
        enumeration and each enumerated stat are charged separately.
        """
        if not self._control_accounting_enabled():
            return
        if self._activation_ticket is None:
            self.evidence_budget.preflight(calls)
        for _ in range(calls):
            self._dispatch_evidence(label)

    @staticmethod
    def evidence_producer() -> str:
        return "local-ledger"

    def _history_load(
        self,
        identity: SessionIdentity,
        *,
        max_scan_pages: int | None,
        now: datetime | None,
    ) -> tuple[SessionRecord | None, object]:
        if max_scan_pages is not None:
            raise ReviewInputError(
                "local history reader does not accept remote scan scope"
            )
        # Read-only authority may outlive execution deadlines; it never admits a mutation.
        document = self._read_document(self._path(identity), fence=True)
        if document is None:
            raise ReviewInputError("history current root is missing")
        record = SessionRecord.from_dict(document)
        if (record.repository, record.repository_id, record.pull_request) != (
            identity.repository,
            identity.repository_id,
            identity.pull_request,
        ):
            raise ReviewInputError("history root identity differs")
        read_session_baseline(self, record)
        read_session_dispositions(self, record)
        return record, read_session_assessment_queue(self, record)

    @staticmethod
    def _history_head(identity: SessionIdentity, binding: Mapping[str, object]) -> None:
        # Local caller supplies the trusted immutable Git snapshot context.
        if (
            binding.get("repository") != identity.repository
            or binding.get("pull_request") != identity.pull_request
        ):
            raise ReviewInputError("history trusted local snapshot differs")

    def associate_assessment_history(
        self,
        identity: SessionIdentity,
        *,
        expected_binding: Mapping[str, object],
        expected_root_sha256: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> HistoryAssociation:
        return associate_history(
            self,
            identity,
            expected_binding=expected_binding,
            expected_root_sha256=expected_root_sha256,
            expected_generation=expected_generation,
            now=now,
        )

    def read_associated_history(
        self,
        proof: HistoryAssociation,
        *,
        source_digest: str,
        operation_id: str,
        now: datetime | None = None,
    ) -> object:
        return read_associated_history(
            self, proof, source_digest=source_digest, operation_id=operation_id, now=now
        )

    def _part_directory(
        self,
        identity: SessionIdentity,
        *,
        storage_id: str | None = None,
        inspection_charged: bool = False,
    ) -> Path:
        if (
            self._activation_ticket is not None
            and identity != self._activation_identity
        ):
            raise ReviewInputError("activation part identity changed")
        if not inspection_charged:
            self._dispatch_evidence(f"part:{storage_id}:directory")
        root = self.root.resolve()
        directory = (
            self.root
            / ".evidence"
            / _safe_ledger_name(identity.repository)
            / str(identity.pull_request)
        )
        for path in (self.root, self.root / ".evidence", directory.parent, directory):
            if path.is_symlink():
                raise ReviewInputError("partition directory must not be a symlink")
        resolved = directory.resolve()
        if root not in resolved.parents or (
            resolved.exists() and not resolved.is_dir()
        ):
            raise ReviewInputError("partition directory is outside ledger root")
        return resolved

    @staticmethod
    def _part_observation(value: os.stat_result) -> tuple[int, ...]:
        if not stat.S_ISREG(value.st_mode):
            raise ReviewInputError("local partition must be a regular file")
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def _read_part(
        self, identity: SessionIdentity, storage_id: str
    ) -> AuthenticatedPart:
        if not re.fullmatch(r"[a-f0-9]{64}", storage_id):
            raise ReviewInputError("local partition identity is invalid")
        key = (identity, storage_id)
        cached = self._part_documents.get(key)
        # Sealed activation plans retain their original exact dispatch labels
        # and liability. This optimization never discounts or rewrites a tail.
        if (
            cached is not None
            # Windows Python 3.11 exposes creation time as st_ctime. It cannot
            # authenticate a cache hit after a same-size edit restoring mtime.
            and sys.platform != "win32"
            and self._activation_ticket is None
            and not self._tail_preparing
        ):
            self._dispatch_evidence(f"part:{storage_id}:cached-observation")
            path = (
                self._part_directory(
                    identity, storage_id=storage_id, inspection_charged=True
                )
                / storage_id
            )
            try:
                observation = self._part_observation(path.stat(follow_symlinks=False))
            except OSError as exc:
                raise ReviewInputError("local partition is missing or invalid") from exc
            if observation == cached[0]:
                self.evidence_budget.check()
                # Return a fresh document, never a mutable cached authority object.
                return AuthenticatedPart(
                    json.loads(cached[1]), self.evidence_producer()
                )
            # Metadata changed: the old proof grants nothing. Read, hash and
            # authenticate the current inode through the ordinary cold path.
            del self._part_documents[key]
        self._dispatch_evidence(f"part:{storage_id}:resolve")
        path = self._part_directory(identity, storage_id=storage_id) / storage_id
        self._dispatch_evidence(f"part:{storage_id}:read")
        if path.is_symlink():
            raise ReviewInputError("local partition must not be a symlink")
        try:
            named_before = self._part_observation(path.stat(follow_symlinks=False))
            with path.open("rb") as handle:
                before = self._part_observation(os.fstat(handle.fileno()))
                raw = handle.read(MAX_PART_BYTES + 1)
                after = self._part_observation(os.fstat(handle.fileno()))
                current = self._part_observation(path.stat(follow_symlinks=False))
            # CPython 3.12 Windows fstat exposes change time, while pathname
            # stat preserves creation time in st_ctime. Compare complete
            # observations within each API; bridge them through common file
            # identity/mode/size/mtime fields. This never permits a cache hit
            # on Windows: every read still authenticates the complete bytes.
            same_file = (
                after[:-1] == current[:-1]
                if sys.platform == "win32"
                else after == current
            )
            if (
                before != after
                or named_before != current
                or not same_file
                or len(raw) > MAX_PART_BYTES
                or hashlib.sha256(raw).hexdigest() != storage_id
            ):
                raise ValueError("invalid part")
            document = json.loads(raw)
            if canonical_bytes(document) != raw:
                raise ValueError("noncanonical part")
        except (OSError, ValueError, UnicodeError) as exc:
            raise ReviewInputError("local partition is missing or invalid") from exc
        self.evidence_budget.check()
        if key not in self._part_documents and (
            len(self._part_documents) >= MAX_PART_READS
            or sum(len(item[1]) for item in self._part_documents.values()) + len(raw)
            > MAX_PART_READS * MAX_PART_BYTES
        ):
            raise ReviewInputError("local partition cache exceeds finite capacity")
        self._part_documents[key] = (current, raw)
        return AuthenticatedPart(document, self.evidence_producer())

    def read_evidence(
        self,
        identity: SessionIdentity,
        manifest: dict[str, object],
        *,
        expected_binding: Mapping[str, object],
    ) -> object:
        if (
            expected_binding.get("repository") != identity.repository
            or expected_binding.get("pull_request") != identity.pull_request
            or expected_binding.get("repository_id") != identity.repository_id
            or expected_binding.get("producer") != self.evidence_producer()
        ):
            raise ReviewInputError("local partition binding does not match")
        if isinstance(manifest, dict) and manifest.get("encoding") == INLINE_ENCODING:
            return read_inline_evidence(manifest, expected_binding=expected_binding)
        return read_partitioned_evidence(
            manifest,
            reader=lambda storage_id: self._read_part(identity, storage_id),
            expected_binding=expected_binding,
            budget=self.evidence_budget,
        )

    def stage_evidence(
        self,
        identity: SessionIdentity,
        *,
        binding: dict[str, object],
        document: object,
        item_count: int,
        max_manifest_bytes: int,
    ) -> dict[str, object]:
        if not self.enable_partition_writes and not (
            self.enable_disposition_snapshot_writes
            and binding.get("purpose") == "dispositions"
        ):
            raise ReviewInputError(
                "partition writers require explicit reader-first enablement"
            )
        if (
            binding.get("repository") != identity.repository
            or binding.get("pull_request") != identity.pull_request
            or binding.get("repository_id") != identity.repository_id
            or binding.get("producer") != self.evidence_producer()
        ):
            raise ReviewInputError("local partition binding does not match")
        directory = self._part_directory(identity)
        self._reserve_local_io(2)  # directory creation and bounded enumeration
        directory.mkdir(parents=True, exist_ok=True)
        sizes: dict[str, int] = {}
        with os.scandir(directory) as entries:
            for entry in entries:
                self.evidence_budget.consume()  # metadata/stat per retained entry
                if (
                    len(sizes) >= MAX_STORED_PARTS
                    or entry.is_symlink()
                    or not entry.is_file()
                    or not re.fullmatch(
                        r"(?:[a-f0-9]{64}|\.staging-[A-Za-z0-9_-]{1,64})", entry.name
                    )
                ):
                    raise ReviewInputError(
                        "partition retention scan exceeded finite bounds"
                    )
                sizes[entry.name] = entry.stat().st_size
        if sum(sizes.values()) > MAX_STORED_PART_BYTES:
            raise ReviewInputError("partition retention byte capacity exceeded")
        prospective_parts = partition_evidence(
            document, binding=binding, item_count=item_count
        )
        # Physical local writes include durable sync/cleanup; reads include
        # containment and object inspection. Reserve later root/fence work too.
        if not self._tail_preparing:
            self.evidence_budget.preflight(8 * len(prospective_parts) + 16)
        missing_sizes = [
            len(canonical_bytes(part))
            for part in prospective_parts
            if hashlib.sha256(canonical_bytes(part)).hexdigest() not in sizes
        ]
        if (
            len(sizes) + len(missing_sizes) > MAX_STORED_PARTS
            or sum(sizes.values()) + sum(missing_sizes) > MAX_STORED_PART_BYTES
        ):
            raise ReviewInputError(
                "partition whole retention capacity exceeded before staging"
            )

        def write(part: dict[str, object]) -> str:
            raw = canonical_bytes(part)
            storage_id = hashlib.sha256(raw).hexdigest()
            if storage_id not in sizes:
                if (
                    len(sizes) >= MAX_STORED_PARTS
                    or sum(sizes.values()) + len(raw) > MAX_STORED_PART_BYTES
                ):
                    raise ReviewInputError("partition retention capacity exceeded")
                # One durable immutable write includes temp write, mandatory
                # sync and cleanup. Charge before any physical write.
                self._reserve_local_io(1)
                path = directory / storage_id
                # Install a complete fsynced inode atomically without replacing
                # an existing immutable object. A crash leaves only .staging
                # debris charged to retention, never referenced as authority.
                temporary = tempfile.NamedTemporaryFile(
                    dir=directory, prefix=".staging-", delete=False
                )
                try:
                    temporary.write(raw)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary.close()
                    self.evidence_budget.check()
                    try:
                        os.link(temporary.name, path)
                    except FileExistsError:
                        pass
                    if os.name != "nt":
                        fd = os.open(directory, os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                finally:
                    temporary.close()
                    os.unlink(temporary.name)
                sizes[storage_id] = len(raw)
            return storage_id

        return stage_partitioned_evidence(
            document,
            binding=binding,
            item_count=item_count,
            max_manifest_bytes=max_manifest_bytes,
            writer=write,
            reader=lambda storage_id: self._read_part(identity, storage_id),
            budget=self.evidence_budget,
            preflight_calls=(len(missing_sizes) + 3 * len(prospective_parts))
            if self._tail_preparing
            else None,
        )

    def _path(self, identity: SessionIdentity) -> Path:
        return (
            self.root
            / _safe_ledger_name(identity.repository)
            / f"{identity.pull_request}.json"
        )

    def _enrollment_path(self, identity: SessionIdentity) -> Path:
        """Independent local witness that this identity was initialized once."""

        digest = hashlib.sha256(
            f"reviewsensei-session-v1\0{identity.repository}\0{identity.pull_request}".encode(
                "utf-8"
            )
        ).hexdigest()
        # Keep the witness outside the mutable identity directory. Deleting
        # ``<repository>/<pr>.json`` (or that whole directory) must not turn
        # an established local session into a fresh enrollment.
        return self.root / ".enrollments" / f"{digest}.v1"

    def _enrollment_directory(self) -> Path:
        """Return the witness directory after containment and symlink checks.

        The witness is read on every load and written on every enrollment, so
        it gets the same discipline as the record path: a pre-existing symlink
        or a directory that resolves outside the operator-supplied root must
        never let that read or write escape the ledger.
        """

        self._consume_control_io()
        root = self.root.resolve()
        directory = self.root / ".enrollments"
        if directory.is_symlink():
            raise ReviewInputError("session enrollment directory is invalid")
        resolved = directory.resolve()
        if resolved != root and root not in resolved.parents:
            raise ReviewInputError(
                "session enrollment directory is outside the ledger root"
            )
        if resolved.exists() and not resolved.is_dir():
            raise ReviewInputError("session enrollment directory is invalid")
        return resolved

    def _validated_enrollment_path(self, identity: SessionIdentity) -> Path:
        """Return the witness path after rejecting symlinks and non-files."""

        path = self._enrollment_directory() / self._enrollment_path(identity).name
        self._consume_control_io()
        if path.is_symlink():
            raise ReviewInputError("session enrollment witness is invalid")
        if path.exists() and not path.is_file():
            raise ReviewInputError("session enrollment witness is invalid")
        return path

    @staticmethod
    def _enrollment_witness(identity: SessionIdentity) -> bytes:
        return (
            hashlib.sha256(
                f"reviewsensei-session-v1\0{identity.repository}\0{identity.pull_request}".encode(
                    "utf-8"
                )
            ).hexdigest()
            + "\n"
        ).encode("ascii")

    def _has_enrollment_witness(self, identity: SessionIdentity) -> bool:
        path = self._validated_enrollment_path(identity)
        self._consume_control_io()
        try:
            with path.open("rb") as handle:
                raw = handle.read(66)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ReviewInputError(
                "session enrollment witness could not be read"
            ) from exc
        if self._control_accounting_enabled():
            self.evidence_budget.check()
        if raw != self._enrollment_witness(identity):
            raise ReviewInputError("session enrollment witness is invalid")
        return True

    def reenroll(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Recover this identity from an expired or witness-only session.

        Authenticated recovery covers exactly two states: a record that expired,
        and the enrollment witness without a record, which is the deleted-record
        state the witness exists to detect. Any other status -- a live session,
        an ambiguous marker, or a record that is present but unreadable -- needs
        investigation rather than a reset, and resetting one would trade a
        recoverable problem for a fresh session budget. The witness is removed
        first so an interrupted recovery leaves a loadable expired record
        instead of a witness with no record.
        """

        # An expired load deliberately withholds the record, so recovery keys
        # off the status and the on-disk record rather than off a record this
        # adapter refuses to hand back.
        loaded = self.load(identity, now=now)
        witness_only = (
            loaded.status == "integrity-failed" and not self._path(identity).exists()
        )
        if loaded.status != "expired" and not witness_only:
            raise ReviewInputError(
                "only an expired or witness-only session can be re-enrolled"
            )
        expired_document = self._read_document(self._path(identity))
        if expired_document is not None and "quarantine_lineage" in expired_document:
            raise ReviewInputError(
                "quarantine lineage requires retained authority and cannot be re-enrolled"
            )
        if expired_document is not None and "assessment_queue" in expired_document:
            raise ReviewInputError(
                "assessment queue requires retained tombstone authority before re-enrollment"
            )
        witness = self._validated_enrollment_path(identity)
        self._reserve_local_io(2)  # explicit witness and root retirement
        try:
            witness.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReviewInputError(
                "session enrollment witness could not be retired"
            ) from exc
        try:
            self._path(identity).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReviewInputError(
                "expired session record could not be retired"
            ) from exc
        return self.initialize(identity, now=now)

    def _create_enrollment_witness(self, identity: SessionIdentity) -> None:
        path = self._validated_enrollment_path(identity)
        self._reserve_local_io(1)  # durable enrollment write including sync
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(self._enrollment_witness(identity))
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except FileExistsError as exc:
            if self._path(identity).exists():
                raise ReviewInputError(
                    "session already exists (concurrent initialization)"
                ) from exc
            # The witness is present but no record exists yet: another
            # initializer won the race before writing its record, or the
            # record was deleted after enrollment. Fail closed either way.
            raise ReviewInputError(
                "session enrollment witness already exists without a session "
                "record; concurrent initialization may be in progress"
            ) from exc
        except OSError as exc:
            raise ReviewInputError(
                "session enrollment witness could not be written"
            ) from exc

    def _read_document(
        self, path: Path, *, fence: bool = False, tail_label: str = "unplanned"
    ) -> Mapping[str, object] | None:
        accounted = self._control_accounting_enabled()
        if self._activation_ticket is not None and (
            self._activation_identity is None
            or path != self._path(self._activation_identity)
        ):
            raise ReviewInputError("activation root path changed")
        self._consume_control_io(fence=fence, label=tail_label)
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_SESSION_RECORD_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ReviewInputError("session ledger could not be read") from exc
        if accounted:
            self.evidence_budget.check()
        if len(raw) > MAX_SESSION_RECORD_BYTES:
            raise ReviewInputError("session record exceeds the configured size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ReviewInputError("session record was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ReviewInputError("session record is invalid")
        self._observe_control_document(payload)
        if not accounted and self._control_accounting_enabled():
            # First richer-root read is retroactively charged before exposing
            # authority. Default inline-only callers retain legacy behavior.
            self._consume_control_io(fence=fence, label=tail_label)
        return payload

    def _write(
        self,
        identity: SessionIdentity,
        record: SessionRecord,
        *,
        exclusive: bool = False,
    ) -> None:
        self._observe_control_document(record.to_dict())
        if self._activation_ticket is not None and (
            identity != self._activation_identity
            or record.record_sha256 != self._activation_ticket._root_sha256
        ):
            raise ReviewInputError("activation root differs from sealed authority")
        path = self._path(identity)
        # One durable temporary-write dispatch includes directory creation,
        # file sync and unconditional cleanup. Activation is a separate fence.
        self._reserve_local_io(1, label="root:write")
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            record.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_SESSION_RECORD_BYTES:
            raise ReviewInputError("session record exceeds the configured size limit")
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f".{identity.pull_request}.",
            suffix=".tmp",
        )
        installed = False
        temporary_removed = False
        handle_closed = False
        try:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle_closed = True
            # Deadline/count is checked again immediately before authority
            # activation, after serialization, temp write and file sync.
            self._consume_control_io(fence=True, label="root:activate")
            if exclusive:
                # Link a fully fsynced temporary file into place without
                # replacing an existing destination. This is the filesystem
                # equivalent of O_CREAT|O_EXCL for initialization.
                os.link(handle.name, path)
                installed = True
                os.unlink(handle.name)
                temporary_removed = True
            else:
                os.replace(handle.name, path)
                installed = True
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            if self._control_accounting_enabled():
                self.evidence_budget.check()
        except (OSError, ReviewInputError) as exc:
            if not handle_closed:
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass
            if not installed and not temporary_removed:
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass
            if exclusive and isinstance(exc, FileExistsError):
                raise ReviewInputError(
                    "session already exists (concurrent initialization)"
                ) from exc
            if isinstance(exc, ReviewInputError):
                raise
            if installed:
                raise ReviewInputError(
                    "session ledger replaced but directory sync failed"
                ) from exc
            raise ReviewInputError("session ledger could not be written") from exc

    def _checkpoint_result_path(
        self, identity: SessionIdentity, transaction: ReviewTransaction | None = None
    ) -> Path:
        if transaction is not None and transaction.result_sha256 is not None:
            return (
                self.root
                / ".checkpoints"
                / _safe_ledger_name(identity.repository)
                / f"pr-{identity.pull_request}"
                / f"{transaction.transaction_id}-{transaction.result_sha256}.json"
            )
        return (
            self.root
            / ".checkpoints"
            / _safe_ledger_name(identity.repository)
            / f"pr-{identity.pull_request}.json"
        )

    def _retain_exact_checkpoint(
        self, identity: SessionIdentity, result: ReviewResult, encoded: bytes
    ) -> None:
        transaction = result.transaction
        if transaction is None or transaction.result_sha256 != result.content_digest():
            raise ReviewInputError("exact checkpoint lacks bound content")
        exact = self._checkpoint_result_path(identity, transaction)
        exact.parent.mkdir(parents=True, exist_ok=True)
        if exact.exists():
            prior = ReviewResult.from_dict(self._read_checkpoint_document(exact))
            if (
                prior.content_digest() != result.content_digest()
                or prior.transaction is None
                or not prior.transaction.logical_identity_matches(transaction)
            ):
                raise ReviewInputError("retained exact checkpoint differs")
            return
        temporary = exact.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, exact)
        if exact.read_bytes() != encoded:
            raise OSError("exact checkpoint readback differs")

    @staticmethod
    def _read_checkpoint_document(path: Path) -> Mapping[str, object]:
        from .validation import DEFAULT_REVIEW_LIMITS

        with path.open("rb") as stream:
            payload = stream.read(DEFAULT_REVIEW_LIMITS.max_result_bytes + 1)
        if len(payload) > DEFAULT_REVIEW_LIMITS.max_result_bytes:
            raise ReviewInputError("retained checkpoint exceeds bounded result size")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ReviewInputError("retained checkpoint is not an object")
        return document

    def retain_checkpoint_result(self, result: ReviewResult) -> None:
        tx = result.transaction
        if tx is None:
            return
        identity = SessionIdentity(
            repository=tx.repository, pull_request=tx.pull_request
        )
        target = self._checkpoint_result_path(identity)
        temp_path = target.with_suffix(".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Upgrade the legacy latest-only layout BEFORE it can be replaced.
            # Validate its identity and content; corruption preserves the file
            # and refuses instead of destroying the only parent authority.
            if target.exists():
                previous = ReviewResult.from_dict(
                    self._read_checkpoint_document(target)
                )
                previous_tx = previous.transaction
                if (
                    previous_tx is None
                    or previous_tx.repository != tx.repository
                    or previous_tx.pull_request != tx.pull_request
                ):
                    raise ReviewInputError(
                        "retained parent checkpoint identity differs"
                    )
                previous_bytes = json.dumps(
                    previous.to_dict(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                self._retain_exact_checkpoint(identity, previous, previous_bytes)
            encoded = json.dumps(
                result.to_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self._retain_exact_checkpoint(identity, result, encoded)
            temp_path.write_bytes(encoded)
            os.replace(temp_path, target)
            if target.read_bytes() != encoded:
                raise OSError("retained checkpoint result readback mismatch")
        except (OSError, ValueError, ReviewInputError) as exc:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            raise ReviewInputError(
                "local checkpoint result retention failed; prior evidence preserved"
            ) from exc

    def load_retained_checkpoint_result(
        self,
        identity: SessionIdentity,
        *,
        transaction: ReviewTransaction | None = None,
        head_sha: str | None = None,
    ) -> ReviewResult | None:
        exact = self._checkpoint_result_path(identity, transaction)
        target = exact if exact.is_file() else self._checkpoint_result_path(identity)
        try:
            if not target.is_file():
                return None
            loaded_result = ReviewResult.from_dict(
                self._read_checkpoint_document(target)
            )
        except (OSError, ValueError, TypeError, ReviewInputError):
            return None
        if loaded_result.transaction is None:
            return None
        if (
            loaded_result.transaction.repository != identity.repository
            or loaded_result.transaction.pull_request != identity.pull_request
        ):
            return None
        if transaction is not None and transaction.result_sha256 is not None:
            if loaded_result.transaction.head_sha != transaction.head_sha:
                return None
        if head_sha is not None and loaded_result.transaction.head_sha != head_sha:
            return None
        return loaded_result

    def load(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionLoadResult:
        try:
            document = self._read_document(self._path(identity))
            if document is None:
                if self._has_enrollment_witness(identity):
                    return SessionLoadResult(
                        status="integrity-failed",
                        detail="session record is missing after enrollment",
                    )
                return SessionLoadResult(status="missing")
            if document.get("schema_version") == "0.1":
                migrated = migrate_session_document(document)
                created_at_value = migrated.get("created_at")
                if not isinstance(created_at_value, str) or not created_at_value:
                    raise ReviewInputError("legacy session record requires created_at")
                created_at = created_at_value
                created_time = _parse_aware_datetime(created_at, label="created_at")
                updated_at_value = migrated.get("updated_at")
                if not isinstance(updated_at_value, str) or not updated_at_value:
                    raise ReviewInputError("legacy session record requires updated_at")
                updated_time = _parse_aware_datetime(
                    updated_at_value, label="updated_at"
                )
                if updated_time < created_time:
                    raise ReviewInputError(
                        "legacy session updated_at precedes created_at"
                    )
                expires_value = migrated.get("expires_at")
                if expires_value is None:
                    expires_at = created_time + DEFAULT_SESSION_TTL
                else:
                    parsed_expires_at = _parse_aware_datetime(
                        expires_value, label="expires_at"
                    )
                    current = _aware_now(now)
                    if parsed_expires_at <= current:
                        raise ReviewInputError(
                            "legacy session expires_at is already expired"
                        )
                    maximum_expiry = created_time + MAX_SESSION_TTL
                    minimum_expiry = max(
                        created_time + MIN_SESSION_TTL,
                        current + MIN_SESSION_TTL,
                    )
                    if maximum_expiry < minimum_expiry:
                        raise ReviewInputError(
                            "legacy session expiry cannot satisfy the configured bounds"
                        )
                    expires_at = min(parsed_expires_at, maximum_expiry)
                    expires_at = max(expires_at, minimum_expiry)
                migrated_record = SessionRecord._construct(
                    repository=identity.repository,
                    pull_request=identity.pull_request,
                    repository_id=identity.repository_id,
                    completed_initial_reviews=_require_bounded_int(
                        migrated["completed_initial_reviews"],
                        label="completed_initial_reviews",
                        minimum=0,
                        maximum=MAX_FAILED_ATTEMPTS,
                    ),
                    completed_verification_rounds=_require_bounded_int(
                        migrated["completed_verification_rounds"],
                        label="completed_verification_rounds",
                        minimum=0,
                        maximum=MAX_FAILED_ATTEMPTS,
                    ),
                    failed_attempts=_require_bounded_int(
                        migrated["failed_attempts"],
                        label="failed_attempts",
                        minimum=0,
                        maximum=MAX_FAILED_ATTEMPTS,
                    ),
                    generation=_require_bounded_int(
                        migrated["generation"],
                        label="generation",
                        minimum=0,
                        maximum=MAX_GENERATION,
                    ),
                    reservation_id=migrated.get("reservation_id"),  # type: ignore[arg-type]
                    reserved_slot=migrated.get("reserved_slot"),  # type: ignore[arg-type]
                    last_committed_reservation_id=migrated.get(
                        "last_committed_reservation_id"
                    ),  # type: ignore[arg-type]
                    created_at=_format_datetime(created_time),
                    updated_at=_format_datetime(updated_time),
                    expires_at=_format_datetime(expires_at),
                )
                if migrated_record.expired(now=now):
                    return SessionLoadResult(status="expired")
                return SessionLoadResult(status="migrated", record=migrated_record)
            record = SessionRecord.from_dict(document)
            read_session_baseline(self, record)
            read_session_dispositions(self, record)
            read_session_assessment_queue(self, record)
        except ReviewInputError:
            return SessionLoadResult(status="integrity-failed")
        if (
            identity.repository != record.repository
            or identity.pull_request != record.pull_request
        ):
            return SessionLoadResult(status="conflict")
        if identity.repository_id is not None and record.repository_id not in {
            None,
            identity.repository_id,
        }:
            return SessionLoadResult(status="conflict")
        return load_session_status(record, now=now)

    def initialize(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
        expires_at: datetime | str | None = None,
    ) -> SessionRecord:
        loaded = self.load(identity, now=now)
        if loaded.status in {"ok", "migrated"}:
            raise ReviewInputError("session already exists")
        if loaded.status in {"integrity-failed", "conflict", "expired"}:
            raise ReviewInputError(f"session ledger load failed: {loaded.status}")
        record = SessionRecord.create(identity, now=now, expires_at=expires_at)
        # Install this conservative, separate witness before the mutable
        # record. A crash after the witness but before record creation may
        # require recovery for a first enrollment, but it cannot reset an
        # established identity after its sole record is deleted.
        self._create_enrollment_witness(identity)
        self._write(identity, record, exclusive=loaded.status == "missing")
        return record

    def persist_migrated(
        self, identity: SessionIdentity, record: SessionRecord
    ) -> None:
        self._write(identity, record)

    def replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        loaded = self.load(identity, now=now)
        if loaded.status == "migrated" and loaded.record is not None:
            self._write(identity, loaded.record)
            loaded = SessionLoadResult(status="ok", record=loaded.record)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("session record is missing")
        updated = mutate(loaded.record)
        _history_legacy_write_guard(self, loaded.record)
        _validate_queue_retention(loaded.record, updated)
        read_session_baseline(self, updated)
        read_session_dispositions(self, updated)
        read_session_assessment_queue(self, updated)
        _history_legacy_write_guard(self, updated)
        current_document = self._read_document(self._path(identity), fence=True)
        if current_document is None:
            raise ReviewInputError("session generation conflict before activation")
        current = SessionRecord.from_dict(current_document)
        if (
            current.generation != loaded.record.generation
            or current.record_sha256 != loaded.record.record_sha256
        ):
            raise ReviewInputError("session generation conflict before activation")
        self._write(identity, updated)
        persisted = self._read_document(self._path(identity), fence=True)
        if (
            persisted is None
            or SessionRecord.from_dict(persisted).record_sha256 != updated.record_sha256
        ):
            raise ReviewInputError(
                "session activation readback is ambiguous or conflicting"
            )
        return updated

    # Kept as a compatibility shim for older in-process callers. New code
    # must use the public CAS seam above.
    def _replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(identity, mutate, now=now)

    def reserve_for_tail(
        self,
        identity: SessionIdentity,
        *,
        operation_binding: Mapping[str, object],
        slot: str,
        reservation_id: str,
        expected_generation: int,
        head_sha: str | None = None,
        now: datetime | None = None,
        witness: object = None,
    ) -> SessionRecord:
        """Create the existing reservation and retain its live owned-readback proof.

        A restored budget is admitted only when ``witness`` is an OperationHandle
        whose binding is ``reservation_id``. A snapshot or digest is not that
        witness. Loaded or duplicate reservations cannot mint another attempt.
        """
        if (
            not self.enable_partition_writes
            or not self._explicit_evidence_budget
            or not isinstance(self.evidence_budget, EvidenceReadBudget)
        ):
            raise ReviewInputError(
                "prepaid activation writers or original budget are unavailable"
            )
        budget = self.evidence_budget
        scope = _tail_attempt_scope(identity, reservation_id, operation_binding)
        witnessed = _operation_witness_matches(witness, reservation_id)
        if (
            scope in budget._live_attempts
            or scope in budget._failed_attempts
            or (budget.restored and not witnessed)
        ):
            raise NonResumableActivationError(
                "original activation attempt cannot be restarted"
            )

        def admit(record: SessionRecord) -> SessionRecord:
            if (
                record.reservation_id is not None
                or record.last_committed_reservation_id == reservation_id
            ):
                raise NonResumableActivationError(
                    "loaded reservation is not a live original attempt proof"
                )
            return mutate_reserved(
                record,
                slot=slot,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                head_sha=head_sha,
                now=now,
            )

        try:
            reserved = self.replace(identity, admit, now=now)
            budget._remember_live_attempt(
                scope,
                reserved.record_sha256,
                owner=self,
                witness=witness,
                reservation_id=reservation_id,
            )
            return reserved
        except BaseException:
            budget._burn_live_attempt(scope)
            raise

    def replace_with_tail(
        self,
        identity: SessionIdentity,
        prepare: Callable[[SessionRecord], SessionRecord],
        *,
        operation_binding: Mapping[str, object],
        attempt_reservation_id: str,
        seal_accounting: Callable[[SessionRecord, Mapping[str, int]], SessionRecord],
        now: datetime | None = None,
        witness: object = None,
    ) -> SessionRecord:
        """Stage, prepay a sealed exact tail, then conditionally activate/read back.

        Every failure burns the live attempt. A missing live attempt or a
        restored budget is admitted only when ``witness`` is an OperationHandle
        whose binding is the attempt reservation. A snapshot or grant refuses.
        """
        if (
            not self.enable_partition_writes
            or not self._explicit_evidence_budget
            or not isinstance(self.evidence_budget, EvidenceReadBudget)
        ):
            raise ReviewInputError(
                "prepaid activation writers or original budget are unavailable"
            )
        budget = self.evidence_budget
        operation = _tail_operation_binding(operation_binding)
        scope = _tail_attempt_scope(identity, attempt_reservation_id, operation)
        witnessed = _operation_witness_matches(witness, attempt_reservation_id)
        if scope in budget._failed_attempts or (
            not witnessed
            and (
                budget.restored
                or scope not in budget._live_attempts
                or budget._live_attempt_owners.get(scope) is not self
            )
        ):
            raise NonResumableActivationError(
                "original activation attempt witness is unavailable"
            )
        ticket: EvidenceTailTicket | None = None
        try:
            loaded = self.load(identity, now=now)
            if loaded.status != "ok" or loaded.record is None:
                raise ReviewInputError(
                    "prepaid activation current authority is unavailable"
                )
            before = loaded.record
            budget._require_live_attempt(
                scope,
                before.record_sha256,
                owner=self,
                witness=witness,
                reservation_id=attempt_reservation_id,
            )
            self._tail_preparing = True
            draft = prepare(before)
            self._tail_preparing = False
            _validate_tail_draft(
                before,
                draft,
                operation=operation,
                reservation_id=attempt_reservation_id,
                budget=budget,
            )
            if self.enable_history_graph:
                read_session_dispositions(self, draft)
                read_session_assessment_queue(self, draft)
                _history_preserve_sources(self, before, draft)
            old_ids = _history_extra_ids(self, before)
            new_ids = _history_extra_ids(self, draft)
            plan_scope = _tail_plan_scope(
                "local",
                before,
                draft,
                operation=operation,
                budget=budget,
                old_history_ids=old_ids,
                new_history_ids=new_ids,
            )
            steps = tuple(
                TailDispatch(f"part:{part_id}:{unit}")
                for part_id in _tail_part_ids(draft, history_ids=new_ids)
                for unit in ("resolve", "directory", "read")
            ) + (
                TailDispatch("root:reload", fence=True),
                TailDispatch("root:write"),
                TailDispatch("root:activate", fence=True),
                TailDispatch("root:readback", fence=True),
            )
            plan = ActivationTailPlan("local", plan_scope, steps)
            ticket = (
                budget.reserve_tail(plan)
                if witness is None
                else budget.reserve_tail(
                    plan, witness=witness, reservation_id=attempt_reservation_id
                )
            )
            sealed = _seal_tail_accounting(draft, seal_accounting, budget)
            _validate_queue_retention(before, sealed)
            ticket.seal(sealed.record_sha256)
            ticket.start(
                scope_sha256=_tail_plan_scope(
                    "local",
                    before,
                    sealed,
                    operation=operation,
                    budget=budget,
                    old_history_ids=old_ids,
                    new_history_ids=new_ids,
                ),
                root_sha256=sealed.record_sha256,
            )
            self._activation_ticket, self._activation_identity = ticket, identity
            read_session_baseline(self, sealed)
            read_session_dispositions(self, sealed)
            read_session_assessment_queue(self, sealed)
            current_document = self._read_document(
                self._path(identity), fence=True, tail_label="root:reload"
            )
            if (
                current_document is None
                or SessionRecord.from_dict(current_document).record_sha256
                != before.record_sha256
            ):
                raise ReviewInputError(
                    "session generation conflict before prepaid activation"
                )
            self._write(identity, sealed)
            persisted = self._read_document(
                self._path(identity), fence=True, tail_label="root:readback"
            )
            if (
                persisted is None
                or SessionRecord.from_dict(persisted).record_sha256
                != sealed.record_sha256
            ):
                raise ReviewInputError(
                    "prepaid activation readback is ambiguous or conflicting"
                )
            ticket.finish()
            budget._advance_live_attempt(scope, sealed.record_sha256)
            return sealed
        except BaseException:
            budget._burn_live_attempt(scope)
            raise
        finally:
            if ticket is not None:
                ticket.abort()
            self._activation_ticket = self._activation_identity = None
            self._tail_preparing = False

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        head_sha: str | None = None,
        rescan_request: AuthorizedRescan | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
            identity,
            lambda record: mutate_reserved(
                record,
                slot=slot,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                head_sha=head_sha,
                rescan_request=rescan_request,
                now=now,
            ),
            now=now,
        )

    def commit(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
            identity,
            lambda record: mutate_commit(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )

    def abort(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
            identity,
            lambda record: mutate_abort(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )


def default_local_session_root(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the per-user state directory that holds local sessions.

    The persistent-local-session command needs one documented location that
    does not require the caller to invent a ledger path.  Each platform's
    conventional per-user state directory is used, and no new dependency is
    introduced to find it.
    """

    environment = os.environ if environ is None else environ
    if sys.platform == "darwin":
        home = environment.get("HOME", "").strip() or _home_directory()
        if home:
            return Path(home) / "Library" / "Application Support" / "review-sensei"
    elif os.name == "nt":
        base = (
            environment.get("LOCALAPPDATA", "").strip()
            or environment.get("APPDATA", "").strip()
        )
        if base:
            return Path(base) / "review-sensei"
    else:
        state_home = environment.get("XDG_STATE_HOME", "").strip()
        if state_home:
            return Path(state_home) / "review-sensei"
        home = environment.get("HOME", "").strip() or _home_directory()
        if home:
            return Path(home) / ".local" / "state" / "review-sensei"
    raise ReviewInputError(
        "no per-user state directory is available; pass --session-ledger "
        "with an explicit directory"
    )


def _home_directory() -> str:
    try:
        return str(Path.home())
    except RuntimeError:  # pragma: no cover - no resolvable home directory
        return ""


def resolve_local_session_ledger(
    path: str | Path | None = None,
    *,
    trusted_root: str | Path | None = None,
) -> LocalSessionLedger | None:
    """Return a local ledger from ``--session-ledger`` or the ambient env.

    Callers with a trusted checkout boundary may pass ``trusted_root`` to
    contain the resolved ledger directory. ``~`` and shell-style environment
    variables are expanded before validation. Without that boundary the
    operator owns the explicitly supplied location, but traversal and Windows
    namespace paths are rejected and relative paths are canonicalized once.
    """

    raw = path if path is not None else os.getenv(SESSION_LEDGER_ENV)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, (str, Path)):
        raise ReviewInputError("session ledger path is invalid")
    raw_text = os.fspath(raw)
    if not isinstance(raw_text, str) or "\x00" in raw_text:
        raise ReviewInputError("session ledger path is invalid")
    raw_text = os.path.expandvars(os.path.expanduser(raw_text))
    if os.name == "nt":
        windows_path = raw_text.replace("/", "\\")
        if windows_path.startswith(("\\\\", "\\\\?\\", "\\\\.\\", "\\??\\")):
            raise ReviewInputError("session ledger path is invalid")
        if ntpath.splitdrive(windows_path)[0] and not ntpath.isabs(windows_path):
            raise ReviewInputError("session ledger path is invalid")
    try:
        root = Path(raw_text)
        if ".." in root.parts:
            raise ReviewInputError("session ledger path traversal is not allowed")
        root = root.resolve()
        if trusted_root is not None:
            if not isinstance(trusted_root, (str, Path)):
                raise ReviewInputError("session ledger trusted root is invalid")
            trusted_text = os.fspath(trusted_root)
            if not isinstance(trusted_text, str) or "\x00" in trusted_text:
                raise ReviewInputError("session ledger trusted root is invalid")
            trusted_text = os.path.expandvars(os.path.expanduser(trusted_text))
            trusted = Path(trusted_text).resolve()
            try:
                root.relative_to(trusted)
            except ValueError as exc:
                raise ReviewInputError(
                    "session ledger path is outside the trusted root"
                ) from exc
    except ReviewInputError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ReviewInputError("session ledger path is invalid") from exc
    return LocalSessionLedger(root)
