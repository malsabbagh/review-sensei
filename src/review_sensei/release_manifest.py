"""Versioned release compatibility manifest validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ReviewInputError
from .schemas import validate_public_document

_SHA = re.compile(r"^[a-f0-9]{64}$")
_GIT_SHA = re.compile(r"^[a-f0-9]{40}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_IMMUTABLE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
TRUSTED_PROVENANCE = frozenset(
    {
        "github-artifact-attestation",
        "pypi-trusted-publishing",
        "npm-oidc-provenance",
    }
)
INSTALL_SOURCE_PYPI = "pypi"
INSTALL_SOURCE_EXECUTING_COMMIT = "executing-commit"
FAILURE_UNAVAILABLE = "unavailable"
FAILURE_NETWORK = "network"
FAILURE_AUTHENTICATION = "authentication"
FAILURE_OTHER = "other"
MOVABLE_CHANNEL = "v4"
REQUIRED_PUBLICATION_LANES = ("workflow", "python", "npm", "worker", "schemas")
_UNAVAILABLE_MARKERS = (
    "No matching distribution found for review-sensei==",
    "Could not find a version that satisfies the requirement review-sensei==",
    "ResolutionImpossible: for review-sensei",
)
_AUTHENTICATION_MARKERS = (
    "401 Client Error",
    "403 Client Error",
    "Incorrect username or password",
    "authentication failed",
    "Access Denied",
)
_NETWORK_MARKERS = (
    "Failed to establish a new connection",
    "Temporary failure in name resolution",
    "Network is unreachable",
    "NameResolutionError",
    "ConnectTimeout",
    "ReadTimeout",
    "SSLError",
    "Connection reset",
)
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
        raise ValueError("version must contain exactly three numeric components")
    parts = [int(part) if part is not None else 0 for part in match.groups()]
    if len(normalized.split(".")) != 3:
        raise ValueError("version must contain exactly three numeric components")
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
            # An unqualified x/* intentionally means every non-negative semver.
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
    """Expand one comma-separated range alternative into constraints.

    Whitespace may surround commas or separate an operator from its version,
    but it is not itself a constraint separator. Additional constraints must
    therefore carry an explicit operator (for example, ``>=1.0.0 <2.0.0``),
    which keeps malformed expressions such as ``1.0.0 2.0.0`` fail-closed.
    """

    segments = [segment.strip() for segment in alternative.split(",")]
    if not segments or any(not segment for segment in segments):
        raise ValueError("range must contain a constraint")
    expanded: list[tuple[str, tuple[int, int, int]]] = []
    for segment in segments:
        tokens = segment.split()
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if (
                index > 0
                and tokens[index - 1] not in _OPERATORS
                and token not in _OPERATORS
            ):
                match = _RANGE_TOKEN.fullmatch(token)
                if match is None or not match.group(1):
                    raise ValueError(
                        "range constraints after the first require an explicit operator"
                    )
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
            or not _VERSION.fullmatch(self.version)
            or not isinstance(self.sha256, str)
            or not _SHA.fullmatch(self.sha256)
        ):
            raise ReviewInputError("release artifact identity is malformed")


def _resolve_provenance_kind(value: Mapping[str, Any]) -> str:
    explicit = value.get("provenance_kind")
    legacy = value.get("provenance")
    if isinstance(explicit, str) and explicit in TRUSTED_PROVENANCE:
        if (
            isinstance(legacy, str)
            and legacy in TRUSTED_PROVENANCE
            and legacy != explicit
        ):
            raise ReviewInputError("manifest provenance_kind disagrees with provenance")
        return explicit
    if isinstance(legacy, str) and legacy in TRUSTED_PROVENANCE:
        return legacy
    raise ReviewInputError("manifest provenance must be an explicit trusted mechanism")


@dataclass(frozen=True)
class CompatibilityManifest:
    release: str
    workflow_commit: str
    workflow: Artifact
    python: Artifact
    npm: tuple[Artifact, ...]
    schemas_version: str
    worker: Artifact
    compatible_worker_range: str
    provenance: str
    provenance_kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.release, str) or not _VERSION.fullmatch(self.release):
            raise ReviewInputError("release version must be semantic versioning")
        if not isinstance(self.workflow_commit, str) or not _GIT_SHA.fullmatch(
            self.workflow_commit
        ):
            raise ReviewInputError("workflow commit must be a 40-character git SHA")
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
        if self.provenance_kind not in TRUSTED_PROVENANCE:
            raise ReviewInputError(
                "manifest provenance must be an explicit trusted mechanism"
            )
        if (
            not isinstance(self.provenance, str)
            or not self.provenance.strip()
            or len(self.provenance) > 256
        ):
            raise ReviewInputError("manifest provenance annotation is malformed")
        if (
            not isinstance(self.npm, tuple)
            or not self.npm
            or any(not isinstance(artifact, Artifact) for artifact in self.npm)
        ):
            raise ReviewInputError("npm artifacts must be a non-empty tuple")
        if len({artifact.name for artifact in self.npm}) != len(self.npm):
            raise ReviewInputError("npm artifact names must be unique")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompatibilityManifest":
        if not isinstance(value, Mapping):
            raise ReviewInputError("compatibility manifest must be an object")
        validate_public_document(dict(value), "compatibility-manifest")
        try:
            artifacts = value["artifacts"]
            npm = tuple(Artifact(**item) for item in artifacts["npm"])
            provenance_kind = _resolve_provenance_kind(value)
            workflow_commit = value.get("workflow_commit")
            if not isinstance(workflow_commit, str) or not _GIT_SHA.fullmatch(
                workflow_commit
            ):
                raise ReviewInputError("workflow commit must be a 40-character git SHA")
            return cls(
                release=value["release"],
                workflow_commit=workflow_commit,
                workflow=Artifact(**artifacts["workflow"]),
                python=Artifact(**artifacts["python"]),
                npm=npm,
                schemas_version=artifacts["schemas_version"],
                worker=Artifact(**artifacts["worker"]),
                compatible_worker_range=value["compatible_worker_range"],
                provenance=value["provenance"],
                provenance_kind=provenance_kind,
            )
        except ReviewInputError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewInputError("compatibility manifest is incomplete") from exc

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": "1.0",
            "release": self.release,
            "workflow_commit": self.workflow_commit,
            "compatible_worker_range": self.compatible_worker_range,
            "provenance": self.provenance,
            "provenance_kind": self.provenance_kind,
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


def _require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ReviewInputError(f"{name} must be a SHA-256 digest")
    return value


def _require_git_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
        raise ReviewInputError(f"{name} must be a 40-character git SHA")
    return value


def digest_file(path: Path) -> str:
    """Return the SHA-256 digest of one regular artifact file."""

    if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
        raise ReviewInputError("release artifact file is missing")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def manifest_digest(manifest: CompatibilityManifest) -> str:
    """Return the canonical SHA-256 digest of a validated compatibility manifest."""

    payload = json.dumps(
        manifest.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact_from_file(name: str, version: str, path: Path) -> Artifact:
    return Artifact(name=name, version=version, sha256=digest_file(path))


def build_compatibility_manifest(
    *,
    release: str,
    workflow_path: Path,
    workflow_name: str,
    workflow_commit: str,
    python_path: Path,
    python_name: str,
    npm_artifacts: Sequence[tuple[str, Path]],
    worker_path: Path,
    worker_name: str,
    schemas_version: str,
    compatible_worker_range: str,
    provenance: str,
    provenance_kind: str,
) -> CompatibilityManifest:
    """Build a validated manifest from exact on-disk artifact bytes."""

    if provenance_kind not in TRUSTED_PROVENANCE:
        raise ReviewInputError(
            "manifest provenance must be an explicit trusted mechanism"
        )
    if not npm_artifacts:
        raise ReviewInputError("npm artifacts are required")
    names = [name for name, _path in npm_artifacts]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ReviewInputError("npm artifact names are required")
    if len(set(names)) != len(names):
        raise ReviewInputError("npm artifact names must be unique")
    try:
        manifest = CompatibilityManifest(
            release=release,
            workflow_commit=workflow_commit,
            workflow=_artifact_from_file(workflow_name, release, workflow_path),
            python=_artifact_from_file(python_name, release, python_path),
            npm=tuple(
                _artifact_from_file(name, release, path) for name, path in npm_artifacts
            ),
            schemas_version=schemas_version,
            worker=_artifact_from_file(worker_name, release, worker_path),
            compatible_worker_range=compatible_worker_range,
            provenance=provenance,
            provenance_kind=provenance_kind,
        )
    except (TypeError, ValueError) as exc:
        raise ReviewInputError("compatibility manifest is incomplete") from exc
    return validate_compatibility_manifest(manifest.to_dict())


def expected_artifact_digests(manifest: CompatibilityManifest) -> dict[str, str]:
    """Return the exact digest map a consumer must observe."""

    observed = {
        "workflow": manifest.workflow.sha256,
        "python": manifest.python.sha256,
        "worker": manifest.worker.sha256,
    }
    for artifact in manifest.npm:
        observed[f"npm:{artifact.name}"] = artifact.sha256
    return observed


def verify_artifact_digests(
    manifest: CompatibilityManifest, observed: Mapping[str, str]
) -> None:
    """Reject missing, extra, or mismatched artifact digests before execution."""

    if not isinstance(observed, Mapping):
        raise ReviewInputError("observed artifact digests must be an object")
    expected = expected_artifact_digests(manifest)
    observed_keys = set(observed)
    expected_keys = set(expected)
    if observed_keys != expected_keys:
        raise ReviewInputError("release artifact combination is missing or ambiguous")
    for name, digest in expected.items():
        candidate = observed[name]
        if not isinstance(candidate, str) or not _SHA.fullmatch(candidate):
            raise ReviewInputError("observed artifact digest is malformed")
        if candidate != digest:
            raise ReviewInputError(
                "release artifact digest does not match the manifest"
            )


def verify_worker_compatibility(
    manifest: CompatibilityManifest, observed_worker_version: str
) -> None:
    """Reject a deployed Worker that is outside the manifest compatibility range."""

    if not isinstance(observed_worker_version, str) or not _VERSION.fullmatch(
        observed_worker_version
    ):
        raise ReviewInputError("observed worker version is malformed")
    try:
        compatible = _range_contains(
            observed_worker_version, manifest.compatible_worker_range
        )
    except ValueError as exc:
        raise ReviewInputError("observed worker version is malformed") from exc
    if not compatible:
        raise ReviewInputError("worker release is outside the compatible range")


def classify_install_failure(message: str) -> str:
    """Classify a package-install failure without treating outages as missing packages.

    Authentication and network markers are evaluated before unavailable
    distribution markers so a resolver message that also mentions a network or
    auth failure cannot authorize the executing-commit fallback.
    """

    if not isinstance(message, str) or not message.strip():
        return FAILURE_OTHER
    if any(marker in message for marker in _AUTHENTICATION_MARKERS):
        return FAILURE_AUTHENTICATION
    if any(marker in message for marker in _NETWORK_MARKERS):
        return FAILURE_NETWORK
    if any(marker in message for marker in _UNAVAILABLE_MARKERS):
        return FAILURE_UNAVAILABLE
    return FAILURE_OTHER


def allow_executing_commit_fallback(failure_kind: str) -> None:
    """Permit the GitHub executing-commit fallback only for a missing distribution."""

    if failure_kind != FAILURE_UNAVAILABLE:
        raise ReviewInputError(
            "github fallback is refused unless the exact PyPI distribution is unavailable"
        )


def prove_release_identity(
    *,
    source: str,
    requested_version: str,
    installed_version: str,
    manifest: CompatibilityManifest,
    executing_commit: str,
    observed_artifact_digests: Mapping[str, str],
    observed_worker_version: str,
) -> None:
    """Prove PyPI-primary or executing-commit identity against one validated manifest.

    This enforces release/version alignment, executing workflow commit equality,
    every manifest artifact digest, and Worker compatibility range membership.
    """

    if requested_version != manifest.release or installed_version != manifest.release:
        raise ReviewInputError("installed package does not match the manifest release")
    if (
        _require_git_sha(executing_commit, "executing commit")
        != manifest.workflow_commit
    ):
        raise ReviewInputError("executing workflow commit does not match the manifest")
    if source not in {INSTALL_SOURCE_PYPI, INSTALL_SOURCE_EXECUTING_COMMIT}:
        raise ReviewInputError("release install source is unsupported")
    verify_artifact_digests(manifest, observed_artifact_digests)
    verify_worker_compatibility(manifest, observed_worker_version)


@dataclass(frozen=True)
class CanaryBinding:
    manifest_sha256: str
    release: str
    workflow_commit: str
    evidence_kind: str
    status: str

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_sha256, "canary manifest digest")
        if not isinstance(self.release, str) or not _VERSION.fullmatch(self.release):
            raise ReviewInputError("canary release must be semantic versioning")
        _require_git_sha(self.workflow_commit, "canary workflow commit")
        if self.evidence_kind == "fixture-downstream":
            if self.status != "bound":
                raise ReviewInputError("fixture canary evidence must bind the manifest")
            return
        if self.evidence_kind == "operator-live":
            if self.status != "deferred-operator-only":
                raise ReviewInputError(
                    "live canary evidence remains operator-only until authorized"
                )
            return
        raise ReviewInputError("canary evidence kind is unsupported")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": "1.0",
            "manifest_sha256": self.manifest_sha256,
            "release": self.release,
            "workflow_commit": self.workflow_commit,
            "evidence_kind": self.evidence_kind,
            "status": self.status,
        }
        validate_public_document(value, "canary-binding")
        return value


def bind_canary_evidence(
    manifest: CompatibilityManifest, evidence_kind: str
) -> CanaryBinding:
    """Bind #34 canary evidence to the exact manifest digest.

    Fixture/downstream evidence is the implemented contract. A live disposable
    repository canary stays operator-only and cannot authorize ``v4`` promotion.
    """

    status = (
        "bound" if evidence_kind == "fixture-downstream" else "deferred-operator-only"
    )
    binding = CanaryBinding(
        manifest_sha256=manifest_digest(manifest),
        release=manifest.release,
        workflow_commit=manifest.workflow_commit,
        evidence_kind=evidence_kind,
        status=status,
    )
    binding.to_dict()
    return binding


def validate_canary_binding(value: Mapping[str, Any]) -> CanaryBinding:
    if not isinstance(value, Mapping):
        raise ReviewInputError("canary binding must be an object")
    validate_public_document(dict(value), "canary-binding")
    try:
        binding = CanaryBinding(
            manifest_sha256=value["manifest_sha256"],
            release=value["release"],
            workflow_commit=value["workflow_commit"],
            evidence_kind=value["evidence_kind"],
            status=value["status"],
        )
    except KeyError as exc:
        raise ReviewInputError("canary binding is incomplete") from exc
    return binding


def evaluate_publication_state(published_lanes: Mapping[str, bool]) -> str:
    """Classify platform publication so a partial release cannot move ``v4``."""

    if not isinstance(published_lanes, Mapping):
        raise ReviewInputError("publication lanes must be an object")
    if set(published_lanes) != set(REQUIRED_PUBLICATION_LANES):
        raise ReviewInputError("publication lanes are missing or ambiguous")
    values: list[bool] = []
    for lane in REQUIRED_PUBLICATION_LANES:
        published = published_lanes[lane]
        if not isinstance(published, bool):
            raise ReviewInputError("publication lane state must be boolean")
        values.append(published)
    if all(values):
        return "complete"
    if any(values):
        return "partial"
    return "failed"


def evaluate_inflight_tag_movement(
    *,
    start_sha: str,
    current_sha: str,
    authorized_grace: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed when ``v4`` moves during a run unless grace is explicitly authorized.

    ``authorized_grace`` is an in-memory per-run authorization only. It is not a
    persisted or schema-validated public record and must not be written to audit
    ledgers.
    """

    start = _require_git_sha(start_sha, "start workflow commit")
    current = _require_git_sha(current_sha, "current workflow commit")
    if start == current:
        return
    if not isinstance(authorized_grace, Mapping):
        raise ReviewInputError("in-flight tag movement is not authorized")
    if authorized_grace.get("authorized") is not True:
        raise ReviewInputError("in-flight tag movement is not authorized")
    expected_start = _require_git_sha(
        authorized_grace.get("start_sha"), "authorized grace start"
    )
    expected_current = _require_git_sha(
        authorized_grace.get("current_sha"), "authorized grace current"
    )
    if expected_start != start or expected_current != current:
        raise ReviewInputError("in-flight tag movement grace does not match this run")


