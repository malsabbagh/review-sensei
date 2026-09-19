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
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol, cast

from .convergence import (
    MAX_FAILED_ATTEMPTS,
    OPERATOR_REVIEW_MODES,
    ReviewConvergencePolicy,
    RoundAdmissionDecision,
    RoundSessionState,
    evaluate_round_admission,
)
from .errors import ReviewInputError
from .schemas import validate_public_document

PUBLIC_SCHEMA_VERSION = "1.0"
SESSION_LEDGER_ENV = "REVIEWSENSEI_SESSION_LEDGER"
DEFAULT_SESSION_TTL = timedelta(days=30)
# Keep a small lower bound when migrating an untrusted legacy expiry that is
# still in the future, so a writable v0.1 file cannot force an immediate reset.
MIN_SESSION_TTL = timedelta(minutes=1)
MAX_SESSION_TTL = timedelta(days=90)
MAX_SESSION_RECORD_BYTES = 4096
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
    }
    if forbidden:
        raise ReviewInputError("legacy session record has unknown fields")
    return migrated


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

    def __post_init__(self) -> None:
        SessionIdentity(
            repository=self.repository,
            pull_request=self.pull_request,
            repository_id=self.repository_id,
        )
        for label in (
            "completed_initial_reviews",
            "completed_verification_rounds",
            "failed_attempts",
        ):
            _require_bounded_int(
                getattr(self, label),
                label=label,
                minimum=0,
                maximum=MAX_FAILED_ATTEMPTS,
            )
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
        created = _parse_aware_datetime(self.created_at, label="created_at")
        updated = _parse_aware_datetime(self.updated_at, label="updated_at")
        expires = _parse_aware_datetime(self.expires_at, label="expires_at")
        if updated < created:
            raise ReviewInputError("session updated_at precedes created_at")
        if expires <= created:
            raise ReviewInputError("session expires_at must be after created_at")
        if expires - created > MAX_SESSION_TTL:
            raise ReviewInputError("session ttl exceeds the configured bound")
        digest = self._payload_digest()
        if self.record_sha256 != digest:
            raise ReviewInputError("session record integrity check failed")

    def _payload_digest(self) -> str:
        payload = self._payload()
        return _digest_payload(payload)

    def _payload(self) -> dict[str, object]:
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
    ) -> "SessionRecord":
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
        }
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
        )

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["record_sha256"] = self.record_sha256
        return payload

    def identity(self) -> SessionIdentity:
        return SessionIdentity(
            repository=self.repository,
            pull_request=self.pull_request,
            repository_id=self.repository_id,
        )

    def to_round_state(self, **flags: bool) -> RoundSessionState:
        return RoundSessionState(
            completed_initial_reviews=self.completed_initial_reviews,
            completed_verification_rounds=self.completed_verification_rounds,
            failed_attempts=self.failed_attempts,
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
                maximum=MAX_FAILED_ATTEMPTS,
            ),
            completed_verification_rounds=_require_bounded_int(
                value.get("completed_verification_rounds", 0),
                label="completed_verification_rounds",
                minimum=0,
                maximum=MAX_FAILED_ATTEMPTS,
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
        )

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
    ) -> "SessionRecord":
        updated = _format_datetime(_aware_now(now))
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

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
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


def _apply_slot(record: SessionRecord, slot: str) -> dict[str, int]:
    if slot == "initial":
        return {
            "completed_initial_reviews": record.completed_initial_reviews + 1,
        }
    if slot == "verification":
        return {
            "completed_verification_rounds": record.completed_verification_rounds + 1,
        }
    if slot == "failed-attempt":
        return {"failed_attempts": record.failed_attempts + 1}
    raise ReviewInputError("reserved_slot is invalid")


def _next_generation(record: SessionRecord) -> int:
    if record.generation >= MAX_GENERATION:
        raise ReviewInputError("session generation is exhausted")
    return record.generation + 1


