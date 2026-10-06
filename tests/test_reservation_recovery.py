from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import MaintainerCommand, apply_session_command
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    prepare_review_transaction,
)

try:
    from fake_github_http import json_response, make_http
    from isolated_working_directory import IsolatedWorkingDirectoryMixin
except ImportError:
    from tests.fake_github_http import json_response, make_http
    from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 7, 99)
HEAD = "a" * 40
LIVE_HEAD = "b" * 40
OWNER = {"run_id": "123", "head_sha": HEAD}


def analysis_record(*, owner=True):
    ledger = InMemorySessionLedger()
    prepared = prepare_review_transaction(
        ledger,
        IDENTITY,
        ReviewConvergencePolicy(mode="merge-focused"),
        reservation_id="d" * 64,
        base_sha="c" * 40,
        head_sha=HEAD,
        configuration_digest="e" * 64,
        evidence_digest="f" * 64,
        now=NOW,
    )
    return prepared.record.evolve(
        reservation_owner=OWNER if owner else None,
        operator_paused=True,
        now=NOW,
    )


class Harness:
    def __init__(self, record=None):
        self.record = record or analysis_record()
        self.attestation = {
            "repository": IDENTITY.repository,
            "repository_id": IDENTITY.repository_id,
            "pull_request": IDENTITY.pull_request,
            "head_sha": LIVE_HEAD,
            "run_id": "456",
            "operation": "command",
        }
        self.grants = 0
        self.patches = 0
        self.reads = 0
        self.active = []
        self.live_head = LIVE_HEAD
        self.run = {
            "id": 123,
            "repository": {"id": 99, "full_name": "owner/repo"},
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
            "event": "pull_request",
            "head_sha": HEAD,
            "path": ".github/workflows/review-sensei-review.yml",
            "pull_requests": [{"number": 7}],
            "run_started_at": "2026-10-06T11:59:00Z",
            "updated_at": "2026-10-06T12:01:00Z",
        }
        self.jobs = [
            {
                "run_id": 123,
                "run_attempt": 1,
                "head_sha": HEAD,
                "name": "review-or-reply / hosted",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-10-06T11:59:30Z",
                "completed_at": "2026-10-06T12:00:30Z",
            }
        ]
        self.candidates = [self.run]
        self.on_run_read = lambda: None
        self.on_comments_read = lambda: None
        http, self.calls = make_http([], routes=[(lambda request: True, self.respond)])
        self.http = http
        self.ledger = GitHubIssueCommentSessionLedger(
            http,
            token="session-token",
            app_slug="sensei[bot]",
            broker=self,
            session_grant="g" * 43,
            session_attestation=self.attestation,
            head_sha=LIVE_HEAD,
            actions_read_token="actions-read-token",
        )

    def verify_session_grant(self, grant, attestation):
        self.grants += 1
        return dict(attestation)

    def comment(self):
        return {
            "id": 42,
            "user": {"type": "Bot", "login": "sensei[bot]"},
            "body": render_session_comment(
                repository_id=99, pull_request=7, record=self.record
            ),
        }

    def respond(self, request):
        url = request.full_url
        if request.method == "PATCH":
            self.patches += 1
            self.record = parse_session_comment(
                json.loads(request.data)["body"], identity=IDENTITY
            )
            return json_response(self.comment())
        if "/issues/7/comments" in url:
            self.on_comments_read()
            return json_response([self.comment()])
        if "/pulls/7" in url:
            return json_response({"head": {"sha": self.live_head}})
        if "/actions/runs?status=" in url:
            return json_response(
                {"workflow_runs": self.active, "total_count": len(self.active)}
            )
        if "/actions/runs?head_sha=" in url:
            return json_response(
                {"workflow_runs": self.candidates, "total_count": len(self.candidates)}
            )
        if "/jobs?" in url:
            return json_response({"jobs": self.jobs, "total_count": len(self.jobs)})
        if "/actions/runs/123" in url:
            self.reads += 1
            self.on_run_read()
            return json_response(self.run)
        raise AssertionError(url)

    def continue_review(self):
        return apply_session_command(
            self.ledger,
            IDENTITY,
            MaintainerCommand("continue", actor="maintainer", head_sha=LIVE_HEAD),
            now=NOW,
        )