@dataclass(frozen=True)
class ChannelPromotionRecord:
    channel: str
    action: str
    status: str
    previous_target: str
    new_target: str
    manifest_sha256: str
    canary_manifest_sha256: str
    publication_state: str
    recorded_at: str

    def __post_init__(self) -> None:
        if self.channel != MOVABLE_CHANNEL:
            raise ReviewInputError("only the operator-managed v4 channel may move")
        if self.action not in {"promote", "rollback", "abandon"}:
            raise ReviewInputError("channel promotion action is unsupported")
        if self.status not in {"in-flight", "complete"}:
            raise ReviewInputError("channel promotion status is unsupported")
        _require_git_sha(self.previous_target, "previous channel target")
        _require_git_sha(self.new_target, "new channel target")
        if self.action == "abandon":
            if self.status != "complete":
                raise ReviewInputError("abandoned promotions must be complete records")
            if self.new_target != self.previous_target:
                raise ReviewInputError("abandoned promotions leave v4 unchanged")
            return
        if self.previous_target == self.new_target:
            raise ReviewInputError("channel promotion must change the v4 target")
        _require_sha256(self.manifest_sha256, "promotion manifest digest")
        _require_sha256(self.canary_manifest_sha256, "promotion canary digest")
        if self.publication_state not in {"complete", "partial", "failed"}:
            raise ReviewInputError("publication state is unsupported")
        if not isinstance(self.recorded_at, str) or not self.recorded_at.strip():
            raise ReviewInputError("channel promotion timestamp is required")
        if (
            self.action == "promote"
            and self.status in {"in-flight", "complete"}
            and self.publication_state != "complete"
        ):
            raise ReviewInputError("v4 promotion requires complete publication")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": "1.0",
            "channel": self.channel,
            "action": self.action,
            "status": self.status,
            "previous_target": self.previous_target,
            "new_target": self.new_target,
            "manifest_sha256": self.manifest_sha256,
            "canary_manifest_sha256": self.canary_manifest_sha256,
            "publication_state": self.publication_state,
            "recorded_at": self.recorded_at,
        }
        validate_public_document(value, "channel-promotion")
        return value


