"""One subordinate decision/verification allowance over the original tracker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .budgets import MAX_TOTAL_OUTPUT_BYTES, MAX_TOTAL_PROMPT_BYTES
from .decision_configuration import DecisionConfiguration
from .errors import ProviderError
from .outcomes import ResourceBudgetTracker


@dataclass
class DecisionExecution:
    tracker: ResourceBudgetTracker
    settings: DecisionConfiguration
    total_call_limit: int
    calls: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    output_reserved: int = 0
    _output_bound_exceeded: bool = field(default=False, init=False)
    started_ms: int = 0
    total_input_limit: int = MAX_TOTAL_PROMPT_BYTES
    total_output_limit: int = MAX_TOTAL_OUTPUT_BYTES

    def __post_init__(self) -> None:
        self.started_ms = self.tracker.elapsed_ms()

    def remaining_seconds(self) -> float:
        return min(
            self.tracker.remaining_seconds(),
            max(
                0.0,
                (self.settings.timeout_ms - self.tracker.elapsed_ms() + self.started_ms)
                / 1000,
            ),
        )

    def execute(self, payload: str, send: Callable[[float, int], str]) -> str:
        if self._output_bound_exceeded:
            raise ProviderError("decision work budget exhausted")
        size = len(payload.encode("utf-8"))
        response_limit = min(
            8192,
            self.tracker.budget.max_output_bytes,
            self.settings.max_output_bytes - self.output_reserved,
            self.total_output_limit
            - self.tracker.response_bytes
            - (self.output_reserved - self.output_bytes),
        )
        if (
            self.tracker.admit_call(payload) is not None
            or self.calls >= self.settings.max_calls
            or self.tracker.provider_calls >= self.total_call_limit
            or self.input_bytes + size > self.settings.max_input_bytes
            or response_limit <= 0
            or self.remaining_seconds() <= 0
            or self.tracker.prompt_bytes + size > self.total_input_limit
            or self.tracker.transport_retries > 0
        ):
            raise ProviderError("decision work budget exhausted")
        # Retain the entire output allowance on an unknown attempt. No retries.
        self.calls += 1
        self.input_bytes += size
        self.output_reserved += response_limit
        self.tracker.record_call(payload)
        raw = send(self.remaining_seconds(), response_limit)
        if not isinstance(raw, str):
            raise ProviderError("decision response is invalid")
        actual = len(raw.encode("utf-8"))
        self.tracker.record_response(raw)
        self.output_bytes += actual
        if actual > response_limit:
            # Preserve observed bytes and all prior unknown reservations. A
            # contract-violating adapter cannot reuse an understated allowance.
            self._output_bound_exceeded = True
            raise ProviderError("decision response exceeded its work bound")
        if self.remaining_seconds() <= 0:
            raise ProviderError("decision response exceeded its work bound")
        self.output_reserved -= response_limit - actual
        return raw
