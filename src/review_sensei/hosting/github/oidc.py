"""GitHub Actions OIDC token verification for the installation broker."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any, Protocol
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPublicKey,
    RSAPublicNumbers,
)

from .errors import (
    GitHubOIDCAudienceError,
    GitHubOIDCClaimsError,
    GitHubOIDCError,
    GitHubOIDCExpiredError,
    GitHubOIDCInvalidTokenError,
    GitHubOIDCIssuerError,
    GitHubOIDCSignatureError,
)

DEFAULT_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
DEFAULT_CLOCK_SKEW_SECONDS = 30
ALLOWED_ALGS = frozenset({"RS256"})
MAX_JWKS_BODY_BYTES = 256 * 1024
MAX_OIDC_TOKEN_BYTES = 64 * 1024
DEFAULT_JWKS_TTL_SECONDS = 60 * 60
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
Opener = Callable[..., Any]


@dataclass(frozen=True)
class VerifiedOIDCClaims:
    """Validated identity claims from a GitHub Actions OIDC token."""

    sub: str
    repository: str
    repository_owner: str
    repository_id: int
    actor: str
    actor_id: int
    workflow: str
    workflow_ref: str
    workflow_sha: str
    job_workflow_ref: str
    job_workflow_sha: str
    event_name: str
    ref: str
    sha: str
    run_id: str
    run_number: str
    run_attempt: str
    runner_environment: str
    jti: str
    aud: str
    iss: str
    exp: int
    iat: int


class JwksFetcher(Protocol):
    """Fetch the JSON Web Key Set for an OIDC issuer."""

    def fetch_jwks(self, issuer: str) -> dict[str, Any]:
        """Return the parsed JWKS document for an issuer."""


class HttpJwksFetcher:
    """Fetch and cache issuer JWKS documents over urllib with a bounded TTL.

    Cached entries expire after ``jwks_ttl_seconds``. A fetch after the TTL
    refreshes the keys and fails closed on refresh errors so a long-lived
    broker process cannot keep accepting tokens signed by a revoked key.
    """

    def __init__(
        self,
        *,
        opener: Opener = urlopen,
        timeout_seconds: float = 10,
        jwks_ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
    ) -> None:
        if (
            isinstance(jwks_ttl_seconds, bool)
            or not isinstance(jwks_ttl_seconds, int)
            or jwks_ttl_seconds <= 0
        ):
            raise GitHubOIDCError("OIDC JWKS TTL must be a positive integer")
        self.opener = opener
        self.timeout_seconds = timeout_seconds
        self.jwks_ttl_seconds = jwks_ttl_seconds
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}

    def fetch_jwks(self, issuer: str) -> dict[str, Any]:
        """Fetch a bounded JWKS response, caching it by issuer with a TTL."""

        if not isinstance(issuer, str) or not issuer.strip():
            raise GitHubOIDCSignatureError("OIDC signing keys could not be fetched")
        with self._lock:
            cached = self._cache.get(issuer)
            now = time.time()
            if cached is not None and cached[1] > now:
                return cached[0]

        url = issuer.rstrip("/") + "/.well-known/jwks.json"
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                body = response.read(MAX_JWKS_BODY_BYTES + 1)
            if not isinstance(body, (bytes, bytearray)):
                raise ValueError
            if len(body) > MAX_JWKS_BODY_BYTES:
                raise ValueError
            parsed = json.loads(bytes(body).decode("utf-8", errors="strict"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("keys"), list):
                raise ValueError
        except Exception as exc:
            raise GitHubOIDCSignatureError(
                "OIDC signing keys could not be fetched"
            ) from exc

        with self._lock:
            self._cache[issuer] = (parsed, time.time() + self.jwks_ttl_seconds)
        return parsed


def _b64url_decode(value: str) -> bytes:
    """Decode an unpadded base64url value used by a compact JWT or JWK."""

    if (
        not isinstance(value, str)
        or not value
        or not _BASE64URL_PATTERN.fullmatch(value)
    ):
        raise ValueError
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def _json_object(value: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise GitHubOIDCInvalidTokenError("OIDC token was invalid") from exc
    if not isinstance(parsed, dict):
        raise GitHubOIDCInvalidTokenError("OIDC token was invalid")
    return parsed


def _jwk_to_rsa_public_key(jwk: dict[str, Any]) -> RSAPublicKey:
    """Build an RSA public key from a base64url-encoded JWK."""

    try:
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        key = RSAPublicNumbers(e, n).public_key()
    except Exception as exc:
        raise GitHubOIDCSignatureError("OIDC token signing key was invalid") from exc
    return key


def _numeric_claim(payload: dict[str, Any], name: str) -> int | float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GitHubOIDCClaimsError("OIDC token claims were invalid")
    if isinstance(value, float) and not isfinite(value):
        raise GitHubOIDCClaimsError("OIDC token claims were invalid")
    return value


def _decimal_id_claim(payload: dict[str, Any], name: str) -> int:
    """Parse GitHub's decimal-string identity claims without coercion quirks."""

    value = payload.get(name)
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise GitHubOIDCClaimsError("OIDC token claims were invalid")
    return int(value)


def _non_empty_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise GitHubOIDCClaimsError("OIDC token claims were invalid")
    return value