def validate_channel_promotion_record(
    value: Mapping[str, Any],
) -> ChannelPromotionRecord:
    if not isinstance(value, Mapping):
        raise ReviewInputError("channel promotion record must be an object")
    validate_public_document(dict(value), "channel-promotion")
    try:
        record = ChannelPromotionRecord(
            channel=value["channel"],
            action=value["action"],
            status=value["status"],
            previous_target=value["previous_target"],
            new_target=value["new_target"],
            manifest_sha256=value["manifest_sha256"],
            canary_manifest_sha256=value["canary_manifest_sha256"],
            publication_state=value["publication_state"],
            recorded_at=value["recorded_at"],
        )
    except KeyError as exc:
        raise ReviewInputError("channel promotion record is incomplete") from exc
    return record


def _ledger_has_inflight(ledger: Sequence[ChannelPromotionRecord]) -> bool:
    blocked_manifests: set[str] = set()
    for record in ledger:
        if record.action == "promote" and record.status == "in-flight":
            blocked_manifests.add(record.manifest_sha256)
        if record.action in {"abandon", "promote"} and record.status == "complete":
            blocked_manifests.discard(record.manifest_sha256)
    return bool(blocked_manifests)


def _published_version_conflict(
    manifest: CompatibilityManifest,
    published_version_digests: Mapping[str, str] | None,
) -> None:
    if published_version_digests is None:
        return
    existing = published_version_digests.get(manifest.release)
    if existing is None:
        return
    digest = manifest_digest(manifest)
    if existing != digest:
        raise ReviewInputError("immutable package versions cannot be replaced")


