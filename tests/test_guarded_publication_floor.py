"""Nonempty guarded routes retain their conservative pre-native floor.

The separately qualified empty pending-history route has a complete three-CAS
plan. It is not evidence that an ordinary nonempty successor can use that plan.
"""

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
    def test_nonempty_original_funding_refuses_before_check_or_review(self):
        self._exercise()

    def test_copied_installed_guard_cannot_supply_floor_quote(self):
        self._exercise(copy_guard=True)

    def _exercise(self, *, copy_guard=False):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            from tests.test_quarantine_evidence import git

            fixture.config_bytes = fixture.config_bytes.replace(
                b"api_request_limit: 512", b"api_request_limit: 494"
            )
            (fixture.root / ".reviewsensei.yml").write_bytes(fixture.config_bytes)
            git(fixture.root, "add", ".reviewsensei.yml")
            git(fixture.root, "commit", "-m", "nonempty original floor allocation")
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Fresh independent finding cannot use the empty-history plan.",
                    "severity": "high",
                    "blocking": True,
                    "evidence_id": "fresh-nonempty-floor",
                }
            ]
            reviews = copy.deepcopy(fixture.reviews)
            old_body = fixture.comment["body"]
            from review_sensei_github.guarded_publication_plan import (
                require_durable_floor,
            )

            quotes = []
            from review_sensei_github.api_run_budget import CallerRunBudget

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
                    event == "call"
                    and frame.f_code is CallerRunBudget.preflight.__code__
                    and frame.f_back is not None
                    and frame.f_back.f_code is require_durable_floor.__code__
                ):
                    quotes.append(
                        (
                            frame.f_back.f_locals["minimum_checkpoint"],
                            frame.f_locals["calls"],
                            frame.f_locals["self"].ordinary_remaining(),
                        )
                    )
                return observe

            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                with registered_resource([registration]):
                    self.assertNotEqual(fixture.invoke(), 0)
            finally:
                sys.setprofile(previous)
                for transport, original in changed:
                    transport._quarantine_capacity_guard = original
            self.assertEqual(
                [(part, calls) for part, calls, _ in quotes],
                [] if copy_guard else [(15, 63)],
                fixture.error,
            )
            self.assertTrue(all(remaining < calls for _, calls, remaining in quotes))
            if copy_guard:
                self.assertEqual(len(changed), 1)
                self.assertIn("installed capacity source changed", fixture.error)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.comment["body"], old_body)
            forbidden = [
                row
                for row in fixture.trace
                if row[1] in {"POST", "PUT", "PATCH", "DELETE"}
                and ("check-runs" in row[2] or row[2].endswith("/reviews"))
            ]
            self.assertEqual(forbidden, [])
            self.assertEqual(
                fixture.current_record().api_run_accounting["configuration"]["limit"],
                494,
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
