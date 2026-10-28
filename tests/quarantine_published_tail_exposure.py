"""Opt-in allocation-frontier proof against installed released reply entry.

Only external HTTP is synthetic. Run separately against the exact old installed
packages as well as the successor. An unknown, unmaterialized allocation must
unwind before another allocator; stored outcomes retain their request uncertainty.
"""

import json
import tempfile
import unittest
from urllib.parse import urlsplit

from review_sensei_github.session_ledger import parse_session_comment
from test_action_reply_entry import ReplyEntryFixture
from test_review_run import EntryFixture


class AllocationFrontierMixin:
    def __init__(self, directory, *, fault_index=None, stored=False):
        super().__init__(directory)
        self.configure_fault(fault_index=fault_index, stored=stored)

    def configure_fault(
        self,
        *,
        fault_index=None,
        stored=False,
        foreign_archive=False,
        second_fault_index=None,
    ):
        self.allocation_attempts = []
        self.mutation_attempts = []
        self.fault_index, self.stored = fault_index, stored
        self.foreign_archive, self.second_fault_index = (
            foreign_archive,
            second_fault_index,
        )
        self.delayed = []

    def external(self, request, timeout):
        path, method = urlsplit(request.full_url).path, request.get_method()
        body = json.loads(request.data) if request.data else {}
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            self.mutation_attempts.append((method, path))
        if method == "POST" and path.endswith("/issues/7/comments"):
            text = body.get("body", "")
            self.allocation_attempts.append(text)
            if len(self.allocation_attempts) in {
                self.fault_index,
                self.second_fault_index,
            }:
                if self.stored:
                    super().external(request, timeout)
                else:
                    self.trace.append((self.attempt, method, path))
                    self.delayed.append(text)
                    if (
                        self.foreign_archive
                        and len(self.allocation_attempts) == self.fault_index
                    ):
                        # Another execution's identical predecessor-bound archive
                        # arrives during reconciliation. This caller's POST is
                        # still delayed: an identical body is not its ACK.
                        self.extra_comments[7_900_000] = {
                            "id": 7_900_000,
                            "body": text,
                            "user": {
                                "id": 999,
                                "login": "reviewsensei[bot]",
                                "type": "Bot",
                            },
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        }
                raise TimeoutError(
                    "synthetic unknown allocation; original request not retried"
                )
        return super().external(request, timeout)


class AllocationFrontierFixture(AllocationFrontierMixin, ReplyEntryFixture):
    pass


class ReviewAllocationFixture(AllocationFrontierMixin, EntryFixture):
    pass


