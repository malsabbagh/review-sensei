"""Closed operational observations; never approval facts or resource authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from review_sensei.bounded_evidence import MAX_PART_READS, EvidenceReadBudget

from .errors import GitHubHTTPTransientError

_STAGES = frozenset(
    {
        "eligibility.readback",
        "check.publication",
        "check.capability",
        "cleanup.discovery",
        "cleanup.hold",
        "cleanup.capability",
        "cleanup.root-readback",
        "cleanup.authority-fence",
        "cleanup.resolve-request",
        "cleanup.resolve-readback",
        "approval.preflight",
        "approval.thread-scan",
        "approval.persisted-readback",
        "approval.request",
        "approval.readback",
    }
)
_GRAPHQL_CATEGORIES = frozenset(
    {
        "FORBIDDEN",
        "RATE_LIMITED",
        "NOT_FOUND",
        "UNAUTHORIZED",
        "INTERNAL",
        "GRAPHQL_VALIDATION_FAILED",
    }
)


@dataclass(frozen=True)
class OperationFailure:
    stage: str
    reason: str
    http_status: int | None
    graphql_categories: tuple[str, ...]
    budget_calls: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "reason": self.reason,
            "http_status": self.http_status,
            "graphql_categories": list(self.graphql_categories),
            "budget_calls": self.budget_calls,
        }


@dataclass(frozen=True)
class PhaseFailures:
    first: OperationFailure
    count: int

    def to_dict(self) -> dict[str, object]:
        return {"first": self.first.to_dict(), "count": self.count}


@dataclass(frozen=True)
class OperationDiagnostics:
    eligibility: PhaseFailures | None
    check: PhaseFailures | None
    cleanup: PhaseFailures | None
    approval: PhaseFailures | None
    budget_calls: int | None
    approval_request_attempted: bool
    transport_request_attempts: int
    next_action: str

    def to_dict(self) -> dict[str, object]:
        phases: dict[str, object] = {}
        for name in ("eligibility", "check", "cleanup", "approval"):
            item = getattr(self, name)
            phases[name] = item.to_dict() if item is not None else None
        return {
            "schema_version": "1.0",
            **phases,
            "budget_calls": self.budget_calls,
            "call_limit": MAX_PART_READS,
            "ordinary_call_limit": MAX_PART_READS - 4,
            "approval_request_attempted": self.approval_request_attempted,
            # An HTTP adapter invocation is not evidence of network dispatch:
            # a deeper original allowance may still reject it locally.
            "network_dispatch_observed": None,
            "transport_request_attempts": self.transport_request_attempts,
            "next_action": self.next_action,
        }


class OperationObserver:
    """One request-local first failure per phase, with bounded aggregate counts."""

    def __init__(self, budget: EvidenceReadBudget | None = None) -> None:
        self.budget = budget
        self.stage = "eligibility.readback"
        self.failures: dict[str, PhaseFailures] = {}
        self.requests = 0
        self.approval_request_attempted = False
        self._last_failure: tuple[str, int] | None = None

    def at(self, stage: str) -> None:
        self.stage = stage if stage in _STAGES else "eligibility.readback"

    def _calls(self) -> int | None:
        calls = self.budget.calls if self.budget is not None else None
        return calls if type(calls) is int and 0 <= calls <= MAX_PART_READS else None

    def request(self, method: str, path: str) -> None:
        self.requests = min(self.requests + 1, 1000)
        if (
            self.stage == "approval.request"
            and method == "POST"
            and path.endswith("/reviews")
        ):
            self.approval_request_attempted = True

    def _fail(
        self, reason: str, status: int | None = None, categories: tuple[str, ...] = ()
    ) -> None:
        phase = self.stage.split(".", 1)[0]
        identity = (phase, self.requests)
        if self._last_failure == identity:
            return  # The caller's parse/raise must not recount one HTTP failure.
        self._last_failure = identity
        first = self.failures.get(phase)
        self.failures[phase] = PhaseFailures(
            first.first
            if first is not None
            else OperationFailure(
                self.stage, reason, status, categories, self._calls()
            ),
            min(1000, first.count + 1) if first is not None else 1,
        )

    def response(self, status: int, payload: Any) -> None:
        if type(status) is int and 400 <= status <= 599:
            self._fail("http-rejected", status)
        elif isinstance(payload, dict) and payload.get("errors") not in (None, []):
            errors = payload["errors"]
            categories: set[str] = set()
            if isinstance(errors, list):
                for error in errors[:8]:
                    value = error.get("type") if isinstance(error, dict) else None
                    categories.add(
                        value
                        if isinstance(value, str) and value in _GRAPHQL_CATEGORIES
                        else "OTHER"
                    )
            self._fail(
                "graphql-rejected",
                status if type(status) is int and 100 <= status <= 599 else None,
                tuple(sorted(categories)[:3]),
            )

    def exception(self, error: BaseException) -> None:
        reason = "unknown"
        current: BaseException | None = error
        for _ in range(4):
            if current is None:
                break
            message = str(current)
            calls = self._calls()
            if message in {
                "partition shared request budget exhausted",
                "broker internal liability exceeds original budget",
            } or (
                message == "review scan shared budget exhausted"
                and calls is not None
                and calls >= MAX_PART_READS - 4
            ):
                reason = "call-limit"
                break
            if message in {
                "ordinary dispatch hold is already outstanding",
                "ordinary dispatch hold cannot coexist with an activation tail",
                "ordinary dispatch hold release does not match",
            }:
                reason = "hold-conflict"
                break
            if message == "partition read deadline exhausted":
                reason = "deadline"
                break
            if isinstance(current, GitHubHTTPTransientError):
                reason = "transport"
            if message in {
                "thread root changed",
                "resolution source changed",
                "thread resolution authority changed",
            }:
                reason = "changed-authority"
                break
            current = current.__cause__
        self._fail(reason)

    def missing_evidence(self) -> None:
        self._fail("invalid-evidence")

    def report(self, *, completed: bool = False) -> OperationDiagnostics | None:
        if not self.failures:
            return None
        return OperationDiagnostics(
            eligibility=self.failures.get("eligibility"),
            check=self.failures.get("check"),
            cleanup=self.failures.get("cleanup"),
            approval=self.failures.get("approval"),
            budget_calls=self._calls(),
            approval_request_attempted=self.approval_request_attempted,
            transport_request_attempts=self.requests,
            next_action="none" if completed else "inspect_operation_failure",
        )
