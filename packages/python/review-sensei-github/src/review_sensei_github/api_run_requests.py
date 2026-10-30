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
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_owner import claim_review_startup
from .caller_operation import CallerOperation
from .errors import GitHubPublicationTransientError
from .http import Opener
from .session_ledger import GitHubIssueCommentSessionLedger


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
    def __init__(self, response: Any, confirmed: Callable[[], None]):
        self._response, self._confirmed = response, confirmed
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
        self._checkpoint_write_request_id: str | None = None
        self._activation_write_request_id: str | None = None
        self._terminal_block: str | None = None
        self._terminal_reason: str | None = None
        self._budget_view: Any = None
        self._logical_phase = "startup"
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
        if operation.remaining < (write_tail or 1):
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

        return self._operation(
            key, WRITE_CONFIRMATION_TAIL, write_tail=WRITE_CONFIRMATION_TAIL
        )

    @contextmanager
    def staged_part_operation(self, key: str) -> Iterator[CallerOperation]:
        """A bounded immutable write inside the existing root allocation.

        Every child request also spends the parent's enumeration capacity.
        Its write/echo proof cannot spend or replace the root's protected tail.
        Unknown child effects stop the root mutation and cannot be replayed.
        """
        from .api_run_plan import WRITE_CONFIRMATION_TAIL

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
            parent.write_tail + WRITE_CONFIRMATION_TAIL
        ):
            raise EvidenceBudgetExhausted("immutable part and root tails are unfunded")
        operation = CallerOperation(
            WRITE_CONFIRMATION_TAIL, WRITE_CONFIRMATION_TAIL, identity[1]
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

    def open(self, request: Request, *, timeout: float) -> _AccountedResponse:
        from .api_run_plan import WRITE_CONFIRMATION_TAIL

        if not isinstance(request, Request):
            raise ReviewInputError("caller request is invalid")
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
        self.observations.append(observation)
        if observation["mutation"]:
            if self._checkpoint is not None and request.get_method() == "PATCH":
                self._checkpoint_write_request_id = request_id
            elif self._state is None and self._activation is not None:
                self._activation_write_request_id = request_id
        self._last_method = request.get_method()

        observed_status = None

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

        # The debit/intent above happens before the physical opener. Exceptions
        # preserve the charge; read/parse/status failures never refund it.
        try:
            response = self._opener(request, timeout=timeout)
        except HTTPError as error:
            observed_status = error.code
            confirmed()
            raise
        observed_status = getattr(response, "status", None)
        return _AccountedResponse(response, confirmed)

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
        if self._activation_write_request_id is not None:
            self._reconciled_mutations.add(self._activation_write_request_id)
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
        from dataclasses import replace

        from .api_run_history import retain_previous_run
        from .api_run_plan import owner_allocations

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
        if (
            old.identity == self.run
            or old.identity.repository_id != self.run.repository_id
        ):
            raise ReviewInputError("new caller run identity conflicts")
        self._reservation_started = True
        self._producer = ledger.evidence_producer()
        ledger.evidence_budget = self.budget_view

        def prepare(record):
            if record.record_sha256 != previous.record_sha256:
                raise ReviewInputError("previous caller root changed")
            if ledger.evidence_producer() != self._producer:
                raise ReviewInputError("previous caller root producer changed")
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
                        or old.terminal_transaction != record.transaction.transaction_id
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

                read_action_result(ledger, identity, record)
            before_archive = len(self.observations)
            reference = retain_previous_run(ledger, identity, record)
            # The issuer authenticated the exact immutable part readback.
            # Charge the lost response permanently; only its effect is settled.
            self._reconciled_mutations.update(
                str(o["request_id"])
                for o in self.observations[before_archive:]
                if o.get("mutation")
                and (o["outcome"] == "unknown" or o.get("uncertain_effect"))
            )
            allocation = owner_allocations(configuration.limit, self.plan.liability)
            state = replace(
                initial_accounting(
                    self.run,
                    configuration,
                    owner,
                    self.plan,
                    prior_nonstartup_liability_upper_bound=0,
                ),
                previous_record=reference,
            )
            state = state.reserve("hosted-review", allocation.first_work).reserve(
                "hosted-terminal", allocation.first_terminal
            )
            updated = record.rollover_api_accounting(state.to_document())
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

        confirmed = self._action_checkpoint(ledger, identity, prepare)
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
            MAX_PARTITION_DECODED_BYTES,
            encode_evidence,
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
        envelope = {
            "schema_version": "caller-action-result-v1",
            "intent_sha256": boundary.intent_sha256,
            "outcome": outcome,
            "result": json_copy(result_document),
        }
        carrier = {
            "schema_version": "caller-action-result-v1",
            "payload": encode_evidence(
                envelope, max_decoded_bytes=MAX_PARTITION_DECODED_BYTES
            ),
        }
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
                )
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
        with self._operation(
            key, capacity, write_tail=WRITE_CONFIRMATION_TAIL
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
        self._root, self._checkpoint = record.record_sha256, None
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
            return super().load(identity, **kwargs)

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
