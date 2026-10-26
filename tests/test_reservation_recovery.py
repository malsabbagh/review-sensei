"""Known-owner Actions evidence and current run recovery; no age inference."""

from __future__ import annotations

import copy
import io
import json
import re
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from review_sensei_github.api_run_owner import ApiRunActionsEvidence
from review_sensei_github.application import resolve_hosted_session_ledger
from review_sensei_github.reservation_recovery import ActionsReservationEvidence
from review_sensei_github.review_hooks import GitHubReviewHooks
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)

from review_sensei.api_work import ApiExecutionOwner, ApiRunAccounting
from review_sensei.cli import main
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import MaintainerCommand, apply_session_command
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    prepare_review_transaction,
    record_session_failed_attempt,
)
from tests.fake_github_http import json_response, make_http
from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin
from tests.test_api_work_persistence import IDENTITY as CURRENT_IDENTITY
from tests.test_review_run import EntryFixture

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 7, 99)
HEAD = "a" * 40
LIVE_HEAD = "b" * 40
OWNER = {"run_id": "123", "head_sha": HEAD}


def analysis_record(*, owner=True):
    memory = InMemorySessionLedger()
    prepared = prepare_review_transaction(
        memory,
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
        reservation_owner=OWNER if owner else None, operator_paused=True, now=NOW
    )


class ActionsHarness:
    """Independent fake Actions metadata; no grant or mutable authority seam."""

    def __init__(self):
        self.run = {
            "id": 123,
            "repository": {"id": 99, "full_name": "owner/repo"},
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
            "event": "pull_request",
            "head_sha": HEAD,
            "run_started_at": "2026-10-06T11:59:00Z",
            "updated_at": "2026-10-06T12:01:00Z",
        }
        self.jobs = [
            {
                "id": 901,
                "run_id": 123,
                "run_attempt": 1,
                "head_sha": HEAD,
                "name": "ReviewSensei / hosted",
                "status": "completed",
                "conclusion": "failure",
                "completed_at": "2026-10-06T12:01:00Z",
            }
        ]
        self.total = None
        self.reads = 0
        self.on_run_read = lambda: None
        self.http, self.calls = make_http(
            [], routes=[(lambda _request: True, self.respond)]
        )

    def respond(self, request):
        if request.method != "GET":
            raise AssertionError("Actions completion evidence is read only")
        path = urlsplit(request.full_url).path
        if path.endswith("/jobs"):
            return json_response(
                {
                    "jobs": copy.deepcopy(self.jobs),
                    "total_count": len(self.jobs) if self.total is None else self.total,
                }
            )
        if re.fullmatch(r"/repos/owner/repo/actions/runs/[1-9][0-9]*", path):
            self.reads += 1
            self.on_run_read()
            return json_response(copy.deepcopy(self.run))
        raise AssertionError(path)

    def verify(self, record=None, *, command_run_id="456"):
        return ActionsReservationEvidence(
            self.http, token="actions-read", command_run_id=command_run_id
        ).verify(IDENTITY, record if record is not None else analysis_record())

    def ceased(self, *, owner=None):
        return ApiRunActionsEvidence(
            self.http, token="actions-read", command_run_id="456"
        ).ceased_run_owner(IDENTITY, 123, owner or ApiExecutionOwner(1, "hosted", 0))


