#!/usr/bin/env python3
"""Bind a major-channel event to signed release data; never execute tagged source."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import check_release_version as versions
import release_docs as docs

MAJOR = re.compile(r"v(?:0|[1-9][0-9]*)\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
MAX_FILE = 512 * 1024
MAX_WHEEL_METADATA = 64 * 1024
MAX_WHEEL_ENTRIES = 4096
SBOM = "review-sensei-sbom.spdx.json"


def sha(value):
    if not isinstance(value, str) or not docs.SHA.fullmatch(value):
        raise ValueError("invalid object/source SHA")
    return value


def event_identity(event, name, env):
    if (
        env.get("GITHUB_REPOSITORY") != docs.REPOSITORY
        or event.get("repository", {}).get("full_name") != docs.REPOSITORY
    ):
        raise ValueError("major validation requires the same public repository")
    if name == "push":
        ref = event.get("ref", "")
        tag = ref.removeprefix("refs/tags/")
        if not ref.startswith("refs/tags/") or not MAJOR.fullmatch(tag):
            return None
        if event.get("deleted") is not False or event.get("after") == "0" * 40:
            return None
        if env.get("GITHUB_REF") != ref:
            raise ValueError("event/context ref differs")
        return {
            "channel": tag,
            "event_sha": sha(env.get("GITHUB_SHA")),
            "after": sha(event.get("after")),
            "mode": "push",
        }
    if name == "workflow_dispatch":
        inputs = event.get("inputs", {})
        tag = inputs.get("channel", "")
        if not MAJOR.fullmatch(tag):
            raise ValueError(
                "dispatch requires a lowercase canonical numeric-major tag"
            )
        if (
            env.get("GITHUB_REF")
            != "refs/heads/" + event["repository"]["default_branch"]
        ):
            raise ValueError("object-bound dispatch must execute on the default branch")
        return {
            "channel": tag,
            "tag_object": sha(inputs.get("tag_object")),
            "source_sha": sha(inputs.get("source_sha")),
            "event_sha": sha(env.get("GITHUB_SHA")),
            "mode": "object-bound-dispatch",
        }
    raise ValueError("unsupported major-channel event")


def signed_major(tag):
    if not MAJOR.fullmatch(tag):
        raise ValueError("invalid major channel")
    ref = docs.api(f"git/ref/tags/{tag}")
    if (
        ref.get("ref") != f"refs/tags/{tag}"
        or ref.get("object", {}).get("type") != "tag"
    ):
        raise ValueError("major channel must be an annotated signed tag")
    object_sha = sha(ref["object"]["sha"])
    value = docs.api(f"git/tags/{object_sha}")
    # Same trust policy as release_docs.tag_identity: GitHub must verify the
    # annotated tag signature, and nesting/lightweight tags are not accepted.
    if (
        value.get("sha") != object_sha
        or value.get("tag") != tag
        or value.get("object", {}).get("type") != "commit"
        or value.get("verification", {}).get("verified") is not True
    ):
        raise ValueError("major tag signature/object is not verified by GitHub")
    return object_sha, sha(value["object"]["sha"])


def bind_event(selection, tag_object, source):
    if selection["mode"] == "push":
        # GitHub may expose a peeled commit in the push/context SHA. When it
        # exposes the tag object, bind that too; never resolve a different head.
        if selection["after"] not in {tag_object, source} or selection[
            "event_sha"
        ] not in {tag_object, source}:
            raise ValueError("stale promotion: current tag differs from the event")
        binding = (
            "tag-object"
            if tag_object in {selection["after"], selection["event_sha"]}
            else "peeled-source"
        )
    else:
        if (selection["tag_object"], selection["source_sha"]) != (tag_object, source):
            raise ValueError("stale object-bound promotion dispatch")
        binding = "tag-object"
    return {
        **selection,
        "tag_object": tag_object,
        "source_sha": source,
        "event_binding": binding,
        "event_object_bound": binding == "tag-object",
    }


def source_file(source, relative):
    data = docs.api(f"contents/{relative}?ref={sha(source)}")
    if (
        data.get("type") != "file"
        or data.get("encoding") != "base64"
        or type(data.get("size")) is not int
        or not 0 < data["size"] <= MAX_FILE
    ):
        raise ValueError("source file is unavailable, unsupported or oversized")
    raw = base64.b64decode("".join(data["content"].split()), validate=True)
    if len(raw) != data["size"]:
        raise ValueError("source file size differs")
    return raw


def release_binding(release, version, tag_object, source):
    tag = "v" + version
    if (
        type(release.get("id")) is not int
        or release["id"] < 1
        or release.get("tag_name") != tag
        or release.get("target_commitish") != source
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or release.get("name") != f"ReviewSensei {tag}"
    ):
        raise ValueError("published stable release identity differs")
    body = release.get("body", "")
    if not isinstance(body, str) or len(body.encode()) > MAX_FILE:
        raise ValueError("unbounded release body")
    matches = re.findall(r"<!-- reviewsensei-release:(.*?) -->", body)
    if len(matches) != 1:
        raise ValueError("one authoritative release binding is required")
    binding = json.loads(matches[0])
    if not isinstance(binding, dict):
        raise ValueError("release binding is not a JSON object")
    if (
        type(binding.get("schema")) is not int
        or binding.get("schema") != 1
        or binding.get("repository") != docs.REPOSITORY
        or binding.get("tag") != tag
        or binding.get("tag_object") != tag_object
        or binding.get("source") != source
        or type(binding.get("run_id")) is not int
        or binding["run_id"] < 1
    ):
        raise ValueError("release provenance binding differs")
    expected = {
        f"review_sensei-{version}-py3-none-any.whl",
        f"review_sensei-{version}.tar.gz",
        "SHA256SUMS",
        SBOM,
    }
    assets = binding.get("assets")
    if (
        not isinstance(assets, dict)
        or set(assets) != expected
        or any(
            not isinstance(v, str) or not DIGEST.fullmatch(v) for v in assets.values()
        )
    ):
        raise ValueError("release asset inventory/digests differ")
    return binding


def published_assets(release_id, binding):
    # Five requested entries suffice to prove or reject this exact four-item
    # inventory. A fifth item fails before any later page can be fetched.
    assets, has_next = docs.api_first_page(
        f"releases/{release_id}/assets?per_page=5&page=1"
    )
    if has_next or not isinstance(assets, list) or len(assets) > 4:
        raise ValueError(
            "published release asset inventory exceeds the exact four-item bound"
        )
    result = {}
    for item in assets:
        name = item.get("name")
        if (
            name not in binding["assets"]
            or name in result
            or type(item.get("id")) is not int
            or item["id"] < 1
            or item.get("state") != "uploaded"
            or type(item.get("size")) is not int
            or not 0 < item["size"] <= 32 * 1024 * 1024
            or item.get("digest") != "sha256:" + binding["assets"][name]
        ):
            raise ValueError("published release assets are incomplete or differ")
        result[name] = item
    if set(result) != set(binding["assets"]):
        raise ValueError("published release assets are incomplete")
    return result


def validate_wheel_metadata(raw, version):
    """Read bounded wheel metadata as data, before eligibility or installation."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            metadata = [
                entry
                for entry in entries
                if entry.filename.endswith(".dist-info/METADATA")
            ]
            if len(entries) > MAX_WHEEL_ENTRIES or len(metadata) != 1:
                raise ValueError("wheel metadata inventory differs")
            entry = metadata[0]
            if (
                entry.filename != f"review_sensei-{version}.dist-info/METADATA"
                or not 0 < entry.file_size <= MAX_WHEEL_METADATA
            ):
                raise ValueError("wheel metadata path or size differs")
            with archive.open(entry) as stream:
                data = stream.read(MAX_WHEEL_METADATA + 1)
            if len(data) != entry.file_size or len(data) > MAX_WHEEL_METADATA:
                raise ValueError("wheel metadata size differs")
            fields = BytesParser().parsebytes(data, headersonly=True)
            names, versions = fields.get_all("Name", []), fields.get_all("Version", [])
            if (
                len(names) != 1
                or re.sub(r"[-_.]+", "-", names[0]).lower() != "review-sensei"
                or versions != [version]
            ):
                raise ValueError("wheel metadata package/version differs")
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise ValueError("wheel metadata is unavailable or malformed") from error


