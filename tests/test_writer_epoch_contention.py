"""Independent contenders must not erase a winning writer's durable evidence."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from review_sensei.recovery_authority import Access, AuthorityRefusal, Funding
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator,
    WireDispatchOutcome,
    WriterContainmentPosture,
)


def _make_access(
    *,
    run_id: int = 41,
    attempt: int = 1,
    caller_tag: str = "b",
    native_exclusive: bool = True,
) -> Access:
    return Access(
        repository="example/repo",
        repository_id=123,
        pull_request=7,
        store_id="a" * 64,
        epoch=1,
        caller_sha256=caller_tag * 64,
        source_sha256="c" * 64,
        head_sha="d" * 40,
        configuration_sha256="e" * 64,
        run_id=run_id,
        attempt=attempt,
        actor_id=99,
        actor_role="admin",
        workflow_sha="5" * 40,
        policy_commit="6" * 40,
        funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
        native_exclusive=native_exclusive,
    )


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
from review_sensei.recovery_authority import Access, AuthorityRefusal, Funding
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator, WriterContainmentPosture,
)

def make_access(run_id: int, caller_tag: str) -> Access:
    return Access(
        repository="example/repo", repository_id=123, pull_request=7,
        store_id="a" * 64, epoch=1, caller_sha256=caller_tag * 64,
        source_sha256="c" * 64, head_sha="d" * 40, configuration_sha256="e" * 64,
        run_id=run_id, attempt=1, actor_id=99, actor_role="admin",
        workflow_sha="5" * 40, policy_commit="6" * 40,
        funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
        native_exclusive=True,
    )

def forbidden_wire(*args):
    raise AssertionError("losing contender must not dispatch")

coordinator = ContainedForwardingCoordinator(
    Path(sys.argv[1]), posture=WriterContainmentPosture.fully_contained(),
    wire_forwarder=forbidden_wire, synthetic_qualification=True,
)
try:
    if sys.argv[2] == "initial":
        coordinator.enroll_initial_writer(
            base_access=make_access(run_id=43, caller_tag="3"),
            writer_route="actions_hosted",
        )
    else:
        predecessor = coordinator.reattach_active_writer(
            base_access=make_access(run_id=41, caller_tag="b"),
            writer_route="actions_hosted",
        )
        coordinator.advance_epoch(
            predecessor_token=predecessor,
            successor_access=make_access(run_id=43, caller_tag="3"),
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

    def _contend_env(self) -> dict[str, str]:
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            (
                str(root),
                str(root / "src"),
                str(root / "packages/python/review-sensei-github/src"),
            )
        )
        return env

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
        from review_sensei.writer_containment import derive_epoch_store_id

        predecessor, prior = self.coordinator.enroll_initial_writer(
            base_access=_make_access(),
            writer_route="actions_hosted",
        )
        predecessor_key = self.coordinator.authenticate(predecessor).key
        child_script = r"""
import sys, time
from pathlib import Path
from review_sensei.recovery_authority import Access, AuthorityRefusal, Funding
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator, WriterContainmentPosture,
)

def make_access(run_id: int, caller_tag: str) -> Access:
    return Access(
        repository="example/repo", repository_id=123, pull_request=7,
        store_id="a" * 64, epoch=1, caller_sha256=caller_tag * 64,
        source_sha256="c" * 64, head_sha="d" * 40, configuration_sha256="e" * 64,
        run_id=run_id, attempt=1, actor_id=99, actor_role="admin",
        workflow_sha="5" * 40, policy_commit="6" * 40,
        funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
        native_exclusive=True,
    )

directory = Path(sys.argv[1])
coord = ContainedForwardingCoordinator(
    directory, posture=WriterContainmentPosture.fully_contained(),
    wire_forwarder=lambda *_: (_ for _ in ()).throw(AssertionError("no wire")),
    synthetic_qualification=True,
)
pred = coord.reattach_active_writer(
    base_access=make_access(41, "b"), writer_route="actions_hosted",
)
orig_init = coord.candidate.initialize
def pause_after_init(*args, **kwargs):
    snap = orig_init(*args, **kwargs)
    (directory / "loser-initialized").write_text("1", encoding="ascii")
    deadline = time.monotonic() + 15
    while not (directory / "winner-activated").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("parent activation timeout")
        time.sleep(0.01)
    return snap

coord.candidate.initialize = pause_after_init  # type: ignore[method-assign]
try:
    coord.advance_epoch(
        predecessor_token=pred,
        successor_access=make_access(43, "3"),
        writer_route="actions_hosted", predecessor_ceased=True,
    )
except AuthorityRefusal as exc:
    assert "predecessor token does not own active writer epoch" in str(exc), str(exc)
    print("refused")
