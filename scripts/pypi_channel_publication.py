#!/usr/bin/env python3
"""Verify the Python publication used by a workflow channel, independently of npm."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import release_docs as docs
from python_release_sources import core_equivalence
from qualify_published_cli import clean_environment, require_hosted_pin

MAX_JSON = 512 * 1024
MAX_ARTIFACT = 32 * 1024 * 1024
MAX_PROVENANCE = 1024 * 1024
MAX_WHEEL_METADATA = 64 * 1024
MAX_WHEEL_ENTRIES = 4096
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
ENGINE_JOBS = {
    "Qualify signed prepared source and exact-head CI",
    "Build and attest release bundle",
    "Build release documentation",
    "Prepare draft GitHub release",
    "Create GitHub release assets",
    "Publish to PyPI with Trusted Publishing",
}
PUBLISH_JOB = "Publish to PyPI with Trusted Publishing"
HOSTED_JOB = "Publish review-sensei-github to PyPI"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_read(url, maximum):
    """Public HTTPS only, without credentials, redirects or unbounded bodies."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"pypi.org", "files.pythonhosted.org"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("untrusted Python registry URL")
    deadline = time.monotonic() + 45
    try:
        request = Request(url, headers={"User-Agent": "ReviewSensei-channel-validator"})
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError("Python registry response is not successful")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > maximum):
                raise ValueError("Python registry response exceeds its bound")
            body = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError("Python registry response deadline exhausted")
                part = response.read1(min(65536, maximum + 1 - len(body)))
                if not part:
                    return bytes(body)
                body.extend(part)
                if len(body) > maximum:
                    raise ValueError("Python registry response exceeds its bound")
    except ValueError:
        raise
    except Exception:
        raise ValueError(
            "Python registry read failed; publication remains unqualified"
        ) from None


def signed_release(tag):
    if not re.fullmatch(
        r"(?:github-)?v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag
    ):
        raise ValueError("canonical immutable Python release tag required")
    ref = docs.api(f"git/ref/tags/{tag}")
    if (
        ref.get("ref") != f"refs/tags/{tag}"
        or ref.get("object", {}).get("type") != "tag"
    ):
        raise ValueError("Python release tag must be signed and annotated")
    obj = ref["object"]["sha"]
    if not isinstance(obj, str) or not docs.SHA.fullmatch(obj):
        raise ValueError("invalid immutable tag object")
    value = docs.api(f"git/tags/{obj}")
    source = value.get("object", {}).get("sha")
    if (
        value.get("sha") != obj
        or value.get("tag") != tag
        or value.get("object", {}).get("type") != "commit"
        or value.get("verification", {}).get("verified") is not True
        or not isinstance(source, str)
        or not docs.SHA.fullmatch(source)
    ):
        raise ValueError("Python release signature/source is not verified")
    return obj, source


