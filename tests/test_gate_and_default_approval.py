"""Slice F acceptance: one check gate and evidence-bound default approval.

These regressions capture the real adapter HTTP events for the two acceptance
matrices of epic #181. Default approval: only eligible auto-approve cases emit
``APPROVE``, and every case reads its eligibility from persisted, exact-head
evidence (or from the finalizer's own live reads), never from a caller-supplied
approval boolean. One gate: the check-run conclusion is the only imposed merge
authority, so no case emits a second review-event authority, reaches a merge
endpoint, or enables auto-merge.

The fixtures are local copies on purpose: a shared fixture mutated by another
suite must not silently change what these acceptance rows exercise.
"""

import json
import unittest
from urllib.error import URLError

from review_sensei.convergence import derive_blocker_candidate
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.application import (
    GitHubApplication,
    GitHubWriteOptions,
)
from review_sensei.hosting.github.approval import approval_eligibility_from_result
from review_sensei.hosting.github.checks import (
    CheckOutcome,
    ReviewCheckPublisher,
    check_outcome_for_result,
    required_check_identity,
    review_check_outcome,
)
from review_sensei.hosting.github.errors import (
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from review_sensei.hosting.github.publication import (
    ReviewApprovalFinalizer,
    ReviewPublisher,
    approval_eligibility_marker,
    approval_marker,
    outcome_from_publication,
    review_marker,
)
from review_sensei.models import ReviewComment, ReviewResult

try:
    from fake_github_http import json_response, make_http
except ModuleNotFoundError:
    from tests.fake_github_http import json_response, make_http

HEAD = "b" * 40
BASE = "a" * 40
APP = "reviewsensei[bot]"

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""


def clean_result():
    return ReviewResult(
        summary="Summary.",
        comments=(),
        provider="ollama",
        review_status="complete",
    )


def optional_only_result():
    return ReviewResult(
        summary="Summary.",
        comments=(
            ReviewComment(
                path="src/app.py", line=2, body="Optional follow-up.", blocking=False
            ),
        ),
        provider="ollama",
        review_status="complete",
    )


def admitted_blocking_result(*, body_comment=False):
    """One complete review whose required finding carries trusted admission.

    The adapter applies caller facts; it does not re-derive them, so a
    required finding is reproduced the way a trusted evaluator hands it over.
    Placement must not dilute enforcement, so the body variant keeps the same
    facts on an unanchored file-level finding.
    """

    comment = ReviewComment(
        path="src/app.py",
        line=None if body_comment else 2,
        body=(
            "This file needs a tighter contract."
            if body_comment
            else "The authorization check is missing."
        ),
        blocking=True,
        severity="high",
        defect_kind="authz-failure",
        fix_effort="small",
        side="FILE" if body_comment else "RIGHT",
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
        summary="Summary.",
        comments=(comment,),
        provider="ollama",
        review_status="complete",
    )
    return result, facts


def partial_coverage_result():
    return ReviewResult(
        summary="Summary.",
        comments=(),
        provider="ollama",
        review_status="partial",
        coverage=CoverageManifest(
            files=(
                FileCoverage(
                    path="src/app.py",
                    outcome="partially-reviewed",
                    reason="too-large-hunk",
                ),
            ),
            enumeration_complete=True,
            enumerated_paths=("src/app.py",),
        ),
    )


def incomplete_result():
    return ReviewResult(
        summary="Summary.",
        comments=(),
        provider="ollama",
        review_status="incomplete",
        evidence_policy="confirmed",
    )


def pr_payload(*, head_sha, author="alice"):
    return {
        "state": "open",
        "draft": False,
        "user": {
            "login": author,
            "type": "Bot" if author.endswith("[bot]") else "User",
        },
        "head": {
            "sha": head_sha,
            "repo": {"full_name": "owner/repo", "fork": False},
        },
        "base": {
            "ref": "main",
            "sha": BASE,
            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
        },
    }


def graphql_review_threads_response(*, nodes=(), has_next_page=False, end_cursor=None):
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
        }
    )


def blocking_thread_node():
    return {
        "isResolved": False,
        "comments": {
            "nodes": [{"body": "[🚫 Blocking] must fix", "author": {"login": APP}}]
        },
    }


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


def non_check_calls(calls):
    """Return the calls a capture made outside the routed check-run gate."""

    return [call for call in calls if "/check-runs" not in call[1]]


