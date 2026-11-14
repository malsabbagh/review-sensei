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
import re
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
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[a-f0-9]{64}$")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None


def _require_containment(condition: bool, reason: str) -> None:
    if not condition:
        raise AuthorityRefusal(reason)


def extract_unsettled_dispatches(snapshot: Snapshot) -> list[str]:
    """Validate canonical snapshot dispatch schema and list unsettled dispatch IDs."""

    _require_containment(
        isinstance(snapshot, Snapshot),
        "canonical snapshot is invalid",
    )
    try:
        doc = snapshot.document()
    except (ValueError, UnicodeError, TypeError) as exc:
        raise AuthorityRefusal("canonical snapshot document is malformed") from exc
    _require_containment(
        isinstance(doc, Mapping) and isinstance(doc.get("dispatches"), Mapping),
        "canonical snapshot dispatches map is malformed",
    )
    dispatches = doc["dispatches"]
    unsettled: list[str] = []
    for dispatch_id, item in dispatches.items():
        _require_containment(
            isinstance(dispatch_id, str)
            and bool(dispatch_id)
            and isinstance(item, Mapping)
            and isinstance(item.get("phase"), str),
            "canonical snapshot dispatch entry is malformed",
        )
        if item["phase"] in {"forwarded", "effect-confirmed"}:
            unsettled.append(dispatch_id)
    return unsettled


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
        allow_synthetic_overrides: bool = False,
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
        self._live_cutover_enabled = bool(live_cutover_enabled)
        self._synthetic_qualification = bool(synthetic_qualification)
        self._allow_synthetic_overrides = bool(allow_synthetic_overrides)
        self._lock = threading.RLock()
        self._grants: dict[str, Access] = {}
        self._attempted_dispatches: set[str] = set()
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
                    base_store_id TEXT NOT NULL,
                    repository_id INTEGER NOT NULL,
                    pull_request INTEGER NOT NULL,
                    active_epoch INTEGER NOT NULL,
                    active_epoch_store_id TEXT NOT NULL,
                    legacy_locked INTEGER NOT NULL,
                    legacy_blob BLOB,
                    CHECK (
                        (legacy_locked = 0 AND legacy_blob IS NULL)
                        OR (legacy_locked = 1 AND legacy_blob IS NOT NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_scope_epochs_pr
                    ON scope_epochs(repository_id, pull_request);
                """
            )
        self.candidate = SqliteAuthorityCandidate(self.authority_path, self)

    def _connect_frontier(self) -> sqlite3.Connection:
        return sqlite3.connect(self.frontier_path, isolation_level=None, timeout=5)

    @staticmethod
    def _scope_key(base_store_id: str, repository_id: int, pull_request: int) -> str:
        _require_containment(
            _is_sha256_hex(base_store_id),
            "base writer store_id must be a 64-hex sha256 digest",
        )
        return f"{base_store_id}:{repository_id}:{pull_request}"

    @staticmethod
    def _require_legacy_invariant(
        legacy_locked: bool, legacy_blob: bytes | None
    ) -> None:
        _require_containment(
            legacy_locked == (legacy_blob is not None),
            "writer epoch legacy lock invariant conflicts with stored legacy blob",
        )

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
        self,
        dispatch_id: str,
        receipt: Receipt | None,
        *,
        token: IssuedWriterToken,
    ) -> None:
        _require_containment(
            self._synthetic_qualification
            and self._allow_synthetic_overrides
            and not self._live_cutover_enabled,
            "synthetic observation overrides are disabled outside qualification",
        )
        with self._lock:
            access = self._authenticate_token(token)
            self._require_active_epoch(access, operation="observation")
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
        legacy_locked = legacy is not None
        self._require_legacy_invariant(legacy_locked, legacy)
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
                existing = db.execute(
                    "SELECT active_epoch FROM scope_epochs WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                _require_containment(
                    existing is None,
                    "PR writer scope is already enrolled; use advance_epoch",
                )
            self._grants[grant_id] = bound_access
            _ISSUED_TOKENS.add(token)
            try:
                snapshot = self.candidate.initialize(token, legacy=legacy)
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
                            "INSERT INTO scope_epochs VALUES(?,?,?,?,?,?,?,?)",
                            (
                                scope_key,
                                base_access.store_id,
                                base_access.repository_id,
                                base_access.pull_request,
                                epoch,
                                epoch_store_id,
                                int(legacy_locked),
                                legacy,
                            ),
                        )
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
            except BaseException:
                self._grants.pop(grant_id, None)
                _ISSUED_TOKENS.discard(token)
                # Epoch keys are shared by independent contenders. A failed
                # attempt does not own the durable rows: deleting them could
                # erase another writer's root, history, or confirmed frontier.
                # Retain partial initialization as evidence and fail closed;
                # there is no implicit reenrollment or accounting reset.
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
            _is_sha256_hex(successor_access.store_id)
            and successor_access.repository == predecessor_access.repository
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
            unsettled = extract_unsettled_dispatches(predecessor_snapshot)
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
                row = db.execute(
                    "SELECT base_store_id, active_epoch, legacy_locked, legacy_blob "
                    "FROM scope_epochs WHERE scope_key=? AND repository_id=? AND pull_request=?",
                    (
                        scope_key,
                        predecessor_token.repository_id,
                        predecessor_token.pull_request,
                    ),
                ).fetchone()
            _require_containment(
                row is not None
                and str(row[0]) == predecessor_token.base_store_id
                and int(row[1]) == predecessor_token.epoch,
                "predecessor token does not own active writer epoch",
            )
            if row is None:
                raise AuthorityRefusal(
                    "predecessor token does not own active writer epoch"
                )
            legacy_locked = bool(row[2])
            carried_legacy = None if row[3] is None else bytes(row[3])
            self._require_legacy_invariant(legacy_locked, carried_legacy)
            if legacy_locked and successor_access.native_exclusive:
                raise AuthorityRefusal(
                    "legacy identity cannot transition to exclusive native authority "
                    "without migration or reset"
                )
            next_epoch = int(row[1]) + 1
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
            try:
                snapshot = self.candidate.initialize(
                    successor_token, legacy=carried_legacy
                )
                with closing(self._connect_frontier()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        # Physical dispatch intent advances this same durable
                        # frontier before the wire call. Revalidate under the
                        # epoch-update transaction: a process-local lock cannot
                        # exclude another host forwarding after our preflight.
                        # If cutover wins first, the old host's subsequent
                        # active-epoch authorization refuses the wire instead.
                        predecessor_frontier = db.execute(
                            "SELECT body FROM frontier WHERE key=?",
                            (predecessor_access.key,),
                        ).fetchone()
                        _require_containment(
                            predecessor_frontier is not None
                            and bytes(predecessor_frontier[0])
                            == predecessor_snapshot.encoded,
                            "predecessor frontier changed during cutover; reconcile or hold required",
                        )
                        updated = db.execute(
                            "UPDATE scope_epochs "
                            "SET active_epoch=?, active_epoch_store_id=? "
                            "WHERE scope_key=? AND active_epoch=?",
                            (
                                next_epoch,
                                epoch_store_id,
                                scope_key,
                                predecessor_token.epoch,
                            ),
                        )
                        _require_containment(
                            updated.rowcount == 1,
                            "predecessor token does not own active writer epoch",
                        )
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
            except BaseException:
                self._grants.pop(grant_id, None)
                _ISSUED_TOKENS.discard(successor_token)
                # Epoch keys are shared by independent contenders. A failed
                # attempt does not own the durable rows: deleting them could
                # erase another writer's root, history, or confirmed frontier.
                # Retain partial initialization as evidence and fail closed;
                # there is no implicit reenrollment or accounting reset.
                raise
            return successor_token, snapshot

    def active_epoch(
        self, *, base_store_id: str, repository_id: int, pull_request: int
    ) -> int:
        scope_key = self._scope_key(base_store_id, repository_id, pull_request)
        with self._lock, closing(self._connect_frontier()) as db:
            row = db.execute(
                "SELECT active_epoch FROM scope_epochs "
                "WHERE scope_key=? AND base_store_id=? AND repository_id=? AND pull_request=?",
                (scope_key, base_store_id, repository_id, pull_request),
            ).fetchone()
        _require_containment(row is not None, "PR writer scope is not enrolled")
        if row is None:
            raise AuthorityRefusal("PR writer scope is not enrolled")
        return int(row[0])

    def verify_external_epoch_floor(
        self,
        *,
        base_store_id: str,
        repository_id: int,
        pull_request: int,
        observed_external_epoch: int,
    ) -> int:
        """Compare one local snapshot against a supplied epoch floor.

        This diagnostic does not authenticate the supplied integer or establish
        writer authority. A deployment must obtain an authenticated witness
        separately; the local checkpoint belongs to the observed local epoch.
        """

        _require_containment(
            type(observed_external_epoch) is int and observed_external_epoch >= 1,
            "observed external epoch floor must be a positive integer",
        )
        scope_key = self._scope_key(base_store_id, repository_id, pull_request)
        with self._lock, closing(self._connect_frontier()) as db:
            # One read transaction prevents mixing epochs across independent
            # hosts. Closing the connection releases the snapshot on refusal.
            db.execute("BEGIN")
            row = db.execute(
                "SELECT active_epoch, active_epoch_store_id FROM scope_epochs "
                "WHERE scope_key=? AND base_store_id=? AND repository_id=? AND pull_request=?",
                (scope_key, base_store_id, repository_id, pull_request),
            ).fetchone()
            _require_containment(row is not None, "PR writer scope is not enrolled")
            if row is None:
                raise AuthorityRefusal("PR writer scope is not enrolled")
            active_epoch = int(row[0])
            active_epoch_store_id = str(row[1])
            expected_store_id = derive_epoch_store_id(
                base_store_id, repository_id, pull_request, active_epoch
            )
            _require_containment(
                active_epoch_store_id == expected_store_id,
                "enrolled writer epoch store derivation conflicts",
            )
            _require_containment(
                active_epoch >= observed_external_epoch,
                "durable frontier regressed behind externally observed epoch",
            )
            frontier_key = f"{active_epoch_store_id}:{repository_id}:{pull_request}"
            checkpoint_row = db.execute(
                "SELECT body FROM frontier WHERE key=?", (frontier_key,)
            ).fetchone()
            _require_containment(
                checkpoint_row is not None,
                "enrolled writer frontier checkpoint is missing",
            )
            db.commit()
        return active_epoch

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
                    "SELECT base_store_id, active_epoch, active_epoch_store_id, "
                    "legacy_locked, legacy_blob FROM scope_epochs "
                    "WHERE scope_key=? AND repository_id=? AND pull_request=?",
                    (scope_key, base_access.repository_id, base_access.pull_request),
                ).fetchone()
            _require_containment(row is not None, "PR writer scope is not enrolled")
            if row is None:
                raise AuthorityRefusal("PR writer scope is not enrolled")
            stored_base_store_id = str(row[0])
            active_epoch = int(row[1])
            stored_epoch_store_id = str(row[2])
            legacy_locked = bool(row[3])
            legacy_blob = None if row[4] is None else bytes(row[4])
            self._require_legacy_invariant(legacy_locked, legacy_blob)
            _require_containment(
                stored_base_store_id == base_access.store_id,
                "reattach base store_id conflicts with enrolled PR scope",
            )
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
            _require_containment(
                epoch_store_id == stored_epoch_store_id,
                "enrolled writer epoch store derivation conflicts",
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
                _ISSUED_TOKENS.discard(token)
                raise
            return token

    def _authenticate_token(self, authorization: object) -> Access:
        _require_containment(
            isinstance(authorization, IssuedWriterToken)
            and authorization in _ISSUED_TOKENS
            and authorization._issuer_id == id(self),
            "unqualified caller",
        )
        if not isinstance(authorization, IssuedWriterToken):
            raise AuthorityRefusal("unqualified caller")
        access = self._grants.get(authorization.grant_id)
        if access is None:
            raise AuthorityRefusal("unqualified caller")
        return access

    def _require_active_epoch(self, access: Access, *, operation: str) -> None:
        with closing(self._connect_frontier()) as db:
            rows = db.execute(
                "SELECT scope_key, base_store_id, active_epoch, active_epoch_store_id, "
                "legacy_locked, legacy_blob FROM scope_epochs "
                "WHERE repository_id=? AND pull_request=?",
                (access.repository_id, access.pull_request),
            ).fetchall()
        matched: tuple[str, int, bool, bytes | None] | None = None
        for (
            scope_key,
            base_store_id,
            active_epoch,
            active_epoch_store_id,
            legacy_locked,
            legacy_blob,
        ) in rows:
            blob = None if legacy_blob is None else bytes(legacy_blob)
            if access.epoch == int(active_epoch) and access.store_id == str(
                active_epoch_store_id
            ):
                matched = (
                    str(scope_key),
                    int(active_epoch),
                    bool(legacy_locked),
                    blob,
                )
                break
            expected_store = derive_epoch_store_id(
                str(base_store_id),
                access.repository_id,
                access.pull_request,
                access.epoch,
            )
            if expected_store == access.store_id:
                matched = (
                    str(scope_key),
                    int(active_epoch),
                    bool(legacy_locked),
                    blob,
                )
                break
        if matched is None:
            raise AuthorityRefusal("writer epoch scope is unknown")
        _scope_key, active_epoch, legacy_locked, legacy_blob_bytes = matched
        self._require_legacy_invariant(legacy_locked, legacy_blob_bytes)
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
        with self._lock, closing(self._connect_frontier()) as db:
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
                dispatch_id not in self._attempted_dispatches
                and dispatch_id not in self._wire_outcomes,
                "physical dispatch already forwarded",
            )
            request_bytes = canonical_bytes(binding["body"])
            _require_containment(
                _sha256_hex(request_bytes) == binding["request_sha256"],
                "forwarded request payload diverged from canonical binding",
            )
            self._attempted_dispatches.add(dispatch_id)
        outcome = self._wire_forwarder(
            str(binding["method"]),
            str(binding["target"]),
            request_bytes,
            dict(binding),
        )
        _require_containment(
            isinstance(outcome, WireDispatchOutcome),
            "wire dispatch outcome is invalid",
        )
        with self._lock:
            self._custom_receipts.pop(dispatch_id, None)
            self._wire_outcomes[dispatch_id] = outcome

    def observe(self, access: Access, binding: dict[str, Any]) -> Receipt | None:
        dispatch_id = str(binding["dispatch_id"])
        with self._lock:
            self._require_active_epoch(access, operation="observation")
            if (
                self._synthetic_qualification
                and self._allow_synthetic_overrides
                and not self._live_cutover_enabled
                and dispatch_id in self._custom_receipts
            ):
                return self._custom_receipts[dispatch_id]
            outcome = self._wire_outcomes.get(dispatch_id)
        if outcome is None:
            return None
        binding_digest = _sha256_hex(canonical_bytes(binding))
        if (
            not outcome.response_complete
            or not outcome.effect_confirmed
            or outcome.usage_calls is None
        ):
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
            effect_confirmed=True,
            response_complete=True,
            usage_calls=outcome.usage_calls,
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
        with self._lock:
            outcome = self._wire_outcomes.get(dispatch_id)
        _require_containment(
            outcome is not None
            and outcome.response_complete
            and outcome.effect_confirmed
            and outcome.usage_calls is not None,
            "wire mutation outcome is incomplete or unconfirmed; hold and reconcile required",
        )
        if outcome is None:
            raise AuthorityRefusal(
                "wire mutation outcome is incomplete or unconfirmed; hold and reconcile required"
            )
        settled = self.candidate.reconcile(token, forwarded, dispatch_id)
        return outcome, settled


@dataclass(frozen=True)
class WriterCredentialRouteEntry:
    """Repository-verified writer and credential route descriptor (G02/G08)."""

    route_id: str
    coordinator_route_key: str
    route_class: str
    entry_modules: tuple[str, ...]
    credential_sources: tuple[str, ...]
    github_write_surfaces: tuple[str, ...]
    workflow_concurrency_group: str | None
    cancel_in_progress: bool | None
    pre_cutover_fencing_mechanism: str
    unrestricted_token_bypass_risk: bool


WRITER_CREDENTIAL_ROUTE_INVENTORY: Final[tuple[WriterCredentialRouteEntry, ...]] = (
    WriterCredentialRouteEntry(
        route_id="actions_hosted",
        coordinator_route_key="actions_hosted",
        route_class="contained",
        entry_modules=(
            "review_sensei_github.cli",
            "review_sensei_github.review_run",
            "review_sensei_github.api_run_host",
            "review_sensei_github.api_run_owner",
            "review_sensei_github.api_run_requests",
            "review_sensei_github.publication",
        ),
        credential_sources=(
            "GITHUB_TOKEN/GH_TOKEN (read-only metadata)",
            "Actions OIDC (sts.reviewsensei.dev)",
            "broker:review_actions",
            "broker:review_session",
            "broker:review_publish",
            "broker:review_approve",
            "broker:check_publish",
            "broker:thread_resolve",
        ),
        github_write_surfaces=(
            "POST/PATCH /repos/{owner}/{repo}/issues/{pr}/comments",
            "POST /repos/{owner}/{repo}/pulls/{pr}/reviews",
            "POST/PATCH /repos/{owner}/{repo}/check-runs",
            "POST /graphql (resolveReviewThread)",
        ),
        workflow_concurrency_group="reviewsensei-provider-review-{repository}-{pull_request}",
        cancel_in_progress=False,
        pre_cutover_fencing_mechanism=(
            "Actions job serialization (cancel-in-progress=false), "
            "ApiRunActionsEvidence cessation proof, and optimistic root_sha256 readback"
        ),
        unrestricted_token_bypass_risk=True,
    ),
    WriterCredentialRouteEntry(
        route_id="actions_local",
        coordinator_route_key="actions_local",
        route_class="contained",
        entry_modules=(
            "review_sensei_github.cli",
            "review_sensei_github.review_run",
            "review_sensei_github.api_run_host",
            "review_sensei_github.api_run_owner",
            "review_sensei_github.api_run_requests",
            "review_sensei_github.publication",
        ),
        credential_sources=(
            "GITHUB_TOKEN/GH_TOKEN (read-only metadata)",
            "Actions OIDC (sts.reviewsensei.dev)",
            "broker:review_actions",
            "broker:review_session",
            "broker:review_publish",
            "broker:review_approve",
            "broker:check_publish",
            "broker:thread_resolve",
        ),
        github_write_surfaces=(
            "POST/PATCH /repos/{owner}/{repo}/issues/{pr}/comments",
            "POST /repos/{owner}/{repo}/pulls/{pr}/reviews",
            "POST/PATCH /repos/{owner}/{repo}/check-runs",
            "POST /graphql (resolveReviewThread)",
        ),
        workflow_concurrency_group="reviewsensei-provider-review-{repository}-{pull_request}",
        cancel_in_progress=False,
        pre_cutover_fencing_mechanism=(
            "Shared PR provider concurrency group (cancel-in-progress=false) with "
            "hosted/command, ApiRunActionsEvidence sibling-job cessation, and root_sha256 check"
        ),
        unrestricted_token_bypass_risk=True,
    ),
    WriterCredentialRouteEntry(
        route_id="actions_command",
        coordinator_route_key="actions_command",
        route_class="contained",
        entry_modules=(
            "review_sensei_github.cli",
            "review_sensei_github.action_run",
            "review_sensei_github.review_run",
            "review_sensei_github.api_run_owner",
            "review_sensei_github.api_run_requests",
            "review_sensei_github.application",
        ),
        credential_sources=(
            "GITHUB_TOKEN/GH_TOKEN (read-only metadata)",
            "Actions OIDC (sts.reviewsensei.dev)",
            "broker:review_actions",
            "broker:review_session",
            "broker:review_publish",
            "broker:review_approve",
            "broker:check_publish",
            "broker:thread_resolve",
        ),
        github_write_surfaces=(
            "POST/PATCH /repos/{owner}/{repo}/issues/{pr}/comments",
            "POST /repos/{owner}/{repo}/pulls/{pr}/reviews",
            "POST/PATCH /repos/{owner}/{repo}/check-runs",
            "POST /graphql (resolveReviewThread)",
        ),
        workflow_concurrency_group="reviewsensei-provider-review-{repository}-{pull_request}",
        cancel_in_progress=False,
        pre_cutover_fencing_mechanism=(
            "Shared PR provider concurrency group (cancel-in-progress=false), "
            "VerifiedRunAction live head/source fence, and ActionRunLedger intent/effect checkpoints"
        ),
        unrestricted_token_bypass_risk=True,
    ),
    WriterCredentialRouteEntry(
        route_id="actions_reply",
        coordinator_route_key="actions_hosted",
        route_class="contained",
        entry_modules=(
            "review_sensei_github.cli",
            "review_sensei_github.action_run",
            "review_sensei_github.action_reply",
            "review_sensei_github.conversation",
            "review_sensei_github.api_run_owner",
            "review_sensei_github.api_run_requests",
        ),
        credential_sources=(
            "GITHUB_TOKEN/GH_TOKEN (read-only metadata)",
            "Actions OIDC (sts.reviewsensei.dev)",
            "broker:review_actions",
            "broker:review_session",
            "broker:mention_reply",
            "broker:review_publish",
            "broker:review_approve",
            "broker:check_publish",
            "broker:thread_resolve",
        ),
        github_write_surfaces=(
            "POST/PATCH /repos/{owner}/{repo}/issues/{pr}/comments",
            "POST /repos/{owner}/{repo}/pulls/{pr}/comments/{id}/replies",
            "POST /repos/{owner}/{repo}/pulls/{pr}/reviews",
            "POST/PATCH /repos/{owner}/{repo}/check-runs",
            "POST /graphql (resolveReviewThread)",
        ),
        workflow_concurrency_group="reviewsensei-provider-review-{repository}-{pull_request}",
        cancel_in_progress=False,
        pre_cutover_fencing_mechanism=(
            "Per-run_id workflow group (no reply-on-reply cancellation) plus shared PR "
            "provider job slot (hosted/local, cancel-in-progress=false) and VerifiedRunAction fence"
        ),
        unrestricted_token_bypass_risk=True,
    ),
    WriterCredentialRouteEntry(
        route_id="manual_cli_local_ledger",
        coordinator_route_key="manual_cli",
        route_class="contained",
        entry_modules=(
            "review_sensei.cli",
            "review_sensei_github.cli",
            "review_sensei_github.command_bodies",
        ),
        credential_sources=("none (local filesystem SessionLedger only)",),
        github_write_surfaces=(),
        workflow_concurrency_group=None,
        cancel_in_progress=None,
        pre_cutover_fencing_mechanism=(
            "Local filesystem ledger only; --github-session-ledger is retired on "
            "manual command CLI and core CLI holds no GitHub credentials"
        ),
        unrestricted_token_bypass_risk=False,
    ),
    WriterCredentialRouteEntry(
        route_id="manual_cli_direct_github",
        coordinator_route_key="manual_cli",
        route_class="contained",
        entry_modules=(
            "review_sensei_github.cli",
            "review_sensei_github.command_bodies",
            "review_sensei_github.application",
            "review_sensei_github.publication",
            "review_sensei_github.conversation",
        ),
        credential_sources=(
            "GITHUB_TOKEN/GH_TOKEN (optional read-only metadata)",
            "explicit --oidc-token -> BrokerClient raw capability bearer tokens",
        ),
        github_write_surfaces=(
            "POST/PATCH /repos/{owner}/{repo}/issues/{pr}/comments",
            "POST /repos/{owner}/{repo}/pulls/{pr}/reviews",
            "POST /repos/{owner}/{repo}/pulls/{pr}/comments/{id}/replies",
            "POST/PATCH /repos/{owner}/{repo}/check-runs",
            "POST /repos/{owner}/{repo}/pulls (learning PRs)",
        ),
        workflow_concurrency_group=None,
        cancel_in_progress=None,
        pre_cutover_fencing_mechanism=(
            "Explicit --allow-write + OIDC capability exchange + optimistic session "
            "comment readback; uncontained until manual_cli_raw_write_tokens_disabled=True"
        ),
        unrestricted_token_bypass_risk=True,
    ),
    WriterCredentialRouteEntry(
        route_id="broker_raw_token",
        coordinator_route_key="broker_raw_token",
        route_class="bypass",
        entry_modules=(
            "review_sensei_github.broker_client",
            "review_sensei_github.auth",
        ),
        credential_sources=(
            "BrokerClient OIDC -> raw GitHub App InstallationToken",
            "GitHubAppAuth RS256 App JWT -> GitHubTokenTransport InstallationToken",
        ),
        github_write_surfaces=(
            "Direct bearer Authorization against api.github.com until GitHub token expiry",
        ),
        workflow_concurrency_group=None,
        cancel_in_progress=None,
        pre_cutover_fencing_mechanism=(
            "Unfenced at GitHub HTTP boundary while raw bearer installation tokens "
            "are returned to worker processes; blocked for exclusive native cutover"
        ),
        unrestricted_token_bypass_risk=True,
    ),
    WriterCredentialRouteEntry(
        route_id="pre_cancellation_inflight",
        coordinator_route_key="pre_cancellation_inflight",
        route_class="bypass",
        entry_modules=(
            "review_sensei_github.http",
            "review_sensei_github.api_run_requests",
            "review_sensei_github.publication",
        ),
        credential_sources=(
            "Already dispatched bearer HTTP socket write prior to SIGTERM/cancellation",
        ),
        github_write_surfaces=(
            "In-flight POST/PATCH/GraphQL mutation already transmitted on wire",
        ),
        workflow_concurrency_group=None,
        cancel_in_progress=None,
        pre_cutover_fencing_mechanism=(
            "Cannot be recalled once on wire; mitigated pre-cutover by requiring "
            "ApiRunActionsEvidence physical job completion and permanent uncertain debit"
        ),
        unrestricted_token_bypass_risk=True,
    ),
)


@dataclass(frozen=True)
class CredentialContainmentBoundaryProof:
    """Structural proof of whether epoch advancement fences a delayed writer."""

    holds_unrestricted_github_token: bool
    coordinator_epoch_advanced: bool
    uses_contained_forwarding: bool
    can_fence_delayed_writer: bool
    residual_clobber_possible: bool
    reason: str


def evaluate_side_coordinator_fencing_proof(
    *,
    holds_unrestricted_github_token: bool,
    coordinator_epoch_advanced: bool,
    uses_contained_forwarding: bool,
) -> CredentialContainmentBoundaryProof:
    """Prove why a side coordinator cannot fence a writer holding a raw GitHub token.

    GitHub REST/GraphQL endpoints validate only the bearer installation token's
    signature/expiry and repository permissions, never a ReviewSensei sidecar
    epoch or session-comment generation. Therefore advancing a sidecar epoch
    while any worker retains a raw GitHub bearer token leaves stale direct HTTP
    writes unfenced unless capability-contained forwarding owns the credential.
    """

    for name, flag in (
        ("holds_unrestricted_github_token", holds_unrestricted_github_token),
        ("coordinator_epoch_advanced", coordinator_epoch_advanced),
        ("uses_contained_forwarding", uses_contained_forwarding),
    ):
        _require_containment(type(flag) is bool, f"{name} must be a boolean")
    if holds_unrestricted_github_token:
        return CredentialContainmentBoundaryProof(
            holds_unrestricted_github_token=True,
            coordinator_epoch_advanced=coordinator_epoch_advanced,
            uses_contained_forwarding=uses_contained_forwarding,
            can_fence_delayed_writer=False,
            residual_clobber_possible=True,
            reason=(
                "unrestricted GitHub bearer token authenticates directly to "
                "api.github.com; side-coordinator epoch advancement cannot revoke "
                "or fence delayed direct PATCH/POST requests"
            ),
        )
    if not uses_contained_forwarding:
        return CredentialContainmentBoundaryProof(
            holds_unrestricted_github_token=False,
            coordinator_epoch_advanced=coordinator_epoch_advanced,
            uses_contained_forwarding=False,
            can_fence_delayed_writer=False,
            residual_clobber_possible=True,
            reason=(
                "mutations are not mediated by ContainedForwardingCoordinator; "
                "epoch checks are bypassed on wire transmission"
            ),
        )
    if not coordinator_epoch_advanced:
        return CredentialContainmentBoundaryProof(
            holds_unrestricted_github_token=False,
            coordinator_epoch_advanced=False,
            uses_contained_forwarding=True,
            can_fence_delayed_writer=False,
            residual_clobber_possible=True,
            reason=(
                "contained forwarding is active, but predecessor epoch remains "
                "current until advance_epoch commits on frontier.sqlite"
            ),
        )
    return CredentialContainmentBoundaryProof(
        holds_unrestricted_github_token=False,
        coordinator_epoch_advanced=True,
        uses_contained_forwarding=True,
        can_fence_delayed_writer=True,
        residual_clobber_possible=False,
        reason=(
            "worker holds only an IssuedWriterToken capability and "
            "ContainedForwardingCoordinator rejects the stale epoch before wire dispatch"
        ),
    )


@dataclass(frozen=True)
class PhysicalBudgetGuaranteeSpec:
    """Verified conservation plus a policy minimum, not a funding grant.

    ``write_confirmation_tail`` is the required request cost of a future write
    and its confirmation reads. It is not an observed reservation or available
    capacity. Admission must independently reserve it from original funds.
    Fully consumed terminal states can conserve requests with no capacity left.
    """

    configured_limit: int
    startup_liability: int
    prior_nonstartup_liability: int
    confirmed_requests: int
    uncertain_requests: int
    reserved_undispatched: int
    remaining_unallocated: int
    write_confirmation_tail: int
    conservation_holds: bool
    rollover_blocked_by_uncertain: bool


def evaluate_physical_budget_guarantees(
    *,
    configured_limit: int,
    startup_liability: int,
    prior_nonstartup_liability: int = 0,
    confirmed_requests: int,
    uncertain_requests: int,
    reserved_undispatched: int,
    remaining_unallocated: int,
    write_confirmation_tail: int = 3,
) -> PhysicalBudgetGuaranteeSpec:
    """Verify conservation, not admission or availability of confirmation funds.

    The compatibility field ``write_confirmation_tail`` describes a policy
    minimum only. No reservation is created or inferred by this diagnostic.
    """

    for label, value in (
        ("configured_limit", configured_limit),
        ("startup_liability", startup_liability),
        ("prior_nonstartup_liability", prior_nonstartup_liability),
        ("confirmed_requests", confirmed_requests),
        ("uncertain_requests", uncertain_requests),
        ("reserved_undispatched", reserved_undispatched),
        ("remaining_unallocated", remaining_unallocated),
        ("write_confirmation_tail", write_confirmation_tail),
    ):
        _require_containment(
            type(value) is int and value >= 0,
            f"{label} must be a non-negative integer",
        )
    _require_containment(
        1 <= configured_limit <= (1 << 53) - 1,
        "configured_limit exceeds physical caller allowance bounds",
    )
    _require_containment(
        write_confirmation_tail >= 3,
        "write_confirmation_tail must preserve at least 1 write + 2 confirmation reads",
    )
    occupied = (
        startup_liability
        + prior_nonstartup_liability
        + confirmed_requests
        + uncertain_requests
        + reserved_undispatched
    )
    conservation_holds = occupied + remaining_unallocated == configured_limit
    _require_containment(
        conservation_holds,
        "physical caller request conservation invariant violated",
    )
    return PhysicalBudgetGuaranteeSpec(
        configured_limit=configured_limit,
        startup_liability=startup_liability,
        prior_nonstartup_liability=prior_nonstartup_liability,
        confirmed_requests=confirmed_requests,
        uncertain_requests=uncertain_requests,
        reserved_undispatched=reserved_undispatched,
        remaining_unallocated=remaining_unallocated,
        write_confirmation_tail=write_confirmation_tail,
        conservation_holds=conservation_holds,
        rollover_blocked_by_uncertain=uncertain_requests > 0,
    )


def inspect_writer_credential_inventory(
    *, live_cutover_enabled: bool = LIVE_CANONICAL_CUTOVER_ENABLED
) -> dict[str, object]:
    """Validate completeness of the repository writer/credential route inventory."""

    _require_containment(
        type(live_cutover_enabled) is bool,
        "live_cutover_enabled must be a boolean",
    )
    contained_keys = {
        entry.coordinator_route_key
        for entry in WRITER_CREDENTIAL_ROUTE_INVENTORY
        if entry.route_class == "contained"
    }
    bypass_keys = {
        entry.coordinator_route_key
        for entry in WRITER_CREDENTIAL_ROUTE_INVENTORY
        if entry.route_class == "bypass"
    }
    _require_containment(
        contained_keys == CONTAINED_WRITER_ROUTES,
        "inventory contained route keys diverge from CONTAINED_WRITER_ROUTES",
    )
    _require_containment(
        bypass_keys == BYPASS_WRITER_ROUTES,
        "inventory bypass route keys diverge from BYPASS_WRITER_ROUTES",
    )
    route_ids = [entry.route_id for entry in WRITER_CREDENTIAL_ROUTE_INVENTORY]
    _require_containment(
        len(route_ids) == len(set(route_ids)),
        "duplicate route_id in WRITER_CREDENTIAL_ROUTE_INVENTORY",
    )
    raw_token_routes = tuple(
        entry.route_id
        for entry in WRITER_CREDENTIAL_ROUTE_INVENTORY
        if entry.unrestricted_token_bypass_risk
    )
    return {
        "live_cutover_enabled": live_cutover_enabled,
        "repository_default_cutover_enabled": LIVE_CANONICAL_CUTOVER_ENABLED,
        "contained_route_keys": tuple(sorted(contained_keys)),
        "bypass_route_keys": tuple(sorted(bypass_keys)),
        "route_ids": tuple(route_ids),
        "raw_token_bypass_risk_routes": raw_token_routes,
        "exclusive_cutover_ready_in_default_runtime": (
            live_cutover_enabled and len(raw_token_routes) == 0
        ),
    }
