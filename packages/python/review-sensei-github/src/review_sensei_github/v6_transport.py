"""ReviewSensei v6 dual-credential transport and rate-limit / permission classifiers.

Separates read-only workflow ``GITHUB_TOKEN`` bulk scans from stateless
ReviewSensei App broker operations, tracks each credential's quota
independently, classifies REST ``403``/``429`` and GraphQL HTTP ``200`` rate
limits distinctly from ``403`` permission errors, and decides between bounded
in-job waits and clean v6.0 maintainer-rerun pauses.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Protocol

from review_sensei.errors import ReviewInputError

from .errors import (
    GitHubHTTPError,
    GitHubHTTPPermissionError,
    GitHubHTTPRateLimitError,
)
from .http import _header_value
from .v6_credential_matrix import (
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
    authorize_v6_endpoint_credential,
    get_v6_endpoint_rule,
)

WORKFLOW_TOKEN_DEFAULT_LIMIT = 1000
WORKFLOW_TOKEN_GHEC_LIMIT = 15000
APP_BROKER_DEFAULT_LIMIT = 5000

DEFAULT_MAX_IN_JOB_SLEEP_SECONDS = 300.0
DEFAULT_CHECKPOINT_SAFETY_MARGIN_SECONDS = 30.0
DEFAULT_APP_CHECKPOINT_RESERVE = 20
MAINTAINER_RERUN_COMMAND = "@reviewsensei verify"

_ALLOWED_HTTP_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})
_ALLOWED_THROTTLE_ACTIONS = frozenset({"sleep", "pause_for_maintainer_rerun"})
_SECONDARY_RATE_LIMIT_PHRASES = (
    "secondary rate limit",
    "abuse detection",
    "exceeded a secondary rate limit",
)
_PRIMARY_RATE_LIMIT_PHRASES = (
    "api rate limit exceeded",
    "rate limit exceeded",
)
_GRAPHQL_RATE_LIMIT_PHRASES = (
    "api rate limit exceeded",
    "secondary rate limit",
    "rate limit",
)
_GRAPHQL_PERMISSION_ERROR_TYPES = frozenset({"FORBIDDEN", "UNAUTHORIZED"})
_GRAPHQL_PERMISSION_PHRASES = (
    "insufficient permission",
    "insufficient permissions",
    "resource not accessible",
    "forbidden",
    "must have admin rights",
)
_LINK_SEGMENT_RE = re.compile(r"<([^>\s]+)>\s*;([^<]+)")
_LINK_REL_RE = re.compile(r'(?:^|;)\s*rel\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s;]+))')


def default_quota_limit(
    credential_kind: CredentialKind, *, is_ghec: bool = False
) -> int:
    """Return the default hourly GitHub API ceiling for ``credential_kind``."""

    if credential_kind == CredentialKind.WORKFLOW_GITHUB_TOKEN:
        return WORKFLOW_TOKEN_GHEC_LIMIT if is_ghec else WORKFLOW_TOKEN_DEFAULT_LIMIT
    if credential_kind == CredentialKind.APP_BROKER:
        return APP_BROKER_DEFAULT_LIMIT
    raise ValueError(f"unsupported credential_kind: {credential_kind!r}")


def _validate_non_negative_finite(
    value: Any, *, name: str, allow_zero: bool = True
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (value < 0 if allow_zero else value <= 0)
    ):
        raise ValueError(
            f"{name} must be a finite {'non-negative' if allow_zero else 'positive'} number"
        )
    return float(value)


def _validate_non_negative_int(
    value: Any, *, name: str, allow_zero: bool = True
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or (value < 0 if allow_zero else value <= 0)
    ):
        raise ValueError(
            f"{name} must be a {'non-negative' if allow_zero else 'positive'} integer"
        )
    return value


def _parse_non_negative_int_header(headers: Any, name: str) -> int | None:
    raw = _header_value(headers, name)
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped or re.fullmatch(r"\d+", stripped) is None:
        return None
    return int(stripped)


def _parse_non_empty_header(headers: Any, name: str) -> str | None:
    raw = _header_value(headers, name)
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_retry_after(
    headers: Any, *, now_epoch: float | None = None
) -> tuple[float | None, bool]:
    """Parse ``Retry-After`` as ``(seconds, counts_as_403_rate_limit_signal)``."""

    raw = _header_value(headers, "Retry-After")
    if not isinstance(raw, str):
        return None, False
    stripped = raw.strip()
    if not stripped:
        return None, False
    if re.fullmatch(r"-?\d+(?:\.\d+)?", stripped) is not None:
        seconds = float(stripped)
        if not math.isfinite(seconds) or seconds < 0:
            return None, False
        return seconds, seconds > 0
    try:
        dt = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None, False
    if dt is None:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = time.time() if now_epoch is None else float(now_epoch)
    return max(0.0, dt.timestamp() - now), True


def _parse_iso8601_epoch(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.endswith("Z"):
        stripped = stripped[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(stripped)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = int(dt.timestamp())
    return epoch if epoch >= 0 else None


def _parse_link_next_url(headers: Any) -> str | None:
    link_header = _parse_non_empty_header(headers, "Link")
    if link_header is None:
        return None
    for match in _LINK_SEGMENT_RE.finditer(link_header):
        url, params = match.group(1), match.group(2)
        rel_match = _LINK_REL_RE.search(params)
        if rel_match is None:
            continue
        rel_value = rel_match.group(1) or rel_match.group(2) or rel_match.group(3) or ""
        if "next" in rel_value.lower().split():
            return url
    return None


def _normalize_body(body: Any) -> tuple[Any, str]:
    parsed = body
    raw_text = ""
    if isinstance(body, (bytes, bytearray)):
        raw_text = bytes(body).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw_text)
        except ValueError:
            parsed = None
    elif isinstance(body, str):
        raw_text = body
        stripped = body.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
    if isinstance(parsed, Mapping) and isinstance(parsed.get("message"), str):
        return parsed, parsed["message"].lower()
    return parsed, raw_text.lower()


def _extract_graphql_rate_limit(
    parsed_body: Any,
) -> tuple[int | None, int | None, int | None, int | None]:
    if not isinstance(parsed_body, Mapping):
        return None, None, None, None
    data = parsed_body.get("data")
    if not isinstance(data, Mapping):
        return None, None, None, None
    rate_limit = data.get("rateLimit")
    if not isinstance(rate_limit, Mapping):
        return None, None, None, None

    raw_limit = rate_limit.get("limit")
    limit = (
        raw_limit
        if isinstance(raw_limit, int)
        and not isinstance(raw_limit, bool)
        and raw_limit > 0
        else None
    )
    raw_remaining = rate_limit.get("remaining")
    remaining = (
        raw_remaining
        if isinstance(raw_remaining, int)
        and not isinstance(raw_remaining, bool)
        and raw_remaining >= 0
        else None
    )
    reset_epoch = _parse_iso8601_epoch(rate_limit.get("resetAt"))
    raw_cost = rate_limit.get("cost")
    cost = (
        raw_cost
        if isinstance(raw_cost, int)
        and not isinstance(raw_cost, bool)
        and raw_cost >= 0
        else None
    )
    return limit, remaining, reset_epoch, cost


def _classify_rest_rate_limit_signal(
    status: int | None,
    *,
    remaining: int | None,
    has_retry_after_signal: bool,
    lower_message: str,
) -> str | None:
    """Return ``'secondary'`` or ``'primary'`` when REST 403/429 signals throttling."""

    if status not in (403, 429):
        return None
    has_secondary_message = any(
        phrase in lower_message for phrase in _SECONDARY_RATE_LIMIT_PHRASES
    )
    if has_secondary_message or has_retry_after_signal:
        return "secondary"

    has_zero_remaining = remaining == 0
    has_positive_remaining = remaining is not None and remaining > 0
    has_primary_message = any(
        phrase in lower_message for phrase in _PRIMARY_RATE_LIMIT_PHRASES
    )
    if has_zero_remaining or (not has_positive_remaining and has_primary_message):
        return "primary"

    if status == 429:
        return "secondary"
    return None


def _raise_graphql_non_rate_limit_error(
    errors: Any,
    *,
    credential_kind: CredentialKind,
    endpoint_id: str,
) -> None:
    """Raise a typed permission or HTTP error when GraphQL HTTP 200 has ``errors``."""

    if isinstance(errors, list):
        for entry in errors:
            if not isinstance(entry, Mapping):
                continue
            err_type = entry.get("type")
            if (
                isinstance(err_type, str)
                and err_type.strip().upper() in _GRAPHQL_PERMISSION_ERROR_TYPES
            ):
                raise GitHubHTTPPermissionError(
                    f"GitHub GraphQL permission denied for {endpoint_id!r} using "
                    f"{credential_kind.value}",
                    http_status=200,
                    failed_lookup=endpoint_id,
                    endpoint_id=endpoint_id,
                    credential_kind=credential_kind.value,
                )
            err_msg = entry.get("message")
            if isinstance(err_msg, str):
                lower_msg = err_msg.lower()
                if any(phrase in lower_msg for phrase in _GRAPHQL_PERMISSION_PHRASES):
                    raise GitHubHTTPPermissionError(
                        f"GitHub GraphQL permission denied for {endpoint_id!r} using "
                        f"{credential_kind.value}",
                        http_status=200,
                        failed_lookup=endpoint_id,
                        endpoint_id=endpoint_id,
                        credential_kind=credential_kind.value,
                    )
    raise GitHubHTTPError(
        f"GitHub GraphQL query returned errors for {endpoint_id!r}",
        http_status=200,
        failed_lookup=endpoint_id,
    )


def _detect_graphql_rate_limit(
    parsed_body: Any,
) -> tuple[bool, int | None, int | None, str]:
    if not isinstance(parsed_body, Mapping):
        return False, None, None, ""
    _, gql_remaining, gql_reset_epoch, _ = _extract_graphql_rate_limit(parsed_body)
    errors = parsed_body.get("errors")
    has_errors = isinstance(errors, list) and len(errors) > 0
    if isinstance(errors, list):
        for entry in errors:
            if not isinstance(entry, Mapping):
                continue
            err_type = entry.get("type")
            if isinstance(err_type, str) and err_type.strip().upper() == "RATE_LIMITED":
                return (
                    True,
                    gql_remaining,
                    gql_reset_epoch,
                    "graphql_rate_limited_error",
                )
            err_msg = entry.get("message")
            if isinstance(err_msg, str):
                lower_msg = err_msg.lower()
                if any(phrase in lower_msg for phrase in _GRAPHQL_RATE_LIMIT_PHRASES):
                    return (
                        True,
                        gql_remaining,
                        gql_reset_epoch,
                        "graphql_rate_limit_message",
                    )

    data = parsed_body.get("data")
    has_null_target_data = data is None or (
        isinstance(data, Mapping)
        and any(key != "rateLimit" and value is None for key, value in data.items())
    )
    if gql_remaining == 0 and (has_errors or has_null_target_data):
        return True, gql_remaining, gql_reset_epoch, "graphql_zero_remaining"
    return False, gql_remaining, gql_reset_epoch, ""


@dataclass(frozen=True)
class RateLimitSnapshot:
    """Point-in-time quota state observed for one GitHub credential channel."""

    credential_kind: CredentialKind
    limit: int
    remaining: int | None = None
    reset_epoch: int | None = None
    resource: str | None = None
    retry_after_seconds: float | None = None
    last_graphql_cost: int | None = None
    observed_requests: int = 0
    rate_limited: bool = False


class CredentialQuotaTracker:
    """Tracks rate-limit quota independently for one GitHub credential channel."""

    def __init__(
        self,
        credential_kind: CredentialKind,
        *,
        is_ghec: bool = False,
        default_limit: int | None = None,
    ) -> None:
        if not isinstance(credential_kind, CredentialKind):
            raise ValueError(f"invalid credential_kind: {credential_kind!r}")
        effective_default = (
            default_quota_limit(credential_kind, is_ghec=is_ghec)
            if default_limit is None
            else _validate_non_negative_int(
                default_limit, name="default_limit", allow_zero=False
            )
        )
        self.credential_kind = credential_kind
        self.is_ghec = bool(is_ghec)
        self.default_limit = effective_default
        self._limit = effective_default
        self._remaining: int | None = None
        self._reset_epoch: int | None = None
        self._resource: str | None = None
        self._retry_after_seconds: float | None = None
        self._last_graphql_cost: int | None = None
        self._observed_requests = 0
        self._rate_limited = False

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def remaining(self) -> int | None:
        return self._remaining

    @property
    def reset_epoch(self) -> int | None:
        return self._reset_epoch

    @property
    def resource(self) -> str | None:
        return self._resource

    @property
    def retry_after_seconds(self) -> float | None:
        return self._retry_after_seconds

    @property
    def last_graphql_cost(self) -> int | None:
        return self._last_graphql_cost

    @property
    def observed_requests(self) -> int:
        return self._observed_requests

    @property
    def rate_limited(self) -> bool:
        return self._rate_limited

    def snapshot(self) -> RateLimitSnapshot:
        """Return an immutable snapshot of the tracked credential quota."""

        return RateLimitSnapshot(
            credential_kind=self.credential_kind,
            limit=self._limit,
            remaining=self._remaining,
            reset_epoch=self._reset_epoch,
            resource=self._resource,
            retry_after_seconds=self._retry_after_seconds,
            last_graphql_cost=self._last_graphql_cost,
            observed_requests=self._observed_requests,
            rate_limited=self._rate_limited,
        )

    def update_from_response(
        self,
        headers: Mapping[str, str] | Any,
        body: Any = None,
        *,
        status: int | None = None,
        is_graphql: bool = False,
        now_epoch: float | None = None,
    ) -> RateLimitSnapshot:
        """Update quota telemetry from response headers and GraphQL ``rateLimit``."""

        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError(f"invalid status: {status!r}")

        self._observed_requests += 1

        header_limit = _parse_non_negative_int_header(headers, "X-RateLimit-Limit")
        if header_limit is not None and header_limit > 0:
            self._limit = header_limit

        header_remaining = _parse_non_negative_int_header(
            headers, "X-RateLimit-Remaining"
        )
        if header_remaining is not None:
            self._remaining = header_remaining

        header_reset = _parse_non_negative_int_header(headers, "X-RateLimit-Reset")
        if header_reset is not None:
            self._reset_epoch = header_reset

        header_resource = _parse_non_empty_header(headers, "X-RateLimit-Resource")
        if header_resource is not None:
            self._resource = header_resource

        retry_after_seconds, has_403_retry_after = _parse_retry_after(
            headers, now_epoch=now_epoch
        )
        self._retry_after_seconds = retry_after_seconds

        parsed_body, lower_message = _normalize_body(body)
        graphql_rate_limited = False
        if is_graphql:
            gql_limit, gql_remaining, gql_reset, gql_cost = _extract_graphql_rate_limit(
                parsed_body
            )
            if gql_limit is not None:
                self._limit = gql_limit
            if gql_remaining is not None:
                self._remaining = gql_remaining
            if gql_reset is not None:
                self._reset_epoch = gql_reset
            if gql_cost is not None:
                self._last_graphql_cost = gql_cost
            if header_resource is None and (
                gql_limit is not None
                or gql_remaining is not None
                or gql_cost is not None
            ):
                self._resource = "graphql"
            graphql_rate_limited, _, _, _ = _detect_graphql_rate_limit(parsed_body)

        has_retry_after_signal = (
            has_403_retry_after if status == 403 else (retry_after_seconds is not None)
        )
        rest_rate_limit_kind = _classify_rest_rate_limit_signal(
            status,
            remaining=header_remaining,
            has_retry_after_signal=has_retry_after_signal,
            lower_message=lower_message,
        )
        self._rate_limited = (
            self._remaining == 0
            or graphql_rate_limited
            or (rest_rate_limit_kind is not None)
        )
        return self.snapshot()


def classify_github_response(
    status: int,
    headers: Mapping[str, str] | Any,
    body: Any,
    *,
    credential_kind: CredentialKind,
    endpoint_id: str,
    is_graphql: bool = False,
    now_epoch: float | None = None,
) -> None:
    """Classify rate-limit, GraphQL, and permission errors from a GitHub response.

    Raises :class:`GitHubHTTPRateLimitError` for REST primary/secondary rate limits
    and GraphQL HTTP ``200`` rate-limit responses, :class:`GitHubHTTPPermissionError`
    for HTTP ``403`` (or GraphQL ``FORBIDDEN``/``UNAUTHORIZED``) permission denials,
    and :class:`GitHubHTTPError` for non-rate-limit GraphQL HTTP ``200`` errors.
    """

    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError(f"invalid status: {status!r}")
    if not isinstance(credential_kind, CredentialKind):
        raise ValueError(f"invalid credential_kind: {credential_kind!r}")
    if not isinstance(endpoint_id, str) or not endpoint_id.strip():
        raise ValueError(f"invalid endpoint_id: {endpoint_id!r}")

    remaining = _parse_non_negative_int_header(headers, "X-RateLimit-Remaining")
    reset_epoch = _parse_non_negative_int_header(headers, "X-RateLimit-Reset")
    resource = _parse_non_empty_header(headers, "X-RateLimit-Resource")
    retry_after_seconds, has_403_retry_after = _parse_retry_after(
        headers, now_epoch=now_epoch
    )
    parsed_body, lower_message = _normalize_body(body)

    if status == 200 and is_graphql:
        gql_limited, gql_remaining, gql_reset, gql_reason = _detect_graphql_rate_limit(
            parsed_body
        )
        if gql_limited:
            effective_remaining = (
                gql_remaining if gql_remaining is not None else remaining
            )
            effective_reset = gql_reset if gql_reset is not None else reset_epoch
            raise GitHubHTTPRateLimitError(
                f"GitHub GraphQL rate limit triggered for {endpoint_id!r} using "
                f"{credential_kind.value} ({gql_reason})",
                credential_kind=credential_kind.value,
                rate_limit_kind="graphql_200",
                retry_after_seconds=retry_after_seconds,
                reset_epoch=effective_reset,
                remaining=effective_remaining,
                resource=resource or "graphql",
                http_status=200,
                endpoint_id=endpoint_id,
            )
        if isinstance(parsed_body, Mapping):
            errors = parsed_body.get("errors")
            if errors:
                _raise_graphql_non_rate_limit_error(
                    errors,
                    credential_kind=credential_kind,
                    endpoint_id=endpoint_id,
                )
        return

    if status not in (403, 429):
        return

    has_retry_after_signal = (
        has_403_retry_after if status == 403 else (retry_after_seconds is not None)
    )
    rate_limit_kind = _classify_rest_rate_limit_signal(
        status,
        remaining=remaining,
        has_retry_after_signal=has_retry_after_signal,
        lower_message=lower_message,
    )
    if rate_limit_kind is not None:
        raise GitHubHTTPRateLimitError(
            f"GitHub {rate_limit_kind} rate limit triggered (HTTP {status}) for "
            f"{endpoint_id!r} using {credential_kind.value}",
            credential_kind=credential_kind.value,
            rate_limit_kind=rate_limit_kind,
            retry_after_seconds=retry_after_seconds,
            reset_epoch=reset_epoch,
            remaining=remaining,
            resource=resource,
            http_status=status,
            endpoint_id=endpoint_id,
        )

    raise GitHubHTTPPermissionError(
        f"GitHub permission denied (HTTP 403) for {endpoint_id!r} using "
        f"{credential_kind.value}",
        http_status=403,
        failed_lookup=endpoint_id,
        endpoint_id=endpoint_id,
        credential_kind=credential_kind.value,
    )


@dataclass(frozen=True)
class ThrottleDecision:
    """Result of evaluating whether to sleep in-job or pause for maintainer rerun."""

    action: str
    wait_seconds: float
    resume_after_epoch: float
    reason: str
    rerun_command: str | None = None

    def __post_init__(self) -> None:
        if self.action not in _ALLOWED_THROTTLE_ACTIONS:
            raise ValueError(f"invalid throttle action: {self.action!r}")


def evaluate_throttle_wait(
    error: GitHubHTTPRateLimitError,
    *,
    max_in_job_sleep_seconds: float = DEFAULT_MAX_IN_JOB_SLEEP_SECONDS,
    remaining_job_seconds: float,
    attempt: int = 1,
    now_epoch: float | None = None,
    base_backoff_seconds: float = 2.0,
    max_backoff_seconds: float = 60.0,
    jitter_seconds: float = 0.0,
    checkpoint_safety_margin_seconds: float = DEFAULT_CHECKPOINT_SAFETY_MARGIN_SECONDS,
) -> ThrottleDecision:
    """Choose bounded in-job sleep vs. clean v6.0 maintainer-rerun pause."""

    if not isinstance(error, GitHubHTTPRateLimitError):
        raise TypeError("error must be a GitHubHTTPRateLimitError")
    max_sleep = _validate_non_negative_finite(
        max_in_job_sleep_seconds, name="max_in_job_sleep_seconds"
    )
    remaining_job = _validate_non_negative_finite(
        remaining_job_seconds, name="remaining_job_seconds"
    )
    validated_attempt = _validate_non_negative_int(
        attempt, name="attempt", allow_zero=False
    )
    now = (
        time.time()
        if now_epoch is None
        else _validate_non_negative_finite(now_epoch, name="now_epoch")
    )
    base_backoff = _validate_non_negative_finite(
        base_backoff_seconds, name="base_backoff_seconds", allow_zero=False
    )
    max_backoff = _validate_non_negative_finite(
        max_backoff_seconds, name="max_backoff_seconds", allow_zero=False
    )
    if max_backoff < base_backoff:
        raise ValueError("max_backoff_seconds must be >= base_backoff_seconds")
    jitter = _validate_non_negative_finite(jitter_seconds, name="jitter_seconds")
    safety_margin = _validate_non_negative_finite(
        checkpoint_safety_margin_seconds, name="checkpoint_safety_margin_seconds"
    )

    if error.retry_after_seconds is not None and error.retry_after_seconds > 0:
        wait_seconds = float(error.retry_after_seconds)
    elif error.reset_epoch is not None and float(error.reset_epoch) > now:
        wait_seconds = float(error.reset_epoch) - now
    else:
        exp_backoff = min(max_backoff, base_backoff * (2 ** (validated_attempt - 1)))
        wait_seconds = exp_backoff + jitter

    resume_after_epoch = now + wait_seconds

    if wait_seconds <= max_sleep and (wait_seconds + safety_margin) < remaining_job:
        return ThrottleDecision(
            action="sleep",
            wait_seconds=wait_seconds,
            resume_after_epoch=resume_after_epoch,
            reason=f"rate_limit_{error.rate_limit_kind}_in_job_sleep",
            rerun_command=None,
        )

    reason = (
        "rate_limit_wait_exceeds_in_job_cap"
        if wait_seconds > max_sleep
        else "insufficient_remaining_job_time_for_wait_and_checkpoint"
    )
    return ThrottleDecision(
        action="pause_for_maintainer_rerun",
        wait_seconds=wait_seconds,
        resume_after_epoch=resume_after_epoch,
        rerun_command=MAINTAINER_RERUN_COMMAND,
        reason=reason,
    )


class DualCredentialTransport(Protocol):
    """Protocol for one credential-bound GitHub HTTP transport."""

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        """Execute one request and return ``(status, headers, parsed_body)``."""


@dataclass(frozen=True)
class DualCredentialResponse:
    """Structured response returned by :class:`DualCredentialGitHubClient`."""

    status: int
    headers: Mapping[str, str]
    body: Any
    link_next_url: str | None
    quota_snapshot: RateLimitSnapshot
    endpoint_id: str
    credential_kind: CredentialKind


class DualCredentialGitHubClient:
    """Executes v6 GitHub requests across isolated workflow and broker transports."""

    def __init__(
        self,
        *,
        workflow_token_transport: DualCredentialTransport,
        broker_app_transport: DualCredentialTransport,
        workflow_quota: CredentialQuotaTracker | None = None,
        app_quota: CredentialQuotaTracker | None = None,
        app_checkpoint_reserve: int = DEFAULT_APP_CHECKPOINT_RESERVE,
        is_ghec: bool = False,
    ) -> None:
        if not callable(getattr(workflow_token_transport, "request", None)):
            raise ValueError("workflow_token_transport must provide request()")
        if not callable(getattr(broker_app_transport, "request", None)):
            raise ValueError("broker_app_transport must provide request()")
        if workflow_quota is None:
            workflow_quota = CredentialQuotaTracker(
                CredentialKind.WORKFLOW_GITHUB_TOKEN, is_ghec=is_ghec
            )
        elif workflow_quota.credential_kind != CredentialKind.WORKFLOW_GITHUB_TOKEN:
            raise ValueError("workflow_quota must track WORKFLOW_GITHUB_TOKEN")
        if app_quota is None:
            app_quota = CredentialQuotaTracker(CredentialKind.APP_BROKER)
        elif app_quota.credential_kind != CredentialKind.APP_BROKER:
            raise ValueError("app_quota must track APP_BROKER")

        self.workflow_token_transport = workflow_token_transport
        self.broker_app_transport = broker_app_transport
        self.workflow_quota = workflow_quota
        self.app_quota = app_quota
        self.app_checkpoint_reserve = _validate_non_negative_int(
            app_checkpoint_reserve, name="app_checkpoint_reserve"
        )

    def execute(
        self,
        endpoint_id: str,
        *,
        method: str,
        path: str,
        event: GitHubEventKind,
        visibility: RepositoryVisibility,
        granted_scopes: Collection[str],
        credential_kind: CredentialKind | None = None,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        is_graphql: bool = False,
        timeout_seconds: float | None = None,
        allow_checkpoint_reserve_override: bool = False,
        now_epoch: float | None = None,
    ) -> DualCredentialResponse:
        """Authorize, dispatch, track quota, and classify a v6 GitHub API call."""

        rule = get_v6_endpoint_rule(endpoint_id)
        selected_credential = (
            rule.credential_kind if credential_kind is None else credential_kind
        )
        authorized_rule = authorize_v6_endpoint_credential(
            endpoint_id,
            selected_credential,
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
        )

        if not isinstance(method, str) or method not in _ALLOWED_HTTP_METHODS:
            raise ReviewInputError(
                f"invalid HTTP method {method!r} for endpoint {endpoint_id!r}",
                diagnostic="v6_invalid_method",
            )
        if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
            raise ReviewInputError(
                f"invalid request path {path!r} for endpoint {endpoint_id!r}",
                diagnostic="v6_invalid_path",
            )

        if (
            authorized_rule.credential_kind == CredentialKind.APP_BROKER
            and not allow_checkpoint_reserve_override
            and self.app_quota.remaining is not None
            and self.app_quota.remaining <= self.app_checkpoint_reserve
        ):
            raise GitHubHTTPRateLimitError(
                f"app_broker remaining quota ({self.app_quota.remaining}) is within "
                f"checkpoint reserve ({self.app_checkpoint_reserve}); refusing "
                f"non-checkpoint operation {endpoint_id!r}",
                credential_kind=CredentialKind.APP_BROKER.value,
                rate_limit_kind="primary",
                remaining=self.app_quota.remaining,
                reset_epoch=self.app_quota.reset_epoch,
                resource=self.app_quota.resource or "core",
                http_status=None,
                endpoint_id=endpoint_id,
            )

        if authorized_rule.credential_kind == CredentialKind.WORKFLOW_GITHUB_TOKEN:
            transport = self.workflow_token_transport
            tracker = self.workflow_quota
        else:
            transport = self.broker_app_transport
            tracker = self.app_quota

        status, response_headers, response_body = transport.request(
            method,
            path,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        snapshot = tracker.update_from_response(
            response_headers,
            response_body,
            status=status,
            is_graphql=is_graphql,
            now_epoch=now_epoch,
        )
        classify_github_response(
            status,
            response_headers,
            response_body,
            credential_kind=authorized_rule.credential_kind,
            endpoint_id=endpoint_id,
            is_graphql=is_graphql,
            now_epoch=now_epoch,
        )

        copied_headers = (
            dict(response_headers.items()) if hasattr(response_headers, "items") else {}
        )
        return DualCredentialResponse(
            status=status,
            headers=MappingProxyType(copied_headers),
            body=response_body,
            link_next_url=_parse_link_next_url(response_headers),
            quota_snapshot=snapshot,
            endpoint_id=endpoint_id,
            credential_kind=authorized_rule.credential_kind,
        )
