"""F's immutable primitive qualification, distinct from hosted activation."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from review_sensei.bounded_evidence import (
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import HumanInventoryResolution, PendingHumanReview
from review_sensei.human_inventory import read_human_inventory, stage_human_inventory
from tests.epic238_f_process_support import barrier, kill_at, load, save
from tests.test_authenticated_partitions import binding
from tests.test_evidence_capacity import human_result, varied_comments


def inventory(count: int) -> PendingHumanReview:
    comments = tuple(
        replace(item, body=item.body + ' Unicode 深/🙂/é \\" exact evidence.')
        for item in varied_comments(count, detail=3, unique_paths=count)
    )
    return PendingHumanReview.from_result(human_result(comments), binding()["base_sha"])


class DurableParts:
    """Explicit authenticated test transport; supplies no activation authority."""

    def __init__(self, root: Path, budget: EvidenceReadBudget, fault=""):
        self.root, self.budget, self.fault = root, budget, fault
        self.writes = self.reads = 0

    def boundary(self, name):
        # The owner charges before calling this transport. Save that charge
        # before either physical I/O or a kill can lose it.
        save(self.root / "budget.json", self.budget.snapshot())
        barrier(self.root, self.fault, name)

    def write(self, document, remaining):
        assert remaining > 0
        index = self.writes
        self.writes += 1
        self.boundary(f"before-write-{index}")
        identity = hashlib.sha256(canonical_bytes(document)).hexdigest()
        path = self.root / f"part-{identity}.json"
        if path.exists():
            assert load(path) == document
        else:
            save(path, document)
        self.boundary(f"after-write-{index}")
        return identity

    def read(self, identity, remaining):
        assert remaining > 0
        index = self.reads
        self.reads += 1
        self.boundary(f"before-read-{index}")
        value = AuthenticatedPart(
            load(self.root / f"part-{identity}.json"), "local-ledger"
        )
        self.boundary(f"after-read-{index}")
        return value


def stage(root: Path, count=250, fault=""):
    budget_path = root / "budget.json"
    budget = EvidenceReadBudget(
        snapshot=load(budget_path) if budget_path.exists() else None
    )
    parts = DurableParts(root, budget, fault)
    manifest = stage_human_inventory(
        inventory(count),
        binding=binding(purpose="human-inventory"),
        max_manifest_bytes=8192,
        writer=parts.write,
        reader=parts.read,
        budget=budget,
    )
    save(root / "manifest.json", manifest)
    return manifest, budget, parts


class FrozenInventoryFaultTests(unittest.TestCase):
    def test_complete_varied_unicode_100_250_across_fresh_transport_and_resolution(
        self,
    ):
        for count in (100, 250):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, before, _ = stage(root, count)
                original_budget = before.snapshot()
                restored_budget = EvidenceReadBudget(
                    snapshot=load(root / "budget.json")
                )
                restored = read_human_inventory(
                    load(root / "manifest.json"),
                    expected_binding=binding(purpose="human-inventory"),
                    reader=DurableParts(root, restored_budget).read,
                    budget=restored_budget,
                )
                self.assertEqual(
                    restored.inventory_document(), inventory(count).inventory_document()
                )
                self.assertEqual(
                    len({item.fingerprint for item in restored.findings}), count
                )
                self.assertEqual(manifest["item_count"], count)
                self.assertEqual(
                    manifest["decoded_bytes"],
                    len(canonical_bytes(restored.inventory_document())),
                )
                self.assertEqual(restored_budget.calls, 3 * len(manifest["parts"]))
                self.assertEqual(
                    restored_budget.snapshot()["deadline_unix_ms"],
                    original_budget["deadline_unix_ms"],
                )
                for number in (1, count // 2, count):
                    updated = replace(
                        restored,
                        resolved=tuple(
                            item.fingerprint for item in restored.findings[:number]
                        ),
                    )
                    save(
                        root / "resolution.json",
                        HumanInventoryResolution.from_inventory(updated).to_dict(),
                    )
                    receipt = HumanInventoryResolution.from_dict(
                        load(root / "resolution.json")
                    )
                    self.assertEqual(receipt.restore(restored), updated)
                    self.assertEqual(
                        len(receipt.restore(restored).pending), count - number
                    )
                self.assertFalse((root / "active-root.json").exists())

    def test_real_parent_kill_before_after_every_part_write_and_readback(self):
        count = len(
            partition_evidence(
                inventory(250).inventory_document(),
                binding=binding(purpose="human-inventory"),
                item_count=250,
            )
        )
        for direction in ("write", "read"):
            for side in ("before", "after"):
                for index in range(count):
                    boundary = f"{side}-{direction}-{index}"
                    with (
                        self.subTest(boundary=boundary),
                        tempfile.TemporaryDirectory() as directory,
                    ):
                        root = Path(directory)
                        kill_at(
                            "tests.test_epic238_frozen_inventory_faults", root, boundary
                        )
                        saved = load(root / "budget.json")
                        self.assertEqual(
                            saved["calls"],
                            index + 1 + (count if direction == "read" else 0),
                        )
                        self.assertFalse((root / "manifest.json").exists())
                        self.assertFalse((root / "active-root.json").exists())
                        manifest, budget, parts = stage(root)
                        restored = read_human_inventory(
                            manifest,
                            expected_binding=binding(purpose="human-inventory"),
                            reader=parts.read,
                            budget=budget,
                        )
                        self.assertEqual(
                            restored.inventory_digest, inventory(250).inventory_digest
                        )
                        self.assertEqual(len(restored.pending), 250)
                        self.assertEqual(budget.calls, saved["calls"] + 3 * count)
                        self.assertEqual(
                            budget.snapshot()["deadline_unix_ms"],
                            saved["deadline_unix_ms"],
                        )
                        self.assertLessEqual(budget.calls, 60)

    def test_fresh_read_refuses_changed_target_and_missing_exact_part(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _, _ = stage(root, 100)
            for field, value in (("head_sha", "e" * 40), ("producer", "github-bot:99")):
                budget = EvidenceReadBudget()
                context = binding(purpose="human-inventory")
                context[field] = value
                with self.subTest(field=field), self.assertRaises(ReviewInputError):
                    read_human_inventory(
                        manifest,
                        expected_binding=context,
                        reader=DurableParts(root, budget).read,
                        budget=budget,
                    )
            (root / f"part-{manifest['parts'][-1]['storage_id']}.json").unlink()
            budget = EvidenceReadBudget()
            with self.assertRaises(FileNotFoundError):
                read_human_inventory(
                    manifest,
                    expected_binding=binding(purpose="human-inventory"),
                    reader=DurableParts(root, budget).read,
                    budget=budget,
                )


if __name__ == "__main__":
    stage(Path(sys.argv[1]), fault=sys.argv[2])
