"""Copyable recovery commands stay aligned with their public parsers/help."""

import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from review_sensei_github.cli import main
from review_sensei_github.rescan import event_requests_remaining, event_requests_rescan

from review_sensei.disposition import parse_maintainer_command


class ApprovalRecoveryInstructionTests(unittest.TestCase):
    def test_every_copyable_mention_uses_the_shipped_parser(self):
        document = (
            Path(__file__).resolve().parents[1] / "docs/approval-recovery.md"
        ).read_text(encoding="utf-8")
        examples = re.findall(r"^@reviewsensei .+$", document, re.MULTILINE)
        self.assertGreaterEqual(len(examples), 7)
        for body in examples:
            with self.subTest(body=body):
                if body.startswith("@reviewsensei review remaining"):
                    self.assertTrue(
                        event_requests_remaining(
                            {"comment": {"body": body}}, "issue_comment"
                        )
                    )
                elif body == "@reviewsensei rescan":
                    self.assertTrue(
                        event_requests_rescan(
                            {"comment": {"body": body}}, "issue_comment"
                        )
                    )
                else:
                    self.assertIsNotNone(
                        parse_maintainer_command(body, actor="alice", head_sha="a" * 40)
                    )
        self.assertIn("**unreleased**", document)
        self.assertIn("APPROVED", document)

    def test_documented_ten_id_boundary_and_help_do_not_claim_approval(self):
        ids = [f"RS-{i:06X}" for i in range(11)]
        body = "@reviewsensei override "
        command = parse_maintainer_command(
            body + " ".join(ids[:10]), actor="alice", head_sha="a" * 40
        )
        self.assertEqual(len(command.finding_references), 10)
        self.assertIsNone(
            parse_maintainer_command(
                body + " ".join(ids), actor="alice", head_sha="a" * 40
            )
        )
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["command", "--help"])
        self.assertEqual(exit.exception.code, 0)
        self.assertIn("at most 10", output.getvalue())
        self.assertIn("native APPROVED", output.getvalue())