def verify_wheel(asset, binding, version, output):
    # Read by validated numeric API id, never by an artifact-provided URL.
    fetched = docs.gh_run(
        [
            "gh",
            "api",
            f"repos/{docs.REPOSITORY}/releases/assets/{asset['id']}",
            "-H",
            "Accept: application/octet-stream",
            "--hostname",
            "github.com",
        ],
        "GitHub release wheel download",
    )
    digest = hashlib.sha256(fetched.stdout).hexdigest()
    name = f"review_sensei-{version}-py3-none-any.whl"
    if len(fetched.stdout) != asset["size"] or digest != binding["assets"][name]:
        raise ValueError("release wheel bytes differ from authoritative digest")
    validate_wheel_metadata(fetched.stdout, version)
    wheel = output / name
    wheel.write_bytes(fetched.stdout)
    docs.gh_run(
        [
            "gh",
            "attestation",
            "verify",
            str(wheel),
            "--repo",
            docs.REPOSITORY,
            "--signer-workflow",
            f"{docs.REPOSITORY}/.github/workflows/release.yml",
            "--source-digest",
            binding["source"],
            "--source-ref",
            f"refs/tags/v{version}",
            "--deny-self-hosted-runners",
            "--format",
            "json",
            "--hostname",
            "github.com",
        ],
        "GitHub wheel attestation verification",
    )
    return digest


