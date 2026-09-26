"""The state-transition procedure cites checks that exist and facts that hold.

The installation guide's "Session state: upgrade, rollback, and in-flight runs"
section is an operator procedure: it names the regression test that holds each
transition rule, and it makes storage claims about learnings, ledger records,
and the Worker's Durable Object namespaces.  These checks keep the citations
and the storage claims true, so the procedure cannot silently cite a renamed
check, a moved path, or a rule the artifacts no longer implement.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from review_sensei.learnings import DEFAULT_LEARNING_DIRECTORY

ROOT = Path(__file__).resolve().parents[1]
INSTALLATION = ROOT / "docs" / "installation.md"
RELEASING = ROOT / "docs" / "releasing.md"
RUNNER = ROOT / ".github" / "workflows" / "review-sensei-run.yml"
WORKER_README = ROOT / "deploy" / "cloudflare" / "README.md"

SECTION_TITLE = "## Session state: upgrade, rollback, and in-flight runs"
CITED_CHECK = re.compile(r"`(test_[a-z0-9_]+)`")
DEFINED_CHECK = re.compile(r"^\s+def (test_[a-z0-9_]+)\(", re.MULTILINE)
OPERATOR_OPERATIONS = (
    "Package publication",
    "public channel movement",
    "App permission acceptance",
    "Worker deployment",
    "production settings changes",
    "paid live inference",
)


def _installation_section() -> str:
    lines = INSTALLATION.read_text(encoding="utf-8").splitlines()
    start = lines.index(SECTION_TITLE)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^#{1,2} ", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _defined_checks() -> set[str]:
    defined: set[str] = set()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        defined.update(DEFINED_CHECK.findall(path.read_text(encoding="utf-8")))
    return defined


def _normalized(text: str) -> str:
    return " ".join(text.split())


class StateTransitionProcedureTests(unittest.TestCase):
    def test_procedure_citations_resolve_to_executable_checks(self):
        cited = set(CITED_CHECK.findall(_installation_section()))
        self.assertGreaterEqual(len(cited), 8)
        missing = sorted(cited - _defined_checks())
        self.assertEqual(missing, [], f"the procedure cites absent checks: {missing}")

    def test_storage_claims_match_the_artifacts(self):
        section = _installation_section()
        self.assertEqual(
            DEFAULT_LEARNING_DIRECTORY.as_posix(), ".github/review-sensei/learnings"
        )
        self.assertIn(DEFAULT_LEARNING_DIRECTORY.as_posix(), section)
        for namespace in ("DeliveryLedger", "BrokerLedger"):
            with self.subTest(namespace=namespace):
                self.assertIn(namespace, section)
                self.assertIn(namespace, WORKER_README.read_text(encoding="utf-8"))
        self.assertIn(
            "Do not delete the `DeliveryLedger` or `BrokerLedger` namespace "
            "during rollback",
            _normalized(WORKER_README.read_text(encoding="utf-8")),
        )

    def test_in_flight_runs_execute_the_identity_they_started_with(self):
        workflow = RUNNER.read_text(encoding="utf-8")
        self.assertIn("REVIEW_SENSEI_WORKFLOW_SHA: ${{ job.workflow_sha }}", workflow)
        self.assertIn("fetch --depth=1", workflow)
        self.assertIn('"$REVIEW_SENSEI_WORKFLOW_SHA"', workflow)
        section = _installation_section()
        self.assertIn(
            "moving a channel tag does not retroactively change an in-flight or "
            "rerun job",
            _normalized(section),
        )
        self.assertIn(
            "the default is fail-closed retry",
            _normalized(RELEASING.read_text(encoding="utf-8")),
        )

    def test_operator_only_operations_stay_operator_only(self):
        section = _normalized(_installation_section())
        for operation in OPERATOR_OPERATIONS:
            with self.subTest(operation=operation):
                self.assertIn(operation, section)


if __name__ == "__main__":
    unittest.main()