def graphql_queries(calls):
    """Return the parsed GraphQL request bodies a capture emitted, in order."""

    return [
        json.loads(payload.decode("utf-8"))
        for method, url, payload in calls
        if method == "POST" and url.endswith("/graphql") and payload
    ]


class FakeCheckRuns:
    """Answer the check-run API the way GitHub does, one run per repository.

    The fake keeps the run it created so the concluding write of one
    publication updates that same run instead of creating a sibling, and it
    records every write so a test asserts the published gate instead of an
    internal flag.
    """

    def __init__(self, *, app_slug=APP, run_id=100, denied=False):
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


class GateAcceptanceCase(unittest.TestCase):
    """Shared harness: one publication with the check capability attached."""

    def publish(self, responses, *, checks=None, **overrides):
        arguments = {
            "token": "token",
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": HEAD,
            "base_branch": "main",
            "base_sha": BASE,
            "result": clean_result(),
            "diff": DIFF,
            "app_slug": APP,
        }
        arguments.update(overrides)
        if checks is not None:
            arguments["check_token"] = "check-token"
        http, calls = make_http(
            responses,
            routes=() if checks is None else ((checks.matches, checks.respond),),
        )
        outcome = ReviewPublisher(http=http).publish(**arguments)
        self.assert_single_event_authority(calls)
        return outcome, calls

    def assert_single_event_authority(self, calls):
        """No row may post a second review-event authority or reach a merge API."""

        self.assertLessEqual(set(posted_events(calls)), {"COMMENT", "APPROVE"})
        for _method, url, payload in calls:
            self.assertNotIn("/merge", url)
            if payload:
                self.assertNotIn(b"auto_merge", payload)


