"""Shared fake GitHub HTTP transport for issue-64 publisher tests."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from review_sensei_github import GitHubHttp


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


def make_http(responses, routes=(), *, review_readback=False, initial_reviews=()):
    """Build a fake transport from a positional queue and optional routes.

    ``routes`` holds ``(matches, respond)`` pairs consulted before the queue,
    where ``matches`` receives the request and returns whether the route owns
    it and ``respond`` returns that route's response. A route lets a test answer
    URL-shaped infrastructure (such as the check-run gate) without threading its
    responses through every ordered list it drives. Seeded rows prefix only
    the first review-list page; later scripted pages remain authoritative.
    """

    calls = []
    initial_reviews = list(initial_reviews)
    stored_reviews = list(initial_reviews)
    route_handlers = tuple(routes)

    def opener(request, timeout):
        calls.append((request.method, request.full_url, request.data))
        for matches, respond in route_handlers:
            if matches(request):
                return respond(request)
        if isinstance(responses, list):
            response = responses.pop(0)
        else:
            response = responses
        # Opt-in server-state fixture: successful review POSTs become visible
        # to later empty review-list reads, as on GitHub. Explicit nonempty
        # scripted pages and errors remain authoritative; default stays off
        # for tests of missing, stale or conflicting readback.
        target = urlsplit(request.full_url)
        is_reviews = target.path.endswith("/reviews")
        first_review_page = parse_qs(target.query).get("page", ["1"]) == ["1"]
        if review_readback and is_reviews and isinstance(response, FakeHTTPResponse):
            if request.method == "POST" and 200 <= response.status < 300:
                payload = json.loads(request.data)
                stored_reviews.append(
                    {
                        "body": payload.get("body", ""),
                        "commit_id": payload.get("commit_id"),
                        "state": {
                            "COMMENT": "COMMENTED",
                            "APPROVE": "APPROVED",
                            "REQUEST_CHANGES": "CHANGES_REQUESTED",
                        }.get(payload.get("event")),
                        "user": {"login": "reviewsensei[bot]"},
                    }
                )
            elif (
                request.method == "GET"
                and response.status == 200
                and first_review_page
                and initial_reviews
            ):
                payload = json.loads(response.body)
                if isinstance(payload, list):
                    response = json_response(initial_reviews + payload)
            elif (
                request.method == "GET"
                and response.status == 200
                and first_review_page
                and response.body == b"[]"
            ):
                response = json_response(stored_reviews)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return FakeHTTPResponse(response[0], response[1])
        return response

    return GitHubHttp(api_url="https://api.github.test", opener=opener), calls