def assert_current(receipt):
    if signed_major(receipt["channel"]) != (
        receipt["tag_object"],
        receipt["source_sha"],
    ):
        raise ValueError(
            "major tag changed during validation; no current-channel qualification"
        )
    if docs.tag_identity(receipt["release_tag"]) != (
        receipt["release_tag_object"],
        receipt["source_sha"],
    ):
        raise ValueError("immutable release tag changed during validation")
    fresh = docs.api(f"releases/tags/{receipt['release_tag']}")
    binding = release_binding(
        fresh, receipt["version"], receipt["release_tag_object"], receipt["source_sha"]
    )
    if binding != receipt["release_binding"]:
        raise ValueError("published release binding changed during validation")
    published_assets(fresh["id"], binding)


def qualify(selection, output, *, reserved=False):
    from major_tag_surface import validate_surface

    if not reserved:
        output.mkdir(parents=True, exist_ok=False)
    selected = bind_event(selection, *signed_major(selection["channel"]))
    source = selected["source_sha"]
    snapshot = output / "source-data"
    relatives = [
        "pyproject.toml",
        "docs/site/data/site-manifest.json",
        ".github/workflows/review-sensei-run.yml",
        ".github/workflows/review-sensei-review.yml",
        "packages/npm/cli/package.json",
        *[
            f"packages/npm/platforms/{name.split('/cli-', 1)[1]}/package.json"
            for name in versions.NPM_TARGETS
        ],
    ]
    hashes = {}
    for relative in relatives:
        raw = source_file(source, relative)
        path = snapshot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        hashes[relative] = hashlib.sha256(raw).hexdigest()
    project = tomllib.loads((snapshot / "pyproject.toml").read_text())["project"]
    version = project["version"]
    docs.version_tuple("v" + version)
    if project.get("name") != "review-sensei":
        raise ValueError("tagged package manifest names a different distribution")
    facts = json.loads((snapshot / "docs/site/data/site-manifest.json").read_text())[
        "release_facts"
    ]
    if (facts["version"], facts["tag"]) != (version, "v" + version):
        raise ValueError("site/package release manifests differ")
    versions.validate_npm_manifests(snapshot, version)
    immutable_object, immutable_source = docs.tag_identity("v" + version)
    if immutable_source != source:
        raise ValueError(
            "major channel does not target its exact immutable released source"
        )
    release = docs.api(f"releases/tags/v{version}")
    binding = release_binding(release, version, immutable_object, source)
    run = docs.api(f"actions/runs/{binding['run_id']}")
    if (
        run.get("id") != binding["run_id"]
        or run.get("head_sha") != source
        or run.get("head_branch") != "v" + version
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] < 1
    ):
        raise ValueError("published release run/source differs")
    docs.validate_run(
        run, docs.api_pages(f"actions/runs/{binding['run_id']}/jobs?filter=all", "jobs")
    )
    if docs.api(f"compare/{source}...main").get("status") not in {"ahead", "identical"}:
        raise ValueError("released source is not an ancestor of reviewed main")
    assets = published_assets(release["id"], binding)
    wheel_digest = verify_wheel(
        assets[f"review_sensei-{version}-py3-none-any.whl"], binding, version, output
    )
    try:
        surface = validate_surface(snapshot)
    except ValueError as error:
        surface = {"status": "ERROR", "data_only": True, "error": str(error)}
    receipt = {
        **selected,
        "version": version,
        "release_tag": "v" + version,
        "release_tag_object": immutable_object,
        "release_run_id": binding["run_id"],
        "release_binding": binding,
        "wheel_sha256": wheel_digest,
        "source_data_sha256": hashes,
        "surface": surface,
        "status": "PASS" if surface["status"] == "PASS" else "ERROR",
        "gaps": [
            "No live provider, broker or PR publication execution",
            "Push payloads exposing only a peeled source cannot identify a same-source re-sign before admission; dispatch binds the exact object",
            "Installed CLI limits are representative; native/npm package execution is outside the PyPI lane",
        ],
    }
    if surface["status"] != "PASS":
        receipt["error"] = surface["error"]
    assert_current(receipt)
    return receipt


