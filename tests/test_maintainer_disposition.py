from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import (
    MAINTAINER_ACTIONS,
    FindingDisposition,
    apply_session_command,
    authorized_maintainer,
    parse_maintainer_command,
    render_convergence_summary,
    session_dispositions,
)
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions
from review_sensei.hosting.github.errors import GitHubPublicationError
from review_sensei.hosting.github.trigger import resolve_issue_comment
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    prepare_session_round,
)

FIXED_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)


def _pull() -> dict[str, object]:
    return {
        "number": 136,
        "title": "review loops",
        "head": {"sha": "b" * 40, "ref": "feature"},
        "base": {"sha": "c" * 40, "ref": "main"},
    }


class MaintainerCommandParseTests(unittest.TestCase):
    def test_supported_commands_parse(self):
        status = parse_maintainer_command("@sensei review status", actor="alice")
        self.assertIsNotNone(status)
        self.assertEqual(status.action, "status")
        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        self.assertEqual(pause.action, "pause")
        verify = parse_maintainer_command("@sensei verify", actor="alice")
        self.assertEqual(verify.action, "verify")
        cont = parse_maintainer_command("@sensei review continue", actor="alice")
        self.assertEqual(cont.action, "continue")
        dismissed = parse_maintainer_command(
            "@sensei dismiss abcd1234abcd1234 --reason accepted architecture",
            actor="alice",
        )
        self.assertEqual(dismissed.action, "dismiss")
        self.assertEqual(dismissed.reason, "accepted architecture")
        reenroll = parse_maintainer_command("@sensei review reenroll", actor="alice")
        self.assertEqual(reenroll.action, "reenroll")

    def test_action_identifiers_are_lowercase_for_any_action_casing(self):
        """The grammar is case-insensitive; dispatch keys stay in the closed set."""

        cases = (
            ("@sensei review STATUS", "status"),
            ("@sensei review PAUSE", "pause"),
            ("@sensei review CONTINUE", "continue"),
            ("@sensei review REENROLL", "reenroll"),
            ("@sensei VERIFY", "verify"),
            ("@sensei DISMISS abcd1234abcd1234 --reason accepted", "dismiss"),
            ("@sensei DEFER abcd1234abcd1234 --reason accepted", "defer"),
            ("@sensei ACCEPT-RISK abcd1234abcd1234 --reason accepted", "accept-risk"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                command = parse_maintainer_command(body, actor="alice")
                self.assertIsNotNone(command)
                self.assertEqual(command.action, expected)
                self.assertIn(command.action, MAINTAINER_ACTIONS)

    def test_retired_rounds_option_is_not_a_command(self):
        # Rounds are uncapped, so no command grants an allowance. The retired
        # ``--rounds`` spellings must not be silently reinterpreted as a plain
        # continue/reenroll: the operator asks for a budget that no longer
        # exists, and honouring it would misrepresent what the engine will do.
        for body in (
            "@sensei review continue --rounds 0",
            "@sensei review continue --rounds 1",
            "@sensei review reenroll --rounds 1",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_maintainer_command(body, actor="alice"))

    def test_unknown_or_bot_self_commands_are_ignored(self):
        self.assertIsNone(
            parse_maintainer_command("@sensei please re-scan", actor="alice")
        )
        self.assertFalse(
            authorized_maintainer(
                login="ReviewSensei[BOT]",
                user_type="Bot",
                association="OWNER",
                app_slug="reviewsensei[bot]",
            )
        )
        self.assertFalse(
            authorized_maintainer(
                login="alice",
                user_type="User",
                association="CONTRIBUTOR",
                app_slug="reviewsensei[bot]",
            )
        )
        self.assertTrue(
            authorized_maintainer(
                login="alice",
                user_type="User",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
            )
        )

    def test_mention_boundary_accepts_markdown_and_whitespace(self):
        self.assertEqual(
            parse_maintainer_command(
                "**operator note** @sensei\nreview pause", actor="alice"
            ).action,
            "pause",
        )
        self.assertEqual(
            parse_maintainer_command(
                "note @sensei\treview pause", actor="alice"
            ).action,
            "pause",
        )
        for body in (
            "@sensei!review pause",
            "@sensei-review pause",
            "@senseis review pause",
            "!@sensei review pause",
            "(@sensei review pause",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_maintainer_command(body, actor="alice"))

    def test_command_parser_matches_shared_cross_runtime_fixture(self):
        fixture_path = (
            Path(__file__).parents[1]
            / "tests"
            / "fixtures"
            / "maintainer-command-parity.json"
        )
        cases = json.loads(fixture_path.read_text(encoding="utf-8"))
        for case in cases:
            body = case["body"]
            try:
                parsed = parse_maintainer_command(body, actor="alice")
            except ReviewInputError:
                parsed = None
            with self.subTest(body=body):
                self.assertEqual(parsed is not None, case["accepted"])

    def test_finding_disposition_requires_reason(self):
        self.assertIsNone(
            parse_maintainer_command("@sensei dismiss abcd1234abcd1234", actor="alice")
        )

    def test_finding_disposition_enforces_storage_bounds(self):
        with self.assertRaisesRegex(ReviewInputError, "actor"):
            FindingDisposition(
                fingerprint="abcd1234abcd1234",
                action="dismiss",
                reason="accepted",
                actor="alice\n",
            )
        with self.assertRaisesRegex(ReviewInputError, "head_sha"):
            FindingDisposition(
                fingerprint="abcd1234abcd1234",
                action="dismiss",
                reason="accepted",
                actor="alice",
                head_sha="not-a-sha",
            )

    def test_finding_disposition_requires_a_bounded_printable_reason(self):
        with self.assertRaisesRegex(ReviewInputError, "reason"):
            FindingDisposition(
                fingerprint="abcd1234abcd1234",
                action="dismiss",
                reason="",
                actor="alice",
            )

    def test_head_bound_disposition_does_not_carry_to_another_head(self):
        disposition = FindingDisposition(
            fingerprint="abcd1234abcd1234",
            action="dismiss",
            reason="accepted",
            actor="alice",
            head_sha="a" * 40,
        )
        self.assertTrue(disposition.honors("abcd1234abcd1234", head_sha="a" * 40))
        self.assertFalse(disposition.honors("abcd1234abcd1234", head_sha="b" * 40))
        self.assertFalse(disposition.honors("abcd1234abcd1234"))


class SessionCommandTests(unittest.TestCase):
    def test_broker_bound_ledger_without_atomic_initialization_fails_closed(self):
        ledger = InMemorySessionLedger()
        ledger._broker = object()  # type: ignore[attr-defined]
        command = parse_maintainer_command("@sensei review pause", actor="alice")
        assert command is not None

        with self.assertRaisesRegex(ReviewInputError, "atomic initialization"):
            apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)
        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "missing")

    def test_identified_continuation_is_only_an_unpause(self):
        ledger = InMemorySessionLedger()
        policy = ReviewConvergencePolicy(mode="merge-focused")
        command = parse_maintainer_command(
            "@sensei review continue",
            actor="alice",
            head_sha="a" * 40,
            command_id="issue-comment-101",
        )
        record, result = apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)

        self.assertTrue(result.applied)
        self.assertFalse(record.operator_paused)
        self.assertEqual(record.continuation_grants, ())
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            policy,
            reservation_id="abcd1234",
            head_sha="a" * 40,
            now=FIXED_NOW,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        self.assertTrue(prepared.decision.admit)

    def test_identified_continuation_command_is_idempotent(self):
        ledger = InMemorySessionLedger()
        command = parse_maintainer_command(
            "@sensei review continue",
            actor="alice",
            head_sha="a" * 40,
            command_id="issue-comment-102",
        )
        first, _ = apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)
        replay, _ = apply_session_command(
            ledger, IDENTITY, command, now=FIXED_NOW + timedelta(minutes=1)
        )
        self.assertEqual(replay.generation, first.generation)
        self.assertEqual(replay.continuation_grants, ())

    def test_identified_continuation_uses_a_generation_guard(self):
        class RacingLedger(InMemorySessionLedger):
            racing = False

            def replace(self, identity, mutate, *, now=None):
                loaded = self.load(identity, now=now)
                assert loaded.record is not None
                if not self.racing:
                    return super().replace(identity, mutate, now=now)
                stale = loaded.record.evolve(
                    now=now, generation=loaded.record.generation + 1
                )
                return mutate(stale)

        ledger = RacingLedger()
        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        apply_session_command(ledger, IDENTITY, pause, now=FIXED_NOW)
        ledger.racing = True
        command = parse_maintainer_command(
            "@sensei review continue",
            actor="alice",
            head_sha="a" * 40,
            command_id="issue-comment-race",
        )
        with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
            apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        assert loaded.record is not None
        self.assertTrue(loaded.record.operator_paused)

    def test_pause_and_continue_mutate_operator_paused(self):
        ledger = InMemorySessionLedger()
        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        record, result = apply_session_command(ledger, IDENTITY, pause, now=FIXED_NOW)
        self.assertTrue(result.applied)
        self.assertTrue(record.operator_paused)
        policy = ReviewConvergencePolicy(mode="merge-focused")
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            policy,
            reservation_id="abcd1234",
            now=FIXED_NOW,
        )
        self.assertFalse(prepared.decision.admit)
        self.assertEqual(prepared.decision.handoff_reason, "paused")
        cont = parse_maintainer_command("@sensei review continue", actor="alice")
        record, result = apply_session_command(ledger, IDENTITY, cont, now=FIXED_NOW)
        self.assertFalse(record.operator_paused)
        self.assertEqual(
            result.summary, "automated review may continue; rounds are uncapped"
        )

        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        paused, _result = apply_session_command(ledger, IDENTITY, pause, now=FIXED_NOW)
        reserved = ledger.reserve(
            IDENTITY,
            slot="initial",
            reservation_id="abcd5678",
            expected_generation=paused.generation,
            now=FIXED_NOW,
        )
        committed = ledger.commit(
            IDENTITY,
            reservation_id="abcd5678",
            expected_generation=reserved.generation,
            now=FIXED_NOW,
        )
        self.assertTrue(committed.operator_paused)

    def test_reenroll_recovers_an_expired_session_and_refuses_live_ones(self):
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalSessionLedger(Path(temp_dir))
            ledger.initialize(IDENTITY, now=FIXED_NOW - timedelta(days=31))
            command = parse_maintainer_command("@sensei review reenroll", actor="alice")
            record, result = apply_session_command(
                ledger, IDENTITY, command, now=FIXED_NOW
            )
            self.assertTrue(result.applied)
            self.assertEqual(result.action, "reenroll")
            self.assertEqual(record.generation, 0)
            self.assertIn("session re-enrolled", result.summary)
            # The recovered session admits a round again, which is what makes
            # this the documented operator path out of an expired session.
            prepared = prepare_session_round(
                ledger,
                IDENTITY,
                ReviewConvergencePolicy(mode="merge-focused"),
                reservation_id="abcd1234",
                now=FIXED_NOW,
            )
            self.assertTrue(prepared.decision.admit)
            # A live session is not silently re-enrolled: recovery is only for
            # state an operator has confirmed is expired.
            with self.assertRaisesRegex(
                ReviewInputError, "only an expired or witness-only session"
            ):
                apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)

    def test_verify_does_not_unpause_without_evidence(self):
        ledger = InMemorySessionLedger()
        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        apply_session_command(ledger, IDENTITY, pause, now=FIXED_NOW)
        verify = parse_maintainer_command("@sensei verify", actor="alice")
        record, result = apply_session_command(ledger, IDENTITY, verify, now=FIXED_NOW)
        self.assertFalse(result.applied)
        self.assertTrue(record.operator_paused)
        self.assertIn("evidence-backed", result.summary)

    def test_status_reports_the_authoritative_head_binding(self):
        ledger = InMemorySessionLedger()
        command = parse_maintainer_command(
            "@sensei review status", actor="alice", head_sha="a" * 40
        )
        _record, result = apply_session_command(
            ledger, IDENTITY, command, now=FIXED_NOW
        )
        self.assertIn(f"head={'a' * 40}", result.summary)

    def test_disposition_is_not_a_verified_fix(self):
        ledger = InMemorySessionLedger()
        command = parse_maintainer_command(
            "@sensei accept-risk abcd1234abcd1234 --reason launch exception",
            actor="alice",
        )
        _record, result = apply_session_command(
            ledger, IDENTITY, command, now=FIXED_NOW
        )
        self.assertIn("not an independently verified fix", result.summary)
        self.assertEqual(result.disposition.action, "accept-risk")
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(len(loaded.record.dispositions), 1)
        self.assertEqual(
            session_dispositions(loaded.record)[0].fingerprint, "abcd1234abcd1234"
        )

    def test_fifth_disposition_is_rejected_without_corrupting_the_record(self):
        ledger = InMemorySessionLedger()
        for index in range(4):
            command = parse_maintainer_command(
                f"@sensei dismiss {index + 1:016x} --reason accepted",
                actor="alice",
            )
            apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)
        fifth = parse_maintainer_command(
            "@sensei dismiss 0000000000000005 --reason accepted", actor="alice"
        )
        with self.assertRaisesRegex(ReviewInputError, "limit"):
            apply_session_command(ledger, IDENTITY, fifth, now=FIXED_NOW)
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(len(loaded.record.dispositions), 4)

    def test_operator_pause_requires_public_cas_replace(self):
        class LegacyLedger:
            def load(self, identity, *, now=None):
                return ledger.load(identity, now=now)

            def initialize(self, identity, *, now=None, expires_at=None):
                return ledger.initialize(identity, now=now, expires_at=expires_at)

            def _write(self, identity, record):
                return None

        ledger = InMemorySessionLedger()
        command = parse_maintainer_command("@sensei review pause", actor="alice")
        with self.assertRaisesRegex(ReviewInputError, "CAS"):
            apply_session_command(LegacyLedger(), IDENTITY, command, now=FIXED_NOW)