else:
    raise AssertionError("losing contender activated after winner committed")
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", child_script, str(self.directory)],
            env=self._contend_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not (self.directory / "loser-initialized").exists():
                if proc.poll() is not None or time.monotonic() >= deadline:
                    _, stderr = proc.communicate(timeout=5)
                    self.fail(f"loser child failed before initialize: {stderr}")
                time.sleep(0.01)

            token, root = self.coordinator.advance_epoch(
                predecessor_token=predecessor,
                successor_access=_make_access(run_id=42, caller_tag="2"),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )
            (self.directory / "winner-activated").write_text("1", encoding="ascii")
            stdout, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "refused")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        self.assertEqual(token.epoch, 3)
        self.assertEqual(
            self.coordinator.active_epoch(
                base_store_id="a" * 64, repository_id=123, pull_request=7
            ),
            3,
        )
        loser_key = f"{derive_epoch_store_id('a' * 64, 123, 7, 2)}:123:7"
        self.assertIsNotNone(self.coordinator.frontier(loser_key))
        self.assertEqual(self.coordinator.frontier(predecessor_key), prior)
        self._assert_winner_intact(token, root)

    def test_barrier_synchronized_multiprocess_advance_epoch_race(self) -> None:
        base_access = _make_access()
        predecessor, prior = self.coordinator.enroll_initial_writer(
            base_access=base_access,
            writer_route="actions_hosted",
        )
        predecessor_key = self.coordinator.authenticate(predecessor).key
        worker_script = r"""
import sys, time
from pathlib import Path
from review_sensei.recovery_authority import Access, AuthorityRefusal, Funding
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator, WriterContainmentPosture,
)

def make_access(run_id: int, caller_tag: str) -> Access:
    return Access(
        repository="example/repo", repository_id=123, pull_request=7,
        store_id="a" * 64, epoch=1, caller_sha256=caller_tag * 64,
        source_sha256="c" * 64, head_sha="d" * 40, configuration_sha256="e" * 64,
        run_id=run_id, attempt=1, actor_id=99, actor_role="admin",
        workflow_sha="5" * 40, policy_commit="6" * 40,
        funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
        native_exclusive=True,
    )

directory = Path(sys.argv[1])
worker_idx = sys.argv[2]
coord = ContainedForwardingCoordinator(
    directory, posture=WriterContainmentPosture.fully_contained(),
    wire_forwarder=lambda *_: (_ for _ in ()).throw(AssertionError("no wire")),
    synthetic_qualification=True,
)
pred = coord.reattach_active_writer(
    base_access=make_access(41, "b"), writer_route="actions_hosted",
)
(directory / f"ready-{worker_idx}").write_text("1", encoding="ascii")
deadline = time.monotonic() + 15
while not (directory / "go").exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("barrier timeout")
    time.sleep(0.002)

try:
    tok, _snap = coord.advance_epoch(
        predecessor_token=pred,
        successor_access=make_access(41 + int(worker_idx), str(int(worker_idx) + 1)),
        writer_route="actions_hosted", predecessor_ceased=True,
    )
    print(f"won:{tok.epoch}")
except AuthorityRefusal as exc:
    print(f"refused:{exc}")
"""
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker_script, str(self.directory), str(idx)],
                env=self._contend_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for idx in (1, 2)
        ]
        try:
            deadline = time.monotonic() + 15
            while not all((self.directory / f"ready-{idx}").exists() for idx in (1, 2)):
                if (
                    any(p.poll() is not None for p in procs)
                    or time.monotonic() >= deadline
                ):
                    outputs = [p.communicate(timeout=5) for p in procs]
                    self.fail(f"worker exited before barrier: {outputs}")
                time.sleep(0.01)
            (self.directory / "go").write_text("1", encoding="ascii")
            results = []
            for p in procs:
                stdout, stderr = p.communicate(timeout=15)
                self.assertEqual(p.returncode, 0, stderr)
                results.append(stdout.strip())
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.communicate()

        winners = [r for r in results if r.startswith("won:")]
        losers = [r for r in results if r.startswith("refused:")]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIn("predecessor token does not own active writer epoch", losers[0])
        winning_epoch = int(winners[0].split(":", 1)[1])
        self.assertIn(winning_epoch, {2, 3})
        self.assertEqual(
            self.coordinator.active_epoch(
                base_store_id="a" * 64, repository_id=123, pull_request=7
            ),
            winning_epoch,
        )
        self.assertEqual(self.coordinator.frontier(predecessor_key), prior)
        winner_idx = 1 if results[0].startswith("won:") else 2
        winner_tok = self.coordinator.reattach_active_writer(
            base_access=_make_access(
                run_id=41 + winner_idx, caller_tag=str(winner_idx + 1)
            ),
            writer_route="actions_hosted",
        )
        winner_snap = self.coordinator.candidate.snapshot(winner_tok)
        self._assert_winner_intact(winner_tok, winner_snap)

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
