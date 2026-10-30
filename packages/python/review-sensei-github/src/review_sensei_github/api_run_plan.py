"""Finite attempted-work policy; none of these values promises completion.

The repository allowance is never enlarged. Operations can refuse on incomplete
history, including fallbacks and terminal pages. The previous worst-case cost
inventory remains historical evidence, not routine admission policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from review_sensei.api_work import MAX_API_REQUEST_LIMIT
from review_sensei.bounded_evidence import MAX_PARTS
from review_sensei.errors import ReviewInputError

from .http import MAX_PAGINATION_PAGES
from .operation_comments import ensure_result_part_capacity

READ_OPERATION_REQUESTS = MAX_PAGINATION_PAGES
# Generic legacy checkpoints retain their complete bounded attempt, including
# fresh readback. The optimized quarantine path below has a different proof.
NATIVE_OPERATION_REQUESTS = READ_OPERATION_REQUESTS
# A quarantine checkpoint owns its census AND two exact-root fences, the head
# read, and the protected mutation/readback tail. It cannot borrow either
# owner's terminal allocation to finish a census it already started.
QUARANTINE_CHECKPOINT_REQUESTS = READ_OPERATION_REQUESTS + 6
# Ordinary checkpoints may stage one complete bounded evidence document. Each
# missing part needs a live-head fence and exact-echo POST. This is an upper
# bound inside the existing owner's tranche, not a new allowance. Terminal
# cleanup uses the smaller envelope because its evidence must already exist.
QUARANTINE_STAGING_REQUESTS = QUARANTINE_CHECKPOINT_REQUESTS + 2 * MAX_PARTS
WRITE_CONFIRMATION_REQUESTS = 2
WRITE_CONFIRMATION_TAIL = 1 + WRITE_CONFIRMATION_REQUESTS
# Attempt a complete owned review/prose readback and its final head fence.
# These finite attempts do not promise that every allowed history will fit.
COMMAND_UPDATE_CONFIRMATION_REQUESTS = 2 * READ_OPERATION_REQUESTS + 1
# Grouped native readback has three independent complete surfaces: review
# frames, immutable admission comments, and the native publication union.
# The inventory Bot/head authority and exact closing root/head add four GETs.
NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS = 3 * READ_OPERATION_REQUESTS + 4
COMMAND_RECEIPT_CONFIRMATION_REQUESTS = READ_OPERATION_REQUESTS + 2
# One enumeration attempt plus the protected write/confirmation tail.
MIN_OPERATION_REQUESTS = 1 + WRITE_CONFIRMATION_TAIL
# Selected cleanup: two complete read attempts and three native mutations.
# Each shares its finite operation allocation, never a global pagination maximum.
TERMINAL_REQUESTS = 2 * READ_OPERATION_REQUESTS + 3 * NATIVE_OPERATION_REQUESTS
EXPANDED_TERMINAL_REQUESTS = (
    2 * READ_OPERATION_REQUESTS + 3 * QUARANTINE_CHECKPOINT_REQUESTS
)


def terminal_requests(
    limit: int,
    *,
    startup: int = 0,
    successor: bool = False,
    legacy: bool = False,
) -> int:
    """Fund each owner's larger tail only from its own added allocation.

    The 512-request work tranche is preserved as limits increase, including
    odd limits/startup liabilities. Frozen v1 runs keep their original split
    for an unclaimed successor as well as every already reserved block.
    """
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_API_REQUEST_LIMIT
        or type(startup) is not int
        or not 0 <= startup <= MAX_API_REQUEST_LIMIT
        or type(successor) is not bool
        or type(legacy) is not bool
    ):
        raise ReviewInputError("caller allocation inputs are invalid")
    if legacy:
        return TERMINAL_REQUESTS

    def owner_total(allowance: int) -> int:
        remaining = allowance - startup
        return remaining // 2 if successor else remaining - remaining // 2

    added = max(0, owner_total(limit) - owner_total(512))
    return TERMINAL_REQUESTS + min(
        EXPANDED_TERMINAL_REQUESTS - TERMINAL_REQUESTS, added
    )


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


def owner_allocations(
    limit: int, startup: int, *, legacy: bool = False
) -> OwnerAllocations:
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_API_REQUEST_LIMIT
        or type(startup) is not int
        or not 0 <= startup <= MAX_API_REQUEST_LIMIT
    ):
        raise ReviewInputError("caller allocation inputs are invalid")
    remaining = limit - startup
    successor = remaining // 2
    first = remaining - successor
    first_terminal = terminal_requests(limit, startup=startup, legacy=legacy)
    successor_terminal = terminal_requests(
        limit, startup=startup, successor=True, legacy=legacy
    )
    if (
        first < first_terminal + MIN_OPERATION_REQUESTS
        or successor < successor_terminal + MIN_OPERATION_REQUESTS
    ):
        raise ReviewInputError("initial and successor operation allocation is unfunded")
    return OwnerAllocations(
        first - first_terminal,
        first_terminal,
        successor - successor_terminal,
        successor_terminal,
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
