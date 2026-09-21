from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.baseline import (
    BaselineFinding,
    ReviewBaseline,
    baseline_from_history_document,
    baseline_history_document,
)
from review_sensei.context import MAX_CACHE_METADATA_ITEMS, ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diagnostics import build_plan, run_doctor
from review_sensei.disposition import apply_session_command, parse_maintainer_command
from review_sensei.errors import ReviewInputError, ReviewSenseiError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubBrokerClientError,
    GitHubHttp,
    GitHubHTTPPaginationLimitError,
    GitHubWriteOptions,
)
from review_sensei.hosting.github.errors import GitHubPublicationTransientError
from review_sensei.hosting.github.session_ledger import (
    SESSION_MARKER_PREFIX,
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)
from review_sensei.models import ReviewResult
from review_sensei.schemas import validate_public_document
from review_sensei.session import (
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    MAX_SESSION_TTL,
    MAX_STORED_CONTINUATION_GRANTS,
    MIN_SESSION_TTL,
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionLoadResult,
    SessionRecord,
    complete_session_round,
    issue_continuation_grant,
    migrate_session_document,
    prepare_session_round,
    record_session_failed_attempt,
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
    def test_continuation_grant_requires_paired_consumption_state(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW).to_dict()
        record["operator_paused"] = False
        record["dispositions"] = []
        record["continuation_grants"] = [
            {
                "command_id": "comment-1",
                "actor": "alice",
                "head_sha": "a" * 40,
                "policy_digest": "b" * 64,
                "issued_at": "2026-09-19T12:00:00Z",
                "expires_at": "2026-09-20T12:00:00Z",
                "consumed_reservation_id": "abcd1234",
                "consumed_generation": None,
            }
        ]
        # Rebuild the digest through the public constructor so the validation
        # oracle isolates the paired state rather than an integrity mismatch.
        with self.assertRaisesRegex(ReviewInputError, "continuation grant"):
            SessionRecord.from_dict(record)

    def test_continuation_grant_cannot_outlive_session(self):
        def grant(*, command_id: str, expires_at: str) -> dict[str, object]:
            return {
                "command_id": command_id,
                "actor": "alice",
                "head_sha": "a" * 40,
                "policy_digest": "b" * 64,
                "issued_at": "2026-09-19T12:00:00Z",
                "expires_at": expires_at,
                "consumed_reservation_id": None,
                "consumed_generation": None,
            }

        session_expires_at = "2026-09-19T13:00:00Z"
        with self.assertRaisesRegex(ReviewInputError, "exceeds session expiry"):
            SessionRecord.create(
                IDENTITY,
                now=FIXED_NOW,
                expires_at=session_expires_at,
                continuation_grants=[
                    grant(
                        command_id="grant-after-session",
                        expires_at="2026-09-19T14:00:00Z",
                    )
                ],
            )

        for command_id, grant_expires_at in (
            ("grant-at-session", session_expires_at),
            ("grant-before-session", "2026-09-19T12:30:00Z"),
        ):
            record = SessionRecord.create(
                IDENTITY,
                now=FIXED_NOW,
                expires_at=session_expires_at,
                continuation_grants=[
                    grant(command_id=command_id, expires_at=grant_expires_at)
                ],
            )
            self.assertEqual(
                record.continuation_grants[0]["expires_at"], grant_expires_at
            )

    def test_continuation_grant_rejects_an_expired_session(self):
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            expires_at="2026-09-19T13:00:00Z",
        )
        with self.assertRaisesRegex(ReviewInputError, "continuation grant expiry"):
            issue_continuation_grant(
                record,
                command_id="grant-expired-session",
                actor="alice",
                head_sha="a" * 40,
                policy_digest="b" * 64,
                now=datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc),
            )

    def test_continuation_grant_history_bound_is_explicit_and_preserves_replay_tombstones(
        self,
    ):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        for index in range(MAX_STORED_CONTINUATION_GRANTS):
            record = issue_continuation_grant(
                record,
                command_id=f"grant-{index + 1}",
                actor="alice",
                head_sha=f"{index + 1:040x}",
                policy_digest="b" * 64,
                now=FIXED_NOW,
            )
            pending = dict(record.continuation_grants[-1])
            pending["consumed_reservation_id"] = f"{index + 1:08x}"
            pending["consumed_generation"] = record.generation + 1
            record = record.evolve(
                now=FIXED_NOW,
                generation=record.generation + 1,
                continuation_grants=(
                    *record.continuation_grants[:-1],
                    pending,
                ),
            )

        before = record.to_dict()
        with self.assertRaisesRegex(ReviewInputError, "history limit"):
            issue_continuation_grant(
                record,
                command_id="grant-over-bound",
                actor="alice",
                head_sha="f" * 40,
                policy_digest="b" * 64,
                now=FIXED_NOW,
            )
        self.assertEqual(record.to_dict(), before)

    @staticmethod
    def _history() -> dict[str, object]:
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository="owner/repo",
                pull_request=136,
                base_sha="a" * 40,
                head_sha="b" * 40,
                engine="ollama",
                model="test",
                profile="default",
                stage_digest="c" * 64,
                context_digest="d" * 64,
                learning_digest="e" * 64,
            ),
            policy_digest="f" * 64,
            complete=True,
            coverage_complete=True,
            findings=(
                BaselineFinding(
                    fingerprint="1" * 64,
                    resolution_criterion="2" * 64,
                    concern="3" * 64,
                    path="src/example.py",
                    symbol="run",
                    defect_kind="bug",
                    generation=1,
                    blocking=True,
                ),
            ),
            reviewed_paths=("src/example.py",),
        )
        return {
            "state": "completed",
            "baseline": baseline_history_document(baseline),
            "progress": [{"event": "completed", "generation": 1}],
            "provenance": {"ledger_digest": "0" * 64},
        }

    def test_untrusted_history_is_bounded_by_bytes_not_only_field_caps(self):
        # The schema bounds every member of the envelope, but only the
        # validation seam can bound its encoded size. A document whose fields
        # all satisfy the schema must still be refused when the envelope
        # exceeds the component bound.
        history = self._history()
        baseline = history["baseline"]
        assert isinstance(baseline, dict)
        baseline["reviewed_paths"] = [
            f"src/{'a' * 200}_{index}.py" for index in range(40)
        ]
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        document = record.to_dict()
        document["convergence_history"] = history
        with self.assertRaisesRegex(ReviewInputError, "exceeds the configured bound"):
            SessionRecord.from_dict(document)

    def test_history_is_inside_the_integrity_boundary(self):
        history = self._history()
        record = SessionRecord.create(
            IDENTITY, now=FIXED_NOW, convergence_history=history
        )
        # The envelope is covered by record_sha256: changing the history alone
        # must fail the integrity check, not silently parse.
        tampered = record.to_dict()
        tampered["convergence_history"]["progress"] = [  # type: ignore[index]
            {"event": "completed", "generation": 2}
        ]
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            SessionRecord.from_dict(tampered)
        changed = self._history()
        changed["progress"] = [{"event": "completed", "generation": 2}]
        other = SessionRecord.create(
            IDENTITY, now=FIXED_NOW, convergence_history=changed
        )
        self.assertNotEqual(record.record_sha256, other.record_sha256)

    def test_history_requires_the_current_digest_shape(self):
        # An upgrade path re-serializes an older record with an envelope. It
        # must not keep the legacy or operator-paused payload, which computes a
        # digest that does not include the history.
        record = SessionRecord.create(
            IDENTITY, now=FIXED_NOW, convergence_history=self._history()
        )
        # A legacy shape is already refused by the C6-field guard; the
        # operator-paused shape is refused by the history guard added here.
        with self.assertRaisesRegex(ReviewInputError, "has C6 fields"):
            replace(record, _digest_shape_input="legacy")
        with self.assertRaisesRegex(ReviewInputError, "current digest shape"):
            replace(record, _digest_shape_input="operator-paused")

    def test_legacy_record_gains_a_covered_history_in_place(self):
        legacy = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        upgraded = legacy.evolve(now=FIXED_NOW, convergence_history=self._history())
        restored = SessionRecord.from_dict(upgraded.to_dict())
        self.assertEqual(restored.convergence_history, self._history())
        tampered = upgraded.to_dict()
        tampered["convergence_history"]["state"] = "invalidated"  # type: ignore[index]
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            SessionRecord.from_dict(tampered)

    def test_history_envelope_bounds_findings_to_the_adr_limit(self):
        findings = tuple(
            BaselineFinding(
                fingerprint=f"{index:064x}",
                resolution_criterion="2" * 64,
                concern="3" * 64,
                path=f"src/example{index}.py",
                symbol="run",
                defect_kind="bug",
                generation=1,
                blocking=index == 5,
            )
            for index in range(1, 6)
        )
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository="owner/repo",
                pull_request=136,
                base_sha="a" * 40,
                head_sha="b" * 40,
                engine="ollama",
                model="test",
                profile="default",
                stage_digest="c" * 64,
                context_digest="d" * 64,
                learning_digest="e" * 64,
            ),
            policy_digest="f" * 64,
            complete=True,
            coverage_complete=True,
            findings=findings,
            reviewed_paths=("src/example.py",),
        )
        document = baseline_history_document(baseline)
        self.assertEqual(len(document["findings"]), 3)
        # Selection is deterministic and content-derived rather than dependent
        # on provider ordering or on how many retries the run took.
        self.assertEqual(
            [item["fingerprint"] for item in document["findings"]],
            sorted(finding.fingerprint for finding in findings)[:3],
        )
        history = {
            "state": "completed",
            "baseline": document,
            "progress": [{"event": "completed", "generation": 1}],
            "provenance": {"ledger_digest": "0" * 64},
        }
        record = SessionRecord.create(
            IDENTITY, now=FIXED_NOW, convergence_history=history
        )
        # What Python writes must satisfy the published schema, so a later
        # untrusted-document read cannot fail on the record Python itself
        # produced.
        validate_public_document(record.to_dict(), "session-record")
        self.assertEqual(
            SessionRecord.from_dict(record.to_dict()).convergence_history, history
        )

    def test_bounded_convergence_history_round_trips_and_is_integrity_covered(self):
        history = self._history()
        record = SessionRecord.create(
            IDENTITY, now=FIXED_NOW, convergence_history=history
        )
        restored = SessionRecord.from_dict(record.to_dict())
        self.assertEqual(restored.convergence_history, history)
        self.assertEqual(
            baseline_from_history_document(restored.convergence_history["baseline"]),
            baseline_from_history_document(history["baseline"]),
        )
        tampered = record.to_dict()
        tampered["convergence_history"]["state"] = "recovery-required"  # type: ignore[index]
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            SessionRecord.from_dict(tampered)

    def test_convergence_history_rejects_unknown_nested_fields(self):
        history = self._history()
        baseline = history["baseline"]
        assert isinstance(baseline, dict)
        findings = baseline["findings"]
        assert isinstance(findings, list)
        findings[0]["untrusted"] = "field"
        with self.assertRaisesRegex(ReviewInputError, "persisted baseline"):
            SessionRecord.create(IDENTITY, now=FIXED_NOW, convergence_history=history)

    def test_convergence_history_requires_lifecycle_and_provenance_fields(self):
        history = self._history()
        del history["provenance"]
        with self.assertRaisesRegex(ReviewInputError, "unknown fields"):
            SessionRecord.create(IDENTITY, now=FIXED_NOW, convergence_history=history)

    def test_completed_convergence_history_requires_a_baseline(self):
        history = self._history()
        del history["baseline"]
        with self.assertRaisesRegex(ReviewInputError, "requires a baseline"):
            SessionRecord.create(IDENTITY, now=FIXED_NOW, convergence_history=history)

    def test_non_completed_convergence_history_rejects_a_baseline(self):
        invalidated = self._history()
        invalidated["state"] = "invalidated"
        with self.assertRaisesRegex(
            ReviewInputError, "only valid for a completed state"
        ):
            SessionRecord.create(
                IDENTITY, now=FIXED_NOW, convergence_history=invalidated
            )
        recovery = self._history()
        recovery["state"] = "recovery-required"
        recovery["baseline"] = None
        with self.assertRaisesRegex(
            ReviewInputError, "only valid for a completed state"
        ):
            SessionRecord.create(IDENTITY, now=FIXED_NOW, convergence_history=recovery)

    def test_baseline_reconstruction_rejects_unclosed_shapes(self):
        document = self._history()["baseline"]
        assert isinstance(document, dict)
        # An extra cache_key field must not be silently dropped.
        broken = json.loads(json.dumps(document))
        broken["cache_key"]["engine_extra"] = "field"
        with self.assertRaisesRegex(ReviewInputError, "invalid shape"):
            baseline_from_history_document(broken)
        # A missing cache_key field must not be silently coerced.
        broken = json.loads(json.dumps(document))
        del broken["cache_key"]["model"]
        with self.assertRaisesRegex(ReviewInputError, "invalid shape"):
            baseline_from_history_document(broken)
        # An unexpected extra finding-level field must not be dropped either.
        broken = json.loads(json.dumps(document))
        broken["findings"][0]["resolution_criterion_extra"] = "field"
        with self.assertRaisesRegex(ReviewInputError, "invalid shape"):
            baseline_from_history_document(broken)
        # A missing finding field must not fall back to a BaselineFinding default.
        broken = json.loads(json.dumps(document))
        del broken["findings"][0]["blocking"]
        with self.assertRaisesRegex(ReviewInputError, "invalid shape"):
            baseline_from_history_document(broken)
        # Findings are only durable as a JSON array; a tuple is not the
        # persisted shape even though BaselineFinding would accept its items.
        broken = json.loads(json.dumps(document))
        broken["findings"] = tuple(broken["findings"])
        with self.assertRaisesRegex(ReviewInputError, "invalid shape"):
            baseline_from_history_document(broken)

        # Cache-key validators raise ContextLoadError for untrusted values;
        # baseline reconstruction must normalize that into the public input
        # error rather than leaking a lower-level context exception.
        broken = json.loads(json.dumps(document))
        broken["cache_key"]["stage_digest"] = "not-a-sha256"
        with self.assertRaisesRegex(ReviewInputError, "persisted baseline"):
            baseline_from_history_document(broken)

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
    def test_restart_retains_consumed_continuation_grant(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        apply_session_command(
            self.ledger,
            IDENTITY,
            parse_maintainer_command(
                "@sensei review continue",
                actor="alice",
                head_sha="a" * 40,
                command_id="comment-restart",
            ),
            now=FIXED_NOW,
            policy=policy,
        )
        self.ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                now=FIXED_NOW,
                completed_initial_reviews=1,
                completed_verification_rounds=2,
            ),
            now=FIXED_NOW,
        )
        prepared = prepare_session_round(
            self.ledger,
            IDENTITY,
            policy,
            reservation_id="abcd1234",
            head_sha="a" * 40,
            now=FIXED_NOW,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        self.assertIsNotNone(prepared.reservation_id)
        restarted = LocalSessionLedger(Path(self.temp.name))
        loaded = restarted.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(
            loaded.record.continuation_grants[0]["consumed_reservation_id"], "abcd1234"
        )

    def test_grant_scope_mismatch_and_direct_rounds_cannot_consume_it(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        apply_session_command(
            self.ledger,
            IDENTITY,
            parse_maintainer_command(
                "@sensei review continue",
                actor="alice",
                head_sha="a" * 40,
                command_id="comment-scope",
            ),
            now=FIXED_NOW,
            policy=policy,
        )
        self.ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                now=FIXED_NOW,
                completed_initial_reviews=1,
                completed_verification_rounds=2,
            ),
            now=FIXED_NOW,
        )
        rejected = prepare_session_round(
            self.ledger,
            IDENTITY,
            policy,
            reservation_id="abcd1234",
            head_sha="b" * 40,
            continuation_rounds=1,
            now=FIXED_NOW,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        self.assertFalse(rejected.decision.admit)
        self.assertEqual(rejected.decision.handoff_reason, "round-budget-exhausted")
        self.assertIsNone(rejected.reservation_id)
        self.assertIsNone(
            self.ledger.load(IDENTITY, now=FIXED_NOW).record.continuation_grants[0][
                "consumed_reservation_id"
            ]
        )

    def test_competing_reservations_consume_a_grant_once(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        apply_session_command(
            self.ledger,
            IDENTITY,
            parse_maintainer_command(
                "@sensei review continue",
                actor="alice",
                head_sha="a" * 40,
                command_id="comment-race",
            ),
            now=FIXED_NOW,
            policy=policy,
        )
        self.ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                now=FIXED_NOW,
                completed_initial_reviews=1,
                completed_verification_rounds=2,
            ),
            now=FIXED_NOW,
        )
        winner = prepare_session_round(
            self.ledger,
            IDENTITY,
            policy,
            reservation_id="abcd1234",
            head_sha="a" * 40,
            now=FIXED_NOW,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        loser = prepare_session_round(
            self.ledger,
            IDENTITY,
            policy,
            reservation_id="ffff1234",
            head_sha="a" * 40,
            now=FIXED_NOW,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        self.assertIsNotNone(winner.reservation_id)
        self.assertIsNone(loser.reservation_id)
        self.assertEqual(loser.decision.handoff_reason, "paused")
        self.assertEqual(
            self.ledger.load(IDENTITY, now=FIXED_NOW).record.continuation_grants[0][
                "consumed_reservation_id"
            ],
            "abcd1234",
        )

    def test_failed_continuation_attempt_keeps_the_grant_consumed(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        apply_session_command(
            self.ledger,
            IDENTITY,
            parse_maintainer_command(
                "@sensei review continue",
                actor="alice",
                head_sha="a" * 40,
                command_id="comment-failure",
            ),
            now=FIXED_NOW,
            policy=policy,
        )
        self.ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                now=FIXED_NOW,
                completed_initial_reviews=1,
                completed_verification_rounds=2,
            ),
            now=FIXED_NOW,
        )
        prepared = prepare_session_round(
            self.ledger,
            IDENTITY,
            policy,
            reservation_id="abcd1234",
            head_sha="a" * 40,
            now=FIXED_NOW,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        record_session_failed_attempt(
            self.ledger, IDENTITY, reservation_id=prepared.reservation_id, now=FIXED_NOW
        )
        replay = prepare_session_round(
            self.ledger,
            IDENTITY,
            policy,
            reservation_id="ffff1234",
            head_sha="a" * 40,
            now=FIXED_NOW,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW).record
        self.assertEqual(loaded.failed_attempts, 1)
        self.assertEqual(
            loaded.continuation_grants[0]["consumed_reservation_id"], "abcd1234"
        )
        self.assertFalse(replay.decision.admit)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = LocalSessionLedger(Path(self.temp.name))

    def test_deleted_identity_directory_with_enrollment_witness_fails_closed(self):
        self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        record_path = self.ledger._path(IDENTITY)
        record_path.unlink()
        record_path.parent.rmdir()

        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "integrity-failed")
        with self.assertRaisesRegex(ReviewInputError, "integrity-failed"):
            prepare_session_round(
                self.ledger,
                IDENTITY,
                ReviewConvergencePolicy(mode="merge-focused"),
                reservation_id="deadbeef",
                now=FIXED_NOW,
            )

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
        with self.assertRaisesRegex(ReviewInputError, "expired"):
            self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        retained = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(retained.status, "expired")
        self.assertEqual(
            json.loads(self.ledger._path(IDENTITY).read_text(encoding="utf-8")),
            expired.to_dict(),
        )

    def test_expired_established_history_never_reopens_an_initial_allowance(self):
        history = SessionRecordTests._history()
        expired = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW - timedelta(days=31),
            ttl=timedelta(days=30),
            completed_initial_reviews=1,
            completed_verification_rounds=2,
            failed_attempts=1,
            generation=4,
            convergence_history=history,
        )
        self.ledger._write(IDENTITY, expired)

        restarted = LocalSessionLedger(Path(self.temp.name))
        self.assertEqual(restarted.load(IDENTITY, now=FIXED_NOW).status, "expired")
        with self.assertRaisesRegex(ReviewInputError, "expired"):
            prepare_session_round(
                restarted,
                IDENTITY,
                ReviewConvergencePolicy(mode="merge-focused"),
                reservation_id="expired-history",
                now=FIXED_NOW,
            )
        retained = restarted.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(retained.status, "expired")
        self.assertEqual(
            json.loads(restarted._path(IDENTITY).read_text(encoding="utf-8")),
            expired.to_dict(),
        )

    def test_integrity_failed_does_not_initialize(self):
        record = self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        path = self.ledger._path(IDENTITY)
        payload = record.to_dict()
        payload["failed_attempts"] = 3
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.ledger.load(IDENTITY).status, "integrity-failed")
        with self.assertRaisesRegex(ReviewInputError, "integrity-failed"):
            self.ledger.initialize(IDENTITY, now=FIXED_NOW)

    def test_restart_loads_integrity_checked_convergence_history(self):
        history = SessionRecordTests._history()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            convergence_history=history,
        )
        self.ledger._write(IDENTITY, record)

        restarted = LocalSessionLedger(Path(self.temp.name))
        loaded = restarted.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "ok")
        self.assertIsNotNone(loaded.record)
        assert loaded.record is not None
        self.assertEqual(loaded.record.completed_initial_reviews, 1)
        self.assertEqual(
            baseline_from_history_document(
                loaded.record.convergence_history["baseline"]
            ),
            baseline_from_history_document(history["baseline"]),
        )

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
            with patch.dict(
                os.environ,
                {
                    "HOME": raw,
                    "USERPROFILE": raw,
                    "RS_LEDGER_DIR": raw,
                },
            ):
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

    def test_trusted_root_expands_home_and_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict(
                os.environ,
                {
                    "HOME": raw,
                    "USERPROFILE": raw,
                    "RS_TRUSTED_ROOT": raw,
                },
            ):
                ledger = resolve_local_session_ledger(
                    "~/ledger", trusted_root="$RS_TRUSTED_ROOT"
                )
                self.assertEqual(ledger.root, (Path(raw) / "ledger").resolve())

    def test_largest_storable_record_still_loads(self):
        # The constructor bound (MAX_SESSION_RECORD_BYTES) is now enforced on
        # every load, so the largest record the bounds still allow has to keep
        # loading: a record that was storable before the check must not be
        # stranded by it. Dispositions supply the bulk and the convergence
        # envelope fills the rest until one of the bounds refuses.
        dispositions = tuple(
            {
                "fingerprint": f"{index:016x}" + "0" * 48,
                "action": "defer",
                "reason": f"reason {index} " + "r" * 500,
                "actor": "maintainer",
                "head_sha": None,
                "expires_at": None,
            }
            for index in range(4)
        )
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository="owner/repo",
                pull_request=136,
                base_sha="a" * 40,
                head_sha="b" * 40,
                engine="ollama",
                model="test",
                profile="default",
                stage_digest="c" * 64,
                context_digest="d" * 64,
                learning_digest="e" * 64,
            ),
            policy_digest="f" * 64,
            complete=True,
            coverage_complete=True,
            reviewed_paths=(),
        )
        record = None
        paths: list[str] = []
        for index in range(MAX_CACHE_METADATA_ITEMS):
            paths.append(f"src/example_{index:03d}.py")
            candidate = ReviewBaseline(
                cache_key=baseline.cache_key,
                policy_digest=baseline.policy_digest,
                complete=True,
                coverage_complete=True,
                reviewed_paths=tuple(paths),
            )
            try:
                candidate_record = SessionRecord.create(
                    IDENTITY,
                    now=FIXED_NOW,
                    dispositions=dispositions,
                    convergence_history={
                        "state": "completed",
                        "baseline": baseline_history_document(candidate),
                        "progress": [{"event": "completed", "generation": 1}],
                        "provenance": {"ledger_digest": "0" * 64},
                    },
                )
            except ReviewInputError:
                # One of the two component bounds refused; the previous
                # candidate is the largest storable record.
                break
            record = candidate_record
        assert record is not None
        size = len(
            json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        self.assertLessEqual(size, MAX_SESSION_RECORD_BYTES)
        self.assertGreater(size, MAX_SESSION_RECORD_BYTES - 1024)

        self.ledger._write(IDENTITY, record)
        restarted = LocalSessionLedger(Path(self.temp.name))
        loaded = restarted.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "ok")
        self.assertIsNotNone(loaded.record)
        assert loaded.record is not None
        self.assertEqual(loaded.record.record_sha256, record.record_sha256)
        self.assertEqual(loaded.record.convergence_history, record.convergence_history)
        self.assertEqual(loaded.record.dispositions, record.dispositions)

    def test_initialize_uses_exclusive_create_for_a_stale_missing_read(self):
        first = self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        del first
        second = LocalSessionLedger(Path(self.temp.name))
        with patch.object(
            second, "load", return_value=SessionLoadResult(status="missing")
        ):
            with self.assertRaisesRegex(ReviewInputError, "concurrent initialization"):
                second.initialize(IDENTITY, now=FIXED_NOW)

    def test_enrollment_witness_rejects_a_symlinked_directory(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (Path(self.temp.name) / ".enrollments").symlink_to(outside)
        # A symlinked witness directory fails closed and never becomes a write
        # target outside the operator-supplied ledger root.
        self.assertEqual(
            self.ledger.load(IDENTITY, now=FIXED_NOW).status, "integrity-failed"
        )
        with self.assertRaisesRegex(ReviewInputError, "integrity-failed"):
            self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        self.assertEqual(list(outside.iterdir()), [])

    def test_enrollment_witness_rejects_a_symlinked_file(self):
        witness_dir = Path(self.temp.name) / ".enrollments"
        witness_dir.mkdir()
        target = Path(self.temp.name) / "target"
        target.write_text("not a witness\n", encoding="utf-8")
        self.ledger._enrollment_path(IDENTITY).symlink_to(target)
        self.assertEqual(
            self.ledger.load(IDENTITY, now=FIXED_NOW).status, "integrity-failed"
        )
        with self.assertRaisesRegex(ReviewInputError, "integrity-failed"):
            self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        self.assertEqual(target.read_text(encoding="utf-8"), "not a witness\n")

    def test_reenroll_retires_an_expired_session_and_enrolls_a_fresh_one(self):
        self.ledger.initialize(IDENTITY, now=FIXED_NOW - timedelta(days=31))
        self.assertEqual(self.ledger.load(IDENTITY, now=FIXED_NOW).status, "expired")
        record = self.ledger.reenroll(IDENTITY, now=FIXED_NOW)
        self.assertEqual(record.generation, 0)
        self.assertEqual(record.completed_initial_reviews, 0)
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "ok")
        self.assertIsNotNone(loaded.record)
        # The fresh session round-trips through a new process with the witness
        # and record consistent again.
        restarted = LocalSessionLedger(Path(self.temp.name))
        self.assertEqual(restarted.load(IDENTITY, now=FIXED_NOW).status, "ok")

    def test_reenroll_recovers_a_witness_only_session(self):
        # The record was deleted after enrollment: the witness is the only
        # durable evidence, and an explicit maintainer recovery re-establishes
        # the session instead of leaving the identity wedged.
        self.ledger._create_enrollment_witness(IDENTITY)
        self.assertEqual(
            self.ledger.load(IDENTITY, now=FIXED_NOW).status, "integrity-failed"
        )
        record = self.ledger.reenroll(IDENTITY, now=FIXED_NOW)
        self.assertEqual(record.generation, 0)
        self.assertEqual(self.ledger.load(IDENTITY, now=FIXED_NOW).status, "ok")
        restarted = LocalSessionLedger(Path(self.temp.name))
        self.assertEqual(restarted.load(IDENTITY, now=FIXED_NOW).status, "ok")

    def test_reenroll_refuses_every_other_load_status(self):
        # No witness and no record: nothing to recover, so the ordinary
        # enrollment path stays the only way to create a session.
        with self.assertRaisesRegex(
            ReviewInputError, "only an expired or witness-only session"
        ):
            self.ledger.reenroll(IDENTITY, now=FIXED_NOW)
        # A live session is never reset by recovery.
        self.ledger.initialize(IDENTITY, now=FIXED_NOW)
        with self.assertRaisesRegex(
            ReviewInputError, "only an expired or witness-only session"
        ):
            self.ledger.reenroll(IDENTITY, now=FIXED_NOW)

    def test_initialize_reports_a_witness_race_without_a_record(self):
        # Run A wins the enrollment witness but has not yet written its
        # record. Run B loses the exclusive create and must fail closed with
        # the actual on-disk state instead of claiming a finished session.
        self.ledger._create_enrollment_witness(IDENTITY)
        self.assertFalse(self.ledger._path(IDENTITY).exists())
        second = LocalSessionLedger(Path(self.temp.name))
        with patch.object(
            second, "load", return_value=SessionLoadResult(status="missing")
        ):
            with self.assertRaisesRegex(
                ReviewInputError,
                "witness already exists without a session record",
            ):
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
            "updated_at": (FIXED_NOW + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        migrated = migrate_session_document(legacy)
        self.assertEqual(migrated["pull_request"], 136)
        path = self.ledger._path(IDENTITY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = self.ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "migrated")
        self.assertEqual(loaded.record.completed_initial_reviews, 1)
        self.assertEqual(
            loaded.record.updated_at,
            (FIXED_NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        )
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

    def test_commit_rejects_unknown_counter_increments(self):
        ledger = InMemorySessionLedger()
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="strict"),
            reservation_id="abcd1234",
            now=FIXED_NOW,
        )
        with patch(
            "review_sensei.session._apply_slot",
            return_value={"unexpected_counter": 1},
        ):
            with self.assertRaisesRegex(ReviewInputError, "increment is unsupported"):
                ledger.commit(
                    IDENTITY,
                    reservation_id="abcd1234",
                    expected_generation=prepared.record.generation,
                    now=FIXED_NOW,
                )

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
        self.assertEqual(
            abort_ledger.abort(
                IDENTITY,
                reservation_id="ffff1234",
                expected_generation=abort_prepared.record.generation,
                now=FIXED_NOW,
            ),
            aborted,
        )

        held_ledger = InMemorySessionLedger()
        held_prepared = prepare_session_round(
            held_ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="strict"),
            reservation_id="aaaa1234",
            now=FIXED_NOW,
        )
        released = held_ledger.abort(
            IDENTITY,
            reservation_id="aaaa1234",
            expected_generation=held_prepared.record.generation,
            now=FIXED_NOW,
        )
        competing = held_ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="bbbb1234",
            expected_generation=released.generation,
            now=FIXED_NOW,
        )
        with self.assertRaisesRegex(ReviewInputError, "reservation does not match"):
            held_ledger.abort(
                IDENTITY,
                reservation_id="aaaa1234",
                expected_generation=competing.generation,
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
    @staticmethod
    def _grant_attestation(**overrides):
        attestation = {
            "version": 1,
            "repository": IDENTITY.repository,
            "repository_id": IDENTITY.repository_id,
            "pull_request": IDENTITY.pull_request,
            "head_sha": "a" * 40,
            "operation": "command",
            "source_comment_id": 13579,
            "run_id": "10000000001",
            "issued_at": 1_700_000_000,
            "concurrency_group": "reviewsensei-session-99-136",
            "job_workflow_ref": (
                "malsabbagh/review-sensei/.github/workflows/"
                "review-sensei-run.yml@refs/tags/v5"
            ),
            "job_workflow_sha": "b" * 40,
            "actor": "octocat",
            "actor_type": "User",
            "association": "OWNER",
            "command_id": 13579,
            "command_digest": "c" * 64,
        }
        attestation.update(overrides)
        return attestation

    class _GrantVerifier:
        def __init__(self, returned=None, error=None):
            self.returned = returned
            self.error = error
            self.calls = []

        def verify_session_grant(self, grant, session_attestation):
            self.calls.append((grant, session_attestation))
            if self.error is not None:
                raise self.error
            return self.returned

    def _grant_bound_ledger(self, http, verifier, attestation=None):
        attestation = attestation or self._grant_attestation()
        return GitHubIssueCommentSessionLedger(
            http,
            token="token",
            broker=verifier,
            session_grant="g" * 43,
            session_attestation=attestation,
            head_sha="a" * 40,
        )

    def test_grant_bound_mutation_verifies_before_remote_discovery(self):
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
                json_response({"head": {"sha": "a" * 40}}),
                json_response({"id": 7, "body": reserved_body}),
                json_response([{"id": 7, "body": reserved_body}]),
            ]
        )
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)

        updated = ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="abcd1234",
            expected_generation=0,
            now=FIXED_NOW,
        )

        self.assertEqual(updated, reserved)
        self.assertEqual(verifier.calls, [("g" * 43, attestation)])
        self.assertEqual(
            [method for method, _url, _data in calls],
            ["GET", "GET", "GET", "PATCH", "GET"],
        )

    def test_missing_grant_bound_session_applies_command_in_one_remote_create(self):
        initial = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        paused = initial.evolve(
            now=FIXED_NOW,
            generation=1,
            operator_paused=True,
        )
        paused_body = render_session_comment(
            repository_id=99, pull_request=136, record=paused
        )
        http, calls = make_http(
            [
                json_response([]),
                json_response([]),
                json_response({"head": {"sha": "a" * 40}}),
                json_response({"id": 7, "body": paused_body}, status=201),
                json_response([{"id": 7, "body": paused_body}]),
            ]
        )
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)
        command = parse_maintainer_command("@sensei review pause", actor="octocat")
        self.assertIsNotNone(command)

        record, result = apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)

        self.assertEqual(record, paused)
        self.assertTrue(result.applied)
        self.assertEqual(verifier.calls, [("g" * 43, attestation)])
        self.assertEqual(
            [method for method, _url, _data in calls],
            ["GET", "GET", "GET", "POST", "GET"],
        )

    def test_missing_grant_bound_session_persists_disposition_in_one_remote_create(
        self,
    ):
        initial = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        disposition = {
            "fingerprint": "abcd1234abcd1234",
            "action": "dismiss",
            "reason": "accepted",
            "actor": "octocat",
            "head_sha": None,
            "expires_at": None,
        }
        persisted = initial.evolve(
            now=FIXED_NOW,
            generation=1,
            dispositions=(disposition,),
        )
        persisted_body = render_session_comment(
            repository_id=99, pull_request=136, record=persisted
        )
        http, calls = make_http(
            [
                json_response([]),
                json_response([]),
                json_response({"head": {"sha": "a" * 40}}),
                json_response({"id": 7, "body": persisted_body}, status=201),
                json_response([{"id": 7, "body": persisted_body}]),
            ]
        )
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)
        command = parse_maintainer_command(
            "@sensei dismiss abcd1234abcd1234 --reason accepted",
            actor="octocat",
        )
        self.assertIsNotNone(command)

        record, result = apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)

        self.assertEqual(record, persisted)
        self.assertTrue(result.applied)
        self.assertEqual(result.disposition.action, "dismiss")
        self.assertEqual(verifier.calls, [("g" * 43, attestation)])
        self.assertEqual(
            [method for method, _url, _data in calls],
            ["GET", "GET", "GET", "POST", "GET"],
        )

    def test_grant_bound_status_read_does_not_consume_mutation_authority(self):
        http, calls = make_http([json_response([])])
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)

        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "missing")
        self.assertEqual(verifier.calls, [])
        self.assertEqual([method for method, _url, _data in calls], ["GET"])

    def test_grant_bound_initialize_failure_consumes_one_attempt(self):
        http, calls = make_http(
            [
                json_response([]),
                json_response({"head": {"sha": "a" * 40}}),
                json_response({"error": "upstream"}, status=500),
                json_response([]),
            ]
        )
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)

        with self.assertRaisesRegex(
            GitHubPublicationTransientError, "could not be verified"
        ):
            ledger.initialize(IDENTITY, now=FIXED_NOW)
        verifier.error = GitHubBrokerClientError("grant already consumed")
        with self.assertRaisesRegex(ReviewInputError, "session grant"):
            ledger.initialize(IDENTITY, now=FIXED_NOW)

        self.assertEqual(len(verifier.calls), 2)
        self.assertEqual(
            [method for method, _url, _data in calls],
            ["GET", "GET", "POST", "GET"],
        )

    def test_grant_bound_initialize_rejects_a_stale_live_head_before_post(self):
        http, calls = make_http(
            [
                json_response([]),
                json_response({"head": {"sha": "d" * 40}}),
            ]
        )
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)

        with self.assertRaisesRegex(ReviewInputError, "head is stale"):
            ledger.initialize(IDENTITY, now=FIXED_NOW)

        self.assertEqual(verifier.calls, [("g" * 43, attestation)])
        self.assertEqual([method for method, _url, _data in calls], ["GET", "GET"])

    def test_grant_bound_replace_rejects_a_stale_live_head_before_patch(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        http, calls = make_http(
            [
                json_response([{"id": 7, "body": body}]),
                json_response([{"id": 7, "body": body}]),
                json_response({"head": {"sha": "d" * 40}}),
            ]
        )
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)

        with self.assertRaisesRegex(ReviewInputError, "head is stale"):
            ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id="abcd1234",
                expected_generation=0,
                now=FIXED_NOW,
            )

        self.assertEqual(verifier.calls, [("g" * 43, attestation)])
        self.assertEqual(
            [method for method, _url, _data in calls], ["GET", "GET", "GET"]
        )

    def test_grant_bound_mutation_rejects_invalid_or_mismatched_grants_before_io(self):
        cases = (
            ("replayed", self._GrantVerifier(error=GitHubBrokerClientError("invalid"))),
            (
                "mismatched",
                self._GrantVerifier(
                    returned=self._grant_attestation(repository_id=100)
                ),
            ),
            (
                "stale-head",
                self._GrantVerifier(
                    returned=self._grant_attestation(head_sha="d" * 40)
                ),
            ),
        )
        for name, verifier in cases:
            with self.subTest(name=name):
                http, calls = make_http([])
                ledger = self._grant_bound_ledger(http, verifier)
                with self.assertRaisesRegex(ReviewInputError, "session grant"):
                    ledger.initialize(IDENTITY, now=FIXED_NOW)
                self.assertEqual(len(verifier.calls), 1)
                self.assertEqual(calls, [])

    def test_grant_bound_ledger_rejects_absent_grant_configuration(self):
        http, _calls = make_http([])
        with self.assertRaisesRegex(ReviewInputError, "grant configuration"):
            GitHubIssueCommentSessionLedger(
                http,
                token="token",
                broker=self._GrantVerifier(),
                session_attestation=self._grant_attestation(),
                head_sha="a" * 40,
            )

    def test_restart_loads_integrity_checked_convergence_history(self):
        history = SessionRecordTests._history()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            convergence_history=history,
        )
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        http, _calls = make_http([json_response([{"id": 7, "body": body}])])
        loaded = GitHubIssueCommentSessionLedger(http, token="token").load(
            IDENTITY, now=FIXED_NOW
        )
        self.assertEqual(loaded.status, "ok")
        self.assertIsNotNone(loaded.record)
        assert loaded.record is not None
        self.assertEqual(loaded.record.completed_initial_reviews, 1)
        self.assertEqual(
            baseline_from_history_document(
                loaded.record.convergence_history["baseline"]
            ),
            baseline_from_history_document(history["baseline"]),
        )

    def test_status_reads_missing_remote_session_without_creating_comment(self):
        http, calls = make_http([json_response([])])
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        command = parse_maintainer_command("@sensei review status", actor="alice")
        _record, result = apply_session_command(
            ledger, IDENTITY, command, now=FIXED_NOW
        )
        self.assertEqual(result.action, "status")
        self.assertEqual([method for method, _url, _data in calls], ["GET"])

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

    def test_initialize_is_idempotent_when_another_writer_already_created(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        http, calls = make_http([json_response([{"id": 7, "body": body}])])
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        self.assertEqual(ledger.initialize(IDENTITY, now=FIXED_NOW), record)
        self.assertEqual([method for method, _url, _data in calls], ["GET"])

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

    def test_quoted_prefix_in_a_trusted_body_does_not_wedge_the_ledger(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        marker = (
            f"{SESSION_MARKER_PREFIX} repo=99 pr=136 gen=0 "
            f"digest={record.record_sha256} -->"
        )
        # The App is allowed to discuss its own marker without that prose
        # becoming established unreadable state for the pull request.
        quoted = f"Maintainer asked about {marker} in review."
        http, _calls = make_http(
            [
                json_response(
                    [
                        {
                            "id": 7,
                            "body": quoted,
                            "user": {
                                "login": "reviewsensei[bot]",
                                "type": "Bot",
                            },
                        }
                    ]
                )
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="token", app_slug="reviewsensei[bot]"
        )
        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "missing")

    def test_unknown_marker_version_fails_closed(self):
        future = (
            f"<!-- reviewsensei:session:v2 repo=99 pr=136 gen=0 digest={'a' * 64} -->"
        )
        http, _calls = make_http(
            [
                json_response(
                    [
                        {
                            "id": 7,
                            "body": future,
                            "user": {
                                "login": "reviewsensei[bot]",
                                "type": "Bot",
                            },
                        }
                    ]
                )
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="token", app_slug="reviewsensei[bot]"
        )
        # A newer schema must fail closed rather than be ignored and silently
        # duplicated; the documented re-enrollment path recovers the PR.
        self.assertEqual(
            ledger.load(IDENTITY, now=FIXED_NOW).status, "integrity-failed"
        )

    def test_reenroll_rewrites_an_expired_hosted_marker_in_place(self):
        expired = SessionRecord.create(IDENTITY, now=FIXED_NOW - timedelta(days=31))
        current = {
            "body": render_session_comment(
                repository_id=99, pull_request=136, record=expired
            )
        }
        calls: list[tuple[str, str, bytes | None]] = []

        def opener(request, timeout):
            del timeout
            calls.append((request.method, request.full_url, request.data))
            if request.method == "GET":
                return FakeHTTPResponse(
                    json.dumps(
                        [
                            {
                                "id": 7,
                                "body": current["body"],
                                "user": {
                                    "login": "reviewsensei[bot]",
                                    "type": "Bot",
                                },
                            }
                        ]
                    ).encode("utf-8"),
                    status=200,
                )
            current["body"] = json.loads(request.data.decode("utf-8"))["body"]
            return FakeHTTPResponse(
                json.dumps({"id": 7, "body": current["body"]}).encode("utf-8"),
                status=200,
            )

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        ledger = GitHubIssueCommentSessionLedger(
            http, token="token", app_slug="reviewsensei[bot]"
        )
        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "expired")
        calls.clear()

        record = ledger.reenroll(IDENTITY, now=FIXED_NOW)

        self.assertEqual(record.generation, 0)
        # Recovery rewrites the App's own expired comment instead of creating a
        # second marker, so the artifact the broker witnessed is restored. The
        # leading reads are the load and discovery; only the PATCH mutates.
        self.assertEqual(
            [method for method, _url, _data in calls], ["GET", "GET", "PATCH", "GET"]
        )
        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "ok")

    def test_grant_bound_reenroll_verifies_once_before_rewriting_expired_marker(self):
        expired = SessionRecord.create(IDENTITY, now=FIXED_NOW - timedelta(days=31))
        replacement = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        expired_body = render_session_comment(
            repository_id=99, pull_request=136, record=expired
        )
        replacement_body = render_session_comment(
            repository_id=99, pull_request=136, record=replacement
        )
        http, calls = make_http(
            [
                json_response([{"id": 7, "body": expired_body}]),
                json_response([{"id": 7, "body": expired_body}]),
                json_response([{"id": 7, "body": expired_body}]),
                json_response({"head": {"sha": "a" * 40}}),
                json_response({"id": 7, "body": replacement_body}),
                json_response([{"id": 7, "body": replacement_body}]),
            ]
        )
        attestation = self._grant_attestation()
        verifier = self._GrantVerifier(returned=attestation)
        ledger = self._grant_bound_ledger(http, verifier, attestation)
        command = parse_maintainer_command("@sensei review reenroll", actor="octocat")

        record, result = apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)

        self.assertEqual(record, replacement)
        self.assertTrue(result.applied)
        self.assertEqual(verifier.calls, [("g" * 43, attestation)])
        self.assertEqual(
            [method for method, _url, _data in calls],
            ["GET", "GET", "GET", "GET", "PATCH", "GET"],
        )

    def test_hosted_reenroll_recreates_a_deleted_marker(self):
        # The broker witness says this head was enrolled, so the operator
        # command establishes a fresh marker instead of leaving the pull
        # request blocked until the retention window elapses.
        posted = {"body": None}
        calls: list[tuple[str, str, bytes | None]] = []

        def opener(request, timeout):
            del timeout
            calls.append((request.method, request.full_url, request.data))
            if request.method == "POST":
                posted["body"] = json.loads(request.data.decode("utf-8"))["body"]
                return FakeHTTPResponse(
                    json.dumps({"id": 7, "body": posted["body"]}).encode("utf-8"),
                    status=201,
                )
            comments = (
                []
                if posted["body"] is None
                else [
                    {
                        "id": 7,
                        "body": posted["body"],
                        "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                    }
                ]
            )
            return FakeHTTPResponse(json.dumps(comments).encode("utf-8"), status=200)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        ledger = GitHubIssueCommentSessionLedger(
            http, token="token", app_slug="reviewsensei[bot]"
        )

        record = ledger.reenroll(IDENTITY, now=FIXED_NOW)

        self.assertEqual(record.generation, 0)
        # Only the POST mutates: the marker is created through the ordinary
        # initialization path once the operator has authenticated recovery.
        self.assertEqual(
            [method for method, _url, _data in calls],
            ["GET", "GET", "POST", "GET"],
        )
        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "ok")

    def test_hosted_reenroll_refuses_a_live_marker(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        body = render_session_comment(repository_id=99, pull_request=136, record=record)
        http, _calls = make_http(
            [
                json_response([{"id": 7, "body": body}]),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="token")
        with self.assertRaisesRegex(
            ReviewInputError, "only an expired or witness-only session"
        ):
            ledger.reenroll(IDENTITY, now=FIXED_NOW)

    def test_malformed_trusted_marker_fails_closed_without_reinitializing(self):
        malformed = f"{SESSION_MARKER_PREFIX} repo=99 pr=136 gen=0 digest=bad -->"
        http, _calls = make_http(
            [
                json_response(
                    [
                        {
                            "id": 7,
                            "body": malformed,
                            "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                        }
                    ]
                ),
                json_response(
                    [
                        {
                            "id": 7,
                            "body": malformed,
                            "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                        }
                    ]
                ),
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="token", app_slug="reviewsensei[bot]"
        )
        self.assertEqual(
            ledger.load(IDENTITY, now=FIXED_NOW).status, "integrity-failed"
        )
        with self.assertRaisesRegex(ReviewInputError, "integrity-failed"):
            ledger.initialize(IDENTITY, now=FIXED_NOW)

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

            def open_session(self, token, **kwargs):
                return type(
                    "Session", (), {"token": "session-token", "state": "enrolled"}
                )()

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

            def open_session(self, token, **kwargs):
                return type(
                    "Session", (), {"token": "session-token", "state": "enrolled"}
                )()

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

    def test_known_session_witness_refuses_a_deleted_comment_marker(self):
        class Broker:
            def open_session(self, token, **kwargs):
                return type(
                    "Session", (), {"token": "session-token", "state": "known"}
                )()

            def exchange(self, token, *, capability=None):
                return "publish-token"

        class Reviewer:
            def publish(self, **kwargs):
                raise AssertionError(
                    "a deleted session marker must stop before publish"
                )

        http, calls = make_http([json_response([])])
        application = GitHubApplication(
            broker=Broker(),
            http=http,
            reviewer=Reviewer(),
            learner=object(),
            replier=object(),
        )
        with self.assertRaisesRegex(
            ReviewSenseiError,
            "marker is missing; authenticated recovery is required",
        ):
            application.publish_review(
                options=GitHubWriteOptions(
                    auto_review=True,
                    github_writes=True,
                    github_session_ledger=True,
                ),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=99,
                pull_request=IDENTITY.pull_request,
                head_sha="a" * 40,
                base_branch="main",
                base_sha="b" * 40,
                result=ReviewResult(summary="ok", comments=(), provider="fixture"),
                diff="diff",
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
            )
        # The fail-closed check must run before any ledger call can re-create
        # the deleted session comment.
        self.assertEqual(
            [method for method, _url, _data in calls],
            ["GET"],
        )

    def test_hosted_session_ledger_requires_a_broker_attested_session_token(self):
        for token in ("", "   ", None):
            with self.subTest(token=token):

                class Broker:
                    def open_session(self, exchange_input, **kwargs):
                        return type("Session", (), {"token": token, "state": "known"})()

                    def exchange(self, exchange_input, *, capability=None):
                        return "publish-token"

                application = GitHubApplication(
                    broker=Broker(),
                    http=object(),
                    reviewer=object(),
                    learner=object(),
                    replier=object(),
                )
                bound_tokens: list[object] = []
                with patch.object(
                    GitHubApplication,
                    "_session_ledger_for_token",
                    autospec=True,
                    side_effect=lambda _self, value, **_kwargs: bound_tokens.append(
                        value
                    ),
                ):
                    with self.assertRaisesRegex(
                        ReviewSenseiError,
                        "hosted session ledger requires a broker-attested "
                        "session token",
                    ):
                        application.publish_review(
                            options=GitHubWriteOptions(
                                auto_review=True,
                                github_writes=True,
                                github_session_ledger=True,
                            ),
                            oidc_token="oidc",
                            repository=IDENTITY.repository,
                            repository_id=99,
                            pull_request=IDENTITY.pull_request,
                            head_sha="a" * 40,
                            base_branch="main",
                            base_sha="b" * 40,
                            result=ReviewResult(
                                summary="ok", comments=(), provider="fixture"
                            ),
                            diff="diff",
                            app_slug="reviewsensei[bot]",
                            convergence_policy=ReviewConvergencePolicy(
                                mode="merge-focused"
                            ),
                        )
                # The publication capability must never be handed to the session
                # ledger as a substitute for the session token.
                self.assertEqual(bound_tokens, [])

    def test_failed_publication_capability_does_not_record_an_enrollment(self):
        class Broker:
            def request_oidc_token(self):
                return "oidc"

            def exchange(self, exchange_input, *, capability=None):
                raise ReviewSenseiError("publication capability exchange failed")

            def open_session(self, exchange_input, **kwargs):
                raise AssertionError(
                    "an enrollment witness must not be recorded for a run that "
                    "never obtained the publication capability"
                )

        application = GitHubApplication(
            broker=Broker(),
            http=object(),
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        with self.assertRaisesRegex(
            ReviewSenseiError, "publication capability exchange failed"
        ):
            application.publish_review(
                options=GitHubWriteOptions(
                    auto_review=True,
                    github_writes=True,
                    github_session_ledger=True,
                ),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=99,
                pull_request=IDENTITY.pull_request,
                head_sha="a" * 40,
                base_branch="main",
                base_sha="b" * 40,
                result=ReviewResult(summary="ok", comments=(), provider="fixture"),
                diff="diff",
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
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

    def test_publication_cleanup_failure_does_not_mask_original_error(self):
        class Broker:
            def exchange(self, token, *, capability=None):
                return "capability-token"

        class Reviewer:
            def publish(self, **kwargs):
                raise RuntimeError("publication failed")

        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=Reviewer(),
            learner=object(),
            replier=object(),
            session_ledger=InMemorySessionLedger(),
        )
        with patch(
            "review_sensei.hosting.github.application.record_session_failed_attempt",
            side_effect=ValueError("cleanup failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "publication failed") as caught:
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
        self.assertEqual(
            loaded.record.last_committed_reservation_id,
            session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha="a" * 40,
                kind="publish",
            ),
        )

    def test_non_published_publication_aborts_without_counting(self):
        from review_sensei.hosting.github import PublicationResult

        class Broker:
            def exchange(self, token, *, capability=None):
                return "capability-token"

        class Reviewer:
            def publish(self, **kwargs):
                return PublicationResult(status="skipped_stale", review_id=None)

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
        self.assertEqual(result.status, "skipped_stale")
        loaded = ledger.load(IDENTITY)
        self.assertEqual(loaded.record.completed_initial_reviews, 0)
        self.assertIsNone(loaded.record.reservation_id)


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
