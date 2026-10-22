"""GitHub merge-gate, caller-workflow drift, and protection-policy diagnostics.

The engine doctor does not import this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from review_sensei.diagnostics import DiagnosticCheck
from review_sensei_github.checks import REVIEW_CHECK_NAME, required_check_identity
from review_sensei_github.setup import (
    CALLER_CONTRACT_VERSION,
    LOCAL_TRIGGER_OVERRIDE_PATH,
    SETUP_APP_LOGIN,
    WORKFLOW_PATH,
    inspect_installed_caller_drift,
    inspect_local_trigger_override,
)


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


def github_caller_workflow_check(context_root: Path) -> DiagnosticCheck | None:
    """Inspect an installed caller workflow under ``context_root`` if present."""

    caller_path = context_root / WORKFLOW_PATH
    if not caller_path.is_file():
        return None
    content = caller_path.read_text(encoding="utf-8")
    drift = inspect_installed_caller_drift(content)
    if drift.is_current:
        return DiagnosticCheck(
            "caller-workflow",
            "pass",
            (
                f"{WORKFLOW_PATH} matches caller contract {CALLER_CONTRACT_VERSION} "
                f"(setup_version={drift.setup_version}, "
                f"recovery_inputs={drift.recovery_inputs_enabled})"
            ),
        )
    stale_list = ", ".join(drift.stale_sections) or "setup_version"
    return DiagnosticCheck(
        "caller-workflow",
        "action",
        (
            f"{WORKFLOW_PATH} is at caller contract {drift.contract_version} "
            f"(target {CALLER_CONTRACT_VERSION}); stale capabilities: {stale_list}"
        ),
    )


def github_trigger_override_check(context_root: Path) -> DiagnosticCheck | None:
    """Inspect an optional local trigger.py override under ``context_root`` if present."""

    override_path = context_root / LOCAL_TRIGGER_OVERRIDE_PATH
    if not override_path.is_file():
        return None
    content = override_path.read_text(encoding="utf-8")
    report = inspect_local_trigger_override(content)
    if report.is_current:
        return DiagnosticCheck(
            "trigger-override",
            "pass",
            (
                f"{LOCAL_TRIGGER_OVERRIDE_PATH} matches standalone command and "
                "rescan contract"
            ),
        )
    missing = ", ".join(report.missing_capabilities)
    return DiagnosticCheck(
        "trigger-override",
        "action",
        (
            f"{LOCAL_TRIGGER_OVERRIDE_PATH} takes precedence over caller fallback "
            f"and is missing capabilities: {missing}"
        ),
    )


def inspect_protection_policy_discrepancies(
    policy: dict[str, Any],
    live_rulesets: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Report read-only discrepancies between .github/protection-policy.json and live GitHub rulesets.

    Never mutates live GitHub rulesets or the checked-in policy file.
    """

    discrepancies: list[dict[str, Any]] = []
    raw_main = policy.get("main_branch")
    raw_ruleset = policy.get("ruleset")
    main_policy: dict[str, Any] = (
        raw_main
        if isinstance(raw_main, dict)
        else (raw_ruleset if isinstance(raw_ruleset, dict) else {})
    )
    raw_release = policy.get("release_tags")
    raw_tags = policy.get("tags")
    release_tag_policy: dict[str, Any] = (
        raw_release
        if isinstance(raw_release, dict)
        else (raw_tags if isinstance(raw_tags, dict) else {})
    )
    rulesets_by_name = {
        str(rs.get("name")): rs
        for rs in live_rulesets
        if isinstance(rs, dict) and isinstance(rs.get("name"), str)
    }

    main_rs_name = str(
        main_policy.get(
            "ruleset_name",
            "main-branch-verification"
            if "main-branch-verification" in rulesets_by_name
            else "main-branch-protection",
        )
    )
    main_rs = rulesets_by_name.get(main_rs_name)
    if main_rs is not None:
        raw_rules = main_rs.get("rules")
        rules: list[Any] = raw_rules if isinstance(raw_rules, list) else []
        pr_rule = next(
            (
                r
                for r in rules
                if isinstance(r, dict) and r.get("type") == "pull_request"
            ),
            None,
        )
        if isinstance(pr_rule, dict):
            raw_pr_params = pr_rule.get("parameters")
            params: dict[str, Any] = (
                raw_pr_params if isinstance(raw_pr_params, dict) else {}
            )
            expected_approvals = main_policy.get(
                "required_approving_review_count",
                main_policy.get("required_approving_reviews"),
            )
            live_approvals = params.get("required_approving_review_count")
            if (
                isinstance(expected_approvals, int)
                and live_approvals != expected_approvals
            ):
                discrepancies.append(
                    {
                        "ruleset_name": main_rs_name,
                        "ruleset_id": main_rs.get("id"),
                        "field": "required_approving_review_count",
                        "policy_expected": expected_approvals,
                        "live_actual": live_approvals,
                    }
                )
            expected_last_push = main_policy.get("require_last_push_approval")
            live_last_push = params.get("require_last_push_approval")
            if (
                isinstance(expected_last_push, bool)
                and live_last_push != expected_last_push
            ):
                discrepancies.append(
                    {
                        "ruleset_name": main_rs_name,
                        "ruleset_id": main_rs.get("id"),
                        "field": "require_last_push_approval",
                        "policy_expected": expected_last_push,
                        "live_actual": live_last_push,
                    }
                )
        status_rule = next(
            (
                r
                for r in rules
                if isinstance(r, dict) and r.get("type") == "required_status_checks"
            ),
            None,
        )
        if isinstance(status_rule, dict):
            raw_status_params = status_rule.get("parameters")
            status_params: dict[str, Any] = (
                raw_status_params if isinstance(raw_status_params, dict) else {}
            )
            raw_checks = status_params.get("required_status_checks")
            checks: list[Any] = raw_checks if isinstance(raw_checks, list) else []
            live_contexts = [
                str(c.get("context"))
                for c in checks
                if isinstance(c, dict) and isinstance(c.get("context"), str)
            ]
            expected_checks = main_policy.get("required_status_checks")
            if isinstance(expected_checks, list):
                expected_contexts = [
                    str(c.get("context"))
                    if isinstance(c, dict) and isinstance(c.get("context"), str)
                    else str(c)
                    for c in expected_checks
                    if (isinstance(c, dict) and isinstance(c.get("context"), str))
                    or isinstance(c, str)
                ]
                if sorted(live_contexts) != sorted(expected_contexts):
                    discrepancies.append(
                        {
                            "ruleset_name": main_rs_name,
                            "ruleset_id": main_rs.get("id"),
                            "field": "required_status_checks",
                            "policy_expected": expected_contexts,
                            "live_actual": live_contexts,
                        }
                    )
        expected_bypass = policy.get("bypass_actors")
        live_main_bypass = main_rs.get("bypass_actors")
        if isinstance(expected_bypass, list) and isinstance(live_main_bypass, list):
            if live_main_bypass != expected_bypass:
                discrepancies.append(
                    {
                        "ruleset_name": main_rs_name,
                        "ruleset_id": main_rs.get("id"),
                        "field": "bypass_actors",
                        "policy_expected": expected_bypass,
                        "live_actual": live_main_bypass,
                    }
                )

    tag_rs_name = str(release_tag_policy.get("ruleset_name", "release-tag-protection"))
    tag_rs = rulesets_by_name.get(tag_rs_name)
    if tag_rs is not None:
        raw_conditions = tag_rs.get("conditions")
        conditions: dict[str, Any] = (
            raw_conditions if isinstance(raw_conditions, dict) else {}
        )
        raw_ref_name = conditions.get("ref_name")
        ref_name: dict[str, Any] = (
            raw_ref_name if isinstance(raw_ref_name, dict) else {}
        )
        live_includes = ref_name.get("include")
        expected_includes = release_tag_policy.get("include_ref_patterns")
        if (
            isinstance(expected_includes, list)
            and isinstance(live_includes, list)
            and list(live_includes) != list(expected_includes)
        ):
            discrepancies.append(
                {
                    "ruleset_name": tag_rs_name,
                    "ruleset_id": tag_rs.get("id"),
                    "field": "include_ref_patterns",
                    "policy_expected": list(expected_includes),
                    "live_actual": list(live_includes),
                }
            )
        live_bypass = tag_rs.get("bypass_actors")
        if isinstance(live_bypass, list) and live_bypass:
            discrepancies.append(
                {
                    "ruleset_name": tag_rs_name,
                    "ruleset_id": tag_rs.get("id"),
                    "field": "bypass_actors",
                    "policy_expected": [],
                    "live_actual": live_bypass,
                }
            )

    return discrepancies


def with_github_checks(
    report: dict[str, Any],
    *,
    context_root: Path | None = None,
) -> dict[str, Any]:
    """Return a doctor report that includes hosted merge-gate and caller drift checks."""

    checks = list(report.get("checks", []))
    checks.append(github_review_gate_check().to_dict())
    status = str(report.get("status", "ok"))
    if context_root is not None:
        for extra in (
            github_caller_workflow_check(context_root),
            github_trigger_override_check(context_root),
        ):
            if extra is not None:
                checks.append(extra.to_dict())
                if extra.status == "action":
                    status = "action"
    return {**report, "status": status, "checks": checks}
