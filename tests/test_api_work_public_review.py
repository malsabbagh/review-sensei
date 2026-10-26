"""One public hosted analysis path with real ledger and caller enforcement."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from fake_github_http import json_response
from test_api_work_persistence import IDENTITY, StartupFixture

from review_sensei.errors import ReviewInputError


class HostedRunFixture(StartupFixture):
    def __init__(self, directory, **kwargs):
        super().__init__(directory, **kwargs)
        self.extra_comments = {}

    def _external_open(self, request, timeout):
        target = urlsplit(request.full_url)
        path, method = target.path, request.method
        payload = json.loads(request.data) if request.data else None
        listing = path.endswith("/issues/7/comments")
        identifier = (
            int(path.rsplit("/", 1)[1]) if "/issues/comments/" in path else None
        )
        extra_write = (
            (listing or identifier is not None)
            and payload
            and "reviewsensei:session:" not in payload.get("body", "")
        )
        user = path == "/user" or path.startswith("/users/")
        if (
            listing
            and method == "GET"
            or extra_write
            or identifier in self.extra_comments
            or user
        ):
            assert (
                len(self.accounted.observations)
                == sum(a == self.attempt for a, _, _ in self.trace) + 1
            )
            self.trace.append((self.attempt, method, path))
            bot = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
            if user:
                return json_response(bot)
            if listing and method == "GET":
                query = parse_qs(target.query)
                size, page = (
                    int(query.get("per_page", [100])[0]),
                    int(query.get("page", [1])[0]),
                )
                rows = ([self.comment] if self.comment else []) + list(
                    self.extra_comments.values()
                )
                return json_response(
                    copy.deepcopy(rows[(page - 1) * size : page * size])
                )
            if method in {"POST", "PATCH"}:
                identifier = identifier or 2_000_000 + len(self.extra_comments)
                item = {
                    "id": identifier,
                    "body": payload["body"],
                    "user": bot,
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                }
                self.extra_comments[identifier] = item
                return json_response(
                    copy.deepcopy(item), 201 if method == "POST" else 200
                )
            return json_response(copy.deepcopy(self.extra_comments[identifier]))
        response = super()._external_open(request, timeout)
        if self.comment is not None:
            self.comment["issue_url"] = (
                "https://api.github.test/repos/owner/repo/issues/7"
            )
        if path.endswith("/pulls/7"):
            pr = json.loads(response.read(524288))
            pr.update(state="open", draft=False)
            pr["base"]["ref"] = "main"
            return json_response(pr)
        return response


class PublicCallerRunTests(unittest.TestCase):
    def test_128_refuses_complete_initial_successor_and_terminal_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HostedRunFixture(directory)
            native, config, _evidence, owner = fixture.prelude()
            before = len(fixture.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "operation allocation is unfunded"
            ):
                fixture.accounted.initialize_and_activate(
                    native, IDENTITY, config, owner
                )
            self.assertEqual(fixture.accounted.usable, 0)
            self.assertIsNone(fixture.comment)
            self.assertTrue(
                all(method == "GET" for _, method, _ in fixture.trace[before:])
            )
            self.assertEqual(config.limit, 128)

    def test_comment_write_keeps_its_bounded_confirmation_tail(self):
        from urllib.request import Request

        from test_api_work_persistence import QUALIFICATION_LIMIT

        for remaining in (2, 3):
            with (
                self.subTest(remaining=remaining),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = HostedRunFixture(directory, limit=QUALIFICATION_LIMIT)
                fixture.initial()
                fixture.recover(reserve_successor=True)
                for _ in range(fixture.accounted.usable - remaining):
                    fixture.http.request(
                        "GET", "/repos/owner/repo/pulls/7", token="synthetic"
                    )
                before = len(fixture.trace)
                request = Request(
                    "https://api.github.test/repos/owner/repo/issues/7/comments",
                    data=b'{"body":"synthetic carrier"}',
                    method="POST",
                )
                if remaining == 2:
                    with self.assertRaisesRegex(
                        ReviewInputError, "business write/readback tail"
                    ):
                        fixture.accounted.open(request, timeout=30)
                    self.assertEqual(len(fixture.trace), before)
                else:
                    with fixture.accounted.open(request, timeout=30) as response:
                        response.read(524288)
                    self.assertEqual(len(fixture.trace), before + 1)
                    self.assertEqual(fixture.accounted.usable, 2)
