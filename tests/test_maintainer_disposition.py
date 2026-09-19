from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from review_sensei.cli import main
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import (
    apply_session_command,
    authorized_maintainer,
    parse_maintainer_command,
    render_convergence_summary,
)
from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions
from review_sensei.hosting.github.trigger import resolve_issue_comment
from review_sensei.session import InMemorySessionLedger, SessionIdentity

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
        cont = parse_maintainer_command(
            "@sensei review continue --rounds 1", actor="alice"
        )
        self.assertEqual(cont.action, "continue")
        self.assertEqual(cont.continuation_rounds, 1)
        dismissed = parse_maintainer_command(
            "@sensei dismiss abcd1234abcd1234 --reason accepted architecture",
            actor="alice",
        )
        self.assertEqual(dismissed.action, "dismiss")
        self.assertEqual(dismissed.reason, "accepted architecture")

    def test_unknown_or_bot_self_commands_are_ignored(self):
        self.assertIsNone(
            parse_maintainer_command("@sensei please re-scan", actor="alice")
        )
        self.assertFalse(
            authorized_maintainer(
                login="reviewsensei[bot]",
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

    def test_finding_disposition_requires_reason(self):
        self.assertIsNone(
            parse_maintainer_command("@sensei dismiss abcd1234abcd1234", actor="alice")
        )


class SessionCommandTests(unittest.TestCase):
    def test_pause_and_continue_mutate_operator_paused(self):
        ledger = InMemorySessionLedger()
        pause = parse_maintainer_command("@sensei review pause", actor="alice")
        record, result = apply_session_command(ledger, IDENTITY, pause, now=FIXED_NOW)
        self.assertTrue(result.applied)
        self.assertTrue(record.operator_paused)
        policy = ReviewConvergencePolicy(mode="merge-focused")
        from review_sensei.session import prepare_session_round

        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            policy,
            reservation_id="abcd1234",
            now=FIXED_NOW,
        )
        self.assertFalse(prepared.decision.admit)
        self.assertEqual(prepared.decision.handoff_reason, "paused")
        cont = parse_maintainer_command(
            "@sensei review continue --rounds 1", actor="alice"
        )
        record, result = apply_session_command(ledger, IDENTITY, cont, now=FIXED_NOW)
        self.assertFalse(record.operator_paused)
        self.assertEqual(result.continuation_rounds, 1)

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


class TriggerCommandTests(unittest.TestCase):
    def test_issue_comment_routes_maintainer_command(self):
        resolution = resolve_issue_comment("@sensei review pause", _pull())
        self.assertEqual(resolution.operation, "command")
        self.assertEqual(resolution.enable_review, "false")


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


class SummaryTests(unittest.TestCase):
    def test_handoff_summary_asks_for_human_review(self):
        text = render_convergence_summary(
            mode="merge-focused",
            round_kind="verification",
            remaining_verification=0,
            verified_fixed=2,
            new_regressions=1,
            advisory=3,
            handoff=True,
            handoff_reason="round-budget-exhausted",
        )
        self.assertIn("human review", text)
        self.assertIn("round-budget-exhausted", text)


class CliCommandTests(unittest.TestCase):
    def test_github_command_pauses_local_ledger_without_writes_flag(self):
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
                    "--session-ledger",
                    str(root),
                ]
            )
            self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
