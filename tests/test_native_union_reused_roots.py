"""V2 union proves reused roots through the bounded production native reader."""

from __future__ import annotations

import copy
import hashlib
import unittest
from dataclasses import replace

from review_sensei_github.native_review_groups import NativeReviewPlan
from review_sensei_github.native_union import NativeUnionReceipt, NativeUnionReusedRoot
from review_sensei_github.publication import FindingIndex, review_result_digest
from test_native_group_publication import GroupWriterBackend

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.publication_staging import PublicationStaging


def _backend(*, all_reused=False):
    backend = GroupWriterBackend()
    count = len(backend.plan.items) if all_reused else 1
    reused = []
    for ordinal, (comment, _, body, _) in enumerate(
        backend.assembled.inline_comments[:count]
    ):
        identifier = 741801 + ordinal * 31
        thread_id = "PRRT_actual_opaque_root_" + str(ordinal)
        row = {
            "n": str(identifier),
            "b": body,
            "p": comment.path,
            "l": comment.line,
            "o": comment.line,
            "a": {"t": "Bot", "n": 999},
            "c": {"o": "e" * 40},
            "z": {"o": "e" * 40},
            "r": {"n": "67291"},
            "q": None,
            "u": "2026-11-12T10:00:00Z",
        }
        backend.old_rows.append(row)
        backend.old_nodes[identifier] = thread_id
        backend.roots.append(backend.root(row))
        reused.append(
            NativeUnionReusedRoot(
                finding_instance_fingerprint(comment),
                thread_id,
                identifier,
                67291,
                "e" * 40,
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )
        )
    previous = backend.plan
    backend.plan = NativeReviewPlan.prepare(
        repository_id=previous.repository_id,
        pull_request=previous.pull_request,
        base_sha=previous.base_sha,
        head_sha=previous.head_sha,
        transaction_id=previous.transaction_id,
        result_sha256=previous.result_sha256,
        items=previous.items[count:],
    )
    backend.host.plan = backend.plan
    backend.host.reused_roots = tuple(reused)
    return backend


def _publish(backend):
    return backend.host.publish_final(
        body_for_receipt=backend.body_for_receipt,
        event="COMMENT",
        old_root_nodes=backend.old_nodes,
    )


