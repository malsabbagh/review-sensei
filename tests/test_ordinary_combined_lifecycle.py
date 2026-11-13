"""Actual hosted producer and cold reply consumer share only remote evidence."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_review_run import EntryFixture

from tests.test_ordinary_native_recovery_cold import subprocess_environment

CHILD = r"""
import copy,json,sys
from pathlib import Path
from unittest.mock import patch
from test_action_reply_entry import ReplyEntryFixture
from test_api_work_persistence import IDENTITY,HEAD
from review_sensei_github.publication import approval_eligibility_from_body
from review_sensei_github import finding_lifecycle as lifecycle
from review_sensei_github.session_ledger import parse_session_comment
from review_sensei.api_work import ApiRunAccounting
p=json.loads(Path(sys.argv[1]).read_text())
f=ReplyEntryFixture(sys.argv[2],limit=512)
f.root=Path(p['root']); f.policy=f.policy_ref=p['policy']; f.config_bytes=bytes.fromhex(p['config'])
for k,v in p['remote'].items():
    if k=='extra_comments': v={int(a):b for a,b in v.items()}
    setattr(f,k,copy.deepcopy(v))
f.run_id=457; f.trace=[]; f.live_head=f.requested_head=HEAD
f.prepare_reply(); f.thread_rows=copy.deepcopy(f.threads); f.threads=f.thread_rows
f.source['body']='@reviewsensei The caller contract is intentional; the added change is checked.'
e=approval_eligibility_from_body(f.reviews[-1]['body'])
assert e and e.human_review
visible=e.human_review.findings
f.reply_document={'body':'The explanation is supported by the current diff.','assessments':[{'fingerprint':x.fingerprint,'decision':'dismissed','rationale':'The current diff contains the intended added change.','human_evidence':'The caller contract is intentional','diff_evidence':'@@ -1 +1,2 @@\n keep\n+change'} for x in visible]}
if p.get('fault')=='deleted': f.threads.clear()
if p.get('fault')=='unknown':
    original_external=f.external
    def uncertain(request,timeout):
        from urllib.parse import urlsplit
        if urlsplit(request.full_url).path=='/graphql' and request.data and json.loads(request.data).get('operationName')=='ResolveReviewThread':
            f.trace.append((f.attempt,request.get_method(),'/graphql'))
            raise TimeoutError('synthetic unresolved native mutation')
        return original_external(request,timeout)
    f.external=uncertain
observed=[]; original=lifecycle.restore_finding_lifecycle
def capture(*a,**kw):
    result=original(*a,**kw); observed.append((a[0],result[1])); return result
with patch.object(lifecycle,'restore_finding_lifecycle',capture): code=f.invoke_reply()
record=parse_session_comment(f.comment['body'],identity=IDENTITY)
state=observed[-1][0]._drift_states.get((123,7)) if observed else None
state_summary=None if state is None else {'obligations':[{'id':o.obligation_id,'criterion':o.criterion.resolution_criterion,'status':o.lifecycle_state,'binding':str(o.thread_binding)} for o in state.inventory.obligations],'slots':[str(x) for x in state.slots]}

from review_sensei.finding_lifecycle import decode_lifecycle_state
initial=decode_lifecycle_state(next(doc for _,doc in observed if doc is not None)['lifecycle']).state
initial_pairs=[[o.obligation_id,o.criterion.resolution_criterion] for o in initial.inventory.obligations]
accounting=ApiRunAccounting.from_document(record.api_run_accounting)
result={'initial_pairs':initial_pairs,'limit':accounting.configuration.limit,'uncertain':accounting.uncertain,'code':code,'error':f.error,'provider_calls':f.provider_calls,'resolved':f.resolved_threads,'visible':len(visible),'last':str(f.last_result),'record':record.to_dict(),'remote':{k:getattr(f,k) for k in p['remote']},'restores':len(observed),'state':state_summary}
Path(sys.argv[3]).write_text(json.dumps(result))
"""


PROMOTE = r"""
import copy,json,sys
from pathlib import Path
from unittest.mock import patch
from test_review_run import EntryFixture
from test_api_work_persistence import IDENTITY,HEAD
from review_sensei_github import finding_lifecycle as lifecycle
from review_sensei_github.session_ledger import parse_session_comment
p=json.loads(Path(sys.argv[1]).read_text())
f=EntryFixture(sys.argv[2],limit=512)
f.root=Path(p['root']); f.policy=f.policy_ref=p['policy']; f.config_bytes=bytes.fromhex(p['config'])
for k,v in p['remote'].items():
    if k=='extra_comments': v={int(a):b for a,b in v.items()}
    setattr(f,k,copy.deepcopy(v))
f.run_id=p.get('run_id',458); f.trace=[]; f.live_head=f.requested_head=HEAD
f.active_finding_cap=1; f.provider_comments=[]
from test_review_run_rescan import request
if not p.get('replay'): request(f,9,'rescan')
else:
    def no_provider(): raise AssertionError('cold replay invoked inference')
    f.before_provider=no_provider
