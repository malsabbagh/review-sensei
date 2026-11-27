"""Typed HTTP client for the ReviewSensei v6 per-PR coordinator routes."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .errors import GitHubBrokerClientError
from .v6_recovery_ledger import (
    NEVER_KEEP_LEDGER_FIELDS,
    WriterOwnershipProof,
    redact_secrets,
)

DEFAULT_COORDINATOR_BASE_URL = "https://github.reviewsensei.dev"
DEFAULT_COORDINATOR_AUDIENCE = "sts.reviewsensei.dev"
COORDINATOR_USER_AGENT = "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)"
MAX_COORDINATOR_BODY_BYTES = 256 * 1024

COORDINATOR_STATES: frozenset[str] = frozenset(
    {
        "IDLE",
        "RESERVED",
        "MINTING",
        "ACTIVE",
        "DRAINING",
        "RECONCILING",
        "HOLD",
    }
)
RECONCILIATION_OUTCOMES: frozenset[str] = frozenset(
    {
        "adopted",
        "proved_not_submitted",
        "ambiguous_hold",
        "incomplete",
    }
)
HOLD_RESOLUTION_KINDS: frozenset[str] = frozenset(
    {
        "reconciled_clean",
        "enter_reconcile",
    }
)
V6_COORDINATOR_DIAGNOSTICS: frozenset[str] = frozenset(
    {
        "broker_v6_pilot_v5_writer_disabled",
        "v6_lease_reserved",
        "v6_lease_active_other_owner",
        "v6_lease_draining",
        "v6_lease_reconciling",
        "v6_lease_hold",
        "v6_lease_head_changed",
        "v6_lease_predecessor_not_terminal",
        "v6_lease_revocation_uncertain",
        "v6_lease_escrow_lost",
        "v6_lease_clock_untrusted",
        "v6_lease_slot_continuity_unproved",
        "v6_lease_unauthorized_tuple",
        "v6_lease_rollback_blocked",
        "v6_lease_mint_uncertain",
    }
)

_HOST_RE = re.compile(r"^[a-z0-9.-]+$")
_SLOT_KEY_RE = re.compile(
    r"^v6-pr:[a-z0-9.-]+:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*$"
)
_SHA1_40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_64_RE = re.compile(r"^[0-9a-f]{64}$")
_RAY_RE = re.compile(r"^[a-f0-9]{16,64}-[a-z]{3}$", re.IGNORECASE)

Opener = Callable[..., Any]


class V6CoordinatorError(GitHubBrokerClientError):
    """Sanitized failure returned by or encountered calling v6 coordinator routes."""

    error_category = "v6_coordinator"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error: str | None = None,
        diagnostic: str | None = None,
        slot_state: Mapping[str, Any] | None = None,
        retry_after_ms: int | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(redact_secrets(message))
        self.status = status
        self.error = redact_secrets(error) if isinstance(error, str) else None
        self.diagnostic = (
            redact_secrets(diagnostic) if isinstance(diagnostic, str) else None
        )
        self.slot_state = (
            MappingProxyType(_sanitize_slot_state(slot_state))
            if isinstance(slot_state, Mapping)
            else None
        )
        self.retry_after_ms = retry_after_ms
        self.correlation_id = correlation_id


def _sanitize_slot_state(raw: Mapping[str, Any]) -> dict[str, Any]:
    banned = NEVER_KEEP_LEDGER_FIELDS | {"token", "authorization"}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        key_str = str(key)
        if key_str.lower() in banned:
            continue
        if isinstance(value, str):
            cleaned[key_str] = redact_secrets(value)
        elif isinstance(value, Mapping):
            cleaned[key_str] = _sanitize_slot_state(value)
        else:
            cleaned[key_str] = value
    return cleaned


def canonicalize_github_host(host: str) -> str:
    """Normalize a GitHub hostname to lowercase ASCII with no port or path."""

    if not isinstance(host, str) or not host or host.strip() != host:
        raise V6CoordinatorError("GitHub host must be a non-empty hostname")
    if any(ch in host for ch in (":", "/", "?", "#", "@", " ", "\t", "\n", "\r")):
        raise V6CoordinatorError(
            f"GitHub host must not include scheme, port, or path: {host!r}"
        )
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise V6CoordinatorError("GitHub host must be ASCII") from exc
    lowered = host.lower()
    if (
        lowered.startswith(".")
        or lowered.endswith(".")
        or ".." in lowered
        or _HOST_RE.fullmatch(lowered) is None
    ):
        raise V6CoordinatorError(f"GitHub host is invalid: {host!r}")
    return lowered


def build_v6_slot_key(
    *,
    host: str,
    app_id: int,
    installation_id: int,
    base_repo_id: int,
    pr_number: int,
) -> str:
    """Construct and validate a canonical v6 per-PR coordination slot key."""

    canonical_host = canonicalize_github_host(host)
    for label, value in (
        ("app_id", app_id),
        ("installation_id", installation_id),
        ("base_repo_id", base_repo_id),
        ("pr_number", pr_number),
    ):
        if type(value) is not int or value < 1:
            raise V6CoordinatorError(
                f"slot_key component {label} must be a positive integer"
            )
    slot_key = (
        f"v6-pr:{canonical_host}:{app_id}:{installation_id}:{base_repo_id}:{pr_number}"
    )
    return validate_v6_slot_key(slot_key)


def validate_v6_slot_key(slot_key: str) -> str:
    """Validate an already-constructed v6 slot key against the frozen contract."""

    if not isinstance(slot_key, str) or _SLOT_KEY_RE.fullmatch(slot_key) is None:
        raise V6CoordinatorError(f"invalid v6 slot_key: {slot_key!r}")
    return slot_key


@dataclass(frozen=True)
class V6LeaseGrant:
    """Active lease grant returned by ``POST /github/v6/lease/acquire``."""

    slot_key: str
    state: str
    generation: int
    lease_epoch: int
    reservation_id: str
    token: str = field(repr=False)
    token_expires_at_ms: int
    app_id: int
    installation_id: int
    base_repo_id: int
    pull_request: int
    head_sha: str
    coordinator_attestation_mac: str

    def to_ownership_proof(
        self,
        *,
        operation_id: str,
        run_id: int,
        run_attempt: int,
    ) -> WriterOwnershipProof:
        """Build a broker-verified ``WriterOwnershipProof`` bound to this lease."""

        return WriterOwnershipProof(
            repository_id=self.base_repo_id,
            pull_request=self.pull_request,
            operation_id=operation_id,
            run_id=run_id,
            run_attempt=run_attempt,
            writer_epoch=self.lease_epoch,
            pr_head_sha=self.head_sha,
            verified_by_broker=True,
        )


@dataclass(frozen=True)
class V6LeaseDrainResult:
    """Response returned by ``/github/v6/lease/drain`` or ``/release``."""

    slot_key: str
    state: str
    generation: int
    lease_epoch: int
    revocation_status: str


@dataclass(frozen=True)
class V6LeaseStateTransition:
    """Response returned by ``/complete-reconcile`` or ``/resolve-hold``."""

    slot_key: str
    state: str
    generation: int
    lease_epoch: int


@dataclass(frozen=True)
class V6SlotStatus:
    """Read-only slot status returned by ``POST /github/v6/lease/status``."""

    slot_key: str
    state: str
    generation: int
    lease_epoch: int
    reservation_id: str | None = None
    owner_run_id: str | None = None
    owner_run_attempt: int | None = None
    owner_head_sha: str | None = None
    owner_workflow_sha: str | None = None
    owner_job: str | None = None
    owner_comment_id: int | None = None
    owner_record_sha256: str | None = None
    token_expires_at_ms: int | None = None
    last_reason_code: str | None = None
    updated_at_ms: int | None = None


class _RefuseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if fp is not None:
            fp.close()
        raise V6CoordinatorError("Coordinator redirects are refused")


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise V6CoordinatorError("Coordinator base_url must be a non-empty HTTPS URL")
    trimmed = base_url.strip().rstrip("/")
    parsed = urlsplit(trimmed)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query:
        raise V6CoordinatorError(
            "Coordinator base_url must be an HTTPS origin without path or query"
        )
    host = parsed.hostname or ""
    canonicalize_github_host(host)
    if parsed.username or parsed.password:
        raise V6CoordinatorError("Coordinator base_url must not contain userinfo")
    return trimmed


def _require_int(value: Any, *, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise V6CoordinatorError(
            f"coordinator field {name} must be an integer >= {minimum}"
        )
    return value


def _require_opt_int(value: Any, *, name: str, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _require_int(value, name=name, minimum=minimum)


def _require_str(value: Any, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise V6CoordinatorError(f"coordinator field {name} must be a non-empty string")
    return value


def _require_opt_str(value: Any, *, name: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, name=name)


def _require_state(value: Any) -> str:
    state = _require_str(value, name="state")
    if state not in COORDINATOR_STATES:
        raise V6CoordinatorError(f"unknown coordinator state {state!r}")
    return state


def _correlation_id(headers: Any) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter("cf-ray")
    if isinstance(value, str) and _RAY_RE.fullmatch(value.strip()) is not None:
        return value.strip()
    return None


class V6CoordinatorClient:
    """Typed HTTP client for ``/github/v6/lease/*`` without quota coupling."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_COORDINATOR_BASE_URL,
        oidc_token_provider: Callable[[], str] | None = None,
        opener: Opener = urlopen,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 30.0,
        audience: str = DEFAULT_COORDINATOR_AUDIENCE,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if oidc_token_provider is not None and not callable(oidc_token_provider):
            raise V6CoordinatorError("oidc_token_provider must be callable")
        if not callable(opener):
            raise V6CoordinatorError("opener must be callable")
        if not callable(clock):
            raise V6CoordinatorError("clock must be callable")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise V6CoordinatorError("timeout_seconds must be positive and finite")
        if not isinstance(audience, str) or not audience.strip():
            raise V6CoordinatorError("audience must be a non-empty string")
        self.oidc_token_provider = oidc_token_provider
        self.opener = (
            build_opener(_RefuseRedirects()).open if opener is urlopen else opener
        )
        self.clock = clock
        self.timeout_seconds = float(timeout_seconds)
        self.audience = audience.strip()

    def _obtain_oidc_token(self) -> str:
        if self.oidc_token_provider is not None:
            token = self.oidc_token_provider()
            if not isinstance(token, str) or not token.strip():
                raise V6CoordinatorError("OIDC token provider returned empty token")
            return token.strip()

        req_token = os.getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
        req_url = os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
        if not req_token or not req_url:
            raise V6CoordinatorError("GitHub Actions OIDC environment is unavailable")
        separator = "&" if "?" in req_url else "?"
        request = Request(
            f"{req_url}{separator}audience={self.audience}",
            headers={"Authorization": f"Bearer {req_token}"},
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_COORDINATOR_BODY_BYTES + 1)
                if not isinstance(raw, (bytes, bytearray)) or len(raw) > (
                    MAX_COORDINATOR_BODY_BYTES
                ):
                    raise V6CoordinatorError("OIDC response was invalid or oversized")
                parsed = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except V6CoordinatorError:
            raise
        except HTTPError as exc:
            raise V6CoordinatorError(
                f"OIDC token request failed with HTTP {exc.code}",
                status=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise V6CoordinatorError(
                f"OIDC token request failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("value"), str):
            raise V6CoordinatorError("OIDC token response payload was invalid")
        value = parsed["value"].strip()
        if not value:
            raise V6CoordinatorError("OIDC token response value was empty")
        return value

    def _post(self, route: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        oidc_token = self._obtain_oidc_token()
        body_dict = {"oidc_token": oidc_token, **payload}
        encoded = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{route}",
            data=encoded,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(encoded)),
                "User-Agent": COORDINATOR_USER_AGENT,
            },
            method="POST",
        )
        correlation_id: str | None = None
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                correlation_id = _correlation_id(getattr(response, "headers", None))
                raw = response.read(MAX_COORDINATOR_BODY_BYTES + 1)
                if not isinstance(raw, (bytes, bytearray)):
                    raise V6CoordinatorError(
                        "Coordinator returned non-bytes response",
                        correlation_id=correlation_id,
                    )
                if len(raw) > MAX_COORDINATOR_BODY_BYTES:
                    raise V6CoordinatorError(
                        "Coordinator response exceeded size limit",
                        correlation_id=correlation_id,
                    )
                parsed = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except V6CoordinatorError:
            raise
        except HTTPError as exc:
            correlation_id = _correlation_id(exc.headers)
            raise self._parse_http_error(exc, correlation_id=correlation_id) from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise V6CoordinatorError(
                f"Coordinator request to {route} failed ({type(exc).__name__})",
                correlation_id=correlation_id,
            ) from exc

        if not isinstance(parsed, dict):
            raise V6CoordinatorError(
                f"Coordinator response from {route} must be a JSON object",
                correlation_id=correlation_id,
            )
        return parsed

    @staticmethod
    def _parse_http_error(
        exc: HTTPError, *, correlation_id: str | None
    ) -> V6CoordinatorError:
        error_code: str | None = None
        diagnostic: str | None = None
        slot_state: Mapping[str, Any] | None = None
        retry_after_ms: int | None = None
        try:
            raw = exc.read(MAX_COORDINATOR_BODY_BYTES + 1)
            if isinstance(raw, (bytes, bytearray)) and len(raw) <= (
                MAX_COORDINATOR_BODY_BYTES
            ):
                parsed = json.loads(bytes(raw).decode("utf-8", errors="strict"))
                if isinstance(parsed, Mapping):
                    if isinstance(parsed.get("error"), str):
                        error_code = parsed["error"]
                    raw_diag = parsed.get("diagnostic")
                    if isinstance(raw_diag, str):
                        diagnostic = raw_diag
                    elif isinstance(raw_diag, Mapping) and isinstance(
                        raw_diag.get("code"), str
                    ):
                        diagnostic = raw_diag["code"]
                    if isinstance(parsed.get("slot_state"), Mapping):
                        slot_state = parsed["slot_state"]
                    raw_retry = parsed.get("retry_after_ms")
                    if type(raw_retry) is int and raw_retry >= 0:
                        retry_after_ms = raw_retry
        except Exception:
            pass
        finally:
            try:
                exc.close()
            except Exception:
                pass

        diag_label = diagnostic or error_code or "unknown"
        return V6CoordinatorError(
            f"Coordinator rejected request with HTTP {exc.code} (diagnostic={diag_label})",
            status=exc.code,
            error=error_code,
            diagnostic=diagnostic,
            slot_state=slot_state,
            retry_after_ms=retry_after_ms,
            correlation_id=correlation_id,
        )

    def acquire(
        self,
        *,
        pull_request: int,
        head_sha: str,
        capabilities: Sequence[str],
        operation_id: str | None = None,
        predecessor_anchor_comment_id: int | None = None,
        control_budget: Mapping[str, Any] | None = None,
    ) -> V6LeaseGrant:
        """Request an exclusive writer lease via ``POST /github/v6/lease/acquire``."""

        _require_int(pull_request, name="pull_request", minimum=1)
        if type(head_sha) is not str or _SHA1_40_RE.fullmatch(head_sha) is None:
            raise V6CoordinatorError("head_sha must be a 40-hex SHA-1")
        if (
            not isinstance(capabilities, Sequence)
            or isinstance(capabilities, (str, bytes, bytearray))
            or not capabilities
            or any(type(c) is not str or not c.strip() for c in capabilities)
        ):
            raise V6CoordinatorError("capabilities must be a non-empty list of strings")

        payload: dict[str, Any] = {
            "pull_request": pull_request,
            "head_sha": head_sha,
            "capabilities": list(capabilities),
        }
        if operation_id is not None:
            payload["operation_id"] = _require_str(operation_id, name="operation_id")
        if predecessor_anchor_comment_id is not None:
            payload["predecessor_anchor_comment_id"] = _require_int(
                predecessor_anchor_comment_id,
                name="predecessor_anchor_comment_id",
                minimum=1,
            )
        if control_budget is not None:
            payload["control_budget"] = dict(control_budget)

        data = self._post("/github/v6/lease/acquire", payload)
        slot_key = validate_v6_slot_key(
            _require_str(data.get("slot_key"), name="slot_key")
        )
        state = _require_state(data.get("state"))
        mac = _require_str(
            data.get("coordinator_attestation_mac"), name="coordinator_attestation_mac"
        )
        if _SHA256_64_RE.fullmatch(mac) is None:
            raise V6CoordinatorError("coordinator_attestation_mac must be 64-char hex")
        resp_head = _require_str(data.get("head_sha"), name="head_sha")
        if _SHA1_40_RE.fullmatch(resp_head) is None:
            raise V6CoordinatorError("response head_sha must be 40-hex")

        return V6LeaseGrant(
            slot_key=slot_key,
            state=state,
            generation=_require_int(
                data.get("generation"), name="generation", minimum=1
            ),
            lease_epoch=_require_int(
                data.get("lease_epoch"), name="lease_epoch", minimum=1
            ),
            reservation_id=_require_str(
                data.get("reservation_id"), name="reservation_id"
            ),
            token=_require_str(data.get("token"), name="token"),
            token_expires_at_ms=_require_int(
                data.get("token_expires_at_ms"), name="token_expires_at_ms", minimum=1
            ),
            app_id=_require_int(data.get("app_id"), name="app_id", minimum=1),
            installation_id=_require_int(
                data.get("installation_id"), name="installation_id", minimum=1
            ),
            base_repo_id=_require_int(
                data.get("base_repo_id"), name="base_repo_id", minimum=1
            ),
            pull_request=_require_int(
                data.get("pull_request"), name="pull_request", minimum=1
            ),
            head_sha=resp_head,
            coordinator_attestation_mac=mac,
        )

    def drain(
        self,
        *,
        slot_key: str,
        expected_generation: int | None = None,
        expected_lease_epoch: int | None = None,
        predecessor_anchor_comment_id: int | None = None,
        control_budget: Mapping[str, Any] | None = None,
    ) -> V6LeaseDrainResult:
        """Drain/fence a slot via ``POST /github/v6/lease/drain``."""

        payload: dict[str, Any] = {"slot_key": validate_v6_slot_key(slot_key)}
        if expected_generation is not None:
            payload["expected_generation"] = _require_int(
                expected_generation, name="expected_generation", minimum=0
            )
        if expected_lease_epoch is not None:
            payload["expected_lease_epoch"] = _require_int(
                expected_lease_epoch, name="expected_lease_epoch", minimum=0
            )
        if predecessor_anchor_comment_id is not None:
            payload["predecessor_anchor_comment_id"] = _require_int(
                predecessor_anchor_comment_id,
                name="predecessor_anchor_comment_id",
                minimum=1,
            )
        if control_budget is not None:
            payload["control_budget"] = dict(control_budget)

        data = self._post("/github/v6/lease/drain", payload)
        return V6LeaseDrainResult(
            slot_key=validate_v6_slot_key(
                _require_str(data.get("slot_key"), name="slot_key")
            ),
            state=_require_state(data.get("state")),
            generation=_require_int(
                data.get("generation"), name="generation", minimum=0
            ),
            lease_epoch=_require_int(
                data.get("lease_epoch"), name="lease_epoch", minimum=0
            ),
            revocation_status=_require_str(
                data.get("revocation_status"), name="revocation_status"
            ),
        )

    def complete_reconcile(
        self,
        *,
        slot_key: str,
        generation: int,
        reconciliation_outcome: str,
        anchor_comment_id: int | None = None,
        head_sha: str | None = None,
        control_budget: Mapping[str, Any] | None = None,
    ) -> V6LeaseStateTransition:
        """Report reconciliation outcome via ``POST /github/v6/lease/complete-reconcile``."""

        if reconciliation_outcome not in RECONCILIATION_OUTCOMES:
            raise V6CoordinatorError(
                f"invalid reconciliation_outcome: {reconciliation_outcome!r}"
            )
        payload: dict[str, Any] = {
            "slot_key": validate_v6_slot_key(slot_key),
            "generation": _require_int(generation, name="generation", minimum=0),
            "reconciliation_outcome": reconciliation_outcome,
        }
        if anchor_comment_id is not None:
            payload["anchor_comment_id"] = _require_int(
                anchor_comment_id, name="anchor_comment_id", minimum=1
            )
        if head_sha is not None:
            if type(head_sha) is not str or _SHA1_40_RE.fullmatch(head_sha) is None:
                raise V6CoordinatorError("head_sha must be a 40-hex SHA-1")
            payload["head_sha"] = head_sha
        if control_budget is not None:
            payload["control_budget"] = dict(control_budget)

        data = self._post("/github/v6/lease/complete-reconcile", payload)
        return V6LeaseStateTransition(
            slot_key=validate_v6_slot_key(
                _require_str(data.get("slot_key"), name="slot_key")
            ),
            state=_require_state(data.get("state")),
            generation=_require_int(
                data.get("generation"), name="generation", minimum=0
            ),
            lease_epoch=_require_int(
                data.get("lease_epoch"), name="lease_epoch", minimum=0
            ),
        )

    def release(
        self,
        *,
        slot_key: str,
        generation: int,
        lease_epoch: int,
        reservation_id: str,
        control_budget: Mapping[str, Any] | None = None,
    ) -> V6LeaseDrainResult:
        """Release an ACTIVE lease via ``POST /github/v6/lease/release``."""

        payload: dict[str, Any] = {
            "slot_key": validate_v6_slot_key(slot_key),
            "generation": _require_int(generation, name="generation", minimum=1),
            "lease_epoch": _require_int(lease_epoch, name="lease_epoch", minimum=1),
            "reservation_id": _require_str(reservation_id, name="reservation_id"),
        }
        if control_budget is not None:
            payload["control_budget"] = dict(control_budget)

        data = self._post("/github/v6/lease/release", payload)
        return V6LeaseDrainResult(
            slot_key=validate_v6_slot_key(
                _require_str(data.get("slot_key"), name="slot_key")
            ),
            state=_require_state(data.get("state")),
            generation=_require_int(
                data.get("generation"), name="generation", minimum=0
            ),
            lease_epoch=_require_int(
                data.get("lease_epoch"), name="lease_epoch", minimum=0
            ),
            revocation_status=_require_str(
                data.get("revocation_status"), name="revocation_status"
            ),
        )

    def status(
        self,
        *,
        pull_request: int,
        slot_key: str | None = None,
        control_budget: Mapping[str, Any] | None = None,
    ) -> V6SlotStatus:
        """Query current slot state via ``POST /github/v6/lease/status``."""

        payload: dict[str, Any] = {
            "pull_request": _require_int(pull_request, name="pull_request", minimum=1)
        }
        if slot_key is not None:
            payload["slot_key"] = validate_v6_slot_key(slot_key)
        if control_budget is not None:
            payload["control_budget"] = dict(control_budget)

        data = self._post("/github/v6/lease/status", payload)
        return V6SlotStatus(
            slot_key=validate_v6_slot_key(
                _require_str(data.get("slot_key"), name="slot_key")
            ),
            state=_require_state(data.get("state")),
            generation=_require_int(
                data.get("generation"), name="generation", minimum=0
            ),
            lease_epoch=_require_int(
                data.get("lease_epoch"), name="lease_epoch", minimum=0
            ),
            reservation_id=_require_opt_str(
                data.get("reservation_id"), name="reservation_id"
            ),
            owner_run_id=_require_opt_str(
                data.get("owner_run_id"), name="owner_run_id"
            ),
            owner_run_attempt=_require_opt_int(
                data.get("owner_run_attempt"), name="owner_run_attempt", minimum=1
            ),
            owner_head_sha=_require_opt_str(
                data.get("owner_head_sha"), name="owner_head_sha"
            ),
            owner_workflow_sha=_require_opt_str(
                data.get("owner_workflow_sha"), name="owner_workflow_sha"
            ),
            owner_job=_require_opt_str(data.get("owner_job"), name="owner_job"),
            owner_comment_id=_require_opt_int(
                data.get("owner_comment_id"), name="owner_comment_id", minimum=1
            ),
            owner_record_sha256=_require_opt_str(
                data.get("owner_record_sha256"), name="owner_record_sha256"
            ),
            token_expires_at_ms=_require_opt_int(
                data.get("token_expires_at_ms"), name="token_expires_at_ms", minimum=1
            ),
            last_reason_code=_require_opt_str(
                data.get("last_reason_code"), name="last_reason_code"
            ),
            updated_at_ms=_require_opt_int(
                data.get("updated_at_ms"), name="updated_at_ms", minimum=0
            ),
        )

    def resolve_hold(
        self,
        *,
        slot_key: str,
        generation: int,
        hold_record_comment_id: int,
        resolution_kind: str,
        anchor_comment_id: int | None = None,
        control_budget: Mapping[str, Any] | None = None,
    ) -> V6LeaseStateTransition:
        """Explicitly resolve a HOLD slot via ``POST /github/v6/lease/resolve-hold``."""

        if resolution_kind not in HOLD_RESOLUTION_KINDS:
            raise V6CoordinatorError(f"invalid resolution_kind: {resolution_kind!r}")
        payload: dict[str, Any] = {
            "slot_key": validate_v6_slot_key(slot_key),
            "generation": _require_int(generation, name="generation", minimum=0),
            "hold_record_comment_id": _require_int(
                hold_record_comment_id, name="hold_record_comment_id", minimum=1
            ),
            "resolution_kind": resolution_kind,
        }
        if anchor_comment_id is not None:
            payload["anchor_comment_id"] = _require_int(
                anchor_comment_id, name="anchor_comment_id", minimum=1
            )
        if control_budget is not None:
            payload["control_budget"] = dict(control_budget)

        data = self._post("/github/v6/lease/resolve-hold", payload)
        return V6LeaseStateTransition(
            slot_key=validate_v6_slot_key(
                _require_str(data.get("slot_key"), name="slot_key")
            ),
            state=_require_state(data.get("state")),
            generation=_require_int(
                data.get("generation"), name="generation", minimum=0
            ),
            lease_epoch=_require_int(
                data.get("lease_epoch"), name="lease_epoch", minimum=0
            ),
        )
