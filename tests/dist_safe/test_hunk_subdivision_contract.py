"""Installed CLI processes, fixed provider files and prohibited sockets."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

# Only transport/observation ports are injected. All planning, execution,
# recovery, CLI parsing and normalization come from the installed distribution.
CHILD = r"""
import hashlib, json, runpy, socket, sys
from pathlib import Path
import review_sensei
import review_sensei.discovery_work as work
from review_sensei.providers.fixture import FixtureProvider
package = Path(review_sensei.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), (package, sys.prefix)
target = Path(sys.argv[1]); sys.argv = ["review-sensei", *sys.argv[2:]]
requests = {}; calls = []; plans = []
original_execute = work.execute_plan
def execute(plan, **kwargs):
    plans.append({"plan_id":plan.plan_id,"snapshot":plan.bundle.snapshot.to_dict(),
        "records":[record.to_dict() for record in plan.bundle.records],
        "record_ids":[record.evidence_id for record in plan.bundle.records],
        "fragment_bodies":[record.analysis.hunk_records[0].text.partition("\n")[2]
            for record in plan.bundle.records if record.fragment is not None],
        "unprocessed":plan.unprocessed})
    render = kwargs["render"]
    def traced_render(batch):
        request = render(batch)
        requests[hashlib.sha256(request.prompt.encode()).hexdigest()] = {
            "batch_id":batch.batch_id,"added_lines":[line for record in batch.records for line in record.analysis.changed_lines.get(record.path,())],
            "diff_bytes":len(batch.diff_context.encode())}
        return request
    kwargs["render"] = traced_render
    return original_execute(plan, **kwargs)
work.execute_plan = execute
original_complete = FixtureProvider.complete
def complete(self, request):
    digest=hashlib.sha256(request.prompt.encode()).hexdigest()
    calls.append({"prompt_sha256":digest,"prompt_bytes":len(request.prompt.encode()),**requests[digest]})
    return original_complete(self, request)
