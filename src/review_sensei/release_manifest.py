"""Versioned release compatibility manifest validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ReviewInputError
from .schemas import validate_public_document

_SHA = re.compile(r"^[a-f0-9]{64}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


@dataclass(frozen=True)
class Artifact:
    name: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > 128
            or not isinstance(self.version, str)
            or not self.version.strip()
            or len(self.version) > 128
            or not isinstance(self.sha256, str)
            or not _SHA.fullmatch(self.sha256)
        ):
            raise ReviewInputError("release artifact identity is malformed")


@dataclass(frozen=True)
class CompatibilityManifest:
    release: str
    workflow: Artifact
    python: Artifact
    npm: tuple[Artifact, ...]
    schemas_version: str
    worker: Artifact
    compatible_worker_range: str
    provenance: str

    def __post_init__(self) -> None:
        if not isinstance(self.release, str) or not _VERSION.fullmatch(self.release):
            raise ReviewInputError("release version must be semantic versioning")
        if (
            not isinstance(self.schemas_version, str)
            or not self.schemas_version.strip()
        ):
            raise ReviewInputError("schema version is required")
        if (
            not isinstance(self.compatible_worker_range, str)
            or not self.compatible_worker_range.strip()
        ):
            raise ReviewInputError("compatible worker range is required")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ReviewInputError("manifest provenance is required")
        if (
            not isinstance(self.npm, tuple)
            or not self.npm
            or any(not isinstance(artifact, Artifact) for artifact in self.npm)
        ):
            raise ReviewInputError("npm artifacts must be a non-empty tuple")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompatibilityManifest":
        if not isinstance(value, Mapping):
            raise ReviewInputError("compatibility manifest must be an object")
        validate_public_document(dict(value), "compatibility-manifest")
        try:
            artifacts = value["artifacts"]
            npm = tuple(Artifact(**item) for item in artifacts["npm"])
            return cls(
                release=value["release"],
                workflow=Artifact(**artifacts["workflow"]),
                python=Artifact(**artifacts["python"]),
                npm=npm,
                schemas_version=artifacts["schemas_version"],
                worker=Artifact(**artifacts["worker"]),
                compatible_worker_range=value["compatible_worker_range"],
                provenance=value["provenance"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewInputError("compatibility manifest is incomplete") from exc

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": "1.0",
            "release": self.release,
            "compatible_worker_range": self.compatible_worker_range,
            "provenance": self.provenance,
            "artifacts": {
                "workflow": self.workflow.__dict__,
                "python": self.python.__dict__,
                "npm": [item.__dict__ for item in self.npm],
                "schemas_version": self.schemas_version,
                "worker": self.worker.__dict__,
            },
        }
        validate_public_document(value, "compatibility-manifest")
        return value


def validate_compatibility_manifest(value: Mapping[str, Any]) -> CompatibilityManifest:
    manifest = CompatibilityManifest.from_dict(value)
    if (
        manifest.release != manifest.python.version
        or manifest.release != manifest.worker.version
        or manifest.release != manifest.workflow.version
    ):
        raise ReviewInputError("release artifacts do not share one release version")
    if not manifest.npm or any(
        item.version != manifest.release for item in manifest.npm
    ):
        raise ReviewInputError("npm artifacts do not match the release version")
    return manifest
