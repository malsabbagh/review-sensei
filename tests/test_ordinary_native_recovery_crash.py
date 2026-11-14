"""A process crash cannot turn ephemeral settlement into durable authority."""

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
from tests import test_ordinary_native_recovery_custody as custody


class SimulatedProcessCrash(BaseException):
    pass


class OrdinaryNativeRecoveryCrashTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "private recovery storage requires POSIX")
    def test_crash_after_confirmed_receipt_before_terminal_keeps_cold_liability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            fixture = custody.OrdinaryNativeRecoveryCustodyTests().fixture(str(source))
            key = root / "private.key"
            key.write_bytes(b"synthetic-only-recovery-key-32byt")
            key.chmod(0o600)
            fixture.recovery_context_dir = root / "handoff"
            fixture.recovery_context_key_file = key
            captured = []

            def crash(proof, checks, **kwargs):
                capture = recovery._CHECK_RESULTS[proof][0]
                captured.append((capture.journal._record, capture.caller))
                raise SimulatedProcessCrash()

            with patch.object(recovery, "consume_check", crash):
                with self.assertRaises(SimulatedProcessCrash):
                    fixture.invoke()
            self.assertEqual(len(captured), 1)
            self.assertIsNone(captured[0][1].accounting.terminal_transaction)
            packet = root / "remote.json"
            packet.write_text(
                json.dumps(
                    {
                        "resume_expected": True,
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
            self.assertEqual(result["provider_calls"], 0)
            self.assertGreater(result["restored_unknown_charges"], 0)
            self.assertTrue(result["states"], result)
            self.assertEqual(set(result["states"]), {"confirmed"})
            # The source-confirmed journal is revalidated on the fresh attempt;
            # the original physical unknown charge is never refunded.
            self.assertEqual(result["code"], 0, result)
            self.assertIsNotNone(result["terminal"])
            self.assertEqual(result["review_states"], ["COMMENTED", "APPROVED"])
            self.assertEqual(result["check_ids"], [fixture.check_runs[0]["id"]])
            self.assertEqual(result["check_statuses"], ["completed"])
            self.assertEqual(
                result["native_writes"],
                [
                    [
                        "PATCH",
                        f"/repos/owner/repo/check-runs/{fixture.check_runs[0]['id']}",
                    ],
                    ["POST", "/repos/owner/repo/pulls/7/reviews"],
                    ["POST", "/repos/owner/repo/pulls/7/reviews"],
                ],
            )
            self.assertTrue(result["no_process_authority"])

    def test_receipt_checkpoint_crash_never_settles_before_durable_return(self):
        from urllib.parse import urlsplit

        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting
        from tests.test_api_work_persistence import IDENTITY

        for stored in (False, True):
            with (
                self.subTest(stored=stored),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = custody.OrdinaryNativeRecoveryCustodyTests().fixture(
                    directory
                )
                original = fixture.external
                captured = []

                def crash_checkpoint(request, timeout):
                    captures = tuple(recovery._ACTIVE.values())
                    if (
                        captures
                        and recovery._CLOSED.get(captures[0])
                        and request.get_method() == "PATCH"
                        and "/issues/comments/" in urlsplit(request.full_url).path
                    ):
                        captured.append(captures[0])
                        if stored:
                            original(request, timeout)
                        raise SimulatedProcessCrash()
                    return original(request, timeout)

                fixture.external = crash_checkpoint
                with self.assertRaises(SimulatedProcessCrash):
                    fixture.invoke()
                self.assertEqual(len(captured), 1)
                capture = captured[0]
                self.assertNotIn(
                    capture.mutation["request_id"], capture.caller._reconciled_mutations
                )
                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                state = ApiRunAccounting.from_document(record.api_run_accounting)
                self.assertIsNone(state.terminal_transaction)
                # Before receipt storage, the original persisted allocation
                # still reserves the not-yet-reported dispatch; it is not free.
                self.assertGreater(state.uncertain + state.reserved_undispatched, 0)
                self.assertGreater(capture.caller.accounting.uncertain, 0)
                if stored:
                    self.assertGreater(state.uncertain, 0)
                self.assertEqual(len(fixture.check_runs), 1)
                self.assertEqual(fixture.reviews, [])
