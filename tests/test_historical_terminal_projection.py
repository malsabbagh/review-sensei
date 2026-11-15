"""DATA conservation only; no original invocation or execution grant exercised."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import historical_terminal_fence as fence
from review_sensei_github import historical_terminal_projection as projection
from review_sensei_github import trust_effects

from review_sensei.api_work import (
    MAX_API_REQUEST_LIMIT,
    ApiRunAccounting,
    PendingApiRequest,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from tests.test_api_work import accounting


class OriginalAccountingGraphTests(unittest.TestCase):
    def graph(self, value, *, models=True):
        from review_sensei_github.guarded_pending_publication import (
            _original_accounting_graph,
        )

        return _original_accounting_graph(value, models=models)

    def test_every_original_model_field_is_visited_without_changing_accounting(self):
        from dataclasses import fields

        value = state()
        original = canonical_bytes(value.to_document())
        self.graph(value)
        self.assertEqual(canonical_bytes(value.to_document()), original)
        models = {}

        def collect(item):
            if type(item) in (tuple, list):
                for child in item:
                    collect(child)
            elif hasattr(type(item), "__dataclass_fields__"):
                models.setdefault(type(item), item)
                for field in fields(item):
                    collect(getattr(item, field.name))

        collect(value)
        self.assertEqual(len(models), 8)
        for model, instance in models.items():
            for field in fields(model):
                with self.subTest(model=model.__name__, field=field.name):
                    before = vars(instance)[field.name]
                    try:
                        object.__setattr__(instance, field.name, object())
                        with self.assertRaises(ReviewInputError):
                            self.graph(value)
                    finally:
                        object.__setattr__(instance, field.name, before)
        self.graph(value)
        self.assertEqual(canonical_bytes(value.to_document()), original)

    def test_mutable_dataclass_metadata_cannot_hide_a_hostile_field(self):
        item = PendingApiRequest("pending", 1)
        object.__setattr__(item, "request_id", object())
        metadata = PendingApiRequest.__dataclass_fields__
        with patch.dict(
            metadata, {"deadline_unix_ms": metadata["deadline_unix_ms"]}, clear=True
        ):
            with self.assertRaises(ReviewInputError):
                self.graph(item)

    def test_hostile_type_and_source_replacement_never_receive_callbacks(self):
        calls = []

        class Meta(type):
            def __eq__(cls, other):
                calls.append("equality")
                return False

            def __hash__(cls):
                calls.append("hash")
                return 0

        class Foreign(metaclass=Meta):
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        for models in (False, True):
            with self.assertRaises(ReviewInputError):
                self.graph({"nested": [Foreign()]}, models=models)
        value = state()
        self.graph(value)
        with patch.object(
            PendingApiRequest, "__post_init__", lambda self: calls.append("source")
        ):
            with self.assertRaises(ReviewInputError):
                self.graph(value)
        self.graph(value)
        self.assertEqual(calls, [])

    def test_original_node_depth_and_cycle_bounds_remain_enforced(self):
        cycle = []
        cycle.append(cycle)
        deep = None
        for _ in range(34):
            deep = [deep]
        for value in (cycle, deep, [None] * 20000):
            with self.assertRaises(ReviewInputError):
                self.graph(value, models=False)
        self.graph([None] * 19999, models=False)
        with self.assertRaises(ReviewInputError):
            self.graph(state(), models=False)


def state():
    value = accounting(limit=512).reserve("old", 3)
    value = value.dispatch("old", PendingApiRequest("old-unknown", 99))
    value = value.dispatch("old", PendingApiRequest("old-confirmed", 100))
    value = value.confirm(PendingApiRequest("old-confirmed", 100))
    return (
        value.reserve("ordinary", 105).reserve("terminal", 50).reserve("successor", 190)
    )


def project(value, sequence=80):
    return ApiRunAccounting.from_document(
        projection.terminal_accounting_data(
            value, ordinary="ordinary", terminal="terminal", sequence=sequence
        )
    )


class HistoricalTerminalProjectionTests(unittest.TestCase):
    def test_full_funding_and_old_liabilities_preserved(self):
        original = state()
        result = project(original)
        blocks = {b.block_id: b for b in result.blocks}
        self.assertEqual(result.remaining, original.remaining)
        self.assertEqual(result.configuration, original.configuration)
        self.assertEqual(blocks["old"], original.blocks[0])
        self.assertEqual(blocks["successor"], original.blocks[-1])
        self.assertEqual(blocks["terminal"].undispatched, 34)
        self.assertEqual(len(blocks["terminal"].pending), 16)
        self.assertEqual(len(blocks["ordinary"].pending), 105)
        self.assertEqual(result.uncertain, original.uncertain + 121)
        original.require_successor(result)

    def test_all_terminal_prefixes_and_mixed_completions_fit_data_upper(self):
        for sequence in (15, 255, 4095, MAX_API_REQUEST_LIMIT - 121):
            original = state()
            upper = project(original, sequence)
            ordinary = original
            for index in range(105):
                request = PendingApiRequest(
                    f"c-0-{sequence + index + 1:x}", MAX_API_REQUEST_LIMIT
                )
                ordinary = ordinary.dispatch("ordinary", request)
                if index % 3:
                    ordinary = ordinary.confirm(request)
            for prefix in range(17):
                candidate = ordinary
                for index in range(prefix):
                    request = PendingApiRequest(
                        f"c-0-{sequence + 106 + index:x}", MAX_API_REQUEST_LIMIT
                    )
                    candidate = candidate.dispatch("terminal", request)
                    if index % 2:
                        candidate = candidate.confirm(request)
                self.assertLessEqual(
                    len(canonical_bytes(candidate.to_document())),
                    len(canonical_bytes(upper.to_document())),
                )
                self.assertEqual(candidate.blocks[0], original.blocks[0])
                self.assertEqual(candidate.blocks[-1], original.blocks[-1])
                self.assertEqual(candidate.remaining, original.remaining)

    def test_wrong_tranche_config_namespace_refuses(self):
        original = state()
        for kwargs in (
            {"ordinary": "absent"},
            {"terminal": "ordinary"},
            {"sequence": True},
            {"sequence": MAX_API_REQUEST_LIMIT - 120},
        ):
            arguments = dict(ordinary="ordinary", terminal="terminal", sequence=80)
            arguments.update(kwargs)
            with self.assertRaises(ReviewInputError):
                projection.terminal_accounting_data(original, **arguments)
        for altered in (
            replace(
                original, configuration=replace(original.configuration, limit=1024)
            ),
            original.dispatch("terminal", PendingApiRequest("already-terminal", 1)),
        ):
            with self.assertRaises(ReviewInputError):
                project(altered)

    def test_hostile_nested_type_refuses_without_callbacks(self):
        calls = []

        class Meta(type):
            def __eq__(cls, other):
                calls.append("eq")
                return False

        class Poison(metaclass=Meta):
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        original = state()
        object.__setattr__(original.blocks[0].pending[0], "request_id", Poison())
        with self.assertRaisesRegex(ReviewInputError, "not original"):
            project(original)
        self.assertEqual(calls, [])

    def test_foreign_source_never_traverses_supplied_binding(self):
        calls = []

        class Poison:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        for invoke in (
            lambda: projection.prepare(Poison(), Poison()),
            lambda: projection._derive(Poison()),
            lambda: projection.require_activation(Poison(), Poison()),
            lambda: fence.require_projection_shape(Poison()),
            lambda: fence.require_active(Poison(), Poison()),
        ):
            with self.assertRaises(ReviewInputError):
                invoke()
        self.assertEqual(calls, [])

    def test_changed_source_profile_refuses_without_callback(self):
        calls = []

        def changed(*args):
            calls.append(args)

        with patch.object(trust_effects, "verification_profile", changed):
            with self.assertRaisesRegex(ReviewInputError, "trust source changed"):
                fence._zero_fences(object())
        self.assertEqual(calls, [])

    def test_replaced_accounting_graph_refuses_without_callback(self):
        from review_sensei_github import guarded_pending_publication

        calls = []
        original = state()

        def changed(*args, **kwargs):
            calls.append((args, kwargs))

        with patch.object(
            guarded_pending_publication, "_original_accounting_graph", changed
        ):
            with self.assertRaises(ReviewInputError):
                project(original)
        self.assertEqual(calls, [])

    def test_representative_ordinary_prefixes_and_completion_patterns(self):
        original = state()
        upper_bytes = len(canonical_bytes(project(original).to_document()))
        for prefix in (0, 1, 9, 10, 99, 104, 105):
            for pattern in ("none", "all", "mixed"):
                candidate = original
                for index in range(prefix):
                    request = PendingApiRequest(
                        f"c-0-{81 + index:x}", MAX_API_REQUEST_LIMIT
                    )
                    candidate = candidate.dispatch("ordinary", request)
                    if pattern == "all" or (pattern == "mixed" and index % 2):
                        candidate = candidate.confirm(request)
                for index in range(16):
                    request = PendingApiRequest(
                        f"c-0-{81 + prefix + index:x}", MAX_API_REQUEST_LIMIT
                    )
                    candidate = candidate.dispatch("terminal", request)
                    if pattern == "all" or (pattern == "mixed" and index % 2):
                        candidate = candidate.confirm(request)
                self.assertLessEqual(
                    len(canonical_bytes(candidate.to_document())), upper_bytes
                )
                self.assertEqual(candidate.blocks[0], original.blocks[0])
                self.assertEqual(candidate.blocks[-1], original.blocks[-1])
                self.assertEqual(candidate.remaining, original.remaining)

    def test_run_data_source_ignores_unrelated_constructor_alias(self):
        # Source-only object: no initialized execution/run custody is claimed.
        from copy import copy

        from review_sensei_github import review_run

        from review_sensei.source_registry import (
            require_instance_source,
            require_original_class_identity,
        )

        run = object.__new__(review_run.ReviewRun)
        calls = []

        def replacement(*args, **kwargs):
            calls.append((args, kwargs))

        with patch.object(review_run, "ApiRunRequestTransport", replacement):
            require_original_class_identity(run, module=review_run, name="ReviewRun")
            require_instance_source(run, methods=())
        for candidate in (run, copy(run), object()):
            # Class identity alone never supplies original activation/lifetime.
            with self.assertRaisesRegex(ReviewInputError, "original source"):
                projection.require_activation(candidate, {})
            with self.assertRaisesRegex(ReviewInputError, "original activation"):
                fence.require_active(candidate, object())
        self.assertEqual(calls, [])

    def test_run_data_export_and_class_changes_refuse_without_callback(self):
        from review_sensei_github import review_run

        from review_sensei.source_registry import (
            require_instance_source,
            require_original_class_identity,
        )

        cls = review_run.ReviewRun
        run = object.__new__(cls)
        calls = []

        class Poison:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        with patch.object(review_run, "ReviewRun", Poison()):
            with self.assertRaisesRegex(ReviewInputError, "class identity changed"):
                require_original_class_identity(
                    run, module=review_run, name="ReviewRun"
                )

        class Subclass(cls):
            pass

        for candidate in (object.__new__(Subclass), Poison()):
            with self.assertRaises(ReviewInputError):
                require_original_class_identity(
                    candidate, module=review_run, name="ReviewRun"
                )

        def changed(*args):
            calls.append(args)

        for name in ("retained_analysis", "__getattribute__", "__setattr__"):
            with patch.object(cls, name, changed, create=True):
                require_original_class_identity(
                    run, module=review_run, name="ReviewRun"
                )
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    require_instance_source(run, methods=())
        self.assertEqual(calls, [])
