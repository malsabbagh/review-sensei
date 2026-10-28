"""Lossless projection contracts; authenticated storage is tested separately."""

import copy
import hashlib
import json
import unittest
from dataclasses import replace

from review_sensei_github.publication import (
    IndexedFinding,
    finding_index_marker,
    review_result_digest,
)
from review_sensei_github.retention_projection import (
    checkpoint_projection,
    inventory_projection,
    restore_checkpoint_projection,
    restore_inventory_projection,
)

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.human_inventory import inventory_result_document
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction


def fixture(count=60, body_bytes=4000, *, evidence_policy="legacy"):
    comments = tuple(
        ReviewComment(
            path=f"src/file{i}.py",
            line=1,
            body=(f"Complete explanation {i}. " + "x" * body_bytes),
            blocking=True,
            effective_blocking=i % 2 == 0,
            needs_human=i % 3 == 0,
        )
        for i in range(count)
    )
    result = ReviewResult(
        summary="Complete retained result.",
        comments=comments,
        provider="fixture",
        model="fixture",
        review_status="complete",
        evidence_policy=evidence_policy,
    )
    tx = ReviewTransaction.create(
        repository="owner/repo",
        pull_request=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        policy_digest="c" * 64,
        configuration_digest="d" * 64,
        evidence_digest="e" * 64,
        reservation_id="f" * 64,
        generation=1,
    ).with_result(result.content_digest())
    bound = replace(result, transaction=tx)
    raw = canonical_bytes(result.to_dict())
    source = canonical_bytes(bound.to_dict())
    digest = review_result_digest(bound)
    binding = {
        "repository": "owner/repo",
        "repository_id": 123,
        "pull_request": 7,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "transaction_id": tx.transaction_id,
        "original_content_digest": bound.content_digest(),
        "publication_digest": digest,
        "policy_digest": "c" * 64,
        "configuration_digest": "d" * 64,
        "evidence_digest": "e" * 64,
    }
    inventory = PendingHumanReview.from_result(bound, "a" * 40)
    document = {
        "human_inventory": inventory_result_document(inventory, result_digest=digest)
        if inventory is not None
        else None,
        "finding_index": finding_index_marker(
            repository_id=123,
            pull_request=7,
            head_sha="b" * 40,
            base_sha="a" * 40,
            result_digest=digest,
            complete_inventory=True,
            entries=tuple(
                IndexedFinding(
                    instance=finding_instance_fingerprint(c),
                    lifecycle=finding_lifecycle_for_comment(c).fingerprint,
                    blocking=c.blocks_approval,
                    needs_human=c.needs_human,
                )
                for c in comments
            ),
        ),
        "visible_prose": [],
    }
    return result, bound, raw, source, binding, document


