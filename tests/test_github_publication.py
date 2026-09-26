import hashlib
import importlib
import inspect
import json
import pkgutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import review_sensei
from review_sensei.convergence import (
    REVIEW_MODE_ENV,
    ReviewConvergencePolicy,
    derive_blocker_candidate,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.hosting.github import (
    GitHubPublicationError,
    GitHubPublicationTransientError,
    ReviewPublisher,
)
from review_sensei.hosting.github.approval import (
    approval_eligibility_from_result,
)
from review_sensei.hosting.github.publication import (
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    approval_eligibility_marker,
    approval_marker,
    finding_blocks_approval,
    finding_declares_blocking,
    finding_fingerprint_from_body,
    finding_marker,
    format_unanchored_findings,
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


class FakeCheckRuns:
    """Answer the check-run API the way GitHub does, one run per repository.

    The fake keeps the run it created so the concluding write of one
    publication updates that same run instead of creating a sibling, and it
    records every write so a test asserts the published gate instead of an
    internal flag.
    """

    def __init__(self, *, app_slug="reviewsensei[bot]", run_id=100, denied=False):
        self.app_slug = app_slug
        self.run_id = run_id
        self.denied = denied
        self.run: dict[str, object] | None = None
        self.writes: list[dict[str, object]] = []
        self.head_shas: list[str] = []

    def matches(self, request) -> bool:
        return "/check-runs" in request.full_url

    def respond(self, request):
        if self.denied:
            return json_response({"message": "Resource not accessible"}, 403)
        if request.method == "GET":
            return json_response({"check_runs": [] if self.run is None else [self.run]})
        body = json.loads(request.data.decode("utf-8"))
        if request.method == "POST":
            self.head_shas.append(body["head_sha"])
            self.run = {
                "id": self.run_id,
                "name": body["name"],
                "app": {"slug": self.app_slug},
            }
            code = 201
        else:
            code = 200
        self.writes.append(body)
        return json_response({"id": self.run_id}, code)

    @property
    def statuses(self) -> list[object]:
        return [write["status"] for write in self.writes]

    @property
    def conclusions(self) -> list[object]:
        return [write.get("conclusion") for write in self.writes]


def eligibility_document(
    *,
    head_sha,
    result=None,
    enabled=True,
    app_authored=False,
    qualification="not-required",
    check_published=True,
):
    """Build the persisted eligibility document one publication would carry."""

    return approval_eligibility_from_result(
        clean_result() if result is None else result,
        head_sha=head_sha,
        enabled=enabled,
        app_authored=app_authored,
        qualification=qualification,
        check_published=check_published,
    )


def review_payloads(calls):
    """Return the parsed review writes a capture emitted, in order."""

    return [
        json.loads(payload.decode("utf-8"))
        for method, url, payload in calls
        if method == "POST" and url.endswith("/pulls/2/reviews") and payload
    ]


def posted_events(calls):
    """Return just the review events a capture emitted, in order."""

    return [payload["event"] for payload in review_payloads(calls)]


def graphql_queries(calls):
    """Return the parsed GraphQL request bodies a capture emitted, in order."""

    return [
        json.loads(payload.decode("utf-8"))
        for method, url, payload in calls
        if method == "POST" and url.endswith("/graphql") and payload
    ]


def non_check_calls(calls):
    """Return the calls a capture made outside the routed check-run gate."""

    return [call for call in calls if "/check-runs" not in call[1]]


class ReviewPublisherTests(unittest.TestCase):
    def publish(self, responses, *, checks=None, **overrides):
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
            "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
            "allow_retired_legacy_policy": True,
        }
        if checks is not None:
            arguments["check_token"] = "check-token"
        arguments.update(overrides)
        http, calls = make_http(
            responses,
            routes=() if checks is None else ((checks.matches, checks.respond),),
        )
        return ReviewPublisher(http=http).publish(**arguments), calls

    def finalize(self, responses, **overrides):
        head = "b" * 40
        arguments = {
            "token": "token",
            "repository": "owner/repo",
            "pull_request": 2,
            "head_sha": head,
            "app_slug": "reviewsensei[bot]",
            "eligibility": eligibility_document(head_sha=head),
        }
        arguments.update(overrides)
        http, calls = make_http(responses)
        return ReviewApprovalFinalizer(http=http).finalize(**arguments), calls

    def test_prepared_review_shortcut_rechecks_publication_context_and_runtime_state(
        self,
    ):
        http, calls = make_http([])
        publisher = ReviewPublisher(http=http)
        head = "b" * 40
        prepared = publisher.prepare(
            result=result(),
            diff=DIFF,
            head_sha=head,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
        )

        with self.assertRaisesRegex(GitHubPublicationError, "publication context"):
            publisher.publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=result(),
                diff=DIFF.replace("+change", "+different-change"),
                app_slug="reviewsensei[bot]",
                prepared_review=prepared,
                convergence_policy=ReviewConvergencePolicy(mode="legacy"),
                allow_retired_legacy_policy=True,
            )
        self.assertEqual(calls, [])

        valid_http, valid_calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
            ]
        )
        published = ReviewPublisher(http=valid_http).publish(
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
            prepared_review=prepared,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(published.status, "published")
        self.assertEqual(valid_calls[4][0], "POST")
        self.assertTrue(valid_calls[4][1].endswith("/repos/owner/repo/pulls/2/reviews"))

        original_comment = result().comments[0]
        runtime_tampered = replace(
            result(),
            comments=(replace(original_comment, effective_blocking=False),),
        )
        self.assertEqual(runtime_tampered.content_digest(), result().content_digest())
        with self.assertRaisesRegex(GitHubPublicationError, "publication context"):
            publisher.publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                result=runtime_tampered,
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                prepared_review=prepared,
                convergence_policy=ReviewConvergencePolicy(mode="legacy"),
                allow_retired_legacy_policy=True,
            )
        self.assertEqual(calls, [])

    def test_retired_legacy_policy_requires_an_explicit_opt_in(self):
        http, calls = make_http([])
        publisher = ReviewPublisher(http=http)

        with self.assertRaisesRegex(
            GitHubPublicationError, "allow_retired_legacy_policy=True"
        ):
            publisher.publish(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha="b" * 40,
                base_branch="main",
                base_sha="a" * 40,
                result=result(),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            )
        self.assertEqual(calls, [])

    def test_retired_legacy_policy_opt_in_must_be_a_boolean(self):
        with self.assertRaisesRegex(
            GitHubPublicationError, "legacy policy opt-in must be a boolean"
        ):
            self.publish([], allow_retired_legacy_policy="yes")

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

    def test_finalizer_withholds_for_an_unclassified_app_thread_without_writing(self):
        head = "b" * 40
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(body="unclassified App root"),)
                ),
                json_response(pr_payload(head_sha=head)),
            ]
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(review_payloads(calls), [])

    def test_finalizer_never_emits_a_second_review_event_for_required_fixes(self):
        """Required fixes stay non-passing through the gate, not a change request."""

        head = "b" * 40
        root = (
            "[🚫 Blocking]\n\nRotate the leaked token.\n\n"
            f"{finding_marker(repository_id=1, pull_request=2, head_sha=head, base_sha='a' * 40, result=result(), blocking=True)}"
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(blocking_thread_node(body=root),)
                ),
                json_response(pr_payload(head_sha=head)),
            ]
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual([call[0] for call in calls], ["GET", "POST", "GET"])

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

    def test_finalizer_honors_a_disabled_document_and_a_blocking_document(self):
        head = "b" * 40
        outcome, calls = self.finalize(
            [], eligibility=eligibility_document(head_sha=head, enabled=False)
        )
        self.assertEqual(outcome.status, "auto_approval_disabled")
        self.assertEqual(calls, [])

        outcome, calls = self.finalize(
            [],
            head_sha=head,
            eligibility=eligibility_document(head_sha=head, result=result()),
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(calls, [])

    def test_finalizer_rejects_invalid_controls_before_networking(self):
        head = "b" * 40
        for overrides in (
            {"eligibility": "document"},
            {"head_sha": "not-a-sha"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(GitHubPublicationError):
                    self.finalize([], **overrides)
        # A document whose facts do not parse is not an approval input: the
        # invalid control withholds before any GitHub read.
        document = eligibility_document(head_sha=head)
        invalid = replace(
            document, facts=replace(document.facts, qualification="maybe")
        )
        outcome, calls = self.finalize([], eligibility=invalid)
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(calls, [])

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
            finalizer._scan_blocking_threads(
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
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
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
        # Required fixes are carried by the check conclusion, never by a second
        # review event, so the published review itself stays a COMMENT.
        self.assertEqual(body["event"], "COMMENT")
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
                json_response(pr_payload(head_sha=head)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 9}, 200),
            ]
        )
        self.assertEqual(outcome.status, "published")
        # Duplicate suppression is a property of publication, not of incremental
        # mode: a plain full review is the common re-review case.
        self.assertEqual(current.coverage_mode, "full")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertEqual(body["comments"], [])
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(finding_fingerprint_from_body(existing), fingerprint)
        self.assertTrue(finding_declares_blocking(existing))

    def test_all_fingerprints_already_published_on_same_head_stays_comment(self):
        """A same-head re-review must not emit a second blocking review event."""

        from review_sensei.context import finding_lifecycle_for_comment

        head = "b" * 40
        current = result()
        fingerprint = finding_lifecycle_for_comment(current.comments[0]).fingerprint
        existing = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
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
                    )
                ),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 9}, 200),
            ],
            checks=FakeCheckRuns(),
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "published")
        body = review_payloads(calls)[0]
        self.assertEqual(body["comments"], [])
        self.assertEqual(body["event"], "COMMENT")
        self.assertIn("Summary.", body["body"])
        # The persisted blocking classification withholds approval before any
        # thread read, so the run emits exactly the one suppression review.
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")

    def test_legacy_v1_marker_suppresses_by_inline_location(self):
        """Legacy v1 roots without fingerprints still participate in dedupe."""

        head = "b" * 40
        current = result()
        legacy = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_sha="a" * 40,
            result=current,
            blocking=True,
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
                                        "body": legacy,
                                        "path": "src/app.py",
                                        "line": 2,
                                        "author": {"login": "reviewsensei[bot]"},
                                    }
                                ]
                            },
                        },
                    )
                ),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 9}, 200),
            ]
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertEqual(body["comments"], [])
        self.assertEqual(body["event"], "COMMENT")

    def test_changed_blocking_classification_is_not_suppressed(self):
        """A reclassified finding must publish even when the fingerprint matches."""

        from review_sensei.context import finding_lifecycle_for_comment

        head = "b" * 40
        current = result()
        fingerprint = finding_lifecycle_for_comment(current.comments[0]).fingerprint
        existing = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_sha="a" * 40,
            result=current,
            blocking=False,
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
                                        "path": "src/app.py",
                                        "line": 2,
                                        "author": {"login": "reviewsensei[bot]"},
                                    }
                                ]
                            },
                        },
                    )
                ),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 9}, 200),
            ]
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertEqual(len(body["comments"]), 1)
        self.assertEqual(body["event"], "COMMENT")

    def test_fingerprint_sweep_matches_app_slug_case_insensitively(self):
        """GitHub logins are case-insensitive, so the slug must match anyway."""

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
                                        "author": {"login": "ReviewSensei[BOT]"},
                                    }
                                ]
                            },
                        },
                    )
                ),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 9}, 200),
            ]
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertEqual(body["comments"], [])

    def test_fingerprint_sweep_fails_closed_on_an_unreadable_root_author(self):
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    nodes=(
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [{"body": "root", "author": "reviewsensei"}]
                            },
                        },
                    )
                ),
                json_response({"id": 5}, 200),
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
                result=result(),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode="legacy"),
                allow_retired_legacy_policy=True,
            )
        self.assertFalse(
            any(
                method == "POST" and url.endswith("/pulls/2/reviews")
                for method, url, _ in calls
            )
        )

    def test_fingerprint_sweep_degrades_when_graphql_is_not_permitted(self):
        """Publication continues without suppression when GraphQL is blocked."""

        head = "b" * 40
        for status in (401, 403):
            with self.subTest(status=status):
                outcome, calls = self.publish(
                    [
                        json_response(pr_payload(head_sha=head)),
                        json_response([]),
                        json_response(pr_payload(head_sha=head)),
                        json_response({}, status),
                        json_response({"id": 5}, 200),
                        json_response(pr_payload(head_sha=head)),
                        json_response(pr_payload(head_sha=head)),
                        json_response([]),
                        json_response({"id": 9}, 200),
                    ]
                )
                self.assertEqual(outcome.status, "published")
                body = __import__("json").loads(calls[4][2].decode("utf-8"))
                self.assertEqual(len(body["comments"]), 1)

    def test_skipped_incremental_pass_cannot_approve_with_blocking_roots(self):
        """A skip carries no findings, so only the finalizer decides approval.

        An unresolved App blocking root on the exact head withholds the
        approval, and the withholding is reported through the diagnostic rather
        than through a second review event.
        """

        head = "b" * 40
        skipped = ReviewResult(
            summary="Incremental review: no changed paths since the last accepted review.",
            comments=(),
            provider="ollama",
            review_status="complete",
            coverage_mode="incremental",
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=head)),
            ],
            result=skipped,
            auto_approve=True,
            checks=FakeCheckRuns(),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(posted_events(calls), ["COMMENT"])

    def test_skipped_incremental_pass_never_approves_without_a_published_gate(self):
        """An empty-path skip needs the published gate before it may approve.

        The skip itself carries no findings, so the pass relies entirely on the
        finalizer's unresolved-blocking-root check -- but an unpublished gate is
        an enforcement gap, and approval is withheld (and reported) instead of
        quietly emitting ``APPROVE`` under a check result nobody can see.
        """

        head = "b" * 40
        skipped = ReviewResult(
            summary="Incremental review: no changed paths since the last accepted review.",
            comments=(),
            provider="ollama",
            review_status="complete",
            coverage_mode="incremental",
        )
        withheld, withheld_calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
            ],
            result=skipped,
            auto_approve=True,
        )
        self.assertEqual(withheld.status, "published")
        self.assertEqual(withheld.diagnostic, "check_permission")
        self.assertEqual(posted_events(withheld_calls), ["COMMENT"])

        approved, approved_calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=()),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}, 200),
            ],
            result=skipped,
            auto_approve=True,
            checks=FakeCheckRuns(),
        )
        self.assertEqual(approved.status, "published")
        self.assertIsNone(approved.diagnostic)
        self.assertEqual(posted_events(approved_calls), ["COMMENT", "APPROVE"])

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
                        convergence_policy=ReviewConvergencePolicy(mode="legacy"),
                        allow_retired_legacy_policy=True,
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
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertIn("Review classification:", body["body"])
        self.assertIn(
            "[🚫 Blocking] [🟠 Severity: High] [⚡ Fix effort: Small] [✅ Lens: Correctness]",
            body["comments"][0]["body"],
        )
        self.assertEqual(body["commit_id"], head)
        self.assertEqual(body["event"], "COMMENT")
        self.assertIn("<!-- reviewsensei:review:v1", body["body"])

    def test_non_blocking_finding_can_publish_an_approval(self):
        head = "b" * 40
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
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
            ],
            checks=checks,
            result=non_blocking_result(),
        )

        self.assertEqual(outcome.status, "published")
        # Only non-blocking findings stay open, so the review publishes as a
        # COMMENT and the exact-head approval is the second review write.
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
        approval = review_payloads(calls)[1]
        self.assertEqual(approval["commit_id"], head)
        self.assertNotIn("comments", approval)
        self.assertEqual(outcome.diagnostic, None)
        self.assertEqual(checks.conclusions, [None, "success"])

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
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
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
            ],
            checks=checks,
            result=clean_result(),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
        approval = review_payloads(calls)[1]
        self.assertEqual(approval["commit_id"], head)
        self.assertNotIn("comments", approval)
        self.assertEqual(checks.statuses, ["in_progress", "completed"])
        self.assertEqual(checks.conclusions, [None, "success"])
        self.assertEqual(checks.head_shas, [head])

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
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=head)),
            ],
            checks=checks,
            result=clean_result(),
        )
        self.assertEqual(outcome.status, "published")
        query = graphql_queries(calls)[0]
        self.assertIn("reviewThreads", query["query"])
        self.assertIn("isResolved", query["query"])
        self.assertIn("body", query["query"])
        # The clean review still publishes; the unresolved App blocking root
        # withholds approval and is reported instead of a second review event.
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(checks.conclusions, [None, "success"])

    def test_resolved_review_threads_do_not_block_clean_review(self):
        head = "b" * 40
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
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
            ],
            checks=checks,
            result=clean_result(),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])

    def test_review_thread_lookup_failure_fails_closed_before_write(self):
        calls = self._run_failed_thread_scan(
            json_response({"errors": [{"message": "not exposed"}]})
        )
        # The review was published, the gate was concluded, and the approval
        # write never followed the untrustworthy thread read.
        self.assertEqual(len(non_check_calls(calls)), 6)

    def test_malformed_review_thread_errors_fail_closed_before_write(self):
        calls = self._run_failed_thread_scan(json_response({"errors": "malformed"}))
        self.assertEqual(len(non_check_calls(calls)), 6)

    def _run_failed_thread_scan(self, thread_response):
        """Drive one clean review whose finalizing thread read cannot be trusted."""

        head = "b" * 40
        checks = FakeCheckRuns()
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                thread_response,
            ],
            routes=((checks.matches, checks.respond),),
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
                check_token="check-token",
                auto_approve=True,
            )
        self.assertEqual(posted_events(calls), ["COMMENT"])
        return calls

    def test_review_thread_lookup_paginates_with_a_bounded_cursor(self):
        head = "b" * 40
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
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
                json_response(pr_payload(head_sha=head)),
            ],
            checks=checks,
            result=clean_result(),
        )
        self.assertEqual(outcome.status, "published")
        queries = graphql_queries(calls)
        self.assertEqual(queries[1]["variables"]["after"], "cursor-1")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")

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
        checks = FakeCheckRuns()
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
            checks=checks,
            result=clean_result(),
            auto_approve=True,
        )

        # An App-authored pull request receives no gate and no finalization
        # read of any kind: the whole run is the two reconciliation reads.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual([call[0] for call in calls], ["GET", "GET"])
        self.assertEqual(checks.writes, [])

    def test_disabled_auto_approve_keeps_blocking_findings_as_comments(self):
        head = "b" * 40
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
            ],
            checks=checks,
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        # The gate is the canonical github.reviews authority and is published
        # for every reviewed head: a legacy approval opt-out withholds APPROVE
        # but never turns the incomplete review's gate green or into a
        # second review-event authority.
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(checks.conclusions, [None, "action_required"])
        self.assertIsNone(outcome.diagnostic)
        self.assertEqual(len(non_check_calls(calls)), 5)

    def test_blocking_result_keeps_a_prior_same_head_approval_untouched(self):
        head = "b" * 40
        prior = review_marker(
            repository_id=1,
            pull_request=2,
            head_sha=head,
            result=clean_result(),
        )
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response(
                    [published_review(marker=prior, head_sha=head, state="APPROVED")]
                ),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
            ],
            checks=checks,
        )
        self.assertEqual(outcome.status, "published")
        # The blocking result publishes its finding as a COMMENT and withholds
        # approval. No REQUEST_CHANGES is emitted as a second gate authority and
        # the earlier approval is left alone rather than silently dismissed.
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(review_payloads(calls)[0]["comments"][0]["path"], "src/app.py")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        # The fixture review is not complete, so the gate reports the
        # non-passing action_required conclusion rather than a green result.
        self.assertEqual(checks.conclusions, [None, "action_required"])

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
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=head)),
            ],
            checks=checks,
            result=clean_result(),
            auto_approve=True,
        )
        # The gate for the current clean result is published, but the live
        # scan of this head's review threads still reports an unresolved App
        # blocking root, so the racy later approval is withheld and no review
        # event is emitted at all.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(posted_events(calls), [])
        self.assertEqual(checks.conclusions, ["success"])
        self.assertEqual(
            [query["operationName"] for query in graphql_queries(calls)],
            ["ReviewThreads"],
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
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response({"id": 6}, 200),
            ],
            checks=checks,
            result=clean_result(),
            auto_approve=True,
        )
        # Publisher returns already_published after reconciling the existing
        # identity review; the nested finalizer write is APPROVE.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), ["APPROVE"])
        self.assertEqual(review_payloads(calls)[0]["commit_id"], head)

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
                json_response({"id": 6}, 200),
            ],
            checks=FakeCheckRuns(),
            result=clean_result(),
            auto_approve=True,
        )
        # Publisher returns already_published after reconciling the existing
        # identity review; the nested finalizer write is APPROVE.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), ["APPROVE"])
        body = review_payloads(calls)[0]
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
                json_response({"id": 6}, 200),
            ]
        )
        self.assertEqual(outcome.status, "approved")
        self.assertEqual(posted_events(calls), ["APPROVE"])

    def test_change_request_posted_after_second_sweep_is_not_dismissed(self):
        """A blocking root only the later thread page reveals still withholds."""

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
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([existing]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(
                    has_next_page=True, end_cursor="cursor-1"
                ),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=head)),
            ],
            checks=checks,
            result=clean_result(),
            auto_approve=True,
        )
        # The gate for the current clean result is published, but the second
        # thread sweep still finds an unresolved App blocking root, so the
        # standing change request is neither dismissed nor duplicated and no
        # review event is emitted.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(posted_events(calls), [])
        self.assertEqual(checks.conclusions, ["success"])
        queries = graphql_queries(calls)
        self.assertEqual(
            [query["variables"]["after"] for query in queries], [None, "cursor-1"]
        )

    def test_foreign_change_request_never_becomes_a_second_authority(self):
        """A change request ReviewSensei did not write is never re-imposed."""

        head = "b" * 40
        foreign = published_review(
            marker="not a reviewsensei change-request marker",
            head_sha=head,
            state="CHANGES_REQUESTED",
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([foreign]),
                json_response({"id": 6}, 200),
            ],
        )
        # The clean eligibility approves on its own evidence; the foreign change
        # request is neither repeated nor treated as an owned gate.
        self.assertEqual(outcome.status, "approved")
        self.assertEqual(posted_events(calls), ["APPROVE"])

    def test_retired_change_request_review_is_never_reimposed(self):
        """An approval supersedes a retired change request without repeating it."""

        head = "b" * 40
        existing = published_review(
            marker="<!-- reviewsensei:changes-requested:v1 head=" + head + " -->",
            head_sha=head,
            state="CHANGES_REQUESTED",
        )
        outcome, calls = self.finalize(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=head)),
            ],
        )
        # The unresolved root withholds approval and the retired change request
        # is not re-issued as a duplicate gate authority.
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(posted_events(calls), [])
        self.assertEqual(existing["state"], "CHANGES_REQUESTED")

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

    def test_out_of_diff_comment_is_retained_in_the_summary(self):
        """Unanchored findings are summary-only by design: no inline thread is created."""

        from review_sensei.hosting.github.approval import has_blocking_findings

        bad = ReviewResult(
            summary="done",
            comments=(
                ReviewComment(
                    path="missing.py",
                    line=1,
                    body="unchanged",
                    blocking=True,
                ),
            ),
            provider="ollama",
        )
        self.assertTrue(has_blocking_findings(bad))
        head = "b" * 40
        responses = [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            json_response({"id": 5}, 200),
        ]
        http, calls = make_http(responses)
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=bad,
            diff=DIFF,
            app_slug="review-sensei[bot]",
            auto_approve=False,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[3][2].decode("utf-8"))
        self.assertEqual(body["comments"], [])
        self.assertIn("## Findings without a publishable inline location", body["body"])
        self.assertIn("`missing.py`", body["body"])
        self.assertIn("unchanged", body["body"])

        blocking_responses = [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            json_response({"id": 6}, 200),
        ]
        blocking_http, blocking_calls = make_http(blocking_responses)
        blocking_outcome = ReviewPublisher(http=blocking_http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=bad,
            diff=DIFF,
            app_slug="review-sensei[bot]",
            auto_approve=True,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(blocking_outcome.status, "published")
        blocking_body = __import__("json").loads(blocking_calls[3][2].decode("utf-8"))
        self.assertEqual(blocking_body["event"], "COMMENT")

    def test_deleted_line_comment_is_published_on_the_left_side(self):
        deletion = """diff --git a/src/legacy.py b/src/legacy.py
deleted file mode 100644
--- a/src/legacy.py
+++ /dev/null
@@ -1 +0,0 @@
-legacy = True
"""
        result = ReviewResult(
            summary="Deletion risk.",
            comments=(
                ReviewComment(
                    path="src/legacy.py",
                    line=1,
                    body="Removing this flag is unsafe.",
                    side="LEFT",
                ),
            ),
            provider="ollama",
        )
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
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=result,
            diff=deletion,
            app_slug="review-sensei[bot]",
            auto_approve=False,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[4][2].decode("utf-8"))
        self.assertEqual(body["comments"][0]["path"], "src/legacy.py")
        self.assertEqual(body["comments"][0]["line"], 1)
        self.assertEqual(body["comments"][0]["side"], "LEFT")
        self.assertNotIn("subject_type", body["comments"][0])

    def test_file_level_blocking_findings_are_retained_in_summary_for_blocking(
        self,
    ):
        # A blocking finding never changes the published review event: the
        # canonical `github.reviews` gate is the single authority, so the
        # finding rides in the summary of the comment and the check conclusion
        # carries the required-fix signal.
        result = ReviewResult(
            summary="Blocking file-wide finding.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=None,
                    body="This file needs a tighter contract.",
                    side="FILE",
                    blocking=True,
                ),
            ),
            provider="ollama",
        )
        head = "b" * 40
        responses = [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            json_response({"id": 5}, 200),
        ]
        http, calls = make_http(responses)
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=result,
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            auto_approve=True,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[3][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["comments"], [])
        self.assertIn("## Findings without a publishable inline location", body["body"])
        self.assertIn("`src/app.py`", body["body"])
        self.assertIn("tighter contract", body["body"])

    def test_file_level_comment_is_retained_in_summary_without_subject_type(self):
        result = ReviewResult(
            summary="File-wide finding.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=None,
                    body="This file needs a tighter contract.",
                    side="FILE",
                ),
            ),
            provider="ollama",
        )
        head = "b" * 40
        responses = [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            json_response({"id": 5}, 200),
        ]
        http, calls = make_http(responses)
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=result,
            diff=DIFF,
            app_slug="review-sensei[bot]",
            auto_approve=False,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[3][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        # GitHub's batch create-review input type defines no file subject type
        # and rejects a subject_type=file comment with HTTP 422, so a
        # file-level finding is folded into the summary instead.
        self.assertEqual(body["comments"], [])
        self.assertNotIn("subject_type", calls[3][2].decode("utf-8"))
        self.assertIn("## Findings without a publishable inline location", body["body"])
        self.assertIn("`src/app.py`", body["body"])
        self.assertIn("tighter contract", body["body"])

    def test_no_review_event_emits_a_subject_type_entry(self):
        # Public contract: no review event may emit `subject_type`, because
        # GitHub's batch create-review request type defines no file subject type
        # and requires a position for every inline comment. A file-level finding
        # therefore rides in the summary of the review that carries findings,
        # which for an approval is the findings review, not the approval marker.
        file_comment = ReviewComment(
            path="src/app.py",
            line=None,
            side="FILE",
            body="This file needs a tighter contract.",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        blocking_comment = replace(file_comment, blocking=True)
        head = "b" * 40
        file_result = ReviewResult(
            summary="File-wide finding.",
            comments=(file_comment,),
            provider="ollama",
            review_status="complete",
        )
        checks = FakeCheckRuns()
        approve = self.publish(
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
            ],
            result=file_result,
            auto_approve=True,
            checks=checks,
        )
        comment = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
            ],
            result=file_result,
            auto_approve=False,
        )
        # A blocking finding still publishes a plain comment: the review event
        # carries the findings, and only the canonical gate (the check
        # conclusion) may signal that fixes are required.
        blocking = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}, 200),
            ],
            result=ReviewResult(
                summary="File-wide finding.",
                comments=(blocking_comment,),
                provider="ollama",
                review_status="complete",
            ),
            auto_approve=True,
        )

        cases = (
            ("APPROVE", approve, 1, 0),
            ("COMMENT", comment, 0, 0),
            ("COMMENT", blocking, 0, 0),
        )
        for event, (outcome, calls), event_index, finding_index in cases:
            with self.subTest(event=event):
                self.assertEqual(outcome.status, "published")
                reviews = [
                    body.decode("utf-8")
                    for method, url, body in calls
                    if method == "POST" and url.endswith("/reviews")
                ]
                self.assertTrue(reviews)
                for payload in reviews:
                    self.assertNotIn("subject_type", payload)
                    parsed = json.loads(payload)
                    if "comments" in parsed:
                        self.assertEqual(parsed["comments"], [])
                self.assertEqual(json.loads(reviews[event_index])["event"], event)
                self.assertIn("tighter contract", reviews[finding_index])

    def test_publication_sources_never_build_a_subject_type_key(self):
        # Structural counterpart to the per-event payload contract: no review
        # event may emit `subject_type`, so the key must not exist in the
        # sources that build review payloads.
        root = Path(__file__).resolve().parents[1]
        for path in sorted((root / "src/review_sensei").rglob("*.py")):
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn('"subject_type"', source)
                self.assertNotIn("'subject_type'", source)

    def test_no_hosted_entry_point_can_opt_into_the_retired_policy(self):
        # `allow_retired_legacy_policy` is an internal escape hatch for this
        # package's tests and historical-fixture replay. No hosted entry point
        # (CLI or GitHubApplication) may reach it, so the identifier must stay
        # confined to the publication module that declares it.
        root = Path(__file__).resolve().parents[1]
        declaring = root / "src/review_sensei/hosting/github/publication.py"
        self.assertTrue(declaring.is_file())
        for path in sorted((root / "src/review_sensei").rglob("*.py")):
            if path == declaring:
                continue
            with self.subTest(path=path.name):
                self.assertNotIn(
                    "allow_retired_legacy_policy",
                    path.read_text(encoding="utf-8"),
                )
        # The textual check above cannot see an internal re-export that
        # accepts the flag dynamically, so pin the signature side too: any
        # route that could let production code opt in needs the parameter in a
        # signature, and only the publisher's own keyword-only parameter may
        # declare it.
        declaring_signatures = set()
        for module_info in pkgutil.walk_packages(
            review_sensei.__path__, prefix="review_sensei."
        ):
            module = importlib.import_module(module_info.name)
            members = []
            for member in vars(module).values():
                if getattr(member, "__module__", None) != module_info.name:
                    continue
                if inspect.isclass(member):
                    members.extend(vars(member).values())
                else:
                    members.append(member)
            for member in members:
                if not callable(member):
                    continue
                try:
                    signature = inspect.signature(member)
                except (TypeError, ValueError):  # pragma: no cover - builtins
                    continue
                parameter = signature.parameters.get("allow_retired_legacy_policy")
                if parameter is None:
                    continue
                declaring_signatures.add(f"{member.__module__}.{member.__qualname__}")
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(
            declaring_signatures,
            {"review_sensei.hosting.github.publication.ReviewPublisher.publish"},
        )

    def test_coverage_digest_and_unanchored_findings_can_fail_summary_limit(self):
        head = "b" * 40
        huge_body = "x" * 400
        result = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=None,
                    body=huge_body,
                    side="FILE",
                ),
            ),
            provider="ollama",
            limits=ReviewLimits(max_summary_bytes=128, max_comment_body_bytes=512),
            coverage=CoverageManifest(
                files=(FileCoverage(path="src/app.py", outcome="reviewed"),),
                enumeration_complete=True,
                enumerated_paths=("src/app.py",),
            ),
        )
        http, _calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
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
                result=result,
                diff=DIFF,
                app_slug="review-sensei[bot]",
                auto_approve=False,
            )

    def test_unanchored_findings_escape_marker_injection(self):
        rendered = format_unanchored_findings(
            (
                ReviewComment(
                    path="src/app.py",
                    line=None,
                    body="<!-- reviewsensei:fake repo=1 pr=2 -->",
                    side="FILE",
                ),
            )
        )
        self.assertNotIn("<!-- reviewsensei:fake", rendered)
        self.assertIn("\\<\\!-- reviewsensei:fake", rendered)

    def test_invalid_inline_line_on_a_changed_file_moves_to_summary(self):
        result = ReviewResult(
            summary="Retained.",
            comments=(ReviewComment(path="src/app.py", line=1, body="unchanged line"),),
            provider="ollama",
        )
        head = "b" * 40
        responses = [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            json_response({"id": 5}, 200),
        ]
        http, calls = make_http(responses)
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=result,
            diff=DIFF,
            app_slug="review-sensei[bot]",
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = __import__("json").loads(calls[3][2].decode("utf-8"))
        self.assertEqual(body["comments"], [])
        self.assertIn("## Findings without a publishable inline location", body["body"])
        self.assertIn("`src/app.py`", body["body"])
        self.assertIn("unchanged line", body["body"])

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
            checks=FakeCheckRuns(),
            result=clean_result(),
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), ["APPROVE"])
        body = review_payloads(calls)[0]
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
            checks=FakeCheckRuns(),
            result=non_blocking,
            auto_approve=True,
        )

        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), ["APPROVE"])
        body = review_payloads(calls)[0]
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
                json_response(pr_payload(head_sha=head)),
            ],
            checks=FakeCheckRuns(),
            result=clean_result(),
            auto_approve=True,
        )
        # The reconciled head keeps its single published review: the open
        # blocking root only withholds approval and is never answered with a
        # second review event.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), [])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")

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
            ],
            checks=FakeCheckRuns(),
        )
        # The blocking result is already published for this head; the persisted
        # facts withhold approval before any network read and no duplicate
        # review event is emitted.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), [])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")

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
                checks=FakeCheckRuns(),
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


