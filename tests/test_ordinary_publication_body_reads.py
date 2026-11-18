"""Normal body publication pays its independent exact-root adoption read."""

import unittest

from tests import test_ordinary_publication_populated as populated
from tests import test_ordinary_publication_reads as read_fixtures


class OrdinaryBodyReadTests(unittest.TestCase):
    def test_normal_body_two_criteria_has_one_owned_active_root(self):
        case = populated.OrdinaryPopulatedPublicationTests()
        case.body_only = True
        case.test_two_original_criteria_one_active_slot_are_wholly_prepaid()

    def test_body_root_role_is_exact_not_a_generic_review_read(self):
        from review_sensei_github import ordinary_publication_reads as reads

        publisher, journal, _ = (
            read_fixtures.OrdinaryPublicationReadCustodyTests().fixture()
        )
        with reads.observe_publication(publisher, journal) as observation:
            prefix = "/repos/owner/repo/pulls/1/reviews/"
            self.assertEqual(observation.role("GET", prefix + "17", None), "review")
            for method, path in (
                ("POST", prefix + "17"),
                ("GET", prefix + "0"),
                ("GET", prefix + "-1"),
                ("GET", prefix + "17/extra"),
                ("GET", prefix.replace("/pulls/1/", "/pulls/2/") + "17"),
            ):
                self.assertIsNone(observation.role(method, path, None))
        self.assertNotIn("review", dict(reads._ROLES))

    def test_optional_body_role_without_allocation_refuses_before_transport(self):
        from review_sensei_github import ordinary_publication_reads as reads

        from review_sensei.errors import ReviewInputError

        publisher, journal, http = (
            read_fixtures.OrdinaryPublicationReadCustodyTests().fixture()
        )
        attempts = []

        def no_network(request, timeout):
            attempts.append(request)
            raise AssertionError("unfunded optional role dispatched")

        # Configure the genuine transport before the original source observer
        # captures it. A foreign transport would test custody, not exhaustion.
        http.opener = no_network
        with reads.observe_publication(publisher, journal) as observation:
            # Component-level negative only: this adds no role or capability.
            reads._REMAINING[observation] = dict(reads._ROLES)
            before = dict(reads._REMAINING[observation])
            with self.assertRaisesRegex(ReviewInputError, "exceeds source tail"):
                publisher.http.request(
                    "GET", "/repos/owner/repo/pulls/1/reviews/17", token="test"
                )
            self.assertEqual(reads._REMAINING[observation], before)
        self.assertEqual(attempts, [])

    def test_normal_body_source_derived_one_short_has_zero_native_effects(self):
        case = populated.OrdinaryPopulatedPublicationTests()
        case.body_only = True
        case.test_source_derived_one_short_holds_populated_tail_before_any_native_write()

    def test_normal_body_edited_or_foreign_exact_root_stays_uncertain(self):
        from copy import deepcopy
        from unittest.mock import patch
        from urllib.parse import urlsplit

        from review_sensei_github import ordinary_publication_plan as planning

        from tests.fake_github_http import json_response

        original_admit = planning.admit_current
        publishers = []

        def admit(publisher, *args, **kwargs):
            publishers.append(publisher)
            return original_admit(publisher, *args, **kwargs)

        original_fixture = populated.CurrentHostedFixture
        for fault in ("append", "edited", "foreign", "duplicate", "missing-context"):
            seen = []

            def make(*args, **kwargs):
                fixture = original_fixture(*args, **kwargs)
                opened = fixture.caller._opener

                def changed(request, timeout):
                    response = opened(request, timeout)
                    path = urlsplit(request.full_url).path
                    if (
                        request.get_method() == "GET"
                        and fixture.service.reviews
                        and path
                        == f"/repos/owner/repo/pulls/7/reviews/{fixture.service.reviews[0]['id']}"
                    ):
                        row = deepcopy(fixture.service.reviews[0])
                        if fault == "append":
                            row["body"] += "\nHuman amendment."
                        elif fault == "edited":
                            row["body"] = "Amended content."
                        elif fault == "foreign":
                            row["user"]["id"] += 1
                        elif fault == "duplicate":
                            row = [row, deepcopy(row)]
                        else:
                            publishers[-1]._native_finding_context.pop(
                                "ordinary_body_source", None
                            )
                        seen.append(True)
                        return json_response(row)
                    return response

                fixture.caller._opener = changed
                return fixture

            case = populated.OrdinaryPopulatedPublicationTests()
            case.body_only = True
            with (
                self.subTest(fault=fault),
                patch.object(populated, "CurrentHostedFixture", make),
                patch.object(planning, "admit_current", admit),
                self.assertRaisesRegex(AssertionError, "uncertain.*active"),
            ):
                case.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
            self.assertTrue(seen)
            self.assertEqual(len(case.fixture.service.reviews), 1)
            self.assertEqual(case.fixture.service.inline, [])
            self.assertFalse(
                any(r["state"] == "APPROVED" for r in case.fixture.service.reviews)
            )
