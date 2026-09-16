import hashlib
import json
import unittest

from review_sensei.hosting.github import (
    GitHubPublicationError,
    GitHubPublicationTransientError,
    ReviewPublisher,
)
from review_sensei.hosting.github.publication import (
    ReviewApprovalFinalizer,
    approval_marker,
    changes_requested_marker,
    finding_blocks_approval,
    finding_declares_blocking,
    finding_fingerprint_from_body,
    finding_marker,
    review_marker,
)
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.validation import ReviewLimits
from review_sensei.verifier import CandidateFinding, EvidenceReference

try:
    from fake_github_http import json_response, make_http
except ModuleNotFoundError:
    from tests.fake_github_http import json_response, make_http

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""


def result():
    return ReviewResult(
        summary="Summary.",
        comments=(
            ReviewComment(path="src/app.py", line=2, body="finding", blocking=True),
        ),
        provider="ollama",
    )


def clean_result():
    return ReviewResult(
        summary="Summary.",
        comments=(),
        provider="ollama",
        review_status="complete",
    )


def classified_result():
    return ReviewResult(
        summary="Summary.",
        comments=(
            ReviewComment(
                path="src/app.py",
                line=2,
                body="finding",
                blocking=True,
                severity="high",
                fix_effort="small",
                category="correctness",
            ),
        ),
        provider="ollama",
    )


def non_blocking_result():
    return ReviewResult(
        summary="Summary.",
        comments=(
            ReviewComment(
                path="src/app.py",
                line=2,
                body="Optional follow-up.",
                blocking=False,
            ),
        ),
        provider="ollama",
        review_status="complete",
    )


def body_limited_result():
    return ReviewResult(
        summary="Summary.",
        comments=(ReviewComment(path="src/app.py", line=2, body="x" * 100),),
        provider="ollama",
        limits=ReviewLimits(max_comment_body_bytes=128),
    )


def pr_payload(*, head_sha, fork=False, state="open", draft=False, author="alice"):
    return {
        "state": state,
        "draft": draft,
        "user": {
            "login": author,
            "type": "Bot" if author.endswith("[bot]") else "User",
        },
        "head": {
            "sha": head_sha,
            "repo": {"full_name": "owner/repo", "fork": fork},
        },
        "base": {
            "ref": "main",
            "sha": "a" * 40,
            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
        },
    }


def published_review(*, marker, head_sha, state):
    return {
        "body": marker,
        "commit_id": head_sha,
        "state": state,
        "user": {"login": "reviewsensei[bot]"},
    }


def graphql_review_threads_response(
    *, nodes=(), has_next_page=False, end_cursor=None, status=200
):
    return json_response(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": list(nodes),
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": end_cursor,
                            },
                        }
                    }
                }
            }
        },
        status,
    )


def blocking_thread_node(
    *, body="[🚫 Blocking] must fix", author="reviewsensei[bot]", resolved=False
):
    return {
        "isResolved": resolved,
        "comments": {"nodes": [{"body": body, "author": {"login": author}}]},
    }


def request_changes_write_responses(head, *, review_id=9):
    return [
        json_response(pr_payload(head_sha=head)),
        json_response([]),
        json_response({"id": review_id}, 200),
    ]


