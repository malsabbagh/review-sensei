"""Opt-in authenticated bounded work receipts, separate from public ledgers.

Keys come from a trusted host, are never serialized, and have no environment
default. Retained normalized outputs and patches remain sensitive diagnostics.
Reusing them always requires current host evidence and semantic validation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from .budgets import EffectiveWorkBudget
from .errors import ReviewInputError
from .execution import CompletedBatch, WorkExecution
from .outcomes import (
    DEFAULT_RECOVERY_TTL_SECONDS,
    MAX_RECOVERY_RESULT_BYTES,
    MAX_RECOVERY_TTL_SECONDS,
    ResourceBudgetTracker,
)
from .planning import ReviewWorkPlan, WorkBatch
from .schemas import validate_public_document

T = TypeVar("T")
_HASH = re.compile(r"^[a-f0-9]{64}$")
MAX_WORK_RECOVERY_ARTIFACTS = 8
MAX_WORK_RECOVERY_BYTES = 8 * 1024 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _plan_document(plan: ReviewWorkPlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "mode": plan.mode,
        "authority": plan.authority_digest,
        "budgets": plan.budget_digest,
        "snapshot": plan.bundle.snapshot.to_dict(),
        "enumeration_complete": plan.bundle.enumeration_complete,
        "records": [
            {
                **record.to_dict(),
                "patch": record.patch,
                "original_diff": record.original_diff,
            }
            for record in plan.bundle.records
        ],
        "requirements": [asdict(item) for item in plan.requirements],
        "batches": [batch.batch_id for batch in plan.batches],
        "unprocessed": plan.unprocessed,
    }


class WorkRecoveryStore:
    """Trusted diagnostics with a private path, HMAC key and aware wall clock.

    ``now`` must provide a trustworthy, nondecreasing wall clock for checkpoint
    age and expiry. Only wall-clock durations cross processes; the tracker uses
    its own monotonic seconds locally, with no shared clock-origin requirement.
    A backward wall-clock value preceding the saved checkpoint is rejected.
    """

    def __init__(
        self,
        directory: Path,
        *,
        key: bytes,
        artifacts: str = "none",
        ttl_seconds: int = DEFAULT_RECOVERY_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(key, bytes)
            or len(key) < 32
            or artifacts not in ("none", "diagnostics")
            or isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= MAX_RECOVERY_TTL_SECONDS
        ):
            raise ReviewInputError(
                "work recovery policy or authentication key is invalid"
            )
        self.directory = Path(directory)
        self._key = key
        self.enabled = artifacts == "diagnostics"
        self.ttl_seconds = ttl_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._expires_by_execution: dict[str, datetime] = {}

    def _signature(self, document: object) -> str:
        return hmac.new(
            self._key,
            b"reviewsensei:work-recovery:v2\x00" + _canonical(document),
            hashlib.sha256,
        ).hexdigest()

    def _validate_directory(self) -> None:
        if self.directory.is_symlink():
            raise ReviewInputError("work recovery directory must not be a symlink")
        if self.directory.exists() and os.name == "posix":
            info = self.directory.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ReviewInputError("work recovery directory must be private")

    def _admit_storage(self, target: Path, encoded_size: int) -> None:
        self._validate_directory()
        if target.is_symlink():
            raise ReviewInputError("work recovery file must not be a symlink")
        count, size = 1, encoded_size
        if self.directory.exists():
            for index, path in enumerate(self.directory.iterdir()):
                if index >= 32:
                    raise ReviewInputError(
                        "work recovery directory entry budget exhausted"
                    )
                if path == target:
                    continue
                if _HASH.fullmatch(path.stem) and path.suffix == ".json":
                    if path.is_symlink() or not path.is_file():
                        raise ReviewInputError("work recovery artifact path is invalid")
                    count += 1
                    size += path.stat().st_size
                if (
                    count > MAX_WORK_RECOVERY_ARTIFACTS
                    or size > MAX_WORK_RECOVERY_BYTES
                ):
                    raise ReviewInputError(
                        "work recovery aggregate storage budget exhausted"
                    )

    def save(
        self,
        execution: WorkExecution[T],
        tracker: ResourceBudgetTracker,
        encode: Callable[[T], object],
        request_digests: Mapping[str, str | None],
    ) -> None:
        if not self.enabled:
            return
        now = self.now()
        if (
            now.tzinfo is None
            or tracker.execution_identity != execution.tracker_identity
        ):
            raise ReviewInputError("work recovery execution identity is invalid")
        expires = self._expires_by_execution.setdefault(
            tracker.execution_identity, now + timedelta(seconds=self.ttl_seconds)
        )
        if now >= expires:
            raise ReviewInputError("work recovery retention expired")
        document = {
            "schema_version": "2.0",
            "plan": _plan_document(execution.plan),
            "execution_identity": execution.tracker_identity,
            "resource_budget": asdict(tracker.budget),
            "request_digests": dict(request_digests),
            "saved_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "elapsed_ms": tracker.elapsed_ms(),
            "counters": {
                name: getattr(tracker, name)
                for name in (
                    "provider_calls",
                    "transport_retries",
                    "structural_retries",
                    "prompt_bytes",
                    "response_bytes",
                )
            },
            "completed": [
                {
                    "batch_id": item.batch.batch_id,
                    "request_digest": item.request_digest,
                    "result": encode(item.value),
                }
                for item in execution.completed
            ],
            "pending": execution.pending,
        }
        envelope = {"document": document, "authentication": self._signature(document)}
        envelope = json.loads(_canonical(envelope))
        validate_public_document(envelope, "work-recovery-artifact")
        encoded = _canonical(envelope)
        if len(encoded) > MAX_RECOVERY_RESULT_BYTES:
            raise ReviewInputError(
                "work recovery artifact exceeds the configured size limit"
            )
        target = self.directory / (execution.plan.plan_id + ".json")
        self._admit_storage(target, len(encoded))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".work-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def load(
        self,
        plan: ReviewWorkPlan,
        tracker: ResourceBudgetTracker,
        budgets: EffectiveWorkBudget,
        decode: Callable[[object, WorkBatch], T],
        request_digests: Mapping[str, str | None],
    ) -> WorkExecution[T] | None:
        admitted = self._load(plan, tracker, budgets, decode, request_digests)
        return admitted[0] if admitted is not None else None

    def load_admission(
        self,
        plan: ReviewWorkPlan,
        tracker: ResourceBudgetTracker,
        budgets: EffectiveWorkBudget,
        decode: Callable[[object, WorkBatch], T],
    ) -> tuple[WorkExecution[T], Mapping[str, str | None]] | None:
        """Restore authenticated original admission before request rendering.

        The executor must compare freshly rendered request digests and validate
        every cached decision before reuse. This private diagnostic receipt is
        not public session authority and cannot authorize a new queue mutation.
        """
        return self._load(plan, tracker, budgets, decode, None)

    def _load(
        self,
        plan: ReviewWorkPlan,
        tracker: ResourceBudgetTracker,
        budgets: EffectiveWorkBudget,
        decode: Callable[[object, WorkBatch], T],
        request_digests: Mapping[str, str | None] | None,
    ) -> tuple[WorkExecution[T], Mapping[str, str | None]] | None:
        if not self.enabled:
            return None
        self._validate_directory()
        path = self.directory / (plan.plan_id + ".json")
        if not path.exists():
            return None
        if self.directory.is_symlink() or path.is_symlink():
            raise ReviewInputError("work recovery path must not be a symlink")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_RECOVERY_RESULT_BYTES + 1)
            if len(raw) > MAX_RECOVERY_RESULT_BYTES:
                raise ValueError("size")
            envelope = json.loads(raw)
            validate_public_document(envelope, "work-recovery-artifact")
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"document", "authentication"}
                or not isinstance(envelope["authentication"], str)
            ):
                raise ValueError("envelope")
            document = envelope["document"]
            if not hmac.compare_digest(
                envelope["authentication"], self._signature(document)
            ):
                raise ValueError("authentication")
            if (
                not isinstance(document, dict)
                or set(document)
                != {
                    "schema_version",
                    "plan",
                    "execution_identity",
                    "resource_budget",
                    "request_digests",
                    "saved_at",
                    "expires_at",
                    "elapsed_ms",
                    "counters",
                    "completed",
                    "pending",
                }
                or document["schema_version"] != "2.0"
            ):
                raise ValueError("schema")
            if (
                _canonical(document["plan"]) != _canonical(_plan_document(plan))
                or document["resource_budget"] != asdict(tracker.budget)
                or plan.budget_digest != budgets.digest
                or (
                    request_digests is not None
                    and document["request_digests"] != dict(request_digests)
                )
            ):
                raise ValueError("identity")
            stored_digests = document["request_digests"]
            if (
                not isinstance(stored_digests, dict)
                or set(stored_digests) != {batch.batch_id for batch in plan.batches}
                or any(
                    value is not None
                    and (not isinstance(value, str) or not _HASH.fullmatch(value))
                    for value in stored_digests.values()
                )
            ):
                raise ValueError("request identities")
            saved = datetime.fromisoformat(document["saved_at"])
            expires = datetime.fromisoformat(document["expires_at"])
            now = self.now()
            if (
                saved.tzinfo is None
                or expires.tzinfo is None
                or now.tzinfo is None
                or not saved <= now < expires
                or not 0 < (expires - saved).total_seconds() <= MAX_RECOVERY_TTL_SECONDS
            ):
                raise ValueError("expiry")
            identity = document["execution_identity"]
            if (
                not isinstance(identity, str)
                or re.fullmatch(r"[a-f0-9]{32}", identity) is None
                or (tracker.provider_calls and tracker.execution_identity != identity)
            ):
                raise ValueError("execution")
            counters = document["counters"]
            maxima = {
                "provider_calls": tracker.budget.max_provider_calls,
                "transport_retries": tracker.budget.max_retry_attempts,
                "structural_retries": tracker.budget.max_retry_attempts,
                "prompt_bytes": budgets.max_total_prompt_bytes,
                "response_bytes": budgets.max_total_output_bytes,
            }
            if (
                not isinstance(counters, dict)
                or set(counters) != set(maxima)
                or any(
                    isinstance(counters[name], bool)
                    or not isinstance(counters[name], int)
                    or not 0 <= counters[name] <= maximum
                    for name, maximum in maxima.items()
                )
            ):
                raise ValueError("counters")
            elapsed = document["elapsed_ms"]
            if (
                isinstance(elapsed, bool)
                or not isinstance(elapsed, int)
                or not 0 <= elapsed <= MAX_RECOVERY_TTL_SECONDS * 1000
            ):
                raise ValueError("elapsed")
            batches = {batch.batch_id: batch for batch in plan.batches}
            if (
                not isinstance(document["completed"], list)
                or len(document["completed"]) > 8
            ):
                raise ValueError("receipts")
            completed = []
            completed_ids: set[str] = set()
            for item in document["completed"]:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"batch_id", "request_digest", "result"}
                    or not isinstance(item["request_digest"], str)
                    or not _HASH.fullmatch(item["request_digest"])
                ):
                    raise ValueError("receipt")
                batch = batches[item["batch_id"]]
                if (
                    batch.batch_id in completed_ids
                    or item["request_digest"] != stored_digests[batch.batch_id]
                ):
                    raise ValueError("receipt request identity")
                completed_ids.add(batch.batch_id)
                completed.append(
                    CompletedBatch(
                        batch, decode(item["result"], batch), item["request_digest"]
                    )
                )
            if not isinstance(document["pending"], list) or any(
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(value, str) for value in item)
                for item in document["pending"]
            ):
                raise ValueError("pending")
            execution = WorkExecution(
                plan,
                tuple(completed),
                tuple(tuple(item) for item in document["pending"]),
                identity,
            )
            # Mutate admission only after authentication, binding and decoding.
            self._expires_by_execution[identity] = min(
                expires, self._expires_by_execution.get(identity, expires)
            )
            tracker.execution_identity = identity
            for name, count in counters.items():
                setattr(tracker, name, max(getattr(tracker, name), count))
            restored_elapsed = elapsed + int((now - saved).total_seconds() * 1000)
            # Sample one local tick so time spent between clock reads is not
            # counted twice. Rebase durations onto this process's monotonic
            # clock; never restore an absolute origin from another process.
            tick = tracker.monotonic()
            current_elapsed = max(0, int((tick - tracker.started) * 1000))
            tracker.started = tick - max(current_elapsed, restored_elapsed) / 1000
            return execution, dict(stored_digests)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ReviewInputError,
            RecursionError,
        ):
            raise ReviewInputError(
                "work recovery artifact is expired, unauthenticated or incompatible"
            ) from None
