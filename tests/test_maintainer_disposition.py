from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import GitHubApplication, GitHubWriteOptions
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.trigger import resolve_issue_comment

from review_sensei.cli import main
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import (
    MAINTAINER_ACTIONS,
    FindingDisposition,
    MaintainerCommandResult,
    apply_session_command,
    authorized_maintainer,
    authorized_override,
    command_exit_code,
    disposition_honors_coverage_uncertainty,
    disposition_honors_fingerprint,
    parse_maintainer_command,
    render_convergence_summary,
    resolve_finding_reference,
    session_dispositions,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
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

    def test_mention_boundary_rejects_surrounding_prose_quotes_and_indentation(self):
        for body in (
            "**operator note** @sensei\nreview pause",
            "note @sensei\treview pause",
            "> @reviewsensei override RS-ABCDEF acceptable risk",
            ">> @sensei review continue",
            "```\n@reviewsensei override RS-ABCDEF acceptable risk\n```",
            "    @reviewsensei override RS-ABCDEF acceptable risk",
            "  @reviewsensei review pause",
            "\t@sensei review continue",
            "\n@reviewsensei override RS-ABCDEF acceptable risk",
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
                expected = case.get("references")
                if expected and parsed is not None:
                    actual = parsed.finding_references or (
                        (parsed.finding_reference,)
                        if parsed.finding_reference is not None
                        else ()
                    )
                    self.assertEqual(list(actual), expected)

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

    def test_verify_names_a_stale_override_and_keeps_the_current_one_partial(self):
        ledger = InMemorySessionLedger()
        fingerprint = "abcd1234abcd1234"
        override = parse_maintainer_command(
            f"@sensei accept-risk {fingerprint} --reason launch exception",
            actor="alice",
            head_sha="a" * 40,
        )
        apply_session_command(ledger, IDENTITY, override, now=FIXED_NOW)
        current = parse_maintainer_command(
            "@sensei verify", actor="alice", head_sha="a" * 40
        )
        _record, applied = apply_session_command(
            ledger, IDENTITY, current, now=FIXED_NOW
        )
        self.assertTrue(applied.applied)
        self.assertIn("approval is re-checked without a new AI review", applied.summary)
        pushed = parse_maintainer_command(
            "@sensei verify", actor="alice", head_sha="b" * 40
        )
        _record, stale = apply_session_command(ledger, IDENTITY, pushed, now=FIXED_NOW)
        self.assertFalse(stale.applied)
        self.assertIn("stale override RS-ABCD12", stale.summary)
        self.assertIn("a new push needs a new override", stale.summary)

    def test_status_reports_the_authoritative_head_binding(self):
        ledger = InMemorySessionLedger()
        command = parse_maintainer_command(
            "@sensei review status", actor="alice", head_sha="a" * 40
        )
        _record, result = apply_session_command(
            ledger, IDENTITY, command, now=FIXED_NOW
        )
        self.assertIn(f"head={'a' * 40}", result.summary)

    def test_status_distinguishes_paused_from_blocked_by_run(self):
        # ADR 0079: the two blocked states must not read the same.
        ledger = InMemorySessionLedger()
        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        apply_session_command(ledger, IDENTITY, pause, now=FIXED_NOW)
        status = parse_maintainer_command("@sensei review status", actor="alice")
        _record, result = apply_session_command(ledger, IDENTITY, status, now=FIXED_NOW)
        self.assertIn(
            "paused by maintainer, comment @sensei review continue", result.summary
        )

        ledger = InMemorySessionLedger()
        ledger.initialize(IDENTITY, now=FIXED_NOW)
        ledger.replace(
            IDENTITY,
            lambda r: r.evolve(
                reservation_id="abcd1234",
                reserved_slot="initial",
                reservation_owner={"run_id": "424242", "head_sha": "a" * 40},
                now=FIXED_NOW,
            ),
            now=FIXED_NOW,
        )
        _record, result = apply_session_command(ledger, IDENTITY, status, now=FIXED_NOW)
        self.assertIn("blocked by held reservation, owner run 424242", result.summary)
        self.assertNotIn("paused by maintainer", result.summary)

    def test_status_without_pause_or_hold_emits_no_blocked_clause(self):
        ledger = InMemorySessionLedger()
        status = parse_maintainer_command("@sensei review status", actor="alice")
        _record, result = apply_session_command(ledger, IDENTITY, status, now=FIXED_NOW)
        self.assertIn("paused=False", result.summary)
        self.assertNotIn("not paused", result.summary)
        self.assertNotIn("blocked by", result.summary)
        self.assertTrue(result.summary.endswith("paused=False"))

        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        apply_session_command(ledger, IDENTITY, pause, now=FIXED_NOW)
        _record, result = apply_session_command(ledger, IDENTITY, status, now=FIXED_NOW)
        self.assertIn("paused by maintainer", result.summary)
        self.assertNotIn("blocked by", result.summary)

    def test_status_composes_pause_and_known_owner_exactly(self):
        ledger = InMemorySessionLedger()
        ledger.initialize(IDENTITY, now=FIXED_NOW)
        ledger.replace(
            IDENTITY,
            lambda r: r.evolve(
                reservation_id="abcd1234",
                reserved_slot="initial",
                reservation_owner={"run_id": "424242", "head_sha": "a" * 40},
                operator_paused=True,
                now=FIXED_NOW,
            ),
            now=FIXED_NOW,
        )
        status = parse_maintainer_command("@sensei review status", actor="alice")
        _record, result = apply_session_command(ledger, IDENTITY, status, now=FIXED_NOW)
        # Pause first, then the owner clause: the two states stay
        # distinguishable as one composed string (ADR 0079).
        self.assertIn(
            "paused by maintainer, comment @sensei review continue to resume; "
            "blocked by held reservation, owner run 424242",
            result.summary,
        )

    def test_status_composes_pause_and_hold_and_names_unknown_owner(self):
        ledger = InMemorySessionLedger()
        ledger.initialize(IDENTITY, now=FIXED_NOW)
        ledger.replace(
            IDENTITY,
            lambda r: r.evolve(
                reservation_id="abcd1234",
                reserved_slot="initial",
                operator_paused=True,
                now=FIXED_NOW,
            ),
            now=FIXED_NOW,
        )
        status = parse_maintainer_command("@sensei review status", actor="alice")
        _record, result = apply_session_command(ledger, IDENTITY, status, now=FIXED_NOW)
        self.assertIn("paused by maintainer", result.summary)
        self.assertIn("blocked by a held reservation, owner unknown", result.summary)

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

    def test_ten_headed_overrides_persist_and_round_trip(self):
        ledger = InMemorySessionLedger()
        head = "ab" * 20
        fingerprints = [f"{index:016x}" for index in range(1, 11)]
        command = parse_maintainer_command(
            "@reviewsensei override " + " ".join(fingerprints) + " acceptable risk",
            actor="alice",
            head_sha=head,
        )
        self.assertIsNotNone(command)
        assert command is not None
        record, result = apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)
        self.assertTrue(result.applied)
        self.assertEqual(
            [item["fingerprint"] for item in record.dispositions], fingerprints
        )
        self.assertEqual(len(result.receipts), 10)
        self.assertTrue(all(item.outcome == "recorded" for item in result.receipts))
        reloaded = SessionRecord.from_dict(record.to_dict())
        self.assertEqual(len(reloaded.dispositions), 10)
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(len(loaded.record.dispositions), 10)

    def test_another_short_override_after_ten_still_round_trips(self):
        ledger = InMemorySessionLedger()
        head = "ab" * 20
        first = [f"{index:016x}" for index in range(1, 11)]
        command = parse_maintainer_command(
            "@reviewsensei override " + " ".join(first) + " acceptable risk",
            actor="alice",
            head_sha=head,
        )
        assert command is not None
        apply_session_command(ledger, IDENTITY, command, now=FIXED_NOW)
        extra = parse_maintainer_command(
            f"@reviewsensei override {11:016x} acceptable risk",
            actor="alice",
            head_sha=head,
        )
        assert extra is not None
        record, result = apply_session_command(ledger, IDENTITY, extra, now=FIXED_NOW)
        self.assertTrue(result.applied)
        self.assertEqual(len(record.dispositions), 11)
        self.assertEqual(
            len(SessionRecord.from_dict(record.to_dict()).dispositions), 11
        )

    def test_gate_evaluation_failure_exits_nonzero_and_withheld_approval_does_not(
        self,
    ):
        failed = MaintainerCommandResult(
            action="verify",
            applied=True,
            operator_paused=False,
            summary="gate re-evaluation failed",
            evaluation_failed=True,
        )
        withheld = MaintainerCommandResult(
            action="verify",
            applied=True,
            operator_paused=False,
            summary="approval withheld (human_adjudication_open)",
        )
        self.assertEqual(command_exit_code(failed), 1)
        self.assertEqual(command_exit_code(withheld), 0)

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
        media = resolve_issue_comment(
            "@reviewsensei I reviewed the media files and I approve", _pull()
        )
        self.assertEqual(media.operation, "command")
        override = resolve_issue_comment(
            "@reviewsensei override RS-ABCDEF acceptable risk", _pull()
        )
        self.assertEqual(override.operation, "command")


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


class FindingOverrideTests(unittest.TestCase):
    def test_displayed_id_becomes_one_accept_risk_disposition(self):
        fingerprint = "abc123" + "0" * 58
        command = parse_maintainer_command(
            "@reviewsensei override RS-ABC123 acceptable risk",
            actor="alice",
            head_sha="a" * 40,
        )
        assert command is not None
        self.assertEqual(command.action, "accept-risk")
        self.assertEqual(command.finding_reference, "RS-ABC123")
        ledger = InMemorySessionLedger()
        record, result = apply_session_command(
            ledger,
            IDENTITY,
            command,
            now=FIXED_NOW,
            finding_fingerprints=(fingerprint,),
        )
        self.assertTrue(result.applied)
        self.assertEqual(record.dispositions[0]["fingerprint"], fingerprint)
        self.assertEqual(record.dispositions[0]["action"], "accept-risk")
        self.assertEqual(record.dispositions[0]["reason"], "acceptable risk")
        self.assertEqual(
            resolve_finding_reference("RS-ABC123", (fingerprint,)), fingerprint
        )

    def test_ambiguous_prefix_and_write_collaborator_do_not_override(self):
        left = "abc123" + "0" * 58
        right = "abc123" + "1" * 58
        with self.assertRaisesRegex(ReviewInputError, "one current finding"):
            resolve_finding_reference("RS-ABC123", (left, right))
        self.assertFalse(authorized_override(association="COLLABORATOR"))
        self.assertFalse(authorized_override(association="MEMBER", permission="write"))
        self.assertTrue(
            authorized_override(association="MEMBER", permission="maintain")
        )
        self.assertTrue(authorized_override(association="OWNER"))
        sentence = parse_maintainer_command(
            "@reviewsensei RS-ABCDEF is acceptable risk", actor="alice"
        )
        assert sentence is not None
        self.assertEqual(sentence.reason, "acceptable risk")
        bare = parse_maintainer_command(
            "@reviewsensei override RS-ABCDEF", actor="alice"
        )
        assert bare is not None
        self.assertEqual(bare.reason, "override")

    def test_collaborator_cannot_apply_the_easy_override(self):
        application = GitHubApplication(
            broker=object(),
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
            body="@reviewsensei override RS-ABC123 acceptable risk",
            actor_login="alice",
            association="COLLABORATOR",
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(result.summary, "unauthorized")
        self.assertFalse(result.applied)

    def test_quoted_indented_fenced_and_prose_commands_never_mutate_ledger(self):
        fingerprint = "abc123" + "0" * 58
        for body in (
            "> @reviewsensei override RS-ABC123 acceptable risk",
            "  > @sensei accept-risk " + fingerprint + " --reason accepted",
            "    @reviewsensei override RS-ABC123 acceptable risk",
            "\t@sensei review reenroll",
            "```\n@reviewsensei override RS-ABC123 acceptable risk\n```",
            "Example:\n@reviewsensei override RS-ABC123 acceptable risk",
            "Please run `@reviewsensei override RS-ABC123 acceptable risk`",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_maintainer_command(body, actor="alice"))
                ledger = InMemorySessionLedger()
                application = GitHubApplication(
                    broker=object(),
                    http=None,
                    reviewer=object(),
                    learner=object(),
                    replier=object(),
                    session_ledger=ledger,
                )
                result = application.apply_maintainer_command(
                    options=GitHubWriteOptions(github_writes=False),
                    oidc_token=None,
                    repository="owner/repo",
                    repository_id=99,
                    pull_request=136,
                    head_sha="b" * 40,
                    body=body,
                    actor_login="alice",
                    association="OWNER",
                    app_slug="reviewsensei[bot]",
                )
                self.assertFalse(result.applied)
                self.assertEqual(result.summary, "not-a-command")
                self.assertIsNone(ledger.load(IDENTITY).record)

    def test_unified_role_matrix_across_legacy_and_short_commands(self):
        fingerprint = "abc123456789def0" + "0" * 48
        elevated_commands = (
            f"@sensei accept-risk {fingerprint} --reason tracked",
            f"@reviewsensei accept-risk {fingerprint} --reason tracked",
            "@reviewsensei override RS-ABC123 acceptable risk",
            f"@sensei override {fingerprint} acceptable risk",
            f"@reviewsensei {fingerprint} is acceptable risk",
            "@reviewsensei accept-coverage-uncertainty --reason remaining slices bounded",
            "@sensei I reviewed the media files and I approve",
            "@reviewsensei review reenroll",
        )
        collaborator_commands = (
            f"@sensei dismiss {fingerprint} --reason false positive",
            f"@reviewsensei defer {fingerprint} --reason follow-up issue",
            "@sensei verify",
            "@reviewsensei review pause",
            "@sensei review continue",
        )
        for body in elevated_commands:
            for assoc, perm in (
                ("COLLABORATOR", None),
                ("MEMBER", "write"),
                ("CONTRIBUTOR", "admin"),
            ):
                with self.subTest(body=body, assoc=assoc, perm=perm):
                    ledger = InMemorySessionLedger()
                    app = GitHubApplication(
                        broker=object(),
                        http=None,
                        reviewer=object(),
                        learner=object(),
                        replier=object(),
                        session_ledger=ledger,
                    )
                    res = app.apply_maintainer_command(
                        options=GitHubWriteOptions(github_writes=False),
                        oidc_token=None,
                        repository="owner/repo",
                        repository_id=99,
                        pull_request=136,
                        head_sha="b" * 40,
                        body=body,
                        actor_login="alice",
                        association=assoc,
                        actor_permission=perm,
                        app_slug="reviewsensei[bot]",
                        finding_fingerprints=(fingerprint,),
                    )
                    self.assertFalse(res.applied)
                    self.assertEqual(res.summary, "unauthorized")

        for body in collaborator_commands:
            with self.subTest(body=body):
                ledger = InMemorySessionLedger()
                app = GitHubApplication(
                    broker=object(),
                    http=None,
                    reviewer=object(),
                    learner=object(),
                    replier=object(),
                    session_ledger=ledger,
                )
                res = app.apply_maintainer_command(
                    options=GitHubWriteOptions(github_writes=False),
                    oidc_token=None,
                    repository="owner/repo",
                    repository_id=99,
                    pull_request=136,
                    head_sha="b" * 40,
                    body=body,
                    actor_login="alice",
                    association="COLLABORATOR",
                    app_slug="reviewsensei[bot]",
                    finding_fingerprints=(fingerprint,),
                )
                self.assertNotEqual(res.summary, "unauthorized")

    def test_coverage_uncertainty_and_finding_risk_dispositions_are_distinct_and_audited(
        self,
    ):
        fingerprint = "abc123456789def0" + "0" * 48
        ledger = InMemorySessionLedger()
        cov_cmd = parse_maintainer_command(
            "@reviewsensei accept-coverage-uncertainty --reason large generated protobufs",
            actor="alice",
            head_sha="b" * 40,
        )
        assert cov_cmd is not None
        self.assertEqual(cov_cmd.action, "accept-coverage-uncertainty")
        record, result = apply_session_command(
            ledger,
            IDENTITY,
            cov_cmd,
            now=FIXED_NOW,
            actor_role="maintain",
            policy_digest="c" * 64,
            provenance="comment:901",
            incomplete_coverage=("unit-3", "unit-4"),
        )
        self.assertTrue(result.applied)
        dispositions = session_dispositions(record)
        self.assertEqual(len(dispositions), 1)
        cov_disp = dispositions[0]
        self.assertTrue(
            disposition_honors_coverage_uncertainty(
                dispositions,
                head_sha="b" * 40,
                incomplete_coverage=("unit-3", "unit-4"),
                policy_digest="c" * 64,
            )
        )
        self.assertFalse(
            disposition_honors_coverage_uncertainty(dispositions, head_sha="b" * 40)
        )
        self.assertFalse(
            disposition_honors_coverage_uncertainty(
                dispositions,
                head_sha="b" * 40,
                incomplete_coverage=("unit-3",),
                policy_digest="c" * 64,
            )
        )
        self.assertFalse(
            disposition_honors_coverage_uncertainty(
                dispositions,
                head_sha="b" * 40,
                incomplete_coverage=("unit-3", "unit-4"),
                policy_digest="d" * 64,
            )
        )
        self.assertFalse(
            disposition_honors_fingerprint(
                dispositions, fingerprint=fingerprint, head_sha="b" * 40
            )
        )
        self.assertFalse(
            disposition_honors_coverage_uncertainty(
                dispositions,
                head_sha="c" * 40,
                incomplete_coverage=("unit-3", "unit-4"),
                policy_digest="c" * 64,
            )
        )
        trail = cov_disp.audit_trail(identity=IDENTITY)
        self.assertEqual(trail["actor"], "alice")
        self.assertEqual(trail["actor_role"], "maintain")
        self.assertEqual(trail["repository"], "owner/repo")
        self.assertEqual(trail["pull_request"], 136)
        self.assertEqual(trail["target_kind"], "coverage_uncertainty")
        self.assertEqual(trail["incomplete_coverage"], ["unit-3", "unit-4"])
        self.assertEqual(trail["resolution_kind"], "coverage_uncertainty_accepted")
        self.assertEqual(trail["invalidation_behavior"], "invalidated_on_new_push")

    def test_post_command_receipt_requires_app_ownership_and_reconciles_stale_outcome(
        self,
    ):
        from review_sensei_github.command_bodies import _post_command_receipt

        from review_sensei.disposition import OverrideReceipt
        from review_sensei.presentation import render_command_receipt

        comment_body = "@reviewsensei override RS-ABCD1234 --reason accepted"
        digest = sha256(comment_body.encode("utf-8")).hexdigest()
        receipts = (OverrideReceipt("RS-ABCD1234", "a" * 64, "recorded"),)
        failed_text = render_command_receipt(
            actor="alice",
            source_comment_id=55,
            command_digest=digest,
            receipts=receipts,
            ledger_written=False,
        )
        succeeded_text = render_command_receipt(
            actor="alice",
            source_comment_id=55,
            command_digest=digest,
            receipts=receipts,
            ledger_written=True,
        )

        class FakeBroker:
            def request_oidc_token(self):
                return "oidc"

            def exchange(self, token, *, capability=None):
                return "ghs_reply_token"

        class FakeHttp:
            def __init__(self, comments):
                self.comments = list(comments)
                self.requests = []

            def repository_path(self, repo, suffix):
                return f"/repos/{repo}{suffix}"

            def paginate(self, *, path, token, max_requests, timeout_seconds):
                return list(self.comments)

            def request(self, method, path, *, token, body):
                self.requests.append((method, path, body["body"]))
                return 200, {
                    "id": 999,
                    "body": body["body"],
                    "user": {"login": "reviewsensei[bot]", "type": "Bot", "id": 777},
                }

        # 1. Spoofed human comment with matching marker is ignored -> POSTs fresh receipt
        http_spoof = FakeHttp(
            [
                {
                    "id": 101,
                    "body": succeeded_text,
                    "user": {"login": "attacker", "type": "User", "id": 666},
                }
            ]
        )
        _post_command_receipt(
            http_spoof,
            FakeBroker(),
            oidc_token="oidc",
            repository="owner/repo",
            pull_request=12,
            source_comment_id=55,
            comment_body=comment_body,
            actor="alice",
            receipts=receipts,
            ledger_written=True,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(len(http_spoof.requests), 1)
        self.assertEqual(http_spoof.requests[0][0], "POST")

        # 2. Prior failed App receipt (ledger_written=False) is reconciled via PATCH when ledger_written=True
        http_reconcile = FakeHttp(
            [
                {
                    "id": 202,
                    "body": failed_text,
                    "user": {"login": "reviewsensei[bot]", "type": "Bot", "id": 777},
                }
            ]
        )
        _post_command_receipt(
            http_reconcile,
            FakeBroker(),
            oidc_token="oidc",
            repository="owner/repo",
            pull_request=12,
            source_comment_id=55,
            comment_body=comment_body,
            actor="alice",
            receipts=receipts,
            ledger_written=True,
            app_slug="reviewsensei[bot]",
            expected_app_user_id=777,
        )
        self.assertEqual(len(http_reconcile.requests), 1)
        self.assertEqual(http_reconcile.requests[0][0], "PATCH")
        self.assertEqual(
            http_reconcile.requests[0][1], "/repos/owner/repo/issues/comments/202"
        )
        self.assertEqual(http_reconcile.requests[0][2], succeeded_text)

        # 3. Exact matching App receipt causes zero additional writes
        http_exact = FakeHttp(
            [
                {
                    "id": 202,
                    "body": succeeded_text,
                    "user": {"login": "reviewsensei[bot]", "type": "Bot", "id": 777},
                }
            ]
        )
        _post_command_receipt(
            http_exact,
            FakeBroker(),
            oidc_token="oidc",
            repository="owner/repo",
            pull_request=12,
            source_comment_id=55,
            comment_body=comment_body,
            actor="alice",
            receipts=receipts,
            ledger_written=True,
            app_slug="reviewsensei[bot]",
            expected_app_user_id=777,
        )
        self.assertEqual(http_exact.requests, [])

    def test_hosted_continue_reenroll_policy_binding_and_risk_acceptance_audit_context(
        self,
    ):
        from review_sensei_github.application import (
            GitHubApplication,
            GitHubWriteOptions,
        )

        from review_sensei.context import finding_lifecycle_for_comment
        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
        from review_sensei.models import ReviewComment, ReviewResult
        from review_sensei.session import (
            ReviewTransaction,
            prepare_session_round,
        )

        head = "b" * 40
        policy_v1 = ReviewConvergencePolicy(mode="merge-focused")
        policy_v2 = ReviewConvergencePolicy(mode="strict")

        for permission, should_authorize in (
            ("write", False),
            (None, False),
            ("maintain", True),
            ("admin", True),
        ):
            with self.subTest(permission=permission):
                ledger = InMemorySessionLedger()
                ledger.initialize(IDENTITY)
                ledger.replace(
                    IDENTITY,
                    lambda cur: cur.evolve(
                        generation=6,
                        operator_paused=False,
                        failed_attempts=6,
                        failed_attempts_head_sha=head,
                    ),
                )

                class PermHttp:
                    def repository_path(self, repo, suffix):
                        return f"/repos/{repo}{suffix}"

                    def request(self, method, path, *, token=None, body=None, **kw):
                        if "/collaborators/" in path and path.endswith("/permission"):
                            if permission is None:
                                return 404, {}
                            return 200, {"permission": permission}
                        return 200, {}

                class PermBroker:
                    def request_oidc_token(self):
                        return "oidc"

                    def exchange(self, token, *, capability=None):
                        return "ghs_tok"

                app = GitHubApplication(
                    broker=PermBroker(),
                    http=PermHttp(),
                    reviewer=object(),
                    learner=object(),
                    replier=object(),
                    session_ledger=ledger,
                )
                res = app.apply_maintainer_command(
                    options=GitHubWriteOptions(
                        github_writes=True, mention_replies=True
                    ),
                    oidc_token="oidc",
                    body="@reviewsensei review continue",
                    actor_login="collab-user",
                    actor_type="User",
                    association="COLLABORATOR",
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=head,
                    app_slug="reviewsensei[bot]",
                )
                if should_authorize:
                    self.assertTrue(res.applied)
                    self.assertIn("successor continuation granted", res.summary)
                    # Policy mismatch must neither admit work nor consume the grant
                    mismatch_prep = prepare_session_round(
                        ledger,
                        IDENTITY,
                        policy_v2,
                        reservation_id="c" * 64,
                        head_sha=head,
                    )
                    self.assertFalse(mismatch_prep.decision.admit)
                    # Matching policy admits and consumes the grant
                    ok_prep = prepare_session_round(
                        ledger,
                        IDENTITY,
                        policy_v1,
                        reservation_id="d" * 64,
                        head_sha=head,
                    )
                    self.assertTrue(ok_prep.decision.admit)
                else:
                    self.assertFalse(res.applied)
                    self.assertEqual(
                        len(ledger.load(IDENTITY).record.continuation_grants), 0
                    )

        # Reenroll retiring a pending transaction at counts 5, 6, and hard ceiling 32
        for prior_failed, expect_admit in ((5, True), (6, True), (32, False)):
            with self.subTest(reenroll_prior_failed=prior_failed):
                ledger = InMemorySessionLedger()
                ledger.initialize(IDENTITY)
                tx = (
                    ReviewTransaction.create(
                        repository=IDENTITY.repository,
                        pull_request=IDENTITY.pull_request,
                        base_sha="a" * 40,
                        head_sha=head,
                        policy_digest=policy_v1.digest(),
                        configuration_digest="1" * 64,
                        evidence_digest="2" * 64,
                        reservation_id="e" * 64,
                        generation=prior_failed + 1,
                    )
                    .with_result("3" * 64)
                    .with_phase("publication_failed")
                )
                ledger.replace(
                    IDENTITY,
                    lambda cur: cur.evolve(
                        generation=prior_failed + 1,
                        operator_paused=False,
                        failed_attempts=prior_failed,
                        failed_attempts_head_sha=head,
                        transaction=tx,
                        last_committed_reservation_id="e" * 64,
                    ),
                )
                cmd = parse_maintainer_command(
                    "@reviewsensei review reenroll",
                    actor="maint-alice",
                    head_sha=head,
                    command_id=f"reenroll-{prior_failed}",
                )
                assert cmd is not None
                _, reenroll_res = apply_session_command(
                    ledger,
                    IDENTITY,
                    cmd,
                    actor_role="maintain",
                    policy_digest=policy_v1.digest(),
                )
                self.assertTrue(reenroll_res.applied)
                next_prep = prepare_session_round(
                    ledger,
                    IDENTITY,
                    policy_v1,
                    reservation_id="f" * 64,
                    head_sha=head,
                )
                self.assertEqual(next_prep.decision.admit, expect_admit)
                if not expect_admit:
                    self.assertIn(
                        "hard failed-attempt ceiling is reached",
                        reenroll_res.summary,
                    )

        # Hosted accept-risk and accept-coverage-uncertainty record full audit context
        sample_comment = ReviewComment(
            path="src/a.py",
            line=10,
            severity="high",
            category="correctness",
            body="Bug",
        )
        fp = finding_lifecycle_for_comment(sample_comment).fingerprint
        partial_result = ReviewResult(
            summary="Partial coverage.",
            comments=(sample_comment,),
            provider="fixture",
            review_status="partial",
            coverage=CoverageManifest(
                files=(
                    FileCoverage("src/a.py", "reviewed"),
                    FileCoverage(
                        "src/b.py", "budget-exhausted", "provider-call-budget"
                    ),
                ),
                hunks=(
                    HunkCoverage(1, "src/a.py", "reviewed"),
                    HunkCoverage(
                        2, "src/b.py", "budget-exhausted", "provider-call-budget"
                    ),
                ),
                enumeration_complete=True,
                enumerated_paths=("src/a.py", "src/b.py"),
            ),
        )
        ledger = InMemorySessionLedger()
        ledger.initialize(IDENTITY)
        check_posts = []

        class AuditHttp:
            def repository_path(self, repo, suffix):
                return f"/repos/{repo}{suffix}"

            def request(self, method, path, *, token=None, body=None, **kw):
                if "/collaborators/" in path and path.endswith("/permission"):
                    return 200, {"permission": "maintain"}
                if path.endswith("/check-runs"):
                    check_posts.append(body)
                    return 201, {"id": 900}
                return 200, {"head": {"sha": head}}

        class AuditBroker:
            def request_oidc_token(self):
                return "oidc"

            def exchange(self, token, *, capability=None):
                return "ghs_tok"

        app = GitHubApplication(
            broker=AuditBroker(),
            http=AuditHttp(),
            reviewer=object(),
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        risk_res = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body=f"@reviewsensei accept-risk {fp[:16]} --reason mitigated upstream",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=701,
            finding_fingerprints=(fp,),
            review_result=partial_result,
        )
        self.assertTrue(risk_res.applied)
        cov_res = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body="@reviewsensei accept-coverage-uncertainty --reason generated files verified",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=702,
            finding_fingerprints=(fp,),
            review_result=partial_result,
        )
        self.assertTrue(cov_res.applied)
        disps = session_dispositions(ledger.load(IDENTITY).record)
        self.assertEqual(len(disps), 2)
        for d in disps:
            self.assertEqual(d.actor_role, "maintain")
            self.assertIsNotNone(d.recorded_at)
            self.assertEqual(d.policy_digest, policy_v1.digest())
            self.assertTrue(str(d.provenance).startswith("comment:70"))
        cov_disp = next(d for d in disps if d.action == "accept-coverage-uncertainty")
        self.assertIn(
            "file:src/b.py:budget-exhausted:provider-call-budget",
            cov_disp.incomplete_coverage,
        )
        from review_sensei_github.checks import review_check_outcome

        check_outcome = review_check_outcome(
            policy="auto-approve",
            review_status="partial",
            required_fixes=False,
            required_decisions=False,
            coverage_uncertainty_accepted=True,
        )
        self.assertEqual(
            check_outcome.title, "Coverage uncertainty accepted by maintainer"
        )

    def test_coverage_acceptance_validates_scope_policy_and_enables_maintainer_exception_approval(
        self,
    ):
        from review_sensei_github.application import (
            GitHubApplication,
            GitHubWriteOptions,
        )
        from review_sensei_github.approval import (
            ApprovalFacts,
            ReviewApprovalEligibility,
        )
        from review_sensei_github.publication import (
            IndexedFinding,
            ReviewPublisher,
            approval_eligibility_from_body,
            approval_eligibility_marker,
            finding_index_marker,
        )

        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
        from review_sensei.disposition import incomplete_coverage_from_result
        from review_sensei.human_assessment import finding_instance_fingerprint
        from review_sensei.models import ReviewComment, ReviewResult

        head = "b" * 40
        base = "c" * 40
        policy_v1 = ReviewConvergencePolicy(mode="merge-focused")
        policy_v2 = ReviewConvergencePolicy(mode="strict")
        sample_comment = ReviewComment(
            path="src/a.py",
            line=10,
            severity="high",
            category="correctness",
            body="Bug",
        )
        instance_fp = finding_instance_fingerprint(sample_comment)
        partial_v1 = ReviewResult(
            summary="Partial coverage v1.",
            comments=(sample_comment,),
            provider="fixture",
            review_status="partial",
            coverage=CoverageManifest(
                files=(
                    FileCoverage("src/a.py", "reviewed"),
                    FileCoverage(
                        "src/b.py", "budget-exhausted", "provider-call-budget"
                    ),
                ),
                hunks=(
                    HunkCoverage(1, "src/a.py", "reviewed"),
                    HunkCoverage(
                        2, "src/b.py", "budget-exhausted", "provider-call-budget"
                    ),
                ),
                enumeration_complete=True,
                enumerated_paths=("src/a.py", "src/b.py"),
            ),
        )
        partial_v2 = ReviewResult(
            summary="Partial coverage v2 with different missing slice.",
            comments=(sample_comment,),
            provider="fixture",
            review_status="partial",
            coverage=CoverageManifest(
                files=(
                    FileCoverage("src/a.py", "reviewed"),
                    FileCoverage(
                        "src/c.py", "budget-exhausted", "provider-call-budget"
                    ),
                ),
                hunks=(
                    HunkCoverage(1, "src/a.py", "reviewed"),
                    HunkCoverage(
                        2, "src/c.py", "budget-exhausted", "provider-call-budget"
                    ),
                ),
                enumeration_complete=True,
                enumerated_paths=("src/a.py", "src/c.py"),
            ),
        )
        cov_v1 = incomplete_coverage_from_result(partial_v1)
        cov_v2 = incomplete_coverage_from_result(partial_v2)
        self.assertIn(f"result:{partial_v1.content_digest()}", cov_v1)
        self.assertNotEqual(cov_v1, cov_v2)

        # 1. Withhold acceptance if current scope cannot be established
        empty_ledger = InMemorySessionLedger()
        empty_cmd = parse_maintainer_command(
            "@reviewsensei accept-coverage-uncertainty --reason no scope",
            actor="maint-alice",
            head_sha=head,
        )
        assert empty_cmd is not None
        _, withheld_res = apply_session_command(
            empty_ledger,
            IDENTITY,
            empty_cmd,
            actor_role="maintain",
            policy_digest=policy_v1.digest(),
            incomplete_coverage=(),
        )
        self.assertFalse(withheld_res.applied)
        self.assertIn("could not be established", withheld_res.summary)

        # 2. End-to-end refinalization and native approval via maintainer-coverage-exception
        def make_review_body(result_digest: str, *, review_status: str = "partial"):
            eligibility = ReviewApprovalEligibility(
                head,
                result_digest,
                ApprovalFacts(
                    enabled=True,
                    app_authored=False,
                    review_status=review_status,
                    evidence_policy="legacy",
                    has_blocking_findings=True,
                    has_human_adjudication_findings=False,
                    coverage_blocker="partial",
                    provider="fixture",
                ),
            )
            index = finding_index_marker(
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=head,
                base_sha=base,
                result_digest=result_digest,
                entries=(
                    IndexedFinding(
                        instance=instance_fp,
                        lifecycle=instance_fp,
                        blocking=True,
                        needs_human=False,
                        body_placed=True,
                    ),
                ),
            )
            return f"Review summary\n\n{index}\n\n{approval_eligibility_marker(eligibility)}"

        reviews = [
            {
                "id": 10,
                "commit_id": head,
                "state": "COMMENTED",
                "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                "body": make_review_body(partial_v1.content_digest()),
            }
        ]
        check_runs = []

        from review_sensei_github import GitHubHttp

        class FinalizeHttp(GitHubHttp):
            def request(self, method, path, *, token=None, body=None, **kw):
                if "/collaborators/" in path and path.endswith("/permission"):
                    return 200, {"permission": "maintain"}
                if method == "GET" and "/reviews" in path and "/comments" not in path:
                    return 200, list(reviews)
                if method == "GET" and path.endswith(f"/pulls/{IDENTITY.pull_request}"):
                    return 200, {
                        "state": "open",
                        "draft": False,
                        "user": {"login": "dev", "type": "User"},
                        "head": {
                            "sha": head,
                            "repo": {"full_name": IDENTITY.repository, "fork": False},
                        },
                        "base": {
                            "sha": base,
                            "repo": {
                                "id": IDENTITY.repository_id,
                                "full_name": IDENTITY.repository,
                                "fork": False,
                            },
                        },
                    }
                if method == "PUT" and "/reviews/" in path:
                    rev_id = int(path.rsplit("/", 1)[1])
                    for item in reviews:
                        if item["id"] == rev_id:
                            item["body"] = body["body"]
                            return 200, item
                if method == "POST" and path.endswith("/reviews"):
                    created = {
                        "id": 10 + len(reviews),
                        "commit_id": head,
                        "state": (
                            "APPROVED"
                            if body.get("event") == "APPROVE"
                            else body.get("event", "COMMENTED")
                        ),
                        "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                        "body": body.get("body", ""),
                    }
                    reviews.append(created)
                    return 200, created
                if path == "/graphql":
                    return 200, {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                if path.split("?", 1)[0].endswith("/check-runs"):
                    if method == "GET":
                        return 200, {"check_runs": []}
                    check_runs.append(body)
                    return 201, {"id": 500 + len(check_runs)}
                return 200, {}

        class FinalizeBroker:
            def request_oidc_token(self):
                return "oidc"

            def exchange(self, token, *, capability=None):
                return f"tok-{capability}"

        http = FinalizeHttp()
        ledger = InMemorySessionLedger()
        ledger.initialize(IDENTITY)
        app = GitHubApplication(
            broker=FinalizeBroker(),
            http=http,
            reviewer=ReviewPublisher(http=http),
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )

        # First accept coverage uncertainty while blocking finding is still open:
        # check stays action_required, native approval is withheld.
        cov_res = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body="@reviewsensei accept-coverage-uncertainty --reason generated protobuf slice verified",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=801,
            finding_fingerprints=(instance_fp,),
            review_result=partial_v1,
            convergence_policy=policy_v1,
        )
        self.assertTrue(cov_res.applied)
        self.assertIn("approval withheld (required_fixes_open)", cov_res.summary)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(check_runs[-1]["conclusion"], "failure")

        # Now accept risk on the blocking finding -> all non-coverage gates pass!
        # Check becomes success ("Coverage uncertainty accepted by maintainer") AND
        # native APPROVE review is submitted via maintainer-coverage-exception while
        # preserving truthful review_status="partial" and coverage_blocker="partial".
        risk_res = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body=f"@reviewsensei accept-risk {instance_fp} --reason mitigated",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=802,
            finding_fingerprints=(instance_fp,),
            review_result=partial_v1,
            convergence_policy=policy_v1,
        )
        self.assertTrue(risk_res.applied)
        self.assertIn(
            "approval submitted (maintainer-coverage-exception)", risk_res.summary
        )
        self.assertEqual(check_runs[-1]["conclusion"], "success")
        self.assertEqual(
            check_runs[-1]["output"]["title"],
            "Coverage uncertainty accepted by maintainer",
        )
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[-1]["state"], "APPROVED")
        self.assertIn(
            "Approved with maintainer coverage exception",
            reviews[-1]["body"],
        )
        self.assertIn(
            f"<!-- reviewsensei:approval-exception:v1 outcome=maintainer-coverage-exception result={partial_v1.content_digest()} gates=review-partial,coverage-partial -->",
            reviews[-1]["body"],
        )
        persisted_eligibility = approval_eligibility_from_body(reviews[0]["body"])
        assert persisted_eligibility is not None
        self.assertEqual(persisted_eligibility.facts.review_status, "partial")
        self.assertEqual(persisted_eligibility.facts.coverage_blocker, "partial")
        decision = persisted_eligibility.evaluate(
            app_authored=False,
            has_open_review_threads=False,
            coverage_uncertainty_accepted=True,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.outcome, "maintainer-coverage-exception")
        self.assertEqual(
            set(decision.satisfied_gates), {"review-partial", "coverage-partial"}
        )

        # 2b. Full file/hunk enumeration with overall partial status (e.g. skipped context/stages)
        # binds acceptance to the result digest and status/evidence_policy scope without a generic head fallback.
        from dataclasses import replace

        from review_sensei.coverage import HunkCoverage
        from review_sensei.disposition import incomplete_coverage_from_result

        full_enum_partial_v1 = ReviewResult(
            summary="Partial analysis with full file/hunk coverage.",
            comments=(),
            provider="fixture",
            model="fixture-model",
            review_status="partial",
            evidence_policy="legacy",
            coverage=CoverageManifest(
                enumerated_paths=("src/a.py",),
                files=(FileCoverage(path="src/a.py", outcome="reviewed"),),
                hunks=(
                    HunkCoverage(
                        index=1,
                        path="src/a.py",
                        outcome="reviewed",
                    ),
                ),
                enumeration_complete=True,
            ),
        )
        full_enum_partial_v2 = ReviewResult(
            summary="Different partial analysis on the same head.",
            comments=(),
            provider="fixture",
            model="fixture-model",
            review_status="partial",
            evidence_policy="legacy",
            coverage=full_enum_partial_v1.coverage,
        )
        self.assertEqual(
            incomplete_coverage_from_result(full_enum_partial_v1),
            (
                f"result:{full_enum_partial_v1.content_digest()}",
                "status:partial:evidence_policy:legacy",
            ),
        )
        # Capacity-exceeded persistence status is not a coverage uncertainty
        self.assertEqual(
            incomplete_coverage_from_result(
                replace(full_enum_partial_v1, persistence_status="capacity-exceeded")
            ),
            (),
        )
        reviews[:] = [
            {
                "id": 15,
                "commit_id": head,
                "state": "COMMENTED",
                "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                "body": (
                    "Review summary\n\n"
                    + finding_index_marker(
                        repository_id=IDENTITY.repository_id,
                        pull_request=IDENTITY.pull_request,
                        head_sha=head,
                        base_sha=base,
                        result_digest=full_enum_partial_v1.content_digest(),
                        entries=(),
                    )
                    + "\n\n"
                    + approval_eligibility_marker(
                        ReviewApprovalEligibility(
                            head_sha=head,
                            result_digest=full_enum_partial_v1.content_digest(),
                            facts=ApprovalFacts(
                                enabled=True,
                                app_authored=False,
                                check_published=True,
                                qualification="not-required",
                                review_status="partial",
                                evidence_policy="legacy",
                                has_open_review_threads=False,
                                has_blocking_findings=False,
                                has_human_adjudication_findings=False,
                                coverage_blocker=None,
                                provider="fixture",
                            ),
                        )
                    )
                ),
            }
        ]
        full_enum_accept = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body="@reviewsensei accept-coverage-uncertainty --reason supplemental stage skipped safely",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=810,
            finding_fingerprints=(),
            review_result=full_enum_partial_v1,
            convergence_policy=policy_v1,
        )
        self.assertTrue(full_enum_accept.applied)
        self.assertIn(
            "approval submitted (maintainer-coverage-exception)",
            full_enum_accept.summary,
        )
        self.assertEqual(reviews[-1]["state"], "APPROVED")
        self.assertIn(
            f"<!-- reviewsensei:approval-exception:v1 outcome=maintainer-coverage-exception result={full_enum_partial_v1.content_digest()} gates=review-partial -->",
            reviews[-1]["body"],
        )
        # A different partial result on the same head is not satisfied by the prior acceptance
        reviews[:] = [
            {
                "id": 18,
                "commit_id": head,
                "state": "COMMENTED",
                "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                "body": (
                    "Review summary\n\n"
                    + finding_index_marker(
                        repository_id=IDENTITY.repository_id,
                        pull_request=IDENTITY.pull_request,
                        head_sha=head,
                        base_sha=base,
                        result_digest=full_enum_partial_v2.content_digest(),
                        entries=(),
                    )
                    + "\n\n"
                    + approval_eligibility_marker(
                        ReviewApprovalEligibility(
                            head_sha=head,
                            result_digest=full_enum_partial_v2.content_digest(),
                            facts=ApprovalFacts(
                                enabled=True,
                                app_authored=False,
                                check_published=True,
                                qualification="not-required",
                                review_status="partial",
                                evidence_policy="legacy",
                                has_open_review_threads=False,
                                has_blocking_findings=False,
                                has_human_adjudication_findings=False,
                                coverage_blocker=None,
                                provider="fixture",
                            ),
                        )
                    )
                ),
            }
        ]
        full_enum_changed = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body="@reviewsensei verify",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=811,
            finding_fingerprints=(),
            review_result=full_enum_partial_v2,
            convergence_policy=policy_v1,
        )
        self.assertIn(
            "approval withheld (review_incomplete)", full_enum_changed.summary
        )
        self.assertEqual(len(reviews), 1)

        # 3. If coverage/result changes on the same head after acceptance, acceptance is withheld!
        reviews[:] = [
            {
                "id": 20,
                "commit_id": head,
                "state": "COMMENTED",
                "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                "body": make_review_body(partial_v2.content_digest()),
            }
        ]
        verify_changed_cov = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body="@reviewsensei verify",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=803,
            finding_fingerprints=(instance_fp,),
            review_result=partial_v2,
            convergence_policy=policy_v1,
        )
        self.assertIn(
            "approval withheld (review_incomplete)", verify_changed_cov.summary
        )
        self.assertEqual(check_runs[-1]["conclusion"], "action_required")
        self.assertEqual(check_runs[-1]["output"]["title"], "Review incomplete")
        self.assertEqual(len(reviews), 1)

        # 4. If policy changes on the same head after acceptance, acceptance is withheld!
        reviews[:] = [
            {
                "id": 30,
                "commit_id": head,
                "state": "COMMENTED",
                "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                "body": make_review_body(partial_v1.content_digest()),
            }
        ]
        verify_changed_policy = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="oidc",
            body="@reviewsensei verify",
            actor_login="maint-alice",
            actor_type="User",
            association="COLLABORATOR",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            source_comment_id=804,
            finding_fingerprints=(instance_fp,),
            review_result=partial_v1,
            convergence_policy=policy_v2,
        )
        self.assertIn(
            "approval withheld (review_incomplete)", verify_changed_policy.summary
        )
        self.assertEqual(check_runs[-1]["conclusion"], "action_required")
        self.assertEqual(len(reviews), 1)

    def test_command_cli_propagates_non_default_convergence_policy_for_continue_and_reenroll(
        self,
    ):
        from review_sensei_github.cli import main as github_main

        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.session import (
            LocalSessionLedger,
            SessionIdentity,
            prepare_session_round,
        )

        head = "b" * 40
        strict_policy = ReviewConvergencePolicy(mode="strict")
        advisory_policy = ReviewConvergencePolicy(mode="advisory")
        default_policy = ReviewConvergencePolicy(mode="merge-focused")
        local_id = SessionIdentity("owner/repo", 136)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = LocalSessionLedger(root)
            ledger.initialize(local_id)
            ledger.replace(
                local_id,
                lambda cur: cur.evolve(
                    generation=6,
                    operator_paused=False,
                    failed_attempts=6,
                    failed_attempts_head_sha=head,
                ),
            )
            status = github_main(
                [
                    "github",
                    "command",
                    "--comment-body",
                    "@reviewsensei review continue",
                    "--actor",
                    "alice",
                    "--association",
                    "OWNER",
                    "--repository",
                    "owner/repo",
                    "--pull-request",
                    "136",
                    "--head-sha",
                    head,
                    "--allow-write",
                    "--session-ledger",
                    str(root),
                    "--review-mode",
                    "strict",
                ]
            )
            self.assertEqual(status, 0)
            # Default policy does not match the strict grant
            mismatch = prepare_session_round(
                ledger,
                local_id,
                default_policy,
                reservation_id="1" * 64,
                head_sha=head,
            )
            self.assertFalse(mismatch.decision.admit)
            # Strict policy matches and admits the successor round
            admitted = prepare_session_round(
                ledger,
                local_id,
                strict_policy,
                reservation_id="2" * 64,
                head_sha=head,
            )
            self.assertTrue(admitted.decision.admit)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cfg_path = root / ".reviewsensei.yml"
            cfg_path.write_text("schema: 1\ngithub:\n  reviews: advisory\n")
            ledger = LocalSessionLedger(root / "ledger")
            ledger.initialize(local_id)
            ledger.replace(
                local_id,
                lambda cur: cur.evolve(
                    generation=6,
                    operator_paused=False,
                    failed_attempts=6,
                    failed_attempts_head_sha=head,
                ),
            )
            status = github_main(
                [
                    "github",
                    "command",
                    "--comment-body",
                    "@reviewsensei review reenroll",
                    "--actor",
                    "alice",
                    "--association",
                    "OWNER",
                    "--repository",
                    "owner/repo",
                    "--pull-request",
                    "136",
                    "--head-sha",
                    head,
                    "--allow-write",
                    "--session-ledger",
                    str(root / "ledger"),
                    "--config",
                    str(cfg_path),
                ]
            )
            self.assertEqual(status, 0)
            admitted = prepare_session_round(
                ledger,
                local_id,
                advisory_policy,
                reservation_id="3" * 64,
                head_sha=head,
            )
            self.assertTrue(admitted.decision.admit)

            # Hosted CLI entrypoint (--github-session-ledger) with non-default config policy
            hosted_ledger = InMemorySessionLedger()
            hosted_ledger.initialize(IDENTITY)
            hosted_ledger.replace(
                IDENTITY,
                lambda cur: cur.evolve(
                    generation=6,
                    operator_paused=False,
                    failed_attempts=6,
                    failed_attempts_head_sha=head,
                ),
            )
            comment_body = "@reviewsensei review continue"
            attestation = {
                "version": 1,
                "repository": IDENTITY.repository,
                "repository_id": IDENTITY.repository_id,
                "pull_request": IDENTITY.pull_request,
                "head_sha": head,
                "operation": "command",
                "source_comment_id": 501,
                "run_id": "42",
                "issued_at": 1,
                "concurrency_group": "reviewsensei-session-99-136",
                "job_workflow_ref": "owner/repo/.github/workflows/review.yml@main",
                "job_workflow_sha": "c" * 40,
            }
            attestation_file = root / "attestation.json"
            attestation_file.write_text(json.dumps(attestation))
            from types import SimpleNamespace
            from unittest.mock import Mock

            from review_sensei_github import GitHubApplication, GitHubHttp

            broker = Mock()
            broker.authorize_session_mutation.return_value = SimpleNamespace(
                token="synthetic-session-capability",
                grant="synthetic-one-use-grant",
                attestation={
                    **attestation,
                    "actor": "alice",
                    "actor_type": "User",
                    "association": "OWNER",
                    "command_id": 501,
                    "command_digest": sha256(comment_body.encode()).hexdigest(),
                },
            )
            http = GitHubHttp()
            with (
                patch.object(
                    http,
                    "request",
                    return_value=(200, {"permission": "admin"}),
                ),
                patch("review_sensei_github.GitHubHttp", return_value=http),
                patch("review_sensei_github.BrokerClient", return_value=broker),
                patch.object(
                    GitHubApplication,
                    "_session_ledger_for_token",
                    return_value=hosted_ledger,
                ),
            ):
                hosted_status = github_main(
                    [
                        "github",
                        "command",
                        "--comment-body",
                        comment_body,
                        "--actor",
                        "alice",
                        "--actor-type",
                        "User",
                        "--association",
                        "OWNER",
                        "--app-slug",
                        "reviewsensei[bot]",
                        "--repository",
                        IDENTITY.repository,
                        "--repository-id",
                        str(IDENTITY.repository_id),
                        "--pull-request",
                        str(IDENTITY.pull_request),
                        "--head-sha",
                        head,
                        "--source-comment-id",
                        "501",
                        "--session-attestation",
                        str(attestation_file),
                        "--github-session-ledger",
                        "--oidc-token",
                        "synthetic-oidc",
                        "--allow-write",
                        "--config",
                        str(cfg_path),
                    ]
                )
            self.assertEqual(hosted_status, 0)
            hosted_mismatch = prepare_session_round(
                hosted_ledger,
                IDENTITY,
                default_policy,
                reservation_id="4" * 64,
                head_sha=head,
            )
            self.assertFalse(hosted_mismatch.decision.admit)
            hosted_admitted = prepare_session_round(
                hosted_ledger,
                IDENTITY,
                advisory_policy,
                reservation_id="5" * 64,
                head_sha=head,
            )
            self.assertTrue(hosted_admitted.decision.admit)


if __name__ == "__main__":
    unittest.main()
