"""Report incomplete check reconciliation through the public media command."""

import unittest
from types import SimpleNamespace

from review_sensei_github import GitHubApplication, GitHubHttp, GitHubWriteOptions

from review_sensei.models import ReviewResult


class MissingCheckBroker:
    def __init__(self, *, error=False):
        self.error = error
        self.capabilities = []

    def exchange(self, token, *, capability):
        self.capabilities.append(capability)
        if self.error:
            raise RuntimeError("transport exception detail")
        return None


class MediaPublisher:
    def __init__(self, assessment):
        self.assessment = assessment
        self.confirmations = 0

    def approve_reviewed_media(self, **kwargs):
        self.confirmations += 1
        return self.assessment


class MediaCommandReportingTests(unittest.TestCase):
    def test_missing_or_failed_check_capability_is_reported_as_incomplete(self):
        for policy in ("auto-approve", "blocking", "advisory"):
            for error in (False, True):
                with self.subTest(policy=policy, error=error):
                    broker = MissingCheckBroker(error=error)
                    assessment = SimpleNamespace(
                        approval=SimpleNamespace(
                            approved=policy == "auto-approve",
                            blockers=()
                            if policy == "auto-approve"
                            else ("auto-approval-disabled",),
                        ),
                        mixed=SimpleNamespace(approved=True),
                        pending_ids=(),
                    )
                    publisher = MediaPublisher(assessment)
                    calls = []

                    def no_remote_write(request, timeout):
                        calls.append(request.method)
                        raise AssertionError(
                            "missing check capability cannot authorize HTTP"
                        )

                    app = GitHubApplication(
                        broker=broker,
                        http=GitHubHttp(opener=no_remote_write),
                        reviewer=object(),
                        learner=object(),
                        replier=object(),
                    )
                    result = app.apply_maintainer_command(
                        options=GitHubWriteOptions(
                            github_writes=True, reviews_policy=policy
                        ),
                        oidc_token="synthetic-oidc",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=2,
                        head_sha="a" * 40,
                        body="@reviewsensei I reviewed the media files and I approve",
                        actor_login="synthetic-maintainer",
                        actor_permission="maintain",
                        association="OWNER",
                        app_slug="reviewsensei[bot]",
                        source_comment_id=20,
                        request_comment_id=10,
                        media_publisher=publisher,
                        review_result=ReviewResult(
                            summary="synthetic reviewed files",
                            comments=(),
                            provider="fixture",
                        ),
                    )
                    self.assertTrue(
                        result.applied
                    )  # Existing media confirmation is retained.
                    self.assertTrue(result.evaluation_failed)
                    self.assertIn("check capability unavailable", result.summary)
                    self.assertIn("retry media approval", result.summary)
                    self.assertNotEqual(result.summary, "media-approved")
                    self.assertNotIn("transport exception detail", result.summary)
                    self.assertEqual(publisher.confirmations, 1)
                    self.assertEqual(broker.capabilities, ["check_publish"])
                    self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()
