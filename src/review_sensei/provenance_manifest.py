"""Immutable source, installed-version, and runtime provenance manifest (G11).

Runtime telemetry, repository source text, and workflow routing checks are
distinct observation layers. None of them confers execution, publication,
recovery, or settlement authority. Unavailable telemetry is represented
explicitly as ``"unknown"`` without triggering network probes, token issuance,
or retries, and every classifier outcome carries a deterministic, bounded
recovery prescription that preserves prior obligations, reservations, and
unknown usage charges.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ReviewInputError
from .release_manifest import CompatibilityManifest, _range_contains

PROVENANCE_MANIFEST_SCHEMA_VERSION = "provenance-manifest-v1"
ATTEMPT_EVIDENCE_SCHEMA_VERSION = "attempt-evidence-packet-v1"
UNKNOWN_PROVENANCE = "unknown"
MISSING_BINDING = "missing"
MAX_PROVENANCE_PACKET_BYTES = 16_384
MAX_PUBLIC_SUMMARY_BYTES = 4_096
MAX_SAFE_TEXT_LENGTH = 200
MAX_SAFE_STEPS = 8
MAX_UNCERTAIN_DISPATCHES = 64
MAX_COUNTER_VALUE = 9_007_199_254_740_991
MAX_VALIDATED_STRING_LENGTH = 1_024
INSTALLED_DISTRIBUTION_NAMES = ("review-sensei", "review-sensei-github")

_GIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]{1,32}(?:\.[0-9A-Za-z-]{1,32}){0,15})?"
    r"(?:\+[0-9A-Za-z-]{1,32}(?:\.[0-9A-Za-z-]{1,32}){0,15})?$"
)
_SAFE_REPO_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/@:-]{1,200}$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_WORKER_REVISION_RE = re.compile(
    r"^(?:[a-f0-9]{40}"
    r"|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
    r"|v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]{1,32}(?:\.[0-9A-Za-z-]{1,32}){0,15})?"
    r"(?:\+[0-9A-Za-z-]{1,32}(?:\.[0-9A-Za-z-]{1,32}){0,15})?)$"
)
_EXECUTOR_ID_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_SECRET_LIKE_RE = re.compile(
    r"(?:"
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{8,}"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{4,}"
    r"|-----BEGIN [A-Z0-9 ]*KEY-----"
    r"|PRIVATE[\s_-]*KEY"
    r"|(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]"
    r"|https?://[^\s/@]+:[^\s/@]+@"
    r"|diff --git "
    r"|@@ -\d+"
    r")",
    re.IGNORECASE,
)

GATE_OUTCOMES = frozenset({"passed", "failed", "skipped", "not_run", "unknown"})
BROKER_ROUTES = frozenset({"/github/token", "/github/session-grant", "none", "unknown"})
SOURCE_EVENT_KINDS = frozenset(
    {
        "pull_request",
        "issue_comment",
        "pull_request_review_comment",
        "workflow_dispatch",
        "local_cli",
        "unknown",
    }
)
DISPATCH_CERTAINTIES = frozenset(
    {
        "no_entry_proof",
        "failed_response",
        "response_receipt",
        "effect_confirmed",
        "unknown",
    }
)
RECEIPT_STATES = frozenset(
    {"confirmed", "pending", "failed", "absent", "not_applicable", "unknown"}
)
CLASSIFIER_OUTCOMES = frozenset(
    {
        "supported_read_only_observation",
        "eligible_recovery_awaiting_authorization",
        "missing_evidence",
        "provenance_mismatch",
        "unsupported_legacy_state",
        "explicit_human_decision_required",
    }
)
RECOVERY_ACTIONS = frozenset(
    {
        "no_action_needed",
        "replay_retained_result",
        "review_remaining",
        "verify_convergence",
        "supply_original_context_or_rescan",
        "rescan_under_pinned_provenance",
        "reconcile_owned_readback_then_hold_or_dispose",
        "controlled_reenrollment",
    }
)
RECOVERY_COMMANDS = frozenset(
    {
        "none",
        "@reviewsensei verify",
        "@reviewsensei review remaining",
        "@reviewsensei rescan",
        "supply-original-recovery-context",
        "reconcile-owned-readback",
        "controlled-reenroll",
    }
)
KNOWN_MISSING_INPUT_LABELS = frozenset(
    {
        "configuration",
        "defaults",
        "context_selection",
        "learnings",
        "baseline",
        "accepted_result",
        "decision_policy",
    }
)


def _reject_secret_or_control(value: str, *, field: str) -> None:
    if len(value) > MAX_VALIDATED_STRING_LENGTH:
        raise ReviewInputError(f"{field} exceeds maximum bounded length")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ReviewInputError(f"{field} contains control characters")
    if _SECRET_LIKE_RE.search(value) is not None:
        raise ReviewInputError(f"{field} contains secret-like or raw-diff content")


def _exact_false_authority(value: object, *, field: str = "authority") -> bool:
    if value is not False:
        raise ReviewInputError(f"{field} must be exactly False; diagnostics grant none")
    return False


def _exact_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise ReviewInputError(f"{field} must be a boolean")
    return value


def _non_negative_int(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER_VALUE:
        raise ReviewInputError(f"{field} must be a bounded non-negative integer")
    return value


def _positive_int_or_unknown(value: object, *, field: str) -> int | str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if type(value) is int and 1 <= value <= MAX_COUNTER_VALUE:
        return value
    raise ReviewInputError(f"{field} must be a positive integer or 'unknown'")


def _non_negative_int_or_unknown(value: object, *, field: str) -> int | str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if type(value) is int and 0 <= value <= MAX_COUNTER_VALUE:
        return value
    raise ReviewInputError(f"{field} must be a non-negative integer or 'unknown'")


def _git_sha_or_unknown(value: object, *, field: str) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if not isinstance(value, str):
        raise ReviewInputError(
            f"{field} must be a 40-char lowercase hex SHA or 'unknown'"
        )
    _reject_secret_or_control(value, field=field)
    if _GIT_SHA_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{field} must be a 40-char lowercase hex SHA or 'unknown'"
        )
    return value


def _sha256_or_unknown(
    value: object, *, field: str, allow_missing: bool = False
) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if allow_missing and value == MISSING_BINDING:
        return MISSING_BINDING
    if not isinstance(value, str):
        raise ReviewInputError(
            f"{field} must be a 64-char lowercase SHA-256 or 'unknown'"
        )
    _reject_secret_or_control(value, field=field)
    if _SHA256_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{field} must be a 64-char lowercase SHA-256 or 'unknown'"
        )
    return value


def _revision_token_or_unknown(value: object, *, field: str) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if not isinstance(value, str):
        raise ReviewInputError(
            f"{field} must be a 40/64-char hex revision or 'unknown'"
        )
    _reject_secret_or_control(value, field=field)
    if _GIT_SHA_RE.fullmatch(value) is None and _SHA256_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{field} must be a 40/64-char hex revision or 'unknown'"
        )
    return value


def _semver_or_unknown(value: object, *, field: str) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if not isinstance(value, str) or len(value) > 128:
        raise ReviewInputError(f"{field} must be a semantic version or 'unknown'")
    _reject_secret_or_control(value, field=field)
    if _SEMVER_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{field} must be a semantic version or 'unknown'")
    return value


def _repo_or_unknown(value: object, *, field: str) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if not isinstance(value, str):
        raise ReviewInputError(f"{field} must be 'owner/repo' or 'unknown'")
    _reject_secret_or_control(value, field=field)
    if _SAFE_REPO_RE.fullmatch(value) is None or ".." in value:
        raise ReviewInputError(f"{field} must be 'owner/repo' or 'unknown'")
    return value


def _ref_or_unknown(value: object, *, field: str) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if not isinstance(value, str):
        raise ReviewInputError(
            f"{field} must be a safe ref/workflow token or 'unknown'"
        )
    _reject_secret_or_control(value, field=field)
    if _SAFE_REF_RE.fullmatch(value) is None or ".." in value or "//" in value:
        raise ReviewInputError(
            f"{field} must be a safe ref/workflow token or 'unknown'"
        )
    return value


def _executor_or_unknown(value: object, *, field: str) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if not isinstance(value, str):
        raise ReviewInputError(
            f"{field} must be a safe executor identifier or 'unknown'"
        )
    _reject_secret_or_control(value, field=field)
    if _EXECUTOR_ID_RE.fullmatch(value) is None or ".." in value or "//" in value:
        raise ReviewInputError(
            f"{field} must be a safe executor identifier or 'unknown'"
        )
    return value


def _worker_revision_or_unknown(value: object, *, field: str) -> str:
    if value is None or value == UNKNOWN_PROVENANCE:
        return UNKNOWN_PROVENANCE
    if not isinstance(value, str):
        raise ReviewInputError(f"{field} must be a Worker revision or 'unknown'")
    _reject_secret_or_control(value, field=field)
    if _WORKER_REVISION_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{field} must be a Worker revision or 'unknown'")
    return value


def _closed_choice(value: object, choices: frozenset[str], *, field: str) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{field} must be one of {sorted(choices)}")
    _reject_secret_or_control(value, field=field)
    if value not in choices:
        raise ReviewInputError(f"{field} must be one of {sorted(choices)}")
    return value


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_mapping_keys(
    data: object, *, required: frozenset[str], optional: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ReviewInputError(f"{label} must be a JSON object")
    keys = set(data.keys())
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise ReviewInputError(f"{label} missing required fields: {sorted(missing)}")
    if extra:
        raise ReviewInputError(f"{label} has unexpected fields: {sorted(extra)}")
    return data


@dataclass(frozen=True)
class SourceEventIdentity:
    """Immutable identity for the triggering event or command source."""

    event_kind: str = UNKNOWN_PROVENANCE
    source_id: int | str = UNKNOWN_PROVENANCE
    source_revision: str = UNKNOWN_PROVENANCE
    source_digest: str = UNKNOWN_PROVENANCE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_kind",
            _closed_choice(self.event_kind, SOURCE_EVENT_KINDS, field="event_kind"),
        )
        object.__setattr__(
            self,
            "source_id",
            _positive_int_or_unknown(self.source_id, field="source_id"),
        )
        object.__setattr__(
            self,
            "source_revision",
            _revision_token_or_unknown(self.source_revision, field="source_revision"),
        )
        object.__setattr__(
            self,
            "source_digest",
            _sha256_or_unknown(self.source_digest, field="source_digest"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_kind": self.event_kind,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_dict(cls, data: object) -> SourceEventIdentity:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {"event_kind", "source_id", "source_revision", "source_digest"}
            ),
            optional=frozenset(),
            label="source_event",
        )
        return cls(
            event_kind=mapping["event_kind"],
            source_id=mapping["source_id"],
            source_revision=mapping["source_revision"],
            source_digest=mapping["source_digest"],
        )


@dataclass(frozen=True)
class GateObservations:
    """Separate observations for routing syntax, live PR checks, and broker auth.

    None of these three gates proves either of the other two:
    - ``routing_preflight`` validates workflow input syntax only; it never fetches
      or authenticates live PR state or broker capabilities.
    - ``authenticated_live_validation`` validates live PR head/base/draft/fork
      state at bootstrap; it does not prove broker token/grant enforcement.
    - ``broker_enforcement`` records OIDC/capability enforcement; it does not
      substitute for live PR state checks or terminal accounting.
    """

    routing_preflight: str = UNKNOWN_PROVENANCE
    authenticated_live_validation: str = UNKNOWN_PROVENANCE
    broker_enforcement: str = UNKNOWN_PROVENANCE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "routing_preflight",
            _closed_choice(
                self.routing_preflight, GATE_OUTCOMES, field="routing_preflight"
            ),
        )
        object.__setattr__(
            self,
            "authenticated_live_validation",
            _closed_choice(
                self.authenticated_live_validation,
                GATE_OUTCOMES,
                field="authenticated_live_validation",
            ),
        )
        object.__setattr__(
            self,
            "broker_enforcement",
            _closed_choice(
                self.broker_enforcement, GATE_OUTCOMES, field="broker_enforcement"
            ),
        )

    @property
    def routing_proves_live_validation(self) -> bool:
        return False

    @property
    def live_validation_proves_broker_enforcement(self) -> bool:
        return False

    @property
    def broker_proves_live_validation(self) -> bool:
        return False

    def has_routing_pass_with_live_refusal_or_unknown(self) -> bool:
        return self.routing_preflight == "passed" and (
            self.authenticated_live_validation in {"failed", "unknown", "not_run"}
        )

    def all_required_gates_confirmed(
        self,
        *,
        require_broker: bool = True,
        require_routing: bool | None = None,
    ) -> bool:
        """Return True only when required routing, live, and broker gates passed.

        Hosted workflows (``require_broker=True``) require ``routing_preflight ==
        "passed"`` by default; local CLI flows (``require_broker=False``) default
        to ``require_routing=False`` while still rejecting an explicit
        ``routing_preflight == "failed"``.
        """

        if self.routing_preflight == "failed":
            return False
        effective_require_routing = (
            require_broker if require_routing is None else require_routing
        )
        if effective_require_routing and self.routing_preflight != "passed":
            return False
        if self.authenticated_live_validation != "passed":
            return False
        if require_broker and self.broker_enforcement != "passed":
            return False
        return True

    def to_dict(self) -> dict[str, str]:
        return {
            "routing_preflight": self.routing_preflight,
            "authenticated_live_validation": self.authenticated_live_validation,
            "broker_enforcement": self.broker_enforcement,
        }

    @classmethod
    def from_dict(cls, data: object) -> GateObservations:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "routing_preflight",
                    "authenticated_live_validation",
                    "broker_enforcement",
                }
            ),
            optional=frozenset(),
            label="gates",
        )
        return cls(
            routing_preflight=mapping["routing_preflight"],
            authenticated_live_validation=mapping["authenticated_live_validation"],
            broker_enforcement=mapping["broker_enforcement"],
        )


@dataclass(frozen=True)
class ProvenanceManifest:
    """Immutable manifest separating repository source from installed runtime."""

    repository: str = UNKNOWN_PROVENANCE
    repository_id: int | str = UNKNOWN_PROVENANCE
    pull_request: int | str = UNKNOWN_PROVENANCE
    main_sha: str = UNKNOWN_PROVENANCE
    base_ref: str = UNKNOWN_PROVENANCE
    base_sha: str = UNKNOWN_PROVENANCE
    head_sha: str = UNKNOWN_PROVENANCE
    source_event: SourceEventIdentity = SourceEventIdentity()
    caller_workflow: str = UNKNOWN_PROVENANCE
    reusable_workflow_ref: str = UNKNOWN_PROVENANCE
    reusable_workflow_tag_object: str = UNKNOWN_PROVENANCE
    reusable_workflow_peeled_commit: str = UNKNOWN_PROVENANCE
    installed_core_version: str = UNKNOWN_PROVENANCE
    installed_hosted_version: str = UNKNOWN_PROVENANCE
    selected_executor: str = UNKNOWN_PROVENANCE
    broker_route: str = UNKNOWN_PROVENANCE
    broker_revision: str = UNKNOWN_PROVENANCE
    frozen_policy_commit: str = UNKNOWN_PROVENANCE
    frozen_policy_sha256: str = UNKNOWN_PROVENANCE
    gates: GateObservations = GateObservations()
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        if not isinstance(self.source_event, SourceEventIdentity):
            raise ReviewInputError("source_event must be a SourceEventIdentity")
        if not isinstance(self.gates, GateObservations):
            raise ReviewInputError("gates must be a GateObservations")
        object.__setattr__(
            self, "repository", _repo_or_unknown(self.repository, field="repository")
        )
        object.__setattr__(
            self,
            "repository_id",
            _positive_int_or_unknown(self.repository_id, field="repository_id"),
        )
        object.__setattr__(
            self,
            "pull_request",
            _positive_int_or_unknown(self.pull_request, field="pull_request"),
        )
        object.__setattr__(
            self, "main_sha", _git_sha_or_unknown(self.main_sha, field="main_sha")
        )
        object.__setattr__(
            self, "base_ref", _ref_or_unknown(self.base_ref, field="base_ref")
        )
        object.__setattr__(
            self, "base_sha", _git_sha_or_unknown(self.base_sha, field="base_sha")
        )
        object.__setattr__(
            self, "head_sha", _git_sha_or_unknown(self.head_sha, field="head_sha")
        )
        object.__setattr__(
            self,
            "caller_workflow",
            _ref_or_unknown(self.caller_workflow, field="caller_workflow"),
        )
        object.__setattr__(
            self,
            "reusable_workflow_ref",
            _ref_or_unknown(self.reusable_workflow_ref, field="reusable_workflow_ref"),
        )
        object.__setattr__(
            self,
            "reusable_workflow_tag_object",
            _git_sha_or_unknown(
                self.reusable_workflow_tag_object,
                field="reusable_workflow_tag_object",
            ),
        )
        object.__setattr__(
            self,
            "reusable_workflow_peeled_commit",
            _git_sha_or_unknown(
                self.reusable_workflow_peeled_commit,
                field="reusable_workflow_peeled_commit",
            ),
        )
        object.__setattr__(
            self,
            "installed_core_version",
            _semver_or_unknown(
                self.installed_core_version, field="installed_core_version"
            ),
        )
        object.__setattr__(
            self,
            "installed_hosted_version",
            _semver_or_unknown(
                self.installed_hosted_version, field="installed_hosted_version"
            ),
        )
        object.__setattr__(
            self,
            "selected_executor",
            _executor_or_unknown(self.selected_executor, field="selected_executor"),
        )
        object.__setattr__(
            self,
            "broker_route",
            _closed_choice(self.broker_route, BROKER_ROUTES, field="broker_route"),
        )
        object.__setattr__(
            self,
            "broker_revision",
            _worker_revision_or_unknown(self.broker_revision, field="broker_revision"),
        )
        object.__setattr__(
            self,
            "frozen_policy_commit",
            _git_sha_or_unknown(
                self.frozen_policy_commit, field="frozen_policy_commit"
            ),
        )
        object.__setattr__(
            self,
            "frozen_policy_sha256",
            _sha256_or_unknown(self.frozen_policy_sha256, field="frozen_policy_sha256"),
        )

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def grants_recovery_authority(self) -> bool:
        return False

    def _identity_fields(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
            "main_sha": self.main_sha,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "source_event.event_kind": self.source_event.event_kind,
            "source_event.source_id": self.source_event.source_id,
            "source_event.source_revision": self.source_event.source_revision,
            "source_event.source_digest": self.source_event.source_digest,
            "caller_workflow": self.caller_workflow,
            "reusable_workflow_ref": self.reusable_workflow_ref,
            "reusable_workflow_tag_object": self.reusable_workflow_tag_object,
            "reusable_workflow_peeled_commit": self.reusable_workflow_peeled_commit,
            "installed_core_version": self.installed_core_version,
            "installed_hosted_version": self.installed_hosted_version,
            "selected_executor": self.selected_executor,
            "broker_route": self.broker_route,
            "broker_revision": self.broker_revision,
            "frozen_policy_commit": self.frozen_policy_commit,
            "frozen_policy_sha256": self.frozen_policy_sha256,
            "gates.routing_preflight": self.gates.routing_preflight,
            "gates.authenticated_live_validation": (
                self.gates.authenticated_live_validation
            ),
            "gates.broker_enforcement": self.gates.broker_enforcement,
        }

    @property
    def unknown_fields(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                key
                for key, val in self._identity_fields().items()
                if val == UNKNOWN_PROVENANCE
            )
        )

    @property
    def known_fields(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                key
                for key, val in self._identity_fields().items()
                if val != UNKNOWN_PROVENANCE
            )
        )

    @property
    def has_complete_runtime_identity(self) -> bool:
        """Return True when all required runtime identity fields are known.

        Observational repository/policy checksums (``main_sha``, ``source_event``,
        ``reusable_workflow_tag_object``, ``frozen_policy_sha256``, ``gates``) may
        remain ``"unknown"`` when uncollected without invalidating runtime identity.
        """

        required_runtime = (
            self.repository,
            self.repository_id,
            self.pull_request,
            self.base_ref,
            self.base_sha,
            self.head_sha,
            self.caller_workflow,
            self.reusable_workflow_peeled_commit,
            self.installed_core_version,
            self.installed_hosted_version,
            self.selected_executor,
            self.frozen_policy_commit,
        )
        if any(item == UNKNOWN_PROVENANCE for item in required_runtime):
            return False
        if self.broker_route == UNKNOWN_PROVENANCE:
            return False
        if self.broker_route == "none":
            return True
        return self.broker_revision != UNKNOWN_PROVENANCE

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROVENANCE_MANIFEST_SCHEMA_VERSION,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
            "main_sha": self.main_sha,
            "main_sha_is_runtime_proof": False,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "source_event": self.source_event.to_dict(),
            "caller_workflow": self.caller_workflow,
            "reusable_workflow_ref": self.reusable_workflow_ref,
            "reusable_workflow_tag_object": self.reusable_workflow_tag_object,
            "reusable_workflow_peeled_commit": self.reusable_workflow_peeled_commit,
            "installed_core_version": self.installed_core_version,
            "installed_hosted_version": self.installed_hosted_version,
            "selected_executor": self.selected_executor,
            "broker_route": self.broker_route,
            "broker_revision": self.broker_revision,
            "frozen_policy_commit": self.frozen_policy_commit,
            "frozen_policy_sha256": self.frozen_policy_sha256,
            "gates": self.gates.to_dict(),
            "authority": False,
        }

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self._body_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = self._body_dict()
        payload["manifest_sha256"] = self.manifest_sha256
        return payload

    @classmethod
    def from_dict(
        cls, data: object, *, require_digest: bool = False
    ) -> ProvenanceManifest:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "schema_version",
                    "repository",
                    "repository_id",
                    "pull_request",
                    "main_sha",
                    "main_sha_is_runtime_proof",
                    "base_ref",
                    "base_sha",
                    "head_sha",
                    "source_event",
                    "caller_workflow",
                    "reusable_workflow_ref",
                    "reusable_workflow_tag_object",
                    "reusable_workflow_peeled_commit",
                    "installed_core_version",
                    "installed_hosted_version",
                    "selected_executor",
                    "broker_route",
                    "broker_revision",
                    "frozen_policy_commit",
                    "frozen_policy_sha256",
                    "gates",
                    "authority",
                }
            ),
            optional=frozenset({"manifest_sha256"}),
            label="provenance_manifest",
        )
        if require_digest and "manifest_sha256" not in mapping:
            raise ReviewInputError(
                "provenance_manifest missing required field: manifest_sha256"
            )
        if mapping["schema_version"] != PROVENANCE_MANIFEST_SCHEMA_VERSION:
            raise ReviewInputError("unsupported provenance manifest schema_version")
        if mapping["main_sha_is_runtime_proof"] is not False:
            raise ReviewInputError("main_sha_is_runtime_proof must be exactly False")
        _exact_false_authority(mapping["authority"], field="authority")
        manifest = cls(
            repository=mapping["repository"],
            repository_id=mapping["repository_id"],
            pull_request=mapping["pull_request"],
            main_sha=mapping["main_sha"],
            base_ref=mapping["base_ref"],
            base_sha=mapping["base_sha"],
            head_sha=mapping["head_sha"],
            source_event=SourceEventIdentity.from_dict(mapping["source_event"]),
            caller_workflow=mapping["caller_workflow"],
            reusable_workflow_ref=mapping["reusable_workflow_ref"],
            reusable_workflow_tag_object=mapping["reusable_workflow_tag_object"],
            reusable_workflow_peeled_commit=mapping["reusable_workflow_peeled_commit"],
            installed_core_version=mapping["installed_core_version"],
            installed_hosted_version=mapping["installed_hosted_version"],
            selected_executor=mapping["selected_executor"],
            broker_route=mapping["broker_route"],
            broker_revision=mapping["broker_revision"],
            frozen_policy_commit=mapping["frozen_policy_commit"],
            frozen_policy_sha256=mapping["frozen_policy_sha256"],
            gates=GateObservations.from_dict(mapping["gates"]),
            authority=False,
        )
        if "manifest_sha256" in mapping:
            expected = _sha256_or_unknown(
                mapping["manifest_sha256"], field="manifest_sha256"
            )
            if expected != manifest.manifest_sha256:
                raise ReviewInputError("manifest_sha256 does not match payload")
        return manifest


@dataclass(frozen=True)
class RecoveryPrescription:
    """Deterministic, bounded recovery path attached to every outcome."""

    action: str
    command: str
    requires_fresh_inference: bool
    steps: tuple[str, ...]
    preserves_obligations: bool = True
    preserves_reservations: bool = True
    preserves_unknown_charges: bool = True
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        if self.preserves_obligations is not True:
            raise ReviewInputError("preserves_obligations must be True")
        if self.preserves_reservations is not True:
            raise ReviewInputError("preserves_reservations must be True")
        if self.preserves_unknown_charges is not True:
            raise ReviewInputError("preserves_unknown_charges must be True")
        object.__setattr__(
            self,
            "action",
            _closed_choice(self.action, RECOVERY_ACTIONS, field="recovery.action"),
        )
        object.__setattr__(
            self,
            "command",
            _closed_choice(self.command, RECOVERY_COMMANDS, field="recovery.command"),
        )
        object.__setattr__(
            self,
            "requires_fresh_inference",
            _exact_bool(
                self.requires_fresh_inference, field="recovery.requires_fresh_inference"
            ),
        )
        if isinstance(self.steps, (str, bytes)) or not isinstance(self.steps, Sequence):
            raise ReviewInputError("recovery.steps must be a sequence of step strings")
        normalized_steps = tuple(self.steps)
        if not 1 <= len(normalized_steps) <= MAX_SAFE_STEPS:
            raise ReviewInputError("recovery.steps must contain 1..8 bounded steps")
        for idx, step in enumerate(normalized_steps):
            if (
                not isinstance(step, str)
                or not step.strip()
                or len(step) > MAX_SAFE_TEXT_LENGTH
            ):
                raise ReviewInputError(f"recovery.steps[{idx}] must be bounded text")
            _reject_secret_or_control(step, field=f"recovery.steps[{idx}]")
        object.__setattr__(self, "steps", normalized_steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "command": self.command,
            "requires_fresh_inference": self.requires_fresh_inference,
            "preserves_obligations": True,
            "preserves_reservations": True,
            "preserves_unknown_charges": True,
            "steps": list(self.steps),
            "authority": False,
        }

    @classmethod
    def from_dict(cls, data: object) -> RecoveryPrescription:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "action",
                    "command",
                    "requires_fresh_inference",
                    "preserves_obligations",
                    "preserves_reservations",
                    "preserves_unknown_charges",
                    "steps",
                    "authority",
                }
            ),
            optional=frozenset(),
            label="recovery",
        )
        return cls(
            action=mapping["action"],
            command=mapping["command"],
            requires_fresh_inference=mapping["requires_fresh_inference"],
            steps=tuple(mapping["steps"])
            if isinstance(mapping["steps"], list)
            else mapping["steps"],
            preserves_obligations=mapping["preserves_obligations"],
            preserves_reservations=mapping["preserves_reservations"],
            preserves_unknown_charges=mapping["preserves_unknown_charges"],
            authority=mapping["authority"],
        )


def prescribe_recovery(
    classifier_outcome: str,
    *,
    has_retained_result: bool = False,
    has_partial_coverage: bool = False,
    has_uncertain_dispatches: bool = False,
    ledger_expired: bool = False,
    only_convergence_pending: bool = False,
) -> RecoveryPrescription:
    """Select a deterministic, bounded recovery path for any classified state.

    Precedence: unresolved ``has_uncertain_dispatches`` always takes priority over
    ``ledger_expired`` so in-flight writes are reconciled via owned readback
    before any controlled reenrollment is attempted.
    """

    outcome = _closed_choice(
        classifier_outcome, CLASSIFIER_OUTCOMES, field="classifier_outcome"
    )
    if has_uncertain_dispatches:
        return RecoveryPrescription(
            action="reconcile_owned_readback_then_hold_or_dispose",
            command="reconcile-owned-readback",
            requires_fresh_inference=False,
            steps=(
                "Perform exact owned readback for every uncertain dispatch first.",
                "Keep unknown usage charges and reservations held without blind retry.",
                "After readback confirms or bounds effects, run @reviewsensei verify or explicit human disposition.",
            ),
        )
    if ledger_expired:
        return RecoveryPrescription(
            action="controlled_reenrollment",
            command="controlled-reenroll",
            requires_fresh_inference=False,
            steps=(
                "Verify marker expiry and absence of active quarantine holds.",
                "Execute operator-authorized controlled reenrollment preserving prior obligations.",
                "Follow with @reviewsensei rescan or @reviewsensei verify on current head.",
            ),
        )
    if outcome == "eligible_recovery_awaiting_authorization":
        if only_convergence_pending:
            return RecoveryPrescription(
                action="verify_convergence",
                command="@reviewsensei verify",
                requires_fresh_inference=False,
                steps=(
                    "Reconcile thread/check convergence via @reviewsensei verify with zero model inference.",
                    "Preserve existing finding dispositions and original request accounting.",
                ),
            )
        if has_partial_coverage:
            return RecoveryPrescription(
                action="review_remaining",
                command="@reviewsensei review remaining",
                requires_fresh_inference=True,
                steps=(
                    "Reuse retained coverage prefix on the matching head/base/policy snapshot.",
                    "Run @reviewsensei review remaining for uncovered candidates within original budget.",
                ),
            )
        if has_retained_result:
            return RecoveryPrescription(
                action="replay_retained_result",
                command="supply-original-recovery-context",
                requires_fresh_inference=False,
                steps=(
                    "Supply the authenticated retained result and original recovery context bundle.",
                    "Replay publication/finalization in a fresh process with zero fresh model inference.",
                ),
            )
    if outcome == "provenance_mismatch":
        return RecoveryPrescription(
            action="rescan_under_pinned_provenance",
            command="@reviewsensei rescan",
            requires_fresh_inference=True,
            steps=(
                "Do not rebind old results across mismatched workflow, package, broker, or policy provenance.",
                "Confirm predecessor accounting is terminal or reconciled without erasing obligations.",
                "Run @reviewsensei rescan under the pinned workflow commit and frozen policy commit.",
            ),
        )
    if outcome == "missing_evidence":
        return RecoveryPrescription(
            action="supply_original_context_or_rescan",
            command="supply-original-recovery-context",
            requires_fresh_inference=False,
            steps=(
                "Do not reconstruct missing provenance or admission inputs from current live state.",
                "Supply the authenticated original recovery-context bundle for zero-inference replay if retained.",
                "Otherwise, once predecessor holds are settled, run @reviewsensei rescan on the current head.",
            ),
        )
    if outcome in {"unsupported_legacy_state", "explicit_human_decision_required"}:
        return RecoveryPrescription(
            action="reconcile_owned_readback_then_hold_or_dispose",
            command="reconcile-owned-readback",
            requires_fresh_inference=False,
            steps=(
                "Preserve legacy/unknown state and all prior finding obligations intact.",
                "Record an explicit maintainer disposition or run @reviewsensei rescan after authorized settlement.",
            ),
        )
    if only_convergence_pending:
        return RecoveryPrescription(
            action="verify_convergence",
            command="@reviewsensei verify",
            requires_fresh_inference=False,
            steps=(
                "Observation complete; run @reviewsensei verify if convergence recheck is needed.",
            ),
        )
    return RecoveryPrescription(
        action="no_action_needed",
        command="none",
        requires_fresh_inference=False,
        steps=(
            "Read-only provenance observation recorded; no recovery mutation required.",
        ),
    )


@dataclass(frozen=True)
class ProvenanceCompatibilityReport:
    """Result of comparing an observed ProvenanceManifest against expectations."""

    status: str
    mismatched_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    notes: tuple[str, ...]
    recovery: RecoveryPrescription
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        if self.status not in {"compatible", "mismatch", "incomplete"}:
            raise ReviewInputError("invalid compatibility status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mismatched_fields": list(self.mismatched_fields),
            "missing_fields": list(self.missing_fields),
            "notes": list(self.notes),
            "recovery": self.recovery.to_dict(),
            "authority": False,
        }


def classify_provenance_compatibility(
    manifest: ProvenanceManifest,
    *,
    expected_peeled_commit: str | None = None,
    expected_tag_object: str | None = None,
    expected_core_version: str | None = None,
    expected_hosted_version: str | None = None,
    expected_policy_commit: str | None = None,
    expected_broker_revision: str | None = None,
    compatibility_manifest: CompatibilityManifest | None = None,
) -> ProvenanceCompatibilityReport:
    """Compare runtime/source provenance without probing or elevating authority."""

    if not isinstance(manifest, ProvenanceManifest):
        raise ReviewInputError("manifest must be a ProvenanceManifest")

    mismatches: list[str] = []
    missing: list[str] = []
    notes: list[str] = []

    if (
        manifest.main_sha != UNKNOWN_PROVENANCE
        and manifest.reusable_workflow_peeled_commit != UNKNOWN_PROVENANCE
        and manifest.main_sha != manifest.reusable_workflow_peeled_commit
    ):
        notes.append("main_sha_differs_from_executing_workflow_commit")

    if (
        manifest.reusable_workflow_tag_object != UNKNOWN_PROVENANCE
        and manifest.reusable_workflow_peeled_commit != UNKNOWN_PROVENANCE
        and manifest.reusable_workflow_tag_object
        == manifest.reusable_workflow_peeled_commit
    ):
        notes.append("tag_object_equals_peeled_commit_lightweight_or_direct_sha")

    if manifest.gates.has_routing_pass_with_live_refusal_or_unknown():
        notes.append("routing_preflight_passed_without_live_validation_pass")

    checks: list[tuple[str, str, str | None]] = [
        (
            "reusable_workflow_peeled_commit",
            manifest.reusable_workflow_peeled_commit,
            expected_peeled_commit,
        ),
        (
            "reusable_workflow_tag_object",
            manifest.reusable_workflow_tag_object,
            expected_tag_object,
        ),
        (
            "installed_core_version",
            manifest.installed_core_version,
            expected_core_version,
        ),
        (
            "installed_hosted_version",
            manifest.installed_hosted_version,
            expected_hosted_version,
        ),
        (
            "frozen_policy_commit",
            manifest.frozen_policy_commit,
            expected_policy_commit,
        ),
        (
            "broker_revision",
            manifest.broker_revision,
            expected_broker_revision,
        ),
    ]

    for field_name, observed_val, expected_val in checks:
        if expected_val is None:
            continue
        if observed_val == UNKNOWN_PROVENANCE:
            missing.append(field_name)
        elif observed_val != expected_val:
            mismatches.append(field_name)

    if compatibility_manifest is not None:
        if (
            expected_peeled_commit is not None
            and expected_peeled_commit != compatibility_manifest.workflow_commit
        ):
            raise ReviewInputError(
                "expected_peeled_commit conflicts with compatibility_manifest.workflow_commit"
            )
        if (
            expected_core_version is not None
            and expected_core_version != compatibility_manifest.python.version
        ):
            raise ReviewInputError(
                "expected_core_version conflicts with compatibility_manifest.python.version"
            )
        if expected_broker_revision is not None:
            normalized_expected_broker = expected_broker_revision.removeprefix("v")
            if _SEMVER_RE.fullmatch(normalized_expected_broker) is not None:
                base_expected_broker = normalized_expected_broker.split("-", 1)[
                    0
                ].split("+", 1)[0]
                if not _range_contains(
                    base_expected_broker,
                    compatibility_manifest.compatible_worker_range,
                ):
                    raise ReviewInputError(
                        "expected_broker_revision conflicts with compatibility_manifest.compatible_worker_range"
                    )

        if manifest.reusable_workflow_peeled_commit == UNKNOWN_PROVENANCE:
            if "reusable_workflow_peeled_commit" not in missing:
                missing.append("reusable_workflow_peeled_commit")
        elif (
            manifest.reusable_workflow_peeled_commit
            != compatibility_manifest.workflow_commit
            and "reusable_workflow_peeled_commit" not in mismatches
        ):
            mismatches.append("reusable_workflow_peeled_commit")

        if manifest.installed_core_version == UNKNOWN_PROVENANCE:
            if "installed_core_version" not in missing:
                missing.append("installed_core_version")
        elif (
            manifest.installed_core_version != compatibility_manifest.python.version
            and "installed_core_version" not in mismatches
        ):
            mismatches.append("installed_core_version")

        if manifest.broker_route not in {"none", UNKNOWN_PROVENANCE}:
            if manifest.broker_revision == UNKNOWN_PROVENANCE:
                if "broker_revision" not in missing:
                    missing.append("broker_revision")
            else:
                normalized_worker = manifest.broker_revision.removeprefix("v")
                if _SEMVER_RE.fullmatch(normalized_worker) is not None:
                    base_semver = normalized_worker.split("-", 1)[0].split("+", 1)[0]
                    if not _range_contains(
                        base_semver, compatibility_manifest.compatible_worker_range
                    ):
                        if "broker_revision" not in mismatches:
                            mismatches.append("broker_revision")
                else:
                    notes.append("broker_revision_is_opaque_deployment_uuid_or_sha")
                    if (
                        expected_broker_revision is None
                        and manifest.broker_revision
                        != compatibility_manifest.worker.version
                        and "broker_revision" not in mismatches
                    ):
                        mismatches.append("broker_revision")

    if mismatches:
        status = "mismatch"
        recovery = prescribe_recovery("provenance_mismatch")
    elif missing:
        status = "incomplete"
        recovery = prescribe_recovery("missing_evidence")
    else:
        status = "compatible"
        recovery = prescribe_recovery("supported_read_only_observation")

    return ProvenanceCompatibilityReport(
        status=status,
        mismatched_fields=tuple(sorted(set(mismatches))),
        missing_fields=tuple(sorted(set(missing))),
        notes=tuple(notes),
        recovery=recovery,
        authority=False,
    )


@dataclass(frozen=True)
class OriginalInputBindings:
    """Content-addressed digests for original recovery inputs (never raw text)."""

    configuration_sha256: str = UNKNOWN_PROVENANCE
    defaults_sha256: str = UNKNOWN_PROVENANCE
    context_selection_sha256: str = UNKNOWN_PROVENANCE
    learnings_sha256: str = UNKNOWN_PROVENANCE
    baseline_sha256: str = UNKNOWN_PROVENANCE
    accepted_result_sha256: str = UNKNOWN_PROVENANCE
    decision_policy_sha256: str = UNKNOWN_PROVENANCE

    def __post_init__(self) -> None:
        for attr in (
            "configuration_sha256",
            "defaults_sha256",
            "context_selection_sha256",
            "learnings_sha256",
            "baseline_sha256",
            "accepted_result_sha256",
            "decision_policy_sha256",
        ):
            object.__setattr__(
                self,
                attr,
                _sha256_or_unknown(getattr(self, attr), field=attr, allow_missing=True),
            )

    @property
    def missing_input_labels(self) -> tuple[str, ...]:
        mapping = {
            "configuration": self.configuration_sha256,
            "defaults": self.defaults_sha256,
            "context_selection": self.context_selection_sha256,
            "learnings": self.learnings_sha256,
            "baseline": self.baseline_sha256,
            "accepted_result": self.accepted_result_sha256,
            "decision_policy": self.decision_policy_sha256,
        }
        return tuple(
            sorted(
                label
                for label, digest in mapping.items()
                if digest in {UNKNOWN_PROVENANCE, MISSING_BINDING}
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "configuration_sha256": self.configuration_sha256,
            "defaults_sha256": self.defaults_sha256,
            "context_selection_sha256": self.context_selection_sha256,
            "learnings_sha256": self.learnings_sha256,
            "baseline_sha256": self.baseline_sha256,
            "accepted_result_sha256": self.accepted_result_sha256,
            "decision_policy_sha256": self.decision_policy_sha256,
            "missing_input_labels": list(self.missing_input_labels),
        }

    @classmethod
    def from_dict(cls, data: object) -> OriginalInputBindings:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "configuration_sha256",
                    "defaults_sha256",
                    "context_selection_sha256",
                    "learnings_sha256",
                    "baseline_sha256",
                    "accepted_result_sha256",
                    "decision_policy_sha256",
                }
            ),
            optional=frozenset({"missing_input_labels"}),
            label="original_bindings",
        )
        instance = cls(
            configuration_sha256=mapping["configuration_sha256"],
            defaults_sha256=mapping["defaults_sha256"],
            context_selection_sha256=mapping["context_selection_sha256"],
            learnings_sha256=mapping["learnings_sha256"],
            baseline_sha256=mapping["baseline_sha256"],
            accepted_result_sha256=mapping["accepted_result_sha256"],
            decision_policy_sha256=mapping["decision_policy_sha256"],
        )
        if "missing_input_labels" in mapping:
            if mapping["missing_input_labels"] != list(instance.missing_input_labels):
                raise ReviewInputError(
                    "missing_input_labels disagrees with digest values"
                )
        return instance


@dataclass(frozen=True)
class AttemptBudgetSummary:
    """Conservative request and reservation ledger for one attempt."""

    run_id: int | str = UNKNOWN_PROVENANCE
    attempt: int | str = UNKNOWN_PROVENANCE
    owner_generation: int | str = UNKNOWN_PROVENANCE
    original_api_limit: int | str = UNKNOWN_PROVENANCE
    reserved_requests: int = 0
    consumed_requests: int = 0
    unknown_usage_requests: int = 0
    protected_tail_requests: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "run_id", _positive_int_or_unknown(self.run_id, field="run_id")
        )
        object.__setattr__(
            self, "attempt", _positive_int_or_unknown(self.attempt, field="attempt")
        )
        object.__setattr__(
            self,
            "owner_generation",
            _non_negative_int_or_unknown(
                self.owner_generation, field="owner_generation"
            ),
        )
        object.__setattr__(
            self,
            "original_api_limit",
            _positive_int_or_unknown(
                self.original_api_limit, field="original_api_limit"
            ),
        )
        for attr in (
            "reserved_requests",
            "consumed_requests",
            "unknown_usage_requests",
            "protected_tail_requests",
        ):
            object.__setattr__(
                self, attr, _non_negative_int(getattr(self, attr), field=attr)
            )

    @property
    def conservative_committed_requests(self) -> int:
        return (
            self.consumed_requests
            + self.unknown_usage_requests
            + self.reserved_requests
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attempt": self.attempt,
            "owner_generation": self.owner_generation,
            "original_api_limit": self.original_api_limit,
            "reserved_requests": self.reserved_requests,
            "consumed_requests": self.consumed_requests,
            "unknown_usage_requests": self.unknown_usage_requests,
            "protected_tail_requests": self.protected_tail_requests,
            "conservative_committed_requests": self.conservative_committed_requests,
        }

    @classmethod
    def from_dict(cls, data: object) -> AttemptBudgetSummary:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "run_id",
                    "attempt",
                    "owner_generation",
                    "original_api_limit",
                    "reserved_requests",
                    "consumed_requests",
                    "unknown_usage_requests",
                    "protected_tail_requests",
                }
            ),
            optional=frozenset({"conservative_committed_requests"}),
            label="budget",
        )
        instance = cls(
            run_id=mapping["run_id"],
            attempt=mapping["attempt"],
            owner_generation=mapping["owner_generation"],
            original_api_limit=mapping["original_api_limit"],
            reserved_requests=mapping["reserved_requests"],
            consumed_requests=mapping["consumed_requests"],
            unknown_usage_requests=mapping["unknown_usage_requests"],
            protected_tail_requests=mapping["protected_tail_requests"],
        )
        if "conservative_committed_requests" in mapping:
            if (
                mapping["conservative_committed_requests"]
                != instance.conservative_committed_requests
            ):
                raise ReviewInputError(
                    "conservative_committed_requests disagrees with request counts"
                )
        return instance


@dataclass(frozen=True)
class LifecycleReceipts:
    """Separate receipts for analysis, retention, publication, check, and close."""

    intent: str = UNKNOWN_PROVENANCE
    execution_started: str = UNKNOWN_PROVENANCE
    provider_request_result: str = UNKNOWN_PROVENANCE
    retained_result_parts: str = UNKNOWN_PROVENANCE
    publication: str = UNKNOWN_PROVENANCE
    check: str = UNKNOWN_PROVENANCE
    thread_cleanup: str = UNKNOWN_PROVENANCE
    native_approval: str = UNKNOWN_PROVENANCE
    terminal_accounting: str = UNKNOWN_PROVENANCE

    def __post_init__(self) -> None:
        for attr in (
            "intent",
            "execution_started",
            "provider_request_result",
            "retained_result_parts",
            "publication",
            "check",
            "thread_cleanup",
            "native_approval",
            "terminal_accounting",
        ):
            object.__setattr__(
                self,
                attr,
                _closed_choice(getattr(self, attr), RECEIPT_STATES, field=attr),
            )

    def publication_settled_without_terminal_accounting(self) -> bool:
        return (
            self.publication == "confirmed" and self.terminal_accounting != "confirmed"
        )

    def check_green_without_approval_or_accounting(self) -> bool:
        return self.check == "confirmed" and (
            self.native_approval != "confirmed"
            or self.terminal_accounting != "confirmed"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "intent": self.intent,
            "execution_started": self.execution_started,
            "provider_request_result": self.provider_request_result,
            "retained_result_parts": self.retained_result_parts,
            "publication": self.publication,
            "check": self.check,
            "thread_cleanup": self.thread_cleanup,
            "native_approval": self.native_approval,
            "terminal_accounting": self.terminal_accounting,
        }

    @classmethod
    def from_dict(cls, data: object) -> LifecycleReceipts:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "intent",
                    "execution_started",
                    "provider_request_result",
                    "retained_result_parts",
                    "publication",
                    "check",
                    "thread_cleanup",
                    "native_approval",
                    "terminal_accounting",
                }
            ),
            optional=frozenset(),
            label="receipts",
        )
        return cls(**mapping)


@dataclass(frozen=True)
class UncertainDispatchRecord:
    """Bounded record of an external write or dispatch whose response was uncertain."""

    operation_key: str
    certainty: str = UNKNOWN_PROVENANCE
    owned_readback_sha256: str = UNKNOWN_PROVENANCE

    def __post_init__(self) -> None:
        if not isinstance(self.operation_key, str):
            raise ReviewInputError("operation_key must be a non-empty key string")
        _reject_secret_or_control(self.operation_key, field="operation_key")
        if (
            _SAFE_KEY_RE.fullmatch(self.operation_key) is None
            or ".." in self.operation_key
        ):
            raise ReviewInputError("operation_key must match safe key syntax")
        object.__setattr__(
            self,
            "certainty",
            _closed_choice(self.certainty, DISPATCH_CERTAINTIES, field="certainty"),
        )
        object.__setattr__(
            self,
            "owned_readback_sha256",
            _sha256_or_unknown(
                self.owned_readback_sha256, field="owned_readback_sha256"
            ),
        )

    @property
    def is_unresolved(self) -> bool:
        return self.certainty in {"unknown", "response_receipt"} and (
            self.owned_readback_sha256 == UNKNOWN_PROVENANCE
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "operation_key": self.operation_key,
            "certainty": self.certainty,
            "owned_readback_sha256": self.owned_readback_sha256,
        }

    @classmethod
    def from_dict(cls, data: object) -> UncertainDispatchRecord:
        mapping = _require_mapping_keys(
            data,
            required=frozenset({"operation_key", "certainty", "owned_readback_sha256"}),
            optional=frozenset(),
            label="uncertain_dispatch",
        )
        return cls(
            operation_key=mapping["operation_key"],
            certainty=mapping["certainty"],
            owned_readback_sha256=mapping["owned_readback_sha256"],
        )


@dataclass(frozen=True)
class NoDuplicateWorkAssertion:
    """Trace-backed assertion that replay/inspection did not duplicate effects."""

    provider_calls: int = 0
    physical_api_dispatches: int = 0
    duplicate_business_effects: int = 0
    unreconciled_retries: int = 0

    def __post_init__(self) -> None:
        for attr in (
            "provider_calls",
            "physical_api_dispatches",
            "duplicate_business_effects",
            "unreconciled_retries",
        ):
            object.__setattr__(
                self, attr, _non_negative_int(getattr(self, attr), field=attr)
            )

    @property
    def holds(self) -> bool:
        """Return True when zero duplicate business effects and zero unreconciled retries occurred."""

        return self.duplicate_business_effects == 0 and self.unreconciled_retries == 0

    @property
    def zero_dispatch_replay(self) -> bool:
        """Return True when replay/inspection also issued zero provider or physical API calls."""

        return (
            self.holds
            and self.provider_calls == 0
            and self.physical_api_dispatches == 0
        )

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "provider_calls": self.provider_calls,
            "physical_api_dispatches": self.physical_api_dispatches,
            "duplicate_business_effects": self.duplicate_business_effects,
            "unreconciled_retries": self.unreconciled_retries,
            "holds": self.holds,
        }

    @classmethod
    def from_dict(cls, data: object) -> NoDuplicateWorkAssertion:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "provider_calls",
                    "physical_api_dispatches",
                    "duplicate_business_effects",
                    "unreconciled_retries",
                }
            ),
            optional=frozenset({"holds"}),
            label="no_duplicate_work",
        )
        instance = cls(
            provider_calls=mapping["provider_calls"],
            physical_api_dispatches=mapping["physical_api_dispatches"],
            duplicate_business_effects=mapping["duplicate_business_effects"],
            unreconciled_retries=mapping["unreconciled_retries"],
        )
        if "holds" in mapping and mapping["holds"] != instance.holds:
            raise ReviewInputError(
                "no_duplicate_work.holds disagrees with trace counts"
            )
        return instance


@dataclass(frozen=True)
class AttemptEvidencePacket:
    """Sanitized, private per-attempt evidence packet (G11 / Section 3)."""

    provenance: ProvenanceManifest
    original_bindings: OriginalInputBindings
    budget: AttemptBudgetSummary
    receipts: LifecycleReceipts
    dispatches: tuple[UncertainDispatchRecord, ...]
    classifier_outcome: str
    recovery: RecoveryPrescription
    no_duplicate_work: NoDuplicateWorkAssertion
    current_base_ref: str = UNKNOWN_PROVENANCE
    current_base_sha: str = UNKNOWN_PROVENANCE
    authority: bool = False

    def __post_init__(self) -> None:
        _exact_false_authority(self.authority, field="authority")
        if not isinstance(self.provenance, ProvenanceManifest):
            raise ReviewInputError("provenance must be a ProvenanceManifest")
        if not isinstance(self.original_bindings, OriginalInputBindings):
            raise ReviewInputError("original_bindings must be an OriginalInputBindings")
        if not isinstance(self.budget, AttemptBudgetSummary):
            raise ReviewInputError("budget must be an AttemptBudgetSummary")
        if not isinstance(self.receipts, LifecycleReceipts):
            raise ReviewInputError("receipts must be a LifecycleReceipts")
        if not isinstance(self.recovery, RecoveryPrescription):
            raise ReviewInputError("recovery must be a RecoveryPrescription")
        if not isinstance(self.no_duplicate_work, NoDuplicateWorkAssertion):
            raise ReviewInputError(
                "no_duplicate_work must be a NoDuplicateWorkAssertion"
            )
        object.__setattr__(
            self,
            "classifier_outcome",
            _closed_choice(
                self.classifier_outcome,
                CLASSIFIER_OUTCOMES,
                field="classifier_outcome",
            ),
        )
        object.__setattr__(
            self,
            "current_base_ref",
            _ref_or_unknown(self.current_base_ref, field="current_base_ref"),
        )
        object.__setattr__(
            self,
            "current_base_sha",
            _git_sha_or_unknown(self.current_base_sha, field="current_base_sha"),
        )
        if isinstance(self.dispatches, (str, bytes)) or not isinstance(
            self.dispatches, Sequence
        ):
            raise ReviewInputError("dispatches must be a sequence")
        normalized_dispatches = tuple(self.dispatches)
        if len(normalized_dispatches) > MAX_UNCERTAIN_DISPATCHES:
            raise ReviewInputError("dispatches exceeds maximum bounded count")
        for item in normalized_dispatches:
            if not isinstance(item, UncertainDispatchRecord):
                raise ReviewInputError(
                    "every dispatch item must be an UncertainDispatchRecord"
                )
        object.__setattr__(self, "dispatches", normalized_dispatches)
        if self.receipts.retained_result_parts == "confirmed" and (
            self.original_bindings.accepted_result_sha256
            in {UNKNOWN_PROVENANCE, MISSING_BINDING}
        ):
            raise ReviewInputError(
                "retained_result_parts cannot be 'confirmed' without accepted_result_sha256"
            )
        if any(item.is_unresolved for item in normalized_dispatches) and (
            self.no_duplicate_work.holds
        ):
            raise ReviewInputError(
                "no_duplicate_work.holds cannot be True while unresolved dispatches remain"
            )
        serialized = _canonical_json_bytes(self._body_dict())
        if len(serialized) > MAX_PROVENANCE_PACKET_BYTES:
            raise ReviewInputError("attempt evidence packet exceeds byte ceiling")

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ATTEMPT_EVIDENCE_SCHEMA_VERSION,
            "provenance": self.provenance.to_dict(),
            "current_base_ref": self.current_base_ref,
            "current_base_sha": self.current_base_sha,
            "original_bindings": self.original_bindings.to_dict(),
            "budget": self.budget.to_dict(),
            "receipts": self.receipts.to_dict(),
            "dispatches": [item.to_dict() for item in self.dispatches],
            "classifier_outcome": self.classifier_outcome,
            "recovery": self.recovery.to_dict(),
            "no_duplicate_work": self.no_duplicate_work.to_dict(),
            "authority": False,
        }

    @property
    def packet_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self._body_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = self._body_dict()
        payload["packet_sha256"] = self.packet_sha256
        return payload

    def to_public_summary(self) -> dict[str, Any]:
        """Return a user-safe, closed-vocabulary summary bounded to 4,096 bytes."""

        recovery_action = _closed_choice(
            self.recovery.action, RECOVERY_ACTIONS, field="recovery.action"
        )
        recovery_command = _closed_choice(
            self.recovery.command, RECOVERY_COMMANDS, field="recovery.command"
        )
        summary = {
            "schema_version": ATTEMPT_EVIDENCE_SCHEMA_VERSION,
            "manifest_sha256": self.provenance.manifest_sha256,
            "packet_sha256": self.packet_sha256,
            "installed_core_version": self.provenance.installed_core_version,
            "installed_hosted_version": self.provenance.installed_hosted_version,
            "reusable_workflow_peeled_commit": (
                self.provenance.reusable_workflow_peeled_commit
            ),
            "broker_route": self.provenance.broker_route,
            "gates": self.provenance.gates.to_dict(),
            "unknown_provenance_count": len(self.provenance.unknown_fields),
            "missing_input_labels": list(self.original_bindings.missing_input_labels),
            "receipts": self.receipts.to_dict(),
            "unresolved_dispatch_count": sum(
                1 for item in self.dispatches if item.is_unresolved
            ),
            "classifier_outcome": _closed_choice(
                self.classifier_outcome,
                CLASSIFIER_OUTCOMES,
                field="classifier_outcome",
            ),
            "recovery_action": recovery_action,
            "recovery_command": recovery_command,
            "no_duplicate_work_holds": self.no_duplicate_work.holds,
            "authority": False,
        }
        encoded = _canonical_json_bytes(summary)
        if len(encoded) > MAX_PUBLIC_SUMMARY_BYTES:  # pragma: no cover - defensive cap
            raise ReviewInputError("public provenance summary exceeds 4,096 bytes")
        return summary

    @classmethod
    def from_dict(
        cls, data: object, *, require_digest: bool = False
    ) -> AttemptEvidencePacket:
        mapping = _require_mapping_keys(
            data,
            required=frozenset(
                {
                    "schema_version",
                    "provenance",
                    "current_base_ref",
                    "current_base_sha",
                    "original_bindings",
                    "budget",
                    "receipts",
                    "dispatches",
                    "classifier_outcome",
                    "recovery",
                    "no_duplicate_work",
                    "authority",
                }
            ),
            optional=frozenset({"packet_sha256"}),
            label="attempt_evidence_packet",
        )
        if require_digest and "packet_sha256" not in mapping:
            raise ReviewInputError(
                "attempt_evidence_packet missing required field: packet_sha256"
            )
        if mapping["schema_version"] != ATTEMPT_EVIDENCE_SCHEMA_VERSION:
            raise ReviewInputError("unsupported attempt evidence packet schema_version")
        _exact_false_authority(mapping["authority"], field="authority")
        raw_dispatches = mapping["dispatches"]
        if not isinstance(raw_dispatches, list):
            raise ReviewInputError("dispatches must be a JSON array")
        packet = cls(
            provenance=ProvenanceManifest.from_dict(
                mapping["provenance"], require_digest=require_digest
            ),
            current_base_ref=mapping["current_base_ref"],
            current_base_sha=mapping["current_base_sha"],
            original_bindings=OriginalInputBindings.from_dict(
                mapping["original_bindings"]
            ),
            budget=AttemptBudgetSummary.from_dict(mapping["budget"]),
            receipts=LifecycleReceipts.from_dict(mapping["receipts"]),
            dispatches=tuple(
                UncertainDispatchRecord.from_dict(item) for item in raw_dispatches
            ),
            classifier_outcome=mapping["classifier_outcome"],
            recovery=RecoveryPrescription.from_dict(mapping["recovery"]),
            no_duplicate_work=NoDuplicateWorkAssertion.from_dict(
                mapping["no_duplicate_work"]
            ),
            authority=False,
        )
        if "packet_sha256" in mapping:
            expected = _sha256_or_unknown(
                mapping["packet_sha256"], field="packet_sha256"
            )
            if expected != packet.packet_sha256:
                raise ReviewInputError("packet_sha256 does not match payload")
        return packet


def inspect_installed_package_versions(
    *,
    version_lookup: Callable[[str], str] | None = None,
) -> tuple[str, str]:
    """Read local installed core and hosted package metadata without probing.

    Missing package metadata resolves to ``"unknown"``; ``pyproject.toml`` or
    checked-out source files are never consulted as installation proof.
    """

    lookup = version_lookup or importlib.metadata.version
    resolved: list[str] = []
    for dist_name in INSTALLED_DISTRIBUTION_NAMES:
        try:
            raw = lookup(dist_name)
        except (importlib.metadata.PackageNotFoundError, LookupError, ValueError):
            resolved.append(UNKNOWN_PROVENANCE)
            continue
        if (
            isinstance(raw, str)
            and len(raw) <= 128
            and raw == raw.strip()
            and _SEMVER_RE.fullmatch(raw) is not None
        ):
            resolved.append(raw)
        else:
            resolved.append(UNKNOWN_PROVENANCE)
    return resolved[0], resolved[1]


def build_attempt_evidence_packet(
    *,
    provenance: ProvenanceManifest,
    original_bindings: OriginalInputBindings | None = None,
    budget: AttemptBudgetSummary | None = None,
    receipts: LifecycleReceipts | None = None,
    dispatches: Sequence[UncertainDispatchRecord] = (),
    classifier_outcome: str | None = None,
    current_base_ref: str = UNKNOWN_PROVENANCE,
    current_base_sha: str = UNKNOWN_PROVENANCE,
    no_duplicate_work: NoDuplicateWorkAssertion | None = None,
    has_partial_coverage: bool = False,
    ledger_expired: bool = False,
) -> AttemptEvidencePacket:
    """Build a complete, sanitized AttemptEvidencePacket with bounded recovery."""

    bindings = original_bindings or OriginalInputBindings()
    effective_budget = budget or AttemptBudgetSummary()
    effective_receipts = receipts or LifecycleReceipts()
    normalized_dispatches = tuple(dispatches)
    unresolved_count = sum(1 for item in normalized_dispatches if item.is_unresolved)
    has_unresolved_dispatch = unresolved_count > 0
    has_retained_result = (
        bindings.accepted_result_sha256 not in {UNKNOWN_PROVENANCE, MISSING_BINDING}
        and effective_receipts.retained_result_parts == "confirmed"
    )
    only_convergence = (
        effective_receipts.publication == "confirmed"
        and effective_receipts.terminal_accounting == "confirmed"
        and (
            effective_receipts.check != "confirmed"
            or effective_receipts.thread_cleanup == "pending"
        )
    )

    if classifier_outcome is None:
        if has_unresolved_dispatch:
            selected_outcome = "explicit_human_decision_required"
        elif bindings.missing_input_labels and (
            effective_receipts.publication in {"pending", "failed"}
        ):
            selected_outcome = "missing_evidence"
        elif (
            has_retained_result
            and effective_receipts.terminal_accounting != "confirmed"
        ) or only_convergence:
            selected_outcome = "eligible_recovery_awaiting_authorization"
        elif effective_receipts.publication_settled_without_terminal_accounting() or (
            effective_receipts.check == "confirmed"
            and effective_receipts.terminal_accounting != "confirmed"
        ):
            selected_outcome = "explicit_human_decision_required"
        else:
            selected_outcome = "supported_read_only_observation"
    else:
        selected_outcome = classifier_outcome

    prescription = prescribe_recovery(
        selected_outcome,
        has_retained_result=has_retained_result,
        has_partial_coverage=has_partial_coverage,
        has_uncertain_dispatches=has_unresolved_dispatch,
        ledger_expired=ledger_expired,
        only_convergence_pending=only_convergence,
    )
    assertion = no_duplicate_work or NoDuplicateWorkAssertion(
        unreconciled_retries=unresolved_count
    )
    return AttemptEvidencePacket(
        provenance=provenance,
        original_bindings=bindings,
        budget=effective_budget,
        receipts=effective_receipts,
        dispatches=normalized_dispatches,
        classifier_outcome=selected_outcome,
        recovery=prescription,
        no_duplicate_work=assertion,
        current_base_ref=current_base_ref,
        current_base_sha=current_base_sha,
        authority=False,
    )
