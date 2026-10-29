"""A final union requires complete native read evidence; flags stay independent."""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.approval import ApprovalFacts, ReviewApprovalEligibility
from review_sensei_github.native_union import NativeUnionReceipt
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from test_native_review_census import NativeCensusBackend

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.publication_staging import PublicationStaging


class NativeUnionTests(unittest.TestCase):
    def test_union_frame_reserve_covers_maximum_union_at_each_base64_alignment(self):
        import base64

        from review_sensei_github.native_union import (
            MAX_UNION_BYTES,
            NATIVE_UNION_FRAME_RESERVE,
        )

        # An opaque canonical JSON string reaches the declared union ceiling.
        # Vary the old frame length through all three encoding alignments.
        union = "u" * (MAX_UNION_BYTES - 2)
        self.assertEqual(len(canonical_bytes(union)), MAX_UNION_BYTES)
        for length in range(6):
            current = {"schema_version": "7", "text": "深🙂" * length}
            old = canonical_bytes(current)
            framed = canonical_bytes(
                {"schema_version": "8", "current": current, "native_union": union}
            )
            growth = len(base64.urlsafe_b64encode(framed)) - len(
                base64.urlsafe_b64encode(old)
            )
            self.assertLessEqual(growth, NATIVE_UNION_FRAME_RESERVE)

    def test_fresh_consumer_reconstructs_exact_plan_from_original_admission(self):
        from review_sensei_github.publication import FindingIndex, review_result_digest
        from test_native_group_publication import GroupWriterBackend

        backend = GroupWriterBackend()
        state = backend.host.publish_parts()
        receipt = NativeUnionReceipt.from_staging(backend.plan, state, producer_id=999)
        index = FindingIndex(
            review_result_digest(backend.result),
            backend.plan.base_sha,
            backend.assembled.index_entries,
        )
        self.assertEqual(receipt.reconstruct_plan(backend.result, index), backend.plan)
        with self.assertRaises(ReviewInputError):
            receipt.reconstruct_plan(replace(backend.result, summary="Changed"), index)
        altered = replace(
            index,
            entries=(replace(index.entries[0], blocking=False), *index.entries[1:]),
        )
        with self.assertRaises(ReviewInputError):
            receipt.reconstruct_plan(backend.result, altered)

    def test_cold_plan_checks_body_explanations_against_original_admission(self):
        from review_sensei_github.native_review_groups import NativeReviewPlan
        from review_sensei_github.publication import FindingIndex, review_result_digest
        from test_native_group_publication import GroupWriterBackend

        from review_sensei.presentation import (
            assign_finding_identifiers,
            build_finding_view,
            render_finding,
        )

        backend = GroupWriterBackend()
        state = backend.host.publish_parts()
        original = NativeUnionReceipt.from_staging(backend.plan, state, producer_id=999)
        entries = backend.assembled.index_entries
        body_entry = replace(entries[0], body_placed=True)
        index = FindingIndex(
            review_result_digest(backend.result),
            backend.plan.base_sha,
            (body_entry, *entries[1:]),
        )
        plan = NativeReviewPlan.prepare(
            repository_id=backend.plan.repository_id,
            pull_request=backend.plan.pull_request,
            head_sha=backend.plan.head_sha,
            base_sha=backend.plan.base_sha,
            transaction_id=backend.plan.transaction_id,
            result_sha256=backend.plan.result_sha256,
            items=backend.plan.items[1:],
        )
        receipt = replace(
            original,
            plan_sha256=plan.sha256,
            inline_count=1,
            parts=(replace(original.parts[0], finding_count=1),),
        )
        comment = backend.result.comments[0]
        identifiers = assign_finding_identifiers([entry.instance for entry in entries])
        view = replace(
            build_finding_view(
                comment,
                fingerprint=body_entry.lifecycle,
                identifier=identifiers[body_entry.instance],
                placement="body",
                advisory=False,
                literal_malformed_fences=True,
            ),
            repeat=True,
        )
        complete = render_finding(view)
        self.assertEqual(
            receipt.reconstruct_plan(backend.result, index, main_body=complete), plan
        )
        for body in ("Missing explanation", complete.replace(comment.body, "Edited")):
            with self.subTest(body=body), self.assertRaises(ReviewInputError):
                receipt.reconstruct_plan(backend.result, index, main_body=body)

    def fixture(self):
        from review_sensei_github.native_review_groups import (
            verify_complete_group_response,
        )

        backend = NativeCensusBackend()
        plan = backend.plan
        state = PublicationStaging.prepare(
            transaction_id=plan.transaction_id,
            result_sha256=plan.result_sha256,
            plan_sha256=plan.sha256,
            part_count=len(plan.groups),
        )
        groups = tuple(
            verify_complete_group_response(
                response,
                plan=plan,
                ordinal=ordinal,
                expected_review_id=int(backend.reviews[ordinal]["n"]),
                producer_id=999,
            )
            for ordinal, response in enumerate(backend.group_payloads)
        )
        for ordinal, group in enumerate(groups):
            state = state.attempt_part(ordinal).confirm_part(
                ordinal,
                native_id=str(group.review_id),
                content_sha256=group.receipt_digest,
            )
        receipt = NativeUnionReceipt.from_staging(plan, state, producer_id=999)
        current = ReviewApprovalEligibility(
            head_sha=plan.head_sha,
            result_digest=receipt.result_digest,
            facts=ApprovalFacts(
                enabled=True,
                app_authored=False,
                review_status="complete",
                evidence_policy="legacy",
                has_blocking_findings=True,
                has_human_adjudication_findings=False,
                coverage_blocker=None,
            ),
        )
        backend.main_body += "\n\n" + approval_eligibility_marker(
            replace(current, native_union=receipt)
        )
        evidence = backend.read()
        return backend, evidence, state, receipt, current

    def test_unaware_and_incomplete_readers_withhold_but_exact_reader_keeps_facts(self):
        _, evidence, _, receipt, current = self.fixture()
        value = replace(current, native_union=receipt)
        document = value.to_dict()
        self.assertEqual(document["schema_version"], "8")
        self.assertEqual(document["current"], current.to_dict())
        self.assertLessEqual(len(canonical_bytes(receipt.to_document())), 4096)
        body = approval_eligibility_marker(value)
        self.assertIsNone(approval_eligibility_from_body(body))
        self.assertIsNone(
            approval_eligibility_from_body(body, native_union_reader=lambda union: True)
        )
        loaded = approval_eligibility_from_body(
            body, native_union_reader=lambda union: evidence
        )
        self.assertEqual(loaded, value)
        self.assertTrue(loaded.facts.has_blocking_findings)
        changed = replace(
            value, facts=replace(value.facts, has_blocking_findings=False)
        )
        self.assertIsNone(
            approval_eligibility_from_body(
                approval_eligibility_marker(changed),
                native_union_reader=lambda union: evidence,
            )
        )
        self.assertIsNone(
            approval_eligibility_from_body(
                body,
                native_union_reader=lambda union: replace(
                    evidence, context_sha256="f" * 64
                ),
            )
        )

    def test_scope_counts_native_hashes_and_unknown_final_parts_are_not_reinterpreted(
        self,
    ):
        backend, evidence, state, receipt, _ = self.fixture()
        changes = [
            lambda value: value.update(head_sha="e" * 40),
            lambda value: value.update(inline_count=249),
            lambda value: value["parts"][0].update(content_sha256="f" * 64),
            lambda value: value["parts"][0].update(native_id="42"),
            lambda value: value["parts"].pop(),
            lambda value: value.update(extra=True),
        ]
        for change in changes:
            document = copy.deepcopy(receipt.to_document())
            change(document)
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                NativeUnionReceipt.from_document(document).require_evidence(evidence)
        partial = PublicationStaging.prepare(
            transaction_id=state.transaction_id,
            result_sha256=state.result_sha256,
            plan_sha256=state.plan_sha256,
            part_count=state.part_count,
        ).attempt_part(0)
        with self.assertRaises(ReviewInputError):
            NativeUnionReceipt.from_staging(backend.plan, partial, producer_id=999)

    def test_legacy_document_does_not_need_or_acquire_a_union(self):
        _, _, _, _, current = self.fixture()
        self.assertEqual(
            ReviewApprovalEligibility.from_dict(current.to_dict()), current
        )
        self.assertNotEqual(current.to_dict()["schema_version"], "8")
        self.assertIsNone(current.native_union)