def _job_time(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value
    ):
        raise ValueError("Python publisher execution timestamp is invalid")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _publisher_execution(job, run):
    """Validate authenticated GitHub execution facts, excluding record aliases."""
    try:
        if (
            type(job["id"]) is not int
            or job["id"] < 1
            or type(job["run_id"]) is not int
            or job["run_id"] != run["id"]
            or job["head_sha"] != run["head_sha"]
            or job["head_branch"] != run["head_branch"]
        ):
            raise ValueError("Python publisher execution scope differs")
        text = ("name", "workflow_name", "runner_name", "runner_group_name")
        if any(not isinstance(job[k], str) or not job[k].strip() for k in text):
            raise ValueError("Python publisher execution identity is incomplete")
        if type(job["runner_id"]) is not int or job["runner_id"] < 1:
            raise ValueError("Python publisher runner identity is invalid")
        group = job["runner_group_id"]
        # GitHub re-exposes its default hosted group as null instead of zero.
        # Stable runner id/name/group name/labels still match independently.
        if group is None and job["runner_group_name"] == "GitHub Actions":
            group = 0
        if type(group) is not int or group < 0:
            raise ValueError("Python publisher runner group is invalid")
        labels = job["labels"]
        if (
            not isinstance(labels, list)
            or not labels
            or any(not isinstance(v, str) or not v.strip() for v in labels)
            or len(set(labels)) != len(labels)
        ):
            raise ValueError("Python publisher runner labels are invalid")
        created = _job_time(job["created_at"])
        started = _job_time(job["started_at"])
        completed = _job_time(job["completed_at"])
        if started > completed or started < created <= completed:
            raise ValueError("Python publisher execution timing is ambiguous")
        steps = job["steps"]
        if not isinstance(steps, list) or not steps:
            raise ValueError("Python publisher execution steps are unavailable")
        facts = []
        previous = 0
        for step in steps:
            number = step["number"]
            if (
                type(number) is not int
                or number <= previous
                or not isinstance(step["name"], str)
                or not step["name"].strip()
                or step["status"] != "completed"
                or step["conclusion"] not in {"success", "skipped"}
            ):
                raise ValueError("Python publisher execution steps are invalid")
            previous = number
            begin, end = step["started_at"], step["completed_at"]
            if (
                not (step["conclusion"] == "skipped" and begin is None and end is None)
                and not started <= _job_time(begin) <= _job_time(end) <= completed
            ):
                raise ValueError("Python publisher step timing is invalid")
            facts.append((number, step["name"], step["conclusion"], begin, end))
        if not any(step[2] == "success" for step in facts):
            raise ValueError("Python publisher execution has no completed step")
        identity = (
            job["run_id"],
            job["head_sha"],
            job["head_branch"],
            job["name"],
            job["workflow_name"],
            job["started_at"],
            job["completed_at"],
            job["runner_id"],
            job["runner_name"],
            group,
            job["runner_group_name"],
            tuple(labels),
            tuple(facts),
        )
        return identity, created <= started
    except (KeyError, TypeError):
        raise ValueError("Python publisher execution identity is incomplete") from None


def _publication_origin(selected, candidates, run):
    successful = [
        job
        for job in candidates
        if job.get("status") == "completed" and job.get("conclusion") == "success"
    ]
    checked = [(job, *_publisher_execution(job, run)) for job in successful]
    if len({job["id"] for job in successful}) != len(successful):
        raise ValueError("Python publisher execution identity is ambiguous")
    identity, original = _publisher_execution(selected, run)
    origins = [
        job
        for job, facts, genuine in checked
        if genuine
        and job["run_attempt"] < selected["run_attempt"]
        and facts == identity
    ]
    if len(origins) + int(original) > 1:
        raise ValueError("Python publisher execution origin is ambiguous")
    if not origins and not original:
        raise ValueError("Python publisher execution origin is unavailable")
    origin = origins[0] if origins else selected
    if sum(job["run_attempt"] == origin["run_attempt"] for job in candidates) != 1:
        raise ValueError("Python publisher execution origin attempt is ambiguous")
    for job in candidates:
        if job["run_attempt"] <= origin["run_attempt"]:
            continue
        # A later alias of this success cannot hide an intervening execution.
        # Incomplete/failed records also hold, without guessing their origin.
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or _publisher_execution(job, run)[0] != identity
        ):
            raise ValueError("newer Python publisher execution conflicts with origin")
    return origin


