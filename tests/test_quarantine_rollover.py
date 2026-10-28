"""Issued exact-root startup tail: real stored/absent archive acknowledgments."""

import copy
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.api_run_budget import CallerRunBudget
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from review_sensei_github.quarantine_rollover import retention_preflight
from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture

from review_sensei.errors import ReviewInputError


class ArchiveFaultFixture(PublishedTailJourneyFixture):
    archive_fault = None
    archive_posts = 0

    def external(self, request, timeout):
        if (
            self.archive_fault is not None
            and request.get_method() == "POST"
            and urlsplit(request.full_url).path.endswith("/issues/7/comments")
            and self.accounted._state is None
            and self.accounted._reservation_started
        ):
            self.archive_posts += 1
            fault, self.archive_fault = self.archive_fault, None
            if fault == "stored":
                super().external(request, timeout)
            else:
                self._meter(request)
            raise TimeoutError("synthetic original archive acknowledgment unknown")
        return super().external(request, timeout)


class QuarantineRolloverTests(unittest.TestCase):
    def test_stored_and_absent_archive_effects_keep_the_exact_tail_and_never_repost(
        self,
    ):
        for loss in ("stored", "absent"):
            with (
                self.subTest(loss=loss),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                f = ArchiveFaultFixture(directory)
                old, registration = f.original_origins()
                with registered_resource([registration]):
                    self.assertEqual(f.invoke(), 0, f.error)
                    previous = f.current_record()
                    inherited = copy.deepcopy(old.to_dict())
                    pending = f.selected.ledger._quarantine_authenticated.inventories
                    origin = next(iter(pending))
                    instance = pending[origin]["assessments"][0]["instance"]
                    f.quarantine_predecessor_comment_id = None
                    f.select_command(
                        "@reviewsensei dismiss "
                        + instance
                        + ' --reason "quarantine:'
                        + origin
                        + ' Original source disproves this historical concern only."',
                        new_run=True,
                    )
                    f.archive_fault = loss
                    bounded = []
                    original_preflight = CallerRunBudget.preflight
                    original_accept = ApiRunRequestTransport._accept_activation

                    def preflight(budget, calls):
                        if budget.caller._state is None and calls == 17:
                            bounded.append({"start": len(f.trace), "calls": calls})
                        return original_preflight(budget, calls)

                    def accept(caller, ledger, record):
                        if bounded and "end" not in bounded[-1]:
                            bounded[-1]["end"] = len(f.trace)
                        return original_accept(caller, ledger, record)

                    with (
                        patch.object(CallerRunBudget, "preflight", preflight),
                        patch.object(
                            ApiRunRequestTransport, "_accept_activation", accept
                        ),
                    ):
                        result = f.invoke_command()
                    self.assertEqual(
                        result,
                        0 if loss == "stored" else 1,
                        f.error or str(f.exception),
                    )
                    self.assertEqual(f.archive_posts, 1)
                    self.assertTrue(bounded)
                    tail = bounded[-1]
                    self.assertLessEqual(
                        tail.get("end", len(f.trace)) - tail["start"], tail["calls"]
                    )
                    unknown = [
                        row
                        for row in f.accounted.observations
                        if row.get("mutation") and row["outcome"] == "unknown"
                    ]
                    self.assertEqual(len(unknown), 1)
                    if loss == "stored":
                        # Startup liability is reserved in full, independently
                        # of the ordinary pending-request counter. Confirming
                        # the immutable carrier settles its effect without
                        # deleting/refunding the original unknown observation.
                        self.assertIn(
                            unknown[0]["request_id"],
                            f.accounted._reconciled_mutations,
                        )
                        self.assertEqual(
                            f.accounted.accounting.startup, f.accounted.plan
                        )
                        self.assertEqual(
                            f.current_record().api_run_accounting["previous_record"][
                                "root_sha256"
                            ],
                            previous.record_sha256,
                        )
                    else:
                        self.assertEqual(
                            f.current_record().record_sha256, previous.record_sha256
                        )
                        self.assertEqual(f.invoke_command(), 1)
                        self.assertEqual(f.archive_posts, 1)
                    self.assertEqual(old.to_dict(), inherited)

    def test_unissued_dictionary_cannot_reduce_retention_preflight(self):
        from types import SimpleNamespace

        ledger = SimpleNamespace(_quarantine_rollover_snapshot={"calls": 1})
        with self.assertRaises(ReviewInputError):
            retention_preflight(ledger, None)
