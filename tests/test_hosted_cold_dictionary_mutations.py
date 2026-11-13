"""Cold authenticated dictionary reader corruption, not end-to-end commands.

The existing FindingDictionaryRestartTests producer predates this slice and has
an original 1024-call configuration. These negatives do not qualify ordinary
512-call publication. The bootstrap hook explicitly invokes the authenticated
reader; the pending command otherwise holds before ordinary consumption. No
optional caller-supplied archive_chain is injected.
"""

import base64
import json
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from review_sensei_github.session_ledger import parse_session_comment

from tests.test_action_run import IDENTITY
from tests.test_ordinary_native_recovery_cold import subprocess_environment

CONSUME = r"""
import json,sys,tempfile
from pathlib import Path
from tests.test_action_run import CommandEntryFixture
from tests.test_finding_dictionary_restart import FIELDS,save_remote
from review_sensei_github import ReviewPublisher
from review_sensei_github.finding_lifecycle import restore_finding_lifecycle
from review_sensei.finding_lifecycle_store import select_finding_lifecycle
root=Path(sys.argv[1]);saved=json.loads((root/'remote.json').read_text())
with tempfile.TemporaryDirectory() as fresh:
    f=CommandEntryFixture(fresh)
    for name in FIELDS: setattr(f,name,saved[name])
    f.root=f.directory=Path(saved['fixture_root'])
    f.extra_comments={int(k):v for k,v in f.extra_comments.items()}
    f.past_jobs={int(k):v for k,v in f.past_jobs.items()}
    f.resolved_threads=set(saved['resolved_threads']);f.attempt=2
    observation={}
    def inspect(run):
        observation['limit']=run.caller.accounting.configuration.limit
        selected,stored=select_finding_lifecycle(run.ledger,run.identity)
        observation['selected_carrier']=stored['schema_version']
        reviewer=ReviewPublisher(http=run.http)
        record,document=restore_finding_lifecycle(reviewer,run.ledger,run.identity,loaded=selected)
        observation['restored']=document is not None
    f.after_action_bootstrap=inspect
    code=f.invoke_command()
    save_remote(f,root/'after.json')
    observation.update(code=code,error=f.error,trace=f.trace)
    (root/'outcome.json').write_text(json.dumps(observation))
"""


class HostedColdDictionaryMutationTests(unittest.TestCase):
    def test_required_original_dictionary_corruption_holds_without_repost(self):
        self._assert_mutations("dictionary")

    def test_required_outer_lifecycle_corruption_holds_without_repost(self):
        self._assert_mutations("lifecycle")

    def _assert_mutations(self, target_kind):
        for mode in ("deleted", "edited", "foreign-producer"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                produced = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.test_finding_dictionary_restart",
                        "produce",
                        str(root),
                    ],
                    env=subprocess_environment(),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(
                    produced.returncode, 77, produced.stdout + produced.stderr
                )
                packet = root / "remote.json"
                before = json.loads(packet.read_text())
                original = parse_session_comment(
                    before["comment"]["body"], identity=IDENTITY
                )
                # Decode only to locate the mutation target. This offline value is
                # never supplied to the consumer as authority or a lifecycle.
                outer = original.finding_lifecycle["manifest"]
                packed = b"".join(
                    base64.b64decode(
                        json.loads(
                            before["extra_comments"][part["storage_id"]]["body"]
                            .split("```json\n", 1)[1]
                            .split("\n```", 1)[0]
                        )["data"]
                    )
                    for part in outer["parts"]
                )
                carrier = json.loads(zlib.decompress(packed))["lifecycle"]
                self.assertEqual(
                    carrier["schema_version"], "finding-lifecycle-packed-v4"
                )
                manifest = (
                    carrier["dictionary_manifest"]
                    if target_kind == "dictionary"
                    else outer
                )
                self.assertEqual(manifest["encoding"], "partitioned-json-v1")
                target = str(manifest["parts"][0]["storage_id"])
                self.assertIn(target, before["extra_comments"])
                self.assertEqual(len(before["resolved_threads"]), 1)
                if mode == "deleted":
                    del before["extra_comments"][target]
                elif mode == "edited":
                    before["extra_comments"][target]["body"] += (
                        "\nHuman replaced required authority."
                    )
                else:
                    before["extra_comments"][target]["user"] = {
                        "id": 123456,
                        "type": "User",
                        "login": "foreign",
                    }
                packet.write_text(json.dumps(before))
                cold = subprocess.run(
                    [sys.executable, "-c", CONSUME, str(root)],
                    env=subprocess_environment(),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(cold.returncode, 0, cold.stdout + cold.stderr)
                outcome = json.loads((root / "outcome.json").read_text())
                after = json.loads((root / "after.json").read_text())
                self.assertNotEqual(outcome["code"], 0, outcome)
                self.assertFalse(outcome.get("restored"), outcome)
                if target_kind == "dictionary":
                    self.assertEqual(
                        outcome.get("selected_carrier"),
                        "finding-lifecycle-packed-v4",
                        outcome,
                    )
                else:
                    self.assertNotIn("selected_carrier", outcome)
                self.assertEqual(outcome.get("limit"), 1024, outcome)
                retained = parse_session_comment(
                    after["comment"]["body"], identity=IDENTITY
                )
                self.assertEqual(retained.finding_lifecycle, original.finding_lifecycle)
                self.assertEqual(retained.transaction, original.transaction)
                self.assertEqual(retained.dispositions, original.dispositions)
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
                self.assertEqual(after["extra_comments"], before["extra_comments"])
                self.assertEqual(before["provider_calls"], 1)
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in after["reviews"])
                )
                self.assertRegex(
                    outcome["error"], "(?i)(evidence|part|producer|artifact|dictionary)"
                )
