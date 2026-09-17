from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import json
import math
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .errors import ReviewInputError, ReviewSenseiError
from .learnings import LearningStore, learning_digest
from .models import (
    LearningEntry,
    ProviderRequest,
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
)
from .providers.base import ReviewProvider
from .providers.profiles import ProviderProfile, get_provider_profile
from .schemas import validate_public_document
from .service import ReviewService
from .stages import ReviewCategory, Stage
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    ReviewLimits,
    read_bounded_utf8,
    validate_repository_path,
)

MAX_CORPUS_FILES = 128
MAX_CORPUS_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CORPUS_JSON_BYTES = 256 * 1024
MAX_JSON_FILE_BYTES = 256 * 1024
MAX_TEXT_FILE_BYTES = 512 * 1024

_PRIVACY_PATTERNS = (
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[0-9A-Za-z]{20,}\b"),
    re.compile(r"\bBearer [0-9A-Za-z._~+/=-]{20,}\b"),
    re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_SECRET_MARKERS = ("PRIVATE_DIFF_MARKER", "PRIVATE_PROMPT_MARKER", "PROD_OUTPUT_MARKER")


def _reproducibility_dict(value: object) -> dict[str, Any]:
    """Copy reproducibility settings into a JSON-object-shaped dictionary."""

    if not isinstance(value, Mapping):
        raise ReviewInputError("promotion record reproducibility must be an object")
    try:
        copied = dict(value)
    except (TypeError, ValueError) as exc:
        raise ReviewInputError(
            "promotion record reproducibility must be an object"
        ) from exc
    if any(not isinstance(key, str) for key in copied):
        raise ReviewInputError("promotion record reproducibility keys must be strings")
    return copied


def _is_fixture_alias(value: str) -> bool:
    """Recognize fixture-only provider aliases without case sensitivity."""

    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    return normalized in {
        "fixture",
        "fixture-provider",
        "fixture-v1",
        "fixtureprovider",
        "stub",
        "stub-provider",
        "stubprovider",
        "fake",
        "fake-provider",
        "fakeprovider",
    }


@dataclass(frozen=True)
class PromotionRecord:
    """Evidence metadata required before promoting a real model/prompt configuration.

    Every status records at least one observed run; only ``supported`` requires
    three or more runs and reproducibility settings before promotion.
    The status and rollback defaults preserve direct-constructor compatibility;
    ``validate_promotion_record`` still requires both fields in untrusted JSON.
    """

    engine_digest: str
    prompt_digest: str
    configuration_digest: str
    corpus_digest: str
    provider: str
    model: str
    observed_revision: str
    run_count: int
    evaluated_at: str
    reproducibility: Mapping[str, Any]
    status: str = "supported"
    rollback_decision: str = "revert-to-baseline"

    def __post_init__(self) -> None:
        for name in (
            "engine_digest",
            "prompt_digest",
            "configuration_digest",
            "corpus_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ReviewInputError(
                    f"promotion record {name} must be a SHA-256 digest"
                )
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.provider,
                self.model,
                self.observed_revision,
                self.evaluated_at,
            )
        ):
            raise ReviewInputError("promotion record identity fields are required")
        reproducibility = _reproducibility_dict(self.reproducibility)
        object.__setattr__(self, "reproducibility", reproducibility)
        if (
            isinstance(self.run_count, bool)
            or not isinstance(self.run_count, int)
            or self.run_count < 1
        ):
            raise ReviewInputError("promotion record run_count must be positive")
        if self.status not in {"supported", "insufficient", "unsupported"}:
            raise ReviewInputError("promotion record status is unsupported")
        if self.status == "supported" and self.run_count < 3:
            raise ReviewInputError(
                "supported promotion evidence requires at least three runs"
            )
        if not self.reproducibility:
            raise ReviewInputError("promotion record requires reproducibility settings")
        if self.status == "supported" and _is_fixture_alias(self.provider):
            raise ReviewInputError("fixture-only evidence cannot support promotion")
        if self.rollback_decision not in {"revert-to-baseline", "hold", "none"}:
            raise ReviewInputError("promotion record rollback decision is unsupported")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": "1.0",
            "engine_digest": self.engine_digest,
            "prompt_digest": self.prompt_digest,
            "configuration_digest": self.configuration_digest,
            "corpus_digest": self.corpus_digest,
            "provider": self.provider,
            "model": self.model,
            "observed_revision": self.observed_revision,
            "run_count": self.run_count,
            "evaluated_at": self.evaluated_at,
            "reproducibility": _reproducibility_dict(self.reproducibility),
            "status": self.status,
            "rollback_decision": self.rollback_decision,
        }
        validate_public_document(value, "promotion-record")
        return value


def validate_promotion_record(value: Mapping[str, Any]) -> PromotionRecord:
    """Parse and validate promotion evidence, rejecting incomplete records."""

    if not isinstance(value, dict):
        raise ReviewInputError("promotion record must be a JSON object")
    validate_public_document(value, "promotion-record")
    try:
        return PromotionRecord(
            **{
                key: value[key]
                for key in (
                    "engine_digest",
                    "prompt_digest",
                    "configuration_digest",
                    "corpus_digest",
                    "provider",
                    "model",
                    "observed_revision",
                    "run_count",
                    "evaluated_at",
                    "reproducibility",
                    "status",
                    "rollback_decision",
                )
            }
        )
    except KeyError as exc:
        raise ReviewInputError("promotion record is incomplete") from exc


@functools.lru_cache(maxsize=1)
def _installed_package_version() -> str:
    try:
        return importlib.metadata.version("review-sensei")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ReviewInputError("review-sensei package metadata is unavailable") from exc


def _required_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ReviewInputError(f"{label} must be a SHA-256 digest")
    return value


def _required_invocation_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewInputError("promotion reports require a unique run invocation_id")
    invocation_id = value.strip()
    if len(invocation_id) > 128:
        raise ReviewInputError("run invocation_id exceeds the 128-character limit")
    return invocation_id


