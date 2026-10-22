#!/usr/bin/env python3
"""Bind independent release runs to the Marshal's single signed core contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

import check_release_version as versions
import github_release as releases
import major_tag_gate as major
import prepare_release as prepare
import publish_npm_release as npm
import release_docs as docs

WORKFLOWS = {"python": "release.yml", "npm": "publish-npm.yml"}
BUNDLE = "review-sensei-npm-release-bundle"
REQUIRED_JOBS = {
    "python": releases.BUILD_JOBS
    | releases.PUBLISH_JOBS
    | {"Create GitHub release assets"},
    "npm": {
        "Qualify signed prepared source and exact-head CI",
        "Assemble and validate npm package set",
        "Publish npm packages with Trusted Publishing",
        *(
            f"{job} ({target})"
            for job in ("Native standalone", "Verify published npm consumer")
            for target in (
                "darwin-arm64",
                "darwin-x64",
                "linux-arm64-gnu",
                "linux-x64-gnu",
                "win32-x64",
            )
        ),
    },
}


def matching_run(channel: str, tag: str, source: str) -> dict:
    """One original run per channel/tag. Recovery uses that run's attempts."""
    workflow = WORKFLOWS[channel]
    query = urlencode({"event": "push", "branch": tag})
    runs = docs.api_pages(f"actions/workflows/{workflow}/runs?{query}", "workflow_runs")
    if len(runs) != 1:
        raise ValueError("exactly one original channel run is required; retry it")
    run = runs[0]
    if (
        type(run.get("id")) is not int
        or run["id"] < 1
        or run.get("path") != f".github/workflows/{workflow}"
        or run.get("event") != "push"
        or run.get("head_branch") != tag
        or run.get("head_sha") != source
        or run.get("repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("head_repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("pull_requests")
    ):
        raise ValueError("channel run differs from signed version/source contract")
    return run


def event_contract(channel: str) -> dict:
    tag = os.environ.get("GITHUB_REF_NAME", "")
    workflow = WORKFLOWS[channel]
    if (
        os.environ.get("GITHUB_REPOSITORY") != docs.REPOSITORY
        or os.environ.get("GITHUB_EVENT_NAME") != "push"
        or os.environ.get("GITHUB_REF") != f"refs/tags/{tag}"
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{docs.REPOSITORY}/.github/workflows/{workflow}@refs/tags/{tag}"
    ):
        raise ValueError("channel requires its canonical same-repository tag push")
    tag_object, source = docs.tag_identity(tag)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if (
        event.get("repository", {}).get("full_name") != docs.REPOSITORY
        or event.get("ref") != f"refs/tags/{tag}"
        or event.get("created") is not True
        or event.get("deleted") is not False
        or event.get("after") not in {tag_object, source}
    ):
        raise ValueError("release event is not the original signed tag creation")
    expected_object = os.environ.get("EXPECTED_TAG_OBJECT")
    if expected_object is not None and expected_object != tag_object:
        raise ValueError("signed tag object changed after source qualification")
    if source != os.environ.get("GITHUB_SHA"):
        raise ValueError("event source differs from signed source")
    run = matching_run(channel, tag, source)
    if (
        str(run["id"]) != os.environ.get("GITHUB_RUN_ID")
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] < 1
        or str(run["run_attempt"]) != os.environ.get("GITHUB_RUN_ATTEMPT")
        or run.get("status") != "in_progress"
    ):
        raise ValueError("current channel run/attempt is not active")
    return {
        "schema": 1,
        "repository": docs.REPOSITORY,
        "version": tag[1:],
        "tag": tag,
        "tag_object": tag_object,
        "source_sha": source,
        "channel": channel,
        "workflow": workflow,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
    }


def qualify(channel: str, root: Path) -> dict:
    contract = event_contract(channel)
    prepare.qualify_tag(root, contract["tag"])
    # All six npm manifests and the core PyPI project must share one version.
    versions.validate_release_tag(contract["tag"], root)
    if event_contract(channel) != contract:
        raise ValueError("channel contract changed during qualification")
    return contract


def latest_jobs(run: dict) -> list[dict]:
    jobs = docs.api_pages(f"actions/runs/{run['id']}/jobs?filter=all", "jobs")
    result = []
    for name in sorted({job["name"] for job in jobs}):
        matches = [job for job in jobs if job["name"] == name]
        attempts = [job.get("run_attempt") for job in matches]
        if any(
            type(a) is not int or not 1 <= a <= run["run_attempt"] for a in attempts
        ):
            raise ValueError("channel job attempt is invalid")
        matches = [job for job in matches if job["run_attempt"] == max(attempts)]
        if len(matches) != 1:
            raise ValueError("channel job execution is ambiguous")
        result.append(matches[0])
    return result


def verify_python_release(root: Path, contract: dict) -> None:
    """Read the verified public Python release without draft/write authority."""
    run = matching_run("python", contract["tag"], contract["source_sha"])
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("matching Python channel run must reach terminal success")
    jobs = latest_jobs(run)
    for name in (
        releases.BUILD_JOBS | releases.PUBLISH_JOBS | {"Create GitHub release assets"}
    ):
        found = [job for job in jobs if job["name"] == name]
        if len(found) != 1 or (
            found[0].get("status") != "completed"
            or found[0].get("conclusion") != "success"
        ):
            raise ValueError(
                "matching Python publication prerequisite has not succeeded"
            )
    release = releases.find_release(contract["tag"])
    if release is None:
        raise ValueError("matching published Python release is required")
    binding = major.release_binding(
        release,
        contract["version"],
        contract["tag_object"],
        contract["source_sha"],
    )
    if binding["run_id"] != run["id"]:
        raise ValueError("published release belongs to another Python run")
    receipt = json.loads((root / prepare.RECEIPT).read_text())
    notes = prepare.released_notes(
        (root / "CHANGELOG.md").read_text(),
        contract["version"],
        receipt["release_date"],
    )
    context = releases.Context(
        contract["tag"],
        contract["tag_object"],
        contract["source_sha"],
        run["id"],
        run["run_attempt"],
        notes,
    )
    releases.verify_release(
        release, context, releases.release_body_inventory(context, binding["assets"])
    )
    major.published_assets(release["id"], binding)


