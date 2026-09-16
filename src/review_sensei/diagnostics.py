"""Read-only installation diagnostics and review-plan previews.

The diagnostic helpers intentionally share the same packaged configuration
loaders as the normal runner, but never construct a provider or call GitHub.
Unknown checks are represented explicitly so an offline invocation cannot
mistake an unavailable network prerequisite for a passing check.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .diff import analyze_diff
from .errors import ReviewInputError, ReviewSenseiError
from .service import DEFAULT_CATEGORY_CATALOG, DEFAULT_STAGES
from .stages import (
    MAX_STAGE_FILES,
    category_catalog_for_configured_stages,
    load_review_categories_from_dir,
    load_stages_from_dir,
)
from .validation import DEFAULT_REVIEW_LIMITS

DOCTOR_OK = 0
DOCTOR_ACTION_REQUIRED = 2
DOCTOR_UNKNOWN = 3
SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _bounded_stage_names(values: Iterable[str]) -> tuple[str, ...]:
    """Read stage names without materializing an unbounded iterable."""

    if isinstance(values, (str, bytes)):
        raise ReviewInputError("stages must be an iterable of stage names")
    try:
        iterator = iter(values)
    except (TypeError, AttributeError) as exc:
        raise ReviewInputError("stages must be an iterable of stage names") from exc
    result: list[str] = []
    for index, stage in enumerate(iterator, start=1):
        if index > MAX_STAGE_FILES:
            raise ReviewInputError("stages has too many entries")
        result.append(stage)
    return tuple(result)


def _package_version() -> str | None:
    try:
        return importlib.metadata.version("review-sensei")
    except importlib.metadata.PackageNotFoundError:
        return None


def run_doctor(
    *,
    stages_dir: Path | None = None,
    categories_dir: Path | None = None,
    context_root: Path | None = None,
    include_network: bool = False,
    provider_mode: str | None = None,
) -> dict[str, Any]:
    """Return bounded, side-effect-free installation diagnostics.

    ``include_network`` is intentionally reported as unknown rather than
    probing an endpoint.  Network probes belong to an explicitly authorized
    integration command and must not be part of ordinary diagnostics.

    Custom ``stages_dir`` uses the same catalog selection as review: the
    category catalog stays unset unless ``categories_dir`` is supplied.
    Stages that declare ``category_ids`` therefore require ``--categories-dir``,
    matching the review runner.

    ``provider_mode`` reports operator configuration only.  When omitted, doctor
    reads ``REVIEWSENSEI_PROVIDER_MODE`` as a diagnostic default; this is not a
    review-time trust boundary and does not authorize provider calls.
    """

    checks: list[DiagnosticCheck] = []
    version = _package_version()
    checks.append(
        DiagnosticCheck(
            "package",
            "pass" if version else "action",
            version or "package metadata unavailable",
        )
    )
    asset_root = Path(__file__).parent
    packaged_stages = asset_root / "default_stages"
    packaged_categories = asset_root / "default_categories"
    required_assets = (packaged_stages, packaged_categories)
    missing = [
        path.name for path in required_assets if path.is_symlink() or not path.is_dir()
    ]
    packaged_error: str | None = None
    packaged_category_catalog = None
    if not missing:
        try:
            packaged_category_catalog = load_review_categories_from_dir(
                packaged_categories
            )
            load_stages_from_dir(
                packaged_stages,
                category_catalog=packaged_category_catalog,
            )
        except (OSError, ReviewSenseiError) as exc:
            packaged_error = str(exc)
    checks.append(
        DiagnosticCheck(
            "packaged-assets",
            "action" if missing or packaged_error else "pass",
            (
                "missing packaged assets: " + ", ".join(missing)
                if missing
                else f"packaged configuration failed validation: {packaged_error}"
                if packaged_error
                else "default stages and categories available"
            ),
        )
    )
    mode = (
        (
            provider_mode
            if provider_mode is not None
            else os.getenv("REVIEWSENSEI_PROVIDER_MODE", "local")
        )
        .strip()
        .lower()
    )
    if mode not in {"local", "cloud"}:
        checks.append(
            DiagnosticCheck(
                "provider-mode", "action", "provider mode must be local or cloud"
            )
        )
    else:
        checks.append(
            DiagnosticCheck("provider-mode", "pass", f"{mode} (offline check)")
        )
    configured_category_catalog = None
    configured_categories_error: str | None = None
    if categories_dir is not None:
        if categories_dir.is_symlink() or not categories_dir.is_dir():
            configured_categories_error = "configured directory is unavailable"
        elif not any(
            path.is_file() and not path.is_symlink()
            for path in categories_dir.iterdir()
        ):
            configured_categories_error = "configured directory is empty"
        else:
            try:
                configured_category_catalog = category_catalog_for_configured_stages(
                    categories_dir
                )
            except (OSError, ReviewSenseiError) as exc:
                configured_categories_error = str(exc)

    for label, configured, expected in (
        ("stages", stages_dir, "stages"),
        ("categories", categories_dir, "categories"),
    ):
        if configured is None:
            checks.append(
                DiagnosticCheck(label, "pass", f"packaged default {expected} selected")
            )
        elif label == "categories" and configured_categories_error is not None:
            checks.append(
                DiagnosticCheck(
                    label,
                    "action",
                    (
                        "configured directory is unavailable"
                        if configured_categories_error
                        == "configured directory is unavailable"
                        else (
                            "configured directory is empty"
                            if configured_categories_error
                            == "configured directory is empty"
                            else (
                                "configured directory failed validation: "
                                f"{configured_categories_error}"
                            )
                        )
                    ),
                )
            )
        elif configured.is_symlink() or not configured.is_dir():
            checks.append(
                DiagnosticCheck(label, "action", "configured directory is unavailable")
            )
        elif not any(
            path.is_file() and not path.is_symlink() for path in configured.iterdir()
        ):
            checks.append(
                DiagnosticCheck(label, "action", "configured directory is empty")
            )
        else:
            configuration_error: str | None = None
            try:
                if label == "categories":
                    if configured_categories_error is not None:
                        configuration_error = configured_categories_error
                    else:
                        assert configured_category_catalog is not None
                elif configured_categories_error is not None:
                    configuration_error = (
                        "skipping stages validation because categories "
                        "configuration failed"
                    )
                else:
                    load_stages_from_dir(
                        configured,
                        category_catalog=category_catalog_for_configured_stages(
                            categories_dir
                        ),
                    )
            except (OSError, ReviewSenseiError) as exc:
                configuration_error = str(exc)
            checks.append(
                DiagnosticCheck(
                    label,
                    "action" if configuration_error else "pass",
                    (
                        f"configured directory failed validation: {configuration_error}"
                        if configuration_error
                        else "configured trusted-base directory is readable"
                    ),
                )
            )
    if context_root is None:
        checks.append(
            DiagnosticCheck("context", "pass", "no supplemental context configured")
        )
    elif context_root.is_symlink() or not context_root.is_dir():
        checks.append(
            DiagnosticCheck(
                "context", "action", "configured context root is unavailable"
            )
        )
    else:
        checks.append(DiagnosticCheck("context", "pass", "context root is readable"))
    if include_network:
        checks.append(
            DiagnosticCheck(
                "network", "unknown", "network probes are not run by doctor"
            )
        )
    else:
        checks.append(
            DiagnosticCheck("network", "unknown", "not checked (offline mode)")
        )
    if any(check.status == "action" for check in checks):
        status = "action"
    elif include_network:
        status = "unknown"
    elif any(check.status == "unknown" and check.name != "network" for check in checks):
        status = "unknown"
    else:
        status = "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "version": version,
        "checks": [check.to_dict() for check in checks],
    }


def build_plan(
    *,
    diff: str | None = None,
    repository: str | None = None,
    pull_request: int | None = None,
    title: str | None = None,
    stages: Iterable[str] = (),
    provider_mode: str | None = None,
) -> dict[str, Any]:
    """Build a read-only execution preview without provider or GitHub calls."""

    if repository is not None:
        if not isinstance(repository, str):
            raise ReviewInputError("repository must be a string")
        if not repository.strip():
            raise ReviewInputError("repository must be non-empty")
        if len(repository.encode("utf-8")) > DEFAULT_REVIEW_LIMITS.max_repository_bytes:
            raise ReviewInputError("repository exceeds the configured size limit")
    if pull_request is not None and (
        isinstance(pull_request, bool)
        or not isinstance(pull_request, int)
        or pull_request < 1
    ):
        raise ReviewInputError("pull_request must be a positive integer")
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise ReviewInputError("title must be a non-empty string")
        if len(title.encode("utf-8")) > DEFAULT_REVIEW_LIMITS.max_title_bytes:
            raise ReviewInputError("title exceeds the configured size limit")

    if diff is None:
        diff_summary: dict[str, Any] = {"supplied": False, "status": "unknown"}
    elif not isinstance(diff, str):
        raise ReviewInputError("diff must be a string")
    else:
        # Plan readiness uses the same bounded diff analysis path as review.
        analysis = analyze_diff(diff, limits=DEFAULT_REVIEW_LIMITS)
        diff_summary = {
            "supplied": True,
            "status": "ready",
            "files": len(analysis.changed_paths),
            "changed_lines": sum(
                len(lines) for lines in analysis.changed_lines.values()
            ),
        }
    selected_stages = _bounded_stage_names(stages) or tuple(
        stage.name for stage in DEFAULT_STAGES
    )
    if any(
        not isinstance(stage, str) or not stage.strip() for stage in selected_stages
    ):
        raise ReviewInputError("stages must contain non-empty strings")
    if len(selected_stages) != len(set(selected_stages)):
        raise ReviewInputError("stages must be unique")
    raw_mode = (
        provider_mode
        if provider_mode is not None
        else os.getenv("REVIEWSENSEI_PROVIDER_MODE", "local")
    )
    mode = raw_mode.strip().lower()
    if mode not in {"local", "cloud"}:
        raise ReviewInputError("provider mode must be local or cloud")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready" if diff_summary["status"] == "ready" else "incomplete",
        "identity": {
            "repository": repository,
            "pull_request": pull_request,
            "title": title,
        },
        "stages": list(selected_stages),
        "categories": [category.id for category in DEFAULT_CATEGORY_CATALOG.categories],
        "provider_mode": mode,
        "budgets": {
            "max_diff_bytes": DEFAULT_REVIEW_LIMITS.max_diff_bytes,
            "max_prompt_bytes": DEFAULT_REVIEW_LIMITS.max_prompt_bytes,
            "max_response_bytes": DEFAULT_REVIEW_LIMITS.max_provider_response_bytes,
        },
        "operations": {
            "provider_calls": 0,
            "github_writes": 0,
            "publication": False,
            "approval": False,
        },
        "diff": diff_summary,
        "skip_reasons": []
        if diff_summary["status"] == "ready"
        else ["diff-not-supplied"],
    }


def render_diagnostic(document: dict[str, Any], *, as_json: bool = False) -> str:
    """Render a bounded human or JSON diagnostic document."""

    if as_json:
        return json.dumps(document, sort_keys=True, indent=2) + "\n"
    lines = [f"status: {document.get('status', 'unknown')}"]
    if "version" in document:
        lines.append(f"version: {document.get('version') or 'unknown'}")
    for check in document.get("checks", []):
        if not isinstance(check, dict):
            continue
        lines.append(
            f"{check.get('status', 'unknown')}: {check.get('name', 'check')} — "
            f"{check.get('detail', '')}"
        )
    if "provider_mode" in document:
        lines.append(f"provider_mode: {document['provider_mode']}")
    if "stages" in document:
        lines.append(f"stages: {len(document['stages'])}")
    if "operations" in document:
        operations = document["operations"]
        lines.append(
            f"operations: provider_calls={operations.get('provider_calls', 0)}, github_writes={operations.get('github_writes', 0)}"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "DOCTOR_ACTION_REQUIRED",
    "DOCTOR_OK",
    "DOCTOR_UNKNOWN",
    "DiagnosticCheck",
    "build_plan",
    "render_diagnostic",
    "run_doctor",
]
