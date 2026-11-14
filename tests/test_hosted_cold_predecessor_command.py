"""Actual pre-entry crash loses only process caches, not predecessor authority."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from tests.test_action_run import IDENTITY
from tests.test_hosted_cold_authority_command import CONSUME, PRODUCE
from tests.test_ordinary_native_recovery_cold import subprocess_environment

CRASH = (
    PRODUCE
    + r"""
import os
f.SOURCE_ID+=1;f.select_command('@reviewsensei verify',new_run=True)
def crash(run):
    save_remote(f,root/'original.json')
    os._exit(77)
f.after_action_bootstrap=crash
f.invoke_command()
raise AssertionError('bootstrap crash was not reached')
"""
)
RESUME = (
    CONSUME.replace(
        "f.SOURCE_ID+=1;f.select_command('@reviewsensei verify',new_run=True)",
        "f.attempt=2",
    )
    .replace("calls=[]", "calls=[];predecessors=[]")
    .replace(
        "calls.append([frame.f_code.co_filename,frame.f_code.co_name])",
        "calls.append([frame.f_code.co_filename,frame.f_code.co_name])\n            if frame.f_code.co_name=='read_previous_run': predecessors.append(json.loads(json.dumps(frame.f_locals['reference'])))",
    )
    .replace("'calls':calls", "'calls':calls,'predecessors':predecessors")
)


class HostedColdPredecessorCommandTests(unittest.TestCase):
    def phase(self, script, root, expected=0):
        done = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            env=subprocess_environment(),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(done.returncode, expected, done.stdout + done.stderr)

    def test_cold_command_requires_original_predecessor_after_preentry_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.phase(CRASH, root, 77)
            packet = json.loads((root / "original.json").read_text())
            original = parse_session_comment(
                packet["comment"]["body"], identity=IDENTITY
            )
            accounting = ApiRunAccounting.from_document(original.api_run_accounting)
            reference = accounting.previous_record
            self.assertIsNotNone(reference)
            target = reference["manifest"]["parts"][0]["storage_id"]
            self.assertIn(target, packet["extra_comments"])
            self.assertFalse(any(r["state"] == "APPROVED" for r in packet["reviews"]))
            for mode in ("unchanged", "deleted", "edited", "foreign-producer"):
                with self.subTest(mode=mode):
                    before = json.loads(json.dumps(packet))
                    if mode == "deleted":
                        del before["extra_comments"][target]
                    elif mode == "edited":
                        before["extra_comments"][target]["body"] += (
                            "\nHuman changed predecessor."
                        )
                    elif mode == "foreign-producer":
                        before["extra_comments"][target]["user"] = {
                            "id": 123456,
                            "type": "User",
                            "login": "foreign",
                        }
                    (root / "remote.json").write_text(json.dumps(before))
                    self.phase(RESUME, root)
                    outcome = json.loads((root / "outcome.json").read_text())
                    after = json.loads((root / "after.json").read_text())
                    self.assertEqual(outcome["limit"], 1024)
                    self.assertTrue(
                        any(
                            name == "read_previous_run" for _, name in outcome["calls"]
                        ),
                        outcome,
                    )
                    self.assertIn(reference, outcome["predecessors"])
                    if mode == "unchanged":
                        self.assertEqual(outcome["code"], 0, outcome)
                    else:
                        self.assertNotEqual(outcome["code"], 0, outcome)
                    retained = parse_session_comment(
                        after["comment"]["body"], identity=IDENTITY
                    )
                    current = ApiRunAccounting.from_document(
                        retained.api_run_accounting
                    )
                    self.assertEqual(current.configuration, accounting.configuration)
                    self.assertEqual(current.identity, accounting.identity)
                    self.assertEqual(current.previous_record, reference)
                    self.assertEqual(
                        current.blocks[: len(accounting.blocks)], accounting.blocks
                    )
                    if mode == "unchanged":
                        self.assertEqual(current.action["phase"], "terminal")
                    else:
                        self.assertEqual(
                            retained.finding_lifecycle, original.finding_lifecycle
                        )
                        self.assertRegex(
                            outcome["error"], "(?i)(partition|evidence|carrier|owner)"
                        )
                    self.assertEqual(retained.dispositions, original.dispositions)
                    self.assertEqual(retained.transaction, original.transaction)
                    for field in (
                        "reviews",
                        "inline",
                        "threads",
                        "check_runs",
                        "resolved_threads",
                        "provider_calls",
                        "review_put_attempts",
                    ):
                        self.assertEqual(
                            after[field], before[field], (mode, field, outcome)
                        )
                    self.assertEqual(before["provider_calls"], 1)