def begin_channel_promotion(
    *,
    manifest: CompatibilityManifest,
    canary: CanaryBinding,
    previous_target: str,
    publication_state: str,
    recorded_at: str,
    ledger: Sequence[ChannelPromotionRecord] = (),
    published_version_digests: Mapping[str, str] | None = None,
) -> ChannelPromotionRecord:
    """Start a serialized ``v4`` promotion after canary binding and complete publication.

    Immutable-version enforcement is digest-based: any artifact-byte or workflow
    commit change for an already published release requires a new release
    version instead of rebinding the same ``release`` value.
    """

    if _ledger_has_inflight(ledger):
        raise ReviewInputError("v4 promotion is already in flight")
    if canary.status != "bound":
        raise ReviewInputError("v4 promotion requires bound canary evidence")
    digest = manifest_digest(manifest)
    if (
        canary.manifest_sha256 != digest
        or canary.workflow_commit != manifest.workflow_commit
    ):
        raise ReviewInputError("canary evidence does not bind this manifest")
    _published_version_conflict(manifest, published_version_digests)
    record = ChannelPromotionRecord(
        channel=MOVABLE_CHANNEL,
        action="promote",
        status="in-flight",
        previous_target=previous_target,
        new_target=manifest.workflow_commit,
        manifest_sha256=digest,
        canary_manifest_sha256=canary.manifest_sha256,
        publication_state=publication_state,
        recorded_at=recorded_at,
    )
    record.to_dict()
    return record


