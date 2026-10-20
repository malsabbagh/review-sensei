import io
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from review_sensei_github import (
    GitHubHttp,
    GitHubHTTPError,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPResponseTooLargeError,
    GitHubHTTPTransientError,
)
from review_sensei_github.http import MAX_GITHUB_RESPONSE_BYTES


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reason = "reason"
        self.headers = {}


class GitHubHttpTests(unittest.TestCase):
    def test_adaptive_pagination_obeys_request_budget_independently(self):
        http, calls = self.make_http((200, b"[1]"))
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            http.paginate(path="/files", token="t", page_sizes=(1,), max_requests=1)
        self.assertEqual(len(calls), 1)

    def test_adaptive_pagination_obeys_deadline_before_second_request(self):
        clock = [0.0]
        timeouts = []

        def opener(request, timeout):
            timeouts.append(timeout)
            clock[0] = 1.0
            return FakeResponse(b"[1]", 200)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            http.paginate(
                path="/files",
                token="t",
                page_sizes=(1,),
                max_requests=10,
                timeout_seconds=0.5,
                monotonic=lambda: clock[0],
            )
        self.assertEqual(timeouts, [0.5])

    def make_http(self, responses, **kwargs):
        calls = []

        def opener(request, timeout):
            calls.append((request.method, request.full_url, request.headers))
            if isinstance(responses, list):
                response = responses.pop(0)
            else:
                response = responses
            if isinstance(response, Exception):
                raise response
            status, body = response
            return FakeResponse(body, status)

        return GitHubHttp(
            api_url="https://api.github.test", opener=opener, **kwargs
        ), calls

    def test_request_json_and_headers(self):
        http, calls = self.make_http((200, b'{"ok":true}'))
        status, body = http.request(
            "GET",
            "/repos/owner/repo",
            token="ghs_opaque",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][1], "https://api.github.test/repos/owner/repo")
        self.assertEqual(calls[0][2]["Authorization"], "Bearer ghs_opaque")

    def test_repository_path_quotes_owner_and_name(self):
        http, _ = self.make_http((200, b"{}"))
        path = http.repository_path("owner/repo", "/pulls/42")
        self.assertEqual(path, "/repos/owner/repo/pulls/42")

    def test_rejects_invalid_owner_or_repo(self):
        http, _ = self.make_http((200, b"{}"))
        with self.assertRaises(GitHubHTTPError):
            http.repository_path("../outside/repo", "")
        with self.assertRaises(GitHubHTTPError):
            http.repository_path("owner/../repo", "")

    def test_http_error_status_is_returned_without_exposing_body(self):
        err = HTTPError(
            "https://api.github.test/repos/owner/repo",
            500,
            "error",
            {},
            FakeResponse(b'{"message":"secret"}', 500),
        )
        http, _ = self.make_http(err)
        status, body = http.request("GET", "/repos/owner/repo", token="t")
        self.assertEqual(status, 500)
        self.assertIsNone(body)
        self.assertNotIn("secret", str(body))
        self.assertNotIn("ghs_opaque", str(body))

    def test_urlerror_transient(self):
        http, _ = self.make_http(URLError("dns"))
        with self.assertRaises(GitHubHTTPTransientError):
            http.request("GET", "/repos/owner/repo", token="t")

    def test_invalid_utf8_json_fails_closed(self):
        http, _ = self.make_http((200, b"\xff"))
        with self.assertRaises(GitHubHTTPError):
            http.request("GET", "/repos/owner/repo", token="t")

    def test_oversized_response_uses_specific_error(self):
        http, _ = self.make_http((200, b"x" * (MAX_GITHUB_RESPONSE_BYTES + 1)))

        with self.assertRaises(GitHubHTTPResponseTooLargeError):
            http.request("GET", "/repos/owner/repo", token="t")

    def test_paginate_fetches_bounded_pages(self):
        page = b"[" + b'{"id":1},' * 99 + b'{"id":1}]'
        http, calls = self.make_http([(200, page), (200, b"[]")])
        values = http.paginate(path="/repos/owner/repo/issues", token="t")
        self.assertEqual(len(values), 100)
        self.assertIn("per_page=100&page=1", calls[0][1])
        self.assertIn("per_page=100&page=2", calls[1][1])

    def test_paginate_refuses_more_than_configured_pages(self):
        page = b"[" + b'{"id":1},' * 99 + b'{"id":1}]'
        http, calls = self.make_http([(200, page)] * 10)
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            http.paginate(path="/repos/owner/repo/issues", token="t")
        self.assertEqual(len(calls), 10)

    def test_paginate_retries_an_oversized_page_with_smaller_requests(self):
        calls = []

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            page_size = int(query["per_page"][0])
            page = int(query["page"][0])
            calls.append((page_size, page))
            if page_size > 10 and page == 1:
                return FakeResponse(b"x" * (MAX_GITHUB_RESPONSE_BYTES + 1))
            start = (page - 1) * page_size
            body = json.dumps(
                [{"id": value} for value in range(start, min(start + page_size, 25))]
            ).encode("utf-8")
            return FakeResponse(body)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)

        values = http.paginate(
            path="/repos/owner/repo/pulls/1/comments",
            token="t",
            page_sizes=(20, 10, 5, 1),
        )

        self.assertEqual([value["id"] for value in values], list(range(25)))
        self.assertEqual(calls, [(20, 1), (10, 1), (10, 2), (20, 2)])

    def test_paginate_recomputes_the_page_after_a_later_size_fallback(self):
        calls = []

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            page_size = int(query["per_page"][0])
            page = int(query["page"][0])
            calls.append((page_size, page))
            if page_size == 20 or (page_size == 10 and page == 2):
                return FakeResponse(b"x" * (MAX_GITHUB_RESPONSE_BYTES + 1))
            start = (page - 1) * page_size
            body = json.dumps(
                [{"id": value} for value in range(start, min(start + page_size, 23))]
            ).encode("utf-8")
            return FakeResponse(body)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)

        values = http.paginate(
            path="/repos/owner/repo/pulls/1/comments",
            token="t",
            page_sizes=(20, 10, 5, 1),
        )

        self.assertEqual([value["id"] for value in values], list(range(23)))
        self.assertEqual(
            calls,
            [(20, 1), (10, 1), (10, 2), (5, 3), (5, 4), (20, 2), (10, 3)],
        )

    def test_paginate_resumes_after_a_full_larger_page(self):
        calls = []

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            page_size = int(query["per_page"][0])
            page = int(query["page"][0])
            calls.append((page_size, page))
            if page_size == 20 and page == 2:
                return FakeResponse(b"x" * (MAX_GITHUB_RESPONSE_BYTES + 1))
            start = (page - 1) * page_size
            body = json.dumps(
                [{"id": value} for value in range(start, min(start + page_size, 25))]
            ).encode("utf-8")
            return FakeResponse(body)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)

        values = http.paginate(
            path="/repos/owner/repo/pulls/1/comments",
            token="t",
            page_sizes=(20, 10, 5, 1),
        )

        self.assertEqual([value["id"] for value in values], list(range(25)))
        self.assertEqual(calls, [(20, 1), (20, 2), (10, 3)])