def _category_digest_payload(category: ReviewCategory) -> dict[str, Any]:
    return {
        "id": category.id,
        "title": category.title,
        "focus": list(category.focus),
        "applies_to": list(category.applies_to),
        "learning_categories": list(category.learning_categories),
        "include_uncategorized_learnings": category.include_uncategorized_learnings,
        "document_sources": [
            {
                "path": source.path,
                "include": list(source.include),
                "exclude": list(source.exclude),
                "required": source.required,
            }
            for source in category.document_sources
        ],
    }


def _stage_digest_payload(stages: Sequence[Stage]) -> list[dict[str, Any]]:
    return [
        {
            "name": stage.name,
            "prompt_template": stage.prompt_template,
            "outputs": list(stage.outputs),
            "categories": [
                _category_digest_payload(category) for category in stage.categories
            ],
        }
        for stage in stages
    ]


def engine_digest(
    *,
    package_version: str | None = None,
    limits: ReviewLimits | None = None,
    enforce_locations: bool = True,
) -> str:
    """Return a bounded SHA-256 digest of package and engine identity.

    The payload is canonical JSON over the installed package version, the
    provider-neutral review engine identity, matching algorithm, output-correction
    attempt budget, location enforcement, and ReviewLimits ceilings. It does not
    hash repository source or untrusted inputs.
    """

    version = (
        package_version if package_version is not None else _installed_package_version()
    )
    if not isinstance(version, str) or not version.strip():
        raise ReviewInputError("engine digest requires a package version")
    profile = limits if limits is not None else DEFAULT_REVIEW_LIMITS
    if not isinstance(profile, ReviewLimits):
        raise ReviewInputError("engine digest limits must be a ReviewLimits value")
    return _json_digest(
        {
            "digest_version": 1,
            "package": "review-sensei",
            "version": version.strip(),
            "engine": "review_sensei.service.ReviewService",
            "enforce_locations": bool(enforce_locations),
            "matching": "one-to-one-path-line-category-normalized-body-terms",
            "max_provider_output_attempts": 2,
            "limits": {
                name: getattr(profile, name)
                for name in sorted(profile.__dataclass_fields__)
            },
        }
    )


def prompt_digest(stages: Sequence[Stage] | None = None) -> str:
    """Return a SHA-256 digest of packaged or caller-supplied stage templates."""

    selected: Sequence[Stage]
    if stages is None:
        from .service import DEFAULT_STAGES

        selected = DEFAULT_STAGES
    else:
        try:
            selected = tuple(stages)
        except TypeError as exc:
            raise ReviewInputError("prompt digest stages must be iterable") from exc
    if not selected:
        raise ReviewInputError("prompt digest requires at least one stage")
    if any(not isinstance(stage, Stage) for stage in selected):
        raise ReviewInputError("prompt digest stages must contain only Stage values")
    return _json_digest(_stage_digest_payload(selected))


def load_evaluation_report(path: Path) -> dict[str, Any]:
    """Load one bounded evaluation-report document from disk."""

    value = _load_json(path, maximum=MAX_JSON_FILE_BYTES, label="evaluation report")
    validate_public_document(value, "evaluation-report")
    return value


def _report_promotion_fields(report: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        raise ReviewInputError("evaluation report must be a JSON object")
    try:
        payload = dict(report)
    except (TypeError, ValueError) as exc:
        raise ReviewInputError("evaluation report must be a JSON object") from exc
    validate_public_document(payload, "evaluation-report")
    try:
        run = payload["run"]
        corpus = payload["corpus"]
    except KeyError as exc:
        raise ReviewInputError("evaluation report is incomplete") from exc
    if not isinstance(run, Mapping) or not isinstance(corpus, Mapping):
        raise ReviewInputError("evaluation report run and corpus must be objects")
    try:
        model = run.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ReviewInputError("evaluation report model is required for promotion")
        provider = run.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ReviewInputError(
                "evaluation report provider is required for promotion"
            )
        prompt = run.get("prompt_digest")
        if prompt is None:
            prompt = run.get("package_stage_digest")
        return {
            "engine_digest": _required_sha256(
                run.get("engine_digest"), label="report engine_digest"
            ),
            "prompt_digest": _required_sha256(prompt, label="report prompt_digest"),
            "configuration_digest": _required_sha256(
                run.get("configuration_digest"), label="report configuration_digest"
            ),
            "corpus_digest": _required_sha256(
                corpus.get("sha256"), label="report corpus digest"
            ),
            "provider": provider.strip(),
            "model": model.strip(),
            "mode": run.get("mode"),
            "passed": payload.get("passed") is True,
            "threshold_failures": list(payload.get("threshold_failures") or []),
            "provider_version": run.get("provider_version"),
            "invocation_id": _required_invocation_id(run.get("invocation_id")),
        }
    except (KeyError, IndexError, AttributeError, TypeError) as exc:
        raise ReviewInputError("evaluation report is incomplete") from exc


def _classify_promotion_evidence(extracted: Sequence[Mapping[str, Any]]) -> str:
    if not extracted:
        raise ReviewInputError("promotion requires at least one evaluation report")
    identities = {
        (
            item["engine_digest"],
            item["prompt_digest"],
            item["configuration_digest"],
            item["corpus_digest"],
            item["provider"],
            item["model"],
        )
        for item in extracted
    }
    if len(identities) != 1:
        raise ReviewInputError(
            "promotion reports must share engine, prompt, configuration, "
            "corpus, and provider identity"
        )
    invocation_ids = [str(item["invocation_id"]) for item in extracted]
    if len(set(invocation_ids)) != len(invocation_ids):
        raise ReviewInputError("promotion reports must be independent")
    fixture = any(_is_fixture_alias(str(item["provider"])) for item in extracted)
    live = all(item["mode"] == "live" for item in extracted)
    passed = all(
        item["passed"] is True and not item["threshold_failures"] for item in extracted
    )
    qualifies = len(extracted) >= 3 and live and not fixture and passed
    if qualifies:
        return "supported"
    live_non_fixture = [
        item
        for item in extracted
        if item["mode"] == "live" and not _is_fixture_alias(str(item["provider"]))
    ]
    if live_non_fixture and not all(
        item["passed"] is True and not item["threshold_failures"]
        for item in live_non_fixture
    ):
        return "unsupported"
    return "insufficient"


def promotion_record_from_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    observed_revision: str,
    reproducibility: Mapping[str, Any],
    evaluated_at: str,
    rollback_decision: str = "revert-to-baseline",
    status: str | None = None,
) -> PromotionRecord:
    """Build a promotion record from independent evaluation reports.

    ``status=supported`` requires at least three independent live runs that
    passed quality thresholds. Fixture-provider reports cannot mint supported
    evidence.
    """

    try:
        documents = tuple(reports)
    except TypeError as exc:
        raise ReviewInputError("promotion reports must be iterable") from exc
    extracted = [_report_promotion_fields(report) for report in documents]
    inferred = _classify_promotion_evidence(extracted)
    if status is None:
        selected_status = inferred
    elif status == inferred:
        selected_status = status
    elif status == "supported":
        raise ReviewInputError(
            "supported promotion requires at least three independent live "
            "evaluation reports that passed quality thresholds"
        )
    else:
        raise ReviewInputError(
            "promotion status does not match the evaluation evidence"
        )
    first = extracted[0]
    provider_versions = {
        item["provider_version"]
        for item in extracted
        if isinstance(item["provider_version"], str)
        and item["provider_version"].strip()
    }
    if len(provider_versions) > 1:
        raise ReviewInputError(
            "promotion reports must share one observed provider revision"
        )
    if (
        isinstance(observed_revision, str)
        and observed_revision.strip()
        and provider_versions
        and observed_revision.strip() not in provider_versions
    ):
        raise ReviewInputError(
            "promotion observed_revision does not match the report provider version"
        )
    return PromotionRecord(
        engine_digest=first["engine_digest"],
        prompt_digest=first["prompt_digest"],
        configuration_digest=first["configuration_digest"],
        corpus_digest=first["corpus_digest"],
        provider=first["provider"],
        model=first["model"],
        observed_revision=observed_revision,
        run_count=len(extracted),
        evaluated_at=evaluated_at,
        reproducibility=reproducibility,
        status=selected_status,
        rollback_decision=rollback_decision,
    )


