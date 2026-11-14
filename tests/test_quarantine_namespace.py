"""Historical origin coverage, not a lineage marker, separates fresh findings."""

import copy
import dis
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import quarantine_namespace as namespace
from review_sensei_github.publication import approval_eligibility_from_body
from test_active_window_lifecycle_g10 import _comment
from test_quarantine_journey import JourneyFixture, registered_resource

from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import drift_obligation_from_comment


class QuarantineNamespaceTests(unittest.TestCase):
    def test_actual_hidden_origins_keep_uncertain_predecessor_but_not_fresh_findings(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            fixture.active_finding_cap = 1
            previous, registration = fixture.original_origins(published_count=3)
            previous_body = fixture.comment["body"]
            previous_reference = copy.deepcopy(previous.finding_lifecycle)
            retained_parts = copy.deepcopy(fixture.extra_comments)
            original = namespace.inherited_namespace_is_historical
            seen = []

            def inspect(ledger, record, state, document):
                seen.append(state)
                authenticated = ledger._quarantine_authenticated
                before_requests = len(fixture.trace)
                # Real current source object must remain registered. A copied
                # payload or bare lineage cannot stand in for source authority.
                for invalid in (None, replace(authenticated)):
                    with patch.object(ledger, "_quarantine_authenticated", invalid):
                        with self.assertRaisesRegex(
                            ReviewInputError, "authenticated origins"
                        ):
                            original(ledger, record, state, document)
                # Coverage boundary value test: even a hidden/ambiguous current
                # criterion absent from ALL authenticated origin inventories
                # prevents separation. No such state is written into the store.
                extra = drift_obligation_from_comment(
                    _comment(
                        body="Independent missing historical obligation.",
                        symbol="foreign",
                    ),
                    head_sha=state.head_sha,
                )
                missing = replace(
                    state,
                    inventory=replace(
                        state.inventory,
                        obligations=(*state.inventory.obligations, extra),
                    ),
                )
                with self.assertRaisesRegex(
                    ReviewInputError, "unrepresented original criterion"
                ):
                    original(ledger, record, missing, document)
                advisory = drift_obligation_from_comment(
                    _comment(
                        body="Hidden advisory without an authenticated source.",
                        symbol="advisory",
                        blocking=False,
                        needs_human=False,
                    ),
                    head_sha=state.head_sha,
                )
                advisory_missing = replace(
                    state,
                    inventory=replace(
                        state.inventory,
                        obligations=(*state.inventory.obligations, advisory),
                    ),
                )
                self.assertFalse(advisory.blocks_approval_now)
                with self.assertRaisesRegex(
                    ReviewInputError, "unrepresented original criterion"
                ):
                    original(ledger, record, advisory_missing, document)
                unknown = {**document, "unknown_trust_effect": True}
                with self.assertRaisesRegex(
                    ReviewInputError, "unclassified native liabilities"
                ):
                    original(ledger, record, state, unknown)
                pending = {**document, "effects": [{"status": "pending"}]}
                with self.assertRaisesRegex(
                    ReviewInputError, "unclassified native liabilities"
                ):
                    original(ledger, record, state, pending)
                self.assertEqual(len(fixture.trace), before_requests)

            returns = {
                item.offset
                for item in dis.get_instructions(original)
                if item.opname in {"RETURN_VALUE", "RETURN_CONST"}
            }

            def observe(frame, event, value):
                if (
                    event == "return"
                    and value is True
                    and frame.f_code is original.__code__
                    and frame.f_globals is original.__globals__
                    and frame.f_lasti in returns
                ):
                    inspect(
                        *(
                            frame.f_locals[name]
                            for name in ("ledger", "record", "state", "document")
                        )
                    )

            previous_profile = sys.getprofile()
            try:
                sys.setprofile(observe)
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
            finally:
                sys.setprofile(previous_profile)
            self.assertTrue(seen)
            self.assertGreater(
                len(seen[0].inventory.obligations), len(seen[0].active_entries)
            )
            eligibility = approval_eligibility_from_body(fixture.reviews[-1]["body"])
            self.assertFalse(eligibility.facts.has_blocking_findings)
            self.assertFalse(eligibility.quarantine_assessments["fresh_human_pending"])
            self.assertEqual(len(eligibility.quarantine_assessments["pending"]), 4)
            self.assertEqual(fixture.check_runs[-1]["conclusion"], "action_required")
            self.assertEqual(fixture.comment["body"], previous_body)
            self.assertIsNone(previous.api_run_accounting["terminal_transaction"])
            current = fixture.current_record()
            self.assertNotEqual(current.finding_lifecycle, previous_reference)
            self.assertEqual(current.api_run_accounting["configuration"]["limit"], 512)
            for identifier, item in retained_parts.items():
                self.assertEqual(fixture.extra_comments[identifier], item)
            self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))