FixtureProvider.complete = complete
def deny(*args, **kwargs): raise AssertionError("live sockets are prohibited")
socket.socket = deny; socket.create_connection = deny
try: runpy.run_module("review_sensei",run_name="__main__")
finally: target.write_text(json.dumps({"package":str(package),"calls":calls,"plans":plans},sort_keys=True))
"""


class InstalledHunkSubdivisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def subdivision_plan(self, witness):
        plans = [
            plan
            for plan in witness["plans"]
            if any("fragment" in record for record in plan["records"])
        ]
        self.assertEqual(len(plans), 1, "expected one executed subdivision plan")
        self.assertTrue(plans[0]["records"])
        self.assertTrue(all("fragment" in record for record in plans[0]["records"]))
        return plans[0]

    def run_cli(
        self,
        *,
        lines=200,
        giant=False,
        calls=8,
        recovery=False,
        separator="",
        source_crlf=False,
    ):
        following = ("+addition", "-deletion", "\\ No newline at end of file")
        bodies = [
            "+"
            + f"line{index}:"
            + (separator + following[index % 3] if separator else "")
            + "x" * 290
            + ("\r\n" if source_crlf else "\n")
            for index in range(lines)
        ]
        if giant:
            bodies[0] = "+" + "x" * 200_000 + "\n"
        diff = self.root / "change.patch"
        diff.write_bytes(
            (
                "diff --git a/src/large.py b/src/large.py\n--- a/src/large.py\n+++ b/src/large.py\n"
                + f"@@ -0,0 +1,{lines} @@\n"
                + "".join(bodies)
            ).encode("utf-8")
        )
        response = self.root / "response.json"
        response.write_text(
            '{"summary":"Offline bounded witness.","comments":[],"learning_proposals":[]}',
            encoding="utf-8",
        )
        config = self.root / "config.yml"
        config.write_text(
            "schema: 1\ngithub:\n  artifacts: diagnostics\nadvanced:\n  review_work:\n    mode: unified\n  resources:\n    max_provider_calls: "
            + str(calls)
            + "\n",
            encoding="utf-8",
        )
        argv = [
            "--diff",
            str(diff),
            "--provider",
            "fixture",
            "--fixture-response",
            str(response),
            "--config",
            str(config),
            "--no-learning-proposals",
            "--format",
            "json",
            "--exit-semantics",
            "operational",
            "--base-sha",
            "a" * 40,
            "--head-sha",
            "b" * 40,
        ]
        if recovery:
            key = self.root / "private-key"
            key.write_bytes(b"k" * 32)
            key.chmod(0o600)
            argv.extend(
                (
                    "--work-recovery-dir",
                    str(self.root / "recovery"),
                    "--work-recovery-key-file",
                    str(key),
                )
            )
        witness = self.root / "witness.json"
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for name in ("SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL"):
            if name in os.environ:
                environment[name] = os.environ[name]
        completed = subprocess.run(
            [sys.executable, "-I", "-c", CHILD, str(witness), *argv],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return (
            json.loads(completed.stdout),
            json.loads(witness.read_text()),
            completed.stderr,
        )

    def test_large_hunk_uses_bounded_calls_with_exhaustive_current_lines(self):
        result, witness, _ = self.run_cli()
        self.assertEqual(result["review_status"], "complete")
        calls = witness["calls"]
        self.assertGreater(len(calls), 1)
        self.assertLessEqual(len(calls), 8)
        self.assertTrue(all(item["prompt_bytes"] <= 49_152 for item in calls))
        lines = [line for call in calls for line in call["added_lines"]]
        self.assertEqual(sorted(lines), list(range(1, 201)))
        self.assertEqual(len(lines), len(set(lines)))
        # Select by content even if an unrelated stage executes beforehand.
        witness["plans"].insert(0, {"records": [], "unprocessed": []})
        records = self.subdivision_plan(witness)["records"]
        intervals = sorted(
            (record["fragment"]["body_start"], record["fragment"]["body_end"])
            for record in records
        )
        cursor = 0
        for start, end in intervals:
            self.assertEqual(start, cursor)
            cursor = end
        self.assertEqual(cursor, records[0]["fragment"]["body_bytes"])
        self.assertTrue(
            all(record["snapshot"]["head_sha"] == "b" * 40 for record in records)
        )

    def test_small_hunk_retains_the_ordinary_evidence_identity(self):
        result, witness, _ = self.run_cli(lines=1)
        self.assertEqual(result["review_status"], "complete")
        self.assertEqual(len(witness["calls"]), 1)
        records = [record for plan in witness["plans"] for record in plan["records"]]
        self.assertEqual(len(records), 1)
        self.assertNotIn("fragment", records[0])
        # Golden identity independently obtained from the pre-subdivision base
        # for this fixed patch, snapshot and ordinary evidence provenance.
        self.assertEqual(
            [identity for plan in witness["plans"] for identity in plan["record_ids"]],
            ["5452374469fe4d69579380b583f60ecbee69de399233af5e0b2b2f50985e8393"],
        )

    def test_forced_subdivision_preserves_source_separators_and_crlf_bytes(self):
        separators = (
            "\r",
            "\v",
            "\f",
            "\x1c",
            "\x1d",
            "\x1e",
            "\x85",
            "\u2028",
            "\u2029",
        )
        cases = [(separator, False) for separator in separators] + [("", True)]
        for separator, source_crlf in cases:
            with self.subTest(separator=repr(separator), source_crlf=source_crlf):
                result, witness, _ = self.run_cli(
                    separator=separator, source_crlf=source_crlf
                )
                self.assertEqual(result["review_status"], "complete")
                self.assertGreater(len(witness["calls"]), 1)
                self.assertLessEqual(len(witness["calls"]), 8)
                self.assertTrue(
                    all(call["prompt_bytes"] <= 49_152 for call in witness["calls"])
                )
                observed = [
                    line for call in witness["calls"] for line in call["added_lines"]
                ]
                self.assertEqual(sorted(observed), list(range(1, 201)))
                self.assertEqual(len(observed), len(set(observed)))
                diff = (self.root / "change.patch").read_bytes()
                hunk = diff.split(b"\n", 3)[3]
                body = hunk.partition(b"\n")[2]
                boundaries = {0}
                cursor = 0
                for line in body.split(b"\n")[:-1]:
                    cursor += len(line) + 1
                    boundaries.add(cursor)
                plan = self.subdivision_plan(witness)
                bodies = [text.encode() for text in plan["fragment_bodies"]]
                # Plans may order records by evidence identity, so reassemble
                # by the authenticated source offsets rather than plan order.
                pieces = sorted(
                    zip(plan["records"], bodies, strict=True),
                    key=lambda item: item[0]["fragment"]["body_start"],
                )
                bodies = [piece for _, piece in pieces]
                self.assertEqual(b"".join(bodies), body)
                cursor = 0
                for record, piece in pieces:
                    proof = record["fragment"]
                    self.assertEqual(proof["body_start"], cursor)
                    self.assertIn(cursor, boundaries)
                    cursor += len(piece)
                    self.assertEqual(proof["body_end"], cursor)
                    self.assertIn(cursor, boundaries)
                    self.assertEqual(proof["body_bytes"], len(body))
                    self.assertEqual(
                        proof["body_sha256"], hashlib.sha256(piece).hexdigest()
                    )
                    self.assertEqual(
                        proof["source_hunk_sha256"], hashlib.sha256(hunk).hexdigest()
                    )
                self.assertEqual(cursor, len(body))

    def test_giant_line_is_retained_and_never_certified_as_complete(self):
        result, witness, _ = self.run_cli(lines=2, giant=True)
        self.assertEqual(result["review_status"], "partial")
        self.assertEqual(result["coverage"]["hunks"][0]["outcome"], "budget-exhausted")
        plan = self.subdivision_plan(witness)
        records = plan["records"]
        self.assertTrue(
            any(
                record["fragment"]["body_end"] - record["fragment"]["body_start"]
                == 200_002
                for record in records
            )
        )
        self.assertTrue(plan["unprocessed"])

    def test_one_call_does_not_mark_an_unvisited_segment_reviewed(self):
        result, witness, _ = self.run_cli(calls=1)
        self.assertEqual(result["review_status"], "partial")
        self.assertEqual(len(witness["calls"]), 1)
        self.assertEqual(result["coverage"]["hunks"][0]["outcome"], "budget-exhausted")

    def test_resume_reuses_authenticated_ranges_without_fresh_calls(self):
        first, first_witness, _ = self.run_cli(recovery=True)
        second, second_witness, _ = self.run_cli(recovery=True)
        self.assertEqual(second, first)
        self.assertGreater(len(first_witness["calls"]), 1)
        self.assertFalse(second_witness["calls"])
        self.assertEqual(second_witness["plans"], first_witness["plans"])

    def test_reassessment_fragments_cannot_clear_a_current_file_obligation(self):
        from review_sensei.budgets import ReviewWorkBudgets
        from review_sensei.errors import ReviewInputError
        from review_sensei.evidence import (
            EvidenceBundle,
            EvidenceRecord,
            EvidenceSnapshot,
        )
        from review_sensei.human_assessment import (
            HumanReviewFinding,
            PendingHumanReview,
        )
        from review_sensei.hunk_subdivision import subdivide_hunk
        from review_sensei.reassessment_work import reassess

        snapshot = EvidenceSnapshot("owner/repo", 1, "a" * 40, "b" * 40)
        source = EvidenceRecord("src/large.py", "@@ -1 +1 @@\n-old\n+new\n", snapshot)
        record = source.analysis.file_records[0]
        pieces = subdivide_hunk(
            record, record.hunks[0], snapshot, admit=lambda part: False
        )
        pending = PendingHumanReview(
            snapshot.base_sha,
            (
                HumanReviewFinding(
                    "f" * 64, source.path, "Verify complete current file behavior."
                ),
            ),
        )

        class Provider:
            name = "fixture"
            model = "fixture"

            def complete(self, request):
                raise AssertionError("fragments must not reach a decision provider")

        with self.assertRaises(ReviewInputError):
            reassess(
                provider=Provider(),
                pending=pending,
                bundle=EvidenceBundle(snapshot, pieces),
                source_body="Please assess the complete current evidence.",
                authority_digest="d" * 64,
                work_budgets=ReviewWorkBudgets(mode="unified"),
            )
        self.assertEqual(len(pending.pending), 1)


if __name__ == "__main__":
    unittest.main()
