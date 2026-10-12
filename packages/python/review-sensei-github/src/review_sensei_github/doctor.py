"""GitHub merge-gate diagnostic. The engine doctor does not import this."""

from __future__ import annotations

from typing import Any

from review_sensei.diagnostics import DiagnosticCheck
from review_sensei_github.checks import REVIEW_CHECK_NAME, required_check_identity
from review_sensei_github.setup import SETUP_APP_LOGIN


def github_review_gate_check() -> DiagnosticCheck:
    gate_identity = required_check_identity(app_slug=SETUP_APP_LOGIN)
    return DiagnosticCheck(
        "review-gate",
        "pass",
        (
            "the merge gate is the check "
            f"{gate_identity['name']!r} produced by App "
            f"{gate_identity['producer']!r}; "
            f"{REVIEW_CHECK_NAME!r} must be marked required by a repository "
            "administrator (ReviewSensei requests no Administration "
            "permission and never changes branch protection), the App needs "
            "Checks: write to publish the gate (eligible auto-approval may "
            "still proceed without it), and GitHub never runs required checks on "
            "App-authored pull requests"
        ),
    )


def with_github_checks(report: dict[str, Any]) -> dict[str, Any]:
    """Return a doctor report that includes the hosted merge-gate check."""

    checks = list(report.get("checks", []))
    checks.append(github_review_gate_check().to_dict())
    return {**report, "checks": checks}
