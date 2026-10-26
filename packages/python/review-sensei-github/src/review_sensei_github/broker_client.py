"""Standard GitHub Actions OIDC request and fixed broker capability exchange."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError

from .broker_admission import inspect_broker_http_error
from .broker_control import BrokerDispatchAccounting
from .errors import (
    GitHubBrokerAdmissionHeldError,
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)
from .run_authority import CAPABILITIES, REQUEST_KEYS, parse_authority

MAX_BROKER_BODY_BYTES = 256 * 1024
DEFAULT_BROKER_URL = "https://github.reviewsensei.dev/github/token"
# Cloudflare's browser integrity check rejects urllib's default agent with
# error 1010 before the Worker runs.
BROKER_USER_AGENT = "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)"
SESSION_GRANT_PATTERN = re.compile(r"sg1\.[1-9][0-9]{10,15}\.[A-Za-z0-9_-]{43}\Z")
Opener = Callable[..., Any]


@dataclass(frozen=True)
class BrokerSession:
    """One broker-attested, current-head session capability."""

    token: str = field(repr=False)
    state: str
    reservation_owner: Mapping[str, object] | None = None
    grant: str | None = field(default=None, repr=False)
    attestation: Mapping[str, object] | None = None
    app_id: int | None = None


@dataclass(frozen=True)
class BrokerSessionGrant:
    """Opaque credential for one serialized hosted session writer.

    The installation token and one-use grant are intentionally omitted from
    the dataclass repr so routine diagnostics cannot log bearer credentials.
    """

    token: str = field(repr=False)
    state: str
    grant: str = field(repr=False)
    attestation: dict[str, object]
    app_id: int | None = None


class _ResponseTooLarge(ValueError):
    pass


class _InvalidResponse(ValueError):
    pass


def _response_metadata(response: Any) -> tuple[int | None, str | None]:
    status = None
    ray = None
    try:
        value = getattr(response, "status", None)
        if type(value) is int and 100 <= value <= 599:
            status = value
        value = response.headers.get("cf-ray")
        if isinstance(value, str) and re.fullmatch(
            r"[a-f0-9]{16,64}-[a-z]{3}", value, re.IGNORECASE
        ):
            ray = value
    except Exception:
        pass
    return status, ray


def _transport_message(
    error: Exception,
    phase: str,
    started: float,
    timeout: float,
    *,
    post: bool,
    admitted_timeout: float,
    status: int | None = None,
    ray: str | None = None,
) -> str:
    if isinstance(error, TimeoutError) or (
        isinstance(error, URLError) and isinstance(error.reason, TimeoutError)
    ):
        failure = "timeout"
    elif isinstance(error, UnicodeDecodeError):
        failure = "invalid_utf8"
    elif isinstance(error, json.JSONDecodeError):
        failure = "invalid_json"
    elif isinstance(error, _ResponseTooLarge):
        failure = "oversized"
    elif isinstance(error, ValueError):
        failure = "invalid_response"
    else:
        failure = "transport"
    elapsed = min(
        9_007_199_254_740_991, max(0, int((time.monotonic() - started) * 1000))
    )
    metadata = ("" if status is None else f" broker.response_status={status}") + (
        "" if ray is None else f" broker.correlation_id={ray}"
    )
    return (
        f"broker.transport_phase={phase} broker.transport_failure={failure} "
        f"broker.elapsed_ms={elapsed} broker.timeout_seconds={timeout:g} "
        f"broker.elapsed_scope=request_with_preparation "
        f"broker.admitted_timeout_ms={int(admitted_timeout * 1000)} "
        f"broker.preparation_ms={max(0, int((admitted_timeout - timeout) * 1000))} "
        f"broker.socket_timeout_ms={int(timeout * 1000)} broker.retry_count=0 "
        f"broker.post_outcome={'unknown' if post else 'not_applicable'}{metadata}"
    )


class _RefuseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise GitHubBrokerClientError("Controlled broker redirects are refused")


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
        before_request: Callable[[], float] | None = None,
        control: BrokerDispatchAccounting | None = None,
        authority: Mapping[str, object] | None = None,
    ) -> None:
        if (
            not isinstance(broker_url, str)
            or not broker_url.startswith("https://")
            or not broker_url.endswith("/token")
        ):
            raise GitHubBrokerClientError("Broker URL must be an HTTPS /token endpoint")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise GitHubBrokerClientError("Broker timeout must be positive")
        self.broker_url = broker_url
        self.opener = opener
        self.timeout = timeout
        if before_request is not None and not callable(before_request):
            raise GitHubBrokerClientError("Broker request accounting is invalid")
        if control is not None:
            if not isinstance(control, BrokerDispatchAccounting):
                raise GitHubBrokerClientError("Broker control configuration is invalid")
            if before_request is not None and (
                getattr(before_request, "__self__", None) is not control.budget
                or getattr(before_request, "__func__", None)
                is not EvidenceReadBudget.consume
            ):
                raise GitHubBrokerClientError(
                    "Broker accounting is not the original budget"
                )
            before_request = control.budget.consume
            if opener is urlopen:
                # Each physical caller request has one prepaid dispatch. The
                # ordinary urllib handler could silently follow several hops.
                self.opener = build_opener(_RefuseRedirects()).open
        self.before_request = before_request
        self.control = control
        self.authority = None if authority is None else parse_authority(authority)
        # Copies share this authentication cache; it owns no allowance and
        # never replaces the Worker's independent verification of new claims.
        self._oidc_cache: dict[str, Any] = {}

    def _current_oidc(self, token: str) -> str:
        from .oidc_refresh import expiry_hint

        now = time.time()
        cached = self._oidc_cache
        if cached.get("original") == token and cached.get("expires", 0) > now + 60:
            return cached["token"]
        expires = expiry_hint(token)
        if expires is None or expires > now + 60:
            # Malformed or opaque assertions receive no local authority. The
            # Worker still authenticates them or refuses their first POST.
            return token
        # Refresh before the first capability POST, never replay a failed POST.
        # This physical OIDC GET spends the same original caller-run allowance.
        fresh = self.request_oidc_token()
        deadline = expiry_hint(fresh)
        if deadline is None or deadline <= time.time() + 60:
            raise GitHubBrokerClientError("Fresh Actions OIDC assertion is unavailable")
        cached.update(original=token, token=fresh, expires=deadline)
        return fresh

    def _request_timeout(self) -> float:
        if self.before_request is None:
            return self.timeout
        try:
            remaining = self.before_request()
        except ReviewInputError as exc:
            # Admission is local: preserve the public broker error contract
            # without labeling an unattempted POST as an uncertain dispatch.
            raise GitHubBrokerClientError(
                "Broker request allowance is exhausted"
            ) from exc
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
            or not 0 < remaining <= 60
        ):
            raise GitHubBrokerClientError("Broker request allowance is exhausted")
        return min(self.timeout, remaining)

    def _dispatch_timeout(self, started: float, admitted: float) -> float:
        """Spend preparation time inside the original window, without a debit."""
        elapsed = time.monotonic() - started
        if not math.isfinite(elapsed) or not 0 <= elapsed < admitted:
            raise GitHubBrokerClientError(
                "Broker request allowance expired before dispatch"
            )
        budget = getattr(self.before_request, "__self__", None)
        if isinstance(budget, EvidenceReadBudget):
            try:
                budget.check()
            except ReviewInputError as exc:
                raise GitHubBrokerClientError(
                    "Broker request allowance expired before dispatch"
                ) from exc
        return admitted - elapsed

    def _check_completed_request(
        self,
        started: float,
        timeout: float,
        *,
        post: bool,
        status: int | None,
        ray: str | None,
        dispatch_timeout: float,
    ) -> None:
        if time.monotonic() - started >= timeout:
            raise GitHubBrokerClientError(
                (
                    "Broker token request failed; "
                    if post
                    else "OIDC token request failed; "
                )
                + _transport_message(
                    TimeoutError(),
                    "response_read",
                    started,
                    dispatch_timeout,
                    post=post,
                    admitted_timeout=timeout,
                    status=status,
                    ray=ray,
                )
            )
        original_budget = getattr(self.before_request, "__self__", None)
        if isinstance(original_budget, EvidenceReadBudget):
            try:
                original_budget.check()
            except ReviewInputError as exc:
                if self.control is not None:
                    # Existing controlled callers distinguish deadline
                    # refusal from broker transport/configuration failure.
                    raise
                raise GitHubBrokerClientError(
                    "Broker response exceeded the original budget; "
                    f"broker.post_outcome={'unknown' if post else 'not_applicable'}"
                ) from exc

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
        started = time.monotonic()
        phase = "request_open"
        status, ray = None, None
        timeout = self._request_timeout()
        dispatch_timeout = self._dispatch_timeout(started, timeout)
        try:
            with self.opener(request, timeout=dispatch_timeout) as response:
                status, ray = _response_metadata(response)
                phase = "response_read"
                raw = response.read(MAX_BROKER_BODY_BYTES + 1)
                phase = "response_size"
                if not isinstance(raw, (bytes, bytearray)):
                    raise _InvalidResponse
                if len(raw) > MAX_BROKER_BODY_BYTES:
                    raise _ResponseTooLarge
                phase = "response_decode"
                parsed = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise GitHubHTTPTransientError(
                    "OIDC token request failed temporarily"
                ) from exc
            raise GitHubBrokerClientError("OIDC token request failed") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise GitHubBrokerClientError(
                "OIDC token request failed; "
                + _transport_message(
                    exc,
                    phase,
                    started,
                    dispatch_timeout,
                    post=False,
                    admitted_timeout=timeout,
                    status=status,
                    ray=ray,
                )
            ) from exc
        self._check_completed_request(
            started,
            timeout,
            post=False,
            status=status,
            ray=ray,
            dispatch_timeout=dispatch_timeout,
        )
        if not isinstance(parsed, dict):
            raise GitHubBrokerClientError("OIDC token response was invalid")
        value = parsed.get("value")
        if not isinstance(value, str) or not value.strip():
            raise GitHubBrokerClientError("OIDC token response was invalid")
        if self.control is not None:
            self.control.budget.check()
        return value

    def exchange(self, oidc_token: str, *, capability: str | None = None) -> str:
        """Exchange an OIDC token for one fixed, named capability token."""

        if not isinstance(oidc_token, str) or not oidc_token.strip():
            raise GitHubBrokerClientError("OIDC token is empty")
        payload: dict[str, str] = {"oidc_token": oidc_token}
        if capability is not None:
            if not isinstance(capability, str) or capability not in {
                "review_publish",
                "thread_resolve",
                "review_status",
                "review_actions",
                "check_publish",
                "inline_reply",
                "issue_reply",
                "learning_write",
                "review_session",
            }:
                raise GitHubBrokerClientError("Broker capability is invalid")
            payload["capability"] = capability
        parsed = self._post(payload)
        if capability == "thread_resolve" and parsed.get("capability") != capability:
            raise GitHubBrokerClientError(
                "Broker cleanup capability response is invalid"
            )
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
        if not isinstance(state, str) or state not in {"enrolled", "known"}:
            raise GitHubBrokerClientError("Broker session response was invalid")
        from review_sensei.errors import ReviewInputError
        from review_sensei.session import _stored_reservation_owner

        try:
            owner = _stored_reservation_owner(parsed.get("reservation_owner"))
        except ReviewInputError as exc:
            raise GitHubBrokerClientError("Broker session owner was invalid") from exc
        if owner is not None and owner["head_sha"] != head_sha:
            raise GitHubBrokerClientError("Broker session owner head was stale")
        attestation = self._validated_attestation_grant(
            parsed.get("session_attestation")
        )
        requested = self.authority
        grant = parsed.get("session_grant")
        if (
            requested is None
            or any(attestation[key] != requested[key] for key in REQUEST_KEYS)
            or not isinstance(grant, str)
            or SESSION_GRANT_PATTERN.fullmatch(grant) is None
        ):
            raise GitHubBrokerClientError("Broker current run grant conflicts")
        return BrokerSession(
            token=token,
            state=state,
            reservation_owner=owner,
            grant=grant,
            attestation=attestation,
            app_id=self._validated_app_id(parsed.get("app_id")),
        )

    def open_session_release(
        self,
        oidc_token: str,
        *,
        repository_id: int,
        pull_request: int,
        head_sha: str,
    ) -> BrokerSession:
        """Obtain a release-only session capability for this run's own head.

        The broker returns a capability token and the verified reservation
        owner even when the PR head has since moved, because releasing a
        reservation publishes nothing. It records no enrollment witness
        and issues no mutation grant; the caller must still prove ledger
        ownership before releasing.
        """

        if not isinstance(oidc_token, str) or not oidc_token.strip():
            raise GitHubBrokerClientError("OIDC token is empty")
        self._validate_session_scope(repository_id, pull_request, head_sha)
        parsed = self._post(
            {
                "oidc_token": oidc_token,
                "capability": "review_session",
                "release_only": True,
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
        if parsed.get("release_only") is not True:
            raise GitHubBrokerClientError("Broker session response was invalid")
        if parsed.get("session_state") is not None or parsed.get("session_grant"):
            raise GitHubBrokerClientError("Broker session response was invalid")
        from review_sensei.errors import ReviewInputError
        from review_sensei.session import _stored_reservation_owner

        try:
            owner = _stored_reservation_owner(parsed.get("reservation_owner"))
        except ReviewInputError as exc:
            raise GitHubBrokerClientError("Broker session owner was invalid") from exc
        if owner is None or owner["head_sha"] != head_sha:
            raise GitHubBrokerClientError("Broker session owner head was stale")
        return BrokerSession(
            token=token,
            state="release",
            reservation_owner=owner,
            app_id=self._validated_app_id(parsed.get("app_id")),
        )

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
            or SESSION_GRANT_PATTERN.fullmatch(grant) is None
            or not isinstance(state, str)
            or state not in {"enrolled", "known"}
        ):
            raise GitHubBrokerClientError("Broker session response was invalid")
        returned = self._validated_attestation_grant(returned_attestation)
        if self.authority is None or any(
            returned[key] != value for key, value in self.authority.items()
        ):
            raise GitHubBrokerClientError("Broker session response was invalid")
        return BrokerSessionGrant(
            token=token,
            state=state,
            grant=grant,
            attestation=returned,
            app_id=self._validated_app_id(parsed.get("app_id")),
        )

    def verify_session_grant(
        self,
        grant: str,
        session_attestation: Mapping[str, object],
    ) -> dict[str, object]:
        """Verify the opaque grant immediately before a remote ledger write."""

        if not isinstance(grant, str) or SESSION_GRANT_PATTERN.fullmatch(grant) is None:
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
    def _validated_app_id(value: object) -> int:
        if type(value) is not int or not 0 < value <= 9_007_199_254_740_991:
            raise GitHubBrokerClientError(
                "Broker authenticated App identity is invalid"
            )
        return value

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
        return parse_authority(value)

    @staticmethod
    def _validated_attestation_grant(value: object) -> dict[str, object]:
        return parse_authority(value, grant=True)

    def _post(
        self, payload: dict[str, Any], *, url: str | None = None
    ) -> dict[str, Any]:
        """Post one bounded capability request and return a validated object."""

        if url is None:
            supplied = payload.get("session_attestation", self.authority)
            authority = self._validated_attestation_request(supplied)
            # A new exchange authenticates the same purpose/source afresh.
            # Refresh only its bounded request age, never its identity or a
            # previously issued mutation grant.
            now = int(time.time())
            authority = {**authority, "issued_at": now, "expires_at": now + 600}
            self.authority = authority
            requested_capability = payload.get("capability", "review_publish")
            if requested_capability not in CAPABILITIES[str(authority["purpose"])]:
                raise GitHubBrokerClientError(
                    "Broker capability conflicts with run purpose"
                )
            payload = {**payload, "session_attestation": authority}
            payload["oidc_token"] = self._current_oidc(payload["oidc_token"])

        control_request = None
        if self.control is not None:
            control_request = self.control.admit(
                verification=url is not None and url.endswith("/session-grant")
            )
            payload = {**payload, "control_budget": control_request}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        started = time.monotonic()
        wall_started = time.time()
        phase = "request_open"
        status, ray = None, None
        timeout = self._request_timeout()
        original_budget = getattr(self.before_request, "__self__", None)
        deadline_ms = int((wall_started + timeout) * 1000)
        if isinstance(original_budget, EvidenceReadBudget):
            deadline_ms = min(deadline_ms, original_budget.wall_deadline_ms)
        request = Request(
            url or self.broker_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": BROKER_USER_AGENT,
                "X-ReviewSensei-Deadline-Unix-Ms": str(deadline_ms),
            },
            method="POST",
        )
        dispatch_timeout = self._dispatch_timeout(started, timeout)
        try:
            with self.opener(request, timeout=dispatch_timeout) as response:
                status, ray = _response_metadata(response)
                phase = "response_read"
                raw = response.read(MAX_BROKER_BODY_BYTES + 1)
                phase = "response_size"
                if not isinstance(raw, (bytes, bytearray)):
                    raise _InvalidResponse
                if len(raw) > MAX_BROKER_BODY_BYTES:
                    raise _ResponseTooLarge
                phase = "response_decode"
                parsed = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except HTTPError as exc:
            inspection = inspect_broker_http_error(exc)
            if inspection.admission_held:
                raise GitHubBrokerAdmissionHeldError(
                    "Broker admission is temporarily held by the operator; "
                    "no automatic retry was attempted."
                ) from exc
            diagnostic = inspection.diagnostic
            if exc.code == 429 or exc.code >= 500:
                raise GitHubHTTPTransientError(
                    "Broker token request failed temporarily; " + diagnostic.message(),
                    broker_diagnostic=diagnostic,
                ) from exc
            raise GitHubBrokerClientError(
                "Broker token request was rejected; " + diagnostic.message(),
                broker_diagnostic=diagnostic,
            ) from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise GitHubBrokerClientError(
                "Broker token request failed; "
                + _transport_message(
                    exc,
                    phase,
                    started,
                    dispatch_timeout,
                    post=True,
                    admitted_timeout=timeout,
                    status=status,
                    ray=ray,
                )
            ) from exc
        self._check_completed_request(
            started,
            timeout,
            post=True,
            status=status,
            ray=ray,
            dispatch_timeout=dispatch_timeout,
        )
        if not isinstance(parsed, dict):
            raise GitHubBrokerClientError("Broker token response was invalid")
        if self.control is not None and control_request is not None:
            self.control.verify(parsed.get("control_receipt"), control_request)
        return parsed
