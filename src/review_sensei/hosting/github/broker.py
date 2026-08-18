"""OIDC-protected GitHub App installation-token broker."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .auth import (
    DEFAULT_GITHUB_API_URL,
    MAX_HTTP_BODY_BYTES,
    GitHubAppAuth,
    InstallationToken,
    RequestedPermissions,
)
from .errors import (
    BrokerPolicyError,
    BrokerRateLimitError,
    BrokerRejectionError,
    GitHubAuthError,
    GitHubOIDCError,
)
from .oidc import (
    JwksFetcher,
    VerifiedOIDCClaims,
    verify_oidc_token,
)

DEFAULT_BROKER_AUDIENCE = "sts.reviewsensei.dev"
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_RATE_LIMIT_MAX_REQUESTS = 10
MAX_AUDIT_RECORDS_IN_MEMORY = 10_000
Opener = Callable[..., Any]


@dataclass(frozen=True)
class ApprovedWorkflow:
    """One repository workflow identity allowed to request a token."""

    repository: str
    workflow_ref: str
    ref: str


@dataclass(frozen=True)
class BrokerPolicy:
    """Configuration for OIDC verification and broker authorization."""

    expected_issuer: str
    expected_audience: str
    approved_workflows: frozenset[ApprovedWorkflow]
    requested_permissions: RequestedPermissions
    clock_skew_seconds: int
    rate_limit_window_seconds: int
    rate_limit_max_requests: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.expected_issuer, str)
            or not self.expected_issuer.strip()
        ):
            raise BrokerPolicyError("Broker issuer must be non-empty")
        if (
            not isinstance(self.expected_audience, str)
            or not self.expected_audience.strip()
        ):
            raise BrokerPolicyError("Broker audience must be non-empty")
        if not isinstance(self.approved_workflows, frozenset):
            raise BrokerPolicyError("Broker approved workflows are invalid")
        for workflow in self.approved_workflows:
            if not isinstance(workflow, ApprovedWorkflow):
                raise BrokerPolicyError("Broker approved workflows are invalid")
            if (
                not isinstance(workflow.repository, str)
                or not isinstance(workflow.workflow_ref, str)
                or not isinstance(workflow.ref, str)
                or not workflow.repository.strip()
                or not workflow.workflow_ref.strip()
                or not workflow.ref.strip()
            ):
                raise BrokerPolicyError("Broker approved workflows are invalid")
        if not isinstance(self.requested_permissions, RequestedPermissions):
            raise BrokerPolicyError("Broker requested permissions are invalid")
        if (
            isinstance(self.clock_skew_seconds, bool)
            or not isinstance(self.clock_skew_seconds, int)
            or self.clock_skew_seconds < 0
        ):
            raise BrokerPolicyError("Broker clock skew must be non-negative")
        if (
            isinstance(self.rate_limit_window_seconds, bool)
            or not isinstance(self.rate_limit_window_seconds, int)
            or self.rate_limit_window_seconds <= 0
        ):
            raise BrokerPolicyError("Broker rate limit window must be positive")
        if (
            isinstance(self.rate_limit_max_requests, bool)
            or not isinstance(self.rate_limit_max_requests, int)
            or self.rate_limit_max_requests <= 0
        ):
            raise BrokerPolicyError("Broker rate limit maximum must be positive")


@dataclass(frozen=True)
class AuditRecord:
    """Non-secret metadata describing one broker exchange verdict."""

    timestamp: float
    request_id: str
    installation_id: int | None
    repository: str | None
    workflow_ref: str | None
    event: str | None
    verdict: str
    reason_category: str


class InstallationMapping(Protocol):
    """Resolve a repository owner/name to its installed App id."""

    def resolve(self, owner: str, repo: str) -> int | None:
        """Return the installation id or ``None`` when no mapping exists."""


class AuditSink(Protocol):
    """Receive non-secret broker audit records."""

    def record(self, record: AuditRecord) -> None:
        """Store or forward one audit record."""


class RateLimiter(Protocol):
    """Decide whether one scoped exchange is within its request limit."""

    def allow(self, key: tuple[int, str, str], now: float) -> bool:
        """Return whether a request may proceed."""


class ForkChecker(Protocol):
    """Determine whether a repository is a fork."""

    def is_fork(self, owner: str, repo: str) -> bool:
        """Return true for forks; implementations fail closed on errors."""


class InMemoryInstallationMapping:
    """Dictionary-backed repository-to-installation mapping."""

    def __init__(self, mapping: Mapping[str, int] | None = None) -> None:
        self.mapping = dict(mapping or {})

    def resolve(self, owner: str, repo: str) -> int | None:
        """Return the configured installation id for one repository."""

        value = self.mapping.get(f"{owner}/{repo}")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value


class InMemoryAuditSink:
    """Thread-safe bounded in-memory audit record sink."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self._lock = threading.RLock()

    def record(self, record: AuditRecord) -> None:
        """Append a record and evict the oldest records over the bound."""

        with self._lock:
            self.records.append(record)
            if len(self.records) > MAX_AUDIT_RECORDS_IN_MEMORY:
                del self.records[: len(self.records) - MAX_AUDIT_RECORDS_IN_MEMORY]


