"""Fresh human progress preserves independently authenticated carried holds."""

import itertools
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch

from review_sensei_github.historical_assessments import HistoricalAssessmentReader
from review_sensei_github.publication import (
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from test_carried_assessment_composition import carried_views
from test_human_assessment import HEAD, Provider, State, prior, response_for

from review_sensei.errors import ReviewInputError
from review_sensei.historical_assessments import hold_view


class CarriedHumanProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from review_sensei_github.session_ledger import parse_session_comment
        from test_action_run import historical_command_case
        from test_review_run import IDENTITY

        with tempfile.TemporaryDirectory() as directory:
            fixture = historical_command_case(directory)
            cls.hold = deepcopy(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).historical_assessments
            )

    def test_application_reassessment_keeps_pending_and_settled_historical_holds(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                hold = deepcopy(self.hold)
                if not pending:
                    hold["settlements"] = [
                        dict(
                            instance=item["instance"],
                            action="dismiss",
                            actor="alice",
                            role="maintain",
                            head_sha=HEAD,
                            source_id=100 + i,
                            source_updated_at="2026-10-29T00:00:00Z",
                            source_body_sha256="d" * 64,
                            reason="The separately authenticated historical decision is retained.",
                            policy_sha256="e" * 64,
                        )
                        for i, item in enumerate(hold["assessments"])
                    ]
                original = prior()
                view = dict(hold_view(hold), fresh_human_pending=True)
                state = State(replace(original, historical_assessments=view))
                reader = HistoricalAssessmentReader(
                    ledger=None,
                    http=state.http,
                    repository="owner/repo",
                    pull_request=1,
                    token="synthetic",
                )
                # The source authentication boundary is supplied independently;
                # its normalization, attachment, requirement and all publication
                # paths remain real. This test concerns fresh progress only.
                reader.read = Mock(return_value=hold)
                provider = Provider(response_for(state.eligibility))
                with patch.object(
                    ReviewApprovalFinalizer, "historical_reader", return_value=reader
                ):
                    outcome, _ = state.application_reply(provider)
                self.assertEqual(outcome.status, "replied")
                self.assertEqual(len(provider.calls), 1)
                current = next(
                    approval_eligibility_from_body(review["body"])
                    for review in reversed(state.reviews)
                    if approval_eligibility_from_body(review["body"]) is not None
                )
                self.assertFalse(current.human_review.pending)
                self.assertFalse(current.historical_assessments["fresh_human_pending"])
                self.assertEqual(
                    current.historical_assessments["pending"], view["pending"]
                )
                self.assertEqual(current.facts.has_human_adjudication_findings, pending)
                if pending:
                    self.assertNotIn("APPROVE", state.events())
                    self.assertEqual(outcome.approval_status, "approval_withheld")
                else:
                    self.assertEqual(outcome.approval_status, "approved")

    def test_fresh_projection_keeps_both_domains_and_replay_digest(self):
        base = prior()
        resolved = replace(
            base.human_review,
            resolved=tuple(item.fingerprint for item in base.human_review.findings),
        )
        for historical, quarantine in itertools.product((False, True), repeat=2):
            with self.subTest(historical=historical, quarantine=quarantine):
                hold, view = carried_views(True, historical, quarantine)
                original = replace(
                    base, historical_assessments=hold, quarantine_assessments=view
                )
                projected = original.with_fresh_human_review(resolved)
                self.assertFalse(projected.fresh_human_pending)
                self.assertEqual(
                    projected.facts.has_human_adjudication_findings,
                    historical or quarantine,
                )
                self.assertEqual(
                    projected.historical_assessments,
                    {**hold, "fresh_human_pending": False},
                )
                self.assertEqual(
                    projected.quarantine_assessments,
                    {**view, "fresh_human_pending": False},
                )
                self.assertEqual(original.with_fresh_human_review(None), original)
                state = State(original)
                _, prepared = state.bridge()
                self.assertEqual(
                    prepared.replay_context_digest,
                    replace(prepared, eligibility=projected).replay_context_digest,
                )
                invalid = replace(
                    original,
                    historical_assessments={**hold, "fresh_human_pending": False},
                )
                with self.assertRaises(ReviewInputError):
                    invalid.with_fresh_human_review(resolved)

    def test_command_missing_carried_reader_refuses_before_writes(self):
        from test_command_capacity_recovery import CommandCapacityState

        for domain in ("historical_assessments", "quarantine_assessments"):
            with (
                self.subTest(domain=domain),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = CommandCapacityState(
                    complete=True,
                    schema="1",
                    padded=False,
                    human_count=1,
                    inline_range=(0, 1),
                )
                historical, quarantine = carried_views(True, True, True)
                held = replace(
                    state.eligibility,
                    **{
                        domain: historical
                        if domain == "historical_assessments"
                        else quarantine
                    },
                )
                body = state.reviews[0]["body"]
                state.reviews[0]["body"] = body.replace(
                    approval_eligibility_marker(state.eligibility),
                    approval_eligibility_marker(held),
                )
                before = deepcopy(state.reviews)
                with self.assertRaisesRegex(
                    ReviewInputError, "source reader is unavailable"
                ):
                    state.verify(state.record(directory))
                self.assertEqual(state.reviews, before)
                self.assertFalse(
                    any(
                        method in {"POST", "PUT", "PATCH", "DELETE"}
                        for method, _, _ in state.calls
                    )
                )
