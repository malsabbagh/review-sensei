"""Fresh, historical and quarantine decisions remain independent approval gates."""

import itertools
import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei_github.approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
)
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from test_github_approval import clean_result
from test_quarantine_assessments import origins_value
from test_quarantine_lineage import decision

from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import assessment_view


def carried_views(fresh, historical, quarantine):
    hold = {
        "scope_sha256": "1" * 64,
        "state_sha256": "2" * 64,
        "pending": ["3" * 64] if historical else [],
        "fresh_human_pending": fresh,
        "review_id": 123,
        "historical_head_sha": "4" * 40,
    }
    origins, inventories = origins_value()
    state = {
        "schema_version": "quarantine-assessment-state-v1",
        "scope_sha256": "0" * 64,
        "settlements": []
        if quarantine
        else [decision(origin["scope_sha256"]) for origin in origins["origins"]],
    }
    view = assessment_view(
        origins, inventories, state, scope="0" * 64, fresh_human_pending=fresh
    )
    return hold, view


class CarriedAssessmentCompositionTests(unittest.TestCase):
    def test_each_domain_independently_blocks_and_round_trips(self):
        base = approval_eligibility_from_result(
            enabled=True, app_authored=False, result=clean_result(), head_sha="a" * 40
        )
        for fresh, historical, quarantine in itertools.product((False, True), repeat=3):
            with self.subTest(
                fresh=fresh, historical=historical, quarantine=quarantine
            ):
                hold, view = carried_views(fresh, historical, quarantine)
                eligibility = replace(
                    base,
                    historical_assessments=hold,
                    quarantine_assessments=view,
                    facts=replace(
                        base.facts,
                        has_human_adjudication_findings=any(
                            (fresh, historical, quarantine)
                        ),
                    ),
                )
                wire = eligibility.to_dict()
                self.assertEqual(wire["schema_version"], "7")
                self.assertEqual(wire["current"]["schema_version"], "6")
                self.assertEqual(ReviewApprovalEligibility.from_dict(wire), eligibility)
                self.assertEqual(eligibility.fresh_human_pending, fresh)
                self.assertEqual(
                    eligibility.evaluate(
                        app_authored=False, has_open_review_threads=False
                    ).approved,
                    not any((fresh, historical, quarantine)),
                )
                forged = deepcopy(wire)
                forged["quarantine_assessments"]["fresh_human_pending"] = not fresh
                with self.assertRaises(ReviewInputError):
                    ReviewApprovalEligibility.from_dict(forged)

    def test_native_union_authenticates_both_nested_hold_views(self):
        from test_native_union import NativeUnionTests

        backend, _, _, receipt, base = NativeUnionTests().fixture()
        hold, view = carried_views(False, True, True)
        eligibility = replace(
            base,
            historical_assessments=hold,
            quarantine_assessments=view,
            native_union=receipt,
            facts=replace(base.facts, has_human_adjudication_findings=True),
        )
        body = approval_eligibility_marker(eligibility)
        wire = eligibility.to_dict()
        self.assertEqual(wire["schema_version"], "8")
        self.assertEqual(wire["current"]["schema_version"], "7")
        self.assertEqual(wire["current"]["current"]["schema_version"], "6")
        marker_start = backend.main_body.index("<!-- reviewsensei:eligibility:v1")
        backend.main_body = backend.main_body[:marker_start] + body
        evidence = backend.read()
        self.assertIsNone(approval_eligibility_from_body(body))
        loaded = approval_eligibility_from_body(
            body, native_union_reader=lambda union: evidence
        )
        self.assertEqual(loaded, eligibility)
        forged = replace(eligibility, historical_assessments={**hold, "pending": []})
        self.assertIsNone(
            approval_eligibility_from_body(
                approval_eligibility_marker(forged),
                native_union_reader=lambda union: evidence,
            )
        )
