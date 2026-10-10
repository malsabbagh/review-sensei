"""Reader-first v4 with trusted context, complete evidence and fresh COMMENT reads."""

import base64
import copy
import unittest
from dataclasses import replace

from review_sensei.bounded_evidence import (
    canonical_bytes,
    read_partitioned_evidence,
    stage_partitioned_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
)
from review_sensei.hosting.github.eligibility_parts import (
    PartitionedEligibilityContext,
    visible_result_document,
)
from review_sensei.hosting.github.errors import GitHubPublicationError
from review_sensei.hosting.github.publication import (
    APPROVAL_ELIGIBILITY_MARKER_PREFIX,
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    review_result_digest,
)
from review_sensei.hosting.github.publication_parts import prepare_finding_prose_parts
from review_sensei.human_assessment import HumanInventoryResolution
from review_sensei.human_inventory import (
    inventory_result_document,
    stage_result_human_inventory,
)
from tests.test_complete_publication_staging import (
    PRODUCER,
    Parts,
    Reviews,
    inventory_context,
    synthetic_budget,
)
from tests.test_evidence_capacity import human_result, varied_comments
from tests.test_finalizer_adaptive_reviews import ReviewPages
from tests.test_human_assessment import APP, BASE, HEAD


class Bundle:
    def __init__(self, count):
        comments = varied_comments(count, detail=3, unique_paths=count)
        result = human_result(comments)
        self.eligibility = approval_eligibility_from_result(
            result, head_sha=HEAD, base_sha=BASE, enabled=True, app_authored=False
        )
        self.inventory = self.eligibility.human_review
        self.result_digest = review_result_digest(result)
        self.evidence = Parts()
        self.reviews = Reviews()
        self.budget = synthetic_budget()
        self.store = self.reviews.store(self.budget)
        self.inventory_manifest = stage_result_human_inventory(
            self.inventory,
            result_digest=self.result_digest,
            binding=inventory_context(),
            max_manifest_bytes=8192,
            writer=self.evidence.write,
            reader=self.evidence.read,
            budget=self.budget,
        )
        self.receipts = self.store.stage(
            prepare_finding_prose_parts(comments, head_sha=HEAD), head_sha=HEAD
        )
        self.instances = tuple(
            instance for receipt in self.receipts for instance in receipt.instances
        )
        self.visible_document = visible_result_document(
            self.receipts,
            result_digest=self.result_digest,
            inventory_digest=self.inventory.inventory_digest,
            expected_instances=self.instances,
        )
        self.visible_manifest = self.stage_document(
            self.visible_document, purpose="evidence", count=count
        )
        self.context = PartitionedEligibilityContext(
            binding=inventory_context(),
            result_digest=self.result_digest,
            activation_generation=4,
            owned_root_id=10000,
            visible_instances=self.instances,
            budget=self.budget,
            max_root_bytes=32768,
            framing_bytes=500,
            future_lifecycle_bytes=1500,
        )
        self.root = dict(
            schema_version="4",
            head_sha=HEAD,
            result_digest=self.result_digest,
            facts=self.eligibility.facts.to_dict(),
            immutable_generation=inventory_context()["generation"],
            activation_generation=4,
            human_inventory=self.inventory_manifest,
            visible_prose=self.visible_manifest,
            human_resolution=HumanInventoryResolution.from_inventory(
                self.inventory
            ).to_dict(),
        )

    def stage_document(self, value, *, purpose, count):
        return stage_partitioned_evidence(
            value,
            binding=inventory_context(purpose=purpose),
            item_count=count,
            max_manifest_bytes=8192,
            writer=lambda document: self.evidence.write(
                document, self.budget.consume()
            ),
            reader=lambda key: self.evidence.read(key, self.budget.consume()),
            budget=self.budget,
        )

    def read(self, manifest, *, expected_binding, budget):
        assert budget is self.context.budget
        return read_partitioned_evidence(
            manifest,
            expected_binding=expected_binding,
            reader=lambda key: self.evidence.read(key, budget.consume()),
            budget=budget,
        )

    def visible(self, receipts, *, budget):
        assert budget is self.context.budget
        self.reviews.store(budget).revalidate(receipts)

    def parse(self, value=None, *, context=None, visible=None):
        return ReviewApprovalEligibility.from_dict(
            value if value is not None else self.root,
            partition_reader=self.read,
            expected_context=context or self.context,
            visible_reader=visible or self.visible,
        )

    def marker(self, value=None):
        return (
            APPROVAL_ELIGIBILITY_MARKER_PREFIX
            + " "
            + base64.urlsafe_b64encode(
                canonical_bytes(value if value is not None else self.root)
            ).decode()
            + " -->"
        )


