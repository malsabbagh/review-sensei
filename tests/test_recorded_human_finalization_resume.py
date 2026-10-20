"""Recorded retry delegates fresh authority checks to the real finalizer once."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.default_human_inventory import HumanInventoryReference
from review_sensei_github.human_assessment import HumanAssessmentPublisher

from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import HumanAssessmentReply
from tests.test_default_human_consumers import reference_document
from tests.test_human_assessment import APP, State, prior


def prepared_resume():
    original = prior()
    immutable = original.human_review
    eligibility = replace(
        original,
        human_review=replace(
            immutable, resolved=tuple(f.fingerprint for f in immutable.findings)
        ),
        facts=replace(original.facts, has_human_adjudication_findings=False),
        human_inventory_reference=HumanInventoryReference.from_dict(
            reference_document()
        ),
    )
    state = State(eligibility)
    validations = []
    damaged = [False]

    class Reader:
        def __call__(self, reference):
            return immutable

        def validate_root(self, review, reviews):
            validations.append("full")
            if damaged[0]:
                raise ReviewInputError("original complete visible prose changed")

        def validate_metadata(self, review, reviews):
            validations.append("metadata")

    publisher = HumanAssessmentPublisher(
        http=state.http, human_inventory_reader=Reader()
    )
    prepared = publisher.prepare(
        token="read",
        repository="owner/repo",
        pull_request=1,
        prepared=state.prepared(),
        app_slug=APP,
    )
    return state, publisher, prepared, validations, damaged


def resume(publisher, prepared):
    return publisher.publish(
        token="issue",
        review_token="review",
        repository="owner/repo",
        pull_request=1,
        prepared=prepared,
        reply=HumanAssessmentReply(
            "Retrying approval finalization of recorded decisions.", ()
        ),
        app_slug=APP,
        recorded_replay=True,
    )


class RecordedHumanFinalizationResumeTests(unittest.TestCase):
    def test_recorded_resume_avoids_duplicate_full_read_before_real_finalizer(self):
        state, publisher, prepared, validations, _ = prepared_resume()
        full_at_prepare = validations.count("full")
        self.assertEqual(full_at_prepare, 1)
        entry = []
        actual = publisher.finalizer.finalize_recorded_decisions

        def observe_entry(**arguments):
            entry.append(validations.count("full"))
            return actual(**arguments)

        with patch.object(
            publisher.finalizer, "finalize_recorded_decisions", observe_entry
        ):
            result = resume(publisher, prepared)
        self.assertEqual(entry, [full_at_prepare])
        self.assertGreater(validations.count("full"), full_at_prepare)
        self.assertEqual(result.approval_status, "approved")
        self.assertEqual(state.events(), ["APPROVE"])
        self.assertEqual(len(state.eligibility.human_review.pending), 0)

    def test_recorded_resume_source_edit_still_stops_before_finalizer(self):
        state, publisher, prepared, _, _ = prepared_resume()
        actual_receipt = publisher.conversation.publish_pending
        entries = []
        actual_finalize = publisher.finalizer.finalize_recorded_decisions

        def edited_after_receipt(**arguments):
            result = actual_receipt(**arguments)
            state.source["body"] += " edited after the completed receipt"
            return result

        def observe_entry(**arguments):
            entries.append(True)
            return actual_finalize(**arguments)

        with (
            patch.object(
                publisher.conversation, "publish_pending", edited_after_receipt
            ),
            patch.object(
                publisher.finalizer, "finalize_recorded_decisions", observe_entry
            ),
        ):
            result = resume(publisher, prepared)
        self.assertEqual(result.status, "skipped_edited_source")
        self.assertEqual(entries, [])
        self.assertEqual(state.events(), [])

    def test_real_finalizer_rechecks_head_base_latest_root_and_full_prose(self):
        for fault in ("head", "base", "latest-root", "prose"):
            with self.subTest(fault=fault):
                state, publisher, prepared, validations, damaged = prepared_resume()
                actual_receipt = publisher.conversation.publish_pending

                def changed_after_receipt(**arguments):
                    result = actual_receipt(**arguments)
                    if fault in {"head", "base"}:
                        setattr(state, fault, "e" * 40)
                    elif fault == "latest-root":
                        newer = replace(
                            state.eligibility,
                            human_review=replace(
                                state.eligibility.human_review, resolved=()
                            ),
                            facts=replace(
                                state.eligibility.facts,
                                has_human_adjudication_findings=True,
                            ),
                        )
                        state.reviews.append(state.review(newer, 100))
                    else:
                        damaged[0] = True
                    return result

                with patch.object(
                    publisher.conversation, "publish_pending", changed_after_receipt
                ):
                    result = resume(publisher, prepared)
                self.assertNotEqual(result.approval_status, "approved")
                self.assertEqual(state.events(), [])
                if fault in {"latest-root", "prose"}:
                    self.assertGreater(validations.count("full"), 1)