class PublishedTailAllocationFrontierQualification(unittest.TestCase):
    def test_foreign_archive_match_can_leave_two_allocations_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.human_fixture(
                directory, fault_index=1, foreign_archive=True, second_fault_index=2
            )
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(len(fixture.delayed), 2)
            self.assertEqual(len(fixture.allocation_attempts), 2)
            self.assertTrue(
                fixture.delayed[0].startswith("ReviewSensei immutable evidence part")
            )

    def test_successful_full_review_can_retain_its_own_ambiguous_startup_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            prior = EntryFixture(directory)
            self.assertEqual(prior.invoke(human=True), 0, prior.error)
            fixture = ReviewAllocationFixture.__new__(ReviewAllocationFixture)
            fixture.__dict__.update(prior.__dict__)
            fixture.next_run()
            fixture.configure_fault(fault_index=1, foreign_archive=True)
            self.assertEqual(fixture.invoke(human=True), 0, fixture.error)
            record = parse_session_comment(
                fixture.comment["body"], identity=fixture.selected.identity
            )
            self.assertEqual(record.transaction.phase, "publication_succeeded")
            self.assertEqual(len(fixture.delayed), 1)
            self.assertGreater(len(fixture.allocation_attempts), 1)

    def human_fixture(self, directory, **fault):
        from test_api_work_persistence import HEAD, IDENTITY
        from test_review_run_rescan import partial_fixture

        prior, _ = partial_fixture(directory)
        original = parse_session_comment(prior.comment["body"], identity=IDENTITY)
        fixture = AllocationFrontierFixture.__new__(AllocationFrontierFixture)
        fixture.__dict__.update(prior.__dict__)
        fixture.next_run()
        fixture.live_head = fixture.requested_head = HEAD
        fixture.prepare_reply()
        fixture.configure_fault(**fault)
        fixture.thread_rows = [
            {
                "id": "native-thread-" + str(c["id"]),
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": c["id"],
                            "body": c["body"],
                            "path": c["path"],
                            "line": c["line"],
                            "author": {"login": "reviewsensei", "__typename": "Bot"},
                        }
                    ]
                },
            }
            for c in fixture.inline
        ]
        fixture.source["body"] = (
            "@reviewsensei The caller contract is intentional; the added change is checked."
        )
        fixture.reply_document = {
            "body": "The explanation is supported by the current diff.",
            "assessments": [
                {
                    "fingerprint": d["fingerprint"],
                    "decision": "dismissed",
                    "rationale": "The current diff contains the intended added change.",
                    "human_evidence": "The caller contract is intentional",
                    "diff_evidence": "@@ -1 +1,2 @@\n keep\n+change",
                }
                for d in original.dispositions
            ],
        }
        return fixture

    def test_human_reassessment_unknown_allocation_frontier_is_also_one(self):
        self.reply_matrix(self.human_fixture)

    def test_successful_publication_excludes_an_unmaterialized_carrier_frontier(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = ReviewAllocationFixture(directory)
            self.assertEqual(baseline.invoke(human=True), 0, baseline.error)
            count = len(baseline.allocation_attempts)
            self.assertGreaterEqual(count, 3)
        for index in range(1, count + 1):
            for stored in (False, True):
                with (
                    self.subTest(index=index, stored=stored),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = ReviewAllocationFixture(
                        directory, fault_index=index, stored=stored
                    )
                    fixture.invoke(human=True)
                    record = (
                        parse_session_comment(
                            fixture.comment["body"], identity=fixture.selected.identity
                        )
                        if fixture.comment
                        else None
                    )
                    if fixture.delayed:
                        self.assertTrue(
                            record is None
                            or record.transaction is None
                            or record.transaction.phase != "publication_succeeded"
                        )
                        self.assertEqual(len(fixture.allocation_attempts), index)
                    if (
                        record is not None
                        and record.transaction is not None
                        and record.transaction.phase == "publication_succeeded"
                    ):
                        self.assertFalse(fixture.delayed)

    def test_unknown_allocation_cannot_reach_another_allocating_stage(self):
        self.reply_matrix(AllocationFrontierFixture)

    def reply_matrix(self, factory):
        with tempfile.TemporaryDirectory() as directory:
            baseline = factory(directory)
            self.assertEqual(baseline.invoke_reply(), 0, baseline.error)
            count = len(baseline.allocation_attempts)
            self.assertGreaterEqual(count, 3)
        for index in range(1, count + 1):
            for stored in (False, True):
                with (
                    self.subTest(index=index, stored=stored),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = factory(directory, fault_index=index, stored=stored)
                    result = fixture.invoke_reply()
                    if not stored:
                        self.assertNotEqual(result, 0)
                        self.assertEqual(len(fixture.delayed), 1)
                        self.assertEqual(
                            len(fixture.allocation_attempts),
                            index,
                            fixture.mutation_attempts,
                        )
                    # No route may retry the same unconfirmed allocation. Root
                    # stored-response reconciliation may progress using its exact
                    # positively visible object, not another root POST.
                    failed_body = fixture.allocation_attempts[index - 1]
                    self.assertEqual(fixture.allocation_attempts.count(failed_body), 1)
                    self.assertLessEqual(len(fixture.delayed), 1)


if __name__ == "__main__":
    unittest.main()
