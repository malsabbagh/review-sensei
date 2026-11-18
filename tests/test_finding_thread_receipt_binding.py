"""Known GraphQL targets have closed receipts, not newly allocated identities."""

import copy
import unittest

from review_sensei_github.finding_lifecycle import (
    FindingLifecycleJournal,
    _thread_effect_target,
    _unpack_journal,
)

from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_journal as fixtures


class ThreadReceiptBindingTests(unittest.TestCase):
    def effect(self):
        return fixtures.effect(
            {
                "query": "mutation { resolveReviewThread }",
                "variables": {"input": {"threadId": "KNOWN_THREAD"}},
            },
            path="/graphql",
            method="POST",
        )

    def response(
        self, *, target="KNOWN_THREAD", resolved=True, name="resolveReviewThread"
    ):
        return {"data": {name: {"thread": {"id": target, "isResolved": resolved}}}}

    def test_ambiguous_multi_mutation_request_has_no_exact_target_witness(self):
        for query in (
            "mutation { resolveReviewThread unresolveReviewThread }",
            "mutation { first: resolveReviewThread second: resolveReviewThread }",
        ):
            effect = self.effect()
            effect["request"]["query"] = query
            self.assertIsNone(_thread_effect_target(effect))

    def test_new_receipt_exactly_binds_target_direction_and_canonical_metadata(self):
        effect = self.effect()
        payload = self.response()
        payload["id"] = 999
        receipt = FindingLifecycleJournal._compact_response(payload, effect=effect)
        self.assertEqual(
            receipt,
            {
                "id": None,
                "producer_id": None,
                "app_id": None,
                "mutation": {
                    "name": "resolveReviewThread",
                    "thread_id": "KNOWN_THREAD",
                    "resolved": True,
                },
            },
        )

    def test_foreign_or_wrong_state_response_keeps_durable_intent_pending(self):
        for payload, status in (
            (self.response(target="OTHER_THREAD"), 200),
            (self.response(resolved=False), 200),
            (self.response(name="unresolveReviewThread"), 200),
            (self.response(), 201),
            ({**self.response(), "errors": [{"message": "unconfirmed"}]}, 200),
            (self.response(resolved=1), 200),
        ):
            with self.subTest(payload=payload, status=status):
                journal = fixtures.journal()
                effect = self.effect()
                before = copy.deepcopy(effect)
                http = fixtures.FakeHTTP((status, payload))
                with self.assertRaises(ReviewInputError):
                    journal._dispatch_effect(
                        http,
                        effect,
                        "POST",
                        "/graphql",
                        token="fixture",
                        body=effect["request"],
                    )
                self.assertEqual(effect, before)
                self.assertEqual(len(http.calls), 1)

    def test_known_future_receipt_preserves_requested_id_without_new_id_allocation(
        self,
    ):
        journal = fixtures.journal()
        journal.effects = [self.effect()]
        _, future, effects = journal._preflight_effect(capacity_only=True)
        document = _unpack_journal(future)
        self.assertEqual(
            document["effects"][-1]["response"][1]["mutation"]["thread_id"],
            "KNOWN_THREAD",
        )
        self.assertEqual(
            effects[-1]["response"][1]["mutation"]["thread_id"], "KNOWN_THREAD"
        )

    def test_retained_integer_boolean_is_not_an_exact_confirmed_receipt(self):
        from review_sensei_github.finding_lifecycle import (
            _confirmed_thread_response_matches,
        )

        effect = self.effect()
        effect["status"] = "confirmed"
        effect["response"] = [
            200,
            FindingLifecycleJournal._compact_response(self.response(), effect=effect),
        ]
        self.assertTrue(_confirmed_thread_response_matches(effect))
        effect["response"][1]["mutation"]["resolved"] = 1
        self.assertFalse(_confirmed_thread_response_matches(effect))

    def test_retained_conflicting_receipt_is_not_rewritten_or_reissued(self):
        journal = fixtures.journal()
        effect = self.effect()
        effect["status"] = "confirmed"
        effect["response"] = [
            200,
            {
                "id": None,
                "producer_id": None,
                "app_id": None,
                "mutation": {
                    "name": "resolveReviewThread",
                    "thread_id": "OTHER_THREAD",
                    "resolved": True,
                },
            },
        ]
        before = copy.deepcopy(effect)
        with self.assertRaisesRegex(
            ReviewInputError, "retained mutation receipt conflicts"
        ):
            journal._readback_thread(effect, token="fixture")
        self.assertEqual(effect, before)
