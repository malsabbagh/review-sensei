"""Structural source diffs never manufacture resolution of durable criteria."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_review_run import EntryFixture

from tests.test_ordinary_cold_location_drift import CHILD
from tests.test_ordinary_native_recovery_cold import subprocess_environment

DELETE = (
    "diff --git a/src/app.py b/src/app.py\ndeleted file mode 100644\n"
    "--- a/src/app.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-keep\n-change\n"
)


def added(path):
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,2 @@\n+keep\n+change\n"


class OrdinaryColdStructuralDriftTests(unittest.TestCase):
    def journey(self, kind):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            f = EntryFixture(str(source), limit=512)
            f.active_finding_cap = 1
            original = {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Confirm the caller interface contract.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
                "symbol": "verify_token",
                "defect_kind": "missing_validation",
            }
            f.provider_comments = [original]
            self.assertEqual(f.invoke(), 0, f.error)
            if kind == "split":
                paths = ["src/first.py", "src/second.py"]
                diff = DELETE + "".join(added(p) for p in paths)
                comments = [dict(original, path=p) for p in paths]
            elif kind == "duplicate":
                paths = ["src/app.py", "src/second.py"]
                diff = (
                    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n keep\n-change\n+changed\n"
                    + added(paths[1])
                )
                comments = [dict(original, path=p) for p in paths]
            else:
                paths = []
                diff = DELETE
                comments = []
            packet = root / "packet.json"
            output = root / "result.json"
            child = root / "child"
            child.mkdir()
            packet.write_text(
                json.dumps(
                    {
                        "root": str(f.root),
                        "policy": f.policy,
                        "config": f.config_bytes.hex(),
                        "diff": diff,
                        "comments": comments,
                        "remote": {
                            k: getattr(f, k)
                            for k in (
                                "comment",
                                "extra_comments",
                                "reviews",
                                "inline",
                                "check_runs",
                                "trace",
                                "threads",
                            )
                        },
                    }
                )
            )
            done = subprocess.run(
                [sys.executable, "-c", CHILD, str(packet), str(child), str(output)],
                capture_output=True,
                text=True,
                env=subprocess_environment(),
                timeout=90,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["code"], 0, result["error"])
            before = result["initial"]["obligations"][0]
            self.assertEqual(len(result["state"]["obligations"]), 1)
            current = result["state"]["obligations"][0]
            criterion = dict(before["criterion"])
            if paths:
                criterion.update(blocking=True, needs_human=True)
            self.assertEqual(current["criterion"], criterion)
            self.assertIsNone(current["resolution_proof_sha256"])
            self.assertNotEqual(current["thread_binding"]["slot_state"], "released")
            self.assertEqual(current["thread_binding"]["thread_id"], f.threads[0]["id"])
            self.assertEqual(len(result["threads"]), 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in result["reviews"]))
            if paths:
                self.assertEqual(current["lifecycle_state"], "ambiguous-multi-target")
                self.assertEqual(
                    set(current["present_anchor"]["candidate_paths"]), set(paths)
                )
            else:
                self.assertEqual(current["lifecycle_state"], "omitted-uncertain")
                self.assertEqual(
                    current["present_anchor"]["drift_kind"], "line_deletion"
                )
                self.assertIsNone(current["present_anchor"]["path"])
                self.assertTrue(current["thread_binding"]["host_marked_outdated"])

    def test_deleted_file_cold_preserves_original_unresolved_obligation(self):
        self.journey("delete")

    def test_split_files_cold_hold_both_paths_without_duplicate_native_target(self):
        self.journey("split")

    def test_duplicate_block_cold_keeps_ambiguous_original_criterion(self):
        self.journey("duplicate")
