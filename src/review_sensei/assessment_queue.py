"""Complete assessment queues and operation receipts for trusted persistence.

This module does not authenticate a document or provide a storage service. A
host must read the latest owned authority and conditionally fence every write by
its generation through the common evidence-partition adapter. Integrity digests
alone never authorize a source, a target, a resolution or a fresh allowance.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from .bounded_evidence import canonical_bytes
from .budgets import EffectiveWorkBudget
from .errors import ReviewInputError
from .evidence import EvidenceSnapshot, evidence_digest
from .execution import CheckpointMutation, CompletedBatch, WorkExecution
from .human_assessment import PendingHumanReview
from .outcomes import ResourceBudgetTracker
from .planning import ReviewWorkPlan, WorkBatch

T = TypeVar("T")
_HASH = re.compile(r"^[a-f0-9]{64}$")
MAX_QUEUE_FINDINGS = 250
MAX_OPERATION_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_OPERATION_RETENTION_SECONDS = 24 * 60 * 60
MAX_RETAINED_ASSESSMENT_OPERATIONS = 32
QUEUE_INTERFACE_VERSION = "assessment-queue-v1"


def _hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def inventory_digest(pending: PendingHumanReview) -> str:
    """Bind all immutable instances/prose/groups, independently of resolutions."""
    document = getattr(pending, "inventory_document", None)
    if callable(document):
        return hashlib.sha256(canonical_bytes(document())).hexdigest()
    # Compatibility bridge for pre-B main. Matches B's frozen immutable domain
    # projection, including original ordering and every metadata field.
    value: dict[str, object] = {
        "base_sha": pending.base_sha,
        "findings": [item.to_dict() for item in pending.findings],
        "resolved": [],
    }
    if any(item.required_paths for item in pending.findings):
        value["schema_version"] = "2"
        value["findings"] = [
            {**item.to_dict(), "required_paths": list(item.evidence_paths)}
            for item in pending.findings
        ]
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class AssessmentQueue:
    snapshot: EvidenceSnapshot
    inventory_digest: str
    inventory_ids: tuple[str, ...]
    pending_order: tuple[str, ...]
    resolved_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot, EvidenceSnapshot)
            or not self.snapshot.base_sha
            or not self.snapshot.head_sha
            or not _hash(self.inventory_digest)
            or not isinstance(self.inventory_ids, tuple)
            or not 1 <= len(self.inventory_ids) <= MAX_QUEUE_FINDINGS
            or not isinstance(self.pending_order, tuple)
            or not isinstance(self.resolved_ids, tuple)
        ):
            raise ReviewInputError("assessment queue identity is invalid")
        assigned = self.pending_order + self.resolved_ids
        if (
            any(not _hash(item) for item in self.inventory_ids + assigned)
            or len(set(self.inventory_ids)) != len(self.inventory_ids)
            or len(set(assigned)) != len(assigned)
            or set(assigned) != set(self.inventory_ids)
        ):
            raise ReviewInputError("assessment queue must retain every instance once")

    @classmethod
    def create(
        cls, pending: PendingHumanReview, snapshot: EvidenceSnapshot
    ) -> AssessmentQueue:
        if pending.base_sha != snapshot.base_sha:
            raise ReviewInputError("assessment queue snapshot is stale")
        return cls(
            snapshot,
            inventory_digest(pending),
            tuple(sorted(item.fingerprint for item in pending.findings)),
            tuple(sorted(item.fingerprint for item in pending.pending)),
            tuple(sorted(pending.resolved)),
        )

    def require_current(
        self, pending: PendingHumanReview, snapshot: EvidenceSnapshot
    ) -> None:
        if (
            self.snapshot != snapshot
            or self.inventory_digest != inventory_digest(pending)
            or set(self.resolved_ids) != set(pending.resolved)
        ):
            raise ReviewInputError("assessment queue authority is stale or foreign")

    def select(
        self, *, targets: tuple[str, ...] | None = None, limit: int = 32
    ) -> tuple[str, ...]:
        """Select exact full instance IDs; short/prose selectors are unsupported.

        The trusted host must authenticate explicit selectors against the same
        snapshot/inventory before calling this seam. No omitted ID is retired.
        """
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 <= limit <= 250
        ):
            raise ReviewInputError("assessment queue selection limit is invalid")
        if targets is None:
            return self.pending_order[:limit]
        if (
            not isinstance(targets, tuple)
            or not targets
            or len(targets) > MAX_QUEUE_FINDINGS
            or any(not _hash(item) for item in targets)
            or len(set(targets)) != len(targets)
            or not set(targets) <= set(self.pending_order)
        ):
            raise ReviewInputError("assessment targets are unknown, stale or ambiguous")
        return tuple(item for item in self.pending_order if item in targets)[:limit]

    def advance(
        self, *, visited: tuple[str, ...], resolved: tuple[str, ...]
    ) -> AssessmentQueue:
        if (
            not isinstance(visited, tuple)
            or not isinstance(resolved, tuple)
            or len(set(visited)) != len(visited)
            or len(set(resolved)) != len(resolved)
            or not set(visited) <= set(self.pending_order)
            or not set(resolved) <= set(self.inventory_ids)
            or not set(self.resolved_ids) <= set(resolved)
            or not (set(resolved) - set(self.resolved_ids)) <= set(visited)
        ):
            raise ReviewInputError("assessment queue progress loses authority")
        remaining = tuple(item for item in self.pending_order if item not in resolved)
        return replace(
            self,
            pending_order=tuple(item for item in remaining if item not in visited)
            + tuple(item for item in remaining if item in visited),
            resolved_ids=tuple(sorted(resolved)),
        )

    def reconcile(self, pending: PendingHumanReview) -> AssessmentQueue:
        """Join new inventory instances without allowing omission or reopening."""
        identities = {item.fingerprint for item in pending.findings}
        if (
            pending.base_sha != self.snapshot.base_sha
            or not set(self.inventory_ids) <= identities
            or not set(self.resolved_ids) <= set(pending.resolved)
        ):
            raise ReviewInputError(
                "assessment inventory reconciliation loses obligations"
            )
        new = identities - set(self.inventory_ids)
        resolved = set(pending.resolved)
        return replace(
            self,
            inventory_digest=inventory_digest(pending),
            inventory_ids=tuple(sorted(identities)),
            pending_order=tuple(
                item for item in self.pending_order if item not in resolved
            )
            + tuple(sorted(new - resolved)),
            resolved_ids=tuple(sorted(resolved)),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": QUEUE_INTERFACE_VERSION,
            "snapshot": self.snapshot.to_dict(),
            "inventory_digest": self.inventory_digest,
            "inventory_ids": list(self.inventory_ids),
            "pending_order": list(self.pending_order),
            "resolved_ids": list(self.resolved_ids),
        }

    @classmethod
    def from_document(cls, value: object) -> AssessmentQueue:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "snapshot",
                "inventory_digest",
                "inventory_ids",
                "pending_order",
                "resolved_ids",
            }
            or value["schema_version"] != QUEUE_INTERFACE_VERSION
        ):
            raise ReviewInputError("assessment queue document is invalid")
        try:
            if (
                any(
                    not isinstance(value[name], list)
                    for name in ("inventory_ids", "pending_order", "resolved_ids")
                )
                or not isinstance(value["snapshot"], dict)
                or set(value["snapshot"])
                != {"repository", "pull_request", "base_sha", "head_sha"}
            ):
                raise ValueError("fields")
            return cls(
                EvidenceSnapshot(**value["snapshot"]),
                value["inventory_digest"],
                tuple(value["inventory_ids"]),
                tuple(value["pending_order"]),
                tuple(value["resolved_ids"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ReviewInputError("assessment queue document is invalid") from exc


class AssessmentCheckpoint:
    """Normalized receipt codec over a host-authenticated, fenced document store.

    ``read`` must return the latest owned operation receipt or None on proven
    absence. ``write`` must stage/read back A's immutable parts and refuse stale
    authority/generation before activation. It must never do an unfenced update.
    Hosts must retain the operation ID/tombstone through expiry: deleting an
    existing receipt and treating it as new would mint another allowance.
    """

    def __init__(
        self,
        *,
        operation_id: str,
        read: Callable[[], object | None],
        write: Callable[[dict[str, object]], None],
        on_mutation: Callable[[dict[str, object], CheckpointMutation], None]
        | None = None,
        root_generation: Callable[[], int] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        retention_seconds: int = 6 * 60 * 60,
    ) -> None:
        if (
            not _hash(operation_id)
            or isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or not 1 <= retention_seconds <= MAX_OPERATION_RETENTION_SECONDS
        ):
            raise ReviewInputError(
                "assessment operation identity or retention is invalid"
            )
        self.operation_id = operation_id
        self.read = read
        self.write = write
        if (on_mutation is None) != (root_generation is None):
            raise ReviewInputError(
                "checkpoint mutation requires a fenced generation getter"
            )
        self.on_mutation = on_mutation
        self.root_generation = root_generation
        self.now = now
        self.retention_seconds = retention_seconds
        self._origin: datetime | None = None
        self._expires: datetime | None = None
        self._deadline: datetime | None = None
        self._saved: datetime | None = None
        self._queue: AssessmentQueue | None = None
        self._restored_digests: dict[str, str | None] = {}

    def prepare_queue(
        self,
        queue: AssessmentQueue,
        pending: PendingHumanReview,
        snapshot: EvidenceSnapshot,
    ) -> AssessmentQueue:
        """Check receipts before rejecting targets resolved after admission.

        Mutable resolutions may grow. Receipt revalidation uses the original
        admitted view; the host unions accepted results into latest authority.
        """
        value = self.read()
        if value is None:
            queue.require_current(pending, snapshot)
            self._queue = queue
            return queue
        if (
            not isinstance(value, dict)
            or value.get("operation_id") != self.operation_id
        ):
            raise ReviewInputError("assessment operation receipt binding is invalid")
        original = AssessmentQueue.from_document(value.get("admitted_queue"))
        if (
            original.snapshot != snapshot
            or original.inventory_digest != inventory_digest(pending)
            or not set(original.resolved_ids) <= set(pending.resolved)
        ):
            raise ReviewInputError("assessment operation inventory authority is stale")
        self._queue = original
        return original

    def _clock(self) -> datetime:
        value = self.now()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ReviewInputError("assessment operation clock must be aware")
        return value.astimezone(timezone.utc)

    def save(
        self,
        execution: WorkExecution[T],
        tracker: ResourceBudgetTracker,
        encode: Callable[[T], object],
        request_digests: Mapping[str, str | None],
        *,
        mutation: CheckpointMutation | None = None,
    ) -> None:
        now = self._clock()
        if execution.tracker_identity != tracker.execution_identity:
            raise ReviewInputError("assessment operation tracker identity is invalid")
        if self._origin is None:
            self._origin = now - timedelta(milliseconds=tracker.elapsed_ms())
            self._deadline = self._origin + timedelta(
                milliseconds=tracker.budget.timeout_ms
            )
            self._expires = self._origin + timedelta(seconds=self.retention_seconds)
        assert self._expires is not None and self._deadline is not None
        if (
            now < self._origin
            or now >= self._expires
            or (self._saved is not None and now < self._saved)
        ):
            raise ReviewInputError(
                "assessment operation receipt expired or clock regressed"
            )
        document: dict[str, object] = {
            "schema_version": QUEUE_INTERFACE_VERSION,
            "operation_id": self.operation_id,
            "plan_id": execution.plan.plan_id,
            "execution_identity": tracker.execution_identity,
            "resource_budget": asdict(tracker.budget),
            "request_digests": dict(request_digests),
            "created_at": self._origin.isoformat(),
            "deadline_at": self._deadline.isoformat(),
            "expires_at": self._expires.isoformat(),
            "saved_at": now.isoformat(),
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
            "pending": [list(item) for item in execution.pending],
            "attempted_ids": list(execution.attempted_ids),
            "response_bytes_reserved": execution.response_bytes_reserved,
            "admitted_queue": self._queue.to_document()
            if self._queue is not None
            else None,
        }
        if (
            len(
                json.dumps(document, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            )
            > MAX_OPERATION_RECEIPT_BYTES
        ):
            raise ReviewInputError(
                "assessment operation receipt exceeds aggregate storage"
            )
        if self.on_mutation is not None:
            if mutation is None or self.root_generation is None:
                raise ReviewInputError("checkpoint mutation identity is required")
            context = replace(mutation, root_generation=self.root_generation())
            self.on_mutation(document, context)
        else:
            self.write(document)
        self._saved = now

    def load(
        self,
        plan: ReviewWorkPlan,
        tracker: ResourceBudgetTracker,
        budgets: EffectiveWorkBudget,
        decode: Callable[[object, WorkBatch], T],
        request_digests: Mapping[str, str | None],
    ) -> WorkExecution[T] | None:
        return self._load(plan, tracker, budgets, decode, request_digests)

    def load_admission(
        self,
        plan: ReviewWorkPlan,
        tracker: ResourceBudgetTracker,
        budgets: EffectiveWorkBudget,
        decode: Callable[[object, WorkBatch], T],
    ) -> tuple[WorkExecution[T], Mapping[str, str | None]] | None:
        """Restore authenticated original accounting before request rendering.

        The executor must compare freshly rendered digests before dispatch or
        receipt activation. Stored digests never substitute for current evidence.
        """
        result = self._load(plan, tracker, budgets, decode, None)
        return (result, dict(self._restored_digests)) if result is not None else None

    def _load(
        self,
        plan: ReviewWorkPlan,
        tracker: ResourceBudgetTracker,
        budgets: EffectiveWorkBudget,
        decode: Callable[[object, WorkBatch], T],
        request_digests: Mapping[str, str | None] | None,
    ) -> WorkExecution[T] | None:
        value = self.read()
        if value is None:
            return None
        try:
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "schema_version",
                    "operation_id",
                    "plan_id",
                    "execution_identity",
                    "resource_budget",
                    "request_digests",
                    "created_at",
                    "deadline_at",
                    "expires_at",
                    "saved_at",
                    "elapsed_ms",
                    "counters",
                    "completed",
                    "pending",
                    "attempted_ids",
                    "response_bytes_reserved",
                    "admitted_queue",
                }
                or len(
                    json.dumps(value, ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
                > MAX_OPERATION_RECEIPT_BYTES
            ):
                raise ValueError("fields")
            if (
                value["schema_version"] != QUEUE_INTERFACE_VERSION
                or value["operation_id"] != self.operation_id
                or value["plan_id"] != plan.plan_id
                or value["resource_budget"] != asdict(tracker.budget)
                or (
                    request_digests is not None
                    and value["request_digests"] != dict(request_digests)
                )
            ):
                raise ValueError("binding")
            stored_digests = value["request_digests"]
            if (
                not isinstance(stored_digests, dict)
                or set(stored_digests) != {batch.batch_id for batch in plan.batches}
                or any(
                    digest is not None and not _hash(digest)
                    for digest in stored_digests.values()
                )
            ):
                raise ValueError("request binding")
            request_digests = stored_digests
            origin, deadline, expires, saved = (
                datetime.fromisoformat(value[name])
                for name in ("created_at", "deadline_at", "expires_at", "saved_at")
            )
            now = self._clock()
            if any(
                item.tzinfo is None for item in (origin, deadline, expires, saved)
            ) or not (
                origin <= saved <= now < expires
                and 0
                < (expires - origin).total_seconds()
                <= MAX_OPERATION_RETENTION_SECONDS
                and deadline
                == origin + timedelta(milliseconds=tracker.budget.timeout_ms)
            ):
                raise ValueError("expiry")
            identity = value["execution_identity"]
            if (
                not isinstance(identity, str)
                or re.fullmatch(r"[a-f0-9]{32}", identity) is None
                or (tracker.provider_calls and tracker.execution_identity != identity)
            ):
                raise ValueError("tracker")
            counters = value["counters"]
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
            elapsed = value["elapsed_ms"]
            if (
                isinstance(elapsed, bool)
                or not isinstance(elapsed, int)
                or not 0 <= elapsed <= MAX_OPERATION_RETENTION_SECONDS * 1000
            ):
                raise ValueError("elapsed")
            batches = {item.batch_id: item for item in plan.batches}
            completed = []
            if not isinstance(value["completed"], list) or len(
                value["completed"]
            ) > len(batches):
                raise ValueError("completed")
            for item in value["completed"]:
                if not isinstance(item, dict) or set(item) != {
                    "batch_id",
                    "request_digest",
                    "result",
                }:
                    raise ValueError("completed fields")
                batch = batches[item["batch_id"]]
                if item["request_digest"] != request_digests.get(batch.batch_id):
                    raise ValueError("request")
                completed.append(
                    CompletedBatch(
                        batch, decode(item["result"], batch), item["request_digest"]
                    )
                )
            pending = value["pending"]
            if (
                not isinstance(pending, list)
                or len(pending) > len(plan.requirements)
                or any(
                    not isinstance(item, list)
                    or len(item) != 2
                    or any(not isinstance(part, str) for part in item)
                    for item in pending
                )
            ):
                raise ValueError("pending")
            result = WorkExecution(
                plan,
                tuple(completed),
                tuple(tuple(item) for item in pending),
                identity,
                tuple(value["attempted_ids"]),
                value["response_bytes_reserved"],
            )
            if not isinstance(value["attempted_ids"], list) or (
                value["admitted_queue"]
                != (self._queue.to_document() if self._queue is not None else None)
            ):
                raise ValueError("queue binding")
            attempted = set(result.attempted_ids)
            attempted_batches = tuple(
                batch
                for batch in plan.batches
                if attempted.intersection(batch.requirement_ids)
            )
            if (
                any(
                    not set(item.batch.requirement_ids) <= attempted
                    for item in completed
                )
                or any(
                    not set(batch.requirement_ids) <= attempted
                    for batch in attempted_batches
                )
                or len(attempted_batches) > counters["provider_calls"]
            ):
                raise ValueError("uncharged receipt")
            if (
                result.response_bytes_reserved + counters["response_bytes"]
                > budgets.max_total_output_bytes
            ):
                raise ValueError("output reservation")
            self._origin, self._expires, self._deadline = origin, expires, deadline
            self._saved = saved
            tracker.execution_identity = identity
            for name, count in counters.items():
                setattr(tracker, name, max(getattr(tracker, name), count))
            restored = max(
                elapsed + int((now - saved).total_seconds() * 1000),
                int((now - origin).total_seconds() * 1000),
            )
            tick = tracker.monotonic()
            current_elapsed = max(0, int((tick - tracker.started) * 1000))
            tracker.started = tick - max(current_elapsed, restored) / 1000
            self._restored_digests = dict(stored_digests)
            return result
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ReviewInputError,
            RecursionError,
        ):
            raise ReviewInputError(
                "assessment operation receipt is expired or incompatible"
            ) from None


class AssessmentJournal:
    """Bounded complete queue/receipt payload for A's owned queue manifest.

    Hosts retain this whole journal through source delivery/finalization and
    reload it before each guarded write. Exhausted retention refuses admission;
    no eviction/GC or random-source replacement creates another allowance.
    This codec cannot turn a non-atomic remote PATCH into a concurrent CAS.
    """

    def __init__(
        self, queue: AssessmentQueue, operations: tuple[dict[str, object], ...] = ()
    ) -> None:
        if (
            not isinstance(queue, AssessmentQueue)
            or not isinstance(operations, tuple)
            or len(operations) > MAX_RETAINED_ASSESSMENT_OPERATIONS
        ):
            raise ReviewInputError(
                "assessment journal exceeds retained operation bounds"
            )
        sources = set()
        for item in operations:
            if (
                not isinstance(item, dict)
                or set(item) != {"source_digest", "operation_id", "receipt"}
                or not _hash(item["source_digest"])
                or not _hash(item["operation_id"])
                or item["source_digest"] in sources
                or not isinstance(item["receipt"], dict)
                or item["receipt"].get("operation_id") != item["operation_id"]
            ):
                raise ReviewInputError(
                    "assessment journal source/operation binding is invalid"
                )
            sources.add(item["source_digest"])
        document = {
            "schema_version": QUEUE_INTERFACE_VERSION,
            "queue": queue.to_document(),
            "operations": list(operations),
        }
        try:
            self._bytes = canonical_bytes(document)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ReviewInputError("assessment journal document is invalid") from exc
        if len(self._bytes) > MAX_OPERATION_RECEIPT_BYTES:
            raise ReviewInputError("assessment journal exceeds aggregate storage")

    def to_document(self) -> dict[str, object]:
        return json.loads(self._bytes)

    def _operations(self) -> tuple[dict[str, object], ...]:
        return tuple(json.loads(self._bytes)["operations"])

    @classmethod
    def from_document(cls, value: object) -> AssessmentJournal:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "queue", "operations"}
            or value["schema_version"] != QUEUE_INTERFACE_VERSION
            or not isinstance(value["operations"], list)
        ):
            raise ReviewInputError("assessment journal document is invalid")
        return cls(
            AssessmentQueue.from_document(value["queue"]), tuple(value["operations"])
        )

    def receipt(self, *, source_digest: str, operation_id: str) -> object | None:
        if not _hash(source_digest) or not _hash(operation_id):
            raise ReviewInputError("assessment journal lookup identity is invalid")
        for item in self._operations():
            if item["source_digest"] == source_digest:
                if item["operation_id"] != operation_id:
                    raise ReviewInputError(
                        "existing assessment source authority changed"
                    )
                return item["receipt"]
        return None

    def record(
        self, *, queue: AssessmentQueue, source_digest: str, receipt: dict[str, object]
    ) -> AssessmentJournal:
        operation_id = receipt.get("operation_id")
        if not _hash(operation_id) or not _hash(source_digest):
            raise ReviewInputError("assessment journal receipt identity is invalid")
        assert isinstance(operation_id, str)
        previous = self.receipt(source_digest=source_digest, operation_id=operation_id)
        if previous is not None:
            if not isinstance(previous, dict) or any(
                previous.get(name) != receipt.get(name)
                for name in (
                    "schema_version",
                    "operation_id",
                    "plan_id",
                    "execution_identity",
                    "resource_budget",
                    "request_digests",
                    "created_at",
                    "deadline_at",
                    "expires_at",
                    "admitted_queue",
                )
            ):
                raise ReviewInputError(
                    "assessment journal update changed original admission"
                )
            old_counts, new_counts = previous.get("counters"), receipt.get("counters")
            if (
                not isinstance(old_counts, dict)
                or not isinstance(new_counts, dict)
                or set(old_counts) != set(new_counts)
                or any(
                    not isinstance(old_counts[name], int)
                    or isinstance(old_counts[name], bool)
                    or not isinstance(new_counts[name], int)
                    or isinstance(new_counts[name], bool)
                    or new_counts[name] < old_counts[name]
                    for name in old_counts
                )
            ):
                raise ReviewInputError(
                    "assessment journal update refunded charged resources"
                )
            old_completed, new_completed = (
                previous.get("completed"),
                receipt.get("completed"),
            )
            old_attempted, new_attempted = (
                previous.get("attempted_ids"),
                receipt.get("attempted_ids"),
            )
            if (
                not isinstance(old_completed, list)
                or not isinstance(new_completed, list)
                or not isinstance(old_attempted, list)
                or not isinstance(new_attempted, list)
                or any(item not in new_completed for item in old_completed)
                or not set(old_attempted) <= set(new_attempted)
            ):
                raise ReviewInputError(
                    "assessment journal update lost accepted receipts"
                )
        prior_queue = AssessmentQueue.from_document(self.to_document()["queue"])
        if (
            prior_queue.snapshot != queue.snapshot
            or not set(prior_queue.inventory_ids) <= set(queue.inventory_ids)
            or not set(prior_queue.resolved_ids) <= set(queue.resolved_ids)
        ):
            raise ReviewInputError("assessment journal update loses obligations")
        # The normalized accepted receipt and its queue progress activate in
        # one document. A crash after charge or acceptance cannot leave the
        # durable cursor/resolution behind its authenticated receipt.
        accepted: set[str] = set()
        completed, attempted = receipt.get("completed"), receipt.get("attempted_ids")
        if (
            not isinstance(completed, list)
            or not isinstance(attempted, list)
            or any(not _hash(item) for item in attempted)
        ):
            raise ReviewInputError("assessment journal progress receipt is invalid")
        for batch in completed:
            if not isinstance(batch, dict) or not isinstance(batch.get("result"), dict):
                raise ReviewInputError("assessment journal accepted batch is invalid")
            decisions = batch["result"].get("assessments")
            if not isinstance(decisions, list):
                raise ReviewInputError(
                    "assessment journal normalized decisions are missing"
                )
            for decision in decisions:
                if (
                    not isinstance(decision, dict)
                    or not _hash(decision.get("fingerprint"))
                    or decision.get("decision")
                    not in {"addressed", "dismissed", "unresolved"}
                ):
                    raise ReviewInputError(
                        "assessment journal normalized decision is invalid"
                    )
                if decision["decision"] != "unresolved":
                    accepted.add(decision["fingerprint"])
        queue = queue.advance(
            visited=tuple(item for item in attempted if item in queue.pending_order),
            resolved=tuple(sorted(set(queue.resolved_ids) | accepted)),
        )
        operations = tuple(
            item
            for item in self._operations()
            if item["source_digest"] != source_digest
        ) + (
            {
                "source_digest": source_digest,
                "operation_id": operation_id,
                "receipt": receipt,
            },
        )
        return AssessmentJournal(queue, operations)


def assessment_operation_id(
    *,
    snapshot: EvidenceSnapshot,
    inventory: str,
    authority: str,
    source: str,
    provider: str,
    policy: str,
    evidence: str,
    targets: tuple[str, ...] | None,
    inventory_generation: int = 0,
) -> str:
    """The host-supplied source digest includes complete actor/comment authority.

    A new source event is distinct; replay of the same event cannot mint a fresh
    operation. Text alone, random run IDs and conversation excerpts cannot be
    used as source authentication. All digests are revalidated by their owners.
    """
    if any(
        not _hash(item)
        for item in (inventory, authority, source, provider, policy, evidence)
    ):
        raise ReviewInputError("assessment operation binding is invalid")
    if (
        isinstance(inventory_generation, bool)
        or not isinstance(inventory_generation, int)
        or not 0 <= inventory_generation <= 2147483647
    ):
        raise ReviewInputError("assessment operation inventory generation is invalid")
    if targets is not None and (
        not isinstance(targets, tuple)
        or not targets
        or any(not _hash(item) for item in targets)
        or len(set(targets)) != len(targets)
        or len(targets) > MAX_QUEUE_FINDINGS
    ):
        raise ReviewInputError("assessment operation targets are invalid")
    return evidence_digest(
        {
            "domain": QUEUE_INTERFACE_VERSION,
            "snapshot": snapshot.to_dict(),
            "inventory": inventory,
            "inventory_generation": inventory_generation,
            "authority": authority,
            "source": source,
            "provider": provider,
            "policy": policy,
            "evidence": evidence,
            "targets": None if targets is None else sorted(targets),
        }
    )
