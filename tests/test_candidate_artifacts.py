"""Actual archive/pack integrity checks; synthetic payloads are never native proof."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "qualify_candidate_artifacts", SCRIPTS / "qualify_candidate_artifacts.py"
)
assert _SPEC is not None and _SPEC.loader is not None
CANDIDATE = importlib.util.module_from_spec(_SPEC)
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    _SPEC.loader.exec_module(CANDIDATE)


class CandidateArtifactsTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "dist").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="candidate-contract-", dir=ROOT / "dist"
        )
        self.addCleanup(self.temporary.cleanup)
        self.stage = Path(self.temporary.name)
        self.root = self.stage / "source"
        self.root.mkdir()
        shutil.copy2(ROOT / "pyproject.toml", self.root / "pyproject.toml")
        shutil.copy2(ROOT / ".gitignore", self.root / ".gitignore")
        shutil.copytree(ROOT / "packages/npm", self.root / "packages/npm")
        for command in (
            ["git", "init", "-q"],
            ["git", "add", "."],
            [
                "git",
                "-c",
                "user.name=Candidate Test",
                "-c",
                "user.email=candidate@example.invalid",
                "commit",
                "-qm",
                "Synthetic candidate source",
            ],
        ):
            subprocess.run(command, cwd=self.root, check=True, capture_output=True)
        self.source = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.stage = self.root / "dist"
        self.stage.mkdir()

    def bundle(self, target):
        root = self.stage / "build" / target
        root.mkdir(parents=True)
        payload = root / Path(str(CANDIDATE.TARGETS[target]["payload"])).name
        payload.write_bytes(b"synthetic native test payload")
        payload.chmod(0o755)
        (root / "_internal").mkdir()
        (root / "_internal" / "runtime.dat").write_bytes(b"synthetic sibling")
        return root

    def archived(self, targets=None):
        native = self.stage / "native-input"
        for target in CANDIDATE.TARGETS if targets is None else targets:
            CANDIDATE.archive_native(
                self.root, target, self.bundle(target), native / target, self.source
            )
        return native

    def packed(self):
        native = self.archived()
        return CANDIDATE.assemble_candidate(
            self.root, native, self.stage / "npm", self.source
        )

    def test_archive_binds_source_and_all_file_bytes_and_preserves_execution(self):
        target = "darwin-arm64"
        bundle = self.bundle(target)
        archive = CANDIDATE.archive_native(
            self.root, target, bundle, self.stage / "archived", self.source
        )
        receipt = json.loads((archive.parent / "native-receipt.json").read_text())
        self.assertEqual(receipt["source_sha"], self.source)
        self.assertEqual(receipt["archive_sha256"], CANDIDATE.digest(archive))
        restored = CANDIDATE.unpack_native(archive, self.stage / "restored")
        self.assertEqual(CANDIDATE.inventory(restored), CANDIDATE.inventory(bundle))
        if os.name != "nt":
            self.assertTrue(os.access(restored / "review-sensei", os.X_OK))

    def test_wrong_checkout_source_is_rejected_before_archiving(self):
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "checked-out head"):
            CANDIDATE.archive_native(
                self.root,
                "darwin-arm64",
                self.bundle("darwin-arm64"),
                self.stage / "archive",
                "a" * 40,
            )
        self.assertFalse((self.stage / "archive").exists())

    def test_dirty_source_refuses_before_artifact_creation(self):
        (self.root / "pyproject.toml").write_text("modified source", encoding="utf-8")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "worktree must be clean"):
            CANDIDATE.archive_native(
                self.root,
                "darwin-arm64",
                self.bundle("darwin-arm64"),
                self.stage / "archive",
                self.source,
            )
        self.assertFalse((self.stage / "archive").exists())

    def test_symlinked_bundle_cannot_enter_candidate_archive(self):
        bundle = self.bundle("darwin-arm64")
        try:
            outside = self.stage / "outside"
            outside.write_bytes(b"outside data")
            (bundle / "alias").symlink_to(outside)
        except OSError:
            self.skipTest("host does not permit test symlinks")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "escapes"):
            CANDIDATE.archive_native(
                self.root, "darwin-arm64", bundle, self.stage / "archive", self.source
            )

    def test_internal_framework_links_are_regularized_without_changing_bytes(self):
        bundle = self.bundle("darwin-arm64")
        try:
            (bundle / "_internal" / "Current").symlink_to("runtime.dat")
        except OSError:
            self.skipTest("host does not permit test symlinks")
        archive = CANDIDATE.archive_native(
            self.root, "darwin-arm64", bundle, self.stage / "archive", self.source
        )
        restored = CANDIDATE.unpack_native(archive, self.stage / "restored")
        self.assertFalse((restored / "_internal/Current").is_symlink())
        self.assertEqual(
            (restored / "_internal/Current").read_bytes(),
            (bundle / "_internal/runtime.dat").read_bytes(),
        )
        receipt = json.loads((archive.parent / "native-receipt.json").read_text())
        self.assertEqual(
            receipt["materialized_symlinks"],
            [{"path": "_internal/Current", "target": "runtime.dat"}],
        )

    def test_cyclic_framework_link_is_rejected_before_archive(self):
        bundle = self.bundle("darwin-arm64")
        try:
            (bundle / "_internal" / "cycle").symlink_to("..", target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit test symlinks")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "cycle"):
            CANDIDATE.archive_native(
                self.root, "darwin-arm64", bundle, self.stage / "archive", self.source
            )

    def test_missing_targets_fail_before_any_npm_pack(self):
        native = self.archived(("darwin-arm64",))
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "all five"):
            CANDIDATE.assemble_candidate(
                self.root, native, self.stage / "npm", self.source
            )

    def test_wrong_source_and_tampered_archive_are_rejected(self):
        native = self.archived(("darwin-arm64",))
        receipt = native / "darwin-arm64" / "native-receipt.json"
        original = json.loads(receipt.read_text())
        changed = dict(original, source_sha="a" * 40)
        CANDIDATE.write_json(receipt, changed)
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "wrong source"):
            CANDIDATE.assemble_candidate(
                self.root, native, self.stage / "wrong-source", self.source
            )
        CANDIDATE.write_json(receipt, original)
        (receipt.parent / original["archive"]).write_bytes(b"tampered native archive")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "digest"):
            CANDIDATE.assemble_candidate(
                self.root, native, self.stage / "tampered", self.source
            )

    def test_archive_inventory_mismatch_is_not_accepted(self):
        native = self.archived(("darwin-arm64",))
        receipt = native / "darwin-arm64" / "native-receipt.json"
        value = json.loads(receipt.read_text())
        value["files"][0]["sha256"] = "0" * 64
        CANDIDATE.write_json(receipt, value)
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "file inventory"):
            CANDIDATE.assemble_candidate(
                self.root, native, self.stage / "wrong-inventory", self.source
            )

    def test_archive_traversal_links_and_duplicates_fail_closed(self):
        for kind in ("traversal", "link", "duplicate"):
            with self.subTest(kind=kind):
                archive = self.stage / (kind + ".tar.gz")
                with tarfile.open(archive, "w:gz") as stream:
                    member = tarfile.TarInfo(
                        "../escape" if kind == "traversal" else "review-sensei/payload"
                    )
                    if kind == "link":
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/outside"
                    else:
                        member.size = 1
                    stream.addfile(member, None if kind == "link" else io.BytesIO(b"x"))
                    if kind == "duplicate":
                        stream.addfile(member, io.BytesIO(b"x"))
                with self.assertRaisesRegex(CANDIDATE.CandidateError, "unsafe"):
                    CANDIDATE.unpack_native(archive, self.stage / kind)
        self.assertFalse((self.stage / "escape").exists())

    @unittest.skipUnless(
        shutil.which("npm"), "npm is required for actual local tarball packing"
    )
    def test_actual_pack_receipt_binds_all_six_tarballs_and_rejects_changed_bytes(self):
        packed = self.packed()
        receipt = CANDIDATE.verify_packed(self.root, packed, self.source)
        self.assertEqual(len(receipt["packages"]), 6)
        self.assertEqual(set(receipt["native"]), set(CANDIDATE.TARGETS))
        first = packed / receipt["packages"][0]["file"]
        first.write_bytes(b"modified exact tarball")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "digest"):
            CANDIDATE.verify_packed(self.root, packed, self.source)

    @unittest.skipUnless(
        shutil.which("npm"), "npm is required for actual local tarball packing"
    )
    def test_packed_receipt_cannot_substitute_a_different_source_or_duplicate_package(
        self,
    ):
        packed = self.packed()
        path = packed / "candidate-packages.json"
        original = json.loads(path.read_text())
        CANDIDATE.write_json(path, dict(original, source_sha="f" * 40))
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "source"):
            CANDIDATE.verify_packed(self.root, packed, self.source)
        original["packages"][-1] = original["packages"][0]
        CANDIDATE.write_json(path, original)
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "six packages"):
            CANDIDATE.verify_packed(self.root, packed, self.source)


class CandidateWorkflowTests(unittest.TestCase):
    def test_existing_native_builds_are_reused_and_missing_targets_are_added(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertEqual(
            workflow.count("bash scripts/build_linux_standalone_in_container.sh"), 1
        )
        self.assertIn("python-version: '3.11.9'", workflow)
        self.assertIn('--report "$RUNNER_TEMP/frozen-crypto.json"', workflow)
        extra = workflow.split("  native-candidate:\n", 1)[1].split(
            "  candidate-npm:\n", 1
        )[0]
        self.assertIn("target: darwin-arm64", extra)
        self.assertIn("target: win32-x64", extra)
        self.assertNotIn("target: darwin-x64", extra)
        for target in CANDIDATE.TARGETS:
            self.assertIn("target: " + target, workflow)
        required = workflow.split("  required-checks:\n", 1)[1]
        for job in (
            "linux-standalone",
            "intel-standalone",
            "native-candidate",
            "candidate-npm",
            "candidate-consumer",
        ):
            self.assertIn('require_success "${{ needs.' + job + '.result }}"', required)

    def test_candidate_pipeline_is_read_only_and_has_bounded_matching_consumers(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        candidate = workflow.split("  candidate-npm:\n", 1)[1].split("  workers:\n", 1)[
            0
        ]
        self.assertIn(
            "needs: [linux-standalone, intel-standalone, native-candidate]", candidate
        )
        self.assertIn("name: candidate-npm-tarballs", candidate)
        self.assertEqual(candidate.count("timeout-minutes: 15"), 2)
        self.assertIn("retention-days: 7", candidate)
        for forbidden in (
            "npm publish",
            "secrets.",
            "id-token: write",
            "attestations: write",
            "environment:",
            "workflow_dispatch",
        ):
            self.assertNotIn(forbidden, candidate)
        script = (SCRIPTS / "qualify_candidate_artifacts.py").read_text()
        for option in ('"--offline"', '"--ignore-scripts"', '"--omit=optional"'):
            self.assertIn(option, script)
