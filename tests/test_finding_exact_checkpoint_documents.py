"""Only actual source producers issue exact bytes and prospective envelopes."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.finding_lifecycle import FindingLifecycleJournal

from review_sensei import finding_lifecycle_store as store
from review_sensei.bounded_evidence import canonical_bytes, partition_evidence
from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_batch as fixtures
from tests import test_finding_ordered_prefix_journal as ordered_fixtures


class ExactCheckpointDocumentTests(unittest.TestCase):
    def issued(self, root):
        journal = fixtures.NativeBatchTests().journal(root)
        captured = {}

        def capture(ledger, record, **kwargs):
            captured.update(ledger=ledger, record=record, kwargs=kwargs)
            return {}

        with patch.object(store, "preflight_finding_lifecycle", capture):
            witness = journal.preflight_native_batch(
                ordered_fixtures.OrderedPrefixJournalTests().requests(), native_calls=10
            )
        return journal, witness, captured

    def test_opaque_scope_documents_and_one_use_are_required(self):
        for fault in (
            "fake",
            "copy",
            "reuse",
            "changed_document",
            "clone_root",
            "mutated_root",
            "closed",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                journal, witness, captured = self.issued(root)
                record = captured["record"]
                kwargs = captured["kwargs"]
                docs = (kwargs["document"], *kwargs["future_documents"])
                proof = kwargs["exact_documents"]
                if fault == "fake":
                    proof = {"exact": True}
                elif fault == "copy":
                    proof = copy.copy(proof)
                elif fault == "reuse":
                    store._consume_exact_documents(proof, journal.ledger, record, docs)
                elif fault == "changed_document":
                    docs = ({"changed": True}, *docs[1:])
                elif fault == "clone_root":
                    record = copy.deepcopy(record)
                elif fault == "mutated_root":
                    object.__setattr__(record, "operator_paused", True)
                else:
                    witness.stop()
                with self.assertRaises(ReviewInputError):
                    store._consume_exact_documents(proof, journal.ledger, record, docs)

    def test_exact_pending_only_not_unknown_stop_or_final(self):
        with tempfile.TemporaryDirectory() as root:
            journal, witness, captured = self.issued(root)
            kwargs = captured["kwargs"]
            docs = (kwargs["document"], *kwargs["future_documents"])
            indices = store._consume_exact_documents(
                kwargs["exact_documents"], journal.ledger, journal._record, docs
            )
            self.assertEqual(indices, frozenset(range(len(docs) - 1)))
            self.assertNotIn(len(docs) - 1, indices)

    def test_known_registered_function_marker_cannot_forge_either_issuer(self):
        with tempfile.TemporaryDirectory() as root:
            journal, witness, captured = self.issued(root)
            source = FindingLifecycleJournal.preflight_native_batch
            with self.assertRaises(ReviewInputError):
                store._issue_exact_documents(
                    source,
                    journal.ledger,
                    journal._record,
                    ({},),
                    frozenset({0}),
                    lambda: True,
                )
            with self.assertRaises(ReviewInputError):
                store._issue_projected_root(
                    source,
                    journal.ledger,
                    journal._record,
                    copy.deepcopy(journal._record),
                    (),
                    "a" * 64,
                    object(),
                    lambda: True,
                )

            @store._sizing_producer
            def custom(self, *, _sizing_factories):
                return _sizing_factories[0](
                    self.ledger, self._record, ({},), frozenset({0}), lambda: True
                )

            with self.assertRaises(ReviewInputError):
                custom(journal)

    def test_compressed_data_excludes_generation_and_count(self):
        binding = dict(
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            generation=1,
            producer="github-bot:10",
            purpose="finding-lifecycle",
            schema_version="1.0",
        )
        value = {
            "schema_version": "finding-lifecycle-evidence-v2",
            "lifecycle": {"fixed": "exact"},
        }
        first = partition_evidence(value, binding=binding, item_count=1)
        last = partition_evidence(
            value, binding={**binding, "generation": 999999999}, item_count=4096
        )
        self.assertEqual([p["data"] for p in first], [p["data"] for p in last])
        self.assertEqual(
            [len(canonical_bytes(p)) for p in first],
            [len(canonical_bytes(p)) for p in last],
        )
