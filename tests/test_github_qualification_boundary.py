"""Offline #115 acceptance: provider status strings are never evidence.

External GitHub HTTP is captured; no model calls or operator evidence is used.
These tests deliberately do not qualify an OpenRouter deployment.
"""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from review_sensei_github import GitHubApplication, GitHubWriteOptions, ReviewPublisher
from review_sensei_github.approval import (
    approval_facts_from_result,
    qualification_state_for_configuration,
    qualification_state_for_publication,
)
from review_sensei_github.publication import approval_eligibility_from_body

from review_sensei.openrouter_qualification import (
    INITIAL_OPENROUTER_QUALIFICATION_TARGET as TARGET,
)
from review_sensei.outcomes import RecoveryArtifact
from review_sensei.providers.profiles import PROVIDER_PROFILES
from tests import test_github_publication as publication_tests
from tests.test_github_application import RecordingBroker
from tests.test_github_publication import (
    DIFF,
    FakeCheckRuns,
    classified_result,
    clean_result,
    graphql_review_threads_response,
    json_response,
    make_http,
    posted_events,
    pr_payload,
    published_review,
    review_payloads,
)


class QualificationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.publisher = publication_tests.ReviewPublisherTests()
        self.result = replace(clean_result(), provider="openrouter", model=TARGET.model)
        self.head = "b" * 40

    def test_profile_or_configuration_status_cannot_claim_checked_evidence(self):
        identity = {
            "profile": "openrouter-sonnet",
            "base_url": TARGET.base_url,
            "openrouter_policy": dict(TARGET.routing_policy().identity_fields()),
        }
        future_profile = replace(
            PROVIDER_PROFILES["openrouter-sonnet"],
            model=TARGET.model,
            qualification_status="qualified",
        )
        with patch.dict(PROVIDER_PROFILES, {"openrouter-sonnet": future_profile}):
            self.assertEqual(
                qualification_state_for_publication(
                    provider="openrouter",
                    model=TARGET.model,
                    provider_identity=identity,
                ),
                "missing",
            )
        self.assertEqual(
            qualification_state_for_configuration(
                provider="openrouter",
                model=TARGET.model,
                upstream_provider=TARGET.upstream_provider,
                base_url=TARGET.base_url,
                qualification_status="qualified",
            ),
            "missing",
        )

    def test_result_facts_cannot_mint_qualification_from_caller_strings(self):
        for claim in ("not-required", "qualified", "missing", "unverified"):
            with self.subTest(claim=claim):
                facts = approval_facts_from_result(
                    self.result, enabled=True, app_authored=False, qualification=claim
                )
                self.assertIn(facts.qualification, {"missing", "unverified"})

    def test_public_publisher_withholds_omitted_and_asserted_qualification(self):
        for claim in (None, "not-required", "qualified", "missing", "unverified"):
            with self.subTest(claim=claim):
                checks = FakeCheckRuns()
                arguments = {} if claim is None else {"qualification": claim}
                outcome, calls = self.publisher.publish(
                    [
                        json_response(pr_payload(head_sha=self.head)),
                        json_response([]),
                        json_response(pr_payload(head_sha=self.head)),
                        json_response({"id": 5}, 200),
                        json_response(pr_payload(head_sha=self.head)),
                        graphql_review_threads_response(),
                        json_response(pr_payload(head_sha=self.head)),
                        json_response([]),
                        json_response({"id": 6}, 200),
                    ],
                    result=self.result,
                    checks=checks,
                    **arguments,
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(posted_events(calls), ["COMMENT"])
                self.assertEqual(outcome.diagnostic, "qualification_unverified")
                self.assertEqual(checks.conclusions, [None, "success"])
                body = review_payloads(calls)[0]["body"]
                eligibility = approval_eligibility_from_body(body)
                self.assertIsNotNone(eligibility)
                self.assertIn(
                    eligibility.facts.qualification, {"missing", "unverified"}
                )

                # Separate finalization must keep the persisted withholding even
                # after all threads resolve. Static withholding needs no HTTP.
                final, final_calls = self.publisher.finalize(
                    [], eligibility=eligibility, require_persisted=True
                )
                self.assertEqual(final.status, "approval_withheld")
                self.assertEqual(final.diagnostic, "qualification_unverified")
                self.assertEqual(final_calls, [])

                # Retrying the same published result cannot re-enable approval
                # by changing the caller's qualification string.
                replay, replay_calls = self.publisher.publish(
                    [
                        json_response(pr_payload(head_sha=self.head)),
                        json_response(
                            [
                                published_review(
                                    marker=body, head_sha=self.head, state="COMMENTED"
                                )
                            ]
                        ),
                    ],
                    result=self.result,
                    checks=checks,
                    qualification="qualified",
                )
                self.assertEqual(replay.status, "already_published")
                self.assertEqual(replay.diagnostic, "qualification_unverified")
                self.assertEqual(posted_events(replay_calls), [])

    def test_application_and_recovery_withhold_without_report_binding(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                checks = FakeCheckRuns()
                http, calls = make_http(
                    [
                        json_response(pr_payload(head_sha=self.head)),
                        json_response([]),
                        json_response(pr_payload(head_sha=self.head)),
                        json_response({"id": 5}, 200),
                    ],
                    routes=((checks.matches, checks.respond),),
                )
                app = GitHubApplication(
                    broker=RecordingBroker(),
                    http=http,
                    reviewer=ReviewPublisher(http=http),
                    learner=None,
                    replier=None,
                )
                arguments = {
                    "options": GitHubWriteOptions(github_writes=True, auto_review=True),
                    "oidc_token": "oidc",
                    "repository": "owner/repo",
                    "repository_id": 1,
                    "pull_request": 2,
                    "head_sha": self.head,
                    "base_branch": "main",
                    "base_sha": "a" * 40,
                    "diff": DIFF,
                    "app_slug": "reviewsensei[bot]",
                }
                if recovery:
                    artifact = RecoveryArtifact.create(
                        repository="owner/repo",
                        pull_request_number=2,
                        base_sha="a" * 40,
                        head_sha=self.head,
                        result=self.result.to_dict(),
                        expires_at=(
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat(),
                    )
                    outcome = app.recover_review(**arguments, artifact=artifact)
                else:
                    outcome = app.publish_review(
                        **arguments,
                        result=self.result,
                        configuration_context={
                            "provider": {
                                "profile": "openrouter-sonnet",
                                "qualification_status": "qualified",
                                "base_url": TARGET.base_url,
                                "openrouter_policy": dict(
                                    TARGET.routing_policy().identity_fields()
                                ),
                            }
                        },
                    )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(outcome.diagnostic, "qualification_unverified")
                self.assertEqual(posted_events(calls), ["COMMENT"])

    def test_qualification_withholding_preserves_changes_and_approval_opt_out(self):
        for enabled, event in ((True, "REQUEST_CHANGES"), (False, "COMMENT")):
            with self.subTest(enabled=enabled):
                checks = FakeCheckRuns()
                outcome, calls = self.publisher.publish(
                    [
                        json_response(pr_payload(head_sha=self.head)),
                        json_response([]),
                        json_response(pr_payload(head_sha=self.head)),
                        graphql_review_threads_response(),
                        json_response({"id": 5}, 200),
                    ],
                    checks=checks,
                    result=replace(classified_result(), provider="openrouter"),
                    auto_approve=enabled,
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(posted_events(calls), [event])
                self.assertEqual(checks.conclusions, [None, "action_required"])


if __name__ == "__main__":
    unittest.main()