def workflow_outputs(receipt):
    values = {"eligible": str(receipt["status"] == "PASS").lower()}
    if receipt["status"] == "PASS":
        values.update(
            {
                key: receipt[key]
                for key in [
                    "version",
                    "wheel_sha256",
                    "source_sha",
                    "tag_object",
                    "channel",
                ]
            }
        )
    if any(
        not isinstance(value, str) or "\n" in value or "\r" in value
        for value in values.values()
    ):
        raise ValueError("workflow output must be a single-line string")
    return "".join(key + "=" + value + "\n" for key, value in values.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--recheck", type=Path)
    args = parser.parse_args()
    receipt = {"status": "ERROR"}
    owned_output = False
    try:
        # Reserve a fresh namespace before any reads. Never replace prior evidence,
        # including when the input is invalid or a recheck fails.
        args.output.mkdir(parents=True, exist_ok=False)
        owned_output = True
        if args.recheck:
            receipt = json.loads(args.recheck.read_text())
            if receipt.get("status") != "PASS":
                raise ValueError("cannot recheck an unqualified receipt")
            assert_current(receipt)
            receipt = {**receipt, "current_identity_rechecked": True}
            if "CLI_RESULT" in os.environ:
                receipt["cli_lanes_result"] = os.environ["CLI_RESULT"]
                if receipt["cli_lanes_result"] != "success":
                    receipt["status"] = "ERROR"
                    receipt["error"] = "at least one installed CLI lane did not succeed"
        else:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            selected = event_identity(
                event, os.environ["GITHUB_EVENT_NAME"], os.environ
            )
            receipt = (
                qualify(selected, args.output, reserved=True)
                if selected
                else {
                    "status": "IGNORED",
                    "reason": "not a live canonical numeric-major tag push",
                }
            )
        if os.environ.get("GITHUB_OUTPUT"):
            lines = workflow_outputs(receipt)
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write(lines)
    except Exception as error:
        # subprocess diagnostics can include authentication details: never copy them.
        receipt = {
            "status": "ERROR",
            "error": (
                "output must be a new directory; existing evidence left unchanged"
                if isinstance(error, FileExistsError) and not owned_output
                else str(error)
                if isinstance(error, ValueError)
                else type(error).__name__
            ),
        }
        if os.environ.get("GITHUB_OUTPUT"):
            try:
                with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                    stream.write("eligible=false\n")
            except (OSError, ValueError):
                print("workflow refusal output could not be written", file=sys.stderr)
    if owned_output:
        (args.output / "gate.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return receipt["status"] not in {"PASS", "IGNORED"}


if __name__ == "__main__":
    sys.exit(main())
