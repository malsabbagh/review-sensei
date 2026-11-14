"""Negative hook compatibility; no projection, effect or budget grant."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from fake_github_http import FakeHTTPResponse
from review_sensei_github import api_run_requests, guarded_hot_namespace
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from test_api_work_persistence import PLAN, RUN

from review_sensei.errors import ReviewInputError


class GuardedHotNamespaceTests(unittest.TestCase):
    def caller(self, directory, sent):
        def physical(request, timeout):
            sent.append(request.full_url)
            return FakeHTTPResponse(b"{}")

        return ApiRunRequestTransport(
            directory=Path(directory),
            run=RUN,
            plan=PLAN,
            attempt=1,
            opener=physical,
            clock=lambda: 10.0,
        )

    def test_inactive_original_open_keeps_one_dispatch_and_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            sent = []
            caller = self.caller(directory, sent)
            with caller.open(Request("https://fixture.test"), timeout=1) as response:
                self.assertEqual(response.read(100), b"{}")
            self.assertEqual(sent, ["https://fixture.test"])
            self.assertEqual(caller._sequence, 1)
            self.assertEqual(caller.startup_dispatches, 1)
            self.assertEqual(caller.observations[0]["deadline_unix_ms"], 11000)

    def test_replaced_alias_or_export_refuses_before_callback_sequence_or_dispatch(
        self,
    ):
        for module, name in (
            (api_run_requests, "_hot_before_sequence"),
            (guarded_hot_namespace, "before_sequence"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                sent = []
                caller = self.caller(directory, sent)
                with patch.object(module, name, return_value=None) as fake:
                    with self.assertRaises(ReviewInputError):
                        caller.open(Request("https://fixture.test"), timeout=1)
                    fake.assert_not_called()
                self.assertEqual(sent, [])
                self.assertEqual(caller._sequence, 0)
                self.assertEqual(caller.startup_dispatches, 0)
                self.assertEqual(caller.observations, [])

    def test_external_installation_and_producer_calls_do_not_issue_custody(self):
        with self.assertRaises(ReviewInputError):
            guarded_hot_namespace.install_caller(ApiRunRequestTransport)
        from review_sensei_github.publication import ReviewPublisher

        with self.assertRaises(ReviewInputError):
            guarded_hot_namespace.begin(object.__new__(ReviewPublisher), None, None)
