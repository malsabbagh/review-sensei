"""Bounded GitHub REST HTTP client for publisher adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .errors import (
    GitHubHTTPError,
    GitHubHTTPResponseTooLargeError,
    GitHubHTTPTransientError,
)

MAX_GITHUB_RESPONSE_BYTES = 512 * 1024
MAX_PAGINATION_PAGES = 10
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")

Opener = Callable[..., Any]


class GitHubHttpTransport(Protocol):
    """Protocol for the bounded GitHub REST transport."""

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        """Return (status, parsed JSON body) for one bounded request."""


def _validated_owner(owner: str) -> str:
    if not isinstance(owner, str) or _SAFE_SEGMENT.fullmatch(owner) is None:
        raise GitHubHTTPError("GitHub owner is invalid")
    return owner


def _validated_repo(repo: str) -> str:
    if not isinstance(repo, str) or _SAFE_SEGMENT.fullmatch(repo) is None:
        raise GitHubHTTPError("GitHub repository is invalid")
    return repo


class GitHubHttp:
    """Strict UTF-8/JSON GitHub REST client with capped pagination."""

    def __init__(
        self,
        *,
        api_url: str = "https://api.github.com",
        opener: Opener = urlopen,
        timeout: int = 30,
    ) -> None:
        if not isinstance(api_url, str) or not api_url.strip():
            raise GitHubHTTPError("GitHub API URL must be non-empty")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise GitHubHTTPError("GitHub HTTP timeout must be positive")
        self.api_url = api_url.rstrip("/")
        self.opener = opener
        self.timeout = timeout

    def repository_path(self, repository: str, suffix: str = "") -> str:
        owner, _, name = repository.partition("/")
        return (
            f"/repos/{quote(_validated_owner(owner), safe='')}/"
            f"{quote(_validated_repo(name), safe='')}{suffix}"
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
            raise GitHubHTTPError("GitHub request path is invalid")
        if not isinstance(token, str) or not token.strip():
            raise GitHubHTTPError("GitHub installation token is empty")
        if body is not None and not isinstance(body, dict):
            raise GitHubHTTPError("GitHub request body must be an object")
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.api_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
                if not isinstance(raw, (bytes, bytearray)):
                    raise GitHubHTTPError("GitHub response was invalid")
                if len(raw) > MAX_GITHUB_RESPONSE_BYTES:
                    raise GitHubHTTPResponseTooLargeError(
                        "GitHub response exceeded the configured size limit"
                    )
                status = int(getattr(response, "status", 200))
                return status, self._parse_body(bytes(raw))
        except HTTPError as exc:
            self._read_bounded_error_body(exc)
            # Preserve the status as data so callers can reconcile ambiguous
            # write responses (409/422/429/5xx) without exposing the bounded
            # error body.  Network failures remain typed exceptions below.
            return exc.code, None
        except (URLError, TimeoutError) as exc:
            raise GitHubHTTPTransientError("GitHub request failed") from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise GitHubHTTPTransientError("GitHub request timed out") from exc
            raise GitHubHTTPTransientError("GitHub request failed") from exc

    @staticmethod
    def _parse_body(raw: bytes) -> dict[str, Any] | list[Any] | None:
        if not raw:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubHTTPError("GitHub response was not valid UTF-8 JSON") from exc
        if not isinstance(parsed, (dict, list)):
            raise GitHubHTTPError("GitHub response must be a JSON object or array")
        return parsed

    @staticmethod
    def _read_bounded_error_body(exc: HTTPError) -> str:
        try:
            raw = exc.read(MAX_GITHUB_RESPONSE_BYTES + 1)
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw)[:MAX_GITHUB_RESPONSE_BYTES].decode(
                    "utf-8", errors="replace"
                )
        except OSError:
            pass
        return ""

    def paginate(
        self,
        *,
        path: str,
        token: str,
    ) -> list[Any]:
        """Return at most ``MAX_PAGINATION_PAGES`` pages of a paginated list."""

        collected: list[Any] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            current = f"{path}{separator}per_page=100&page={page}"
            status, body = self.request("GET", current, token=token)
            if status == 404:
                raise GitHubHTTPError("GitHub pagination target was not found")
            if status < 200 or status >= 300:
                raise GitHubHTTPError("GitHub pagination request was rejected")
            if not isinstance(body, list):
                raise GitHubHTTPError("GitHub pagination response was invalid")
            collected.extend(body)
            if len(body) < 100:
                return collected
        raise GitHubHTTPError("GitHub pagination exceeded configured page limit")
