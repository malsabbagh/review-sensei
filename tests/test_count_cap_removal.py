"""Slice C acceptance: legitimate updates are never stopped by a count.

The long-sequence test runs many successive changed-head reviews, each round
through a fresh disk-backed ledger instance, so the former 5/8/32 thresholds are
crossed without any in-memory state carrying over. The upgrade tests start from
a record a count-capped install would have refused -- counters at the retired
ceilings plus a retained one-use grant -- and prove the next eligible run is
admitted while manual pauses, dispositions, and counted history survive.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import apply_session_command, parse_maintainer_command
from review_sensei.errors import ReviewInputError
from review_sensei.outcomes import PUBLIC_DIAGNOSTICS
from review_sensei.sequence import (
    ObservedExecutionMetrics,
    SequenceStep,
    replay_review_sequence,
)
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    complete_session_round,
    prepare_session_round,
    session_reservation_id,
)

FIXED_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)
POLICY = ReviewConvergencePolicy(mode="merge-focused")
# Round numbers one past each retired limit: the old five-round default, the
# old eight-round ceiling, and the old 32-round validation bound.
ROUNDS_PAST_RETIRED_LIMITS = (6, 9, 33)
LONG_SEQUENCE_LENGTH = 40


def _head(round_number: int) -> str:
    return f"{round_number:040x}"


def _reservation(head: str) -> str:
    return session_reservation_id(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        head_sha=head,
        kind="publish",
    )


class LongSequenceTests(unittest.TestCase):
    def test_forty_changed_head_reviews_are_admitted_across_fresh_processes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            kinds: list[str] = []
            for round_number in range(1, LONG_SEQUENCE_LENGTH + 1):
                # A fresh ledger instance per round stands in for a fresh
                # process: every fact of the previous round is read back from
                # disk rather than remembered in this process.
                ledger = LocalSessionLedger(root)
                head = _head(round_number)
                prepared = prepare_session_round(
                    ledger,
                    IDENTITY,
                    POLICY,
                    reservation_id=_reservation(head),
                    now=FIXED_NOW,
                    head_sha=head,
                    latest_head_reviewed=True,
                    coverage_complete=True,
                    independently_approval_eligible=True,
                )
                self.assertTrue(
                    prepared.decision.admit, f"round {round_number} was refused"
                )
                self.assertFalse(prepared.decision.handoff)
                self.assertTrue(prepared.decision.may_emit_approve)
                kinds.append(prepared.decision.round_kind)
                complete_session_round(
                    ledger, IDENTITY, prepared, published=True, now=FIXED_NOW
                )
            self.assertEqual(kinds[0], "initial")
            self.assertEqual(set(kinds[1:]), {"verification"})
            for round_number in ROUNDS_PAST_RETIRED_LIMITS:
                self.assertEqual(kinds[round_number - 1], "verification")
            loaded = LocalSessionLedger(root).load(IDENTITY, now=FIXED_NOW)
            self.assertEqual(loaded.record.completed_initial_reviews, 1)
            # Round one is the initial review; rounds two through forty are the
            # verification rounds a count cap used to stop.
            self.assertEqual(
                loaded.record.completed_verification_rounds,
                LONG_SEQUENCE_LENGTH - 1,
            )
            # No round needed a replenishment grant, so none was ever stored.
            self.assertEqual(loaded.record.continuation_grants, ())


class OldExhaustedStateUpgradeTests(unittest.TestCase):
    @staticmethod
    def _grant() -> dict[str, object]:
        return {
            "command_id": "comment-1",
            "actor": "alice",
            "head_sha": "a" * 40,
            "policy_digest": "b" * 64,
            "issued_at": "2026-09-19T12:00:00Z",
            "expires_at": "2026-09-19T13:30:00Z",
            "consumed_reservation_id": None,
            "consumed_generation": None,
        }

    def _exhausted_old_record(self, root: Path, *, paused: bool) -> None:
        """Persist a record a count-capped install would have refused."""

        ledger = LocalSessionLedger(root)
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=8,
            continuation_grants=[self._grant()],
        )
        if paused:
            record = record.evolve(
                now=FIXED_NOW, generation=record.generation + 1, operator_paused=True
            )
        ledger._write(IDENTITY, record)
        dismiss = parse_maintainer_command(
            "@sensei dismiss abcd1234abcd1234 --reason accepted", actor="alice"
        )
        apply_session_command(ledger, IDENTITY, dismiss, now=FIXED_NOW)

    def test_old_exhausted_session_admits_a_changed_head_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._exhausted_old_record(root, paused=False)
            ledger = LocalSessionLedger(root)
            before = ledger.load(IDENTITY, now=FIXED_NOW).record
            self.assertEqual(before.completed_verification_rounds, 8)
            self.assertEqual(len(before.dispositions), 1)
            self.assertEqual(len(before.continuation_grants), 1)

            head = "c" * 40
            prepared = prepare_session_round(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=_reservation(head),
                now=FIXED_NOW,
                head_sha=head,
                latest_head_reviewed=True,
                coverage_complete=True,
                independently_approval_eligible=True,
            )
            self.assertTrue(prepared.decision.admit)
            self.assertEqual(prepared.decision.round_kind, "verification")
            self.assertFalse(prepared.decision.handoff)
            self.assertTrue(prepared.decision.may_emit_approve)
            complete_session_round(
                ledger, IDENTITY, prepared, published=True, now=FIXED_NOW
            )
            resumed = ledger.load(IDENTITY, now=FIXED_NOW).record
            # The obsolete allowance is gone, not the history behind it.
            self.assertEqual(resumed.completed_initial_reviews, 1)
            self.assertEqual(resumed.completed_verification_rounds, 9)
            self.assertEqual(resumed.dispositions, before.dispositions)
            self.assertEqual(resumed.continuation_grants, before.continuation_grants)
            self.assertFalse(resumed.operator_paused)

    def test_manual_pause_and_dispositions_survive_the_upgrade(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._exhausted_old_record(root, paused=True)
            ledger = LocalSessionLedger(root)
            blocked = prepare_session_round(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=_reservation("c" * 40),
                now=FIXED_NOW,
                head_sha="c" * 40,
                latest_head_reviewed=True,
                coverage_complete=True,
                independently_approval_eligible=True,
            )
            # An independent maintainer pause is not the obsolete reason and
            # must keep blocking the session.
            self.assertFalse(blocked.decision.admit)
            self.assertTrue(blocked.decision.handoff)
            self.assertEqual(blocked.decision.handoff_reason, "paused")

            resume = parse_maintainer_command("@sensei review continue", actor="alice")
            _record, result = apply_session_command(
                ledger, IDENTITY, resume, now=FIXED_NOW
            )
            self.assertTrue(result.applied)
            self.assertEqual(
                result.summary, "automated review may continue; rounds are uncapped"
            )

            head = "d" * 40
            resumed = prepare_session_round(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=_reservation(head),
                now=FIXED_NOW,
                head_sha=head,
                latest_head_reviewed=True,
                coverage_complete=True,
                independently_approval_eligible=True,
            )
            self.assertTrue(resumed.decision.admit)
            loaded = ledger.load(IDENTITY, now=FIXED_NOW).record
            self.assertFalse(loaded.operator_paused)
            self.assertEqual(loaded.completed_verification_rounds, 8)
            self.assertEqual(len(loaded.dispositions), 1)

    def test_counters_beyond_the_old_thresholds_are_valid_history(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = LocalSessionLedger(root)
            record = SessionRecord.create(
                IDENTITY,
                now=FIXED_NOW,
                completed_initial_reviews=1000,
                completed_verification_rounds=1000,
            )
            ledger._write(IDENTITY, record)
            round_tripped = SessionRecord.from_dict(record.to_dict())
            self.assertEqual(round_tripped.completed_verification_rounds, 1000)
            head = "e" * 40
            prepared = prepare_session_round(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=_reservation(head),
                now=FIXED_NOW,
                head_sha=head,
                latest_head_reviewed=True,
                coverage_complete=True,
            )
            # Counters far above the retired thresholds are reported history:
            # they neither refuse the round nor stand in for approval.
            self.assertTrue(prepared.decision.admit)
            self.assertEqual(prepared.decision.round_kind, "verification")
            self.assertFalse(prepared.decision.may_emit_approve)

        with self.assertRaisesRegex(ReviewInputError, "out of bounds"):
            SessionRecord.create(
                IDENTITY,
                now=FIXED_NOW,
                completed_verification_rounds=1_000_001,
            )

    def test_retired_budget_diagnostic_is_not_public_vocabulary(self):
        self.assertNotIn("round-budget-exhausted", PUBLIC_DIAGNOSTICS)


class PerHeadRetryBoundTests(unittest.TestCase):
    def test_a_changed_head_starts_a_fresh_retry_bound(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stale_head = "a" * 40
            ledger = LocalSessionLedger(root)
            record = SessionRecord.create(
                IDENTITY,
                now=FIXED_NOW,
                completed_initial_reviews=1,
                completed_verification_rounds=8,
                failed_attempts=6,
                failed_attempts_head_sha=stale_head,
            )
            ledger._write(IDENTITY, record)

            # The retry bound belongs to the head that charged it: a changed
            # head starts a fresh budget instead of inheriting a lock.
            new_head = "b" * 40
            prepared = prepare_session_round(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=_reservation(new_head),
                now=FIXED_NOW,
                head_sha=new_head,
                latest_head_reviewed=True,
                coverage_complete=True,
                independently_approval_eligible=True,
            )
            self.assertTrue(prepared.decision.admit)
            self.assertFalse(prepared.decision.handoff)
            complete_session_round(
                ledger, IDENTITY, prepared, published=True, now=FIXED_NOW
            )
            loaded = ledger.load(IDENTITY, now=FIXED_NOW).record
            self.assertEqual(loaded.completed_verification_rounds, 9)
            self.assertEqual(loaded.failed_attempts, 0)
            self.assertEqual(loaded.failed_attempts_head_sha, new_head)

            # Every head keeps its own logical-review admission, so the stale
            # head is reviewable again rather than permanently locked out.
            revisited = prepare_session_round(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=_reservation(stale_head),
                now=FIXED_NOW,
                head_sha=stale_head,
                latest_head_reviewed=True,
                coverage_complete=True,
                independently_approval_eligible=True,
            )
            self.assertTrue(revisited.decision.admit)
            self.assertEqual(revisited.decision.round_kind, "verification")


class DiagnosticRepresentationTests(unittest.TestCase):
    def test_a_forty_round_replay_serializes_past_the_retired_bound(self):
        steps = [
            SequenceStep(
                label=f"round-{round_number}",
                head_sha=_head(round_number),
                blocking_identities=(),
            )
            for round_number in range(1, LONG_SEQUENCE_LENGTH + 1)
        ]
        report = replay_review_sequence(steps, POLICY)
        payload = report.to_dict()
        # The retired 32-round validation bound is gone from the report
        # representation too, not only from admission: a sequence past it is
        # reported as counted history.
        self.assertEqual(len(payload["steps"]), LONG_SEQUENCE_LENGTH)
        self.assertEqual(
            payload["completed_verification_rounds"], LONG_SEQUENCE_LENGTH - 1
        )
        self.assertTrue(all(step["admit"] for step in payload["steps"]))

    def test_observed_execution_metrics_carry_history_past_the_retired_bound(self):
        metrics = ObservedExecutionMetrics(
            completed_rounds=LONG_SEQUENCE_LENGTH - 1,
            handoffs=0,
            provider_calls=4,
            failed_attempts=0,
        )
        self.assertEqual(
            metrics.to_dict()["completed_rounds"], LONG_SEQUENCE_LENGTH - 1
        )
        with self.assertRaisesRegex(
            ReviewInputError, "observed completed rounds are invalid"
        ):
            ObservedExecutionMetrics(
                completed_rounds=1_000_001,
                handoffs=0,
                provider_calls=4,
                failed_attempts=0,
            )


if __name__ == "__main__":
    unittest.main()
