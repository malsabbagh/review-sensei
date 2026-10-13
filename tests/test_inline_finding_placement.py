"""Placement, body budget, and thread-resolution regressions."""

import json
import unittest

from review_sensei_github import GitHubPublicationError, ReviewPublisher
from review_sensei_github.approval import approval_eligibility_from_result
from review_sensei_github.publication import (
    IndexedFinding,
    ReviewApprovalFinalizer,
    finding_index_marker,
    parse_finding_index,
)

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.validation import ReviewLimits
from tests.fake_github_http import json_response, make_http, placement_responses
from tests.test_github_publication import (
    DIFF,
    clean_result,
    graphql_review_threads_response,
    pr_payload,
)

HEAD = "b" * 40
BASE = "a" * 40
LEFT_DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,1 @@
-old
+new
"""
BINARY_DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
Binary files a/src/app.py and b/src/app.py differ
"""


def _result(*comments, **fields):
    return ReviewResult(
        summary="Summary.",
        comments=comments,
        provider="ollama",
        review_status="complete",
        **fields,
    )


class InlinePlacementTests(unittest.TestCase):
    def publish(self, responses, **overrides):
        arguments = {
            "token": "token",
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": HEAD,
            "base_branch": "main",
            "base_sha": BASE,
            "result": _result(
                ReviewComment(path="src/app.py", line=2, body="finding", blocking=True)
            ),
            "diff": DIFF,
            "app_slug": "reviewsensei[bot]",
            "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
            "allow_retired_legacy_policy": True,
            "auto_approve": False,
        }
        arguments.update(overrides)
        http, calls = make_http(responses)
        return ReviewPublisher(http=http).publish(**arguments), calls

    def _review(self, calls):
        return next(
            json.loads(data.decode("utf-8"))
            for method, url, data in calls
            if method == "POST" and str(url).endswith("/pulls/2/reviews")
        )

    def test_deleted_side_anchor_is_published_inline(self):
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
            ],
            result=_result(
                ReviewComment(
                    path="src/app.py",
                    line=1,
                    side="LEFT",
                    body="deleted",
                    blocking=True,
                )
            ),
            diff=LEFT_DIFF,
        )
        self.assertEqual(outcome.status, "published")
        review = self._review(calls)
        self.assertEqual(review["comments"][0]["side"], "LEFT")
        self.assertEqual(review["comments"][0]["line"], 1)

    def test_file_missing_and_binary_findings_stay_in_the_body(self):
        cases = (
            (
                "file",
                ReviewComment(
                    path="src/app.py",
                    line=None,
                    side="FILE",
                    body="file",
                    blocking=True,
                ),
                DIFF,
            ),
            (
                "missing-path",
                ReviewComment(
                    path="src/missing.py", line=1, body="gone", blocking=True
                ),
                DIFF,
            ),
            (
                "binary",
                ReviewComment(path="src/app.py", line=1, body="binary", blocking=True),
                BINARY_DIFF,
            ),
        )
        for name, comment, diff in cases:
            with self.subTest(name=name):
                outcome, calls = self.publish(
                    [
                        json_response(pr_payload(head_sha=HEAD)),
                        json_response([]),
                        json_response(pr_payload(head_sha=HEAD)),
                        json_response({"id": 5}, 200),
                    ],
                    result=_result(comment),
                    diff=diff,
                )
                self.assertEqual(outcome.status, "published")
                review = self._review(calls)
                self.assertEqual(review["comments"], [])
                index = parse_finding_index(
                    review["body"], repository_id=1, pull_request=2, head_sha=HEAD
                )
                self.assertTrue(index.entries[0].body_placed)
                self.assertTrue(index.entries[0].blocking)

    def test_advisory_review_keeps_a_valid_line_in_the_body(self):
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}, 200),
            ],
            result=_result(
                ReviewComment(path="src/app.py", line=2, body="note", blocking=True)
            ),
            convergence_policy=ReviewConvergencePolicy(mode="advisory"),
        )
        review = self._review(calls)
        self.assertEqual(outcome.status, "published")
        self.assertEqual(review["comments"], [])
        self.assertIn("note", review["body"])

    def test_optional_threads_follow_conversation_resolution(self):
        comment = ReviewComment(
            path="src/app.py", line=2, body="optional", blocking=False
        )
        required, required_calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                *placement_responses(required=True),
                json_response({"id": 5}, 200),
            ],
            result=_result(comment),
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(required.status, "published")
        self.assertEqual(self._review(required_calls)["comments"], [])
        permitted, permitted_calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                *placement_responses(required=False),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
            ],
            result=_result(comment),
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            allow_retired_legacy_policy=True,
        )
        self.assertEqual(permitted.status, "published")
        self.assertEqual(len(self._review(permitted_calls)["comments"]), 1)

    def test_conflicting_classifications_are_rejected(self):
        from review_sensei.errors import ReviewInputError

        with self.assertRaisesRegex(
            ReviewInputError, "conflicting finding classifications"
        ):
            self.publish(
                [
                    json_response(pr_payload(head_sha=HEAD)),
                    json_response([]),
                    json_response(pr_payload(head_sha=HEAD)),
                ],
                result=_result(
                    ReviewComment(
                        path="src/app.py", line=2, body="same", blocking=True
                    ),
                    ReviewComment(
                        path="src/app.py", line=2, body="same", blocking=False
                    ),
                ),
            )

    def test_ambiguous_rejection_does_not_invent_an_invalid_line(self):
        with self.assertRaisesRegex(
            GitHubPublicationError, "intended review is absent"
        ):
            self.publish(
                [
                    json_response(pr_payload(head_sha=HEAD)),
                    json_response([]),
                    json_response(pr_payload(head_sha=HEAD)),
                    graphql_review_threads_response(),
                    json_response({"message": "Validation Failed"}, 422),
                    json_response([]),
                ],
                result=_result(
                    ReviewComment(
                        path="src/app.py", line=2, body="finding", blocking=True
                    )
                ),
            )

    def test_failed_supersession_leaves_publication_successful(self):
        from review_sensei_github.publication import (
            finding_instance_marker,
            finding_marker,
        )

        from review_sensei.context import finding_lifecycle_for_comment

        comment = ReviewComment(
            path="src/app.py", line=2, body="finding", blocking=True
        )
        current = _result(comment)
        marker = finding_marker(
            repository_id=1,
            pull_request=2,
            head_sha="c" * 40,
            base_sha=BASE,
            result=current,
            blocking=True,
            fingerprint=finding_lifecycle_for_comment(comment).fingerprint,
        )
        marker += "\n\n" + finding_instance_marker(comment)
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(
                    nodes=(
                        {
                            "id": "PRRT_old",
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
                    )
                ),
                json_response({"id": 5}, 200),
                json_response({}, 500),
            ],
            result=current,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(len(self._review(calls)["comments"]), 1)
        self.assertTrue(any(str(url).endswith("/graphql") for _, url, _ in calls))

    def test_optional_prose_can_be_omitted_without_dropping_the_index(self):
        low = ReviewComment(
            path="src/low.py",
            line=None,
            side="FILE",
            body="low " + ("x" * 4000),
            blocking=False,
            severity="low",
        )
        high = ReviewComment(
            path="src/high.py",
            line=None,
            side="FILE",
            body="high " + ("y" * 4000),
            blocking=False,
            severity="high",
        )
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}, 200),
            ],
            result=_result(
                low,
                high,
                limits=ReviewLimits(max_summary_bytes=100),
            ),
        )
        self.assertEqual(outcome.status, "published")
        review = self._review(calls)
        self.assertIn("omitted for size", review["body"])
        index = parse_finding_index(
            review["body"], repository_id=1, pull_request=2, head_sha=HEAD
        )
        self.assertEqual(len(index.entries), 2)
        self.assertTrue(all(entry.body_placed for entry in index.entries))
        self.assertIn("high ", review["body"])
        self.assertNotIn(low.body, review["body"])

    def test_missing_inline_thread_withholds_approval(self):
        instance = "ab" * 32
        lifecycle = "cd" * 32
        eligibility = approval_eligibility_from_result(
            clean_result(),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
            qualification="not-required",
            check_published=True,
        )
        index = finding_index_marker(
            repository_id=1,
            pull_request=2,
            head_sha=HEAD,
            base_sha=BASE,
            result_digest=eligibility.result_digest,
            entries=(
                IndexedFinding(
                    instance=instance,
                    lifecycle=lifecycle,
                    blocking=True,
                    needs_human=False,
                    body_placed=False,
                ),
            ),
        )
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=HEAD)),
                json_response(
                    [
                        {
                            "id": 5,
                            "commit_id": HEAD,
                            "user": {"login": "reviewsensei[bot]"},
                            "body": "Review.\n\n" + index,
                        }
                    ]
                ),
            ]
        )
        outcome = ReviewApprovalFinalizer(http=http).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug="reviewsensei[bot]",
            eligibility=eligibility,
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertFalse(
            any(
                method == "POST" and str(url).endswith("/pulls/2/reviews")
                for method, url, _data in calls
            )
        )
