import unittest

from review_sensei.hosting.github import (
    GitHubPublicationError,
    GitHubPublicationTransientError,
    ReviewPublisher,
)
from review_sensei.hosting.github.publication import review_marker
from review_sensei.models import ReviewComment, ReviewResult

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
        comments=(ReviewComment(path="src/app.py", line=2, body="finding"),),
        provider="ollama",
    )


def pr_payload(*, head_sha, fork=False, state="open", draft=False):
    return {
        "state": state,
        "draft": draft,
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
            json_response({"id": 5}, 201),
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
        post = calls[3]
        body = __import__("json").loads(post[2].decode("utf-8"))
        self.assertIn("<!-- reviewsensei:review:v1", body["body"])
        self.assertEqual(body["commit_id"], head)
        self.assertEqual(body["comments"][0]["path"], "src/app.py")

    def test_head_is_rechecked_after_marker_pagination_before_write(self):
        head = "b" * 40
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha="c" * 40)),
            ]
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
            result=result(),
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
            result=result(),
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

    def test_existing_marker_is_reconciled_before_post(self):
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
                    [
                        {
                            "body": f"Summary.\n\n{marker}",
                            "commit_id": head,
                            "user": {"login": "reviewsensei[bot]"},
                        }
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
            result=result(),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "already_published")
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
                    [
                        {
                            "body": marker,
                            "commit_id": head,
                            "user": {"login": "reviewsensei[bot]"},
                        }
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
            result=result(),
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
                result=result(),
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
            result=result(),
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
                outcome, calls = self.publish(json_response(payload))
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
                        response,
                    ]
                )


if __name__ == "__main__":
    unittest.main()
