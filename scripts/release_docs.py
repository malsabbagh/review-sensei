#!/usr/bin/env python3
"""Build and qualify static release docs; never execute downloaded artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPOSITORY = "malsabbagh/review-sensei"
TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ARTIFACT = "review-sensei-release-docs"
REQUIRED_JOBS = {
    "Build release documentation",
    "Publish npm packages with Trusted Publishing",
    "Publish to PyPI with Trusted Publishing",
    "Create GitHub release assets",
}


def version_tuple(tag: str) -> tuple[int, ...]:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("docs require an immutable vX.Y.Z tag")
    return tuple(int(part) for part in match.groups())


def api(path: str) -> Any:
    """Use gh's authenticated GitHub API, never URLs supplied by artifacts."""
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/{path}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def api_pages(path: str, key: str | None = None) -> list[dict[str, Any]]:
    result = []
    # Fail closed rather than silently making a decision from truncated history.
    for page in range(1, 101):
        separator = "&" if "?" in path else "?"
        value = api(f"{path}{separator}per_page=100&page={page}")
        items = value[key] if key else value
        result.extend(items)
        if len(items) < 100:
            return result
    raise ValueError("release history pagination limit exceeded")


def tag_identity(tag: str) -> tuple[str, str]:
    version_tuple(tag)
    ref = api(f"git/ref/tags/{tag}")
    if ref.get("ref") != f"refs/tags/{tag}" or ref["object"]["type"] != "tag":
        raise ValueError("release docs require an annotated tag")
    tag_sha = ref["object"]["sha"]
    signed = api(f"git/tags/{tag_sha}")
    if (
        signed.get("tag") != tag
        or signed["object"]["type"] != "commit"
        or signed.get("verification", {}).get("verified") is not True
    ):
        raise ValueError("release tag signature is not verified by GitHub")
    source_sha = signed["object"]["sha"]
    if not SHA.fullmatch(tag_sha) or not SHA.fullmatch(source_sha):
        raise ValueError("invalid release object identity")
    return tag_sha, source_sha