def publication_run(source, tag, workflow, required, publisher):
    """Require newest executions of Python jobs; overall/npm status is irrelevant."""
    runs = docs.api_pages(
        f"actions/workflows/{workflow}/runs?head_sha={source}&event=push",
        "workflow_runs",
    )
    matches = [r for r in runs if r.get("head_branch") == tag]
    if not matches:
        raise ValueError("no source-bound Python publication run")
    if any(type(r.get("id")) is not int or r["id"] < 1 for r in matches):
        raise ValueError("invalid publication run identity")
    run = max(matches, key=lambda r: r["id"])
    if (
        run.get("path") != ".github/workflows/" + workflow
        or run.get("head_sha") != source
        or run.get("event") != "push"
        or run.get("repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("head_repository", {}).get("full_name") != docs.REPOSITORY
        or run.get("pull_requests")
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] < 1
    ):
        raise ValueError("Python publication run/source differs")
    jobs = docs.api_pages(f"actions/runs/{run['id']}/jobs?filter=all", "jobs")
    selected = {}
    for name in required:
        candidates = [j for j in jobs if j.get("name") == name]
        if not candidates or any(
            type(j.get("run_attempt")) is not int
            or not 1 <= j["run_attempt"] <= run["run_attempt"]
            for j in candidates
        ):
            raise ValueError("Python publication job attempt is invalid or missing")
        latest = max(j["run_attempt"] for j in candidates)
        newest = [j for j in candidates if j["run_attempt"] == latest]
        if len(newest) != 1 or (
            newest[0].get("status") != "completed"
            or newest[0].get("conclusion") != "success"
            or type(newest[0].get("id")) is not int
            or newest[0]["id"] < 1
        ):
            raise ValueError("latest Python publication prerequisite did not succeed")
        selected[name] = newest[0]
    publisher_job = _publication_origin(
        selected[publisher], [j for j in jobs if j.get("name") == publisher], run
    )
    fresh = docs.api(f"actions/runs/{run['id']}")
    if any(
        fresh.get(k) != run.get(k)
        for k in ("id", "head_sha", "head_branch", "run_attempt")
    ):
        raise ValueError("Python publication run changed during qualification")
    return {
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "publication_attempt": publisher_job["run_attempt"],
        "publication_job_id": publisher_job["id"],
        "workflow": workflow,
    }


def distribution_metadata(project, version):
    value = json.loads(
        public_read(f"https://pypi.org/pypi/{project}/{version}/json", MAX_JSON)
    )
    if (
        value.get("info", {}).get("name") != project
        or value.get("info", {}).get("version") != version
    ):
        raise ValueError("Python registry project/version differs")
    stem = project.replace("-", "_") + "-" + version
    expected = {stem + "-py3-none-any.whl": "bdist_wheel", stem + ".tar.gz": "sdist"}
    urls = value.get("urls")
    if not isinstance(urls, list) or len(urls) != 2:
        raise ValueError("Python publication requires exactly its wheel and sdist")
    result = {}
    for item in urls:
        name = item.get("filename")
        digest = item.get("digests", {}).get("sha256")
        url = item.get("url")
        if (
            name not in expected
            or name in result
            or item.get("packagetype") != expected[name]
            or item.get("yanked") is not False
            or type(item.get("size")) is not int
            or not 0 < item["size"] <= MAX_ARTIFACT
            or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or not isinstance(url, str)
            or urlsplit(url).hostname != "files.pythonhosted.org"
        ):
            raise ValueError("Python publication artifact identity is invalid")
        result[name] = {"sha256": digest, "size": item["size"], "url": url}
    return result


def validate_wheel_metadata(raw, version, project="review-sensei", engine=None):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            metadata = [
                e for e in entries if e.filename.endswith(".dist-info/METADATA")
            ]
            if len(entries) > MAX_WHEEL_ENTRIES or len(metadata) != 1:
                raise ValueError("wheel metadata inventory differs")
            entry = metadata[0]
            if (
                entry.filename
                != f"{project.replace('-', '_')}-{version}.dist-info/METADATA"
                or not 0 < entry.file_size <= MAX_WHEEL_METADATA
            ):
                raise ValueError("wheel metadata size/name differs")
            with archive.open(entry) as stream:
                data = stream.read(MAX_WHEEL_METADATA + 1)
            if len(data) != entry.file_size or len(data) > MAX_WHEEL_METADATA:
                raise ValueError("wheel metadata size differs")
            fields = BytesParser().parsebytes(data, headersonly=True)
            if fields.get_all("Name", []) != [project] or fields.get_all(
                "Version", []
            ) != [version]:
                raise ValueError("wheel metadata package/version differs")
            if engine is not None:
                require_hosted_pin(fields.get_all("Requires-Dist", []), engine)
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise ValueError("wheel metadata is unavailable or malformed") from None


