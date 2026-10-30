"""Historical probes must exercise pinned readers without modern import leakage."""

from __future__ import annotations

import importlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "check_partition_reader_compatibility",
    "check_eligibility_v4_reader_compatibility",
)


def load_probe(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HistoricalReaderProbeTests(unittest.TestCase):
    def test_missing_pinned_objects_fail_before_fixture_imports_without_fetch(self):
        for name in SCRIPTS:
            with self.subTest(script=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(
                    ["git", "init", "--quiet", str(root)],
                    check=True,
                    capture_output=True,
                )
                script = root / "scripts" / (name + ".py")
                script.parent.mkdir()
                script.write_bytes((ROOT / "scripts" / script.name).read_bytes())
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(script)],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(
                    "pinned source 5a121dfe39fb978c7d71def0d08dc570aad83f1d is unavailable locally",
                    result.stderr,
                )
                self.assertLess(len(result.stderr), 200)
                self.assertNotIn("Traceback", result.stderr)

    def test_partial_pin_set_reports_later_missing_pin_and_complete_set_passes(self):
        for name in SCRIPTS:
            probe = load_probe(name)
            with self.subTest(script=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(
                    ["git", "init", "--quiet", str(root)],
                    check=True,
                    capture_output=True,
                )
                tree = (
                    subprocess.run(
                        ["git", "mktree"],
                        cwd=root,
                        input=b"",
                        check=True,
                        capture_output=True,
                    )
                    .stdout.decode()
                    .strip()
                )
                pins = []
                for index in range(2):
                    document = (
                        f"tree {tree}\nauthor Synthetic <synthetic@example.test> 0 +0000\n"
                        f"committer Synthetic <synthetic@example.test> 0 +0000\n\npin {index}\n"
                    ).encode()
                    command = ["git", "hash-object", "-t", "commit", "--stdin"]
                    digest = (
                        subprocess.run(
                            command,
                            cwd=root,
                            input=document,
                            check=True,
                            capture_output=True,
                        )
                        .stdout.decode()
                        .strip()
                    )
                    pins.append((digest, document))
                subprocess.run(
                    ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                    cwd=root,
                    input=pins[0][1],
                    check=True,
                    capture_output=True,
                )
                overrides = {"ROOT": root, "READERS": tuple(p[0] for p in pins)}
                if hasattr(probe, "QUEUE_READER"):
                    overrides["QUEUE_READER"] = pins[0][0]
                error = io.StringIO()
                with patch.multiple(probe, **overrides), redirect_stderr(error):
                    self.assertEqual(probe.main(), 1)
                    self.assertIn("pinned source " + pins[1][0], error.getvalue())
                    self.assertNotIn("pinned source " + pins[0][0], error.getvalue())
                    self.assertLess(len(error.getvalue()), 200)
                    subprocess.run(
                        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                        cwd=root,
                        input=pins[1][1],
                        check=True,
                        capture_output=True,
                    )
                    error.seek(0)
                    error.truncate()
                    self.assertTrue(probe.pinned_sources_available())
                    self.assertEqual(error.getvalue(), "")

    def test_module_origin_assertion_rejects_current_or_unknown_implementation(self):
        for name in SCRIPTS:
            probe = load_probe(name)
            for origin in (__file__, None):
                module = ModuleType("synthetic")
                module.__file__ = origin
                with (
                    self.subTest(script=name, origin=origin),
                    self.assertRaisesRegex(RuntimeError, "outside archived source"),
                ):
                    probe.assert_archived_origins(Path("/tmp/archived-source"), module)

    def test_worker_refuses_wrong_origin_before_reading_fixture(self):
        for name in SCRIPTS:
            probe = load_probe(name)
            package = importlib.import_module("review_sensei.hosting.github")
            modules = {}
            for suffix in ("http", "session_ledger", "errors", "publication"):
                module = ModuleType("review_sensei.hosting.github." + suffix)
                module.__file__ = __file__
                modules[module.__name__] = module
            modules["review_sensei.hosting.github.http"].GitHubHttp = object
            modules[
                "review_sensei.hosting.github.session_ledger"
            ].GitHubIssueCommentSessionLedger = object
            modules[
                "review_sensei.hosting.github.errors"
            ].GitHubPublicationError = RuntimeError
            modules[
                "review_sensei.hosting.github.publication"
            ].ReviewApprovalFinalizer = object
            with (
                self.subTest(script=name),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(sys.modules, modules),
                patch.object(
                    package,
                    "http",
                    modules["review_sensei.hosting.github.http"],
                    create=True,
                ),
                patch.object(
                    package,
                    "session_ledger",
                    modules["review_sensei.hosting.github.session_ledger"],
                    create=True,
                ),
                patch.object(
                    package,
                    "publication",
                    modules["review_sensei.hosting.github.publication"],
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "outside archived source"):
                    probe.worker(
                        Path(directory), Path(directory) / "unread-fixture.json"
                    )

    def test_real_archived_workers_report_verified_source_and_refuse_latest_authority(
        self,
    ):
        for name in SCRIPTS:
            probe = load_probe(name)
            readers = (
                (*probe.READERS, probe.QUEUE_READER)
                if hasattr(probe, "QUEUE_READER")
                else probe.READERS
            )
            for commit in readers:
                result = subprocess.run(
                    ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                    cwd=ROOT,
                    capture_output=True,
                )
                if result.returncode:
                    self.skipTest(
                        "pinned objects absent; schema lane acquires and runs the historical probes"
                    )
            with self.subTest(script=name), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(ROOT / "scripts" / (name + ".py")),
                    ],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(len(lines), 5 if "partition" in name else 4)
                for commit in readers:
                    self.assertIn(commit + "/src", result.stdout)
                for line in lines:
                    self.assertTrue(
                        line.startswith("PASS verified archived source "), line
                    )
                    self.assertIn("withholds", line)

    def test_commit_present_but_archived_tree_missing_fails_with_bounded_diagnostic(
        self,
    ):
        for name in SCRIPTS:
            probe = load_probe(name)
            commit = probe.READERS[0]
            original = subprocess.run(
                ["git", "cat-file", "commit", commit], cwd=ROOT, capture_output=True
            )
            if original.returncode:
                self.skipTest(
                    "pinned object absent; schema lane acquires the historical objects"
                )
            with self.subTest(script=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(
                    ["git", "init", "--quiet", str(root)],
                    check=True,
                    capture_output=True,
                )
                written = subprocess.run(
                    ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                    cwd=root,
                    input=original.stdout,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(written.stdout.decode().strip(), commit)
                error = io.StringIO()
                with patch.object(probe, "ROOT", root), redirect_stderr(error):
                    self.assertEqual(
                        subprocess.run(
                            ["git", "cat-file", "-e", commit],
                            cwd=root,
                            capture_output=True,
                        ).returncode,
                        0,
                    )
                    self.assertIsNone(probe.archive_source(commit))
                self.assertIn(
                    "pinned source " + commit + " is unavailable locally",
                    error.getvalue(),
                )
                self.assertNotIn("Traceback", error.getvalue())
                self.assertLess(len(error.getvalue()), 200)
