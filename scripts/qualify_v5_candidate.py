#!/usr/bin/env python3
"""Qualify an exact v5 candidate before the owner's separately authorized tag move."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import major_tag_gate as gate
import prepare_release as prepare
import release_docs as docs
from qualify_published_cli import SUITES
from release_channels import latest_jobs

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_REPORT = 512 * 1024


def installed_run(run_id, hosted):
    """Admit only the original automatic qualification for this hosted attempt."""
    if type(run_id) is not int or run_id < 1:
        raise ValueError("positive installed CLI run ID required")
    run = docs.api(f"actions/runs/{run_id}")
    title = f"Qualify published CLI after {hosted['run_id']}/{hosted['run_attempt']}"
    if (
        run.get("id") != run_id
        or run.get("event") != "workflow_run"
        or run.get("path") != ".github/workflows/published-cli.yml"
        or run.get("head_branch") != "main"
        or run.get("display_title") != title
        or run.get("repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("head_repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("pull_requests")
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] < 1
    ):
        raise ValueError(
            "installed qualification must be the successful automatic Python run"
        )
    harness = gate.sha(run["head_sha"])
    if docs.api(f"compare/{hosted['source']}...{harness}").get("status") not in {
        "ahead",
        "identical",
    }:
        raise ValueError("installed qualifier predates the signed source")
    if docs.api(f"compare/{harness}...main").get("status") not in {
        "ahead",
        "identical",
    }:
        raise ValueError("installed qualifier is outside reviewed main")
    query = urlencode(
        {"event": "workflow_run", "branch": "main", "created": ">=" + run["created_at"]}
    )
    runs = docs.api_pages(
        f"actions/workflows/published-cli.yml/runs?{query}", "workflow_runs"
    )
    if [r["id"] for r in runs if r.get("display_title") == title] != [run_id]:
        raise ValueError(
            "installed qualification for this publication is stale or ambiguous"
        )
    return run


def reports(artifacts, name, run, job, members):
    if (
        not job
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or type(job.get("id")) is not int
        or job["id"] < 1
        or job.get("run_id") != run["id"]
        or job.get("head_sha") != run["head_sha"]
    ):
        raise ValueError(f"installed Python job did not succeed: {name}")
    # Failed-job retries can retain earlier successful lanes or upload a new
    # archive under the same name. Bind to the actual latest job's execution.
    started = datetime.fromisoformat(job["started_at"])
    completed = datetime.fromisoformat(job["completed_at"])
    matches = [
        a
        for a in artifacts
        if a["name"] == name
        and started <= datetime.fromisoformat(a["created_at"]) <= completed
    ]
    if len(matches) != 1:
        raise ValueError(f"missing or ambiguous installed evidence: {name}")
    artifact = matches[0]
    origin = artifact.get("workflow_run", {})
    if (
        type(artifact.get("id")) is not int
        or artifact["id"] < 1
        or artifact.get("expired") is not False
        or origin.get("id") != run["id"]
        or origin.get("head_sha") != run["head_sha"]
        or type(artifact.get("size_in_bytes")) is not int
        or not 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE
    ):
        raise ValueError(f"invalid installed evidence identity: {name}")
    raw = docs.gh_run(
        [
            "gh",
            "api",
            f"repos/{docs.REPOSITORY}/actions/artifacts/{artifact['id']}/zip",
            "--hostname",
            "github.com",
        ],
        "installed Python receipt download",
    ).stdout
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if len(raw) != artifact["size_in_bytes"] or digest != artifact.get("digest"):
        raise ValueError(f"installed evidence digest differs: {name}")
    result = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for member in members:
            entries = [item for item in archive.infolist() if item.filename == member]
            if len(entries) != 1 or not 0 < entries[0].file_size <= MAX_REPORT:
                raise ValueError(f"missing or oversized installed receipt: {member}")
            result[member] = json.loads(archive.read(entries[0]))
    return result, {
        "id": artifact["id"],
        "digest": digest,
        "job_id": job["id"],
        "job_attempt": job["run_attempt"],
    }


def verify_installed(receipt, run_id):
    projects = receipt["pypi_publication"]["projects"]
    core, hosted = projects["review-sensei"], projects["review-sensei-github"]
    run = installed_run(run_id, hosted)
    artifacts = docs.api_pages(f"actions/runs/{run_id}/artifacts", "artifacts")
    jobs = {j["name"]: j for j in latest_jobs(run)}
    files, origin = reports(
        artifacts,
        "published-cli-publication-gate",
        run,
        jobs.get("Require exact successful PyPI publication"),
        ["publication-gate.json", "core-publication-readiness.json"],
    )
    publication = files["publication-gate.json"]
    peer = files["core-publication-readiness.json"]
    expected = {
        "status": "PASS",
        "mode": "confirmed-hosted-pypi-publish",
        "signed_sources_rechecked": True,
        "core_tag_object": core["tag_object"],
        "core_source_sha": core["source"],
        "github_tag_object": hosted["tag_object"],
        "source_sha": hosted["source"],
        "version": core["version"],
        "github_version": hosted["version"],
        "release_run_id": hosted["run_id"],
        "release_attempt": hosted["run_attempt"],
    }
    expected_peer = {
        "status": "PASS",
        "source": core["source"],
        "tag": core["tag"],
        "workflow": core["workflow"],
        "run_id": core["run_id"],
        "run_attempt": core["run_attempt"],
    }
    for observed, fields in [(publication, expected), (peer, expected_peer)]:
        if any(observed.get(k) != v for k, v in fields.items()):
            raise ValueError(
                "installed gate does not bind the current signed Python publications"
            )
    evidence = {"publication": origin}
    for os_name in ("ubuntu-latest", "macos-latest"):
        for py in ("3.11", "3.14"):
            job = jobs.get(f"PyPI {core['version']} / {os_name} / Python {py}")
            name = f"published-cli-{os_name}-py{py}"
            files, evidence[name] = reports(artifacts, name, run, job, ["summary.json"])
            summary = files["summary.json"]
            if (
                summary.get("status") != "PASS"
                or summary.get("mode") != "current-lifecycle"
                or summary.get("requested_version") != core["version"]
                or summary.get("requested_github_version") != hosted["version"]
                or summary.get("wheel", {}).get("sha256") != receipt["wheel_sha256"]
                or summary.get("github_wheel", {}).get("sha256")
                != receipt["github_wheel_sha256"]
                or summary.get("selected_suites") != list(SUITES)
                or summary.get("excluded_suites") != []
            ):
                raise ValueError(
                    f"installed Python evidence differs from the qualified wheels: {name}"
                )
    return {
        "run_id": run_id,
        "run_attempt": run["run_attempt"],
        "harness_sha": run["head_sha"],
        "artifacts": evidence,
    }


def qualify(source, old_object, run_id, output):
    selection = {
        "mode": "v5-candidate",
        "source_sha": gate.sha(source),
        "channel": "v5",
        "tag_object": gate.sha(old_object),
    }
    receipt = gate.qualify(selection, output)
    if receipt["status"] != "PASS":
        raise ValueError("candidate reusable workflow surface did not qualify")
    receipt["installed_python"] = verify_installed(receipt, run_id)
    if prepare.ci_once(source) is None:
        raise ValueError("candidate is missing qualified exact-main Python CI")
    for name, project in receipt["pypi_publication"]["projects"].items():
        required, publisher = (
            (gate.pypi.ENGINE_JOBS, gate.pypi.PUBLISH_JOB)
            if name == "review-sensei"
            else ({gate.pypi.HOSTED_JOB}, gate.pypi.HOSTED_JOB)
        )
        current = gate.pypi.publication_run(
            project["source"], project["tag"], project["workflow"], required, publisher
        )
        if any(project.get(key) != value for key, value in current.items()):
            raise ValueError("Python publication attempt changed before promotion")
    fresh = installed_run(
        run_id, receipt["pypi_publication"]["projects"]["review-sensei-github"]
    )
    installed = receipt["installed_python"]
    if (fresh["run_attempt"], fresh["head_sha"]) != (
        installed["run_attempt"],
        installed["harness_sha"],
    ):
        raise ValueError("installed Python qualification changed before promotion")
    gate.assert_current(receipt)
    (output / "v5-candidate.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--old-v5-object", required=True)
    parser.add_argument("--installed-run", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        qualify(args.source, args.old_v5_object, args.installed_run, args.output)
    except Exception as error:
        print(
            "v5 candidate held: "
            + (str(error) if isinstance(error, ValueError) else type(error).__name__),
            file=sys.stderr,
        )
        return 1
    print(f"Qualified v5 candidate {args.source} against lease {args.old_v5_object}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
