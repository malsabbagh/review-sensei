"""GitHub App JWT and installation-token authentication adapter."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import (
    GitHubAuthConfigurationError,
    GitHubAuthError,
    GitHubAuthInsufficientPermissionError,
    GitHubAuthTransientError,
    GitHubAuthUnavailableInstallationError,
)
from .jwt import jwt_encode_rs256

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_JWT_LIFETIME_SECONDS = 9 * 60
DEFAULT_MAX_JWT_LIFETIME_SECONDS = 10 * 60
DEFAULT_CLOCK_SKEW_SECONDS = 30
DEFAULT_TOKEN_REFRESH_SECONDS = 5 * 60
MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_HTTP_BODY_BYTES = 256 * 1024
ALLOWED_PERMISSIONS = frozenset(
    {
        "actions",
        "administration",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "issues",
        "members",
        "metadata",
        "organization_administration",
        "organization_hooks",
        "organization_packages",
        "organization_plan",
        "organization_projects",
        "organization_secrets",
        "organization_self_hosted_runners",
        "organization_user_blocking",
        "packages",
        "pages",
        "pull_requests",
        "repository_hooks",
        "repository_projects",
        "secret_scanning_alerts",
        "secrets",
        "security_events",
        "single_file",
        "statuses",
        "team_discussions",
        "vulnerability_alerts",
        "workflows",
    }
)


class Clock(Protocol):
    def __call__(self) -> float:
        """Return the current Unix time in seconds."""


def _system_clock() -> float:
    return time.time()


class PrivateKeySource(Protocol):
    def load_private_key_pem(self) -> bytes:
        """Return the GitHub App private key as PEM bytes."""


class FilePrivateKeySource:
    """Load a private key from a PEM file."""

    def __init__(self, path: str | Path) -> None:
        if not str(path).strip():
            raise GitHubAuthConfigurationError(
                "GitHub App private key path must be non-empty"
            )
        self.path = Path(path)

    def load_private_key_pem(self) -> bytes:
        try:
            with self.path.open("rb") as stream:
                content = stream.read(MAX_PRIVATE_KEY_BYTES + 1)
        except OSError as exc:
            raise GitHubAuthConfigurationError(
                "GitHub App private key could not be read"
            ) from exc
        if len(content) > MAX_PRIVATE_KEY_BYTES:
            raise GitHubAuthConfigurationError(
                "GitHub App private key exceeds the configured size limit"
            )
        return content


class EnvPrivateKeySource:
    """Load a private key from an environment variable."""

    def __init__(self, variable: str = "GITHUB_APP_PRIVATE_KEY") -> None:
        if not variable.strip():
            raise GitHubAuthConfigurationError(
                "GitHub App private key environment variable must be non-empty"
            )
        self.variable = variable

    def load_private_key_pem(self) -> bytes:
        value = os.environ.get(self.variable)
        if value is None:
            raise GitHubAuthConfigurationError(f"{self.variable} is not set")
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_PRIVATE_KEY_BYTES:
            raise GitHubAuthConfigurationError(
                "GitHub App private key exceeds the configured size limit"
            )
        return encoded


@dataclass(frozen=True)
class RequestedPermissions:
    """Requested installation permissions, validated before transport."""

    values: frozenset[str]

    def __init__(self, values: frozenset[str] | set[str] | tuple[str, ...]) -> None:
        normalized = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise GitHubAuthConfigurationError(
                    "GitHub permission names must be non-empty strings"
                )
            canonical = value.strip().lower().replace("-", "_")
            if canonical not in ALLOWED_PERMISSIONS:
                raise GitHubAuthConfigurationError(
                    "GitHub App requested an unsupported permission"
                )
            normalized.add(canonical)
        object.__setattr__(self, "values", frozenset(normalized))

    @property
    def as_dict(self) -> dict[str, str]:
        return {permission: "write" for permission in sorted(self.values)}

    def are_subset_of(self, granted_permissions: dict[str, str]) -> bool:
        """Return whether every requested permission is granted as write."""

        if not isinstance(granted_permissions, dict):
            return False
        normalized_granted = {
            str(key).strip().lower().replace("-", "_"): str(value).strip().lower()
            for key, value in granted_permissions.items()
        }
        return all(
            permission in normalized_granted
            and normalized_granted[permission] == "write"
            for permission in self.values
        )


@dataclass(frozen=True)
class InstallationToken:
    """An opaque GitHub installation access token and its expiry."""

    token: str
    expires_at: datetime
    permissions: dict[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise GitHubAuthError("GitHub installation token was empty")
        if not isinstance(self.expires_at, datetime):
            raise GitHubAuthError("GitHub installation token expiry was invalid")
        if not isinstance(self.permissions, dict):
            raise GitHubAuthError("GitHub installation token permissions were invalid")

    @property
    def expires_at_epoch(self) -> float:
        return self.expires_at.timestamp()

    def remaining_seconds(self, now: float) -> float:
        return self.expires_at_epoch - now


AppInstallationToken = InstallationToken


@dataclass(frozen=True)
class InstallationTokenScope:
    """Cache key for one installation token request."""

    installation_id: int
    repository: str | None
    permissions: frozenset[str]

    def __post_init__(self) -> None:
        if isinstance(self.installation_id, bool) or not isinstance(
            self.installation_id, int
        ):
            raise GitHubAuthConfigurationError(
                "GitHub installation id must be a positive integer"
            )
        if self.installation_id <= 0:
            raise GitHubAuthConfigurationError(
                "GitHub installation id must be a positive integer"
            )
        if self.repository is not None:
            if not isinstance(self.repository, str):
                raise GitHubAuthConfigurationError(
                    "GitHub repository must be an owner/repo slug"
                )
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
                raise GitHubAuthConfigurationError(
                    "GitHub repository must be an owner/repo slug"
                )
        object.__setattr__(
            self,
            "permissions",
            frozenset(sorted(permission.lower() for permission in self.permissions)),
        )


@dataclass(frozen=True)
class _CachedToken:
    token: InstallationToken
    expires_at: float


Opener = Callable[..., Any]


class GitHubTokenTransport:
    """HTTP transport for GitHub App installation-token exchange."""

    def __init__(
        self,
        *,
        api_url: str = DEFAULT_GITHUB_API_URL,
        opener: Opener = urlopen,
    ) -> None:
        if not api_url.strip():
            raise GitHubAuthConfigurationError("GitHub API URL must be non-empty")
        self.api_url = api_url.rstrip("/")
        self.opener = opener

    def request_installation_token(
        self,
        *,
        installation_id: int,
        repository: str,
        app_jwt: str,
        requested_permissions: RequestedPermissions,
    ) -> InstallationToken:
        url = f"{self.api_url}/app/installations/{installation_id}/access_tokens"
        body_dict: dict[str, Any] = {
            "permissions": requested_permissions.as_dict,
            "repositories": [repository.rsplit("/", 1)[-1]],
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=30) as response:
                payload = self._read_json_response(response)
        except HTTPError as exc:
            body_text = self._read_bounded_http_error_body(exc)
            raise self._map_http_error(exc.code, body_text) from exc
        except (URLError, TimeoutError) as exc:
            raise GitHubAuthTransientError("GitHub App token request failed") from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise GitHubAuthTransientError(
                    "GitHub App token request timed out"
                ) from exc
            raise GitHubAuthTransientError("GitHub App token request failed") from exc

        return self._parse_installation_token(payload)

    def _read_json_response(self, response: Any) -> dict[str, Any]:
        body = response.read(MAX_HTTP_BODY_BYTES + 1)
        if not isinstance(body, (bytes, bytearray)):
            raise GitHubAuthError("GitHub App token response was invalid")
        if len(body) > MAX_HTTP_BODY_BYTES:
            raise GitHubAuthError(
                "GitHub App token response exceeded the configured size limit"
            )
        try:
            text = bytes(body).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitHubAuthError(
                "GitHub App token response was not valid UTF-8"
            ) from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitHubAuthError("GitHub App token response was invalid JSON") from exc
        if not isinstance(data, dict):
            raise GitHubAuthError("GitHub App token response was invalid")
        return data

    def _read_bounded_http_error_body(self, exc: HTTPError) -> str:
        try:
            body = exc.read(MAX_HTTP_BODY_BYTES + 1)
            if isinstance(body, (bytes, bytearray)):
                return bytes(body)[:MAX_HTTP_BODY_BYTES].decode(
                    "utf-8", errors="replace"
                )
        except OSError:
            pass
        return ""

    def _map_http_error(self, status: int, body_text: str) -> GitHubAuthError:
        lower = body_text.lower()
        if status == 404 or "not found" in lower or "suspended" in lower:
            return GitHubAuthUnavailableInstallationError(
                "GitHub App installation is unavailable"
            )
        if "rate limit" in lower or "abuse detection" in lower:
            return GitHubAuthTransientError(
                "GitHub App token request failed temporarily"
            )
        if status == 403 or "permission" in lower:
            return GitHubAuthInsufficientPermissionError(
                "GitHub App installation lacks the requested permission"
            )
        if "installation" in lower:
            return GitHubAuthUnavailableInstallationError(
                "GitHub App installation is unavailable"
            )
        if status == 401 or status in (400, 422):
            return GitHubAuthConfigurationError(
                "GitHub App authentication configuration failed"
            )
        if status == 429 or status >= 500:
            return GitHubAuthTransientError(
                "GitHub App token request failed temporarily"
            )
        return GitHubAuthTransientError("GitHub App token request failed")

    def _parse_installation_token(self, payload: dict[str, Any]) -> InstallationToken:
        token = payload.get("token")
        expires_at = payload.get("expires_at")
        permissions = payload.get("permissions")
        if not isinstance(token, str) or not token.strip():
            raise GitHubAuthError("GitHub App token response did not include a token")
        if not isinstance(expires_at, str):
            raise GitHubAuthError(
                "GitHub App token response did not include a valid expiry"
            )
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubAuthError(
                "GitHub App token response did not include a valid expiry"
            ) from exc
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if not isinstance(permissions, dict):
            permissions = {}
        return InstallationToken(
            token=token,
            expires_at=expires,
            permissions={str(key): str(value) for key, value in permissions.items()},
        )


class GitHubAppAuth:
    """Issue GitHub App JWTs and installation-scoped access tokens."""

    def __init__(
        self,
        *,
        app_id: int,
        private_key_source: PrivateKeySource,
        transport: GitHubTokenTransport | None = None,
        clock: Clock = _system_clock,
        jwt_lifetime_seconds: int = DEFAULT_JWT_LIFETIME_SECONDS,
        clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
        token_refresh_seconds: int = DEFAULT_TOKEN_REFRESH_SECONDS,
        cache_enabled: bool = True,
    ) -> None:
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise GitHubAuthConfigurationError(
                "GitHub App id must be a positive integer"
            )
        self.app_id = app_id
        self.private_key_source = private_key_source
        self.transport = transport or GitHubTokenTransport()
        self.clock = clock
        if (
            isinstance(jwt_lifetime_seconds, bool)
            or not isinstance(jwt_lifetime_seconds, int)
            or jwt_lifetime_seconds <= 0
            or jwt_lifetime_seconds > DEFAULT_MAX_JWT_LIFETIME_SECONDS
        ):
            raise GitHubAuthConfigurationError(
                "GitHub App JWT lifetime must be a positive integer "
                "no greater than 600 seconds"
            )
        self.jwt_lifetime_seconds = jwt_lifetime_seconds
        if (
            isinstance(clock_skew_seconds, bool)
            or not isinstance(clock_skew_seconds, int)
            or clock_skew_seconds < 0
        ):
            raise GitHubAuthConfigurationError(
                "GitHub App clock skew must be non-negative"
            )
        self.clock_skew_seconds = clock_skew_seconds
        if (
            isinstance(token_refresh_seconds, bool)
            or not isinstance(token_refresh_seconds, int)
            or token_refresh_seconds < 0
        ):
            raise GitHubAuthConfigurationError(
                "GitHub App token refresh window must be non-negative"
            )
        self.token_refresh_seconds = token_refresh_seconds
        self.cache_enabled = cache_enabled
        self._lock = threading.RLock()
        self._cache: dict[InstallationTokenScope, _CachedToken] = {}

    def create_app_jwt(self, now_seconds: float | None = None) -> str:
        now = self.clock() if now_seconds is None else now_seconds
        claims = {
            "iat": int(now - self.clock_skew_seconds),
            "exp": int(now + self.jwt_lifetime_seconds),
            "iss": str(self.app_id),
        }
        try:
            return jwt_encode_rs256(
                claims,
                private_key_pem=self.private_key_source.load_private_key_pem(),
            )
        except GitHubAuthError:
            raise
        except Exception as exc:
            raise GitHubAuthConfigurationError(
                "GitHub App JWT could not be created"
            ) from exc

    def installation_token(
        self,
        *,
        installation_id: int,
        repository: str,
        requested_permissions: RequestedPermissions,
    ) -> InstallationToken:
        scope = InstallationTokenScope(
            installation_id=installation_id,
            repository=repository,
            permissions=requested_permissions.values,
        )
        now = self.clock()

        if self.cache_enabled:
            with self._lock:
                cached = self._cache.get(scope)
                if cached is not None and cached.expires_at > now:
                    if cached.token.remaining_seconds(now) > self.token_refresh_seconds:
                        return cached.token
                self._cache.pop(scope, None)

        app_jwt = self.create_app_jwt(now)
        try:
            token = self.transport.request_installation_token(
                installation_id=installation_id,
                repository=repository,
                app_jwt=app_jwt,
                requested_permissions=requested_permissions,
            )
        except GitHubAuthError:
            with self._lock:
                self._cache.pop(scope, None)
            raise

        if token.remaining_seconds(now) <= 0:
            raise GitHubAuthError("GitHub App token was already expired")
        if not requested_permissions.are_subset_of(token.permissions):
            raise GitHubAuthInsufficientPermissionError(
                "GitHub App installation lacks the requested permission"
            )
        if self.cache_enabled:
            with self._lock:
                self._cache[scope] = _CachedToken(
                    token=token,
                    expires_at=token.expires_at_epoch,
                )
        return token

    def clear_cache(self, *, installation_id: int | None = None) -> None:
        with self._lock:
            if installation_id is None:
                self._cache.clear()
                return
            for key in list(self._cache):
                if key.installation_id == installation_id:
                    del self._cache[key]
