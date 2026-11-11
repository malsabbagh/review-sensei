"""Credential-contained writer fencing and epoch cutover protocol (G02).

This module extends the unconnected authority candidate with explicit writer-route
containment, trusted host grant issuance, monotonic per-PR epoch cutover, and
credential-contained HTTP forwarding.

Live canonical-authority cutover remains disabled by default
(``LIVE_CANONICAL_CUTOVER_ENABLED = False``). Placing an authority coordinator
beside an unfenced token-issuing broker or direct raw-token writer cannot fence
delayed network writes; therefore ``WriterContainmentPosture`` refuses exclusive
native authority whenever any bypass token path remains active.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import weakref
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from .bounded_evidence import canonical_bytes
from .recovery_authority import (
    RESPONSE_BYTES,
    Access,
    AuthorityHost,
    AuthorityRefusal,
    Dispatch,
    Receipt,
    Snapshot,
    SqliteAuthorityCandidate,
)

# Live canonical-authority cutover MUST remain disabled by default until an
# external service-owned host satisfies full credential containment and linearizable
# frontier durability across all writer routes.
LIVE_CANONICAL_CUTOVER_ENABLED: Final[bool] = False

CONTAINED_WRITER_ROUTES: Final[frozenset[str]] = frozenset(
    {
        "actions_hosted",
        "actions_local",
        "actions_command",
        "manual_cli",
    }
)
BYPASS_WRITER_ROUTES: Final[frozenset[str]] = frozenset(
    {
        "broker_raw_token",
        "pre_cancellation_inflight",
    }
)
ALL_WRITER_ROUTES: Final[frozenset[str]] = (
    CONTAINED_WRITER_ROUTES | BYPASS_WRITER_ROUTES
)

_ISSUED_TOKENS: weakref.WeakSet[IssuedWriterToken] = weakref.WeakSet()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_containment(condition: bool, reason: str) -> None:
    if not condition:
        raise AuthorityRefusal(reason)


def derive_epoch_store_id(
    base_store_id: str,
    repository_id: int,
    pull_request: int,
    epoch: int,
) -> str:
    """Derive a deterministic 64-hex store identifier for one PR writer epoch."""

    material = f"writer-epoch-v1:{base_store_id}:{repository_id}:{pull_request}:{epoch}"
    return _sha256_hex(material.encode("utf-8"))


@dataclass(frozen=True)
class WriterContainmentPosture:
    """Deployment credential-containment facts required before exclusive fencing."""

    broker_raw_write_tokens_disabled: bool
    manual_cli_raw_write_tokens_disabled: bool
    actions_raw_write_tokens_disabled: bool
    all_mutations_routed_via_forwarder: bool
    pre_cutover_inflight_drained: bool

    @classmethod
    def fully_contained(cls) -> WriterContainmentPosture:
        return cls(
            broker_raw_write_tokens_disabled=True,
            manual_cli_raw_write_tokens_disabled=True,
            actions_raw_write_tokens_disabled=True,
            all_mutations_routed_via_forwarder=True,
            pre_cutover_inflight_drained=True,
        )

    @classmethod
    def unfenced_broker(cls) -> WriterContainmentPosture:
        return cls(
            broker_raw_write_tokens_disabled=False,
            manual_cli_raw_write_tokens_disabled=True,
            actions_raw_write_tokens_disabled=True,
            all_mutations_routed_via_forwarder=False,
            pre_cutover_inflight_drained=False,
        )

    def is_fully_contained(self) -> bool:
        fields = (
            self.broker_raw_write_tokens_disabled,
            self.manual_cli_raw_write_tokens_disabled,
            self.actions_raw_write_tokens_disabled,
            self.all_mutations_routed_via_forwarder,
            self.pre_cutover_inflight_drained,
        )
        return all(type(flag) is bool and flag for flag in fields)

    def require_exclusive_containment(self) -> None:
        _require_containment(
            self.is_fully_contained(),
            "unfenced token issuer or direct writer defeats canonical writer containment",
        )


class IssuedWriterToken:
    """Process-authenticated host capability token; never constructible from JSON."""

    __slots__ = (
        "__weakref__",
        "_issuer_id",
        "base_store_id",
        "epoch",
        "grant_id",
        "pull_request",
        "repository_id",
        "writer_route",
    )

    def __init__(
        self,
        *,
        issuer_id: int,
        grant_id: str,
        writer_route: str,
        base_store_id: str,
        repository_id: int,
        pull_request: int,
        epoch: int,
    ) -> None:
        self._issuer_id = issuer_id
        self.grant_id = grant_id
        self.writer_route = writer_route
        self.base_store_id = base_store_id
        self.repository_id = repository_id
        self.pull_request = pull_request
        self.epoch = epoch


@dataclass(frozen=True)
class WireDispatchOutcome:
    """Result returned by a service-contained physical HTTP forwarder."""

    status_code: int
    response_body: bytes
    effect_confirmed: bool = True
    response_complete: bool = True
    usage_calls: int | None = 1


WireForwarder = Callable[[str, str, bytes, dict[str, Any]], WireDispatchOutcome]


class ContainedForwardingCoordinator(AuthorityHost):
    """Trusted host issuer, linearizable epoch frontier, and contained forwarder.

    Callers hold only an ``IssuedWriterToken`` capability and never possess raw
    GitHub installation write credentials. Every mutating write must pass:
    1. Trusted host authentication and route containment verification.
    2. Monotonic active-epoch check on the protected nonrollback frontier.
    3. Durable one-use ``prepare`` + ``forward`` binding in ``SqliteAuthorityCandidate``.
    """

    def __init__(
        self,
        directory: Path,
        *,
        posture: WriterContainmentPosture,
        wire_forwarder: WireForwarder,
        now_ms: int = 1000,
        live_cutover_enabled: bool = LIVE_CANONICAL_CUTOVER_ENABLED,
        synthetic_qualification: bool = False,
    ) -> None:
        _require_containment(
            isinstance(posture, WriterContainmentPosture),
            "writer containment posture is invalid",
        )
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self.authority_path = self._directory / "authority.sqlite"
        self.frontier_path = self._directory / "frontier.sqlite"
        self.posture = posture
        self._wire_forwarder = wire_forwarder
        self._now_ms = now_ms
        self._live_cutover_enabled = live_cutover_enabled
        self._synthetic_qualification = synthetic_qualification
        self._lock = threading.RLock()
        self._grants: dict[str, Access] = {}
        self._wire_outcomes: dict[str, WireDispatchOutcome] = {}
        self._custom_receipts: dict[str, Receipt | None] = {}
        with closing(self._connect_frontier()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS frontier(
                    key TEXT PRIMARY KEY,
                    body BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scope_epochs(
                    scope_key TEXT PRIMARY KEY,
                    active_epoch INTEGER NOT NULL,
                    legacy_locked INTEGER NOT NULL,
                    legacy_blob BLOB
                );
                """
            )
        self.candidate = SqliteAuthorityCandidate(self.authority_path, self)

    def _connect_frontier(self) -> sqlite3.Connection:
        return sqlite3.connect(self.frontier_path, isolation_level=None, timeout=5)

    @staticmethod
    def _scope_key(base_store_id: str, repository_id: int, pull_request: int) -> str:
        return f"{base_store_id}:{repository_id}:{pull_request}"

    def _require_cutover_gate(
        self, *, native_exclusive: bool, writer_route: str
    ) -> None:
        _require_containment(
            writer_route in ALL_WRITER_ROUTES,
            f"unsupported writer route: {writer_route!r}",
        )
        if not native_exclusive:
            return
        _require_containment(
            self._live_cutover_enabled or self._synthetic_qualification,
            "live canonical-authority cutover is disabled by default",
        )
        _require_containment(
            writer_route in CONTAINED_WRITER_ROUTES,
            "bypass writer route cannot receive canonical exclusive authority",
        )
        self.posture.require_exclusive_containment()

    def set_now_ms(self, value: int) -> None:
        with self._lock:
            self._now_ms = value

    def set_observation_override(
        self, dispatch_id: str, receipt: Receipt | None
    ) -> None:
        with self._lock:
            self._custom_receipts[dispatch_id] = receipt

    def enroll_initial_writer(
        self,
        *,
        base_access: Access,
        writer_route: str,
        legacy: bytes | None = None,
    ) -> tuple[IssuedWriterToken, Snapshot]:
        """Issue epoch-1 authority for a PR scope and initialize its candidate root."""

        _require_containment(type(base_access) is Access, "base access is invalid")
        self._require_cutover_gate(
            native_exclusive=base_access.native_exclusive,
            writer_route=writer_route,
        )
        if base_access.native_exclusive:
            _require_containment(
                legacy is None,
                "legacy identity requires explicit migration or reset before exclusive authority",
            )
        scope_key = self._scope_key(
            base_access.store_id,
            base_access.repository_id,
            base_access.pull_request,
        )
        epoch = 1
        epoch_store_id = derive_epoch_store_id(
            base_access.store_id,
            base_access.repository_id,
            base_access.pull_request,
            epoch,
        )
        bound_access = replace(base_access, store_id=epoch_store_id, epoch=epoch)
        grant_id = _sha256_hex(
            f"{scope_key}:{epoch}:{writer_route}:{bound_access.caller_sha256}".encode(
                "utf-8"
            )
        )
        token = IssuedWriterToken(
            issuer_id=id(self),
            grant_id=grant_id,
            writer_route=writer_route,
            base_store_id=base_access.store_id,
            repository_id=base_access.repository_id,
            pull_request=base_access.pull_request,
            epoch=epoch,
        )
        with self._lock:
            with closing(self._connect_frontier()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    existing = db.execute(
                        "SELECT active_epoch FROM scope_epochs WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    _require_containment(
                        existing is None,
                        "PR writer scope is already enrolled; use advance_epoch",
                    )
                    db.execute(
                        "INSERT INTO scope_epochs VALUES(?,?,?,?)",
                        (scope_key, epoch, int(legacy is not None), legacy),
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            self._grants[grant_id] = bound_access
            _ISSUED_TOKENS.add(token)
            try:
                snapshot = self.candidate.initialize(token, legacy=legacy)
            except BaseException:
                self._grants.pop(grant_id, None)
                with closing(self._connect_frontier()) as db:
                    db.execute(
                        "DELETE FROM scope_epochs WHERE scope_key=? AND active_epoch=1",
                        (scope_key,),
                    )
                    db.commit()
                raise
            return token, snapshot

    def advance_epoch(
        self,
        *,
        predecessor_token: IssuedWriterToken,
        successor_access: Access,
        writer_route: str,
        predecessor_ceased: bool,
    ) -> tuple[IssuedWriterToken, Snapshot]:
        """Atomically fence the predecessor epoch and enroll the successor epoch.

        Cutover refuses fail-closed when:
        - predecessor cessation is unproven (cancellation alone is not cessation),
        - predecessor has any unsettled physical dispatch (``forwarded`` or
          ``effect-confirmed`` without terminal settlement),
        - predecessor frontier/SQLite state diverged, or
        - legacy state is present and successor requests ``native_exclusive=True``.
        """

        predecessor_access = self._authenticate_token(predecessor_token)
        _require_containment(
            type(successor_access) is Access, "successor access is invalid"
        )
        _require_containment(
            predecessor_ceased is True,
            "predecessor writer cessation is unproven; cancellation is not cessation",
        )
        _require_containment(
            successor_access.repository == predecessor_access.repository
            and successor_access.repository_id == predecessor_access.repository_id
            and successor_access.pull_request == predecessor_access.pull_request
            and successor_access.store_id == predecessor_token.base_store_id,
            "successor writer scope conflicts with predecessor scope",
        )
        self._require_cutover_gate(
            native_exclusive=successor_access.native_exclusive,
            writer_route=writer_route,
        )
        with self._lock:
            predecessor_snapshot = self.candidate.snapshot(predecessor_token)
            dispatches = predecessor_snapshot.document()["dispatches"]
            unsettled = [
                dispatch_id
                for dispatch_id, item in dispatches.items()
                if item["phase"] in {"forwarded", "effect-confirmed"}
            ]
            _require_containment(
                not unsettled,
                "predecessor epoch has unsettled physical dispatch; reconcile or hold required",
            )
            scope_key = self._scope_key(
                predecessor_token.base_store_id,
                predecessor_token.repository_id,
                predecessor_token.pull_request,
            )
            with closing(self._connect_frontier()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute(
                        "SELECT active_epoch, legacy_locked, legacy_blob "
                        "FROM scope_epochs WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    _require_containment(
                        row is not None and int(row[0]) == predecessor_token.epoch,
                        "predecessor token does not own active writer epoch",
                    )
                    legacy_locked = bool(row[1])
                    carried_legacy = None if row[2] is None else bytes(row[2])
                    if legacy_locked and successor_access.native_exclusive:
                        raise AuthorityRefusal(
                            "legacy identity cannot transition to exclusive native authority "
                            "without migration or reset"
                        )
                    next_epoch = int(row[0]) + 1
                    db.execute(
                        "UPDATE scope_epochs SET active_epoch=? WHERE scope_key=?",
                        (next_epoch, scope_key),
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

            epoch_store_id = derive_epoch_store_id(
                predecessor_token.base_store_id,
                predecessor_token.repository_id,
                predecessor_token.pull_request,
                next_epoch,
            )
            bound_access = replace(
                successor_access, store_id=epoch_store_id, epoch=next_epoch
            )
            grant_id = _sha256_hex(
                f"{scope_key}:{next_epoch}:{writer_route}:{bound_access.caller_sha256}".encode(
                    "utf-8"
                )
            )
            successor_token = IssuedWriterToken(
                issuer_id=id(self),
                grant_id=grant_id,
                writer_route=writer_route,
                base_store_id=predecessor_token.base_store_id,
                repository_id=predecessor_token.repository_id,
                pull_request=predecessor_token.pull_request,
                epoch=next_epoch,
            )
            self._grants[grant_id] = bound_access
            _ISSUED_TOKENS.add(successor_token)
            snapshot = self.candidate.initialize(successor_token, legacy=carried_legacy)
            return successor_token, snapshot

    def active_epoch(
        self, *, base_store_id: str, repository_id: int, pull_request: int
    ) -> int:
        scope_key = self._scope_key(base_store_id, repository_id, pull_request)
        with closing(self._connect_frontier()) as db:
            row = db.execute(
                "SELECT active_epoch FROM scope_epochs WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
        _require_containment(row is not None, "PR writer scope is not enrolled")
        return int(row[0])

    def reattach_active_writer(
        self,
        *,
        base_access: Access,
        writer_route: str,
    ) -> IssuedWriterToken:
        """Re-bind the active epoch's host capability after coordinator restart."""

        _require_containment(type(base_access) is Access, "base access is invalid")
        self._require_cutover_gate(
            native_exclusive=base_access.native_exclusive,
            writer_route=writer_route,
        )
        scope_key = self._scope_key(
            base_access.store_id,
            base_access.repository_id,
            base_access.pull_request,
        )
        with self._lock:
            with closing(self._connect_frontier()) as db:
                row = db.execute(
                    "SELECT active_epoch, legacy_locked FROM scope_epochs "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
            _require_containment(row is not None, "PR writer scope is not enrolled")
            assert row is not None
            active_epoch = int(row[0])
            legacy_locked = bool(row[1])
            if legacy_locked and base_access.native_exclusive:
                raise AuthorityRefusal(
                    "legacy identity requires explicit migration or reset before exclusive authority"
                )
            epoch_store_id = derive_epoch_store_id(
                base_access.store_id,
                base_access.repository_id,
                base_access.pull_request,
                active_epoch,
            )
            bound_access = replace(
                base_access, store_id=epoch_store_id, epoch=active_epoch
            )
            grant_id = _sha256_hex(
                f"{scope_key}:{active_epoch}:{writer_route}:{bound_access.caller_sha256}".encode(
                    "utf-8"
                )
            )
            token = IssuedWriterToken(
                issuer_id=id(self),
                grant_id=grant_id,
                writer_route=writer_route,
                base_store_id=base_access.store_id,
                repository_id=base_access.repository_id,
                pull_request=base_access.pull_request,
                epoch=active_epoch,
            )
            self._grants[grant_id] = bound_access
            _ISSUED_TOKENS.add(token)
            try:
                self.candidate.snapshot(token)
            except BaseException:
                self._grants.pop(grant_id, None)
                raise
            return token

    def _authenticate_token(self, authorization: object) -> Access:
        _require_containment(
            isinstance(authorization, IssuedWriterToken)
            and authorization in _ISSUED_TOKENS
            and authorization._issuer_id == id(self),
            "unqualified caller",
        )
        assert isinstance(authorization, IssuedWriterToken)
        access = self._grants.get(authorization.grant_id)
        _require_containment(access is not None, "unqualified caller")
        assert access is not None
        return access

    def _require_active_epoch(self, access: Access, *, operation: str) -> None:
        with closing(self._connect_frontier()) as db:
            row = db.execute(
                "SELECT scope_key, active_epoch, legacy_locked FROM scope_epochs "
                "WHERE scope_key LIKE ?",
                (f"%:{access.repository_id}:{access.pull_request}",),
            ).fetchall()
        matched: tuple[str, int, int] | None = None
        for scope_key, active_epoch, legacy_locked in row:
            base_store_id = str(scope_key).split(":", 1)[0]
            expected_store = derive_epoch_store_id(
                base_store_id,
                access.repository_id,
                access.pull_request,
                access.epoch,
            )
            if expected_store == access.store_id:
                matched = (str(scope_key), int(active_epoch), int(legacy_locked))
                break
        _require_containment(matched is not None, "writer epoch scope is unknown")
        assert matched is not None
        _scope_key, active_epoch, legacy_locked = matched
        _require_containment(
            access.epoch == active_epoch,
            "stale writer epoch fenced before physical dispatch",
        )
        if operation != "observation":
            _require_containment(
                not legacy_locked and access.native_exclusive,
                "legacy effects or writer exclusion block native actions",
            )
            self.posture.require_exclusive_containment()

    # --- AuthorityHost protocol methods ---

    def authenticate(self, authorization: object) -> Access:
        return self._authenticate_token(authorization)

    def now_ms(self) -> int:
        with self._lock:
            return self._now_ms

    def frontier(self, key: str) -> Snapshot | None:
        with closing(self._connect_frontier()) as db:
            row = db.execute("SELECT body FROM frontier WHERE key=?", (key,)).fetchone()
            return None if row is None else Snapshot(bytes(row[0]))

    def advance(self, key: str, expected: str | None, checkpoint: Snapshot) -> None:
        with self._lock, closing(self._connect_frontier()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT body FROM frontier WHERE key=?", (key,)
                ).fetchone()
                current_digest = None if row is None else _sha256_hex(bytes(row[0]))
                if current_digest != expected:
                    raise AuthorityRefusal("frontier conflict")
                db.execute(
                    "INSERT OR REPLACE INTO frontier VALUES(?,?)",
                    (key, checkpoint.encoded),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def authorize(self, access: Access, binding: dict[str, Any]) -> None:
        with self._lock:
            self._require_active_epoch(access, operation=str(binding["operation"]))

    def forward(self, access: Access, binding: dict[str, Any]) -> None:
        with self._lock:
            self._require_active_epoch(access, operation=str(binding["operation"]))
            dispatch_id = str(binding["dispatch_id"])
            _require_containment(
                dispatch_id not in self._wire_outcomes,
                "physical dispatch already forwarded",
            )
            request_bytes = canonical_bytes(binding["body"])
            _require_containment(
                _sha256_hex(request_bytes) == binding["request_sha256"],
                "forwarded request payload diverged from canonical binding",
            )
        outcome = self._wire_forwarder(
            str(binding["method"]),
            str(binding["target"]),
            request_bytes,
            dict(binding),
        )
        with self._lock:
            self._wire_outcomes[dispatch_id] = outcome

    def observe(self, access: Access, binding: dict[str, Any]) -> Receipt | None:
        dispatch_id = str(binding["dispatch_id"])
        with self._lock:
            if dispatch_id in self._custom_receipts:
                return self._custom_receipts[dispatch_id]
            outcome = self._wire_outcomes.get(dispatch_id)
        if outcome is None:
            return None
        binding_digest = _sha256_hex(canonical_bytes(binding))
        if not outcome.response_complete:
            return Receipt(
                binding_sha256=binding_digest,
                effect_confirmed=outcome.effect_confirmed,
                response_complete=False,
                usage_calls=None,
                response_bytes=None,
            )
        _require_containment(
            len(outcome.response_body) <= RESPONSE_BYTES,
            "wire response exceeds candidate bound",
        )
        return Receipt(
            binding_sha256=binding_digest,
            effect_confirmed=outcome.effect_confirmed,
            response_complete=True,
            usage_calls=1 if outcome.usage_calls is None else outcome.usage_calls,
            response_bytes=len(outcome.response_body),
        )

    def fenced_http_mutation(
        self,
        token: IssuedWriterToken,
        *,
        dispatch_id: str,
        operation: str,
        method: str,
        target: str,
        payload: Mapping[str, Any],
        reservation_sha256: str,
        liability_calls: int = 4,
        deadline_ms: int | None = None,
    ) -> tuple[WireDispatchOutcome, Snapshot]:
        """Execute a contained HTTP mutation through prepare -> forward -> reconcile."""

        access = self._authenticate_token(token)
        canonical_body = canonical_bytes(dict(payload))
        request = Dispatch(
            dispatch_id=dispatch_id,
            operation=operation,
            method=method,
            target=target,
            body=canonical_body,
            effect_sha256=_sha256_hex(
                f"{operation}:{method}:{target}:".encode("utf-8") + canonical_body
            ),
            reservation_sha256=reservation_sha256,
            liability_calls=liability_calls,
            deadline_ms=(
                access.funding.deadline_ms - 1 if deadline_ms is None else deadline_ms
            ),
        )
        current = self.candidate.snapshot(token)
        prepared = self.candidate.prepare(token, current, request)
        forwarded = self.candidate.forward(token, prepared, request)
        settled = self.candidate.reconcile(token, forwarded, dispatch_id)
        with self._lock:
            outcome = self._wire_outcomes[dispatch_id]
        return outcome, settled
