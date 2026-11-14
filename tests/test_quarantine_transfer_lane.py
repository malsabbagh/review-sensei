"""Production broker transfer controls run in the dependency-complete Worker lane."""

import ast
import unittest
from pathlib import Path


class QuarantineTransferLaneTests(unittest.TestCase):
    def test_both_production_controls_remain_required_in_worker_qualification(self):
        root = Path(__file__).resolve().parents[1]
        module = "quarantine_lifecycle_transfer_qualification"
        self.assertFalse(
            (root / "tests/test_quarantine_lifecycle_transfer.py").exists()
        )
        source = ast.parse((root / "tests" / f"{module}.py").read_text())
        case = next(
            node
            for node in source.body
            if isinstance(node, ast.ClassDef)
            and node.name == "QuarantineLifecycleTransferTests"
        )
        methods = {
            node.name
            for node in case.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }
        self.assertEqual(
            methods,
            {
                "test_copied_actual_decision_transfer_refuses_before_refinalization",
                "test_head_change_after_shared_response_is_seen_by_later_fresh_fence",
            },
        )
        workflow = (root / ".github/workflows/ci.yml").read_text()
        worker = workflow.split("  workers:\n", 1)[1].split("\n  codeql:\n", 1)[0]
        command = (
            "python -W error::ResourceWarning -m unittest "
            "tests.quarantine_worker_qualification "
            f"tests.{module} -v"
        )
        self.assertEqual(workflow.count(f"tests.{module}"), 1)
        self.assertIn(command, worker)
        self.assertLess(worker.index("run: npm ci"), worker.index(command))
        self.assertIn('node-version: "22"', worker)
