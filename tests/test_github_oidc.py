import base64
import io
import json
import time
import unittest
from typing import Any
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from review_sensei.hosting.github import (
    DEFAULT_BROKER_AUDIENCE,
    DEFAULT_OIDC_ISSUER,
    GitHubOIDCAudienceError,
    GitHubOIDCClaimsError,
    GitHubOIDCError,
    GitHubOIDCExpiredError,
    GitHubOIDCInvalidTokenError,
    GitHubOIDCIssuerError,
    GitHubOIDCSignatureError,
    VerifiedOIDCClaims,
    verify_oidc_token,
)
from review_sensei.hosting.github.oidc import MAX_OIDC_TOKEN_BYTES, HttpJwksFetcher


def make_key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def make_jwks(public_pem, kid="github-actions"):
    key = serialization.load_pem_public_key(public_pem)
    numbers = key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": b64url(
                    numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
                ),
                "e": b64url(
                    numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
                ),
            }
        ]
    }


def make_token(claims, private_pem, *, kid="github-actions", alg="RS256"):
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    header_part = b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_part = b64url(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    key = serialization.load_pem_private_key(private_pem, password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_part}.{payload_part}.{b64url(signature)}"


class FakeJwksFetcher:
    def __init__(self, jwks=None, error=None):
        self.jwks = jwks
        self.error = error
        self.issuers = []

    def fetch_jwks(self, issuer: str) -> dict[str, Any]:
        self.issuers.append(issuer)
        if self.error is not None:
            raise self.error
        return self.jwks


class GitHubOIDCTests(unittest.TestCase):
    now = 1_700_000_000

    def setUp(self):
        self.private_pem, self.public_pem = make_key_pair()
        self.other_private_pem, self.other_public_pem = make_key_pair()
        self.fetcher = FakeJwksFetcher(make_jwks(self.public_pem))

    def claims(self):
        return {
            "iss": DEFAULT_OIDC_ISSUER,
            "aud": DEFAULT_BROKER_AUDIENCE,
            "sub": "repo:owner/repo:ref:refs/heads/main",
            "repository": "owner/repo",
            "repository_owner": "owner",
            "repository_id": "123",
            "actor": "octocat",
            "actor_id": "1001",
            "workflow": "Review",
            "workflow_ref": "owner/repo/.github/workflows/review.yml@refs/heads/main",
            "workflow_sha": "a" * 40,
            "event_name": "pull_request",
            "ref": "refs/heads/main",
            "job_workflow_ref": "owner/repo/.github/workflows/review.yml@refs/heads/main",
            "job_workflow_sha": "a" * 40,
            "sha": "b" * 40,
            "run_id": "1000",
            "run_number": "42",
            "run_attempt": "1",
            "runner_environment": "github-hosted",
            "jti": "unique-jti",
            "exp": self.now + 300,
            "iat": self.now - 10,
        }

    def token(self, claims=None, private_pem=None, **kwargs):
        return make_token(
            claims or self.claims(),
            private_pem or self.private_pem,
            **kwargs,
        )

    def verify(self, token):
        return verify_oidc_token(
            token,
            expected_issuer=DEFAULT_OIDC_ISSUER,
            expected_audience=DEFAULT_BROKER_AUDIENCE,
            jwks_fetcher=self.fetcher,
            now=self.now,
        )

    def test_valid_oidc_token_returns_claims(self):
        claims = self.verify(self.token())
        self.assertIsInstance(claims, VerifiedOIDCClaims)
        self.assertEqual(claims.repository, "owner/repo")
        self.assertEqual(claims.repository_id, 123)
        self.assertEqual(claims.actor, "octocat")
        self.assertEqual(claims.actor_id, 1001)
        self.assertEqual(claims.event_name, "pull_request")
        self.assertEqual(claims.runner_environment, "github-hosted")
        self.assertEqual(claims.jti, "unique-jti")
        self.assertEqual(claims.aud, DEFAULT_BROKER_AUDIENCE)

    def test_wrong_issuer_rejected(self):
        claims = self.claims()
        claims["iss"] = "https://issuer.invalid"
        with self.assertRaises(GitHubOIDCIssuerError):
            self.verify(self.token(claims))

    def test_wrong_audience_rejected(self):
        claims = self.claims()
        claims["aud"] = "other-audience"
        with self.assertRaises(GitHubOIDCAudienceError):
            self.verify(self.token(claims))

    def test_audience_list_with_extra_audience_rejected(self):
        claims = self.claims()
        claims["aud"] = [DEFAULT_BROKER_AUDIENCE, "other"]
        with self.assertRaises(GitHubOIDCAudienceError):
            self.verify(self.token(claims))

    def test_audience_list_with_single_match_accepted(self):
        claims = self.claims()
        claims["aud"] = [DEFAULT_BROKER_AUDIENCE]
        self.assertEqual(self.verify(self.token(claims)).aud, DEFAULT_BROKER_AUDIENCE)

    def test_expired_token_rejected(self):
        claims = self.claims()
        claims["exp"] = self.now - 31
        with self.assertRaises(GitHubOIDCExpiredError):
            self.verify(self.token(claims))

    def test_future_nbf_rejected(self):
        claims = self.claims()
        claims["nbf"] = self.now + 31
        with self.assertRaises(GitHubOIDCInvalidTokenError):
            self.verify(self.token(claims))

    def test_future_iat_rejected(self):
        claims = self.claims()
        claims["iat"] = self.now + 31
        with self.assertRaises(GitHubOIDCInvalidTokenError):
            self.verify(self.token(claims))

    def test_wrong_signature_rejected(self):
        with self.assertRaises(GitHubOIDCSignatureError):
            self.verify(self.token(private_pem=self.other_private_pem))

    def test_missing_kid_rejected(self):
        with self.assertRaises(GitHubOIDCInvalidTokenError):
            self.verify(self.token(kid=None))

    def test_unknown_kid_rejected(self):
        with self.assertRaises(GitHubOIDCSignatureError):
            self.verify(self.token(kid="unknown"))

    def test_unsupported_alg_rejected(self):
        with self.assertRaises(GitHubOIDCInvalidTokenError):
            self.verify(self.token(alg="HS256"))

    def test_malformed_token_rejected(self):
        with self.assertRaises(GitHubOIDCInvalidTokenError):
            self.verify("not.a.jwt")
        with self.assertRaises(GitHubOIDCInvalidTokenError):
            self.verify("too-few-parts")

    def test_missing_required_claim_rejected(self):
        claims = self.claims()
        del claims["event_name"]
        with self.assertRaises(GitHubOIDCClaimsError):
            self.verify(self.token(claims))

    def test_numeric_repository_id_rejected_because_github_uses_strings(self):
        claims = self.claims()
        claims["repository_id"] = 123
        with self.assertRaises(GitHubOIDCClaimsError):
            self.verify(self.token(claims))

    def test_non_decimal_identity_claim_rejected(self):
        claims = self.claims()
        claims["actor_id"] = "1e3"
        with self.assertRaises(GitHubOIDCClaimsError):
            self.verify(self.token(claims))

    def test_oversized_token_rejected(self):
        with self.assertRaises(GitHubOIDCInvalidTokenError):
            self.verify("x" * (MAX_OIDC_TOKEN_BYTES + 1))

    def test_jwks_fetch_failure_raises_signature_error(self):
        self.fetcher.error = RuntimeError("private claim and key")
        with self.assertRaises(GitHubOIDCSignatureError):
            self.verify(self.token())

    def test_messages_never_include_token_or_claim_values(self):
        claims = self.claims()
        claims["iss"] = "https://sensitive-issuer.invalid"
        token = self.token(claims)
        with self.assertRaises(GitHubOIDCIssuerError) as context:
            self.verify(token)
        message = str(context.exception)
        self.assertNotIn(token, message)
        self.assertNotIn("owner/repo", message)
        self.assertNotIn("sensitive-issuer.invalid", message)


class HttpJwksFetcherTTLTests(unittest.TestCase):
    def _opener(self, body: bytes):
        def opener(request, timeout):
            return io.BytesIO(body)

        return opener

    def test_uses_github_oidc_jwks_endpoint(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, request.get_header("Accept")))
            return io.BytesIO(b'{"keys":[]}')

        fetcher = HttpJwksFetcher(opener=opener)
        fetcher.fetch_jwks(DEFAULT_OIDC_ISSUER)

        self.assertEqual(
            calls,
            [(f"{DEFAULT_OIDC_ISSUER}/.well-known/jwks", "application/json")],
        )

    def test_cache_serves_within_ttl_then_refreshes(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return io.BytesIO(b'{"keys":[]}')

        fetcher = HttpJwksFetcher(opener=opener, jwks_ttl_seconds=60)
        now = time.time()
        with mock.patch(
            "review_sensei.hosting.github.oidc.time.time", return_value=now
        ):
            fetcher.fetch_jwks(DEFAULT_OIDC_ISSUER)
            fetcher.fetch_jwks(DEFAULT_OIDC_ISSUER)
        self.assertEqual(len(calls), 1)  # second call served from cache
        with mock.patch(
            "review_sensei.hosting.github.oidc.time.time",
            return_value=now + 61,
        ):
            fetcher.fetch_jwks(DEFAULT_OIDC_ISSUER)
        self.assertEqual(len(calls), 2)  # TTL expired, refreshed

    def test_refresh_failure_fails_closed(self):
        fetcher = HttpJwksFetcher(
            opener=lambda request, timeout: (_ for _ in ()).throw(RuntimeError()),
            jwks_ttl_seconds=60,
        )
        with self.assertRaises(GitHubOIDCSignatureError):
            fetcher.fetch_jwks(DEFAULT_OIDC_ISSUER)

    def test_non_positive_ttl_rejected(self):
        with self.assertRaises(GitHubOIDCError):
            HttpJwksFetcher(jwks_ttl_seconds=0)
        with self.assertRaises(GitHubOIDCError):
            HttpJwksFetcher(jwks_ttl_seconds=-1)

    def test_refresh_failure_does_not_serve_stale_cache(self):
        good_body = b'{"keys":[]}'
        good = [True]

        def opener(request, timeout):
            if good[0]:
                return io.BytesIO(good_body)
            raise RuntimeError("refresh failed")

        fetcher = HttpJwksFetcher(opener=opener, jwks_ttl_seconds=60)
        now = time.time()
        with mock.patch(
            "review_sensei.hosting.github.oidc.time.time", return_value=now
        ):
            fetcher.fetch_jwks(DEFAULT_OIDC_ISSUER)
        good[0] = False
        with mock.patch(
            "review_sensei.hosting.github.oidc.time.time",
            return_value=now + 61,
        ):
            with self.assertRaises(GitHubOIDCSignatureError):
                fetcher.fetch_jwks(DEFAULT_OIDC_ISSUER)


if __name__ == "__main__":
    unittest.main()