class RetentionProjectionTests(unittest.TestCase):
    def test_original_confirmed_policy_is_preserved_without_running_policy_again(self):
        _, _, _, source, binding, document = fixture(4, evidence_policy="confirmed")
        encoded = inventory_projection(source, document, binding, {"original": True})
        self.assertEqual(
            restore_inventory_projection(encoded, binding, lambda _: source), document
        )

    def test_original_instance_cannot_be_reassigned_to_another_lifecycle(self):
        from review_sensei_github.publication import parse_finding_index

        _, _, _, source, binding, document = fixture(4)
        index = parse_finding_index(
            document["finding_index"],
            repository_id=123,
            pull_request=7,
            head_sha="b" * 40,
        )
        changed = copy.deepcopy(document)
        changed["finding_index"] = finding_index_marker(
            repository_id=123,
            pull_request=7,
            head_sha="b" * 40,
            base_sha="a" * 40,
            result_digest=binding["publication_digest"],
            complete_inventory=True,
            entries=(replace(index.entries[0], lifecycle="0" * 64), *index.entries[1:]),
        )
        with self.assertRaises(ReviewInputError):
            inventory_projection(source, changed, binding, {})

    def test_logical_checkpoint_can_cross_original_physical_result_cap(self):
        from review_sensei_github.operation_comments import _MAX_RESULT_BYTES
        from review_sensei_github.publication_recovery import _payload

        original, bound, _, _, _, _ = fixture(63, 16_300)
        raw = canonical_bytes(original.to_dict())
        padding = _MAX_RESULT_BYTES - 100 - len(raw)
        self.assertGreater(padding, 0)
        original = replace(original, summary=original.summary + "s" * padding)
        raw = canonical_bytes(original.to_dict())
        bound = replace(
            original,
            transaction=replace(
                bound.transaction, result_sha256=original.content_digest()
            ),
        )
        logical = canonical_bytes(bound.to_dict())
        self.assertLessEqual(len(raw), _MAX_RESULT_BYTES)
        self.assertGreater(len(logical), _MAX_RESULT_BYTES)
        # Standalone native result parts retain their unchanged physical cap.
        with self.assertRaises(ValueError):
            _payload(bound)
        self.assertEqual(_payload(bound, projected=True), logical)
        encoded = checkpoint_projection(raw, bound, {"selected": "source"})
        self.assertLessEqual(len(encoded), 8192)
        self.assertEqual(
            restore_checkpoint_projection(encoded, bound.transaction, lambda _: raw),
            logical,
        )

    def test_checkpoint_preserves_complete_payload_and_closed_baseline_overflow(self):
        result, bound, raw, _, _, _ = fixture(2)
        reference = {"original_owned_event": 71, "scope": "original"}
        for overflow in (False, True):
            target = bound
            if overflow:
                target = replace(
                    result,
                    review_status="partial",
                    persistence_status="capacity-exceeded",
                )
                target = replace(
                    target,
                    transaction=replace(
                        bound.transaction, result_sha256=target.content_digest()
                    ),
                )
            payload = checkpoint_projection(raw, target, reference)
            calls = []

            def authenticated(ref):
                self.assertEqual(ref, reference)
                calls.append(ref)
                return raw

            restored = restore_checkpoint_projection(
                payload, target.transaction, authenticated
            )
            self.assertEqual(restored, canonical_bytes(target.to_dict()))
            self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(raw), result.to_dict())

    def test_checkpoint_rejects_changes_even_outside_content_digest(self):
        original, bound, raw, _, _, _ = fixture(1)
        for unbound in (
            replace(original, provider="changed"),
            replace(original, summary="changed"),
            replace(original, work_policy_digest="1" * 64),
            replace(original, review_status="partial"),
        ):
            target = replace(
                unbound,
                transaction=replace(
                    bound.transaction, result_sha256=unbound.content_digest()
                ),
            )
            with (
                self.subTest(field=target.to_dict()),
                self.assertRaises(ReviewInputError),
            ):
                checkpoint_projection(raw, target, {})
        payload = checkpoint_projection(raw, bound, {"source": "original"})
        with self.assertRaises(ReviewInputError):
            restore_checkpoint_projection(
                payload, bound.transaction, lambda _: raw + b" "
            )
        changed = json.loads(payload)
        changed["unrecognized_patch"] = {}
        with self.assertRaises(ReviewInputError):
            restore_checkpoint_projection(
                canonical_bytes(changed), bound.transaction, lambda _: raw
            )

    def test_inventory_keeps_all_prose_instances_and_original_runtime_flags(self):
        for count, size in ((0, 1), (60, 16000), (250, 400)):
            with self.subTest(count=count):
                _, _, _, source, binding, document = fixture(count, size)
                reference = {
                    "checkpoint_event": "original",
                    "payload_sha256": hashlib.sha256(source).hexdigest(),
                }
                encoded = inventory_projection(source, document, binding, reference)
                self.assertLessEqual(len(encoded), 65_536)

                def authenticated(ref):
                    self.assertEqual(ref, reference)
                    return source

                self.assertEqual(
                    restore_inventory_projection(encoded, binding, authenticated),
                    document,
                )
                if count:
                    self.assertLess(len(encoded), len(canonical_bytes(document)))
                self.assertNotIn(
                    "needs_human", json.loads(source)["comments"][0] if count else {}
                )

    def test_inventory_changed_flags_ordinals_fields_and_source_refuse(self):
        _, _, _, source, binding, document = fixture(4)
        encoded = inventory_projection(
            source, document, binding, {"source": "original"}
        )
        for change in (
            "flag",
            "ordinal",
            "missing",
            "extra",
            "binding",
            "instance_type",
        ):
            candidate = json.loads(encoded)
            if change == "flag":
                candidate["admission"][0]["human"] = not candidate["admission"][0][
                    "human"
                ]
            elif change == "ordinal":
                candidate["admission"][0]["ordinal"] = 1
            elif change == "missing":
                candidate["admission"].pop()
            elif change == "extra":
                candidate["new_authority"] = True
            elif change == "instance_type":
                candidate["admission"][0]["instance"] = []
            else:
                candidate["binding"]["transaction_id"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                restore_inventory_projection(
                    canonical_bytes(candidate), binding, lambda _: source
                )
        changed_source = json.loads(source)
        changed_source["comments"][0]["body"] += " changed"
        with self.assertRaises(ReviewInputError):
            restore_inventory_projection(
                encoded, binding, lambda _: canonical_bytes(changed_source)
            )
        unsupported = copy.deepcopy(document)
        unsupported["visible_prose"] = [{"unproven": "receipt"}]
        with self.assertRaises(ReviewInputError):
            inventory_projection(source, unsupported, binding, {})


if __name__ == "__main__":
    unittest.main()