def verify_plan(path):
    """Credential-free child: verify signatures/transparency and exact OIDC claims."""
    from cryptography.x509.oid import ObjectIdentifier
    from pypi_attestations import AttestationType, Distribution, Provenance
    from sigstore.verify import policy

    plan = json.loads(path.read_text())
    for item in plan:
        publisher = {
            "kind": "GitHub",
            "repository": docs.REPOSITORY,
            "workflow": item["workflow"],
            "environment": item["environment"],
        }
        provenance = Provenance.model_validate_json(
            Path(item["provenance"]).read_bytes()
        )
        if len(provenance.attestation_bundles) != 1:
            raise ValueError("exactly one PyPI publication attestation bundle required")
        bundle = provenance.attestation_bundles[0]
        if bundle.publisher.model_dump() != publisher or len(bundle.attestations) != 1:
            raise ValueError("PyPI publisher/workflow/environment differs")
        workflow_uri = f"https://github.com/{docs.REPOSITORY}/.github/workflows/{item['workflow']}@refs/tags/{item['tag']}"

        class Environment:
            def verify(self, cert):
                # Fulcio's GitHub environment claim is a DER UTF8String. Both
                # supported environment names are short fixed ASCII strings.
                expected = item["environment"].encode("ascii")
                actual = cert.extensions.get_extension_for_oid(
                    ObjectIdentifier("1.3.6.1.4.1.57264.1.23")
                ).value.value
                if actual != bytes([12, len(expected)]) + expected:
                    raise ValueError("signed PyPI environment claim differs")

        identity = policy.AllOf(
            [
                policy.OIDCIssuerV2("https://token.actions.githubusercontent.com"),
                policy.OIDCBuildSignerURI(workflow_uri),
                policy.OIDCBuildSignerDigest(item["source"]),
                policy.OIDCRunnerEnvironment("github-hosted"),
                policy.OIDCSourceRepositoryURI("https://github.com/" + docs.REPOSITORY),
                policy.OIDCSourceRepositoryDigest(item["source"]),
                policy.OIDCSourceRepositoryRef("refs/tags/" + item["tag"]),
                policy.OIDCBuildTrigger("push"),
                policy.OIDCRunInvocationURI(
                    f"https://github.com/{docs.REPOSITORY}/actions/runs/{item['run_id']}/attempts/{item['publication_attempt']}"
                ),
                Environment(),
            ]
        )
        kind, _ = bundle.attestations[0].verify(
            identity, Distribution.from_file(Path(item["artifact"]))
        )
        if kind != AttestationType.PYPI_PUBLISH_V1:
            raise ValueError("PyPI publication attestation predicate differs")


