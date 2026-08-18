import io
import unittest
from unittest import mock
from urllib.error import HTTPError

try:
    from test_github_auth import (
        BytesPrivateKeySource,
        FakeClock,
        FakeResponse,
        RecordingTransport,
        make_key_pair,
    )
except ModuleNotFoundError:
    from tests.test_github_auth import (
        BytesPrivateKeySource,
        FakeClock,
        FakeResponse,
        RecordingTransport,
        make_key_pair,
    )

from review_sensei.hosting.github import (
    ApprovedWorkflow,
    AuditRecord,
    BrokerPolicy,
    BrokerRateLimitError,
    BrokerRejectionError,
    GitHubAppAuth,
    GitHubAuthInsufficientPermissionError,
    GitHubAuthTransientError,
    GitHubAuthUnavailableInstallationError,
    GitHubOIDCInvalidTokenError,
    InMemoryAuditSink,
    InMemoryInstallationMapping,
    OIDCBroker,
    RequestedPermissions,
    TokenBucketRateLimiter,
    VerifiedOIDCClaims,
)
from review_sensei.hosting.github.broker import (
    MAX_AUDIT_RECORDS_IN_MEMORY,
    GitHubForkChecker,
    GitHubInstallationLookup,
)


class StubForkChecker:
    def __init__(self, value=False, error=None):
        self.value = value
        self.error = error

    def is_fork(self, owner, repo):
        if self.error is not None:
            raise self.error
        return self.value


class StubRateLimiter:
    def __init__(self, value):
        self.value = value
        self.keys = []

    def allow(self, key, now):
        self.keys.append((key, now))
        return self.value


class StubAuth:
    def __init__(self, token=None, error=None):
        self.token = token
        self.error = error
        self.calls = []

    def installation_token(self, *, installation_id, repository, requested_permissions):
        self.calls.append((installation_id, repository, requested_permissions))
        if self.error is not None:
            raise self.error
        return self.token


