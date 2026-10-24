#!/usr/bin/env python3
"""Wait read-only for one exact original publication run to succeed."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPOSITORY = "malsabbagh/review-sensei"
WORKFLOWS = {
    "release.yml": ("v", "Publish to PyPI with Trusted Publishing"),
    "release-github.yml": ("github-v", "Publish review-sensei-github to PyPI"),
    "publish-npm.yml": ("v", "Publish npm packages with Trusted Publishing"),
}
MAX_WAIT = 90 * 60
INTERVAL = 30


class PublicationTimeout(ValueError):
    """The exact original publication did not become ready within its deadline."""


def api(path: str) -> dict:
    separator = "&" if "?" in path else "?"
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}"
        f"{separator}readiness={time.time_ns()}",
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(request, timeout=15) as response:
        body = response.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("publication readiness response exceeds its bound")
    return json.loads(body)


def pages(path: str, key: str) -> list[dict]:
    result = []
    for page in range(1, 11):
        separator = "&" if "?" in path else "?"
        items = api(f"{path}{separator}per_page=100&page={page}")[key]
        if not isinstance(items, list):
            raise ValueError("publication readiness inventory is invalid")
        result.extend(items)
        if len(items) < 100:
            return result
    raise ValueError("publication readiness pagination exceeded its bound")


def publication_run(workflow: str, tag: str, source: str) -> dict | None:
    query = urlencode({"event": "push", "branch": tag})
    runs = pages(f"actions/workflows/{workflow}/runs?{query}", "workflow_runs")
    if not runs:
        return None
    if len(runs) != 1:
        raise ValueError("publication requires one original run; retry that run")
    run = runs[0]
    if (
        run.get("path") != f".github/workflows/{workflow}"
        or run.get("event") != "push"
        or run.get("head_branch") != tag
        or run.get("head_sha") != source
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("pull_requests")
        or type(run.get("id")) is not int
        or run["id"] < 1
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] < 1
        or run.get("status")
        not in {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
        or run.get("conclusion")
        not in {
            None,
            "success",
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
            "neutral",
            "skipped",
            "stale",
            "startup_failure",
        }
    ):
        raise ValueError("publication run differs from the exact source contract")
    return run


def wait_for_publication(
    workflow: str,
    tag: str,
    source: str,
    report: dict,
    *,
    read=publication_run,
    read_jobs=pages,
    sleep=time.sleep,
    clock=time.monotonic,
    timeout=MAX_WAIT,
) -> None:
    prefix, publisher = WORKFLOWS[workflow]
    if not re.fullmatch(prefix + r"\d+\.\d+\.\d+", tag) or not re.fullmatch(
        r"[a-f0-9]{40}", source
    ):
        raise ValueError("publication readiness requires exact tag and source")
    deadline = clock() + timeout
    identity = None
    attempt = 0
    while True:
        try:
            run = read(workflow, tag, source)
            if run is not None:
                if identity is not None and (
                    run["id"] != identity or run["run_attempt"] < attempt
                ):
                    raise ValueError("publication run identity or attempt regressed")
                identity, attempt = run["id"], run["run_attempt"]
                report.update(
                    run_id=identity,
                    run_attempt=attempt,
                    run_status=run.get("status"),
                    run_conclusion=run.get("conclusion"),
                )
                if (
                    run.get("status") == "completed"
                    and run.get("conclusion") == "success"
                ):
                    jobs = read_jobs(f"actions/runs/{identity}/jobs?filter=all", "jobs")
                    matches = [job for job in jobs if job.get("name") == publisher]
                    if not matches or any(
                        type(job.get("run_attempt")) is not int
                        or not 1 <= job["run_attempt"] <= attempt
                        for job in matches
                    ):
                        raise ValueError("publication job execution is invalid")
                    latest = max(job["run_attempt"] for job in matches)
                    matches = [job for job in matches if job["run_attempt"] == latest]
                    if len(matches) != 1 or (
                        matches[0].get("status") != "completed"
                        or matches[0].get("conclusion") != "success"
                    ):
                        raise ValueError("successful publication job is not confirmed")
                    refreshed = read(workflow, tag, source)
                    if refreshed is None or any(
                        refreshed.get(key) != run.get(key)
                        for key in ("id", "run_attempt", "status", "conclusion")
                    ):
                        raise ValueError(
                            "publication changed during readiness verification"
                        )
                    report.update(status="PASS", publication_job_id=matches[0]["id"])
                    return
            else:
                report.update(run_status="not-created", run_conclusion=None)
        except HTTPError as error:
            code = error.code
            error.close()
            if code not in {429, 500, 502, 503, 504}:
                raise ValueError("publication readiness API access failed") from None
            report["run_status"] = "api-retry"
        except (URLError, TimeoutError):
            report["run_status"] = "api-retry"
        remaining = deadline - clock()
        if remaining <= 0:
            raise PublicationTimeout(
                "publication readiness timed out; inspect the original run"
            )
        print(json.dumps({"publication_readiness": report["run_status"]}), flush=True)
        sleep(min(INTERVAL, remaining))


def wait_for_pages_publication(
    core_run_id: int,
    tag: str,
    source: str,
    report: dict,
    *,
    read=None,
    read_jobs=None,
    read_original=None,
    wait=None,
) -> None:
    """Defer Pages for split npm, preserving historical combined-run recovery."""
    import release_docs as docs

    if type(core_run_id) is not int or core_run_id < 1:
        raise ValueError("Pages readiness requires an exact core run ID")
    read = docs.api if read is None else read
    read_jobs = docs.api_pages if read_jobs is None else read_jobs
    read_original = docs.channel_run if read_original is None else read_original
    wait = wait_for_publication if wait is None else wait
    run = read(f"actions/runs/{core_run_id}")
    if (
        type(run.get("id")) is not int
        or run.get("id") != core_run_id
        or run.get("head_branch") != tag
        or run.get("head_sha") != source
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] < 1
    ):
        raise ValueError("Pages core publication identity differs")
    jobs = read_jobs(f"actions/runs/{core_run_id}/jobs?filter=all", "jobs")
    split = docs.uses_independent_npm_channel(jobs)
    docs.validate_run(run, jobs, split_npm=split)
    report.update(core_run_id=core_run_id, core_run_attempt=run["run_attempt"])
    if not split:
        report.update(status="PASS", workflow="release.yml", mode="combined")
        return
    original = read_original("release.yml", tag, source)
    if any(
        original.get(key) != run.get(key)
        for key in ("id", "run_attempt", "status", "conclusion")
    ):
        raise ValueError("Pages original core publication changed")
    report["mode"] = "split"
    wait("publish-npm.yml", tag, source, report)
    # This is readiness only. Pages still recomputes full channel/artifact,
    # consumer/recorder, signature, provenance and high-water eligibility.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", choices=tuple(WORKFLOWS), required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--pages-core-run-id", type=int)
    args = parser.parse_args()
    report = {
        "status": "ERROR",
        "workflow": args.workflow,
        "tag": args.tag,
        "source": args.source,
    }
    try:
        if args.pages_core_run_id is None:
            wait_for_publication(args.workflow, args.tag, args.source, report)
        elif args.workflow == "publish-npm.yml":
            wait_for_pages_publication(
                args.pages_core_run_id, args.tag, args.source, report
            )
        else:
            raise ValueError("Pages core classification is only for npm readiness")
    except PublicationTimeout:
        report["reason"] = "publication-timeout"
        print(
            "publication readiness timed out; inspect and retry the original run",
            file=sys.stderr,
        )
    except (ValueError, OSError, KeyError, TypeError):
        report["reason"] = "publication-contract-or-access"
        # No remote response, exception text or credential enters evidence.
        print(
            "publication readiness failed; original publication remains required",
            file=sys.stderr,
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    return report["status"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
