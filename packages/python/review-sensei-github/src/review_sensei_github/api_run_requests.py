"""Physical caller enforcement across authenticated Actions run lifecycles.

Startup slots are source-selected, prefunded, nonrefundable and one-use.
The Worker allowance remains independent. Ordinary capacity can be
enabled only by this adapter's actual native mutation and authenticated readback,
never by loading a caller-supplied accounting document into a transport.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from weakref import WeakKeyDictionary

from review_sensei.api_work import (
    LEGACY_ACCOUNTING_VERSION,
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    StartupPlan,
    initial_accounting,
)
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.finding_lifecycle_store import (
    _checkpoint_pricer,
    _install_checkpoint_pricer,
)
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_owner import claim_review_startup
from .caller_operation import CallerOperation
from .errors import GitHubHTTPError, GitHubPublicationTransientError
from .http import Opener
from .session_ledger import GitHubIssueCommentSessionLedger


@dataclass(frozen=True, eq=False)
class _PlainFindingPagePlan:
    caller: Any
    root: str
    record: SessionRecord
    record_bytes: bytes
    inventory_ids: tuple[int, ...]
    inventory_bytes: bytes
    page_size: int
    page_bounds: tuple[int, ...]
    page_requests: tuple[tuple[int, int], ...]
    maximum_items: int
    checkpoint_parts: tuple[int, ...]
    root_calls: int
    fences: int


# These capabilities are issued only after the complete source frontier passes
# the existing ownership/dependency checks. A caller attribute is not a grant.
_PLAIN_FINDING_PLANS: WeakKeyDictionary[Any, _PlainFindingPagePlan] = (
    WeakKeyDictionary()
)
_PLAIN_FINDING_ACTIVE: WeakKeyDictionary[Any, tuple[_PlainFindingPagePlan, bool]] = (
    WeakKeyDictionary()
)


_PLAIN_FINDING_ISSUED: WeakKeyDictionary[Any, tuple[Any, ...]] = WeakKeyDictionary()


def _plain_page_signature(plan):
    return (
        plan.caller,
        plan.root,
        id(plan.record),
        plan.record_bytes,
        plan.inventory_ids,
        plan.inventory_bytes,
        plan.page_size,
        plan.page_bounds,
        plan.page_requests,
        plan.maximum_items,
        plan.checkpoint_parts,
        plan.root_calls,
        plan.fences,
    )


def _require_plain_page_plan(plan):
    if type(plan) is not _PlainFindingPagePlan or _PLAIN_FINDING_ISSUED.get(
        plan
    ) != _plain_page_signature(plan):
        raise ReviewInputError("finding lifecycle page plan is not source-issued")


def json_copy(value: Any) -> Any:
    """Detach source-owned values from mutable caller references."""
    from review_sensei.bounded_evidence import canonical_bytes

    return json.loads(canonical_bytes(value))


class _RefuseCallerRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> Any:
        fp.close()
        raise ReviewInputError("caller redirects are refused")


class _AccountedResponse:
    def __init__(
        self,
        response: Any,
        confirmed: Callable[[], None],
        observed: Callable[[Any], None] | None = None,
    ):
        self._response, self._confirmed = response, confirmed
        self._observed = observed
        self._read = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def __enter__(self) -> _AccountedResponse:
        self._response.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._response.__exit__(*args)

    def read(self, size: int) -> Any:
        value = self._response.read(size)
        if not self._read:
            self._confirmed()
            if self._observed is not None:
                self._observed(value)
            self._read = True
        return value


@dataclass(frozen=True)
class AnalysisAbortBoundary:
    """Trusted process observation after admission, before operation entry."""

    run: ApiRunIdentity
    owner: ApiExecutionOwner
    transaction: ReviewTransaction


@dataclass(frozen=True)
class ActionEffectBoundary:
    """An in-process execution entry, issued after the durable entry marker."""

    intent_sha256: str
    owner: ApiExecutionOwner


@dataclass(frozen=True)
class ActionResultProof:
    """Validated small result; only the owning settlement method issues it."""

    boundary: ActionEffectBoundary
    manifest: dict[str, object]
    outcome: dict[str, object]


@dataclass(frozen=True)
class ActionTransportState:
    """Public read-only snapshot of transport ownership and in-memory work flags."""

    is_activated: bool
    root_sha256: str | None
    owner: ApiExecutionOwner | None
    has_unresolved_requests: bool
    has_effect_boundary: bool
    has_result_proof: bool
    has_live_started_accounting: bool

    @property
    def has_unresolved_or_started_work(self) -> bool:
        return (
            self.has_unresolved_requests
            or self.has_effect_boundary
            or self.has_result_proof
        )


@dataclass(frozen=True)
class CallerTransportContainmentSnapshot:
    """Read-only containment and physical-budget snapshot for ApiRunRequestTransport."""

    run: ApiRunIdentity
    job: str
    attempt: int
    logical_phase: str
    live_cutover_enabled: bool
    is_activated: bool
    accounting_version: str | None
    legacy_accounting_overlap_refused: bool
    startup_dispatches: int
    startup_liability: int
    configured_limit: int | None
    confirmed_requests: int
    uncertain_requests: int
    reserved_undispatched: int
    remaining_unallocated: int | None
    usable_requests: int
    unresolved_mutation_count: int
    terminal_claimed: bool
    has_action_effect_boundary: bool
    has_action_result_proof: bool
    conservation_holds: bool


@dataclass
class _ThreadResolutionCapture:
    """Private, one-invocation correlation; never imported from a ledger."""

    token: object
    scope: tuple[Any, ...]
    url: str
    mutation_bytes: bytes
    readback_bytes: bytes
    thread_id: str
    mutation: dict[str, Any] | None = None
    readback: dict[str, Any] | None = None
    credential: str | None = None
    validated: bool = False
    consumed: bool = False
    invalid: bool = False


class ApiRunRequestTransport:
    """One source-controlled startup slot, then one confirmed owned block.

    The constructor claims one local slot in the source-owned runner directory.
    The host must establish authoritative PR writer serialization; neither that
    local marker nor generation checks provide it. Injected openers must dispatch
    once without hidden retries. Standard urllib redirects are refused. There is
    no snapshot restore/direct activation; same-slot restarts remain unsupported.
    """

    def __init__(
        self,
        *,
        directory: Path,
        run: ApiRunIdentity,
        plan: StartupPlan,
        attempt: int,
        opener: Opener,
        job: str = "hosted",
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if job not in {"hosted", "local", "command"}:
            raise ReviewInputError("review startup job is unsupported")
        self.job = job
        slots = [s for s in plan.slots if s.job == job and s.attempt == attempt]
        if len(slots) != 1:
            raise ReviewInputError("startup transport slot is unsupported")
        claim_review_startup(directory, run, attempt, job)
        self.run, self.plan, self.attempt = run, plan, attempt
        self._slot, self._clock, self._monotonic = slots[0], clock, monotonic
        self._opener = (
            build_opener(_RefuseCallerRedirects()).open if opener is urlopen else opener
        )
        self.startup_dispatches = 0
        self.observations: list[dict[str, object]] = []
        self._state: ApiRunAccounting | None = None
        self._block: str | None = None
        self._root: str | None = None
        self._sequence = 0
        self._activation: tuple[SessionRecord, str] | None = None
        self._checkpoint: SessionRecord | None = None
        self._last_method: str | None = None
        self._activation_readback_attempted = False
        self._quarantine_census_api_url: str | None = None
        self._quarantine_checkpoint_snapshot: Any = None
        self._checkpoint_readback_attempted = False
        self._producer: str | None = None
        self._reservation_started = False
        self._abort_source: dict[str, Any] | None = None
        self._analysis_boundary: AnalysisAbortBoundary | None = None
        self._operation_entry_started = False
        self._provider_effect_unresolved = False
        self._abort_close_attempted = False
        self._reconciled_mutations: set[str] = set()
        self._effect_record: SessionRecord | None = None
        self._thread_resolution_capture: _ThreadResolutionCapture | None = None
        self._checkpoint_write_request_id: str | None = None
        self._activation_write_request_id: str | None = None
        self._terminal_block: str | None = None
        self._terminal_reason: str | None = None
        self._budget_view: Any = None
        self._logical_phase = "startup"
        self._authentication_endpoints: set[str] = set()
        self._control_write_candidates: dict[str, tuple[str, str]] = {}
        self._confirmed_control_writes: dict[str, tuple[str, str]] = {}
        self._admitting_action = False
        self._physical_mutation_ids: set[str] = set()
        self.native_walks: dict[str, int] = {}
        self._active_operation: CallerOperation | None = None
        self._staged_part_operation: CallerOperation | None = None
        self._checkpoint_operation: CallerOperation | None = None
        self._failed_operations: set[tuple[str | None, str]] = set()
        self._issued_operations: list[tuple[CallerOperation, str | None]] = []
        self._action_effect_boundary: ActionEffectBoundary | None = None
        self._action_result_proof: ActionResultProof | None = None
        self._action_result_fingerprint: str | None = None

    def label_phase(self, phase: str) -> None:
        if phase not in {
            "admission",
            "provider",
            "completion",
            "publication",
            "terminal",
        }:
            raise ReviewInputError("caller lifecycle phase is invalid")
        self._logical_phase = phase

    def containment_and_budget_snapshot(self) -> CallerTransportContainmentSnapshot:
        """Inspect live containment posture, legacy refusal, and physical budget conservation."""
        from review_sensei.writer_containment import (
            LIVE_CANONICAL_CUTOVER_ENABLED,
            evaluate_physical_budget_guarantees,
        )

        unresolved_mutations = sum(
            1
            for item in self.observations
            if item.get("mutation")
            and (
                item["outcome"] in {"unknown", "expired"}
                or item.get("uncertain_effect")
            )
            and item["request_id"] not in self._reconciled_mutations
        )
        state = self._state
        if state is None:
            return CallerTransportContainmentSnapshot(
                run=self.run,
                job=self.job,
                attempt=self.attempt,
                logical_phase=self._logical_phase,
                live_cutover_enabled=LIVE_CANONICAL_CUTOVER_ENABLED,
                is_activated=False,
                accounting_version=None,
                legacy_accounting_overlap_refused=False,
                startup_dispatches=self.startup_dispatches,
                startup_liability=self.plan.liability,
                configured_limit=None,
                confirmed_requests=0,
                uncertain_requests=0,
                reserved_undispatched=0,
                remaining_unallocated=None,
                usable_requests=0,
                unresolved_mutation_count=unresolved_mutations,
                terminal_claimed=self._terminal_reason is not None,
                has_action_effect_boundary=self._action_effect_boundary is not None,
                has_action_result_proof=self._action_result_proof is not None,
                # No active accounting exists to establish full conservation.
                conservation_holds=False,
            )
        guarantee = evaluate_physical_budget_guarantees(
            configured_limit=state.configuration.limit,
            startup_liability=state.startup.liability,
            prior_nonstartup_liability=state.prior_nonstartup_liability,
            confirmed_requests=state.confirmed,
            uncertain_requests=state.uncertain,
            reserved_undispatched=state.reserved_undispatched,
            remaining_unallocated=state.remaining,
        )
        return CallerTransportContainmentSnapshot(
            run=self.run,
            job=self.job,
            attempt=self.attempt,
            logical_phase=self._logical_phase,
            live_cutover_enabled=LIVE_CANONICAL_CUTOVER_ENABLED,
            is_activated=True,
            accounting_version=state.version,
            legacy_accounting_overlap_refused=state.version
            != LEGACY_ACCOUNTING_VERSION,
            startup_dispatches=self.startup_dispatches,
            startup_liability=state.startup.liability,
            configured_limit=state.configuration.limit,
            confirmed_requests=state.confirmed,
            uncertain_requests=state.uncertain,
            reserved_undispatched=state.reserved_undispatched,
            remaining_unallocated=state.remaining,
            usable_requests=self.usable,
            unresolved_mutation_count=unresolved_mutations,
            terminal_claimed=self._terminal_reason is not None,
            has_action_effect_boundary=self._action_effect_boundary is not None,
            has_action_result_proof=self._action_result_proof is not None,
            conservation_holds=guarantee.conservation_holds,
        )

    @property
    def budget_view(self) -> Any:
        if self._budget_view is None:
            from .api_run_budget import CallerRunBudget

            self._budget_view = CallerRunBudget(self)
        return self._budget_view

    def begin_terminal(self, reason: str) -> None:
        """One-way use of the original owner's already prepaid terminal work."""
        if self._terminal_reason is not None or self._terminal_block is None:
            raise ReviewInputError(
                "caller terminal completion is unavailable or already claimed"
            )
        block = next(
            (b for b in self.accounting.blocks if b.block_id == self._terminal_block),
            None,
        )
        if block is None or block.issued_by != self.accounting.owner:
            raise ReviewInputError("caller terminal reservation owner conflicts")
        self._terminal_reason = reason
        self._block = self._terminal_block
        self.label_phase("terminal")

    def bind_terminal_reservation(self, block_id: str) -> None:
        from .api_run_plan import terminal_requests

        block = next(
            (b for b in self.accounting.blocks if b.block_id == block_id), None
        )
        if (
            self._terminal_block is not None
            or block is None
            or block.issued_by != self.accounting.owner
            or block.undispatched
            != terminal_requests(
                self.accounting.configuration.limit,
                startup=self.accounting.startup.liability,
                successor=self.accounting.owner.run_attempt > 1,
                legacy=self.accounting.version == LEGACY_ACCOUNTING_VERSION,
            )
        ):
            raise ReviewInputError("caller terminal reservation conflicts")
        self._terminal_block = block_id
        self.label_phase("admission")

    @property
    def accounting(self) -> ApiRunAccounting:
        if self._state is None:
            raise ReviewInputError("ordinary caller capacity is not confirmed")
        return self._state

    @property
    def usable(self) -> int:
        if self._state is None:
            return 0
        return next(
            b.undispatched for b in self._state.blocks if b.block_id == self._block
        )

    @contextmanager
    def read_operation(
        self, key: str, *, retained: CallerOperation | None = None
    ) -> Iterator[CallerOperation | None]:
        # Nested pagination/freshness reads share the enclosing operation. A new
        # pagination wrapper cannot replenish its counter or confirmation tail.
        if self._state is None or self._active_operation is not None:
            if retained is not None and retained is not self._active_operation:
                raise ReviewInputError("caller confirmation operation conflicts")
            yield self._active_operation
            return
        from .api_run_plan import READ_OPERATION_REQUESTS

        with self._operation(
            key, READ_OPERATION_REQUESTS, retained=retained
        ) as operation:
            yield operation

    @contextmanager
    def _operation(
        self,
        key: str,
        capacity: int,
        *,
        write_tail: int = 0,
        retained: CallerOperation | None = None,
        prewrite_reads: int = 0,
    ) -> Iterator[CallerOperation]:
        if self._active_operation is not None:
            raise ReviewInputError("caller mutation cannot nest another operation")
        identity = (self._root, key)
        if retained is None and identity in self._failed_operations:
            raise ReviewInputError("caller failed operation cannot be replenished")
        if retained is not None and (
            retained.confirmed
            or not retained.write_attempted
            or not any(
                retained is issued and block == self._block
                for issued, block in self._issued_operations
            )
        ):
            raise ReviewInputError(
                "caller operation is foreign, settled or reconstructed"
            )
        operation = retained or CallerOperation(
            min(capacity, self.usable), write_tail, key
        )
        if retained is None:
            self._issued_operations.append((operation, self._block))
        if operation.remaining < ((write_tail + prewrite_reads) or 1):
            raise EvidenceBudgetExhausted("caller operation allocation is unfunded")
        self._active_operation = operation
        try:
            yield operation
        except BaseException:
            self._failed_operations.add(identity)
            raise
        finally:
            if operation.write_attempted and not operation.confirmed:
                self._failed_operations.add(identity)
            self._active_operation = None

    def mutation_operation(self, key: str) -> Any:
        from .api_run_plan import WRITE_CONFIRMATION_TAIL
        from .caller_operation import prewrite_fence_requests

        fences = prewrite_fence_requests(self)
        return self._operation(
            key,
            WRITE_CONFIRMATION_TAIL + fences,
            write_tail=WRITE_CONFIRMATION_TAIL,
            prewrite_reads=fences,
        )

    def _thread_resolution_scope(self) -> tuple[Any, ...] | None:
        record = self._effect_record
        if (
            self._state is None
            or self._activation is None
            or record is None
            or record.record_sha256 != self._root
            or record.transaction is None
            or self._checkpoint is not None
        ):
            return None
        transaction = record.transaction
        # Routine confirmed checkpoints may advance the current record hash.
        # The activated root, issuer, owner and transaction remain the subject.
        return (
            self,
            self.run,
            self.attempt,
            self.job,
            self.accounting.identity,
            self.accounting.owner,
            self._producer,
            self._activation[0].record_sha256,
            transaction.transaction_id,
            transaction.base_sha,
            transaction.head_sha,
            transaction.result_sha256,
            self._action_effect_boundary,
        )

    @contextmanager
    def thread_resolution_readback(
        self, http: Any, *, thread_id: str
    ) -> Iterator[Callable[[], None] | None]:
        """Correlate one real resolve dispatch and its existing exact readback.

        The yielded opaque callback conveys only this live resolver's successful
        validation. It is ineffective without the matching physical requests and
        authenticated response, and expires when this invocation exits.
        """
        from .caller_operation import caller_for_http
        from .thread_resolution import _MUTATION, _RESOLUTION_READBACK_QUERY

        scope = self._thread_resolution_scope()
        if scope is None:
            yield None
            return
        if (
            caller_for_http(http) is not self
            or not isinstance(thread_id, str)
            or not thread_id
            or self._thread_resolution_capture is not None
        ):
            raise ReviewInputError("thread resolution correlation is unavailable")
        capture = _ThreadResolutionCapture(
            token=object(),
            scope=scope,
            url=http.api_url + "/graphql",
            mutation_bytes=json.dumps(
                {
                    "operationName": "ResolveReviewThread",
                    "query": _MUTATION,
                    "variables": {"input": {"threadId": thread_id}},
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            readback_bytes=json.dumps(
                {
                    "operationName": "ResolutionReadback",
                    "query": _RESOLUTION_READBACK_QUERY,
                    "variables": {"id": thread_id},
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            thread_id=thread_id,
        )
        self._thread_resolution_capture = capture
        try:
            yield lambda: self._confirm_thread_resolution(capture.token)
        finally:
            self._thread_resolution_capture = None

    def _capture_thread_request(
        self, request: Request, observation: dict[str, Any]
    ) -> _ThreadResolutionCapture | None:
        capture = self._thread_resolution_capture
        if capture is None:
            return None
        if (
            capture.invalid
            or capture.consumed
            or self._thread_resolution_scope() != capture.scope
            or request.full_url != capture.url
            or request.get_method() != "POST"
            or observation["phase"] != "ordinary"
        ):
            capture.invalid = True
            return None
        credential = hashlib.sha256(
            str(request.get_header("Authorization")).encode("utf-8")
        ).hexdigest()
        if capture.mutation is None and request.data == capture.mutation_bytes:
            capture.mutation, capture.credential = observation, credential
            return None
        if (
            capture.mutation is not None
            and capture.readback is None
            and request.data == capture.readback_bytes
            and credential == capture.credential
        ):
            capture.readback = observation
            return capture
        capture.invalid = True
        return None

    def _observe_thread_readback(
        self, capture: _ThreadResolutionCapture, raw: Any, status: Any
    ) -> None:
        from .http import MAX_GITHUB_RESPONSE_BYTES, GitHubHttp

        if (
            capture is not self._thread_resolution_capture
            or capture.invalid
            or status != 200
            or not isinstance(raw, bytes)
            or len(raw) > MAX_GITHUB_RESPONSE_BYTES
        ):
            return
        try:
            value = GitHubHttp._parse_body(raw)
        except GitHubHTTPError:
            # The normal HTTP parser still raises its original typed failure.
            return
        data = value.get("data") if isinstance(value, dict) else None
        capture.validated = bool(
            isinstance(value, dict)
            and value.get("errors") in (None, [])
            and isinstance(data, dict)
            and data.get("node") == {"id": capture.thread_id, "isResolved": True}
        )

    def _confirm_thread_resolution(self, token: object) -> None:
        capture = self._thread_resolution_capture
        if (
            capture is None
            or token is not capture.token
            or capture.invalid
            or capture.consumed
            or not capture.validated
            or capture.scope != self._thread_resolution_scope()
            or capture.mutation is None
            or capture.readback is None
            or not any(capture.mutation is o for o in self.observations)
            or not any(capture.readback is o for o in self.observations)
            or capture.readback["outcome"] != "response"
            or capture.readback.get("uncertain_effect")
            or int(self._clock() * 1000) > capture.readback["deadline_unix_ms"]
        ):
            raise ReviewInputError("thread resolution readback is unconfirmed")
        # This settles an idempotent effect, not the lost HTTP response. Keep
        # its debit, unknown receipt, deadline and pending charge unchanged.
        self._reconciled_mutations.add(str(capture.mutation["request_id"]))
        capture.consumed = True

    @contextmanager
    def staged_part_operation(self, key: str) -> Iterator[CallerOperation]:
        """A bounded immutable write inside the existing root allocation.

        Every child request also spends the parent's enumeration capacity.
        Its write/echo proof cannot spend or replace the root's protected tail.
        Unknown child effects stop the root mutation and cannot be replayed.
        """
        from .api_run_plan import WRITE_CONFIRMATION_TAIL
        from .caller_operation import prewrite_fence_requests

        fences = prewrite_fence_requests(self)
        parent = self._active_operation
        identity = (self._root, "part:" + key)
        if (
            parent is None
            or parent.write_attempted
            or not parent.write_tail
            or self._staged_part_operation is not None
            or identity in self._failed_operations
        ):
            raise ReviewInputError("immutable part operation is unavailable")
        if min(parent.remaining, self.usable) < (
            parent.write_tail + fences + WRITE_CONFIRMATION_TAIL + fences
        ):
            raise EvidenceBudgetExhausted("immutable part and root tails are unfunded")
        operation = CallerOperation(
            WRITE_CONFIRMATION_TAIL + fences, WRITE_CONFIRMATION_TAIL, identity[1]
        )
        self._staged_part_operation = operation
        try:
            yield operation
        except BaseException:
            self._failed_operations.add(identity)
            raise
        finally:
            if operation.write_attempted and not operation.confirmed:
                self._failed_operations.add(identity)
            self._staged_part_operation = None

    def _register_authentication_endpoints(self, token_url: str) -> None:
        # Called by the configured BrokerClient, never inferred from a URL suffix.
        self._authentication_endpoints.update(
            (token_url, token_url.removesuffix("/token") + "/session-grant")
        )

    def _confirm_control_write(
        self,
        request_id: str | None,
        record: SessionRecord,
        kind: str,
        ledger: GitHubIssueCommentSessionLedger,
    ) -> None:
        candidate = self._control_write_candidates.get(request_id or "")
        if candidate is None or candidate[0] != record.record_sha256:
            return
        requested, configured = urlsplit(candidate[1]), urlsplit(ledger.http.api_url)
        if (requested.scheme, requested.netloc) != (
            configured.scheme,
            configured.netloc,
        ):
            return
        if requested.query or requested.fragment:
            return
        assert request_id is not None
        self._confirmed_control_writes[request_id] = (kind, record.record_sha256)

    def open(self, request: Request, *, timeout: float) -> _AccountedResponse:
        from .api_run_plan import WRITE_CONFIRMATION_TAIL

        if not isinstance(request, Request):
            raise ReviewInputError("caller request is invalid")
        from .quarantine_creation_census import before_creation_request

        before_creation_request(self, request)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ReviewInputError("caller request deadline is invalid")
        deadline = int((self._clock() + timeout) * 1000)
        request_deadline = self._monotonic() + timeout
        self._sequence += 1
        from .quarantine_census import is_readonly_census

        read_only_census = is_readonly_census(request, self._quarantine_census_api_url)
        is_mutation = (
            request.get_method() in {"POST", "PUT", "PATCH", "DELETE"}
            and not read_only_census
        )
        pending: PendingApiRequest | None = None
        if self._state is None:
            if self.startup_dispatches >= len(self._slot.requests):
                raise EvidenceBudgetExhausted("startup caller allowance is exhausted")
            if (
                is_mutation
                and self._activation is not None
                and len(self._slot.requests) - self.startup_dispatches
                < WRITE_CONFIRMATION_TAIL
            ):
                raise EvidenceBudgetExhausted(
                    "startup caller write/readback tail is unfunded"
                )
            self.startup_dispatches += 1
            phase = "startup"
            request_id = f"startup-{self.run.repository_id}-{self.run.run_id}-{self.attempt}-{self._sequence}"
        else:
            if self._active_operation is not None:
                mutation = is_mutation
                if self._staged_part_operation is not None:
                    self._staged_part_operation.consume(mutation=mutation)
                    # The root allocation is not replenished by a child write.
                    self._active_operation.consume(mutation=False)
                else:
                    self._active_operation.consume(mutation=mutation)
            if (
                request.get_method() == "PATCH"
                and self._checkpoint is not None
                and self.usable < WRITE_CONFIRMATION_TAIL
            ):
                raise EvidenceBudgetExhausted(
                    "caller checkpoint write/readback tail is unfunded"
                )
            path = urlsplit(request.full_url).path
            if (
                is_mutation
                and (path.startswith("/repos/") or path == "/graphql")
                and self.usable < WRITE_CONFIRMATION_TAIL
            ):
                # Protect finite confirmation room without promising that the
                # complete owned listing will fit. No internal Worker liability
                # is debited and no replacement allowance is created.
                raise EvidenceBudgetExhausted(
                    "caller business write/readback tail is unfunded"
                )
            phase = "ordinary"
            # Repository/run identity is already authenticated by this accounting
            # root. Preserve one exact request/deadline receipt per dispatch,
            # using its owner generation and physical sequence without repeating
            # that outer scope in every receipt. Existing IDs remain untouched.
            request_id = f"c-{self._state.owner.generation:x}-{self._sequence:x}"
            pending = PendingApiRequest(request_id, deadline)
            assert self._block is not None
            if self.usable <= 0:
                raise EvidenceBudgetExhausted("caller request allocation is exhausted")
            self._state = self._state.dispatch(self._block, pending)
        observation = {
            "phase": phase,
            "lifecycle_phase": self._logical_phase,
            "request_id": request_id,
            "deadline_unix_ms": deadline,
            "outcome": "unknown",
        }
        observation["mutation"] = is_mutation
        if is_mutation:
            self._physical_mutation_ids.add(request_id)
            if (
                request.get_method() == "POST"
                and request.full_url in self._authentication_endpoints
            ):
                self._confirmed_control_writes[request_id] = (
                    "broker_authentication",
                    str(self.run.run_id),
                )
            expected_root = (
                self._checkpoint
                if self._admitting_action
                else (
                    self._activation[0]
                    if self._state is None and self._activation is not None
                    else None
                )
            )
            if expected_root is not None and request.get_method() in {"PATCH", "POST"}:
                from .session_ledger import parse_session_comment

                try:
                    raw_body = request.data
                    if not isinstance(raw_body, (bytes, bytearray)):
                        raise ValueError("control write body is unavailable")
                    payload = json.loads(raw_body)
                    body = payload.get("body") if isinstance(payload, dict) else None
                    parsed = (
                        parse_session_comment(body, identity=expected_root.identity())
                        if isinstance(body, str)
                        else None
                    )
                    path = urlsplit(request.full_url).path
                    repository = expected_root.repository
                    target_matches = (
                        request.get_method() == "PATCH"
                        and path.startswith(f"/repos/{repository}/issues/comments/")
                        and path.rsplit("/", 1)[-1].isdigit()
                    ) or (
                        request.get_method() == "POST"
                        and path
                        == f"/repos/{repository}/issues/{expected_root.pull_request}/comments"
                    )
                    if (
                        target_matches
                        and parsed is not None
                        and parsed.record_sha256 == expected_root.record_sha256
                    ):
                        self._control_write_candidates[request_id] = (
                            expected_root.record_sha256,
                            request.full_url,
                        )
                except (ValueError, TypeError, ReviewInputError):
                    pass
        self.observations.append(observation)
        try:
            from .check_accounting import _dispatch

            _dispatch(self, request, observation)
        except Exception:
            # Missing supplementary evidence refuses accounting completion.
            try:
                from .review_lifecycle_diagnostics import check_proof_hook_failed

                check_proof_hook_failed("dispatch")
            except BaseException:
                pass
        if observation["mutation"]:
            if self._checkpoint is not None and request.get_method() == "PATCH":
                self._checkpoint_write_request_id = request_id
            elif self._state is None and self._activation is not None:
                self._activation_write_request_id = request_id
        self._last_method = request.get_method()

        observed_status = None
        resolution_readback = self._capture_thread_request(request, observation)

        def confirmed() -> None:
            if observation["outcome"] == "expired":
                raise EvidenceBudgetExhausted(
                    "caller response exceeded its original request deadline"
                )
            if observation["outcome"] != "unknown":
                return
            if pending is not None:
                assert self._state is not None
                self._state = self._state.confirm(pending)
            observation["outcome"] = "response"
            observation["uncertain_effect"] = bool(observation.get("mutation")) and (
                observed_status in {202, 409, 422, 429}
                or isinstance(observed_status, int)
                and observed_status >= 500
            )
            if (
                int(self._clock() * 1000) > deadline
                or self._monotonic() > request_deadline
            ):
                observation["outcome"] = "expired"
                raise EvidenceBudgetExhausted(
                    "caller response exceeded its original request deadline"
                )
            try:
                from .check_accounting import _response

                _response(self, observation, observed_status)
            except Exception:
                try:
                    from .review_lifecycle_diagnostics import check_proof_hook_failed

                    check_proof_hook_failed("response")
                except BaseException:
                    pass

        # The debit/intent above happens before the physical opener. Exceptions
        # preserve the charge; read/parse/status failures never refund it.
        try:
            response = self._opener(request, timeout=timeout)
        except HTTPError as error:
            observed_status = error.code
            confirmed()
            raise
        observed_status = getattr(response, "status", None)
        return _AccountedResponse(
            response,
            confirmed,
            None
            if resolution_readback is None
            else lambda raw: self._observe_thread_readback(
                resolution_readback, raw, observed_status
            ),
        )

    def _require_ledger(
        self, ledger: GitHubIssueCommentSessionLedger, identity: SessionIdentity
    ) -> None:
        if not isinstance(ledger, GitHubIssueCommentSessionLedger):
            raise ReviewInputError("caller transport requires the native ledger")
        from .publication_recovery import _publication_transport

        opener = _publication_transport(ledger.http).opener
        if (
            not ledger.app_slug
            or identity.repository_id != self.run.repository_id
            or getattr(opener, "__self__", None) is not self
            or getattr(opener, "__func__", None) is not ApiRunRequestTransport.open
            or ledger.evidence_budget is not None
            and ledger.evidence_budget is not self._budget_view
            or ledger._broker is not None
            or ledger._activation_ticket is not None
        ):
            raise ReviewInputError("caller transport is not the owning native ledger")

    def observe_action_transport_state(
        self, ledger: GitHubIssueCommentSessionLedger, identity: SessionIdentity
    ) -> ActionTransportState:
        """Validate owning ledger binding and return read-only action transport flags."""
        self._require_ledger(ledger, identity)
        live_started = False
        if self._state is not None:
            live_action = self._state.action
            live_started = self._state.uncertain > 0 or (
                live_action is not None and bool(live_action["execution_started"])
            )
        return ActionTransportState(
            is_activated=self._state is not None,
            root_sha256=self._root,
            owner=None if self._state is None else self._state.owner,
            has_unresolved_requests=self._action_unresolved(),
            has_effect_boundary=self._action_effect_boundary is not None,
            has_result_proof=self._action_result_proof is not None,
            has_live_started_accounting=live_started,
        )

    def _accept_activation(
        self, ledger: GitHubIssueCommentSessionLedger, record: SessionRecord
    ) -> ApiRunAccounting:
        if self._state is not None:
            raise ReviewInputError("caller reservation is already active")
        if self._activation is None:
            raise ReviewInputError("caller reservation has no owned intent")
        expected, block_id = self._activation
        if ledger.evidence_producer() != self._producer:
            raise ReviewInputError("caller reservation producer changed")
        if record.record_sha256 != expected.record_sha256:
            raise ReviewInputError("caller reservation publication is unconfirmed")
        state = ApiRunAccounting.from_document(record.api_run_accounting)
        state.require_writer_version()
        if (
            state.identity != self.run
            or state.startup != self.plan
            or state.owner.run_attempt != self.attempt
            or state.owner.job != self.job
        ):
            raise ReviewInputError("confirmed caller reservation scope conflicts")
        block = next((b for b in state.blocks if b.block_id == block_id), None)
        if block is None or block.issued_by != state.owner:
            raise ReviewInputError("confirmed caller reservation owner conflicts")
        self._has_quarantine_assessments = record.quarantine_lineage is not None
        self._state, self._block, self._root = state, block_id, record.record_sha256
        self._effect_record = record
        self._has_historical_assessments = record.historical_assessments is not None
        if self._activation_write_request_id is not None:
            self._reconciled_mutations.add(self._activation_write_request_id)
        self._confirm_control_write(
            self._activation_write_request_id, record, "owned_root_activation", ledger
        )
        return state

    def reserve_and_activate(
        self,
        ledger: GitHubIssueCommentSessionLedger,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        block_id: str,
    ) -> ApiRunAccounting:
        self._require_ledger(ledger, identity)
        if (
            self._state is not None
            or self._activation is not None
            or self._reservation_started
        ):
            raise ReviewInputError("caller reservation was already attempted")
        self._reservation_started = True
        self._producer = ledger.evidence_producer()
        # The cessation proof runs inside the mutation callback. Keep native
        # preactivation readback enabled so a root changed during that proof
        # cannot be overwritten by the successor's previously read candidate.
        ledger.evidence_budget = self.budget_view

        def prepare(record: SessionRecord) -> SessionRecord:
            if ledger.evidence_producer() != self._producer:
                raise ReviewInputError("caller reservation producer changed")
            previous = ApiRunAccounting.from_document(record.api_run_accounting)
            previous.require_writer_version()
            if previous.identity != self.run or previous.startup != self.plan:
                raise ReviewInputError("prior caller reservation scope conflicts")
            updated = mutate(record)
            candidate = ApiRunAccounting.from_document(updated.api_run_accounting)
            previous.require_successor(candidate)
            new = next((b for b in candidate.blocks if b.block_id == block_id), None)
            if (
                candidate.owner.job != self.job
                or candidate.owner.run_attempt != self.attempt
                or new is None
                or new.issued_by != candidate.owner
                or new.pending
                or new.undispatched < 1
            ):
                raise ReviewInputError("new caller reservation scope conflicts")
            if any(b.block_id == block_id for b in previous.blocks):
                raise ReviewInputError("caller reservation must be new")
            self._activation = updated, block_id
            return updated

        confirmed = ledger.replace(identity, prepare)
        return self._accept_activation(ledger, confirmed)

    def initialize_and_activate(
        self,
        ledger: GitHubIssueCommentSessionLedger,
        identity: SessionIdentity,
        configuration: FrozenApiConfiguration,
        owner: ApiExecutionOwner,
        *,
        trust_source: tuple[Any, Any] | None = None,
    ) -> ApiRunAccounting:
        """Confirm a first run record before enabling its ordinary tranche.

        The native initializer establishes complete absence and refuses an
        existing marker. The host must already have authenticated the frozen
        config, current Actions owner and authoritative writer serialization.
        This bounded first-run path does not roll over historical run records.
        Work and terminal reservations are finite source liabilities. One
        complete successor remains unallocated inside the original allowance.
        """
        self._require_ledger(ledger, identity)
        if self._reservation_started or self._state is not None:
            raise ReviewInputError("caller reservation was already attempted")
        if (
            owner.run_attempt != 1
            or owner.run_attempt != self.attempt
            or owner.job != self.job
        ):
            raise ReviewInputError("initial caller reservation owner conflicts")
        self._reservation_started = True

        def prepare(record: SessionRecord) -> SessionRecord:
            from .api_run_plan import owner_allocations

            state = initial_accounting(
                self.run,
                configuration,
                owner,
                self.plan,
                prior_nonstartup_liability_upper_bound=0,
            )
            allocation = owner_allocations(configuration.limit, self.plan.liability)
            state = state.reserve("hosted-review", allocation.first_work).reserve(
                "hosted-terminal", allocation.first_terminal
            )
            updated = record.evolve(api_run_accounting=state.to_document())
            if trust_source is not None:
                from .trust_enrollment import (
                    current_observation,
                    prepare_original_enrollment,
                )

                observed = current_observation(*trust_source)
                authority = observed["authority"]
                if (
                    authority["run_id"] != str(self.run.run_id)
                    or authority["run_attempt"] != owner.run_attempt
                    or authority["job"] != owner.job
                    or observed["app_id"] != ledger.authenticated_app_id
                    or ledger.token != trust_source[1].token
                    or ledger._reservation_owner
                    != {
                        "run_id": str(self.run.run_id),
                        "head_sha": authority["head_sha"],
                    }
                ):
                    raise ReviewInputError(
                        "trust enrollment original caller source conflicts"
                    )
                updated = prepare_original_enrollment(*trust_source, updated)
            self._activation = updated, "hosted-review"
            return updated

        confirmed = ledger.initialize_with_mutation(identity, prepare)
        # A genuinely missing ledger has no observed marker author yet. The
        # native initializer authenticates the configured Bot on exact readback.
        self._producer = ledger.evidence_producer()
        accepted = self._accept_activation(ledger, confirmed)
        self.bind_terminal_reservation("hosted-terminal")
        return accepted

    def rollover_and_activate(
        self, ledger, identity, configuration, owner, previous, cessation
    ):
        """New run only: archive exact terminal root before replacing accounting."""
        return self._rollover_and_activate(
            ledger, identity, configuration, owner, previous, cessation
        )

    def rollover_historical_and_activate(
        self, ledger, identity, configuration, owner, previous, authenticate
    ):
        """Distinct transition: authenticate within the serialized mutation."""
        return self._rollover_and_activate(
            ledger,
            identity,
            configuration,
            owner,
            previous,
            None,
            historical_authenticator=authenticate,
        )

    def rollover_unstarted_and_activate(
        self, ledger, identity, configuration, owner, previous, authenticate
    ):
        """New run only: settle and archive a proven unstarted action on source mutation.

        The predecessor record must already use the current writer accounting version
        (validated by ``_validate_rollover_prechecks``), and its settled
        ``canceled_unstarted`` receipt is authenticated via ``read_action_result``
        before archival under ``caller-run-root-v1``; pre-v2 ``historical_authenticator``
        publication enrollment does not apply. A successor run may carry a fresh
        ``FrozenApiConfiguration`` (for example, an updated workflow/model configuration
        on a subsequent dispatch) provided its own startup and work tranches are
        pre-validated before any mutation is attempted.
        """
        from .api_run_history import read_action_result
        from .api_run_plan import WRITE_CONFIRMATION_TAIL, owner_allocations
        from .unstarted_action_recovery import (
            require_witness_issued,
            settle_unstarted_record,
        )

        self._validate_rollover_prechecks(ledger, identity, owner, previous)
        if not isinstance(configuration, FrozenApiConfiguration):
            raise ReviewInputError("new caller run configuration is invalid")
        allocation = owner_allocations(configuration.limit, self.plan.liability)
        (
            initial_accounting(
                self.run,
                configuration,
                owner,
                self.plan,
                prior_nonstartup_liability_upper_bound=0,
            )
            .reserve("hosted-review", allocation.first_work)
            .reserve("hosted-terminal", allocation.first_terminal)
        )
        observed_producer = ledger.evidence_producer()
        prior_budget = ledger.evidence_budget
        unstarted_prepared = False

        def settle_prepare(record):
            nonlocal unstarted_prepared
            if unstarted_prepared:
                raise ReviewInputError(
                    "unstarted rollover callback is already consumed"
                )
            unstarted_prepared = True
            if record.record_sha256 != previous.record_sha256:
                raise ReviewInputError("previous caller root changed")
            if ledger.evidence_producer() != observed_producer:
                raise ReviewInputError("previous caller root producer changed")
            witness = require_witness_issued(authenticate(record))
            settled = settle_unstarted_record(
                identity=identity,
                record=record,
                producer=observed_producer,
                witness=witness,
            )
            read_action_result(ledger, identity, settled)
            self.budget_view.preflight(WRITE_CONFIRMATION_TAIL)
            return settled

        ledger.evidence_budget = self.budget_view
        try:
            settled_confirmed = ledger.replace(identity, settle_prepare)
            if any(
                o.get("mutation")
                and (o["outcome"] != "response" or o.get("uncertain_effect"))
                and str(o["request_id"]) not in self._reconciled_mutations
                for o in self.observations
            ):
                raise ReviewInputError(
                    "unstarted cancellation startup write is uncertain"
                )
        except BaseException:
            ledger.evidence_budget = prior_budget
            raise
        self._reservation_started = True
        self._producer = observed_producer
        return self._rollover_after_prechecks(
            ledger,
            identity,
            configuration,
            owner,
            settled_confirmed,
            lambda: None,
        )

    def _validate_rollover_prechecks(
        self, ledger, identity, owner, previous
    ) -> ApiRunAccounting:
        self._require_ledger(ledger, identity)
        if (
            self._state is not None
            or self._reservation_started
            or owner.run_attempt != 1
            or owner.job != self.job
            or owner.generation != 0
        ):
            raise ReviewInputError(
                "new caller run startup was already attempted or conflicts"
            )
        old = ApiRunAccounting.from_document(previous.api_run_accounting)
        old.require_writer_version()
        if (
            old.identity == self.run
            or old.identity.repository_id != self.run.repository_id
        ):
            raise ReviewInputError("new caller run identity conflicts")
        return old

    def _rollover_and_activate(
        self,
        ledger,
        identity,
        configuration,
        owner,
        previous,
        cessation,
        *,
        historical_authenticator=None,
    ):
        self._validate_rollover_prechecks(ledger, identity, owner, previous)
        self._reservation_started = True
        self._producer = ledger.evidence_producer()
        ledger.evidence_budget = self.budget_view
        return self._rollover_after_prechecks(
            ledger,
            identity,
            configuration,
            owner,
            previous,
            cessation,
            historical_authenticator=historical_authenticator,
        )

    def _rollover_after_prechecks(
        self,
        ledger,
        identity,
        configuration,
        owner,
        previous,
        cessation,
        *,
        historical_authenticator=None,
    ):
        from dataclasses import replace

        from .api_run_history import RETAINED_CONFIRMATION_TAIL, retain_previous_run
        from .api_run_plan import owner_allocations

        old = ApiRunAccounting.from_document(previous.api_run_accounting)
        old.require_writer_version()
        historical_prepared = False

        def prepare(record):
            nonlocal historical_prepared
            if historical_authenticator is not None:
                if historical_prepared:
                    raise ReviewInputError(
                        "historical rollover callback is already consumed"
                    )
                historical_prepared = True
            if record.record_sha256 != previous.record_sha256:
                raise ReviewInputError("previous caller root changed")
            if ledger.evidence_producer() != self._producer:
                raise ReviewInputError("previous caller root producer changed")
            completion = None
            if historical_authenticator is not None:
                from .historical_completion import HistoricalPublicationCompletion

                completion = historical_authenticator(record)
                if type(completion) is not HistoricalPublicationCompletion:
                    raise ReviewInputError(
                        "historical enrollment lacks typed completion"
                    )
                completion.require(
                    caller=self,
                    ledger=ledger,
                    identity=identity,
                    configuration=configuration,
                    owner=owner,
                    previous=record,
                )
            else:
                cessation()
            if (
                record.reservation_id is not None
                or record.reserved_slot is not None
                or (old.action is not None and old.action["phase"] != "terminal")
                or (
                    old.action is None
                    and old.analysis_abort is None
                    and (
                        record.transaction is None
                        or record.transaction.phase
                        not in {"publication_succeeded", "publication_suppressed"}
                        or (
                            completion is None
                            and old.terminal_transaction
                            != record.transaction.transaction_id
                        )
                    )
                )
                or (old.analysis_abort is not None and record.transaction is not None)
            ):
                phase = (
                    record.transaction.phase
                    if record.transaction is not None
                    else "no-transaction"
                )
                raise ReviewInputError(
                    "previous caller run is not terminal: "
                    f"run={old.identity.run_id} root={record.record_sha256} phase={phase}; "
                    "terminal accounting or action completion is unconfirmed; "
                    "installed recovery classifiers: literal-blocker-path-prepublication-v1, "
                    "body-limit-prepublication-v1, published-tail-reply-isolation-v1; "
                    "each requires its own authenticated source and activation"
                )
            if old.action is not None:
                from .api_run_history import read_action_result

                retained_action = read_action_result(ledger, identity, record)
                from .accepted_semantic_history import guard_successor_admission

                guard_successor_admission(
                    self, ledger, identity, record, retained_action
                )
                # Data only: this cannot authorize cancellation or dispatch.
                self._accepted_semantic_predecessor = retained_action["result"].get(
                    "accepted_semantic_history", {"missing": True}
                )
            else:
                from .accepted_semantic_history import history_from_record

                carried_history = history_from_record(ledger, identity, record)
                from .accepted_semantic_history import guard_carried_successor

                guard_carried_successor(self, ledger, identity, record, carried_history)
                self._accepted_semantic_predecessor = (
                    carried_history
                    if carried_history is not None
                    else None
                    if old.previous_record is None
                    else {"missing": True}
                )
            allocation = owner_allocations(configuration.limit, self.plan.liability)

            def candidate_state(reference):
                return (
                    replace(
                        initial_accounting(
                            self.run,
                            configuration,
                            owner,
                            self.plan,
                            prior_nonstartup_liability_upper_bound=0,
                        ),
                        previous_record=reference,
                    )
                    .reserve("hosted-review", allocation.first_work)
                    .reserve("hosted-terminal", allocation.first_terminal)
                )

            from .accepted_semantic_history import carry_history_in_rollover

            if completion is not None:
                from .api_run_history import preview_retained_reference

                preview = preview_retained_reference(
                    ledger, record, completion=completion
                )
                record.rollover_historical_api_accounting(
                    candidate_state(preview).to_document(),
                    assessments=completion.assessments,
                )

            # Qualify metadata capacity before any archive write; no preview
            # archive identity is installed or treated as authority.
            preview_candidate = carry_history_in_rollover(
                ledger,
                identity,
                record,
                record,
                getattr(self, "_accepted_semantic_predecessor", None),
            )
            carried_reference = preview_candidate.finding_lifecycle
            before_archive = len(self.observations)
            if completion is not None:
                # Existing complete archive/write/readback tail plus fresh old
                # and current Actions inventories and current PR after archive.
                self.budget_view.preflight(RETAINED_CONFIRMATION_TAIL + 5)
            reference = retain_previous_run(
                ledger, identity, record, completion=completion
            )
            if completion is not None:
                from .review_completion_recovery import _get

                completion.selection.require_current_owner()
                completion.selection.require_current_pull(
                    _get(
                        ledger.http,
                        ledger.http.repository_path(
                            identity.repository, f"/pulls/{identity.pull_request}"
                        ),
                        ledger.token,
                    )
                )
            archive_mutations = [
                o for o in self.observations[before_archive:] if o.get("mutation")
            ]
            if len(archive_mutations) == 1:
                self._confirmed_control_writes[
                    str(archive_mutations[0]["request_id"])
                ] = ("owned_predecessor_archive", record.record_sha256)
            # The issuer authenticated the exact immutable part readback.
            # Charge the lost response permanently; only its effect is settled.
            self._reconciled_mutations.update(
                str(o["request_id"])
                for o in self.observations[before_archive:]
                if o.get("mutation")
                and (o["outcome"] == "unknown" or o.get("uncertain_effect"))
            )
            state = candidate_state(reference)
            updated = (
                record.rollover_historical_api_accounting(
                    state.to_document(), assessments=completion.assessments
                )
                if completion is not None
                else record.rollover_api_accounting(state.to_document())
            )
            if carried_reference != updated.finding_lifecycle:
                updated = updated.evolve(
                    now=datetime.fromisoformat(
                        updated.updated_at.replace("Z", "+00:00")
                    ),
                    generation=updated.generation,
                    finding_lifecycle=carried_reference,
                )
            self._activation = updated, "hosted-review"
            return updated

        if previous.quarantine_lineage is not None:
            from .quarantine_rollover import replace_quarantine_rollover

            confirmed = replace_quarantine_rollover(
                self, ledger, identity, previous, prepare
            )
        else:
            confirmed = ledger.replace(identity, prepare)
        result = self._accept_activation(ledger, confirmed)
        self.bind_terminal_reservation("hosted-terminal")
        return result

    def reconcile_activation(
        self, ledger: GitHubIssueCommentSessionLedger, identity: SessionIdentity
    ) -> ApiRunAccounting:
        self._require_ledger(ledger, identity)
        if (
            self._state is not None
            or self._activation is None
            or self._last_method not in {"POST", "PATCH"}
            or self._activation_readback_attempted
        ):
            raise ReviewInputError("caller reservation has no funded reconciliation")
        self._activation_readback_attempted = True
        loaded = ledger.load(identity)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("caller reservation publication is unconfirmed")
        if self._producer is None:
            expected, _ = self._activation
            if (
                self._last_method != "GET"
                or loaded.record.record_sha256 != expected.record_sha256
            ):
                raise ReviewInputError("caller initial publication is unconfirmed")
            # The native exact readback authenticates the configured Bot before
            # its numeric producer identity can first be observed. No absence
            # or caller document can authorize another POST.
            self._producer = ledger.evidence_producer()
        result = self._accept_activation(ledger, loaded.record)
        if self._activation_write_request_id is not None:
            self._reconciled_mutations.add(self._activation_write_request_id)
        return result

    def close_analysis_abort(self, ledger, identity, confirmed):
        from dataclasses import replace

        source = self._abort_source
        if source is None or self._abort_close_attempted:
            return confirmed
        self._abort_close_attempted = True
        boundary = self._analysis_boundary
        if (
            boundary is None
            or self._operation_entry_started
            or boundary.run != self.accounting.identity
            or boundary.owner != self.accounting.owner
            or boundary.transaction.to_dict() != source["transaction"]
            or self._provider_effect_unresolved
            or self._checkpoint is not None
            or self._root != source["cleanup_root"]
            or confirmed.record_sha256 != self._root
            or any(
                (o["outcome"] in {"unknown", "expired"} or o.get("uncertain_effect"))
                and o.get("mutation")
                and o["request_id"] not in self._reconciled_mutations
                for o in self.observations
            )
        ):
            raise ReviewInputError("analysis abort cleanup is not confirmed")

        def close(record):
            if (
                record.record_sha256 != source["cleanup_root"]
                or record.reservation_id is not None
                or record.transaction is not None
            ):
                raise ReviewInputError("analysis abort cleanup root changed")
            self._state = replace(self.accounting, analysis_abort=source)
            return record.evolve(generation=record.generation + 1)

        return ledger.replace(identity, close)

    def _action_unresolved(self) -> bool:
        return (
            self._provider_effect_unresolved
            or self._checkpoint is not None
            or any(
                (
                    item["outcome"] in {"unknown", "expired"}
                    or item.get("uncertain_effect")
                )
                and item.get("mutation")
                and item["request_id"] not in self._reconciled_mutations
                for item in self.observations
            )
        )

    def _action_checkpoint(self, ledger, identity, mutate):
        from .errors import GitHubPublicationTransientError

        try:
            return self.replace_checkpoint(ledger, identity, mutate)
        except GitHubPublicationTransientError:
            # The original protected readback tail is the only recovery here.
            # A missing or changed exact echo never authorizes another PATCH.
            if self._checkpoint is None:
                raise
            return self.reconcile_checkpoint(ledger, identity)

    def admit_action(
        self, ledger, identity, *, purpose, source, head_sha, operation_key
    ) -> dict[str, Any]:
        """Confirm durable source-owned action intent before any business effect.

        The adapter must authenticate the current-purpose grant and exact live
        source first. These arguments are never a public document authority.
        A same-run successor may retain an unstarted identical intent; a started
        action requires exact operation-result recovery, never ordinary replay.
        """
        from review_sensei.bounded_evidence import canonical_bytes

        self._require_ledger(ledger, identity)
        if self._action_unresolved():
            raise ReviewInputError("caller action admission has unresolved work")
        intent = {
            "purpose": purpose,
            "source": source,
            "run_id": self.run.run_id,
            "owner": asdict(self.accounting.owner),
            "head_sha": head_sha,
            "configuration_digest": self.accounting.configuration.digest,
            "operation_key": operation_key,
            "source_root": self._root,
        }
        existing = self.accounting.action
        if existing is not None:
            original = existing["intent"]
            if any(
                original[key] != intent[key]
                for key in (
                    "purpose",
                    "source",
                    "run_id",
                    "head_sha",
                    "configuration_digest",
                    "operation_key",
                )
            ):
                raise ReviewInputError("caller action source or operation conflicts")
            if existing["phase"] != "pending" or existing["execution_started"]:
                raise ReviewInputError(
                    "caller action is complete or requires exact result recovery"
                )
            # Fresh native discovery authenticates the actual current owner and
            # retains the original antecedent root across accounting-only writes.
            loaded = ledger.load(identity)
            if (
                loaded.status != "ok"
                or loaded.record is None
                or loaded.record.record_sha256 != self._root
                or ApiRunAccounting.from_document(
                    loaded.record.api_run_accounting
                ).action
                != existing
            ):
                raise ReviewInputError("caller action admission root changed")
            return json_copy(original)
        action = {
            "intent": intent,
            "phase": "pending",
            "execution_started": False,
            "result": None,
            "outcome": None,
        }
        candidate = replace(self.accounting, action=action)

        def prepare(record):
            if record.reservation_id is not None or record.reserved_slot is not None:
                raise ReviewInputError(
                    "caller action conflicts with a review reservation"
                )
            if record.transaction is not None and record.transaction.phase not in {
                "publication_succeeded",
                "publication_suppressed",
            }:
                raise ReviewInputError(
                    "caller action conflicts with an unfinished review"
                )
            # Validate pure admission before changing any live accounting value.
            canonical_bytes(candidate.to_document())
            self._state = replace(self.accounting, action=action)
            return record.evolve(generation=record.generation + 1)

        self._admitting_action = True
        try:
            confirmed = self._action_checkpoint(ledger, identity, prepare)
        finally:
            self._admitting_action = False
        retained = ApiRunAccounting.from_document(confirmed.api_run_accounting).action
        if retained is None or retained["intent"] != intent:
            raise ReviewInputError("caller action intent publication is unconfirmed")
        return json_copy(intent)

    def begin_action_effect(self, ledger, identity, intent) -> ActionEffectBoundary:
        """Durably mark entry before executing the selected command or reply."""
        from review_sensei.bounded_evidence import canonical_bytes

        action = self.accounting.action
        if (
            action is None
            or action["intent"] != intent
            or action["phase"] != "pending"
            or action["execution_started"]
            or self._action_effect_boundary is not None
            or self._action_unresolved()
        ):
            raise ReviewInputError(
                "caller action execution is unavailable or may have started"
            )

        from .accepted_semantic_history import preflight_history_capacity

        preflight_history_capacity(self, identity, intent)

        def prepare(record):
            self._state = replace(
                self.accounting, action={**action, "execution_started": True}
            )
            return record.evolve(generation=record.generation + 1)

        self._action_checkpoint(ledger, identity, prepare)
        boundary = ActionEffectBoundary(
            hashlib.sha256(canonical_bytes(intent)).hexdigest(), self.accounting.owner
        )
        self._action_effect_boundary = boundary
        return boundary

    def settle_action_effect(
        self,
        ledger,
        identity,
        boundary: ActionEffectBoundary,
        *,
        outcome,
        result_document,
    ) -> ActionResultProof:
        """Validate a completed in-process result, never infer it from absence.

        Call only after all selected business/journal/publication obligations
        have confirmed success. Provider errors or unknown outcomes must escape
        the adapter without calling this method. A predecessor's boundary is
        deliberately not reconstructible from its serialized intent.
        """
        from review_sensei.bounded_evidence import (
            inline_manifest,
            read_inline_evidence,
        )

        from .api_run_history import action_result_binding, decode_action_result_carrier

        self._require_ledger(ledger, identity)
        action = self.accounting.action
        if (
            not isinstance(boundary, ActionEffectBoundary)
            or boundary is not self._action_effect_boundary
            or boundary.owner != self.accounting.owner
            or action is None
            or action["phase"] != "pending"
            or not action["execution_started"]
            or self._action_unresolved()
            or not isinstance(result_document, dict)
        ):
            raise ReviewInputError(
                "caller action result is not an owned confirmed outcome"
            )
        from .accepted_semantic_history import enrich_settlement

        result_document = enrich_settlement(
            self, ledger, identity, action["intent"], result_document
        )
        envelope = {
            "schema_version": "caller-action-result-v1",
            "intent_sha256": boundary.intent_sha256,
            "outcome": outcome,
            "result": json_copy(result_document),
        }
        from review_sensei.action_result_codec import encode_action_result_carrier

        history = result_document.get("accepted_semantic_history")
        self._action_result_history = (
            json_copy(history) if history is not None else None
        )
        carrier = encode_action_result_carrier(
            envelope,
            reference_history=(
                self._effect_record is not None
                and self._effect_record.finding_lifecycle is not None
            ),
        )
        manifest = inline_manifest(
            carrier,
            binding=action_result_binding(
                identity, self.accounting, ledger.evidence_producer()
            ),
            item_count=1,
        )
        if manifest is None:
            raise ReviewInputError(
                "caller action result exceeds its inline retention bound"
            )
        if (
            decode_action_result_carrier(
                read_inline_evidence(
                    manifest,
                    expected_binding=cast(Mapping[str, object], manifest["binding"]),
                ),
                history=history,
            )
            != envelope
        ):
            raise ReviewInputError("caller action result codec differs")
        # The accounting constructor validates the closed outcome as well as
        # the evidence manifest before any terminal mutation is prepared.
        replace(
            self.accounting,
            action={
                **action,
                "phase": "terminal",
                "result": manifest,
                "outcome": outcome,
            },
        )
        proof = ActionResultProof(boundary, json_copy(manifest), json_copy(outcome))
        self._action_result_proof = proof
        from review_sensei.bounded_evidence import canonical_bytes

        self._action_result_fingerprint = hashlib.sha256(
            canonical_bytes({"manifest": proof.manifest, "outcome": proof.outcome})
        ).hexdigest()
        return proof

    def close_action(self, ledger, identity, proof: ActionResultProof):
        """Persist one immutable terminal receipt for the admitted action."""
        from review_sensei.bounded_evidence import canonical_bytes

        from .api_run_history import read_action_result

        self._require_ledger(ledger, identity)
        if (
            not isinstance(proof, ActionResultProof)
            or proof is not self._action_result_proof
            or self._action_unresolved()
            or hashlib.sha256(
                canonical_bytes({"manifest": proof.manifest, "outcome": proof.outcome})
            ).hexdigest()
            != self._action_result_fingerprint
        ):
            raise ReviewInputError("caller action has no settled result proof")
        action = self.accounting.action
        if action is None or proof.boundary is not self._action_effect_boundary:
            raise ReviewInputError("caller action completion binding conflicts")
        if action["phase"] == "terminal":
            if action["result"] != proof.manifest or action["outcome"] != proof.outcome:
                raise ReviewInputError("caller action terminal receipt changed")
            loaded = ledger.load(identity)
            if (
                loaded.status != "ok"
                or loaded.record is None
                or loaded.record.record_sha256 != self._root
            ):
                raise ReviewInputError("caller action terminal root changed")
            read_action_result(ledger, identity, loaded.record)
            return loaded.record
        if self._terminal_reason is None:
            self.begin_terminal("action-completion")

        def prepare(record):
            self._state = replace(
                self.accounting,
                action={
                    **action,
                    "phase": "terminal",
                    "result": proof.manifest,
                    "outcome": proof.outcome,
                },
            )
            from review_sensei.finding_lifecycle_store import (
                reference_with_terminal_action_history,
            )

            reference = reference_with_terminal_action_history(
                record,
                accounting=self._state,
                candidate_history=getattr(self, "_action_result_history", None),
            )
            return record.evolve(
                generation=record.generation + 1, finding_lifecycle=reference
            )

        confirmed = self._action_checkpoint(ledger, identity, prepare)
        read_action_result(ledger, identity, confirmed)
        return confirmed

    def cancel_unstarted_action(self, ledger, identity, witness):
        """Persist one bounded terminal cancellation for a proven unstarted action."""
        from .api_run_history import read_action_result
        from .unstarted_action_recovery import (
            SAME_RUN_AUTHORITY_MODES,
            _validate_unstarted_record,
            require_witness_issued,
            settle_unstarted_accounting,
        )

        self._require_ledger(ledger, identity)
        verified = require_witness_issued(witness)
        if (
            self._action_unresolved()
            or self._action_effect_boundary is not None
            or self._action_result_proof is not None
        ):
            raise ReviewInputError(
                "caller transport has started or unresolved work; cannot cancel as unstarted"
            )
        producer = ledger.evidence_producer()
        if (
            verified.identity != identity
            or verified.target_run_id != self.run.run_id
            or verified.producer != producer
            or verified.authority_mode not in SAME_RUN_AUTHORITY_MODES
        ):
            raise ReviewInputError(
                "unstarted cancellation witness scope conflicts with active caller"
            )
        action = self.accounting.action
        if action is None:
            raise ReviewInputError("caller action intent is unavailable")
        if action["intent"] != verified.intent:
            raise ReviewInputError(
                "caller action intent does not match cancellation witness"
            )
        if action["execution_started"]:
            raise ReviewInputError("started action cannot be canceled as unstarted")
        expected_outcome = {"kind": "canceled_unstarted", "reason": verified.reason}
        if action["phase"] == "terminal":
            if action["outcome"] != expected_outcome:
                raise ReviewInputError("caller action terminal receipt changed")
            loaded = ledger.load(identity)
            if (
                loaded.status != "ok"
                or loaded.record is None
                or loaded.record.record_sha256 != self._root
            ):
                raise ReviewInputError("caller action terminal root changed")
            decoded = read_action_result(ledger, identity, loaded.record)
            result_doc = decoded.get("result")
            if (
                decoded.get("intent_sha256") != verified.intent_sha256
                or not isinstance(result_doc, Mapping)
                or result_doc.get("superseded_from_root_sha256")
                != verified.target_root_sha256
                or result_doc.get("authority_proof_sha256")
                != verified.authority_proof_sha256
            ):
                raise ReviewInputError(
                    "caller action terminal receipt does not match witness target root"
                )
            return loaded.record
        if self._root != verified.target_root_sha256:
            raise ReviewInputError("unstarted cancellation root changed")
        settled_state = settle_unstarted_accounting(
            identity=identity,
            state=self.accounting,
            producer=producer,
            witness=verified,
        )
        if self._terminal_reason is None:
            self.begin_terminal("action-completion")

        def prepare(record):
            if record.record_sha256 != verified.target_root_sha256:
                raise ReviewInputError("unstarted cancellation root changed")
            if (
                self._action_unresolved()
                or self._action_effect_boundary is not None
                or self._action_result_proof is not None
                or self.accounting.uncertain > 0
                or self.accounting.action is None
                or self.accounting.action["execution_started"]
            ):
                raise ReviewInputError(
                    "caller transport has started or unresolved work; cannot cancel as unstarted"
                )
            if record.publication_staging is not None or (
                record.assessment_queue is not None
                and record.assessment_queue.get("active_operation") is not None
            ):
                raise ReviewInputError(
                    "durable pending receipt blocks unstarted cancellation"
                )
            _validate_unstarted_record(
                identity=identity,
                record=record,
                expected_intent=verified.intent,
            )
            self._state = settled_state
            return record.evolve(generation=record.generation + 1)

        confirmed = self._action_checkpoint(ledger, identity, prepare)
        read_action_result(ledger, identity, confirmed)
        return confirmed

    def finish_review_run(self, ledger, identity, transaction):
        """Confirm one close marker after normal publication and its full tail.

        This does not complete the review transaction a second time. It binds
        the finished run to that already terminal transaction before rollover.
        Unknown HTTP charges remain; unresolved mutations cannot close a run.
        """
        from dataclasses import replace

        if (
            self._terminal_reason not in {"publication-completion", "already-published"}
            or self._checkpoint is not None
            or self.accounting.terminal_transaction is not None
            or any(
                (o["outcome"] in {"unknown", "expired"} or o.get("uncertain_effect"))
                and o.get("mutation")
                and o["request_id"] not in self._reconciled_mutations
                for o in self.observations
            )
        ):
            raise ReviewInputError("caller run has unresolved terminal work")

        def close(record):
            if (
                self._terminal_reason == "already-published"
                and self.accounting.owner.run_attempt == 2
            ):
                raise ReviewInputError(
                    "successor publication tail is unverified; authenticated predecessor completion is required"
                )
            current = record.transaction
            if (
                current is None
                or current.transaction_id != transaction.transaction_id
                or current.phase
                not in {"publication_succeeded", "publication_suppressed"}
                or current.result_sha256 != transaction.result_sha256
                or record.reservation_id is not None
            ):
                raise ReviewInputError("caller run terminal transaction conflicts")
            self._state = replace(
                self.accounting, terminal_transaction=current.transaction_id
            )
            return record.evolve(generation=record.generation + 1)

        return ledger.replace(identity, close)

    def replace_checkpoint(
        self,
        ledger: GitHubIssueCommentSessionLedger,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord] | None = None,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        self._require_ledger(ledger, identity)
        if self._checkpoint is not None:
            raise ReviewInputError("caller checkpoint has unresolved publication")
        self.accounting
        from .api_run_plan import (
            NATIVE_OPERATION_REQUESTS,
            QUARANTINE_CHECKPOINT_REQUESTS,
            QUARANTINE_STAGING_REQUESTS,
            WRITE_CONFIRMATION_TAIL,
        )

        self._checkpoint_readback_attempted = False

        def prepare(record: SessionRecord) -> SessionRecord:
            if ledger.evidence_producer() != self._producer:
                raise ReviewInputError("caller checkpoint producer changed")
            if record.record_sha256 != self._root:
                raise ReviewInputError("caller checkpoint root changed")
            updated = (
                mutate(record)
                if mutate is not None
                else record.evolve(generation=record.generation + 1)
            ).evolve(
                api_run_accounting=self.accounting.to_document(),
            )
            if (
                self._terminal_reason == "analysis-abort"
                and record.transaction is not None
                and record.transaction.phase == "analysis"
                and record.reservation_id == record.transaction.reservation_id
                and updated.reservation_id is None
                and updated.transaction is None
            ):
                self._abort_source = {
                    "source_root": record.record_sha256,
                    "cleanup_root": updated.record_sha256,
                    "transaction": record.transaction.to_dict(),
                    "entry_state": "not_started",
                    "run_id": self.run.run_id,
                    "owner": asdict(self.accounting.owner),
                }
            self._checkpoint = updated
            return updated

        # All discovery/rediscovery/activation scans and the terminal page share
        # this finite allocation. Complete enumeration remains mandatory; the
        # write and its confirmation remain protected before every read.
        key = "native:" + getattr(mutate, "__qualname__", "checkpoint")
        from .caller_operation import prewrite_fence_requests

        fences = prewrite_fence_requests(self)
        quarantine = isinstance(ledger, ApiRunSessionLedger) and getattr(
            ledger, "_quarantine_required", False
        )
        capacity = NATIVE_OPERATION_REQUESTS
        if quarantine:
            capacity = (
                QUARANTINE_STAGING_REQUESTS
                if self._terminal_reason is None
                else QUARANTINE_CHECKPOINT_REQUESTS
            )
        if quarantine:
            from .quarantine_census import lifecycle_checkpoint_operation

            specialized_quarantine = lifecycle_checkpoint_operation(
                ledger, self, identity
            )
            if specialized_quarantine is not None:
                # Admit the complete source-priced census/fence/stage tail,
                # not a truncated min(capacity, remaining) operation.
                self.budget_view.preflight(specialized_quarantine)
                capacity = specialized_quarantine
        confirmation_tail = WRITE_CONFIRMATION_TAIL
        if not quarantine and isinstance(ledger, ApiRunSessionLedger):
            if (
                getattr(ledger._take_plain_finding_operation, "__func__", None)
                is not ApiRunSessionLedger._take_plain_finding_operation
            ):
                raise ReviewInputError("finding lifecycle operation source changed")
            specialized = ledger._take_plain_finding_operation(self, identity)
            if specialized is not None:
                capacity, confirmation_tail = specialized
        with self._operation(
            key,
            capacity + fences,
            write_tail=confirmation_tail,
            prewrite_reads=fences,
        ) as operation:
            self._checkpoint_operation = operation
            try:
                if quarantine:
                    from .quarantine_checkpoint import replace_quarantine_checkpoint

                    confirmed = replace_quarantine_checkpoint(
                        self, ledger, identity, prepare, now=now
                    )
                else:
                    confirmed = GitHubIssueCommentSessionLedger.replace(
                        ledger, identity, prepare, now=now
                    )
                self._accept_checkpoint(ledger, confirmed)
                operation.confirmed = True
                self._checkpoint_operation = None
                return confirmed
            except BaseException:
                if not operation.write_attempted:
                    # Preparing pure bytes conveys no mutation authority. A
                    # failed enumeration made no write and needs no readback.
                    self._checkpoint = None
                    self._checkpoint_operation = None
                raise

    def _accept_checkpoint(
        self, ledger: GitHubIssueCommentSessionLedger, record: SessionRecord
    ) -> None:
        if ledger.evidence_producer() != self._producer:
            raise ReviewInputError("caller checkpoint producer changed")
        if (
            self._checkpoint is None
            or record.record_sha256 != self._checkpoint.record_sha256
        ):
            raise ReviewInputError("caller checkpoint publication is unconfirmed")
        persisted = ApiRunAccounting.from_document(record.api_run_accounting)
        persisted.require_successor(self.accounting)
        from .quarantine_checkpoint import expire

        expire(getattr(self, "_quarantine_checkpoint_snapshot", None))
        if self._admitting_action:
            self._confirm_control_write(
                self._checkpoint_write_request_id,
                record,
                "owned_action_admission",
                ledger,
            )
        self._root, self._checkpoint = record.record_sha256, None
        self._effect_record = record
        if (
            self._analysis_boundary is None
            and not self._operation_entry_started
            and record.transaction is not None
            and record.transaction.phase == "analysis"
            and record.reservation_id == record.transaction.reservation_id
        ):
            self._analysis_boundary = AnalysisAbortBoundary(
                self.accounting.identity, self.accounting.owner, record.transaction
            )
        if self._checkpoint_write_request_id is not None:
            self._reconciled_mutations.add(self._checkpoint_write_request_id)

    def reconcile_checkpoint(
        self, ledger: GitHubIssueCommentSessionLedger, identity: SessionIdentity
    ) -> SessionRecord:
        self._require_ledger(ledger, identity)
        if (
            self._checkpoint is None
            or self._checkpoint_operation is None
            or not self._checkpoint_operation.write_attempted
            or self._checkpoint_readback_attempted
        ):
            raise ReviewInputError("caller checkpoint has no funded reconciliation")
        self._checkpoint_readback_attempted = True
        operation = self._checkpoint_operation
        assert operation is not None
        with self._operation(operation.key, 0, retained=operation):
            if getattr(self, "_quarantine_checkpoint_snapshot", None) is not None:
                from .quarantine_checkpoint import reconcile_quarantine_checkpoint

                record = reconcile_quarantine_checkpoint(self, ledger, identity)
            else:
                loaded = ledger.load(identity)
                if loaded.status != "ok" or loaded.record is None:
                    raise ReviewInputError(
                        "caller checkpoint publication is unconfirmed"
                    )
                record = loaded.record
            self._accept_checkpoint(ledger, record)
            if self._checkpoint_write_request_id is not None:
                self._reconciled_mutations.add(self._checkpoint_write_request_id)
            operation.confirmed = True
            self._checkpoint_operation = None
            return record


class ApiRunSessionLedger(GitHubIssueCommentSessionLedger):
    """Native transaction writes through one already confirmed caller owner.

    reserve/commit/abort use the native public methods, which delegate to this
    replace seam. There is no admission/provider/completion budget factory.
    """

    def __init__(
        self,
        native: GitHubIssueCommentSessionLedger,
        caller: ApiRunRequestTransport,
        identity: SessionIdentity,
    ):
        caller._require_ledger(native, identity)
        caller.accounting
        self.__dict__.update(native.__dict__)
        self.caller = caller
        if self._quarantine_required:
            caller._quarantine_census_api_url = self.http.api_url
        self.evidence_budget = caller.budget_view
        self._terminal_claimed = False
        self._part_staging_identity: SessionIdentity | None = None

    def preflight_finding_lifecycle_retention(
        self, identity, *, added_parts, added_bytes, checkpoint_parts=None
    ):
        """Read-only proof against the existing complete run-owned frontier/tail.

        No discovery, enlarged page envelope or allocation occurs here. Missing
        authenticated context is unsupported, not permission to mint a budget.
        Actual staging repeats its checkpoint-snapshot and capacity checks.
        """
        from review_sensei.bounded_evidence import MAX_STORED_PARTS

        from .quarantine_published_tail import retained_storage_reserve

        self.caller._require_ledger(self, identity)
        self.caller.accounting
        if (
            type(added_parts) is not int
            or type(added_bytes) is not int
            or not 0 <= added_parts <= MAX_STORED_PARTS
            or added_bytes < 0
            or (added_parts == 0 and (added_bytes != 0 or self._quarantine_required))
            or (added_parts > 0 and added_bytes == 0)
            or not self._inventory_complete
            or self.caller._terminal_reason is not None
        ):
            raise ReviewInputError("finding lifecycle retained frontier is unsupported")
        if not self._quarantine_required:
            return self._preflight_plain_finding_parts(
                identity,
                added_parts=added_parts,
                added_bytes=added_bytes,
                checkpoint_parts=checkpoint_parts,
            )
        from .quarantine_census import lifecycle_capacity_plan

        scope = lifecycle_capacity_plan(self, identity, added_parts=added_parts)
        items = self._cached_comments()
        carriers = tuple(self._owned_carrier_items(items, identity))
        reserve_parts, reserve_bytes = retained_storage_reserve(self)
        # Existing admitted tail still owns its storage promise. New lifecycle
        # bytes cannot silently consume that independent recovery reservation.
        from review_sensei.bounded_evidence import MAX_PART_BYTES

        from .native_manifest import MANIFEST_WIRE_BOUND

        authenticated = self._quarantine_authenticated
        assert authenticated is not None
        promised_parts = 2 * len(authenticated.pending) + 5
        promised_bytes = (promised_parts - 1) * MAX_PART_BYTES + MANIFEST_WIRE_BOUND
        retained_parts = len(carriers) + reserve_parts + added_parts + promised_parts
        retained_bytes = (
            sum(len(item["body"].encode()) for item in carriers)
            + reserve_bytes
            + added_bytes
            + promised_bytes
        )
        comments = len(scope.plan.known) + len(scope.plan.unknown)
        assert self.evidence_budget is not None
        self.evidence_budget.preflight(2 * added_parts + 5)
        return {
            "additional_calls": 0,
            "comment_allocations": comments,
            "retained_parts": retained_parts,
            "retained_bytes": retained_bytes,
        }

    @_checkpoint_pricer
    def preflight_finding_lifecycle_checkpoint_pricing(
        self,
        record,
        *,
        added_parts,
        added_bytes,
        checkpoint_parts,
        checkpoint_stage=False,
        checkpoint_target=None,
        _checkpoint_factory=None,
    ):
        """Issue a sealed price only for an exact source-owned checkpoint writer."""
        if checkpoint_stage:
            if not self._quarantine_required:
                return None
            from .quarantine_census import lifecycle_staging_pricing

            return lifecycle_staging_pricing(
                self,
                record,
                checkpoint_parts=checkpoint_parts,
                target=checkpoint_target,
                factory=_checkpoint_factory,
            )
        if self._quarantine_required:
            from .quarantine_census import lifecycle_checkpoint_pricing

            return lifecycle_checkpoint_pricing(
                self,
                record,
                added_parts=added_parts,
                added_bytes=added_bytes,
                checkpoint_parts=checkpoint_parts,
                factory=_checkpoint_factory,
            )
        from review_sensei.bounded_evidence import canonical_bytes

        from .review_run import ActionRunLedger, ReviewRunLedger
        from .run_authority import VerifiedRunAction

        if type(self) not in {ApiRunSessionLedger, ActionRunLedger, ReviewRunLedger}:
            return None
        if type(self) is ReviewRunLedger and self.caller._abort_source is not None:
            return None
        expected_replace = (
            ApiRunSessionLedger.replace
            if type(self) is ApiRunSessionLedger
            else ReviewRunLedger.replace
            if type(self) is ReviewRunLedger
            else ActionRunLedger.replace
        )
        implementations = (
            ("replace", expected_replace),
            (
                "replace_finding_lifecycle_inline",
                ApiRunSessionLedger.replace_finding_lifecycle_inline,
            ),
            (
                "preflight_finding_lifecycle_inline",
                ApiRunSessionLedger.preflight_finding_lifecycle_inline,
            ),
            (
                "replace_finding_lifecycle_parts",
                ApiRunSessionLedger.replace_finding_lifecycle_parts,
            ),
            (
                "_preflight_plain_finding_parts",
                ApiRunSessionLedger._preflight_plain_finding_parts,
            ),
            (
                "_plain_finding_selected_input",
                ApiRunSessionLedger._plain_finding_selected_input,
            ),
            ("stage_evidence", ApiRunSessionLedger.stage_evidence),
            (
                "_take_plain_finding_operation",
                ApiRunSessionLedger._take_plain_finding_operation,
            ),
            (
                "_read_plain_finding_pages",
                ApiRunSessionLedger._read_plain_finding_pages,
            ),
            ("_bounded_comments", GitHubIssueCommentSessionLedger._bounded_comments),
            (
                "preflight_finding_lifecycle_retention",
                ApiRunSessionLedger.preflight_finding_lifecycle_retention,
            ),
        )
        if any(
            getattr(getattr(self, name), "__func__", None) is not implementation
            for name, implementation in implementations
        ):
            return None
        identity = record.identity()
        if self._plain_finding_selected_input(identity) is not record:
            return None
        original = canonical_bytes(record.to_dict())
        guard = getattr(self, "business_guard", None)
        owner = getattr(guard, "__self__", None)
        run = getattr(owner, "run", None)
        guard_calls = 0
        if type(self) is ActionRunLedger:
            if (
                getattr(guard, "__func__", None) is not VerifiedRunAction.fence
                or type(owner) is not VerifiedRunAction
                or run is None
                or run.ledger is not self
                or run.caller is not self.caller
                or run.identity != identity
                or owner.owner != self.caller.accounting.owner
            ):
                return None
            guard_calls = 2  # actual fresh source GET and PR GET, outside checkpoint
        elif guard is not None:
            return None
        authority_bytes = canonical_bytes(owner.document) if owner is not None else None

        def validate():
            if (
                self._plain_finding_selected_input(identity) is not record
                or canonical_bytes(record.to_dict()) != original
                or any(
                    getattr(getattr(self, name), "__func__", None) is not implementation
                    for name, implementation in implementations
                )
                or getattr(self, "business_guard", None) is not guard
                or type(self) is ReviewRunLedger
                and self.caller._abort_source is not None
            ):
                return False
            return owner is None or (
                owner.run is run
                and run.ledger is self
                and run.caller is self.caller
                and owner.owner == self.caller.accounting.owner
                and canonical_bytes(owner.document) == authority_bytes
            )

        page_plan = None
        observed = self.preflight_finding_lifecycle_retention(
            identity,
            added_parts=added_parts,
            added_bytes=added_bytes,
            checkpoint_parts=checkpoint_parts,
        )
        page_plan = _PLAIN_FINDING_PLANS.get(self)
        source_validate = validate

        def validate_page_plan():
            if not source_validate():
                return False
            if page_plan is None:
                return True
            _require_plain_page_plan(page_plan)
            return (
                page_plan.record is record
                and page_plan.caller is self.caller
                and page_plan.root == self.caller._root
                and page_plan.inventory_bytes
                == canonical_bytes(self._cached_comments())
            )

        if not validate_page_plan():
            raise ReviewInputError(
                "finding lifecycle checkpoint pricing source changed"
            )
        return _checkpoint_factory(
            self,
            record,
            checkpoint_parts=checkpoint_parts,
            capacity=observed,
            calls=tuple(
                (5 if n == 0 else 4 + 3 * n)
                + 2 * ((len(page_plan.page_bounds) if page_plan is not None else 1) - 1)
                + guard_calls
                for n in checkpoint_parts
            ),
            validate=validate_page_plan,
            page_counts=tuple(
                (len(page_plan.page_bounds) if page_plan is not None else 1)
                for _ in checkpoint_parts
            ),
        )

    def preflight_finding_lifecycle_inline(self, record):
        from review_sensei.finding_lifecycle_store import (
            _inline_checkpoint_read_free,
        )

        if not _inline_checkpoint_read_free(record):
            return None
        from review_sensei.finding_lifecycle_store import _qualified_checkpoint_pricer

        pricer = _qualified_checkpoint_pricer(self)
        if pricer is None:
            return None
        proof = pricer(
            record,
            added_parts=0,
            added_bytes=0,
            checkpoint_parts=(0,),
        )
        if proof is None:
            return None
        return {
            "checkpoint_pricing": proof,
            "conditional_calls": 4,
            "post_mutator_calls": 3,
            "page_size": 100,
            "sealed": bool(getattr(self, "_plain_finding_scope_active", False)),
        }

    def replace_finding_lifecycle_inline(self, identity, mutate, *, record):
        plan = self.preflight_finding_lifecycle_inline(record)
        if plan is None or plan["sealed"]:
            raise ReviewInputError(
                "finding lifecycle inline source scope is unavailable"
            )
        return self.replace_finding_lifecycle_parts(
            identity,
            mutate,
            record=record,
            parts=0,
            retained_bytes=0,
        )

    def _preflight_plain_finding_parts(
        self, identity, *, added_parts, added_bytes, checkpoint_parts
    ):
        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.session import MAX_SESSION_RECORD_BYTES

        from .api_run_plan import NATIVE_OPERATION_REQUESTS
        from .caller_operation import prewrite_fence_requests
        from .session_ledger import parse_session_comment

        checkpoints = (added_parts,) if checkpoint_parts is None else checkpoint_parts
        if (
            not isinstance(checkpoints, tuple)
            or not checkpoints
            or any(type(n) is not int or n < 0 for n in checkpoints)
            or sum(checkpoints) != added_parts
        ):
            raise ReviewInputError("finding lifecycle checkpoint part plan conflicts")
        fences = prewrite_fence_requests(self.caller)
        selected = self._plain_finding_selected_input(identity)
        root_calls = 4 if selected is not None else 5
        # A specialized closed full-page plan can reserve more than the
        # generic ten-call tranche, never more than the same caller can fund.
        items = self._cached_comments()
        if (
            not self._inventory_complete
            or self._history_reading
            or self._broker is not None
            or self._activation_ticket is not None
            or self._tail_scan_pages is not None
        ):
            raise ReviewInputError("finding lifecycle plain frontier is unsupported")
        roots = []
        root_bodies = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("body"), str):
                raise ReviewInputError("finding lifecycle plain frontier is incomplete")
            parsed = parse_session_comment(item["body"], identity=identity)
            if parsed is not None:
                self._part_owner(item, identity)
                roots.append(parsed)
                root_bodies.append(item["body"])
        if len(roots) != 1 or roots[0].record_sha256 != self.caller._root:
            raise ReviewInputError("finding lifecycle retained session root changed")
        record = roots[0]
        history = record.convergence_history
        baseline = history.get("baseline") if isinstance(history, Mapping) else None
        disposition_needs_reads = record.disposition_snapshot is not None
        if record.disposition_snapshot is not None:
            from review_sensei.bounded_evidence import read_inline_evidence
            from review_sensei.disposition_snapshot import (
                normalize_snapshot,
                snapshot_document,
            )

            snapshot = normalize_snapshot(record.disposition_snapshot)
            assert snapshot is not None
            manifest = snapshot["state_manifest"]
            if (
                snapshot["schema_version"] == "1.0"
                and manifest["encoding"] == "inline-json-v1"
            ):
                binding = manifest["binding"]
                if binding["producer"] != self.evidence_producer():
                    raise ReviewInputError(
                        "disposition snapshot producer does not match session"
                    )
                # This exact decoder cannot call a reader/transport. Validate
                # the committed bytes before granting the zero-read plan.
                document = read_inline_evidence(manifest, expected_binding=binding)
                snapshot_document(
                    document,
                    result_digest=snapshot["result_digest"],
                    item_count=manifest["item_count"],
                )
                disposition_needs_reads = False
            elif (
                snapshot["schema_version"] == "1.0"
                and manifest["encoding"] == "partitioned-json-v1"
                and selected is not None
            ):
                from review_sensei.bounded_evidence import read_partitioned_evidence

                if manifest["binding"]["producer"] != self.evidence_producer():
                    raise ReviewInputError("disposition snapshot producer changed")
                assert self.evidence_budget is not None
                document = read_partitioned_evidence(
                    manifest,
                    expected_binding=manifest["binding"],
                    reader=lambda sid: self._current_plain_snapshot_part(identity, sid),
                    budget=self.evidence_budget,
                )
                snapshot_document(
                    document,
                    result_digest=snapshot["result_digest"],
                    item_count=manifest["item_count"],
                )
                disposition_needs_reads = False
        if (
            record.assessment_queue is not None
            or disposition_needs_reads
            or record.historical_assessments is not None
            or isinstance(baseline, Mapping)
            and baseline.get("encoding") == "partitioned-json-v1"
        ):
            raise ReviewInputError(
                "finding lifecycle plain root needs unpriced evidence reads"
            )
        # Original single-page 512KiB response cap. Root replacement is bounded
        # independently of compression; each carrier retains existing audited
        # 4096-byte response metadata and canonical encoded-part wire reserve.
        from .session_ledger import plain_session_comment_json_bound

        root_wire = 6 * MAX_SESSION_RECORD_BYTES
        if getattr(self, "_controlled_restart", None) is None:
            grants = set(self._observed_consumed_grants)
            if self._accepted_consumed_grant is not None:
                grants.add(self._accepted_consumed_grant)
            root_wire = plain_session_comment_json_bound(
                consumed_grants=grants,
                repository_id=identity.repository_id,
                pull_request=identity.pull_request,
            )
        # The scoped source-owned path PATCHes the one selected comment. Its
        # old body and replacement cannot coexist in a listing. Retain every
        # metadata field/immutable part, and subtract only this exact JSON string.
        replaced_body = (
            len(canonical_bytes(root_bodies[0])) if selected is not None else 0
        )
        wire = len(canonical_bytes(items)) - replaced_body + root_wire + 4096
        # Core supplies the sum of canonical part wire maxima (compressBound
        # included, with the short final part), not arbitrary payload lengths.
        # Actual staging repeats this with canonical rendered-part lengths.
        # Keep framing/outer-JSON escapes and native metadata for every part.
        wire += added_bytes + added_parts * (512 + 4096)
        if len(items) + added_parts > 200:
            raise ReviewInputError(
                "finding lifecycle comment allocation capacity exceeded"
            )
        page_count = 1
        if len(items) + added_parts >= 100 or wire >= 512 * 1024:
            from .review_run import ActionRunLedger, ReviewRunLedger

            if (
                type(self)
                not in {ApiRunSessionLedger, ActionRunLedger, ReviewRunLedger}
                or selected is None
                or getattr(self.replace_finding_lifecycle_parts, "__func__", None)
                is not ApiRunSessionLedger.replace_finding_lifecycle_parts
                or getattr(self._preflight_plain_finding_parts, "__func__", None)
                is not ApiRunSessionLedger._preflight_plain_finding_parts
                or getattr(self._bounded_comments, "__func__", None)
                is not GitHubIssueCommentSessionLedger._bounded_comments
            ):
                raise ReviewInputError("finding lifecycle paged source is unqualified")
            # GitHub returns issue comments in creation order. Pin the observed
            # prefix; all future immutable parts append. A changed ordering is
            # a refusal, never a reason to change pages or omit old history.
            ids = tuple(item.get("id") for item in items)
            if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(
                ids
            ):
                raise ReviewInputError("finding lifecycle frontier identity is invalid")
            bounds = [len(canonical_bytes(item)) for item in items]
            root_index = next(
                i for i, item in enumerate(items) if item["body"] == root_bodies[0]
            )
            bounds[root_index] += root_wire - replaced_body + 4096
            # Per-page partitioning needs individual maxima, not an average of
            # aggregate bytes. The original canonical part bound is unchanged.
            from review_sensei.bounded_evidence import MAX_PART_BYTES

            bounds.extend([MAX_PART_BYTES + 512 + 4096] * added_parts)
            # Seal contiguous native offsets, not one pessimistic page size
            # for the entire frontier. Sizes may grow only at aligned offsets;
            # no response-driven retry or unobserved inventory is introduced.
            requests: list[tuple[int, int]] = []
            response_bounds: list[int] = []
            offset = 0
            while offset <= len(bounds):
                for size in (100, 20, 10, 5, 1):
                    if offset % size:
                        continue
                    members = bounds[offset : offset + size]
                    bound = 2 + sum(members) + max(0, len(members) - 1)
                    if bound < 512 * 1024:
                        requests.append((size, offset // size + 1))
                        response_bounds.append(bound)
                        offset += size
                        break
                else:
                    raise ReviewInputError("finding lifecycle page capacity exceeded")
                if len(requests) > 10:
                    raise ReviewInputError("finding lifecycle page capacity exceeded")
            page_requests = tuple(requests)
            page_size = page_requests[0][0]
            page_bounds = tuple(response_bounds)
            if self.caller._root != selected.record_sha256:
                raise ReviewInputError("finding lifecycle page plan root changed")
            plan = _PlainFindingPagePlan(
                self.caller,
                selected.record_sha256,
                selected,
                canonical_bytes(selected.to_dict()),
                ids,
                canonical_bytes(items),
                page_size,
                page_bounds,
                page_requests,
                len(bounds),
                checkpoints,
                root_calls,
                fences,
            )
            _PLAIN_FINDING_ISSUED[plan] = _plain_page_signature(plan)
            _PLAIN_FINDING_PLANS[self] = plan
            page_count = len(page_bounds)
        else:
            _PLAIN_FINDING_PLANS.pop(self, None)
            if any(
                root_calls + 3 * n + (n + 1) * fences
                > NATIVE_OPERATION_REQUESTS + fences
                for n in checkpoints
            ):
                raise EvidenceBudgetExhausted(
                    "finding lifecycle parts exceed original checkpoint operation"
                )
        carriers = tuple(self._owned_carrier_items(items, identity))
        assert self.evidence_budget is not None
        self.evidence_budget.preflight(
            sum(
                root_calls + 3 * n + 2 * (page_count - 1) + (n + 1) * fences
                for n in checkpoints
            )
        )
        return {
            "additional_calls": 0,
            "comment_allocations": len(items) + added_parts,
            "retained_parts": len(carriers) + added_parts,
            "retained_bytes": sum(len(i["body"].encode()) for i in carriers)
            + added_bytes,
        }

    def _take_plain_finding_operation(self, caller, identity):
        from review_sensei.bounded_evidence import canonical_bytes

        from .api_run_plan import NATIVE_OPERATION_REQUESTS, WRITE_CONFIRMATION_TAIL

        active = _PLAIN_FINDING_ACTIVE.get(self)
        if active is None:
            return None
        plan, consumed = active
        _require_plain_page_plan(plan)
        if (
            consumed
            or caller is not plan.caller
            or caller is not self.caller
            or caller._root != plan.root
            or identity != plan.record.identity()
            or canonical_bytes(plan.record.to_dict()) != plan.record_bytes
            or self._plain_finding_selected_input(identity) is not plan.record
            or len(plan.checkpoint_parts) != 1
            or canonical_bytes(self._cached_comments()) != plan.inventory_bytes
        ):
            raise ReviewInputError("finding lifecycle paged operation custody changed")
        _PLAIN_FINDING_ACTIVE[self] = (plan, True)
        pages = len(plan.page_bounds)
        calls = plan.root_calls + 3 * plan.checkpoint_parts[0] + 2 * (pages - 1)
        assert self.evidence_budget is not None
        self.evidence_budget.preflight(
            calls + (plan.checkpoint_parts[0] + 1) * plan.fences
        )
        return max(
            NATIVE_OPERATION_REQUESTS, calls
        ), WRITE_CONFIRMATION_TAIL + pages - 1

    def _read_plain_finding_pages(self, identity):
        from review_sensei.bounded_evidence import canonical_bytes

        active = _PLAIN_FINDING_ACTIVE.get(self)
        if active is None:
            return None
        plan, consumed = active
        _require_plain_page_plan(plan)
        if (
            not consumed
            or self.caller is not plan.caller
            or self.caller._root != plan.root
            or identity != plan.record.identity()
            or canonical_bytes(plan.record.to_dict()) != plan.record_bytes
        ):
            raise ReviewInputError("finding lifecycle page plan custody changed")
        if self._inventory_complete and not self._list_refresh:
            return self._cached_comments()
        collected = []
        for (page_size, page), bound in zip(
            plan.page_requests, plan.page_bounds, strict=True
        ):
            path = f"{self._comments_path(identity)}?per_page={page_size}&page={page}"
            status, payload = self._request("GET", path)
            if (
                status != 200
                or not isinstance(payload, list)
                or len(payload) > page_size
                or any(not isinstance(item, dict) for item in payload)
                or len(canonical_bytes(payload)) > bound
            ):
                raise ReviewInputError("finding lifecycle planned page changed")
            collected.extend(payload)
            if len(collected) > plan.maximum_items:
                raise ReviewInputError("finding lifecycle planned frontier grew")
            if len(payload) < page_size:
                observed_ids = tuple(item.get("id") for item in collected)
                if (
                    any(type(value) is not int or value <= 0 for value in observed_ids)
                    or len(set(observed_ids)) != len(observed_ids)
                    or observed_ids[: len(plan.inventory_ids)] != plan.inventory_ids
                ):
                    raise ReviewInputError(
                        "finding lifecycle planned frontier order changed"
                    )
                self._replace_inventory(collected)
                self._inventory_complete = True
                self._list_refresh = False
                self._note_anchor(collected)
                return self._cached_comments()
        raise ReviewInputError("finding lifecycle planned terminal page is not short")

    def _current_plain_snapshot_part(self, identity, storage_id):
        """Authenticate only the complete currently observed listing body."""
        if not self._inventory_complete or self._list_refresh:
            raise ReviewInputError("finding lifecycle snapshot listing is stale")
        if not isinstance(storage_id, str) or not storage_id.isdecimal():
            raise ReviewInputError("finding lifecycle snapshot part identity changed")
        item = self._comments_by_id.get(int(storage_id))
        if not isinstance(item, dict):
            raise ReviewInputError("finding lifecycle snapshot part is missing")
        return self._part_from_item(item, identity, storage_id)

    def _plain_finding_document(self, record, manifest):
        """Decode only bytes in the source-owned complete current listing."""
        from review_sensei.bounded_evidence import (
            read_inline_evidence,
            read_partitioned_evidence,
        )

        from .finding_lifecycle_dictionary import _manifest

        current_binding = record.finding_lifecycle["manifest"]["binding"]
        binding = _manifest(manifest, current_binding)
        if (
            binding["producer"] != self.evidence_producer()
            or binding["repository"] != record.repository
            or binding["repository_id"] != record.repository_id
            or binding["pull_request"] != record.pull_request
            or binding["generation"] > record.generation
            or not self._inventory_complete
            or self._list_refresh
        ):
            raise ReviewInputError("finding dictionary complete listing scope changed")
        if self.evidence_budget is None:
            raise ReviewInputError("finding dictionary original read budget is missing")
        self.evidence_budget.check()
        if manifest["encoding"] == "inline-json-v1":
            return read_inline_evidence(manifest, expected_binding=binding)
        return read_partitioned_evidence(
            manifest,
            expected_binding=binding,
            reader=lambda sid: self._current_plain_snapshot_part(
                record.identity(), sid
            ),
            budget=self.evidence_budget,
        )

    def _require_plain_finding_dictionary(self, record):
        from review_sensei.bounded_evidence import canonical_bytes

        from .finding_lifecycle_dictionary import is_dictionary, unpack

        scope = getattr(self, "_plain_finding_dictionary_scope", None)
        if scope is None and not getattr(self, "_plain_finding_scope_active", False):
            return
        if record.finding_lifecycle is None:
            if scope is not None:
                raise ReviewInputError("finding dictionary current root disappeared")
            return
        manifest = record.finding_lifecycle["manifest"]
        envelope = self._plain_finding_document(record, manifest)
        if not isinstance(envelope, dict) or "lifecycle" not in envelope:
            raise ReviewInputError("finding dictionary selected carrier is invalid")
        current = envelope["lifecycle"]
        if is_dictionary(current):
            base = self._plain_finding_document(record, current["dictionary_manifest"])
            unpack(current, base=base, current_binding=manifest["binding"])
        if scope is not None:
            identity, carrier_bytes = scope
            if record.identity() != identity:
                raise ReviewInputError("finding dictionary checkpoint identity changed")
            carrier = json.loads(carrier_bytes)
            base = self._plain_finding_document(record, carrier["dictionary_manifest"])
            unpack(carrier, base=base, current_binding=manifest["binding"])
            if canonical_bytes(carrier) != carrier_bytes:
                raise ReviewInputError("finding dictionary checkpoint bytes changed")

    @contextmanager
    def finding_dictionary_checkpoint(self, record, carrier):
        """Additional negative dependency fences; this grants no writer authority."""
        from review_sensei.bounded_evidence import canonical_bytes

        from .finding_lifecycle_dictionary import is_dictionary

        if (
            not is_dictionary(carrier)
            or getattr(self, "_plain_finding_dictionary_scope", None) is not None
        ):
            raise ReviewInputError("finding dictionary checkpoint scope is invalid")
        self._plain_finding_dictionary_scope: tuple[Any, bytes] | None = (
            record.identity(),
            canonical_bytes(carrier),
        )
        try:
            self._require_plain_finding_dictionary(record)
            yield
        finally:
            self._plain_finding_dictionary_scope = None

    def _require_plain_finding_snapshot(self, record):
        self._require_plain_finding_dictionary(record)
        scope = getattr(self, "_plain_finding_snapshot_scope", None)
        if scope is None:
            return
        identity, root, snapshot, _ids = scope
        from review_sensei.bounded_evidence import canonical_bytes

        if (
            record.identity() != identity
            or canonical_bytes(record.disposition_snapshot) != snapshot
            or self.caller._root != root
            or self.caller._checkpoint_operation is None
            or self.caller._active_operation is not self.caller._checkpoint_operation
            or not self._plain_finding_scope_active
        ):
            raise ReviewInputError("finding lifecycle retained snapshot scope changed")

    def _read_plain_finding_snapshot_part(self, identity, storage_id):
        scope = getattr(self, "_plain_finding_snapshot_scope", None)
        if scope is None or storage_id not in scope[3]:
            return None
        record = getattr(self.caller, "_effect_record", None)
        if record is None or identity != scope[0]:
            raise ReviewInputError("finding lifecycle snapshot scope identity changed")
        self._require_plain_finding_snapshot(record)
        return self._current_plain_snapshot_part(identity, storage_id)

    def _plain_finding_selected_input(self, identity):
        """Validate the existing owned complete frontier without another read."""
        from review_sensei.bounded_evidence import canonical_bytes

        from .session_ledger import parse_session_comment

        record = getattr(self.caller, "_effect_record", None)
        if (
            record is None
            or record.identity() != identity
            or record.record_sha256 != self.caller._root
            or not self._inventory_complete
            or self._history_reading
            or self._broker is not None
            or self._activation_ticket is not None
            or self._tail_scan_pages is not None
            or getattr(self, "_controlled_restart", None) is not None
            or self._quarantine_required
        ):
            return None
        self.caller._require_ledger(self, identity)
        roots = []
        for item in self._cached_comments():
            if not isinstance(item, dict) or not isinstance(item.get("body"), str):
                return None
            parsed = parse_session_comment(item["body"], identity=identity)
            if parsed is not None:
                self._part_owner(item, identity)
                roots.append((item.get("id"), parsed))
        if len(roots) != 1 or type(roots[0][0]) is not int or roots[0][0] <= 0:
            return None
        if canonical_bytes(roots[0][1].to_dict()) != canonical_bytes(record.to_dict()):
            raise ReviewInputError("finding lifecycle selected input bytes changed")
        return record

    def _consume_plain_finding_input(self, identity):
        capability = getattr(self, "_plain_finding_input", None)
        if capability is None:
            if getattr(self, "_plain_finding_scope_active", False):
                raise ReviewInputError(
                    "finding lifecycle selected input already consumed"
                )
            return None
        self._plain_finding_input = None
        record, commitment = capability
        from review_sensei.bounded_evidence import canonical_bytes

        if (
            record is not getattr(self.caller, "_effect_record", None)
            or record is not self._plain_finding_selected_input(identity)
            or canonical_bytes(record.to_dict()) != commitment
            or self.caller._checkpoint_operation is None
            or self.caller._active_operation is not self.caller._checkpoint_operation
        ):
            raise ReviewInputError("finding lifecycle selected input authority changed")
        # Run the full existing marker/producer/conflict validation over the
        # independently observed frontier, not only the lightweight preflight.
        self._list_refresh = False
        comment_id, parsed = self._discover(identity)
        if parsed is None or canonical_bytes(parsed.to_dict()) != commitment:
            raise ReviewInputError("finding lifecycle selected frontier changed")
        return comment_id, record

    def replace_finding_lifecycle_parts(
        self, identity, mutate, *, record, parts, retained_bytes
    ):
        self.preflight_finding_lifecycle_retention(
            identity,
            added_parts=parts,
            added_bytes=retained_bytes,
            checkpoint_parts=(parts,),
        )
        if self._quarantine_required:
            from .quarantine_census import (
                activate_lifecycle_capacity,
                lifecycle_capacity_plan,
            )

            if record is not self.caller._effect_record:
                raise ReviewInputError("quarantine lifecycle input is not source-owned")
            scope = lifecycle_capacity_plan(self, identity, added_parts=parts)
            with activate_lifecycle_capacity(scope):
                return self.replace(identity, mutate)
        if getattr(self, "_finding_lifecycle_page_size", None) is not None:
            raise ReviewInputError("finding lifecycle scoped mutation cannot nest")
        from review_sensei.bounded_evidence import canonical_bytes

        selected = self._plain_finding_selected_input(identity)
        if selected is not None and record is not selected:
            raise ReviewInputError(
                "finding lifecycle selected input is not source-owned"
            )
        if getattr(self, "_plain_finding_scope_active", False):
            raise ReviewInputError("finding lifecycle selected input cannot nest")
        page_plan = _PLAIN_FINDING_PLANS.pop(self, None)
        if page_plan is not None:
            _PLAIN_FINDING_ACTIVE[self] = (page_plan, False)
        self._finding_lifecycle_page_size = 100
        self._plain_finding_input = (
            (record, canonical_bytes(record.to_dict()))
            if selected is not None
            else None
        )
        self._plain_finding_scope_active = selected is not None
        self._plain_finding_snapshot_scope = None
        snapshot = record.disposition_snapshot
        if (
            selected is not None
            and snapshot is not None
            and snapshot["schema_version"] == "1.0"
            and snapshot["state_manifest"]["encoding"] == "partitioned-json-v1"
        ):
            self._plain_finding_snapshot_scope = (
                identity,
                record.record_sha256,
                canonical_bytes(snapshot),
                frozenset(p["storage_id"] for p in snapshot["state_manifest"]["parts"]),
            )
        try:
            return self.replace(identity, mutate)
        finally:
            self._plain_finding_snapshot_scope = None
            self._plain_finding_input = None
            self._plain_finding_scope_active = False
            self._finding_lifecycle_page_size = None
            _PLAIN_FINDING_ACTIVE.pop(self, None)

    def stage_disposition_snapshot_batch(self, identity, **kwargs):
        if self._part_staging_identity is not None:
            raise ReviewInputError("immutable part staging cannot nest")
        self._part_staging_identity = identity
        try:
            return super().stage_disposition_snapshot_batch(identity, **kwargs)
        finally:
            self._part_staging_identity = None

    def _preflight_disposition_snapshot_batch(
        self, identity, record, *, missing_parts, read_parts, retained_parts
    ):
        snapshot = getattr(self, "_quarantine_checkpoint_snapshot", None)
        if snapshot is None or self._part_staging_identity != identity:
            raise ReviewInputError(
                "run-owned disposition batch requires exact checkpoint scope"
            )
        snapshot.require()
        if (
            snapshot.identity != identity
            or snapshot.record.record_sha256 != record.record_sha256
        ):
            raise ReviewInputError("run-owned disposition batch root changed")
        # The issued census and exact owned creation echoes provide every part
        # read. Each new part retains its live-head GET/POST allowance; five
        # existing requests remain for root fences/head/write/confirmation.
        required = 2 * missing_parts + 5
        if snapshot.operation.remaining < required:
            raise EvidenceBudgetExhausted(
                "complete disposition batch exceeds existing checkpoint allocation"
            )
        assert self.evidence_budget is not None
        self.evidence_budget.preflight(required)

    def stage_evidence(
        self,
        identity: SessionIdentity,
        *,
        binding: dict[str, object],
        document: object,
        item_count: int,
        max_manifest_bytes: int,
    ) -> dict[str, object]:
        if self._part_staging_identity is not None:
            raise ReviewInputError("immutable part staging cannot nest")
        snapshot = getattr(self, "_quarantine_checkpoint_snapshot", None)
        if snapshot is not None:
            snapshot.preflight_staging(
                document, binding, item_count, max_manifest_bytes
            )
        self._part_staging_identity = identity
        try:
            return super().stage_evidence(
                identity,
                binding=binding,
                document=document,
                item_count=item_count,
                max_manifest_bytes=max_manifest_bytes,
            )
        finally:
            self._part_staging_identity = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        identity = self._part_staging_identity
        if identity is None or method == "GET":
            return super()._request(method, path, body=body)
        content = body.get("body") if body is not None else None
        if (
            not self._mutation_active
            or method != "POST"
            or path != self._comments_path(identity)
            or not isinstance(content, str)
            or not content.startswith("ReviewSensei immutable evidence part v1\n")
            or not content.endswith("<!-- reviewsensei:evidence-part:v1 -->")
        ):
            raise ReviewInputError("immutable part mutation is invalid")
        key = hashlib.sha256(content.encode()).hexdigest()
        with self.caller.staged_part_operation(key) as operation:
            status, payload = super()._request(method, path, body=body)
            if (
                status != 201
                or not isinstance(payload, dict)
                or type(payload.get("id")) is not int
                or payload["id"] < 1
                or payload.get("body") != content
            ):
                raise GitHubPublicationTransientError(
                    "immutable part create was not confirmed"
                )
            self._part_owner(payload, identity)
            operation.confirmed = True
            snapshot = getattr(self, "_quarantine_checkpoint_snapshot", None)
            if snapshot is not None:
                snapshot.note_staged(payload)
            return status, payload

    def load(self, identity: SessionIdentity, **kwargs: Any) -> Any:
        with self.caller.read_operation("native-load"):
            loaded = super().load(identity, **kwargs)
        from review_sensei.bounded_evidence import canonical_bytes

        owned = self.caller._effect_record
        observed = loaded.record
        if (
            loaded.status in {"ok", "migrated"}
            and owned is not None
            and observed is not None
            and observed.identity() == owned.identity() == identity
            and observed.record_sha256 == owned.record_sha256 == self.caller._root
            and canonical_bytes(observed.to_dict()) == canonical_bytes(owned.to_dict())
        ):
            return replace(loaded, record=owned)
        return loaded

    def _fetch_pages(
        self, identity: SessionIdentity, *, since: str | None
    ) -> list[Any]:
        phase = self.caller._logical_phase
        self.caller.native_walks[phase] = self.caller.native_walks.get(phase, 0) + 1
        return super()._fetch_pages(identity, since=since)

    def claim_analysis_cleanup(
        self, identity: SessionIdentity, reservation: str
    ) -> bool:
        if self._terminal_claimed:
            return False
        self._terminal_claimed = True
        self.caller.begin_terminal("analysis-abort")
        return True

    def claim_publication_completion(
        self, identity: SessionIdentity, transaction: Any, published: bool
    ) -> None:
        if self._terminal_claimed:
            raise ReviewInputError("caller terminal mutation was already attempted")
        self._terminal_claimed = True
        self.caller.begin_terminal("publication-completion")

    def replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.caller.replace_checkpoint(self, identity, mutate, now=now)


# Register the exact source implementation, not a generic cheaper-count hook.


_install_checkpoint_pricer(
    ApiRunSessionLedger.preflight_finding_lifecycle_checkpoint_pricing
)
