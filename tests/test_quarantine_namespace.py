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

    def test_round2_pre_write_failure_retaining_round1_lifecycle_allows_round1_scope(
        self,
    ):
        from types import SimpleNamespace

        from review_sensei_github.quarantine_origin import (
            _AUTHENTICATED,
            AuthenticatedQuarantine,
        )

        from review_sensei.bounded_evidence import (
            canonical_bytes,
            evidence_manifest,
            partition_evidence,
        )
        from review_sensei.quarantine_assessments import origin_scope
        from review_sensei.session import SessionIdentity

        identity = SessionIdentity("owner/repo", 7, 123)
        head_r1 = "1" * 40
        base_r1 = "2" * 40
        head_r2 = "3" * 40
        base_r2 = "4" * 40
        r1_op_id = "a" * 64
        r2_tx_id = "b" * 64
        lineage_scope = "e" * 64

        ev_binding = {
            "repository": identity.repository,
            "repository_id": identity.repository_id,
            "pull_request": identity.pull_request,
            "base_sha": base_r1,
            "head_sha": head_r1,
            "policy_digest": "5" * 64,
            "configuration_digest": "6" * 64,
            "generation": 1,
            "producer": "github-bot:999",
            "purpose": "evidence",
            "schema_version": "1.0",
        }
        fl_ref = {
            "operation_id": r1_op_id,
            "manifest": {
                "binding": {
                    **ev_binding,
                    "purpose": "finding-lifecycle",
                }
            },
        }
        predecessor = SimpleNamespace(
            identity=lambda: identity,
            record_sha256="c" * 64,
            finding_lifecycle=fl_ref,
            transaction=SimpleNamespace(
                phase="publication_failed",
                transaction_id=r2_tx_id,
                head_sha=head_r2,
                base_sha=base_r2,
            ),
        )
        lineage = {
            "predecessor_record_sha256": predecessor.record_sha256,
            "scope_sha256": lineage_scope,
        }
        record = SimpleNamespace(
            identity=lambda: identity,
            record_sha256="d" * 64,
            quarantine_lineage=lineage,
            finding_lifecycle=fl_ref,
        )
        source_doc = {
            "schema_version": "quarantine-lifecycle-published-assessment-source-v1"
        }
        inventory_doc = {
            "schema_version": "quarantine-assessment-inventory-v1",
            "assessments": [],
        }
        source_parts = partition_evidence(source_doc, binding=ev_binding, item_count=1)
        inventory_parts = partition_evidence(
            inventory_doc, binding=ev_binding, item_count=0
        )
        origin = {
            "schema_version": "quarantine-assessment-origin-v1",
            "kind": "retained-result",
            "source_manifest": evidence_manifest(
                source_doc,
                binding=ev_binding,
                item_count=1,
                parts=source_parts,
                storage_ids=tuple(str(200 + i) for i in range(len(source_parts))),
            ),
            "inventory_manifest": evidence_manifest(
                inventory_doc,
                binding=ev_binding,
                item_count=0,
                parts=inventory_parts,
                storage_ids=tuple(str(300 + i) for i in range(len(inventory_parts))),
            ),
        }
        origin["scope_sha256"] = origin_scope(origin)
        archive = {
            "assessment_origins": {
                "schema_version": "quarantine-assessment-origins-v1",
                "origins": [origin],
            }
        }
        inventories = {origin["scope_sha256"]: inventory_doc}
        assessment_state = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": lineage_scope,
            "settlements": [],
        }
        source_docs = {"retained-result": source_doc}
        caller = SimpleNamespace(_require_ledger=lambda *_a: None)
        auth = AuthenticatedQuarantine(
            record_sha256=record.record_sha256,
            lineage_bytes=canonical_bytes(lineage),
            archive_bytes=canonical_bytes(archive),
            inventories_bytes=canonical_bytes(inventories),
            state_bytes=canonical_bytes(assessment_state),
            source_documents_bytes=canonical_bytes(source_docs),
            original_caller=caller,
            original_identity=identity,
        )
        _AUTHENTICATED.add(auth)
        ledger = SimpleNamespace(caller=caller, _quarantine_authenticated=auth)
        state = SimpleNamespace(
            head_sha=head_r1,
            base_sha=base_r1,
            inventory=SimpleNamespace(obligations=()),
        )
        document = {"unknown_trust_effect": False, "effects": []}

        with (
            patch(
                "review_sensei_github.quarantine_namespace.consume_namespace_original",
                create=True,
                return_value=None,
            ),
            patch(
                "review_sensei_github.quarantine_lifecycle_transfer.consume_namespace_original",
                return_value=None,
            ),
            patch(
                "review_sensei_github.quarantine_origin.read_predecessor",
                return_value=predecessor,
            ),
        ):
            self.assertTrue(
                namespace.inherited_namespace_is_historical(
                    ledger, record, state, document
                )
            )

            # Post-write failure (operation_id == transaction_id) does NOT widen allowed_scopes.
            post_write_pred = SimpleNamespace(
                identity=lambda: identity,
                record_sha256=predecessor.record_sha256,
                finding_lifecycle={**fl_ref, "operation_id": r2_tx_id},
                transaction=predecessor.transaction,
            )
            post_write_rec = SimpleNamespace(
                identity=lambda: identity,
                record_sha256=record.record_sha256,
                quarantine_lineage=lineage,
                finding_lifecycle=post_write_pred.finding_lifecycle,
            )
            with patch(
                "review_sensei_github.quarantine_origin.read_predecessor",
                return_value=post_write_pred,
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "original snapshot conflicts"
                ):
                    namespace.inherited_namespace_is_historical(
                        ledger, post_write_rec, state, document
                    )
