"""Actual opted-in empty plain operator-state command route."""

import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.action_run import execute_command
from review_sensei_github.session_ledger import parse_session_comment

from tests import test_trust_status_route as status_fixture
from tests.test_run_authority import NOW
from tests.test_trust_effect_initialization import IDENTITY


class PauseFixture(status_fixture.StatusFixture):
    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        if request.get_method() == "GET" and path.endswith(
            "/collaborators/alice/permission"
        ):
            self.native.append(("GET", path, None))
            return json_response({"permission": "admin"})
        return super().open(request, timeout)


class TrustPauseRouteTests(unittest.TestCase):
    def test_actual_pause_and_unpaused_continue(self):
        for action, expected_roots in (("pause", 4), ("continue", 3)):
            with (
                self.subTest(action=action),
                tempfile.TemporaryDirectory() as directory,
                patch("time.time", return_value=NOW),
                patch.object(status_fixture, "BODY", "@reviewsensei review " + action),
            ):
                f = PauseFixture(directory)
                self.assertEqual(execute_command(f.run, f.args), 0)
                record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(record.operator_paused, action == "pause")
                self.assertEqual(
                    sum(m == "PATCH" for m, _, _ in f.native), expected_roots
                )
                self.assertEqual(f.caller.accounting.action["phase"], "terminal")
                self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_actual_quote_and_one_short_hold_before_first_root(self):
        from urllib.request import Request

        from review_sensei_github.api_run_plan import NATIVE_OPERATION_REQUESTS
        from review_sensei_github.trust_status import STATUS_WORK_CALLS

        from review_sensei.errors import EvidenceBudgetExhausted

        for action in ("pause", "continue"):
            quote = (
                STATUS_WORK_CALLS
                + NATIVE_OPERATION_REQUESTS
                + 1
                + int(action == "continue")
            )
            for remaining in (quote, quote - 1):
                with (
                    self.subTest(action=action, remaining=remaining),
                    tempfile.TemporaryDirectory() as directory,
                    patch("time.time", return_value=NOW),
                    patch.object(
                        status_fixture, "BODY", "@reviewsensei review " + action
                    ),
                ):
                    f = PauseFixture(directory)
                    before = f.comment["body"]
                    while f.caller.usable > remaining:
                        with f.caller.open(
                            Request("https://api.github.test/repos/owner/repo/pulls/7"),
                            timeout=30,
                        ) as response:
                            response.read(65536)
                    if remaining == quote:
                        self.assertEqual(execute_command(f.run, f.args), 0)
                    else:
                        with self.assertRaises(EvidenceBudgetExhausted):
                            execute_command(f.run, f.args)
                        self.assertEqual(f.comment["body"], before)
                        self.assertFalse(any(m == "PATCH" for m, _, _ in f.native))
                    self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_pause_stored_loss_at_each_root_never_replays(self):
        for stage in range(1, 5):
            with (
                self.subTest(stage=stage),
                tempfile.TemporaryDirectory() as directory,
                patch("time.time", return_value=NOW),
                patch.object(status_fixture, "BODY", "@reviewsensei review pause"),
            ):
                f = PauseFixture(directory)
                f.loss_stage = stage
                self.assertEqual(execute_command(f.run, f.args), 0)
                self.assertEqual(sum(m == "PATCH" for m, _, _ in f.native), 4)
                record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertTrue(record.operator_paused)
                self.assertEqual(f.caller.accounting.action["phase"], "terminal")
                self.assertTrue(
                    any(o.get("outcome") == "unknown" for o in f.caller.observations)
                )

    def test_unstored_business_write_is_not_replayed_or_terminalized(self):
        from review_sensei.errors import ReviewInputError

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("time.time", return_value=NOW),
            patch.object(status_fixture, "BODY", "@reviewsensei review pause"),
        ):
            f = PauseFixture(directory)
            f.loss_stage, f.absent_loss = 3, True
            with self.assertRaises((ReviewInputError, OSError)):
                execute_command(f.run, f.args)
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertFalse(record.operator_paused)
            self.assertEqual(sum(m == "PATCH" for m, _, _ in f.native), 3)
            self.assertNotEqual(f.caller.accounting.action["phase"], "terminal")
            self.assertTrue(
                any(o.get("outcome") == "unknown" for o in f.caller.observations)
            )

    def test_current_role_downgrade_stops_before_any_root(self):
        from review_sensei_github.errors import GitHubBrokerClientError

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("time.time", return_value=NOW),
            patch.object(status_fixture, "BODY", "@reviewsensei review pause"),
        ):
            f = PauseFixture(directory)
            original = f.comment["body"]
            f.change = "role"
            with self.assertRaises(GitHubBrokerClientError):
                execute_command(f.run, f.args)
            self.assertEqual(f.comment["body"], original)
            self.assertFalse(any(m == "PATCH" for m, _, _ in f.native))
