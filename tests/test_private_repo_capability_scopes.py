"""Regression checks for private-repo token capabilities and diagnostics."""

from __future__ import annotations

import base64
import copy
import io
import json
import tempfile
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fake_github_http import FakeHTTPResponse, json_response, make_http
from review_sensei_github import (
    ConversationPublisher,
    GitHubBrokerClientError,
    GitHubConversationError,
    GitHubHttp,
    GitHubPublicationError,
    ReviewPublisher,
)
from review_sensei_github.approval import approval_eligibility_from_result
from review_sensei_github.checks import (
    REVIEW_CHECK_EXTERNAL_ID,
    check_outcome_for_result,
)
from review_sensei_github.publication import (
    AssembledReview,
    ReviewApprovalFinalizer,
    _sanitize_graphql_path,
    approval_eligibility_marker,
    finding_index_marker,
    review_result_digest,
)
from review_sensei_github.publication_failure_recovery import resolve_check_read_token
from review_sensei_github.recovery_route_advisor import classify_live_ledger_recovery
from review_sensei_github.review_completion_recovery import (
    _check,
    _confirm_native_effects,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import IDENTITY
from test_historical_recovery_matrix_g03 import _diagnostic_ledger
from test_publication_failure_recovery import HEAD_2, PublicationFailureRecoveryFixture

from review_sensei.models import ReviewResult

HEAD_1 = "b" * 40
BASE_1 = "a" * 40


class _PrivateRepoFailureRecoveryFixture(PublicationFailureRecoveryFixture):
    """Fail `/commits/*/check-runs` with HTTP 403 unless the check-read token is used."""

    def __init__(self, directory: str, *, limit: int = 512) -> None:
        super().__init__(directory, limit=limit)
        self.check_run_auth_headers: list[str] = []

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        auth = request.get_header("Authorization") or ""
        if method == "GET" and "/commits/" in path and path.endswith("/check-runs"):
            self.check_run_auth_headers.append(auth)
            if auth != "Bearer synthetic-check_publish":
                self._meter_if_unmetered(request)
                return json_response(
                    {"message": "Resource not accessible by integration"},
                    403,
                )
        return super().external(request, timeout)


def _header_response(value: object, *, status: int = 200, request_id: str | None):
    resp = FakeHTTPResponse(json.dumps(value).encode("utf-8"), status)
    if request_id is not None:
        resp.headers = {"X-GitHub-Request-Id": request_id}
    return resp


class PrivateRepoCapabilityScopeTests(unittest.TestCase):
    def test_graphql_forbidden_diagnostics_include_sanitized_path_and_request_id(self):
        error_payload = {
            "errors": [
                {
                    "type": "FORBIDDEN",
                    "path": [
                        "repository",
                        "pullRequest",
                        "reviewThreads",
                        "nodes",
                        0,
                        "comments",
                        "nodes",
                        0,
                        "commit",
                    ],
                    "message": (
                        "Resource not accessible by integration "
                        "sensitive-internal-detail"
                    ),
                }
            ]
        }

        def call_site(site: str, response: FakeHTTPResponse):
            http, _ = make_http([response])
            publisher = ReviewPublisher(http=http)
            if site == "suppression":
                return publisher._published_finding_suppression(
                    token="synthetic-tok",
                    repository="owner/repo",
                    pull_request=7,
                    app_slug="reviewsensei[bot]",
                    repository_id=1,
                    head_sha=HEAD_1,
                    base_sha=BASE_1,
                )
            if site == "blocking":
                return ReviewApprovalFinalizer(http=http)._scan_blocking_threads(
                    token="synthetic-tok",
                    repository="owner/repo",
                    pull_request=7,
                    repository_id=1,
                    head_sha=HEAD_1,
                    base_sha=BASE_1,
                    app_slug="reviewsensei[bot]",
                )
            return publisher._has_open_review_threads(
                token="synthetic-tok",
                repository="owner/repo",
                pull_request=7,
            )

        for site in ("suppression", "blocking", "has_unresolved"):
            with self.subTest(site=site):
                resp = _header_response(
                    error_payload,
                    status=200,
                    request_id="ABCD:1234:5678:90AB",
                )
                with self.assertRaises(GitHubPublicationError) as raised:
                    call_site(site, resp)
                message = str(raised.exception)
                self.assertIn("graphql FORBIDDEN", message)
                self.assertIn(
                    "path=repository.pullRequest.reviewThreads.nodes[*].comments.nodes[*].commit",
                    message,
                )
                self.assertIn("request_id=ABCD:1234:5678:90AB", message)
                self.assertNotIn("sensitive-internal-detail", message)

    def test_sanitize_graphql_path_and_request_id_capture_and_reset(self):
        self.assertIsNone(_sanitize_graphql_path("not-a-list"))
        self.assertIsNone(_sanitize_graphql_path([]))
        self.assertIsNone(_sanitize_graphql_path(["repository", "bad segment!"]))
        self.assertIsNone(_sanitize_graphql_path(["repository", True]))
        self.assertIsNone(_sanitize_graphql_path(["repository", -1]))
        self.assertEqual(
            _sanitize_graphql_path(["repository", "pullRequest", "nodes", 3, "commit"]),
            "repository.pullRequest.nodes[*].commit",
        )
        long_path = ["f" for _ in range(20)]
        sanitized_long = _sanitize_graphql_path(long_path)
        self.assertIsNotNone(sanitized_long)
        assert sanitized_long is not None
        self.assertTrue(sanitized_long.endswith("..."))

        def build_http_error(request_id: str) -> HTTPError:
            headers = Message()
            headers["X-GitHub-Request-Id"] = request_id
            return HTTPError(
                "https://api.github.test/graphql",
                403,
                "Forbidden",
                headers,
                io.BytesIO(b'{"message":"Forbidden"}'),
            )

        http, _ = make_http(
            [
                _header_response({"ok": True}, status=200, request_id="REQ-1:2"),
                _header_response({"ok": True}, status=200, request_id="bad req id!"),
                build_http_error("ERR:REQ:999"),
                _header_response({"ok": True}, status=200, request_id=None),
            ]
        )
        status1, _ = http.request("GET", "/repos/owner/repo", token="tok")
        self.assertEqual(status1, 200)
        self.assertEqual(http.last_request_id, "REQ-1:2")

        # Unsafe request IDs are ignored and never leak into diagnostics.
        http.request("GET", "/repos/owner/repo", token="tok")
        self.assertIsNone(http.last_request_id)

        # HTTPError captures safe X-GitHub-Request-Id headers and surfaces via HTTP 403 msg.
        with self.assertRaises(GitHubPublicationError) as pub_exc:
            ReviewPublisher(http=http)._has_open_review_threads(
                token="tok",
                repository="owner/repo",
                pull_request=7,
            )
        self.assertEqual(http.last_request_id, "ERR:REQ:999")
        self.assertIn("request_id=ERR:REQ:999", str(pub_exc.exception))

        # Request ID resets cleanly before each new request.
        http.request("GET", "/repos/owner/repo", token="tok")
        self.assertIsNone(http.last_request_id)

    def test_resolve_check_read_token_caches_and_falls_back_on_broker_error(self):
        calls: list[int] = []

        def provider() -> str:
            calls.append(1)
            return "check-tok-1"

        ledger = SimpleNamespace(
            token="session-tok",
            _quarantine_check_token_provider=provider,
        )
        self.assertEqual(resolve_check_read_token(ledger), "check-tok-1")
        self.assertEqual(resolve_check_read_token(ledger), "check-tok-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(getattr(ledger, "_cached_check_read_token"), "check-tok-1")

        def failing_provider() -> str:
            raise GitHubBrokerClientError("broker offline")

        fallback_ledger = SimpleNamespace(
            token="session-tok",
            _quarantine_check_token_provider=failing_provider,
        )
        self.assertEqual(resolve_check_read_token(fallback_ledger), "session-tok")
        self.assertFalse(hasattr(fallback_ledger, "_cached_check_read_token"))

    def test_private_repo_failure_recovery_uses_check_read_token_across_classifier_and_admission(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = _PrivateRepoFailureRecoveryFixture(directory, limit=512)
            f.provider_comments = []
            self.assertEqual(f.invoke(), 0, f.error)
            f.next_review_run(head_sha=HEAD_2)

            def fail_before_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_before_write):
                self.assertEqual(f.invoke(), 1)
            failed_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(failed_rec)
            assert failed_rec is not None and failed_rec.transaction is not None
            self.assertEqual(failed_rec.transaction.phase, "publication_failed")

            # Readonly classification must propagate check-read token provider onto ro_ledger.
            f.next_review_run(head_sha=HEAD_2)
            f.check_run_auth_headers.clear()
            ledger, _observed = _diagnostic_ledger(f)
            base_opener = ledger.http.opener

            def ro_opener(request, timeout=None):
                path = urlsplit(request.full_url).path
                auth = request.get_header("Authorization") or ""
                if request.get_method() == "GET":
                    if "/commits/" in path and path.endswith("/check-runs"):
                        f.check_run_auth_headers.append(auth)
                        if auth != "Bearer synthetic-check_publish":
                            return json_response(
                                {"message": "Resource not accessible by integration"},
                                403,
                            )
                        head_sha = path.split("/commits/")[1].split("/")[0]
                        rows = [
                            c for c in f.check_runs if c.get("head_sha") == head_sha
                        ]
                        return json_response(
                            {
                                "total_count": len(rows),
                                "check_runs": copy.deepcopy(rows),
                            }
                        )
                    if path.endswith("/pulls/7/reviews"):
                        return json_response(copy.deepcopy(f.reviews))
                    if path.endswith("/pulls/7/comments"):
                        return json_response([])
                return base_opener(request, timeout)

            ledger.http.opener = ro_opener
            ledger.token = "synthetic-review_session"
            ledger._quarantine_check_token_provider = lambda: "synthetic-check_publish"
            classification = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=f.requested_head,
                actions_token="synthetic-actions",
                inspect_settlement_witness=True,
            )
            self.assertNotIn(
                "clean_native_effect_census", classification.missing_witnesses
            )
            self.assertNotIn(
                "valid_pre_external_write_settlement_witness",
                classification.missing_witnesses,
            )
            self.assertIn("Bearer synthetic-check_publish", f.check_run_auth_headers)
            self.assertNotIn(
                "Bearer synthetic-review_session", f.check_run_auth_headers
            )

            # Without a check-read token provider, readonly census falls back to the
            # session token, hits 403 on `/commits/*/check-runs`, and flags missing census.
            f.check_run_auth_headers.clear()
            ledger._quarantine_check_token_provider = None
            ledger._cached_check_read_token = None
            fallback_class = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=f.requested_head,
                actions_token="synthetic-actions",
                inspect_settlement_witness=True,
            )
            self.assertIn("Bearer synthetic-review_session", f.check_run_auth_headers)
            self.assertIn(
                "clean_native_effect_census", fallback_class.missing_witnesses
            )

            # Full successor rollover & admission must succeed against 403-gated check-runs.
            f.check_run_auth_headers.clear()
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertIn("Bearer synthetic-check_publish", f.check_run_auth_headers)
            self.assertNotIn(
                "Bearer synthetic-review_session", f.check_run_auth_headers
            )
            recovered = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(recovered)
            assert recovered is not None and recovered.transaction is not None
            self.assertEqual(recovered.transaction.phase, "publication_succeeded")

    def test_confirm_native_effects_uses_check_token_while_keeping_pr_reads_on_token(
        self,
    ):
        result = ReviewResult(summary="Summary.", comments=(), provider="ollama")
        res_digest = review_result_digest(result)
        assembled = AssembledReview(
            inline_comments=(),
            index_entries=(),
            head_sha=HEAD_1,
            state="COMMENT",
            reason=None,
            overview="Summary.",
            coverage_line=None,
            required_views=(),
            optional_views=(),
            limitations=(),
            placement_note=None,
            body_explanations=(),
            inline_blocker_instances=(),
            requests_changes=False,
        )
        idx_marker = finding_index_marker(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_1,
            base_sha=BASE_1,
            result_digest=res_digest,
            entries=(),
        )
        eligibility = approval_eligibility_from_result(
            result,
            head_sha=HEAD_1,
            enabled=False,
            app_authored=False,
            qualification="qualified",
            check_published=True,
            base_sha=BASE_1,
        )
        eligibility_marker = approval_eligibility_marker(eligibility)
        review_body = f"Summary.\n\n{idx_marker}\n\n{eligibility_marker}"
        review_row = {
            "id": 901,
            "node_id": "PRR_1",
            "body": review_body,
            "commit_id": HEAD_1,
            "state": "COMMENTED",
            "submitted_at": "2026-11-24T00:00:00Z",
            "user": {
                "type": "Bot",
                "login": "reviewsensei[bot]",
                "id": 999,
            },
        }
        seen_auth: list[tuple[str, str]] = []
        expected_check = check_outcome_for_result(result, policy="blocking")

        def opener(request, timeout):
            _ = timeout
            path = urlsplit(request.full_url).path
            auth = request.get_header("Authorization") or ""
            seen_auth.append((path, auth))
            if path.endswith("/check-runs"):
                if auth != "Bearer check-read-tok":
                    return json_response(
                        {"message": "Resource not accessible by integration"},
                        403,
                    )
                return json_response(
                    {
                        "total_count": 1,
                        "check_runs": [
                            {
                                "id": 77,
                                "name": "ReviewSensei",
                                "external_id": REVIEW_CHECK_EXTERNAL_ID,
                                "status": "completed",
                                "conclusion": expected_check.conclusion,
                                "head_sha": HEAD_1,
                                "output": {
                                    "title": expected_check.title,
                                    "summary": expected_check.summary,
                                },
                                "app": {"id": 999, "slug": "reviewsensei"},
                            }
                        ],
                    }
                )
            if auth != "Bearer session-tok":
                return json_response(
                    {"message": "Resource not accessible by integration"},
                    403,
                )
            if path.endswith("/pulls/7/reviews"):
                return json_response([review_row])
            if path.endswith("/pulls/7/reviews/901"):
                return json_response(review_row)
            if path.endswith("/pulls/7/reviews/901/comments"):
                return json_response([])
            if path.endswith("/pulls/7/comments"):
                return json_response([])
            if path == "/graphql":
                payload = json.loads(request.data)
                if payload.get("operationName") == "ReviewCompletionReviewDates":
                    return json_response(
                        {
                            "data": {
                                "nodes": [
                                    {
                                        "databaseId": 901,
                                        "createdAt": "2026-11-24T00:00:00Z",
                                        "updatedAt": "2026-11-24T00:00:00Z",
                                        "lastEditedAt": None,
                                        "commit": {"oid": HEAD_1},
                                    }
                                ]
                            }
                        }
                    )
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "headRefOid": HEAD_1,
                                    "baseRefOid": BASE_1,
                                    "reviewThreads": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [],
                                    },
                                }
                            }
                        }
                    }
                )
            raise AssertionError(f"unexpected request {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        _ = _check
        context = SimpleNamespace(
            producer_id=999,
            app_slug="reviewsensei[bot]",
            publication_digest=res_digest,
        )
        store = SimpleNamespace(
            authenticated_reference=None,
            read_authenticated_snapshot=lambda ref: {},
        )
        run = SimpleNamespace(
            head_sha=HEAD_1,
            base_sha=BASE_1,
            plan=SimpleNamespace(policy=SimpleNamespace(reviews="blocking")),
        )
        analysis = SimpleNamespace(
            policy=SimpleNamespace(automatic_github_review_events=False)
        )
        pull = {"user": {"login": "human"}}
        effects = _confirm_native_effects(
            run=run,
            analysis=analysis,
            context=context,
            store=store,
            result=result,
            assembled=assembled,
            publisher=ReviewPublisher(http=http),
            http=http,
            token="session-tok",
            check_token="check-read-tok",
            identity=IDENTITY,
            pull=pull,
            current_base=BASE_1,
            current_head=HEAD_1,
        )
        self.assertEqual(effects["review_id"], 901)
        check_calls = [auth for path, auth in seen_auth if path.endswith("/check-runs")]
        pr_calls = [
            auth for path, auth in seen_auth if not path.endswith("/check-runs")
        ]
        self.assertEqual(
            check_calls, ["Bearer check-read-tok", "Bearer check-read-tok"]
        )
        self.assertTrue(pr_calls)
        self.assertTrue(all(auth == "Bearer session-tok" for auth in pr_calls))

    def test_conversation_load_base_learnings_requires_contents_read_scope(self):
        http_forbidden, _ = make_http(
            json_response({"message": "Resource not accessible by integration"}, 403)
        )
        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http_forbidden)._load_base_learnings(
                token="tok-without-contents-read",
                repository="owner/repo",
                base_sha=BASE_1,
                changed_paths=("src/app.py",),
            )

        learning = {
            "id": "python.scope-check",
            "title": "Scope check",
            "rule": "Require contents:read on private repositories.",
            "scope": ["src/**"],
            "status": "active",
        }
        encoded = base64.b64encode(
            (json.dumps(learning) + "\n").encode("utf-8")
        ).decode("ascii")
        http_ok, calls = make_http(
            [
                json_response(
                    [
                        {
                            "type": "file",
                            "path": ".github/review-sensei/learnings/rule.json",
                            "size": 120,
                        }
                    ]
                ),
                json_response({"content": encoded, "encoding": "base64"}),
            ]
        )
        entries = ConversationPublisher(http=http_ok)._load_base_learnings(
            token="tok-with-contents-read",
            repository="owner/repo",
            base_sha=BASE_1,
            changed_paths=("src/app.py",),
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "python.scope-check")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
