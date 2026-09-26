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


def placement_responses(*, required=False):
    """Script the conversation-resolution reads in host-call order.

    A positive requirement is answered by the branch rules read alone; an
    unlisted requirement answers "no rules" and then "no branch protection".
    """

    if required:
        return [
            json_response(
                [
                    {
                        "type": "pull_request",
                        "parameters": {"required_review_thread_resolution": True},
                    }
                ]
            )
        ]
    return [json_response([]), json_response({"message": "Not Found"}, 404)]


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
