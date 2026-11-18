"""Cold original-enrollment pause/continue through real bootstrap accounting."""

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from fake_github_http import json_response
from review_sensei_github import action_run
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.session_ledger import parse_session_comment
from review_sensei_github.trust_effects import enable

from tests.test_trust_enrollment_entry import IDENTITY, ObservedEntryFixture


class CurrentRoleEntryFixture(ObservedEntryFixture):
    def external(self, request, timeout):
        payload = json.loads(request.data) if request.data else {}
        if payload.get("refresh_effect_sha256"):
            self.effect_refreshes = getattr(self, "effect_refreshes", 0) + 1
            self._meter(request)
            authority = payload["session_attestation"]
            return json_response(
                {
                    "session_attestation": authority,
                    "control_receipt": {**payload["control_budget"], "dispatched": 9},
                    "current_observation": authority["current_observation"],
                    "observation_binding": {
                        "schema_version": "effect-observation-v1",
                        "effect_sha256": payload["refresh_effect_sha256"],
                        "original_attestation_sha256": hashlib.sha256(
                            json.dumps(
                                authority, ensure_ascii=False, separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                    },
                }
            )
        response = super().external(request, timeout)
        if payload.get("observe_actor_role"):
            data = json.loads(response.read(512 * 1024))
            observation = data["session_attestation"]["current_observation"]
            observation["schema_version"] = "current-observation-v2"
            observation["actor"].update(
                repository_permission="admin", repository_role="admin"
            )
            return json_response(data)
        return response


class ColdPauseTests(unittest.TestCase):
    def test_real_pause_then_cold_activation_and_fresh_unpause(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentRoleEntryFixture(directory)
            self.assertEqual(
                f.opted(lambda: f.invoke_command("@reviewsensei review pause")),
                0,
                f.error,
            )
            original = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertTrue(original.operator_paused)
            enrollment = copy.deepcopy(original.trust_enrollment)
            f.select_command("@reviewsensei review continue", new_run=True)
            captured = []

            def capture(run, args):
                captured.append((run, args))
                return 0

            # Only capture the real bootstrap result. Restore all module/source
            # definitions before installing or using the effect mode.
            with patch.object(action_run, "execute_command", capture):
                self.assertEqual(f.opted(f.invoke_command), 0, f.error)
            run, args = captured[0]
            broker = BrokerClient(
                broker_url=run.broker.broker_url,
                control=run.broker.control,
                opener=run.caller.open,
                authority={**run.broker.authority, "version": 4},
            )
            session = broker.open_session(
                "synthetic-oidc",
                repository_id=123,
                pull_request=7,
                head_sha=run.head_sha,
                observe_actor_role=True,
            )
            run = replace(
                run, session_authority=session.attestation, session_grant=session.grant
            )
            enable(run.caller, broker, session, api_url=run.http.api_url)
            start = len(f.trace)
            provider_calls = f.provider_calls
            self.assertEqual(action_run.execute_command(run, args), 0)
            self.assertEqual(f.effect_refreshes, 4)
            self.assertEqual(
                sum(method == "PATCH" for _, method, _ in f.trace[start:]), 4
            )
            self.assertEqual(f.provider_calls, provider_calls)
            current = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertFalse(current.operator_paused)
            self.assertEqual(current.trust_enrollment, enrollment)
            self.assertIsNotNone(current.api_run_accounting["previous_record"])
            self.assertEqual(current.api_run_accounting["configuration"]["limit"], 512)
            self.assertEqual(run.caller.accounting.action["phase"], "terminal")
