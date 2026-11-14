"""Original source seals cannot be minted from supplied modules or late calls."""

import types
import unittest
from unittest.mock import patch

from review_sensei import disposition
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal, seal_module


class SourceRegistryTests(unittest.TestCase):
    def test_real_module_read_only_seal_and_late_registration_refusal(self):
        require_module_seal(disposition)
        with self.assertRaisesRegex(ReviewInputError, "defining module"):
            seal_module()

    def test_copied_module_or_late_callable_cannot_supply_original_source(self):
        copied = types.ModuleType(disposition.__name__)
        copied.__dict__.update(disposition.__dict__)
        with self.assertRaisesRegex(ReviewInputError, "unavailable"):
            require_module_seal(copied)
        with patch.object(disposition, "apply_session_command", lambda *a, **k: None):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(disposition)
        require_module_seal(disposition)

    def test_original_ledger_method_consumer_is_read_only_and_rejects_shadow(self):
        from review_sensei_github.review_run import ActionRunLedger, ReviewRunLedger
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        from review_sensei.source_registry import require_instance_source

        methods = ("load", "read_evidence", "replace", "evidence_producer")
        for cls in (GitHubIssueCommentSessionLedger, ActionRunLedger, ReviewRunLedger):
            # This checks executable source only, not initialized data authority.
            ledger = object.__new__(cls)
            require_instance_source(ledger, methods=methods)
            with patch.object(ledger, "load", lambda *a, **k: None):
                with self.assertRaisesRegex(
                    ReviewInputError, "instance method changed"
                ):
                    require_instance_source(ledger, methods=methods)

    def test_new_methods_and_lookup_behavior_cannot_join_original_class(self):
        from review_sensei_github.review_run import ActionRunLedger

        from review_sensei.source_registry import require_instance_source

        ledger = object.__new__(ActionRunLedger)
        for name in ("new_method", "__getattribute__", "__getattr__", "__setattr__"):
            with (
                self.subTest(name=name),
                patch.object(ActionRunLedger, name, lambda *a, **k: None, create=True),
            ):
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    require_instance_source(ledger, methods=(name,))
        require_instance_source(ledger, methods=("load",))

    def test_existing_dataclass_behavior_is_not_an_unsealed_dunder(self):
        from review_sensei import session

        with patch.object(session.SessionRecord, "__setattr__", lambda *a, **k: None):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(session)

    def test_class_consumer_ignores_unrelated_factory_but_module_consumer_does_not(
        self,
    ):
        from review_sensei_github import review_run

        from review_sensei.source_registry import require_instance_source

        ledger = object.__new__(review_run.ActionRunLedger)
        methods = ("load", "read_evidence", "replace", "evidence_producer")
        with patch.object(review_run, "GitHubHttp", lambda *a, **k: None):
            require_instance_source(ledger, methods=methods)
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(review_run)

    def test_module_validation_checks_each_class_binding_once_without_caching(self):
        import inspect

        from review_sensei_github import review_run
        from review_sensei_github.review_run import ActionRunLedger

        original = inspect.getattr_static
        observed = []

        def observe(owner, name, *args, **kwargs):
            if owner is ActionRunLedger and name == "load":
                observed.append(name)
            return original(owner, name, *args, **kwargs)

        with patch.object(inspect, "getattr_static", observe):
            require_module_seal(review_run)
        # One fresh static lookup serves membership and binding validation.
        # It must not repeat lookups or cache success across validations.
        self.assertEqual(observed, ["load"])
        with patch.object(ActionRunLedger, "load", lambda *args, **kwargs: None):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(review_run)
        require_module_seal(review_run)

    def test_default_binding_replacement_and_content_mutation_reject(self):
        from review_sensei_github import http

        function = http.GitHubHttp.paginate
        original = function.__kwdefaults__
        for replacement in (dict(original), {**original, "before_read": lambda: 30}):
            with patch.object(function, "__kwdefaults__", replacement):
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    require_module_seal(http)
        with patch.object(function, "__defaults__", (None,)):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(http)
        require_module_seal(http)

    def test_empty_default_fast_path_rechecks_both_current_attributes(self):
        function = disposition._require_reason
        self.assertIsNone(function.__defaults__)
        self.assertIsNone(function.__kwdefaults__)
        require_module_seal(disposition)
        for attribute, replacement in (
            ("__defaults__", ()),
            ("__defaults__", (None,)),
            ("__kwdefaults__", {}),
            ("__kwdefaults__", {"reason": None}),
        ):
            with self.subTest(attribute=attribute, value=replacement):
                with patch.object(function, attribute, replacement):
                    with self.assertRaisesRegex(ReviewInputError, "source changed"):
                        require_module_seal(disposition)
                require_module_seal(disposition)

    def test_original_mutable_defaults_cannot_be_cleared_or_mutated_in_place(self):
        from review_sensei_github import http

        function = http.GitHubHttp.paginate
        original = function.__kwdefaults__
        self.assertIsNone(function.__defaults__)
        self.assertIsNotNone(original)
        require_module_seal(http)
        with patch.object(function, "__kwdefaults__", None):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(http)
        previous = original["before_read"]
        try:
            original["before_read"] = lambda: None
            self.assertIs(function.__kwdefaults__, original)
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(http)
        finally:
            original["before_read"] = previous
        require_module_seal(http)

    def test_plain_function_cannot_acquire_a_forged_unwrap_target(self):
        function = disposition.apply_session_command
        self.assertFalse(hasattr(function, "__wrapped__"))
        with patch.object(function, "__wrapped__", lambda: None, create=True):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(disposition)
        require_module_seal(disposition)

    def test_original_wrapper_chain_binds_links_and_inner_source(self):
        import inspect

        from review_sensei_github import finding_lifecycle

        from review_sensei.source_registry import require_instance_source

        cls = finding_lifecycle.FindingLifecycleJournal
        function = cls.preflight_command_finalization
        original = function.__wrapped__
        inner = inspect.unwrap(function)
        journal = object.__new__(cls)
        methods = ("preflight_command_finalization",)
        require_module_seal(finding_lifecycle)
        require_instance_source(journal, methods=methods)
        copied = types.FunctionType(original.__code__, original.__globals__)
        for replacement in (copied, function):
            with (
                self.subTest(link=replacement),
                patch.object(function, "__wrapped__", replacement),
            ):
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    require_module_seal(finding_lifecycle)
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    require_instance_source(journal, methods=methods)
        for attribute, replacement in (
            ("__code__", inner.__code__.replace(co_name="forged_inner")),
            ("__defaults__", (object(),)),
            ("__kwdefaults__", {"forged": object()}),
        ):
            with self.subTest(attribute=attribute):
                previous = getattr(inner, attribute)
                try:
                    setattr(inner, attribute, replacement)
                    with self.assertRaisesRegex(ReviewInputError, "source changed"):
                        require_module_seal(finding_lifecycle)
                    with self.assertRaisesRegex(ReviewInputError, "source changed"):
                        require_instance_source(journal, methods=methods)
                finally:
                    setattr(inner, attribute, previous)
        require_module_seal(finding_lifecycle)
        require_instance_source(journal, methods=methods)
        self.assertIs(function.__wrapped__, original)
        self.assertIs(inspect.unwrap(function), inner)

    def test_stdlib_copy_preserves_original_class_and_slot_cache_is_pinned(self):
        import copy

        from review_sensei_github.publication import ReviewPublisher

        from review_sensei.source_registry import require_instance_source

        instance = object.__new__(ReviewPublisher)
        require_instance_source(instance, methods=())
        original = ReviewPublisher.__slotnames__
        clone = copy.copy(instance)
        self.assertIs(ReviewPublisher.__slotnames__, original)
        require_instance_source(instance, methods=())
        require_instance_source(clone, methods=())
        with patch.object(ReviewPublisher, "__slotnames__", list(original)):
            with self.assertRaisesRegex(ReviewInputError, "class source changed"):
                require_instance_source(instance, methods=())
        original.append("injected_slot")
        try:
            with self.assertRaisesRegex(ReviewInputError, "class source changed"):
                require_instance_source(instance, methods=())
        finally:
            original.pop()
        with patch.object(ReviewPublisher, "publish", lambda *a, **k: None):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_instance_source(instance, methods=())
        require_instance_source(instance, methods=())
