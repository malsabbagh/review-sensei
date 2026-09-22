"""Regressions for omitted-policy behavior at the F7 cutover boundaries."""

import hashlib
import json
import unittest
from unittest.mock import patch

from review_sensei.convergence import (
    REVIEW_MODE_ENV,
    ReviewConvergencePolicy,
    derive_blocker_candidate,
)
from review_sensei.hosting.github import GitHubPublicationError, ReviewPublisher
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.verifier import (
    CandidateFinding,
    EvidenceReference,
    prepare_publishable_review,
)

try:
    from fake_github_http import json_response, make_http
    from test_github_publication import (
        DIFF,
        graphql_review_threads_response,
        pr_payload,
        result,
    )
except ModuleNotFoundError:
    from tests.fake_github_http import json_response, make_http
    from tests.test_github_publication import (
        DIFF,
        graphql_review_threads_response,
        pr_payload,
        result,
    )

HEAD = "b" * 40


def publication_arguments(review):
    return {
        "token": "token",
        "repository": "owner/repo",
        "repository_id": 1,
        "pull_request": 2,
        "head_sha": HEAD,
        "base_branch": "main",
        "base_sha": "a" * 40,
        "result": review,
        "diff": DIFF,
        "app_slug": "reviewsensei[bot]",
        "auto_approve": False,
    }


def publication_responses(*, inline=False):
    responses = [
        json_response(pr_payload(head_sha=HEAD)),
        json_response([]),
        json_response(pr_payload(head_sha=HEAD)),
    ]
    if inline:
        responses.append(graphql_review_threads_response())
    responses.append(json_response({"id": 5}))
    return responses


class CutoverDefaultTests(unittest.TestCase):
    def test_omitted_policy_is_merge_focused_without_ambient_reselection(self):
        for ambient in ("legacy", "merge-focused", "advisory", "strict"):
            with self.subTest(ambient=ambient):
                http, calls = make_http(publication_responses())
                publisher = ReviewPublisher(http=http)
                with patch.dict("os.environ", {REVIEW_MODE_ENV: ambient}):
                    prepared = publisher.prepare(
                        result=result(), diff=DIFF, head_sha=HEAD
                    )
                    outcome = publisher.publish(
                        **publication_arguments(prepared.result),
                        prepared_review=prepared,
                    )
                self.assertEqual(
                    prepared.convergence_policy_digest,
                    ReviewConvergencePolicy(mode="merge-focused").digest(),
                )
                self.assertFalse(prepared.result.comments[0].blocks_approval)
                self.assertEqual(outcome.status, "published")
                body = json.loads(calls[-1][2].decode("utf-8"))
                self.assertEqual(body["event"], "COMMENT")
                self.assertEqual(body["comments"], [])
                self.assertIn("## Advisory observations", body["body"])
                self.assertEqual(len(calls), 4)

    def test_legacy_prepared_artifact_cannot_enter_the_default_publication_path(self):
        http, calls = make_http([])
        publisher = ReviewPublisher(http=http)
        prepared = publisher.prepare(
            result=result(),
            diff=DIFF,
            head_sha=HEAD,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
        )
        with self.assertRaisesRegex(GitHubPublicationError, "publication context"):
            publisher.publish(
                **publication_arguments(prepared.result), prepared_review=prepared
            )
        self.assertEqual(calls, [])

    def test_default_policy_preserves_approval_opt_out_for_an_admitted_blocker(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Restore the authorization check before reading tenant data.",
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
        review = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        http, calls = make_http(publication_responses(inline=True))
        outcome = ReviewPublisher(http=http).publish(
            **publication_arguments(review), blocker_candidates=(facts,)
        )
        self.assertEqual(outcome.status, "published")
        writes = [
            json.loads(body.decode("utf-8"))
            for method, url, body in calls
            if method == "POST" and url.endswith("/pulls/2/reviews")
        ]
        self.assertEqual([body["event"] for body in writes], ["COMMENT"])
        self.assertEqual(len(writes[0]["comments"]), 1)
        self.assertIn("blocking=true", writes[0]["comments"][0]["body"])
        self.assertEqual(len(calls), 5)

    def test_default_policy_admitted_blocker_forces_request_changes(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Restore the authorization check before reading tenant data.",
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
        review = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        http, calls = make_http(publication_responses(inline=True))
        outcome = ReviewPublisher(http=http).publish(
            **{**publication_arguments(review), "auto_approve": True},
            blocker_candidates=(facts,),
        )
        self.assertEqual(outcome.status, "published")
        writes = [
            json.loads(body.decode("utf-8"))
            for method, url, body in calls
            if method == "POST" and url.endswith("/pulls/2/reviews")
        ]
        self.assertEqual([body["event"] for body in writes], ["REQUEST_CHANGES"])
        self.assertEqual(len(writes[0]["comments"]), 1)
        self.assertIn("blocking=true", writes[0]["comments"][0]["body"])

    def test_verifier_omitted_policy_matches_explicit_merge_focused(self):
        snapshot = {"src/app.py": "keep\nchange\n"}
        digest = hashlib.sha256(
            json.dumps(
                snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        candidate = CandidateFinding(
            "The new line needs a bound.",
            "Call the changed helper with empty input.",
            "src/app.py",
            (EvidenceReference("src/app.py", 2, digest, "change"),),
            "Retain only findings supported by the supplied snapshot.",
        )
        for evidence_policy in ("legacy", "confirmed"):
            with self.subTest(evidence_policy=evidence_policy):
                arguments = {
                    "evidence_policy": evidence_policy,
                    "changed_lines": {"src/app.py": frozenset({2})},
                    "current_head_sha": HEAD,
                }
                if evidence_policy == "confirmed":
                    arguments.update(
                        candidates=(candidate,),
                        snapshot=snapshot,
                        snapshot_sha256=digest,
                    )
                implicit = prepare_publishable_review(result(), **arguments)
                explicit = prepare_publishable_review(
                    result(),
                    **arguments,
                    convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
                )
                self.assertEqual(implicit, explicit)
                self.assertEqual(
                    implicit.convergence_policy_digest,
                    ReviewConvergencePolicy(mode="merge-focused").digest(),
                )
                self.assertTrue(implicit.result.comments)
                self.assertTrue(
                    all(not item.blocks_approval for item in implicit.result.comments)
                )


if __name__ == "__main__":
    unittest.main()
