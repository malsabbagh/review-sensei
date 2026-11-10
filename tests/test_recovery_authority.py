"""Offline candidate only: the host ports below are not deployed authority."""

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from review_sensei.recovery_authority import (
    Access,
    AuthorityRefusal,
    Dispatch,
    Funding,
    Receipt,
    SqliteAuthorityCandidate,
)


def child_environment():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class Host:
    def __init__(self, directory=None):
        self.token = object()
        self.now = 1000
        self.access = Access(
            repository="example/repo",
            repository_id=123,
            pull_request=7,
            store_id="a" * 64,
            epoch=1,
            caller_sha256="b" * 64,
            source_sha256="c" * 64,
            head_sha="d" * 40,
            configuration_sha256="e" * 64,
            run_id=41,
            attempt=1,
            actor_id=99,
            actor_role="admin",
            workflow_sha="5" * 40,
            policy_commit="6" * 40,
            funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
            native_exclusive=False,
        )
        self.frontiers = {}
        self.lock = threading.Lock()
        self.sent = []
        self.receipts = {}
        self.frontier_path = (
            None if directory is None else Path(directory) / "frontier.sqlite"
        )
        if self.frontier_path is not None:
            with closing(sqlite3.connect(self.frontier_path)) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS frontier(key TEXT PRIMARY KEY, body BLOB)"
                )
                db.commit()

    def authenticate(self, token):
        if token is not self.token:
            raise AuthorityRefusal("unqualified caller")
        return self.access

    def now_ms(self):
        return self.now

    def frontier(self, key):
        if self.frontier_path is not None:
            from review_sensei.recovery_authority import Snapshot

            with closing(sqlite3.connect(self.frontier_path)) as db:
                row = db.execute(
                    "SELECT body FROM frontier WHERE key=?", (key,)
                ).fetchone()
                return None if row is None else Snapshot(bytes(row[0]))
        with self.lock:
            return self.frontiers.get(key)

    def advance(self, key, expected, checkpoint):
        if self.frontier_path is not None:
            with closing(sqlite3.connect(self.frontier_path)) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT body FROM frontier WHERE key=?", (key,)
                ).fetchone()
                old = None if row is None else hashlib.sha256(row[0]).hexdigest()
                if old != expected:
                    raise AuthorityRefusal("frontier conflict")
                db.execute(
                    "INSERT OR REPLACE INTO frontier VALUES(?,?)",
                    (key, checkpoint.encoded),
                )
                db.commit()
            return
        with self.lock:
            old = self.frontiers.get(key)
            if (None if old is None else old.digest) != expected:
                raise AuthorityRefusal("frontier conflict")
            self.frontiers[key] = checkpoint

    def forward(self, access, binding):
        self.sent.append(copy.deepcopy(binding))

    def authorize(self, access, binding):
        pass  # Synthetic host assumption, not a production authority issuer.

    def observe(self, access, binding):
        return self.receipts.get(binding["dispatch_id"])


class RecoveryAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "authority.sqlite"
        self.host = Host()
        self.store = SqliteAuthorityCandidate(self.path, self.host)
        self.legacy = json.dumps(
            {
                "phase": "publication_succeeded",
                "terminal": None,
                "required": list(range(7)),
                "optional": list(range(7, 17)),
                "unknown_effect": True,
                "usage": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def dispatch(self, **changes):
        fields = dict(
            dispatch_id="original-physical-id",
            operation="observation",
            method="LOCAL",
            target="analysis-observation",
            body=b'{"count":15}',
            effect_sha256="1" * 64,
            reservation_sha256="2" * 64,
            liability_calls=20,
            deadline_ms=90000,
        )
        return Dispatch(**(fields | changes))

    def test_native_actions_remain_blocked_and_originals_survive(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        self.assertEqual(self.store.read_legacy(self.host.token), self.legacy)
        self.assertFalse(root.native_authority)
        with self.assertRaises(AuthorityRefusal):
            self.store.prepare(
                self.host.token,
                root,
                self.dispatch(
                    operation="approval",
                    method="POST",
                    target="/repos/example/repo/pulls/7/reviews",
                ),
            )
        self.assertEqual(self.store.snapshot(self.host.token), root)
        self.assertEqual(self.host.sent, [])

    def test_forwarding_is_durable_and_one_use(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        request = self.dispatch()
        prepared = self.store.prepare(self.host.token, root, request)
        sent = self.store.forward(self.host.token, prepared, request)
        self.assertEqual(self.host.sent[0]["dispatch_id"], request.dispatch_id)
        self.assertEqual(
            sent.document()["dispatches"][request.dispatch_id]["phase"], "forwarded"
        )
        restarted = SqliteAuthorityCandidate(self.path, self.host)
        with self.assertRaises(AuthorityRefusal):
            restarted.forward(self.host.token, sent, request)
        self.assertEqual(len(self.host.sent), 1)
        self.assertEqual(restarted.read_legacy(self.host.token), self.legacy)

    def test_lost_observation_consumes_its_finite_attempt(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        request = self.dispatch()
        prepared = self.store.prepare(self.host.token, root, request)
        sent = self.store.forward(self.host.token, prepared, request)
        with patch.object(self.host, "observe", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.store.reconcile(self.host.token, sent, request.dispatch_id)
        retained = self.store.snapshot(self.host.token)
        self.assertEqual(
            retained.document()["dispatches"][request.dispatch_id]["observations"], 1
        )

    def test_one_original_reservation_cannot_fund_two_dispatch_ids(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        prepared = self.store.prepare(self.host.token, root, self.dispatch())
        with self.assertRaises(AuthorityRefusal):
            self.store.prepare(
                self.host.token, prepared, self.dispatch(dispatch_id="another-id")
            )

    def forwarded(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        request = self.dispatch()
        prepared = self.store.prepare(self.host.token, root, request)
        return self.store.forward(self.host.token, prepared, request), request

    def receipt(self, root, request, *, complete=False, **changes):
        binding = root.document()["dispatches"][request.dispatch_id]["binding"]
        fields = dict(
            binding_sha256=hashlib.sha256(
                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            effect_confirmed=True,
            response_complete=complete,
            usage_calls=3 if complete else None,
            response_bytes=80 if complete else None,
        )
        self.host.receipts[request.dispatch_id] = Receipt(**(fields | changes))

    def test_exact_effect_does_not_settle_response_usage_or_refund_liability(self):
        sent, request = self.forwarded()
        self.receipt(sent, request)
        observed = self.store.reconcile(self.host.token, sent, request.dispatch_id)
        item = observed.document()["dispatches"][request.dispatch_id]
        self.assertEqual(item["phase"], "effect-confirmed")
        self.assertIsNone(item["usage_calls"])
        self.assertFalse(item["response_complete"])
        self.receipt(observed, request, complete=True)
        completed = self.store.reconcile(self.host.token, observed, request.dispatch_id)
        self.assertEqual(
            completed.document()["dispatches"][request.dispatch_id]["phase"], "settled"
        )
        self.assertEqual(
            completed.document()["held_calls"], sent.document()["held_calls"]
        )
        self.assertFalse(completed.native_authority)
        self.assertEqual(self.store.read_legacy(self.host.token), self.legacy)

    def test_absence_never_rearms_forwarding_or_observation(self):
        sent, request = self.forwarded()
        first = self.store.reconcile(self.host.token, sent, request.dispatch_id)
        second = self.store.reconcile(self.host.token, first, request.dispatch_id)
        with self.assertRaises(AuthorityRefusal):
            self.store.reconcile(self.host.token, second, request.dispatch_id)
        with self.assertRaises(AuthorityRefusal):
            self.store.forward(self.host.token, second, request)
        self.assertEqual(
            second.document()["dispatches"][request.dispatch_id]["phase"], "forwarded"
        )

    def test_changed_caller_scope_epoch_source_or_funding_refuses(self):
        original = self.host.access
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        for field, value in (
            ("repository", "foreign/repo"),
            ("repository_id", 456),
            ("pull_request", 8),
            ("store_id", "0" * 64),
            ("epoch", 2),
            ("caller_sha256", "0" * 64),
            ("source_sha256", "0" * 64),
            ("head_sha", "0" * 40),
            ("configuration_sha256", "0" * 64),
            ("run_id", 42),
            ("attempt", 2),
            ("actor_id", 100),
            ("actor_role", "read"),
            ("workflow_sha", "0" * 40),
            ("policy_commit", "0" * 40),
            ("funding", replace(original.funding, reference="0" * 64)),
        ):
            with self.subTest(field=field):
                self.host.access = replace(original, **{field: value})
                with self.assertRaises(AuthorityRefusal):
                    self.store.prepare(self.host.token, root, self.dispatch())
        self.host.access = original
        with self.assertRaises(AuthorityRefusal):
            self.store.snapshot(asdict(original))
        self.assertEqual(self.store.snapshot(self.host.token), root)

    def test_every_original_dispatch_field_is_bound(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        request = self.dispatch()
        prepared = self.store.prepare(self.host.token, root, request)
        for field, value in (
            ("dispatch_id", "different"),
            ("operation", "approval"),
            ("method", "POST"),
            ("target", "another-observation"),
            ("body", b'{"count":16}'),
            ("effect_sha256", "3" * 64),
            ("reservation_sha256", "3" * 64),
            ("liability_calls", 21),
            ("deadline_ms", 95000),
        ):
            with self.subTest(field=field), self.assertRaises(AuthorityRefusal):
                self.store.forward(
                    self.host.token, prepared, replace(request, **{field: value})
                )
        self.assertEqual(self.store.snapshot(self.host.token), prepared)
        self.assertEqual(self.host.sent, [])

    def test_original_deadline_is_not_renewed_by_restart(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        prepared = self.store.prepare(self.host.token, root, self.dispatch())
        self.host.now = 90000
        restarted = SqliteAuthorityCandidate(self.path, self.host)
        with self.assertRaises(AuthorityRefusal):
            restarted.forward(self.host.token, prepared, self.dispatch())
        self.host.now = 100000
        with self.assertRaises(AuthorityRefusal):
            restarted.snapshot(self.host.token)
        self.assertEqual(self.host.sent, [])

    def test_protected_tail_and_capacity_refuse_before_forward(self):
        original = self.host.access
        for funding in (
            replace(original.funding, calls=60),
            replace(original.funding, retained_bytes=8388600),
            replace(original.funding, retained_parts=256),
            replace(original.funding, comments=200),
        ):
            with (
                self.subTest(funding=funding),
                tempfile.TemporaryDirectory() as directory,
            ):
                host = Host()
                host.access = replace(original, funding=funding)
                store = SqliteAuthorityCandidate(
                    Path(directory) / "authority.sqlite", host
                )
                with self.assertRaises(AuthorityRefusal):
                    root = store.initialize(host.token, legacy=self.legacy)
                    store.prepare(host.token, root, self.dispatch())
                self.assertEqual(host.sent, [])

    def test_two_connections_cannot_claim_or_advance_the_same_revision(self):
        stores = (self.store, SqliteAuthorityCandidate(self.path, self.host))

        def claim(store):
            try:
                return store.initialize(self.host.token, legacy=self.legacy)
            except AuthorityRefusal:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            roots = list(pool.map(claim, stores))
        root = next(value for value in roots if value is not None)
        self.assertEqual(sum(value is not None for value in roots), 1)

        def prepare(pair):
            store, index = pair
            try:
                return store.prepare(
                    self.host.token,
                    root,
                    self.dispatch(
                        dispatch_id=f"dispatch-{index}",
                        reservation_sha256=str(index + 3) * 64,
                    ),
                )
            except AuthorityRefusal:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, zip(stores, range(2))))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.store.snapshot(self.host.token).document()["revision"], 1)

    def test_frontier_loss_and_commit_gap_are_held_without_reenrollment(self):
        for fault in ("lost_frontier_ack", "sqlite_commit"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                host = Host()
                store = SqliteAuthorityCandidate(
                    Path(directory) / "authority.sqlite", host
                )
                root = store.initialize(host.token, legacy=self.legacy)
                advance = host.advance

                def lost(*args):
                    advance(*args)
                    raise TimeoutError("synthetic lost frontier acknowledgement")

                target, name, replacement = (
                    (host, "advance", lost)
                    if fault == "lost_frontier_ack"
                    else (
                        store,
                        "_commit_database",
                        lambda db: (_ for _ in ()).throw(OSError()),
                    )
                )
                with (
                    patch.object(target, name, side_effect=replacement),
                    self.assertRaises((TimeoutError, OSError)),
                ):
                    store.prepare(host.token, root, self.dispatch())
                with self.assertRaises(AuthorityRefusal):
                    store.snapshot(host.token)
                with self.assertRaises(AuthorityRefusal):
                    store.initialize(host.token, legacy=self.legacy)
                self.assertEqual(host.sent, [])

    def test_database_restore_cannot_rearm_consumed_dispatch(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        prepared = self.store.prepare(self.host.token, root, self.dispatch())
        saved = Path(self.tmp.name) / "old.sqlite"
        with (
            closing(sqlite3.connect(self.path)) as src,
            closing(sqlite3.connect(saved)) as dst,
        ):
            src.backup(dst)
        self.store.forward(self.host.token, prepared, self.dispatch())
        with (
            closing(sqlite3.connect(saved)) as src,
            closing(sqlite3.connect(self.path)) as dst,
        ):
            src.backup(dst)
        with self.assertRaises(AuthorityRefusal):
            self.store.forward(self.host.token, prepared, self.dispatch())
        with self.assertRaises(AuthorityRefusal):
            self.store.snapshot(self.host.token)
        self.assertEqual(len(self.host.sent), 1)

    def test_process_death_after_consumption_never_allows_resend(self):
        host = Host(self.tmp.name)
        store = SqliteAuthorityCandidate(self.path, host)
        root = store.initialize(host.token, legacy=self.legacy)
        store.prepare(host.token, root, self.dispatch())
        code = """
import os,sys
from pathlib import Path
repository=Path(sys.argv[2]); sys.path[:0]=[str(repository/'tests'),str(repository/'src')]
from test_recovery_authority import Host, RecoveryAuthorityTests
from review_sensei.recovery_authority import SqliteAuthorityCandidate
root=Path(sys.argv[1]); host=Host(root); store=SqliteAuthorityCandidate(root/'authority.sqlite',host)
def die(*args): os._exit(77)
host.forward=die
store.forward(host.token,store.snapshot(host.token),RecoveryAuthorityTests().dispatch())
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                self.tmp.name,
                str(Path(__file__).resolve().parents[1]),
            ],
            env=child_environment(),
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 77, result.stderr.decode())
        retained = store.snapshot(host.token)
        self.assertEqual(
            retained.document()["dispatches"][self.dispatch().dispatch_id]["phase"],
            "forwarded",
        )
        with self.assertRaises(AuthorityRefusal):
            store.forward(host.token, retained, self.dispatch())

    def test_process_death_after_fake_effect_retains_unknown_response(self):
        host = Host(self.tmp.name)
        store = SqliteAuthorityCandidate(self.path, host)
        root = store.initialize(host.token, legacy=self.legacy)
        store.prepare(host.token, root, self.dispatch())
        code = """
import json,os,signal,sys
from pathlib import Path
repository=Path(sys.argv[2]); sys.path[:0]=[str(repository/'tests'),str(repository/'src')]
from test_recovery_authority import Host, RecoveryAuthorityTests
from review_sensei.recovery_authority import SqliteAuthorityCandidate
root=Path(sys.argv[1]); host=Host(root); store=SqliteAuthorityCandidate(root/'authority.sqlite',host)
def effect_then_die(access,binding):
    (root/'fake-effect.json').write_text(json.dumps(binding),encoding='utf-8')
    if os.name != 'nt': os.kill(os.getpid(),signal.SIGKILL)
    os._exit(78)
host.forward=effect_then_die
store.forward(host.token,store.snapshot(host.token),RecoveryAuthorityTests().dispatch())
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                self.tmp.name,
                str(Path(__file__).resolve().parents[1]),
            ],
            env=child_environment(),
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(
            result.returncode, 78 if os.name == "nt" else -9, result.stderr.decode()
        )
        effect = json.loads((Path(self.tmp.name) / "fake-effect.json").read_text())
        self.assertEqual(effect["dispatch_id"], self.dispatch().dispatch_id)
        retained = store.snapshot(host.token)
        item = retained.document()["dispatches"][self.dispatch().dispatch_id]
        self.assertEqual(item["phase"], "forwarded")
        self.assertIsNone(item["usage_calls"])
        self.assertFalse(item["response_complete"])
        with self.assertRaises(AuthorityRefusal):
            store.forward(host.token, retained, self.dispatch())

    def test_settling_one_effect_preserves_another_unknown_dispatch(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        first, second = (
            self.dispatch(),
            self.dispatch(dispatch_id="second", reservation_sha256="3" * 64),
        )
        current = self.store.prepare(self.host.token, root, first)
        current = self.store.prepare(self.host.token, current, second)
        current = self.store.forward(self.host.token, current, first)
        current = self.store.forward(self.host.token, current, second)
        self.receipt(current, first, complete=True)
        settled = self.store.reconcile(self.host.token, current, first.dispatch_id)
        self.assertEqual(
            settled.document()["dispatches"][first.dispatch_id]["phase"], "settled"
        )
        other = settled.document()["dispatches"][second.dispatch_id]
        self.assertEqual(other["phase"], "forwarded")
        self.assertIsNone(other["usage_calls"])
        self.assertEqual(
            current.document()["held_calls"], settled.document()["held_calls"]
        )
        self.assertEqual(self.store.read_legacy(self.host.token), self.legacy)

    def test_57243_byte_original_and_late_legacy_projection_remain_non_authorizing(
        self,
    ):
        envelope = json.loads(self.legacy)
        envelope["padding"] = ""
        base = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        envelope["padding"] = "x" * (57243 - len(base))
        legacy = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(len(legacy), 57243)
        root = self.store.initialize(self.host.token, legacy=legacy)
        request = self.dispatch(body=b'{"complete":true,"files":15,"hunks":46}')
        prepared = self.store.prepare(self.host.token, root, request)
        observed = self.store.forward(self.host.token, prepared, request)
        # A delayed old native projection changes outside this canonical store.
        projection = {"terminal": None}
        projection["terminal"] = "late legacy PATCH"
        self.assertEqual(self.store.snapshot(self.host.token), observed)
        self.assertEqual(self.store.read_legacy(self.host.token), legacy)
        self.assertFalse(observed.native_authority)
        for operation in ("approval", "review_publish"):
            with self.subTest(operation=operation), self.assertRaises(AuthorityRefusal):
                self.store.prepare(
                    self.host.token,
                    observed,
                    self.dispatch(
                        dispatch_id=operation,
                        operation=operation,
                        method="POST",
                        target="/repos/example/repo/pulls/7/reviews",
                        reservation_sha256="4" * 64,
                    ),
                )

    def test_unqualified_authorization_and_invalid_receipts_never_clear_holds(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        with patch.object(
            self.host, "authorize", side_effect=AuthorityRefusal("unfunded")
        ):
            with self.assertRaises(AuthorityRefusal):
                self.store.prepare(self.host.token, root, self.dispatch())
        self.assertEqual(self.store.snapshot(self.host.token), root)
        sent = self.store.forward(
            self.host.token,
            self.store.prepare(self.host.token, root, self.dispatch()),
            self.dispatch(),
        )
        self.receipt(sent, self.dispatch(), complete=True, binding_sha256="0" * 64)
        with self.assertRaises(AuthorityRefusal):
            self.store.reconcile(self.host.token, sent, self.dispatch().dispatch_id)
        retained = self.store.snapshot(self.host.token)
        item = retained.document()["dispatches"][self.dispatch().dispatch_id]
        self.assertEqual(item["observations"], 1)
        self.assertFalse(item["effect_confirmed"])
        self.assertIsNone(item["usage_calls"])

    def test_response_bound_and_missing_usage_refuse_without_refunding(self):
        for fields in (
            dict(response_bytes=524289),
            dict(usage_calls=None),
            dict(effect_confirmed=False),
        ):
            with (
                self.subTest(fields=fields),
                tempfile.TemporaryDirectory() as directory,
            ):
                host, store, path = self.host, self.store, self.path
                self.host = Host()
                self.path = Path(directory) / "authority.sqlite"
                self.store = SqliteAuthorityCandidate(self.path, self.host)
                try:
                    sent, request = self.forwarded()
                    self.receipt(sent, request, complete=True, **fields)
                    with self.assertRaises(AuthorityRefusal):
                        self.store.reconcile(self.host.token, sent, request.dispatch_id)
                    current = self.store.snapshot(self.host.token)
                    self.assertEqual(
                        current.document()["held_calls"], sent.document()["held_calls"]
                    )
                    self.assertEqual(
                        current.document()["dispatches"][request.dispatch_id]["phase"],
                        "forwarded",
                    )
                finally:
                    self.host, self.store, self.path = host, store, path

    def test_opposite_commit_divergence_and_missing_frontier_refuse(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        current = self.store.prepare(self.host.token, root, self.dispatch())
        for frontier in (root, None):
            with self.subTest(frontier=frontier), self.host.lock:
                self.host.frontiers[self.host.access.key] = frontier
            with self.assertRaises(AuthorityRefusal):
                self.store.snapshot(self.host.token)
            with self.assertRaises(AuthorityRefusal):
                self.store.initialize(self.host.token, legacy=self.legacy)
        self.host.frontiers[self.host.access.key] = current

    def test_stale_snapshot_cannot_replay_identical_caller_business_values(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        prepared = self.store.prepare(self.host.token, root, self.dispatch())
        self.assertEqual(root.document()["access"], prepared.document()["access"])
        with self.assertRaises(AuthorityRefusal):
            self.store.prepare(
                self.host.token,
                root,
                self.dispatch(dispatch_id="stale", reservation_sha256="3" * 64),
            )
        self.assertEqual(self.store.snapshot(self.host.token), prepared)

    def test_root_projection_and_query_bounds_refuse_before_native_effects(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        with self.assertRaises(AuthorityRefusal):
            self.store.prepare(self.host.token, root, self.dispatch(body=b"x" * 8193))
        current = root
        for number in range(8):
            request = self.dispatch(
                dispatch_id=f"large-{number}",
                reservation_sha256=str(number + 1) * 64,
                body=json.dumps({"value": "x" * 7000}, separators=(",", ":")).encode(),
            )
            try:
                current = self.store.prepare(self.host.token, current, request)
            except AuthorityRefusal:
                break
        else:
            self.fail("root bound must refuse before eight large requests")
        self.assertEqual(self.store.snapshot(self.host.token), current)
        self.assertEqual(self.host.sent, [])

    def test_largest_effect_only_checkpoint_is_prefunded_before_forwarding(self):
        sent, request = self.forwarded()
        self.receipt(sent, request)
        observed = self.store.reconcile(self.host.token, sent, request.dispatch_id)
        ceiling = len(observed.encoded) - 1
        with tempfile.TemporaryDirectory() as directory:
            host = Host()
            store = SqliteAuthorityCandidate(Path(directory) / "authority.sqlite", host)
            with patch("review_sensei.recovery_authority.ROOT_BYTES", ceiling):
                root = store.initialize(host.token, legacy=self.legacy)
                with self.assertRaises(AuthorityRefusal):
                    store.prepare(host.token, root, request)
            self.assertEqual(host.sent, [])

    def test_future_revision_width_is_prefunded_before_forwarding(self):
        root = self.store.initialize(self.host.token, legacy=self.legacy)
        prepared = self.store.prepare(self.host.token, root, self.dispatch())
        widest = prepared.document()
        widest["revision"] = 2147483647
        ceiling = (
            len(json.dumps(widest, sort_keys=True, separators=(",", ":")).encode()) - 1
        )
        with tempfile.TemporaryDirectory() as directory:
            host = Host()
            store = SqliteAuthorityCandidate(Path(directory) / "authority.sqlite", host)
            with patch("review_sensei.recovery_authority.ROOT_BYTES", ceiling):
                root = store.initialize(host.token, legacy=self.legacy)
                with self.assertRaises(AuthorityRefusal):
                    store.prepare(host.token, root, self.dispatch())
            self.assertEqual(host.sent, [])

    def test_initial_root_history_and_frontier_copies_all_count(self):
        root = self.store.initialize(self.host.token, legacy=None)
        for constant, ceiling in (
            ("RETAINED_BYTES", 3 * len(root.encoded) - 1),
            ("RETAINED_PARTS", 2),
            ("COMMENT_ALLOCATIONS", 2),
        ):
            with (
                self.subTest(constant=constant),
                tempfile.TemporaryDirectory() as directory,
            ):
                host = Host()
                store = SqliteAuthorityCandidate(
                    Path(directory) / "authority.sqlite", host
                )
                with patch("review_sensei.recovery_authority." + constant, ceiling):
                    with self.assertRaises(AuthorityRefusal):
                        store.initialize(host.token, legacy=None)
                self.assertIsNone(host.frontier(host.access.key))