def verify_attestation(path: Path, contract: dict, build_attempt: int) -> None:
    result = docs.gh_run(
        [
            "gh",
            "attestation",
            "verify",
            str(path),
            "--repo",
            docs.REPOSITORY,
            "--signer-workflow",
            f"{docs.REPOSITORY}/.github/workflows/{contract['workflow']}",
            "--source-digest",
            contract["source_sha"],
            "--source-ref",
            f"refs/tags/{contract['tag']}",
            "--deny-self-hosted-runners",
            "--format",
            "json",
            "--hostname",
            "github.com",
        ],
        "npm bundle attestation verification",
        text=True,
    )
    verified = json.loads(result.stdout)
    invocation = f"https://github.com/{docs.REPOSITORY}/actions/runs/{contract['run_id']}/attempts/{build_attempt}"
    if not isinstance(verified, list) or not verified:
        raise ValueError("npm attestation verification returned no verified result")
    for item in verified:
        value = item["verificationResult"]
        certificate = value["signature"]["certificate"]
        statement = value["statement"]
        if (
            certificate.get("runInvocationURI") != invocation
            or statement["predicate"]["runDetails"]["metadata"].get("invocationId")
            != invocation
        ):
            raise ValueError("npm attestation belongs to another run/build attempt")


def verify_npm(root: Path, bundle: Path) -> None:
    contract = qualify("npm", root)
    artifact_id = int(os.environ["BUNDLE_ARTIFACT_ID"])
    build_attempt = int(os.environ["BUNDLE_BUILD_ATTEMPT"])
    digest = os.environ["BUNDLE_ARTIFACT_DIGEST"]
    if (
        artifact_id < 1
        or not 1 <= build_attempt <= contract["run_attempt"]
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise ValueError("invalid original npm artifact identity")
    artifacts = docs.api_pages(
        f"actions/runs/{contract['run_id']}/artifacts", "artifacts"
    )
    matches = [item for item in artifacts if item.get("name") == BUNDLE]
    if len(matches) != 1 or (
        matches[0].get("id") != artifact_id
        or matches[0].get("expired") is not False
        or matches[0].get("digest") != "sha256:" + digest
        or matches[0].get("workflow_run", {}).get("id") != contract["run_id"]
        or matches[0].get("workflow_run", {}).get("head_sha") != contract["source_sha"]
    ):
        raise ValueError(
            "npm artifact is not the exact original same-run/source bundle"
        )
    npm.verify_resumed_bundle(
        bundle, contract["version"], expected_source_sha=contract["source_sha"]
    )
    subjects = npm.list_attestation_subjects(bundle)
    if (
        len(subjects) != 7
        or len(set(subjects)) != 7
        or "bundle-metadata.json" not in subjects
    ):
        raise ValueError("npm bundle must attest six tarballs and source metadata")
    if bundle.is_symlink() or any(p.is_symlink() for p in bundle.rglob("*")):
        raise ValueError("npm bundle contains symlinks")
    if {p.name for p in bundle.iterdir()} != set(subjects) | {
        "SHA256SUMS",
        "integrity.jsonl",
    }:
        raise ValueError("unexpected npm bundle inventory")
    for name in subjects:
        verify_attestation(bundle / name, contract, build_attempt)
    verify_python_release(root, contract)
    if event_contract("npm") != contract:
        raise ValueError("npm contract changed before publication")


def record(channel: str) -> dict:
    """Retain all non-recorder outcomes; any incomplete/failed job holds success."""
    contract = event_contract(channel)
    run = docs.api(f"actions/runs/{contract['run_id']}")
    jobs = [
        {
            key: job.get(key)
            for key in ("id", "name", "run_attempt", "status", "conclusion")
        }
        for job in latest_jobs(run)
        if job["name"] != f"Record independent {channel} channel result"
    ]
    artifacts = docs.api_pages(
        f"actions/runs/{contract['run_id']}/artifacts", "artifacts"
    )
    return {
        **contract,
        "jobs": jobs,
        "artifacts": [
            {key: item.get(key) for key in ("id", "name", "digest", "expired")}
            for item in artifacts
            if not item.get("name", "").startswith("release-")
        ],
        "successful": REQUIRED_JOBS[channel] <= {job["name"] for job in jobs}
        and all(
            j["status"] == "completed" and j["conclusion"] == "success" for j in jobs
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("qualify", "verify-npm", "record"))
    parser.add_argument("--channel", choices=tuple(WORKFLOWS))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command != "verify-npm" and args.channel is None:
        parser.error("--channel is required for qualify and record")
    try:
        if args.command == "verify-npm":
            if args.bundle is None:
                raise ValueError("npm verification requires its exact bundle")
            verify_npm(args.root, args.bundle)
        elif args.command == "qualify":
            result = qualify(args.channel, args.root)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                for key in ("tag_object", "source_sha", "version"):
                    output.write(f"{key}={result[key]}\n")
        else:
            if args.output is None:
                raise ValueError("channel recording requires an output path")
            args.output.write_text(json.dumps(record(args.channel), indent=2) + "\n")
    except (ValueError, OSError, KeyError, TypeError, npm.PublishError):
        print("release channel contract failed; publication held", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
