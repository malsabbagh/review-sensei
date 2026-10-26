"""One non-replenishable allocation inside an already reserved caller tranche."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from review_sensei.errors import ReviewInputError


@dataclass
class CallerOperation:
    capacity: int
    write_tail: int = 0
    key: str = field(default="", repr=False)
    used: int = 0
    write_attempted: bool = False
    confirmation_used: int = 0
    confirmed: bool = False

    @property
    def remaining(self) -> int:
        return self.capacity - self.used

    def consume(self, *, mutation: bool) -> None:
        if self.remaining <= 0:
            raise ReviewInputError("caller operation allocation exhausted")
        if mutation:
            if not self.write_tail or self.write_attempted:
                raise ReviewInputError("caller operation cannot repeat a mutation")
            if self.remaining < self.write_tail:
                raise ReviewInputError("caller mutation confirmation tail is unfunded")
            self.write_attempted = True
        elif self.write_attempted:
            if self.confirmation_used >= self.write_tail - 1:
                raise ReviewInputError("caller write confirmation allocation exhausted")
            self.confirmation_used += 1
        elif self.write_tail and self.remaining <= self.write_tail:
            raise ReviewInputError(
                "caller enumeration cannot preserve write confirmation"
            )
        self.used += 1


def caller_for_http(http: Any) -> Any:
    from .api_run_requests import ApiRunRequestTransport

    seen: set[int] = set()
    while http is not None and id(http) not in seen:
        seen.add(id(http))
        owner = getattr(getattr(http, "opener", None), "__self__", None)
        if isinstance(owner, ApiRunRequestTransport):
            return owner
        http = getattr(http, "transport", None)
    return None


def http_read_operation(
    http: Any, key: str, *, retained: CallerOperation | None = None
) -> Any:
    caller = caller_for_http(http)
    if caller is None or caller._state is None:
        return nullcontext()
    return caller.read_operation(
        hashlib.sha256(key.encode()).hexdigest(), retained=retained
    )


def http_write_operation(http: Any, key: str) -> Any:
    caller = caller_for_http(http)
    if caller is None or caller._state is None:
        return nullcontext()
    return caller.mutation_operation(key)
