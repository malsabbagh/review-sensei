"""Finite attempted-work policy; none of these values promises completion.

The repository allowance is never enlarged. Operations can refuse on incomplete
history, including fallbacks and terminal pages. The previous worst-case cost
inventory remains historical evidence, not routine admission policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from review_sensei.errors import ReviewInputError

from .http import MAX_PAGINATION_PAGES
from .operation_comments import ensure_result_part_capacity

READ_OPERATION_REQUESTS = MAX_PAGINATION_PAGES
NATIVE_OPERATION_REQUESTS = MAX_PAGINATION_PAGES
WRITE_CONFIRMATION_REQUESTS = 2
WRITE_CONFIRMATION_TAIL = 1 + WRITE_CONFIRMATION_REQUESTS
# One enumeration attempt plus the protected write/confirmation tail.
MIN_OPERATION_REQUESTS = 1 + WRITE_CONFIRMATION_TAIL
# Selected cleanup: two complete read attempts and three native mutations.
# Each shares its finite operation allocation, never a global pagination maximum.
TERMINAL_REQUESTS = 2 * READ_OPERATION_REQUESTS + 3 * NATIVE_OPERATION_REQUESTS


@dataclass(frozen=True)
class OwnerAllocations:
    first_work: int
    first_terminal: int
    successor_work: int
    successor_terminal: int

    @property
    def first_total(self) -> int:
        return self.first_work + self.first_terminal

    @property
    def successor_total(self) -> int:
        return self.successor_work + self.successor_terminal


def owner_allocations(limit: int, startup: int) -> OwnerAllocations:
    if type(limit) is not int or type(startup) is not int or startup < 0:
        raise ReviewInputError("caller allocation inputs are invalid")
    remaining = limit - startup
    successor = remaining // 2
    first = remaining - successor
    if min(first, successor) < TERMINAL_REQUESTS + MIN_OPERATION_REQUESTS:
        raise ReviewInputError("initial and successor operation allocation is unfunded")
    return OwnerAllocations(
        first - TERMINAL_REQUESTS,
        TERMINAL_REQUESTS,
        successor - TERMINAL_REQUESTS,
        TERMINAL_REQUESTS,
    )


@dataclass(frozen=True)
class PublicationAttemptPlan:
    retained_parts: int
    selected_mutations: int
    capability_posts: int

    @property
    def minimum_requests(self) -> int:
        # One request and one possible owned echo readback per selected mutation.
        # Enumeration is attempted within the remaining allocation, not prepaid
        # at every possible history maximum. Inventory parts are planned by the
        # inventory store once its actual prepared payload is known.
        return (
            2 * (self.retained_parts + self.selected_mutations) + self.capability_posts
        )


def publication_attempt_plan(
    payload: bytes, *, inventory: bool, approve: bool
) -> PublicationAttemptPlan:
    parts = ensure_result_part_capacity(len(payload))
    # Root COMMENT, optional APPROVE, two check updates, and inventory intent.
    mutations = 1 + int(approve) + 2 + int(inventory)
    return PublicationAttemptPlan(parts, mutations, 2)
