"""Receipt operation pricing is necessary, never sufficient route authority."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.action_receipt import publish_command_receipt
from review_sensei_github.api_run_plan import COMMAND_RECEIPT_CONFIRMATION_REQUESTS
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.caller_operation import CallerOperation

from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from tests.test_run_authority import NOW, Response
from tests.test_trust_effect_initialization import IDENTITY, InitialEffectFixture


class ReceiptFixture(InitialEffectFixture):
    mismatch = False

    def __init__(self, directory):
        from pathlib import Path

        from review_sensei_github.api_run_host import review_startup_plan
        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
        from review_sensei_github.trust_effects import enable

        from review_sensei.api_work import ApiRunIdentity
        from tests.test_current_trust_observation import observed
        from tests.test_run_authority import HEAD

        self.calls, self.native, self.native_requests = [], [], []
        self.comment = self.refresh_hook = self.change = None
        self.one_short = self.lost_post = False
        self.request, self.grant = observed("command")
        observation = self.grant["current_observation"]
        observation["schema_version"] = "current-observation-v2"
        observation["actor"].update(
            repository_permission="admin", repository_role="admin"
        )
        self.caller = ApiRunRequestTransport(
            directory=Path(directory),
            run=ApiRunIdentity(123, 456),
            plan=review_startup_plan("command"),
            attempt=1,
            job="command",
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
        body = json.loads(request.data) if request.data else {}
        if body.get("session_attestation", {}).get("version") == 3:
            self.calls.append((request.full_url, body))
            return Response(
                {
                    "token": "synthetic-receipt-token",
                    "capability": "other" if self.mismatch else body["capability"],
                }
            )
        return super().open(request, timeout)

    def initialize(self):
        from review_sensei.api_work import ApiExecutionOwner, FrozenApiConfiguration

        return self.caller.initialize_and_activate(
            self.ledger,
            IDENTITY,
            FrozenApiConfiguration("a" * 40, "c" * 64, 512),
            ApiExecutionOwner(1, "command", 0),
            trust_source=(self.broker, self.session),
        )

    def publish(self):
        broker = BrokerClient(
            opener=self.caller.open, authority={**self.request, "version": 3}
        )
        run = SimpleNamespace(
            broker=broker,
            oidc="synthetic-oidc",
            identity=IDENTITY,
            http=self.http,
            ledger=self.ledger,
            caller=self.caller,
        )
        # Rendering/fencing inputs do not grant a business permit. Actual
        # transport remains source-bound and must reject this unadmitted route.
        authority = SimpleNamespace(
            document={"source": {"id": 1234}, "actor": {"login": "alice"}},
            fence=lambda: None,
        )
        publish_command_receipt(
            run,
            authority,
            "synthetic-command",
            SimpleNamespace(receipts=(), applied=True),
        )


class TrustCommandReceiptTests(unittest.TestCase):
    def setUp(self):
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_actual_source_capability_prices_refresh_but_cannot_grant_dispatch(self):
        f = ReceiptFixture(self.directory.name)
        f.initialize()
        operation = f.caller._operation
        captured = []

        @contextmanager
        def observe(key, capacity, **kwargs):
            if key.startswith("command-receipt:"):
                captured.append((capacity, kwargs.copy()))
            with operation(key, capacity, **kwargs) as value:
                yield value

        with patch.object(f.caller, "_operation", observe):
            with self.assertRaisesRegex(ReviewInputError, "complete route admission"):
                f.publish()
        tail = 1 + COMMAND_RECEIPT_CONFIRMATION_REQUESTS
        self.assertEqual(
            captured, [(tail + 1, {"write_tail": tail, "prewrite_reads": 1})]
        )
        self.assertEqual(sum(m == "POST" for m, _, _ in f.native), 1)
        self.assertEqual(f.caller.accounting.configuration.limit, 512)
        self.assertEqual(sum("refresh_effect_sha256" in b for _, b in f.calls), 1)

    def test_wrong_source_capability_refuses_before_receipt_operation(self):
        f = ReceiptFixture(self.directory.name)
        f.initialize()
        f.mismatch = True
        with self.assertRaisesRegex(ReviewInputError, "matching source issuance"):
            f.publish()
        self.assertFalse(
            any(
                o.key.startswith("command-receipt:")
                for o, _ in f.caller._issued_operations
            )
        )
        self.assertEqual(sum(m == "POST" for m, _, _ in f.native), 1)

    def test_original_budget_one_short_refuses_before_operation_or_refresh(self):
        from urllib.request import Request

        f = ReceiptFixture(self.directory.name)
        f.initialize()
        tail = 1 + COMMAND_RECEIPT_CONFIRMATION_REQUESTS
        # Existing capability exchange and receipt discovery each spend one.
        # Leave only the unchanged native tail, not its extra verification.
        while f.caller.usable > tail + 2:
            with f.caller.open(
                Request("https://api.github.test/repos/owner/repo/pulls/7"), timeout=30
            ) as response:
                response.read(65536)
        with self.assertRaises(EvidenceBudgetExhausted):
            f.publish()
        self.assertEqual(f.caller.usable, tail)
        self.assertFalse(
            any(
                o.key.startswith("command-receipt:")
                for o, _ in f.caller._issued_operations
            )
        )
        self.assertEqual(sum(m == "POST" for m, _, _ in f.native), 1)
        self.assertEqual(sum("refresh_effect_sha256" in b for _, b in f.calls), 1)
        self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_one_extra_read_preserves_exact_confirmation_and_one_short_refuses(self):
        tail = 1 + COMMAND_RECEIPT_CONFIRMATION_REQUESTS
        operation = CallerOperation(tail + 1, tail)
        operation.consume(mutation=False)  # broker verification
        operation.consume(mutation=True)
        for _ in range(COMMAND_RECEIPT_CONFIRMATION_REQUESTS):
            operation.consume(mutation=False)
        self.assertEqual(operation.remaining, 0)
        with self.assertRaises(EvidenceBudgetExhausted):
            CallerOperation(tail, tail).consume(mutation=False)
