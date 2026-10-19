"""Receipt-only metadata fences never replace authoritative prose validation."""

from __future__ import annotations

import unittest
from dataclasses import replace

from review_sensei_github.default_human_inventory import HumanInventoryReference
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.human_assessment import HumanAssessmentPublisher
from review_sensei_github.publication import ReviewApprovalFinalizer

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import HumanAssessmentService
from tests.test_default_human_consumers import reference_document
from tests.test_human_assessment import (
    APP,
    HEAD,
    HUMAN,
    Provider,
    State,
    prior,
    response_for,
)


def reference_state():
    eligibility = replace(
        prior(),
        human_inventory_reference=HumanInventoryReference.from_dict(
            reference_document()
        ),
    )
    return State(eligibility)


def load(finalizer, **extra):
    return finalizer.load_eligibility(
        token="read",
        repository="owner/repo",
        pull_request=1,
        head_sha=HEAD,
        app_slug=APP,
        **extra,
    )


class PendingReceiptMetadataFenceTests(unittest.TestCase):
    def test_explicit_metadata_mode_still_validates_root_and_defaults_to_full(self):
        state = reference_state()
        observed = []
        factories = []

        class Reader:
            def __call__(self, reference):
                return state.eligibility.human_review

            def validate_root(self, review, reviews):
                observed.append(("full", review["id"], review in reviews))

            def validate_metadata(self, review, reviews):
                observed.append(("metadata", review["id"], review in reviews))

        def factory(**scope):
            factories.append(scope)
            return Reader()

        finalizer = ReviewApprovalFinalizer(
            http=state.http,
            evidence_budget=EvidenceReadBudget(),
            human_inventory_reader_factory=factory,
        )
        self.assertEqual(
            load(finalizer, validate_visible_prose=False), state.eligibility
        )
        self.assertEqual(load(finalizer), state.eligibility)
        self.assertEqual(
            load(finalizer, validate_visible_prose=False), state.eligibility
        )
        self.assertEqual(
            observed,
            [("metadata", 20, True), ("full", 20, True), ("metadata", 20, True)],
        )
        self.assertEqual(len(factories), 1)

    def test_metadata_mode_without_metadata_validator_falls_back_to_full(self):
        state = reference_state()
        roots = []

        class Reader:
            def __call__(self, reference):
                return state.eligibility.human_review

            def validate_root(self, review, reviews):
                roots.append(review["id"])

        finalizer = ReviewApprovalFinalizer(
            http=state.http, human_inventory_reader_factory=lambda **_: Reader()
        )
        self.assertEqual(
            load(finalizer, validate_visible_prose=False), state.eligibility
        )
        self.assertEqual(roots, [20])

    def test_bad_metadata_or_malformed_latest_never_exposes_older_authority(self):
        state = reference_state()
        observed = []

        class Reader:
            def __call__(self, reference):
                observed.append("inventory")
                return state.eligibility.human_review

            def validate_root(self, review, reviews):
                raise AssertionError("explicit metadata validator must run")

            def validate_metadata(self, review, reviews):
                observed.append(review["id"])
                if review["id"] == 21:
                    raise ReviewInputError("newest root metadata conflicts")

        finalizer = ReviewApprovalFinalizer(
            http=state.http,
            human_inventory_reader_factory=lambda **_: Reader(),
            evidence_budget=EvidenceReadBudget(),
        )
        state.reviews.append({**state.reviews[0], "id": 21})
        self.assertIsNone(load(finalizer, validate_visible_prose=False))
        with self.assertRaises(GitHubPublicationError):
            load(finalizer, validate_visible_prose=False, require_valid=True)
        self.assertNotIn(20, observed)
        before = len(observed)
        state.reviews.append(
            {
                **state.reviews[0],
                "id": 22,
                "body": "<!-- reviewsensei:eligibility:v1 broken -->",
            }
        )
        self.assertIsNone(load(finalizer, validate_visible_prose=False))
        self.assertEqual(len(observed), before)
        self.assertEqual(state.events(), [])

    def test_public_assessment_uses_metadata_only_for_receipts_full_for_authority(self):
        self._public_assessment(prose_changes=False)

    def test_prose_changed_during_receipts_cannot_publish_authority_or_approval(self):
        self._public_assessment(prose_changes=True)

    def test_bad_metadata_during_receipts_raises_actionable_failure(self):
        self._public_assessment(prose_changes=False, metadata_changes=True)

    def _public_assessment(self, *, prose_changes, metadata_changes=False):
        state = reference_state()
        validations = []
        publications = []
        changed = [False]
        metadata_changed = [False]

        class Reader:
            def __call__(self, reference):
                return state.eligibility.human_review

            def validate_root(self, review, reviews):
                validations.append("full")
                if changed[0]:
                    raise ReviewInputError("full visible explanation changed")

            def validate_metadata(self, review, reviews):
                validations.append("metadata")
                if metadata_changed[0]:
                    raise ReviewInputError("independent transaction or root changed")

        def hook(current, method, path, body):
            if method == "POST" and path.endswith("/issues/1/comments"):
                changed[0] = prose_changes
                metadata_changed[0] = metadata_changes
            if method == "POST" and path.endswith("/pulls/1/reviews"):
                publications.append((body["event"], validations[-1]))

        state.hook = hook
        publisher = HumanAssessmentPublisher(
            http=state.http,
            evidence_budget=EvidenceReadBudget(),
            human_inventory_reader_factory=lambda **_: Reader(),
        )
        prepared = publisher.prepare(
            token="read",
            repository="owner/repo",
            pull_request=1,
            prepared=state.prepared(),
            app_slug=APP,
        )
        self.assertEqual(validations, ["full"])
        reply = HumanAssessmentService(Provider(response_for(state.eligibility))).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=HUMAN,
        )
        arguments = dict(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        if metadata_changes:
            with self.assertRaisesRegex(GitHubPublicationError, "root conflicts"):
                publisher.publish(**arguments)
            self.assertEqual(state.events(), [])
            self.assertGreater(state.reply_count, 0)
            return
        result = publisher.publish(**arguments)
        self.assertIn("metadata", validations)
        self.assertEqual(validations[-1], "full")
        self.assertGreater(state.reply_count, 0)
        if prose_changes:
            self.assertNotEqual(result.approval_status, "approved")
            self.assertEqual(state.events(), [])
        else:
            self.assertEqual(result.approval_status, "approved")
            self.assertEqual(publications, [("COMMENT", "full"), ("APPROVE", "full")])

    def test_legacy_read_is_unchanged_and_invalid_mode_refuses_before_io(self):
        state = State(prior())

        def forbidden(_reference):
            raise AssertionError("legacy document must not invoke inventory reader")

        finalizer = ReviewApprovalFinalizer(
            http=state.http, human_inventory_reader=forbidden
        )
        self.assertEqual(
            load(finalizer, validate_visible_prose=False), state.eligibility
        )
        before = len(state.calls)
        with self.assertRaises(GitHubPublicationError):
            load(finalizer, validate_visible_prose="false")
        self.assertEqual(len(state.calls), before)
