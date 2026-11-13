"""Ordinary opt-in CLI enrollment and cold command admission, no live service."""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github.run_authority import parse_authority
from review_sensei_github.session_ledger import parse_session_comment
from test_action_run import CommandEntryFixture
from test_api_work_persistence import IDENTITY


class ObservedEntryFixture(CommandEntryFixture):
    def __init__(self, directory):
        super().__init__(directory, limit=512)
        self.observed_installation = 99
        self.observed_requests = []

    def external(self, request, timeout):
        if urlsplit(request.full_url).hostname == "broker.github.test" and request.data:
            payload = json.loads(request.data)
            authority = payload.get("session_attestation", {})
            if authority.get("version") == 4:
                self._meter(request)
                verifying = urlsplit(request.full_url).path.endswith("/session-grant")
                authority = parse_authority(authority, grant=verifying)
                self.observed_requests.append(
                    ("verify" if verifying else payload["capability"], 4)
                )
                receipt = {
                    **payload["control_budget"],
                    "dispatched": min(
                        self.fanout, payload["control_budget"]["max_dispatches"]
                    ),
                }
                if verifying:
                    return json_response(
                        {"session_attestation": authority, "control_receipt": receipt}
                    )
                assert payload["capability"] == "review_session"
                actor = self._actor() if self.command_active else None
                observation = {
                    "schema_version": "current-observation-v1",
                    "repository": {
                        "id": 123,
                        "full_name": "owner/repo",
                        "owner_id": 42,
                        "owner_login": "owner",
                        "fork": False,
                    },
                    "installation": {
                        "id": self.observed_installation,
                        "repository_selection": "selected",
                        "repository_access": True,
                        "permissions": {"metadata": "read", "pull_requests": "write"},
                    },
                    "capability": {
                        "name": "review_session",
                        "permissions": {"metadata": "read", "pull_requests": "write"},
                    },
                    "actor": {
                        "id": 101,
                        "login": "alice",
                        "source_association": actor["association"] if actor else None,
                    },
                }
                return json_response(
                    {
                        "token": "synthetic-review_session",
                        "capability": "review_session",
                        "control_receipt": receipt,
                        "session_state": "known" if self.comment else "enrolled",
                        "app_id": 999,
                        "reservation_owner": {
                            "run_id": str(self.run_id),
                            "head_sha": self.requested_head,
                        },
                        "session_grant": "sg1.9999999999999." + "A" * 43,
                        "session_attestation": {
                            **authority,
                            "actor": actor,
                            "current_observation": observation,
                        },
                    }
                )
        return super().external(request, timeout)

    def opted(self, call):
        original = package_cli.main

        def invoke(argv):
            return original([argv[0], "--enable-current-trust-enrollment", *argv[1:]])

        with patch.object(package_cli, "main", side_effect=invoke):
            return call()


class TrustEnrollmentEntryTests(unittest.TestCase):
    def test_actual_opted_review_then_cold_command_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
            first = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            original = copy.deepcopy(first.trust_enrollment)
            self.assertIsNotNone(original)
            provider_calls = fixture.provider_calls
            fixture.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(fixture.opted(fixture.invoke_command), 0, fixture.error)
            current = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(current.trust_enrollment, original)
            self.assertEqual(fixture.provider_calls, provider_calls)
            self.assertEqual(current.api_run_accounting["configuration"]["limit"], 512)
            self.assertTrue(fixture.observed_requests)
            self.assertTrue(
                all(
                    capability in {"review_session", "verify"}
                    for capability, _ in fixture.observed_requests
                )
            )

    def test_enrollment_uses_existing_dispatch_and_original_allowance(self):
        traces = []
        for opted in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                fixture = ObservedEntryFixture(directory)
                code = fixture.opted(fixture.invoke) if opted else fixture.invoke()
                self.assertEqual(code, 0, fixture.error)
                traces.append([(method, path) for _, method, path in fixture.trace])
                current = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(
                    current.api_run_accounting["configuration"]["limit"], 512
                )
        self.assertEqual(traces[0], traces[1])

    def test_enrolled_toggle_off_and_changed_installation_refuse_before_business_writes(
        self,
    ):
        for change in ("toggle-off", "installation"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = ObservedEntryFixture(directory)
                self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
                before = copy.deepcopy(fixture.comment)
                fixture.select_command("@reviewsensei review status", new_run=True)
                if change == "installation":
                    fixture.observed_installation = 100
                start = len(fixture.trace)
                call = (
                    fixture.invoke_command
                    if change == "toggle-off"
                    else lambda: fixture.opted(fixture.invoke_command)
                )
                self.assertEqual(call(), 1, fixture.error)
                self.assertIn("trust", fixture.error)
                self.assertEqual(fixture.comment, before)
                self.assertFalse(
                    any(
                        method != "GET" and path.startswith("/repos/")
                        for _, method, path in fixture.trace[start:]
                    )
                )

    def test_legacy_existing_root_is_not_reenrolled_by_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            before = copy.deepcopy(fixture.comment)
            fixture.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(fixture.opted(fixture.invoke_command), 1, fixture.error)
            self.assertIn("legacy migration required", fixture.error)
            self.assertEqual(fixture.comment, before)
