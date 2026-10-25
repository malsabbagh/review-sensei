"""Pre-dispatch enforcement for the bounded startup/recovery experiment.

This is not a public workflow entry or a Worker allowance. Startup slots are
source-selected, prefunded, nonrefundable and one-use. Ordinary capacity can be
enabled only by this adapter's actual native mutation and authenticated readback,
never by loading a caller-supplied accounting document into a transport.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    StartupPlan,
    initial_accounting,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_owner import claim_hosted_startup
from .http import MAX_PAGINATION_PAGES, Opener
from .session_ledger import GitHubIssueCommentSessionLedger

NATIVE_REPLACE_CALLER_REQUESTS = 3 * MAX_PAGINATION_PAGES + 1
_WRITE_AND_READBACK = 1 + MAX_PAGINATION_PAGES


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
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        slots = [s for s in plan.slots if s.job == "hosted" and s.attempt == attempt]
        if len(slots) != 1:
            raise ReviewInputError("startup transport slot is unsupported")
        claim_hosted_startup(directory, run, attempt)
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
        self._checkpoint_readback_attempted = False
        self._producer: str | None = None
        self._reservation_started = False

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

    def open(self, request: Request, *, timeout: float) -> _AccountedResponse:
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
        pending: PendingApiRequest | None = None
        if self._state is None:
            if self.startup_dispatches >= len(self._slot.requests):
                raise ReviewInputError("startup caller allowance is exhausted")
            if (
                request.get_method() in {"POST", "PATCH"}
                and self._activation is not None
                and len(self._slot.requests) - self.startup_dispatches
                < _WRITE_AND_READBACK
            ):
                raise ReviewInputError("startup caller write/readback tail is unfunded")
            self.startup_dispatches += 1
            phase = "startup"
            request_id = f"startup-{self.run.repository_id}-{self.run.run_id}-{self.attempt}-{self._sequence}"
        else:
            if (
                request.get_method() == "PATCH"
                and self._checkpoint is not None
                and self.usable < _WRITE_AND_READBACK
            ):
                raise ReviewInputError(
                    "caller checkpoint write/readback tail is unfunded"
                )
            path = urlsplit(request.full_url).path
            if (
                request.get_method() in {"POST", "PATCH"}
                and ("/issues/comments/" in path or path.endswith("/comments"))
                and self.usable < _WRITE_AND_READBACK
            ):
                # Comment carriers can require a complete bounded listing to
                # confirm their owned write. This checks room; it does not
                # debit an internal liability or create a second allowance.
                raise ReviewInputError("caller comment write/readback tail is unfunded")
            phase = "ordinary"
            request_id = f"caller-{self.run.repository_id}-{self.run.run_id}-{self._state.owner.generation}-{self._sequence}"
            pending = PendingApiRequest(request_id, deadline)
            assert self._block is not None
            self._state = self._state.dispatch(self._block, pending)
        observation = {
            "phase": phase,
            "request_id": request_id,
            "deadline_unix_ms": deadline,
            "outcome": "unknown",
        }
        self.observations.append(observation)
        self._last_method = request.get_method()

        def confirmed() -> None:
            if observation["outcome"] == "expired":
                raise ReviewInputError(
                    "caller response exceeded its original request deadline"
                )
            if observation["outcome"] != "unknown":
                return
            if pending is not None:
                assert self._state is not None
                self._state = self._state.confirm(pending)
            observation["outcome"] = "response"
            if (
                int(self._clock() * 1000) > deadline
                or self._monotonic() > request_deadline
            ):
                observation["outcome"] = "expired"
                raise ReviewInputError(
                    "caller response exceeded its original request deadline"
                )

        # The debit/intent above happens before the physical opener. Exceptions
        # preserve the charge; read/parse/status failures never refund it.
        try:
            response = self._opener(request, timeout=timeout)
        except HTTPError:
            confirmed()
            raise
        return _AccountedResponse(response, confirmed)

    def _require_ledger(
        self, ledger: GitHubIssueCommentSessionLedger, identity: SessionIdentity
    ) -> None:
        if not isinstance(ledger, GitHubIssueCommentSessionLedger):
            raise ReviewInputError("caller transport requires the native ledger")
        opener = ledger.http.opener
        if (
            not ledger.app_slug
            or identity.repository_id != self.run.repository_id
            or getattr(opener, "__self__", None) is not self
            or getattr(opener, "__func__", None) is not ApiRunRequestTransport.open
            or ledger.evidence_budget is not None
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
            or state.owner.job != "hosted"
        ):
            raise ReviewInputError("confirmed caller reservation scope conflicts")
        block = next((b for b in state.blocks if b.block_id == block_id), None)
        if block is None or block.issued_by != state.owner:
            raise ReviewInputError("confirmed caller reservation owner conflicts")
        self._state, self._block, self._root = state, block_id, record.record_sha256
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
                candidate.owner.job != "hosted"
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
        All remaining capacity is reserved once, not replenished per phase.
        """
        self._require_ledger(ledger, identity)
        if self._reservation_started or self._state is not None:
            raise ReviewInputError("caller reservation was already attempted")
        if (
            owner.run_attempt != 1
            or owner.run_attempt != self.attempt
            or owner.job != "hosted"
        ):
            raise ReviewInputError("initial caller reservation owner conflicts")
        self._reservation_started = True

        def prepare(record: SessionRecord) -> SessionRecord:
            state = initial_accounting(
                self.run,
                configuration,
                owner,
                self.plan,
                prior_nonstartup_liability_upper_bound=0,
            )
            if state.remaining < NATIVE_REPLACE_CALLER_REQUESTS:
                raise ReviewInputError("initial caller persistence tranche is unfunded")
            # The full remainder is one durable tranche. Native writes still
            # require their complete bounded persistence tail before dispatch.
            state = state.reserve("hosted-review", state.remaining)
            updated = record.evolve(api_run_accounting=state.to_document())
            self._activation = updated, "hosted-review"
            return updated

        confirmed = ledger.initialize_with_mutation(identity, prepare)
        # A genuinely missing ledger has no observed marker author yet. The
        # native initializer authenticates the configured Bot on exact readback.
        self._producer = ledger.evidence_producer()
        return self._accept_activation(ledger, confirmed)

    def reconcile_activation(
        self, ledger: GitHubIssueCommentSessionLedger, identity: SessionIdentity
    ) -> ApiRunAccounting:
        self._require_ledger(ledger, identity)
        if (
            self._state is not None
            or self._activation is None
            or self._last_method != "PATCH"
            or self._activation_readback_attempted
        ):
            raise ReviewInputError("caller reservation has no funded reconciliation")
        self._activation_readback_attempted = True
        loaded = ledger.load(identity)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("caller reservation publication is unconfirmed")
        return self._accept_activation(ledger, loaded.record)

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
        if self.usable < NATIVE_REPLACE_CALLER_REQUESTS:
            raise ReviewInputError("caller checkpoint operation is unfunded")
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
            self._checkpoint = updated
            return updated

        # Bypass only this accounting adapter's override, retaining the actual
        # native mutation, fresh rediscovery and authenticated exact readback.
        confirmed = GitHubIssueCommentSessionLedger.replace(
            ledger, identity, prepare, now=now
        )
        self._accept_checkpoint(ledger, confirmed)
        return confirmed

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
        self._root, self._checkpoint = record.record_sha256, None

    def reconcile_checkpoint(
        self, ledger: GitHubIssueCommentSessionLedger, identity: SessionIdentity
    ) -> SessionRecord:
        self._require_ledger(ledger, identity)
        if (
            self._checkpoint is None
            or self._last_method != "PATCH"
            or self._checkpoint_readback_attempted
        ):
            raise ReviewInputError("caller checkpoint has no funded reconciliation")
        self._checkpoint_readback_attempted = True
        loaded = ledger.load(identity)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("caller checkpoint publication is unconfirmed")
        self._accept_checkpoint(ledger, loaded.record)
        return loaded.record


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

    def replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.caller.replace_checkpoint(self, identity, mutate, now=now)
