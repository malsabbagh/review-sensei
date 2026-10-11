#!/usr/bin/env python3
"""Resolve an exact version only after the same Release attempt published to PyPI."""

import argparse
import base64
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib.request import Request, urlopen

from qualify_published_cli import exact_version


def release_identity(event, repository):
    run = event["workflow_run"]
    if (
        run["repository"]["full_name"] != repository
        or run["head_repository"]["full_name"] != repository
        or run["event"] != "push"
        or run["path"] != ".github/workflows/release.yml"
        or run["status"] != "completed"
        or not re.fullmatch(r"[a-f0-9]{40}", run["head_sha"])
    ):
        raise ValueError("event is not a completed same-repository Release push")
    for key in ("id", "run_attempt"):
        if type(run[key]) is not int or run[key] < 1:
            raise ValueError("invalid Release run identity")
    return run


def published_job(jobs):
    matching = [
        j for j in jobs if j.get("name") == "Publish to PyPI with Trusted Publishing"
    ]
    if len(matching) != 1 or matching[0].get("conclusion") != "success":
        raise ValueError(
            "the exact Release attempt has no confirmed successful PyPI publication job"
        )
    return matching[0]["id"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="manual dispatch: exact published version")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"status": "ERROR"}
    try:
        if args.version:
            if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
                raise ValueError("manual version requires workflow_dispatch")
            version = exact_version(args.version)
            report.update(mode="manual-exact-version", version=version)
        else:
            if os.environ.get("GITHUB_EVENT_NAME") != "workflow_run":
                raise ValueError("automatic publication gate requires workflow_run")
            repo = os.environ["GITHUB_REPOSITORY"]
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            run = release_identity(event, repo)

            def get(path):
                request = Request(
                    "https://api.github.com/repos/" + repo + "/" + path,
                    headers={
                        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                with urlopen(request, timeout=15) as response:
                    return json.load(response)

            jobs = []
            for page in range(1, 11):
                part = get(
                    f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page={page}"
                )
                jobs.extend(part["jobs"])
                if len(part["jobs"]) < 100:
                    break
            else:
                raise ValueError("Release job pagination exceeded the bounded gate")
            job_id = published_job(jobs)
            manifest = get("contents/pyproject.toml?ref=" + run["head_sha"])
            if manifest.get("encoding") != "base64" or manifest.get("size", 0) > 65536:
                raise ValueError("unbounded or unsupported release manifest")
            content = base64.b64decode(manifest["content"]).decode("utf-8")
            version = exact_version(tomllib.loads(content)["project"]["version"])
            report.update(
                mode="confirmed-pypi-publish",
                version=version,
                source_sha=run["head_sha"],
                release_run_id=run["id"],
                release_attempt=run["run_attempt"],
                publication_job_id=job_id,
            )
        report["status"] = "PASS"
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("version=" + version + "\n")
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report["status"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
