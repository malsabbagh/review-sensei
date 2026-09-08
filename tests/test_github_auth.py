import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from review_sensei.errors import ReviewSenseiError
from review_sensei.hosting.github import (
    EnvPrivateKeySource,
    FilePrivateKeySource,
    GitHubAppAuth,
    GitHubAuthConfigurationError,
    GitHubAuthInsufficientPermissionError,
    GitHubAuthTransientError,
    GitHubAuthUnavailableInstallationError,
    GitHubTokenTransport,
    InstallationToken,
    InstallationTokenScope,
    RequestedPermissions,
    decode_jwt_parts,
)
from review_sensei.hosting.github.jwt import verify_rs256_signature


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


class BytesPrivateKeySource:
    def __init__(self, private_pem):
        self.private_pem = private_pem

    def load_private_key_pem(self):
        return self.private_pem


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size):
        return self.body[:size]


class FakeHTTPResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reason = "reason"
        self.headers = {}


def http_error(status, body=b"{}"):
    return HTTPError(
        "https://api.github.com/app/installations/1/access_tokens",
        status,
        "error",
        {},
        FakeHTTPResponse(body, status),
    )


class RecordingTransport:
    def __init__(self, token_value=None, permissions=None, error=None):
        self.requests = []
        self.token_value = token_value or "ghs_long_opaque_token"
        self.permissions = permissions or {
            "contents": "write",
            "pull_requests": "write",
        }
        self.error = error

    def request_installation_token(
        self, *, installation_id, repository, app_jwt, requested_permissions
    ):
        self.requests.append(
            {
                "installation_id": installation_id,
                "repository": repository,
                "app_jwt": app_jwt,
                "requested_permissions": requested_permissions,
            }
        )
        if self.error is not None:
            raise self.error
        expires = datetime.now(timezone.utc) + timedelta(minutes=55)
        return InstallationToken(
            token=self.token_value,
            expires_at=expires,
            permissions=self.permissions,
        )


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class GitHubAuthTests(unittest.TestCase):
    def setUp(self):
        self.private_pem, self.public_pem = make_key_pair()

    def make_auth(self, transport=None, clock=None, **kwargs):
        return GitHubAppAuth(
            app_id=123,
            private_key_source=BytesPrivateKeySource(self.private_pem),
            transport=transport,
            clock=clock or FakeClock(1_700_000_000),
            **kwargs,
        )

    def test_app_jwt_claims_and_rs256_signature(self):
        auth = self.make_auth(
            clock=FakeClock(1_700_000_000),
            jwt_lifetime_seconds=540,
            clock_skew_seconds=15,
        )

        token = auth.create_app_jwt()
        decoded = decode_jwt_parts(token)

        self.assertEqual(decoded.header["alg"], "RS256")
        self.assertEqual(decoded.header["typ"], "JWT")
        self.assertEqual(decoded.payload["iss"], "123")
        self.assertEqual(decoded.payload["iat"], 1_699_999_985)
        self.assertEqual(decoded.payload["exp"], 1_700_000_540)
        verify_rs256_signature(token, self.public_pem)

    def test_jwt_lifetime_cannot_exceed_github_limit(self):
        with self.assertRaises(GitHubAuthConfigurationError):
            self.make_auth(jwt_lifetime_seconds=601)

    def test_installation_token_uses_jwt_and_sends_narrow_request(self):
        transport = RecordingTransport()
        auth = self.make_auth(transport=transport)
        permissions = RequestedPermissions({"contents", "pull_requests"})

        token = auth.installation_token(
            installation_id=7,
            repository="owner/repo",
            requested_permissions=permissions,
        )

        self.assertEqual(token.token, "ghs_long_opaque_token")
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request["installation_id"], 7)
        self.assertEqual(request["repository"], "owner/repo")
        self.assertEqual(
            set(request["requested_permissions"].values),
            {"contents", "pull_requests"},
        )
        decoded = decode_jwt_parts(request["app_jwt"])
        self.assertEqual(decoded.payload["iss"], "123")

    def test_token_transport_payload_does_not_include_full_repository_slug(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            headers = {
                str(key).lower(): str(value) for key, value in request.headers.items()
            }
            captured["api_version"] = headers.get("x-github-api-version")
            return FakeResponse(
                b'{"token":"ghs_abc","expires_at":"2030-01-01T00:00:00Z",'
                b'"permissions":{"contents":"write"}}'
            )

        auth = self.make_auth(
            transport=GitHubTokenTransport(
                api_url="https://api.github.test", opener=opener
            )
        )
        auth.installation_token(
            installation_id=7,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )

        self.assertEqual(
            captured["url"],
            "https://api.github.test/app/installations/7/access_tokens",
        )
        self.assertEqual(captured["body"]["permissions"], {"contents": "write"})
        self.assertEqual(captured["body"]["repositories"], ["repo"])
        self.assertTrue(captured["authorization"].startswith("Bearer "))
        self.assertEqual(captured["api_version"], "2022-11-28")

    def test_cache_is_scoped_by_installation_repository_and_permissions(self):
        transport = RecordingTransport()
        auth = self.make_auth(transport=transport)

        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        auth.installation_token(
            installation_id=1,
            repository="owner/other",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        auth.installation_token(
            installation_id=2,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"pull_requests"}),
        )

        self.assertEqual(len(transport.requests), 4)
        scopes = [
            (request["installation_id"], request["repository"])
            for request in transport.requests
        ]
        self.assertEqual(
            scopes,
            [
                (1, "owner/repo"),
                (1, "owner/other"),
                (2, "owner/repo"),
                (1, "owner/repo"),
            ],
        )

    def test_token_refreshes_before_expiry_window(self):
        clock = FakeClock(1_700_000_000)
        transport = RecordingTransport()
        auth = self.make_auth(transport=transport, clock=clock)
        expires = datetime.fromtimestamp(1_700_000_500, tz=timezone.utc)
        calls = []

        def custom_transport(
            *, installation_id, repository, app_jwt, requested_permissions
        ):
            calls.append((installation_id, repository))
            return InstallationToken(
                token=f"token-{len(calls)}",
                expires_at=expires,
                permissions={"contents": "write"},
            )

        transport.request_installation_token = custom_transport
        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        clock.now = 1_700_000_300  # remaining 200s <= refresh window of 300s
        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        self.assertEqual(len(calls), 2)

    def test_token_not_cached_when_granted_permissions_are_insufficient(self):
        transport = RecordingTransport(permissions={"contents": "read"})
        auth = self.make_auth(transport=transport)

        with self.assertRaises(GitHubAuthInsufficientPermissionError):
            auth.installation_token(
                installation_id=1,
                repository="owner/repo",
                requested_permissions=RequestedPermissions({"contents"}),
            )
        transport.permissions = {"contents": "write"}
        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        self.assertEqual(len(transport.requests), 2)

    def test_transient_failure_clears_cached_token(self):
        clock = FakeClock(1_700_000_000)
        transport = RecordingTransport()
        auth = self.make_auth(transport=transport, clock=clock)
        expires = datetime.fromtimestamp(1_700_000_500, tz=timezone.utc)
        calls = []

        def fixed_expiry_transport(
            *, installation_id, repository, app_jwt, requested_permissions
        ):
            calls.append((installation_id, repository))
            if transport.error is not None:
                raise transport.error
            return InstallationToken(
                token="ghs_fixed_expiry",
                expires_at=expires,
                permissions={"contents": "write"},
            )

        transport.request_installation_token = fixed_expiry_transport
        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        clock.now = 1_700_000_300
        transport.error = GitHubAuthTransientError("temporary")

        with self.assertRaises(GitHubAuthTransientError):
            auth.installation_token(
                installation_id=1,
                repository="owner/repo",
                requested_permissions=RequestedPermissions({"contents"}),
            )
        transport.error = None
        auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )
        self.assertEqual(len(calls), 3)

    def test_http_error_mapping_keeps_static_messages(self):
        cases = (
            (
                404,
                '{"message":"not found PRIVATE_BODY"}',
                GitHubAuthUnavailableInstallationError,
            ),
            (
                403,
                '{"message":"missing permission PRIVATE_BODY"}',
                GitHubAuthInsufficientPermissionError,
            ),
            (
                403,
                '{"message":"rate limit exceeded PRIVATE_BODY"}',
                GitHubAuthTransientError,
            ),
            (
                429,
                '{"message":"rate limit exceeded PRIVATE_BODY"}',
                GitHubAuthTransientError,
            ),
            (500, '{"message":"server PRIVATE_BODY"}', GitHubAuthTransientError),
            (
                401,
                '{"message":"bad config PRIVATE_BODY"}',
                GitHubAuthConfigurationError,
            ),
            (
                422,
                '{"message":"bad config PRIVATE_BODY"}',
                GitHubAuthConfigurationError,
            ),
        )
        for status, body_text, expected in cases:
            with self.subTest(status=status):
                transport = GitHubTokenTransport()
                raised = transport._map_http_error(status, body_text)
                self.assertIsInstance(raised, expected)
                self.assertNotIn("PRIVATE_BODY", str(raised))

    def test_transport_returns_opaque_variable_length_token_without_prefix_logic(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse(
                b'{"token":"very-long-token-that-must-be-opaque-123",'
                b'"expires_at":"2030-01-01T00:00:00Z",'
                b'"permissions":{"contents":"write"}}'
            )

        auth = self.make_auth(transport=GitHubTokenTransport(opener=opener))
        token = auth.installation_token(
            installation_id=1,
            repository="owner/repo",
            requested_permissions=RequestedPermissions({"contents"}),
        )

        self.assertEqual(token.token, "very-long-token-that-must-be-opaque-123")

    def test_invalid_scope_and_permission_inputs_fail_closed(self):
        with self.assertRaises(GitHubAuthConfigurationError):
            InstallationTokenScope(0, "owner/repo", frozenset())
        with self.assertRaises(GitHubAuthConfigurationError):
            InstallationTokenScope(1, "bad slug", frozenset())
        with self.assertRaises(GitHubAuthConfigurationError):
            RequestedPermissions({"not_a_real_permission"})

    def test_private_key_sources_load_bounded_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.pem"
            path.write_bytes(self.private_pem)
            source = FilePrivateKeySource(path)
            self.assertEqual(source.load_private_key_pem(), self.private_pem)

        try:
            os.environ["TEST_GITHUB_APP_PRIVATE_KEY"] = self.private_pem.decode("ascii")
            source = EnvPrivateKeySource("TEST_GITHUB_APP_PRIVATE_KEY")
            self.assertEqual(source.load_private_key_pem(), self.private_pem)
        finally:
            os.environ.pop("TEST_GITHUB_APP_PRIVATE_KEY", None)

    def test_errors_are_review_sensei_errors_with_stable_categories(self):
        auth = GitHubAppAuth(
            app_id=123,
            private_key_source=BytesPrivateKeySource(b"not a key"),
            clock=FakeClock(1_700_000_000),
        )
        with self.assertRaises(ReviewSenseiError) as raised:
            auth.create_app_jwt()
        self.assertEqual(raised.exception.error_category, "github_auth_configuration")


if __name__ == "__main__":
    unittest.main()
