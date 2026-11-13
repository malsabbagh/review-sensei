"""Actual closed status source, original funding, fresh root-only effects."""

import copy
import hashlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.action_run import execute_command
from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.review_run import ActionRunLedger, ReviewRun

from review_sensei.errors import ReviewInputError
from review_sensei.hosted import HostedPlan, HostedPolicy
from tests.test_current_trust_observation import observed
from tests.test_run_authority import HEAD, NOW, Response
from tests.test_trust_command_receipt import ReceiptFixture
from tests.test_trust_effect_initialization import IDENTITY

BODY = "@reviewsensei review status"


class StatusFixture(ReceiptFixture):
    def __init__(self, directory):
        request, grant = observed("command")
        for document in (request, grant):
            document["source"]["body_sha256"] = hashlib.sha256(
                BODY.encode()
            ).hexdigest()
        with patch(
            "tests.test_current_trust_observation.observed",
            return_value=(request, grant),
        ):
            super().__init__(directory)
        self.loss_stage = None
        self.absent_loss = False
        self.initialize()
        self.action_ledger = ActionRunLedger(self.ledger, self.caller, IDENTITY)
        self.run = ReviewRun(
            caller=self.caller,
            ledger=self.action_ledger,
            http=self.http,
            broker=self.broker,
            oidc="synthetic-oidc",
            identity=IDENTITY,
            plan=HostedPlan(
                "synthetic",
                "synthetic",
                "https://provider.invalid",
                False,
                None,
                False,
                False,
                "hosted",
                "remote",
                None,
                30,
                HostedPolicy(
                    False,
                    True,
                    "advisory",
                    True,
                    "disabled",
                    "disabled",
                    "enabled",
                    "default",
                ),
                None,
                {},
            ),
            base_sha="a" * 40,
            head_sha=HEAD,
            base_branch="main",
            head_repository="owner/repo",
            configuration_path=None,
            session_authority=self.session.attestation,
            session_grant=self.session.grant,
        )
        self.args = SimpleNamespace(
            comment_body=BODY,
            source_comment_id=self.request["source"]["id"],
            app_slug="reviewsensei[bot]",
            review_mode=None,
        )

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        if path.endswith("/session-grant"):
            body = json.loads(request.data)
            if "refresh_effect_sha256" not in body:
                self.calls.append((request.full_url, body))
                return Response(
                    {"session_attestation": copy.deepcopy(body["session_attestation"])}
                )
        if urlsplit(request.full_url).hostname == "api.github.test":
            if request.get_method() == "GET" and path.endswith("/issues/comments/81"):
                self.native.append(("GET", path, None))
                return json_response(
                    {
                        "id": 81,
                        "body": BODY,
                        "updated_at": self.request["source"]["updated_at"],
                        "user": {"login": "alice", "type": "User", "id": 101},
                        "author_association": "OWNER",
                    }
                )
            if request.get_method() == "GET" and path.endswith("/pulls/7"):
                self.native.append(("GET", path, None))
                return json_response({"head": {"sha": HEAD}, "state": "open"})
            if request.get_method() == "PATCH" and path.endswith(
                "/issues/comments/1000000"
            ):
                self.native.append(("PATCH", path, request.data))
                self.native_requests.append((request, self.caller._sequence))
                losing = self.loss_stage == sum(m == "PATCH" for m, _, _ in self.native)
                if not losing or not self.absent_loss:
                    self.comment["body"] = json.loads(request.data)["body"]
                if losing:
                    raise OSError("synthetic status root response lost")
                return json_response(copy.deepcopy(self.comment))
        return super().open(request, timeout)