class ReservationRecoveryTests(unittest.TestCase):
    def test_authenticated_completed_owner_is_recovered_once_atomically(self):
        h = Harness()
        before = h.record
        record, result = h.continue_review()
        self.assertTrue(result.applied)
        self.assertIsNone(record.reservation_id)
        self.assertIsNone(record.reservation_owner)
        self.assertIsNone(record.transaction)
        self.assertEqual(record.generation, before.generation + 1)
        self.assertEqual(record.failed_attempts, 1)
        self.assertEqual(record.failed_attempts_head_sha, HEAD)
        self.assertEqual(
            record.completed_initial_reviews, before.completed_initial_reviews
        )
        self.assertEqual(
            record.completed_verification_rounds, before.completed_verification_rounds
        )
        self.assertEqual(record.expires_at, before.expires_at)
        self.assertFalse(record.operator_paused)
        self.assertEqual(h.grants, 1)
        self.assertEqual(h.patches, 1)
        again, _ = h.continue_review()
        self.assertEqual(again, record)
        self.assertEqual(h.patches, 1)

    def test_lazy_read_capability_is_not_requested_for_unpause_without_hold(self):
        h = Harness(
            SessionRecord.create(IDENTITY, now=NOW).evolve(
                operator_paused=True, now=NOW
            )
        )
        h.ledger._actions_read_token = None
        calls = []
        h.ledger._actions_token_provider = lambda: (
            calls.append("requested") or "actions-read"
        )
        record, _ = h.continue_review()
        self.assertFalse(record.operator_paused)
        self.assertEqual(calls, [])

    def test_publication_checkpoint_is_preserved_by_continuation(self):
        h = Harness()
        tx = h.record.transaction.with_result("e" * 64)
        h.record = h.record.evolve(
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id=tx.reservation_id,
            transaction=tx,
            now=NOW,
        )
        record, _ = h.continue_review()
        self.assertEqual(record.transaction, tx)
        self.assertEqual(record.failed_attempts, 0)
        self.assertEqual(h.reads, 0)

    def test_rerun_after_initial_proof_before_cas_is_rejected(self):
        h = Harness()

        def race():
            if h.grants:
                h.run["status"] = "in_progress"

        h.on_run_read = race
        with self.assertRaisesRegex(ReviewInputError, "active or its identity"):
            h.continue_review()
        self.assertEqual(h.patches, 0)

    def test_concurrent_new_owner_is_preserved_during_second_proof(self):
        h = Harness()

        def race():
            if h.reads == 4:
                h.record = h.record.evolve(
                    generation=h.record.generation + 1,
                    reservation_owner={**OWNER, "run_id": "999"},
                    now=NOW,
                )

        h.on_run_read = race
        with self.assertRaisesRegex(ReviewInputError, "ownership or generation"):
            h.continue_review()
        self.assertEqual(h.patches, 0)
        self.assertEqual(h.record.reservation_owner["run_id"], "999")

    def test_live_or_unknown_owner_state_never_mutates(self):
        for field, value in (
            ("status", "in_progress"),
            ("status", "queued"),
            ("conclusion", None),
            ("run_attempt", True),
            ("repository", {"id": 88, "full_name": "owner/repo"}),
        ):
            with self.subTest(field=field, value=value):
                h = Harness()
                h.run[field] = value
                with self.assertRaises(ReviewInputError):
                    h.continue_review()
                self.assertEqual(h.patches, 0)

    def test_unrelated_active_ci_does_not_block_exact_owner_recovery(self):
        h = Harness()
        h.active = [
            {
                "id": 999,
                "status": "in_progress",
                "path": ".github/workflows/ci.yml",
                "pull_requests": [],
            }
        ]
        record, _ = h.continue_review()
        self.assertIsNone(record.reservation_id)
        self.assertEqual(h.patches, 1)
        self.assertEqual(h.reads, 4)
        self.assertFalse(any("?status=" in url for _, url, _ in h.calls))

    def test_rerun_between_completion_reads_invalidates_proof(self):
        h = Harness()

        def race():
            if h.reads == 2:
                h.run["run_attempt"] = 2

        h.on_run_read = race
        with self.assertRaisesRegex(ReviewInputError, "proof is stale"):
            h.continue_review()
        self.assertEqual(h.patches, 0)

    def test_generation_change_after_proof_never_discards_new_owner(self):
        h = Harness()

        def race():
            if h.grants:
                h.record = h.record.evolve(generation=h.record.generation + 1, now=NOW)
                h.on_comments_read = lambda: None

        h.on_comments_read = race
        with self.assertRaisesRegex(ReviewInputError, "ownership or generation"):
            h.continue_review()
        self.assertEqual(h.patches, 0)
        self.assertIsNotNone(h.record.reservation_id)

    def test_head_advance_before_write_never_mutates(self):
        h = Harness()
        h.live_head = "c" * 40
        with self.assertRaisesRegex(ReviewInputError, "head is stale"):
            h.continue_review()
        self.assertEqual(h.patches, 0)

    def test_legacy_exact_head_unique_failed_run_and_job_recover(self):
        h = Harness(analysis_record(owner=False))
        record, result = h.continue_review()
        self.assertTrue(result.applied)
        self.assertIsNone(record.reservation_id)
        self.assertEqual(record.failed_attempts, 1)

    def test_legacy_later_mutation_cannot_rebind_timestamp_to_another_run(self):
        original = analysis_record(owner=False)
        # A later pause/disposition write moves updated_at but leaves the
        # analysis transaction's generation unchanged. The new timestamp
        # could overlap another failed PR run, so it is no longer origin proof.
        later = NOW + timedelta(minutes=2)
        h = Harness(original.evolve(generation=original.generation + 1, now=later))
        h.run.update(
            run_started_at="2026-10-06T12:01:00Z",
            updated_at="2026-10-06T12:03:00Z",
        )
        h.jobs[0].update(
            started_at="2026-10-06T12:01:00Z",
            completed_at="2026-10-06T12:03:00Z",
        )
        with self.assertRaisesRegex(ReviewInputError, "origin timestamp"):
            apply_session_command(
                h.ledger,
                IDENTITY,
                MaintainerCommand("continue", actor="maintainer", head_sha=LIVE_HEAD),
                now=later,
            )
        self.assertEqual(h.patches, 0)
        self.assertIsNotNone(h.record.reservation_id)

    def test_legacy_ambiguous_or_unbound_evidence_never_mutates(self):
        for defect in (
            "two_runs",
            "manual",
            "wrong_head",
            "two_jobs",
            "wrong_pr",
            "outside_job",
            "successful",
            "active_job",
        ):
            with self.subTest(defect=defect):
                h = Harness(analysis_record(owner=False))
                if defect == "two_runs":
                    h.candidates.append(dict(h.run, id=124))
                if defect == "manual":
                    h.run["event"] = "workflow_dispatch"
                if defect == "wrong_head":
                    h.run["head_sha"] = LIVE_HEAD
                if defect == "two_jobs":
                    h.jobs.append(dict(h.jobs[0]))
                if defect == "wrong_pr":
                    h.run["pull_requests"] = [{"number": 8}]
                if defect == "outside_job":
                    h.jobs[0]["started_at"] = "2026-10-06T12:00:01Z"
                if defect == "successful":
                    h.run["conclusion"] = "success"
                if defect == "active_job":
                    h.jobs[0]["status"] = "in_progress"
                with self.assertRaises(ReviewInputError):
                    h.continue_review()
                self.assertEqual(h.patches, 0)

    def test_missing_owner_and_transaction_fail_closed(self):
        h = Harness(analysis_record(owner=False).evolve(transaction=None, now=NOW))
        with self.assertRaisesRegex(ReviewInputError, "no provable"):
            h.continue_review()
        self.assertEqual(h.patches, 0)

    def test_no_authenticated_command_grant_cannot_recover(self):
        h = Harness()
        h.ledger._broker = None
        with self.assertRaisesRegex(ReviewInputError, "authenticated maintainer"):
            h.continue_review()
        self.assertEqual(h.patches, 0)

    def test_budget_is_saturated_and_same_head_stays_exhausted(self):
        h = Harness(
            analysis_record().evolve(
                failed_attempts=32, failed_attempts_head_sha=HEAD, now=NOW
            )
        )
        record, _ = h.continue_review()
        self.assertEqual(record.failed_attempts, 32)
        self.assertEqual(record.failed_attempts_head_sha, HEAD)

    def test_owner_round_trips_and_cannot_outlive_reservation(self):
        record = analysis_record()
        self.assertEqual(SessionRecord.from_dict(record.to_dict()), record)
        with self.assertRaisesRegex(ReviewInputError, "current held reservation"):
            SessionRecord.create(IDENTITY, now=NOW, reservation_owner=OWNER)
        with self.assertRaisesRegex(ReviewInputError, "current held reservation"):
            SessionRecord.from_dict(
                {**record.to_dict(), "reservation_id": None, "reserved_slot": None}
            )
        with self.assertRaisesRegex(ReviewInputError, "head does not match"):
            record.evolve(reservation_owner={**OWNER, "head_sha": LIVE_HEAD}, now=NOW)
        payload = record.to_dict()
        payload["reservation_owner"] = {**OWNER, "run_id": "999"}
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            SessionRecord.from_dict(payload)

    def test_new_hosted_reservation_stores_broker_owner_in_first_write(self):
        h = Harness(SessionRecord.create(IDENTITY, now=NOW))
        ledger = GitHubIssueCommentSessionLedger(
            h.http,
            token="session-token",
            app_slug="sensei[bot]",
            reservation_owner=OWNER,
        )
        record = ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="d" * 64,
            expected_generation=0,
            head_sha=HEAD,
            now=NOW,
        )
        self.assertEqual(record.reservation_owner, OWNER)
        self.assertEqual(h.patches, 1)

    def test_local_continue_cannot_silently_unlock_held_reservation(self):
        ledger = InMemorySessionLedger()
        ledger.initialize(IDENTITY, now=NOW)
        ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="d" * 64,
            expected_generation=0,
            now=NOW,
        )
        with self.assertRaisesRegex(ReviewInputError, "authenticated Actions"):
            apply_session_command(
                ledger,
                IDENTITY,
                MaintainerCommand("continue", actor="maintainer"),
                now=NOW,
            )


class PreProviderFailureCleanupTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def test_context_failure_before_provider_call_cleans_up_and_counts_once(self):
        diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n keep\n+change\n"
        for transaction in (False, True):
            with (
                self.subTest(transaction=transaction),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                (root / "review.patch").write_text(diff)
                (root / "response.json").write_text('{"summary":"ok","comments":[]}')
                args = [
                    "--diff",
                    str(root / "review.patch"),
                    "--provider",
                    "fixture",
                    "--model",
                    "fixture-v1",
                    "--fixture-response",
                    str(root / "response.json"),
                    "--repository",
                    "owner/repo",
                    "--pull-request",
                    "7",
                    "--base-sha",
                    "c" * 40,
                    "--head-sha",
                    HEAD,
                    "--review-mode",
                    "merge-focused",
                    "--session-ledger",
                    str(root / "ledger"),
                ]
                if transaction:
                    args.append("--transaction")
                with (
                    patch(
                        "review_sensei.cli.build_review_context_selection",
                        side_effect=ReviewInputError(
                            "review context contains too many documents"
                        ),
                    ),
                    patch("review_sensei.cli.ReviewService.run") as provider,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(main(args), 2)
                provider.assert_not_called()
                loaded = LocalSessionLedger(root / "ledger").load(
                    SessionIdentity("owner/repo", 7)
                )
                if transaction:
                    self.assertEqual(loaded.status, "missing")
                else:
                    self.assertIsNone(loaded.record.reservation_id)
                    self.assertEqual(loaded.record.failed_attempts, 1)
