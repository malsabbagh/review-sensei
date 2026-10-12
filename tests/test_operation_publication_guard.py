"""Complete bounded operation evidence is required before publication."""

import hashlib
import json
import unittest
from unittest.mock import patch

from review_sensei_github.commands import (
    require_accepted_result as _require_accepted_result,
)
from review_sensei_github.http import MAX_PAGINATION_PAGES
from review_sensei_github.operation_host import OPERATION_COMMENT_MARKER

from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult


class OperationPublicationGuardTests(unittest.TestCase):
    def setUp(self):
        self.result = ReviewResult(
            summary="Synthetic accepted result.", comments=(), provider="fixture"
        )
        self.digest = hashlib.sha256(
            json.dumps(
                self.result.to_dict(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.unrelated = {"user": {"login": "synthetic-user"}, "body": "unrelated"}
        self.full_page = [self.unrelated] * 100

    def operation(self, digest):
        return {
            "user": {"login": "reviewsensei[bot]"},
            "body": f"{OPERATION_COMMENT_MARKER}\n{digest}\n",
        }

    def check(self, pages, *, token=True):
        environment = {"GITHUB_RUN_ID": "synthetic-run"}
        if token:
            environment["GITHUB_TOKEN"] = "synthetic-metadata-token"
        with (
            patch.dict("os.environ", environment, clear=True),
            patch(
                "socket.socket.connect", side_effect=AssertionError("network forbidden")
            ),
            patch(
                "review_sensei_github.http.GitHubHttp.request",
                side_effect=pages,
            ) as request,
        ):
            try:
                _require_accepted_result(
                    self.result,
                    repository="synthetic/repo",
                    pull_request=11,
                    app_slug="reviewsensei[bot]",
                )
            finally:
                self.requests = request.call_args_list

    def test_page_two_mismatching_operation_refuses(self):
        with self.assertRaisesRegex(ReviewInputError, "does not match"):
            self.check([(200, self.full_page), (200, [self.operation("f" * 64)])])
        self.assertEqual(len(self.requests), 2)

    def test_page_two_matching_and_absent_operation_require_complete_scan(self):
        for final in ([self.operation(self.digest)], []):
            self.check([(200, self.full_page), (200, final)])
            self.assertEqual(len(self.requests), 2)
            self.assertTrue(self.requests[1].args[1].endswith("per_page=100&page=2"))

    def test_later_failure_and_malformed_inventory_never_allow_legacy(self):
        for final in ((403, None), (200, {}), (200, self.full_page + [self.unrelated])):
            with self.subTest(final_status=final[0]):
                with self.assertRaisesRegex(
                    ReviewInputError, "inventory is incomplete or unavailable"
                ):
                    self.check([(200, self.full_page), final])
                self.assertEqual(len(self.requests), 2)

    def test_matching_operation_does_not_allow_incomplete_tail(self):
        first = [self.operation(self.digest), *self.full_page[:99]]
        with self.assertRaisesRegex(
            ReviewInputError, "inventory is incomplete or unavailable"
        ):
            self.check([(200, first), (403, None)])
        self.assertEqual(len(self.requests), 2)

    def test_full_page_limit_refuses_incomplete_inventory(self):
        with self.assertRaisesRegex(
            ReviewInputError, "inventory is incomplete or unavailable"
        ):
            self.check([(200, self.full_page)] * MAX_PAGINATION_PAGES)
        self.assertEqual(len(self.requests), MAX_PAGINATION_PAGES)

    def test_missing_metadata_token_refuses_before_http(self):
        with self.assertRaisesRegex(
            ReviewInputError, "operation record is unavailable"
        ):
            self.check([], token=False)
        self.assertEqual(self.requests, [])

    def test_untrusted_author_cannot_supply_accepted_digest(self):
        untrusted = {
            "user": {"login": "synthetic-user"},
            "body": self.operation(self.digest)["body"],
        }
        with self.assertRaisesRegex(ReviewInputError, "does not match"):
            self.check([(200, [untrusted, self.operation("f" * 64)])])


if __name__ == "__main__":
    unittest.main()
