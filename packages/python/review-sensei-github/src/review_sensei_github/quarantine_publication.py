"""Funded immutable publication proof for dormant V3 quarantine entry.

Every network read uses the original caller. Cryptographic verification is
offline; unavailable verifier dependencies or trust roots refuse. Serialized
receipts are comparison evidence, never process-issued authority.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import time
import weakref
from dataclasses import dataclass
from datetime import datetime
from urllib.request import Request

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

from .caller_operation import caller_for_http

REPOSITORY = "malsabbagh/review-sensei"
MAX_READS = 14
MAX_BYTES = 512 * 1024
# First issuance is 14 reads; each later boundary checks the two immutable tags.
# The first startup can perform issuance + creator revalidation + cold origin
# reconstruction twice. The fixed 32-slot envelope leaves room for those four
# tag pairs without charging protected terminal or reconciliation allocations.
STARTUP_READ_SLOTS = 32
_ISSUED: weakref.WeakSet[PublicationProof] = weakref.WeakSet()


def _require_hosted_pin(requires, version):
    if not isinstance(requires, list) or any(not isinstance(r, str) for r in requires):
        raise ReviewInputError("quarantine hosted dependency metadata is invalid")
    pins = [r for r in requires if re.match(r"review[-_.]sensei\s*[=<>!~(;]", r, re.I)]
    if pins != ["review-sensei==" + version]:
        raise ReviewInputError("quarantine hosted package does not pin the core")


def _sha(value, width=40):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-f0-9]{" + str(width) + "}", value) is not None
    )


def _time(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value)
        is None
    ):
        raise ReviewInputError("quarantine publication timestamp is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReviewInputError("quarantine publication timestamp is invalid") from error


@dataclass(frozen=True, eq=False, init=False)
class PublicationProof:
    _caller: object
    _intent: bytes
    _receipt: bytes

    def __init__(self, http, intent, receipt):
        object.__setattr__(self, "_caller", caller_for_http(http))
        object.__setattr__(self, "_intent", canonical_bytes(intent))
        object.__setattr__(self, "_receipt", canonical_bytes(receipt))

    @property
    def receipt(self):
        return json.loads(self._receipt)

    def require(self, http, intent, source):
        if (
            self not in _ISSUED
            or self._caller is None
            or caller_for_http(http) is not self._caller
            or self._intent != canonical_bytes(intent)
            or self.receipt["source_commit"] != source
        ):
            raise ReviewInputError("quarantine publication proof is foreign")
        return self.receipt

    def require_before(self, created_at):
        if self not in _ISSUED or any(
            _time(p["completed_at"]) > _time(created_at)
            for p in self.receipt["projects"]
        ):
            raise ReviewInputError(
                "quarantine publication must precede creator execution"
            )


def _verify(provenance, artifact, project):
    """Use the same official PyPI signature/identity claims as the installer."""
    try:
        from cryptography.x509.oid import ObjectIdentifier
        from pypi_attestations import AttestationType, Distribution, Provenance
        from sigstore.verify import policy

        value = Provenance.model_validate_json(provenance)
        if len(value.attestation_bundles) != 1:
            raise ValueError("publication bundle union")
        bundle = value.attestation_bundles[0]
        publisher = {
            "kind": "GitHub",
            "repository": REPOSITORY,
            "workflow": project["workflow"],
            "environment": project["environment"],
        }
        if bundle.publisher.model_dump() != publisher or len(bundle.attestations) != 1:
            raise ValueError("publication publisher")

        class Environment:
            def verify(self, cert):
                expected = project["environment"].encode("ascii")
                actual = cert.extensions.get_extension_for_oid(
                    ObjectIdentifier("1.3.6.1.4.1.57264.1.23")
                ).value.value
                if actual != bytes([12, len(expected)]) + expected:
                    raise ValueError("publication environment")

        identity = policy.AllOf(
            [
                policy.OIDCIssuerV2("https://token.actions.githubusercontent.com"),
                policy.OIDCBuildSignerURI(
                    f"https://github.com/{REPOSITORY}/.github/workflows/{project['workflow']}@refs/tags/{project['tag']}"
                ),
                policy.OIDCBuildSignerDigest(project["source"]),
                policy.OIDCRunnerEnvironment("github-hosted"),
                policy.OIDCSourceRepositoryURI("https://github.com/" + REPOSITORY),
                policy.OIDCSourceRepositoryDigest(project["source"]),
                policy.OIDCSourceRepositoryRef("refs/tags/" + project["tag"]),
                policy.OIDCBuildTrigger("push"),
                policy.OIDCRunInvocationURI(
                    f"https://github.com/{REPOSITORY}/actions/runs/{project['run_id']}/attempts/{project['run_attempt']}"
                ),
                Environment(),
            ]
        )
        kind, _ = bundle.attestations[0].verify(
            identity,
            Distribution(name=artifact["filename"], digest=artifact["sha256"]),
            offline=True,
        )
        if kind != AttestationType.PYPI_PUBLISH_V1:
            raise ValueError("publication predicate")
    except Exception as error:
        raise ReviewInputError(
            "quarantine publication cryptographic verification failed"
        ) from error


def authenticate_publication(*, http, token, intent, source):
    if (
        not _sha(source)
        or not isinstance(intent, dict)
        or set(intent) != {"hosted_version", "core_version"}
        or any(
            not isinstance(v, str)
            or re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", v)
            is None
            for v in intent.values()
        )
    ):
        raise ReviewInputError("quarantine publication intent is invalid")
    caller = caller_for_http(http)
    if caller is None:
        raise ReviewInputError("quarantine publication has no original caller")
    _require_hosted_pin(
        importlib.metadata.requires("review-sensei-github"), intent["core_version"]
    )
    cache = getattr(caller, "_quarantine_publication_proofs", {})
    key = hashlib.sha256(canonical_bytes(intent)).hexdigest()
    cached = cache.get(key)
    count, deadline = 0, time.monotonic() + 180

    def get(path, *, public=False):
        nonlocal count
        if count >= MAX_READS or time.monotonic() >= deadline:
            raise ReviewInputError("quarantine publication read bound exhausted")
        count += 1
        if public:
            request = Request(
                "https://pypi.org" + path,
                headers={"User-Agent": "ReviewSensei-quarantine-provenance"},
                method="GET",
            )
            with caller.open(
                request, timeout=min(15, deadline - time.monotonic())
            ) as response:
                raw = response.read(MAX_BYTES + 1)
                if response.status != 200 or len(raw) > MAX_BYTES:
                    raise ReviewInputError(
                        "quarantine registry response is unavailable or oversized"
                    )
                value = json.loads(raw)
        else:
            status, value = http.request(
                "GET", http.repository_path(REPOSITORY, path), token=token
            )
            if status != 200:
                raise ReviewInputError("quarantine publication metadata is unavailable")
        if not isinstance(value, dict):
            raise ReviewInputError("quarantine publication metadata is malformed")
        return value

    if type(cached) is PublicationProof:
        receipt = cached.require(http, intent, source)
        for project in receipt["projects"]:
            ref = get("/git/ref/tags/" + project["tag"])
            if (
                ref.get("ref") != "refs/tags/" + project["tag"]
                or ref.get("object", {}).get("type") != "tag"
                or ref.get("object", {}).get("sha") != project["tag_object"]
            ):
                raise ReviewInputError("quarantine immutable publication tag changed")
        return cached

    projects = []
    for name, version, tag, workflow, environment, publisher in (
        (
            "review-sensei",
            intent["core_version"],
            "v" + intent["core_version"],
            "release.yml",
            "pypi",
            "Publish to PyPI with Trusted Publishing",
        ),
        (
            "review-sensei-github",
            intent["hosted_version"],
            "github-v" + intent["hosted_version"],
            "release-github.yml",
            "pypi-github",
            "Publish review-sensei-github to PyPI",
        ),
    ):
        if importlib.metadata.version(name) != version:
            raise ReviewInputError("quarantine installed package identity differs")
        ref = get("/git/ref/tags/" + tag)
        obj = ref.get("object", {})
        if (
            ref.get("ref") != "refs/tags/" + tag
            or obj.get("type") != "tag"
            or not _sha(obj.get("sha"))
        ):
            raise ReviewInputError(
                "quarantine publication needs an immutable annotated tag"
            )
        signed = get("/git/tags/" + obj["sha"])
        commit = signed.get("object", {}).get("sha")
        if (
            signed.get("sha") != obj["sha"]
            or signed.get("tag") != tag
            or signed.get("object", {}).get("type") != "commit"
            or signed.get("verification", {}).get("verified") is not True
            or not _sha(commit)
            or name.endswith("-github")
            and commit != source
        ):
            raise ReviewInputError("quarantine signed publication source conflicts")
        runs = get(f"/actions/workflows/{workflow}/runs?head_sha={commit}&event=push")
        rows = runs.get("workflow_runs")
        if (
            not isinstance(rows, list)
            or runs.get("total_count") != len(rows)
            or not 1 <= len(rows) <= 100
        ):
            raise ReviewInputError("quarantine publication run inventory is incomplete")
        matches = [r for r in rows if r.get("head_branch") == tag]
        if not matches or any(
            type(r.get("id")) is not int or r["id"] < 1 for r in matches
        ):
            raise ReviewInputError("quarantine publication run is absent")
        run = max(matches, key=lambda r: r["id"])
        if (
            run.get("head_sha") != commit
            or run.get("event") != "push"
            or run.get("path") != ".github/workflows/" + workflow
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("head_repository", {}).get("full_name") != REPOSITORY
            or run.get("pull_requests")
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] < 1
        ):
            raise ReviewInputError("quarantine publisher source/run conflicts")
        inventory = get(f"/actions/runs/{run['id']}/jobs?filter=all")
        jobs = inventory.get("jobs")
        if (
            not isinstance(jobs, list)
            or inventory.get("total_count") != len(jobs)
            or not 1 <= len(jobs) <= 100
        ):
            raise ReviewInputError("quarantine publisher job inventory is incomplete")
        candidates = [j for j in jobs if j.get("name") == publisher]
        if not candidates or any(
            type(j.get("run_attempt")) is not int
            or not 1 <= j["run_attempt"] <= run["run_attempt"]
            for j in candidates
        ):
            raise ReviewInputError("quarantine publisher attempt is invalid")
        latest = max(j["run_attempt"] for j in candidates)
        selected = [j for j in candidates if j["run_attempt"] == latest]
        if (
            len(selected) != 1
            or selected[0].get("status") != "completed"
            or selected[0].get("conclusion") != "success"
            or type(selected[0].get("id")) is not int
            or selected[0]["id"] < 1
        ):
            raise ReviewInputError("quarantine publication did not succeed")
        _time(selected[0].get("completed_at"))
        fresh = get(f"/actions/runs/{run['id']}")
        if any(
            fresh.get(k) != run.get(k)
            for k in ("id", "head_sha", "head_branch", "run_attempt")
        ):
            raise ReviewInputError("quarantine publisher changed during authentication")
        metadata = get(f"/pypi/{name}/{version}/json", public=True)
        filename = name.replace("-", "_") + "-" + version + "-py3-none-any.whl"
        artifacts = metadata.get("urls")
        if (
            metadata.get("info", {}).get("name") != name
            or metadata.get("info", {}).get("version") != version
            or not isinstance(artifacts, list)
        ):
            raise ReviewInputError("quarantine registry package identity conflicts")
        if name.endswith("-github"):
            _require_hosted_pin(
                metadata["info"].get("requires_dist"), intent["core_version"]
            )
        wheels = [a for a in artifacts if a.get("filename") == filename]
        if (
            len(wheels) != 1
            or wheels[0].get("yanked") is not False
            or wheels[0].get("packagetype") != "bdist_wheel"
            or not _sha(wheels[0].get("digests", {}).get("sha256"), 64)
        ):
            raise ReviewInputError("quarantine wheel identity is ambiguous")
        artifact = {"filename": filename, "sha256": wheels[0]["digests"]["sha256"]}
        project = {
            "name": name,
            "version": version,
            "tag": tag,
            "tag_object": obj["sha"],
            "source": commit,
            "workflow": workflow,
            "environment": environment,
            "run_id": run["id"],
            "run_attempt": latest,
            "job_id": selected[0]["id"],
            "completed_at": selected[0]["completed_at"],
            **artifact,
        }
        provenance = get(
            f"/integrity/{name}/{version}/{filename}/provenance", public=True
        )
        _verify(canonical_bytes(provenance), artifact, project)
        projects.append(project)
    proof = PublicationProof(
        http,
        intent,
        {
            "schema_version": "quarantine-publication-scope-v1",
            "source_commit": source,
            "projects": projects,
        },
    )
    _ISSUED.add(proof)
    cache[key] = proof
    caller._quarantine_publication_proofs = cache
    return proof