owners=[]; original=lifecycle.restore_finding_lifecycle
def capture(*a,**kw):
    result=original(*a,**kw); owners.append(a[0]); return result
with patch.object(lifecycle,'restore_finding_lifecycle',capture): code=f.invoke()
state=owners[-1]._drift_states.get((123,7)) if owners else None
summary=None if state is None else {'obligations':[{'id':o.obligation_id,'criterion':o.criterion.resolution_criterion,'status':o.lifecycle_state,'binding':str(o.thread_binding)} for o in state.inventory.obligations],'slots':[str(x) for x in state.slots]}
Path(sys.argv[3]).write_text(json.dumps({'code':code,'error':f.error,'provider_calls':f.provider_calls,'state':summary,'reviews':len(f.reviews),'threads':f.threads,'remote':{k:getattr(f,k) for k in p['remote']},'terminal':parse_session_comment(f.comment['body'],identity=IDENTITY).api_run_accounting}))
"""


class OrdinaryCombinedLifecycleTests(unittest.TestCase):
    def test_current_producer_cold_human_consumer_preserves_hidden_criterion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            f = EntryFixture(str(source), limit=512)
            f.active_finding_cap = 1
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": body,
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for body in (
                    "Confirm the caller interface contract.",
                    "Confirm the retention policy contract.",
                )
            ]
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(len(f.threads), 1)
            packet = root / "remote.json"
            packet.write_text(
                json.dumps(
                    {
                        "fault": getattr(self, "fault", None),
                        "root": str(f.root),
                        "policy": f.policy,
                        "config": f.config_bytes.hex(),
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
            child = root / "child"
            child.mkdir()
            output = root / "result.json"
            done = subprocess.run(
                [sys.executable, "-c", CHILD, str(packet), str(child), str(output)],
                capture_output=True,
                text=True,
                env=subprocess_environment(),
                timeout=90,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["limit"], 512)
            self.assertEqual(result["visible"], 1)
            self.assertEqual(
                result["initial_pairs"],
                [[x["id"], x["criterion"]] for x in result["state"]["obligations"]],
            )
            if getattr(self, "fault", None):
                self.assertEqual(result["resolved"], [], result["error"])
                if self.fault == "unknown":
                    self.assertGreater(result["uncertain"], 0)
                self.assertEqual(len(result["state"]["obligations"]), 2)
                self.assertFalse(
                    any(
                        "slot_state='released'" in x["binding"]
                        for x in result["state"]["obligations"]
                    )
                )
                return
            self.assertEqual(result["code"], 0, result)
            self.assertEqual(result["provider_calls"], 1)
            self.assertEqual(len(result["resolved"]), 1, result)
            original_packet = json.loads(packet.read_text())
            original_packet["remote"] = result["remote"]
            packet.write_text(json.dumps(original_packet))
            promoted_root = root / "promotion"
            promoted_root.mkdir()
            done = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    PROMOTE,
                    str(packet),
                    str(promoted_root),
                    str(output),
                ],
                capture_output=True,
                text=True,
                env=subprocess_environment(),
                timeout=90,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            promoted = json.loads(output.read_text())
            self.assertEqual(promoted["code"], 0, promoted)
            self.assertEqual(promoted["provider_calls"], 1)
            before = {x["id"]: x for x in result["state"]["obligations"]}
            after = {x["id"]: x for x in promoted["state"]["obligations"]}
            self.assertEqual(
                {k: v["criterion"] for k, v in before.items()},
                {k: v["criterion"] for k, v in after.items()},
            )
            released = [k for k, v in before.items() if v["status"] == "verified-fixed"]
            self.assertEqual(len(released), 1)
            hidden = next(k for k in before if k not in released)
            self.assertEqual(after[released[0]]["status"], "verified-fixed")
            self.assertEqual(after[hidden]["status"], "omitted-uncertain")
            self.assertIn("slot_state='active'", after[hidden]["binding"])
            self.assertEqual(sum(not x["isResolved"] for x in promoted["threads"]), 1)
            self.assertIsNotNone(promoted["terminal"]["terminal_transaction"])
            replay_packet = dict(
                original_packet, remote=promoted["remote"], replay=True, run_id=460
            )
            packet.write_text(json.dumps(replay_packet))
            replay_root = root / "replay"
            replay_root.mkdir()
            done = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    PROMOTE,
                    str(packet),
                    str(replay_root),
                    str(output),
                ],
                capture_output=True,
                text=True,
                env=subprocess_environment(),
                timeout=90,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            replay = json.loads(output.read_text())
            self.assertEqual(replay["code"], 0, replay["error"])
            self.assertEqual(replay["provider_calls"], 0)
            for name in ("reviews", "inline", "threads", "check_runs"):
                self.assertEqual(replay["remote"][name], promoted["remote"][name])

    def test_deleted_native_target_never_releases_original_obligation(self):
        self.fault = "deleted"
        self.test_current_producer_cold_human_consumer_preserves_hidden_criterion()

    def test_unknown_native_resolution_never_releases_original_obligation(self):
        self.fault = "unknown"
        self.test_current_producer_cold_human_consumer_preserves_hidden_criterion()
