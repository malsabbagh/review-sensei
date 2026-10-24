"""Failure categories preserve evidence, publication order and approval policy."""

import io
import logging
import traceback
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch
from urllib.error import HTTPError

from review_sensei_github.application import GitHubApplication, GitHubWriteOptions
from review_sensei_github.check_diagnostics import CheckCapability, CheckFailure
from review_sensei_github.checks import ReviewCheckPublisher
from review_sensei_github.errors import (
    GitHubBrokerClientError,
    GitHubHTTPError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
)
from review_sensei_github.publication import ReviewPublisher, outcome_from_publication

from review_sensei.outcomes import (
    PUBLIC_DIAGNOSTICS,
    RunOutcome,
    emit_host_outcome,
    render_actions_summary,
    sanitize_diagnostic,
)
from review_sensei.presentation import (
    render_check_permission_warning,
    render_check_warning,
)
from review_sensei.schemas import validate_public_document
from review_sensei.validation import ReviewLimits
from tests import test_gate_and_default_approval as gate

SECRET = "SENTINEL_PRIVATE_CHECK_CREDENTIAL"
STATUSES = (401, 403, 404, 422, 429, 500, 503, 599)


def expected_code(status):
    if status in (401, 403):
        return "check_access_denied"
    if status == 429 or status >= 500:
        return "check_temporarily_unavailable"
    return "check_publication_failed"


def responses_for_review():
    return [
        gate.json_response(gate.pr_payload(head_sha=gate.HEAD)),
        gate.json_response([]),
        gate.json_response(gate.pr_payload(head_sha=gate.HEAD)),
        gate.json_response({"id": 5}),
        gate.json_response(gate.pr_payload(head_sha=gate.HEAD)),
        gate.graphql_review_threads_response(),
        gate.json_response(gate.pr_payload(head_sha=gate.HEAD)),
        gate.json_response([]),
        gate.json_response({"id": 6}),
    ]


class FailingChecks(gate.FakeCheckRuns):
    def __init__(self, *, operation, status=403, final=False, failure=None):
        super().__init__()
        self.operation = operation
        self.status = status
        self.final = final
        self.failure = failure
        self.requests = []
        if operation == "update":
            self.run = {"id": 100, "name": "ReviewSensei", "app": {"slug": gate.APP}}

    def respond(self, request):
        operation = {"GET": "lookup", "POST": "create", "PATCH": "update"}[
            request.method
        ]
        self.requests.append(operation)
        attempt = sum(item == "lookup" for item in self.requests)
        if (
            self.final
            and self.operation == "create"
            and operation == "lookup"
            and attempt == 2
        ):
            self.run = None
        if operation == self.operation and (not self.final or attempt == 2):
            if isinstance(self.failure, Exception):
                raise self.failure
            if self.failure == "malformed":
                return gate.json_response(
                    {"check_runs": SECRET, "id": SECRET},
                    200 if operation != "create" else 201,
                )
            return gate.json_response(
                {"message": SECRET, "url": SECRET, "headers": SECRET}, self.status
            )
        return super().respond(request)


