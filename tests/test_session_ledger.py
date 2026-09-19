from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diagnostics import build_plan, run_doctor
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions
from review_sensei.hosting.github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)
from review_sensei.models import ReviewResult
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    complete_session_round,
    migrate_session_document,
    prepare_session_round,
    session_reservation_id,
)

try:
    from fake_github_http import json_response, make_http
except ImportError:
    from tests.fake_github_http import json_response, make_http


FIXED_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)


class SessionRecordTests(unittest.TestCase):
    def test_create_round_trips_and_rejects_tampering(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        restored = SessionRecord.from_dict(record.to_dict())
        self.assertEqual(restored.generation, 0)
        self.assertEqual(restored.completed_initial_reviews, 0)
        payload = record.to_dict()
        payload["completed_initial_reviews"] = 1
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            SessionRecord.from_dict(payload)

    def test_identity_rejects_head_as_session_key(self):
        with self.assertRaisesRegex(ReviewInputError, "repository"):
            SessionIdentity("owner/repo@deadbeef", 136)

    def test_ttl_bound(self):
        with self.assertRaisesRegex(ReviewInputError, "ttl"):
            SessionRecord.create(IDENTITY, now=FIXED_NOW, ttl=timedelta(days=91))


class LocalSessionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = LocalSessionLedger(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_expired_and_cas(self):
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "missing")
        record = self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        self.assertEqual(record.generation, 0)
        reserved = self.ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="abcd1234",
            expected_generation=0,
            now=FIXED_NOW,
        )
        self.assertEqual(reserved.reservation_id, "abcd1234")
        again = self.ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="abcd1234",
            expected_generation=reserved.generation,
            now=FIXED_NOW,
        )
        self.assertEqual(again.generation, reserved.generation)
        with self.assertRaisesRegex(ReviewInputError, "conflict"):
            self.ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id="ffff1234",
                expected_generation=0,
                now=FIXED_NOW,
            )
        committed = self.ledger.commit(
            IDENTITY,
            reservation_id="abcd1234",
            expected_generation=reserved.generation,
            now=FIXED_NOW,
        )
        self.assertEqual(committed.completed_initial_reviews, 1)
        self.assertIsNone(committed.reservation_id)
        replay = self.ledger.commit(
            IDENTITY,
            reservation_id="abcd1234",
            expected_generation=committed.generation,
            now=FIXED_NOW,
        )
        self.assertEqual(replay.completed_initial_reviews, 1)
        expired = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW - timedelta(days=31),
            ttl=timedelta(days=30),
        )
        self.ledger._write(IDENTITY, expired)
        self.assertEqual(self.ledger.load(IDENTITY, now=FIXED_NOW).status, "expired")

    def test_integrity_failed_does_not_initialize(self):
        record = self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        path = self.ledger._path(IDENTITY)
        payload = record.to_dict()
        payload["failed_attempts"] = 3
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.ledger.load(IDENTITY).status, "integrity-failed")
        with self.assertRaisesRegex(ReviewInputError, "integrity-failed"):
            self.ledger.initialize(IDENTITY, now=FIXED_NOW)

    def test_legacy_v01_migrates_counters(self):
        created = SessionRecord.create(
            IDENTITY, now=FIXED_NOW, completed_initial_reviews=1
        )
        legacy = {
            "schema_version": "0.1",
            "repository": IDENTITY.repository,
            "pull_request_number": IDENTITY.pull_request,
            "repository_id": IDENTITY.repository_id,
            "completed_initial_reviews": 1,
            "completed_verification_rounds": 0,
            "failed_attempts": 0,
            "generation": 0,
            "created_at": created.created_at,
            "expires_at": created.expires_at,
        }
        migrated = migrate_session_document(legacy)
        self.assertEqual(migrated["pull_request"], 136)
        path = self.ledger._path(IDENTITY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "migrated")
        self.assertEqual(loaded.record.completed_initial_reviews, 1)


