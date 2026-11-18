"""Only the original bounded, current-root discovery POST is a read."""

import copy
import json
import unittest
from unittest.mock import patch
from urllib.request import Request

from review_sensei_github import thread_resolution
from review_sensei_github.quarantine_census import QUERY, is_readonly_census

from review_sensei.session import SessionIdentity


class ResolutionReadClassificationTests(unittest.TestCase):
    def test_exact_scope_and_closed_source_only(self):
        identity = SessionIdentity("owner/repository", 7, 123)
        value = {
            "operationName": "FindingResolutionThreads",
            "query": thread_resolution._QUERY,
            "variables": {
                "owner": "owner",
                "name": "repository",
                "number": 7,
                "first": 100,
                "after": None,
            },
        }

        def classified(body, *, scope=identity, url="https://api.github.com/graphql"):
            request = Request(url, method="POST", data=json.dumps(body).encode())
            return is_readonly_census(
                request, "https://api.github.com", identity=scope, resolution_only=True
            )

        self.assertTrue(classified(value))
        for key, changed in (
            ("owner", "foreign"),
            ("name", "other"),
            ("number", 8),
            ("number", True),
            ("first", 0),
            ("first", 101),
            ("after", "x" * 2049),
        ):
            candidate = copy.deepcopy(value)
            candidate["variables"][key] = changed
            self.assertFalse(classified(candidate), (key, changed))
        for key, changed in (
            ("operationName", "Other"),
            ("query", value["query"] + "\nmutation Evil { x }"),
            ("query", "mutation ResolveReviewThread { x }"),
            ("extensions", {}),
        ):
            candidate = copy.deepcopy(value)
            candidate[key] = changed
            self.assertFalse(classified(candidate), key)
        self.assertFalse(classified(value, scope=None))
        self.assertFalse(classified(value, url="https://foreign.example/graphql"))
        self.assertFalse(
            classified(value, url="https://api.github.com/graphql?extra=1")
        )
        with patch.object(thread_resolution, "_QUERY", value["query"] + " "):
            self.assertFalse(classified(value))
            self.assertFalse(classified({**value, "query": thread_resolution._QUERY}))
        with patch.object(
            thread_resolution.FindingThreadResolver,
            "_discover_candidates",
            lambda *a, **k: (),
        ):
            self.assertFalse(classified(value))
        self.assertTrue(classified(value))
        # Opted ordinary classification does not broaden the old census lane.
        self.assertFalse(classified({"query": QUERY, "variables": value["variables"]}))
