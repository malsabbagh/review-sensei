"""Pure prospective bytes preserve both origins; they authorize no effect."""

import copy
import json
import unittest
from types import SimpleNamespace

from review_sensei_github.quarantine_command_projection import project_decision
from test_quarantine_assessments import origins_value
from test_quarantine_lineage import decision, lineage_value

from review_sensei.bounded_evidence import canonical_bytes, inline_manifest
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import pending_assessments
from review_sensei.quarantine_state import assessment_cursor


class DecisionProjectionTests(unittest.TestCase):
    def setup_projection(self):
        origins, inventories = origins_value()
        binding = {
            **lineage_value()["origin_manifest"]["binding"],
            "policy_digest": "0" * 64,
            "generation": 2_147_483_647,
        }
        state = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [],
        }
        authenticated = SimpleNamespace(
            archive={"assessment_origins": origins},
            inventories=inventories,
            state=state,
            lineage={"scope_sha256": "0" * 64},
        )
        record = SimpleNamespace(
            quarantine_lineage={
                "assessment_state_manifest": inline_manifest(
                    assessment_cursor(scope="0" * 64), binding=binding, item_count=0
                )
            }
        )
        return authenticated, record, binding, origins

    def test_exact_and_last_settlement_projection_preserve_other_origin_and_inputs(
        self,
    ):
        auth, record, binding, origins = self.setup_projection()
        before = copy.deepcopy((auth.state, record.quarantine_lineage))
        first = project_decision(
            auth,
            record,
            decision(origins["origins"][0]["scope_sha256"]),
            binding=binding,
        )
        self.assertTrue(first.creates_event)
        self.assertGreater(first.event_body_bytes, 0)
        self.assertEqual((auth.state, record.quarantine_lineage), before)
        first_state = json.loads(first.state_bytes)
        self.assertEqual(
            len(
                pending_assessments(
                    origins, auth.inventories, first_state, scope="0" * 64
                )
            ),
            1,
        )
        # Feed the value to the next pure projection, not a live reader or ledger.
        auth.state = first_state
        record.quarantine_lineage["assessment_state_manifest"] = json.loads(
            first.cursor_manifest_bytes
        )
        second = project_decision(
            auth,
            record,
            decision(origins["origins"][1]["scope_sha256"]),
            binding=binding,
        )
        final = json.loads(second.state_bytes)
        self.assertEqual(final["settlements"][0], first_state["settlements"][0])
        self.assertEqual(
            pending_assessments(origins, auth.inventories, final, scope="0" * 64), ()
        )
        cursor = json.loads(second.cursor_manifest_bytes)["document"]
        self.assertEqual(cursor["tail_manifest"]["parts"][0]["storage_id"], "9" * 64)
        self.assertEqual(cursor["count"], 2)

    def test_duplicate_and_conflict_project_unchanged_history_without_new_event(self):
        auth, record, binding, origins = self.setup_projection()
        selected = decision(origins["origins"][0]["scope_sha256"])
        first = project_decision(auth, record, selected, binding=binding)
        auth.state = json.loads(first.state_bytes)
        record.quarantine_lineage["assessment_state_manifest"] = json.loads(
            first.cursor_manifest_bytes
        )
        for action in (selected["action"], "accept-risk"):
            with self.subTest(action=action):
                projected = project_decision(
                    auth, record, {**selected, "action": action}, binding=binding
                )
                self.assertFalse(projected.creates_event)
                self.assertIsNone(projected.event_bytes)
                self.assertEqual(projected.event_body_bytes, 0)
                self.assertEqual(projected.state_bytes, canonical_bytes(auth.state))
                self.assertEqual(
                    projected.cursor_manifest_bytes, first.cursor_manifest_bytes
                )

    def test_foreign_target_is_not_a_prospective_successor(self):
        auth, record, binding, _origins = self.setup_projection()
        with self.assertRaises(ReviewInputError):
            project_decision(auth, record, decision("f" * 64), binding=binding)
