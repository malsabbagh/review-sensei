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
        # Ordinary classes use one fresh namespace snapshot, not repeated
        # per-member static lookups. The changed method below must still be
        # refused on the very next validation; no success is cached.
        self.assertEqual(observed, [])
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

    def test_hostile_export_refuses_without_dynamic_attribute_callbacks(self):
        from review_sensei_github import api_run_requests

        calls = []

        class Hostile:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError("replacement attribute callback")

        with patch.object(api_run_requests, "_AccountedResponse", Hostile()):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(api_run_requests)
        self.assertEqual(calls, [])
        require_module_seal(api_run_requests)

    def test_missing_export_does_not_invoke_module_getattr(self):
        calls = []

        def hostile(name):
            calls.append(name)
            raise AssertionError("module callback")

        original = disposition.apply_session_command
        try:
            del disposition.apply_session_command
            with patch.object(disposition, "__getattr__", hostile, create=True):
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    require_module_seal(disposition)
        finally:
            disposition.apply_session_command = original
        self.assertEqual(calls, [])
        require_module_seal(disposition)

    def test_module_subclass_refuses_without_callbacks(self):
        calls = []

        class HostileModule(types.ModuleType):
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError("module subclass callback")

        with self.assertRaisesRegex(ReviewInputError, "unavailable"):
            require_module_seal(HostileModule("hostile"))
        self.assertEqual(calls, [])

    def test_original_descriptor_and_bound_method_contracts_remain_valid(self):
        from review_sensei import session
        from review_sensei.source_registry import require_instance_source

        ledger = object.__new__(session.LocalSessionLedger)
        record = object.__new__(session.SessionRecord)
        self.assertIs(
            type(vars(session.LocalSessionLedger)["evidence_producer"]), staticmethod
        )
        self.assertIs(type(vars(session.SessionRecord)["from_dict"]), classmethod)
        require_module_seal(session)
        require_instance_source(ledger, methods=("load", "evidence_producer"))
        require_instance_source(record, methods=("from_dict",))

    def test_hostile_class_descriptor_refuses_without_callbacks(self):
        from review_sensei import session

        calls = []

        class Hostile:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError("descriptor attribute callback")

            def __get__(self, instance, owner=None):
                calls.append("descriptor get")
                raise AssertionError("descriptor callback")

        with patch.object(session.LocalSessionLedger, "evidence_producer", Hostile()):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(session)
        self.assertEqual(calls, [])
        require_module_seal(session)

    def test_hostile_export_type_equality_is_not_normalization(self):
        calls = []

        class HostileMeta(type):
            def __eq__(cls, other):
                calls.append("type equality")
                raise AssertionError("metaclass callback")

        class Hostile(metaclass=HostileMeta):
            pass

        with patch.object(disposition, "apply_session_command", Hostile()):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(disposition)
        self.assertEqual(calls, [])

    def test_hostile_module_name_is_not_hashed(self):
        calls = []

        class Hostile:
            def __hash__(self):
                calls.append("hash")
                raise AssertionError("name hash callback")

        with patch.object(disposition, "__name__", Hostile()):
            with self.assertRaisesRegex(ReviewInputError, "unavailable"):
                require_module_seal(disposition)
        self.assertEqual(calls, [])

    def test_mutated_default_refuses_without_hostile_callbacks(self):
        from review_sensei_github import http

        calls = []

        class HostileMeta(type):
            def __eq__(cls, other):
                calls.append("metaclass equality")
                raise AssertionError("default type callback")

            def __hash__(cls):
                calls.append("metaclass hash")
                raise AssertionError("default type hash")

        class Hostile(metaclass=HostileMeta):
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError("default attribute callback")

            def __repr__(self):
                calls.append("repr")
                raise AssertionError("default repr callback")

        function = http.GitHubHttp.paginate
        original = function.__kwdefaults__["before_read"]
        hostile = Hostile()
        for value in (hostile, [hostile], {"nested": hostile}):
            try:
                function.__kwdefaults__["before_read"] = value
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    require_module_seal(http)
            finally:
                function.__kwdefaults__["before_read"] = original
            self.assertEqual(calls, [])
            require_module_seal(http)

    def test_original_function_default_still_binds_exact_code(self):
        from review_sensei_github import http

        function = http.GitHubHttp.__init__
        original = function.__kwdefaults__["opener"]
        self.assertIs(type(original), types.FunctionType)
        require_module_seal(http)
        code = original.__code__
        try:
            original.__code__ = code.replace(co_name="changed_default")
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(http)
        finally:
            original.__code__ = code
        require_module_seal(http)

    def test_mutated_original_slot_name_refuses_without_equality_callback(self):
        from review_sensei_github import operation_host

        calls = []

        class Hostile:
            def __eq__(self, other):
                calls.append("slot equality")
                raise AssertionError("slot callback")

        slots = operation_host.AdmissionProof.__slotnames__
        self.assertTrue(slots)
        original = slots[0]
        require_module_seal(operation_host)
        try:
            slots[0] = Hostile()
            with self.assertRaisesRegex(ReviewInputError, "class source changed"):
                require_module_seal(operation_host)
        finally:
            slots[0] = original
        self.assertEqual(calls, [])
        require_module_seal(operation_host)