def validate_run(run: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    if (
        run.get("path") != ".github/workflows/release.yml"
        or run.get("event") != "push"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("pull_requests")
    ):
        raise ValueError("docs require a successful same-repository tag Release run")
    version_tuple(run.get("head_branch", ""))
    if not SHA.fullmatch(run.get("head_sha", "")):
        raise ValueError("invalid release source SHA")
    for name in REQUIRED_JOBS:
        matches = [job for job in jobs if job.get("name") == name]
        attempts = [job.get("run_attempt") for job in matches]
        if not attempts or any(
            type(attempt) is not int or not 1 <= attempt <= run["run_attempt"]
            for attempt in attempts
        ):
            raise ValueError(f"required release job attempt is invalid: {name}")
        latest_attempt = max(attempts)
        matches = [job for job in matches if job["run_attempt"] == latest_attempt]
        if len(matches) != 1 or matches[0].get("conclusion") != "success":
            raise ValueError(f"required release job did not succeed: {name}")


def qualify(run_id: int) -> dict[str, Any]:
    run = api(f"actions/runs/{run_id}")
    if run.get("id") != run_id:
        raise ValueError("release run identity does not match the request")
    # A publisher-only retry does not rerun successful build/docs jobs. Read
    # all attempts and validate the newest execution of each required job.
    jobs = api_pages(f"actions/runs/{run_id}/jobs?filter=all", "jobs")
    validate_run(run, jobs)
    tag = run["head_branch"]
    tag_sha, source_sha = tag_identity(tag)
    if source_sha != run["head_sha"]:
        raise ValueError("release tag moved away from the successful run")
    # Signed source must also have reached the reviewed main line.
    comparison = api(f"compare/{source_sha}...main")
    if comparison.get("status") not in {"ahead", "identical"}:
        raise ValueError("release source is not an ancestor of main")
    release = api(f"releases/tags/{tag}")
    if (
        release.get("tag_name") != tag
        or release.get("draft")
        or release.get("prerelease")
    ):
        raise ValueError("release docs require a published stable GitHub release")
    artifacts = api_pages(f"actions/runs/{run_id}/artifacts", "artifacts")
    matches = [
        a for a in artifacts if a.get("name") == ARTIFACT and not a.get("expired")
    ]
    if len(matches) != 1:
        raise ValueError("exactly one retained release docs artifact is required")
    return {
        "schema_version": "1.0",
        "repository": REPOSITORY,
        "tag": tag,
        "version": tag[1:],
        "source_sha": source_sha,
        "tag_object_sha": tag_sha,
        "release_run_id": run_id,
        "release_run_attempt": run["run_attempt"],
        "artifact_id": matches[0]["id"],
    }


def assert_latest(selection: dict[str, Any]) -> None:
    """An older rerun cannot roll back a newer successfully published cutoff."""
    candidate = version_tuple(selection["tag"])
    for release in api_pages("releases"):
        tag = release.get("tag_name", "")
        if release.get("draft") or release.get("prerelease") or not TAG.fullmatch(tag):
            continue
        if version_tuple(tag) <= candidate:
            continue
        query = urlencode({"event": "push", "branch": tag, "status": "success"})
        runs = api_pages(f"actions/workflows/release.yml/runs?{query}", "workflow_runs")
        for run in runs:
            # A successful newer Release is a high-water mark even if its
            # artifact expired or its tag was subsequently moved/deleted.
            jobs = api_pages(f"actions/runs/{run['id']}/jobs?filter=all", "jobs")
            validate_run(run, jobs)
            raise ValueError(f"docs cannot replace newer successful release {tag}")


def load_script(root: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ValueError(f"missing release docs helper {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def file_inventory(directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinks are forbidden in release docs")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def validate_installation_versions(site: Path, version: str) -> None:
    patterns = (
        r"pip install review-sensei==([0-9]+\.[0-9]+\.[0-9]+)",
        r"@reviewsensei/cli@([0-9]+\.[0-9]+\.[0-9]+)",
        r"Release ([0-9]+\.[0-9]+\.[0-9]+)",
    )
    for path in site.rglob("*.html"):
        content = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if any(value != version for value in re.findall(pattern, content)):
                raise ValueError(
                    f"site installation version disagrees with tag: {path.name}"
                )


def build(root: Path, output: Path, tag: str, run_id: int, attempt: int) -> None:
    version = load_script(root, "check_release_version").validate_release_tag(tag, root)
    if run_id < 1 or attempt < 1:
        raise ValueError("invalid release docs build identity")
    source_sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if os.environ.get("GITHUB_SHA") != source_sha:
        raise ValueError("checkout does not match the release event SHA")
    status = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True,
    )
    if status:
        raise ValueError("release docs require a clean source checkout")
    tag_sha, tagged_sha = tag_identity(tag)
    local_tag = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", f"refs/tags/{tag}"], text=True
    ).strip()
    if tagged_sha != source_sha or local_tag != tag_sha:
        raise ValueError("checkout/tag does not match verified remote release source")
    pages = load_script(root, "build_site_pages")
    pages.check_site_pages(root=root)
    file_inventory(root / "docs/site")
    validate_installation_versions(root / "docs/site", version)
    if output.exists():
        raise ValueError("docs build output must be absent")
    output.mkdir(parents=True)
    site = output / "site"
    shutil.copytree(root / "docs/site", site)
    pages.build_site_pages(
        manifest_path=site / "data/site-manifest.json",
        providers_output=site / "providers/index.html",
        releases_output=site / "releases/index.html",
    )
    # Public docs/example links point to immutable source, including snippets
    # embedded in JS strings. The separate operator-managed @v5 channel stays
    # as documented; a package tag never promotes that channel.
    prefix = f"https://github.com/{REPOSITORY}/"
    for path in site.rglob("*.html"):
        content = path.read_text(encoding="utf-8")
        for kind in ("blob", "tree"):
            content = content.replace(
                f"{prefix}{kind}/main/", f"{prefix}{kind}/{source_sha}/"
            )
        # Unversioned installation commands would follow registry latest rather
        # than this docs cutoff, including after a partially failed release.
        content = re.sub(
            r"pip install review-sensei(?![=\w-])",
            f"pip install review-sensei=={version}",
            content,
        )
        content = content.replace(
            ">implemented on main<", ">implemented in this release<"
        )
        banner = (
            f'<p class="release-provenance" style="padding:12px;text-align:center">'
            f'Released docs: <a href="{prefix}releases/tag/{tag}">{tag}</a> · '
            f'<a href="{prefix}tree/{source_sha}">source {source_sha[:12]}</a> · '
            '<a href="/data/release-provenance.json">build provenance</a></p>'
        )
        content = content.replace("</main>", banner + "</main>")
        path.write_text(content, encoding="utf-8")
    provenance = {
        "schema_version": "1.0",
        "repository": REPOSITORY,
        "tag": tag,
        "version": version,
        "source_sha": source_sha,
        "tag_object_sha": tag_sha,
        "release_run_id": run_id,
        "build_run_attempt": attempt,
        "source_docs": f"{prefix}tree/{source_sha}/docs",
        "source_examples": f"{prefix}tree/{source_sha}/examples",
        "workflow_channel": "v5",
    }
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (site / "schemas/release-provenance.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(provenance)
    (site / "data/release-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    (output / "files.json").write_text(
        json.dumps(file_inventory(site), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def verify_bundle(bundle: Path, selection: dict[str, Any]) -> None:
    if set(p.name for p in bundle.iterdir()) != {"site", "files.json"}:
        raise ValueError("unexpected docs bundle entries")
    if any(p.is_symlink() for p in bundle.iterdir()):
        raise ValueError("symlinks are forbidden in release docs")
    site = bundle / "site"
    expected = json.loads((bundle / "files.json").read_text(encoding="utf-8"))
    actual = file_inventory(site)
    if actual != expected or not actual or "index.html" not in actual:
        raise ValueError("release docs file inventory does not match")
    provenance = json.loads(
        (site / "data/release-provenance.json").read_text(encoding="utf-8")
    )
    for key in (
        "schema_version",
        "repository",
        "tag",
        "version",
        "source_sha",
        "tag_object_sha",
        "release_run_id",
    ):
        if provenance.get(key) != selection[key]:
            raise ValueError(f"release docs provenance mismatch: {key}")
    attempt = provenance.get("build_run_attempt")
    # Re-running a failed publisher keeps the earlier successful docs artifact.
    if type(attempt) is not int or not 1 <= attempt <= selection["release_run_attempt"]:
        raise ValueError("release docs build attempt is invalid")
    manifest = json.loads(
        (site / "data/site-manifest.json").read_text(encoding="utf-8")
    )
    if any(manifest["release_facts"][k] != selection[k] for k in ("version", "tag")):
        raise ValueError("site manifest and qualified release disagree")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    builder = sub.add_parser("build")
    builder.add_argument("--root", type=Path, default=Path("."))
    builder.add_argument("--output", type=Path, required=True)
    builder.add_argument("--tag", required=True)
    builder.add_argument("--run-id", type=int, required=True)
    builder.add_argument("--attempt", type=int, required=True)
    select = sub.add_parser("select")
    select.add_argument("--run-id", type=int, required=True)
    select.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--selection", type=Path, required=True)
    recheck = sub.add_parser("recheck")
    recheck.add_argument("--selection-json", required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(
                args.root.resolve(),
                args.output.resolve(),
                args.tag,
                args.run_id,
                args.attempt,
            )
        elif args.command == "select":
            selection = qualify(args.run_id)
            assert_latest(selection)
            args.output.write_text(
                json.dumps(selection, indent=2) + "\n", encoding="utf-8"
            )
            with Path(os.environ["GITHUB_OUTPUT"]).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(f"artifact_id={selection['artifact_id']}\n")
                handle.write(
                    f"selection={json.dumps(selection, separators=(',', ':'))}\n"
                )
        else:
            selection = json.loads(
                args.selection_json
                if args.command == "recheck"
                else args.selection.read_text(encoding="utf-8")
            )
            # Re-check the remote cutoff immediately before staging a deployment.
            if qualify(selection["release_run_id"]) != selection:
                raise ValueError("release identity changed during docs qualification")
            assert_latest(selection)
            if args.command == "verify":
                verify_bundle(args.bundle, selection)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"release docs failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
