"""Only authenticated carrier evidence and charged liabilities cross restart."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import test_ordinary_native_recovery_custody as fixtures


class OrdinaryNativeRecoveryColdTests(unittest.TestCase):
    CHILD = r"""
import json, sys
from pathlib import Path
from unittest.mock import patch
from test_recovery_context_g17 import OrdinaryEntryHostedAndCliRecoveryG17Tests
from test_api_work_persistence import IDENTITY
from review_sensei.api_work import ApiRunAccounting
from review_sensei_github.session_ledger import parse_session_comment
from review_sensei_github import ordinary_native_recovery as recovery
from review_sensei_github import finding_lifecycle as lifecycle
packet=json.loads(Path(sys.argv[1]).read_text())
f=OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(sys.argv[2],limit=512)
f.root=Path(packet["root"])
f.policy=f.policy_ref=packet["policy"]
f.config_bytes=bytes.fromhex(packet["config_hex"])
f.attempt=2
f.recovery_context_dir=Path(packet["handoff"])
f.recovery_context_key_file=Path(packet["key_file"])
f.override_run_directory=Path(sys.argv[2])/"run"
f.override_run_directory.mkdir()
for name,value in packet["remote"].items():
    if name == "extra_comments": value={int(key):item for key,item in value.items()}
    setattr(f,name,value)
old=parse_session_comment(f.comment["body"],identity=IDENTITY)
original=ApiRunAccounting.from_document(old.api_run_accounting)
assert not recovery._ACTIVE and not recovery._CHECK_RESULTS
observed=[]
restore=lifecycle.restore_finding_lifecycle
def observe(*args,**kwargs):
    result=restore(*args,**kwargs)
    if result[1] is not None: observed.append([effect["status"] for effect in result[1]["effects"]])
    return result
def no_provider(): raise AssertionError("cold recovery invoked inference")
f.before_provider=no_provider
with patch.object(lifecycle,"restore_finding_lifecycle",observe): code=f.invoke()
assert f.accounted is not None, f.error
current=parse_session_comment(f.comment["body"],identity=IDENTITY)
now=ApiRunAccounting.from_document(current.api_run_accounting)
assert now.blocks[:2] == original.blocks[:2]
assert now.configuration == original.configuration and now.identity == original.identity
assert f.reviews == packet["remote"]["reviews"] and f.check_runs == packet["remote"]["check_runs"]
assert not recovery._ACTIVE and not recovery._CHECK_RESULTS
print(json.dumps({"code":code,"error":f.error,"states":observed[-1] if observed else [],
    "unknown_charges":original.uncertain,"restored_unknown_charges":now.uncertain,
    "terminal":now.terminal_transaction,"no_process_authority":True,"no_native_effects":True,"provider_calls":f.provider_calls}))
"""

    def test_cold_confirmed_and_unmatched_carriers_preserve_unknown_charges(self):
        for changed in (False, True):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = root / "source"
                source.mkdir()
                fixture = fixtures.OrdinaryNativeRecoveryCustodyTests().fixture(
                    str(source), altered_native=changed
                )
                key_file = root / "private.key"
                key_file.write_bytes(b"synthetic-only-recovery-key-32byt")
                key_file.chmod(0o600)
                fixture.recovery_context_dir = root / "handoff"
                fixture.recovery_context_key_file = key_file
                self.assertEqual(fixture.invoke(), 1 if changed else 0, fixture.error)
                packet = root / "remote.json"
                packet.write_text(
                    json.dumps(
                        {
                            "root": str(fixture.root),
                            "policy": fixture.policy,
                            "config_hex": fixture.config_bytes.hex(),
                            "handoff": str(fixture.recovery_context_dir),
                            "key_file": str(key_file),
                            "remote": {
                                name: getattr(fixture, name)
                                for name in (
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
                completed = subprocess.run(
                    [sys.executable, "-c", self.CHILD, str(packet), str(child)],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    env=os.environ.copy(),
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["code"], 1, result["error"])
                self.assertEqual(result["provider_calls"], 0)
                self.assertTrue(result["no_process_authority"])
                self.assertTrue(result["no_native_effects"])
                self.assertGreater(result["unknown_charges"], 0)
                self.assertGreaterEqual(
                    result["restored_unknown_charges"], result["unknown_charges"]
                )
                if changed:
                    self.assertIn("pending", result["states"])
                else:
                    # A fully terminal original operation is not reopened merely
                    # because its physical loss charge remains in the history.
                    self.assertIn("caller run is already terminal", result["error"])
                    self.assertIsNotNone(result["terminal"])
                    self.assertEqual(result["states"], [])
