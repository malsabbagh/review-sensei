"""Independent broker dispatch control and caller request accounting.

The trusted broker enforces the admitted ceiling before every internal request.
Receipts detect unsupported/changed accounting; they confer no source authority.
Internal dispatch receipts are observations, never caller-budget debits/refunds.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError

from .errors import GitHubBrokerClientError

# Internal dispatches the hosted command broker admits per capability exchange.
# This is a service dispatch bound, independent of the caller allowance.
COMMAND_EXCHANGE_DISPATCH_LIABILITY = 10
MAX_BROKER_EXCHANGE_DISPATCHES = 60


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
                type(value) is not int
                or not 1 <= value <= MAX_BROKER_EXCHANGE_DISPATCHES
                for value in (self.exchange_liability, self.verification_liability)
            )
        ):
            raise GitHubBrokerClientError("Broker control configuration is invalid")

    def admit(self, *, verification: bool) -> dict[str, object]:
        liability = (
            self.verification_liability if verification else self.exchange_liability
        )
        self.budget.check()
        # The HTTP client charges its one POST through before_request. Internal
        # Worker auth/history/token dispatches belong to a separate service pool.
        # Neither observed nor unused internal work changes caller capacity.
        if self.budget.ordinary_remaining() < 1:
            raise ReviewInputError("broker request exceeds caller budget")
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
