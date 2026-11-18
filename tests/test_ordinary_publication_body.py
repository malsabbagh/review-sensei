"""Actual normalized FILE-side findings must retain body lifecycle criteria."""

import unittest

from tests import test_ordinary_publication_populated as populated


class OrdinaryBodyPublicationTests(unittest.TestCase):
    def test_body_placed_two_criteria_all_losses_keep_one_owned_root(self):
        case = populated.OrdinaryPopulatedPublicationTests()
        case.body_only = case.lose_root = case.lose_checks = True
        case.test_two_original_criteria_one_active_slot_are_wholly_prepaid()

    def test_changed_duplicate_and_foreign_root_never_adopt_body_slot(self):
        from copy import deepcopy
        from unittest.mock import patch
        from urllib.parse import urlsplit

        from review_sensei_github.errors import GitHubPublicationError
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting
        from review_sensei.errors import ReviewInputError
        from tests.fake_github_http import json_response
        from tests.test_api_work_persistence import IDENTITY

        original_fixture = populated.CurrentHostedFixture
        for change in ("edited", "foreign", "duplicate"):
            with self.subTest(change=change):
                seen = []

                def fixture_factory(*args, **kwargs):
                    fixture = original_fixture(*args, **kwargs)
                    opened = fixture.caller._opener

                    def altered(request, timeout):
                        response = opened(request, timeout)
                        path = urlsplit(request.full_url).path
                        if (
                            request.get_method() == "GET"
                            and path.endswith("/pulls/7/reviews")
                            and fixture.service.reviews
                        ):
                            rows = deepcopy(fixture.service.reviews)
                            if change == "edited":
                                rows[0]["body"] += "\nHuman edit after publication."
                            elif change == "foreign":
                                rows[0]["user"]["id"] += 1
                            else:
                                rows.append(deepcopy(rows[0]))
                            seen.append(True)
                            return json_response(rows)
                        return response

                    fixture.caller._opener = altered
                    return fixture

                case = populated.OrdinaryPopulatedPublicationTests()
                case.body_only = case.lose_root = True
                # The positive helper deliberately requires recovered success.
                # An actual bounded refusal must fail that oracle, while the
                # durable predicates below establish the negative outcome.
                with (
                    patch.object(populated, "CurrentHostedFixture", fixture_factory),
                    self.assertRaises(
                        (ReviewInputError, GitHubPublicationError, AssertionError)
                    ),
                ):
                    case.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
                service = case.fixture.service
                self.assertTrue(seen)
                self.assertEqual(len(service.reviews), 1)
                self.assertEqual(service.inline, [])
                self.assertFalse(
                    any(review["state"] == "APPROVED" for review in service.reviews)
                )
                record = parse_session_comment(
                    service.comment["body"], identity=IDENTITY
                )
                self.assertIsNone(
                    ApiRunAccounting.from_document(
                        record.api_run_accounting
                    ).terminal_transaction
                )
                unknown = [
                    item
                    for item in service.accounted.observations
                    if item.get("mutation") and item["outcome"] == "unknown"
                ]
                self.assertTrue(unknown)
                self.assertTrue(
                    all(
                        item["request_id"]
                        not in service.accounted._reconciled_mutations
                        for item in unknown
                    )
                )

    def test_missing_body_context_keeps_slot_uncertain(self):
        import sys

        from review_sensei_github.finding_lifecycle import (
            FindingLifecycleJournal,
            restore_finding_lifecycle,
        )

        from review_sensei.finding_lifecycle import decode_lifecycle_state

        changed, restored = [], []

        def observe(frame, event, arg):
            if (
                event == "call"
                and frame.f_code is FindingLifecycleJournal._readback.__code__
            ):
                effect = frame.f_locals["effect"]
                if effect["method"] == "POST" and effect["path"].endswith("/reviews"):
                    journal = frame.f_locals["self"]
                    context = getattr(journal.reviewer, "_native_finding_context", None)
                    if isinstance(context, dict):
                        context["assembled"] = None
                        changed.append(True)
            if (
                event == "return"
                and frame.f_code is restore_finding_lifecycle.__code__
                and isinstance(arg, tuple)
                and arg[1] is not None
            ):
                restored.append(arg[1])

        case = populated.OrdinaryPopulatedPublicationTests()
        case.body_only = case.lose_root = True
        previous = sys.getprofile()
        try:
            sys.setprofile(observe)
            with self.assertRaisesRegex(AssertionError, "uncertain.*active"):
                case.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
        finally:
            sys.setprofile(previous)
        self.assertTrue(changed)
        self.assertTrue(restored)
        state = decode_lifecycle_state(restored[-1]["lifecycle"]).state
        self.assertEqual(len(state.authoritative_decision_inventory.obligations), 2)
        self.assertEqual(state.display_active_findings.slots[0].state, "uncertain")
        self.assertEqual(restored[-1]["native_receipts"], [])
        self.assertEqual(len(case.fixture.service.reviews), 1)
        self.assertEqual(case.fixture.service.inline, [])

    def test_source_derived_body_one_short_refuses_before_all_native_writes(self):
        case = populated.OrdinaryPopulatedPublicationTests()
        case.body_only = case.lose_root = True
        case.test_source_derived_one_short_holds_populated_tail_before_any_native_write()
