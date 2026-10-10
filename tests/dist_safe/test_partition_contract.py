"""Partition codec and adapter parity using only installed wheel schemas."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei.bounded_evidence import EvidenceReadBudget  # noqa: E402
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.schemas import validate_public_document  # noqa: E402
from review_sensei.session import (  # noqa: E402
    LocalSessionLedger,
    SessionIdentity,
    assessment_queue_manifest_capacity,
    read_session_assessment_queue,
)


class InstalledPartitionTests(unittest.TestCase):
    def test_installed_parts_round_trip_and_missing_latest_part_refuses(self):
        identity = SessionIdentity("synthetic/repo", 240, 99)
        binding = {
            "repository": identity.repository,
            "repository_id": 99,
            "pull_request": 240,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "policy_digest": "c" * 64,
            "configuration_digest": "d" * 64,
            "generation": 1,
            "producer": "local-ledger",
            "purpose": "evidence",
            "schema_version": "1.0",
        }
        document = {
            "items": [
                hashlib.sha256(str(index).encode()).hexdigest() for index in range(250)
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(
                Path(directory),
                evidence_budget=EvidenceReadBudget(),
                enable_partition_writes=True,
            )
            manifest = ledger.stage_evidence(
                identity,
                binding=binding,
                document=document,
                item_count=250,
                max_manifest_bytes=8192,
            )
            validate_public_document(manifest, "evidence-manifest")
            reader = LocalSessionLedger(Path(directory))
            self.assertEqual(
                reader.read_evidence(identity, manifest, expected_binding=binding),
                document,
            )
            next(Path(directory).glob(".evidence/*/*/*")).unlink()
            with self.assertRaises(ReviewInputError):
                reader.read_evidence(identity, manifest, expected_binding=binding)

    def test_installed_queue_slot_schema_digest_and_complete_restart(self):
        identity = SessionIdentity("synthetic/repo", 240, 99)
        document = {"schema_version": "1.0", "synthetic_journal": ["retained"]}
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            ledger.initialize(identity)

            def mutation(record):
                binding = {
                    "repository": identity.repository,
                    "repository_id": 99,
                    "pull_request": 240,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                    "policy_digest": "c" * 64,
                    "configuration_digest": "d" * 64,
                    "generation": record.generation,
                    "producer": "local-ledger",
                    "purpose": "queue",
                    "schema_version": "1.0",
                }
                manifest = ledger.stage_evidence(
                    identity,
                    binding=binding,
                    document=document,
                    item_count=1,
                    max_manifest_bytes=assessment_queue_manifest_capacity(record),
                )
                root = {
                    "schema_version": "1.0",
                    "inventory_digest": "e" * 64,
                    "inventory_generation": record.generation,
                    "state_manifest": manifest,
                    "active_operation": None,
                }
                validate_public_document(root, "assessment-queue-root")
                return record.evolve(
                    assessment_queue=root, generation=record.generation + 1
                )

            saved = ledger.replace(identity, mutation)
            reader = LocalSessionLedger(Path(directory))
            loaded = reader.load(identity)
            self.assertEqual(loaded.record, saved)
            self.assertEqual(
                read_session_assessment_queue(reader, loaded.record), document
            )
