"""The negative floor distinguishes authentic plain and guarded profiles."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.guarded_pending_publication import admit as admit_pending
from review_sensei_github.publication_recovery import _publication_transport
from test_quarantine_activation import registered_resource
from test_quarantine_journey import JourneyFixture
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class GuardedFloorProfileTests(unittest.TestCase):
    def test_authentic_plain_isolation_keeps_its_existing_first_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            _old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            old_body = fixture.comment["body"]
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.comment["body"], old_body)
            self.assertEqual(
                fixture.current_record().quarantine_lineage["origin_count"], 2
            )
            self.assertIsNone(
                _publication_transport(
                    fixture.selected.ledger.http
                )._quarantine_capacity_guard
            )
            self.assertFalse(
                any(row.get("state") == "APPROVED" for row in fixture.reviews)
            )
            self.assertEqual(
                fixture.current_record().api_run_accounting["configuration"]["limit"],
                512,
            )

    def test_guarded_profile_cannot_become_plain_by_removing_or_copying_guard(self):
        for replacement in ("missing", "copy"):
            with (
                self.subTest(replacement=replacement),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = PublishedTailJourneyFixture(directory)
                _old, registration = fixture.original_origins(
                    failed_count=1, published_count=1
                )
                reviews, checks = (
                    copy.deepcopy(fixture.reviews),
                    copy.deepcopy(fixture.check_runs),
                )
                changed = []

                def observe(frame, event, arg):
                    if (
                        not changed
                        and event == "call"
                        and frame.f_code is admit_pending.__code__
                    ):
                        journal = frame.f_locals["journal"]
                        if journal is not None and journal._record.quarantine_lineage:
                            transport = _publication_transport(journal.ledger.http)
                            guard = transport._quarantine_capacity_guard
                            self.assertIsNotNone(guard)
                            transport._quarantine_capacity_guard = (
                                None if replacement == "missing" else copy.copy(guard)
                            )
                            changed.append((transport, guard))
                    return observe

                prior = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    with registered_resource([registration]):
                        self.assertNotEqual(fixture.invoke(), 0)
                finally:
                    sys.setprofile(prior)
                    for transport, guard in changed:
                        transport._quarantine_capacity_guard = guard
                self.assertEqual(len(changed), 1)
                self.assertIn(
                    "installed capacity source changed",
                    fixture.error,
                )
                self.assertEqual(fixture.reviews, reviews)
                self.assertEqual(fixture.check_runs, checks)
