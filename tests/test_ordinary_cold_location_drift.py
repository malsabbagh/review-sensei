"""Real diff-derived cold location drift, without supplied drift descriptors."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from review_sensei_github.publication import NativeFindingReceipt
from test_review_run import EntryFixture

from review_sensei.bounded_evidence import canonical_bytes
from tests.test_ordinary_native_recovery_cold import subprocess_environment


class ColdDriftEntryFixture(EntryFixture):
    """Return the real complete, head-selected check census HTTP shape."""

    def external(self, request, timeout):
        from fake_github_http import json_response

        path = urlsplit(request.full_url).path
        if (
            request.get_method() == "GET"
            and "/commits/" in path
            and path.endswith("/check-runs")
        ):
            self.trace.append((self.attempt, request.get_method(), path))
            head = path.split("/commits/", 1)[1].split("/", 1)[0]
            rows = [
                copy.deepcopy(row) for row in self.check_runs if row["head_sha"] == head
            ]
            return json_response({"check_runs": rows, "total_count": len(rows)})
        return super().external(request, timeout)


CHILD = r"""
import copy,dis,inspect,json,sys
from dataclasses import asdict
from pathlib import Path
from tests.test_ordinary_cold_location_drift import ColdDriftEntryFixture
from review_sensei_github import finding_lifecycle as lifecycle
from review_sensei_github.session_ledger import parse_session_comment
from review_sensei.finding_lifecycle import decode_lifecycle_state
from test_api_work_persistence import IDENTITY
p=json.loads(Path(sys.argv[1]).read_text())
f=ColdDriftEntryFixture(sys.argv[2],limit=512)
f.root=Path(p['root']);f.policy=f.policy_ref=p['policy'];f.config_bytes=bytes.fromhex(p['config'])
for k,v in p['remote'].items():
    if k=='extra_comments':v={int(a):b for a,b in v.items()}
    setattr(f,k,copy.deepcopy(v))
f.next_run();f.active_finding_cap=1
f.run_id=p.get('run_id',f.run_id)
f.live_head=f.requested_head=p.get('head',f.live_head)
(f.root/'review.patch').write_bytes(p['diff'].encode('utf-8'))
f.provider_comments=p['comments']
seen=[];original=inspect.unwrap(lifecycle.restore_finding_lifecycle)
returns={i.offset for i in dis.get_instructions(original) if i.opname in {'RETURN_VALUE','RETURN_CONST'}}
def capture(frame,event,value):
    if event=='return' and frame.f_code is original.__code__ and frame.f_globals is original.__globals__ and frame.f_lasti in returns:
        seen.append((frame.f_locals['reviewer'],value[1]))
previous=sys.getprofile()
try:
    sys.setprofile(capture);code=f.invoke()
finally:
    sys.setprofile(previous)
state=seen[-1][0]._drift_states.get((123,7)) if seen else None
initial=decode_lifecycle_state(next(doc for _,doc in seen if doc is not None)['lifecycle']).state if any(doc is not None for _,doc in seen) else None
def describe(s):
    return None if s is None else {'obligations':[asdict(o) for o in s.inventory.obligations],'slots':[asdict(x) for x in s.slots]}
