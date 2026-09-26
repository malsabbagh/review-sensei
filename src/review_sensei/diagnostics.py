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
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .baseline import VerificationScope, preview_verification_scope
from .convergence import (
    OPERATOR_REVIEW_MODES,
    ReviewConvergencePolicy,
    evaluate_round_admission,
    resolve_review_convergence_policy,
    resolve_shadow_review_mode,
)
from .diff import analyze_diff
from .errors import ReviewInputError, ReviewSenseiError
from .provider_config import (
    LOCAL_LOOPBACK_HOSTS,
    provider_mode_default,
    resolve_effective_provider_configuration,
)
from .release_manifest import validate_compatibility_manifest
from .service import DEFAULT_CATEGORY_CATALOG, DEFAULT_STAGES
from .session import (
    LOAD_STATUSES,
    SessionIdentity,
    admission_diagnostic,
    resolve_local_session_ledger,
)
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
# Diagnostic-only token; it is never a persisted SessionRecord or a
# SessionLoadResult status.
INVALID_SESSION_STATUS = "invalid"
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)
MAX_PROBE_BYTES = 8192
PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _session_ledger_diagnostic(
    *,
    session_ledger: Path | str | None,
    repository: str | None,
    pull_request: int | None,
    policy: ReviewConvergencePolicy | None,
) -> tuple[DiagnosticCheck | None, dict[str, object] | None]:
    ledger = resolve_local_session_ledger(session_ledger)
    if ledger is None:
        return None, None
    if repository is None or pull_request is None:
        return (
            DiagnosticCheck(
                "session-ledger",
                "action",
                "session ledger requires repository and pull-request",
            ),
            {"status": "unavailable"},
        )
    try:
        identity = SessionIdentity(repository=repository, pull_request=pull_request)
    except ReviewInputError as exc:
        return DiagnosticCheck("session-ledger", "action", str(exc)), {
            "status": "invalid"
        }
    loaded = ledger.load(identity)
    if loaded.status == "ok" and loaded.record is not None:
        record = loaded.record
        payload: dict[str, object] = {
            "status": "ok",
            "completed_initial_reviews": record.completed_initial_reviews,
            "completed_verification_rounds": record.completed_verification_rounds,
            "failed_attempts": record.failed_attempts,
            "generation": record.generation,
        }
        if policy is not None:
            decision = evaluate_round_admission(record.to_round_state(), policy)
            payload["admit"] = decision.admit
            payload["handoff"] = decision.handoff
            payload["handoff_reason"] = decision.handoff_reason
            payload["diagnostic"] = admission_diagnostic(decision)
            payload["may_emit_approve"] = decision.may_emit_approve
        return (
            DiagnosticCheck(
                "session-ledger",
                "pass",
                (
                    "initial="
                    f"{record.completed_initial_reviews} "
                    "verification="
                    f"{record.completed_verification_rounds} "
                    f"failed_attempts={record.failed_attempts}"
                ),
            ),
            payload,
        )
    status = loaded.status
    if status == "missing":
        return (
            DiagnosticCheck(
                "session-ledger",
                "pass",
                "session ledger is not yet initialized",
            ),
            {"status": status},
        )
    detail = f"session ledger is {status}"
    payload = {"status": status}
    if loaded.detail is not None:
        detail = f"{detail}: {loaded.detail}"
        payload["detail"] = loaded.detail
    return DiagnosticCheck("session-ledger", "action", detail), payload


def _plan_session_record(
    *,
    session_ledger: Path | str | None,
    repository: str | None,
    pull_request: int | None,
    policy: ReviewConvergencePolicy | None = None,
) -> dict[str, object] | None:
    check, payload = _session_ledger_diagnostic(
        session_ledger=session_ledger,
        repository=repository,
        pull_request=pull_request,
        policy=policy,
    )
    if check is None:
        return None
    return _normalize_session_record(payload)


