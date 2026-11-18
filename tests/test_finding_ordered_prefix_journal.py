"""A journal commits only the exact prepriced pending prefix."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.ordered_native_batch import bind_ordered_batch

from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_batch as batch
from tests import test_finding_lifecycle_journal as fixtures


class OrderedPrefixJournalTests(unittest.TestCase):
    def requests(self):
        return tuple(
            (
                "POST",
                "/graphql",
                {
                    "query": "mutation { resolveReviewThread }",
                    "variables": {"input": {"threadId": target}},
                },
            )
            for target in ("T1", "T2")
        )

    def test_unbound_skipped_or_changed_content_has_no_pending_or_native_write(self):
        for fault in ("unbound", "skip", "content"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                journal = batch.NativeBatchTests().journal(root)
                requests = self.requests()
                witness = journal.preflight_native_batch(requests, native_calls=10)
                if fault != "unbound":
                    bind_ordered_batch(witness, requests)
                if fault == "content":
                    journal.reviewer._unknown_effect_holds.add((7, 1))
                request = requests[1 if fault == "skip" else 0]
                http = fixtures.FakeHTTP((200, {}))
                before = copy.deepcopy(journal.effects)
                with patch.object(journal, "checkpoint") as checkpoint:
                    with self.assertRaises(ReviewInputError):
                        journal.request(
                            http,
                            request[0],
                            request[1],
                            body=request[2],
                            token="fixture",
                        )
                    checkpoint.assert_not_called()
                self.assertEqual(journal.effects, before)
                self.assertEqual(http.calls, [])

    def test_response_mismatch_retains_pending_and_blocks_later_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            journal = batch.NativeBatchTests().journal(root)
            requests = self.requests()
            witness = journal.preflight_native_batch(requests, native_calls=10)
            bind_ordered_batch(witness, requests)
            http = fixtures.FakeHTTP(
                (
                    200,
                    {
                        "data": {
                            "resolveReviewThread": {
                                "thread": {"id": "FOREIGN", "isResolved": True}
                            }
                        }
                    },
                )
            )
            with self.assertRaises(ReviewInputError):
                journal.request(
                    http, *requests[0][:2], body=requests[0][2], token="fixture"
                )
            self.assertEqual(len(http.calls), 1)
            self.assertEqual(journal.effects[0]["status"], "pending")
            before = copy.deepcopy(journal.effects)
            witness.stop()
            with self.assertRaises(ReviewInputError):
                journal.request(
                    http, *requests[1][:2], body=requests[1][2], token="fixture"
                )
            self.assertEqual(journal.effects, before)
            self.assertEqual(len(http.calls), 1)

    def test_transferred_command_commitments_reject_changed_bytes_or_request_before_write(
        self,
    ):
        from review_sensei_github.finding_lifecycle import _digest, _request_identity

        for fault in ("content", "identity"):
            with self.subTest(fault=fault):
                journal = fixtures.journal()
                method, path, raw = self.requests()[0]
                digest = _digest(
                    {"method": method, "path": path, "request": _request_identity(raw)}
                )
                journal._command_prepriced_request_keys = frozenset({digest})
                journal._command_prepriced_pending_documents = {digest: b"changed"}
                if fault == "identity":
                    raw = copy.deepcopy(raw)
                    raw["variables"]["input"]["threadId"] = "UNPLANNED"
                http = fixtures.FakeHTTP((200, {}))
                with patch.object(journal, "checkpoint") as checkpoint:
                    with self.assertRaises(ReviewInputError):
                        journal.request(http, method, path, body=raw, token="fixture")
                    checkpoint.assert_not_called()
                self.assertEqual(http.calls, [])
                self.assertEqual(journal.effects, [])
