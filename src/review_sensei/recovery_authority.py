"""Unconnected prospective authority candidate, never a recovery capability.

SQLite supplies local transactions only. The service-owned host must independently
authenticate callers/funding, maintain a nonrollback frontier, contain credentials
and account for its complete physical protocol. No such deployed host is supplied
here. Snapshot bytes and these dataclasses cannot authorize native operations.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .bounded_evidence import MAX_PARTITION_DECODED_BYTES, canonical_bytes
from .errors import ReviewInputError

ROOT_BYTES = 32768
RETAINED_BYTES = 8388608
RETAINED_PARTS = 256
COMMENT_ALLOCATIONS = 200
QUERY_BYTES = 8192
RESPONSE_BYTES = 524288
MAX_REVISION = 2147483647
# Downward candidate profile, not an allocation or an additional allowance.
MAX_CALLS = 512
_OPERATIONS = {
    "observation",
    "review_publish",
    "check_publish",
    "approval",
    "thread_resolve",
    "reply",
}


class AuthorityRefusal(ReviewInputError):
    """Candidate prerequisites conflict or remain unqualified."""


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise AuthorityRefusal(reason)


def _sha(value: Any, width: int = 64) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch("[a-f0-9]{" + str(width) + "}", value) is not None
    )


def _integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Funding:
    """Host-authenticated original allocation; serialized claims are insufficient."""

    reference: str
    calls: int
    protected_calls: int
    deadline_ms: int
    retained_bytes: int
    retained_parts: int
    comments: int
    already_used_calls: int = 0


@dataclass(frozen=True)
class Access:
    """Returned only by the configured host verifier, never accepted as a token."""

    repository: str
    repository_id: int
    pull_request: int
    store_id: str
    epoch: int
    caller_sha256: str
    source_sha256: str
    head_sha: str
    configuration_sha256: str
    run_id: int
    attempt: int
    actor_id: int
    actor_role: str
    workflow_sha: str
    policy_commit: str
    funding: Funding
    native_exclusive: bool

    @property
    def key(self) -> str:
        return f"{self.store_id}:{self.repository_id}:{self.pull_request}"


@dataclass(frozen=True)
class Dispatch:
    dispatch_id: str
    operation: str
    method: str
    target: str
    body: bytes
    effect_sha256: str
    reservation_sha256: str
    liability_calls: int
    deadline_ms: int


@dataclass(frozen=True)
class Receipt:
    """Independent exact host observation, not caller-supplied settlement text."""

    binding_sha256: str
    effect_confirmed: bool
    response_complete: bool
    usage_calls: int | None
    response_bytes: int | None


@dataclass(frozen=True)
class Snapshot:
    encoded: bytes

    @property
    def digest(self) -> str:
        return _digest(self.encoded)

    @property
    def native_authority(self) -> bool:
        return False

    def document(self) -> dict[str, Any]:
        return json.loads(self.encoded)


class AuthorityHost(Protocol):
    """Strong host obligations, deliberately without a production implementation.

    frontier/advance must be protected, linearizable, nonrollback and retain the
    exact checkpoint. An uncertain advance or SQLite/frontier divergence holds
    the identity; neither side is automatically repaired from the other. The
    host prices and durably charges all physical calls from original funding.
    forward must use the SAME actual dispatch ID/request and contain credentials.
    observe authenticates exact effect, response and usage independently.
    """

    def authenticate(self, authorization: object) -> Access: ...
    def now_ms(self) -> int: ...
    def frontier(self, key: str) -> Snapshot | None: ...
    def advance(self, key: str, expected: str | None, checkpoint: Snapshot) -> None: ...
    def authorize(self, access: Access, binding: dict[str, Any]) -> None: ...
    def forward(self, access: Access, binding: dict[str, Any]) -> None: ...
    def observe(self, access: Access, binding: dict[str, Any]) -> Receipt | None: ...


class SqliteAuthorityCandidate:
    """Conditional local adapter. Not imported by CLI, broker or session readers.

    No takeover/reset/migration/cancellation or funding replenishment API exists.
    Logical replicated payload limits below exclude SQLite file overhead. They
    are not a deployed-capacity certificate. A real host still must independently
    price startup, authentication, pagination, frontier and forwarding calls.
    """

    def __init__(self, path: Path, host: AuthorityHost):
        self.path, self.host = path, host
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS roots(key TEXT PRIMARY KEY, body BLOB NOT NULL, legacy BLOB);
                CREATE TABLE IF NOT EXISTS history(key TEXT NOT NULL, revision INTEGER NOT NULL,
                    body BLOB NOT NULL, PRIMARY KEY(key, revision));
            """)

    def _connect(self):
        return sqlite3.connect(self.path, isolation_level=None, timeout=5)

    def _access(self, authorization: object) -> Access:
        a = self.host.authenticate(authorization)
        _require(
            type(a) is Access and type(a.funding) is Funding,
            "caller or funding issuer is unqualified",
        )
        f = a.funding
        _require(
            isinstance(a.repository, str)
            and re.fullmatch(
                r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}", a.repository
            )
            is not None
            and all(
                _integer(v, 1, (1 << 53) - 1)
                for v in (a.repository_id, a.pull_request, a.run_id, a.actor_id)
            )
            and _integer(a.attempt, 1, MAX_REVISION)
            and _integer(a.epoch, 1, MAX_REVISION)
            and all(
                _sha(v)
                for v in (
                    a.store_id,
                    a.caller_sha256,
                    a.source_sha256,
                    a.configuration_sha256,
                    f.reference,
                )
            )
            and _sha(a.head_sha, 40)
            and _sha(a.workflow_sha, 40)
            and _sha(a.policy_commit, 40)
            and isinstance(a.actor_role, str)
            and a.actor_role in {"read", "write", "maintain", "admin"}
            and type(a.native_exclusive) is bool
            and _integer(f.calls, 1, MAX_CALLS)
            and _integer(f.protected_calls, 1, f.calls)
            and _integer(f.already_used_calls, 0, f.calls)
            and _integer(f.deadline_ms, 1, 253402300799999)
            and _integer(f.retained_bytes, 0, RETAINED_BYTES)
            and _integer(f.retained_parts, 0, RETAINED_PARTS)
            and _integer(f.comments, 0, COMMENT_ALLOCATIONS),
            "caller or original allocation is invalid",
        )
        now = self.host.now_ms()
        _require(
            _integer(now, 1, 253402300799999) and now < f.deadline_ms,
            "original allocation deadline expired or clock unavailable",
        )
        return a

    def _load(self, db, a: Access) -> tuple[Snapshot, bytes | None]:
        row = db.execute(
            "SELECT body,legacy FROM roots WHERE key=?", (a.key,)
        ).fetchone()
        _require(
            row is not None,
            "canonical root is unavailable; reenrollment is unsupported",
        )
        snapshot = Snapshot(bytes(row[0]))
        value = snapshot.document()
        _require(
            value["access"] == asdict(a), "original caller, epoch or funding changed"
        )
        frontier = self.host.frontier(a.key)
        _require(
            type(frontier) is Snapshot and frontier == snapshot,
            "nonrollback frontier is unavailable or divergent",
        )
        legacy = None if row[1] is None else bytes(row[1])
        _require(
            value["legacy_sha256"] == (None if legacy is None else _digest(legacy)),
            "original legacy evidence changed",
        )
        return snapshot, legacy

    def _fit(self, db, a: Access, value, legacy):
        encoded = canonical_bytes(value)
        projected = json.loads(encoded)
        # A truthful effect-only intermediate is larger than a settled receipt.
        # Select the largest supported representation independently per item,
        # and reserve the maximum revision width before the first forwarding.
        projected["revision"] = MAX_REVISION
        for item in projected["dispatches"].values():
            variants = [dict(item)]
            for phase, complete, usage, response in (
                ("forwarded", False, None, None),
                ("effect-confirmed", False, None, None),
                ("settled", True, MAX_CALLS, RESPONSE_BYTES),
            ):
                variants.append(
                    dict(
                        item,
                        phase=phase,
                        effect_confirmed=True,
                        response_complete=complete,
                        usage_calls=usage,
                        response_bytes=response,
                        observations=2,
                    )
                )
            item.update(
                max(variants, key=lambda candidate: len(canonical_bytes(candidate)))
            )
        worst = max(len(encoded), len(canonical_bytes(projected)))
        _require(
            worst <= ROOT_BYTES, "canonical root or future outcome exceeds its bound"
        )
        sizes = [
            len(row[0])
            for row in db.execute("SELECT body FROM history WHERE key=?", (a.key,))
        ]
        # Count both local and protected-frontier copies, plus future outcome
        # checkpoints for every retained intent. Neither old domain is free.
        future = 2 * sum(
            3 - min(item["observations"], 2) for item in value["dispatches"].values()
        )
        _require(
            value["revision"] + future <= MAX_REVISION,
            "canonical revision cannot fund future checkpoints",
        )
        sizes += [worst] * (1 + future)
        if legacy is not None:
            sizes.append(len(legacy))
        # The current roots row is distinct from its append-only history row
        # and the frontier copy. Count it even at history-free initialization.
        size = 2 * sum(sizes) + worst + a.funding.retained_bytes
        parts = (
            2 * sum((n + 32255) // 32256 for n in sizes)
            + (worst + 32255) // 32256
            + a.funding.retained_parts
        )
        comments = 2 * len(sizes) + 1 + a.funding.comments
        _require(
            size <= RETAINED_BYTES
            and parts <= RETAINED_PARTS
            and comments <= COMMENT_ALLOCATIONS,
            "original retained capacity cannot fund the complete candidate protocol",
        )
        _require(
            value["held_calls"] + a.funding.protected_calls <= a.funding.calls,
            "original funding cannot preserve the protected tail",
        )
        return Snapshot(encoded)

    def _commit_database(self, db):
        db.commit()

    def _persist(self, db, a, previous, value, legacy):
        _require(
            _integer(value["revision"], 0, MAX_REVISION), "canonical revision exhausted"
        )
        snapshot = self._fit(db, a, value, legacy)
        db.execute(
            "INSERT OR REPLACE INTO roots VALUES(?,?,?)",
            (a.key, snapshot.encoded, legacy),
        )
        db.execute(
            "INSERT INTO history VALUES(?,?,?)",
            (a.key, value["revision"], snapshot.encoded),
        )
        # Frontier first: loss/SQL failure after advance leaves a deliberate
        # mismatch. No retry or automatic repair can rearm old prepared state.
        self.host.advance(
            a.key, None if previous is None else previous.digest, snapshot
        )
        self._commit_database(db)
        return snapshot

    def initialize(self, authorization: object, *, legacy: bytes | None) -> Snapshot:
        a = self._access(authorization)
        _require(
            legacy is None
            or type(legacy) is bytes
            and 0 < len(legacy) <= MAX_PARTITION_DECODED_BYTES,
            "legacy envelope is invalid or oversized",
        )
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                _require(
                    db.execute("SELECT 1 FROM roots WHERE key=?", (a.key,)).fetchone()
                    is None
                    and self.host.frontier(a.key) is None,
                    "canonical identity already exists",
                )
                value = dict(
                    version="recovery-authority-candidate-v1",
                    access=asdict(a),
                    revision=0,
                    legacy_sha256=None if legacy is None else _digest(legacy),
                    held_calls=a.funding.already_used_calls + 2,
                    dispatches={},
                )
                return self._persist(db, a, None, value, legacy)
            except BaseException:
                db.rollback()
                raise

    def snapshot(self, authorization: object) -> Snapshot:
        a = self._access(authorization)
        with closing(self._connect()) as db:
            return self._load(db, a)[0]

    def read_legacy(self, authorization: object) -> bytes | None:
        a = self._access(authorization)
        with closing(self._connect()) as db:
            return self._load(db, a)[1]

    def _change(self, authorization, expected, change: Callable):
        a = self._access(authorization)
        _require(type(expected) is Snapshot, "expected checkpoint is invalid")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current, legacy = self._load(db, a)
                _require(current == expected, "stale canonical checkpoint")
                value = current.document()
                if not change(a, value, legacy):
                    db.rollback()
                    return current
                value["revision"] += 1
                return self._persist(db, a, current, value, legacy)
            except BaseException:
                db.rollback()
                raise

    def _binding(self, a: Access, request: Dispatch) -> dict[str, Any]:
        _require(type(request) is Dispatch, "dispatch is invalid")
        _require(
            isinstance(request.dispatch_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request.dispatch_id) is not None
            and isinstance(request.operation, str)
            and request.operation in _OPERATIONS
            and isinstance(request.method, str)
            and request.method in {"LOCAL", "POST", "PUT", "PATCH"}
            and isinstance(request.target, str)
            and 0 < len(request.target.encode()) <= 1024
            and not any(ord(c) < 32 for c in request.target)
            and type(request.body) is bytes
            and 0 < len(request.body) <= QUERY_BYTES
            and _sha(request.effect_sha256)
            and _sha(request.reservation_sha256)
            and _integer(request.liability_calls, 1, MAX_CALLS)
            and _integer(request.deadline_ms, 1, a.funding.deadline_ms),
            "dispatch binding is invalid",
        )
        _require(
            self.host.now_ms() < request.deadline_ms,
            "original dispatch deadline expired",
        )
        try:
            body = json.loads(request.body)
            _require(
                canonical_bytes(body) == request.body, "request bytes are not canonical"
            )
        except (ValueError, UnicodeError) as exc:
            raise AuthorityRefusal("request body is invalid") from exc
        if request.operation == "observation":
            _require(request.method == "LOCAL", "observation cannot select native HTTP")
        else:
            _require(
                request.method != "LOCAL"
                and request.target.startswith("/repos/" + a.repository + "/"),
                "native request scope conflicts",
            )
        return dict(
            dispatch_id=request.dispatch_id,
            operation=request.operation,
            method=request.method,
            target=request.target,
            body=body,
            request_sha256=_digest(request.body),
            effect_sha256=request.effect_sha256,
            reservation_sha256=request.reservation_sha256,
            liability_calls=request.liability_calls,
            deadline_ms=request.deadline_ms,
            caller=asdict(a),
        )

    def prepare(
        self, authorization: object, expected: Snapshot, request: Dispatch
    ) -> Snapshot:
        def change(a, value, legacy):
            binding = self._binding(a, request)
            _require(
                request.operation == "observation"
                or legacy is None
                and a.native_exclusive,
                "legacy effects or writer exclusion block native actions",
            )
            self.host.authorize(a, binding)
            items = value["dispatches"]
            if request.dispatch_id in items:
                _require(
                    items[request.dispatch_id]["binding"] == binding,
                    "durable dispatch identity conflicts",
                )
                return False
            _require(
                not any(
                    item["binding"]["reservation_sha256"] == request.reservation_sha256
                    for item in items.values()
                ),
                "original reservation is already bound",
            )
            # Conservatively retain the full dispatch liability and twenty
            # candidate control units for consumption/two observations. This is
            # a debit from the original envelope, never a new allocation. Real
            # host pricing/charging remains an independent integration gate.
            value["held_calls"] += request.liability_calls + 20
            items[request.dispatch_id] = dict(
                binding=binding,
                phase="prepared",
                effect_confirmed=False,
                response_complete=False,
                usage_calls=None,
                response_bytes=None,
                observations=0,
            )
            return True

        return self._change(authorization, expected, change)

    def forward(
        self, authorization: object, expected: Snapshot, request: Dispatch
    ) -> Snapshot:
        def change(a, value, legacy):
            binding = self._binding(a, request)
            item = value["dispatches"].get(request.dispatch_id)
            _require(
                item is not None
                and item["binding"] == binding
                and item["phase"] == "prepared",
                "dispatch is absent, changed or already consumed",
            )
            _require(
                request.operation == "observation"
                or legacy is None
                and a.native_exclusive,
                "legacy effects or writer exclusion block native actions",
            )
            item["phase"] = "forwarded"
            return True

        current = self._change(authorization, expected, change)
        a = self._access(authorization)
        _require(self.host.frontier(a.key) == current, "forwarding frontier changed")
        self.host.authorize(
            a, current.document()["dispatches"][request.dispatch_id]["binding"]
        )
        self.host.forward(
            a, current.document()["dispatches"][request.dispatch_id]["binding"]
        )
        return current

    def reconcile(
        self, authorization: object, expected: Snapshot, dispatch_id: str
    ) -> Snapshot:
        def consume(a, value, legacy):
            item = value["dispatches"].get(dispatch_id)
            _require(
                item is not None
                and item["phase"] in {"forwarded", "effect-confirmed"}
                and item["observations"] < 2,
                "dispatch observation is unsupported or exhausted",
            )
            item["observations"] += 1
            return True

        consumed = self._change(authorization, expected, consume)
        a = self._access(authorization)
        receipt = self.host.observe(
            a, consumed.document()["dispatches"][dispatch_id]["binding"]
        )

        def change(a, value, legacy):
            item = value["dispatches"][dispatch_id]
            if receipt is not None:
                _require(
                    type(receipt) is Receipt
                    and receipt.binding_sha256
                    == _digest(canonical_bytes(item["binding"]))
                    and type(receipt.effect_confirmed) is bool
                    and type(receipt.response_complete) is bool,
                    "exact host receipt conflicts",
                )
                _require(
                    not receipt.response_complete
                    or receipt.effect_confirmed
                    and _integer(
                        receipt.usage_calls, 1, item["binding"]["liability_calls"]
                    )
                    and _integer(receipt.response_bytes, 0, RESPONSE_BYTES),
                    "response or usage is unqualified",
                )
                _require(
                    receipt.response_complete
                    or receipt.usage_calls is None
                    and receipt.response_bytes is None,
                    "unknown response cannot manufacture usage",
                )
                item["effect_confirmed"] |= receipt.effect_confirmed
                if receipt.response_complete:
                    item.update(
                        response_complete=True,
                        usage_calls=receipt.usage_calls,
                        response_bytes=receipt.response_bytes,
                    )
                item["phase"] = (
                    "settled"
                    if item["response_complete"]
                    else "effect-confirmed"
                    if item["effect_confirmed"]
                    else "forwarded"
                )
            return True

        return self._change(authorization, consumed, change)
