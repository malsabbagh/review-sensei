"""Standard GitHub Actions OIDC request and fixed broker capability exchange."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import GitHubBrokerClientError, GitHubHTTPTransientError

MAX_BROKER_BODY_BYTES = 256 * 1024
DEFAULT_BROKER_URL = "https://github.reviewsensei.dev/github/token"
Opener = Callable[..., Any]


@dataclass(frozen=True)
class BrokerSession:
    """One broker-attested, current-head session capability."""

    token: str
    state: str


@dataclass(frozen=True)
class BrokerSessionGrant:
    """Opaque, expiring authority for one serialized hosted session writer."""

    token: str
    state: str
    grant: str
    attestation: dict[str, object]


_SESSION_ATTESTATION_REQUEST_KEYS = frozenset(
    {
        "version",
        "repository",
        "repository_id",
        "pull_request",
        "head_sha",
        "operation",
        "source_comment_id",
        "run_id",
        "issued_at",
        "concurrency_group",
        "job_workflow_ref",
        "job_workflow_sha",
    }
)
_SESSION_ATTESTATION_GRANT_KEYS = _SESSION_ATTESTATION_REQUEST_KEYS | frozenset(
    {"actor", "actor_type", "association", "command_id", "command_digest"}
)


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
                "review_status",
                "inline_reply",
                "issue_reply",
                "learning_write",
                "review_session",
            }:
                raise GitHubBrokerClientError("Broker capability is invalid")
            payload["capability"] = capability
        parsed = self._post(payload)
        token = parsed.get("token")
        if not isinstance(token, str) or not token.strip():
            raise GitHubBrokerClientError("Broker token response was invalid")
        return token

    def open_session(
        self,
        oidc_token: str,
        *,
        repository_id: int,
        pull_request: int,
        head_sha: str,
    ) -> BrokerSession:
        """Obtain a current-head session capability and its enrollment verdict."""

        if not isinstance(oidc_token, str) or not oidc_token.strip():
            raise GitHubBrokerClientError("OIDC token is empty")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request <= 0
            or not isinstance(head_sha, str)
            or re.fullmatch(r"[a-f0-9]{40}", head_sha) is None
        ):
            raise GitHubBrokerClientError("Broker session scope is invalid")
        parsed = self._post(
            {
                "oidc_token": oidc_token,
                "capability": "review_session",
                "session": {
                    "repository_id": repository_id,
                    "pull_request": pull_request,
                    "head_sha": head_sha,
                },
            }
        )
        token = parsed.get("token")
        if not isinstance(token, str) or not token.strip():
            raise GitHubBrokerClientError("Broker token response was invalid")
        if parsed.get("capability") != "review_session":
            raise GitHubBrokerClientError("Broker session response was invalid")
        state = parsed.get("session_state")
        if state == "rate_limited":
            # Enrollment spends the same per-scope budget as an assertion, so
            # an exhausted window is a transient broker condition the caller
            # can retry rather than a malformed response.
            raise GitHubHTTPTransientError("broker session enrollment is rate limited")
        if state not in {"enrolled", "known"}:
            raise GitHubBrokerClientError("Broker session response was invalid")
        return BrokerSession(token=token, state=state)

    def authorize_session_mutation(
        self,
        oidc_token: str,
        *,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        session_attestation: Mapping[str, object],
    ) -> BrokerSessionGrant:
        """Issue a broker-attested grant for one serialized session mutation.

        The caller may carry the attestation between jobs, but cannot choose
        its authority: the Worker checks it against OIDC and live GitHub state
        before returning a random opaque grant.
        """

        if not isinstance(oidc_token, str) or not oidc_token.strip():
            raise GitHubBrokerClientError("OIDC token is empty")
        self._validate_session_scope(repository_id, pull_request, head_sha)
        attestation = self._validated_attestation_request(session_attestation)
        parsed = self._post(
            {
                "oidc_token": oidc_token,
                "capability": "review_session",
                "session": {
                    "repository_id": repository_id,
                    "pull_request": pull_request,
                    "head_sha": head_sha,
                },
                "session_attestation": attestation,
            }
        )
        token = parsed.get("token")
        grant = parsed.get("session_grant")
        state = parsed.get("session_state")
        returned_attestation = parsed.get("session_attestation")
        if (
            not isinstance(token, str)
            or not token.strip()
            or not isinstance(grant, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", grant) is None
            or state not in {"enrolled", "known"}
        ):
            raise GitHubBrokerClientError("Broker session response was invalid")
        returned = self._validated_attestation_grant(returned_attestation)
        if any(returned[key] != value for key, value in attestation.items()):
            raise GitHubBrokerClientError("Broker session response was invalid")
        return BrokerSessionGrant(
            token=token, state=state, grant=grant, attestation=returned
        )

    def verify_session_grant(
        self,
        grant: str,
        session_attestation: Mapping[str, object],
    ) -> dict[str, object]:
        """Verify the opaque grant immediately before a remote ledger write."""

        if not isinstance(grant, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", grant) is None:
            raise GitHubBrokerClientError("Broker session grant is invalid")
        attestation = self._validated_attestation_grant(session_attestation)
        parsed = self._post(
            {"session_grant": grant, "session_attestation": attestation},
            url=self.broker_url.removesuffix("/token") + "/session-grant",
        )
        returned = self._validated_attestation_grant(parsed.get("session_attestation"))
        if returned != attestation:
            raise GitHubBrokerClientError("Broker session grant was not verified")
        return returned

    @staticmethod
    def _validate_session_scope(
        repository_id: int, pull_request: int, head_sha: str
    ) -> None:
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request <= 0
            or not isinstance(head_sha, str)
            or re.fullmatch(r"[a-f0-9]{40}", head_sha) is None
        ):
            raise GitHubBrokerClientError("Broker session scope is invalid")

    @staticmethod
    def _validated_attestation_request(value: object) -> dict[str, object]:
        if not isinstance(value, Mapping) or set(value) != _SESSION_ATTESTATION_REQUEST_KEYS:
            raise GitHubBrokerClientError("Broker session attestation is invalid")
        result = dict(value)
        if (
            result["version"] != 1
            or not isinstance(result["repository"], str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result["repository"])
            is None
            or isinstance(result["repository_id"], bool)
            or not isinstance(result["repository_id"], int)
            or result["repository_id"] <= 0
            or isinstance(result["pull_request"], bool)
            or not isinstance(result["pull_request"], int)
            or result["pull_request"] <= 0
            or not isinstance(result["head_sha"], str)
            or re.fullmatch(r"[a-f0-9]{40}", result["head_sha"]) is None
            or result["operation"] not in {"review", "command"}
            or (
                result["source_comment_id"] is not None
                and (
                    isinstance(result["source_comment_id"], bool)
                    or not isinstance(result["source_comment_id"], int)
                    or result["source_comment_id"] <= 0
                )
            )
            or (result["operation"] == "command" and result["source_comment_id"] is None)
            or not isinstance(result["run_id"], str)
            or re.fullmatch(r"[1-9][0-9]{0,18}", result["run_id"]) is None
            or isinstance(result["issued_at"], bool)
            or not isinstance(result["issued_at"], int)
            or not isinstance(result["concurrency_group"], str)
            or not isinstance(result["job_workflow_ref"], str)
            or not isinstance(result["job_workflow_sha"], str)
            or re.fullmatch(r"[a-f0-9]{40}", result["job_workflow_sha"]) is None
        ):
            raise GitHubBrokerClientError("Broker session attestation is invalid")
        return result

    @classmethod
    def _validated_attestation_grant(cls, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping) or set(value) != _SESSION_ATTESTATION_GRANT_KEYS:
            raise GitHubBrokerClientError("Broker session attestation is invalid")
        result = dict(value)
        request = {key: result[key] for key in _SESSION_ATTESTATION_REQUEST_KEYS}
        cls._validated_attestation_request(request)
        if result["operation"] == "review":
            if any(
                result[key] is not None
                for key in (
                    "actor",
                    "actor_type",
                    "association",
                    "command_id",
                    "command_digest",
                )
            ):
                raise GitHubBrokerClientError("Broker session attestation is invalid")
        elif (
            not isinstance(result["actor"], str)
            or not result["actor"].strip()
            or not isinstance(result["actor_type"], str)
            or result["actor_type"].lower() != "user"
            or result["association"] not in {"OWNER", "MEMBER", "COLLABORATOR"}
            or isinstance(result["command_id"], bool)
            or not isinstance(result["command_id"], int)
            or result["command_id"] <= 0
            or not isinstance(result["command_digest"], str)
            or re.fullmatch(r"[a-f0-9]{64}", result["command_digest"]) is None
        ):
            raise GitHubBrokerClientError("Broker session attestation is invalid")
        return result

    def _post(self, payload: dict[str, Any], *, url: str | None = None) -> dict[str, Any]:
        """Post one bounded capability request and return a validated object."""

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            url or self.broker_url,
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
        return parsed
