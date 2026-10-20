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


def declare_fixture_posix_modes(tarball: Path) -> None:
    """Declare portable modes for known synthetic fixture executables.

    Windows cannot represent local POSIX execute bits. These fake payloads have
    a declared tar mode independent of the host; production pack/validation is
    unchanged, and explicit non-executable negative archives stay untouched.
    """
    rewritten = tarball.with_suffix(".fixture-modes.tgz")
    executable_members = {"package/bin/review-sensei", "package/bin/review-sensei.js"}
    with (
        tarfile.open(tarball, "r:gz") as source,
        tarfile.open(rewritten, "w:gz") as output,
    ):
        for member in source.getmembers():
            if member.isfile() and member.name in executable_members:
                member.mode = 0o755
            output.addfile(
                member, source.extractfile(member) if member.isfile() else None
            )
    rewritten.replace(tarball)


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
        run = subprocess.run

        def run_fixture_pack(command, *args, **kwargs):
            result = run(command, *args, **kwargs)
            if len(command) > 1 and command[1] == "pack" and result.returncode == 0:
                destination = Path(command[command.index("--pack-destination") + 1])
                for item in json.loads(result.stdout):
                    declare_fixture_posix_modes(destination / item["filename"])
            return result

        fixture_pack = patch.object(
            CANDIDATE.subprocess, "run", side_effect=run_fixture_pack
        )
        fixture_pack.start()
        self.addCleanup(fixture_pack.stop)

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

    def test_windows_source_modes_construct_declared_posix_fixture_without_weakening_validation(
        self,
    ):
        archive = self.stage / "windows-source-modes.tgz"
        package = self.root / "packages/npm/platforms/linux-x64-gnu"
        contents = {
            "package/" + name: (package / name).read_bytes()
            for name in ("package.json", "README.md", "LICENSE")
        }
        contents["package/bin/review-sensei"] = b"synthetic fixture payload"
        contents["package/bin/_internal/runtime.dat"] = b"synthetic runtime"
        with tarfile.open(archive, "w:gz") as stream:
            for name, body in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(body)
                member.mode = 0o644
                stream.addfile(member, io.BytesIO(body))
        invalid = self.stage / "executable-negative.tgz"
        shutil.copy2(archive, invalid)
        with self.assertRaisesRegex(ValueError, "not executable"):
            CANDIDATE.validate(archive)
        declare_fixture_posix_modes(archive)
        self.assertEqual(CANDIDATE.validate(archive)["role"], "platform")
        with tarfile.open(archive, "r:gz") as stream:
            self.assertEqual(stream.getmember("package/bin/review-sensei").mode, 0o755)
            self.assertEqual(
                stream.getmember("package/bin/_internal/runtime.dat").mode, 0o644
            )
        with self.assertRaisesRegex(ValueError, "not executable"):
            CANDIDATE.validate(invalid)

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

    def test_archive_noncanonical_and_case_collisions_refuse_before_extraction(self):
        names = (
            ("review-sensei/a", "review-sensei/./a"),
            ("review-sensei/a", "review-sensei//a"),
            ("review-sensei/A", "review-sensei/a"),
            ("review-sensei/A/one", "review-sensei/a/two"),
        )
        for index, pair in enumerate(names):
            with self.subTest(pair=pair):
                archive = self.stage / f"collision-{index}.tar.gz"
                with tarfile.open(archive, "w:gz") as stream:
                    for name in pair:
                        member = tarfile.TarInfo(name)
                        member.size = 1
                        stream.addfile(member, io.BytesIO(b"x"))
                output = self.stage / f"collision-{index}"
                with self.assertRaisesRegex(CANDIDATE.CandidateError, "unsafe"):
                    CANDIDATE.unpack_native(archive, output)
                self.assertEqual(list(output.rglob("*")), [])

    def test_conventional_directory_markers_remain_valid(self):
        archive = self.stage / "directory-markers.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            directory = tarfile.TarInfo("review-sensei/")
            directory.type = tarfile.DIRTYPE
            stream.addfile(directory)
            member = tarfile.TarInfo("review-sensei/payload")
            member.size = 1
            stream.addfile(member, io.BytesIO(b"x"))
        restored = CANDIDATE.unpack_native(
            archive, self.stage / "directory-markers", target="win32-x64"
        )
        self.assertEqual((restored / "payload").read_bytes(), b"x")

    def test_native_file_inventory_order_is_identical_under_windows_path_ordering(self):
        class WindowsOrderedPath(type(Path())):
            def __lt__(self, other):
                return str(self).casefold() < str(other).casefold()

        bundle = self.bundle("win32-x64")
        (bundle / "_internal/VCRUNTIME140.dll").write_bytes(b"uppercase runtime")
        actual = CANDIDATE.inventory(WindowsOrderedPath(bundle))
        expected = CANDIDATE.inventory(bundle)
        self.assertEqual(actual, expected)
        paths = [item["path"] for item in actual]
        self.assertEqual(paths, sorted(paths))

    def assert_isolated_npm_child(self, environment):
        self.assertIsNotNone(
            environment, "npm child inherited the entire ambient environment"
        )
        self.assertFalse(
            any(name.lower().startswith("npm_config_") for name in environment)
        )
        for name in (
            "NPM_TOKEN",
            "NODE_AUTH_TOKEN",
            "NODE_OPTIONS",
            "PRIVATE_SENTINEL",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PATH"], os.environ["PATH"])
        self.assertEqual(
            environment.get("SystemRoot", environment.get("SYSTEMROOT")),
            self.hostile_npm_environment()["SystemRoot"],
        )
        self.assertEqual(environment["LANG"], "C.UTF-8")
        self.assertNotEqual(environment["HOME"], os.environ.get("HOME"))

    def hostile_npm_environment(self):
        return {
            "NPM_TOKEN": "synthetic-sentinel",
            "NODE_AUTH_TOKEN": "synthetic-sentinel",
            "npm_config_proxy": "http://127.0.0.1:9",
            "NPM_CONFIG_GLOBAL": "true",
            "NODE_OPTIONS": "synthetic-sentinel",
            "PRIVATE_SENTINEL": "synthetic-sentinel",
            "SystemRoot": os.environ.get(
                "SystemRoot", os.environ.get("SYSTEMROOT", "synthetic-system-root")
            ),
            "LANG": "C.UTF-8",
        }

    @unittest.skipUnless(
        shutil.which("npm"), "npm is required for actual local tarball packing"
    )
    def test_actual_pack_children_receive_only_allowlisted_environment(self):
        native = self.archived()
        run = subprocess.run
        inspected = []

        def inspect(command, *args, **kwargs):
            if "pack" in command:
                self.assert_isolated_npm_child(kwargs.get("env"))
                inspected.append(command)
            return run(command, *args, **kwargs)

        with (
            patch.dict(os.environ, self.hostile_npm_environment()),
            patch.object(CANDIDATE.subprocess, "run", side_effect=inspect),
        ):
            CANDIDATE.assemble_candidate(
                self.root, native, self.stage / "isolated-pack", self.source
            )
        self.assertEqual(len(inspected), 6)

    @unittest.skipUnless(
        shutil.which("npm"), "npm is required for actual local tarball packing"
    )
    def test_install_child_receives_only_allowlisted_environment(self):
        packed = self.packed()
        run = subprocess.run

        class InspectedInstall(Exception):
            pass

        def inspect(command, *args, **kwargs):
            if "install" in command:
                self.assert_isolated_npm_child(kwargs.get("env"))
                self.assertIn("--offline", command)
                self.assertIn("--ignore-scripts", command)
                self.assertIn("--omit=optional", command)
                raise InspectedInstall
            return run(command, *args, **kwargs)

        with (
            patch.dict(os.environ, self.hostile_npm_environment()),
            patch.object(CANDIDATE, "validate_host_target"),
            patch.object(CANDIDATE.subprocess, "run", side_effect=inspect),
        ):
            with self.assertRaises(InspectedInstall):
                CANDIDATE.consume_candidate(
                    self.root,
                    packed,
                    "win32-x64",
                    self.stage / "isolated-install",
                    self.source,
                )

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
    def test_consumer_verifies_downloaded_bytes_before_installing(self):
        packed = self.packed()
        receipt = json.loads((packed / "candidate-packages.json").read_text())
        (packed / receipt["packages"][0]["file"]).write_bytes(b"changed after download")
        with (
            patch.object(CANDIDATE, "validate_host_target"),
            patch.object(CANDIDATE.subprocess, "run", wraps=subprocess.run) as runner,
        ):
            with self.assertRaisesRegex(CANDIDATE.CandidateError, "digest"):
                CANDIDATE.consume_candidate(
                    self.root,
                    packed,
                    "darwin-arm64",
                    self.stage / "consumer",
                    self.source,
                )
        self.assertFalse(
            any("install" in call.args[0] for call in runner.call_args_list)
        )

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
