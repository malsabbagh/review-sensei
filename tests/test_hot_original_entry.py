import tempfile
import unittest
from urllib.request import Request

from hot_original_entry import OriginalWire, invoke_original
from test_review_run import EntryFixture


class OriginalEntryAdapterTests(unittest.TestCase):
    def test_unknown_url_is_never_forwarded(self):
        wire = OriginalWire(None)
        for url in ("https://example.test/", "http://api.github.com/repos/owner/repo"):
            with self.subTest(url=url), self.assertRaises(AssertionError):
                wire.open(Request(url), timeout=1)

    def test_original_engine_and_owner_complete_empty_review(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            fixture.api_url = "https://api.github.com"
            result, wire = invoke_original(fixture)
            self.assertEqual(result, 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertTrue(wire.requests)
            self.assertIs(fixture.selected.caller, fixture.accounted)
