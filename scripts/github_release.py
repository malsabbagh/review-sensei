#!/usr/bin/env python3
"""Stage verified tag assets as a draft; publish only after both registries succeed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import prepare_release as prepare
import release_docs as docs
import validate_release

BUILD_JOBS = {
    "Qualify signed prepared source and exact-head CI",
    "Build and attest release bundle",
    "Build release documentation",
    "Assemble and validate npm package set",
}
PUBLISH_JOBS = {
    "Publish to PyPI with Trusted Publishing",
    "Publish npm packages with Trusted Publishing",
    "Prepare draft GitHub release",
}
ARTIFACT = "review-sensei-release-bundle"
SBOM = "review-sensei-sbom.spdx.json"


@dataclass(frozen=True)
class Context:
    tag: str
    tag_object: str
    source: str
    run_id: int
    attempt: int
    notes: str


def write_api(path: str, method: str, payload: dict[str, Any]) -> None:
    """Capture diagnostics: never echo a token or a server-supplied command."""
    subprocess.run(
        [
            "gh",
            "api",
            f"repos/{docs.REPOSITORY}/{path}",
            "--method",
            method,
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )


def qualify_run(context: Context, *, publish: bool) -> None:
    run = docs.api(f"actions/runs/{context.run_id}")
    # Both commands execute inside the active Release attempt. A job rerun
    # increments GITHUB_RUN_ATTEMPT; a completed attempt is not write authority.
    if (
        run.get("id") != context.run_id
        or run.get("run_attempt") != context.attempt
        or run.get("path") != ".github/workflows/release.yml"
        or run.get("event") != "push"
        or run.get("head_branch") != context.tag
        or run.get("head_sha") != context.source
        or run.get("repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("head_repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("pull_requests")
        or run.get("status") != "in_progress"
    ):
        raise ValueError("current Release run does not match the verified tag/source")
    jobs = docs.api_pages(f"actions/runs/{context.run_id}/jobs?filter=all", "jobs")
    # Publisher-only retries retain earlier successful builds. A newer failed
    # execution must never be masked by an older success.
    for name in BUILD_JOBS | (PUBLISH_JOBS if publish else set()):
        matches = [job for job in jobs if job.get("name") == name]
        attempts = [job.get("run_attempt") for job in matches]
        if not attempts or any(
            type(attempt) is not int or not 1 <= attempt <= context.attempt
            for attempt in attempts
        ):
            raise ValueError(f"invalid prerequisite job attempts: {name}")
        latest = max(attempts)
        matches = [job for job in matches if job["run_attempt"] == latest]
        if len(matches) != 1 or (
            matches[0].get("status") != "completed"
            or matches[0].get("conclusion") != "success"
        ):
            raise ValueError(f"prerequisite job has not succeeded: {name}")
    artifacts = docs.api_pages(f"actions/runs/{context.run_id}/artifacts", "artifacts")
    bundles = [item for item in artifacts if item.get("name") == ARTIFACT]
    if len(bundles) != 1 or (
        bundles[0].get("expired") is not False
        or bundles[0].get("workflow_run", {}).get("id") != context.run_id
        or bundles[0].get("workflow_run", {}).get("head_sha") != context.source
    ):
        raise ValueError("exactly one retained same-source release bundle is required")
    if docs.tag_identity(context.tag) != (context.tag_object, context.source):
        raise ValueError("signed release tag identity changed")


def qualify_source(root: Path, tag: str) -> Context:
    if (
        os.environ.get("GITHUB_REPOSITORY") != docs.REPOSITORY
        or os.environ.get("GITHUB_EVENT_NAME") != "push"
        or os.environ.get("GITHUB_REF") != f"refs/tags/{tag}"
    ):
        raise ValueError("GitHub releases require a same-repository tag push")
    identity = docs.tag_identity(tag)
    prepare.qualify_tag(root, tag)
    receipt = json.loads((root / prepare.RECEIPT).read_text(encoding="utf-8"))
    tag_object, source = docs.tag_identity(tag)
    if (tag_object, source) != identity:
        raise ValueError("signed tag identity changed during source qualification")
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    if run_id < 1 or attempt < 1 or source != os.environ.get("GITHUB_SHA"):
        raise ValueError("invalid release run/event identity")
    notes = prepare.released_notes(
        (root / "CHANGELOG.md").read_text(encoding="utf-8"),
        receipt["version"],
        receipt["release_date"],
    )
    return Context(tag, tag_object, source, run_id, attempt, notes)


def bundle_assets(bundle: Path, context: Context) -> dict[str, Path]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("release bundle must be a regular directory")
    paths = list(bundle.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("release bundle contains a symlink")
    files = {
        path.relative_to(bundle).as_posix(): path for path in paths if path.is_file()
    }
    version = context.tag[1:]
    expected = {
        f"dist/review_sensei-{version}.tar.gz",
        f"dist/review_sensei-{version}-py3-none-any.whl",
        "release/SHA256SUMS",
        f"release/{SBOM}",
    }
    if set(files) != expected or any(
        not path.is_file() and not path.is_dir() for path in paths
    ):
        raise ValueError(
            "release bundle inventory differs from the four expected assets"
        )
    validate_release.validate_release_directory(bundle / "dist", version)
    assets = {path.name: path for path in files.values()}
    entries: dict[str, str] = {}
    for line in assets["SHA256SUMS"].read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([a-zA-Z0-9_.-]+)", line)
        if not match or match[2] in entries:
            raise ValueError("invalid or duplicate release checksum entry")
        entries[match[2]] = match[1]
    if set(entries) != set(assets) - {"SHA256SUMS"}:
        raise ValueError("checksums must cover the wheel, sdist and SBOM exactly")
    for name, digest in entries.items():
        if hashlib.sha256(assets[name].read_bytes()).hexdigest() != digest:
            raise ValueError("release asset checksum differs")
        # subject-checksums attests the listed subjects, not the checksum file.
        subprocess.run(
            [
                "gh",
                "attestation",
                "verify",
                str(assets[name]),
                "--repo",
                docs.REPOSITORY,
                "--signer-workflow",
                f"{docs.REPOSITORY}/.github/workflows/release.yml",
                "--source-digest",
                context.source,
                "--source-ref",
                f"refs/tags/{context.tag}",
                "--deny-self-hosted-runners",
                "--format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return assets


def release_body(context: Context, assets: dict[str, Path]) -> str:
    inventory = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sorted(assets.items())
    }
    binding = json.dumps(
        {
            "schema": 1,
            "repository": docs.REPOSITORY,
            "tag": context.tag,
            "tag_object": context.tag_object,
            "source": context.source,
            "run_id": context.run_id,
            "assets": inventory,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    prefix = f"https://github.com/{docs.REPOSITORY}"
    return (
        f"{context.notes}\n\n"
        f"Source: [{context.source}]({prefix}/commit/{context.source})\n\n"
        f"Build and publication: [Release run {context.run_id}]"
        f"({prefix}/actions/runs/{context.run_id})\n\n"
        f"<!-- reviewsensei-release:{binding} -->\n"
    )


def find_release(tag: str) -> dict[str, Any] | None:
    releases = [
        item for item in docs.api_pages("releases") if item.get("tag_name") == tag
    ]
    if len(releases) > 1:
        raise ValueError("duplicate GitHub releases for the tag")
    return releases[0] if releases else None


def verify_release(release: dict[str, Any], context: Context, body: str) -> None:
    if (
        type(release.get("id")) is not int
        or release.get("tag_name") != context.tag
        or release.get("target_commitish") != context.source
        or release.get("name") != f"ReviewSensei {context.tag}"
        or release.get("body") != body
        or type(release.get("draft")) is not bool
        or release.get("prerelease") is not False
    ):
        raise ValueError(
            "existing release identity or reviewed notes differ; no overwrite"
        )


def verify_assets(release: dict[str, Any], assets: dict[str, Path]) -> set[str]:
    remote = docs.api_pages(f"releases/{release['id']}/assets")
    seen: set[str] = set()
    for item in remote:
        name = item.get("name")
        if name not in assets or name in seen:
            raise ValueError("unexpected or duplicate GitHub release asset")
        path = assets[name]
        expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if (
            item.get("state") != "uploaded"
            or item.get("size") != path.stat().st_size
            or item.get("digest") != expected
        ):
            # Current GitHub assets expose SHA-256. Missing digests stop rather
            # than trusting a name/size or downloading an untrusted URL.
            raise ValueError(
                "existing GitHub release asset bytes differ or lack a digest"
            )
        seen.add(name)
    return set(assets) - seen


def manage(context: Context, assets: dict[str, Path], *, publish: bool) -> None:
    body = release_body(context, assets)
    qualify_run(context, publish=publish)
    release = find_release(context.tag)
    if release is None:
        if publish:
            raise ValueError(
                "publishing requires the verified draft from this Release run"
            )
        qualify_run(context, publish=False)
        try:
            write_api(
                "releases",
                "POST",
                {
                    "tag_name": context.tag,
                    "target_commitish": context.source,
                    "name": f"ReviewSensei {context.tag}",
                    "body": body,
                    "draft": True,
                    "prerelease": False,
                    "make_latest": "false",
                },
            )
        except subprocess.CalledProcessError:
            pass  # A lost response may have created the exact draft. Read back.
        release = find_release(context.tag)
        if release is None:
            raise ValueError("GitHub draft creation did not produce a verified release")
    verify_release(release, context, body)
    missing = verify_assets(release, assets)
    if not release["draft"]:
        # Already-public releases are read-only, even when rerunning preparation.
        qualify_run(context, publish=True)
        if missing:
            raise ValueError("published release assets are incomplete; no mutation")
        return
    if publish and missing:
        raise ValueError("draft assets are incomplete; retry draft preparation first")
    for name in sorted(missing):
        qualify_run(context, publish=False)
        fresh = find_release(context.tag)
        if fresh is None or fresh["id"] != release["id"]:
            raise ValueError("draft release identity changed before asset upload")
        verify_release(fresh, context, body)
        if not fresh["draft"]:
            raise ValueError("draft was published before asset upload")
        remaining = verify_assets(fresh, assets)
        if name not in remaining:
            continue
        try:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "upload",
                    context.tag,
                    str(assets[name]),
                    "--repo",
                    docs.REPOSITORY,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            pass  # Reconcile only exact uploaded bytes; never --clobber.
        if name in verify_assets(fresh, assets):
            raise ValueError("release asset upload did not produce the expected bytes")
    fresh = find_release(context.tag)
    if fresh is None or fresh["id"] != release["id"]:
        raise ValueError("release changed during asset preparation")
    verify_release(fresh, context, body)
    if verify_assets(fresh, assets):
        raise ValueError("release assets are incomplete")
    qualify_run(context, publish=publish)
    if not publish and not fresh["draft"]:
        qualify_run(context, publish=True)
    if publish and fresh["draft"]:
        try:
            write_api(
                f"releases/{fresh['id']}",
                "PATCH",
                {
                    "draft": False,
                    "make_latest": "legacy",
                },
            )
        except subprocess.CalledProcessError:
            pass  # A lost PATCH response may already have published the draft.
        published = find_release(context.tag)
        if published is None or published["id"] != release["id"]:
            raise ValueError("published release identity differs")
        verify_release(published, context, body)
        if published["draft"] or verify_assets(published, assets):
            raise ValueError("GitHub release publication did not complete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("draft", "publish"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        context = qualify_source(args.root.resolve(), args.tag)
        assets = bundle_assets(args.bundle, context)
        manage(context, assets, publish=args.command == "publish")
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError):
        print(
            "GitHub release verification or operation failed; publication held",
            file=sys.stderr,
        )
        return 1
    print(f"verified GitHub release {args.command}: {context.tag} {context.source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