class TokenBucketRateLimiter:
    """Thread-safe fixed-window request limiter."""

    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        max_requests: int = DEFAULT_RATE_LIMIT_MAX_REQUESTS,
    ) -> None:
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or window_seconds <= 0
        ):
            raise BrokerPolicyError("Broker rate limit window must be positive")
        if (
            isinstance(max_requests, bool)
            or not isinstance(max_requests, int)
            or max_requests <= 0
        ):
            raise BrokerPolicyError("Broker rate limit maximum must be positive")
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._requests: dict[tuple[int, str, str], list[float]] = {}
        self._lock = threading.RLock()

    def allow(self, key: tuple[int, str, str], now: float) -> bool:
        """Allow up to the configured number of requests in one window."""

        with self._lock:
            timestamps = self._requests.setdefault(key, [])
            cutoff = now - self.window_seconds
            timestamps[:] = [
                timestamp for timestamp in timestamps if timestamp >= cutoff
            ]
            if len(timestamps) >= self.max_requests:
                return False
            timestamps.append(now)
            return True


def _read_json_response(response: Any) -> dict[str, Any]:
    body = response.read(MAX_HTTP_BODY_BYTES + 1)
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_HTTP_BODY_BYTES:
        raise ValueError
    parsed = json.loads(bytes(body).decode("utf-8", errors="strict"))
    if not isinstance(parsed, dict):
        raise ValueError
    return parsed


def _repository_url(api_url: str, owner: str, repo: str, suffix: str) -> str:
    safe_owner = quote(owner, safe="")
    safe_repo = quote(repo, safe="")
    base = f"{api_url.rstrip('/')}/repos/{safe_owner}/{safe_repo}"
    return f"{base}/{suffix}" if suffix else base


class GitHubInstallationLookup:
    """Resolve installation ids through the GitHub App installation endpoint."""

    def __init__(
        self,
        *,
        auth: GitHubAppAuth,
        api_url: str = DEFAULT_GITHUB_API_URL,
        opener: Opener = urlopen,
    ) -> None:
        if not isinstance(api_url, str) or not api_url.strip():
            raise BrokerPolicyError("GitHub API URL must be non-empty")
        self.auth = auth
        self.api_url = api_url.rstrip("/")
        self.opener = opener

    def resolve(self, owner: str, repo: str) -> int | None:
        """Return the GitHub installation id, or none on any transport error."""

        try:
            app_jwt = self.auth.create_app_jwt()
            request = Request(
                _repository_url(self.api_url, owner, repo, "installation"),
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="GET",
            )
            with self.opener(request, timeout=30) as response:
                payload = _read_json_response(response)
            installation_id = payload.get("id")
            if (
                isinstance(installation_id, bool)
                or not isinstance(installation_id, int)
                or installation_id <= 0
            ):
                return None
            return installation_id
        except Exception:
            return None


class GitHubForkChecker:
    """Check repository fork status using an authenticated GitHub App request.

    The fork lookup is authenticated with the GitHub App JWT so private
    repositories the App is installed on are reachable. A 404/403 after
    authentication means the App is not installed on that repository, so the
    lookup returns ``False`` (not a fork) and the downstream installation
    mapping and approved-workflow policy reject the request. Genuine transport
    failures are treated as ``True`` (fork) so the broker fails closed rather
    than minting against an unverifiable repository.
    """

    def __init__(
        self,
        *,
        auth: GitHubAppAuth,
        api_url: str = DEFAULT_GITHUB_API_URL,
        opener: Opener = urlopen,
    ) -> None:
        if not isinstance(api_url, str) or not api_url.strip():
            raise BrokerPolicyError("GitHub API URL must be non-empty")
        self.auth = auth
        self.api_url = api_url.rstrip("/")
        self.opener = opener

    def is_fork(self, owner: str, repo: str) -> bool:
        """Return fork status for an authenticated App repository lookup."""

        try:
            app_jwt = self.auth.create_app_jwt()
            request = Request(
                _repository_url(self.api_url, owner, repo, ""),
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="GET",
            )
            with self.opener(request, timeout=30) as response:
                payload = _read_json_response(response)
            fork = payload.get("fork")
            return fork if isinstance(fork, bool) else True
        except HTTPError as exc:
            # 403/404 after App authentication means the App is not installed
            # on that repository: treat as not-a-fork so the installation
            # mapping and approved-workflow policy reject it explicitly.
            if exc.code in (403, 404):
                return False
            return True
        except Exception:
            return True


