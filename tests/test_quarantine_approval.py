"""Held original assessments survive empty reviews and equal fresh instances."""

import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei_github.approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
)
from test_github_approval import clean_result
from test_quarantine_assessments import origins_value
from test_quarantine_lineage import decision

from review_sensei.errors import ReviewInputError


class QuarantineApprovalTests(unittest.TestCase):
    def test_empty_fresh_result_cannot_clear_either_original_hold(self):
        from review_sensei.quarantine_assessments import assessment_view

        origins, inventories = origins_value()
        state = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [],
        }
        eligibility = approval_eligibility_from_result(
            enabled=True, app_authored=False, result=clean_result(), head_sha="a" * 40
        )
        view = assessment_view(
            origins, inventories, state, scope="0" * 64, fresh_human_pending=False
        )
        held = replace(
            eligibility,
            quarantine_assessments=view,
            facts=replace(eligibility.facts, has_human_adjudication_findings=True),
        )
        self.assertFalse(
            held.evaluate(app_authored=False, has_open_review_threads=False).approved
        )
        wire = held.to_dict()
        self.assertEqual(wire["schema_version"], "7")
        self.assertEqual(ReviewApprovalEligibility.from_dict(wire), held)
        scopes = [o["scope_sha256"] for o in origins["origins"]]
        state["settlements"].append(decision(scopes[0]))
        one = assessment_view(
            origins, inventories, state, scope="0" * 64, fresh_human_pending=False
        )
        self.assertEqual(len(one["pending"]), 1)
        state["settlements"].append(decision(scopes[1]))
        clear = assessment_view(
            origins, inventories, state, scope="0" * 64, fresh_human_pending=False
        )
        all_settled = replace(eligibility, quarantine_assessments=clear)
        self.assertTrue(
            all_settled.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved
        )
        self.assertEqual(
            ReviewApprovalEligibility.from_dict(all_settled.to_dict()), all_settled
        )
        fresh = replace(
            all_settled,
            quarantine_assessments={**clear, "fresh_human_pending": True},
            facts=replace(all_settled.facts, has_human_adjudication_findings=True),
        )
        self.assertFalse(
            fresh.evaluate(app_authored=False, has_open_review_threads=False).approved
        )
        self.assertEqual(ReviewApprovalEligibility.from_dict(fresh.to_dict()), fresh)

    def test_forged_incomplete_duplicate_and_nested_views_refuse(self):
        from review_sensei.quarantine_assessments import assessment_view, validate_view

        origins, inventories = origins_value()
        state = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [],
        }
        view = assessment_view(
            origins, inventories, state, scope="0" * 64, fresh_human_pending=False
        )
        for edit in (
            {"fresh_human_pending": 0},
            {"pending": view["pending"] * 2},
            {"origins": []},
            {"state_sha256": "bad"},
        ):
            with self.subTest(edit=edit), self.assertRaises(ReviewInputError):
                validate_view({**view, **edit})
        eligibility = approval_eligibility_from_result(
            enabled=True, app_authored=False, result=clean_result(), head_sha="a" * 40
        )
        wire = replace(
            eligibility,
            quarantine_assessments=view,
            facts=replace(eligibility.facts, has_human_adjudication_findings=True),
        ).to_dict()
        bad = deepcopy(wire)
        bad["current"] = wire
        with self.assertRaises(ReviewInputError):
            ReviewApprovalEligibility.from_dict(bad)
        bad = deepcopy(wire)
        bad["current"]["facts"]["has_human_adjudication_findings"] = True
        with self.assertRaises(ReviewInputError):
            ReviewApprovalEligibility.from_dict(bad)