def qualify(source, version, github_version, output):
    if not docs.SHA.fullmatch(source):
        raise ValueError("exact Python release source required")
    output.mkdir(parents=True, exist_ok=False)
    projects = [
        (
            "review-sensei",
            version,
            "v" + version,
            "release.yml",
            "pypi",
            ENGINE_JOBS,
            PUBLISH_JOB,
        ),
        (
            "review-sensei-github",
            github_version,
            "github-v" + github_version,
            "release-github.yml",
            "pypi-github",
            {HOSTED_JOB},
            HOSTED_JOB,
        ),
    ]
    engine_identity = signed_release("v" + version)
    hosted_identity = signed_release("github-v" + github_version)
    if hosted_identity[1] != source:
        raise ValueError("hosted immutable tag does not bind the selected source")
    equivalence = core_equivalence(engine_identity[1], source)
    receipt = {"source": source, "core_equivalence": equivalence, "projects": {}}
    plan = []
    for (
        project,
        selected_version,
        tag,
        workflow,
        environment,
        required,
        publisher,
    ) in projects:
        tag_object, tag_source = (
            engine_identity if project == "review-sensei" else hosted_identity
        )
        run = publication_run(tag_source, tag, workflow, required, publisher)
        artifacts = distribution_metadata(project, selected_version)
        record = {
            "version": selected_version,
            "tag": tag,
            "tag_object": tag_object,
            "source": tag_source,
            **run,
            "artifacts": artifacts,
        }
        receipt["projects"][project] = record
        for name, metadata in artifacts.items():
            raw = public_read(metadata["url"], MAX_ARTIFACT)
            if (
                len(raw) != metadata["size"]
                or hashlib.sha256(raw).hexdigest() != metadata["sha256"]
            ):
                raise ValueError("PyPI distribution bytes differ from registry digest")
            artifact = output / name
            artifact.write_bytes(raw)
            if name.endswith(".whl"):
                validate_wheel_metadata(
                    raw,
                    selected_version,
                    project,
                    version if project.endswith("-github") else None,
                )
            provenance = output / (name + ".provenance.json")
            provenance.write_bytes(
                public_read(
                    f"https://pypi.org/integrity/{project}/{selected_version}/{name}/provenance",
                    MAX_PROVENANCE,
                )
            )
            plan.append(
                {
                    "artifact": str(artifact.resolve()),
                    "provenance": str(provenance.resolve()),
                    "source": tag_source,
                    "tag": tag,
                    "workflow": workflow,
                    "environment": environment,
                    **run,
                }
            )
    plan_path = output / "verification-plan.json"
    plan_path.write_text(json.dumps(plan))
    home = output / "verifier-home"
    home.mkdir()
    try:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--verify-plan",
                str(plan_path.resolve()),
            ],
            env=clean_environment(home.resolve()),
            capture_output=True,
            check=True,
            timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        raise ValueError(
            "PyPI cryptographic publication verification failed or timed out"
        ) from None
    assert_current(receipt)
    return receipt


def assert_current(receipt):
    if set(receipt["projects"]) != {"review-sensei", "review-sensei-github"}:
        raise ValueError("both Python publication identities are required")
    for project, record in receipt["projects"].items():
        expected_source = record.get("source", receipt["source"])
        if project == "review-sensei-github" and expected_source != receipt["source"]:
            raise ValueError("hosted publication differs from selected workflow source")
        if signed_release(record["tag"]) != (record["tag_object"], expected_source):
            raise ValueError("immutable Python release tag changed during validation")
        if distribution_metadata(project, record["version"]) != record["artifacts"]:
            raise ValueError(
                "public PyPI distribution identity changed during validation"
            )
    engine_source = receipt["projects"]["review-sensei"].get(
        "source", receipt["source"]
    )
    if engine_source != receipt["source"] or "core_equivalence" in receipt:
        if "core_equivalence" not in receipt:
            raise ValueError(
                "split Python sources require core build-input equivalence"
            )
        if (
            core_equivalence(engine_source, receipt["source"])
            != receipt["core_equivalence"]
        ):
            raise ValueError("core build-input equivalence changed during validation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-plan", type=Path)
    parser.add_argument("--source")
    parser.add_argument("--version")
    parser.add_argument("--github-version")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.verify_plan:
            verify_plan(args.verify_plan)
        else:
            if not all([args.source, args.version, args.github_version, args.output]):
                parser.error("source, versions and fresh output required")
            result = qualify(
                args.source, args.version, args.github_version, args.output
            )
            (args.output / "publication.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
        return 0
    except Exception:
        print(
            "Python publication verification failed; raw diagnostics withheld",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
