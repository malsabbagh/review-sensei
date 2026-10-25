"""Explicit production broker oracle for the analysis admission reservation.

Only external transports are synthetic. Requires pinned Worker dependencies;
run with python -m tests.epic238_analysis_admission_qualification.
"""

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from review_sensei_github.review_hooks import (
    ANALYSIS_SESSION_INTERNAL_DISPATCHES,
    GitHubReviewHooks,
)

from review_sensei.bounded_evidence import EvidenceReadBudget


class AnalysisAdmissionQualification(unittest.TestCase):
    def test_actual_broker_cold_warm_fallback_and_pretransport_refusal(self):
        liability = ANALYSIS_SESSION_INTERNAL_DISPATCHES
        # Pin the ADR's documented ceiling here too, independently of the unit
        # suite, so a silent constant change cannot pass this oracle by echoing
        # whatever value production happens to use.
        self.assertEqual(liability, 30)
        budget = EvidenceReadBudget()
        client = GitHubReviewHooks._admission_broker(
            SimpleNamespace(repository_id=987654321, pull_request=7, head_sha="a" * 40),
            budget,
            liability,
        )
        request = client.control.admit(verification=False)
        self.assertEqual(request["max_dispatches"], liability)
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "node",
                str(root / "tests/media_broker_profile.mjs"),
                str(request["max_dispatches"]),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        profiles = [
            item
            for item in json.loads(result.stdout)["profiles"]
            if item.get("capability") == "analysis-admission"
        ]
        self.assertEqual(len(profiles), 11)
        for profile in profiles:
            self.assertLessEqual(profile["dispatched"], request["max_dispatches"])
            if profile.get("error"):
                self.assertNotIn("session_state", profile)
        # Admission prepays exactly the enumerated 30; the caller's own HTTP
        # charge is a separate dispatch this oracle never makes. Assert the
        # literal rather than echoing the value pinned above, so this total is
        # evidence independent of the constant it just checked.
        self.assertEqual(budget.calls, 30)


if __name__ == "__main__":
    unittest.main()