class GitHubBrokerTests(unittest.TestCase):
    now = 1_700_000_000

    def setUp(self):
        self.private_pem, self.public_pem = make_key_pair()
        self.transport = RecordingTransport(token_value="ghs_installation_secret")
        self.auth = GitHubAppAuth(
            app_id=123,
            private_key_source=BytesPrivateKeySource(self.private_pem),
            transport=self.transport,
            clock=FakeClock(self.now),
            cache_enabled=False,
        )
        self.claims = VerifiedOIDCClaims(
            sub="repo:owner/repo:ref:refs/heads/main",
            repository="owner/repo",
            repository_owner="owner",
            repository_id=1234,
            installation_id=7,
            workflow="Review",
            workflow_ref="owner/repo/.github/workflows/review.yml@refs/heads/main",
            workflow_sha="a" * 40,
            event="push",
            ref="refs/heads/main",
            job_workflow_ref="owner/repo/.github/workflows/review.yml@refs/heads/main",
            aud="sts.reviewsensei.dev",
            iss="https://token.actions.githubusercontent.com",
            exp=self.now + 300,
            iat=self.now - 10,
        )
        self.workflow = ApprovedWorkflow(
            repository="owner/repo",
            workflow_ref=self.claims.workflow_ref,
            ref="refs/heads/main",
        )
        self.policy = BrokerPolicy(
            expected_issuer=self.claims.iss,
            expected_audience=self.claims.aud,
            approved_workflows=frozenset({self.workflow}),
            requested_permissions=RequestedPermissions({"contents"}),
            clock_skew_seconds=30,
            rate_limit_window_seconds=60,
            rate_limit_max_requests=10,
        )
        self.sink = InMemoryAuditSink()
        self.clock = FakeClock(self.now)
        self.mapping = InMemoryInstallationMapping({"owner/repo": 7})

    def make_broker(self, *, auth=None, fork_checker=None, rate_limiter=None):
        return OIDCBroker(
            auth=auth or self.auth,
            policy=self.policy,
            jwks_fetcher=object(),
            installation_mapping=self.mapping,
            fork_checker=fork_checker or StubForkChecker(False),
            rate_limiter=rate_limiter,
            audit_sink=self.sink,
            clock=self.clock,
            request_id_factory=lambda: "request-id",
        )

    def exchange(self, broker=None, token="oidc.token.secret"):
        with mock.patch(
            "review_sensei.hosting.github.broker.verify_oidc_token",
            return_value=self.claims,
        ):
            return (broker or self.make_broker()).exchange(token)

    def test_trusted_workflow_gets_repo_scoped_token(self):
        token = self.exchange()
        self.assertEqual(token.token, "ghs_installation_secret")
        self.assertEqual(len(self.transport.requests), 1)
        request = self.transport.requests[0]
        self.assertEqual(request["installation_id"], 7)
        self.assertEqual(request["repository"], "owner/repo")
        self.assertEqual(
            request["requested_permissions"].values, frozenset({"contents"})
        )

    def test_unapproved_repository_rejected(self):
        claims = self.claims
        unapproved = VerifiedOIDCClaims(
            **{**claims.__dict__, "repository": "other/repo"}
        )
        broker = self.make_broker()
        with (
            mock.patch(
                "review_sensei.hosting.github.broker.verify_oidc_token",
                return_value=unapproved,
            ),
            self.assertRaises(BrokerRejectionError),
        ):
            broker.exchange("oidc.token")
        self.assertEqual(self.sink.records[-1].reason_category, "broker_rejection")

    def test_unapproved_workflow_ref_rejected(self):
        claims = VerifiedOIDCClaims(
            **{
                **self.claims.__dict__,
                "workflow_ref": "owner/repo/.github/workflows/other.yml@refs/heads/main",
            }
        )
        broker = self.make_broker()
        with (
            mock.patch(
                "review_sensei.hosting.github.broker.verify_oidc_token",
                return_value=claims,
            ),
            self.assertRaises(BrokerRejectionError),
        ):
            broker.exchange("oidc.token")

    def test_unapproved_ref_rejected(self):
        claims = VerifiedOIDCClaims(
            **{**self.claims.__dict__, "ref": "refs/heads/release"}
        )
        broker = self.make_broker()
        with (
            mock.patch(
                "review_sensei.hosting.github.broker.verify_oidc_token",
                return_value=claims,
            ),
            self.assertRaises(BrokerRejectionError),
        ):
            broker.exchange("oidc.token")

    def test_fork_rejected(self):
        broker = self.make_broker(fork_checker=StubForkChecker(True))
        with self.assertRaises(BrokerRejectionError):
            self.exchange(broker)
        self.assertEqual(self.sink.records[-1].reason_category, "broker_rejection")

    def test_fork_checker_failure_fails_closed(self):
        broker = self.make_broker(fork_checker=StubForkChecker(error=RuntimeError()))
        with self.assertRaises(BrokerRejectionError):
            self.exchange(broker)

    def test_no_installation_mapping_rejected(self):
        self.mapping = InMemoryInstallationMapping()
        with self.assertRaises(BrokerRejectionError):
            self.exchange()
        self.assertIsNone(self.sink.records[-1].installation_id)

    def test_rate_limit_exceeded_rejected(self):
        broker = self.make_broker(rate_limiter=StubRateLimiter(False))
        with self.assertRaises(BrokerRateLimitError):
            self.exchange(broker)
        self.assertEqual(self.sink.records[-1].reason_category, "broker_rate_limit")

    def test_suspended_installation_propagates_auth_error(self):
        error = GitHubAuthUnavailableInstallationError("installation unavailable")
        broker = self.make_broker(
            auth=StubAuth(error=error),
        )
        with self.assertRaises(GitHubAuthUnavailableInstallationError):
            self.exchange(broker)
        self.assertEqual(
            self.sink.records[-1].reason_category,
            "github_auth_unavailable_installation",
        )

    def test_insufficient_permissions_propagates_auth_error(self):
        error = GitHubAuthInsufficientPermissionError("permission denied")
        with self.assertRaises(GitHubAuthInsufficientPermissionError):
            self.exchange(self.make_broker(auth=StubAuth(error=error)))
        self.assertEqual(
            self.sink.records[-1].reason_category,
            "github_auth_insufficient_permission",
        )

    def test_transient_github_failure_propagates(self):
        error = GitHubAuthTransientError("transient")
        with self.assertRaises(GitHubAuthTransientError):
            self.exchange(self.make_broker(auth=StubAuth(error=error)))
        self.assertEqual(self.sink.records[-1].reason_category, "github_auth_transient")

    def test_audit_record_contains_no_secrets(self):
        oidc_token = "oidc-token-secret"
        app_jwt = ""
        self.exchange(token=oidc_token)
        app_jwt = self.transport.requests[0]["app_jwt"]
        records = list(self.sink.records)
        broker = self.make_broker(fork_checker=StubForkChecker(True))
        with self.assertRaises(BrokerRejectionError):
            self.exchange(broker, token=oidc_token)
        for record in self.sink.records:
            self.assertNotIn(oidc_token, str(record))
            self.assertNotIn("ghs_installation_secret", str(record))
            self.assertNotIn(app_jwt, str(record))
        self.assertEqual(len(records), 1)

    def test_audit_record_verdict_and_reason_set(self):
        self.exchange()
        self.assertEqual(self.sink.records[-1].verdict, "issued")
        self.assertEqual(self.sink.records[-1].reason_category, "ok")
        with self.assertRaises(BrokerRejectionError):
            self.exchange(self.make_broker(fork_checker=StubForkChecker(True)))
        self.assertEqual(self.sink.records[-1].verdict, "rejected")
        self.assertEqual(self.sink.records[-1].reason_category, "broker_rejection")

    def test_oidc_verification_failure_audited(self):
        broker = self.make_broker()
        with (
            mock.patch(
                "review_sensei.hosting.github.broker.verify_oidc_token",
                side_effect=GitHubOIDCInvalidTokenError("OIDC token was invalid"),
            ),
            self.assertRaises(GitHubOIDCInvalidTokenError),
        ):
            broker.exchange("invalid-token")
        record = self.sink.records[-1]
        self.assertEqual(record.verdict, "rejected")
        self.assertEqual(record.reason_category, "github_oidc_invalid_token")
        self.assertIsNone(record.installation_id)
        self.assertIsNone(record.repository)

    def test_ref_wildcard_allows_any_ref(self):
        self.policy = BrokerPolicy(
            expected_issuer=self.policy.expected_issuer,
            expected_audience=self.policy.expected_audience,
            approved_workflows=frozenset(
                {
                    ApprovedWorkflow(
                        repository=self.claims.repository,
                        workflow_ref=self.claims.workflow_ref,
                        ref="*",
                    )
                }
            ),
            requested_permissions=self.policy.requested_permissions,
            clock_skew_seconds=30,
            rate_limit_window_seconds=60,
            rate_limit_max_requests=10,
        )
        claims = VerifiedOIDCClaims(
            **{**self.claims.__dict__, "ref": "refs/pull/123/merge"}
        )
        broker = self.make_broker()
        with mock.patch(
            "review_sensei.hosting.github.broker.verify_oidc_token", return_value=claims
        ):
            token = broker.exchange("oidc-token")
        self.assertEqual(token.token, "ghs_installation_secret")

    def test_in_memory_audit_sink_bounded(self):
        for index in range(MAX_AUDIT_RECORDS_IN_MEMORY + 5):
            self.sink.record(
                AuditRecord(
                    timestamp=float(index),
                    request_id=str(index),
                    installation_id=None,
                    repository=None,
                    workflow_ref=None,
                    event=None,
                    verdict="rejected",
                    reason_category="broker_rejection",
                )
            )
        self.assertEqual(len(self.sink.records), MAX_AUDIT_RECORDS_IN_MEMORY)
        self.assertEqual(self.sink.records[0].request_id, "5")

    def test_token_bucket_rate_limiter_window_and_cap(self):
        limiter = TokenBucketRateLimiter(window_seconds=60, max_requests=2)
        key = (7, "owner/repo", self.claims.workflow_ref)
        self.assertTrue(limiter.allow(key, 1000))
        self.assertTrue(limiter.allow(key, 1001))
        self.assertFalse(limiter.allow(key, 1002))
        self.assertTrue(limiter.allow(key, 1061))

    def test_github_installation_lookup_happy_path_and_failure(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            return FakeResponse(b'{"id":7}')

        lookup = GitHubInstallationLookup(
            auth=self.auth,
            api_url="https://api.github.test",
            opener=opener,
        )
        self.assertEqual(lookup.resolve("owner", "repo"), 7)
        self.assertEqual(
            captured["url"], "https://api.github.test/repos/owner/repo/installation"
        )
        self.assertTrue(captured["authorization"].startswith("Bearer "))

        failing = GitHubInstallationLookup(
            auth=self.auth,
            opener=lambda request, timeout: (_ for _ in ()).throw(RuntimeError()),
        )
        self.assertIsNone(failing.resolve("owner", "repo"))

    def test_github_fork_checker_authenticated_reads_fork_and_fails_closed(self):
        captured = {}

        def opener(request, timeout):
            captured["authorization"] = request.headers.get("Authorization")
            captured["url"] = request.full_url
            return FakeResponse(b'{"fork":false}')

        checker = GitHubForkChecker(
            auth=self.auth,
            api_url="https://api.github.test",
            opener=opener,
        )
        self.assertFalse(checker.is_fork("owner", "repo"))
        self.assertTrue(captured["authorization"].startswith("Bearer "))
        self.assertEqual(captured["url"], "https://api.github.test/repos/owner/repo")
        failing = GitHubForkChecker(
            auth=self.auth,
            opener=lambda request, timeout: (_ for _ in ()).throw(RuntimeError()),
        )
        self.assertTrue(failing.is_fork("owner", "repo"))

    def test_github_fork_checker_404_treated_as_not_fork(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b"{}"))

        checker = GitHubForkChecker(
            auth=self.auth,
            api_url="https://api.github.test",
            opener=opener,
        )
        # 404 after App auth means the App is not installed there; the
        # downstream installation mapping and approved-workflow policy reject
        # it, so the fork checker must not reject private/unknown repos.
        self.assertFalse(checker.is_fork("owner", "repo"))

    def test_github_fork_checker_403_treated_as_not_fork(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO(b"{}"))

        checker = GitHubForkChecker(
            auth=self.auth,
            api_url="https://api.github.test",
            opener=opener,
        )
        self.assertFalse(checker.is_fork("owner", "repo"))

    def test_github_fork_checker_500_fails_closed_as_fork(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 500, "server", {}, io.BytesIO(b"{}"))

        checker = GitHubForkChecker(
            auth=self.auth,
            api_url="https://api.github.test",
            opener=opener,
        )
        self.assertTrue(checker.is_fork("owner", "repo"))

    def test_installation_id_mismatch_rejected(self):
        claims = VerifiedOIDCClaims(**{**self.claims.__dict__, "installation_id": 999})
        broker = self.make_broker()
        with (
            mock.patch(
                "review_sensei.hosting.github.broker.verify_oidc_token",
                return_value=claims,
            ),
            self.assertRaises(BrokerRejectionError) as context,
        ):
            broker.exchange("oidc.token")
        self.assertIn("installation id", str(context.exception))
        self.assertEqual(self.sink.records[-1].reason_category, "broker_rejection")
        self.assertEqual(self.sink.records[-1].installation_id, 7)


if __name__ == "__main__":
    unittest.main()