class TriggerCommandTests(unittest.TestCase):
    def test_issue_comment_routes_maintainer_command(self):
        resolution = resolve_issue_comment("@sensei review pause", _pull())
        self.assertEqual(resolution.operation, "command")

    def test_issue_comment_routes_finding_command_and_plain_text_to_reply(self):
        finding = resolve_issue_comment(
            "@sensei dismiss abcd1234abcd1234 --reason accepted", _pull()
        )
        self.assertEqual(finding.operation, "command")
        plain = resolve_issue_comment("please take a look", _pull())
        self.assertEqual(plain.operation, "reply")


class DisabledWriteTests(unittest.TestCase):
    def test_disabled_writes_do_not_exchange_broker_capability(self):
        class Broker:
            def __init__(self):
                self.exchanges = []

            def request_oidc_token(self):
                self.exchanges.append("oidc")
                return "oidc"

            def exchange(self, token, *, capability=None):
                self.exchanges.append(capability)
                return "token"

        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
            session_ledger=InMemorySessionLedger(),
        )
        result = application.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=False),
            oidc_token="oidc",
            repository="owner/repo",
            repository_id=99,
            pull_request=136,
            head_sha="b" * 40,
            body="@sensei review pause",
            actor_login="alice",
            association="MEMBER",
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(result.summary, "writes_disabled")
        self.assertEqual(application.broker.exchanges, [])

    def test_hosted_disabled_mutation_does_not_authorize_a_session_grant(self):
        class Broker:
            def __init__(self):
                self.calls = []

            def request_oidc_token(self):
                self.calls.append("request_oidc_token")
                raise AssertionError("writes-disabled mutation must not request OIDC")

            def authorize_session_mutation(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                raise AssertionError(
                    "writes-disabled mutation must not authorize a session grant"
                )

        broker = Broker()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        result = application.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=False, github_session_ledger=True),
            oidc_token="oidc",
            repository="owner/repo",
            repository_id=99,
            pull_request=136,
            head_sha="b" * 40,
            body="@sensei review pause",
            actor_login="alice",
            association="MEMBER",
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(result.summary, "writes_disabled")
        self.assertEqual(broker.calls, [])

    def test_local_status_does_not_exchange_write_capability(self):
        class Broker:
            def __init__(self):
                self.exchanges = []

            def exchange(self, token, *, capability=None):
                self.exchanges.append(capability)
                return "token"

        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
            session_ledger=InMemorySessionLedger(),
        )
        result = application.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=False),
            oidc_token=None,
            repository="owner/repo",
            repository_id=99,
            pull_request=136,
            head_sha="b" * 40,
            body="@sensei review status",
            actor_login="alice",
            association="MEMBER",
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(result.action, "status")
        self.assertEqual(application.broker.exchanges, [])

    def test_hosted_status_uses_read_only_capability(self):
        class Broker:
            def __init__(self):
                self.exchanges = []

            def exchange(self, token, *, capability=None):
                self.exchanges.append(capability)
                return "token"

        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        ledger = InMemorySessionLedger()
        with patch.object(
            application, "_session_ledger_for_token", return_value=ledger
        ):
            result = application.apply_maintainer_command(
                options=GitHubWriteOptions(
                    github_writes=False, github_session_ledger=True
                ),
                oidc_token="oidc",
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="b" * 40,
                body="@sensei review status",
                actor_login="alice",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
            )
        self.assertEqual(result.action, "status")
        self.assertEqual(application.broker.exchanges, ["review_status"])

    def test_hosted_status_does_not_read_an_injected_local_ledger(self):
        class Broker:
            def __init__(self):
                self.exchanges = []

            def exchange(self, token, *, capability=None):
                self.exchanges.append(capability)
                return "token"

        local_ledger = InMemorySessionLedger()
        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        apply_session_command(local_ledger, IDENTITY, pause, now=FIXED_NOW)
        remote_ledger = InMemorySessionLedger()
        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
            session_ledger=local_ledger,
        )
        with patch.object(
            application, "_session_ledger_for_token", return_value=remote_ledger
        ) as get_ledger:
            result = application.apply_maintainer_command(
                options=GitHubWriteOptions(
                    github_writes=False, github_session_ledger=True
                ),
                oidc_token="oidc",
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="b" * 40,
                body="@sensei review status",
                actor_login="alice",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
            )
        self.assertIn("paused=False", result.summary)
        self.assertTrue(get_ledger.call_args.kwargs["prefer_remote"])

    def test_hosted_status_requires_caller_oidc_token(self):
        class Broker:
            def __init__(self):
                self.exchanges = []
                self.oidc_requests = 0

            def request_oidc_token(self):
                self.oidc_requests += 1
                return "minted-oidc"

            def exchange(self, token, *, capability=None):
                self.exchanges.append(capability)
                return "token"

        broker = Broker()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        with patch.object(
            application,
            "_session_ledger_for_token",
            return_value=InMemorySessionLedger(),
        ):
            with self.assertRaisesRegex(GitHubPublicationError, "caller-supplied"):
                application.apply_maintainer_command(
                    options=GitHubWriteOptions(
                        github_writes=False, github_session_ledger=True
                    ),
                    oidc_token=None,
                    repository="owner/repo",
                    repository_id=99,
                    pull_request=136,
                    head_sha="b" * 40,
                    body="@sensei review status",
                    actor_login="alice",
                    association="MEMBER",
                    app_slug="reviewsensei[bot]",
                )
        self.assertEqual(broker.exchanges, [])
        self.assertEqual(broker.oidc_requests, 0)

    def test_hosted_mutation_requires_broker_attested_command_grant(self):
        body = "@sensei review pause"
        request_attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 99,
            "pull_request": 136,
            "head_sha": "b" * 40,
            "operation": "command",
            "source_comment_id": 501,
            "run_id": "42",
            "issued_at": 1,
            "concurrency_group": "reviewsensei-session-99-136",
            "job_workflow_ref": "owner/repo/.github/workflows/review.yml@main",
            "job_workflow_sha": "c" * 40,
        }

        class Broker:
            def __init__(self):
                self.authorizations = []

            def authorize_session_mutation(self, token, **kwargs):
                self.authorizations.append((token, kwargs))
                return type(
                    "SessionGrant",
                    (),
                    {
                        "token": "capability-token",
                        "grant": "opaque-grant",
                        "attestation": {
                            "repository": "owner/repo",
                            "repository_id": 99,
                            "pull_request": 136,
                            "head_sha": "b" * 40,
                            "operation": "command",
                            "source_comment_id": 501,
                            "actor": "alice",
                            "actor_type": "User",
                            "association": "MEMBER",
                            "command_id": 501,
                            "command_digest": sha256(body.encode("utf-8")).hexdigest(),
                        },
                    },
                )()

        broker = Broker()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        with patch.object(
            application,
            "_session_ledger_for_token",
            return_value=InMemorySessionLedger(),
        ):
            result = application.apply_maintainer_command(
                options=GitHubWriteOptions(
                    github_writes=True, github_session_ledger=True
                ),
                oidc_token="caller-oidc",
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="b" * 40,
                body=body,
                actor_login="alice",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
                source_comment_id=501,
                session_attestation=request_attestation,
            )
        self.assertTrue(result.applied)
        self.assertEqual(
            broker.authorizations,
            [
                (
                    "caller-oidc",
                    {
                        "repository_id": 99,
                        "pull_request": 136,
                        "head_sha": "b" * 40,
                        "session_attestation": request_attestation,
                    },
                )
            ],
        )

    def test_hosted_mutation_rejects_a_non_atomic_grant_ledger(self):
        body = "@sensei review pause"
        request_attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 99,
            "pull_request": 136,
            "head_sha": "b" * 40,
            "operation": "command",
            "source_comment_id": 501,
            "run_id": "42",
            "issued_at": 1,
            "concurrency_group": "reviewsensei-session-99-136",
            "job_workflow_ref": "owner/repo/.github/workflows/review.yml@main",
            "job_workflow_sha": "c" * 40,
        }

        class Broker:
            def authorize_session_mutation(self, token, **kwargs):
                del token, kwargs
                return type(
                    "SessionGrant",
                    (),
                    {
                        "token": "capability-token",
                        "grant": "opaque-grant",
                        "attestation": {
                            "repository": "owner/repo",
                            "repository_id": 99,
                            "pull_request": 136,
                            "head_sha": "b" * 40,
                            "operation": "command",
                            "source_comment_id": 501,
                            "actor": "alice",
                            "actor_type": "User",
                            "association": "MEMBER",
                            "command_id": 501,
                            "command_digest": sha256(body.encode("utf-8")).hexdigest(),
                        },
                    },
                )()

        class NonAtomicGrantLedger(InMemorySessionLedger):
            _broker = object()

        application = GitHubApplication(
            broker=Broker(),
            http=None,
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        with patch.object(
            application,
            "_session_ledger_for_token",
            return_value=NonAtomicGrantLedger(),
        ):
            with self.assertRaisesRegex(GitHubPublicationError, "atomic session"):
                application.apply_maintainer_command(
                    options=GitHubWriteOptions(
                        github_writes=True, github_session_ledger=True
                    ),
                    oidc_token="caller-oidc",
                    repository="owner/repo",
                    repository_id=99,
                    pull_request=136,
                    head_sha="b" * 40,
                    body=body,
                    actor_login="mallory",
                    association="CONTRIBUTOR",
                    app_slug="reviewsensei[bot]",
                    source_comment_id=501,
                    session_attestation=request_attestation,
                )

    def test_hosted_mutation_rejects_a_broker_attestation_that_binds_another_command(
        self,
    ):
        body = "@sensei review pause"
        request_attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 99,
            "pull_request": 136,
            "head_sha": "b" * 40,
            "operation": "command",
            "source_comment_id": 501,
            "run_id": "42",
            "issued_at": 1,
            "concurrency_group": "reviewsensei-session-99-136",
            "job_workflow_ref": "owner/repo/.github/workflows/review.yml@main",
            "job_workflow_sha": "c" * 40,
        }
        attestation = {
            "repository": "owner/repo",
            "repository_id": 99,
            "pull_request": 136,
            "head_sha": "b" * 40,
            "operation": "command",
            "source_comment_id": 501,
            "actor": "alice",
            "actor_type": "User",
            "association": "MEMBER",
            "command_id": 501,
            "command_digest": sha256(body.encode("utf-8")).hexdigest(),
        }

        def broker_with(**overrides):
            def authorize_session_mutation(self, token, **kwargs):
                del self, token, kwargs
                return type(
                    "SessionGrant",
                    (),
                    {
                        "token": "capability-token",
                        "grant": "opaque-grant",
                        "attestation": {**attestation, **overrides},
                    },
                )()

            return type(
                "Broker", (), {"authorize_session_mutation": authorize_session_mutation}
            )()

        # The first override is an attestation for another head, the second the
        # digest a broker records when the comment no longer holds this body.
        for message, overrides in (
            ("scope was invalid", {"head_sha": "d" * 40}),
            (
                "command was stale",
                {"command_digest": sha256(b"@sensei review continue").hexdigest()},
            ),
        ):
            with self.subTest(message=message):
                application = GitHubApplication(
                    broker=broker_with(**overrides),
                    http=None,
                    reviewer=object(),
                    learner=object(),
                    replier=object(),
                )
                with patch.object(
                    application,
                    "_session_ledger_for_token",
                    return_value=InMemorySessionLedger(),
                ):
                    with self.assertRaisesRegex(GitHubPublicationError, message):
                        application.apply_maintainer_command(
                            options=GitHubWriteOptions(
                                github_writes=True, github_session_ledger=True
                            ),
                            oidc_token="caller-oidc",
                            repository="owner/repo",
                            repository_id=99,
                            pull_request=136,
                            head_sha="b" * 40,
                            body=body,
                            actor_login="alice",
                            association="MEMBER",
                            app_slug="reviewsensei[bot]",
                            source_comment_id=501,
                            session_attestation=request_attestation,
                        )