class ApprovalEligibilityMarkerTests(unittest.TestCase):
    """The persisted eligibility document must survive its own encoding."""

    def test_published_marker_roundtrips_the_eligibility_document(self):
        head = "b" * 40
        document = eligibility_document(head_sha=head)
        marker = approval_eligibility_marker(document)
        # The writer emits padded base64url, so a reader that accepted only
        # the stripped form would silently withhold every delayed approval.
        self.assertIn("= -->", marker)
        body = f"Review summary.\n\n{marker}"
        self.assertEqual(approval_eligibility_from_body(body), document)

    def test_stripped_payload_padding_decodes_to_the_same_document(self):
        head = "b" * 40
        document = eligibility_document(head_sha=head)
        stripped = approval_eligibility_marker(document).replace("= -->", " -->")
        self.assertEqual(approval_eligibility_from_body(stripped), document)

    def test_unreadable_documents_fail_closed(self):
        head = "b" * 40
        marker = approval_eligibility_marker(eligibility_document(head_sha=head))
        broken = marker.replace("v1 ", "v1 !!!", 1)
        for body in (
            None,
            12345,
            "no marker here",
            broken,
            "<!-- reviewsensei:eligibility:v1 e30= -->",
        ):
            with self.subTest(body=body):
                self.assertIsNone(approval_eligibility_from_body(body))


