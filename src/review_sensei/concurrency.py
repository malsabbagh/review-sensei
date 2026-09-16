"""Provider-neutral concurrency policy for review hosts.

The library does not own a process-wide queue or a workflow runner.  Instead,
it exposes the stable groups and admission rules that an embedding application
can enforce with its scheduler.  Keeping this policy in the review package
lets different hosts agree on the same latest-wins and per-pull-request
provider behavior without importing GitHub or provider SDKs.

GitHub-hosted runs use native workflow ``concurrency`` groups: one active
review per repository and pull request, with cancel-in-progress for latest
wins.  ``ProviderAdmission`` is the in-process library contract for tests and
local hosts.  It is not a global lock and does not coordinate separate
processes or GitHub Actions jobs.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from .errors import AdmissionCancelled, AdmissionRejected, ReviewInputError
from .models import ReviewRequest

_LOGGER = logging.getLogger(__name__)
_MAX_WAITERS = 1024
_WAITER_POLL_SECONDS = 0.05
ADMISSION_STATUSES = frozenset(
    {
        "admitted",
        "released",
        "failed",
        "cancelled",
        "rejected_capacity",
    }
)

_SAFE_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_TRIGGER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _safe_identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{label} must be a non-empty safe identifier")
    normalized = value.strip()
    if not normalized or not pattern.fullmatch(normalized):
        raise ReviewInputError(f"{label} must be a non-empty safe identifier")
    return normalized


def _positive_number(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewInputError(f"{label} must be a positive integer")
    if value > 2147483647:
        raise ReviewInputError(f"{label} is too large (maximum is 2147483647)")
    return value


def _encode_components(namespace: str, *components: str) -> str:
    """Encode components with length prefixing to guarantee collision-free keys."""
    encoded_parts = [f"{len(part)}:{part}" for part in components]
    return f"review-sensei:{namespace}:" + ":".join(encoded_parts)


def _trigger_identifier(value: object) -> str:
    if isinstance(value, bool):
        raise ReviewInputError(
            "trigger_id must be a positive integer or safe identifier"
        )
    if isinstance(value, int):
        return str(_positive_number(value, label="trigger_id"))
    return _safe_identifier(value, label="trigger_id", pattern=_SAFE_TRIGGER)


@dataclass(frozen=True)
class ConcurrencyGroup:
    """Admission policy for one host-enforced concurrency group."""

    key: str
    max_active: int = 1
    cancel_in_progress: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ReviewInputError("concurrency group key must be a non-empty string")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.key):
            raise ReviewInputError(
                "concurrency group key must not contain control characters"
            )
        if (
            isinstance(self.max_active, bool)
            or not isinstance(self.max_active, int)
            or self.max_active < 1
        ):
            raise ReviewInputError("concurrency max_active must be a positive integer")
        if not isinstance(self.cancel_in_progress, bool):
            raise ReviewInputError("concurrency cancel_in_progress must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "max_active": self.max_active,
            "cancel_in_progress": self.cancel_in_progress,
        }


@dataclass(frozen=True)
class ReviewConcurrencyPlan:
    """Host-enforced concurrency policy for one review trigger.

    A pull-request review has two scopes:

    * ``workflow`` is latest-wins, so a newer revision supersedes an older
      review for the same repository and pull request.
    * ``provider`` admits one provider-consuming stage for that pull request at
      a time, without cancelling the active stage.  Its key includes the
      provider and pull request, so unrelated pull requests remain independent.

    A non-review trigger receives only an isolated workflow group.  This is for
    ignored or informational triggers that must not cancel an active review.
    The plan describes policy; the caller remains responsible for enforcing it
    across processes, workers, or hosted workflow runs.
    """

    workflow: ConcurrencyGroup
    provider: ConcurrencyGroup | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, ConcurrencyGroup):
            raise ReviewInputError("workflow must be a ConcurrencyGroup")
        if self.provider is not None and not isinstance(
            self.provider, ConcurrencyGroup
        ):
            raise ReviewInputError("provider must be a ConcurrencyGroup or None")

    @classmethod
    def for_pull_request(
        cls,
        repository: str,
        pull_request_number: int,
        *,
        provider_name: str = "ollama",
    ) -> "ReviewConcurrencyPlan":
        """Build the latest-wins and per-provider policy for a pull request."""

        repository_id = _safe_identifier(
            repository,
            label="repository",
            pattern=_SAFE_REPOSITORY,
        )
        number = _positive_number(pull_request_number, label="pull_request_number")
        provider_id = _safe_identifier(
            provider_name,
            label="provider_name",
            pattern=_SAFE_PROVIDER,
        )
        return cls(
            workflow=ConcurrencyGroup(
                key=_encode_components("review", repository_id, str(number)),
                max_active=1,
                cancel_in_progress=True,
            ),
            provider=ConcurrencyGroup(
                key=_encode_components(
                    "provider", provider_id, repository_id, str(number)
                ),
                max_active=1,
                cancel_in_progress=False,
            ),
        )

    @classmethod
    def for_request(
        cls,
        request: ReviewRequest,
        *,
        provider_name: str = "ollama",
    ) -> "ReviewConcurrencyPlan":
        """Build a plan from review metadata, failing when it is incomplete."""

        if not isinstance(request, ReviewRequest):
            raise ReviewInputError("request must be a ReviewRequest")
        if request.repository is None:
            raise ReviewInputError("concurrency requires review repository metadata")
        if request.pull_request_number is None:
            raise ReviewInputError("concurrency requires a pull request number")
        return cls.for_pull_request(
            request.repository,
            request.pull_request_number,
            provider_name=provider_name,
        )

    @classmethod
    def for_non_review_trigger(
        cls,
        repository: str,
        trigger_id: str | int,
    ) -> "ReviewConcurrencyPlan":
        """Isolate a trigger that will not run a provider review.

        Hosts can use this for ignored comments, duplicate notifications, or
        other events that should never cancel a pull-request review.  The
        trigger identifier is part of the group, so separate triggers do not
        contend with one another.
        """

        repository_id = _safe_identifier(
            repository,
            label="repository",
            pattern=_SAFE_REPOSITORY,
        )
        trigger = _trigger_identifier(trigger_id)
        return cls(
            workflow=ConcurrencyGroup(
                key=_encode_components("trigger", repository_id, trigger),
                max_active=1,
                cancel_in_progress=True,
            )
        )

    @property
    def workflow_key(self) -> str:
        return self.workflow.key

    @property
    def provider_key(self) -> str | None:
        return self.provider.key if self.provider is not None else None

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow": self.workflow.to_dict(),
            "provider": self.provider.to_dict() if self.provider is not None else None,
        }


@dataclass(frozen=True)
class AdmissionOutcome:
    """Metadata-only result of an in-process admission decision."""

    status: str
    key: str
    active: int
    waiters: int
    max_active: int
    max_waiters: int

    def __post_init__(self) -> None:
        if self.status not in ADMISSION_STATUSES:
            raise ReviewInputError("admission status is not a supported outcome")
        if not isinstance(self.key, str) or not self.key.strip():
            raise ReviewInputError("admission key must be a non-empty string")
        for label, value in (
            ("active", self.active),
            ("waiters", self.waiters),
            ("max_active", self.max_active),
            ("max_waiters", self.max_waiters),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReviewInputError(
                    f"admission {label} must be a non-negative integer"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "key": self.key,
            "active": self.active,
            "waiters": self.waiters,
            "max_active": self.max_active,
            "max_waiters": self.max_waiters,
        }


class AdmissionLease:
    """One granted in-process slot that must be released exactly once."""

    def __init__(self, admission: "ProviderAdmission") -> None:
        self._admission = admission
        self._released = False
        self._lock = threading.Lock()

    def release(self, *, outcome: str = "released") -> AdmissionOutcome:
        if outcome not in {"released", "failed", "cancelled"}:
            raise ReviewInputError("admission lease outcome is not supported")
        with self._lock:
            if self._released:
                return self._admission.snapshot(outcome)
            self._released = True
        return self._admission._release(outcome)

    def __enter__(self) -> "AdmissionLease":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        if exc_type is None:
            self.release(outcome="released")
            return None
        if exc_type in (AdmissionCancelled, KeyboardInterrupt, InterruptedError):
            self.release(outcome="cancelled")
            return None
        self.release(outcome="failed")
        return None


class ProviderAdmission:
    """Bounded in-process admission for one ``ConcurrencyGroup``.

    GitHub-hosted reviews do not use this helper.  They join the reusable
    workflow concurrency group (``max_active=1``, cancel-in-progress for
    reviews).  Local hosts and tests use one ``ProviderAdmission`` per group
    so unrelated pull requests and repositories stay independent.

    Waiters are bounded.  A request that would grow the waiter set past
    ``max_waiters`` is rejected immediately instead of accumulating.
    Cancelled or failed work releases the slot so a later review can run.
    """

    def __init__(
        self,
        group: ConcurrencyGroup,
        *,
        max_waiters: int | None = None,
    ) -> None:
        if not isinstance(group, ConcurrencyGroup):
            raise ReviewInputError("admission requires a ConcurrencyGroup")
        waiter_bound = group.max_active if max_waiters is None else max_waiters
        if (
            isinstance(waiter_bound, bool)
            or not isinstance(waiter_bound, int)
            or waiter_bound < 0
        ):
            raise ReviewInputError(
                "admission max_waiters must be a non-negative integer"
            )
        if waiter_bound > _MAX_WAITERS:
            raise ReviewInputError(
                f"admission max_waiters is too large (maximum is {_MAX_WAITERS})"
            )
        self._group = group
        self._max_waiters = waiter_bound
        self._lock = threading.Lock()
        self._slots = threading.Condition(self._lock)
        self._active = 0
        self._waiters = 0

    @property
    def group(self) -> ConcurrencyGroup:
        return self._group

    @property
    def max_waiters(self) -> int:
        return self._max_waiters

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def waiters(self) -> int:
        with self._lock:
            return self._waiters

    def snapshot(self, status: str = "admitted") -> AdmissionOutcome:
        with self._lock:
            return self._outcome_locked(status)

    def try_acquire(self) -> AdmissionLease | None:
        """Grant a slot immediately or reject without enqueueing a waiter."""

        with self._lock:
            if self._active >= self._group.max_active:
                self._log_locked("rejected_capacity")
                return None
            return self._grant_locked()

    def acquire(
        self,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AdmissionLease:
        """Grant a slot, waiting up to the waiter bound.

        ``timeout`` bounds how long one caller waits.  It does not grow the
        waiter set past ``max_waiters``.
        """

        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise ReviewInputError("admission timeout must be a non-negative number")
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        with self._lock:
            if self._active < self._group.max_active:
                return self._grant_locked()
            if self._waiters >= self._max_waiters:
                self._log_locked("rejected_capacity")
                raise AdmissionRejected(
                    "provider admission waiters are at the configured bound"
                )
            self._waiters += 1
            acquired = False
            try:
                while self._active >= self._group.max_active:
                    if cancel_event is not None and cancel_event.is_set():
                        self._log_locked("cancelled")
                        raise AdmissionCancelled(
                            "provider admission wait was cancelled"
                        )
                    remaining = None
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._log_locked("rejected_capacity")
                            raise AdmissionRejected("provider admission timed out")
                    wait_for = _WAITER_POLL_SECONDS
                    if remaining is not None:
                        wait_for = min(wait_for, remaining)
                    self._slots.wait(timeout=wait_for)
                self._waiters -= 1
                acquired = True
                return self._grant_locked()
            finally:
                if not acquired:
                    self._waiters -= 1

    def _grant_locked(self) -> AdmissionLease:
        self._active += 1
        self._log_locked("admitted")
        return AdmissionLease(self)

    def _release(self, outcome: str) -> AdmissionOutcome:
        with self._lock:
            if self._active < 1:
                raise ReviewInputError("admission lease is not active")
            self._active -= 1
            self._slots.notify()
            self._log_locked(outcome)
            return self._outcome_locked(outcome)

    def _outcome_locked(self, status: str) -> AdmissionOutcome:
        return AdmissionOutcome(
            status=status,
            key=self._group.key,
            active=self._active,
            waiters=self._waiters,
            max_active=self._group.max_active,
            max_waiters=self._max_waiters,
        )

    def _log_locked(self, status: str) -> None:
        # Metadata only: group key, counts, and a closed-set status. Never log
        # prompts, diffs, review bodies, or other source content.
        _LOGGER.info(
            "concurrency admission status=%s key=%s max_active=%s active=%s max_waiters=%s waiters=%s",
            status,
            self._group.key,
            self._group.max_active,
            self._active,
            self._max_waiters,
            self._waiters,
        )