class DefaultApprovalAcceptanceTests(GateAcceptanceCase):
    def test_complete_eligible_positive_emits_one_bound_comment_and_approve(self):
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response({"id": 6}),
            ],
            checks=checks,
        )
        self.assertEqual(outcome.status, "published")
        self.assertIsNone(outcome.diagnostic)
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
        approval = review_payloads(calls)[1]
        self.assertEqual(approval["commit_id"], HEAD)
        self.assertEqual(
            approval["body"],
            approval_marker(
                repository_id=1, pull_request=2, head_sha=HEAD, base_sha=BASE
            ),
        )
        self.assertNotIn("comments", approval)
        self.assertEqual(checks.head_shas, [HEAD])
        self.assertEqual(checks.conclusions, [None, "success"])

    def test_optional_only_findings_remain_advisory_and_still_approve(self):
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response({"id": 6}),
            ],
            checks=checks,
            result=optional_only_result(),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
        comment = review_payloads(calls)[0]
        self.assertEqual(comment["comments"], [])
        self.assertIn("## Advisory observations", comment["body"])
        self.assertIn("Optional follow-up.", comment["body"])
        self.assertEqual(checks.conclusions, [None, "success"])

    def test_required_inline_finding_fails_the_gate_and_withholds(self):
        checks = FakeCheckRuns()
        result, facts = admitted_blocking_result()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response({"id": 5}),
            ],
            checks=checks,
            result=result,
            blocker_candidates=(facts,),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(checks.conclusions, [None, "failure"])
        self.assertEqual(review_payloads(calls)[0]["comments"][0]["path"], "src/app.py")
        # The withheld approval costs no reads: the required finding is decided
        # from the same result the review published.
        self.assertEqual(len(non_check_calls(calls)), 5)

    def test_required_body_finding_fails_the_gate_and_withholds(self):
        checks = FakeCheckRuns()
        result, facts = admitted_blocking_result(body_comment=True)
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
            ],
            checks=checks,
            result=result,
            blocker_candidates=(facts,),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertEqual(checks.conclusions, [None, "failure"])
        comment = review_payloads(calls)[0]
        self.assertEqual(comment["comments"], [])
        self.assertIn("This file needs a tighter contract.", comment["body"])
        # An unanchored required finding is carried by the body without any
        # thread read, and it still fails the gate and withholds approval.
        self.assertEqual(len(non_check_calls(calls)), 4)

    def test_partial_coverage_never_approves(self):
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
            ],
            checks=checks,
            result=partial_coverage_result(),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "review_incomplete")
        self.assertEqual(checks.conclusions, [None, "action_required"])

    def test_incomplete_review_evidence_never_approves(self):
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
            ],
            checks=checks,
            result=incomplete_result(),
        )
        # A review that never established complete evidence maps onto the
        # non-passing conclusion, never onto a passing or skipped one.
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "review_incomplete")
        self.assertEqual(checks.conclusions, [None, "action_required"])

    def test_stale_head_withholds_before_any_approval_write(self):
        eligibility = approval_eligibility_from_result(
            clean_result(), head_sha=HEAD, enabled=True, app_authored=False
        )
        http, calls = make_http([json_response(pr_payload(head_sha="c" * 40))])
        outcome = ReviewApprovalFinalizer(http=http).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=eligibility,
        )
        self.assertEqual(outcome.status, "skipped_stale_head")
        self.assertEqual([method for method, _, _ in calls], ["GET"])

    def test_unqualified_configuration_never_approves(self):
        for qualification in ("unverified", "missing"):
            with self.subTest(qualification=qualification):
                checks = FakeCheckRuns()
                outcome, calls = self.publish(
                    [
                        json_response(pr_payload(head_sha=HEAD)),
                        json_response([]),
                        json_response(pr_payload(head_sha=HEAD)),
                        json_response({"id": 5}),
                    ],
                    checks=checks,
                    qualification=qualification,
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(posted_events(calls), ["COMMENT"])
                self.assertEqual(outcome.diagnostic, "qualification_unverified")
                self.assertEqual(checks.conclusions, [None, "success"])

    def test_app_authored_pull_request_receives_no_gate_and_no_approval(self):
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD, author=APP)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD, author=APP)),
                json_response({"id": 5}),
            ],
            checks=checks,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "app_authored")
        self.assertEqual(checks.writes, [])

    def test_missing_gate_capability_reports_permission_and_withholds(self):
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
            ],
            checks=None,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "check_permission")
        self.assertFalse(any("/check-runs" in url for _, url, _ in calls))

    def test_writes_disabled_never_reaches_the_api(self):
        class ExplodingBroker:
            def request_oidc_token(self):
                raise AssertionError("disabled writes must not request a capability")

            def exchange(self, token, *, capability=None):
                raise AssertionError("disabled writes must not exchange a capability")

        class EnabledBroker:
            def request_oidc_token(self):
                return "oidc-token"

            def exchange(self, token, *, capability=None):
                return f"capability-{capability}"

        class UnusedPublisher:
            def __getattr__(self, _name):
                raise AssertionError("disabled writes must not publish")

        def application(http, broker):
            return GitHubApplication(
                broker=broker,
                http=http,
                reviewer=ReviewPublisher(http=http),
                learner=UnusedPublisher(),
                replier=UnusedPublisher(),
            )

        def publish(target, options):
            return target.publish_review(
                options=options,
                oidc_token=None,
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=HEAD,
                base_branch="main",
                base_sha=BASE,
                result=clean_result(),
                diff=DIFF,
                app_slug=APP,
            )

        disabled_http, disabled_calls = make_http([])
        outcome = publish(
            application(disabled_http, ExplodingBroker()),
            GitHubWriteOptions(auto_review=True, github_writes=False),
        )
        self.assertEqual(outcome.status, "disabled")
        # No write-capability exchange, no gate, no review event, no approval.
        self.assertEqual(disabled_calls, [])
        projected = outcome_from_publication(outcome)
        self.assertEqual(projected.status, "skipped_policy")
        self.assertEqual(projected.diagnostic, "writes_disabled")

        # The same call with writes enabled reaches the API, so the empty
        # capture above is the disabled option and not a broken harness.
        checks = FakeCheckRuns()
        enabled_http, enabled_calls = make_http(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response({"id": 6}),
            ],
            routes=((checks.matches, checks.respond),),
        )
        enabled_outcome = publish(
            application(enabled_http, EnabledBroker()),
            GitHubWriteOptions(auto_review=True, github_writes=True),
        )
        self.assertEqual(enabled_outcome.status, "published")
        self.assertEqual(posted_events(enabled_calls), ["COMMENT", "APPROVE"])

    def test_delayed_finalization_approves_only_from_the_persisted_document(self):
        eligibility = approval_eligibility_from_result(
            clean_result(), head_sha=HEAD, enabled=True, app_authored=False
        )
        carried = [
            {
                "id": 13,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": f"Review summary.\n\n{approval_eligibility_marker(eligibility)}",
                "user": {"login": APP, "type": "Bot"},
            }
        ]
        read_http, read_calls = make_http([json_response(carried)])
        loaded = ReviewApprovalFinalizer(http=read_http).load_eligibility(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertEqual(loaded, eligibility)
        self.assertEqual([method for method, _, _ in read_calls], ["GET"])

        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=HEAD)),
                json_response(carried),
                json_response({"id": 9}),
            ]
        )
        outcome = ReviewApprovalFinalizer(http=http).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=loaded,
        )
        self.assertEqual(outcome.status, "approved")
        self.assertEqual(posted_events(calls), ["APPROVE"])
        self.assertEqual(review_payloads(calls)[0]["commit_id"], HEAD)

    def test_delayed_finalization_without_a_document_never_infers_eligibility(self):
        http, calls = make_http(
            [
                json_response([]),
                json_response(
                    [
                        {
                            "id": 13,
                            "commit_id": HEAD,
                            "state": "COMMENTED",
                            "body": "Review summary without a marker.",
                            "user": {"login": APP, "type": "Bot"},
                        },
                        {
                            "id": 14,
                            "commit_id": HEAD,
                            "state": "COMMENTED",
                            "body": "Human review.",
                            "user": {"login": "alice", "type": "User"},
                        },
                    ]
                ),
            ]
        )
        loaded = ReviewApprovalFinalizer(http=http).load_eligibility(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertIsNone(loaded)
        self.assertEqual({method for method, _, _ in calls}, {"GET"})


class OneGateAcceptanceTests(GateAcceptanceCase):
    def test_gate_identity_never_adopts_another_apps_run(self):
        checks = FakeCheckRuns()
        checks.run = {
            "id": 7,
            "name": "ReviewSensei",
            "app": {"slug": "another-app[bot]"},
        }
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response({"id": 6}),
            ],
            checks=checks,
        )
        self.assertEqual(outcome.status, "published")
        # The pending write states its identity and producer, and the foreign
        # run 7 is never updated: the App creates and then owns run 100 for
        # this head.
        self.assertEqual(checks.head_shas, [HEAD])
        self.assertEqual(checks.writes[0]["name"], "ReviewSensei")
        self.assertEqual(checks.writes[0]["external_id"], "reviewsensei-review-v1")
        patched = [url for method, url, _ in calls if method == "PATCH"]
        self.assertEqual([url.split("/check-runs/")[-1] for url in patched], ["100"])

    def test_gate_permission_failure_still_publishes_the_review(self):
        checks = FakeCheckRuns(denied=True)
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
            ],
            checks=checks,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "check_permission")
        self.assertEqual(checks.writes, [])

    def test_pending_gate_state_precedes_its_conclusion_on_the_same_head(self):
        checks = FakeCheckRuns()
        outcome, _calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response({"id": 6}),
            ],
            checks=checks,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(checks.statuses, ["in_progress", "completed"])
        self.assertEqual(checks.conclusions, [None, "success"])

    def test_cancellation_publishes_a_non_passing_conclusion(self):
        http, calls = make_http(
            [json_response({"check_runs": []}), json_response({"id": 4}, 201)]
        )
        result = ReviewCheckPublisher(http=http).cancel(
            token="token", repository="owner/repo", head_sha=HEAD, app_slug=APP
        )
        self.assertEqual(result.status, "created")
        body = json.loads(calls[1][2].decode("utf-8"))
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["conclusion"], "cancelled")
        self.assertEqual(body["head_sha"], HEAD)

    def test_same_head_race_withholds_a_racy_approval(self):
        """A blocking root only the later thread page reveals still withholds."""

        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
                json_response(pr_payload(head_sha=HEAD)),
                graphql_review_threads_response(
                    has_next_page=True, end_cursor="cursor-1"
                ),
                graphql_review_threads_response(nodes=(blocking_thread_node(),)),
                json_response(pr_payload(head_sha=HEAD)),
            ],
            checks=checks,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        # The review still publishes its passing gate: the live scan of the
        # exact head is what withheld the approval, not the review result.
        self.assertEqual(checks.conclusions, [None, "success"])
        queries = graphql_queries(calls)
        self.assertEqual(
            [query["operationName"] for query in queries], ["ReviewThreads"] * 2
        )
        self.assertEqual(
            [query["variables"]["after"] for query in queries], [None, "cursor-1"]
        )

    def test_advisory_transition_imposes_no_gate_and_no_approval(self):
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                json_response({"id": 5}),
            ],
            checks=checks,
            reviews_policy="advisory",
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        self.assertIsNone(outcome.diagnostic)
        self.assertEqual(checks.conclusions, [None, "neutral"])

    def test_advisory_transition_after_an_approval_neither_repeats_nor_revokes_it(
        self,
    ):
        existing = [
            {
                "body": review_marker(
                    repository_id=1,
                    pull_request=2,
                    head_sha=HEAD,
                    result=clean_result(),
                ),
                "commit_id": HEAD,
                "state": "COMMENTED",
                "user": {"login": APP, "type": "Bot"},
            },
            {
                "body": approval_marker(
                    repository_id=1, pull_request=2, head_sha=HEAD, base_sha=BASE
                ),
                "commit_id": HEAD,
                "state": "APPROVED",
                "user": {"login": APP, "type": "Bot"},
            },
        ]
        checks = FakeCheckRuns()
        outcome, calls = self.publish(
            [json_response(pr_payload(head_sha=HEAD)), json_response(existing)],
            checks=checks,
            reviews_policy="advisory",
        )
        # The prior approval is neither repeated nor replaced by any event:
        # advisory imposes no gate and holds no approval authority.
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), [])
        self.assertIsNone(outcome.diagnostic)
        self.assertEqual(checks.conclusions, ["neutral"])


