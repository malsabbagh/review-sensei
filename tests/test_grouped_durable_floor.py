"""Original grouped publication refuses an unfunded tail before check effects."""

import copy
import io
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from hot_original_entry import invoke_original
from review_sensei_github.api_run_budget import CallerRunBudget
from review_sensei_github.guarded_publication_plan import require_grouped_durable_floor
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import NativePublishedTailJourneyFixture


class GroupedDurableFloorTests(unittest.TestCase):
    def test_original_512_grouped_route_holds_before_first_native_effect(self):
        self._original_route(checks_enabled=True)

    def test_denied_checks_still_hold_before_first_grouped_native_effect(self):
        self._original_route(checks_enabled=False)

    def _original_route(self, *, checks_enabled):
        quotes, native_writes = [], []

        def observe(frame, event, value):
            if event != "call":
                return
            if frame.f_code is CallerRunBudget.preflight.__code__:
                parent = frame.f_back
                if (
                    parent is not None
                    and parent.f_code is require_grouped_durable_floor.__code__
                ):
                    quotes.append(frame.f_locals["calls"])
            if frame.f_code is NativePublishedTailJourneyFixture.external.__code__:
                request = frame.f_locals["request"]
                path = urlsplit(request.full_url).path
                if request.get_method() in {"POST", "PATCH", "PUT", "DELETE"} and (
                    path.endswith("/reviews") or "/check-runs" in path
                ):
                    native_writes.append((request.get_method(), path))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = NativePublishedTailJourneyFixture(directory)
            fixture.api_url = "https://api.github.com"
            old, registration = fixture.original_origins()
            old_document = copy.deepcopy(old.api_run_accounting)
            old_reviews = copy.deepcopy(fixture.reviews)
            old_checks = copy.deepcopy(fixture.check_runs)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Current published assessment 0 requires independent human judgment.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            if not checks_enabled:
                original_broker = fixture.broker_response
                original_provider_calls = fixture.provider_calls

                def deny_check(payload):
                    if (
                        payload.get("capability") == "check_publish"
                        and fixture.provider_calls > original_provider_calls
                    ):
                        raise HTTPError(
                            "https://github.reviewsensei.dev/github/token",
                            403,
                            "synthetic denied",
                            {},
                            io.BytesIO(b"{}"),
                        )
                    return original_broker(payload)

                fixture.broker_response = deny_check
            with registered_resource([registration]):
                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    result, _wire = invoke_original(fixture)
                finally:
                    sys.setprofile(previous)
            self.assertEqual(result, 1, fixture.error)
            self.assertIn("cannot fund the complete operation", fixture.error)
            # Reservation/transaction fusion frees seven prefix calls, not
            # these mandatory fresh claim and native-reconciliation reads.
            self.assertEqual(quotes, [99 if checks_enabled else 65])
            self.assertEqual(native_writes, [])
            self.assertEqual(fixture.reviews, old_reviews)
            self.assertEqual(fixture.check_runs, old_checks)
            self.assertEqual(old.api_run_accounting, old_document)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertEqual(
                len(fixture.selected.ledger._quarantine_authenticated.pending), 14
            )
            self.assertFalse(any(row["state"] == "APPROVED" for row in fixture.reviews))


if __name__ == "__main__":
    unittest.main()
