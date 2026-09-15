"""Versioned release compatibility manifest validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ReviewInputError
from .schemas import validate_public_document

_SHA = re.compile(r"^[a-f0-9]{64}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_RANGE_VERSION = re.compile(
    r"^(0|[1-9][0-9]*|[xX*])"
    r"(?:\.(0|[1-9][0-9]*|[xX*]))?"
    r"(?:\.(0|[1-9][0-9]*|[xX*]))?$"
)
_RANGE_TOKEN = re.compile(r"^(<=|>=|==|=|<|>|\^|~)?\s*(.+)$")
_OPERATORS = frozenset({"<=", ">=", "==", "=", "<", ">", "^", "~"})


def _version_parts(value: str) -> tuple[int, int, int]:
    """Parse a bounded numeric semantic version for range comparisons."""

    if not isinstance(value, str):
        raise ValueError("version must be a string")
    normalized = value.strip()
    match = _RANGE_VERSION.fullmatch(normalized)
    if match is None or any(part in {"x", "X", "*"} for part in match.groups() if part):
        raise ValueError("version must contain three numeric components")
    parts = [int(part) if part is not None else 0 for part in match.groups()]
    if len(normalized.split(".")) != 3:
        raise ValueError("version must contain three numeric components")
    return parts[0], parts[1], parts[2]


def _constraint_parts(token: str) -> list[tuple[str, tuple[int, int, int]]]:
    match = _RANGE_TOKEN.fullmatch(token)
    if match is None:
        raise ValueError("range constraint is malformed")
    operator = match.group(1) or "="
    version_match = _RANGE_VERSION.fullmatch(match.group(2))
    if version_match is None:
        raise ValueError("range version is malformed")
    components = version_match.groups()
    wildcard_at = next(
        (index for index, part in enumerate(components) if part in {"x", "X", "*"}),
        None,
    )
    if wildcard_at is not None:
        if operator != "=":
            raise ValueError("wildcard range constraints require equality")
        if any(
            part not in {None, "x", "X", "*"} for part in components[wildcard_at + 1 :]
        ):
            raise ValueError("wildcard range constraints must end at the wildcard")
        if wildcard_at == 0:
            return [(">=", (0, 0, 0))]
        lower: tuple[int, int, int] = (
            int(components[0]) if components[0] not in {None, "x", "X", "*"} else 0,
            int(components[1]) if components[1] not in {None, "x", "X", "*"} else 0,
            int(components[2]) if components[2] not in {None, "x", "X", "*"} else 0,
        )
        upper_values = list(lower)
        upper_values[wildcard_at - 1] += 1
        for index in range(wildcard_at, 3):
            upper_values[index] = 0
        upper: tuple[int, int, int] = (
            upper_values[0],
            upper_values[1],
            upper_values[2],
        )
        return [(">=", lower), ("<", upper)]
    base: tuple[int, int, int] = (
        int(components[0]) if components[0] is not None else 0,
        int(components[1]) if components[1] is not None else 0,
        int(components[2]) if components[2] is not None else 0,
    )
    if operator == "^":
        if base[0] > 0:
            upper = (base[0] + 1, 0, 0)
        elif base[1] > 0:
            upper = (0, base[1] + 1, 0)
        else:
            upper = (0, 0, base[2] + 1)
        return [(">=", base), ("<", upper)]
    if operator == "~":
        return [(">=", base), ("<", (base[0], base[1] + 1, 0))]
    return [(operator, base)]


def _parse_range_alternative(
    alternative: str,
) -> list[tuple[str, tuple[int, int, int]]]:
    """Expand one comma/space-separated range alternative into constraints."""

    tokens = [token for token in re.split(r"\s*,\s*|\s+", alternative) if token]
    if not tokens:
        raise ValueError("range must contain a constraint")
    expanded: list[tuple[str, tuple[int, int, int]]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _OPERATORS:
            if index + 1 >= len(tokens):
                raise ValueError("range constraint is incomplete")
            if tokens[index + 1] in _OPERATORS:
                raise ValueError("range operators must be contiguous")
            token += tokens[index + 1]
            index += 1
        expanded.extend(_constraint_parts(token))
        index += 1
    return expanded


def _constraint_matches(
    candidate: tuple[int, int, int],
    constraint: tuple[str, tuple[int, int, int]],
) -> bool:
    operator, bound = constraint
    if operator in {"=", "=="}:
        return candidate == bound
    if operator == ">":
        return candidate > bound
    if operator == ">=":
        return candidate >= bound
    if operator == "<":
        return candidate < bound
    if operator == "<=":
        return candidate <= bound
    raise ValueError("range operator is unsupported")


def _range_contains(version: str, expression: str) -> bool:
    """Return whether a strict semantic version is admitted by a bounded range."""

    if (
        not isinstance(expression, str)
        or not expression.strip()
        or len(expression) > 128
    ):
        raise ValueError("compatible worker range is malformed")
    candidate = _version_parts(version)
    alternatives = expression.split("||")
    if any(not part.strip() for part in alternatives):
        raise ValueError("range alternative is empty")
    for alternative in alternatives:
        expanded = _parse_range_alternative(alternative.strip())
        if all(_constraint_matches(candidate, constraint) for constraint in expanded):
            return True
    return False


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
        try:
            contains_worker = _range_contains(
                self.worker.version, self.compatible_worker_range
            )
        except ValueError as exc:
            raise ReviewInputError("compatible worker range is malformed") from exc
        if not contains_worker:
            raise ReviewInputError(
                "compatible worker range does not include the declared worker version"
            )
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
