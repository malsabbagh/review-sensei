"""Actual guarded producer refuses unfunded durable effects before native work."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class GuardedPublicationFloorTests(unittest.TestCase):
    def test_original_512_refuses_before_check_or_review(self):
        self._exercise()

    def test_copied_installed_guard_cannot_supply_floor_quote(self):
        self._exercise(copy_guard=True)

    def _exercise(self, *, copy_guard=False):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            old, registration = fixture.original_origins(
                failed_count=9, published_count=5
            )
            reviews = copy.deepcopy(fixture.reviews)
            old_body = fixture.comment["body"]
            from review_sensei_github.guarded_publication_plan import (
                require_durable_floor,
            )

            refused = []
            changed = []

            def observe(frame, event, arg):
                if (
                    copy_guard
                    and not changed
                    and event == "call"
                    and frame.f_code is require_durable_floor.__code__
                ):
                    from review_sensei_github.publication_recovery import (
                        _publication_transport,
                    )

                    journal = frame.f_locals["journal"]
                    if (
                        journal is not None
                        and journal._record.quarantine_lineage is not None
                    ):
                        transport = _publication_transport(journal.ledger.http)
                        original = transport._quarantine_capacity_guard
                        transport._quarantine_capacity_guard = copy.copy(original)
                        changed.append((transport, original))
                if (
                    frame.f_code is require_durable_floor.__code__
                    and event == "exception"
                ):
                    if type(arg[1]).__name__ == "EvidenceBudgetExhausted":
                        refused.append(
                            (
                                frame.f_locals["minimum_checkpoint"],
                                frame.f_locals["minimum_calls"],
                            )
                        )
                return observe

            previous = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    self.assertNotEqual(fixture.invoke(), 0)
            finally:
                sys.settrace(previous)
                for transport, original in changed:
                    transport._quarantine_capacity_guard = original
            self.assertEqual(refused, [] if copy_guard else [(15, 63)])
            if copy_guard:
                self.assertEqual(len(changed), 1)
                self.assertIn("installed capacity source changed", fixture.error)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.comment["body"], old_body)
            forbidden = [
                row
                for row in fixture.trace
                if row[0] in {"POST", "PUT", "PATCH", "DELETE"}
                and ("check-runs" in row[1] or row[1].endswith("/reviews"))
            ]
            self.assertEqual(forbidden, [])
            self.assertEqual(
                fixture.current_record().api_run_accounting["configuration"]["limit"],
                512,
            )

            from review_sensei.api_work import ApiRunAccounting

            accounting = ApiRunAccounting.from_document(
                fixture.current_record().api_run_accounting
            )
            self.assertEqual(accounting.uncertain, 0)
            self.assertEqual(accounting.startup.liability, 130)
            self.assertEqual(accounting.startup_used, (("hosted", 1),))
            self.assertGreater(accounting.confirmed, 0)
            self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
