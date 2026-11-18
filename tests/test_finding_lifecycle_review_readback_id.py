"""An unconfirmed review ID cannot become a follow-up comments endpoint."""

import unittest

import tests.test_finding_lifecycle_journal as fixtures
from review_sensei.bounded_evidence import canonical_bytes


class ReviewReadbackIdentityTests(unittest.TestCase):
    def exercise(self, identifier):
        request = {**fixtures.REQUEST, "event": "APPROVE", "comments": []}
        effect = fixtures.effect(request)
        journal = fixtures.journal(retained=fixtures.document([effect]))
        observed = fixtures.native_review(id=identifier, state="APPROVED")
        http = fixtures.ScriptedHTTP([(200, [observed]), (200, [])])
        before = canonical_bytes(journal.effects)
        result = journal._readback(http, journal.effects[0], token="synthetic-token")
        self.assertEqual(canonical_bytes(journal.effects), before)
        self.assertEqual(journal.effects[0]["status"], "pending")
        self.assertIsNone(journal.effects[0]["response"])
        self.assertTrue(all(method == "GET" for method, _, _ in http.calls))
        return result, http.calls, observed

    def test_invalid_id_does_not_send_followup_comments_get(self):
        for identifier in (
            True,
            False,
            0,
            -1,
            2**63,
            10**100,
            "1201",
            None,
            1.5,
            [],
            {},
        ):
            with self.subTest(identifier=identifier):
                result, calls, _ = self.exercise(identifier)
                self.assertIsNone(result)
                self.assertEqual(
                    [path for _, path, _ in calls],
                    [fixtures.PATH + "?per_page=100&page=1"],
                )

    def test_valid_signed63_endpoints_keep_independent_comments_read(self):
        for identifier in (1, 1201, 2**63 - 1):
            with self.subTest(identifier=identifier):
                result, calls, observed = self.exercise(identifier)
                self.assertEqual(result, (200, observed))
                self.assertEqual(
                    [path for _, path, _ in calls],
                    [
                        fixtures.PATH + "?per_page=100&page=1",
                        fixtures.PATH
                        + "/"
                        + str(identifier)
                        + "/comments?per_page=100&page=1",
                    ],
                )

    def test_signed63_subclass_is_not_coerced_to_native_id(self):
        class Number(int):
            pass

        result, calls, _ = self.exercise(Number(1201))
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