def validate_promotion_against_report(
    record: PromotionRecord,
    report: Mapping[str, Any],
) -> None:
    """Bind one promotion record to one evaluation report, failing closed."""

    if not isinstance(record, PromotionRecord):
        raise ReviewInputError("promotion record is required")
    fields = _report_promotion_fields(report)
    for name in (
        "engine_digest",
        "prompt_digest",
        "configuration_digest",
        "corpus_digest",
        "provider",
        "model",
    ):
        if getattr(record, name) != fields[name]:
            raise ReviewInputError(
                f"promotion record {name} does not match the evaluation report"
            )
    provider_version = fields["provider_version"]
    if (
        isinstance(provider_version, str)
        and provider_version.strip()
        and provider_version.strip() != record.observed_revision
    ):
        raise ReviewInputError(
            "promotion observed_revision does not match the report provider version"
        )
    if record.status == "supported":
        if not fields["passed"] or fields["threshold_failures"]:
            raise ReviewInputError("evaluation report did not pass quality thresholds")
        if fields["mode"] != "live":
            raise ReviewInputError(
                "supported promotion requires live evaluation reports"
            )
        if _is_fixture_alias(record.provider) or _is_fixture_alias(fields["provider"]):
            raise ReviewInputError("fixture-only evidence cannot support promotion")
    elif not fields["passed"] or fields["threshold_failures"]:
        raise ReviewInputError("evaluation report did not pass quality thresholds")


def require_supported_promotion(
    record: PromotionRecord | Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
) -> PromotionRecord:
    """Fail-closed gate for model, prompt, or routing-config promotion.

    Release and documentation workflows must call this before promoting a
    real provider model, stage prompt, generation setting, or routing
    configuration. Ordinary CI fixture evaluation must not call it to mint
    approval. Fixture reports cannot produce a ``supported`` record.
    """

    parsed = (
        record
        if isinstance(record, PromotionRecord)
        else validate_promotion_record(record)
    )
    if parsed.status != "supported":
        raise ReviewInputError("promotion requires a supported promotion record")
    try:
        documents = tuple(reports)
    except TypeError as exc:
        raise ReviewInputError("promotion reports must be iterable") from exc
    if len(documents) < 3:
        raise ReviewInputError("promotion requires at least three evaluation reports")
    minted = promotion_record_from_reports(
        documents,
        observed_revision=parsed.observed_revision,
        reproducibility=parsed.reproducibility,
        evaluated_at=parsed.evaluated_at,
        rollback_decision=parsed.rollback_decision,
        status="supported",
    )
    for name in (
        "engine_digest",
        "prompt_digest",
        "configuration_digest",
        "corpus_digest",
        "provider",
        "model",
        "observed_revision",
        "run_count",
        "status",
        "rollback_decision",
    ):
        if getattr(minted, name) != getattr(parsed, name):
            raise ReviewInputError(
                f"promotion record {name} does not match live evaluation evidence"
            )
    for report in documents:
        validate_promotion_against_report(parsed, report)
    return parsed


def _profile_report_endpoint_scopes(profile: ProviderProfile) -> frozenset[str]:
    if profile.endpoint_scope == "local":
        return frozenset({"loopback"})
    return frozenset({"remote"})


