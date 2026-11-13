"""Actual source exchange, no source/capability inference from supplied tokens."""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.trust_capabilities import require_source_capability

from review_sensei.errors import ReviewInputError
from tests.test_run_authority import NOW, Response
from tests.test_trust_effects import EffectSourceFixture


class CapabilityFixture(EffectSourceFixture):
    mismatch = False

    def __init__(self, directory):
        super().__init__(directory)
        from review_sensei_github.trust_effects import enable

        enable(
            self.caller, self.broker, self.session, api_url="https://api.github.test"
        )

    def open(self, request, timeout):
        body = json.loads(request.data)
        if body.get("session_attestation", {}).get("version") == 3:
            self.calls.append((request.full_url, body))
            return Response(
                {
                    "token": "synthetic-capability-" + body["capability"],
                    "capability": "other" if self.mismatch else body["capability"],
                }
            )
        return super().open(request, timeout)

    def capability_client(self):
        return BrokerClient(
            opener=self.caller.open, authority={**self.request, "version": 3}
        )


class TrustCapabilityTests(unittest.TestCase):
    def setUp(self):
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_source_capability_exact_scope_without_new_requests(self):
        f = CapabilityFixture(self.directory.name)
        broker = f.capability_client()
        token = broker.exchange("synthetic-oidc", capability="review_publish")
        before = f.caller.startup_dispatches
        for _ in range(2):
            require_source_capability(
                f.caller,
                token,
                capability="review_publish",
                authority=f.session.attestation,
            )
        self.assertEqual(f.caller.startup_dispatches, before)
        self.assertEqual(before, 2)
        for capability in ("check_publish", "thread_resolve", "review_session"):
            with self.assertRaises(ReviewInputError):
                require_source_capability(
                    f.caller,
                    token,
                    capability=capability,
                    authority=f.session.attestation,
                )
        other = copy.deepcopy(f.session.attestation)
        other["head_sha"] = "c" * 40
        with self.assertRaises(ReviewInputError):
            require_source_capability(
                f.caller, token, capability="review_publish", authority=other
            )

    def test_issued_token_cannot_exceed_source_observed_permissions(self):
        f = CapabilityFixture(self.directory.name)
        broker = f.capability_client()
        token = broker.exchange("synthetic-oidc", capability="check_publish")
        with self.assertRaisesRegex(
            ReviewInputError, "observed installation permissions"
        ):
            require_source_capability(
                f.caller,
                token,
                capability="check_publish",
                authority=f.session.attestation,
            )
        supplied = copy.deepcopy(f.session.attestation)
        supplied["current_observation"]["installation"]["permissions"]["checks"] = (
            "write"
        )
        with self.assertRaisesRegex(ReviewInputError, "original source profile"):
            require_source_capability(
                f.caller, token, capability="check_publish", authority=supplied
            )

    def test_response_capability_mismatch_preserves_legacy_return_but_issues_nothing(
        self,
    ):
        f = CapabilityFixture(self.directory.name)
        f.mismatch = True
        broker = f.capability_client()
        token = broker.exchange("synthetic-oidc", capability="review_publish")
        self.assertTrue(token)
        with self.assertRaises(ReviewInputError):
            require_source_capability(
                f.caller,
                token,
                capability="review_publish",
                authority=f.session.attestation,
            )

    def test_direct_helper_decorated_source_and_scope_substitution_cannot_issue(self):
        from review_sensei_github.trust_capabilities import _install_capability_source

        f = CapabilityFixture(self.directory.name)
        broker = f.capability_client()
        with self.assertRaises(ReviewInputError):
            _install_capability_source(BrokerClient)
        with patch.object(
            broker,
            "_post",
            return_value={"token": "synthetic-forged", "capability": "review_publish"},
        ):
            token = broker.exchange("synthetic-oidc", capability="review_publish")
        with self.assertRaises(ReviewInputError):
            require_source_capability(
                f.caller,
                token,
                capability="review_publish",
                authority=f.session.attestation,
            )
        self.assertEqual(len(f.calls), 1)
        token = broker.exchange("synthetic-oidc", capability="review_publish")
        for field, value in (
            ("opener", lambda *a, **k: None),
            ("broker_url", "https://other.invalid/token"),
        ):
            with (
                patch.object(broker, field, value),
                self.assertRaises(ReviewInputError),
            ):
                require_source_capability(
                    f.caller,
                    token,
                    capability="review_publish",
                    authority=f.session.attestation,
                )
        with (
            patch.object(BrokerClient, "_post", lambda *a, **k: {}),
            self.assertRaises(ReviewInputError),
        ):
            require_source_capability(
                f.caller,
                token,
                capability="review_publish",
                authority=f.session.attestation,
            )
