"""Versioned release compatibility manifest validation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ReviewInputError
from .schemas import validate_public_document

_SHA = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class Artifact:
    name: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.name or not self.version or not _SHA.fullmatch(self.sha256):
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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompatibilityManifest":
        if not isinstance(value, Mapping):
            raise ReviewInputError("compatibility manifest must be an object")
        validate_public_document(dict(value), "compatibility-manifest")
        try:
            artifacts = value["artifacts"]
            npm = tuple(Artifact(**item) for item in artifacts["npm"])
            return cls(
                release=str(value["release"]),
                workflow=Artifact(**artifacts["workflow"]),
                python=Artifact(**artifacts["python"]),
                npm=npm,
                schemas_version=str(artifacts["schemas_version"]),
                worker=Artifact(**artifacts["worker"]),
                compatible_worker_range=str(value["compatible_worker_range"]),
                provenance=str(value["provenance"]),
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
    if manifest.release != manifest.python.version or manifest.release != manifest.worker.version:
        raise ReviewInputError("release artifacts do not share one release version")
    if not manifest.npm or any(item.version != manifest.release for item in manifest.npm):
        raise ReviewInputError("npm artifacts do not match the release version")
    return manifest

