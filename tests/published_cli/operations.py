#!/usr/bin/env python3
"""Current installed review-run admission, retained roots and publication."""

import argparse
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from qualify import Runner


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--expected-version", required=True)
    p.add_argument("--expected-github-version", required=True)
    p.add_argument("--report", required=True, type=Path)
    a = p.parse_args()
    assert importlib.metadata.version("review-sensei") == a.expected_version
    assert (
        importlib.metadata.version("review-sensei-github") == a.expected_github_version
    )
    r = Runner(a)
    rows = []
    child = Path(__file__).with_name("run_child.py").resolve()
    cases = []
    for partial in [False, True]:
        for token in ["missing", "present"]:
            for appid in ["", "99"]:
                cases.append(
                    dict(
                        id=f"workflow-token-{token}-appid-{appid or 'empty'}-"
                        + ("partial" if partial else "complete"),
                        partial=partial,
                        token=token,
                        appid=appid,
                        listing="none",
                        expect_continuation=token == "present",
                    )
                )
    for partial in [False, True]:
        for listing, expected in [
            ("matching", True),
            ("mismatch", False),
            ("403", False),
            ("wrong-type", False),
            ("second-page-mismatch", False),
            ("second-page-matching", True),
            ("second-page-absent", True),
            ("second-page-403", False),
            ("second-page-malformed", False),
            ("scan-limit", False),
            ("oversized-page", False),
            ("timeout", False),
            ("second-page-timeout", False),
            ("shared-deadline", False),
        ]:
            cases.append(
                dict(
                    id="readback-"
                    + listing
                    + "-"
                    + ("partial" if partial else "complete"),
                    partial=partial,
                    token="present",
                    appid="99",
                    listing=listing,
                    expect_continuation=expected,
                )
            )
    cases.extend(
        [
            dict(
                id="non-actions-no-token",
                partial=True,
                token="missing",
                appid="",
                listing="none",
                runid="",
                expect_continuation=False,
            ),
            dict(
                id="disabled-entry-no-token",
                partial=True,
                token="missing",
                appid="",
                listing="none",
                entry="disabled",
                expect_continuation=False,
            ),
            dict(
                id="wrong-author-mismatching-record",
                partial=True,
                token="present",
                appid="",
                listing="mismatch",
                login="other[bot]",
                expect_continuation=True,
            ),
        ]
    )
    for appid in ("", "99"):
        cases.append(
            dict(
                id="reply-authenticated-appid-" + (appid or "empty"),
                purpose="reply",
                partial=False,
                token="present",
                appid=appid,
                listing="none",
                expect_continuation=True,
            )
        )
    for fault in (
        "missing-token",
        "purpose",
        "head_sha",
        "overlap",
        "source-author",
        "provider-incomplete",
        "lost-reply",
        "cleanup",
        "disabled",
        "unfunded",
    ):
        cases.append(
            dict(
                id="reply-" + fault,
                purpose="reply",
                fault=fault,
                partial=False,
                token="missing" if fault == "missing-token" else "present",
                appid="99",
                listing="none",
                limit=128 if fault == "unfunded" else 512,
                expect_continuation=False,
            )
        )
    for command in ("review", "publish", "reply"):
        cases.append(
            dict(
                id="retired-actions-" + command,
                purpose="retired",
                command=command,
                partial=False,
                token="present",
                appid="99",
                listing="none",
                expect_continuation=False,
            )
        )
    for c in cases:
        wd = r.root / "cases" / c["id"]
        wd.mkdir(parents=True, exist_ok=True)
        env = {
            **r.env,
            "HOME": str(wd),
            "GITHUB_RUN_ID": c.get("runid", "100"),
            "GITHUB_RUN_ATTEMPT": "1",
            "REVIEWSENSEI_APP_ID": c["appid"],
        }
        if c["token"] == "present":
            env["GITHUB_TOKEN"] = "synthetic-read-only-token"
        proc = subprocess.run(
            [
                sys.executable,
                str(
                    child
                    if c.get("purpose", "review") == "review"
                    else child.with_name("entry_child.py")
                ),
                json.dumps(c),
            ],
            cwd=wd,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        (wd / "stdout.json").write_text(proc.stdout)
        (wd / "stderr.txt").write_text(proc.stderr)
        d = json.loads(proc.stdout)
        continued = d["completed"]
        checks = {
            "continuation": continued == c["expect_continuation"],
            "process_exit_matches_cli": proc.returncode == d["exit"],
            "no_live_connections": d["live_network"] == "blocked",
            "no_traceback": "Traceback" not in proc.stderr + d["stderr"],
            "physical_caller_accounting": d["physical_attempts"] == d["observations"],
            "no_unauthorized_approval": continued or not d["reviews"],
        }
        if (
            c["token"] == "missing"
            and c.get("runid", "100")
            and c.get("entry", "enabled") == "enabled"
        ):
            checks["exact_observed_diagnostic"] = (
                "review startup identity or read capability is unavailable"
                in d["stderr"]
                and not d["requests"]
            )
        if continued:
            checks["actual_terminal_root"] = d["caller_limit"] == 512 and (
                d["terminal_transaction"] is not None
                or d.get("action", {}).get("phase") == "terminal"
            )
            checks["conserved_caller_limit"] = d["conserved"] is True
            checks["actual_engine_or_retained_completion"] = (
                d["provider_calls"] > 0
                or d.get("terminal_reason") == "already-published"
            )
        if continued and c["partial"]:
            # A retained completion must use its original published evidence;
            # requiring a fresh review here would defeat the no-replay oracle.
            partial = d["seed"] if d.get("seed_exit") is not None else d
            checks["partial_withholds_native_approval"] = not any(
                x["state"] == "APPROVED" for x in partial["reviews"]
            )
            checks["partial_review_present"] = any(
                x["state"] == "COMMENTED" for x in partial["reviews"]
            )
            checks["actual_partial_coverage"] = "partial_coverage" in partial["stderr"]
            checks["partial_merge_check_withheld"] = (
                "action_required" in partial["check_conclusions"]
            )
        if continued and c.get("purpose", "review") == "review":
            if d["seed_exit"] is not None:
                checks["retained_completion_has_no_new_effect"] = (
                    d["seed_exit"] == 0
                    and d["seed"]["terminal_transaction"] is not None
                    and d["provider_calls"] == 0
                    and not d["reviews"]
                    and d["retained_reviews_unchanged"]
                    and d["terminal_transaction"] == d["seed"]["terminal_transaction"]
                )
            elif not c["partial"]:
                checks["fresh_complete_native_approval"] = (
                    any(x["state"] == "APPROVED" for x in d["reviews"])
                    and "success" in d["check_conclusions"]
                )
        if "mismatch" in c["listing"] and not c.get("login"):
            checks["integrity_guard_reached"] = d[
                "provider_calls"
            ] == 0 and "integrity" in (d["root_parse_error"] or "")
        if c["listing"] in {"scan-limit", "oversized-page"}:
            checks["bounded_discovery_fault_reached"] = (
                bool(d["fault_hits"]) and d["provider_calls"] == 0
            )
        if c["listing"].startswith("second-page-"):
            checks["read_later_page"] = any(
                x["query"].get("page") == ["2"] for x in d["requests"]
            )
        if c["listing"] == "scan-limit":
            checks["bounded_scan"] = (
                sum(
                    x["method"] == "GET" and x["path"].endswith("/issues/7/comments")
                    for x in d["requests"]
                )
                <= 10
            )
        if c["listing"] in {
            "403",
            "wrong-type",
            "timeout",
            "shared-deadline",
            "second-page-403",
            "second-page-malformed",
            "second-page-timeout",
        }:
            checks["requested_fault_guard_reached"] = (
                bool(d["fault_hits"]) and d["provider_calls"] == 0
            )
        if c.get("purpose") == "reply":
            fault = c.get("fault")
            if continued:
                checks["real_reply_once"] = (
                    d["reply_posts"] == 1
                    and d["action"]["outcome"]["kind"] == "applied"
                )
            elif fault in {"lost-reply", "cleanup", "provider-incomplete"}:
                checks["entered_then_pending"] = (
                    d["action"]["execution_started"]
                    and d["action"]["phase"] == "pending"
                    and d["provider_calls"] == 1
                )
                checks["stopped_successor_never_replays"] = (
                    d["successor_exit"] == 1
                    and d["no_replay"]
                    and d["retained_pending"]
                    and d["successor_observations_match"]
                )
                if fault != "provider-incomplete":
                    checks["uncertain_effect_retained"] = (
                        d["uncertain"] > 0 and d["reply_posts"] == 1
                    )
            else:
                checks["refusal_before_inference"] = (
                    d["provider_calls"] == 0
                    and d["reply_posts"] == 0
                    and bool(d["stderr"])
                )
                if fault in {"purpose", "head_sha"}:
                    checks["authenticated_scope_fault_reached"] = d["fault_hits"] == [
                        "grant-" + fault
                    ]
                expected = {
                    "purpose": "Broker current run authority is invalid",
                    "head_sha": "Broker current run grant conflicts",
                    "overlap": "API owner job inventory is incomplete or invalid",
                    "source-author": "Current action source changed",
                    "disabled": "trusted policy does not enable the current review route",
                    "unfunded": "initial and successor operation allocation is unfunded",
                }
                if fault in expected:
                    checks["exact_guard_diagnostic"] = expected[fault] in d["stderr"]
        if c.get("purpose") == "retired":
            checks["explicit_retired_guard_before_effects"] = (
                "phase entry is retired" in d["stderr"]
                and not d["physical_attempts"]
                and d["exit"] == 1
            )
        row = {
            "id": c["id"],
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "spec": c,
            "receipt": d,
            "exit": proc.returncode,
            "level": d["level"],
        }
        rows.append(row)
    result = {
        "version": a.expected_version,
        "total": len(rows),
        "pass": sum(x["status"] == "PASS" for x in rows),
        "fail": sum(x["status"] == "FAIL" for x in rows),
        "rows": rows,
        "scope": "real installed review-run authorization, caller accounting, durable roots and native publication; only external HTTP/provider responses synthetic",
    }
    (r.root / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
    for row in rows:
        if row["status"] != "PASS":
            print(json.dumps(row))
    return bool(result["fail"])


if __name__ == "__main__":
    sys.exit(main())
