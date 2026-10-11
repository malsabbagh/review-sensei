#!/usr/bin/env python3
"""Credential-free exact wheel reproduction of hosted operation publication gate."""

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
    p.add_argument("--report", required=True, type=Path)
    a = p.parse_args()
    assert importlib.metadata.version("review-sensei") == a.expected_version
    r = Runner(a)
    rows = []
    child = Path(__file__).with_name("operation_child.py").resolve()
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
                expect_continuation=True,
            ),
            dict(
                id="disabled-entry-no-token",
                partial=True,
                token="missing",
                appid="",
                listing="none",
                entry="disabled",
                expect_continuation=True,
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
            [sys.executable, str(child), json.dumps(c)],
            cwd=wd,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        (wd / "stdout.json").write_text(proc.stdout)
        (wd / "stderr.txt").write_text(proc.stderr)
        d = json.loads(proc.stdout)
        continued = bool(d["publication_continuations"])
        checks = {
            "continuation": continued == c["expect_continuation"],
            "process_exit_matches_cli": proc.returncode == d["exit"],
            "no_live_connections": d["live_network"] == "blocked",
            "no_traceback": "Traceback" not in proc.stderr + d["stderr"],
        }
        if (
            c["token"] == "missing"
            and c.get("runid", "100")
            and c.get("entry", "enabled") == "enabled"
        ):
            checks["exact_observed_diagnostic"] = (
                "operation record is unavailable" in d["stderr"]
                and not d["http_requests"]
            )
        if continued and c["partial"]:
            checks["partial_preserved"] = d["outcome"]["status"] == "partial"
        if c["listing"].startswith("second-page-"):
            checks["read_later_page"] = any(
                x["query"].get("page") == ["2"] for x in d["http_requests"]
            )
        if c["listing"] == "scan-limit":
            checks["bounded_scan"] = len(d["http_requests"]) <= 10
        if c["listing"] == "second-page-absent" and continued:
            checks["complete_no_record"] = len(d["http_requests"]) == 2
        if c["listing"] == "second-page-matching" and continued:
            checks["complete_matching_record"] = len(d["http_requests"]) == 2
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
        "scope": "operation acceptance gate with actual installed CLI; publication continuation mocked, not final approval or real GitHub writes",
    }
    (r.root / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
    for row in rows:
        if row["status"] != "PASS":
            print(json.dumps(row))
    return bool(result["fail"])


if __name__ == "__main__":
    sys.exit(main())
