"""Shared fake GitHub HTTP transport for issue-64 publisher tests."""

from __future__ import annotations

import json

from review_sensei.hosting.github import GitHubHttp


class FakeHTTPResponse:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status
        self.reason = "reason"
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size):
        return self.body[:size]


def json_response(value, status=200):
    return FakeHTTPResponse(json.dumps(value).encode("utf-8"), status)


def make_http(responses):
    calls = []

    def opener(request, timeout):
        calls.append((request.method, request.full_url, request.data))
        if isinstance(responses, list):
            response = responses.pop(0)
        else:
            response = responses
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return FakeHTTPResponse(response[0], response[1])
        return response

    return GitHubHttp(api_url="https://api.github.test", opener=opener), calls