class SharedReviewReadbackFixtureTests(unittest.TestCase):
    def test_seeded_reviews_appear_once_in_bounded_multi_page_listing(self):
        from tests.fake_github_http import json_response, make_http

        http, calls = make_http(
            [
                json_response([{"id": 2}]),
                json_response([{"id": 3}, {"id": 4}]),
                json_response([]),
            ],
            review_readback=True,
            initial_reviews=[{"id": 1}],
        )
        reviews = http.paginate(
            path="/repos/owner/repo/pulls/2/reviews",
            token="synthetic",
            page_sizes=(2,),
            max_requests=3,
            expected_items=4,
        )
        self.assertEqual(reviews, [{"id": value} for value in (1, 2, 3, 4)])
        self.assertEqual(len({review["id"] for review in reviews}), len(reviews))
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["page"] for _, url, _ in calls],
            [["1"], ["2"], ["3"]],
        )

    def test_independent_listing_still_sees_seeded_first_page(self):
        from tests.fake_github_http import json_response, make_http

        http, _ = make_http(
            [json_response([]), json_response([])],
            review_readback=True,
            initial_reviews=[{"id": 1}],
        )
        for _ in range(2):
            self.assertEqual(
                http.paginate(
                    path="/repos/owner/repo/pulls/2/reviews",
                    token="synthetic",
                    page_sizes=(2,),
                    max_requests=1,
                ),
                [{"id": 1}],
            )

    def test_explicit_missing_readback_never_injects_seeded_or_posted_rows(self):
        from tests.fake_github_http import json_response, make_http

        http, _ = make_http(
            [json_response({"id": 2}), json_response([])],
            review_readback=False,
            initial_reviews=[{"id": 1}],
        )
        http.request(
            "POST",
            "/repos/owner/repo/pulls/2/reviews",
            token="synthetic",
            body={
                "body": "synthetic review",
                "commit_id": "b" * 40,
                "event": "COMMENT",
            },
        )
        self.assertEqual(
            http.paginate(
                path="/repos/owner/repo/pulls/2/reviews",
                token="synthetic",
                page_sizes=(2,),
                max_requests=1,
            ),
            [],
        )

    def test_app_owned_eligibility_loader_reads_complete_seeded_history(self):
        from review_sensei_github.publication import (
            ReviewApprovalFinalizer,
            approval_eligibility_marker,
        )

        from tests.fake_github_http import json_response, make_http
        from tests.test_github_publication import eligibility_document, published_review

        head = "b" * 40
        eligibility = eligibility_document(head_sha=head)
        seeded = published_review(
            marker=approval_eligibility_marker(eligibility),
            head_sha=head,
            state="COMMENTED",
        )
        for readback in (True, False):
            with self.subTest(readback=readback):
                # Both listings fill two 100-row pages and require a terminal
                # read. Only the opt-in fixture supplies the App-owned record.
                first_count = 99 if readback else 100
                http, calls = make_http(
                    [
                        json_response([{"id": value} for value in range(first_count)]),
                        json_response([{"id": value} for value in range(100, 200)]),
                        json_response([]),
                    ],
                    review_readback=readback,
                    initial_reviews=[seeded],
                )
                loaded = ReviewApprovalFinalizer(http=http).load_eligibility(
                    token="synthetic",
                    repository="owner/repo",
                    pull_request=2,
                    head_sha=head,
                    app_slug="reviewsensei[bot]",
                )
                self.assertEqual(loaded, eligibility if readback else None)
                self.assertEqual(
                    [parse_qs(urlsplit(url).query)["page"] for _, url, _ in calls],
                    [["1"], ["2"], ["3"]],
                )


if __name__ == "__main__":
    unittest.main()
