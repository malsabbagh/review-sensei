"""Actual initialization-only refresh dispatch inside the original startup cap."""

import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
from review_sensei_github.trust_effects import enable

from review_sensei.api_work import ApiExecutionOwner, FrozenApiConfiguration
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.session import SessionIdentity
from tests.test_run_authority import HEAD, NOW
from tests.test_trust_effects import EffectSourceFixture

IDENTITY = SessionIdentity("owner/repo", 7, repository_id=123)


class InitialEffectFixture(EffectSourceFixture):
    def __init__(self, directory):
        self.comment = None
        self.native = []
        self.native_requests = []
        self.one_short = False
        self.refresh_hook = None
        self.lost_post = False
        super().__init__(directory)
        self.http = GitHubHttp(
            api_url="https://api.github.test", opener=self.caller.open
        )
        self.ledger = GitHubIssueCommentSessionLedger(
            self.http,
            token=self.session.token,
            app_slug="reviewsensei[bot]",
            reservation_owner={"run_id": "456", "head_sha": HEAD},
        )
        self.ledger.authenticated_app_id = 999
        self.ledger._head_sha = HEAD
        self.profile = enable(
            self.caller, self.broker, self.session, api_url=self.http.api_url
        )

    def open(self, request, timeout):
        if urlsplit(request.full_url).hostname != "api.github.test":
            response = super().open(request, timeout)
            if (
                request.full_url.endswith("/session-grant")
                and self.refresh_hook is not None
            ):
                self.refresh_hook()
            return response
        method, path = request.get_method(), urlsplit(request.full_url).path
        self.native.append((method, path, request.data))
        self.native_requests.append((request, self.caller._sequence))
        if (
            method == "GET"
            and path.endswith("/issues/7/comments")
            and self.comment is None
        ):
            if self.one_short:
                # Spend actual read requests up to exactly three remaining
                # original startup slots; never change the plan or allowance.
                self.one_short = False
                while (
                    len(self.caller._slot.requests) - self.caller.startup_dispatches > 3
                ):
                    with self.caller.open(
                        __import__("urllib.request", fromlist=["Request"]).Request(
                            "https://api.github.test/repos/owner/repo/pulls/7"
                        ),
                        timeout=30,
                    ) as response:
                        response.read(65536)
        if method == "GET" and path.endswith("/pulls/7"):
            return json_response({"head": {"sha": HEAD}})
        if method == "GET" and path.endswith("/issues/7/comments"):
            return json_response(
                [] if self.comment is None else [copy.deepcopy(self.comment)]
            )
        if method == "POST" and path.endswith("/issues/7/comments"):
            self.comment = {
                "id": 1000000,
                "body": json.loads(request.data)["body"],
                "user": {"id": 999, "type": "Bot", "login": "reviewsensei[bot]"},
            }
            if self.lost_post:
                raise OSError("synthetic native response lost")
            return json_response(copy.deepcopy(self.comment), status=201)
        raise AssertionError((method, path))

    def initialize(self):
        return self.caller.initialize_and_activate(
            self.ledger,
            IDENTITY,
            FrozenApiConfiguration("a" * 40, "c" * 64, 512),
            ApiExecutionOwner(1, "hosted", 0),
            trust_source=(self.broker, self.session),
        )


