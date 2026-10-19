"""Default reference eligibility preserves complete inventory and ordinary policy."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei_github.approval import ApprovalFacts, ReviewApprovalEligibility

from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    HumanInventoryResolution,
    HumanReviewFinding,
    PendingHumanReview,
)

BASE = "a" * 40
HEAD = "b" * 40
RESULT = "c" * 64


def rich_inventory() -> PendingHumanReview:
    """Synthetic unique text prevents compression from hiding the legacy ceiling."""
    return PendingHumanReview(
        BASE,
        tuple(
            HumanReviewFinding(
                hashlib.sha256(f"finding:{index}".encode()).hexdigest(),
                f"src/file{index}.py",
                " ".join(
                    hashlib.sha256(f"text:{index}:{part}".encode()).hexdigest()
                    for part in range(8)
                ),
            )
            for index in range(250)
        ),
    )


def facts(*, pending: bool = True, **changes) -> ApprovalFacts:
    value = ApprovalFacts(
        enabled=True,
        app_authored=False,
        review_status="complete",
        evidence_policy="confirmed",
        has_blocking_findings=False,
        has_human_adjudication_findings=pending,
        coverage_blocker=None,
        qualification="not-required",
    )
    return replace(value, **changes)


def reference_document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "purpose": "default-human-inventory",
        "event_key": "d" * 64 + "\t" + "e" * 64,
        "payload_digest": "f" * 64,
    }


def document(inventory: PendingHumanReview) -> dict[str, object]:
    return {
        "schema_version": "5",
        "head_sha": HEAD,
        "result_digest": RESULT,
        "facts": facts(pending=bool(inventory.pending)).to_dict(),
        "human_inventory_reference": reference_document(),
        "human_resolution": HumanInventoryResolution.from_inventory(
            inventory
        ).to_dict(),
    }


class DefaultHumanEligibilityTests(unittest.TestCase):
    def test_reference_document_requires_independent_reader(self):
        with self.assertRaisesRegex(ReviewInputError, "reader"):
            ReviewApprovalEligibility.from_dict(document(rich_inventory()))

    def test_rich_inventory_roundtrip_partial_and_complete_preserve_policy(self):
        original = rich_inventory()
        with self.assertRaises(ReviewInputError):
            original.to_dict()
        seen = []

        def read(reference):
            seen.append(reference.to_dict())
            return original

        for count in (0, 1, 249, 250):
            with self.subTest(resolved=count):
                current = replace(
                    original,
                    resolved=tuple(
                        sorted(item.fingerprint for item in original.findings[:count])
                    ),
                )
                wire = document(current)
                parsed = ReviewApprovalEligibility.from_dict(
                    wire, human_inventory_reader=read
                )
                self.assertEqual(parsed.human_review, current)
                self.assertEqual(parsed.to_dict(), wire)
                marker_size = len("<!-- reviewsensei:eligibility:v1  -->") + len(
                    base64.urlsafe_b64encode(
                        json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
                    )
                )
                self.assertLess(marker_size, 65_536)
                self.assertIsNone(parsed.partitioned_root)
                self.assertEqual(parsed.facts.qualification, "not-required")
                decision = parsed.evaluate(
                    app_authored=False, has_open_review_threads=False
                )
                self.assertEqual(decision.approved, count == 250)
        self.assertEqual(seen, [reference_document()] * 4)

    def test_reference_resolution_cannot_change_or_expand_inventory(self):
        inventory = rich_inventory()
        for change in (
            {"inventory_digest": "0" * 64},
            {"resolved": ["0" * 64]},
            {"resolved": [inventory.findings[0].fingerprint] * 2},
        ):
            with self.subTest(change=change):
                wire = document(inventory)
                wire["human_resolution"].update(change)
                with self.assertRaises(ReviewInputError):
                    ReviewApprovalEligibility.from_dict(
                        wire, human_inventory_reader=lambda _ref: inventory
                    )

    def test_reference_facts_cannot_clear_pending_without_resolution(self):
        inventory = rich_inventory()
        wire = document(inventory)
        wire["facts"]["has_human_adjudication_findings"] = False
        with self.assertRaises(ReviewInputError):
            ReviewApprovalEligibility.from_dict(
                wire, human_inventory_reader=lambda _ref: inventory
            )

    def test_reference_schema_rejects_extra_missing_and_bad_identity(self):
        inventory = rich_inventory()
        original = document(inventory)
        variants = [
            {**original, "human_review": {}},
            {
                key: value
                for key, value in original.items()
                if key != "human_resolution"
            },
            {**original, "head_sha": "not-a-head"},
            {**original, "result_digest": "0" * 63},
            {**original, "human_inventory_reference": {}},
        ]
        for wire in variants:
            with self.subTest(wire=wire):
                with self.assertRaises(ReviewInputError):
                    ReviewApprovalEligibility.from_dict(
                        wire, human_inventory_reader=lambda _ref: inventory
                    )

    def test_all_resolved_does_not_clear_unrelated_policy_blockers(self):
        inventory = rich_inventory()
        inventory = replace(
            inventory, resolved=tuple(item.fingerprint for item in inventory.findings)
        )
        for changes in (
            {"has_blocking_findings": True},
            {"review_status": "partial"},
            {"coverage_blocker": "partial"},
            {"qualification": "missing"},
            {"qualification": "unverified"},
        ):
            with self.subTest(changes=changes):
                wire = deepcopy(document(inventory))
                wire["facts"].update(changes)
                parsed = ReviewApprovalEligibility.from_dict(
                    wire, human_inventory_reader=lambda _ref: inventory
                )
                self.assertFalse(
                    parsed.evaluate(
                        app_authored=False, has_open_review_threads=False
                    ).approved
                )
                self.assertEqual(parsed.to_dict()["facts"], wire["facts"])


class DefaultHumanConsumerTests(unittest.TestCase):
    def state(self, *, extra_human=True, **fact_changes):
        from review_sensei_github.default_human_inventory import HumanInventoryReference

        from tests.test_human_assessment import State, prior

        original = prior(extra_human=extra_human, **fact_changes)
        reference = HumanInventoryReference.from_dict(reference_document())
        original = replace(original, human_inventory_reference=reference)
        state = State(original)
        seen = []

        def reader(actual):
            self.assertEqual(actual, reference)
            seen.append(actual)
            return original.human_review

        return state, reader, seen

    def assessment(self, state, reader, *, target_path="src/app.py"):
        from review_sensei_github.human_assessment import HumanAssessmentPublisher

        from review_sensei.human_assessment import HumanAssessmentService
        from tests.test_human_assessment import APP, HUMAN, Provider, response_for

        publisher = HumanAssessmentPublisher(
            http=state.http, human_inventory_reader=reader
        )
        prepared = publisher.prepare(
            token="read",
            repository="owner/repo",
            pull_request=1,
            prepared=replace(
                state.prepared(), source_updated_at=state.source["updated_at"]
            ),
            app_slug=APP,
        )
        payload = response_for(state.eligibility)
        finding = next(
            item
            for item in prepared.eligibility.human_review.pending
            if item.path == target_path
        )
        payload["assessments"][0]["fingerprint"] = finding.fingerprint
        reply = HumanAssessmentService(Provider(payload)).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=HUMAN,
        )
        return publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        ), publisher

    def test_public_assessment_preserves_reference_across_partial_and_fresh_owner(self):
        from tests.test_human_assessment import APP, HEAD

        state, reader, seen = self.state()
        first, publisher = self.assessment(state, reader)
        self.assertIs(publisher.finalizer, publisher.conversation.finalizer)
        self.assertEqual(first.status, "replied")
        self.assertNotEqual(first.approval_status, "approved")
        self.assertEqual(state.events(), ["COMMENT"])
        current = publisher.finalizer.load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertEqual(len(current.human_review.pending), 1)
        self.assertIsNotNone(current.human_inventory_reference)
        self.assertEqual(current.facts.qualification, "not-required")
        before = len(seen)
        # An edited, newly snapshotted authorized mention supplies fresh source
        # authority. The new publisher has no retained in-process inventory.
        state.source["updated_at"] = "second-authorized-snapshot"
        second, fresh = self.assessment(state, reader, target_path="src/other.py")
        self.assertIsNot(fresh, publisher)
        self.assertGreater(len(seen), before)
        self.assertEqual(second.approval_status, "approved")
        self.assertEqual(state.events(), ["COMMENT", "COMMENT", "APPROVE"])
        final = fresh.finalizer.load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertFalse(final.human_review.pending)
        self.assertEqual(
            final.human_inventory_reference, current.human_inventory_reference
        )

    def test_public_current_reply_uses_reader_on_every_pending_fence(self):
        from review_sensei_github.conversation import ConversationPublisher

        from review_sensei.models import ConversationReply
        from tests.test_human_assessment import APP, HEAD

        state, reader, seen = self.state()
        publisher = ConversationPublisher(
            http=state.http, human_inventory_reader=reader
        )
        outcome = publisher.publish_current(
            token="issue",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            head_sha=HEAD,
            reply=ConversationReply(
                "Please retain the complete pending inventory.", False
            ),
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(outcome.status, "replied")
        self.assertGreater(len(seen), 1)
        self.assertEqual(state.events(), [])
        report = "\n".join(item["body"] for item in state.replies)
        for finding in state.eligibility.human_review.findings:
            self.assertIn(finding.fingerprint, report)

    def test_unavailable_reader_stops_public_prepare_before_writes(self):
        from review_sensei_github.errors import GitHubPublicationError
        from review_sensei_github.human_assessment import HumanAssessmentPublisher

        from tests.test_human_assessment import APP

        state, _, _ = self.state()

        def unavailable(_reference):
            raise ReviewInputError("complete inventory is unavailable")

        publisher = HumanAssessmentPublisher(
            http=state.http, human_inventory_reader=unavailable
        )
        with self.assertRaises(GitHubPublicationError):
            publisher.prepare(
                token="read",
                repository="owner/repo",
                pull_request=1,
                prepared=state.prepared(),
                app_slug=APP,
            )
        self.assertFalse(any(method != "GET" for method, _, _ in state.calls))
        self.assertEqual(state.events(), [])
