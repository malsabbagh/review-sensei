"""Expiry hints schedule reads only; all external HTTP here is synthetic.

Unsigned fixture JWTs prove no authentication property. Current Worker grant
qualification owns that proof; these tests exercise caller transport/accounting.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import (
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)
from review_sensei_github.oidc_refresh import expiry_hint

from review_sensei.errors import ReviewInputError
from tests.test_api_work_lifecycle import LifecycleFixture
from tests.test_api_work_persistence import HEAD, IDENTITY
from tests.test_run_authority import OPAQUE_GRANT, Response, granted

NOW = 1_800_000_000


def unsigned_hint(document):
    """Bounded unverified scheduling input, deliberately not a signed JWT."""
    raw = json.dumps(document, separators=(",", ":")).encode()
    return "unsigned." + base64.urlsafe_b64encode(raw).decode().rstrip("=") + ".invalid"


class RefreshFixture:
    def __init__(self, directory):
        self.lifecycle = LifecycleFixture(directory, limit=1024)
        native, config, _evidence, owner = self.lifecycle.prelude()
        self.caller = self.lifecycle.accounted
        self.caller.initialize_and_activate(native, IDENTITY, config, owner)
        self.now = NOW
        self.monotonic = 100.0
        self.caller._clock = lambda: self.now
        self.caller._monotonic = lambda: self.monotonic
        self.initial_calls = self.caller.budget_view.calls
        self.initial_observations = len(self.caller.observations)
        self.initial_usable = self.caller.usable
        self.initial_state = self.caller.accounting
        self.calls = []
        self.fresh = [unsigned_hint({"exp": NOW + 600})]
        self.get_error = None
        self.post_error = None
        self.mutate_response = None
        self.caller._opener = self.external_open
        self.control = BrokerDispatchAccounting(
            self.caller.budget_view, "a" * 64, 60, 60
        )
        self.authority = self.lifecycle.review_authority()
        self.client = BrokerClient(
            broker_url="https://broker.github.test/api/github/token",
            opener=self.caller.open,
            control=self.control,
            authority=self.authority,
        )

    def advance(self, seconds):
        self.now += seconds
        self.monotonic += seconds

    def external_open(self, request, timeout):
        # Real caller admission/debit precedes this entirely synthetic boundary.
        assert (
            len(self.caller.observations)
            == self.initial_observations + len(self.calls) + 1
        )
        assert self.caller.budget_view.calls == self.initial_calls + len(self.calls) + 1
        assert 0 < timeout <= 30
        payload = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), request.full_url, payload))
        if request.get_method() == "GET":
            assert (
                request.full_url
                == "https://oidc.github.test/token?fixture=1&audience=sts.reviewsensei.dev"
            )
            assert (
                request.get_header("Authorization") == "Bearer synthetic-oidc-request"
            )
            if self.get_error:
                raise self.get_error
            return Response({"value": self.fresh.pop(0)})
        assert request.get_method() == "POST"
        assert request.full_url == "https://broker.github.test/api/github/token"
        if self.post_error:
            raise self.post_error
        document = {
            "token": "synthetic-capability-token",
            "capability": payload["capability"],
            "control_receipt": {**payload["control_budget"], "dispatched": 30},
            "session_state": "known",
            "app_id": 999,
            "reservation_owner": None,
            "session_grant": OPAQUE_GRANT,
            "session_attestation": granted(payload["session_attestation"]),
        }
        if self.mutate_response:
            self.mutate_response(document)
        return Response(document)

    def exchange(self, token, *, client=None):
        return (client or self.client).exchange(token, capability="review_publish")

    def assert_accounting(self, test, count, *, uncertain=0):
        state = self.caller.accounting
        test.assertEqual(len(self.calls), count)
        test.assertEqual(self.caller.budget_view.calls, self.initial_calls + count)
        test.assertEqual(
            state.confirmed, self.initial_state.confirmed + count - uncertain
        )
        test.assertEqual(state.uncertain, self.initial_state.uncertain + uncertain)
        test.assertEqual(self.caller.usable, self.initial_usable - count)
        test.assertEqual(state.configuration, self.initial_state.configuration)
        test.assertEqual(state.owner, self.initial_state.owner)
        test.assertEqual(state.startup, self.initial_state.startup)
        test.assertEqual(state.remaining, self.initial_state.remaining)
        test.assertEqual(
            [(b.block_id, b.issued_by) for b in state.blocks],
            [(b.block_id, b.issued_by) for b in self.initial_state.blocks],
        )
        terminal = self.caller._terminal_block
        test.assertEqual(
            next(b for b in state.blocks if b.block_id == terminal),
            next(b for b in self.initial_state.blocks if b.block_id == terminal),
        )
        for method, _url, payload in self.calls:
            if method != "POST":
                continue
            actual = payload["session_attestation"]
            expected = {
                **self.authority,
                "issued_at": actual["issued_at"],
                "expires_at": actual["expires_at"],
            }
            test.assertEqual(actual, expected)
            test.assertEqual(actual["version"], 3)
            test.assertEqual(actual["expires_at"] - actual["issued_at"], 600)
            test.assertEqual(payload["control_budget"]["operation_id"], "a" * 64)
            test.assertEqual(payload["control_budget"]["max_dispatches"], 60)


class OidcExpiryHintTests(unittest.TestCase):
    def test_only_bounded_positive_integer_exp_is_a_scheduling_hint(self):
        for value in (1, NOW, 9_007_199_254_740_991):
            with self.subTest(value=value):
                self.assertEqual(expiry_hint(unsigned_hint({"exp": value})), value)
        for value in (
            None,
            True,
            False,
            0,
            -1,
            1.0,
            "1800000000",
            9_007_199_254_740_992,
        ):
            with self.subTest(value=value):
                self.assertIsNone(expiry_hint(unsigned_hint({"exp": value})))

    def test_opaque_malformed_nonobject_and_oversized_inputs_have_no_hint(self):
        values = [
            None,
            True,
            1,
            b"opaque",
            "",
            "opaque",
            "a.b",
            "a.b.c.d",
            "a.!.c",
            "a._w.c",
            "a." + base64.urlsafe_b64encode(b"not json").decode() + ".c",
            unsigned_hint([]),
            unsigned_hint("text"),
            unsigned_hint({}),
            unsigned_hint({"exp": NOW, "padding": "x" * 32768}),
            "a" * 65537,
        ]
        for token in values:
            with self.subTest(token=repr(token)[:100]):
                self.assertIsNone(expiry_hint(token))


class OidcRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = RefreshFixture(self.directory.name)
        self.time_patch = patch(
            "review_sensei_github.broker_client.time",
            SimpleNamespace(
                time=lambda: self.fixture.now, monotonic=lambda: self.fixture.monotonic
            ),
        )
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.env_patch = patch.dict(
            os.environ,
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc-request",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
            },
            clear=True,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_refresh_get_and_capability_post_spend_two_physical_attempts(self):
        f = self.fixture
        original = unsigned_hint({"exp": NOW + 60})
        fresh = f.fresh[0]
        self.assertEqual(f.exchange(original), "synthetic-capability-token")
        self.assertEqual([c[0] for c in f.calls], ["GET", "POST"])
        self.assertEqual(f.calls[1][2]["oidc_token"], fresh)
        self.assertIs(f.client.opener.__self__, f.caller)
        self.assertIs(f.client.control.budget, f.caller.budget_view)
        f.assert_accounting(self, 2)

    def test_copies_reuse_fresh_assertion_for_same_original_without_another_get(self):
        f = self.fixture
        other = copy.copy(f.client)
        original = unsigned_hint({"exp": NOW + 59})
        fresh = f.fresh[0]
        f.exchange(original)
        f.exchange(original, client=other)
        self.assertIs(other._oidc_cache, f.client._oidc_cache)
        self.assertEqual([c[0] for c in f.calls], ["GET", "POST", "POST"])
        self.assertEqual(
            [c[2]["oidc_token"] for c in f.calls if c[0] == "POST"], [fresh, fresh]
        )
        f.assert_accounting(self, 3)

    def test_expired_cache_refreshes_once_and_preserves_original_run_allowance(self):
        f = self.fixture
        first = unsigned_hint({"exp": NOW + 61})
        second = unsigned_hint({"exp": NOW + 700})
        f.fresh = [first, second]
        original = unsigned_hint({"exp": NOW})
        f.exchange(original)
        f.advance(62)
        other = copy.copy(f.client)
        f.exchange(original, client=other)
        f.exchange(original)
        self.assertEqual(
            [c[0] for c in f.calls], ["GET", "POST", "GET", "POST", "POST"]
        )
        self.assertEqual(
            [c[2]["oidc_token"] for c in f.calls if c[0] == "POST"],
            [first, second, second],
        )
        self.assertEqual(f.client._oidc_cache["original"], original)
        f.assert_accounting(self, 5)

    def test_cache_is_bound_to_original_assertion(self):
        f = self.fixture
        first, second = f.fresh[0], unsigned_hint({"exp": NOW + 700})
        f.fresh.append(second)
        f.exchange(unsigned_hint({"exp": NOW, "jti": "one"}))
        f.exchange(unsigned_hint({"exp": NOW, "jti": "two"}))
        self.assertEqual([c[0] for c in f.calls], ["GET", "POST", "GET", "POST"])
        self.assertEqual(
            [c[2]["oidc_token"] for c in f.calls if c[0] == "POST"], [first, second]
        )
        f.assert_accounting(self, 4)

    def test_fresh_looking_hint_has_no_local_authentication_or_retry_authority(self):
        f = self.fixture
        original = unsigned_hint(
            {"exp": NOW + 61, "repository": "attacker/repo", "run_id": "999"}
        )
        f.post_error = HTTPError(
            "https://broker.github.test/token",
            401,
            "synthetic refusal",
            {},
            io.BytesIO(),
        )
        self.addCleanup(f.post_error.close)
        with self.assertRaises(GitHubBrokerClientError):
            f.exchange(original)
        self.assertEqual([c[0] for c in f.calls], ["POST"])
        self.assertEqual(f.calls[0][2]["oidc_token"], original)
        self.assertEqual(f.client._oidc_cache, {})
        f.assert_accounting(self, 1)

    def test_opaque_or_malformed_original_goes_unchanged_to_refusing_worker(self):
        f = self.fixture
        f.post_error = HTTPError(
            "https://broker.github.test/token",
            403,
            "synthetic refusal",
            {},
            io.BytesIO(),
        )
        self.addCleanup(f.post_error.close)
        for token in ("opaque", "unsigned.!.invalid", unsigned_hint({"exp": True})):
            with self.subTest(token=token), self.assertRaises(GitHubBrokerClientError):
                f.exchange(token)
        self.assertEqual([c[0] for c in f.calls], ["POST"] * 3)
        self.assertEqual(
            [c[2]["oidc_token"] for c in f.calls],
            ["opaque", "unsigned.!.invalid", unsigned_hint({"exp": True})],
        )
        self.assertEqual(f.client._oidc_cache, {})
        f.assert_accounting(self, 3)

    def test_invalid_fresh_assertion_refuses_before_capability_post(self):
        f = self.fixture
        values = [
            "opaque",
            "a.!.c",
            unsigned_hint({"exp": NOW + 60}),
            unsigned_hint({"exp": NOW - 1}),
            unsigned_hint({"exp": "future"}),
            None,
            "",
        ]
        f.fresh = values.copy()
        for value in values:
            with self.subTest(value=value), self.assertRaises(GitHubBrokerClientError):
                f.exchange(unsigned_hint({"exp": NOW}))
        self.assertEqual([c[0] for c in f.calls], ["GET"] * len(values))
        self.assertEqual(f.client._oidc_cache, {})
        f.assert_accounting(self, len(values))

    def test_refresh_timeout_is_charged_uncertain_with_no_post_or_retry(self):
        f = self.fixture
        f.get_error = TimeoutError("synthetic OIDC timeout")
        with self.assertRaisesRegex(
            GitHubBrokerClientError, "OIDC token request failed"
        ):
            f.exchange(unsigned_hint({"exp": NOW}))
        self.assertEqual([c[0] for c in f.calls], ["GET"])
        self.assertEqual(f.client._oidc_cache, {})
        f.assert_accounting(self, 1, uncertain=1)

    def test_refresh_http_failure_has_no_post_or_retry(self):
        f = self.fixture
        for status in (403, 429, 503):
            f.get_error = HTTPError(
                "https://oidc.github.test/token",
                status,
                "synthetic refusal",
                {},
                io.BytesIO(),
            )
            self.addCleanup(f.get_error.close)
            expected = (
                GitHubHTTPTransientError
                if status in (429, 503)
                else GitHubBrokerClientError
            )
            with self.subTest(status=status), self.assertRaises(expected):
                f.exchange(unsigned_hint({"exp": NOW}))
        self.assertEqual([c[0] for c in f.calls], ["GET"] * 3)
        self.assertEqual(f.client._oidc_cache, {})
        f.assert_accounting(self, 3)

    def test_post_timeout_after_refresh_never_replays_and_keeps_both_charges(self):
        f = self.fixture
        f.post_error = TimeoutError("synthetic capability timeout")
        with self.assertRaises(GitHubBrokerClientError):
            f.exchange(unsigned_hint({"exp": NOW}))
        self.assertEqual([c[0] for c in f.calls], ["GET", "POST"])
        f.assert_accounting(self, 2, uncertain=1)

    def test_refreshed_assertion_does_not_accept_wrong_returned_run_scope(self):
        f = self.fixture
        for field, value in (
            ("head_sha", "d" * 40),
            ("run_id", "999"),
            ("repository_id", 999),
        ):

            def wrong_scope(document):
                document["session_attestation"][field] = value

            f.mutate_response = wrong_scope
            f.fresh.append(unsigned_hint({"exp": NOW + 600}))
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(GitHubBrokerClientError, "grant conflicts"),
            ):
                f.client.open_session(
                    unsigned_hint({"exp": NOW, "jti": field}),
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                )
        self.assertEqual([c[0] for c in f.calls], ["GET", "POST"] * 3)
        f.assert_accounting(self, 6)

    def test_refresh_cannot_spend_protected_capacity_or_reset_budget(self):
        f = self.fixture
        # Consume real physical reads to leave a bounded hold acceptable to the
        # shared facade. No counters or durable state are assigned by this test.
        while f.caller.usable > 20:
            f.caller._opener = lambda request, timeout: Response({})

            with f.caller.open(
                Request("https://github.test/read"), timeout=30
            ) as response:
                response.read(1024)
        f.caller._opener = f.external_open
        f.initial_calls = f.caller.budget_view.calls
        f.initial_observations = len(f.caller.observations)
        f.initial_usable = f.caller.usable
        f.initial_state = f.caller.accounting
        held = f.caller.usable - 1
        f.caller.budget_view.hold_ordinary(held)
        original = unsigned_hint({"exp": NOW})
        with self.assertRaises(ReviewInputError):
            f.exchange(original)
        self.assertEqual([c[0] for c in f.calls], ["GET"])
        self.assertEqual(f.caller.budget_view._ordinary_hold, held)
        self.assertEqual(f.caller.budget_view.ordinary_remaining(), 0)
        f.assert_accounting(self, 1)
        f.caller.budget_view.release_ordinary(held)
        f.exchange(original, client=copy.copy(f.client))
        self.assertEqual([c[0] for c in f.calls], ["GET", "POST"])
        f.assert_accounting(self, 2)

    def test_refresh_cannot_create_missing_current_run_authority(self):
        f = self.fixture
        client = copy.copy(f.client)
        client.authority = None
        with self.assertRaises(GitHubBrokerClientError):
            f.exchange(unsigned_hint({"exp": NOW}), client=client)
        self.assertEqual(f.client._oidc_cache, {})
        f.assert_accounting(self, 0)

    def test_fresh_hint_claims_cannot_upgrade_source_authority_or_bypass_refusal(self):
        f = self.fixture
        fresh = unsigned_hint(
            {
                "exp": NOW + 600,
                "repository": "attacker/repo",
                "run_id": "999",
                "purpose": "command",
                "actor": {"association": "OWNER"},
            }
        )
        f.fresh = [fresh]
        f.post_error = HTTPError(
            "https://broker.github.test/token",
            403,
            "synthetic Worker refusal",
            {},
            io.BytesIO(),
        )
        self.addCleanup(f.post_error.close)
        with self.assertRaises(GitHubBrokerClientError):
            f.exchange(unsigned_hint({"exp": NOW}))
        self.assertEqual([c[0] for c in f.calls], ["GET", "POST"])
        self.assertEqual(f.calls[1][2]["oidc_token"], fresh)
        f.assert_accounting(self, 2)

    def test_absent_oidc_request_environment_does_not_dispatch_or_authorize(self):
        f = self.fixture
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                GitHubBrokerClientError, "OIDC token is not available"
            ):
                f.exchange(unsigned_hint({"exp": NOW}))
        self.assertEqual(f.client._oidc_cache, {})
        f.assert_accounting(self, 0)
