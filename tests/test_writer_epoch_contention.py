"""Independent contenders must not erase a winning writer's durable evidence."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from review_sensei.recovery_authority import AuthorityRefusal
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator,
    WireDispatchOutcome,
    WriterContainmentPosture,
)
from tests.test_stale_ledger_fencing_g02 import _make_access


class WriterEpochContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.effects: list[str] = []

        def wire(_method, target, _body, _binding):
            self.effects.append(target)
            return WireDispatchOutcome(200, b"{}")

        self.coordinator = ContainedForwardingCoordinator(
            self.directory,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=wire,
            synthetic_qualification=True,
        )

    def _contend_in_fresh_process(self, operation: str) -> None:
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            (
                str(root),
                str(root / "src"),
                str(root / "packages/python/review-sensei-github/src"),
                str(root / "tests"),
            )
        )
        script = r"""
import sys
from pathlib import Path
from review_sensei.recovery_authority import AuthorityRefusal
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator, WireDispatchOutcome, WriterContainmentPosture,
)
from tests.test_stale_ledger_fencing_g02 import _make_access

def forbidden_wire(*args):
    raise AssertionError("losing contender must not dispatch")

coordinator = ContainedForwardingCoordinator(
    Path(sys.argv[1]), posture=WriterContainmentPosture.fully_contained(),
    wire_forwarder=forbidden_wire, synthetic_qualification=True,
)
try:
    if sys.argv[2] == "initial":
        coordinator.enroll_initial_writer(
            base_access=_make_access(run_id=43, caller_tag="3"),
            writer_route="actions_hosted",
        )
    else:
        predecessor = coordinator.reattach_active_writer(
            base_access=_make_access(), writer_route="actions_hosted",
        )
        coordinator.advance_epoch(
            predecessor_token=predecessor,
            successor_access=_make_access(run_id=43, caller_tag="3"),
            writer_route="actions_hosted", predecessor_ceased=True,
        )
except AuthorityRefusal as exc:
    assert "canonical identity already exists" in str(exc), str(exc)
    print("refused")
else:
    raise AssertionError("two independent contenders were admitted")
"""
        outcome = subprocess.run(
            [sys.executable, "-c", script, str(self.directory), operation],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertEqual(outcome.stdout.strip(), "refused")

    def _assert_winner_intact(self, token, original) -> None:
        coordinator = self.coordinator
        self.assertEqual(coordinator.candidate.snapshot(token), original)
        self.assertEqual(
            coordinator.frontier(coordinator.authenticate(token).key), original
        )
        with closing(sqlite3.connect(coordinator.authority_path)) as db:
            history = db.execute(
                "SELECT body FROM history WHERE key=? ORDER BY revision",
                (coordinator.authenticate(token).key,),
            ).fetchall()
        self.assertEqual([bytes(row[0]) for row in history], [original.encoded])
        self.assertEqual(self.effects, [])
        _, settled = coordinator.fenced_http_mutation(
            token,
            dispatch_id="winner",
            operation="reply",
            method="POST",
            target="/repos/example/repo/issues/7/comments",
            payload={"body": "once"},
            reservation_sha256="1" * 64,
        )
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(settled.document()["dispatches"]["winner"]["phase"], "settled")
        self.assertGreater(
            settled.document()["held_calls"], original.document()["held_calls"]
        )

    def test_losing_initial_enrollment_preserves_winner_root_and_history(self) -> None:
        initialize = self.coordinator.candidate.initialize

        def interleaved(*args, **kwargs):
            result = initialize(*args, **kwargs)
            self._contend_in_fresh_process("initial")
            return result

        with patch.object(self.coordinator.candidate, "initialize", interleaved):
            token, root = self.coordinator.enroll_initial_writer(
                base_access=_make_access(),
                writer_route="actions_hosted",
            )
        self._assert_winner_intact(token, root)

    def test_losing_successor_preserves_winner_and_predecessor_evidence(self) -> None:
        predecessor, prior = self.coordinator.enroll_initial_writer(
            base_access=_make_access(),
            writer_route="actions_hosted",
        )
        predecessor_key = self.coordinator.authenticate(predecessor).key
        initialize = self.coordinator.candidate.initialize

        def interleaved(*args, **kwargs):
            result = initialize(*args, **kwargs)
            self._contend_in_fresh_process("successor")
            return result

        with patch.object(self.coordinator.candidate, "initialize", interleaved):
            token, root = self.coordinator.advance_epoch(
                predecessor_token=predecessor,
                successor_access=_make_access(run_id=42, caller_tag="2"),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )
        self.assertEqual(token.epoch, 2)
        self.assertEqual(self.coordinator.frontier(predecessor_key), prior)
        self._assert_winner_intact(token, root)

    def test_failure_after_initialize_retains_unactivated_evidence_and_holds_retry(
        self,
    ) -> None:
        initialize = self.coordinator.candidate.initialize
        retained = []

        def fail_after_persist(*args, **kwargs):
            retained.append(initialize(*args, **kwargs))
            raise AuthorityRefusal("activation acknowledgement unavailable")

        with patch.object(self.coordinator.candidate, "initialize", fail_after_persist):
            with self.assertRaisesRegex(
                AuthorityRefusal, "acknowledgement unavailable"
            ):
                self.coordinator.enroll_initial_writer(
                    base_access=_make_access(),
                    writer_route="actions_hosted",
                )
        with closing(sqlite3.connect(self.coordinator.authority_path)) as db:
            roots = db.execute("SELECT body FROM roots").fetchall()
            history = db.execute("SELECT body FROM history").fetchall()
            self.assertEqual(roots, [(retained[0].encoded,)])
            self.assertEqual(history, [(retained[0].encoded,)])
        with self.assertRaisesRegex(
            AuthorityRefusal, "canonical identity already exists"
        ):
            self.coordinator.enroll_initial_writer(
                base_access=_make_access(),
                writer_route="actions_hosted",
            )
        self.assertEqual(self.effects, [])