class CheckPublisherContractTests(unittest.TestCase):
    """The gate publisher's own contract: vocabulary, identity, and failure."""

    def check_publisher(self, responses):
        http, calls = make_http(list(responses))
        return ReviewCheckPublisher(http=http), calls

    def test_policy_and_status_vocabulary_fails_closed(self):
        with self.assertRaisesRegex(ReviewInputError, "policy mode"):
            review_check_outcome(
                policy="merge-focused", review_status="complete", required_fixes=False
            )
        with self.assertRaisesRegex(ReviewInputError, "review status"):
            review_check_outcome(
                policy="auto-approve", review_status="unknown", required_fixes=False
            )
        with self.assertRaisesRegex(ReviewInputError, "validated review result"):
            check_outcome_for_result("not-a-result", policy="auto-approve")
        with self.assertRaisesRegex(ReviewInputError, "producer slug"):
            required_check_identity(app_slug=" ")

    def test_complete_rejects_an_invalid_conclusion_and_empty_output(self):
        publisher, calls = self.check_publisher([])
        with self.assertRaisesRegex(GitHubPublicationError, "conclusion is invalid"):
            publisher.complete(
                token="token",
                repository="owner/repo",
                head_sha=HEAD,
                app_slug=APP,
                outcome=CheckOutcome(conclusion="passed", title="T", summary="S"),
            )
        with self.assertRaisesRegex(ReviewInputError, "must be non-empty"):
            publisher.complete(
                token="token",
                repository="owner/repo",
                head_sha=HEAD,
                app_slug=APP,
                outcome=CheckOutcome(conclusion="success", title="", summary="S"),
            )
        # Both rejections happen before any API traffic.
        self.assertEqual(calls, [])

    def test_lookup_and_write_failures_are_bounded(self):
        publisher, _calls = self.check_publisher(
            [json_response({"message": "nope"}, 404)]
        )
        with self.assertRaisesRegex(GitHubPublicationError, "check lookup failed"):
            publisher.start(
                token="token", repository="owner/repo", head_sha=HEAD, app_slug=APP
            )

        publisher, _calls = self.check_publisher([json_response({"check_runs": {}})])
        with self.assertRaisesRegex(GitHubPublicationError, "response was invalid"):
            publisher.start(
                token="token", repository="owner/repo", head_sha=HEAD, app_slug=APP
            )

        publisher, _calls = self.check_publisher(
            [json_response({"check_runs": []}), json_response({"message": "nope"}, 422)]
        )
        with self.assertRaisesRegex(GitHubPublicationError, "rejected"):
            publisher.start(
                token="token", repository="owner/repo", head_sha=HEAD, app_slug=APP
            )

        publisher, _calls = self.check_publisher(
            [json_response({"check_runs": []}), json_response({"message": "nope"}, 404)]
        )
        with self.assertRaisesRegex(GitHubPublicationError, "not found"):
            publisher.start(
                token="token", repository="owner/repo", head_sha=HEAD, app_slug=APP
            )

    def test_transport_failure_is_transient(self):
        publisher, _calls = self.check_publisher([URLError("unreachable")])
        with self.assertRaisesRegex(GitHubPublicationTransientError, "temporarily"):
            publisher.start(
                token="token", repository="owner/repo", head_sha=HEAD, app_slug=APP
            )


if __name__ == "__main__":
    unittest.main()