class ReservationRecoveryTests(unittest.TestCase):
    def test_explicit_completed_owner_proof_is_read_only_and_reread(self):
        h = ActionsHarness()
        record = analysis_record()
        original = record.to_dict()
        self.assertEqual(h.verify(record), ("123", 1))
        self.assertEqual(h.reads, 2)
        self.assertEqual(record.to_dict(), original)
        self.assertTrue(all(method == "GET" for method, _url, _body in h.calls))
        self.assertFalse(
            any(
                "head_sha=" in url or "?status=" in url
                for _method, url, _body in h.calls
            )
        )

    def test_active_foreign_or_unknown_run_never_establishes_completion(self):
        for key, value in (
            ("id", 124),
            ("status", "in_progress"),
            ("status", "queued"),
            ("conclusion", None),
            ("conclusion", "unknown"),
            ("run_attempt", True),
            ("run_attempt", 0),
            ("repository", {"id": 88, "full_name": "owner/repo"}),
            ("repository", {"id": 99, "full_name": "foreign/repo"}),
        ):
            with self.subTest(key=key, value=value):
                h = ActionsHarness()
                h.run[key] = value
                with self.assertRaises(ReviewInputError):
                    h.verify()
                self.assertTrue(all(method == "GET" for method, _url, _body in h.calls))

    def test_missing_owner_refuses_before_http_despite_plausible_timestamps(self):
        for timestamp in (NOW - timedelta(days=365), NOW, NOW + timedelta(days=365)):
            for transaction in (True, False):
                with self.subTest(timestamp=timestamp, transaction=transaction):
                    record = analysis_record(owner=False)
                    if not transaction:
                        record = record.evolve(transaction=None, now=NOW)
                    h = ActionsHarness()
                    h.run.update(
                        run_started_at=timestamp.isoformat(),
                        updated_at=timestamp.isoformat(),
                    )
                    h.jobs[0]["completed_at"] = timestamp.isoformat()
                    with self.assertRaisesRegex(
                        ReviewInputError, "explicit authenticated owner"
                    ):
                        h.verify(record)
                    self.assertEqual(h.calls, [])
                    self.assertIsNotNone(record.reservation_id)

    def test_own_active_run_and_publication_work_refuse_before_http(self):
        h = ActionsHarness()
        with self.assertRaisesRegex(ReviewInputError, "own active run"):
            h.verify(command_run_id="123")
        self.assertEqual(h.calls, [])
        record = analysis_record()
        transaction = record.transaction.with_result("e" * 64)
        pending = record.evolve(
            reservation_id=None,
            reserved_slot=None,
            reservation_owner=None,
            last_committed_reservation_id=transaction.reservation_id,
            transaction=transaction,
            now=NOW,
        )
        with self.assertRaises(ReviewInputError):
            h.verify(pending)
        self.assertEqual(h.calls, [])
        self.assertEqual(pending.transaction, transaction)

    def test_rerun_or_changed_completion_between_reads_invalidates_proof(self):
        for change in ("attempt", "timestamp", "active"):
            with self.subTest(change=change):
                h = ActionsHarness()

                def race():
                    if h.reads == 2:
                        h.run.update(
                            {"run_attempt": 2}
                            if change == "attempt"
                            else {"updated_at": "2026-10-06T12:02:00Z"}
                            if change == "timestamp"
                            else {"status": "in_progress"}
                        )

                h.on_run_read = race
                with self.assertRaises(ReviewInputError):
                    h.verify()
                self.assertEqual(h.reads, 2)

    def test_exact_owner_job_and_run_cessation_are_both_required(self):
        h = ActionsHarness()
        self.assertEqual(h.ceased(), (123, 901, "2026-10-06T12:01:00Z", "failure"))
        self.assertEqual(len(h.calls), 2)
        for defect in (
            "active-run",
            "rerun",
            "foreign-run",
            "active-job",
            "wrong-job",
            "wrong-job-run",
            "wrong-attempt",
            "incomplete",
            "duplicate",
            "missing-stamp",
            "naive-stamp",
        ):
            with self.subTest(defect=defect):
                h = ActionsHarness()
                if defect == "active-run":
                    h.run["status"] = "in_progress"
                elif defect == "rerun":
                    h.run["run_attempt"] = 2
                elif defect == "foreign-run":
                    h.run["repository"]["id"] = 100
                elif defect == "active-job":
                    h.jobs[0]["status"] = "in_progress"
                elif defect == "wrong-job":
                    h.jobs[0]["name"] = "ReviewSensei / command"
                elif defect == "wrong-job-run":
                    h.jobs[0]["run_id"] = 124
                elif defect == "wrong-attempt":
                    h.jobs[0]["run_attempt"] = 2
                elif defect == "incomplete":
                    h.total = 2
                elif defect == "duplicate":
                    h.jobs.append(copy.deepcopy(h.jobs[0]))
                elif defect == "missing-stamp":
                    h.jobs[0].pop("completed_at")
                elif defect == "naive-stamp":
                    h.jobs[0]["completed_at"] = "2026-10-06T12:01:00"
                with self.assertRaises(ReviewInputError):
                    h.ceased()
                self.assertTrue(all(method == "GET" for method, _url, _body in h.calls))

    def test_current_owner_permits_unrelated_ci_and_rejects_overlapping_writers(self):
        for sibling in ("ordinary-ci", "command"):
            with self.subTest(sibling=sibling):
                h = ActionsHarness()
                h.run.update(
                    id=456, run_attempt=2, status="in_progress", conclusion=None
                )
                h.jobs[0].update(
                    run_id=456,
                    run_attempt=2,
                    status="in_progress",
                    conclusion=None,
                    completed_at=None,
                )
                h.jobs.append(
                    {**h.jobs[0], "id": 902, "name": "ReviewSensei / " + sibling}
                )
                evidence = ApiRunActionsEvidence(
                    h.http, token="actions-read", command_run_id="456"
                )
                if sibling == "ordinary-ci":
                    self.assertEqual(
                        evidence.current_owner(
                            IDENTITY, attempt=2, generation=1, event="pull_request"
                        ),
                        ApiExecutionOwner(2, "hosted", 1),
                    )
                else:
                    with self.assertRaisesRegex(ReviewInputError, "overlapping"):
                        evidence.current_owner(
                            IDENTITY, attempt=2, generation=1, event="pull_request"
                        )

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

    def test_old_phase_entrypoints_refuse_before_capability_or_http(self):
        http, calls = make_http([])
        broker = Mock()
        for version in (1, 2, 3):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(ReviewInputError, "phase ledgers are retired"),
            ):
                GitHubIssueCommentSessionLedger(
                    http,
                    token="unused",
                    app_slug="reviewsensei[bot]",
                    broker=broker,
                    session_grant="unused",
                    session_attestation={"version": version},
                    head_sha=HEAD,
                )
        with self.assertRaisesRegex(
            ReviewInputError, "phase ledger resolution is retired"
        ):
            resolve_hosted_session_ledger(
                broker=broker,
                http=http,
                oidc_token=None,
                repository=IDENTITY.repository,
                repository_id=99,
                pull_request=7,
                head_sha=HEAD,
            )
        with self.assertRaisesRegex(ReviewInputError, "phase entry is retired"):
            GitHubReviewHooks().resolve_ledger(Namespace(), resolved_head_sha=HEAD)
        self.assertEqual(calls, [])
        self.assertEqual(broker.mock_calls, [])

    def test_injected_native_owner_is_stored_in_the_first_reservation_write(self):
        state = {"record": SessionRecord.create(IDENTITY, now=NOW)}

        def response(request):
            if urlsplit(request.full_url).path.endswith("/pulls/7"):
                return json_response({"head": {"sha": HEAD}})
            if request.method == "PATCH":
                state["record"] = parse_session_comment(
                    json.loads(request.data)["body"], identity=IDENTITY
                )
            item = {
                "id": 42,
                "body": render_session_comment(
                    repository_id=99, pull_request=7, record=state["record"]
                ),
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
            }
            return json_response(item if request.method == "PATCH" else [item])

        http, calls = make_http([], routes=[(lambda _request: True, response)])
        ledger = GitHubIssueCommentSessionLedger(
            http,
            token="injected-native",
            app_slug="reviewsensei[bot]",
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
        self.assertEqual(sum(method == "PATCH" for method, _url, _data in calls), 1)

    def test_injected_native_unpause_without_a_hold_does_not_request_actions(self):
        state = {
            "record": SessionRecord.create(IDENTITY, now=NOW).evolve(
                operator_paused=True, now=NOW
            )
        }

        def response(request):
            if request.method == "PATCH":
                state["record"] = parse_session_comment(
                    json.loads(request.data)["body"], identity=IDENTITY
                )
            item = {
                "id": 42,
                "body": render_session_comment(
                    repository_id=99, pull_request=7, record=state["record"]
                ),
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
            }
            return json_response(item if request.method == "PATCH" else [item])

        http, calls = make_http([], routes=[(lambda _request: True, response)])
        actions = Mock(
            side_effect=AssertionError("unpause has no owner cessation to prove")
        )
        ledger = GitHubIssueCommentSessionLedger(
            http,
            token="injected-native",
            app_slug="reviewsensei[bot]",
            actions_token_provider=actions,
        )
        record, result = apply_session_command(
            ledger,
            IDENTITY,
            MaintainerCommand("continue", actor="maintainer", head_sha=HEAD),
            now=NOW,
        )
        self.assertTrue(result.applied)
        self.assertFalse(record.operator_paused)
        actions.assert_not_called()
        self.assertFalse(any("/actions/" in url for _method, url, _data in calls))

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

    def test_local_failed_attempt_saturates_without_reopening_same_head(self):
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
        held = ledger.replace(
            IDENTITY,
            lambda record: record.evolve(
                failed_attempts=32, failed_attempts_head_sha=HEAD, now=NOW
            ),
            now=NOW,
        )
        charged = record_session_failed_attempt(
            ledger,
            IDENTITY,
            reservation_id=prepared.reservation_id,
            expected_generation=held.generation,
            head_sha=HEAD,
            now=NOW,
        )
        self.assertEqual(charged.failed_attempts, 32)
        self.assertEqual(charged.failed_attempts_head_sha, HEAD)
        self.assertEqual(charged.completed_initial_reviews, 0)
        replay = record_session_failed_attempt(
            ledger,
            IDENTITY,
            reservation_id=prepared.reservation_id,
            head_sha=HEAD,
            now=NOW,
        )
        self.assertEqual(replay, charged)
        from review_sensei.session import prepare_session_round

        refused = prepare_session_round(
            ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="1" * 64,
            head_sha=HEAD,
            now=NOW,
        )
        self.assertFalse(refused.decision.admit)


class PreProviderFailureCleanupTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def test_context_failure_before_provider_call_cleans_up_and_counts_once(self):
        diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n keep\n+change\n"
        for transaction in (False, True):
            with (
                self.subTest(transaction=transaction),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                (root / "review.patch").write_text(diff, encoding="utf-8", newline="\n")
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


class AdmissionHookTests(unittest.TestCase):
    """prepare_session_round: paused gate first, self-heal second."""

    def _held_ledger(self, *, paused: bool, recoverable: bool):
        ledger = InMemorySessionLedger()
        identity = SessionIdentity("owner/repo", 7, 99)
        prepare_review_transaction(
            ledger,
            identity,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="d" * 64,
            base_sha="c" * 40,
            head_sha=HEAD,
            configuration_digest="e" * 64,
            evidence_digest="f" * 64,
            now=NOW,
        )
        if paused:
            ledger.replace(
                identity,
                lambda r: r.evolve(operator_paused=True, now=NOW),
                now=NOW,
            )
        calls = []

        def _recover(_identity, *, now=None):
            calls.append("recover")
            if not recoverable:
                raise ReviewInputError(
                    "session reservation is held by run 123 (still running)"
                )
            return ledger.replace(
                _identity,
                lambda r: r.evolve(
                    reservation_id=None,
                    reserved_slot=None,
                    transaction=None,
                    last_committed_reservation_id="d" * 64,
                    now=now,
                ),
                now=now,
            )

        ledger.recover_abandoned_analysis = _recover  # type: ignore[attr-defined]
        return ledger, identity, calls

    def test_paused_session_never_attempts_recovery(self):
        from review_sensei.session import prepare_session_round

        ledger, identity, calls = self._held_ledger(paused=True, recoverable=True)
        prepared = prepare_session_round(
            ledger,
            identity,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="1" * 64,
            now=NOW,
            head_sha=HEAD,
        )
        self.assertEqual(calls, [])
        self.assertFalse(prepared.decision.admit)

    def test_unpaused_held_session_heals_then_admits_current_head(self):
        from review_sensei.session import prepare_session_round

        ledger, identity, calls = self._held_ledger(paused=False, recoverable=True)
        prepared = prepare_session_round(
            ledger,
            identity,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="1" * 64,
            now=NOW,
            head_sha=HEAD,
        )
        self.assertEqual(calls, ["recover"])
        self.assertTrue(prepared.decision.admit)

    def test_failed_recovery_stays_blocked_as_today(self):
        from review_sensei.session import prepare_session_round

        ledger, identity, calls = self._held_ledger(paused=False, recoverable=False)
        prepared = prepare_session_round(
            ledger,
            identity,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="1" * 64,
            now=NOW,
            head_sha=HEAD,
        )
        self.assertEqual(calls, ["recover"])
        self.assertFalse(prepared.decision.admit)


class RecoveryRaceFixture(EntryFixture):
    """Change authenticated predecessor metadata/root at the second proof."""

    def __init__(self, directory):
        super().__init__(directory, limit=512)
        self.predecessor_reads = 0
        self.race = None

    def external(self, request, timeout):
        response = super().external(request, timeout)
        path = urlsplit(request.full_url).path
        old_jobs = self.attempt == 2 and path.endswith("/attempts/1/jobs")
        if not old_jobs:
            return response
        self.predecessor_reads += 1
        value = json.loads(response.read(524288))
        if self.race == "completion" and self.predecessor_reads >= 2:
            value["jobs"][0]["completed_at"] = "2026-10-26T00:01:00Z"
        if self.race == "owner" and self.predecessor_reads == 2:

            def takeover(record):
                accounting = ApiRunAccounting.from_document(record.api_run_accounting)
                successor = accounting.claim_startup(ApiExecutionOwner(2, "hosted", 1))
                return record.evolve(
                    generation=record.generation + 1,
                    api_run_accounting=successor.to_document(),
                )

            self.rewrite_record(mutate=takeover)
        return json_response(value)


class CurrentReservationRunTests(unittest.TestCase):
    def pending(self, fixture):
        def interrupted():
            raise KeyboardInterrupt()

        fixture.before_publication = interrupted
        self.assertEqual(fixture.invoke(), 130, fixture.error)
        record = parse_session_comment(
            fixture.comment["body"], identity=CURRENT_IDENTITY
        )
        self.assertEqual(record.transaction.phase, "publication_pending")
        self.assertEqual(fixture.provider_calls, 1)
        fixture.before_publication = None
        return record

    def test_proven_same_run_successor_reuses_exact_retained_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            f = RecoveryRaceFixture(directory)
            pending = self.pending(f)
            predecessor = ApiRunAccounting.from_document(pending.api_run_accounting)
            f.attempt = 2
            self.assertEqual(f.invoke(), 0, f.error)
            final = parse_session_comment(f.comment["body"], identity=CURRENT_IDENTITY)
            self.assertEqual(
                final.transaction.transaction_id, pending.transaction.transaction_id
            )
            self.assertEqual(
                final.transaction.result_sha256, pending.transaction.result_sha256
            )
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertEqual(
                f.accounted.accounting.blocks[: len(predecessor.blocks)],
                predecessor.blocks,
            )
            self.assertEqual(f.accounted.accounting.owner.run_attempt, 2)
            self.assertEqual(f.provider_calls, 1)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)

    def test_active_changed_or_foreign_predecessor_never_replays_pending_work(self):
        for defect in ("active", "completion", "owner", "head", "run"):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = RecoveryRaceFixture(directory)
                pending = self.pending(f)
                original = f.comment["body"]
                f.attempt = 2
                if defect == "active":
                    f.previous_active = True
                elif defect in {"completion", "owner"}:
                    f.race = defect
                elif defect == "head":
                    f.live_head = "f" * 40
                else:
                    f.run_id += 1
                before = len(f.trace)
                self.assertEqual(f.invoke(), 1)
                current = parse_session_comment(
                    f.comment["body"], identity=CURRENT_IDENTITY
                )
                self.assertEqual(current.transaction, pending.transaction)
                self.assertEqual(f.provider_calls, 1)
                self.assertFalse(
                    any(
                        method == "PATCH"
                        for _attempt, method, _path in f.trace[before:]
                    )
                )
                self.assertFalse(f.reviews)
                if defect == "owner":
                    self.assertEqual(
                        current.api_run_accounting["owner"],
                        {"run_attempt": 2, "job": "hosted", "generation": 1},
                    )
                else:
                    self.assertEqual(f.comment["body"], original)
                refused_trace = len(f.trace)
                self.assertEqual(f.invoke(), 1)
                self.assertEqual(len(f.trace), refused_trace)
                self.assertEqual(f.provider_calls, 1)

    def test_missing_reservation_owner_never_gains_timestamp_recovery_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            f = RecoveryRaceFixture(directory)

            def lose_owner():
                f.rewrite_record(
                    mutate=lambda record: record.evolve(
                        generation=record.generation + 1, reservation_owner=None
                    )
                )
                raise ReviewInputError("external owner binding changed before entry")

            f.before_entry = lose_owner
            self.assertEqual(f.invoke(), 1)
            held = parse_session_comment(f.comment["body"], identity=CURRENT_IDENTITY)
            self.assertIsNone(held.reservation_owner)
            self.assertIsNotNone(held.reservation_id)
            self.assertIsNone(held.api_run_accounting["analysis_abort"])
            before = f.comment["body"]
            f.before_entry = None
            f.next_run()
            self.assertEqual(f.invoke(), 1)
            self.assertEqual(f.comment["body"], before)
            self.assertEqual(f.provider_calls, 0)
            self.assertFalse(f.reviews)

    def test_provider_unknown_never_becomes_a_clean_abort_or_new_run(self):
        for error in (
            ProviderError("provider outcome unknown", transient=True),
            TimeoutError("provider deadline"),
            KeyboardInterrupt(),
        ):
            with (
                self.subTest(error=type(error).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = RecoveryRaceFixture(directory)

                def interrupted():
                    raise error

                f.before_provider = interrupted
                self.assertEqual(
                    f.invoke(), 130 if isinstance(error, KeyboardInterrupt) else 1
                )
                failed = parse_session_comment(
                    f.comment["body"], identity=CURRENT_IDENTITY
                )
                self.assertIsNone(failed.api_run_accounting["analysis_abort"])
                # An uncertain provider operation is not fabricated into a
                # completed failed analysis; its caller allocations stay spent.
                self.assertEqual(failed.failed_attempts, 0)
                self.assertEqual(failed.completed_initial_reviews, 0)
                self.assertGreater(f.accounted.accounting.confirmed, 0)
                attempts = f.provider_calls
                self.assertGreater(attempts, 0)
                before = f.comment["body"]
                f.before_provider = None
                f.next_run()
                self.assertEqual(f.invoke(), 1)
                self.assertEqual(f.comment["body"], before)
                self.assertEqual(f.provider_calls, attempts)
                self.assertFalse(f.reviews)


if __name__ == "__main__":
    unittest.main()
