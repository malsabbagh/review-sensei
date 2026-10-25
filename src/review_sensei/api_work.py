"""Caller-request accounting values, independent of transport authorization.

These transitions do not authenticate a serialized record or lock a writer.
The hosted adapter must authenticate the record and serialize every writer
before using it. A generation is conflict detection, never a comment-PATCH CAS.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, cast

from .errors import ReviewInputError

DEFAULT_API_REQUEST_LIMIT = 128  # Provisional until full public-path measurement.
ACCOUNTING_VERSION = "caller-requests-v1"
_HEX40 = re.compile(r"[a-f0-9]{40}")
_HEX64 = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _natural(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and value >= int(positive)


@dataclass(frozen=True)
class ApiRunIdentity:
    repository_id: int
    run_id: int

    def __post_init__(self) -> None:
        if not all(
            _natural(v, positive=True) for v in (self.repository_id, self.run_id)
        ):
            raise ReviewInputError("API accounting run identity is invalid")


@dataclass(frozen=True)
class ApiExecutionOwner:
    """An execution owner does not identify a new allowance."""

    run_attempt: int
    job: str
    generation: int

    def __post_init__(self) -> None:
        if (
            self.run_attempt not in (1, 2)
            or type(self.run_attempt) is not int
            or not isinstance(self.job, str)
            or _NAME.fullmatch(self.job) is None
            or not _natural(self.generation)
        ):
            raise ReviewInputError("unsupported API accounting execution owner")


@dataclass(frozen=True)
class FrozenApiConfiguration:
    """Provenance values; the adapter must establish trusted-base provenance."""

    commit: str
    digest: str
    limit: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.commit, str)
            or _HEX40.fullmatch(self.commit) is None
            or not isinstance(self.digest, str)
            or _HEX64.fullmatch(self.digest) is None
            or not _natural(self.limit, positive=True)
        ):
            raise ReviewInputError("frozen API configuration is invalid")


@dataclass(frozen=True)
class StartupSlot:
    """One source-controlled job/attempt envelope, including its durable tail.

    Every label denotes one caller HTTP request. Worker internal dispatches
    cannot appear here. The production adapter must supply measured envelopes;
    the accounting module deliberately invents neither a minimum nor a maximum.
    """

    job: str
    attempt: int
    requests: tuple[str, ...]

    def __post_init__(self) -> None:
        ApiExecutionOwner(self.attempt, self.job, 0)
        if (
            not isinstance(self.requests, tuple)
            or not self.requests
            or any(
                not isinstance(s, str) or _NAME.fullmatch(s) is None
                for s in self.requests
            )
        ):
            raise ReviewInputError("API startup envelope is invalid")


@dataclass(frozen=True)
class StartupPlan:
    slots: tuple[StartupSlot, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.slots, tuple)
            or not self.slots
            or any(not isinstance(s, StartupSlot) for s in self.slots)
        ):
            raise ReviewInputError("API startup plan is invalid")
        keys = {(s.job, s.attempt) for s in self.slots}
        jobs = {s.job for s in self.slots}
        if len(keys) != len(self.slots) or keys != {
            (j, a) for j in jobs for a in (1, 2)
        }:
            raise ReviewInputError(
                "API startup requires initial and one rerun slot per job"
            )

    @property
    def liability(self) -> int:
        return sum(len(s.requests) for s in self.slots)

    def require_limit(self, limit: int) -> None:
        if not _natural(limit, positive=True) or limit < self.liability:
            raise ReviewInputError(
                "api_request_limit cannot fund the measured startup plan"
            )


@dataclass(frozen=True)
class PendingApiRequest:
    request_id: str
    deadline_unix_ms: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or _NAME.fullmatch(self.request_id) is None
            or not _natural(self.deadline_unix_ms, positive=True)
        ):
            raise ReviewInputError("pending caller request is invalid")


@dataclass(frozen=True)
class CallerRequestBlock:
    block_id: str
    issued_by: ApiExecutionOwner
    undispatched: int
    pending: tuple[PendingApiRequest, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.block_id, str)
            or _NAME.fullmatch(self.block_id) is None
            or not isinstance(self.issued_by, ApiExecutionOwner)
            or not _natural(self.undispatched)
            or not isinstance(self.pending, tuple)
            or any(not isinstance(p, PendingApiRequest) for p in self.pending)
            or len({p.request_id for p in self.pending}) != len(self.pending)
        ):
            raise ReviewInputError("caller request block is invalid")


@dataclass(frozen=True)
class ApiRunAccounting:
    """Pure accounting state, not an authenticated authority or HTTP budget yet.

    Startup slots are permanently occupied, even if a process dies before its
    first record write. Ordinary blocks must be persisted and read back before
    dispatch. Their persistence requests come from a previously confirmed block.
    No method refunds unused, abandoned, timed-out or uncertain capacity.
    """

    identity: ApiRunIdentity
    configuration: FrozenApiConfiguration
    owner: ApiExecutionOwner
    startup: StartupPlan
    startup_used: tuple[tuple[str, int], ...]
    prior_nonstartup_liability: int = 0
    blocks: tuple[CallerRequestBlock, ...] = ()
    completed: tuple[PendingApiRequest, ...] = ()
    version: str = ACCOUNTING_VERSION

    def __post_init__(self) -> None:
        if (
            self.version != ACCOUNTING_VERSION
            or not isinstance(self.identity, ApiRunIdentity)
            or not isinstance(self.configuration, FrozenApiConfiguration)
            or not isinstance(self.owner, ApiExecutionOwner)
            or not isinstance(self.startup, StartupPlan)
            or not _natural(self.prior_nonstartup_liability)
            or not isinstance(self.blocks, tuple)
            or any(not isinstance(b, CallerRequestBlock) for b in self.blocks)
            or len({b.block_id for b in self.blocks}) != len(self.blocks)
            or any(b.issued_by.generation > self.owner.generation for b in self.blocks)
            or not isinstance(self.completed, tuple)
            or any(not isinstance(p, PendingApiRequest) for p in self.completed)
            or not isinstance(self.startup_used, tuple)
            or any(
                type(k) is not tuple
                or len(k) != 2
                or not isinstance(k[0], str)
                or type(k[1]) is not int
                for k in self.startup_used
            )
        ):
            raise ReviewInputError("caller accounting record is invalid or obsolete")
        keys = {(s.job, s.attempt) for s in self.startup.slots}
        if (
            len(set(self.startup_used)) != len(self.startup_used)
            or not set(self.startup_used) <= keys
            or (self.owner.job, self.owner.run_attempt) not in self.startup_used
        ):
            raise ReviewInputError("caller accounting startup ownership is invalid")
        request_ids = [p.request_id for b in self.blocks for p in b.pending] + [
            p.request_id for p in self.completed
        ]
        if len(set(request_ids)) != len(request_ids):
            raise ReviewInputError("caller accounting has reused request identities")
        self.startup.require_limit(self.configuration.limit)
        if self.remaining < 0:
            raise ReviewInputError("caller accounting exceeds the frozen run allowance")

    @property
    def reserved_undispatched(self) -> int:
        return sum(b.undispatched for b in self.blocks)

    @property
    def confirmed(self) -> int:
        return len(self.completed)

    @property
    def uncertain(self) -> int:
        return sum(len(b.pending) for b in self.blocks)

    @property
    def remaining(self) -> int:
        return (
            self.configuration.limit
            - self.startup.liability
            - self.prior_nonstartup_liability
            - self.confirmed
            - self.reserved_undispatched
            - self.uncertain
        )

    def reserve(self, block_id: str, requests: int) -> ApiRunAccounting:
        """Propose a record; its new block is unusable until authenticated readback.

        This is a value transition only. Calling it cannot authorize HTTP. The
        owning adapter must first dispatch its write/readback using old capacity.
        """
        if not _natural(requests, positive=True):
            raise ReviewInputError("caller reservation must contain requests")
        return replace(
            self,
            blocks=(*self.blocks, CallerRequestBlock(block_id, self.owner, requests)),
        )

    def dispatch(self, block_id: str, request: PendingApiRequest) -> ApiRunAccounting:
        """Move one already durable slot to pending before physical dispatch."""
        if not isinstance(request, PendingApiRequest):
            raise ReviewInputError("pending caller request is invalid")
        if any(request.request_id == p.request_id for p in self.completed) or any(
            request.request_id == p.request_id for b in self.blocks for p in b.pending
        ):
            raise ReviewInputError("caller request identity was already used")
        blocks = []
        found = False
        for block in self.blocks:
            if block.block_id == block_id:
                if block.issued_by != self.owner:
                    raise ReviewInputError(
                        "caller request block requires its issuing owner"
                    )
                if block.undispatched == 0:
                    raise ReviewInputError("caller request block is exhausted")
                block = replace(
                    block,
                    undispatched=block.undispatched - 1,
                    pending=(*block.pending, request),
                )
                found = True
            blocks.append(block)
        if not found:
            raise ReviewInputError("caller request block is absent")
        return replace(self, blocks=tuple(blocks))

    def confirm(self, request: PendingApiRequest) -> ApiRunAccounting:
        """A known response changes category; it never creates capacity."""
        if not isinstance(request, PendingApiRequest):
            raise ReviewInputError("caller response request binding is invalid")
        found = sum(p == request for b in self.blocks for p in b.pending)
        if found != 1:
            raise ReviewInputError("caller response has no unique pending request")
        return replace(
            self,
            completed=(*self.completed, request),
            blocks=tuple(
                replace(b, pending=tuple(p for p in b.pending if p != request))
                for b in self.blocks
            ),
        )

    def claim_startup(self, owner: ApiExecutionOwner) -> ApiRunAccounting:
        """Propose a serialized owner transition; cessation is an adapter guard.

        No timeout-based takeover exists here. The adapter must establish prior
        owner cessation and confirmed checkpoint ownership before this transition.
        A same-slot restart is always unsupported, including after a crash.
        """
        if not isinstance(owner, ApiExecutionOwner):
            raise ReviewInputError("unsupported API accounting execution owner")
        key = (owner.job, owner.run_attempt)
        if owner.run_attempt < self.owner.run_attempt:
            raise ReviewInputError(
                "caller accounting cannot return to an earlier attempt"
            )
        if key in self.startup_used or key not in {
            (s.job, s.attempt) for s in self.startup.slots
        }:
            raise ReviewInputError("startup slot is consumed or unsupported")
        if owner.generation != self.owner.generation + 1:
            raise ReviewInputError("caller accounting owner generation conflicts")
        return replace(self, owner=owner, startup_used=(*self.startup_used, key))

    def require_binding(
        self, identity: ApiRunIdentity, configuration: FrozenApiConfiguration
    ) -> None:
        """Reject foreign run/config values after the adapter authenticates them."""
        if self.identity != identity or self.configuration != configuration:
            raise ReviewInputError(
                "caller accounting run or frozen configuration conflicts"
            )

    def to_document(self) -> dict[str, object]:
        """Closed value serialization; this does not authenticate a checkpoint."""
        return cast(dict[str, object], json.loads(json.dumps(asdict(self))))

    def require_successor(self, candidate: ApiRunAccounting) -> None:
        """Value retention at a checked root; no replacement owner authority.

        Authenticating/serializing the actual writer remains the adapter's job.
        This check prevents a record builder from erasing prepaid uncertainty,
        resetting counters or moving a crashed owner's remainder into new work.
        """
        self.require_binding(candidate.identity, candidate.configuration)
        if (
            candidate.startup != self.startup
            or candidate.startup_used[: len(self.startup_used)] != self.startup_used
            or candidate.completed[: len(self.completed)] != self.completed
            or candidate.prior_nonstartup_liability < self.prior_nonstartup_liability
            or candidate.remaining > self.remaining
            or candidate.owner.generation < self.owner.generation
        ):
            raise ReviewInputError(
                "caller accounting cannot discard prior charges or history"
            )
        current = {b.block_id: b for b in candidate.blocks}
        for previous in self.blocks:
            block = current.get(previous.block_id)
            if (
                block is None
                or block.issued_by != previous.issued_by
                or block.issued_by != candidate.owner
                and block.undispatched != previous.undispatched
                or any(
                    p not in block.pending and p not in candidate.completed
                    for p in previous.pending
                )
            ):
                raise ReviewInputError(
                    "caller accounting cannot transfer or discard owner liability"
                )

    @classmethod
    def from_document(cls, value: object) -> ApiRunAccounting:
        from .schemas import validate_public_document

        validate_public_document(value, "api-run-accounting")
        document = cast(Mapping[str, Any], value)

        def block(item: dict[str, Any]) -> CallerRequestBlock:
            return CallerRequestBlock(
                item["block_id"],
                ApiExecutionOwner(**item["issued_by"]),
                item["undispatched"],
                tuple(PendingApiRequest(**p) for p in item["pending"]),
            )

        return cls(
            identity=ApiRunIdentity(**document["identity"]),
            configuration=FrozenApiConfiguration(**document["configuration"]),
            owner=ApiExecutionOwner(**document["owner"]),
            startup=StartupPlan(
                tuple(
                    StartupSlot(s["job"], s["attempt"], tuple(s["requests"]))
                    for s in document["startup"]["slots"]
                )
            ),
            startup_used=tuple((s[0], s[1]) for s in document["startup_used"]),
            prior_nonstartup_liability=document["prior_nonstartup_liability"],
            blocks=tuple(block(b) for b in document["blocks"]),
            completed=tuple(PendingApiRequest(**p) for p in document["completed"]),
            version=document["version"],
        )


def initial_accounting(
    identity: ApiRunIdentity,
    configuration: FrozenApiConfiguration,
    owner: ApiExecutionOwner,
    startup: StartupPlan,
    *,
    prior_nonstartup_liability_upper_bound: int | None,
) -> ApiRunAccounting:
    """Initial value after authoritative absence, never an absence authenticator.

    If discovery cannot establish a conservative prior non-startup charge,
    refuse. This bound excludes the startup slots already prefunded by the
    source-controlled plan. It is uncertainty, never a confirmed dispatch.
    The adapter must prove absence and serialization; a caller-supplied zero
    cannot. Both attempts' startup charges precede any ordinary reservation.
    """
    if prior_nonstartup_liability_upper_bound is None or not _natural(
        prior_nonstartup_liability_upper_bound
    ):
        raise ReviewInputError("prior caller consumption is unknown")
    if not isinstance(owner, ApiExecutionOwner) or not isinstance(startup, StartupPlan):
        raise ReviewInputError("initial caller execution owner is invalid")
    key = (owner.job, owner.run_attempt)
    if key not in {(s.job, s.attempt) for s in startup.slots} or owner.generation != 0:
        raise ReviewInputError("initial caller execution owner is invalid")
    unavailable = tuple(
        (s.job, s.attempt)
        for s in startup.slots
        if s.attempt < owner.run_attempt or (s.job, s.attempt) == key
    )
    return ApiRunAccounting(
        identity,
        configuration,
        owner,
        startup,
        unavailable,
        prior_nonstartup_liability_upper_bound,
    )