class NativeQualificationTests(unittest.TestCase):
    def test_group_writer_and_bounded_reader_prove_reused_membership(self):
        for all_reused in [False, True]:
            with self.subTest(all_reused=all_reused):
                backend = _backend(all_reused=all_reused)
                state, evidence = _publish(backend)
                receipt = NativeUnionReceipt.from_staging(
                    backend.plan,
                    state,
                    producer_id=999,
                    reused_roots=backend.host.reused_roots,
                )
                assert (
                    receipt.to_document()["schema_version"] == "native-review-union-v2"
                )
                assert receipt.inline_count == 2
                assert receipt.context() == backend.plan.context(999)
                assert len(canonical_bytes(receipt.to_document())) <= 4096
                assert evidence.physical_reads <= 10
                receipt.require_evidence(evidence)
                assert (
                    NativeUnionReceipt.from_document(receipt.to_document()) == receipt
                )
                index = FindingIndex(
                    review_result_digest(backend.result),
                    backend.plan.base_sha,
                    backend.assembled.index_entries,
                )
                assert receipt.reconstruct_plan(backend.result, index) == backend.plan
                posts = backend.post_count
                _publish(backend)
                assert backend.post_count == posts == (1 if all_reused else 2)

    def test_reused_native_evidence_mutations_fail_closed(self):
        for field in [
            "body",
            "producer",
            "review",
            "commit",
            "thread",
            "resolved",
            "missing",
            "duplicate",
        ]:
            with self.subTest(field=field):
                backend = _backend()
                state, evidence = _publish(backend)
                receipt = NativeUnionReceipt.from_staging(
                    backend.plan,
                    state,
                    producer_id=999,
                    reused_roots=backend.host.reused_roots,
                )
                reused = receipt.reused_roots[0]
                roots, rows, ids = (
                    list(evidence.roots),
                    copy.deepcopy(list(evidence.root_bodies)),
                    list(evidence.thread_ids),
                )
                ordinal = next(
                    (
                        i
                        for i, root in enumerate(roots)
                        if root.comment_id == reused.comment_id
                    )
                )
                body_index = next(
                    (
                        i
                        for i, row in enumerate(rows)
                        if int(row["n"]) == reused.comment_id
                    )
                )
                if field == "body":
                    rows[body_index]["b"] += " human edit"
                elif field == "producer":
                    roots[ordinal] = replace(roots[ordinal], producer_id=998)
                elif field == "review":
                    roots[ordinal] = replace(roots[ordinal], review_id=4)
                elif field == "commit":
                    roots[ordinal] = replace(roots[ordinal], head_sha="f" * 40)
                elif field == "thread":
                    ids = [
                        (cid, "different" if cid == reused.comment_id else tid)
                        for cid, tid in ids
                    ]
                elif field == "resolved":
                    roots[ordinal] = replace(roots[ordinal], resolved=True)
                elif field == "missing":
                    rows.pop(body_index)
                else:
                    rows.append(rows[body_index])
                changed = replace(
                    evidence,
                    roots=tuple(roots),
                    root_bodies=tuple(rows),
                    thread_ids=tuple(ids),
                )
                with self.assertRaises(ReviewInputError):
                    receipt.require_evidence(changed)

    def test_reused_document_is_closed_and_v1_cannot_erase_it(self):
        backend = _backend()
        state, _ = _publish(backend)
        receipt = NativeUnionReceipt.from_staging(
            backend.plan, state, producer_id=999, reused_roots=backend.host.reused_roots
        )
        value = receipt.to_document()
        changes = [
            lambda v: v.update(schema_version="native-review-union-v1"),
            lambda v: v["reused_roots"][0].update(extra=True),
            lambda v: v["reused_roots"][0].update(comment_id=True),
            lambda v: v["reused_roots"][0].update(thread_id="intent:invented"),
            lambda v: v["reused_roots"].append(v["reused_roots"][0]),
            lambda v: v.update(reused_roots=[]),
        ]
        for change in changes:
            changed = copy.deepcopy(value)
            change(changed)
            with self.assertRaises(ReviewInputError):
                NativeUnionReceipt.from_document(changed)
        wrong_body = replace(
            receipt,
            reused_roots=(replace(receipt.reused_roots[0], body_sha256="f" * 64),),
        )
        index = FindingIndex(
            review_result_digest(backend.result),
            backend.plan.base_sha,
            backend.assembled.index_entries,
        )
        with self.assertRaises(ReviewInputError):
            wrong_body.reconstruct_plan(backend.result, index)

    def test_reused_membership_cannot_expand_reserved_frame_capacity(self):
        backend = _backend(all_reused=True)
        state = PublicationStaging.prepare(
            transaction_id=backend.plan.transaction_id,
            result_sha256=backend.plan.result_sha256,
            plan_sha256=backend.plan.sha256,
            part_count=0,
        )
        root = backend.host.reused_roots[0]
        with self.assertRaisesRegex(ReviewInputError, "reserved frame capacity"):
            NativeUnionReceipt.from_staging(
                backend.plan,
                state,
                producer_id=999,
                reused_roots=tuple(
                    (
                        replace(
                            root,
                            instance=hashlib.sha256(str(i).encode()).hexdigest(),
                            comment_id=2000 + i,
                            thread_id=f"PRRT_{i}" + "x" * 200,
                        )
                        for i in range(12)
                    )
                ),
            )

    def test_packaged_v8_schema_accepts_v2_and_rejects_unknown_root_fields(self):
        from review_sensei_github.native_union import native_union_frame_document

        from review_sensei.schemas import validate_public_document

        backend = _backend()
        _, evidence = _publish(backend)
        document = native_union_frame_document(evidence.main_body)
        validate_public_document(document, "eligibility-v8")
        changed = copy.deepcopy(document)
        changed["native_union"]["reused_roots"][0]["foreign_authority"] = True
        with self.assertRaises(ReviewInputError):
            validate_public_document(changed, "eligibility-v8")
