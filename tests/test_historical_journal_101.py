"""Closed C3 arithmetic/source refusal tests, not product qualification."""

import unittest
from unittest.mock import patch

from review_sensei_github import (
    finding_lifecycle,
    ordinary_publication_reads,
    publication,
)
from review_sensei_github import quarantine_command_finalization as finalization
from review_sensei_github import quarantine_historical_execution as execution

from review_sensei.errors import ReviewInputError


class HistoricalJournal101Tests(unittest.TestCase):
    def test_complete_success_and_stop_prefixes_keep_lifetime_minima(self):
        model = finalization.journal_c3_101_model()
        self.assertEqual(model["normal_calls"], 92)
        self.assertEqual(model["combined_loss_calls"], 100)
        self.assertEqual(model["ordinary_required"], 101)
        rows = {row["phase"]: row for row in model["local_headroom"]}
        for name, available, minimum in (
            ("pending-journal", 47, 36),
            ("review-update", 35, 35),
            ("finish-journal", 23, 15),
            ("receipt", 11, 10),
        ):
            self.assertEqual(
                rows[name], {"phase": name, "available": available, "minimum": minimum}
            )
        stop = model["put_stop"]
        self.assertEqual(stop["recovery_readback"], 1)
        self.assertEqual(stop["independent_finish_readback"], 1)
        self.assertEqual(stop["remaining"], 10)
        # Known rejection omits both reads, malformed/ambiguous response needs
        # one finish read, transient may need both. No branch replays the PUT.
        for readbacks in (0, 1, 2):
            self.assertGreaterEqual(35 - 1 - readbacks - 12 - 10, 10)

    def test_legacy_recipe_is_not_rewritten(self):
        stages = finalization._intermediate_stages(5, 2, 2, 0)
        self.assertEqual(dict(stages)["pending-journal"], 13)
        self.assertEqual(dict(stages)["finish-journal"], 13)
        self.assertEqual(sum(n for _, n in stages), 96)

    def test_foreign_journal_does_not_execute_callbacks(self):
        calls = []

        class Poison:
            def __getattribute__(self, key):
                calls.append(key)
                raise AssertionError(key)

        with self.assertRaises(ReviewInputError):
            execution.require_no_ordinary_witness(Poison())
        self.assertEqual(calls, [])

    def test_observation_impostor_refuses_before_witness_call(self):
        # Exact uninitialized source objects are only negative input fixtures;
        # they do not issue a lifecycle lease or claim positive custody.
        journal = object.__new__(finding_lifecycle.FindingLifecycleJournal)
        reviewer = object.__new__(publication.ReviewPublisher)
        journal.reviewer = reviewer
        reviewer._ordinary_publication_observation = object()
        with self.assertRaisesRegex(ReviewInputError, "ordinary recovery"):
            execution.require_no_ordinary_witness(journal)

    def test_replaced_witness_export_never_runs(self):
        calls = []
        with patch.object(
            ordinary_publication_reads,
            "issued_witness",
            lambda journal: calls.append(journal),
        ):
            with self.assertRaises(ReviewInputError):
                execution.require_no_ordinary_witness(object())
        self.assertEqual(calls, [])

    def test_unissued_observation_cannot_select_101(self):
        with self.assertRaises(ReviewInputError):
            finalization.require_c3_101_observation(object())

    def test_original_definition_seals_survive_normal_install(self):
        from review_sensei_github import api_run_requests, ordinary_native_recovery

        from review_sensei.source_registry import require_module_seal

        self.assertIsNotNone(api_run_requests.ApiRunRequestTransport)
        self.assertIsNotNone(ordinary_native_recovery._SOURCE)
        require_module_seal(ordinary_native_recovery)
        require_module_seal(ordinary_publication_reads)

    def test_one_short_retains_exact_put_boundary(self):
        model = finalization.journal_c3_101_model()
        rows = {row["phase"]: row for row in model["local_headroom"]}
        self.assertEqual(rows["review-update"]["available"] - 1, 34)
        self.assertLess(
            rows["review-update"]["available"] - 1, rows["review-update"]["minimum"]
        )

    def test_original_reservation_constant_tamper_refuses(self):
        from review_sensei_github import api_run_plan

        with patch.object(
            api_run_plan, "NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS", 33
        ):
            with self.assertRaises(ReviewInputError):
                finalization.journal_c3_101_model()

    def test_dispatch_entry_rejects_substituted_hook_before_callback(self):
        calls = []
        journal = object.__new__(finding_lifecycle.FindingLifecycleJournal)
        with patch.object(
            execution, "before_native_attempt", lambda *args: calls.append("fake")
        ):
            with self.assertRaises(ReviewInputError):
                finding_lifecycle.FindingLifecycleJournal._dispatch_effect(
                    journal, object(), {}, "PUT", "/unissued", token="", body={}
                )
        self.assertEqual(calls, [])

    def test_dispatch_checks_before_dynamic_native_attempt_import(self):
        import ast
        import inspect
        import textwrap

        source = ast.parse(
            textwrap.dedent(
                inspect.getsource(
                    finding_lifecycle.FindingLifecycleJournal._dispatch_effect
                )
            )
        )
        body = source.body[0].body
        guard = next(
            i
            for i, node in enumerate(body)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "before_native_attempt"
        )
        native_import = next(
            i
            for i, node in enumerate(body)
            if isinstance(node, ast.ImportFrom)
            and node.module == "ordinary_native_recovery"
        )
        self.assertLess(guard, native_import)

    def test_inactive_journal_hook_does_not_follow_fields(self):
        class Poison:
            def __getattribute__(self, name):
                raise AssertionError(name)

        execution.before_native_attempt(Poison(), None, None, None, None)

    def test_late_effects_container_never_invokes_index_callback(self):
        calls = []

        class Poison:
            def __getitem__(self, key):
                calls.append(key)
                return object()

            def __bool__(self):
                calls.append("bool")
                return True

        journal = object.__new__(finding_lifecycle.FindingLifecycleJournal)
        effect = {}
        for value in (Poison(), (), [], [object()]):
            journal.effects = value
            with self.assertRaises(ReviewInputError):
                execution.require_last_effect(journal, effect)
        journal.effects = [effect]
        execution.require_last_effect(journal, effect)
        self.assertEqual(calls, [])
