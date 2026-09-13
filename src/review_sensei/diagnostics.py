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
from .errors import ReviewInputError
from .service import DEFAULT_CATEGORY_CATALOG, DEFAULT_STAGES
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
) -> dict[str, Any]:
    """Return bounded, side-effect-free installation diagnostics.

    ``include_network`` is intentionally reported as unknown rather than
    probing an endpoint.  Network probes belong to an explicitly authorized
    integration command and must not be part of ordinary diagnostics.
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
    required_assets = (
        asset_root / "default_stages" / "01-default-review.json",
        asset_root / "default_categories" / "01-correctness.json",
    )
    missing = [path.name for path in required_assets if not path.is_file()]
    checks.append(
        DiagnosticCheck(
            "packaged-assets",
            "action" if missing else "pass",
            "missing packaged assets"
            if missing
            else "default stages and categories available",
        )
    )
    mode = os.getenv("REVIEWSENSEI_PROVIDER_MODE", "local").strip().lower()
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
    for label, configured, expected in (
        ("stages", stages_dir, "stages"),
        ("categories", categories_dir, "categories"),
    ):
        if configured is None:
            checks.append(
                DiagnosticCheck(label, "pass", f"packaged default {expected} selected")
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
            checks.append(
                DiagnosticCheck(
                    label, "pass", "configured trusted-base directory is readable"
                )
            )
    if context_root is None:
        checks.append(
            DiagnosticCheck("context", "pass", "no supplemental context configured")
        )
    elif not context_root.is_dir():
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
    status = (
        "action"
        if any(check.status == "action" for check in checks)
        else (
            "unknown" if any(check.status == "unknown" for check in checks) else "pass"
        )
    )
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

    if diff is None:
        diff_summary: dict[str, Any] = {"supplied": False, "status": "unknown"}
    else:
        analysis = analyze_diff(diff, limits=DEFAULT_REVIEW_LIMITS)
        diff_summary = {
            "supplied": True,
            "status": "ready",
            "files": len(analysis.changed_paths),
            "changed_lines": len(analysis.changed_lines),
        }
    selected_stages = tuple(stages) or tuple(stage.name for stage in DEFAULT_STAGES)
    mode = (
        (provider_mode or os.getenv("REVIEWSENSEI_PROVIDER_MODE", "local"))
        .strip()
        .lower()
    )
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
        lines.append(f"{check['status']}: {check['name']} — {check['detail']}")
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
