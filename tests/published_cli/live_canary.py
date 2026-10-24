#!/usr/bin/env python3
"""Optional two bounded synthetic reviews; requires existing OLLAMA_API_KEY.

No credentials are configured; no raw provider payloads are retained.
Default deterministic integration tests never call this file.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PATCH = "diff --git a/src/pagination.py b/src/pagination.py\n--- a/src/pagination.py\n+++ b/src/pagination.py\n@@ -1,3 +1,3 @@\n def page(items, page_number, page_size):\n     start = page_number * page_size\n-    return items[start:start + page_size]\n+    return items[start:page_size]\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--expected-version", required=True)
    p.add_argument("--report", type=Path, required=True)
    a = p.parse_args()
    assert importlib.metadata.version("review-sensei") == a.expected_version
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        print(
            "Existing OLLAMA_API_KEY is unavailable; live canaries were not executed."
        )
        return 2
    a.report.mkdir(parents=True, exist_ok=True)
    cli = Path(sys.executable).parent / (
        "review-sensei.exe" if os.name == "nt" else "review-sensei"
    )
    rows = []
    for model in ["deepseek-v4.1-flash:cloud", "glm-5.3:cloud"]:
        wd = (a.report / model.split(":")[0]).resolve()
        wd.mkdir(exist_ok=True)
        (wd / "input.patch").write_text(PATCH, encoding="utf-8", newline="\n")
        (wd / "config.yml").write_text(
            "schema: 1\ninference:\n  backend: cloud-ollama\nadvanced:\n  review_work:\n    mode: unified\n  resources:\n    max_provider_calls: 2\n    review_deadline_seconds: 90\n    timeout_seconds: 45\n"
        )
        env = {
            k: os.environ[k]
            for k in ["PATH", "SYSTEMROOT", "WINDIR"]
            if k in os.environ
        }
        env.update(HOME=str(wd), OLLAMA_API_KEY=key, PYTHONUTF8="1")
        argv = [
            str(cli),
            "--diff",
            "input.patch",
            "--config",
            "config.yml",
            "--model",
            model,
            "--format",
            "json",
            "--no-learning-proposals",
            "--outcome",
            "outcome.json",
            "--instructions",
            "Review only this synthetic pagination defect. Identify the incorrect slice end when page_number is positive, at changed line 3. Return concise strict JSON.",
        ]
        tick = time.monotonic()
        try:
            cp = subprocess.run(argv, cwd=wd, env=env, capture_output=True, timeout=100)
            assert key.encode() not in cp.stdout + cp.stderr
            try:
                doc = json.loads(cp.stdout)
            except ValueError:
                doc = {}
            outcome = (
                json.loads((wd / "outcome.json").read_text())
                if (wd / "outcome.json").exists()
                else {}
            )
            findings = [
                c
                for c in doc.get("comments", [])
                if c.get("path") == "src/pagination.py"
                and c.get("line") == 3
                and c.get("blocking")
            ]
            checks = {
                "complete": doc.get("review_status") == "complete",
                "full_coverage": doc.get("coverage", {}).get("fully_reviewed") is True,
                "seeded_blocker": bool(findings),
                "exit_required_fix": cp.returncode == 1,
                "calls_bounded": outcome.get("provider_calls", 99) <= 2,
                "no_credential_output": True,
            }
            row = {
                "model": model,
                "status": "PASS" if all(checks.values()) else "FAIL",
                "checks": checks,
                "exit": cp.returncode,
                "outcome": outcome,
                "stdout_bytes": len(cp.stdout),
                "stdout_sha256": hashlib.sha256(cp.stdout).hexdigest(),
                "stderr_bytes": len(cp.stderr),
                "stderr_sha256": hashlib.sha256(cp.stderr).hexdigest(),
                "validated_comment_count": len(doc.get("comments", [])),
                "elapsed_seconds": round(time.monotonic() - tick, 2),
                "level": "installed-CLI-real-approved-Ollama-cloud",
                "max_calls": 2,
                "deadline_seconds": 90,
                "raw_payload_retained": False,
            }
        except subprocess.TimeoutExpired:
            row = {
                "model": model,
                "status": "FAIL",
                "reason": "bounded process timeout",
                "elapsed_seconds": round(time.monotonic() - tick, 2),
                "level": "installed-CLI-real-approved-Ollama-cloud",
            }
        rows.append(row)
        (a.report / "results.json").write_text(
            json.dumps({"version": a.expected_version, "rows": rows}, indent=2)
        )
        print(json.dumps(row), flush=True)
    return 1 if any(r["status"] == "FAIL" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
