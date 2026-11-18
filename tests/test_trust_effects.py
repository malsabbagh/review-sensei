"""Real client/caller source fixture for closed effect pricing; no live service."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from review_sensei_github.api_run_host import HOSTED_API_STARTUP_PLAN
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.trust_effects import (
    enable,
    verification_calls,
    verification_profile,
)
from review_sensei_github.trust_enrollment import refresh_current_observation

from review_sensei.api_work import ApiRunIdentity
from review_sensei.errors import ReviewInputError
from tests.test_current_trust_observation import observed
from tests.test_run_authority import HEAD, NOW, OPAQUE_GRANT, Response, granted


class EffectSourceFixture:
    """Only synthetic HTTP is replaced; real source issuance and caller charge."""

    def __init__(self, directory):
        self.calls = []
        self.change = None
        self.request, self.grant = observed("review")
        observation = self.grant["current_observation"]
        observation["schema_version"] = "current-observation-v2"
        observation["actor"].update(
            repository_permission="admin", repository_role="admin"
        )
        self.caller = ApiRunRequestTransport(
            directory=Path(directory),
            run=ApiRunIdentity(123, 456),
            plan=HOSTED_API_STARTUP_PLAN,
            attempt=1,
            opener=self.open,
        )
        self.broker = BrokerClient(opener=self.caller.open, authority=self.request)
        self.session = self.broker.open_session(
            "synthetic-oidc",
            repository_id=123,
            pull_request=7,
            head_sha=HEAD,
            observe_actor_role=True,
        )

    def open(self, request, timeout):
        body = json.loads(request.data)
        self.calls.append((request.full_url, body))
        if request.full_url.endswith("/session-grant"):
            original = copy.deepcopy(body["session_attestation"])
            observation = copy.deepcopy(original["current_observation"])
            if self.change == "role":
                observation["actor"]["repository_permission"] = "read"
            if self.change == "lost":
                raise OSError("synthetic response lost")
            bound = {
                "schema_version": "effect-observation-v1",
                "effect_sha256": body["refresh_effect_sha256"],
                "original_attestation_sha256": hashlib.sha256(
                    json.dumps(
                        original, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            }
            if self.change == "binding":
                bound["effect_sha256"] = "f" * 64
            return Response(
                {
                    "session_attestation": original,
                    "current_observation": observation,
                    "observation_binding": bound,
                }
            )
        authority = granted(body["session_attestation"])
        authority["current_observation"] = copy.deepcopy(
            self.grant["current_observation"]
        )
        return Response(
            {
                "token": "synthetic-token",
                "capability": "review_session",
                "session_state": "known",
                "app_id": 999,
                "reservation_owner": None,
                "session_grant": OPAQUE_GRANT,
                "session_attestation": authority,
            }
        )


class TrustEffectSourceTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_real_source_profile_is_immutable_and_quotes_no_default_cost(self):
        fixture = EffectSourceFixture(self.directory.name)
        self.assertEqual(verification_calls(fixture.caller), 0)
        profile = enable(
            fixture.caller,
            fixture.broker,
            fixture.session,
            api_url="https://api.github.test",
        )
        self.assertIs(verification_profile(fixture.caller), profile)
        self.assertEqual(verification_calls(fixture.caller), 1)
        with self.assertRaises((AttributeError, TypeError)):
            profile.original = b"forged"
        with self.assertRaises(ReviewInputError):
            enable(
                fixture.caller,
                fixture.broker,
                fixture.session,
                api_url="https://api.github.test",
            )
        self.assertEqual(fixture.calls[0][1]["observe_actor_role"], True)
        self.assertEqual(fixture.caller.startup_dispatches, 1)

    def test_profile_rechecks_source_scope_and_dispatch_after_enable(self):
        fixture = EffectSourceFixture(self.directory.name)
        enable(
            fixture.caller,
            fixture.broker,
            fixture.session,
            api_url="https://api.github.test",
        )
        for owner, field, replacement in (
            (fixture.caller, "run", ApiRunIdentity(123, 999)),
            (fixture.caller, "run", ApiRunIdentity(999, 456)),
            (fixture.caller, "attempt", 2),
            (fixture.caller, "job", "command"),
            (fixture.broker, "opener", lambda *a, **kw: None),
            (fixture.broker, "broker_url", "https://other.invalid/token"),
            (fixture.caller, "_opener", lambda *a, **kw: None),
        ):
            with self.subTest(field=field), patch.object(owner, field, replacement):
                with self.assertRaises(ReviewInputError):
                    verification_profile(fixture.caller)
        with patch.object(ApiRunRequestTransport, "open", lambda *a, **kw: None):
            with self.assertRaises(ReviewInputError):
                verification_profile(fixture.caller)
        self.assertEqual(len(fixture.calls), 1)

    def test_no_model_or_direct_installer_can_issue_profile(self):
        from review_sensei_github.trust_effects import _install_transport_source

        fixture = EffectSourceFixture(self.directory.name)
        with self.assertRaises(ReviewInputError):
            _install_transport_source(ApiRunRequestTransport)
        with self.assertRaises(ReviewInputError):
            enable(
                fixture.caller,
                fixture.broker,
                copy.copy(fixture.session),
                api_url="https://api.github.test",
            )
        self.assertEqual(verification_calls(fixture.caller), 0)
        self.assertEqual(len(fixture.calls), 1)

    def test_fresh_source_exchange_preserves_original_and_charges_each_call(self):
        fixture = EffectSourceFixture(self.directory.name)
        original = copy.deepcopy(fixture.session.attestation)
        for digest in ("a" * 64, "b" * 64):
            result = refresh_current_observation(
                fixture.broker, fixture.session, digest
            )
            self.assertEqual(result, original["current_observation"])
        self.assertEqual(fixture.caller.startup_dispatches, 3)
        self.assertEqual(len(fixture.calls), 3)
        self.assertEqual(fixture.session.attestation, original)
        self.assertEqual(fixture.calls[1][1]["refresh_effect_sha256"], "a" * 64)
        self.assertEqual(fixture.calls[2][1]["refresh_effect_sha256"], "b" * 64)

    def test_changed_role_binding_lost_response_never_becomes_proof(self):
        fixture = EffectSourceFixture(self.directory.name)
        for change in ("role", "binding", "lost"):
            fixture.change = change
            with self.subTest(change=change), self.assertRaises(Exception):
                refresh_current_observation(fixture.broker, fixture.session, "a" * 64)
        self.assertEqual(fixture.caller.startup_dispatches, 4)
        self.assertEqual(fixture.caller.observations[-1]["outcome"], "unknown")

    def test_unpriced_mode_holds_native_before_verification_or_business_write(self):
        fixture = EffectSourceFixture(self.directory.name)
        enable(
            fixture.caller,
            fixture.broker,
            fixture.session,
            api_url="https://api.github.test",
        )
        request = Request(
            "https://api.github.test/repos/owner/repo/pulls/7/reviews",
            data=b"{}",
            method="POST",
        )
        with self.assertRaisesRegex(
            ReviewInputError, "complete route admission unavailable"
        ):
            fixture.caller.open(request, timeout=30)
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(fixture.caller.startup_dispatches, 1)

    def test_substituted_refresh_and_mutated_original_cannot_issue(self):
        fixture = EffectSourceFixture(self.directory.name)
        with patch.object(
            fixture.broker, "refresh_session_observation", return_value={}
        ):
            with self.assertRaisesRegex(ReviewInputError, "dispatch changed"):
                refresh_current_observation(fixture.broker, fixture.session, "a" * 64)
        fixture.session.attestation["current_observation"]["actor"][
            "repository_permission"
        ] = "read"
        with self.assertRaisesRegex(ReviewInputError, "observation changed"):
            refresh_current_observation(fixture.broker, fixture.session, "a" * 64)
        self.assertEqual(len(fixture.calls), 1)