def _system_clock() -> float:
    return time.time()


def _default_request_id() -> str:
    return uuid.uuid4().hex


class OIDCBroker:
    """Verify an OIDC workflow and issue a scoped installation token."""

    def __init__(
        self,
        *,
        auth: GitHubAppAuth,
        policy: BrokerPolicy,
        jwks_fetcher: JwksFetcher,
        installation_mapping: InstallationMapping,
        fork_checker: ForkChecker,
        rate_limiter: RateLimiter | None = None,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], float] = _system_clock,
        request_id_factory: Callable[[], str] = _default_request_id,
    ) -> None:
        self.auth = auth
        self.policy = policy
        self.jwks_fetcher = jwks_fetcher
        self.installation_mapping = installation_mapping
        self.fork_checker = fork_checker
        self.rate_limiter = rate_limiter
        self.audit_sink = audit_sink
        self.clock = clock
        self.request_id_factory = request_id_factory

    def exchange(self, oidc_token: str) -> InstallationToken:
        """Verify, authorize, and exchange one workflow OIDC token."""

        now = self.clock()
        request_id = self.request_id_factory()
        try:
            claims = verify_oidc_token(
                oidc_token,
                expected_issuer=self.policy.expected_issuer,
                expected_audience=self.policy.expected_audience,
                jwks_fetcher=self.jwks_fetcher,
                now=now,
                clock_skew_seconds=self.policy.clock_skew_seconds,
            )
        except GitHubOIDCError as exc:
            self._audit(
                request_id,
                None,
                None,
                None,
                None,
                "rejected",
                exc.error_category,
            )
            raise

        if not self._is_approved(claims):
            self._audit(
                request_id,
                claims.installation_id,
                claims.repository,
                claims.workflow_ref,
                claims.event,
                "rejected",
                "broker_rejection",
            )
            raise BrokerRejectionError("OIDC workflow identity is not approved")

        owner, _, repo = claims.repository.partition("/")
        try:
            is_fork = self.fork_checker.is_fork(owner, repo)
        except Exception:
            is_fork = True
        if is_fork:
            self._audit(
                request_id,
                claims.installation_id,
                claims.repository,
                claims.workflow_ref,
                claims.event,
                "rejected",
                "broker_rejection",
            )
            raise BrokerRejectionError("OIDC repository is a fork")

        try:
            installation_id = self.installation_mapping.resolve(owner, repo)
        except Exception:
            installation_id = None
        if installation_id is None:
            self._audit(
                request_id,
                None,
                claims.repository,
                claims.workflow_ref,
                claims.event,
                "rejected",
                "broker_rejection",
            )
            raise BrokerRejectionError("OIDC repository has no installation mapping")
        if installation_id != claims.installation_id:
            self._audit(
                request_id,
                installation_id,
                claims.repository,
                claims.workflow_ref,
                claims.event,
                "rejected",
                "broker_rejection",
            )
            raise BrokerRejectionError("OIDC installation id did not match the mapping")

        if self.rate_limiter is not None:
            key = (installation_id, claims.repository, claims.workflow_ref)
            if not self.rate_limiter.allow(key, now):
                self._audit(
                    request_id,
                    installation_id,
                    claims.repository,
                    claims.workflow_ref,
                    claims.event,
                    "rejected",
                    "broker_rate_limit",
                )
                raise BrokerRateLimitError("Broker rate limit exceeded")

        try:
            token = self.auth.installation_token(
                installation_id=installation_id,
                repository=claims.repository,
                requested_permissions=self.policy.requested_permissions,
            )
        except GitHubAuthError as exc:
            self._audit(
                request_id,
                installation_id,
                claims.repository,
                claims.workflow_ref,
                claims.event,
                "rejected",
                exc.error_category,
            )
            raise

        self._audit(
            request_id,
            installation_id,
            claims.repository,
            claims.workflow_ref,
            claims.event,
            "issued",
            "ok",
        )
        return token

    def _is_approved(self, claims: VerifiedOIDCClaims) -> bool:
        return any(
            workflow.repository == claims.repository
            and workflow.workflow_ref == claims.workflow_ref
            and (workflow.ref == claims.ref or workflow.ref == "*")
            for workflow in self.policy.approved_workflows
        )

    def _audit(
        self,
        request_id: str,
        installation_id: int | None,
        repository: str | None,
        workflow_ref: str | None,
        event: str | None,
        verdict: str,
        reason_category: str,
    ) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink.record(
            AuditRecord(
                timestamp=self.clock(),
                request_id=request_id,
                installation_id=installation_id,
                repository=repository,
                workflow_ref=workflow_ref,
                event=event,
                verdict=verdict,
                reason_category=reason_category,
            )
        )
