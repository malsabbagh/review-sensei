"""Native check identities cannot exceed the preflight receipt width."""

import unittest

from review_sensei_github.check_diagnostics import CheckOperationError
from review_sensei_github.checks import ReviewCheckPublisher, review_check_outcome

from review_sensei.errors import ReviewInputError
from tests import test_finding_check_receipt_binding as receipt_fixture
from tests import test_finding_lifecycle_journal as fixture
from tests import test_gate_and_default_approval as gate

MAX_ID = 2**63 - 1


class CheckNativeIdentityTests(unittest.TestCase):
    def complete(self, responses):
        http, calls = gate.make_http(list(responses))
        publisher = ReviewCheckPublisher(http=http)
        return publisher, calls

    def invoke(self, publisher):
        return publisher.complete(
            token="fixture",
            repository="owner/repo",
            head_sha=gate.HEAD,
            app_slug=gate.APP,
            outcome=review_check_outcome(
                policy="auto-approve", review_status="partial", required_fixes=False
            ),
        )

    def row(self, identifier):
        return {
            "id": identifier,
            "name": "ReviewSensei",
            "head_sha": gate.HEAD,
            "app": {"slug": "reviewsensei", "id": 123},
        }

    def test_create_signed_native_boundary_and_oversize(self):
        for identifier in (1, MAX_ID, MAX_ID + 1, 0, -1, True):
            with self.subTest(identifier=identifier):
                publisher, calls = self.complete(
                    [
                        gate.json_response({"check_runs": []}),
                        gate.json_response({"id": identifier}, 201),
                    ]
                )
                if type(identifier) is int and 1 <= identifier <= MAX_ID:
                    result = self.invoke(publisher)
                    self.assertEqual(
                        (result.status, result.check_run_id), ("created", identifier)
                    )
                else:
                    with self.assertRaises(CheckOperationError):
                        self.invoke(publisher)
                self.assertEqual([c[0] for c in calls], ["GET", "POST"])

    def test_update_requires_exact_bounded_target(self):
        for target, returned in (
            (MAX_ID, MAX_ID),
            (42, 43),
            (42, MAX_ID + 1),
            (42, True),
        ):
            with self.subTest(target=target, returned=returned):
                publisher, calls = self.complete(
                    [
                        gate.json_response({"check_runs": [self.row(target)]}),
                        gate.json_response({"id": returned}),
                    ]
                )
                if target == returned:
                    self.assertEqual(self.invoke(publisher).check_run_id, target)
                else:
                    with self.assertRaises(CheckOperationError):
                        self.invoke(publisher)
                self.assertEqual([c[0] for c in calls], ["GET", "PATCH"])

    def test_malformed_matching_lookup_does_not_create(self):
        for identifier in (MAX_ID + 1, 0, -1, True, "42"):
            with self.subTest(identifier=identifier):
                publisher, calls = self.complete(
                    [
                        gate.json_response({"check_runs": [self.row(identifier)]}),
                    ]
                )
                with self.assertRaises(CheckOperationError):
                    self.invoke(publisher)
                self.assertEqual([c[0] for c in calls], ["GET"])

    def test_oversized_journal_receipt_and_precondition_remain_unknown(self):
        setup = receipt_fixture.CheckReceiptBindingTests()
        for field in ("id", "app"):
            with self.subTest(field=field):
                journal, effect, source, _ = setup.prepared()
                response = receipt_fixture.native()
                if field == "id":
                    response["id"] = MAX_ID + 1
                else:
                    response["app"]["id"] = MAX_ID + 1
                with self.assertRaises(ReviewInputError):
                    journal._dispatch_effect(
                        fixture.FakeHTTP((200, response)),
                        effect,
                        "PATCH",
                        "/repos/owner/repo/check-runs/1234",
                        token="fixture",
                        body=effect["request"],
                    )
                self.assertEqual(effect["status"], "pending")
                self.assertIsNone(effect["response"])
