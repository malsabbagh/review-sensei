#!/usr/bin/env python3
"""Bind one successful hosted publication and its exact published core version."""

import argparse
import base64
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib.request import Request, urlopen

from qualify_published_cli import HOSTED_MANIFEST, exact_version, hosted_version


def github_read(repository, path):
    request = Request(
        "https://api.github.com/repos/" + repository + "/" + path,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def signed_identity(get, tag):
    ref = get("git/ref/tags/" + tag)
    identity = ref.get("object", {})
    if (
        ref.get("ref") != "refs/tags/" + tag
        or identity.get("type") != "tag"
        or not re.fullmatch(r"[a-f0-9]{40}", identity.get("sha", ""))
    ):
        raise ValueError("publication requires an exact annotated signed tag")
    signed = get("git/tags/" + identity["sha"])
    source = signed.get("object", {})
    if (
        signed.get("sha") != identity["sha"]
        or signed.get("tag") != tag
        or source.get("type") != "commit"
        or not re.fullmatch(r"[a-f0-9]{40}", source.get("sha", ""))
        or signed.get("verification", {}).get("verified") is not True
    ):
        raise ValueError("publication tag signature/source is not verified")
    return identity["sha"], source["sha"]


def recheck(report, peer, get, repository):
    if (
        report.get("mode") != "confirmed-hosted-pypi-publish"
        or signed_identity(get, "v" + exact_version(report["version"]))
        != (report["core_tag_object"], report["core_source_sha"])
        or signed_identity(get, "github-v" + exact_version(report["github_version"]))
        != (report["github_tag_object"], report["source_sha"])
        or peer.get("status") != "PASS"
        or peer.get("workflow") != "release.yml"
        or peer.get("tag") != "v" + report["version"]
        or peer.get("source") != report["core_source_sha"]
    ):
        raise ValueError("publication identities changed while waiting")
    for run_id, attempt, source, tag, workflow in (
        (
            report["release_run_id"],
            report["release_attempt"],
            report["source_sha"],
            "github-v" + report["github_version"],
            "release-github.yml",
        ),
        (
            peer["run_id"],
            peer["run_attempt"],
            report["core_source_sha"],
            "v" + report["version"],
            "release.yml",
        ),
    ):
        run = get(f"actions/runs/{run_id}")
        if (
            run.get("id") != run_id
            or run.get("run_attempt") != attempt
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("head_sha") != source
            or run.get("head_branch") != tag
            or run.get("path") != ".github/workflows/" + workflow
            or run.get("event") != "push"
            or run.get("repository", {}).get("full_name") != repository
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("pull_requests")
        ):
            raise ValueError("publication run changed after readiness")


def release_identity(event, repository):
    run = event["workflow_run"]
    if (
        run["repository"]["full_name"] != repository
        or run["head_repository"]["full_name"] != repository
        or run["event"] != "push"
        or run["path"] != ".github/workflows/release-github.yml"
        or run["status"] != "completed"
        or run.get("conclusion") != "success"
        or not re.fullmatch(r"[a-f0-9]{40}", run["head_sha"])
    ):
        raise ValueError("event is not a completed same-repository Release push")
    for key in ("id", "run_attempt"):
        if type(run[key]) is not int or run[key] < 1:
            raise ValueError("invalid Release run identity")
    return run


def published_job(jobs, name="Publish to PyPI with Trusted Publishing"):
    matching = [j for j in jobs if j.get("name") == name]
    if len(matching) != 1 or matching[0].get("conclusion") != "success":
        raise ValueError(
            "the exact Release attempt has no confirmed successful PyPI publication job"
        )
    return matching[0]["id"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="manual dispatch: exact published version")
    parser.add_argument(
        "--github-version",
        help="manual dispatch: exact published review-sensei-github version",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--recheck-report", type=Path)
    parser.add_argument("--core-readiness", type=Path)
    args = parser.parse_args()
    if args.recheck_report:
        if (
            not args.core_readiness
            or args.report
            or args.version
            or args.github_version
        ):
            parser.error("recheck needs only its original report and core readiness")
        report = json.loads(args.recheck_report.read_text())
        report["status"] = "ERROR"
        try:
            repository = os.environ["GITHUB_REPOSITORY"]
            recheck(
                report,
                json.loads(args.core_readiness.read_text()),
                lambda path: github_read(repository, path),
                repository,
            )
            report.update(status="PASS", signed_sources_rechecked=True)
        except (ValueError, OSError, KeyError, TypeError):
            report["error"] = "publication identity/readiness recheck failed"
        args.recheck_report.write_text(json.dumps(report, indent=2) + "\n")
        return report["status"] != "PASS"
    if args.report is None or args.core_readiness:
        parser.error("qualification requires its output report")
    report = {"status": "ERROR"}
    try:
        if args.version or args.github_version:
            if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
                raise ValueError("manual version requires workflow_dispatch")
            if not args.version or not args.github_version:
                raise ValueError("manual dispatch requires both exact versions")
            version = exact_version(args.version)
            github_version = exact_version(args.github_version)
            report.update(
                mode="manual-exact-version",
                version=version,
                github_version=github_version,
            )
        else:
            if os.environ.get("GITHUB_EVENT_NAME") != "workflow_run":
                raise ValueError("automatic publication gate requires workflow_run")
            repo = os.environ["GITHUB_REPOSITORY"]
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            run = release_identity(event, repo)

            def get(path):
                return github_read(repo, path)

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
            job_id = published_job(jobs, "Publish review-sensei-github to PyPI")

            def manifest(path):
                data = get("contents/" + path + "?ref=" + run["head_sha"])
                if data.get("encoding") != "base64" or data.get("size", 0) > 65536:
                    raise ValueError("unbounded or unsupported release manifest")
                return base64.b64decode(data["content"]).decode("utf-8")

            content = manifest("pyproject.toml")
            version = exact_version(tomllib.loads(content)["project"]["version"])
            github_version = hosted_version(manifest(HOSTED_MANIFEST), version)
            core_object, core_source = signed_identity(get, "v" + version)
            github_object, github_source = signed_identity(
                get, "github-v" + github_version
            )
            if (
                github_source != run["head_sha"]
                or run.get("head_branch") != "github-v" + github_version
            ):
                raise ValueError("hosted publication event differs from signed release")
            report.update(
                mode="confirmed-hosted-pypi-publish",
                version=version,
                github_version=github_version,
                source_sha=run["head_sha"],
                release_run_id=run["id"],
                release_attempt=run["run_attempt"],
                publication_job_id=job_id,
                core_source_sha=core_source,
                core_tag_object=core_object,
                github_tag_object=github_object,
            )
        report["status"] = "PASS"
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("version=" + version + "\n")
            output.write("github_version=" + github_version + "\n")
            if "core_source_sha" in report:
                output.write("core_source_sha=" + report["core_source_sha"] + "\n")
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report["status"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