def abandon_in_flight_promotion(
    in_flight: ChannelPromotionRecord,
    *,
    recorded_at: str,
    ledger: Sequence[ChannelPromotionRecord] = (),
) -> ChannelPromotionRecord:
    """Record an abandoned in-flight promotion without moving ``v4``."""

    if in_flight.status != "in-flight" or in_flight.action != "promote":
        raise ReviewInputError("channel promotion is not in flight")
    if in_flight not in ledger:
        raise ReviewInputError("channel promotion is not in the audit ledger")
    record = ChannelPromotionRecord(
        channel=in_flight.channel,
        action="abandon",
        status="complete",
        previous_target=in_flight.previous_target,
        new_target=in_flight.previous_target,
        manifest_sha256=in_flight.manifest_sha256,
        canary_manifest_sha256=in_flight.canary_manifest_sha256,
        publication_state="failed",
        recorded_at=recorded_at,
    )
    record.to_dict()
    return record


def complete_channel_promotion(
    in_flight: ChannelPromotionRecord,
    *,
    recorded_at: str,
    ledger: Sequence[ChannelPromotionRecord] = (),
) -> ChannelPromotionRecord:
    """Mark a previously serialized promotion complete."""

    if in_flight.status != "in-flight" or in_flight.action != "promote":
        raise ReviewInputError("channel promotion is not in flight")
    if in_flight not in ledger:
        raise ReviewInputError("channel promotion is not in the audit ledger")
    record = ChannelPromotionRecord(
        channel=in_flight.channel,
        action="promote",
        status="complete",
        previous_target=in_flight.previous_target,
        new_target=in_flight.new_target,
        manifest_sha256=in_flight.manifest_sha256,
        canary_manifest_sha256=in_flight.canary_manifest_sha256,
        publication_state=in_flight.publication_state,
        recorded_at=recorded_at,
    )
    record.to_dict()
    return record