def _normalize_session_record(
    record: dict[str, object] | None,
) -> dict[str, object] | None:
    """Expose a stable status token in caller-facing diagnostics."""

    if record is None:
        return None
    normalized = dict(record)
    status = normalized.get("status")
    if status is None:
        normalized["status"] = "missing"
    elif not isinstance(status, str) or status not in LOAD_STATUSES:
        normalized["status"] = INVALID_SESSION_STATUS
    return normalized


def _verification_scope_from_session(
    *,
    policy: ReviewConvergencePolicy | None,
    session_record: dict[str, object] | None,
    changed_paths: tuple[str, ...] = (),
) -> VerificationScope | None:
    if policy is None:
        return None
    completed: int | None = None
    session_status: str | None = None
    if session_record is not None:
        if "status" not in session_record:
            # A partial record has no trustworthy counter, but it is not an
            # integrity failure.  Treat it like an uninitialized ledger so
            # preview remains a safe baseline-required display.
            session_status = "missing"
        else:
            status = session_record.get("status")
            session_status = (
                status
                if isinstance(status, str) and status in LOAD_STATUSES
                else "invalid"
            )
        if session_status == "ok":
            initial = session_record.get("completed_initial_reviews")
            if isinstance(initial, int) and not isinstance(initial, bool):
                completed = initial
            else:
                # A syntactically valid status with an invalid counter is not
                # a zero-count ledger; it is untrusted state.
                session_status = "invalid"
        elif session_status == "missing":
            completed = 0
    return preview_verification_scope(
        policy=policy,
        completed_initial_reviews=completed,
        session_status=session_status,
        changed_paths=changed_paths,
    )


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


def _snapshot_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _GIT_SHA_RE.fullmatch(value):
        raise ReviewInputError(f"{label} must be a 40-character lowercase Git SHA")
    return value


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().casefold()
    return host in LOCAL_LOOPBACK_HOSTS


def _provider_network_probe_checks(
    *,
    base_url: str,
    model: str,
    provider: str,
    allow_data_egress: bool,
    opener: Callable[..., Any],
) -> tuple[DiagnosticCheck, DiagnosticCheck]:
    remote = not _is_loopback_url(base_url)
    if remote and not allow_data_egress:
        return (
            DiagnosticCheck(
                "endpoint",
                "unknown",
                "remote endpoint not probed without explicit data-egress authorization",
            ),
            DiagnosticCheck(
                "model",
                "unknown",
                "model inventory not probed for a remote endpoint",
            ),
        )
    if provider in {"openrouter", "openai-compatible", "fixture"}:
        return (
            DiagnosticCheck(
                "endpoint",
                "unknown",
                f"{provider} endpoint not probed without a dedicated read-only probe",
            ),
            DiagnosticCheck(
                "model",
                "unknown",
                f"{provider} model inventory not probed offline",
            ),
        )
    return probe_provider_endpoint(
        base_url=base_url,
        model=model,
        opener=opener,
        allow_remote=allow_data_egress,
    )


def _bounded_probe_get(
    url: str,
    *,
    opener: Callable[..., Any],
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ReviewInputError("probe URL must be a credential-free http(s) URL")
    request = Request(url, method="GET", headers=headers or {})
    try:
        with opener(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200))
            body = response.read(MAX_PROBE_BYTES + 1)
    except HTTPError as exc:
        body = exc.read(MAX_PROBE_BYTES + 1) if exc.fp is not None else b""
        return int(exc.code), body
    except (URLError, TimeoutError, OSError) as exc:
        raise ReviewInputError(f"unreachable endpoint: {type(exc).__name__}") from exc
    if len(body) > MAX_PROBE_BYTES:
        raise ReviewInputError("probe response exceeds the diagnostic size limit")
    return status, body


def _provider_probe_url(base_url: str, suffix: str) -> str:
    return base_url.rstrip("/") + suffix


