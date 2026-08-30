"""Standard GitHub Actions OIDC request and fixed broker capability exchange."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import GitHubBrokerClientError, GitHubHTTPTransientError

MAX_BROKER_BODY_BYTES = 256 * 1024
DEFAULT_BROKER_URL = "https://github.reviewsensei.dev/github/token"
Opener = Callable[..., Any]


class BrokerClient:
    """Exchange one Actions OIDC token for one capability token.

    The broker URL and audit are fixed for the released workflow; this client
    does not accept arbitrary broker workflow inputs.
    """

    def __init__(
        self,
        *,
        broker_url: str = DEFAULT_BROKER_URL,
        opener: Opener = urlopen,
        timeout: int = 30,
    ) -> None:
        if not isinstance(broker_url, str) or not broker_url.startswith("https://"):
            raise GitHubBrokerClientError("Broker URL must be HTTPS")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise GitHubBrokerClientError("Broker timeout must be positive")
        self.broker_url = broker_url
        self.opener = opener
        self.timeout = timeout

    def request_oidc_token(self) -> str:
        """Request the standard GitHub Actions OIDC token from env claims."""

        token = os.getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        url = os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL")
        if not token or not url:
            raise GitHubBrokerClientError("GitHub Actions OIDC token is not available")
        request = Request(
            f"{url}&audience=sts.reviewsensei.dev",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BROKER_BODY_BYTES + 1)
                if not isinstance(raw, (bytes, bytearray)):
                    raise ValueError
                if len(raw) > MAX_BROKER_BODY_BYTES:
                    raise ValueError
                parsed = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise GitHubHTTPTransientError(
                    "OIDC token request failed temporarily"
                ) from exc
            raise GitHubBrokerClientError("OIDC token request failed") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise GitHubBrokerClientError("OIDC token request failed") from exc
        if not isinstance(parsed, dict):
            raise GitHubBrokerClientError("OIDC token response was invalid")
        value = parsed.get("value")
        if not isinstance(value, str) or not value.strip():
            raise GitHubBrokerClientError("OIDC token response was invalid")
        return value

    def exchange(self, oidc_token: str, *, capability: str | None = None) -> str:
        """Exchange an OIDC token for one fixed, named capability token."""

        if not isinstance(oidc_token, str) or not oidc_token.strip():
            raise GitHubBrokerClientError("OIDC token is empty")
        payload: dict[str, str] = {"oidc_token": oidc_token}
        if capability is not None:
            if capability not in {
                "review_publish",
                "inline_reply",
                "issue_reply",
                "learning_write",
            }:
                raise GitHubBrokerClientError("Broker capability is invalid")
            payload["capability"] = capability
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.broker_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BROKER_BODY_BYTES + 1)
                if not isinstance(raw, (bytes, bytearray)):
                    raise ValueError
                if len(raw) > MAX_BROKER_BODY_BYTES:
                    raise ValueError
                parsed = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise GitHubHTTPTransientError(
                    "Broker token request failed temporarily"
                ) from exc
            raise GitHubBrokerClientError("Broker token request was rejected") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise GitHubBrokerClientError("Broker token request failed") from exc
        if not isinstance(parsed, dict):
            raise GitHubBrokerClientError("Broker token response was invalid")
        token = parsed.get("token")
        if not isinstance(token, str) or not token.strip():
            raise GitHubBrokerClientError("Broker token response was invalid")
        return token
