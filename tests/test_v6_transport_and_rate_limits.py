"""Unit tests for ReviewSensei v6 dual-credential transport and classifiers."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from email.message import Message
from typing import Any

from review_sensei_github.errors import (
    GitHubHTTPError,
    GitHubHTTPPermissionError,
    GitHubHTTPRateLimitError,
    GitHubHTTPTransientError,
)
from review_sensei_github.v6_credential_matrix import (
    WORKFLOW_READ_ONLY_SCOPES,
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
    V6CredentialPermissionError,
)
from review_sensei_github.v6_transport import (
    APP_BROKER_DEFAULT_LIMIT,
    MAINTAINER_RERUN_COMMAND,
    WORKFLOW_TOKEN_DEFAULT_LIMIT,
    WORKFLOW_TOKEN_GHEC_LIMIT,
    CredentialQuotaTracker,
    DualCredentialGitHubClient,
    DualCredentialResponse,
    RateLimitSnapshot,
    ThrottleDecision,
    classify_github_response,
    evaluate_throttle_wait,
)

from review_sensei.errors import ReviewInputError


class RecordingTransport:
    """In-memory stub implementing ``DualCredentialTransport``."""

    def __init__(
        self,
        responses: list[tuple[int, Mapping[str, str] | Message, Any]] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": dict(headers or {}),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self._responses:
            raise AssertionError("RecordingTransport received unexpected call")
        status, resp_headers, resp_body = self._responses.pop(0)
        if isinstance(resp_headers, Message):
            return status, dict(resp_headers.items()), resp_body
        return status, resp_headers, resp_body


class ResponseClassifierTests(unittest.TestCase):
    """Verify strict separation between REST/GraphQL rate limits and 403 permissions."""

    def test_rest_403_and_429_primary_rate_limit(self) -> None:
        for status, headers, body in (
            (
                403,
                {
                    "x-ratelimit-remaining": "0",
                    "X-RateLimit-Reset": "1764176400",
                    "X-RateLimit-Resource": "core",
                },
                {"message": "API rate limit exceeded for installation ID 1"},
            ),
            (
                429,
                {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1764176500",
                },
                {"message": "API rate limit exceeded"},
            ),
            (
                403,
                {},
                {"message": "API rate limit exceeded"},
            ),
        ):
            with self.subTest(status=status, headers=headers):
                with self.assertRaises(GitHubHTTPRateLimitError) as caught:
                    classify_github_response(
                        status,
                        headers,
                        body,
                        credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                        endpoint_id="pr_files",
                    )
                err = caught.exception
                self.assertIsInstance(err, GitHubHTTPTransientError)
                self.assertNotIsInstance(err, GitHubHTTPPermissionError)
                self.assertEqual(err.error_category, "github_http_rate_limit")
                self.assertEqual(err.rate_limit_kind, "primary")
                self.assertEqual(
                    err.credential_kind, CredentialKind.WORKFLOW_GITHUB_TOKEN.value
                )
                self.assertEqual(err.http_status, status)
                self.assertEqual(err.endpoint_id, "pr_files")

    def test_rest_403_and_429_secondary_rate_limit(self) -> None:
        for status, headers, body in (
            (
                403,
                {"Retry-After": "45", "X-RateLimit-Remaining": "900"},
                {"message": "You have exceeded a secondary rate limit."},
            ),
            (
                403,
                {"retry-after": "Wed, 26 Nov 2026 16:01:00 GMT"},
                {"message": "forbidden"},
            ),
            (
                403,
                {"X-RateLimit-Remaining": "10"},
                {"message": "Request blocked by abuse detection mechanism"},
            ),
            (
                429,
                {"Retry-After": "30"},
                {"message": "You have exceeded a secondary rate limit."},
            ),
            (
                429,
                {},
                {},
            ),
        ):
            with self.subTest(status=status, headers=headers, body=body):
                with self.assertRaises(GitHubHTTPRateLimitError) as caught:
                    classify_github_response(
                        status,
                        headers,
                        body,
                        credential_kind=CredentialKind.APP_BROKER,
                        endpoint_id="pr_review_publish",
                        now_epoch=1795708800.0,
                    )
                err = caught.exception
                self.assertEqual(err.rate_limit_kind, "secondary")
                self.assertEqual(err.credential_kind, CredentialKind.APP_BROKER.value)
                self.assertEqual(err.http_status, status)
                self.assertEqual(err.endpoint_id, "pr_review_publish")

    def test_graphql_http_200_rate_limited_classified_as_throttle(self) -> None:
        cases = (
            (
                "type_rate_limited",
                {
                    "data": {
                        "repository": None,
                        "rateLimit": {
                            "limit": 1000,
                            "remaining": 0,
                            "resetAt": "2026-11-26T16:15:00Z",
                            "cost": 1,
                        },
                    },
                    "errors": [
                        {
                            "type": "RATE_LIMITED",
                            "message": "API rate limit already exceeded for user ID 1.",
                        }
                    ],
                },
            ),
            (
                "message_secondary_rate_limit",
                {
                    "data": None,
                    "errors": [
                        {
                            "type": "FORBIDDEN",
                            "message": "You have exceeded a secondary rate limit.",
                        }
                    ],
                },
            ),
            (
                "zero_remaining_with_null_target_data",
                {
                    "data": {
                        "repository": None,
                        "rateLimit": {
                            "limit": 1000,
                            "remaining": 0,
                            "resetAt": "2026-11-26T16:15:00Z",
                            "cost": 1,
                        },
                    },
                },
            ),
        )
        for label, body in cases:
            with self.subTest(case=label):
                with self.assertRaises(GitHubHTTPRateLimitError) as caught:
                    classify_github_response(
                        200,
                        {},
                        body,
                        credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                        endpoint_id="graphql_review_threads",
                        is_graphql=True,
                    )
                err = caught.exception
                self.assertEqual(err.rate_limit_kind, "graphql_200")
                self.assertEqual(err.http_status, 200)
                self.assertEqual(err.resource, "graphql")
                if label != "message_secondary_rate_limit":
                    self.assertEqual(err.remaining, 0)
                    self.assertIsNotNone(err.reset_epoch)

        # Zero remaining WITHOUT errors and WITH non-null target data succeeds.
        classify_github_response(
            200,
            {},
            {
                "data": {
                    "repository": {"pullRequest": {"id": "PR_1"}},
                    "rateLimit": {
                        "limit": 1000,
                        "remaining": 0,
                        "resetAt": "2026-11-26T16:15:00Z",
                        "cost": 1,
                    },
                }
            },
            credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            endpoint_id="graphql_review_threads",
            is_graphql=True,
        )

    def test_permission_403_never_classified_as_rate_limit(self) -> None:
        for headers, body in (
            (
                {"X-RateLimit-Remaining": "998"},
                {"message": "Resource not accessible by integration"},
            ),
            (
                {"Retry-After": "0"},
                {"message": "Resource not accessible by integration"},
            ),
            (
                {"X-RateLimit-Remaining": "10"},
                {"message": "API rate limit exceeded"},
            ),
        ):
            with self.subTest(headers=headers, body=body):
                with self.assertRaises(GitHubHTTPPermissionError) as caught:
                    classify_github_response(
                        403,
                        headers,
                        body,
                        credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                        endpoint_id="pr_metadata",
                    )
                err = caught.exception
                self.assertIsInstance(err, GitHubHTTPError)
                self.assertNotIsInstance(err, GitHubHTTPTransientError)
                self.assertEqual(err.error_category, "github_http_permission")
                self.assertEqual(err.http_status, 403)
                self.assertEqual(err.endpoint_id, "pr_metadata")
                self.assertEqual(
                    err.credential_kind, CredentialKind.WORKFLOW_GITHUB_TOKEN.value
                )

    def test_error_validation_and_v6_permission_inheritance(self) -> None:
        with self.assertRaises(ValueError):
            GitHubHTTPRateLimitError("bad kind", rate_limit_kind="invalid")
        with self.assertRaises(ValueError):
            GitHubHTTPRateLimitError("bad retry", retry_after_seconds=-1.0)
        v6_err = V6CredentialPermissionError(
            "missing checks: read",
            endpoint_id="check_runs_and_suites",
            credential=CredentialKind.WORKFLOW_GITHUB_TOKEN,
            missing_scopes=("checks: read",),
        )
        self.assertIsInstance(v6_err, GitHubHTTPPermissionError)
        self.assertEqual(v6_err.http_status, 403)
        self.assertEqual(v6_err.missing_scope, "checks: read")


class DualCredentialTransportTests(unittest.TestCase):
    """Verify transport isolation, independent quotas, and checkpoint reserve."""

    def test_permission_403_on_bulk_read_performs_zero_retries_and_zero_app_calls(
        self,
    ) -> None:
        workflow_transport = RecordingTransport(
            [
                (
                    403,
                    {"X-RateLimit-Remaining": "999", "X-RateLimit-Limit": "1000"},
                    {"message": "Resource not accessible by integration"},
                )
            ]
        )
        app_transport = RecordingTransport()
        client = DualCredentialGitHubClient(
            workflow_token_transport=workflow_transport,
            broker_app_transport=app_transport,
        )

        with self.assertRaises(GitHubHTTPPermissionError) as caught:
            client.execute(
                "pr_issue_comments",
                method="GET",
                path="/repos/owner/repo/issues/1/comments",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.FORK_PULL_REQUEST,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            )

        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(caught.exception.endpoint_id, "pr_issue_comments")
        self.assertEqual(
            caught.exception.credential_kind,
            CredentialKind.WORKFLOW_GITHUB_TOKEN.value,
        )
        self.assertEqual(len(workflow_transport.calls), 1)
        self.assertEqual(len(app_transport.calls), 0)

        # Missing granted scope fails before touching either transport.
        with self.assertRaises(V6CredentialPermissionError):
            client.execute(
                "check_runs_and_suites",
                method="GET",
                path="/repos/owner/repo/commits/abc/check-runs",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=("pull-requests: read",),
            )
        self.assertEqual(len(workflow_transport.calls), 1)
        self.assertEqual(len(app_transport.calls), 0)

        # Explicit App fallback on a bulk read is forbidden before dispatch.
        with self.assertRaises(ReviewInputError) as fallback_err:
            client.execute(
                "pr_files",
                method="GET",
                path="/repos/owner/repo/pulls/1/files",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                credential_kind=CredentialKind.APP_BROKER,
            )
        self.assertEqual(fallback_err.exception.diagnostic, "v6_app_fallback_forbidden")
        self.assertEqual(len(app_transport.calls), 0)

    def test_independent_quota_tracking_and_ghec_ceiling(self) -> None:
        standard_tracker = CredentialQuotaTracker(CredentialKind.WORKFLOW_GITHUB_TOKEN)
        ghec_tracker = CredentialQuotaTracker(
            CredentialKind.WORKFLOW_GITHUB_TOKEN, is_ghec=True
        )
        app_tracker = CredentialQuotaTracker(CredentialKind.APP_BROKER)

        self.assertEqual(standard_tracker.limit, WORKFLOW_TOKEN_DEFAULT_LIMIT)
        self.assertEqual(ghec_tracker.limit, WORKFLOW_TOKEN_GHEC_LIMIT)
        self.assertEqual(app_tracker.limit, APP_BROKER_DEFAULT_LIMIT)

        workflow_transport = RecordingTransport(
            [
                (
                    200,
                    {
                        "X-RateLimit-Limit": "15000",
                        "X-RateLimit-Remaining": "14990",
                        "X-RateLimit-Reset": "1764176400",
                        "X-RateLimit-Resource": "core",
                        "Link": (
                            "<https://api.github.com/repos/o/r/issues/1/comments?page=2>; "
                            'rel="next", '
                            "<https://api.github.com/repos/o/r/issues/1/comments?page=4>; "
                            'rel="last"'
                        ),
                    },
                    [{"id": 1}],
                ),
                (
                    200,
                    {},
                    {
                        "data": {
                            "repository": {"pullRequest": {"id": "PR_1"}},
                            "rateLimit": {
                                "limit": 15000,
                                "remaining": 0,
                                "resetAt": "2026-11-26T17:00:00Z",
                                "cost": 2,
                            },
                        },
                        "errors": [{"type": "RATE_LIMITED", "message": "rate limit"}],
                    },
                ),
            ]
        )
        app_transport = RecordingTransport(
            [
                (
                    201,
                    {
                        "X-RateLimit-Limit": "5000",
                        "X-RateLimit-Remaining": "4980",
                        "X-RateLimit-Reset": "1764177000",
                        "X-RateLimit-Resource": "core",
                    },
                    {"id": 999},
                )
            ]
        )

        client = DualCredentialGitHubClient(
            workflow_token_transport=workflow_transport,
            broker_app_transport=app_transport,
            workflow_quota=ghec_tracker,
            app_quota=app_tracker,
        )

        resp = client.execute(
            "pr_issue_comments",
            method="GET",
            path="/repos/o/r/issues/1/comments",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
        )
        self.assertIsInstance(resp, DualCredentialResponse)
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            resp.link_next_url,
            "https://api.github.com/repos/o/r/issues/1/comments?page=2",
        )
        self.assertIsInstance(resp.quota_snapshot, RateLimitSnapshot)
        self.assertEqual(client.workflow_quota.remaining, 14990)
        self.assertIsNone(client.app_quota.remaining)

        # Exhausting WORKFLOW_GITHUB_TOKEN via GraphQL 200 leaves APP_BROKER intact.
        with self.assertRaises(GitHubHTTPRateLimitError) as gql_err:
            client.execute(
                "graphql_review_threads",
                method="POST",
                path="/graphql",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                body={"query": "query {}"},
                is_graphql=True,
            )
        self.assertEqual(gql_err.exception.rate_limit_kind, "graphql_200")
        self.assertEqual(client.workflow_quota.remaining, 0)
        self.assertTrue(client.workflow_quota.rate_limited)
        self.assertEqual(client.workflow_quota.last_graphql_cost, 2)

        # App broker can still persist a checkpoint cleanly.
        checkpoint_resp = client.execute(
            "write_intent_comment",
            method="POST",
            path="/repos/o/r/issues/1/comments",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"body": "intent"},
        )
        self.assertEqual(checkpoint_resp.status, 201)
        self.assertEqual(client.app_quota.remaining, 4980)
        self.assertFalse(client.app_quota.rate_limited)
        self.assertEqual(len(workflow_transport.calls), 2)
        self.assertEqual(len(app_transport.calls), 1)

    def test_app_checkpoint_reserve_protects_checkpoint_mutations(self) -> None:
        workflow_transport = RecordingTransport()
        app_transport = RecordingTransport(
            [
                (
                    200,
                    {
                        "X-RateLimit-Limit": "5000",
                        "X-RateLimit-Remaining": "18",
                        "X-RateLimit-Reset": "1764177000",
                    },
                    {"id": 1},
                ),
                (
                    201,
                    {
                        "X-RateLimit-Limit": "5000",
                        "X-RateLimit-Remaining": "17",
                        "X-RateLimit-Reset": "1764177000",
                    },
                    {"id": 2},
                ),
            ]
        )
        client = DualCredentialGitHubClient(
            workflow_token_transport=workflow_transport,
            broker_app_transport=app_transport,
            app_checkpoint_reserve=20,
        )

        # First call brings App remaining down to 18 (<= reserve of 20).
        client.execute(
            "broker_app_identity",
            method="GET",
            path="/app",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
        )
        self.assertEqual(client.app_quota.remaining, 18)

        # Non-checkpoint mutation is blocked without calling broker_app_transport.
        with self.assertRaises(GitHubHTTPRateLimitError) as reserve_err:
            client.execute(
                "pr_review_publish",
                method="POST",
                path="/repos/o/r/pulls/1/reviews",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                body={"event": "COMMENT", "body": "review"},
            )
        self.assertEqual(reserve_err.exception.remaining, 18)
        self.assertEqual(len(app_transport.calls), 1)

        # Checkpoint write with override=True succeeds using the reserved headroom.
        saved = client.execute(
            "completion_receipt_comment",
            method="POST",
            path="/repos/o/r/issues/1/comments",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"body": "receipt"},
            allow_checkpoint_reserve_override=True,
        )
        self.assertEqual(saved.status, 201)
        self.assertEqual(client.app_quota.remaining, 17)
        self.assertEqual(len(app_transport.calls), 2)


class ThrottleDecisionTests(unittest.TestCase):
    """Verify in-job sleep vs. v6.0 maintainer-rerun pause decisions."""

    def test_short_wait_sleeps_and_long_or_tight_wait_pauses(self) -> None:
        short_err = GitHubHTTPRateLimitError(
            "secondary",
            rate_limit_kind="secondary",
            retry_after_seconds=45.0,
        )
        sleep_decision = evaluate_throttle_wait(
            short_err,
            max_in_job_sleep_seconds=300.0,
            remaining_job_seconds=600.0,
            now_epoch=1000.0,
        )
        self.assertIsInstance(sleep_decision, ThrottleDecision)
        self.assertEqual(sleep_decision.action, "sleep")
        self.assertEqual(sleep_decision.wait_seconds, 45.0)
        self.assertEqual(sleep_decision.resume_after_epoch, 1045.0)
        self.assertIsNone(sleep_decision.rerun_command)

        with self.assertRaises(FrozenInstanceError):
            sleep_decision.action = "pause_for_maintainer_rerun"  # type: ignore[misc]

        # Long wait exceeds max_in_job_sleep_seconds -> pause for maintainer rerun.
        long_err = GitHubHTTPRateLimitError(
            "primary",
            rate_limit_kind="primary",
            reset_epoch=2000,
        )
        long_decision = evaluate_throttle_wait(
            long_err,
            max_in_job_sleep_seconds=300.0,
            remaining_job_seconds=1800.0,
            now_epoch=1000.0,
        )
        self.assertEqual(long_decision.action, "pause_for_maintainer_rerun")
        self.assertEqual(long_decision.wait_seconds, 1000.0)
        self.assertEqual(long_decision.resume_after_epoch, 2000.0)
        self.assertEqual(long_decision.rerun_command, MAINTAINER_RERUN_COMMAND)

        # Short wait with insufficient remaining job time (inside checkpoint margin).
        tight_decision = evaluate_throttle_wait(
            short_err,
            max_in_job_sleep_seconds=300.0,
            remaining_job_seconds=60.0,
            now_epoch=1000.0,
        )
        self.assertEqual(tight_decision.action, "pause_for_maintainer_rerun")
        self.assertEqual(tight_decision.rerun_command, MAINTAINER_RERUN_COMMAND)

    def test_exponential_backoff_with_deterministic_jitter_and_validation(self) -> None:
        bare_err = GitHubHTTPRateLimitError("graphql", rate_limit_kind="graphql_200")
        d1 = evaluate_throttle_wait(
            bare_err,
            remaining_job_seconds=600.0,
            attempt=1,
            now_epoch=1000.0,
            base_backoff_seconds=2.0,
            max_backoff_seconds=16.0,
            jitter_seconds=0.5,
        )
        d3 = evaluate_throttle_wait(
            bare_err,
            remaining_job_seconds=600.0,
            attempt=3,
            now_epoch=1000.0,
            base_backoff_seconds=2.0,
            max_backoff_seconds=16.0,
            jitter_seconds=0.5,
        )
        d6 = evaluate_throttle_wait(
            bare_err,
            remaining_job_seconds=600.0,
            attempt=6,
            now_epoch=1000.0,
            base_backoff_seconds=2.0,
            max_backoff_seconds=16.0,
            jitter_seconds=0.5,
        )
        self.assertEqual(d1.wait_seconds, 2.5)
        self.assertEqual(d3.wait_seconds, 8.5)
        self.assertEqual(d6.wait_seconds, 16.5)

        for bad_kwargs in (
            {"remaining_job_seconds": -1.0},
            {"remaining_job_seconds": float("nan")},
            {"remaining_job_seconds": True},
            {"remaining_job_seconds": 100.0, "max_in_job_sleep_seconds": -5.0},
            {"remaining_job_seconds": 100.0, "attempt": 0},
            {"remaining_job_seconds": 100.0, "jitter_seconds": -0.1},
        ):
            with self.subTest(bad_kwargs=bad_kwargs), self.assertRaises(ValueError):
                evaluate_throttle_wait(bare_err, **bad_kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
