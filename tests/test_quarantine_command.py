"""Exact individual historical decisions never share ordinary finding scope."""

import unittest

from review_sensei.errors import ReviewInputError


class QuarantineCommandTests(unittest.TestCase):
    def test_full_scope_full_instance_and_substantive_reason_are_required(self):
        from review_sensei_github.quarantine_action import parse_quarantine_command

        prefix = "@reviewsensei dismiss " + "A" * 64 + ' --reason "'
        command = parse_quarantine_command(
            prefix
            + "quarantine:"
            + "b" * 64
            + ' Original source and caller evidence disproves this concern."'
        )
        self.assertEqual(command.scope, "b" * 64)
        self.assertEqual(command.reference, "RS-" + "A" * 64)
        self.assertEqual(command.action, "dismiss")
        self.assertIsNone(
            parse_quarantine_command(
                "@reviewsensei dismiss "
                + "a" * 64
                + ' --reason "ordinary current finding decision"'
            )
        )
        for body in (
            prefix + "quarantine:" + "b" * 63 + ' A sufficiently long reason."',
            prefix + "quarantine:" + "b" * 64 + ' short"',
            '@reviewsensei dismiss AAAAAA --reason "quarantine:'
            + "b" * 64
            + ' A sufficiently long reason."',
            prefix + "QUARANTINE:" + "b" * 64 + ' A sufficiently long reason."',
        ):
            with self.subTest(body=body), self.assertRaises(ReviewInputError):
                parse_quarantine_command(body)
