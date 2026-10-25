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
            liability=liability,
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
        document = json.loads(result.stdout)
        profiles = [
            item
            for item in document["profiles"]
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
        witnesses = {item["name"]: item for item in document["witness_qualification"]}
        self.assertEqual(len(witnesses), 8)
        # Name the cases we index so a harness rename fails as a readable
        # membership error instead of a KeyError at the indexing line.
        for name in (
            "both-ladders-fallback-annotated",
            "both-ladders-one-short-fallback-annotated",
            "byte-ladder-cap-fallback-annotated",
        ):
            self.assertIn(name, witnesses)
        maximum = witnesses["both-ladders-fallback-annotated"]
        self.assertEqual(maximum["dispatched"], 30)
        self.assertEqual(maximum["witness_reads"], {"review": 10, "comment": 10})
        # The ceiling is exact, so assert its composition and not only the
        # total: the funded arm must spend ten non-witness dispatches (JWKS 1,
        # failed Git plus annotated-tag fallback 3, installation/repository/
        # capability 4, head 1, App identity 1). If the exchange grows one of
        # those while the constant stays 30, a maximum history flips from the
        # witness's closed code to budget exhaustion.
        self.assertEqual(
            maximum["dispatched"] - sum(maximum["witness_reads"].values()), 10
        )
        self.assertEqual(maximum["session_state"], "enrolled")
        one_short = witnesses["both-ladders-one-short-fallback-annotated"]
        self.assertEqual(one_short["dispatched"], 29)
        self.assertEqual(one_short["error"], "broker_control_budget_exhausted")
        self.assertEqual(
            witnesses["byte-ladder-cap-fallback-annotated"]["error"],
            "github_session_witness_failed",
        )
        self.assertEqual(
            document["reader_qualification"],
            {"restarted_reads": 4, "fresh_items": 51},
        )


if __name__ == "__main__":
    unittest.main()