def check_compatibility_manifest(path: Path | None) -> DiagnosticCheck:
    """Validate a supplied compatibility manifest without network access."""

    if path is None:
        return DiagnosticCheck(
            "compatibility",
            "unknown",
            "compatibility evidence not supplied",
        )
    if path.is_symlink() or not path.is_file():
        return DiagnosticCheck(
            "compatibility",
            "action",
            "unavailable compatibility evidence",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReviewInputError("compatibility manifest must be an object")
        validate_compatibility_manifest(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ReviewSenseiError,
        ValueError,
    ) as exc:
        return DiagnosticCheck(
            "compatibility",
            "action",
            f"unavailable compatibility evidence: {exc}",
        )
    return DiagnosticCheck(
        "compatibility",
        "pass",
        "compatibility manifest validated",
    )


def probe_provider_endpoint(
    *,
    base_url: str,
    model: str,
    opener: Callable[..., Any],
    allow_remote: bool,
) -> tuple[DiagnosticCheck, DiagnosticCheck]:
    """Read-only endpoint and model probes. Never send a generation request."""

    if not allow_remote and not _is_loopback_url(base_url):
        skipped = DiagnosticCheck(
            "endpoint",
            "unknown",
            "remote endpoint not probed without explicit data-egress authorization",
        )
        return skipped, DiagnosticCheck(
            "model",
            "unknown",
            "model inventory not probed for a remote endpoint",
        )
    try:
        status, body = _bounded_probe_get(
            _provider_probe_url(base_url, "/version"), opener=opener
        )
    except ReviewInputError as exc:
        detail = str(exc)
        failed = DiagnosticCheck("endpoint", "action", detail)
        return failed, DiagnosticCheck("model", "unknown", "model inventory not probed")
    if status != 200:
        failed = DiagnosticCheck(
            "endpoint", "action", f"unreachable endpoint (HTTP {status})"
        )
        return failed, DiagnosticCheck("model", "unknown", "model inventory not probed")
    endpoint = DiagnosticCheck("endpoint", "pass", "local runner endpoint reachable")
    try:
        tags_status, tags_body = _bounded_probe_get(
            _provider_probe_url(base_url, "/tags"), opener=opener
        )
    except ReviewInputError as exc:
        return endpoint, DiagnosticCheck("model", "action", str(exc))
    if tags_status != 200:
        return endpoint, DiagnosticCheck(
            "model", "action", f"missing runner/model (HTTP {tags_status})"
        )
    try:
        payload = json.loads(tags_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return endpoint, DiagnosticCheck(
            "model", "action", "missing runner/model (malformed inventory)"
        )
    names: set[str] = set()
    models = payload.get("models") if isinstance(payload, dict) else None
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
    if model not in names:
        return endpoint, DiagnosticCheck("model", "action", "missing runner/model")
    return endpoint, DiagnosticCheck("model", "pass", "configured model is installed")


def probe_repository_metadata(
    repository: str | None,
    *,
    token: str | None,
    opener: Callable[..., Any],
) -> DiagnosticCheck:
    """Read-only GitHub metadata probe using an already-present token only."""

    if repository is None:
        return DiagnosticCheck(
            "repository-metadata",
            "unknown",
            "repository metadata not checked (repository not supplied)",
        )
    if not _SAFE_REPOSITORY_RE.fullmatch(repository):
        return DiagnosticCheck(
            "repository-metadata",
            "action",
            "repository identity is malformed",
        )
    if not token:
        return DiagnosticCheck(
            "repository-metadata",
            "unknown",
            "repository metadata not checked (no token)",
        )
    url = f"https://api.github.com/repos/{repository}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "review-sensei-doctor",
    }
    try:
        status, _body = _bounded_probe_get(url, opener=opener, headers=headers)
    except ReviewInputError as exc:
        return DiagnosticCheck("repository-metadata", "action", str(exc))
    if status in {401, 403, 404}:
        return DiagnosticCheck(
            "repository-metadata",
            "action",
            "inaccessible repository metadata",
        )
    if status != 200:
        return DiagnosticCheck(
            "repository-metadata",
            "action",
            f"inaccessible repository metadata (HTTP {status})",
        )
    return DiagnosticCheck(
        "repository-metadata", "pass", "repository metadata is readable"
    )


def run_doctor(
    *,
    stages_dir: Path | None = None,
    categories_dir: Path | None = None,
    context_root: Path | None = None,
    include_network: bool = False,
    provider_mode: str | None = None,
    review_mode: str | None = None,
    repository: str | None = None,
    profile: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    compatibility_manifest: Path | None = None,
    allow_data_egress: bool = False,
    opener: Callable[..., Any] = urlopen,
    session_ledger: Path | str | None = None,
    pull_request: int | None = None,
) -> dict[str, Any]:
    """Return bounded, side-effect-free installation diagnostics.

    Offline checks never open sockets.  ``include_network`` enables read-only
    probes for a loopback runner/model, optional repository metadata when a
    token is already present, and compatibility-evidence availability.
    Probes never mint broker tokens or send a generation request.

    Custom ``stages_dir`` uses the same catalog selection as review: the
    category catalog stays unset unless ``categories_dir`` is supplied.
    Stages that declare ``category_ids`` therefore require ``--categories-dir``,
    matching the review runner.

    ``provider_mode`` reports operator configuration only.  When omitted, doctor
    reads ``REVIEWSENSEI_PROVIDER_MODE`` as a diagnostic default; this is not a
    review-time trust boundary and does not authorize provider calls.

    ``review_mode`` reports the issue #136 review-convergence policy.  When
    omitted, doctor reads ``REVIEWSENSEI_REVIEW_MODE`` and defaults to
    ``legacy``.  ``legacy`` stays display-only and keeps ADR 0032 publication.
    Operator modes report ``enforcement=publication`` because C2 applies the
    blocker-admission evaluator before GitHub review events.  The setting does
    not change ``REVIEWSENSEI_AUTO_APPROVE`` default-on semantics.

    ``session_ledger`` reports the issue #136 C3 durable session record when a
    local ledger path is supplied or ``REVIEWSENSEI_SESSION_LEDGER`` is set.
    Missing, expired, or tampered state is explicit. Doctor never writes.
    Operator modes also report C4 verification scope and C5 automation
    admission. Without a ledger, operator modes cannot enforce round budgets.
    ``legacy`` stays unscoped. After a completed initial review the next pass
    is verification over existing concerns plus changed and related paths.
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
    try:
        mode = provider_mode_default(provider_mode)
        provider_configuration = resolve_effective_provider_configuration(
            profile=profile,
            provider=provider,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            provider_mode=mode,
        )
        checks.append(
            DiagnosticCheck("provider-mode", "pass", f"{mode} (offline check)")
        )
        if provider_configuration.get(
            "credential_required"
        ) and not provider_configuration.get("credential_present"):
            credential_env = provider_configuration.get("credential_env")
            checks.append(
                DiagnosticCheck(
                    "credential",
                    "action",
                    f"environment variable {credential_env} is unavailable",
                )
            )
    except ReviewInputError as exc:
        provider_configuration = None
        checks.append(DiagnosticCheck("provider-mode", "action", str(exc)))
    review_convergence: ReviewConvergencePolicy | None
    try:
        review_convergence = resolve_review_convergence_policy(mode=review_mode)
        checks.append(
            DiagnosticCheck(
                "review-convergence",
                "pass",
                review_convergence.doctor_detail(),
            )
        )
    except ReviewInputError as exc:
        review_convergence = None
        checks.append(DiagnosticCheck("review-convergence", "action", str(exc)))
    shadow_policy: ReviewConvergencePolicy | None = None
    try:
        shadow_mode = resolve_shadow_review_mode()
        if shadow_mode is not None:
            shadow_policy = resolve_review_convergence_policy(mode=shadow_mode)
            checks.append(
                DiagnosticCheck(
                    "review-shadow",
                    "pass",
                    (
                        f"observation-only {shadow_policy.mode}; "
                        "publication stays on the resolved review mode"
                    ),
                )
            )
    except ReviewInputError as exc:
        checks.append(DiagnosticCheck("review-shadow", "action", str(exc)))
    session_record: dict[str, object] | None = None
    session_check, raw_session_record = _session_ledger_diagnostic(
        session_ledger=session_ledger,
        repository=repository,
        pull_request=pull_request,
        policy=review_convergence,
    )
    session_record = _normalize_session_record(raw_session_record)
    if session_check is not None:
        checks.append(session_check)
    if (
        review_convergence is not None
        and review_convergence.mode in OPERATOR_REVIEW_MODES
    ):
        if session_check is None:
            checks.append(
                DiagnosticCheck(
                    "automation-admission",
                    "action",
                    "operator mode cannot report automated-review admission "
                    "without a session ledger",
                )
            )
        elif session_record is not None and session_record.get("status") == "ok":
            admit = session_record.get("admit")
            reason = session_record.get("handoff_reason")
            detail = (
                f"admit={admit} verification="
                f"{session_record.get('completed_verification_rounds')}"
            )
            if not admit:
                detail = f"{detail} diagnostic={session_record.get('diagnostic')}"
            if session_record.get("handoff"):
                detail = f"{detail} handoff={reason}"
            checks.append(
                DiagnosticCheck(
                    "automation-admission",
                    "pass" if admit else "action",
                    detail,
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    "automation-admission",
                    "action",
                    "session ledger is not ready for round enforcement",
                )
            )
    verification_scope = None
    if review_convergence is not None:
        verification_scope = _verification_scope_from_session(
            policy=review_convergence,
            session_record=session_record,
        )
        if verification_scope is not None:
            check_status = (
                "action" if verification_scope.status == "ledger-untrusted" else "pass"
            )
            detail = (
                f"status={verification_scope.status} "
                f"round={verification_scope.round_kind} "
                "late_admission="
                f"{verification_scope.late_admission_required}"
            )
            if verification_scope.invalidation_reason is not None:
                detail = f"{detail} reason={verification_scope.invalidation_reason}"
            checks.append(DiagnosticCheck("verification-scope", check_status, detail))
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
    checks.append(
        DiagnosticCheck(
            "symbol-context",
            "pass",
            "opt-in trusted-base symbol context is disabled by default",
        )
    )
    from .hosting.github.checks import REVIEW_CHECK_NAME, required_check_identity
    from .hosting.github.setup import SETUP_APP_LOGIN

    gate_identity = required_check_identity(app_slug=SETUP_APP_LOGIN)
    checks.append(
        DiagnosticCheck(
            "review-gate",
            "pass",
            (
                "the merge gate is the check "
                f"{gate_identity['name']!r} produced by App "
                f"{gate_identity['producer']!r}; "
                f"{REVIEW_CHECK_NAME!r} must be marked required by a repository "
                "administrator (ReviewSensei requests no Administration "
                "permission and never changes branch protection), the App needs "
                "Checks: write, and GitHub never runs required checks on "
                "App-authored pull requests"
            ),
        )
    )
    manifest_path = compatibility_manifest
    if manifest_path is None:
        configured_manifest = os.getenv(
            "REVIEWSENSEI_COMPATIBILITY_MANIFEST", ""
        ).strip()
        if configured_manifest:
            manifest_path = Path(configured_manifest)
    if include_network or manifest_path is not None:
        checks.append(check_compatibility_manifest(manifest_path))
    if include_network and provider_configuration is not None:
        endpoint_check, model_check = _provider_network_probe_checks(
            base_url=str(provider_configuration["base_url"]),
            model=str(provider_configuration["model"]),
            provider=str(provider_configuration["provider"]),
            allow_data_egress=allow_data_egress,
            opener=opener,
        )
        checks.append(endpoint_check)
        checks.append(model_check)
        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        checks.append(probe_repository_metadata(repository, token=token, opener=opener))
    else:
        checks.append(
            DiagnosticCheck("network", "unknown", "not checked (offline mode)")
        )
    if any(check.status == "action" for check in checks):
        status = "action"
    elif any(
        check.status == "unknown"
        and check.detail
        not in {
            "not checked (offline mode)",
            "compatibility evidence not supplied",
            "repository metadata not checked (repository not supplied)",
        }
        for check in checks
    ):
        status = "unknown"
    else:
        status = "pass"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "version": version,
        "checks": [check.to_dict() for check in checks],
    }
    if provider_configuration is not None:
        report["provider_configuration"] = provider_configuration
    if review_convergence is not None:
        report["review_convergence"] = review_convergence.to_dict()
    if shadow_policy is not None:
        report["shadow_review_convergence"] = shadow_policy.to_dict()
    if session_record is not None:
        report["session_record"] = session_record
    if verification_scope is not None:
        report["verification"] = verification_scope.to_dict()
    return report


def build_plan(
    *,
    diff: str | None = None,
    repository: str | None = None,
    pull_request: int | None = None,
    title: str | None = None,
    stages: Iterable[str] = (),
    profile: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    provider_mode: str | None = None,
    review_mode: str | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    categories_dir: Path | None = None,
    session_ledger: Path | str | None = None,
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
    identity_base = _snapshot_sha(base_sha, label="base_sha") if base_sha else None
    identity_head = _snapshot_sha(head_sha, label="head_sha") if head_sha else None

    if diff is None:
        diff_summary: dict[str, Any] = {"supplied": False, "status": "unknown"}
        changed_paths: tuple[str, ...] = ()
    elif not isinstance(diff, str):
        raise ReviewInputError("diff must be a string")
    else:
        # Plan readiness uses the same bounded diff analysis path as review.
        analysis = analyze_diff(diff, limits=DEFAULT_REVIEW_LIMITS)
        changed_paths = analysis.changed_paths
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
    mode = provider_mode_default(provider_mode)
    provider_configuration = resolve_effective_provider_configuration(
        profile=profile,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        provider_mode=mode,
    )
    if categories_dir is None:
        categories = [category.id for category in DEFAULT_CATEGORY_CATALOG.categories]
    else:
        catalog = load_review_categories_from_dir(categories_dir)
        categories = [category.id for category in catalog.categories]
    skip_reasons: list[str] = []
    if diff_summary["status"] != "ready":
        skip_reasons.append("diff-not-supplied")
    if identity_base is None or identity_head is None:
        skip_reasons.append("snapshot-identity-not-supplied")
    shadow_plan: dict[str, object] | None = None
    shadow_mode = resolve_shadow_review_mode()
    if shadow_mode is not None:
        shadow_plan = resolve_review_convergence_policy(mode=shadow_mode).to_dict()
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready" if diff_summary["status"] == "ready" else "incomplete",
        "identity": {
            "repository": repository,
            "pull_request": pull_request,
            "title": title,
            "base_sha": identity_base,
            "head_sha": identity_head,
        },
        "stages": list(selected_stages),
        "categories": categories,
        "provider_mode": mode,
        "provider_configuration": provider_configuration,
        "review_convergence": resolve_review_convergence_policy(
            mode=review_mode
        ).to_dict(),
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
        "skip_reasons": skip_reasons,
    }
    if shadow_plan is not None:
        document["shadow_review_convergence"] = shadow_plan
    session_record = _plan_session_record(
        session_ledger=session_ledger,
        repository=repository,
        pull_request=pull_request,
        policy=resolve_review_convergence_policy(mode=review_mode),
    )
    if session_record is not None:
        document["session_record"] = session_record
    elif (
        resolve_review_convergence_policy(mode=review_mode).mode
        in OPERATOR_REVIEW_MODES
    ):
        skip_reasons.append("session-ledger-required")
        document["skip_reasons"] = skip_reasons
    verification_scope = _verification_scope_from_session(
        policy=resolve_review_convergence_policy(mode=review_mode),
        session_record=session_record,
        changed_paths=changed_paths,
    )
    if verification_scope is not None:
        document["verification"] = verification_scope.to_dict()
    return document


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
    provider_configuration = document.get("provider_configuration")
    if isinstance(provider_configuration, dict):
        lines.append(
            "provider: "
            f"{provider_configuration.get('provider')} "
            f"model={provider_configuration.get('model')}"
        )
        lines.append(
            "inference: "
            f"execution={provider_configuration.get('execution_location')} "
            f"inference={provider_configuration.get('inference_location')}"
        )
        credential_env = provider_configuration.get("credential_env")
        if credential_env:
            present = provider_configuration.get("credential_present")
            lines.append(
                f"credential: {credential_env} ({'present' if present else 'missing'})"
            )
        policy = provider_configuration.get("openrouter_policy")
        if isinstance(policy, dict) and policy.get("upstream_provider"):
            lines.append(
                f"openrouter_policy: upstream={policy.get('upstream_provider')}"
            )
        qualification = provider_configuration.get("qualification_status")
        if qualification:
            lines.append(f"qualification: {qualification}")
        timeout_seconds = provider_configuration.get("timeout_seconds")
        if timeout_seconds is not None:
            lines.append(f"timeout_seconds: {timeout_seconds}")
    review_convergence = document.get("review_convergence")
    if isinstance(review_convergence, dict) and review_convergence.get("mode"):
        lines.append(
            "review_convergence: "
            f"mode={review_convergence.get('mode')} "
            f"enforcement={review_convergence.get('enforcement')} "
            "rounds=uncapped "
            f"failed_attempts={review_convergence.get('max_failed_attempts')}"
        )
    shadow_review = document.get("shadow_review_convergence")
    if isinstance(shadow_review, dict) and shadow_review.get("mode"):
        lines.append(
            "review_shadow: "
            f"mode={shadow_review.get('mode')} observation-only "
            f"enforcement={shadow_review.get('enforcement')}"
        )
    session_record = document.get("session_record")
    if isinstance(session_record, dict):
        status = session_record.get("status")
        if status:
            detail = f"status={status}"
            if status == "ok":
                detail += (
                    f" initial={session_record.get('completed_initial_reviews', 0)}"
                    f" verification={session_record.get('completed_verification_rounds', 0)}"
                    f" failed_attempts={session_record.get('failed_attempts', 0)}"
                )
                if "admit" in session_record:
                    detail += f" admit={session_record.get('admit')}"
                if session_record.get("handoff"):
                    detail += f" handoff={session_record.get('handoff_reason')}"
            lines.append(f"session_ledger: {detail}")
    verification = document.get("verification")
    if isinstance(verification, dict) and verification.get("status"):
        lines.append(
            "verification: "
            f"status={verification.get('status')} "
            f"round={verification.get('round_kind')} "
            f"late_admission={verification.get('late_admission_required')} "
            f"paths={len(verification.get('reviewed_paths') or ())}"
        )
    identity = document.get("identity")
    if isinstance(identity, dict):
        if identity.get("base_sha") or identity.get("head_sha"):
            lines.append(
                "snapshot: "
                f"base={identity.get('base_sha') or 'unknown'} "
                f"head={identity.get('head_sha') or 'unknown'}"
            )
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
    "check_compatibility_manifest",
    "probe_provider_endpoint",
    "probe_repository_metadata",
    "render_diagnostic",
    "resolve_effective_provider_configuration",
    "run_doctor",
]
