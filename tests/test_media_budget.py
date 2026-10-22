"""Reserved finalization calls remain part of the original media allowance."""

import io
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.media_budget import MediaCheckFinalizationHttp, media_http
from review_sensei_github.publication_recovery import publication_http

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from tests.fake_github_http import json_response


class MediaBudgetTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.now = 100.0
        self.budget = EvidenceReadBudget(clock=lambda: self.now)

        def transport(request, timeout):
            self.calls.append((request.method, request.full_url, timeout))
            return json_response({})

        self.http = MediaCheckFinalizationHttp(
            GitHubHttp(opener=transport),
            self.budget,
            repository="owner/repo",
            head_sha="a" * 40,
        )

    def test_only_existing_four_reserved_calls_can_follow_ordinary_exhaustion(self):
        for _ in range(60):
            self.budget.consume()
        with self.assertRaises(ReviewInputError):
            self.budget.consume()
        for _ in range(4):
            self.http.request(
                "GET",
                "/repos/owner/repo/commits/" + "a" * 40 + "/check-runs?per_page=100",
                token="synthetic",
            )
        self.assertEqual(self.budget.calls, 64)
        self.assertEqual(len(self.calls), 4)
        with self.assertRaises(ReviewInputError):
            self.http.request("POST", "/repos/owner/repo/check-runs", token="synthetic")
        self.assertEqual(len(self.calls), 4)

    def test_tail_cannot_dispatch_evidence_or_another_target(self):
        for method, path in (
            ("GET", "/repos/owner/repo/pulls/2"),
            ("GET", "/repos/owner/repo/commits/" + "b" * 40 + "/check-runs"),
            ("POST", "/graphql"),
            ("POST", "/repos/other/repo/check-runs"),
            ("PATCH", "/repos/owner/repo/check-runs/0"),
            ("DELETE", "/repos/owner/repo/check-runs/1"),
            ("GET", "https://example.test/repos/owner/repo/check-runs"),
        ):
            with self.subTest(method=method, path=path):
                with self.assertRaises(ReviewInputError):
                    self.http.request(method, path, token="synthetic")
        self.assertEqual(self.budget.calls, 0)
        self.assertFalse(self.calls)

    def test_expired_original_deadline_cannot_use_reserved_calls(self):
        self.now = self.budget.deadline
        with self.assertRaises(ReviewInputError):
            self.http.request(
                "PATCH", "/repos/owner/repo/check-runs/1", token="synthetic"
            )
        self.assertFalse(self.calls)

    def test_media_refuses_a_redirect_before_an_uncharged_second_physical_request(self):
        attempts = []

        class SyntheticHTTPS(HTTPSHandler):
            def https_open(self, request):
                attempts.append(request.full_url)
                redirect = len(attempts) == 1
                headers = Message()
                if redirect:
                    headers["Location"] = (
                        "https://api.github.com/repos/owner/repo/redirect"
                    )
                response = addinfourl(
                    io.BytesIO(b"" if redirect else b'{"ok":true}'),
                    headers,
                    request.full_url,
                    302 if redirect else 200,
                )
                response.msg = "Found" if redirect else "OK"
                return response

        def opened(*handlers):
            return build_opener(SyntheticHTTPS(), *handlers)

        with patch(
            "review_sensei_github.media_budget.build_opener", side_effect=opened
        ):
            ordinary = GitHubHttp()
            controlled = media_http(ordinary)
            self.assertIsNot(controlled, ordinary)
            with self.assertRaisesRegex(
                GitHubPublicationError, "redirects are refused"
            ):
                publication_http(controlled, self.budget).request(
                    "GET", "/repos/owner/repo/original", token="synthetic"
                )
        self.assertEqual(self.budget.calls, 1)
        self.assertEqual(len(attempts), 1)

        # The media boundary does not alter the legacy explicit opener contract.
        attempts.clear()
        legacy = GitHubHttp(opener=build_opener(SyntheticHTTPS()).open)
        self.assertIs(media_http(legacy), legacy)
        legacy.request("GET", "/repos/owner/repo/original", token="synthetic")
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