def mutate_reserved(
    record: SessionRecord,
    *,
    slot: str,
    reservation_id: str,
    expected_generation: int,
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
    if slot not in RESERVATION_SLOTS:
        raise ReviewInputError("reserved_slot is invalid")
    _reservation_id(reservation_id, label="reservation_id")
    return record.evolve(
        now=now,
        generation=_next_generation(record),
        reservation_id=reservation_id,
        reserved_slot=slot,
    )


def mutate_commit(
    record: SessionRecord,
    *,
    reservation_id: str,
    expected_generation: int,
    now: datetime | None = None,
) -> SessionRecord:
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
    return record.evolve(
        now=now,
        generation=_next_generation(record),
        reservation_id=None,
        reserved_slot=None,
        last_committed_reservation_id=reservation_id,
        **increments,
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
    )


def prepare_session_round(
    ledger: SessionLedger,
    identity: SessionIdentity,
    policy: ReviewConvergencePolicy,
    *,
    reservation_id: str,
    now: datetime | None = None,
    continuation_rounds: int = 0,
    **state_flags: bool,
) -> PreparedSessionRound:
    """Reserve a counted slot when C1 would admit the round.

    A foreign in-flight reservation pauses this job. The same
    ``reservation_id`` after commit is a same-head duplicate. Unadmitted
    operator rounds are not reserved so C5 can refuse inference and
    publication without consuming the budget.
    """

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    loaded = ledger.load(identity, now=now)
    if loaded.status == "expired":
        record = ledger.initialize(identity, now=now)
    elif loaded.status == "missing":
        record = ledger.initialize(identity, now=now)
    elif loaded.status in {"ok", "migrated"} and loaded.record is not None:
        record = loaded.record
    else:
        raise ReviewInputError(f"session ledger load failed: {loaded.status}")
    flags = dict(state_flags)
    held = record.reservation_id
    if held is not None and held != reservation_id:
        flags.setdefault("paused", True)
    elif record.last_committed_reservation_id == reservation_id and held is None:
        flags.setdefault("same_head_duplicate", True)
    decision = evaluate_round_admission(
        record.to_round_state(**flags),
        policy,
        continuation_rounds=continuation_rounds,
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
        now=now,
    )
    return PreparedSessionRound(
        record=reserved, decision=decision, reservation_id=reservation_id
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
        expected_initial = prepared.record.completed_initial_reviews + (
            1 if slot == "initial" else 0
        )
        expected_verification = prepared.record.completed_verification_rounds + (
            1 if slot == "verification" else 0
        )
        expected_failed = prepared.record.failed_attempts + (
            1 if slot == "failed-attempt" else 0
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
        if current.status in {"integrity-failed", "conflict"}:
            raise ReviewInputError(f"session ledger load failed: {current.status}")
        record = SessionRecord.create(identity, now=now, expires_at=expires_at)
        self._records[(identity.repository, identity.pull_request)] = record
        return record

    def _replace(
        self,
        identity: SessionIdentity,
        mutate,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        loaded = self.load(identity, now=now)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("session record is missing")
        updated = mutate(loaded.record)
        self._records[(identity.repository, identity.pull_request)] = updated
        return updated

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self._replace(
            identity,
            lambda record: mutate_reserved(
                record,
                slot=slot,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
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
        return self._replace(
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
        return self._replace(
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


class LocalSessionLedger:
    """Filesystem ledger under an operator-supplied directory.

    This adapter assumes a single writer per repository/pull-request identity.
    Its atomic file replacement protects individual writes, but it does not
    claim inter-process locking; concurrent hosted jobs should use the
    GitHub-backed adapter instead, whose hosted checks are also best-effort
    unless the deployment serializes writers.
    """

    SINGLE_WRITER_PER_IDENTITY = True

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ReviewInputError("session ledger root is invalid")
        self.root = root

    def _path(self, identity: SessionIdentity) -> Path:
        return (
            self.root
            / _safe_ledger_name(identity.repository)
            / f"{identity.pull_request}.json"
        )

    def _read_document(self, path: Path) -> Mapping[str, object] | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ReviewInputError("session ledger could not be read") from exc
        if len(raw) > MAX_SESSION_RECORD_BYTES:
            raise ReviewInputError("session record exceeds the configured size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewInputError("session record was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ReviewInputError("session record is invalid")
        return payload

    def _write(
        self,
        identity: SessionIdentity,
        record: SessionRecord,
        *,
        exclusive: bool = False,
    ) -> None:
        path = self._path(identity)
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
        except OSError as exc:
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
            if installed:
                raise ReviewInputError(
                    "session ledger replaced but directory sync failed"
                ) from exc
            raise ReviewInputError("session ledger could not be written") from exc

    def load(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionLoadResult:
        try:
            document = self._read_document(self._path(identity))
            if document is None:
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
        if loaded.status in {"integrity-failed", "conflict"}:
            raise ReviewInputError(f"session ledger load failed: {loaded.status}")
        record = SessionRecord.create(identity, now=now, expires_at=expires_at)
        self._write(identity, record, exclusive=loaded.status == "missing")
        return record

    def persist_migrated(
        self, identity: SessionIdentity, record: SessionRecord
    ) -> None:
        self._write(identity, record)

    def _replace(
        self,
        identity: SessionIdentity,
        mutate,
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
        self._write(identity, updated)
        return updated

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self._replace(
            identity,
            lambda record: mutate_reserved(
                record,
                slot=slot,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
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
        return self._replace(
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
        return self._replace(
            identity,
            lambda record: mutate_abort(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )


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