def _validate_supplied_live_reports(
    profile: ProviderProfile,
    reports: Sequence[Mapping[str, Any]],
) -> None:
    """Reject live reports that do not match the profile promotion contract."""

    expected = _profile_report_endpoint_scopes(profile)
    for report in reports:
        fields = _report_promotion_fields(report)
        if fields["mode"] != "live":
            continue
        if fields["provider"] != profile.provider:
            raise ReviewInputError(
                "promotion live report provider does not match profile"
            )
        if fields["model"] not in profile.allowed_models():
            raise ReviewInputError("promotion live report model does not match profile")
        if _report_endpoint_scope(report) not in expected:
            raise ReviewInputError(
                "promotion record endpoint scope does not match profile"
            )


def _profile_live_reports(
    profile: ProviderProfile,
    reports: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return live reports that satisfy the profile promotion contract."""

    expected = _profile_report_endpoint_scopes(profile)
    live_reports: list[Mapping[str, Any]] = []
    for report in reports:
        fields = _report_promotion_fields(report)
        if fields["mode"] != "live":
            continue
        if fields["provider"] != profile.provider:
            continue
        if fields["model"] not in profile.allowed_models():
            continue
        if _report_endpoint_scope(report) not in expected:
            continue
        live_reports.append(report)
    return live_reports


def _validate_profile_stage_model_evidence(
    profile: ProviderProfile,
    record: PromotionRecord,
    reports: Sequence[Mapping[str, Any]],
) -> None:
    """Require live reports for each declared per-stage model when present."""

    if not profile.stage_models:
        return
    required_models = frozenset(model for _, model in profile.stage_models)
    live_models = {
        _report_promotion_fields(report)["model"]
        for report in _profile_live_reports(profile, reports)
    }
    if profile.endpoint_scope == "remote" and not live_models:
        raise ReviewInputError(
            "remote profile promotion requires live evaluation reports"
        )
    missing = required_models - live_models
    if missing:
        raise ReviewInputError(
            "profile promotion requires live reports for each declared stage model"
        )


def _report_endpoint_scope(report: Mapping[str, Any]) -> str:
    run = report.get("run")
    if not isinstance(run, Mapping):
        raise ReviewInputError("evaluation report is incomplete")
    scope = run.get("endpoint_scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ReviewInputError("evaluation report endpoint_scope is required")
    return scope.strip()


def validate_profile_promotion(
    profile_name: str,
    record: PromotionRecord,
    reports: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Reject fixture-only or mismatched evidence for a named provider profile.

    Remote profiles require at least one live report whose ``endpoint_scope``
    matches the profile's declared endpoint policy. Local profiles validate live
    report scopes when ``reports`` are supplied. Callers promoting with live
    evidence should pass reports or use ``require_supported_promotion``.
    """

    profile = get_provider_profile(profile_name)
    if profile.qualification_status == "unqualified":
        raise ReviewInputError("unqualified provider profile cannot support promotion")
    if _is_fixture_alias(record.provider):
        raise ReviewInputError("fixture-only evidence cannot support promotion")
    if record.status != "supported":
        raise ReviewInputError("profile promotion requires supported evidence")
    if record.provider != profile.provider:
        raise ReviewInputError(
            "promotion record provider "
            f"{record.provider!r} does not match profile "
            f"'{profile.name}' (requires {profile.provider!r})"
        )
    if record.model not in profile.allowed_models():
        raise ReviewInputError("promotion record model does not match profile")
    if reports:
        _validate_supplied_live_reports(profile, reports)
    _validate_profile_stage_model_evidence(profile, record, reports)
    if profile.endpoint_scope == "remote":
        live_reports = _profile_live_reports(profile, reports)
        if not live_reports:
            raise ReviewInputError(
                "remote profile promotion requires live evaluation reports"
            )
        for report in live_reports:
            fields = _report_promotion_fields(report)
            if fields["provider"] != record.provider:
                raise ReviewInputError(
                    "promotion live report provider does not match record"
                )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _json_digest(value: object) -> str:
    return _sha256_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def _normalize_terms(value: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"[^a-z0-9]+", value.lower()) if token)


def _sanitized_error(kind: str, relative: Path) -> ReviewInputError:
    return ReviewInputError(f"corpus {kind} failed for {relative.as_posix()}")


