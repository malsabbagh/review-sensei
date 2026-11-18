"""Repeated detached source branch sizing must not consume runtime bindings."""

import sys
import unittest

from tests import test_command_capacity_binding as commands


class CommandBranchProjectionTests(unittest.TestCase):
    def test_real_command_plans_size_twice_then_execute_and_cold_verify(self):
        observed = []
        previous = sys.getprofile()

        def observe(frame, event, arg):
            if (
                event != "call"
                or frame.f_code.co_name != "preflight_command_finalization"
                or not frame.f_code.co_filename.replace("\\", "/").endswith(
                    "/finding_lifecycle.py"
                )
            ):
                return
            values = frame.f_locals
            journal, plan = values["self"], values["plan"]
            before = len(journal.ledger.caller.observations)
            first = journal._command_branch_documents(
                plan, http=values["http"], budget=values["budget"]
            )
            second = journal._command_branch_documents(
                plan, http=values["http"], budget=values["budget"]
            )
            self.assertEqual(first, second)
            self.assertEqual(len(journal.ledger.caller.observations), before)
            observed.append(plan.record.finding_lifecycle["manifest"]["encoding"])

        try:
            sys.setprofile(observe)
            commands.CommandCapacityBindingTests().test_default_budget_two_finding_command_and_cold_verify_use_one_check_capability()
        finally:
            sys.setprofile(previous)
        self.assertGreaterEqual(len(observed), 2)
        self.assertIn("partitioned-json-v1", observed)
