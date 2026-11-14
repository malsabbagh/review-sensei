"""Cold commands must not write a decision with a known unsupported tail."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_action
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_journey import JourneyFixture, registered_resource
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture


class ColdCommandPreflightTests(unittest.TestCase):
    def test_unsupported_installed_tail_stops_before_decision_and_action_boundary(self):
        self.exercise_guard("unsupported")

    def test_removed_and_copied_installed_guards_cannot_bypass_preflight(self):
        for fault in ("removed", "copied"):
            with self.subTest(fault=fault):
                self.exercise_guard(fault)

    def test_genuine_plain_target_does_not_require_a_native_guard(self):
        self.exercise_guard("plain")

    def test_plain_first_decision_uses_shared_projection_without_changing_authority(
        self,
    ):
        self.exercise_guard("plain-positive")

    def test_actual_decision_permit_fields_cannot_be_replaced_before_cursor_write(self):
        self.exercise_guard("plain-permit-seal")

    def test_actual_candidate_get_receipt_rejects_corruption_copy_and_replay(self):
        self.exercise_guard("plain-candidate-receipt")

    def exercise_guard(self, fault):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = (
                JourneyFixture
                if fault.startswith("plain")
                else PublishedTailJourneyFixture
            )(directory)
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            original_history = old.to_dict()
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                inventories = (
                    fixture.selected.ledger._quarantine_authenticated.inventories
                )
                origin = next(iter(inventories))
                instance = inventories[origin]["assessments"][0]["instance"]
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + instance
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                entered = []
                touched = []
                sealed = []
                snapshots = []
                receipt_checks = []
                helper_code = quarantine_action._require_installed_command_tail.__code__
                stage_code = quarantine_action._stage_decision_event.__code__

                def observe(frame, event, arg):
                    from review_sensei_github import quarantine_candidate_receipt

                    if fault == "plain-candidate-receipt":
                        if (
                            frame.f_code is quarantine_candidate_receipt.finish.__code__
                            and event == "return"
                        ):
                            snapshots.append(frame.f_locals["snapshot"])
                        if (
                            frame.f_code
                            is quarantine_action._confirmed_own_decision.__code__
                        ):
                            run = frame.f_locals["run"]
                            confirmed = frame.f_locals["confirmed"]
                            from review_sensei.errors import ReviewInputError

                            if event == "call":
                                snapshot = snapshots[-1]
                                before_reads = len(fixture.trace)
                                for field, changed in (
                                    ("owner_id", snapshot.owner_id + 1),
                                    ("comment_id", snapshot.comment_id + 1),
                                    ("candidate_body", snapshot.candidate_body + " "),
                                ):
                                    original_value = getattr(snapshot, field)
                                    try:
                                        object.__setattr__(snapshot, field, changed)
                                        with self.assertRaisesRegex(
                                            ReviewInputError, "changed"
                                        ):
                                            quarantine_candidate_receipt.consume(
                                                run.caller,
                                                run.ledger,
                                                run.identity,
                                                confirmed,
                                            )
                                    finally:
                                        object.__setattr__(
                                            snapshot, field, original_value
                                        )
                                with self.assertRaisesRegex(
                                    ReviewInputError, "changed"
                                ):
                                    quarantine_candidate_receipt.consume(
                                        run.caller,
                                        run.ledger,
                                        run.identity,
                                        copy.copy(confirmed),
                                    )
                                observation = run.caller.observations[-1]
                                original_outcome = observation["outcome"]
                                try:
                                    observation["outcome"] = "unknown"
                                    with self.assertRaisesRegex(
                                        ReviewInputError, "changed"
                                    ):
                                        quarantine_candidate_receipt.consume(
                                            run.caller,
                                            run.ledger,
                                            run.identity,
                                            confirmed,
                                        )
                                finally:
                                    observation["outcome"] = original_outcome
                                self.assertEqual(len(fixture.trace), before_reads)
                                receipt_checks.append(before_reads)
                            elif event == "return":
                                with self.assertRaisesRegex(
                                    ReviewInputError, "unavailable"
                                ):
                                    quarantine_candidate_receipt.consume(
                                        run.caller, run.ledger, run.identity, confirmed
                                    )
                                self.assertEqual(len(fixture.trace), receipt_checks[-1])
                    if (
                        fault == "plain-permit-seal"
                        and event == "call"
                        and frame.f_code
                        is quarantine_action.require_native_decision.__code__
                        and frame.f_locals["before"].quarantine_lineage[
                            "assessment_state_manifest"
                        ]
                        != frame.f_locals["after"].quarantine_lineage[
                            "assessment_state_manifest"
                        ]
                    ):
                        from review_sensei.errors import ReviewInputError

                        ledger = frame.f_locals["ledger"]
                        permit = ledger._quarantine_decision_permit
                        before_reads = len(fixture.trace)
                        for field, replacement in (
                            ("tail", b"{}"),
                            ("before_state", b"{}"),
                            ("scope", "0" * 64),
                            ("head_sha", "0" * 40),
                            ("run_id", permit.run_id + 1),
                        ):
                            original_value = getattr(permit, field)
                            try:
                                object.__setattr__(permit, field, replacement)
                                with self.assertRaisesRegex(
                                    ReviewInputError, "issued exact"
                                ):
                                    quarantine_action.require_native_decision(
                                        ledger,
                                        frame.f_locals["before"],
                                        frame.f_locals["after"],
                                    )
                            finally:
                                object.__setattr__(permit, field, original_value)
                        self.assertEqual(len(fixture.trace), before_reads)
                        sealed.append(permit)
                    if frame.f_code is helper_code:
                        if event == "call":
                            from review_sensei_github.publication_recovery import (
                                _publication_transport,
                            )

                            run = frame.f_locals["run"]
                            target = frame.f_back.f_locals["target"]
                            from review_sensei_github.quarantine_lifecycle_transfer import (
                                confirmed_transfer,
                                sizing_snapshot,
                            )

                            from review_sensei.errors import ReviewInputError

                            before_reads = len(fixture.trace)
                            record, document = sizing_snapshot(target, run)
                            self.assertIs(record, run.caller._effect_record)
                            self.assertIsNotNone(record.finding_lifecycle)
                            self.assertIsInstance(document, dict)
                            with self.assertRaisesRegex(
                                ReviewInputError, "custody changed"
                            ):
                                sizing_snapshot(copy.copy(target), run)
                            with self.assertRaisesRegex(
                                ReviewInputError, "not confirmed"
                            ):
                                confirmed_transfer(
                                    target, run, record, decision_permit=None
                                )
                            document.clear()
                            self.assertTrue(sizing_snapshot(target, run)[1])
                            self.assertEqual(len(fixture.trace), before_reads)
                            transport = _publication_transport(run.ledger.http)
                            original = transport._quarantine_capacity_guard
                            touched.append((transport, original))
                            if fault == "removed":
                                transport._quarantine_capacity_guard = None
                            elif fault == "copied":
                                transport._quarantine_capacity_guard = copy.copy(
                                    original
                                )
                            elif fault == "plain":
                                self.assertIsNone(original)
                        elif event == "return" and touched:
                            transport, original = touched[-1]
                            transport._quarantine_capacity_guard = original
                    if event == "call" and (
                        frame.f_code is stage_code
                        or frame.f_code.co_name == "begin_action_effect"
                    ):
                        if fault == "plain":
                            from review_sensei.errors import ReviewInputError

                            raise ReviewInputError(
                                "synthetic stop after plain capacity preflight"
                            )
                        entered.append(frame.f_code.co_name)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    status = fixture.invoke_command()
                    if fault in {
                        "plain-positive",
                        "plain-permit-seal",
                        "plain-candidate-receipt",
                    }:
                        self.assertEqual(status, 0, fixture.error)
                    else:
                        self.assertNotEqual(status, 0)
                finally:
                    sys.setprofile(previous)
                expected = (
                    "synthetic stop after plain capacity preflight"
                    if fault == "plain"
                    else "installed command tail has no complete lifecycle proof"
                    if fault == "unsupported"
                    else "installed capacity source changed"
                )
                if fault in {
                    "plain-positive",
                    "plain-permit-seal",
                    "plain-candidate-receipt",
                }:
                    self.assertEqual(
                        entered, ["begin_action_effect", "_stage_decision_event"]
                    )
                    self.assertEqual(
                        len(
                            fixture.selected.ledger._quarantine_authenticated.state[
                                "settlements"
                            ]
                        ),
                        1,
                    )
                else:
                    self.assertIn(expected, fixture.error)
                    self.assertEqual(entered, [])
                if fault == "plain-permit-seal":
                    self.assertEqual(len(sealed), 1)
                    self.assertIn(sealed[0], quarantine_action._DECISION_CANDIDATES)
                if fault == "plain-candidate-receipt":
                    from review_sensei_github.quarantine_lifecycle_transfer import (
                        _NAMESPACE_RESTORE,
                    )

                    self.assertEqual(len(receipt_checks), 1)
                    self.assertNotIn(fixture.selected.ledger, _NAMESPACE_RESTORE)
                self.assertEqual(len(touched), 1)
                self.assertEqual(old.to_dict(), original_history)


if __name__ == "__main__":
    unittest.main()


class PassiveBudgetObserverTests(unittest.TestCase):
    def test_budget_construction_fails_only_inside_scope_without_source_replacement(
        self,
    ):
        from test_action_run import forbid_replacement_phase_budget

        from review_sensei.bounded_evidence import EvidenceReadBudget

        original = EvidenceReadBudget.__init__
        before_profile, before_trace = sys.getprofile(), sys.gettrace()
        EvidenceReadBudget()
        with self.assertRaisesRegex(AssertionError, "no replacement phase budget"):
            with forbid_replacement_phase_budget():
                self.assertIs(EvidenceReadBudget.__init__, original)
                EvidenceReadBudget()
        self.assertIs(EvidenceReadBudget.__init__, original)
        self.assertIs(sys.getprofile(), before_profile)
        self.assertIs(sys.gettrace(), before_trace)
        EvidenceReadBudget()

    def test_existing_profile_observations_are_preserved(self):
        from test_action_run import forbid_replacement_phase_budget

        calls = []

        def target():
            return 1

        def previous(frame, event, arg):
            if frame.f_code is target.__code__ and event == "call":
                calls.append(event)

        original = sys.getprofile()
        try:
            sys.setprofile(previous)
            with forbid_replacement_phase_budget():
                self.assertEqual(target(), 1)
            self.assertIs(sys.getprofile(), previous)
        finally:
            sys.setprofile(original)
        self.assertEqual(calls, ["call"])
