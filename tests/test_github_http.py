import io
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from review_sensei.hosting.github import (
    GitHubHttp,
    GitHubHTTPError,
    GitHubHTTPResponseTooLargeError,
    GitHubHTTPTransientError,
)
from review_sensei.hosting.github.http import MAX_GITHUB_RESPONSE_BYTES


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reason = "reason"
        self.headers = {}


class GitHubHttpTests(unittest.TestCase):
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
        with self.assertRaises(GitHubHTTPError):
            http.paginate(path="/repos/owner/repo/issues", token="t")
        self.assertEqual(len(calls), 10)

    def test_paginate_retries_an_oversized_page_with_smaller_requests(self):
        calls = []

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            page_size = int(query["per_page"][0])
            page = int(query["page"][0])
            calls.append((page_size, page))
            if page_size > 10:
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
        self.assertEqual(calls, [(20, 1), (10, 1), (10, 2), (10, 3)])

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
            [(20, 1), (10, 1), (10, 2), (5, 3), (5, 4), (5, 5)],
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


if __name__ == "__main__":
    unittest.main()
