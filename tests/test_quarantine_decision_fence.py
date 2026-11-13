"""Typed existing decision fence retains four real authenticated reads."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_action as action
from review_sensei_github.publication import approval_eligibility_from_body

from review_sensei.errors import ReviewInputError
from tests.test_quarantine_journey import JourneyFixture, registered_resource


class QuarantineDecisionFenceTests(unittest.TestCase):
    def test_actual_command_uses_four_reads_and_rejects_copied_changed_foreign_guards(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                pending = approval_eligibility_from_body(
                    fixture.reviews[-1]["body"]
                ).quarantine_assessments["pending"][0]
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + pending["instance"]
                    + ' --reason "quarantine:'
                    + pending["origin_scope_sha256"]
                    + ' Original source evidence disproves this exact historical concern."',
                    new_run=True,
                )
                issue = action.issue_decision_fence
                issued, spans, starts = [], [], []

                def observe(run, authority):
                    guard = issue(run, authority)
                    before = len(fixture.trace)
                    with patch.object(run.ledger, "business_guard", guard):
                        self.assertEqual(
                            action.decision_fence_capacity(run.ledger)[0], 4
                        )
                        self.assertIsNone(
                            action.decision_fence_capacity(copy.copy(run.ledger))
                        )
                    for wrong in (
                        copy.copy(guard),
                        action.QuarantineDecisionFence(run, authority),
                    ):
                        with patch.object(run.ledger, "business_guard", wrong):
                            self.assertIsNone(
                                action.decision_fence_capacity(run.ledger)
                            )
                        with self.assertRaisesRegex(ReviewInputError, "source changed"):
                            wrong()
                    with patch.object(run.ledger, "business_guard", lambda: None):
                        self.assertIsNone(action.decision_fence_capacity(run.ledger))
                    original_document = authority.document
                    with patch.object(
                        authority,
                        "document",
                        {**original_document, "head_sha": "f" * 40},
                    ):
                        with self.assertRaisesRegex(ReviewInputError, "source changed"):
                            guard()
                    with patch.object(
                        run.ledger._quarantine_origin_reader,
                        "current_scope",
                        copy.copy(run.ledger._quarantine_origin_reader.current_scope),
                    ):
                        with self.assertRaisesRegex(ReviewInputError, "source changed"):
                            guard()
                    self.assertEqual(len(fixture.trace), before)
                    issued.append(guard)
                    return guard

                def profile(frame, event, value):
                    if frame.f_code is action.decision_authority.__code__ and issued:
                        if event == "call":
                            starts.append(len(fixture.trace))
                        elif event == "return":
                            begin = starts.pop()
                            if value == "maintain":
                                spans.append(fixture.trace[begin:])

                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    with patch.object(action, "issue_decision_fence", observe):
                        self.assertEqual(fixture.invoke_command(), 0, fixture.error)
                finally:
                    sys.setprofile(previous)
                self.assertTrue(issued)
                self.assertTrue(spans)
                for span in spans:
                    self.assertEqual(len(span), 4, span)
                    self.assertTrue(all(method == "GET" for _, method, _ in span))
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
