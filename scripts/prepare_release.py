#!/usr/bin/env python3
"""Prepare reviewed release metadata on main; never create a tag or publish."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import check_release_version as versions
import release_docs as docs
import release_docs_bot as bot
from sync_release_docs import git

TRUSTED_ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ".publication/release-preparation.json"
MARKDOWN_TARGETS = {
    "README.md": ("package-npm-1",),
    "docs/installation.md": ("package-npm-1",),
    "packages/npm/cli/README.md": ("package-npm-1", "package-npm-2"),
    **{
        f"packages/npm/platforms/{name.split('/cli-', 1)[1]}/README.md": (
            "package-npm-1",
        )
        for name in versions.NPM_TARGETS
    },
}
NPM_MANIFESTS = (
    "packages/npm/cli/package.json",
    *(
        f"packages/npm/platforms/{name.split('/cli-', 1)[1]}/package.json"
        for name in versions.NPM_TARGETS
    ),
)
PAYLOAD_PATHS = frozenset(
    (
        "pyproject.toml",
        "CHANGELOG.md",
        *MARKDOWN_TARGETS,
        *NPM_MANIFESTS,
        "docs/site/data/site-manifest.json",
        "docs/site/providers/index.html",
        "docs/site/releases/index.html",
        *("docs/site/" + path for path in docs.INSTALLATION_TARGETS),
    )
)
ALLOWED_PATHS = PAYLOAD_PATHS | {RECEIPT}
CI_JOBS = frozenset(
    (
        "Compatibility (Python 3.11, ubuntu-latest)",
        "Compatibility (Python 3.12, windows-latest)",
        "Compatibility (Python 3.13, macos-latest)",
        "Compatibility (Python 3.14, ubuntu-latest)",
        "Quality gates",
        "Schemas and Action pins",
        "Build and verify package",
        "Node launcher and npm package contracts",
        "Linux standalone baseline (linux-arm64-gnu)",
        "Linux standalone baseline (linux-x64-gnu)",
        "Intel macOS standalone ABI and parity",
        "Cloudflare Worker",
        "CodeQL (python)",
        "CodeQL (javascript-typescript)",
        "Required checks",
    )
)


def inputs(version: str, date: str, sha: str) -> None:
    docs.version_tuple("v" + version)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("release date must be YYYY-MM-DD")
    dt.date.fromisoformat(date)
    if not docs.SHA.fullmatch(sha):
        raise ValueError("release source must be an exact commit SHA")


def safe_paths(root: Path) -> None:
    for relative in ALLOWED_PATHS:
        path = root / relative
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError(f"release preparation path is a symlink: {relative}")
        if relative != RECEIPT and not path.is_file():
            raise ValueError(f"release preparation file is missing: {relative}")


def file_hashes(root: Path) -> dict[str, str]:
    safe_paths(root)
    return {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in sorted(PAYLOAD_PATHS)
    }


def changelog_parts(text: str) -> tuple[str, str, str]:
    headings = list(re.finditer(r"^## [^\n]+$", text, re.MULTILINE))
    if not headings or headings[0].group() != "## Unreleased":
        raise ValueError("changelog must begin with one Unreleased section")
    if sum(item.group() == "## Unreleased" for item in headings) != 1:
        raise ValueError("changelog has ambiguous Unreleased sections")
    first = headings[0]
    end = headings[1].start() if len(headings) > 1 else len(text)
    return text[: first.end()], text[first.end() : end].strip(), text[end:]


def released_notes(text: str, version: str, date: str) -> str:
    headings = list(re.finditer(r"^## [^\n]+$", text, re.MULTILINE))
    matching = [
        i for i, h in enumerate(headings) if h.group().startswith(f"## {version} - ")
    ]
    if len(matching) != 1 or headings[matching[0]].group() != f"## {version} - {date}":
        raise ValueError("release preparation changelog date/heading differs")
    index = matching[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    notes = text[headings[index].end() : end].strip()
    if not notes:
        raise ValueError("release requires reviewed changelog notes")
    return notes


def check_site(root: Path) -> None:
    pages = docs.load_script(TRUSTED_ROOT, "build_site_pages")
    pages.check_site_pages(
        root=root,
        manifest_path=root / "docs/site/data/site-manifest.json",
        providers_output=root / "docs/site/providers/index.html",
        releases_output=root / "docs/site/releases/index.html",
    )


def rewrite_marked(path: Path, targets: tuple[str, ...], version: str) -> None:
    text = path.read_text(encoding="utf-8")
    for start, end in reversed(docs.installation_spans(text, targets)):
        snippet = text[start:end]
        if not re.search(r"@reviewsensei/cli@\d+\.\d+\.\d+", snippet):
            raise ValueError("marked package installation target lacks a version")
        snippet = re.sub(
            r"(@reviewsensei/cli@)\d+\.\d+\.\d+",
            lambda match: match[1] + version,
            snippet,
        )
        text = text[:start] + snippet + text[end:]
    path.write_text(text, encoding="utf-8", newline="\n")


def receipt_record(root: Path) -> dict[str, Any]:
    safe_paths(root)
    receipt = json.loads((root / RECEIPT).read_text(encoding="utf-8"))
    if set(receipt) != {
        "schema_version",
        "repository",
        "version",
        "tag",
        "release_date",
        "base_sha",
        "notes_sha256",
        "files",
    }:
        raise ValueError("release preparation receipt fields differ")
    inputs(receipt["version"], receipt["release_date"], receipt["base_sha"])
    if (
        receipt["schema_version"] != "1.0"
        or receipt["repository"] != docs.REPOSITORY
        or receipt["tag"] != "v" + receipt["version"]
        or not isinstance(receipt["files"], dict)
        or set(receipt["files"]) != PAYLOAD_PATHS
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (*receipt["files"].values(), receipt["notes_sha256"])
        )
    ):
        raise ValueError("release preparation receipt identity differs")
    return receipt


def validate_payload(root: Path) -> dict[str, Any]:
    receipt = receipt_record(root)
    if receipt["files"] != file_hashes(root):
        raise ValueError("release preparation payload bytes differ")
    versions.validate_release_tag(receipt["tag"], root)
    notes = released_notes(
        (root / "CHANGELOG.md").read_text(encoding="utf-8"),
        receipt["version"],
        receipt["release_date"],
    )
    if receipt["notes_sha256"] != hashlib.sha256(notes.encode()).hexdigest():
        raise ValueError("reviewed release notes differ from preparation receipt")
    manifest = json.loads((root / "docs/site/data/site-manifest.json").read_text())
    if (
        manifest["release_facts"]["version"] != receipt["version"]
        or manifest["release_facts"]["tag"] != receipt["tag"]
        or manifest["last_updated"] != receipt["release_date"]
    ):
        raise ValueError("prepared source docs do not match the release")
    check_site(root)
    docs.validate_installation_versions(root / "docs/site", receipt["version"])
    for relative, targets in MARKDOWN_TARGETS.items():
        text = (root / relative).read_text(encoding="utf-8")
        for start, end in docs.installation_spans(text, targets):
            values = re.findall(r"@reviewsensei/cli@(\d+\.\d+\.\d+)", text[start:end])
            if not values or any(value != receipt["version"] for value in values):
                raise ValueError("prepared package/documentation pin differs")
    return receipt


def generate(root: Path, version: str, date: str, base: str) -> dict[str, Any]:
    """Deterministic local renderer; consumes only notes already on reviewed main."""
    inputs(version, date, base)
    safe_paths(root)
    current = versions.project_version(root)
    versions.validate_npm_manifests(root, current)
    check_site(root)
    # Validate every reviewed marker before modifying the disposable checkout.
    for relative, targets in MARKDOWN_TARGETS.items():
        text = (root / relative).read_text(encoding="utf-8")
        for start, end in docs.installation_spans(text, targets):
            if not re.search(r"@reviewsensei/cli@\d+\.\d+\.\d+", text[start:end]):
                raise ValueError("marked package installation target lacks a version")
    if docs.version_tuple("v" + version) < docs.version_tuple("v" + current):
        raise ValueError("release preparation cannot decrease the source version")
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    prefix, pending, rest = changelog_parts(text)
    if current == version:
        previous = receipt_record(root)
        if previous["version"] != version or previous["release_date"] != date:
            raise ValueError("same-version preparation intent differs")
        old_notes = released_notes(text, version, date)
        notes = (pending + "\n\n" + old_notes).strip() if pending else old_notes
        rest = rest.replace(old_notes, notes, 1)
    else:
        if not pending:
            raise ValueError("release requires reviewed Unreleased notes")
        if re.search(rf"^## {re.escape(version)} - ", rest, re.MULTILINE):
            raise ValueError("release version already occurs in the changelog")
        notes = pending
        rest = f"## {version} - {date}\n\n{notes}\n\n" + rest
    (root / "CHANGELOG.md").write_text(prefix + "\n\n" + rest, encoding="utf-8")
    project = root / "pyproject.toml"
    source = project.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^\[project\]\n.*?(?=^\[|\Z)", source)
    if section is None:
        raise ValueError("project metadata section is missing")
    updated, count = re.subn(
        r'^version = "[^"\n]+"$',
        f'version = "{version}"',
        section.group(),
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("project version declaration is ambiguous")
    project.write_text(
        source[: section.start()] + updated + source[section.end() :], encoding="utf-8"
    )
    for relative in NPM_MANIFESTS:
        path = root / relative
        value = json.loads(path.read_text(encoding="utf-8"))
        value["version"] = version
        if relative == NPM_MANIFESTS[0]:
            value["optionalDependencies"] = {
                name: version for name in versions.NPM_TARGETS
            }
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    for relative, targets in MARKDOWN_TARGETS.items():
        rewrite_marked(root / relative, targets, version)
    manifest_path = root / "docs/site/data/site-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_facts"].update(version=version, tag="v" + version)
    manifest["last_updated"] = date
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    docs.load_script(TRUSTED_ROOT, "build_site_pages").build_site_pages(
        manifest_path=manifest_path,
        providers_output=root / "docs/site/providers/index.html",
        releases_output=root / "docs/site/releases/index.html",
    )
    docs.rewrite_installation_versions(root / "docs/site", version)
    receipt = {
        "schema_version": "1.0",
        "repository": docs.REPOSITORY,
        "version": version,
        "tag": "v" + version,
        "release_date": date,
        "base_sha": base,
        "notes_sha256": hashlib.sha256(notes.encode()).hexdigest(),
        "files": file_hashes(root),
    }
    (root / RECEIPT).parent.mkdir(parents=True, exist_ok=True)
    (root / RECEIPT).write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return validate_payload(root)


def validate_index(root: Path) -> None:
    changed = set(
        filter(None, git(root, "diff", "--cached", "--name-only", "-z").split("\0"))
    )
    if not changed or RECEIPT not in changed or not changed.issubset(ALLOWED_PATHS):
        raise ValueError("release preparation attempted a non-generated change")
    git(root, "diff", "--cached", "--check")
    for relative in ALLOWED_PATHS:
        entry = git(root, "ls-files", "--stage", "--", relative)
        header, path = entry.split("\t")
        mode, sha, stage = header.split()
        if mode != "100644" or stage != "0" or path != relative:
            raise ValueError("prepared Git index requires exact regular files")
        if git(root, "hash-object", "--no-filters", "--", relative) != sha:
            raise ValueError("Git storage filters changed prepared bytes")


def verify_commit(root: Path, sha: str) -> dict[str, Any]:
    if not docs.SHA.fullmatch(sha) or git(root, "rev-parse", "HEAD") != sha:
        raise ValueError("preparation checkout does not match exact source")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("prepared source checkout must be clean")
    receipt = validate_payload(root)
    parents = git(root, "rev-list", "--parents", "-n", "1", sha).split()
    if parents != [sha, receipt["base_sha"]]:
        raise ValueError("preparation commit must have the exact reviewed parent")
    changed = set(
        filter(
            None,
            git(root, "diff", "--name-only", "-z", receipt["base_sha"], sha).split(
                "\0"
            ),
        )
    )
    if RECEIPT not in changed or not changed.issubset(ALLOWED_PATHS):
        raise ValueError("preparation commit changed source outside the allowlist")
    return receipt


def remote_main() -> str:
    sha = docs.api("branches/main")["commit"]["sha"]
    if not docs.SHA.fullmatch(sha):
        raise ValueError("main source identity is invalid")
    return sha


def absent_github(path: str) -> None:
    # Only an actual 404 proves absence; network/auth/rate-limit errors stop.
    result = subprocess.run(
        ["gh", "api", "--include", f"repos/{docs.REPOSITORY}/{path}"],
        capture_output=True,
        text=True,
    )
    status = re.search(r"^HTTP/\S+ (\d{3})", result.stdout, re.MULTILINE)
    if not status or status[1] != "404":
        raise ValueError(
            "release tag/object already exists or absence could not be verified"
        )


def absent_registry(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=15):
            raise ValueError("release version already exists in a package registry")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise ValueError("registry absence could not be verified") from None
    except urllib.error.URLError:
        raise ValueError("registry absence could not be verified") from None


def unused(version: str) -> None:
    absent_github(f"git/ref/tags/v{version}")
    absent_github(f"releases/tags/v{version}")
    absent_registry(f"https://pypi.org/pypi/review-sensei/{version}/json")
    for name in (versions.NPM_LAUNCHER_NAME, *versions.NPM_TARGETS):
        absent_registry(
            f"https://registry.npmjs.org/{name.replace('/', '%2F')}/{version}"
        )


def ci_once(sha: str) -> dict[str, Any] | None:
    if not docs.SHA.fullmatch(sha):
        raise ValueError("CI requires exact source SHA")
    runs = docs.api_pages(
        f"actions/workflows/ci.yml/runs?head_sha={sha}&event=push&branch=main",
        "workflow_runs",
    )
    if not runs:
        return None
    run = max(runs, key=lambda value: value["id"])
    if (
        run.get("head_sha") != sha
        or run.get("head_branch") != "main"
        or run.get("path") != ".github/workflows/ci.yml"
        or run.get("event") != "push"
        or run.get("repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("head_repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("pull_requests")
    ):
        raise ValueError("CI run does not match the exact main push")
    if run.get("status") != "completed":
        return None
    if run.get("conclusion") != "success":
        raise ValueError("exact-head CI failed; repair/retry CI without tagging")
    jobs = docs.api_pages(f"actions/runs/{run['id']}/jobs?filter=all", "jobs")
    names = {job.get("name") for job in jobs}
    if not CI_JOBS.issubset(names):
        raise ValueError("exact-head CI is missing required jobs")
    for name in names:
        matches = [job for job in jobs if job.get("name") == name]
        attempts = [job.get("run_attempt") for job in matches]
        if type(run.get("run_attempt")) is not int or any(
            type(attempt) is not int or not 1 <= attempt <= run["run_attempt"]
            for attempt in attempts
        ):
            raise ValueError("CI job attempt identity is invalid")
        latest = [job for job in matches if job["run_attempt"] == max(attempts)]
        if (
            len(latest) != 1
            or latest[0].get("status") != "completed"
            or latest[0].get("conclusion") != "success"
        ):
            raise ValueError("every latest exact-head CI job must succeed")
    fresh = docs.api(f"actions/runs/{run['id']}")
    if any(
        fresh.get(key) != run.get(key)
        for key in ("id", "head_sha", "status", "conclusion", "run_attempt")
    ):
        raise ValueError("CI advanced during qualification; retry the check")
    return run


def workflow_context() -> None:
    if (
        os.environ.get("GITHUB_REPOSITORY") != docs.REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_ACTOR") != "malsabbagh"
        or os.environ.get("GITHUB_TRIGGERING_ACTOR") != "malsabbagh"
    ):
        raise ValueError(
            "preparation requires an owner dispatch/rerun from public main"
        )


def prepare(root: Path, version: str, date: str, base: str) -> str:
    inputs(version, date, base)
    workflow_context()
    if (
        git(root, "remote", "get-url", "origin").removesuffix(".git")
        != f"https://github.com/{docs.REPOSITORY}"
    ):
        raise ValueError("preparation origin must be the fixed release repository")
    committer = bot.check(
        os.environ.get("RELEASE_DOCS_APP_ID", ""),
        os.environ.get("RELEASE_DOCS_APP_SLUG", ""),
    )
    unused(version)
    with tempfile.TemporaryDirectory(prefix="prepare-release-") as temporary:
        checkout = Path(temporary) / "main"
        git(root, "fetch", "--no-tags", "origin", "refs/heads/main")
        observed = git(root, "rev-parse", "FETCH_HEAD")
        if observed != remote_main():
            raise ValueError(
                "main advanced during fetch; dispatch again from fresh main"
            )
        git(root, "worktree", "add", "--detach", str(checkout), observed)
        try:
            if git(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
                raise ValueError("preparation checkout must be clean")
            if (checkout / RECEIPT).exists():
                try:
                    previous = verify_commit(checkout, observed)
                except ValueError:
                    previous = None
                if previous and previous["version"] == version:
                    if previous["release_date"] != date or base not in {
                        observed,
                        previous["base_sha"],
                    }:
                        raise ValueError("same-version preparation intent differs")
                    return observed
            if observed != base:
                raise ValueError(
                    "reviewed main advanced; select fresh source before preparing"
                )
            if ci_once(base) is None:
                raise ValueError("reviewed main CI is not terminal success")
            generate(checkout, version, date, base)
            git(checkout, "add", "--", *sorted(ALLOWED_PATHS))
            validate_index(checkout)
            git(
                checkout,
                "-c",
                f"user.name={committer['name']}",
                "-c",
                f"user.email={committer['email']}",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-s",
                "-m",
                f"release: prepare v{version}",
            )
            sha = git(checkout, "rev-parse", "HEAD")
            verify_commit(checkout, sha)
            unused(version)
            if remote_main() != base or ci_once(base) is None:
                raise ValueError(
                    "reviewed main/CI advanced before push; dispatch again"
                )
            if (
                bot.check(
                    os.environ.get("RELEASE_DOCS_APP_ID", ""),
                    os.environ.get("RELEASE_DOCS_APP_SLUG", ""),
                )
                != committer
            ):
                raise ValueError("release App identity changed before push")
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "credential.helper=",
                    "-c",
                    "credential.helper=!gh auth git-credential",
                    "push",
                    "origin",
                    "HEAD:refs/heads/main",
                ],
                capture_output=True,
                text=True,
            )
            # A dropped response may follow a successful push. Reconcile only
            # this exact commit; never force or regenerate on a different main.
            if result.returncode and remote_main() != sha:
                raise ValueError("non-force release push failed; inspect fresh main")
            if remote_main() != sha:
                raise ValueError(
                    "main advanced after preparation; requalify fresh source"
                )
            return sha
        finally:
            git(root, "worktree", "remove", "--force", str(checkout))


def wait_ci(
    root: Path, sha: str, timeout: int, version: str | None, date: str | None
) -> dict[str, Any]:
    if not 1 <= timeout <= 3600:
        raise ValueError("CI timeout must be between 1 and 3600 seconds")
    deadline = time.monotonic() + timeout
    while True:
        if remote_main() != sha:
            raise ValueError(
                "main advanced; dispatch preparation again from the selected new source"
            )
        run = ci_once(sha)
        if run is not None:
            break
        if time.monotonic() >= deadline:
            raise ValueError(
                "exact-head CI timed out; retry preparation without tagging"
            )
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    if version is not None:
        if date is None:
            raise ValueError("prepared CI requires release date")
        inputs(version, date, sha)
        unused(version)
        with tempfile.TemporaryDirectory(prefix="qualify-preparation-") as temporary:
            checkout = Path(temporary) / "main"
            git(root, "fetch", "--no-tags", "origin", "refs/heads/main")
            if git(root, "rev-parse", "FETCH_HEAD") != sha:
                raise ValueError("prepared main advanced during CI qualification")
            git(root, "worktree", "add", "--detach", str(checkout), sha)
            try:
                receipt = verify_commit(checkout, sha)
                if receipt["version"] != version or receipt["release_date"] != date:
                    raise ValueError("qualified release preparation intent differs")
            finally:
                git(root, "worktree", "remove", "--force", str(checkout))
        if remote_main() != sha or ci_once(sha) is None:
            raise ValueError("prepared main/CI advanced before signed-tag handoff")
    return run


def qualify_tag(root: Path, tag: str) -> None:
    tag_object, sha = docs.tag_identity(tag)
    if (
        git(root, "rev-parse", "HEAD") != sha
        or os.environ.get("GITHUB_SHA") != sha
        or git(root, "rev-parse", f"refs/tags/{tag}") != tag_object
    ):
        raise ValueError("tag/event/local source identities differ")
    receipt = verify_commit(root, sha)
    if receipt["tag"] != tag:
        raise ValueError("signed tag differs from the prepared version")
    if docs.api(f"compare/{sha}...main").get("status") not in {"ahead", "identical"}:
        raise ValueError("prepared tag source must be on main")
    if ci_once(sha) is None:
        raise ValueError("tagged preparation CI is not terminal success")
    if docs.tag_identity(tag) != (tag_object, sha):
        raise ValueError("release tag identity changed during qualification")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "prepare"):
        item = sub.add_parser(name)
        item.add_argument("--root", type=Path, default=TRUSTED_ROOT)
        item.add_argument("--version", required=True)
        item.add_argument("--date", required=True)
        item.add_argument("--base-sha", required=True)
    item = sub.add_parser("wait-ci")
    item.add_argument("--sha", required=True)
    item.add_argument("--version")
    item.add_argument("--date")
    item.add_argument("--timeout", type=int, default=3600)
    item = sub.add_parser("qualify-tag")
    item.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        if args.command == "render":
            generate(args.root.resolve(), args.version, args.date, args.base_sha)
            print("Rendered local release candidate; no network, tag or publication")
        elif args.command == "prepare":
            sha = prepare(args.root.resolve(), args.version, args.date, args.base_sha)
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with Path(output).open("a", encoding="utf-8") as handle:
                    handle.write(f"source_sha={sha}\n")
            print(
                json.dumps(
                    {"source_sha": sha, "version": args.version, "tag_created": False}
                )
            )
        elif args.command == "wait-ci":
            run = wait_ci(TRUSTED_ROOT, args.sha, args.timeout, args.version, args.date)
            print(f"Exact source {args.sha}: CI {run['id']} completed successfully")
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if args.version and summary:
                with Path(summary).open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"## Prepared v{args.version}\n\nSource: `{args.sha}`. "
                        f"[Full exact-head CI](https://github.com/{docs.REPOSITORY}/actions/runs/{run['id']}) passed.\n\n"
                        "No tag or package was published. Complete the readers-first "
                        "qualification and use the owner's existing signing identity:\n\n"
                        f"```bash\ngit fetch origin main\ngit tag -s v{args.version} {args.sha} -m 'ReviewSensei {args.version}'\n"
                        f"git push origin refs/tags/v{args.version}\n```\n\n"
                        "Hold if main/CI advances or this tag exists. Release builds "
                        "Python/npm from this exact signed source. Channel/Worker changes remain separate.\n"
                    )
        else:
            qualify_tag(TRUSTED_ROOT, args.tag)
            print("Signed tag, generated source receipt and exact-head CI qualified")
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError):
        # Do not expose captured subprocess output or network response bodies.
        # ValueError messages in this helper contain only validated public data.
        exc = sys.exc_info()[1]
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"release preparation stopped: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