def _load_json(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    text = read_bounded_utf8(path, maximum=maximum, label=label)
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReviewInputError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReviewInputError(f"{label} must be a JSON object")
    return value


def _canonical_path(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{label} must be a string")
    return validate_repository_path(value, label=label)


def _file_size(path: Path) -> int:
    return path.stat().st_size


def _relative_path(root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReviewInputError("corpus file escaped the corpus root") from exc


def _iter_corpus_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ReviewInputError("corpus root is not a directory")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReviewInputError("corpus contains a symlink")
        if path.is_file():
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise ReviewInputError("corpus file escaped the corpus root") from exc
            files.append(path)
    if len(files) > MAX_CORPUS_FILES:
        raise ReviewInputError("corpus contains too many files")
    total = sum(_file_size(path) for path in files)
    if total > MAX_CORPUS_TOTAL_BYTES:
        raise ReviewInputError("corpus exceeds the total size limit")
    return files


def _scan_text(value: str, *, relative: Path) -> None:
    for pattern in _PRIVACY_PATTERNS:
        if pattern.search(value):
            raise _sanitized_error("privacy", relative)
    for marker in _SECRET_MARKERS:
        if marker in value:
            raise _sanitized_error("privacy", relative)


@dataclass(frozen=True)
class Corpus:
    """Validated evaluation corpus metadata and asset paths."""

    root: Path
    document: dict[str, Any]
    digest: str
    files: tuple[Path, ...]

    @property
    def corpus_id(self) -> str:
        return str(self.document["corpus_id"])

    @property
    def version(self) -> str:
        return str(self.document["version"])

    def asset_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ReviewInputError("corpus asset escaped the corpus root") from exc
        if path.is_symlink() or not path.is_file():
            raise ReviewInputError("corpus asset is not a regular file")
        return path

    def read_asset(self, relative: str, *, maximum: int, label: str) -> str:
        return read_bounded_utf8(
            self.asset_path(relative),
            maximum=maximum,
            label=label,
        )


def load_corpus(corpus_path: Path) -> Corpus:
    root = corpus_path.resolve().parent
    value = _load_json(
        corpus_path,
        maximum=MAX_CORPUS_JSON_BYTES,
        label="corpus",
    )
    validate_public_document(value, "evaluation-corpus")
    sample_repository = value["sample_repository"]
    for root_key in ("base_root", "head_root"):
        root_relative = _canonical_path(
            sample_repository[root_key],
            label=f"corpus sample repository {root_key}",
        )
        sample_root = (root / root_relative).resolve()
        try:
            sample_root.relative_to(root.resolve())
        except ValueError as exc:
            raise ReviewInputError(
                f"corpus sample repository {root_key} escaped the corpus root"
            ) from exc
        if sample_root.is_symlink() or not sample_root.is_dir():
            raise ReviewInputError(
                f"corpus sample repository {root_key} is not a regular directory"
            )
    files = _iter_corpus_files(root)
    declared: set[str] = set()
    for entry in value.get("files", []):
        relative = _canonical_path(entry["path"], label="corpus inventory path")
        declared.add(relative)
        actual = (root / relative).resolve()
        try:
            actual.relative_to(root.resolve())
        except ValueError as exc:
            raise ReviewInputError(
                "corpus inventory path escaped the corpus root"
            ) from exc
        if actual.is_symlink() or not actual.is_file():
            raise ReviewInputError("corpus inventory path is not a regular file")
        expected_sha = entry["sha256"]
        if relative == corpus_path.name:
            manifest = json.loads(corpus_path.read_text(encoding="utf-8"))
            for manifest_entry in manifest.get("files", []):
                if manifest_entry.get("path") == relative:
                    manifest_entry["sha256"] = ""
            actual_sha = _json_digest(manifest)
        else:
            actual_sha = _sha256_bytes(actual.read_bytes())
        if expected_sha != actual_sha:
            raise ReviewInputError("corpus inventory sha256 does not match the file")
    actual_relative = {_relative_path(root, path).as_posix() for path in files}
    if declared != actual_relative:
        raise ReviewInputError("corpus inventory does not match the declared file tree")

    total_bytes = sum(_file_size(path) for path in files)
    if total_bytes > MAX_CORPUS_TOTAL_BYTES:
        raise ReviewInputError("corpus exceeds the total size limit")

    for path in files:
        relative_path = _relative_path(root, path)
        maximum = MAX_JSON_FILE_BYTES if path.suffix == ".json" else MAX_TEXT_FILE_BYTES
        text = read_bounded_utf8(path, maximum=maximum, label="corpus file")
        _scan_text(text, relative=relative_path)

    for case in value.get("cases", []):
        for key in ("diff_path", "response_path", "expected_result_path"):
            relative = case.get(key)
            if relative is not None:
                asset = _canonical_path(relative, label="corpus case path")
                if asset not in declared:
                    raise ReviewInputError("corpus case asset is not declared")

    return Corpus(
        root=root,
        document=value,
        digest=_sha256_bytes(corpus_path.read_bytes()),
        files=tuple(files),
    )


def package_stage_digest() -> str:
    return prompt_digest()


def _packaged_category_digest() -> str:
    from .service import DEFAULT_CATEGORY_CATALOG

    return _json_digest(
        [
            {
                "id": category.id,
                "title": category.title,
                "focus": list(category.focus),
                "applies_to": list(category.applies_to),
                "learning_categories": list(category.learning_categories),
                "include_uncategorized_learnings": category.include_uncategorized_learnings,
            }
            for category in DEFAULT_CATEGORY_CATALOG.categories
        ]
    )


def configuration_digest(
    corpus: Corpus,
    mode: str,
    provider: str,
    model: str | None,
    *,
    openrouter_policy: Mapping[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "corpus_id": corpus.corpus_id,
        "corpus_version": corpus.version,
        "mode": mode,
        "provider": provider,
        "model": model,
        "review_configuration_id": corpus.document["review_configuration"]["id"],
        "category_digest": _packaged_category_digest(),
        "stage_digest": package_stage_digest(),
    }
    if openrouter_policy is not None:
        payload["openrouter_policy"] = dict(openrouter_policy)
    return _json_digest(payload)


class MeasuredProvider:
    """Provider wrapper retaining only non-secret call metrics."""

    name: str
    model: str | None

    def __init__(self, provider: ReviewProvider | None) -> None:
        self._provider = provider
        self.name = provider.name if provider is not None else "aggregate"
        self.model = provider.model if provider is not None else None
        self.calls = 0
        self.elapsed_ms: list[int] = []
        self.prompt_bytes = 0
        self.response_bytes = 0

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        if self._provider is None:
            raise RuntimeError("aggregate metrics provider cannot complete requests")
        started = time.perf_counter()
        response = self._provider.complete(request)
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        self.calls += 1
        self.elapsed_ms.append(elapsed_ms)
        self.prompt_bytes += len(request.prompt.encode("utf-8"))
        self.response_bytes += len(response.text.encode("utf-8"))
        return response

    def snapshot(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "elapsed_ms": self.elapsed_ms,
            "prompt_bytes": self.prompt_bytes,
            "response_bytes": self.response_bytes,
        }


def endpoint_scope(base_url: str | None) -> str:
    if not base_url:
        return "none"
    parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(base_url)
    host = parsed.hostname or ""
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return "loopback"
    return "remote"


@dataclass(frozen=True)
class ExpectedFinding:
    path: str
    line: int | None
    category: str | None
    body_terms: tuple[str, ...]
    side: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExpectedFinding":
        line = value.get("line")
        return cls(
            path=str(value["path"]),
            line=int(line) if line is not None else None,
            category=value.get("category"),
            body_terms=tuple(
                normalized
                for term in value["body_terms"]
                for normalized in _normalize_terms(str(term))
            ),
            side=value.get("side") if isinstance(value.get("side"), str) else None,
        )


def _without_derived_coverage(document: dict[str, object]) -> dict[str, object]:
    """Drop derived lifecycle metadata and a default coverage mode.

    ``finding_lifecycles`` is an additive v1 field computed from finding
    identity, and ``full`` is the default coverage mode, so a legacy fixture
    that omits both still describes the same deterministic review.
    """

    normalized = dict(document)
    normalized.pop("finding_lifecycles", None)
    if normalized.get("coverage_mode") == "full":
        normalized.pop("coverage_mode")
    return normalized


def _one_to_one_matches(
    comments: Sequence[ReviewComment],
    expected: Sequence[ExpectedFinding],
) -> tuple[int, int, int]:
    unmatched = list(expected)
    actual_matches = 0
    false_positives = 0
    for comment in comments:
        found = None
        for index, candidate in enumerate(unmatched):
            terms = _normalize_terms(comment.body)
            line_matches = (
                candidate.line is None
                or comment.line is None
                or comment.line == candidate.line
            )
            side_matches = candidate.side is None or comment.side == candidate.side
            if (
                comment.path == candidate.path
                and line_matches
                and side_matches
                and (
                    candidate.category is None or comment.category == candidate.category
                )
                and set(candidate.body_terms).issubset(terms)
            ):
                found = index
                break
        if found is None:
            false_positives += 1
        else:
            unmatched.pop(found)
            actual_matches += 1
    return actual_matches, len(expected) - len(unmatched), false_positives


def compare_chunked_against_baseline(
    *,
    baseline: ReviewResult,
    chunked: ReviewResult,
    baseline_usage: Mapping[str, int],
    chunked_usage: Mapping[str, int],
) -> dict[str, object]:
    """Compare a chunked review with a complete small-change baseline.

    Recall and precision use the baseline comments as the expected set, so a
    defect split across chunk boundaries is a miss unless the chunked result
    still reports it. Usage is compared as raw call and byte counts.
    """

    expected = tuple(
        ExpectedFinding(
            path=comment.path,
            line=comment.line,
            category=comment.category,
            body_terms=tuple(_normalize_terms(comment.body)),
            side=comment.side,
        )
        for comment in baseline.comments
    )
    matches, _, false_positives = _one_to_one_matches(chunked.comments, expected)
    recall = _percent(matches, len(expected))
    precision = _percent(matches, matches + false_positives)
    return {
        "recall": recall,
        "precision": precision,
        "baseline_calls": int(baseline_usage.get("calls", 0)),
        "chunked_calls": int(chunked_usage.get("calls", 0)),
        "baseline_prompt_bytes": int(baseline_usage.get("prompt_bytes", 0)),
        "chunked_prompt_bytes": int(chunked_usage.get("prompt_bytes", 0)),
        "cross_boundary_misses": len(expected) - matches,
    }


def _percent(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator


def _p95(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return float(ordered[index])


def run_case(
    corpus: Corpus,
    case: dict[str, Any],
    service: ReviewService,
    measured: MeasuredProvider,
    *,
    learnings: Sequence[LearningEntry] = (),
) -> dict[str, Any]:
    case_id = str(case["id"])
    kind = str(case["kind"])
    category = str(case["category"])
    expected_category = str(case["expected_error_category"])
    started = time.perf_counter()
    status = "failed"
    expected_matches = 0
    actual_matches = 0
    false_positives = 0
    location_valid = True
    category_valid = True
    calls_before = measured.calls
    prompt_before = measured.prompt_bytes
    response_before = measured.response_bytes
    request = ReviewRequest(
        diff=corpus.read_asset(
            str(case["diff_path"]),
            maximum=1048576,
            label="case diff",
        ),
        repository="synthetic/sample",
        title=str(case["title"]),
        learnings=tuple(learnings),
        propose_learnings=False,
        limits=DEFAULT_REVIEW_LIMITS,
    )
    result = None
    error_category = "none"
    try:
        result = service.review(request)
    except ReviewSenseiError as exc:
        error_category = exc.error_category
    elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
    case_calls = measured.calls - calls_before
    case_prompt_bytes = measured.prompt_bytes - prompt_before
    case_response_bytes = measured.response_bytes - response_before

    if kind == "contract-rejection":
        status = (
            "rejected-expected"
            if error_category == expected_category
            else "rejected-unexpected"
        )
        return {
            "id": case_id,
            "kind": kind,
            "category": category,
            "status": status,
            "expected_matches": 0,
            "actual_matches": 0,
            "false_positives": 0,
            "location_valid": True,
            "category_valid": True,
            "elapsed_ms": elapsed_ms,
            "provider_calls": case_calls,
            "prompt_bytes": case_prompt_bytes,
            "response_bytes": case_response_bytes,
        }

    if result is None:
        status = "rejected-unexpected"
        return {
            "id": case_id,
            "kind": kind,
            "category": category,
            "status": status,
            "expected_matches": 0,
            "actual_matches": 0,
            "false_positives": 0,
            "location_valid": False,
            "category_valid": False,
            "elapsed_ms": elapsed_ms,
            "provider_calls": case_calls,
            "prompt_bytes": case_prompt_bytes,
            "response_bytes": case_response_bytes,
        }

    expected = [
        ExpectedFinding.from_dict(item) for item in case.get("expected_findings", [])
    ]
    actual_matches, expected_matches, false_positives = _one_to_one_matches(
        result.comments,
        expected,
    )
    if case.get("acceptable_no_finding", False) and result.comments:
        status = "failed"
    elif not case.get("expected_result_path"):
        status = "passed"
    else:
        expected_document = json.loads(
            corpus.read_asset(
                str(case["expected_result_path"]),
                maximum=MAX_JSON_FILE_BYTES,
                label="expected result",
            )
        )
        # Pre-status fixtures remain valid as complete deterministic outputs.
        # Evaluation accepts both the legacy fixture shape and the additive
        # explicit ``complete`` status, while publication keeps its
        # fail-closed parser strict for artifacts missing a status.
        actual_document = result.to_dict()
        if isinstance(expected_document, dict):
            if expected_document.get("review_status") == "complete":
                expected_document = dict(expected_document)
                expected_document.pop("review_status")
            if actual_document.get("review_status") == "complete":
                actual_document = dict(actual_document)
                actual_document.pop("review_status")
            # Lifecycle records are derived from the finding-identity algorithm
            # rather than from provider output, so pinning them here would make
            # the corpus assert an implementation detail and break on any
            # identity change. The corpus asserts the deterministic review
            # content; coverage defaults are normalized like review_status.
            expected_document = _without_derived_coverage(expected_document)
            actual_document = _without_derived_coverage(actual_document)
            if "coverage" not in expected_document:
                actual_document = dict(actual_document)
                actual_document.pop("coverage", None)
            if "evidence_policy" not in expected_document:
                status = "failed"
            elif expected_document.get("evidence_policy") != actual_document.get(
                "evidence_policy"
            ):
                status = "failed"
            else:
                status = "passed" if expected_document == actual_document else "failed"
        else:
            status = "passed" if expected_document == actual_document else "failed"
    location_valid = all(
        comment.path and (comment.line is None or comment.line > 0)
        for comment in result.comments
    )
    category_valid = all(
        comment.category
        in {"correctness", "security", "architecture", "maintainability", "tests"}
        for comment in result.comments
        if comment.category is not None
    )
    return {
        "id": case_id,
        "kind": kind,
        "category": category,
        "status": status,
        "expected_matches": expected_matches,
        "actual_matches": actual_matches,
        "false_positives": false_positives,
        "location_valid": location_valid,
        "category_valid": category_valid,
        "elapsed_ms": elapsed_ms,
        "provider_calls": case_calls,
        "prompt_bytes": case_prompt_bytes,
        "response_bytes": case_response_bytes,
    }


def evaluate_fixture(
    corpus: Corpus,
    *,
    learnings: Sequence[LearningEntry] = (),
) -> dict[str, Any]:
    from .providers.fixture import FixtureProvider

    cases: list[dict[str, Any]] = []
    measured = MeasuredProvider(None)
    # Routed through the single selection seam rather than re-filtering, so the
    # lifecycle rule is not duplicated in this module.
    selected = LearningStore(learnings).selectable_entries
    for case in corpus.document["cases"]:
        response_path = corpus.asset_path(str(case["response_path"]))
        fixture = FixtureProvider(response_path, model="fixture-v1")
        case_measured = MeasuredProvider(fixture)
        service = ReviewService(case_measured)
        cases.append(
            run_case(
                corpus,
                case,
                service,
                case_measured,
                learnings=selected,
            )
        )
        measured.calls += case_measured.calls
        measured.elapsed_ms.extend(case_measured.elapsed_ms)
        measured.prompt_bytes += case_measured.prompt_bytes
        measured.response_bytes += case_measured.response_bytes
    return build_report(
        corpus,
        cases,
        measured,
        mode="fixture",
        provider="fixture",
        provider_version=None,
        model="fixture-v1",
        endpoint_scope="none",
    )


COMPARISON_DELTA_KEYS = (
    "actionable_precision",
    "false_positive_rate",
    "expected_finding_recall",
    "location_validity",
    "category_coverage",
)


def compare_learning_effect(
    corpus: Corpus,
    learnings: Sequence[LearningEntry] | LearningStore = (),
) -> dict[str, Any]:
    """Compare the same fixture cases with and without selected learnings.

    Selection reuses ``LearningStore.selectable_entries`` so the comparison
    reports on the same entries a review would use. Deltas are estimates, not
    causal proof. Sparse production feedback must not be treated as a promotion
    or effectiveness claim.
    """

    store = (
        learnings if isinstance(learnings, LearningStore) else LearningStore(learnings)
    )
    selected = store.selectable_entries
    with_learnings = evaluate_fixture(corpus, learnings=selected)
    without_learnings = evaluate_fixture(corpus, learnings=())
    with_quality = dict(with_learnings["quality"])
    without_quality = dict(without_learnings["quality"])
    # Allowlisted so a future numeric field in the fixture report cannot widen
    # the comparison's public contract or be reported as a quality estimate.
    # A renamed or retyped quality key fails loudly here rather than silently
    # narrowing the reported deltas.
    for key in COMPARISON_DELTA_KEYS:
        if not isinstance(with_quality.get(key), (int, float)) or not isinstance(
            without_quality.get(key), (int, float)
        ):
            raise ReviewInputError(
                f"fixture quality metric {key} is missing or not numeric"
            )
    delta = {
        key: with_quality[key] - without_quality[key] for key in COMPARISON_DELTA_KEYS
    }
    return {
        "schema_version": "1.0",
        "causal_claim": False,
        "disclaimer": (
            "Precision, recall, and false-positive deltas are estimates from the "
            "same synthetic cases with and without selected approved learnings. "
            "Sparse production feedback is not causal proof."
        ),
        "selected_learning_ids": [entry.id for entry in selected],
        "learning_digest": learning_digest(selected),
        "with_learnings": with_quality,
        "without_learnings": without_quality,
        "delta": delta,
        "with_learnings_passed": with_learnings["passed"],
        "without_learnings_passed": without_learnings["passed"],
    }


def evaluate_live(
    corpus: Corpus,
    provider: ReviewProvider,
    *,
    provider_version: str,
    model: str | None,
    endpoint_scope: str,
) -> dict[str, Any]:
    measured = MeasuredProvider(provider)
    cases: list[dict[str, Any]] = []
    for case in corpus.document["cases"]:
        if case["kind"] == "contract-rejection":
            cases.append(
                {
                    "id": str(case["id"]),
                    "kind": "contract-rejection",
                    "category": str(case["category"]),
                    "status": "skipped",
                    "expected_matches": 0,
                    "actual_matches": 0,
                    "false_positives": 0,
                    "location_valid": True,
                    "category_valid": True,
                    "elapsed_ms": 0,
                    "provider_calls": 0,
                    "prompt_bytes": 0,
                    "response_bytes": 0,
                }
            )
            continue
        service = ReviewService(measured)
        cases.append(run_case(corpus, case, service, measured))
    openrouter_policy = None
    routing_policy = getattr(provider, "routing_policy", None)
    if routing_policy is not None and hasattr(routing_policy, "identity_fields"):
        openrouter_policy = routing_policy.identity_fields()
    return build_report(
        corpus,
        cases,
        measured,
        mode="live",
        provider=provider.name,
        provider_version=provider_version,
        model=model,
        endpoint_scope=endpoint_scope,
        openrouter_policy=openrouter_policy,
    )


def build_report(
    corpus: Corpus,
    cases: Sequence[dict[str, Any]],
    measured: MeasuredProvider,
    *,
    mode: str,
    provider: str,
    provider_version: str | None,
    model: str | None,
    endpoint_scope: str,
    openrouter_policy: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    quality_cases = [case for case in cases if case["kind"] == "quality"]
    expected = sum(case["expected_matches"] for case in quality_cases)
    actual = sum(case["actual_matches"] for case in quality_cases)
    false_positives = sum(case["false_positives"] for case in quality_cases)
    precision = _percent(actual, actual + false_positives)
    recall = _percent(actual, expected)
    fpr = false_positives / max(1, len(quality_cases))
    location_valid = _percent(
        sum(case["location_valid"] for case in quality_cases),
        len(quality_cases),
    )
    categories = {
        str(case["category"])
        for case in quality_cases
        if case["category"] and case["status"] == "passed"
    }
    required_categories = {"correctness", "security", "architecture", "tests"}
    category_coverage = _percent(
        len(categories & required_categories), len(required_categories)
    )

    exact = sum(
        1
        for case in cases
        if case["kind"] == "quality"
        and case["status"] in {"passed", "rejected-expected"}
    )
    rejection = sum(
        1
        for case in cases
        if case["kind"] == "contract-rejection"
        and case["status"] == "rejected-expected"
    )
    contract_cases = [case for case in cases if case["kind"] == "contract-rejection"]
    exact_rate = _percent(exact, len(quality_cases))
    rejection_rate = _percent(rejection, len(contract_cases))

    threshold_failures: list[str] = []
    report = {
        "schema_version": "1.0",
        "corpus": {
            "id": corpus.corpus_id,
            "version": corpus.version,
            "sha256": corpus.digest,
        },
        "run": {
            "mode": mode,
            "review_sensei_version": _installed_package_version(),
            "provider": provider,
            "provider_version": provider_version,
            "model": model,
            "endpoint_scope": endpoint_scope,
            "invocation_id": secrets.token_hex(16),
            "engine_digest": engine_digest(),
            "prompt_digest": prompt_digest(),
            "package_stage_digest": package_stage_digest(),
            "configuration_digest": configuration_digest(
                corpus,
                mode,
                provider,
                model,
                openrouter_policy=openrouter_policy,
            ),
        },
        "configuration": {
            "review_configuration_id": corpus.document["review_configuration"]["id"],
        },
        "privacy": {
            "status": "clean",
            "scanned_inventory_count": len(corpus.files),
        },
        "cases": [dict(case) for case in cases],
        "metrics": {
            "provider_calls": measured.calls,
            "prompt_bytes": measured.prompt_bytes,
            "response_bytes": measured.response_bytes,
            "token_proxy_4_bytes": math.ceil(
                (measured.prompt_bytes + measured.response_bytes) / 4
            ),
            "elapsed_total_ms": sum(measured.elapsed_ms),
            "elapsed_mean_ms": (sum(measured.elapsed_ms) / len(measured.elapsed_ms))
            if measured.elapsed_ms
            else 0.0,
            "elapsed_p95_ms": _p95(measured.elapsed_ms),
        },
        "deterministic": {
            "exact_fixture_result_rate": exact_rate,
            "expected_contract_rejection_rate": rejection_rate,
        },
        "quality": {
            "actionable_precision": precision,
            "false_positive_rate": fpr,
            "expected_finding_recall": recall,
            "location_validity": location_valid,
            "category_coverage": category_coverage,
        },
        "threshold_failures": threshold_failures,
        "passed": True,
    }
    thresholds = corpus.document["deterministic_thresholds"]
    if exact_rate < float(thresholds["exact_fixture_result_rate"]):
        threshold_failures.append("exact_fixture_result_rate")
    if rejection_rate < float(thresholds["expected_contract_rejection_rate"]):
        threshold_failures.append("expected_contract_rejection_rate")
    quality = corpus.document["quality_thresholds"]
    if precision < float(quality["actionable_precision_minimum"]):
        threshold_failures.append("actionable_precision_minimum")
    if fpr > float(quality["false_positive_rate_maximum"]):
        threshold_failures.append("false_positive_rate_maximum")
    if recall < float(quality["expected_finding_recall_minimum"]):
        threshold_failures.append("expected_finding_recall_minimum")
    if location_valid < float(quality["location_validity_minimum"]):
        threshold_failures.append("location_validity_minimum")
    if category_coverage < float(quality["category_coverage_minimum"]):
        threshold_failures.append("category_coverage_minimum")
    report["passed"] = not threshold_failures
    validate_public_document(report, "evaluation-report")
    return report
