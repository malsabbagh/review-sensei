"""Minimal RS256 JWT signing helpers for GitHub App authentication."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
)

from .errors import GitHubAuthConfigurationError


class JWTAlgorithms:
    """Names of algorithms this package can sign."""

    RS256 = "RS256"


@dataclass(frozen=True)
class DecodedJWT:
    """Header and payload from a JWT after base64 decoding."""

    header: dict[str, Any]
    payload: dict[str, Any]


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise GitHubAuthConfigurationError("JWT encoding failed") from exc


def _load_rsa_private_key(pem: bytes) -> RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise GitHubAuthConfigurationError(
            "GitHub App private key could not be loaded"
        ) from exc
    if not isinstance(key, RSAPrivateKey):
        raise GitHubAuthConfigurationError("GitHub App private key must be an RSA key")
    return key


def jwt_encode_rs256(
    claims: dict[str, Any],
    *,
    private_key_pem: bytes,
) -> str:
    """Return a compact JWT signed with RS256."""

    header = {"alg": JWTAlgorithms.RS256, "typ": "JWT"}
    try:
        signing_input = (
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url_encode(
                json.dumps(claims, separators=(",", ":"), sort_keys=True).encode(
                    "utf-8"
                )
            )
        )
        key = _load_rsa_private_key(private_key_pem)
        signature = key.sign(
            signing_input.encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except GitHubAuthConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise GitHubAuthConfigurationError("JWT signing failed") from exc
    return signing_input + "." + _b64url_encode(signature)


def decode_jwt_parts(token: str) -> DecodedJWT:
    """Decode JWT header and payload for tests and inspection.

    This function does not verify signatures. Tests should verify RS256
    signatures against the app public key when they need cryptographic proof.
    """

    parts = token.split(".")
    if len(parts) != 3:
        raise GitHubAuthConfigurationError("JWT is malformed")
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubAuthConfigurationError("JWT payload is malformed") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise GitHubAuthConfigurationError("JWT payload is malformed")
    return DecodedJWT(header=header, payload=payload)


def verify_rs256_signature(token: str, public_key_pem: bytes) -> None:
    """Verify an RS256 JWT against a public key.

    Intended for tests and operational tooling, not for the authentication
    adapter itself. The adapter signs with the private key only.
    """

    parts = token.split(".")
    if len(parts) != 3:
        raise GitHubAuthConfigurationError("JWT is malformed")
    signing_input = (parts[0] + "." + parts[1]).encode("ascii")
    try:
        signature = _b64url_decode(parts[2])
        key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(key, RSAPublicKey):
            raise GitHubAuthConfigurationError(
                "JWT signature verification requires an RSA public key"
            )
        key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except GitHubAuthConfigurationError:
        raise
    except Exception as exc:
        raise GitHubAuthConfigurationError("JWT signature verification failed") from exc