Path(sys.argv[3]).write_text(json.dumps({'record':parse_session_comment(f.comment['body'],identity=IDENTITY).to_dict(),'code':code,'error':f.error,'initial_document':next((doc for _,doc in seen if doc is not None),None),'initial':describe(initial),'state':describe(state),'threads':f.threads,'reviews':f.reviews,'receipts':seen[-1][0].native_finding_receipts(repository_id=123,pull_request=7) if seen else [],'provider_calls':f.provider_calls,'remote':{k:getattr(f,k) for k in p['remote']}}))
"""


class OrdinaryColdLocationDriftTests(unittest.TestCase):
    def test_rename_diff_cold_retains_criterion_and_unique_native_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            f = ColdDriftEntryFixture(str(source), limit=512)
            f.active_finding_cap = 1
            comment = {
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
            f.provider_comments = [comment]
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(len(f.threads), 1)
            diff = (
                "diff --git a/src/app.py b/src/renamed.py\n"
                "similarity index 90%\nrename from src/app.py\nrename to src/renamed.py\n"
                "--- a/src/app.py\n+++ b/src/renamed.py\n@@ -1,2 +1,2 @@\n keep\n-change\n+changed\n"
            )
            if getattr(self, "copy_case", False):
                diff = diff.replace("rename from", "copy from").replace(
                    "rename to", "copy to"
                )
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
                        "comments": (
                            [comment, dict(comment, path="src/renamed.py")]
                            if getattr(self, "copy_case", False)
                            else [dict(comment, path="src/renamed.py")]
                        ),
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
            initial = result["initial"]["obligations"][0]
            # This is the actual authenticated pre-transition carrier, not an
            # expected model assembled from the later conservative projection.
            self.assertFalse(initial["criterion"]["blocking"])
            self.assertTrue(initial["criterion"]["needs_human"])
            self.assertFalse(initial["latest_comment"]["effective_blocking"])
            self.assertTrue(initial["latest_comment"]["needs_human"])
            obligations = result["state"]["obligations"]
            self.assertEqual(len(obligations), 1, result["state"])
            current = obligations[0]
            expected_criterion = dict(initial["criterion"])
            if getattr(self, "copy_case", False):
                # Ambiguity conservatively strengthens gates, never identity.
                expected_criterion.update(blocking=True, needs_human=True)
            self.assertEqual(current["criterion"], expected_criterion)
            self.assertIsNone(current["resolution_proof_sha256"])
            self.assertEqual(current["thread_binding"]["thread_id"], f.threads[0]["id"])
            self.assertEqual(
                current["thread_binding"]["comment_id"],
                f.threads[0]["comments"]["nodes"][0]["databaseId"],
            )
            self.assertEqual(len(result["threads"]), 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in result["reviews"]))
            if getattr(self, "copy_case", False):
                self.assertEqual(current["lifecycle_state"], "ambiguous-multi-target")
                self.assertEqual(
                    set(current["present_anchor"]["candidate_paths"]),
                    {"src/app.py", "src/renamed.py"},
                )
                later = json.loads(packet.read_text())
                later.update(
                    remote=result["remote"],
                    run_id=458,
                    head="e" * 40,
                    comments=[],
                    diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n keep\n-change\n+validate_contract()\n",
                )
                packet.write_text(json.dumps(later))
                partial_root = root / "partial"
                partial_root.mkdir()
                done = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        CHILD,
                        str(packet),
                        str(partial_root),
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    env=subprocess_environment(),
                    timeout=90,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                partial = json.loads(output.read_text())
                self.assertEqual(partial["code"], 0, partial["error"])
                self.assertEqual(len(partial["state"]["obligations"]), 1)
                held = partial["state"]["obligations"][0]
                self.assertEqual(held["criterion"], current["criterion"])
                self.assertEqual(held["lifecycle_state"], "ambiguous-multi-target")
                self.assertEqual(
                    set(held["present_anchor"]["candidate_paths"]),
                    {"src/app.py", "src/renamed.py"},
                )
                self.assertIsNone(held["resolution_proof_sha256"])
                self.assertNotEqual(held["thread_binding"]["slot_state"], "released")
                self.assertEqual(len(partial["threads"]), 1)
                self.assertFalse(partial["threads"][0]["isResolved"])
                self.assertEqual(
                    partial["threads"][0]["id"], current["thread_binding"]["thread_id"]
                )
                self.assertEqual(
                    {
                        k: v
                        for k, v in held["thread_binding"].items()
                        if k != "proof_sha256"
                    },
                    {
                        k: v
                        for k, v in current["thread_binding"].items()
                        if k != "proof_sha256"
                    },
                )
                # Fresh logical head changes the projection hash. Separately
                # validate the retained physical receipt against native bytes.
                for phase, obligation, head in (
                    (result, current, "d" * 40),
                    (partial, held, "e" * 40),
                ):
                    matching = []
                    for raw in phase["receipts"]:
                        receipt = NativeFindingReceipt(
                            **{k: v for k, v in raw.items() if k != "schema_version"}
                        )
                        if (
                            receipt.thread_id
                            == obligation["thread_binding"]["thread_id"]
                        ):
                            matching.append(receipt)
                    self.assertEqual(len(matching), 1)
                    receipt = matching[0]
                    native = phase["threads"][0]["comments"]["nodes"][0]
                    # Core's retained-binding hash is a logical projection
                    # commitment, not a new source-issued native receipt.
                    projected_proof = hashlib.sha256(
                        canonical_bytes(
                            {
                                "thread_id": receipt.thread_id,
                                "head_sha": head,
                                "obligation_id": obligation["criterion"][
                                    "obligation_id"
                                ],
                                "placement": obligation["thread_binding"]["placement"],
                            }
                        )
                    ).hexdigest()
                    self.assertEqual(
                        obligation["thread_binding"]["proof_sha256"], projected_proof
                    )
                    self.assertEqual(
                        receipt.native_head_sha or receipt.head_sha,
                        native["commit"]["oid"],
                    )
                    self.assertEqual(receipt.thread_id, phase["threads"][0]["id"])
                    self.assertEqual(receipt.comment_id, native["databaseId"])
                    self.assertEqual(
                        receipt.producer_id, native["author"]["databaseId"]
                    )
                    self.assertEqual(
                        receipt.body_sha256,
                        hashlib.sha256(native["body"].encode()).hexdigest(),
                    )
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in partial["reviews"])
                )
            else:
                self.assertEqual(current["present_anchor"]["path"], "src/renamed.py")
                self.assertEqual(current["present_anchor"]["drift_kind"], "file_rename")
                self.assertEqual(current["thread_binding"]["slot_state"], "active")

    def test_copy_diff_cold_preserves_ambiguity_without_duplicate_thread(self):
        self.copy_case = True
        self.test_rename_diff_cold_retains_criterion_and_unique_native_binding()
