"""Existing review updates cannot allocate a different native receipt identity."""

import copy
import unittest

from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_journal as fixture


class PutReceiptBindingTests(unittest.TestCase):
    def run_response(self, payload, status=200):
        journal = fixture.journal()
        effect = fixture.effect(
            {"body": fixture.REQUEST["body"]}, path=fixture.PATH + "/1201", method="PUT"
        )
        http = fixture.FakeHTTP((status, payload))
        return journal, effect, http

    def test_exact_existing_review_is_confirmed_with_known_numeric_producer(self):
        journal, effect, http = self.run_response(fixture.native_review())
        journal._dispatch_effect(
            http,
            effect,
            "PUT",
            effect["path"],
            token="fixture",
            body={"body": fixture.REQUEST["body"]},
        )
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["response"],
            [200, {"id": 1201, "producer_id": 9001, "app_id": None, "mutation": None}],
        )

    def test_wrong_scope_malformed_or_other_success_status_stays_pending(self):
        for fault in (
            "id",
            "boolean_id",
            "producer",
            "head",
            "body",
            "status",
            "missing",
        ):
            with self.subTest(fault=fault):
                payload = fixture.native_review()
                if fault == "id":
                    payload["id"] = 9999
                elif fault == "boolean_id":
                    payload["id"] = True
                elif fault == "producer":
                    payload["user"]["id"] = 9999
                elif fault == "head":
                    payload["commit_id"] = "e" * 40
                elif fault == "body":
                    payload["body"] = "foreign"
                elif fault == "missing":
                    payload = None
                journal, effect, http = self.run_response(
                    payload, 201 if fault == "status" else 200
                )
                before = copy.deepcopy(effect)
                with self.assertRaises(ReviewInputError):
                    journal._dispatch_effect(
                        http,
                        effect,
                        "PUT",
                        effect["path"],
                        token="fixture",
                        body={"body": fixture.REQUEST["body"]},
                    )
                self.assertEqual(effect, before)
                self.assertEqual(len(http.calls), 1)
