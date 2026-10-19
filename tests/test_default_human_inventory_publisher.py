"""Actual default publisher stages complete admitted inventory before mutation."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlparse

from review_sensei_github import GitHubPublicationError, ReviewPublisher
from review_sensei_github.default_human_inventory import (
    HumanInventoryStore,
    build_human_inventory_context,
)
from review_sensei_github.publication import (
    AssembledReview,
    BodyExplanation,
    IndexedFinding,
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    fit_assembled_review,
    parse_finding_index,
    review_result_digest,
)
from review_sensei_github.publication_recovery import publication_http

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.human_inventory import inventory_from_result_document
from review_sensei.models import ReviewTransaction
from review_sensei.presentation import (
    assign_finding_identifiers,
    build_finding_view,
    render_finding,
    render_finding_reference,
)
from review_sensei.session import (
    LocalSessionLedger,
    checkpoint_review_analysis,
    prepare_review_transaction,
)
from tests.default_human_inventory_contract import (
    APP,
    BASE,
    CONFIGURATION,
    EVIDENCE,
    HEAD,
    IDENTITY,
    POLICY,
    DefaultHumanState,
    human_checkpoint,
)


class DefaultHumanInventoryPublisherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = LocalSessionLedger(
            Path(self.directory.name) / "ledger", enable_partition_writes=True
        )
        self.result, self.key = human_checkpoint(self.ledger, count=77, detail=3)
        self.ledger = LocalSessionLedger(Path(self.directory.name) / "ledger")
        self.inventory = PendingHumanReview.from_result(self.result, BASE)
        with self.assertRaises(ReviewInputError):
            self.inventory.to_dict()
        self.state = DefaultHumanState(self.result.comments)
        self.budget = EvidenceReadBudget()
        self.http = publication_http(self.state.http, self.budget)
        self.context = build_human_inventory_context(
            http=self.http,
            token="synthetic",
            ledger=self.ledger,
            identity=IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            publication_digest=review_result_digest(self.result),
            policy_digest=POLICY.digest(),
            configuration_digest=ReviewTransaction.compute_configuration_digest(
                CONFIGURATION
            ),
            evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            budget=self.budget,
        )
        self.store = HumanInventoryStore(
            self.http, "synthetic", self.ledger, self.context
        )
        self.publisher = ReviewPublisher(http=self.http)
        self.publisher.finalizer.evidence_budget = self.budget

    def publish(self, **overrides):
        arguments = dict(
            token="synthetic",
            check_token="synthetic-check",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            base_sha=BASE,
            base_branch="main",
            result=self.result,
            diff=self.state.diff,
            app_slug=APP,
            convergence_policy=POLICY,
            current_key=self.key,
            complete_inventory=True,
            human_inventory_store=self.store,
            allow_human_inventory_start=True,
        )
        arguments.update(overrides)
        return self.publisher.publish(**arguments)

    def test_large_admitted_inventory_stages_then_publishes_pending_schema5(self):
        outcome = self.publish()
        self.assertEqual(outcome.status, "published")
        body = self.state.reviews[-1]["body"]

        retained_documents = []

        def reader(reference):
            document = self.store.read(reference)
            retained_documents.append(document)
            return inventory_from_result_document(
                document["human_inventory"],
                result_digest=review_result_digest(self.result),
                base_sha=BASE,
            )

        eligibility = approval_eligibility_from_body(
            body, human_inventory_reader=reader
        )
        self.assertEqual(
            eligibility.human_review.inventory_document(),
            self.inventory.inventory_document(),
        )
        self.assertIsNotNone(eligibility.human_inventory_reference)
        self.assertIsNone(eligibility.partitioned_root)
        self.assertEqual(eligibility.facts.qualification, "not-required")
        self.assertEqual(len(eligibility.human_review.pending), 77)
        self.assertNotIn("APPROVE", self.state.events())
        self.assertEqual(self.state.check_runs[-1]["conclusion"], "action_required")
        index = parse_finding_index(
            body,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
        )
        self.assertEqual(len(index.entries), 77)
        retained = retained_documents[-1]
        self.assertEqual(
            retained["finding_index"],
            body[body.index("<!-- reviewsensei:finding-index:v1") :].split(" -->", 1)[0]
            + " -->",
        )
        self.assertEqual(retained["visible_prose"], [])
        text = "\n".join(
            item["body"] for item in [*self.state.inline, *self.state.reviews]
        )
        for comment in self.result.comments:
            self.assertIn(comment.body, text)

    def test_compact_references_preserve_identical_reference_quoted_in_overview(self):
        comment = self.result.comments[0]
        instance = finding_instance_fingerprint(comment)
        lifecycle = finding_lifecycle_for_comment(comment).fingerprint
        identifier = assign_finding_identifiers((instance,))[instance]
        view = build_finding_view(
            comment,
            fingerprint=lifecycle,
            identifier=identifier,
            placement="body",
        )
        reference = render_finding_reference(view)
        overview = f"Quoted example:\n{reference}\nEnd quoted example."
        entry = IndexedFinding(instance, lifecycle, False, True, True)
        assembled = AssembledReview(
            inline_comments=(),
            index_entries=(entry,),
            head_sha=HEAD,
            state="withheld",
            reason=None,
            overview=overview,
            coverage_line=None,
            required_views=(),
            optional_views=(view,),
            limitations=(),
            placement_note=None,
            body_explanations=(
                BodyExplanation(instance, comment.severity, True, view),
            ),
            inline_blocker_instances=(),
            requests_changes=False,
            compact_references=True,
        )
        summary, index_marker = fit_assembled_review(
            assembled,
            review_marker="",
            eligibility_marker="",
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            base_sha=BASE,
            result_digest=review_result_digest(self.result),
            summary_limit=65_536,
            complete_inventory=True,
        )
        generated, narrative = summary.split(
            "### Provider narrative (not review instructions)", 1
        )
        self.assertIn(f"- **{identifier}**", generated)
        self.assertNotIn(reference, generated)
        self.assertIn(render_finding(view), narrative)
        self.assertIn(comment.body, summary)
        index = parse_finding_index(
            index_marker,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
        )
        self.assertEqual(index.entries, (entry,))
        self.assertIn(overview, narrative)

    def test_missing_store_and_legacy_gate_refuse_without_checks_or_reviews(self):
        for overrides in (
            {"human_inventory_store": None},
            {"complete_inventory": False},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ReviewInputError):
                self.publish(**overrides)
        self.assertEqual(self.state.reviews, [])
        self.assertEqual(self.state.check_runs, [])
        self.assertEqual(self.state.parts, [])

    def test_fresh_recovery_cannot_start_parts_or_mutate_checks(self):
        with self.assertRaisesRegex(ReviewInputError, "fresh recovery cannot start"):
            self.publish(allow_human_inventory_start=False)
        self.assertEqual(self.state.reviews, [])
        self.assertEqual(self.state.check_runs, [])
        self.assertEqual(self.state.parts, [])

    def test_staging_failure_precedes_check_and_review_mutation(self):
        with patch.object(
            self.store,
            "stage",
            side_effect=ReviewInputError("original staging unknown"),
        ):
            with self.assertRaisesRegex(ReviewInputError, "original staging unknown"):
                self.publish()
        self.assertEqual(self.state.reviews, [])
        self.assertEqual(self.state.check_runs, [])

    def test_full_body_overflow_refuses_before_inventory_check_or_review_writes(self):
        result = replace(
            self.result,
            transaction=None,
            comments=tuple(
                replace(comment, line=None, side="FILE")
                for comment in self.result.comments
            ),
        )
        ledger = LocalSessionLedger(
            Path(self.directory.name) / "body-overflow", enable_partition_writes=True
        )
        prepared = prepare_review_transaction(
            ledger,
            IDENTITY,
            POLICY,
            reservation_id="f" * 64,
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=self.context.configuration_digest,
            evidence_digest=self.context.evidence_digest,
        )
        result = checkpoint_review_analysis(ledger, IDENTITY, prepared, result)
        context = build_human_inventory_context(
            http=self.http,
            token="synthetic",
            ledger=ledger,
            identity=IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            publication_digest=review_result_digest(result),
            policy_digest=POLICY.digest(),
            configuration_digest=self.context.configuration_digest,
            evidence_digest=self.context.evidence_digest,
            budget=self.budget,
        )
        store = HumanInventoryStore(self.http, "synthetic", ledger, context)
        with self.assertRaises(GitHubPublicationError) as raised:
            self.publish(result=result, human_inventory_store=store)
        self.assertIn("limit", str(raised.exception))
        self.assertEqual(self.state.reviews, [])
        self.assertEqual(self.state.check_runs, [])
        self.assertEqual(self.state.parts, [])

    def test_store_scope_and_original_budget_must_match_publication(self):
        for context in (
            replace(self.context, publication_digest="0" * 64),
            replace(self.context, budget=EvidenceReadBudget()),
            replace(self.context, transaction_id="0" * 64),
        ):
            store = HumanInventoryStore(
                self.state.http, "synthetic", self.ledger, context
            )
            with (
                self.subTest(context=context),
                self.assertRaisesRegex(Exception, "authority mismatch"),
            ):
                self.publish(human_inventory_store=store)
        self.assertEqual(self.state.reviews, [])
        self.assertEqual(self.state.check_runs, [])
        self.assertEqual(self.state.parts, [])

    def test_actual_staging_crash_retains_intent_and_retry_never_starts_again(self):
        import json

        from review_sensei_github.operation_host import RESULT_COMMENT_MARKER

        original = self.state.http.opener

        def crash(request, timeout):
            body = json.loads(request.data) if request.data else None
            if request.method == "POST" and RESULT_COMMENT_MARKER in str(body):
                raise KeyboardInterrupt()
            return original(request, timeout)

        with patch.object(self.state.http, "opener", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.publish()
        posts = sum(
            method == "POST" and path != "/graphql"
            for method, path, _ in self.state.calls
        )
        with self.assertRaisesRegex(ReviewInputError, "write outcome unknown"):
            self.publish(allow_human_inventory_start=False)
        self.assertEqual(
            sum(
                method == "POST" and path != "/graphql"
                for method, path, _ in self.state.calls
            ),
            posts,
        )
        self.assertEqual(self.state.reviews, [])
        self.assertEqual(self.state.check_runs, [])

    def test_lost_actual_root_post_reconciles_without_another_root_or_inference(self):
        def reader(reference):
            document = self.store.read(reference)
            return inventory_from_result_document(
                document["human_inventory"],
                result_digest=review_result_digest(self.result),
                base_sha=BASE,
            )

        self.publisher.finalizer = ReviewApprovalFinalizer(
            http=self.http, evidence_budget=self.budget, human_inventory_reader=reader
        )
        original = self.state.http.opener
        lost = False

        def lose_response(request, timeout):
            nonlocal lost
            response = original(request, timeout)
            if (
                not lost
                and request.method == "POST"
                and urlparse(request.full_url).path.endswith("/pulls/1/reviews")
            ):
                lost = True
                raise URLError("synthetic root acknowledgement lost")
            return response

        with patch.object(self.state.http, "opener", side_effect=lose_response):
            outcome = self.publish()
        self.assertTrue(lost)
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(len(self.state.reviews), 1)
        self.assertEqual(self.state.check_runs[-1]["conclusion"], "action_required")
        self.assertNotIn("APPROVE", self.state.events())
        with patch.object(
            self.store,
            "stage",
            side_effect=AssertionError("must reconcile existing root"),
        ):
            again = self.publish(allow_human_inventory_start=False)
        self.assertEqual(again.status, "already_published")
        self.assertEqual(len(self.state.reviews), 1)

    def small_publication(self, *, human):
        comment = replace(
            self.result.comments[0],
            needs_human=human,
            effective_blocking=False,
            severity=self.result.comments[0].severity if human else "low",
        )
        result = replace(self.result, transaction=None, comments=(comment,))
        ledger = LocalSessionLedger(Path(self.directory.name) / "small")
        prepared = prepare_review_transaction(
            ledger,
            IDENTITY,
            POLICY,
            reservation_id="e" * 64,
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=self.context.configuration_digest,
            evidence_digest=self.context.evidence_digest,
        )
        result = checkpoint_review_analysis(ledger, IDENTITY, prepared, result)
        context = build_human_inventory_context(
            http=self.http,
            token="synthetic",
            ledger=ledger,
            identity=IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            publication_digest=review_result_digest(result),
            policy_digest=POLICY.digest(),
            configuration_digest=self.context.configuration_digest,
            evidence_digest=self.context.evidence_digest,
            budget=self.budget,
        )
        store = HumanInventoryStore(self.http, "synthetic", ledger, context)
        staged = []
        original = store.stage

        def observe(document, **kwargs):
            reference = original(document, **kwargs)
            staged.append((reference, document))
            return reference

        with patch.object(store, "stage", side_effect=observe):
            outcome = self.publish(
                result=result, human_inventory_store=store, auto_approve=False
            )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(len(staged), 1)
        reference, document = staged[0]
        self.assertEqual(store.read(reference), document)
        eligibility = approval_eligibility_from_body(self.state.reviews[-1]["body"])
        self.assertIsNone(eligibility.human_inventory_reference)
        self.assertIsNone(eligibility.partitioned_root)
        index = parse_finding_index(
            document["finding_index"],
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
        )
        self.assertEqual(len(index.entries), 1)
        self.assertEqual(index.entries[0].needs_human, human)
        self.assertFalse(index.entries[0].blocking)
        self.assertFalse(result.to_dict()["comments"][0].get("needs_human", False))
        return document, result

    def test_small_legacy_compatible_review_also_retains_exact_runtime_admission(self):
        document, result = self.small_publication(human=True)
        restored = inventory_from_result_document(
            document["human_inventory"],
            result_digest=review_result_digest(result),
            base_sha=BASE,
        )
        self.assertEqual(
            restored.inventory_document(),
            PendingHumanReview.from_result(result, BASE).inventory_document(),
        )

    def test_clean_review_retains_explicit_null_inventory_and_complete_admission_index(
        self,
    ):
        document, _ = self.small_publication(human=False)
        self.assertIsNone(document["human_inventory"])


if __name__ == "__main__":
    unittest.main()