class CheckDiagnosticsTests(gate.GateAcceptanceCase):
    def assert_unconfirmed(self, outcome, calls, code):
        self.assertEqual(outcome.status, "published")
        self.assertEqual(outcome.diagnostic, code)
        body = gate.review_payloads(calls)[0]["body"]
        self.assertIn("Warning: ReviewSensei merge gate unconfirmed", body)
        self.assertIn("publication could not be confirmed", body)
        self.assertIn(code, body)
        self.assertNotIn("Grant", body)
        self.assertNotIn("branch protection", body)
        # This is the exact eligibility document persisted with the review.
        from review_sensei_github.publication import approval_eligibility_from_body

        eligibility = approval_eligibility_from_body(body)
        self.assertIsNotNone(eligibility)
        self.assertFalse(eligibility.facts.check_published)
        public = outcome_from_publication(outcome).to_dict()
        validate_public_document(public, "run-outcome")
        self.assertEqual(public["diagnostic"], code)
        self.assertNotIn(SECRET, body + repr(outcome) + repr(public))

    def test_all_http_failures_have_warnings_and_persisted_false_gate(self):
        for operation, final in (
            ("lookup", False),
            ("create", False),
            ("update", False),
            ("lookup", True),
            ("create", True),
            ("update", True),
        ):
            for status in STATUSES:
                with self.subTest(operation=operation, final=final, status=status):
                    checks = FailingChecks(
                        operation=operation, status=status, final=final
                    )
                    outcome, calls = self.publish(responses_for_review(), checks=checks)
                    self.assert_unconfirmed(outcome, calls, expected_code(status))
                    self.assertEqual(gate.posted_events(calls), ["COMMENT", "APPROVE"])
                    expected = (
                        ["lookup"] if operation == "lookup" else ["lookup", operation]
                    )
                    if final:
                        expected = [
                            "lookup",
                            "update" if operation == "update" else "create",
                        ] + expected
                    self.assertEqual(checks.requests, expected)

    def test_malformed_and_transport_failures_are_closed_and_do_not_retry(self):
        for operation in ("lookup", "create", "update"):
            for failure, code in (
                ("malformed", "check_publication_failed"),
                (
                    GitHubHTTPError(SECRET + "403 permission denied"),
                    "check_publication_failed",
                ),
                (GitHubHTTPTransientError(SECRET), "check_temporarily_unavailable"),
            ):
                with self.subTest(operation=operation, failure=type(failure).__name__):
                    # The fake HTTP opener cannot raise already-adapted errors;
                    # patch the transport seam for typed error cases instead.
                    if isinstance(failure, Exception):
                        checks = FailingChecks(operation=operation)
                        original = checks.respond

                        def respond(request):
                            current = {
                                "GET": "lookup",
                                "POST": "create",
                                "PATCH": "update",
                            }[request.method]
                            if current == operation:
                                checks.requests.append(current)
                                raise failure
                            return original(request)

                        checks.respond = respond
                    else:
                        checks = FailingChecks(operation=operation, failure=failure)
                    outcome, calls = self.publish(responses_for_review(), checks=checks)
                    self.assert_unconfirmed(outcome, calls, code)
                    self.assertEqual(
                        checks.requests,
                        ["lookup"] if operation == "lookup" else ["lookup", operation],
                    )

    def test_failure_warning_survives_full_summary_budget_without_losing_findings(self):
        # Force the existing assembled summary to consume its entire budget.
        # The notice must still appear as framing within the same body limit.
        result = replace(
            gate.clean_result(),
            summary="Summary. " * 18,
            limits=ReviewLimits(max_summary_bytes=512),
        )
        with (
            patch(
                "review_sensei_github.publication._published_summary_byte_limit",
                return_value=512,
            ),
            patch(
                "review_sensei_github.publication.render_check_warning",
                wraps=render_check_warning,
            ) as notices,
        ):
            outcome, calls = self.publish(responses_for_review(), result=result)
        # Reserve the concise notice, try both summary notices, then use framing.
        self.assertEqual(notices.call_count, 4)
        self.assert_unconfirmed(outcome, calls, "check_capability_unavailable")
        body = gate.review_payloads(calls)[0]["body"]
        self.assertGreater(
            len(body.split("<!-- reviewsensei:review:v1", 1)[0].encode()), 512
        )
        self.assertLessEqual(
            len(gate.review_payloads(calls)[0]["body"].encode()), 65536
        )

    def test_gate_failure_keeps_blocked_and_incomplete_approval_ineligible(self):
        blocking, facts = gate.admitted_blocking_result(body_comment=True)
        for result, overrides, event in (
            (blocking, {"blocker_candidates": (facts,)}, "REQUEST_CHANGES"),
            (replace(gate.clean_result(), review_status="incomplete"), {}, "COMMENT"),
        ):
            with self.subTest(status=result.review_status):
                outcome, calls = self.publish(
                    responses_for_review(),
                    checks=FailingChecks(operation="create", status=503),
                    result=result,
                    **overrides,
                )
                self.assert_unconfirmed(outcome, calls, "check_temporarily_unavailable")
                self.assertEqual(gate.posted_events(calls), [event])

    def test_legacy_and_new_codes_are_valid_and_render_neutral_warnings(self):
        codes = (
            "check_permission",
            "check_capability_unavailable",
            "check_access_denied",
            "check_temporarily_unavailable",
            "check_publication_failed",
        )
        for code in codes:
            with self.subTest(code=code):
                self.assertIn(code, PUBLIC_DIAGNOSTICS)
                self.assertEqual(sanitize_diagnostic(code), code)
                validate_public_document(
                    RunOutcome("reviewed", diagnostic=code).to_dict(), "run-outcome"
                )
                for concise in (False, True):
                    notice = render_check_warning(code, concise=concise)
                    self.assertIn(code, notice)
                    self.assertNotIn("Grant", notice)
                    self.assertNotIn("branch protection", notice)
        self.assertIn(
            "legacy diagnostic does not establish the cause",
            render_check_permission_warning(),
        )
        self.assertNotIn(SECRET, render_check_warning(SECRET))

    def test_every_failure_has_a_host_warning_including_replayed_reviews(self):
        for code in (
            "check_permission",
            "check_capability_unavailable",
            "check_access_denied",
            "check_temporarily_unavailable",
            "check_publication_failed",
        ):
            for status in ("reviewed", "already_published", "partial"):
                with self.subTest(code=code, status=status):
                    outcome = RunOutcome(status, diagnostic=code)
                    with (
                        patch.dict("os.environ", {}, clear=True),
                        patch("sys.stderr", new_callable=io.StringIO) as stderr,
                    ):
                        emit_host_outcome(outcome)
                    for text in (stderr.getvalue(), render_actions_summary(outcome)):
                        self.assertIn("warning:", text)
                        self.assertIn("publication could not be confirmed", text)
                        self.assertIn(code, text)
                        self.assertNotIn(SECRET, text)

    def test_check_failure_evidence_is_bounded_and_never_contains_exception_text(self):
        for operation in ("lookup", "create", "update"):
            for status in STATUSES:
                with self.subTest(operation=operation, status=status):
                    checks = FailingChecks(operation=operation, status=status)
                    http, _ = gate.make_http(
                        [], routes=((checks.matches, checks.respond),)
                    )
                    try:
                        result = ReviewCheckPublisher(http=http).start(
                            token=SECRET,
                            repository="owner/repo",
                            head_sha=gate.HEAD,
                            app_slug=gate.APP,
                        )
                    except GitHubPublicationError as exc:
                        failure = exc.failure
                        self.assertNotIn(
                            SECRET,
                            str(exc)
                            + repr(exc)
                            + "".join(traceback.format_exception(exc)),
                        )
                    else:
                        failure = result.failure
                        self.assertNotIn(SECRET, repr(result))
                    self.assertEqual(
                        failure, CheckFailure(expected_code(status), operation, status)
                    )
                    with self.assertRaises(FrozenInstanceError):
                        failure.status = 200
        for args in (
            (SECRET, "lookup"),
            ("check_publication_failed", SECRET),
            ("check_publication_failed", "lookup", SECRET),
        ):
            with self.assertRaises(ValueError):
                CheckFailure(*args)
        self.assertNotIn(SECRET, repr(CheckCapability(token=SECRET)))

    def test_success_confirms_the_gate_without_a_warning(self):
        checks = gate.FakeCheckRuns()
        outcome, calls = self.publish(responses_for_review(), checks=checks)
        self.assertIsNone(outcome.diagnostic)
        self.assertEqual(checks.statuses, ["in_progress", "completed"])
        self.assertEqual(gate.posted_events(calls), ["COMMENT", "APPROVE"])
        body = gate.review_payloads(calls)[0]["body"]
        from review_sensei_github.publication import approval_eligibility_from_body

        self.assertTrue(approval_eligibility_from_body(body).facts.check_published)
        self.assertNotIn("merge gate unconfirmed", body)

    def test_missing_capability_has_no_permission_or_transient_inference(self):
        class Broker:
            token = None

            def exchange(self, token, *, capability):
                return self.token

        broker = Broker()
        application = GitHubApplication(
            broker=broker, http=None, reviewer=None, learner=None, replier=None
        )
        for token in (None, "", " "):
            broker.token = token
            self.assertEqual(
                application._check_capability("oidc"),
                CheckCapability(
                    failure=CheckFailure("check_capability_unavailable", "capability")
                ),
            )

    def test_capability_diagnostic_is_local_to_each_application_invocation(self):
        class Broker:
            failure = None
            exchanges = []

            def exchange(self, token, *, capability):
                self.exchanges.append(capability)
                if capability == "check_publish" and self.failure is not None:
                    raise self.failure
                return SECRET if capability == "check_publish" else "review-token"

        broker = Broker()
        application = GitHubApplication(
            broker=broker, http=None, reviewer=None, learner=None, replier=None
        )
        for error, code in (
            (
                GitHubBrokerClientError(SECRET + "429 timeout"),
                "check_capability_unavailable",
            ),
            (GitHubHTTPTransientError(SECRET), "check_temporarily_unavailable"),
        ):
            with self.subTest(error=type(error).__name__):
                broker.failure = error
                failure = application._check_capability("oidc")
                self.assertEqual(failure.failure, CheckFailure(code, "capability"))
                self.assertIsNone(failure.token)
                self.assertNotIn(SECRET, repr(failure))
                broker.failure = None
                success = application._check_capability("oidc")
                self.assertIsNone(success.failure)
                self.assertEqual(success.token, SECRET)
                outcome, calls = self.publish(
                    responses_for_review(), check_capability_failure=failure.failure
                )
                self.assert_unconfirmed(outcome, calls, code)
        for status in STATUSES:
            error = GitHubBrokerClientError(SECRET)
            error.__cause__ = HTTPError(
                SECRET, status, SECRET, {}, io.BytesIO(SECRET.encode())
            )
            broker.failure = error
            result = application._check_capability("oidc")
            error.__cause__.close()
            code = (
                expected_code(status)
                if status in (401, 403, 429, 500, 503, 599)
                else "check_capability_unavailable"
            )
            self.assertEqual(result.failure, CheckFailure(code, "capability", status))
            self.assertNotIn(SECRET, repr(result))

    def test_application_forwards_capability_failure_without_extra_exchanges(self):
        class Broker:
            def __init__(self):
                self.exchanges = []

            def exchange(self, token, *, capability):
                self.exchanges.append(capability)
                if capability == "check_publish":
                    raise GitHubHTTPTransientError(SECRET)
                return "review-token"

        checks = FailingChecks(operation="create")
        http, calls = gate.make_http(
            responses_for_review(), routes=((checks.matches, checks.respond),)
        )
        broker = Broker()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=ReviewPublisher(http=http),
            learner=None,
            replier=None,
        )
        with self.assertNoLogs(level=logging.WARNING):
            outcome = application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="oidc",
                repository="owner/repo",
                repository_id=1,
                pull_request=2,
                head_sha=gate.HEAD,
                base_branch="main",
                base_sha=gate.BASE,
                result=gate.clean_result(),
                diff=gate.DIFF,
                app_slug=gate.APP,
            )
        self.assert_unconfirmed(outcome, calls, "check_temporarily_unavailable")
        self.assertEqual(broker.exchanges, ["review_publish", "check_publish"])
        self.assertEqual(checks.requests, [])
