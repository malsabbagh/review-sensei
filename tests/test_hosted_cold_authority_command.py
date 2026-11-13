"""Unmodified cold verify consumes actual retained lifecycle authority.

Uses the pre-existing 20-finding fixture's original 1024-call allowance, not a
replacement for ordinary512 qualification. Profiling observes calls only; no
bootstrap hook invokes readers on behalf of the command.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from review_sensei_github.session_ledger import parse_session_comment

from tests.test_action_run import IDENTITY
from tests.test_ordinary_native_recovery_cold import subprocess_environment

PRODUCE = r"""
import sys
from pathlib import Path
from tests.test_action_run import HEAD,CommandEntryFixture
from tests.test_finding_dictionary_restart import save_remote
from review_sensei_github.publication import parse_finding_index
root=Path(sys.argv[1]);(root/'producer').mkdir();f=CommandEntryFixture(root/'producer')
f.provider_comments=[{'path':'src/app.py','line':2,'side':'RIGHT','body':f'Confirm interface assumption {i}.','severity':'high','blocking':False,'needs_human':True} for i in range(20)]
assert f.invoke()==0,f.error
index=parse_finding_index(f.reviews[-1]['body'],repository_id=123,pull_request=7,head_sha=HEAD)
f.SOURCE_ID+=1;f.select_command('@reviewsensei override '+' '.join('RS-'+i.instance[:6].upper() for i in index.entries[:10])+' acceptable risk',new_run=True)
assert f.invoke_command()==0,f.error
save_remote(f,root/'original.json')
"""

CONSUME = r"""
import sys,json,shutil,tempfile
from pathlib import Path
from tests.test_action_run import CommandEntryFixture
from tests.test_finding_dictionary_restart import FIELDS,save_remote
root=Path(sys.argv[1]);saved=json.loads((root/'remote.json').read_text())
with tempfile.TemporaryDirectory() as fresh:
    f=CommandEntryFixture(fresh)
    for name in FIELDS:setattr(f,name,saved[name])
    copied=Path(fresh)/'original';shutil.copytree(saved['fixture_root'],copied)
    f.root=f.directory=copied
    f.extra_comments={int(k):v for k,v in f.extra_comments.items()}
    f.past_jobs={int(k):v for k,v in f.past_jobs.items()}
    f.resolved_threads=set(saved['resolved_threads'])
    f.SOURCE_ID+=1;f.select_command('@reviewsensei verify',new_run=True)
    calls=[]
    def observe(frame,event,arg):
        if event=='call' and frame.f_code.co_name in {'restore_finding_lifecycle','select_finding_lifecycle','read_previous_run'}:
            calls.append([frame.f_code.co_filename,frame.f_code.co_name])
    sys.setprofile(observe)
    try: code=f.invoke_command()
    finally: sys.setprofile(None)
    save_remote(f,root/'after.json')
    (root/'outcome.json').write_text(json.dumps({'code':code,'error':f.error,'calls':calls,'trace':f.trace,'limit':f.accounted.accounting.configuration.limit}))
"""


class HostedColdAuthorityCommandTests(unittest.TestCase):
    def phase(self, script, root):
        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            env=subprocess_environment(),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_verify_rejects_required_lifecycle_carrier_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.phase(PRODUCE, root)
            original_packet = json.loads((root / "original.json").read_text())
            original = parse_session_comment(
                original_packet["comment"]["body"], identity=IDENTITY
            )
            self.assertEqual(original.api_run_accounting["action"]["phase"], "terminal")
            self.assertEqual(len(original_packet["resolved_threads"]), 10)
            manifest = original.finding_lifecycle["manifest"]
            self.assertEqual(manifest["encoding"], "partitioned-json-v1")
            target = manifest["parts"][0]["storage_id"]
            for mode in ("unchanged", "deleted", "edited", "foreign-producer"):
                with self.subTest(mode=mode):
                    before = json.loads(json.dumps(original_packet))
                    if mode == "deleted":
                        del before["extra_comments"][target]
                    elif mode == "edited":
                        before["extra_comments"][target]["body"] += (
                            "\nHuman replaced authority."
                        )
                    elif mode == "foreign-producer":
                        before["extra_comments"][target]["user"] = {
                            "id": 123456,
                            "type": "User",
                            "login": "foreign",
                        }
                    (root / "remote.json").write_text(json.dumps(before))
                    self.phase(CONSUME, root)
                    after = json.loads((root / "after.json").read_text())
                    outcome = json.loads((root / "outcome.json").read_text())
                    self.assertEqual(outcome["limit"], 1024)
                    if mode == "unchanged":
                        self.assertEqual(outcome["code"], 0, outcome)
                    else:
                        self.assertNotEqual(outcome["code"], 0, outcome)
                        self.assertRegex(
                            outcome["error"], "(?i)(partition|evidence|carrier|owner)"
                        )
                    self.assertTrue(
                        any(
                            name == "select_finding_lifecycle"
                            for _, name in outcome["calls"]
                        ),
                        outcome,
                    )
                    retained = parse_session_comment(
                        after["comment"]["body"], identity=IDENTITY
                    )
                    if mode == "unchanged":
                        # Successful verification appends semantic history; its
                        # source inventory and criterion bytes do not change.
                        current_manifest = retained.finding_lifecycle["manifest"]
                        self.assertEqual(current_manifest["sha256"], manifest["sha256"])
                        self.assertEqual(
                            current_manifest["item_count"], manifest["item_count"]
                        )
                        for key, value in manifest["binding"].items():
                            if key != "generation":
                                self.assertEqual(
                                    current_manifest["binding"][key], value
                                )
                        self.assertGreaterEqual(
                            current_manifest["binding"]["generation"],
                            manifest["binding"]["generation"],
                        )
                    else:
                        self.assertEqual(
                            retained.finding_lifecycle, original.finding_lifecycle
                        )
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
                    self.assertEqual(before["provider_calls"], 1)
                    self.assertFalse(
                        any(r["state"] == "APPROVED" for r in after["reviews"])
                    )
