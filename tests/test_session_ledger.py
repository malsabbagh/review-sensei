from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diagnostics import build_plan, run_doctor
from review_sensei.errors import ReviewInputError, ReviewSenseiError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubHttp,
    GitHubHTTPPaginationLimitError,
    GitHubWriteOptions,
)
from review_sensei.hosting.github.session_ledger import (
    SESSION_MARKER_PREFIX,
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)
from review_sensei.models import ReviewResult
from review_sensei.session import (
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_TTL,
    MIN_SESSION_TTL,
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionLoadResult,
    SessionRecord,
    complete_session_round,
    migrate_session_document,
    prepare_session_round,
    resolve_local_session_ledger,
    session_reservation_id,
)

try:
    from fake_github_http import FakeHTTPResponse, json_response, make_http
except ImportError:
    from tests.fake_github_http import FakeHTTPResponse, json_response, make_http


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

    def test_identity_rejects_gitHub_invalid_component_edges(self):
        for repository in (
            "owner/.repo",
            "owner/repo-.",
            "-owner/repo",
            f"{'o' * 40}/repo",
            f"owner/{'r' * 101}",
        ):
            with self.subTest(repository=repository):
                with self.assertRaisesRegex(ReviewInputError, "repository"):
                    SessionIdentity(repository, 136)

    def test_ttl_bound(self):
        with self.assertRaisesRegex(ReviewInputError, "ttl"):
            SessionRecord.create(IDENTITY, now=FIXED_NOW, ttl=timedelta(days=91))

    def test_schema_validation_is_reserved_for_untrusted_documents(self):
        with patch("review_sensei.session.validate_public_document") as validate:
            record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
            record.evolve(now=FIXED_NOW, generation=1)
            validate.assert_not_called()

            restored = SessionRecord.from_dict(record.to_dict())
            validate.assert_called_once_with(restored.to_dict(), "session-record")

    def test_reservation_and_last_commit_ids_must_differ(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW).to_dict()
        record.update(
            {
                "reservation_id": "abcd1234",
                "reserved_slot": "initial",
                "last_committed_reservation_id": "abcd1234",
            }
        )
        with self.assertRaisesRegex(ReviewInputError, "must differ"):
            SessionRecord.from_dict(record)


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

    def test_malformed_documents_return_integrity_failed(self):
        self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        path = self.ledger._path(IDENTITY)
        path.write_text("{", encoding="utf-8")
        self.assertEqual(self.ledger.load(IDENTITY).status, "integrity-failed")
        path.write_text(json.dumps({"schema_version": "2.0"}), encoding="utf-8")
        self.assertEqual(self.ledger.load(IDENTITY).status, "integrity-failed")

    def test_local_ledger_documents_single_writer_contract(self):
        self.assertTrue(self.ledger.SINGLE_WRITER_PER_IDENTITY)
        self.assertIn("single writer", (self.ledger.__doc__ or "").lower())

    def test_repository_paths_are_injective(self):
        left = SessionIdentity("owner--repo/x", 136)
        right = SessionIdentity("owner/repo--x", 136)
        self.assertNotEqual(self.ledger._path(left), self.ledger._path(right))

    @unittest.skipUnless(os.name != "nt", "directory fsync is POSIX-only")
    def test_directory_sync_failure_preserves_replaced_record(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        with patch(
            "review_sensei.session.os.fsync",
            side_effect=[None, OSError("directory sync failed")],
        ):
            with self.assertRaisesRegex(ReviewInputError, "directory sync failed"):
                self.ledger._write(IDENTITY, record)
        self.assertEqual(self.ledger.load(IDENTITY).status, "ok")

    def test_invalid_ledger_path_is_sanitized(self):
        with self.assertRaisesRegex(ReviewInputError, "path is invalid"):
            resolve_local_session_ledger("bad\x00path")

    def test_ledger_path_expands_home_and_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict(os.environ, {"HOME": raw, "RS_LEDGER_DIR": raw}):
                self.assertEqual(
                    resolve_local_session_ledger("~/ledger").root,
                    (Path(raw) / "ledger").resolve(),
                )
                self.assertEqual(
                    resolve_local_session_ledger("$RS_LEDGER_DIR/ledger").root,
                    (Path(raw) / "ledger").resolve(),
                )

    def test_ledger_path_can_be_contained_by_a_trusted_root(self):
        with tempfile.TemporaryDirectory() as trusted_raw:
            trusted = Path(trusted_raw)
            ledger = resolve_local_session_ledger(
                trusted / "ledger", trusted_root=trusted
            )
            self.assertEqual(ledger.root, (trusted / "ledger").resolve())
            with self.assertRaisesRegex(ReviewInputError, "outside the trusted root"):
                resolve_local_session_ledger(
                    trusted.parent / "escape", trusted_root=trusted
                )

    def test_initialize_uses_exclusive_create_for_a_stale_missing_read(self):
        first = self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        del first
        second = LocalSessionLedger(Path(self.temp.name))
        with patch.object(
            second, "load", return_value=SessionLoadResult(status="missing")
        ):
            with self.assertRaisesRegex(ReviewInputError, "concurrent initialization"):
                second.initialize(IDENTITY, now=FIXED_NOW)

    def test_legacy_short_expiry_is_floored(self):
        legacy = {
            "schema_version": "0.1",
            "repository": IDENTITY.repository,
            "pull_request_number": IDENTITY.pull_request,
            "repository_id": IDENTITY.repository_id,
            "created_at": FIXED_NOW.isoformat().replace("+00:00", "Z"),
            "expires_at": (FIXED_NOW + timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        path = self.ledger._path(IDENTITY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "migrated")
        self.assertGreaterEqual(
            datetime.fromisoformat(loaded.record.expires_at.replace("Z", "+00:00")),
            FIXED_NOW + MIN_SESSION_TTL,
        )

    def test_legacy_missing_created_at_fails_closed(self):
        legacy = {
            "schema_version": "0.1",
            "repository": IDENTITY.repository,
            "pull_request_number": IDENTITY.pull_request,
        }
        path = self.ledger._path(IDENTITY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(
            self.ledger.load(IDENTITY, now=FIXED_NOW).status, "integrity-failed"
        )

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
        }
        migrated = migrate_session_document(legacy)
        self.assertEqual(migrated["pull_request"], 136)
        path = self.ledger._path(IDENTITY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "migrated")
        self.assertEqual(loaded.record.completed_initial_reviews, 1)
        self.assertEqual(loaded.record.expires_at, created.expires_at)

    def test_legacy_v01_clamps_overlong_expiry(self):
        legacy = {
            "schema_version": "0.1",
            "repository": IDENTITY.repository,
            "pull_request_number": IDENTITY.pull_request,
            "repository_id": IDENTITY.repository_id,
            "created_at": FIXED_NOW.isoformat().replace("+00:00", "Z"),
            "expires_at": (FIXED_NOW + timedelta(days=120))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        path = self.ledger._path(IDENTITY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "migrated")
        expected_expiry = (
            (FIXED_NOW + MAX_SESSION_TTL).isoformat().replace("+00:00", "Z")
        )
        self.assertEqual(loaded.record.expires_at, expected_expiry)


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

    def test_complete_session_round_is_idempotent_after_commit_and_abort(self):
        commit_ledger = InMemorySessionLedger()
        commit_prepared = prepare_session_round(
            commit_ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="abcd1234",
            now=FIXED_NOW,
        )
        committed = complete_session_round(
            commit_ledger, IDENTITY, commit_prepared, published=True, now=FIXED_NOW
        )
        replayed_commit = complete_session_round(
            commit_ledger, IDENTITY, commit_prepared, published=True, now=FIXED_NOW
        )
        self.assertEqual(replayed_commit, committed)
        self.assertEqual(replayed_commit.completed_initial_reviews, 1)
        with self.assertRaisesRegex(ReviewInputError, "outcome conflicts"):
            complete_session_round(
                commit_ledger,
                IDENTITY,
                commit_prepared,
                published=False,
                now=FIXED_NOW,
            )
        same_attempt = prepare_session_round(
            commit_ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="abcd1234",
            now=FIXED_NOW,
        )
        self.assertIsNone(same_attempt.reservation_id)
        self.assertEqual(same_attempt.record.completed_initial_reviews, 1)

        abort_ledger = InMemorySessionLedger()
        abort_prepared = prepare_session_round(
            abort_ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="strict"),
            reservation_id="ffff1234",
            now=FIXED_NOW,
        )
        aborted = complete_session_round(
            abort_ledger, IDENTITY, abort_prepared, published=False, now=FIXED_NOW
        )
        replayed_abort = complete_session_round(
            abort_ledger, IDENTITY, abort_prepared, published=False, now=FIXED_NOW
        )
        self.assertEqual(replayed_abort, aborted)
        self.assertEqual(replayed_abort.completed_initial_reviews, 0)
        with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
            abort_ledger.abort(
                IDENTITY,
                reservation_id="ffff1234",
                expected_generation=abort_prepared.record.generation,
                now=FIXED_NOW,
            )

    def test_complete_session_round_rejects_a_competing_writer(self):
        ledger = InMemorySessionLedger()
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="abcd1234",
            now=FIXED_NOW,
        )
        aborted = ledger.abort(
            IDENTITY,
            reservation_id="abcd1234",
            expected_generation=prepared.record.generation,
            now=FIXED_NOW,
        )
        competing = ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="ffff1234",
            expected_generation=aborted.generation,
            now=FIXED_NOW,
        )
        ledger.commit(
            IDENTITY,
            reservation_id="ffff1234",
            expected_generation=competing.generation,
            now=FIXED_NOW,
        )
        with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
            complete_session_round(
                ledger, IDENTITY, prepared, published=True, now=FIXED_NOW
            )


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
                json_response([{"id": 7, "body": body}]),
                json_response([{"id": 7, "body": body}]),
                json_response({"id": 7, "body": reserved_body}),
                json_response([{"id": 7, "body": reserved_body}]),
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

    def test_initialize_fails_closed_on_concurrent_duplicate_comments(self):
        first = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        second = first.evolve(now=FIXED_NOW, generation=1)
        first_body = render_session_comment(
            repository_id=99, pull_request=136, record=first
        )
        second_body = render_session_comment(
            repository_id=99, pull_request=136, record=second
        )
        http, _calls = make_http(
            [
                json_response([]),
                json_response({"id": 7, "body": first_body}, status=201),
                json_response(
                    [
                        {"id": 7, "body": first_body},
                        {"id": 8, "body": second_body},
                    ]
                ),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        with self.assertRaisesRegex(ReviewInputError, "multiple session comments"):
            ledger.initialize(IDENTITY, now=FIXED_NOW)

    def test_update_rejects_a_different_comment_id(self):
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
                json_response([{"id": 7, "body": body}]),
                json_response([{"id": 7, "body": body}]),
                json_response({"id": 8, "body": reserved_body}),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        with self.assertRaisesRegex(ReviewInputError, "update lost"):
            ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id="abcd1234",
                expected_generation=0,
                now=FIXED_NOW,
            )

    def test_update_reads_back_the_generation_after_patch(self):
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
        http, calls = make_http(
            [
                json_response([{"id": 7, "body": body}]),
                json_response([{"id": 7, "body": body}]),
                json_response({"id": 7, "body": reserved_body}),
                json_response([{"id": 7, "body": reserved_body}]),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        updated = ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="abcd1234",
            expected_generation=0,
            now=FIXED_NOW,
        )
        self.assertEqual(updated, reserved)
        self.assertEqual(
            [method for method, _url, _data in calls], ["GET", "GET", "PATCH", "GET"]
        )

    def test_update_rejects_a_competing_generation_before_patch(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        competing = record.evolve(now=FIXED_NOW, generation=1)
        competing_body = render_session_comment(
            repository_id=99, pull_request=136, record=competing
        )
        http, calls = make_http(
            [
                json_response([{"id": 7, "body": body}]),
                json_response([{"id": 7, "body": competing_body}]),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
            ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id="abcd1234",
                expected_generation=0,
                now=FIXED_NOW,
            )
        self.assertEqual([method for method, _url, _data in calls], ["GET", "GET"])

    def test_update_rejects_a_different_record_digest(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        competing = record.evolve(
            now=FIXED_NOW,
            generation=1,
            reservation_id="ffff1234",
            reserved_slot="initial",
        )
        competing_body = render_session_comment(
            repository_id=99, pull_request=136, record=competing
        )
        http, _calls = make_http(
            [
                json_response([{"id": 7, "body": body}]),
                json_response([{"id": 7, "body": body}]),
                json_response({"id": 7, "body": competing_body}),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        with self.assertRaisesRegex(ReviewInputError, "update lost"):
            ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id="abcd1234",
                expected_generation=0,
                now=FIXED_NOW,
            )

    def test_human_marker_is_ignored(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        http, _calls = make_http(
            [
                json_response(
                    [
                        {
                            "id": 7,
                            "body": body,
                            "user": {"login": "attacker", "type": "User"},
                        }
                    ]
                )
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="token", app_slug="reviewsensei[bot]"
        )
        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "missing")

    def test_marker_must_be_terminal(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        quoted = f"{body}\nQuoted after marker"
        self.assertIsNone(parse_session_comment(quoted, identity=IDENTITY))

    def test_parser_uses_the_final_json_fence_before_the_marker(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        quoted = f'```json\n{{"unrelated":true}}\n```\n\n{body}'
        self.assertEqual(parse_session_comment(quoted, identity=IDENTITY), record)

    def test_multiple_markers_fail_closed(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        http, _calls = make_http(
            [json_response([{"id": 1, "body": body}, {"id": 2, "body": body}])]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "conflict")

    def test_oversized_session_comments_are_skipped(self):
        oversized = SESSION_MARKER_PREFIX + ("x" * (MAX_SESSION_COMMENT_BYTES + 1))
        self.assertIsNone(parse_session_comment(oversized, identity=IDENTITY))
        http, _calls = make_http([json_response([{"id": 1, "body": oversized}])])
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "missing")

    def test_quoted_marker_prefix_without_marker_is_ignored(self):
        body = f"A maintainer quoted {SESSION_MARKER_PREFIX} in a reply."
        self.assertIsNone(parse_session_comment(body, identity=IDENTITY))

    def test_marker_for_another_identity_is_ignored(self):
        record = SessionRecord.create(
            SessionIdentity("other/repo", 999, repository_id=100), now=FIXED_NOW
        )
        body = render_session_comment(
            repository_id=100, pull_request=999, record=record
        )
        self.assertIsNone(parse_session_comment(body, identity=IDENTITY))

    def test_pagination_limit_is_a_fail_closed_load_status(self):
        http, _calls = make_http([json_response([])])
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        with patch.object(
            http,
            "paginate",
            side_effect=GitHubHTTPPaginationLimitError("page limit"),
        ):
            loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "integrity-failed")
        self.assertEqual(loaded.detail, "session comment discovery exceeded bound")

    def test_malformed_session_comment_maps_to_integrity_failed(self):
        body = (
            "```json\n{}\n```\n"
            f"{SESSION_MARKER_PREFIX} repo=99 pr=136 gen=0 "
            f"digest={'0' * 64} -->"
        )
        http, _calls = make_http([json_response([{"id": 1, "body": body}])])
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "integrity-failed")


class GitHubApplicationSessionTests(unittest.TestCase):
    def test_github_session_ledger_requires_a_head_sha(self):
        class Broker:
            def exchange(self, token, *, capability=None):
                return "capability-token"

        application = GitHubApplication(
            broker=Broker(),
            http=object(),
            reviewer=object(),
            learner=object(),
            replier=object(),
            session_ledger=object(),
        )
        with self.assertRaisesRegex(ReviewSenseiError, "head_sha"):
            application.publish_review(
                options=GitHubWriteOptions(
                    auto_review=True,
                    github_writes=True,
                    github_session_ledger=True,
                ),
                oidc_token="oidc",
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="",
                base_branch="main",
                base_sha="b" * 40,
                result=ReviewResult(summary="ok", comments=(), provider="fixture"),
                diff="diff",
                app_slug="reviewsensei[bot]",
            )

    def test_github_session_ledger_writes_through_application(self):
        from review_sensei.hosting.github import PublicationResult

        class Broker:
            def exchange(self, token, *, capability=None):
                return "capability-token"

        class Reviewer:
            def publish(self, **kwargs):
                return PublicationResult(status="published", review_id=7)

        head_sha = "a" * 40
        state = {"get_count": 0, "body": None}
        calls = []

        def opener(request, timeout):
            del timeout
            calls.append((request.method, request.full_url, request.data))
            if request.method == "GET":
                state["get_count"] += 1
                value = (
                    []
                    if state["get_count"] <= 2
                    else [
                        {
                            "id": 7,
                            "body": state["body"],
                            "user": {
                                "login": "reviewsensei[bot]",
                                "type": "Bot",
                            },
                        }
                    ]
                )
                status = 200
            else:
                payload = json.loads(request.data.decode("utf-8"))
                state["body"] = payload["body"]
                value = {"id": 7, "body": state["body"]}
                status = 201 if request.method == "POST" else 200
            return FakeHTTPResponse(json.dumps(value).encode("utf-8"), status=status)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        application = GitHubApplication(
            broker=Broker(),
            http=http,
            reviewer=Reviewer(),
            learner=object(),
            replier=object(),
        )
        publication = application.publish_review(
            options=GitHubWriteOptions(
                auto_review=True,
                github_writes=True,
                github_session_ledger=True,
            ),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=99,
            pull_request=IDENTITY.pull_request,
            head_sha=head_sha,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(summary="ok", comments=(), provider="fixture"),
            diff="diff",
            app_slug="reviewsensei[bot]",
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertEqual(publication.status, "published")
        self.assertEqual(
            [method for method, _url, _data in calls],
            [
                "GET",
                "GET",
                "POST",
                "GET",
                "GET",
                "GET",
                "PATCH",
                "GET",
                "GET",
                "GET",
                "GET",
                "PATCH",
                "GET",
            ],
        )

    def test_preparation_failure_releases_a_reservation(self):
        class Broker:
            def request_oidc_token(self):
                return "oidc-token"

            def exchange(self, token, *, capability=None):
                return f"capability-{capability}"

        class Reviewer:
            def publish(self, **kwargs):
                raise AssertionError("publisher must not run")

        ledger = InMemorySessionLedger()
        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=Reviewer(),
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        original_prepare = prepare_session_round

        def reserve_then_fail(*args, **kwargs):
            original_prepare(*args, **kwargs)
            raise RuntimeError("preparation failed")

        with patch(
            "review_sensei.hosting.github.application.prepare_session_round",
            side_effect=reserve_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "preparation failed"):
                application.publish_review(
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
        loaded = ledger.load(IDENTITY)
        self.assertIsNone(loaded.record.reservation_id)

    def test_preparation_cleanup_failure_does_not_mask_original_error(self):
        class Broker:
            def exchange(self, token, *, capability=None):
                return "capability-token"

        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
            session_ledger=InMemorySessionLedger(),
        )
        with (
            patch(
                "review_sensei.hosting.github.application.prepare_session_round",
                side_effect=RuntimeError("preparation failed"),
            ),
            patch.object(
                GitHubApplication,
                "_abort_held_session_reservation",
                return_value=ValueError("cleanup failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "preparation failed") as caught:
                application.publish_review(
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
                )
        self.assertTrue(
            any("cleanup failed" in note for note in caught.exception.__notes__)
        )

    def test_publisher_base_exception_releases_a_reservation(self):
        class Broker:
            def request_oidc_token(self):
                return "oidc-token"

            def exchange(self, token, *, capability=None):
                return f"capability-{capability}"

        class Reviewer:
            def publish(self, **kwargs):
                raise KeyboardInterrupt()

        ledger = InMemorySessionLedger()
        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=Reviewer(),
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        with self.assertRaises(KeyboardInterrupt):
            application.publish_review(
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
        loaded = ledger.load(IDENTITY)
        self.assertIsNone(loaded.record.reservation_id)

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
    def test_diagnostic_does_not_print_counters_for_unavailable_session(self):
        from review_sensei.diagnostics import render_diagnostic

        rendered = render_diagnostic({"session_record": {"status": "integrity-failed"}})
        self.assertIn("session_ledger: status=integrity-failed", rendered)
        self.assertNotIn("initial=0", rendered)

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
            self.assertEqual(plan["identity"]["pull_request"], 136)
            self.assertEqual(plan["session_record"]["status"], "ok")
            with tempfile.TemporaryDirectory() as empty_raw:
                missing = run_doctor(
                    session_ledger=Path(empty_raw),
                    repository="owner/repo",
                    pull_request=136,
                    review_mode="merge-focused",
                )
                missing_check = next(
                    item
                    for item in missing["checks"]
                    if item["name"] == "session-ledger"
                )
                self.assertEqual(missing_check["status"], "pass")
                self.assertEqual(
                    missing_check["detail"],
                    "session ledger is not yet initialized",
                )


if __name__ == "__main__":
    unittest.main()