class FreshStaticMemberSnapshotTests(unittest.TestCase):
    def _snapshot_reader(self):
        # Exercise the private pure enumeration unit as well as the public seal
        # regressions below. It is closure-owned, not a replaceable module hook.
        closure = dict(
            zip(
                require_module_seal.__code__.co_freevars,
                (cell.cell_contents for cell in require_module_seal.__closure__),
            )
        )
        require_class = closure["require_class"]
        return dict(
            zip(
                require_class.__code__.co_freevars,
                (cell.cell_contents for cell in require_class.__closure__),
            )
        )["static_members"]

    def _reference(self, cls):
        import inspect

        def normalized(member):
            if (
                type(member) is types.MethodType
                or type(member) is staticmethod
                or type(member) is classmethod
            ):
                return member.__func__
            return member

        return {
            name: normalized(inspect.getattr_static(cls, name)) for name in dir(cls)
        }

    def _assert_same_members(self, cls):
        expected = self._reference(cls)
        actual = self._snapshot_reader()(cls)
        self.assertEqual(actual.keys(), expected.keys())
        for name, value in expected.items():
            self.assertIs(actual[name], value, name)
        return actual

    def test_exact_members_match_original_reader_for_current_source_classes(self):
        import importlib
        import inspect

        names = (
            "review_sensei.api_work",
            "review_sensei.session",
            "review_sensei.models",
            "review_sensei_github.http",
            "review_sensei_github.api_run_requests",
            "review_sensei_github.publication",
        )
        ordinary = unusual = 0
        for name in names:
            module = importlib.import_module(name)
            for value in vars(module).values():
                if inspect.isclass(value) and value.__module__ == name:
                    with self.subTest(module=name, cls=value.__name__):
                        self._assert_same_members(value)
                    ordinary += type(value) is type
                    unusual += type(value) is not type
            require_module_seal(module)
        self.assertGreater(ordinary, 0)
        self.assertGreater(unusual, 0)

    def test_inherited_descriptors_are_static_and_never_invoked(self):
        calls = []

        class Descriptor:
            def __get__(self, instance, owner=None):
                calls.append("descriptor")
                raise AssertionError("descriptor execution")

        class Base:
            descriptor = Descriptor()

            @staticmethod
            def static_method():
                raise AssertionError("static method execution")

            @classmethod
            def class_method(cls):
                raise AssertionError("class method execution")

            @property
            def property_member(self):
                calls.append("property")
                raise AssertionError("property execution")

        class Child(Base):
            __slots__ = ("slot",)

        members = self._assert_same_members(Child)
        self.assertIs(members["descriptor"], vars(Base)["descriptor"])
        self.assertIs(members["static_method"], Base.static_method)
        self.assertIs(members["class_method"], Base.class_method.__func__)
        self.assertIs(members["property_member"], vars(Base)["property_member"])
        self.assertIs(members["slot"], vars(Child)["slot"])
        self.assertEqual(calls, [])

    def test_every_snapshot_observes_namespace_and_mro_changes(self):
        class Base:
            def method(self):
                pass

        class Alternate:
            def method(self):
                pass

        class Child(Base):
            pass

        first = self._assert_same_members(Child)
        replacement = lambda self: None  # noqa: E731
        Base.method = replacement
        second = self._assert_same_members(Child)
        self.assertIs(second["method"], replacement)
        self.assertIsNot(second["method"], first["method"])
        Child.added = replacement
        self.assertIs(self._assert_same_members(Child)["added"], replacement)
        del Child.added
        self.assertNotIn("added", self._assert_same_members(Child))
        Child.__bases__ = (Alternate,)
        self.assertIs(self._assert_same_members(Child)["method"], Alternate.method)

    def test_custom_metaclass_keeps_exact_original_reader_and_callback_behavior(self):
        import inspect

        calls = []

        class Meta(type):
            def __getattribute__(cls, name):
                calls.append(("getattribute", name))
                return type.__getattribute__(cls, name)

            def __eq__(cls, other):
                calls.append(("equality", type(other)))
                return type.__eq__(cls, other)

            def __hash__(cls):
                calls.append(("hash",))
                return type.__hash__(cls)

        class Custom(metaclass=Meta):
            def method(self):
                pass

        expected = self._reference(Custom)
        original_callbacks = list(calls)
        calls.clear()
        looked_up = []
        original = inspect.getattr_static

        def observe(owner, name, *args, **kwargs):
            looked_up.append(name)
            return original(owner, name, *args, **kwargs)

        with patch.object(inspect, "getattr_static", observe):
            actual = self._snapshot_reader()(Custom)
        self.assertEqual(calls, original_callbacks)
        self.assertEqual(looked_up, list(expected))
        self.assertEqual(actual.keys(), expected.keys())
        for name, member in expected.items():
            self.assertIs(actual[name], member)

    def test_non_exact_string_namespace_key_keeps_original_reader(self):
        import inspect

        class Name(str):
            pass

        def method(self):
            pass

        cls = type("UnusualKeys", (), {Name("method"): method})
        self.assertTrue(any(type(key) is Name for key in vars(cls)))
        original = inspect.getattr_static
        looked_up = []

        def observe(owner, name, *args, **kwargs):
            looked_up.append(name)
            return original(owner, name, *args, **kwargs)

        with patch.object(inspect, "getattr_static", observe):
            actual = self._snapshot_reader()(cls)
        self.assertEqual(looked_up, dir(cls))
        self.assertIs(actual["method"], method)
        self._assert_same_members(cls)

    def test_replaced_property_accessor_code_is_still_checked(self):
        from review_sensei import api_work

        function = api_work.ApiRunAccounting.remaining.fget
        original = function.__code__
        require_module_seal(api_work)
        try:
            function.__code__ = original.replace(co_name="changed_property")
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(api_work)
        finally:
            function.__code__ = original
        require_module_seal(api_work)

    def test_replaced_descriptor_with_hostile_metaclass_never_gets_callbacks(self):
        from review_sensei import session

        calls = []

        class Meta(type):
            def __getattribute__(cls, name):
                calls.append("metaclass getattribute")
                raise AssertionError("metaclass attribute execution")

            def __eq__(cls, other):
                calls.append("metaclass equality")
                raise AssertionError("metaclass equality execution")

            def __hash__(cls):
                calls.append("metaclass hash")
                raise AssertionError("metaclass hash execution")

        class Descriptor(metaclass=Meta):
            def __get__(self, instance, owner=None):
                calls.append("descriptor")
                raise AssertionError("descriptor execution")

        with patch.object(session.SessionRecord, "from_dict", Descriptor()):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(session)
        self.assertEqual(calls, [])
        require_module_seal(session)
