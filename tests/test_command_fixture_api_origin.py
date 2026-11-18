"""Synthetic command callers preserve the same API origin as review callers."""

import tempfile
import unittest

from test_action_run import CommandEntryFixture


class CommandFixtureApiOriginTests(unittest.TestCase):
    def test_source_and_http_share_the_configured_origin_without_inference(self):
        for origin in ("https://api.github.test", "https://api.github.com"):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as root:
                fixture = CommandEntryFixture(root)
                fixture.api_url = origin
                self.assertEqual(
                    fixture.invoke_command("@reviewsensei review pause"),
                    0,
                    (fixture.error, fixture.exception),
                )
                self.assertEqual(fixture.http.api_url, origin)
                self.assertEqual(
                    fixture.source["issue_url"],
                    origin + "/repos/owner/repo/issues/7",
                )
                self.assertEqual(fixture.provider_calls, 0)
                self.assertEqual(
                    len(fixture.accounted.observations), len(fixture.trace)
                )