class TrustStatusRouteTests(unittest.TestCase):
    def setUp(self):
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_actual_status_source_finishes_three_fresh_roots(self):
        f = StatusFixture(self.directory.name)
        self.assertEqual(execute_command(f.run, f.args), 0)
        self.assertEqual(sum(m == "PATCH" for m, _, _ in f.native), 3)
        self.assertEqual(sum("refresh_effect_sha256" in b for _, b in f.calls), 4)
        self.assertEqual(f.caller.accounting.action["phase"], "terminal")
        self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_exact_original_work_quote_and_one_short_before_any_root_effect(self):
        from urllib.request import Request

        from review_sensei_github.trust_status import STATUS_WORK_CALLS

        from review_sensei.errors import EvidenceBudgetExhausted

        for remaining in (STATUS_WORK_CALLS, STATUS_WORK_CALLS - 1):
            with (
                self.subTest(remaining=remaining),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = StatusFixture(directory)
                original = f.comment["body"]
                while f.caller.usable > remaining:
                    with f.caller.open(
                        Request("https://api.github.test/repos/owner/repo/pulls/7"),
                        timeout=30,
                    ) as response:
                        response.read(65536)
                if remaining == STATUS_WORK_CALLS:
                    self.assertEqual(execute_command(f.run, f.args), 0)
                else:
                    with self.assertRaises(EvidenceBudgetExhausted):
                        execute_command(f.run, f.args)
                    self.assertEqual(f.comment["body"], original)
                    self.assertEqual(sum(m == "PATCH" for m, _, _ in f.native), 0)
                self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_changed_role_and_source_implementation_refuse_before_native_effect(self):
        from review_sensei.errors import ReviewInputError

        for fault in ("role", "load", "request", "command", "after-refresh-token"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = StatusFixture(directory)
                original = f.comment["body"]
                if fault == "role":
                    f.change = "role"
                elif fault == "load":
                    f.action_ledger.load = lambda *a, **k: None
                elif fault == "request":
                    f.http.request = lambda *a, **k: None
                elif fault == "command":
                    f.args.comment_body = "@reviewsensei review pause"
                else:
                    f.refresh_hook = lambda: setattr(
                        f.action_ledger, "token", "foreign-token"
                    )
                with self.assertRaises((ReviewInputError, GitHubBrokerClientError)):
                    execute_command(f.run, f.args)
                self.assertEqual(f.comment["body"], original)
                self.assertEqual(sum(m == "PATCH" for m, _, _ in f.native), 0)

    def test_stored_response_losses_reconcile_exact_roots_without_replay(self):
        for stage in (1, 2, 3):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                f = StatusFixture(directory)
                f.loss_stage = stage
                self.assertEqual(execute_command(f.run, f.args), 0)
                self.assertEqual(sum(m == "PATCH" for m, _, _ in f.native), 3)
                self.assertEqual(f.caller.accounting.action["phase"], "terminal")
                self.assertTrue(
                    any(
                        o["outcome"] == "unknown" and o.get("mutation")
                        for o in f.caller.observations
                    )
                )
                self.assertGreater(f.caller.accounting.uncertain, 0)

    def test_missing_lost_root_retains_unknown_and_never_replays(self):
        f = StatusFixture(self.directory.name)
        original = f.comment["body"]
        f.loss_stage, f.absent_loss = 1, True
        with self.assertRaises(ReviewInputError):
            execute_command(f.run, f.args)
        self.assertEqual(f.comment["body"], original)
        self.assertEqual(sum(m == "PATCH" for m, _, _ in f.native), 1)
        self.assertGreater(f.caller.accounting.uncertain, 0)

    def test_source_registration_cannot_be_reissued_and_route_does_not_escape(self):
        from urllib.request import Request

        from review_sensei_github import action_run
        from review_sensei_github.trust_effects import _install_command_source

        with self.assertRaisesRegex(ReviewInputError, "registration is invalid"):
            _install_command_source(action_run)
        f = StatusFixture(self.directory.name)
        self.assertEqual(execute_command(f.run, f.args), 0)
        before = len(f.native)
        with self.assertRaisesRegex(ReviewInputError, "complete route admission"):
            f.caller.open(
                Request(
                    "https://api.github.test/repos/owner/repo/issues/comments/1000000",
                    data=b"{}",
                    method="PATCH",
                ),
                timeout=30,
            )
        self.assertEqual(len(f.native), before)
