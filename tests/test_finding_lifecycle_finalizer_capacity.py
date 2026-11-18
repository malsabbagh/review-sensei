"""Finalizer state projections are capacity witnesses, never live authority."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import finding_lifecycle as carrier

from review_sensei.active_finding_window import apply_dispositions_to_window_state
from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_journal as f


class FinalizerCapacityTests(unittest.TestCase):
    def test_projects_current_and_final_states_without_installing_authority(self):
        value = f.journal()
        current = value._planned
        projected = apply_dispositions_to_window_state(
            current,
            honored_fingerprints={
                o.fingerprint
                for o in current.authoritative_decision_inventory.obligations
            },
            honored_instances=set(),
            actor="maintainer",
            head_sha=f.HEAD,
            slot_released=True,
        )
        original_document = value._document()
        original_states = dict(value.reviewer._active_window_states)
        partial = apply_dispositions_to_window_state(
            current,
            honored_fingerprints={
                o.fingerprint
                for o in current.authoritative_decision_inventory.obligations
            },
            honored_instances=set(),
            actor="maintainer",
            head_sha=f.HEAD,
            slot_released=False,
        )
        # The real finalizer applies one aggregate bool: any unresolved target
        # selects the contingent projection for ALL honored dispositions.
        value.prepare_finalizer_capacity_states((partial, projected))
        self.assertIs(value._planned, current)
        self.assertEqual(value._document(), original_document)
        self.assertEqual(value.reviewer._active_window_states, original_states)
        value.effects = [f.effect()]
        seen = []
        pack = carrier._pack_journal

        def observe(document):
            seen.append(document["lifecycle"]["state"])
            return pack(document)

        with patch.object(carrier, "_pack_journal", side_effect=observe):
            value._preflight_effect(capacity_only=True)
        from review_sensei.finding_lifecycle import encode_lifecycle_state

        for outcome in (partial, projected):
            expected = encode_lifecycle_state(
                outcome, input_identity=(f.HEAD, f.BASE, f.INPUT)
            )["state"]
            self.assertIn(expected, seen)
        self.assertGreater(len({repr(state) for state in seen}), 1)
        self.assertIs(value._planned, current)
        self.assertEqual(value.reviewer._active_window_states, original_states)

    def test_rejects_foreign_or_untyped_projections_without_effects(self):
        value = f.journal()
        for states in (
            [value._planned],
            ({},),
            (replace(value._planned, head_sha="f" * 40),),
            (value._planned,) * 5,
        ):
            with self.subTest(states=type(states)):
                with self.assertRaises(ReviewInputError):
                    value.prepare_finalizer_capacity_states(states)
                self.assertFalse(hasattr(value, "_finalizer_capacity_states"))
                self.assertEqual(value.effects, [])

    def test_only_multiple_eligible_verified_contingent_targets_hold_before_effects(
        self,
    ):
        from review_sensei_github.publication import NativeFindingReceipt

        from review_sensei.active_finding_window import (
            commit_window_publication,
            evaluate_active_window_round,
            evaluate_window_approval_eligibility,
            select_display_active_window,
        )
        from review_sensei.finding_lifecycle import encode_lifecycle_state
        from review_sensei.human_assessment import finding_instance_fingerprint

        value = f.journal()
        state = evaluate_active_window_round(
            f.result(f.comment(), f.comment("src/two.py")),
            head_sha=f.HEAD,
            base_sha=f.BASE,
            max_active_findings=2,
        )
        state = commit_window_publication(
            state,
            created_targets={
                entry.obligation_id: "T" + str(i)
                for i, entry in enumerate(state.display_active_findings.entries)
            },
        )
        inventory = replace(
            state.authoritative_decision_inventory,
            obligations=tuple(
                replace(
                    item,
                    lifecycle_state="verified-fixed",
                    target_state="contingent",
                    resolution_proof_sha256="f" * 64,
                )
                for item in state.authoritative_decision_inventory.obligations
            ),
        )
        window = select_display_active_window(
            inventory,
            slots=tuple(
                replace(slot, state="contingent")
                for slot in state.display_active_findings.slots
            ),
            max_active_findings=2,
        )
        state = replace(
            state,
            authoritative_decision_inventory=inventory,
            display_active_findings=window,
            approval_eligibility=evaluate_window_approval_eligibility(
                changed_file_coverage=state.changed_file_coverage,
                authoritative_decision_inventory=inventory,
                display_active_findings=window,
                review_complete=True,
            ),
        )
        encode_lifecycle_state(state, input_identity=(f.HEAD, f.BASE, f.INPUT))
        receipts = tuple(
            NativeFindingReceipt(
                repository_id=7,
                pull_request=1,
                head_sha=f.HEAD,
                base_sha=f.BASE,
                producer_id=9001,
                review_id=1201,
                instance=finding_instance_fingerprint(item.comment),
                body_sha256="a" * 64,
                thread_id=item.native_target,
                comment_id=2100 + i,
                native_head_sha=f.HEAD,
            ).to_dict()
            for i, item in enumerate(inventory.obligations)
        )
        from review_sensei_github.finding_lifecycle_consumers import (
            project_confirmed_slot_release,
        )

        projected = project_confirmed_slot_release(
            state,
            key=(7, 1),
            receipts={r["instance"]: r for r in receipts},
            instances={receipts[0]["instance"]},
            head_sha=f.HEAD,
        )
        self.assertEqual(
            sum(
                slot.state == "released"
                for slot in projected.display_active_findings.slots
            ),
            1,
        )
        self.assertTrue(
            all(
                slot.state == "contingent"
                for slot in state.display_active_findings.slots
            )
        )
        encode_lifecycle_state(projected, input_identity=(f.HEAD, f.BASE, f.INPUT))
        held = project_confirmed_slot_release(
            state,
            key=(7, 1),
            receipts={r["instance"]: {**r, "head_sha": "c" * 40} for r in receipts},
            instances={receipts[0]["instance"]},
            head_sha=f.HEAD,
        )
        self.assertIs(held, state)
        value._planned = state
        value.reviewer._active_window_states[(7, 1)] = state
        value.reviewer.native_finding_receipts = lambda **kw: receipts
        http = f.FakeHTTP((200, {}))
        value._readback_http = http
        # Read-only/unrelated history and the existing single-target path stay
        # supported. Two genuine eligible targets may release partial subsets.
        value.prepare_finalizer_capacity_states((state,))
        value.prepare_finalizer_capacity_states(
            (state,), cleanup_instances=frozenset((receipts[0]["instance"],))
        )
        with self.assertRaisesRegex(
            ReviewInputError, "verified_fixed_subset_capacity_unqualified"
        ):
            value.prepare_finalizer_capacity_states(
                (state,), cleanup_instances=frozenset(r["instance"] for r in receipts)
            )
        self.assertEqual(http.calls, [])
        self.assertEqual(value.effects, [])
        self.assertIs(value._planned, state)
