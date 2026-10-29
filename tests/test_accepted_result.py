"""Original public computation → exact owned event/parts source validation."""

import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.accepted_result import (
    read_accepted_result,
    selected_accepted_result,
)
from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_host import OperationComment
from review_sensei_github.publication_recovery import _event_key
from review_sensei_github.retention_projection import (
    checkpoint_projection,
    inventory_projection,
    restore_checkpoint_projection,
    restore_inventory_projection,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_review_run import EntryFixture

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult, ReviewTransaction


class FreshNativeComments:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = 0

    def list_comments(self):
        self.requests += 1
        rows = [self.fixture.comment, *self.fixture.extra_comments.values()]
        return tuple(
            OperationComment(
                row["id"], row["body"], row["user"]["id"], row["user"]["type"]
            )
            for row in rows
            if row is not None
        )

    def create(self, body):
        raise AssertionError("source reader must not recreate missing data")

    def update(self, comment_id, body):
        raise AssertionError("source reader must not mutate prior data")


class AcceptedResultTests(unittest.TestCase):
    def test_projection_preserves_the_inventory_emitted_by_publication(self):
        from review_sensei_github.default_human_inventory import HumanInventoryStore

        observed = []
        original = HumanInventoryStore._payload
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=512)

            def record(store, document):
                # Observe the real publisher's document without changing its
                # writer, authority, policy, budgets or stored legacy bytes.
                payload = original(store, document)
                reference, source = store._checkpoint_source(
                    FreshNativeComments(f).list_comments()
                )
                binding = store.context.binding()
                encoded = inventory_projection(source, document, binding, reference)

                def load(selected):
                    self.assertEqual(selected, reference)
                    return source

                self.assertEqual(
                    restore_inventory_projection(encoded, binding, load), document
                )
                observed.append(copy.deepcopy(document))
                return payload

            with patch.object(HumanInventoryStore, "_payload", record):
                self.assertEqual(f.invoke(human=True), 0, f.error)
            self.assertTrue(observed)
            self.assertIsNotNone(observed[0]["human_inventory"])

    def test_projection_matches_the_actual_public_checkpoint_and_successor(self):
        for overflow in (False, True):
            with (
                self.subTest(overflow=overflow),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory, limit=512)
                f.provider_comments = [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Preserve the complete original explanation.",
                        "severity": "low",
                        "blocking": False,
                    }
                ]
                for run in range(2 if not overflow else 1):
                    capacity = (
                        patch(
                            "review_sensei.session.checkpoint_baseline_capacity",
                            return_value=128,
                        )
                        if overflow
                        else nullcontext()
                    )
                    with capacity:
                        self.assertEqual(f.invoke(), 1 if overflow else 0, f.error)
                    if overflow:
                        self.assertIn("baseline_capacity_exceeded", f.error)
                    tx = parse_session_comment(
                        f.comment["body"], identity=f.selected.identity
                    ).transaction
                    captured = selected_accepted_result(f.selected.http, tx, app_id=999)
                    store = ResultPartStore(FreshNativeComments(f), app_user_id=999)
                    available = store.read_available(
                        event_key=_event_key(f.selected.identity, tx)
                    )
                    self.assertIsNotNone(available)
                    actual = ReviewResult.from_dict(json.loads(available[1]))
                    if overflow:
                        self.assertEqual(actual.persistence_status, "capacity-exceeded")
                    descriptor = checkpoint_projection(
                        captured.payload, actual, captured.reference
                    )
                    self.assertEqual(
                        restore_checkpoint_projection(
                            descriptor,
                            tx,
                            lambda ref: read_accepted_result(
                                ref, tx, store, repository_id=123, app_id=999
                            ),
                        ),
                        available[1],
                    )
                    if run == 0 and not overflow:
                        f.next_run()

    def test_public_accepted_embedded_and_fallback_sources_reconstruct_exact_checkpoint(
        self,
    ):
        for rich in (False, True):
            with self.subTest(rich=rich), tempfile.TemporaryDirectory() as directory:
                f = EntryFixture(directory, limit=512)
                if rich:
                    f.provider_comments = [
                        {
                            "path": "src/app.py",
                            "line": 2,
                            "side": "RIGHT",
                            "body": f"Original complete explanation {i}. " + "x" * 3500,
                            "severity": "low",
                            "blocking": False,
                        }
                        for i in range(13)
                    ]
                self.assertEqual(f.invoke(), 0, f.error)
                tx = parse_session_comment(
                    f.comment["body"], identity=f.selected.identity
                ).transaction
                captured = selected_accepted_result(
                    f.selected.http, tx, app_id=f.selected.ledger.authenticated_app_id
                )
                port = FreshNativeComments(f)
                store = ResultPartStore(
                    port, app_user_id=captured.reference["producer_id"]
                )

                def source(reference):
                    return read_accepted_result(
                        reference, tx, store, repository_id=123, app_id=999
                    )

                self.assertEqual(source(captured.reference), captured.payload)
                self.assertEqual(bool(captured.reference["part_ids"]), rich)
                pending = replace(tx, phase="publication_pending")
                raw_result = ReviewResult.from_dict(
                    __import__("json").loads(captured.payload)
                )
                checkpoint = replace(raw_result, transaction=pending)
                descriptor = checkpoint_projection(
                    captured.payload, checkpoint, captured.reference
                )
                self.assertEqual(
                    restore_checkpoint_projection(descriptor, tx, source),
                    canonical_bytes(checkpoint.to_dict()),
                )

    def test_changed_missing_duplicate_and_cross_transaction_sources_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=512)
            self.assertEqual(f.invoke(), 0, f.error)
            tx = parse_session_comment(
                f.comment["body"], identity=f.selected.identity
            ).transaction
            captured = selected_accepted_result(f.selected.http, tx, app_id=999)
            store = ResultPartStore(
                FreshNativeComments(f), app_user_id=captured.reference["producer_id"]
            )
            event_id = captured.reference["event_comment_id"]
            saved = copy.deepcopy(f.extra_comments)
            for fault in ("missing", "changed", "duplicate", "unowned", "foreign_ref"):
                f.extra_comments = copy.deepcopy(saved)
                ref = captured.reference
                if fault == "missing":
                    del f.extra_comments[event_id]
                elif fault == "changed":
                    f.extra_comments[event_id]["body"] += " edited"
                elif fault == "duplicate":
                    duplicate = copy.deepcopy(f.extra_comments[event_id])
                    duplicate["id"] += 7_000_000
                    f.extra_comments[duplicate["id"]] = duplicate
                elif fault == "unowned":
                    f.extra_comments[event_id]["user"]["id"] += 1
                else:
                    ref["repository_id"] += 1
                with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                    read_accepted_result(ref, tx, store, repository_id=123, app_id=999)
            f.extra_comments = saved
            other = ReviewTransaction.create(
                repository=tx.repository,
                pull_request=tx.pull_request,
                base_sha=tx.base_sha,
                head_sha=tx.head_sha,
                policy_digest="9" * 64,
                configuration_digest=tx.configuration_digest,
                evidence_digest=tx.evidence_digest,
                reservation_id=tx.reservation_id,
                generation=tx.generation,
            ).with_result(tx.result_sha256)
            with self.assertRaises(ReviewInputError):
                read_accepted_result(
                    captured.reference, other, store, repository_id=123, app_id=999
                )


if __name__ == "__main__":
    unittest.main()
