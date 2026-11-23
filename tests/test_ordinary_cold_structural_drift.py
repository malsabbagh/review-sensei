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
            # Preserve original severity separately from the ADR 0134 ambiguity hold.
            self.assertEqual(current["criterion"], before["criterion"])
            self.assertIsNone(current["resolution_proof_sha256"])
            self.assertNotEqual(current["thread_binding"]["slot_state"], "released")
            self.assertEqual(current["thread_binding"]["thread_id"], f.threads[0]["id"])
            self.assertEqual(len(result["threads"]), 1)
            self.assertFalse(result["threads"][0]["isResolved"])
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

    def _run_cold_child(self, packet_path, child_dir, output_path):
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                CHILD,
                str(packet_path),
                str(child_dir),
                str(output_path),
            ],
            capture_output=True,
            text=True,
            env=subprocess_environment(),
            timeout=90,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(output_path.read_text())

    def test_partial_split_fix_cold_retains_ambiguous_original_criterion(self):
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

            paths = ["src/first.py", "src/second.py"]
            packet = root / "packet.json"
            output = root / "result.json"
            child_split = root / "split"
            child_split.mkdir()
            packet.write_text(
                json.dumps(
                    {
                        "root": str(f.root),
                        "policy": f.policy,
                        "config": f.config_bytes.hex(),
                        "diff": DELETE + "".join(added(p) for p in paths),
                        "comments": [dict(original, path=p) for p in paths],
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
            split_result = self._run_cold_child(packet, child_split, output)
            self.assertEqual(split_result["code"], 0, split_result["error"])
            initial_criterion = split_result["initial"]["obligations"][0]["criterion"]
            held = split_result["state"]["obligations"][0]
            self.assertEqual(held["lifecycle_state"], "ambiguous-multi-target")

            # Cold Round 3 fixes only `src/first.py` while omitting `src/second.py`:
            # must remain ambiguous-multi-target without resolving or approving.
            child_partial = root / "partial_split_fix"
            child_partial.mkdir()
            packet.write_text(
                json.dumps(
                    {
                        "root": str(f.root),
                        "policy": f.policy,
                        "config": f.config_bytes.hex(),
                        "run_id": 458,
                        "head": "e" * 40,
                        "diff": (
                            "diff --git a/src/first.py b/src/first.py\n"
                            "--- a/src/first.py\n+++ b/src/first.py\n"
                            "@@ -1,2 +1,2 @@\n keep\n-change\n+validate_token()\n"
                        ),
                        "comments": [],
                        "remote": split_result["remote"],
                    }
                )
            )
            partial = self._run_cold_child(packet, child_partial, output)
            self.assertEqual(partial["code"], 0, partial["error"])
            self.assertEqual(len(partial["state"]["obligations"]), 1)
            ob = partial["state"]["obligations"][0]
            self.assertEqual(ob["criterion"], initial_criterion)
            self.assertEqual(ob["lifecycle_state"], "ambiguous-multi-target")
            self.assertEqual(set(ob["present_anchor"]["candidate_paths"]), set(paths))
            self.assertIsNone(ob["resolution_proof_sha256"])
            self.assertNotEqual(ob["thread_binding"]["slot_state"], "released")
            self.assertEqual(len(partial["threads"]), 1)
            self.assertFalse(partial["threads"][0]["isResolved"])
            self.assertFalse(any(r["state"] == "APPROVED" for r in partial["reviews"]))

    def test_line_move_and_whole_file_rewrite_cold_preserve_original_criterion(self):
        cases = (
            (
                "line_move",
                (
                    "diff --git a/src/app.py b/src/app.py\n"
                    "--- a/src/app.py\n+++ b/src/app.py\n"
                    "@@ -1,2 +1,14 @@\n"
                    + "".join(f"+pad_{i}\n" for i in range(12))
                    + " keep\n-change\n+change()\n"
                ),
                lambda orig: [dict(orig, line=14)],
            ),
            (
                "whole_file_rewrite_present",
                (
                    "diff --git a/src/app.py b/src/app.py\n"
                    "--- a/src/app.py\n+++ b/src/app.py\n"
                    "@@ -1,4 +1,4 @@\n"
                    "-old1\n-old2\n-old3\n-old4\n"
                    "+new1\n+new2\n+new3\n+new4\n"
                ),
                lambda orig: [
                    dict(
                        orig,
                        line=3,
                        body="Rewritten handler still lacks token validation.",
                    )
                ],
            ),
            (
                "whole_file_rewrite_omitted",
                (
                    "diff --git a/src/app.py b/src/app.py\n"
                    "--- a/src/app.py\n+++ b/src/app.py\n"
                    "@@ -1,4 +1,4 @@\n"
                    "-old1\n-old2\n-old3\n-old4\n"
                    "+new1\n+new2\n+new3\n+new4\n"
                ),
                lambda _orig: [],
            ),
        )
        for name, diff, make_comments in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
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
                            "comments": make_comments(original),
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
                result = self._run_cold_child(packet, child, output)
                self.assertEqual(result["code"], 0, result["error"])
                initial_criterion = result["initial"]["obligations"][0]["criterion"]
                self.assertEqual(len(result["state"]["obligations"]), 1)
                ob = result["state"]["obligations"][0]
                self.assertEqual(ob["criterion"], initial_criterion)
                self.assertEqual(ob["thread_binding"]["thread_id"], f.threads[0]["id"])
                self.assertEqual(len(result["threads"]), 1)
                self.assertFalse(result["threads"][0]["isResolved"])
                self.assertIsNone(ob["resolution_proof_sha256"])
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in result["reviews"])
                )
                self.assertEqual(ob["present_anchor"]["path"], "src/app.py")
                if name == "line_move":
                    self.assertEqual(ob["present_anchor"]["line"], 14)
                    self.assertEqual(ob["thread_binding"]["slot_state"], "active")
                elif name == "whole_file_rewrite_present":
                    self.assertEqual(
                        ob["present_anchor"]["drift_kind"], "whole_file_rewrite"
                    )
                    self.assertEqual(ob["thread_binding"]["slot_state"], "active")
                else:
                    self.assertEqual(
                        ob["present_anchor"]["drift_kind"], "whole_file_rewrite"
                    )
                    self.assertEqual(ob["lifecycle_state"], "omitted-uncertain")
                    self.assertNotEqual(ob["thread_binding"]["slot_state"], "released")

    def test_mixed_drift_and_uncertainty_matrix_across_consecutive_cold_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            f = EntryFixture(str(source), limit=512)
            f.active_finding_cap = 2
            (f.root / "review.patch").write_bytes(
                (
                    "diff --git a/src/alpha.py b/src/alpha.py\n"
                    "--- a/src/alpha.py\n+++ b/src/alpha.py\n"
                    "@@ -1,2 +1,2 @@\n keep\n-old\n+change\n"
                    "diff --git a/src/beta.py b/src/beta.py\n"
                    "--- a/src/beta.py\n+++ b/src/beta.py\n"
                    "@@ -1,2 +1,2 @@\n keep\n-old\n+change\n"
                ).encode("utf-8")
            )
            alpha = {
                "path": "src/alpha.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Confirm alpha caller contract.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
                "symbol": "alpha_check",
                "defect_kind": "missing_validation",
            }
            beta = {
                "path": "src/beta.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Must enforce beta auth boundary.",
                "severity": "critical",
                "blocking": True,
                "needs_human": False,
                "symbol": "beta_guard",
                "defect_kind": "auth_bypass",
            }
            f.provider_comments = [alpha, beta]
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(len(f.threads), 2)

            # Cold Round 2: alpha undergoes clean file_rename while beta's file is
            # deleted (`omitted-uncertain`). Both criteria and threads remain open.
            packet = root / "packet.json"
            output = root / "result.json"
            child_r2 = root / "round2"
            child_r2.mkdir()
            r2_diff = (
                "diff --git a/src/alpha.py b/src/alpha_renamed.py\n"
                "similarity index 90%\n"
                "rename from src/alpha.py\n"
                "rename to src/alpha_renamed.py\n"
                "--- a/src/alpha.py\n+++ b/src/alpha_renamed.py\n"
                "@@ -1,2 +1,2 @@\n keep\n-change\n+changed\n"
                "diff --git a/src/beta.py b/src/beta.py\n"
                "deleted file mode 100644\n"
                "--- a/src/beta.py\n+++ /dev/null\n"
                "@@ -1,2 +0,0 @@\n-keep\n-change\n"
            )
            packet.write_text(
                json.dumps(
                    {
                        "root": str(f.root),
                        "policy": f.policy,
                        "config": f.config_bytes.hex(),
                        "cap": 2,
                        "diff": r2_diff,
                        "comments": [dict(alpha, path="src/alpha_renamed.py")],
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
            r2 = self._run_cold_child(packet, child_r2, output)
            self.assertEqual(r2["code"], 0, r2["error"])
            initial_by_path = {
                ob["criterion"]["origin_path"]: ob
                for ob in r2["initial"]["obligations"]
            }
            self.assertEqual(set(initial_by_path), {"src/alpha.py", "src/beta.py"})
            r2_by_origin = {
                ob["criterion"]["origin_path"]: ob for ob in r2["state"]["obligations"]
            }
            self.assertEqual(len(r2_by_origin), 2)
            r2_alpha = r2_by_origin["src/alpha.py"]
            r2_beta = r2_by_origin["src/beta.py"]
            self.assertEqual(
                r2_alpha["criterion"], initial_by_path["src/alpha.py"]["criterion"]
            )
            self.assertEqual(r2_alpha["present_anchor"]["path"], "src/alpha_renamed.py")
            self.assertEqual(r2_alpha["present_anchor"]["drift_kind"], "file_rename")
            self.assertEqual(r2_alpha["thread_binding"]["slot_state"], "active")
            self.assertEqual(
                r2_beta["criterion"], initial_by_path["src/beta.py"]["criterion"]
            )
            self.assertEqual(r2_beta["lifecycle_state"], "omitted-uncertain")
            self.assertEqual(r2_beta["present_anchor"]["drift_kind"], "line_deletion")
            self.assertIsNone(r2_beta["resolution_proof_sha256"])
            self.assertEqual(len(r2["threads"]), 2)
            self.assertTrue(all(not t["isResolved"] for t in r2["threads"]))
            self.assertFalse(any(r["state"] == "APPROVED" for r in r2["reviews"]))

            # Cold Round 3: renamed alpha is copied (`ambiguous-multi-target`) while
            # beta remains uncertain. Both original criteria, threads, and holds survive.
            child_r3 = root / "round3"
            child_r3.mkdir()
            r3_diff = (
                "diff --git a/src/alpha_renamed.py b/src/alpha_copy.py\n"
                "similarity index 90%\n"
                "copy from src/alpha_renamed.py\n"
                "copy to src/alpha_copy.py\n"
                "--- a/src/alpha_renamed.py\n+++ b/src/alpha_copy.py\n"
                "@@ -1,2 +1,2 @@\n keep\n-changed\n+copied\n"
            )
            packet.write_text(
                json.dumps(
                    {
                        "root": str(f.root),
                        "policy": f.policy,
                        "config": f.config_bytes.hex(),
                        "cap": 2,
                        "run_id": 458,
                        "head": "e" * 40,
                        "diff": r3_diff,
                        "comments": [
                            dict(alpha, path="src/alpha_renamed.py"),
                            dict(alpha, path="src/alpha_copy.py"),
                        ],
                        "remote": r2["remote"],
                    }
                )
            )
            r3 = self._run_cold_child(packet, child_r3, output)
            self.assertEqual(r3["code"], 0, r3["error"])
            r3_by_origin = {
                ob["criterion"]["origin_path"]: ob for ob in r3["state"]["obligations"]
            }
            self.assertEqual(len(r3_by_origin), 2)
            r3_alpha = r3_by_origin["src/alpha.py"]
            r3_beta = r3_by_origin["src/beta.py"]
            self.assertEqual(
                r3_alpha["criterion"], initial_by_path["src/alpha.py"]["criterion"]
            )
            self.assertEqual(r3_alpha["lifecycle_state"], "ambiguous-multi-target")
            self.assertEqual(
                set(r3_alpha["present_anchor"]["candidate_paths"]),
                {"src/alpha_renamed.py", "src/alpha_copy.py"},
            )
            self.assertEqual(
                r3_beta["criterion"], initial_by_path["src/beta.py"]["criterion"]
            )
            self.assertEqual(r3_beta["lifecycle_state"], "omitted-uncertain")
            self.assertIsNone(r3_alpha["resolution_proof_sha256"])
            self.assertIsNone(r3_beta["resolution_proof_sha256"])
            self.assertEqual(len(r3["threads"]), 2)
            self.assertTrue(all(not t["isResolved"] for t in r3["threads"]))
            self.assertFalse(any(r["state"] == "APPROVED" for r in r3["reviews"]))
