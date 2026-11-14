"""Existing intermediate execution qualifies only the shared terminal fence."""

import sys
import unittest

from review_sensei_github import historical_terminal_fence as fence

import tests.test_quarantine_historical_execution as historical


class HistoricalTerminalFenceRuntimeTests(unittest.TestCase):
    def exercise(self, *, losses=False):
        entries, requests, successors, errors = [], [], [], []
        watched = {
            fence.before_terminal.__code__,
            fence.before_request.__code__,
            fence.confirm_checkpoint.__code__,
        }

        def trace(frame, event, value):
            if frame.f_code not in watched:
                return None
            if event == "exception":
                errors.append(str(value[1]))
            if event == "return":
                entry = frame.f_locals.get("entry")
                if entry is None or entry.get("phase") != "closing":
                    return trace
                caller = frame.f_locals["caller"]
                if frame.f_code is fence.before_terminal.__code__:
                    terminal = next(
                        b
                        for b in caller.accounting.blocks
                        if b.block_id == entry["terminal_block"]
                    )
                    entries.append(
                        (terminal.undispatched, entry["execution"], entry["proof"])
                    )
                elif frame.f_code is fence.before_request.__code__:
                    requests.append(
                        (entry["operation"], entry["close"], entry["attempts"])
                    )
                elif frame.f_locals.get("terminal") is True:
                    operation = entry["operation"]
                    successors.append(
                        {
                            "operation": operation,
                            "attempts": entry["attempts"],
                            "reserve": next(
                                b.undispatched
                                for b in caller.accounting.blocks
                                if b.block_id == entry["terminal_block"]
                            ),
                            "root": frame.f_locals["record"],
                            "reconciled": caller._checkpoint_readback_attempted,
                            "inactive": id(caller) not in frame.f_locals["active"],
                        }
                    )
            return trace

        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            historical.HistoricalExecutionTests().exercise(
                faults=("roots", "receipt-stored") if losses else ()
            )
        finally:
            sys.settrace(previous)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], 50)
        self.assertEqual(len(successors), 1)
        result = successors[0]
        self.assertTrue(result["inactive"])
        self.assertEqual(
            result["root"].api_run_accounting["action"]["phase"], "terminal"
        )
        self.assertEqual(result["operation"].capacity, 16)
        self.assertGreaterEqual(result["attempts"], 1)
        self.assertLessEqual(result["attempts"], 16)
        self.assertEqual(result["reserve"], 50 - result["attempts"])
        self.assertGreaterEqual(result["reserve"], 34)
        self.assertEqual(len(requests), result["attempts"])
        self.assertTrue(all(op is result["operation"] for op, _, _ in requests))
        self.assertTrue(all(close is requests[0][1] for _, close, _ in requests))
        self.assertEqual(
            [count for _, _, count in requests], list(range(1, len(requests) + 1))
        )
        self.assertEqual(result["reconciled"], losses)

    def test_original_intermediate_uses_one_original_terminal_operation(self):
        self.exercise()

    def test_original_loss_reconciliation_retains_same_terminal_operation(self):
        self.exercise(losses=True)
