"""Original held route: namespace custody is not publication admission."""

import copy
import dis
import inspect
import sys
import tempfile
import unittest
from unittest.mock import patch

from hot_original_entry import invoke_original
from review_sensei_github import guarded_hot_namespace as namespace
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import NativePublishedTailJourneyFixture

from review_sensei.api_work import MAX_API_REQUEST_LIMIT
from review_sensei.errors import ReviewInputError


class GuardedHotNamespaceHeldRouteTests(unittest.TestCase):
    def inspect_namespace_entry(self, entry):
        """Passive extension seam for additional inert source-data controls."""

    def test_original_held512_confirms_root_and_preserves_namespace_until_cleanup(self):
        require = inspect.getclosurevars(namespace.begin).nonlocals["require"]
        entries, initial_roots, guards, accepted, failures, refused = (
            [],
            [],
            [],
            [],
            [],
            [],
        )
        returns = {
            function.__code__: {
                instruction.offset
                for instruction in dis.get_instructions(function)
                if instruction.opname in {"RETURN_VALUE", "RETURN_CONST"}
            }
            for function in (
                namespace.begin,
                namespace.before_sequence,
                namespace.confirm_checkpoint,
            )
        }

        def reject(label, entry, caller):
            with self.assertRaises(ReviewInputError, msg=label):
                require(entry, caller)
            refused.append(label)

        def observe(frame, event, value):
            if event != "return" or frame.f_lasti not in returns.get(frame.f_code, ()):
                return
            if frame.f_code is namespace.begin.__code__:
                entry = frame.f_locals.get("entry")
                self.assertIsNotNone(
                    entry, "original eligible producer must issue custody"
                )
                entries.append(entry)
                initial_roots.append(entry["root"])
                self.inspect_namespace_entry(entry)
                caller = entry["caller"]
                require(entry, caller)
                reject("copied", dict(entry), caller)
                reject("expired", dict(entry, expired=True), caller)
                original = caller._block, caller._terminal_reason, caller._sequence
                try:
                    caller._block = entry["blocks"][1]
                    caller._terminal_reason = None
                    reject("manual-terminal", entry, caller)
                    caller._terminal_reason = "not-publication-completion"
                    reject("wrong-purpose", entry, caller)
                    caller._block, caller._terminal_reason = original[:2]
                    caller._sequence = MAX_API_REQUEST_LIMIT + 1
                    reject("overflow", entry, caller)
                finally:
                    caller._block, caller._terminal_reason, caller._sequence = original
            elif frame.f_code is namespace.before_sequence.__code__:
                entry = frame.f_locals.get("entry")
                if entry is not None:
                    guards.append(frame.f_locals["caller"]._terminal_reason)
            elif frame.f_code is namespace.confirm_checkpoint.__code__:
                entry = frame.f_locals.get("entry")
                if entry is not None:
                    record = frame.f_locals["record"]
                    self.assertEqual(entry["root"], record.record_sha256)
                    accepted.append(record.record_sha256)

        def trace(frame, event, value):
            if frame.f_code is not namespace.confirm_checkpoint.__code__:
                return None
            if event == "exception":
                failures.append((value[0].__name__, str(value[1])))
            return trace

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = NativePublishedTailJourneyFixture(directory)
            fixture.api_url = "https://api.github.com"
            old, registration = fixture.original_origins()
            old_accounting = copy.deepcopy(old.api_run_accounting)
            reviews, checks = (
                copy.deepcopy(fixture.reviews),
                copy.deepcopy(fixture.check_runs),
            )
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Current published assessment 0 requires independent human judgment.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            previous_profile, previous_trace = sys.getprofile(), sys.gettrace()
            try:
                with registered_resource([registration]):
                    sys.setprofile(observe)
                    sys.settrace(trace)
                    result, _wire = invoke_original(fixture)
            finally:
                sys.setprofile(previous_profile)
                sys.settrace(previous_trace)
            self.assertEqual(result, 1)
            self.assertIn("cannot fund the complete operation", fixture.error)
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            caller = entry["caller"]
            self.assertTrue(accepted)
            self.assertNotEqual(entry["root"], initial_roots[0])
            self.assertEqual(caller._root, entry["root"])
            self.assertIsNone(caller._checkpoint)
            self.assertEqual(failures, [])
            self.assertIn(None, guards)
            self.assertIn("publication-completion", guards)
            self.assertTrue(entry["terminal_entered"])
            self.assertTrue(entry["expired"])
            self.assertNotIn("run_frame", entry)
            reject("after-run", entry, caller)
            self.assertEqual(len(refused), 6)
            self.assertEqual(caller.accounting.configuration.limit, 512)
            self.assertEqual(caller.accounting.remaining, entry["remaining"])
            self.assertEqual(
                caller.accounting.prior_nonstartup_liability, entry["prior"]
            )
            for block in caller.accounting.blocks:
                before = entry["tranches"][block.block_id]
                self.assertTrue(set(before.pending).issubset(set(block.pending)))
                if block.block_id not in entry["blocks"]:
                    self.assertEqual(block, before)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            self.assertEqual(old.api_run_accounting, old_accounting)