class EffectiveBlockerPublicationTests(unittest.TestCase):
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
            "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
            "allow_retired_legacy_policy": True,
        }
        arguments.update(overrides)
        http, calls = make_http(responses)
        return ReviewPublisher(http=http).publish(**arguments), calls

    def _responses(self):
        head = "b" * 40
        return [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            graphql_review_threads_response(),
            json_response({"id": 5}, 200),
        ]

    def _comment_only_responses(self):
        head = "b" * 40
        return [
            json_response(pr_payload(head_sha=head)),
            json_response([]),
            json_response(pr_payload(head_sha=head)),
            json_response({"id": 5}, 200),
        ]

    def test_legacy_publishes_a_comment_for_an_admitted_blocker(self):
        # The stable check is the only imposed merge gate (ADR 0057), so even
        # the retired legacy mode may not post REQUEST_CHANGES: a model
        # blocker is published inline on the comment review instead.
        outcome, calls = self.publish(
            self._responses(),
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(self._posted_events(calls), ["COMMENT"])
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(len(body["comments"]), 1)
        self.assertIn("blocking=true", body["comments"][0]["body"])

    def test_merge_focused_does_not_request_changes_for_unevidenced_model_blocker(
        self,
    ):
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        outcome, calls = self.publish(
            self._comment_only_responses(),
            result=ReviewResult(
                summary="Summary.",
                comments=(
                    ReviewComment(
                        path="src/app.py",
                        line=2,
                        body="finding",
                        blocking=True,
                        severity="medium",
                    ),
                ),
                provider="ollama",
            ),
            convergence_policy=policy,
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["comments"], [])
        self.assertIn("## Advisory observations", body["body"])
        self.assertIn("Proposed: Blocking", body["body"])
        self.assertNotIn("blocking=true", body["body"])

    def test_merge_focused_publishes_inline_when_facts_admit_despite_non_blocking(
        self,
    ):
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        outcome, calls = self.publish(
            self._responses(),
            result=ReviewResult(
                summary="Summary.", comments=(comment,), provider="ollama"
            ),
            convergence_policy=policy,
            blocker_candidates=(facts,),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(self._posted_events(calls), ["COMMENT"])
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(len(body["comments"]), 1)
        self.assertIn("blocking=true", body["comments"][0]["body"])
        self.assertIn("Proposed: Non-blocking", body["comments"][0]["body"])

    def test_merge_focused_folds_admitted_file_level_blocker_without_auto_approve(
        self,
    ):
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        comment = ReviewComment(
            path="src/app.py",
            line=None,
            side="FILE",
            body="This file needs a tighter contract.",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        # An admitted blocker stays inline in operator mode even without
        # auto_approve, but a file-level target is not publishable inline.
        outcome, calls = self.publish(
            self._comment_only_responses(),
            result=ReviewResult(
                summary="Summary.", comments=(comment,), provider="ollama"
            ),
            convergence_policy=policy,
            blocker_candidates=(facts,),
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        self.assertNotIn("subject_type", calls[-1][2].decode("utf-8"))
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["comments"], [])
        self.assertIn("## Findings without a publishable inline location", body["body"])
        self.assertIn("tighter contract", body["body"])

    def test_confirmed_merge_focused_does_not_invent_failure_conditions(self):
        snapshot = {"src/app.py": "keep\nchange\n"}
        digest = hashlib.sha256(
            json.dumps(
                dict(sorted(snapshot.items())),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        confirmed = CandidateFinding(
            "Added line is unbounded.",
            "Call the changed helper with empty input.",
            "src/app.py",
            (EvidenceReference("src/app.py", 2, digest, "change"),),
            "The new line can fail closed callers.",
        )
        outcome, calls = self.publish(
            self._comment_only_responses(),
            result=ReviewResult(
                summary="Summary.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(confirmed,),
            snapshot=snapshot,
            snapshot_sha256=digest,
            evidence_policy="confirmed",
            convergence_policy=policy,
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["comments"], [])
        self.assertIn("## Advisory observations", body["body"])
        self.assertNotIn("blocking=true", body["body"])

    def test_confirmed_merge_focused_survives_dropped_candidate_facts(self):
        snapshot = {"src/app.py": "keep\nchange\n"}
        digest = hashlib.sha256(
            json.dumps(
                dict(sorted(snapshot.items())),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
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
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        outcome, calls = self.publish(
            self._responses(),
            result=ReviewResult(
                summary="Summary.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(confirmed, rejected),
            snapshot=snapshot,
            snapshot_sha256=digest,
            evidence_policy="confirmed",
            convergence_policy=policy,
            input_blocker_candidates=(facts, facts),
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(len(body["comments"]), 1)
        self.assertIn("Added line is unbounded.", body["comments"][0]["body"])

    def test_advisory_mode_publishes_comment_only_summary(self):
        policy = ReviewConvergencePolicy(mode="advisory", enforcement="publication")
        outcome, calls = self.publish(
            self._comment_only_responses(),
            result=ReviewResult(
                summary="Summary.",
                comments=(
                    ReviewComment(
                        path="src/app.py",
                        line=2,
                        body="finding",
                        blocking=True,
                        severity="medium",
                    ),
                ),
                provider="ollama",
            ),
            convergence_policy=policy,
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["comments"], [])
        self.assertIn("## Review observations", body["body"])

    def _posted_events(self, calls):
        events = []
        for call in calls:
            if len(call) < 3 or call[2] in (None, b""):
                continue
            try:
                body = json.loads(call[2].decode("utf-8"))
            except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(body, dict) and "event" in body:
                events.append(body["event"])
        return events

    def _admitted_non_blocking_comment(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="ollama"
        )
        return comment, facts, result

    def test_admitted_blocking_finding_never_becomes_a_review_event(self):
        """The gate carries enforcement, so no policy posts a review event for it."""

        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="finding",
                    blocking=False,
                    effective_blocking=True,
                ),
            ),
            provider="ollama",
        )
        for auto_approve in (False, True):
            with self.subTest(auto_approve=auto_approve):
                outcome, calls = self.publish(
                    self._comment_only_responses(),
                    result=admitted,
                    convergence_policy=policy,
                    auto_approve=auto_approve,
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(self._posted_events(calls), ["COMMENT"])
        advisory = ReviewConvergencePolicy(mode="advisory", enforcement="publication")
        outcome, calls = self.publish(
            self._comment_only_responses(),
            result=admitted,
            convergence_policy=advisory,
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(self._posted_events(calls), ["COMMENT"])

    def test_explicit_auto_approve_false_stays_comment_when_merge_focused_admits(
        self,
    ):
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        _comment, facts, review = self._admitted_non_blocking_comment()
        outcome, calls = self.publish(
            self._responses(),
            result=review,
            convergence_policy=policy,
            blocker_candidates=(facts,),
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(len(body["comments"]), 1)
        self.assertIn("blocking=true", body["comments"][0]["body"])
        self.assertNotIn("APPROVE", self._posted_events(calls))
        self.assertNotIn("REQUEST_CHANGES", self._posted_events(calls))

    def test_merge_focused_auto_approve_false_keeps_admitted_blocker_inline(self):
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = ReviewComment(
            path="src/app.py",
            line=2,
            body="must fix",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        demoted = ReviewComment(
            path="src/app.py",
            line=2,
            body="optional polish",
            blocking=True,
            severity="medium",
        )
        facts = derive_blocker_candidate(
            admitted,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        demoted_facts = derive_blocker_candidate(demoted, on_changed_path=True)
        outcome, calls = self.publish(
            self._responses(),
            result=ReviewResult(
                summary="Summary.",
                comments=(admitted, demoted),
                provider="ollama",
            ),
            convergence_policy=policy,
            blocker_candidates=(facts, demoted_facts),
            auto_approve=False,
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(len(body["comments"]), 1)
        self.assertIn("must fix", body["comments"][0]["body"])
        self.assertIn("blocking=true", body["comments"][0]["body"])
        self.assertIn("## Advisory observations", body["body"])
        self.assertIn("optional polish", body["body"])
        self.assertNotIn("APPROVE", self._posted_events(calls))
        self.assertNotIn("REQUEST_CHANGES", self._posted_events(calls))

    def test_env_merge_focused_does_not_apply_leftover_blocker_facts(self):
        _comment, facts, review = self._admitted_non_blocking_comment()
        with patch.dict("os.environ", {REVIEW_MODE_ENV: "merge-focused"}):
            with self.assertRaises(GitHubPublicationError):
                self.publish(
                    self._responses(),
                    result=review,
                    blocker_candidates=(facts,),
                    auto_approve=False,
                )

    def test_explicit_legacy_policy_is_not_replaced_from_environment(self):
        with patch.dict("os.environ", {REVIEW_MODE_ENV: "merge-focused"}):
            outcome, calls = self.publish(self._responses())
        self.assertEqual(outcome.status, "published")
        self.assertEqual(self._posted_events(calls), ["COMMENT"])
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(len(body["comments"]), 1)
        self.assertIn("blocking=true", body["comments"][0]["body"])

    def test_advisory_with_auto_approve_true_does_not_approve_or_request_changes(
        self,
    ):
        policy = ReviewConvergencePolicy(mode="advisory", enforcement="publication")
        _comment, facts, review = self._admitted_non_blocking_comment()
        outcome, calls = self.publish(
            self._comment_only_responses(),
            result=review,
            convergence_policy=policy,
            blocker_candidates=(facts,),
            auto_approve=True,
        )
        self.assertEqual(outcome.status, "published")
        body = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["comments"], [])
        self.assertIn("## Review observations", body["body"])
        self.assertNotIn("APPROVE", self._posted_events(calls))
        self.assertNotIn("REQUEST_CHANGES", self._posted_events(calls))


if __name__ == "__main__":
    unittest.main()