def record_channel_rollback(
    *,
    current: ChannelPromotionRecord,
    restored: CompatibilityManifest,
    canary: CanaryBinding,
    recorded_at: str,
    ledger: Sequence[ChannelPromotionRecord] = (),
) -> ChannelPromotionRecord:
    """Record a rollback onto previously published immutable artifacts."""

    if current.status != "complete":
        raise ReviewInputError("rollback requires a completed channel record")
    if current not in ledger:
        raise ReviewInputError("rollback source is not in the audit ledger")
    digest = manifest_digest(restored)
    if restored.workflow_commit != current.previous_target:
        raise ReviewInputError(
            "rollback must target the previous immutable channel SHA"
        )
    if canary.status != "bound":
        raise ReviewInputError("rollback requires bound canary evidence")
    if (
        canary.manifest_sha256 != digest
        or canary.workflow_commit != restored.workflow_commit
    ):
        raise ReviewInputError("canary evidence does not bind the restored manifest")
    record = ChannelPromotionRecord(
        channel=MOVABLE_CHANNEL,
        action="rollback",
        status="complete",
        previous_target=current.new_target,
        new_target=current.previous_target,
        manifest_sha256=digest,
        canary_manifest_sha256=canary.manifest_sha256,
        publication_state="complete",
        recorded_at=recorded_at,
    )
    record.to_dict()
    return record


def refuse_immutable_tag_replacement(channel: str) -> None:
    """Reject silent replacement of immutable ``vX.Y.Z`` package tags."""

    if channel == MOVABLE_CHANNEL:
        return
    if isinstance(channel, str) and _IMMUTABLE_TAG.fullmatch(channel):
        raise ReviewInputError("immutable package tags cannot be replaced")
    raise ReviewInputError(
        "release channel must be the operator-managed v4 tag or an immutable vX.Y.Z package tag"
    )
