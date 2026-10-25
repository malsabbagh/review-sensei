"""Opt-in original-liability accounting for broker-internal physical dispatches.

The trusted broker enforces the admitted ceiling before every internal request.
Receipts detect unsupported/changed accounting; they confer no source authority.
Unused/uncertain admitted liability is never refunded or converted to a new run.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from review_sensei.bounded_evidence import MAX_PART_READS, EvidenceReadBudget
from review_sensei.errors import ReviewInputError

from .errors import GitHubBrokerClientError

# Internal dispatches the hosted command broker admits per capability exchange.
# The command finalization reserve derives from this value; a broker configured
# with a different liability reports it back so the reserve follows the broker.
COMMAND_EXCHANGE_DISPATCH_LIABILITY = 10


@dataclass(frozen=True)
class BrokerDispatchAccounting:
    budget: EvidenceReadBudget
    operation_id: str
    exchange_liability: int
    verification_liability: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.budget, EvidenceReadBudget)
            or not isinstance(self.operation_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.operation_id) is None
            or any(
                type(value) is not int or not 1 <= value <= MAX_PART_READS - 4
                for value in (self.exchange_liability, self.verification_liability)
            )
        ):
            raise GitHubBrokerClientError("Broker control configuration is invalid")

    def admit(self, *, verification: bool) -> dict[str, object]:
        liability = (
            self.verification_liability if verification else self.exchange_liability
        )
        self.budget.check()
        # Include the caller's HTTP request, charged by the same client's
        # before_request callback. Refuse an unfunded whole request atomically,
        # including any ordinary hold a later phase owns.
        # ``ordinary_remaining`` already subtracts the consumed calls, so this
        # comparison is exactly ``calls + liability + 1 <= ceiling - hold``: the
        # hold is the only term added to the older pre-hold admission test, and
        # nothing is counted twice.
        if liability + 1 > self.budget.ordinary_remaining():
            raise ReviewInputError("broker internal liability exceeds original budget")
        for _ in range(liability):
            self.budget.consume()
        return {
            "schema_version": "1.0",
            "operation_id": self.operation_id,
            "request_id": secrets.token_hex(32),
            "deadline_unix_ms": self.budget.wall_deadline_ms,
            "max_dispatches": liability,
        }

    def verify(self, receipt: object, request: dict[str, object]) -> None:
        maximum = request.get("max_dispatches")
        if (
            type(maximum) is not int
            or not isinstance(receipt, dict)
            or set(receipt) != set(request) | {"dispatched"}
            or any(
                type(receipt[key]) is not type(value) or receipt[key] != value
                for key, value in request.items()
            )
            or type(receipt["dispatched"]) is not int
            or not 0 <= receipt["dispatched"] <= maximum
        ):
            raise GitHubBrokerClientError(
                "Broker control receipt is missing or invalid"
            )
        self.budget.check()
