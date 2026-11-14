"""Pure capacity request shapes use the actual native producer fields."""

import unittest
from types import SimpleNamespace

from review_sensei_github.checks import CheckOutcome
from review_sensei_github.http import GitHubHttp
from review_sensei_github.ordinary_publication_plan import request_branches
from review_sensei_github.publication import ReviewPublisher

from review_sensei.models import ReviewComment


class OrdinaryPublicationRequestShapesTests(unittest.TestCase):
    def test_actual_inline_payload_and_request_changes_survive_every_prefix(self):
        publisher = ReviewPublisher(http=GitHubHttp())
        journal = SimpleNamespace(
            reviewer=publisher,
            identity=SimpleNamespace(repository="owner/repo", pull_request=7),
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
        )
        inline = publisher._inline_comment_payload(
            ReviewComment("src/app.py", 12, "original criterion"),
            "rendered criterion",
            "left",
        )
        branches = request_branches(
            publisher,
            journal,
            frame="Actual bounded root frame",
            comments=(inline,),
            outcome=CheckOutcome("failure", "Required fixes", "One required fix"),
            failure_outcomes=(
                CheckOutcome("failure", "Publication failed", "Review not confirmed"),
            ),
            approval_body=None,
            review_event="REQUEST_CHANGES",
        )
        found = []
        for branch in branches:
            for method, path, body, _ in branch:
                if path.endswith("/reviews"):
                    found.append(body)
                    self.assertEqual(method, "POST")
                    self.assertEqual(
                        body,
                        {
                            "body": "Actual bounded root frame",
                            "event": "REQUEST_CHANGES",
                            "commit_id": "b" * 40,
                            "comments": [inline],
                        },
                    )
        self.assertTrue(found)
        self.assertEqual(inline["body"], "rendered criterion")
        self.assertEqual(inline["side"], "LEFT")