def _validate_claims(
    payload: dict[str, Any],
    *,
    expected_issuer: str,
    expected_audience: str,
    now: float,
    clock_skew_seconds: int,
) -> VerifiedOIDCClaims:
    issuer = payload.get("iss")
    if issuer != expected_issuer or not isinstance(issuer, str):
        raise GitHubOIDCIssuerError("OIDC token issuer did not match")

    audience = payload.get("aud")
    if isinstance(audience, str):
        if audience != expected_audience:
            raise GitHubOIDCAudienceError("OIDC token audience did not match")
    elif isinstance(audience, list):
        if audience != [expected_audience]:
            raise GitHubOIDCAudienceError("OIDC token audience did not match")
    else:
        raise GitHubOIDCAudienceError("OIDC token audience did not match")

    exp_value = _numeric_claim(payload, "exp")
    if now > exp_value + clock_skew_seconds:
        raise GitHubOIDCExpiredError("OIDC token has expired")
    iat_value = _numeric_claim(payload, "iat")
    if iat_value - clock_skew_seconds > now:
        raise GitHubOIDCInvalidTokenError("OIDC token was issued in the future")
    if "nbf" in payload:
        nbf_value = _numeric_claim(payload, "nbf")
        if now + clock_skew_seconds < nbf_value:
            raise GitHubOIDCInvalidTokenError("OIDC token is not yet valid")

    sub = _non_empty_string(payload, "sub")
    repository = _non_empty_string(payload, "repository")
    if _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise GitHubOIDCClaimsError("OIDC token claims were invalid")
    repository_owner = _non_empty_string(payload, "repository_owner")
    repository_id = _decimal_id_claim(payload, "repository_id")
    actor = _non_empty_string(payload, "actor")
    actor_id = _decimal_id_claim(payload, "actor_id")
    workflow = _non_empty_string(payload, "workflow")
    workflow_ref = _non_empty_string(payload, "workflow_ref")
    workflow_sha = _non_empty_string(payload, "workflow_sha")
    event_name = _non_empty_string(payload, "event_name")
    ref = _non_empty_string(payload, "ref")
    job_workflow_ref = _non_empty_string(payload, "job_workflow_ref")
    job_workflow_sha = _non_empty_string(payload, "job_workflow_sha")
    sha = _non_empty_string(payload, "sha")
    run_id = _non_empty_string(payload, "run_id")
    run_number = _non_empty_string(payload, "run_number")
    run_attempt = _non_empty_string(payload, "run_attempt")
    runner_environment = _non_empty_string(payload, "runner_environment")
    jti = _non_empty_string(payload, "jti")

    if not isinstance(exp_value, (int, float)) or not isinstance(
        iat_value, (int, float)
    ):
        raise GitHubOIDCClaimsError("OIDC token claims were invalid")
    return VerifiedOIDCClaims(
        sub=sub,
        repository=repository,
        repository_owner=repository_owner,
        repository_id=repository_id,
        actor=actor,
        actor_id=actor_id,
        workflow=workflow,
        workflow_ref=workflow_ref,
        workflow_sha=workflow_sha,
        job_workflow_ref=job_workflow_ref,
        job_workflow_sha=job_workflow_sha,
        event_name=event_name,
        ref=ref,
        sha=sha,
        run_id=run_id,
        run_number=run_number,
        run_attempt=run_attempt,
        runner_environment=runner_environment,
        jti=jti,
        aud=audience if isinstance(audience, str) else expected_audience,
        iss=issuer,
        exp=int(exp_value),
        iat=int(iat_value),
    )


def verify_oidc_token(
    token: str,
    *,
    expected_issuer: str,
    expected_audience: str,
    jwks_fetcher: JwksFetcher,
    now: float,
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
) -> VerifiedOIDCClaims:
    """Verify a GitHub Actions OIDC JWT and return its trusted claims."""

    if (
        not isinstance(token, str)
        or not token
        or len(token.encode("utf-8", errors="replace")) > MAX_OIDC_TOKEN_BYTES
    ):
        raise GitHubOIDCInvalidTokenError("OIDC token was invalid")
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise GitHubOIDCInvalidTokenError("OIDC token was invalid")

    try:
        header = _json_object(_b64url_decode(parts[0]))
        payload = _json_object(_b64url_decode(parts[1]))
    except GitHubOIDCError:
        raise
    except Exception as exc:
        raise GitHubOIDCInvalidTokenError("OIDC token was invalid") from exc

    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in ALLOWED_ALGS:
        raise GitHubOIDCInvalidTokenError("OIDC token algorithm is not supported")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise GitHubOIDCInvalidTokenError("OIDC token key id is missing")

    try:
        jwks = jwks_fetcher.fetch_jwks(expected_issuer)
    except GitHubOIDCError:
        raise
    except Exception as exc:
        raise GitHubOIDCSignatureError(
            "OIDC signing keys could not be fetched"
        ) from exc
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise GitHubOIDCSignatureError("OIDC token signing key was not found")
    matching_key: dict[str, Any] | None = None
    for key in keys:
        if not isinstance(key, dict):
            continue
        if key.get("kid") == kid and key.get("alg") == "RS256":
            matching_key = key
            break
    if matching_key is None:
        raise GitHubOIDCSignatureError("OIDC token signing key was not found")

    try:
        public_key = _jwk_to_rsa_public_key(matching_key)
        signature = _b64url_decode(parts[2])
        public_key.verify(
            signature,
            (parts[0] + "." + parts[1]).encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except GitHubOIDCSignatureError:
        raise
    except Exception as exc:
        raise GitHubOIDCSignatureError(
            "OIDC token signature verification failed"
        ) from exc

    try:
        return _validate_claims(
            payload,
            expected_issuer=expected_issuer,
            expected_audience=expected_audience,
            now=now,
            clock_skew_seconds=clock_skew_seconds,
        )
    except GitHubOIDCError:
        raise
    except Exception as exc:
        raise GitHubOIDCClaimsError("OIDC token claims were invalid") from exc
