"""Dictionary representation cannot change full history or select authority."""

import base64
import copy
import hashlib
import json
import unittest
import zlib

from review_sensei_github import finding_lifecycle_dictionary as codec
from review_sensei_github.finding_lifecycle import _pack_journal

from review_sensei.bounded_evidence import inline_manifest
from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_journal as f


class DictionaryCodecTests(unittest.TestCase):
    def fixture(self):
        binding = dict(
            repository="owner/repo",
            repository_id=7,
            pull_request=1,
            base_sha=f.BASE,
            head_sha=f.HEAD,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            generation=1,
            producer="github-bot:9001",
            purpose="finding-lifecycle",
            schema_version="1.0",
        )
        base = {
            "schema_version": "finding-lifecycle-evidence-v2",
            "lifecycle": _pack_journal(f.document()),
        }
        manifest = inline_manifest(base, binding=binding, item_count=1)
        target = f.document([f.effect()])
        packed = codec.pack(
            target,
            base=base,
            manifest=manifest,
            current_binding={**binding, "generation": 2},
        )
        return base, manifest, {**binding, "generation": 2}, target, packed

    def test_complete_history_receipts_and_pending_intent_roundtrip(self):
        base, manifest, binding, target, packed = self.fixture()
        before = copy.deepcopy((base, manifest, target))
        self.assertEqual(
            codec.unpack(packed, base=base, current_binding=binding), target
        )
        self.assertEqual((base, manifest, target), before)
        self.assertEqual(packed["dictionary_manifest"], manifest)
        self.assertLessEqual(packed["dictionary_bytes"], 32768)

    def test_foreign_or_newer_base_scope_is_rejected(self):
        for field, value in [
            ("repository", "foreign/repo"),
            ("producer", "github-bot:999"),
            ("head_sha", "e" * 40),
            ("base_sha", "e" * 40),
            ("policy_digest", "e" * 64),
            ("configuration_digest", "e" * 64),
            ("generation", 0),
        ]:
            with self.subTest(field=field):
                base, _, binding, _, packed = self.fixture()
                with self.assertRaises(ReviewInputError):
                    codec.unpack(
                        packed, base=base, current_binding={**binding, field: value}
                    )

    def test_closed_schema_commitments_bounds_and_recursive_base(self):
        for fault in (
            "extra",
            "digest",
            "dictionary_digest",
            "length",
            "bool_length",
            "dictionary_length",
            "algorithm",
            "base",
            "recursive",
            "base64",
            "encoded_limit",
        ):
            with self.subTest(fault=fault):
                base, _, binding, _, packed = self.fixture()
                if fault == "extra":
                    packed["extra"] = True
                elif fault == "digest":
                    packed["sha256"] = "f" * 64
                elif fault == "dictionary_digest":
                    packed["dictionary_sha256"] = "f" * 64
                elif fault == "length":
                    packed["decoded_bytes"] += 1
                elif fault == "bool_length":
                    packed["decoded_bytes"] = True
                elif fault == "dictionary_length":
                    packed["dictionary_bytes"] += 1
                elif fault == "algorithm":
                    packed["dictionary_derivation"] = "unknown"
                elif fault == "base":
                    base["lifecycle"]["native_atoms"].append(["integer", 1])
                elif fault == "recursive":
                    base["lifecycle"] = copy.deepcopy(packed)
                elif fault == "base64":
                    packed["data"] = "!"
                else:
                    packed["data"] = "A" * (codec.MAX_BYTES + 1)
                with self.assertRaises(ReviewInputError):
                    codec.unpack(packed, base=base, current_binding=binding)

    def test_trailing_truncated_overexpanding_and_noncanonical_streams(self):
        for fault in ("trailing", "truncated", "overflow", "noncanonical"):
            with self.subTest(fault=fault):
                base, manifest, binding, target, packed = self.fixture()
                data = base64.b64decode(packed["data"])
                if fault == "trailing":
                    data += zlib.compress(b"{}")
                elif fault == "truncated":
                    data = data[:-1]
                else:
                    dictionary, _ = codec._base(base, manifest, binding)
                    raw = (
                        b"x" * (packed["decoded_bytes"] + 1)
                        if fault == "overflow"
                        else json.dumps(target, indent=2).encode()
                    )
                    if fault == "noncanonical":
                        packed["decoded_bytes"] = len(raw)
                        packed["sha256"] = hashlib.sha256(raw).hexdigest()
                    compressor = zlib.compressobj(level=9, zdict=dictionary)
                    data = compressor.compress(raw) + compressor.flush()
                packed["data"] = base64.b64encode(data).decode()
                with self.assertRaises(ReviewInputError):
                    codec.unpack(packed, base=base, current_binding=binding)

    def test_actual_store_restore_closes_fence_after_dependency_even_inline(self):
        import tempfile
        from unittest.mock import patch

        from review_sensei_github.finding_lifecycle import _unpack_journal

        from review_sensei.finding_lifecycle_store import (
            read_finding_lifecycle,
            stage_finding_lifecycle,
        )
        from tests.test_finding_lifecycle_batch import NativeBatchTests

        with tempfile.TemporaryDirectory() as directory:
            journal = NativeBatchTests().journal(directory)
            ledger = journal.ledger
            document = journal._document()
            kwargs = dict(
                operation_id=journal.operation_id,
                base_sha=journal.base_sha,
                head_sha=journal.head_sha,
                policy_digest=journal.policy_digest,
                configuration_digest=journal.configuration_digest,
                item_count=1,
            )
            first = stage_finding_lifecycle(
                ledger, journal._record, document=_pack_journal(document), **kwargs
            )
            manifest = first.finding_lifecycle["manifest"]
            base = {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": _pack_journal(document),
            }
            packed = codec.pack(
                document,
                base=base,
                manifest=manifest,
                current_binding=manifest["binding"],
            )
            current = stage_finding_lifecycle(ledger, first, document=packed, **kwargs)
            stored = read_finding_lifecycle(ledger, current)
            self.assertTrue(codec.is_dictionary(stored))
            with patch.object(ledger, "load", wraps=ledger.load) as loads:
                resolved = codec.resolve_stored(ledger, current, stored)
                self.assertEqual(loads.call_count, 1)
            self.assertEqual(_unpack_journal(resolved), document)
            # A root change after the dependent bytes were decoded never returns
            # a full journal selected by the stale root.
            original_unpack = codec.unpack

            def change_root(*args, **kw):
                value = original_unpack(*args, **kw)
                ledger.replace(
                    current.identity(),
                    lambda r: r.evolve(
                        generation=r.generation + 1, operator_paused=True
                    ),
                )
                return value

            with patch.object(codec, "unpack", side_effect=change_root):
                with self.assertRaisesRegex(ReviewInputError, "authority changed"):
                    codec.resolve_stored(ledger, current, stored)

    def test_partitioned_base_missing_and_one_short_refuse_before_reconstruction(self):
        import tempfile
        from unittest.mock import patch

        from review_sensei.finding_lifecycle_store import (
            read_finding_lifecycle,
            stage_finding_lifecycle,
        )
        from tests.test_finding_lifecycle_batch import NativeBatchTests

        for fault in ("missing", "one_short"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                journal = NativeBatchTests().journal(directory)
                ledger = journal.ledger
                body = "".join(
                    hashlib.sha256(str(i).encode()).hexdigest() for i in range(200)
                )
                journal.effects = [f.effect({**f.REQUEST, "body": body})]
                document = journal._document()
                kwargs = dict(
                    operation_id=journal.operation_id,
                    base_sha=journal.base_sha,
                    head_sha=journal.head_sha,
                    policy_digest=journal.policy_digest,
                    configuration_digest=journal.configuration_digest,
                    item_count=1,
                )
                first = stage_finding_lifecycle(
                    ledger, journal._record, document=_pack_journal(document), **kwargs
                )
                manifest = first.finding_lifecycle["manifest"]
                self.assertEqual(manifest["encoding"], "partitioned-json-v1")
                base = {
                    "schema_version": "finding-lifecycle-evidence-v2",
                    "lifecycle": _pack_journal(document),
                }
                packed = codec.pack(
                    document,
                    base=base,
                    manifest=manifest,
                    current_binding=manifest["binding"],
                )
                current = stage_finding_lifecycle(
                    ledger, first, document=packed, **kwargs
                )
                stored = read_finding_lifecycle(ledger, current)
                if fault == "missing":
                    path = (
                        ledger._part_directory(current.identity())
                        / manifest["parts"][0]["storage_id"]
                    )
                    path.unlink()
                    with self.assertRaises(ReviewInputError):
                        codec.resolve_stored(ledger, current, stored)
                else:
                    cost = codec.restore_cost(ledger, stored, record=current)
                    while ledger.evidence_budget.ordinary_remaining() >= cost:
                        ledger.evidence_budget.consume()
                    with (
                        patch.object(
                            ledger, "read_evidence", wraps=ledger.read_evidence
                        ) as reads,
                        patch.object(ledger, "load", wraps=ledger.load) as loads,
                    ):
                        with self.assertRaises(ReviewInputError):
                            codec.resolve_stored(ledger, current, stored)
                        self.assertEqual(reads.call_count, 0)
                        self.assertEqual(loads.call_count, 0)

    def test_unknown_or_final_checkpoint_never_uses_pending_dictionary_encoder(self):
        import tempfile
        from unittest.mock import patch

        from review_sensei.finding_lifecycle_store import read_finding_lifecycle
        from tests.test_finding_lifecycle_batch import NativeBatchTests

        with tempfile.TemporaryDirectory() as directory:
            journal = NativeBatchTests().journal(directory)
            journal.effects = [f.effect()]
            with patch.object(
                journal,
                "_pending_wire",
                side_effect=AssertionError("final must be full"),
            ):
                journal.checkpoint()
            stored = read_finding_lifecycle(journal.ledger, journal._record)
            self.assertEqual(stored["schema_version"], "finding-lifecycle-packed-v3")