class RoundPersistenceTests(unittest.TestCase):
    def test_operator_mode_reserves_and_commits_without_refusing(self):
        ledger = InMemorySessionLedger()
        policy = ReviewConvergencePolicy(mode="merge-focused")
        reservation = session_reservation_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            head_sha="a" * 40,
            kind="publish",
        )
        prepared = prepare_session_round(
            ledger, IDENTITY, policy, reservation_id=reservation, now=FIXED_NOW
        )
        self.assertTrue(prepared.decision.admit)
        self.assertEqual(prepared.decision.round_kind, "initial")
        self.assertIsNotNone(prepared.reservation_id)
        complete_session_round(
            ledger, IDENTITY, prepared, published=True, now=FIXED_NOW
        )
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.record.completed_initial_reviews, 1)

    def test_legacy_does_not_count(self):
        ledger = InMemorySessionLedger()
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            ReviewConvergencePolicy(),
            reservation_id="abcd1234",
            now=FIXED_NOW,
        )
        self.assertIsNone(prepared.reservation_id)
        complete_session_round(
            ledger, IDENTITY, prepared, published=True, now=FIXED_NOW
        )
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.record.completed_initial_reviews, 0)

    def test_failed_publish_aborts_reservation(self):
        ledger = InMemorySessionLedger()
        policy = ReviewConvergencePolicy(mode="strict")
        prepared = prepare_session_round(
            ledger, IDENTITY, policy, reservation_id="abcd1234", now=FIXED_NOW
        )
        complete_session_round(
            ledger, IDENTITY, prepared, published=False, now=FIXED_NOW
        )
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertIsNone(loaded.record.reservation_id)
        self.assertEqual(loaded.record.completed_initial_reviews, 0)


class GitHubSessionLedgerTests(unittest.TestCase):
    def test_initialize_and_cas_via_issue_comment(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        reserved = record.evolve(
            now=FIXED_NOW,
            generation=1,
            reservation_id="abcd1234",
            reserved_slot="initial",
        )
        reserved_body = render_session_comment(
            repository_id=99, pull_request=136, record=reserved
        )
        http, _calls = make_http(
            [
                json_response([]),
                json_response({"id": 7, "body": body}, status=201),
                json_response([{"id": 7, "body": body}]),
                json_response({"id": 7, "body": reserved_body}),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        created = ledger.initialize(IDENTITY, now=FIXED_NOW)
        self.assertEqual(created.generation, 0)
        updated = ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="abcd1234",
            expected_generation=0,
            now=FIXED_NOW,
        )
        self.assertEqual(updated.reservation_id, "abcd1234")

    def test_multiple_markers_fail_closed(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        http, _calls = make_http(
            [json_response([{"id": 1, "body": body}, {"id": 2, "body": body}])]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "conflict")


class GitHubApplicationSessionTests(unittest.TestCase):
    def test_operator_publish_commits_local_ledger(self):
        class Broker:
            def request_oidc_token(self):
                return "oidc-token"

            def exchange(self, token, *, capability=None):
                return f"capability-{capability}"

        class Reviewer:
            def publish(self, **kwargs):
                from review_sensei.hosting.github import PublicationResult

                return PublicationResult(status="published", review_id=1)

        ledger = InMemorySessionLedger()
        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=Reviewer(),
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        result = application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository="owner/repo",
            repository_id=99,
            pull_request=136,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(summary="ok", comments=(), provider="fixture"),
            diff="diff",
            app_slug="reviewsensei[bot]",
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertEqual(result.status, "published")
        loaded = ledger.load(IDENTITY)
        self.assertEqual(loaded.record.completed_initial_reviews, 1)


class DiagnosticSessionTests(unittest.TestCase):
    def test_doctor_and_plan_display_local_session(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = LocalSessionLedger(root)
            record = SessionRecord.create(
                IDENTITY, now=FIXED_NOW, completed_initial_reviews=1
            )
            ledger._write(IDENTITY, record)
            doctor = run_doctor(
                session_ledger=root,
                repository="owner/repo",
                pull_request=136,
                review_mode="merge-focused",
            )
            check = next(
                item for item in doctor["checks"] if item["name"] == "session-ledger"
            )
            self.assertEqual(check["status"], "pass")
            self.assertEqual(doctor["session_record"]["completed_initial_reviews"], 1)
            plan = build_plan(
                session_ledger=root,
                repository="owner/repo",
                pull_request=136,
            )
            self.assertEqual(plan["session_record"]["status"], "ok")
            missing = run_doctor()
            self.assertFalse(
                any(item["name"] == "session-ledger" for item in missing["checks"])
            )


if __name__ == "__main__":
    unittest.main()