class PartitionedEligibilityReaderTests(unittest.TestCase):
    def test_complete_100_250_result_bound_reader_all_resolution_and_no_approval(self):
        for count in (100, 250):
            with self.subTest(count=count):
                bundle = Bundle(count)
                parsed = bundle.parse()
                self.assertEqual(parsed.to_dict(), bundle.root)
                self.assertEqual(
                    parsed.human_review.inventory_document(),
                    bundle.inventory.inventory_document(),
                )
                resolved = replace(
                    parsed.human_review,
                    resolved=tuple(
                        item.fingerprint for item in parsed.human_review.findings
                    ),
                )
                complete = replace(
                    parsed,
                    human_review=resolved,
                    facts=replace(parsed.facts, has_human_adjudication_findings=False),
                )
                document = complete.to_dict()
                self.assertLess(len(bundle.marker(document).encode()) + 2000, 32768)
                self.assertEqual(len(document["human_resolution"]["resolved"]), count)
                self.assertLessEqual(bundle.budget.calls, 60)
                # V4 finalization is explicitly disabled even when static facts
                # and every inventory obligation would otherwise permit it.
                outcome = ReviewApprovalFinalizer(
                    http=bundle.reviews.http, evidence_budget=bundle.budget
                ).finalize(
                    token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    head_sha=HEAD,
                    app_slug=APP,
                    eligibility=complete,
                )
                self.assertEqual(outcome.status, "approval_withheld")
                self.assertFalse(
                    any(
                        payload and payload.get("event") == "APPROVE"
                        for _, _, payload in bundle.reviews.calls
                    )
                )

    def test_resolvers_and_independent_context_are_required(self):
        bundle = Bundle(1)
        for kwargs in (
            {},
            {"expected_context": bundle.context},
            {"expected_context": bundle.context, "partition_reader": bundle.read},
            {"partition_reader": bundle.read, "visible_reader": bundle.visible},
        ):
            with self.assertRaises(ReviewInputError):
                ReviewApprovalEligibility.from_dict(bundle.root, **kwargs)
        self.assertIsNone(approval_eligibility_from_body(bundle.marker()))

    def test_snapshot_result_generation_and_domain_mismatch_precede_part_reads(self):
        bundle = Bundle(1)
        before = bundle.budget.calls
        for field, value in (
            ("result_digest", "e" * 64),
            ("head_sha", "c" * 40),
            ("immutable_generation", True),
            ("activation_generation", 5),
        ):
            changed = dict(bundle.root, **{field: value})
            with self.assertRaises(ReviewInputError):
                bundle.parse(changed)
        for field, value in (
            ("producer", "github-bot:999"),
            ("head_sha", "d" * 40),
            ("purpose", "baseline"),
            ("generation", 77),
            ("configuration_digest", "a" * 64),
        ):
            changed = copy.deepcopy(bundle.root)
            changed["visible_prose"]["binding"][field] = value
            with self.assertRaises(ReviewInputError):
                bundle.parse(changed)
        with self.assertRaises(ReviewInputError):
            bundle.parse(dict(bundle.root, extra="not closed"))
        self.assertEqual(bundle.budget.calls, before)

    def test_result_envelopes_are_not_raw_inventory_or_foreign_result(self):
        for kind in ("raw", "foreign", "metadata"):
            bundle = Bundle(1)
            value = inventory_result_document(
                bundle.inventory, result_digest=bundle.result_digest
            )
            if kind == "raw":
                value = bundle.inventory.inventory_document()
            elif kind == "foreign":
                value["result_digest"] = "e" * 64
            else:
                value["extra"] = "not closed"
            manifest = bundle.stage_document(value, purpose="human-inventory", count=1)
            with self.assertRaises(ReviewInputError):
                bundle.parse(dict(bundle.root, human_inventory=manifest))

    def test_missing_incomplete_or_foreign_visible_envelopes_withhold(self):
        for kind in ("missing", "count", "result", "inventory", "placement", "head"):
            bundle = Bundle(2)
            value = copy.deepcopy(bundle.visible_document)
            if kind == "missing":
                value["parts"] = []
            elif kind == "count":
                value["finding_count"] = 1
            elif kind in {"result", "inventory"}:
                value[kind + "_digest"] = "e" * 64
            elif kind == "placement":
                value["parts"][0]["placement"] = "issue-comment"
            else:
                value["parts"][0]["head_sha"] = "e" * 40
            manifest = bundle.stage_document(value, purpose="evidence", count=2)
            with self.assertRaises(ReviewInputError):
                bundle.parse(dict(bundle.root, visible_prose=manifest))

    def test_resolution_foreign_unknown_and_fact_conflicts(self):
        bundle = Bundle(2)
        for change in ({"inventory_digest": "e" * 64}, {"resolved": ["e" * 64]}):
            value = copy.deepcopy(bundle.root)
            value["human_resolution"].update(change)
            with self.assertRaises(ReviewInputError):
                bundle.parse(value)
        value = copy.deepcopy(bundle.root)
        value["facts"]["has_human_adjudication_findings"] = False
        with self.assertRaises(ReviewInputError):
            bundle.parse(value)

    def test_actual_framed_future_allocation_refuses_before_readback(self):
        bundle = Bundle(100)
        before = bundle.budget.calls
        context = replace(bundle.context, max_root_bytes=8192)
        with self.assertRaises(ReviewInputError):
            bundle.parse(context=context)
        self.assertEqual(bundle.budget.calls, before)
        with self.assertRaises(ReviewInputError):
            replace(bundle.context, max_root_bytes=32769)

    def test_actual_fresh_visible_body_readback_is_required(self):
        bundle = Bundle(1)
        bundle.reviews.items[0]["body"] += "changed"
        with self.assertRaises(ReviewInputError):
            bundle.parse()
        bundle = Bundle(1)
        with self.assertRaises(ReviewInputError):
            bundle.parse(visible=lambda receipts, *, budget: False)

    def test_actual_finalizer_scan_and_parts_share_one_budget_and_latest_root(self):
        bundle = Bundle(100)
        owned = dict(
            id=10000,
            body=bundle.marker(),
            commit_id=HEAD,
            state="COMMENTED",
            user={"id": PRODUCER, "type": "Bot", "login": APP},
        )
        pages = ReviewPages([*bundle.reviews.items, owned])
        finalizer = ReviewApprovalFinalizer(
            http=pages.http, evidence_budget=bundle.budget
        )
        parsed = finalizer.load_eligibility(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            require_valid=True,
            partition_reader=bundle.read,
            expected_context=bundle.context,
            visible_reader=bundle.visible,
        )
        self.assertEqual(parsed.to_dict(), bundle.root)
        self.assertLessEqual(bundle.budget.calls, 60)
        before = bundle.budget.calls
        with self.assertRaises(GitHubPublicationError):
            ReviewApprovalFinalizer(http=pages.http).load_eligibility(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                expected_context=bundle.context,
                partition_reader=bundle.read,
                visible_reader=bundle.visible,
            )
        self.assertEqual(bundle.budget.calls, before)
        pages.items.append(
            dict(
                owned,
                id=10001,
                body=APPROVAL_ELIGIBILITY_MARKER_PREFIX + " malformed -->",
            )
        )
        self.assertIsNone(
            finalizer.load_eligibility(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                expected_context=bundle.context,
                partition_reader=bundle.read,
                visible_reader=bundle.visible,
            )
        )


if __name__ == "__main__":
    unittest.main()