class ReviewPublisherTests(unittest.TestCase):
    def publish(self, responses, **overrides):
        head = "b" * 40
        arguments = {
            "token": "token",
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": head,
            "base_branch": "main",
            "base_sha": "a" * 40,
            "result": result(),
            "diff": DIFF,
            "app_slug": "reviewsensei[bot]",
        }
        arguments.update(overrides)
        http, calls = make_http(responses)
        return ReviewPublisher(http=http).publish(**arguments), calls

    def finalize(self, responses, **overrides):
        head = "b" * 40
        arguments = {
            "token": "token",
            "repository": "owner/repo",
            "pull_request": 2,
            "head_sha": head,
            "app_slug": "reviewsensei[bot]",
        }
        arguments.update(overrides)
        http, calls = make_http(responses)
        return ReviewApprovalFinalizer(http=http).finalize(**arguments), calls

    def test_finalizer_approves_with_only_non_blocking_and_human_threads_open(self):
        head = "b" * 40
        marker = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_sha="a" * 40,
            result=non_blocking_result(),
            blocking=False,
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "body": marker,
                                        "author": {"login": "reviewsensei[bot]"},
                                    }
                                ]
                            },
                        },
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "body": "[🚫 Blocking] human finding",
                                        "author": {"login": "alice"},
                                    }
                                ]
                            },
                        },
                    )
                ),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 9}, 200),
            ]
        )
        self.assertEqual(outcome.status, "approved")
        approval = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(approval["event"], "APPROVE")
        self.assertNotIn("comments", approval)

    def test_finalizer_fails_closed_for_an_unclassified_app_thread(self):
        head = "b" * 40
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(body="unclassified App root"),)
                ),
                *request_changes_write_responses(head),
            ]
        )
        self.assertEqual(outcome.status, "changes_requested")
        self.assertEqual(outcome.review_id, 9)
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertEqual(body["commit_id"], head)
        self.assertIn("<!-- reviewsensei:changes-requested:v1", body["body"])
        self.assertIn("unclassified App root", body["body"])
        self.assertFalse(
            any(
                method == "POST"
                and url.endswith("/pulls/2/reviews")
                and __import__("json").loads(payload.decode("utf-8")).get("event")
                == "APPROVE"
                for method, url, payload in calls
                if payload
            )
        )

    def test_change_request_body_includes_stripped_blocking_excerpts(self):
        head = "b" * 40
        root = (
            "[🚫 Blocking]\n\nRotate the leaked token.\n\n"
            "To discuss this finding, reply with @sensei followed by your question.\n\n"
            f"{finding_marker(repository_id=1, pull_request=2, head_sha=head, base_sha='a' * 40, result=result(), blocking=True)}"
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(body=root),)
                ),
                *request_changes_write_responses(head),
            ]
        )
        self.assertEqual(outcome.status, "changes_requested")
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))["body"]
        self.assertIn("Rotate the leaked token.", body)
        self.assertNotIn("reviewsensei:finding:v1", body)
        self.assertNotIn("To discuss this finding", body)

    def test_finalizer_reconciles_an_existing_exact_head_approval(self):
        head = "b" * 40
        marker = approval_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_sha="a" * 40,
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="APPROVED")]
                ),
            ]
        )
        self.assertEqual(outcome.status, "already_approved")
        self.assertEqual([call[0] for call in calls], ["GET", "POST", "GET", "GET"])

    def test_finalizer_reconciles_an_ambiguous_approval_write(self):
        head = "b" * 40
        marker = approval_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_sha="a" * 40,
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({}, 500),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="APPROVED")]
                ),
            ]
        )
        self.assertEqual(outcome.status, "already_approved")
        self.assertEqual(
            [call[0] for call in calls], ["GET", "POST", "GET", "GET", "POST", "GET"]
        )

    def test_finalizer_honors_explicit_opt_out_and_known_blocking_result(self):
        outcome, calls = self.finalize([], enabled=False)
        self.assertEqual(outcome.status, "auto_approval_disabled")
        self.assertEqual(calls, [])

        head = "b" * 40
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                *request_changes_write_responses(head, review_id=11),
            ],
            known_blocking_finding=True,
        )
        self.assertEqual(outcome.status, "changes_requested")
        self.assertEqual(outcome.review_id, 11)
        self.assertEqual([call[0] for call in calls], ["GET", "GET", "GET", "POST"])
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertNotIn("comments", body)
        self.assertIn(
            "See the inline ReviewSensei comments on this head.", body["body"]
        )

    def test_finalizer_rejects_invalid_controls_before_networking(self):
        for overrides in (
            {"enabled": "true"},
            {"known_blocking_finding": "false"},
            {"head_sha": "not-a-sha"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(GitHubPublicationError):
                    self.finalize([], **overrides)

    def test_finalizer_skips_ineligible_pull_requests(self):
        head = "b" * 40
        cases = (
            (pr_payload(head_sha=head, state="closed"), "skipped_pr_state"),
            (pr_payload(head_sha=head, draft=True), "skipped_pr_state"),
            (pr_payload(head_sha="c" * 40), "skipped_stale_head"),
            (pr_payload(head_sha=head, fork=True), "skipped_fork"),
            (
                pr_payload(head_sha=head, author="reviewsensei[bot]"),
                "skipped_app_authored",
            ),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                outcome, calls = self.finalize([json_response(payload)])
                self.assertEqual(outcome.status, expected)
                self.assertEqual([call[0] for call in calls], ["GET"])

    def test_finalizer_rejects_malformed_preflight_and_thread_responses(self):
        head = "b" * 40
        malformed_pr = pr_payload(head_sha=head)
        malformed_pr["base"] = None
        with self.assertRaises(GitHubPublicationError):
            self.finalize([json_response(malformed_pr)])
        with self.assertRaises(GitHubPublicationError):
            self.finalize(
                [
                    json_response(pr_payload(head_sha=head)),
                    json_response({"data": {"repository": {}}}),
                ]
            )

    def test_finalizer_paginates_review_threads_before_approving(self):
        head = "b" * 40
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    has_next_page=True, end_cursor="page-2"
                ),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 12}),
            ]
        )
        self.assertEqual(outcome.status, "approved")
        first_query = __import__("json").loads(calls[1][2].decode("utf-8"))
        second_query = __import__("json").loads(calls[2][2].decode("utf-8"))
        self.assertIsNone(first_query["variables"]["after"])
        self.assertEqual(second_query["variables"]["after"], "page-2")

    def test_finalizer_reconciles_or_reports_terminal_approval_errors(self):
        head = "b" * 40
        for status, error in (
            (422, GitHubPublicationError),
            (404, GitHubPublicationError),
            (403, GitHubPublicationError),
            (429, GitHubPublicationTransientError),
        ):
            with self.subTest(status=status):
                responses = [
                    json_response(pr_payload(head_sha=head)),
                    graphql_review_threads_response(),
                    json_response(pr_payload(head_sha=head)),
                    json_response([]),
                    json_response({}, status),
                ]
                if status in {422, 429}:
                    responses.append(json_response([]))
                with self.assertRaises(error):
                    self.finalize(responses)

    def test_finding_classification_is_exact_head_bound_and_legacy_compatible(self):
        head = "b" * 40
        marker = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_sha="a" * 40,
            result=non_blocking_result(),
            blocking=False,
        )
        arguments = {
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": head,
            "base_sha": "a" * 40,
        }
        self.assertFalse(finding_blocks_approval(body=marker, **arguments))
        self.assertIsNone(
            finding_blocks_approval(
                body=marker,
                head_sha="c" * 40,
                **{key: value for key, value in arguments.items() if key != "head_sha"},
            )
        )
        self.assertTrue(
            finding_blocks_approval(body="[🚫 Blocking] legacy", **arguments)
        )
        self.assertFalse(
            finding_blocks_approval(body="[💬 Non-blocking] legacy", **arguments)
        )
        self.assertIsNone(finding_blocks_approval(body=None, **arguments))
        self.assertTrue(finding_declares_blocking("[🚫 Blocking] legacy"))
        self.assertFalse(finding_declares_blocking(None))

    def test_finalizer_rejects_invalid_preflight_shapes_and_statuses(self):
        head = "b" * 40
        missing_repository = pr_payload(head_sha=head)
        missing_repository["base"]["repo"] = None
        invalid_repository_id = pr_payload(head_sha=head)
        invalid_repository_id["base"]["repo"]["id"] = True
        for response in (
            json_response({}, 404),
            json_response([], 200),
            json_response(missing_repository),
            json_response(invalid_repository_id),
        ):
            with self.subTest(status=response.status):
                with self.assertRaises(GitHubPublicationError):
                    self.finalize([response])

    def test_finalizer_fails_closed_for_invalid_review_thread_pages(self):
        head = "b" * 40
        thread_errors = json_response({"data": {}, "errors": [{"message": "no"}]})
        malformed_nodes = graphql_review_threads_response(nodes=({"isResolved": "no"},))
        malformed_roots = graphql_review_threads_response(
            nodes=({"isResolved": False, "comments": {"nodes": []}},)
        )
        missing_cursor = graphql_review_threads_response(has_next_page=True)
        malformed_page_info = json_response(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": "yes"},
                            }
                        }
                    }
                }
            }
        )
        for response, error in (
            (json_response({}, 429), GitHubPublicationTransientError),
            (json_response({}, 400), GitHubPublicationError),
            (thread_errors, GitHubPublicationError),
            (malformed_nodes, GitHubPublicationError),
            (malformed_roots, GitHubPublicationError),
            (missing_cursor, GitHubPublicationError),
            (malformed_page_info, GitHubPublicationError),
        ):
            with self.subTest(status=response.status):
                with self.assertRaises(error):
                    self.finalize([json_response(pr_payload(head_sha=head)), response])

    def test_finalizer_rejects_invalid_thread_repository_and_approval_payload(self):
        finalizer = ReviewApprovalFinalizer(http=make_http([])[0])
        with self.assertRaises(GitHubPublicationError):
            finalizer._has_open_blocking_findings(
                token="token",
                repository="owner",
                pull_request=2,
                repository_id=1,
                head_sha="b" * 40,
                base_sha="a" * 40,
                app_slug="reviewsensei[bot]",
            )
        with self.assertRaises(GitHubPublicationError):
            self.finalize(
                [
                    json_response(pr_payload(head_sha="b" * 40)),
                    graphql_review_threads_response(),
                    json_response(pr_payload(head_sha="b" * 40)),
                    json_response([]),
                    json_response({}, 200),
                ]
            )

    def test_finalizer_ignores_non_mapping_reviews_during_reconciliation(self):
        head = "b" * 40
        outcome, _ = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response(["unexpected review"]),
                json_response({"id": 22}),
            ]
        )
        self.assertEqual(outcome.status, "approved")

    def test_finalizer_rechecks_write_target_before_approval(self):
        head = "b" * 40
        closed = pr_payload(head_sha=head, state="closed")
        app_authored = pr_payload(head_sha=head, author="reviewsensei[bot]")
        changed_base = pr_payload(head_sha=head)
        changed_base["base"]["sha"] = "c" * 40
        for payload, expected in (
            (closed, "skipped_pr_state"),
            (app_authored, "skipped_app_authored"),
            (changed_base, "skipped_stale_base"),
        ):
            with self.subTest(expected=expected):
                outcome, _ = self.finalize(
                    [
                        json_response(pr_payload(head_sha=head)),
                        graphql_review_threads_response(),
                        json_response(payload),
                    ]
                )
                self.assertEqual(outcome.status, expected)

    def test_marker_declares_blocking_for_current_classification(self):
        marker = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha="b" * 40,
            base_sha="a" * 40,
            result=result(),
            blocking=True,
        )
        self.assertTrue(finding_declares_blocking(marker))

    def test_disabled_review_requests_no_write(self):
        from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions

        broker_calls = []

        class DisabledBroker:
            def exchange(self, token):
                broker_calls.append(token)
                return "ghs_capability"

        app = GitHubApplication(
            broker=DisabledBroker(),
            http=make_http([])[0],
            reviewer=ReviewPublisher(http=make_http([])[0]),
            learner=ReviewPublisher(http=make_http([])[0]),
            replier=ReviewPublisher(http=make_http([])[0]),
        )
        outcome = app.publish_review(
            options=GitHubWriteOptions(),
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha="b" * 40,
            result=result(),
            diff=DIFF,
            app_slug="review-sensei[bot]",
        )
        self.assertEqual(outcome.status, "disabled")
        self.assertEqual(broker_calls, [])

    def test_publishes_valid_review_with_marker_and_inline_comments(self):
        head = "b" * 40
        responses = [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            graphql_review_threads_response(),
            json_response({"id": 5}, 200),
        ]
        http, calls = make_http(responses)
        outcome = ReviewPublisher(http=http).publish(
            token="ghs_token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=result(),
            diff=DIFF,
            app_slug="review-sensei[bot]",
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(outcome.review_id, 5)
        post = calls[4]
        body = __import__("json").loads(post[2].decode("utf-8"))
        self.assertIn("<!-- reviewsensei:review:v1", body["body"])
        self.assertIn("coverage=full", body["body"])
        self.assertIn("<!-- reviewsensei:finding:v2", body["comments"][0]["body"])
        self.assertIn(
            "To discuss this finding, reply with @sensei followed by your question.",
            body["body"],
        )
        self.assertEqual(body["commit_id"], head)
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertEqual(body["comments"][0]["path"], "src/app.py")
        self.assertIn(
            "To discuss this finding, reply with @sensei followed by your question.",
            body["comments"][0]["body"],
        )

    def test_existing_fingerprint_is_not_republished_across_heads(self):
        from review_sensei.context import finding_lifecycle_for_comment

        head = "b" * 40
        current = result()
        fingerprint = finding_lifecycle_for_comment(current.comments[0]).fingerprint
        existing = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha="c" * 40,
            base_sha="a" * 40,
            result=current,
            blocking=True,
            fingerprint=fingerprint,
            state="still-present",
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "body": existing,
                                        "author": {"login": "reviewsensei[bot]"},
                                    }
                                ]
                            },
                        },
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "body": "human discussion",
                                        "author": {"login": "alice"},
                                    }
                                ]
                            },
                        },
                    )
                ),
                json_response({"id": 5}, 200),
            ]
        )
        self.assertEqual(outcome.status, "published")
        # Duplicate suppression is a property of publication, not of incremental
        # mode: a plain full review is the common re-review case.
        self.assertEqual(current.coverage_mode, "full")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertEqual(body["comments"], [])
        self.assertEqual(finding_fingerprint_from_body(existing), fingerprint)
        self.assertTrue(finding_declares_blocking(existing))

    def test_full_review_fingerprint_sweep_fails_closed_before_write(self):
        """An uncertain sweep must not publish a possible duplicate."""

        head = "b" * 40
        for sweep, expected in (
            (json_response({}, 500), GitHubPublicationTransientError),
            (
                graphql_review_threads_response(nodes=({"isResolved": "no"},)),
                GitHubPublicationError,
            ),
        ):
            with self.subTest(sweep=expected.__name__):
                http, calls = make_http(
                    [
                        json_response(pr_payload(head_sha=head)),
                        json_response([]),
                        json_response(pr_payload(head_sha=head)),
                        sweep,
                        json_response({"id": 5}, 200),
                    ]
                )
                with self.assertRaises(expected):
                    ReviewPublisher(http=http).publish(
                        token="token",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=2,
                        head_sha=head,
                        base_branch="main",
                        base_sha="a" * 40,
                        result=result(),
                        diff=DIFF,
                        app_slug="reviewsensei[bot]",
                    )
                self.assertFalse(
                    any(
                        method == "POST" and url.endswith("/pulls/2/reviews")
                        for method, url, _ in calls
                    )
                )

    def test_publishes_classification_metadata_without_changing_identity_fields(self):
        head = "b" * 40
        responses = [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            graphql_review_threads_response(),
            json_response({"id": 5}, 200),
        ]
        http, calls = make_http(responses)
        outcome = ReviewPublisher(http=http).publish(
            token="ghs_token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=classified_result(),
            diff=DIFF,
            app_slug="review-sensei[bot]",
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertIn("Review classification:", body["body"])
        self.assertIn(
            "[🚫 Blocking] [🟠 Severity: High] [⚡ Fix effort: Small] [✅ Lens: Correctness]",
            body["comments"][0]["body"],
        )
        self.assertEqual(body["commit_id"], head)
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertIn("<!-- reviewsensei:review:v1", body["body"])

    def test_non_blocking_finding_can_publish_an_approval(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}, 200),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=non_blocking_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            auto_approve=True,
        )

        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[9][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")
        self.assertNotIn("comments", body)

    def test_rejects_formatted_comment_that_exceeds_output_limit(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
            ]
        )
        with self.assertRaises(GitHubPublicationError):
            ReviewPublisher(http=http).publish(
                token="ghs_token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=body_limited_result(),
                diff=DIFF,
                app_slug="review-sensei[bot]",
            )
        self.assertEqual(len(calls), 3)

    def test_rejects_complete_summary_body_above_github_transport_limit(self):
        head = "b" * 40
        comments = tuple(
            ReviewComment(
                path="src/app.py",
                line=2,
                body=str(index),
                category=f"lens-{index:03d}-" + ("x" * 240),
            )
            for index in range(250)
        )
        oversized = ReviewResult(
            summary="Summary.",
            comments=comments,
            provider="ollama",
        )
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
            ]
        )
        with self.assertRaises(GitHubPublicationError):
            ReviewPublisher(http=http).publish(
                token="ghs_token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=oversized,
                diff=DIFF,
                app_slug="review-sensei[bot]",
            )
        self.assertEqual(len(calls), 3)

    def test_clean_review_uses_approve_event_by_default(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}, 200),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[8][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")
        self.assertEqual(body["commit_id"], head)
        self.assertNotIn("comments", body)

    def test_non_boolean_auto_approve_is_rejected_before_network_calls(self):
        http, calls = make_http([])
        with self.assertRaisesRegex(
            GitHubPublicationError, "auto_approve must be a boolean"
        ):
            ReviewPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha="b" * 40,
                base_branch="main",
                base_sha="a" * 40,
                result=clean_result(),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                auto_approve="false",  # type: ignore[arg-type]
            )
        self.assertEqual(calls, [])

    def test_open_blocking_review_thread_prevents_final_approval(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                *request_changes_write_responses(head, review_id=7),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "published")
        query = __import__("json").loads(calls[5][2].decode("utf-8"))
        self.assertEqual(calls[5][1], "https://api.github.test/graphql")
        self.assertIn("reviewThreads", query["query"])
        self.assertIn("isResolved", query["query"])
        self.assertIn("body", query["query"])
        finding_review = __import__("json").loads(calls[3][2].decode("utf-8"))
        self.assertEqual(finding_review["event"], "COMMENT")
        change_request = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(change_request["event"], "REQUEST_CHANGES")
        self.assertEqual(change_request["commit_id"], head)
        self.assertNotIn("comments", change_request)
        self.assertIn("[🚫 Blocking] must fix", change_request["body"])

    def test_resolved_review_threads_do_not_block_clean_review(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=({"isResolved": True},)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}, 200),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[8][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")

    def test_review_thread_lookup_failure_fails_closed_before_write(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                json_response({"errors": [{"message": "not exposed"}]}),
            ]
        )
        with self.assertRaises(GitHubPublicationError):
            ReviewPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=clean_result(),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                auto_approve=True,
            )
        self.assertEqual(len(calls), 6)

    def test_malformed_review_thread_errors_fail_closed_before_write(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                json_response({"errors": "malformed"}),
            ]
        )
        with self.assertRaises(GitHubPublicationError):
            ReviewPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=clean_result(),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                auto_approve=True,
            )
        self.assertEqual(len(calls), 6)

    def test_review_thread_lookup_paginates_with_a_bounded_cursor(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=({"isResolved": True},),
                    has_next_page=True,
                    end_cursor="cursor-1",
                ),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                *request_changes_write_responses(head, review_id=8),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "published")
        second_query = __import__("json").loads(calls[6][2].decode("utf-8"))
        self.assertEqual(second_query["variables"]["after"], "cursor-1")
        finding_review = __import__("json").loads(calls[3][2].decode("utf-8"))
        self.assertEqual(finding_review["event"], "COMMENT")
        change_request = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(change_request["event"], "REQUEST_CHANGES")

    def test_app_authored_clean_review_falls_back_to_comment_event(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head, author="reviewsensei[bot]")),
                json_response([]),
                json_response(pr_payload(head_sha=head, author="reviewsensei[bot]")),
                json_response({"id": 5}, 200),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[3][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")

    def test_app_authored_reconciled_comment_never_finalizes(self):
        """A retry must preserve the App-authored PR approval exclusion."""

        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=clean_result(),
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head, author="reviewsensei[bot]")),
                json_response(
                    [
                        published_review(
                            marker=marker,
                            head_sha=head,
                            state="COMMENTED",
                        )
                    ]
                ),
            ],
            result=clean_result(),
            auto_approve=True,
        )

        self.assertEqual(outcome.status, "already_published")
        self.assertEqual([call[0] for call in calls], ["GET", "GET"])

    def test_disabled_auto_approve_keeps_blocking_findings_as_comments(self):
        head = "b" * 40
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
            ],
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertNotEqual(body["event"], "REQUEST_CHANGES")
        self.assertEqual(len(calls), 5)
        self.assertEqual(
            sum(
                1
                for method, url, _ in calls
                if method == "POST" and url.endswith("/pulls/2/reviews")
            ),
            1,
        )

    def test_blocking_result_overrides_same_head_approval(self):
        head = "b" * 40
        prior = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=clean_result(),
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=prior, head_sha=head, state="APPROVED")]
                ),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
            ]
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertEqual(body["comments"][0]["path"], "src/app.py")

    def test_change_request_does_not_yield_to_a_racy_later_approval(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        existing = published_review(
            marker=marker, head_sha=head, state="CHANGES_REQUESTED"
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=head)),
            ],
            result=clean_result(),
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertFalse(
            any(
                method == "POST"
                and url.endswith("/pulls/2/reviews")
                and payload
                and __import__("json").loads(payload.decode("utf-8")).get("event")
                == "APPROVE"
                for method, url, payload in calls
                if payload
            )
        )

    def test_deleted_blocking_roots_do_not_deadlock_an_existing_change_request(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        existing = published_review(
            marker=marker, head_sha=head, state="CHANGES_REQUESTED"
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 6}, 200),
            ],
            result=clean_result(),
            auto_approve=True,
        )
        # Publisher returns already_published after reconciling the existing
        # identity review; the nested finalizer write is APPROVE.
        self.assertEqual(outcome.status, "already_published")
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")

    def test_change_request_promotes_to_approve_after_blocking_roots_resolve(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        existing = published_review(
            marker=marker, head_sha=head, state="CHANGES_REQUESTED"
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(resolved=True),)
                ),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(resolved=True),)
                ),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(resolved=True),)
                ),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 6}, 200),
            ],
            result=clean_result(),
            auto_approve=True,
        )
        # Publisher returns already_published after reconciling the existing
        # identity review; the nested finalizer write is APPROVE.
        self.assertEqual(outcome.status, "already_published")
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")
        self.assertEqual(body["commit_id"], head)
        self.assertNotIn("comments", body)

    def test_finalizer_promotes_resolved_change_request_as_approved(self):
        head = "b" * 40
        existing = published_review(
            marker=review_marker(
                repository_id=1,
                pull_request=2,
                head_sha=head,
                result=result(),
            ),
            head_sha=head,
            state="CHANGES_REQUESTED",
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(resolved=True),)
                ),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(resolved=True),)
                ),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(resolved=True),)
                ),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 6}, 200),
            ]
        )
        self.assertEqual(outcome.status, "approved")
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")

    def test_change_request_posted_after_second_sweep_is_not_dismissed(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        existing = published_review(
            marker=marker, head_sha=head, state="CHANGES_REQUESTED"
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=head)),
            ],
            result=clean_result(),
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertFalse(
            any(
                method == "POST"
                and url.endswith("/pulls/2/reviews")
                and payload
                and __import__("json").loads(payload.decode("utf-8")).get("event")
                == "APPROVE"
                for method, url, payload in calls
                if payload
            )
        )

    def test_unmarked_app_change_request_does_not_skip_owned_marker(self):
        head = "b" * 40
        foreign = published_review(
            marker="not a reviewsensei change-request marker",
            head_sha=head,
            state="CHANGES_REQUESTED",
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(pr_payload(head_sha=head)),
                json_response([foreign]),
                json_response({"id": 11}, 200),
            ],
            known_blocking_finding=True,
        )
        self.assertEqual(outcome.status, "changes_requested")
        body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertIn("<!-- reviewsensei:changes-requested:v1", body["body"])

    def test_owned_change_request_marker_is_idempotent(self):
        head = "b" * 40
        marker = changes_requested_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_sha="a" * 40,
        )
        existing = published_review(
            marker=marker, head_sha=head, state="CHANGES_REQUESTED"
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
            ],
            known_blocking_finding=True,
        )
        self.assertEqual(outcome.status, "already_changes_requested")
        self.assertFalse(
            any(
                method == "POST" and url.endswith("/pulls/2/reviews")
                for method, url, _ in calls
            )
        )

    def test_head_is_rechecked_after_marker_pagination_before_write(self):
        head = "b" * 40
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha="c" * 40)),
            ],
            result=clean_result(),
        )

        self.assertEqual(outcome.status, "skipped_stale_head")
        self.assertEqual([call[0] for call in calls], ["GET", "GET", "GET"])

    def test_stale_head_skips_write(self):
        head = "b" * 40
        http, calls = make_http(json_response(pr_payload(head_sha="c" * 40)))
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="review-sensei[bot]",
        )
        self.assertEqual(outcome.status, "skipped_stale_head")
        self.assertEqual(len(calls), 1)

    def test_base_repository_name_mismatch_skips_write(self):
        head = "b" * 40
        payload = pr_payload(head_sha=head)
        payload["base"]["repo"]["full_name"] = "other/repo"
        http, calls = make_http(json_response(payload))
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="review-sensei[bot]",
        )
        self.assertEqual(outcome.status, "skipped_repository_mismatch")
        self.assertEqual(len(calls), 1)

    def test_out_of_diff_comment_fails_closed_before_write(self):
        bad = ReviewResult(
            summary="done",
            comments=(ReviewComment(path="src/app.py", line=1, body="unchanged"),),
            provider="ollama",
        )
        http, calls = make_http([])
        with self.assertRaises(GitHubPublicationError):
            ReviewPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha="b" * 40,
                base_branch="main",
                base_sha="a" * 40,
                result=bad,
                diff=DIFF,
                app_slug="review-sensei[bot]",
            )
        self.assertEqual(calls, [])

    def test_existing_approval_is_reconciled_before_post(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=clean_result(),
        )
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [
                        published_review(
                            marker=f"Summary.\n\n{marker}",
                            head_sha=head,
                            state="APPROVED",
                        )
                    ]
                ),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual([call[0] for call in calls], ["GET", "GET"])

    def test_clean_rerun_promotes_same_head_comment_after_threads_resolve(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="COMMENTED")]
                ),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=({"isResolved": True},)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}, 200),
            ],
            result=clean_result(),
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "already_published")
        body = __import__("json").loads(calls[6][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")
        self.assertEqual(body["commit_id"], head)
        self.assertNotIn("comments", body)

    def test_non_blocking_rerun_promotes_same_head_comment_after_threads_resolve(self):
        head = "b" * 40
        non_blocking = non_blocking_result()
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=non_blocking,
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="COMMENTED")]
                ),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=({"isResolved": True},)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}, 200),
            ],
            result=non_blocking,
            auto_approve=True,
        )

        self.assertEqual(outcome.status, "already_published")
        body = __import__("json").loads(calls[6][2].decode("utf-8"))
        self.assertEqual(body["event"], "APPROVE")
        self.assertNotIn("comments", body)

    def test_clean_rerun_does_not_duplicate_comment_while_threads_are_open(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="COMMENTED")]
                ),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                *request_changes_write_responses(head, review_id=8),
            ],
            result=clean_result(),
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "already_published")
        change_request = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(change_request["event"], "REQUEST_CHANGES")
        self.assertEqual(change_request["commit_id"], head)
        self.assertEqual(
            sum(
                1
                for method, url, payload in calls
                if method == "POST"
                and url.endswith("/pulls/2/reviews")
                and payload
                and __import__("json").loads(payload.decode("utf-8")).get("event")
                == "REQUEST_CHANGES"
            ),
            1,
        )

    def test_finding_rerun_does_not_duplicate_same_head_comment(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="COMMENTED")]
                ),
                json_response(pr_payload(head_sha=head)),
                *request_changes_write_responses(head, review_id=8),
            ]
        )
        self.assertEqual(outcome.status, "already_published")
        change_request = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(change_request["event"], "REQUEST_CHANGES")
        self.assertNotIn("comments", change_request)

    def test_promotion_failure_does_not_reconcile_to_prior_comment(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        prior_comment = published_review(
            marker=marker, head_sha=head, state="COMMENTED"
        )
        with self.assertRaises(GitHubPublicationTransientError):
            self.publish(
                [
                    json_response(pr_payload(head_sha=head)),
                    json_response([prior_comment]),
                    json_response(pr_payload(head_sha=head)),
                    graphql_review_threads_response(),
                    json_response(pr_payload(head_sha=head)),
                    json_response([]),
                    json_response({}, 500),
                    json_response([prior_comment]),
                ],
                result=clean_result(),
                auto_approve=True,
            )

    def test_matching_review_with_unknown_state_fails_closed(self):
        head = "b" * 40
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=result(),
        )
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="DISMISSED")]
                ),
            ]
        )
        with self.assertRaises(GitHubPublicationError):
            ReviewPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=clean_result(),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
            )
        self.assertEqual([call[0] for call in calls], ["GET", "GET"])

    def test_same_head_deduplicates_even_when_model_result_changes(self):
        head = "b" * 40
        different = ReviewResult(
            summary="A different nondeterministic summary.",
            comments=(),
            provider="ollama",
        )
        marker = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=different,
        )
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=marker, head_sha=head, state="APPROVED")]
                ),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual([call[0] for call in calls], ["GET", "GET"])

    def test_marker_reconciliation_failure_fails_closed_before_post(self):
        head = "b" * 40
        http, calls = make_http(
            [json_response(pr_payload(head_sha=head)), json_response({}, 500)]
        )
        with self.assertRaises(GitHubPublicationError):
            ReviewPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=clean_result(),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
            )
        self.assertEqual(len(calls), 2)

    def test_authoritative_base_binding_skips_changed_base(self):
        head = "b" * 40
        payload = pr_payload(head_sha=head)
        payload["base"]["ref"] = "develop"
        http, calls = make_http(json_response(payload))
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=clean_result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "skipped_stale_base")
        self.assertEqual(len(calls), 1)

    def test_invalid_inputs_and_diff_fail_before_github(self):
        cases = (
            {"repository_id": True},
            {"pull_request": 0},
            {"head_sha": "not-a-sha"},
            {"base_branch": ""},
            {"base_sha": "not-a-sha"},
            {"result": None},
            {"diff": "not a diff"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(GitHubPublicationError):
                    self.publish([], **overrides)

    def test_invalid_preflight_responses_fail_closed(self):
        head = "b" * 40
        invalid_payloads = []
        for mutate in (
            lambda value: value.pop("head"),
            lambda value: value["head"].pop("repo"),
            lambda value: value["head"]["repo"].__setitem__("fork", "false"),
            lambda value: value.pop("base"),
            lambda value: value["base"].pop("repo"),
            lambda value: value["base"]["repo"].__setitem__("id", True),
            lambda value: value["base"]["repo"].__setitem__("fork", "false"),
            lambda value: value["base"].__setitem__("sha", "invalid"),
            lambda value: value.pop("user"),
            lambda value: value["user"].__setitem__("login", ""),
        ):
            payload = pr_payload(head_sha=head)
            mutate(payload)
            invalid_payloads.append(json_response(payload))
        responses = [json_response({}, 404), json_response({}, 500), json_response([])]
        responses.extend(invalid_payloads)
        for response in responses:
            with self.subTest(status=response.status):
                with self.assertRaises(GitHubPublicationError):
                    self.publish(response)

    def test_preflight_skips_ineligible_pull_requests(self):
        head = "b" * 40
        cases = []
        for expected, mutate in (
            ("skipped_pr_state", lambda value: value.__setitem__("state", "closed")),
            ("skipped_pr_state", lambda value: value.__setitem__("draft", True)),
            (
                "skipped_fork",
                lambda value: value["head"]["repo"].__setitem__("fork", True),
            ),
            (
                "skipped_fork",
                lambda value: value["head"]["repo"].__setitem__(
                    "full_name", "fork/repo"
                ),
            ),
            (
                "skipped_repository_mismatch",
                lambda value: value["base"]["repo"].__setitem__("id", 2),
            ),
            (
                "skipped_fork",
                lambda value: value["base"]["repo"].__setitem__("fork", True),
            ),
        ):
            payload = pr_payload(head_sha=head)
            mutate(payload)
            cases.append((expected, payload))
        for expected, payload in cases:
            with self.subTest(expected=expected):
                outcome, calls = self.publish(
                    json_response(payload),
                    result=clean_result(),
                )
                self.assertEqual(outcome.status, expected)
                self.assertEqual(len(calls), 1)

    def test_post_failure_statuses_are_mapped_and_reconciled(self):
        head = "b" * 40
        terminal = ((404, GitHubPublicationError), (403, GitHubPublicationError))
        ambiguous = (
            (422, GitHubPublicationError),
            (409, GitHubPublicationTransientError),
            (429, GitHubPublicationTransientError),
            (500, GitHubPublicationTransientError),
        )
        for status, error in terminal:
            with self.subTest(status=status):
                with self.assertRaises(error):
                    self.publish(
                        [
                            json_response(pr_payload(head_sha=head)),
                            json_response([]),
                            json_response(pr_payload(head_sha=head)),
                            graphql_review_threads_response(),
                            json_response({}, status),
                        ]
                    )
        for status, error in ambiguous:
            with self.subTest(status=status):
                with self.assertRaises(error):
                    self.publish(
                        [
                            json_response(pr_payload(head_sha=head)),
                            json_response([]),
                            json_response(pr_payload(head_sha=head)),
                            graphql_review_threads_response(),
                            json_response({}, status),
                            json_response([]),
                        ]
                    )
        for response in (json_response({}, 200), json_response({"id": "bad"}, 201)):
            with self.assertRaises(GitHubPublicationError):
                self.publish(
                    [
                        json_response(pr_payload(head_sha=head)),
                        json_response([]),
                        json_response(pr_payload(head_sha=head)),
                        graphql_review_threads_response(),
                        response,
                    ]
                )

    def _confirmed_snapshot(self):
        snapshot = {"src/app.py": "keep\nchange\n"}
        digest = hashlib.sha256(
            json.dumps(
                dict(sorted(snapshot.items())),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        return snapshot, digest

    def test_confirmed_policy_does_not_publish_unverified_candidates(self):
        snapshot, digest = self._confirmed_snapshot()
        confirmed = CandidateFinding(
            "Added line is unbounded.",
            "Call the changed helper with empty input.",
            "src/app.py",
            (EvidenceReference("src/app.py", 2, digest, "change"),),
            "The new line can fail closed callers.",
        )
        rejected = CandidateFinding(
            "This claims a secret that is not in the snapshot.",
            "Read an API key.",
            "src/app.py",
            (EvidenceReference("src/app.py", 2, digest, "api_key = secret"),),
            "Unsupported evidence cannot become a finding.",
        )
        unverified = ReviewResult(
            summary="Review complete.",
            comments=(
                ReviewComment(path="src/app.py", line=2, body="unverified finding"),
            ),
            provider="ollama",
            review_status="complete",
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha="b" * 40)),
                json_response([]),
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response({"id": 9}, 200),
            ],
            result=unverified,
            candidates=(confirmed, rejected),
            snapshot=snapshot,
            snapshot_sha256=digest,
            evidence_policy="confirmed",
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(len(body["comments"]), 1)
        self.assertIn("Added line is unbounded.", body["comments"][0]["body"])
        self.assertNotIn("unverified finding", body["comments"][0]["body"])
        self.assertNotIn("api_key = secret", body["comments"][0]["body"])
        self.assertIn("Verification coverage:", body["body"])
        self.assertIn("Unpublished candidates are not findings", body["body"])

    def test_confirmed_policy_without_snapshot_fails_closed_before_write(self):
        with self.assertRaises(GitHubPublicationError):
            self.publish(
                [
                    json_response(pr_payload(head_sha="b" * 40)),
                    json_response([]),
                    json_response(pr_payload(head_sha="b" * 40)),
                    json_response({"id": 9}, 200),
                ],
                evidence_policy="confirmed",
            )


if __name__ == "__main__":
    unittest.main()