class TrustInitializationTests(unittest.TestCase):
    def setUp(self):
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_real_initializer_refreshes_exact_root_once_before_post(self):
        f = InitialEffectFixture(self.directory.name)
        result = f.initialize()
        self.assertEqual(result.configuration.limit, 512)
        self.assertIsNotNone(f.comment)
        self.assertEqual(sum(m == "POST" for m, _, _ in f.native), 1)
        self.assertEqual(len(f.calls), 2)
        self.assertIn("refresh_effect_sha256", f.calls[-1][1])
        self.assertEqual(f.caller.startup_dispatches, len(f.calls) + len(f.native))
        self.assertEqual(f.caller.accounting.configuration.limit, 512)
        request, sequence = next(
            (r, n) for r, n in f.native_requests if r.get_method() == "POST"
        )
        binding = {
            "schema_version": "native-trust-effect-v1",
            "method": "POST",
            "url": request.full_url,
            "body_sha256": hashlib.sha256(request.data).hexdigest(),
            "authorization_sha256": hashlib.sha256(
                request.get_header("Authorization").encode()
            ).hexdigest(),
            "content_type": request.get_header("Content-type"),
            "accept": request.get_header("Accept"),
            "api_version": request.get_header("X-github-api-version"),
            "if_match": request.get_header("If-match"),
            "if_none_match": request.get_header("If-none-match"),
            "original_sha256": hashlib.sha256(f.profile.original).hexdigest(),
            "grant_sha256": f.profile.grant_sha256,
            "sequence": sequence,
        }
        self.assertEqual(
            f.calls[-1][1]["refresh_effect_sha256"],
            hashlib.sha256(canonical_bytes(binding)).hexdigest(),
        )

    def test_changed_role_refuses_before_initial_root_and_preserves_charge(self):
        f = InitialEffectFixture(self.directory.name)
        f.change = "role"
        with self.assertRaisesRegex(Exception, "unconfirmed"):
            f.initialize()
        self.assertIsNone(f.comment)
        self.assertFalse(any(m == "POST" for m, _, _ in f.native))
        self.assertEqual(len(f.calls), 2)

    def test_one_short_original_tail_refuses_before_refresh_or_root(self):
        f = InitialEffectFixture(self.directory.name)
        f.one_short = True
        with self.assertRaisesRegex(
            EvidenceBudgetExhausted, "refresh and confirmation tail"
        ):
            f.initialize()
        self.assertIsNone(f.comment)
        self.assertFalse(any(m == "POST" for m, _, _ in f.native))
        self.assertEqual(len(f.calls), 1)

    def test_authentication_or_body_change_after_refresh_refuses_native(self):
        from urllib.request import Request

        for change in ("authorization", "content_type", "body"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = InitialEffectFixture(directory)
                pending = []

                def capture(*args, **kwargs):
                    request = Request(*args, **kwargs)
                    if request.get_method() == "POST":
                        pending.append(request)
                    return request

                def mutate():
                    request = pending[-1]
                    if change == "authorization":
                        request.add_header(
                            "Authorization", "Bearer different-synthetic-token"
                        )
                    elif change == "content_type":
                        request.add_header("Content-Type", "text/plain")
                    else:
                        request.data = b"{}"

                f.refresh_hook = mutate
                with patch("review_sensei_github.http.Request", side_effect=capture):
                    with self.assertRaisesRegex(
                        ReviewInputError, "verification sequence changed"
                    ):
                        f.initialize()
                self.assertIsNone(f.comment)
                self.assertFalse(any(m == "POST" for m, _, _ in f.native))

    def test_native_response_loss_preserves_unknown_and_never_replays(self):
        f = InitialEffectFixture(self.directory.name)
        f.lost_post = True
        with self.assertRaises(GitHubPublicationTransientError):
            f.initialize()
        self.assertIsNotNone(f.comment)
        self.assertEqual(sum(m == "POST" for m, _, _ in f.native), 1)
        self.assertEqual(f.caller.observations[-1]["outcome"], "unknown")
        self.assertIsNone(f.caller._state)
        self.assertEqual(len(f.calls), 2)

    def test_native_source_or_session_token_substitution_refuses_before_reads(self):
        f = InitialEffectFixture(self.directory.name)
        with patch.object(f.ledger, "initialize_with_mutation", return_value=None):
            with self.assertRaisesRegex(ReviewInputError, "implementation changed"):
                f.initialize()
        object.__setattr__(f.session, "token", "different-synthetic-token")
        with self.assertRaisesRegex(ReviewInputError, "observation changed"):
            f.initialize()
        self.assertEqual(f.native, [])
        self.assertEqual(len(f.calls), 1)

    def test_unqualified_business_still_holds_after_successful_initialization(self):
        from urllib.request import Request

        f = InitialEffectFixture(self.directory.name)
        f.initialize()
        before = len(f.calls) + len(f.native)
        with self.assertRaisesRegex(
            ReviewInputError, "complete route admission unavailable"
        ):
            f.caller.open(
                Request(
                    "https://api.github.test/repos/owner/repo/pulls/7/reviews",
                    data=b"{}",
                    method="POST",
                ),
                timeout=30,
            )
        self.assertEqual(len(f.calls) + len(f.native), before)