class SummaryTests(unittest.TestCase):
    def test_handoff_summary_asks_for_human_review(self):
        text = render_convergence_summary(
            mode="merge-focused",
            round_kind="verification",
            completed_rounds=20,
            verified_fixed=2,
            new_regressions=1,
            advisory=3,
            handoff=True,
            handoff_reason="failed-attempt-budget-exhausted",
        )
        self.assertIn("human review", text)
        self.assertIn("rounds=20", text)
        self.assertIn("failed-attempt-budget-exhausted", text)
        self.assertNotIn("remaining", text)

    def test_handoff_summary_rejects_unbounded_counts_and_reasons(self):
        with self.assertRaisesRegex(ReviewInputError, "completed_rounds"):
            render_convergence_summary(
                mode="merge-focused",
                round_kind="verification",
                completed_rounds=-1,
            )
        with self.assertRaisesRegex(ReviewInputError, "handoff_reason"):
            render_convergence_summary(
                mode="merge-focused",
                round_kind="verification",
                completed_rounds=0,
                handoff=True,
                handoff_reason="x" * 129,
            )


class CliCommandTests(unittest.TestCase):
    def test_github_command_requires_write_opt_in_for_local_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = main(
                [
                    "github",
                    "command",
                    "--comment-body",
                    "@sensei review pause",
                    "--actor",
                    "alice",
                    "--association",
                    "MEMBER",
                    "--repository",
                    "owner/repo",
                    "--pull-request",
                    "136",
                    "--allow-write",
                    "--session-ledger",
                    str(root),
                ]
            )
            self.assertEqual(status, 0)
            loaded = LocalSessionLedger(root).load(SessionIdentity("owner/repo", 136))
            self.assertEqual(loaded.status, "ok")
            self.assertTrue(loaded.record.operator_paused)

    def test_github_command_status_remains_read_only_without_write_opt_in(self):
        with tempfile.TemporaryDirectory() as raw:
            status = main(
                [
                    "github",
                    "command",
                    "--comment-body",
                    "@sensei review status",
                    "--actor",
                    "alice",
                    "--association",
                    "MEMBER",
                    "--repository",
                    "owner/repo",
                    "--pull-request",
                    "136",
                    "--session-ledger",
                    raw,
                ]
            )
            self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
