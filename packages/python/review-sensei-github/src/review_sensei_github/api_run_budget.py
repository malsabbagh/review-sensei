"""Read/check facade over the single physical caller meter; no phase window."""

from __future__ import annotations

import time
from typing import Any

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError


class CallerRunBudget(EvidenceReadBudget):
    def __init__(self, caller: Any):
        # Deliberately do not construct an old60/64 metadata allowance/deadline.
        self.caller = caller
        self.restored = False
        self.clock = time.monotonic
        self._ordinary_hold = 0
        self._tail_ticket = None
        self._live_attempts = {}
        self._live_attempt_owners = {}
        self._failed_attempts = set()

    @property
    def calls(self) -> int:
        # Known ordinary run attempts survive checkpoint/owner restoration.
        # Startup and opaque abandoned reservations remain separate liabilities;
        # neither is falsely reported as a confirmed physical dispatch.
        state = self.caller.accounting
        return state.confirmed + state.uncertain

    @calls.setter
    def calls(self, value: int) -> None:
        raise ReviewInputError("caller charges are owned by the physical transport")

    @property
    def wall_deadline_ms(self) -> int:
        return int((self.caller._clock() + 60) * 1000)

    @wall_deadline_ms.setter
    def wall_deadline_ms(self, value: int) -> None:
        raise ReviewInputError("caller request deadlines cannot be replaced")

    def check(self) -> None:
        # Before the first durable record only the source-prefunded startup
        # slot can dispatch. This view never creates an ordinary phase window.
        if self.caller._state is None:
            if self.caller.startup_dispatches > len(self.caller._slot.requests):
                raise ReviewInputError("caller startup allocation is exhausted")
            return
        self.caller.accounting

    def _remaining_seconds(self) -> float:
        self.check()
        return 60.0

    def ordinary_remaining(self) -> int:
        if self.caller._state is None:
            return len(self.caller._slot.requests) - self.caller.startup_dispatches
        return self.caller.usable - self._ordinary_hold

    def consume(self, *, fence: bool = False) -> float:
        self.preflight(1)
        # Actual charging, pending intent and exact request deadline occur once
        # immediately before the physical opener, including raw native reads.
        return 60.0

    def preflight(self, calls: int) -> None:
        self.check()
        if type(calls) is not int or calls < 0 or calls > self.ordinary_remaining():
            raise EvidenceBudgetExhausted(
                "caller run cannot fund the complete operation"
            )

    def snapshot(self) -> dict[str, object]:
        raise ReviewInputError("caller accounting uses the authenticated run record")
