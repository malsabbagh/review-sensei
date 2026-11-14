"""A cold populated replay preserves complete original hidden criteria."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import ordinary_native_recovery as recovery

from tests import test_ordinary_native_recovery_cold as cold
from tests import test_ordinary_native_recovery_crash as crashes
from tests import test_ordinary_native_recovery_custody as custody


class OrdinaryPopulatedColdTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "private recovery storage requires POSIX")
    def test_confirmed_check_crash_resumes_populated_original_at_512(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            fixture = custody.OrdinaryNativeRecoveryCustodyTests().fixture(str(source))
            fixture.active_finding_cap = 1
            fixture.provider_comments = [
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
                    "Confirm the external caller contract before forwarding this data.",
                    "Confirm the external retention policy permits retaining this data.",
                )
            ]
            key = root / "private.key"
            key.write_bytes(b"synthetic-only-recovery-key-32byt")
            key.chmod(0o600)
            fixture.recovery_context_dir = root / "handoff"
            fixture.recovery_context_key_file = key
            captured = []

            def crash(proof, checks, **kwargs):
                capture = recovery._CHECK_RESULTS[proof][0]
                captured.append(capture.journal._record)
                raise crashes.SimulatedProcessCrash()

            with patch.object(recovery, "consume_check", crash):
                try:
                    fixture.invoke()
                except crashes.SimulatedProcessCrash:
                    pass
                else:
                    self.fail(fixture.error)
            self.assertEqual(len(captured), 1)
            packet = root / "remote.json"
            packet.write_text(
                json.dumps(
                    {
                        "resume_expected": not getattr(self, "fault", None),
                        "fault": getattr(self, "fault", None),
                        "active_finding_cap": 1,
                        "root": str(fixture.root),
                        "policy": fixture.policy,
                        "config_hex": fixture.config_bytes.hex(),
                        "handoff": str(fixture.recovery_context_dir),
                        "key_file": str(key),
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
                [
                    sys.executable,
                    "-c",
                    cold.OrdinaryNativeRecoveryColdTests.CHILD,
                    str(packet),
                    str(child),
                ],
                capture_output=True,
                text=True,
                timeout=45,
                env=cold.subprocess_environment(),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(len(result["criteria"]), 2)
            self.assertTrue(all(item["human"] for item in result["criteria"]))
            if getattr(self, "fault", None):
                self.assertEqual(result["code"], 1, result)
                self.assertIn(
                    "original lifecycle admission membership conflicts", result["error"]
                )
                self.assertEqual(result["provider_calls"], 0)
                self.assertEqual(result["native_writes"], [])
                self.assertIsNone(result["terminal"])
                self.assertEqual(
                    result["restored_unknown_charges"], result["unknown_charges"]
                )
                return
            self.assertEqual(result["code"], 0, result)
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(result["states"], ["confirmed"])
            self.assertEqual(
                result["restored_unknown_charges"], result["unknown_charges"]
            )
            self.assertGreater(result["unknown_charges"], 0)
            self.assertIsNotNone(result["terminal"])
            self.assertEqual(result["review_states"], ["COMMENTED"])
            self.assertEqual(
                result["native_writes"],
                [
                    [
                        "PATCH",
                        f"/repos/owner/repo/check-runs/{fixture.check_runs[0]['id']}",
                    ],
                    ["POST", "/repos/owner/repo/pulls/7/reviews"],
                ],
            )

    def test_current_drift_two_criteria_all_losses_complete_at_original_512(self):
        from urllib.parse import urlsplit

        from review_sensei_github import ordinary_publication_plan as planning
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting
        from review_sensei.finding_lifecycle import decode_lifecycle_state
        from tests import test_recovery_context_g17 as entry
        from tests.test_api_work_persistence import IDENTITY

        with tempfile.TemporaryDirectory() as directory:
            fixture = (
                entry.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                    directory
                )
            )
            fixture.active_finding_cap = 1
            fixture.provider_comments = [
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
                    "Confirm the external caller contract before forwarding data.",
                    "Confirm the external retention policy permits retention.",
                )
            ]
            original = fixture.external
            lost = set()

            def stored_loss(request, timeout):
                response = original(request, timeout)
                method, path = request.get_method(), urlsplit(request.full_url).path
                key = (
                    method
                    if "/check-runs" in path and method in {"POST", "PATCH"}
                    else "root"
                    if method == "POST" and path.endswith("/pulls/7/reviews")
                    else None
                )
                if key is not None and key not in lost:
                    lost.add(key)
                    raise TimeoutError("synthetic current drift stored loss")
                return response

            fixture.external = stored_loss
            original_admit = planning.admit_current
            captured = []

            def admit(publisher, observer, journal, **kwargs):
                result = original_admit(publisher, observer, journal, **kwargs)
                captured.append(journal)
                return result

            with patch.object(planning, "admit_current", admit):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(lost, {"POST", "PATCH", "root"})
            self.assertEqual(len(captured), 1)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            accounting = ApiRunAccounting.from_document(record.api_run_accounting)
            self.assertEqual(accounting.configuration.limit, 512)
            self.assertGreaterEqual(accounting.uncertain, 3)
            self.assertIsNotNone(accounting.terminal_transaction)
            self.assertEqual(
                record.finding_lifecycle, captured[0]._record.finding_lifecycle
            )
            state = decode_lifecycle_state(captured[0]._state()).state
            self.assertEqual(len(state.inventory.obligations), 2)
            self.assertEqual(len(state.slots), 1)
            self.assertEqual(state.slots[0].state, "active")
            self.assertEqual(
                sum(
                    ob.thread_binding is not None for ob in state.inventory.obligations
                ),
                1,
            )
            self.assertTrue(
                all(ob.criterion.needs_human for ob in state.inventory.obligations)
            )
            self.assertEqual(len(fixture.inline), 1)
            self.assertEqual([r["state"] for r in fixture.reviews], ["COMMENTED"])
            self.assertEqual(fixture.check_runs[0]["conclusion"], "action_required")

    @unittest.skipUnless(os.name == "posix", "private recovery storage requires POSIX")
    def test_fresh_process_changed_original_criterion_holds_before_native_write(self):
        self.fault = "criterion"
        self.test_confirmed_check_crash_resumes_populated_original_at_512()
