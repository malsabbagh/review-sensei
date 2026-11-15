"""Inert source-custody controls, without product fixtures or native requests."""

import types
import unittest
from unittest.mock import patch

from review_sensei_github import api_run_requests as host

from review_sensei import finding_lifecycle_store as core
from review_sensei.errors import ReviewInputError


class CheckpointPricerSourceTests(unittest.TestCase):
    def test_registered_graph_and_custom_inherited_fallback(self):
        core._require_checkpoint_graph(core._CHECKPOINT_SOURCE)

        class Custom(host.ApiRunSessionLedger):
            pass

        owner = object.__new__(Custom)
        self.assertIsNone(owner.preflight_finding_lifecycle_checkpoint_pricing(None))

    def test_each_original_closure_cell_refuses_before_callback(self):
        calls = []

        def poison(*args, **kwargs):
            calls.append(True)
            raise AssertionError("callback")

        graph = core._CHECKPOINT_GRAPH
        for row in graph:
            for _name, cell, original in row[7]:
                cell.cell_contents = poison
                try:
                    with self.assertRaises(ReviewInputError):
                        core._require_checkpoint_graph(graph[2][0])
                finally:
                    cell.cell_contents = original
        self.assertEqual(calls, [])

    def test_inner_code_and_wrapper_link_refuse(self):
        method = core._CHECKPOINT_GRAPH[0][0]
        original = method.__code__
        method.__code__ = (lambda *a, **k: None).__code__
        try:
            with self.assertRaises(ReviewInputError):
                core._require_checkpoint_graph(core._CHECKPOINT_SOURCE)
        finally:
            method.__code__ = original
        with patch.object(core._CHECKPOINT_SOURCE, "__wrapped__", lambda: None):
            with self.assertRaises(ReviewInputError):
                core._require_checkpoint_graph(core._CHECKPOINT_SOURCE)

    def test_clone_with_substituted_closure_refuses(self):
        wrapper = core._CHECKPOINT_SOURCE
        calls = []

        def cell(value):
            return (lambda: value).__closure__[0]

        cells = tuple(
            cell(lambda *a, **k: calls.append(True)) if name == "method" else original
            for name, original in zip(wrapper.__code__.co_freevars, wrapper.__closure__)
        )
        clone = types.FunctionType(wrapper.__code__, wrapper.__globals__, closure=cells)
        clone.__kwdefaults__ = wrapper.__kwdefaults__
        with self.assertRaises(ReviewInputError):
            clone(object())
        self.assertEqual(calls, [])

    def test_later_cell_mutation_and_duplicate_install_refuse(self):
        core._require_checkpoint_graph(core._CHECKPOINT_SOURCE)
        row = core._CHECKPOINT_GRAPH[1]
        _name, cell, original = row[7][0]
        cell.cell_contents = object()
        try:
            with self.assertRaises(ReviewInputError):
                core._qualified_checkpoint_pricer(object())
        finally:
            cell.cell_contents = original
        with self.assertRaises(ReviewInputError):
            core._install_checkpoint_pricer(core._CHECKPOINT_SOURCE)

    def test_external_decorator_is_not_registered(self):
        calls = []

        @core._checkpoint_pricer
        def preflight_finding_lifecycle_checkpoint_pricing(owner, **kwargs):
            calls.append(True)

        with self.assertRaises(ReviewInputError):
            preflight_finding_lifecycle_checkpoint_pricing(object())
        self.assertEqual(calls, [])

    def test_global_support_replacement_refuses_before_callback(self):
        calls = []
        with patch.object(
            core, "_CHECKPOINT_SUPPORTS", lambda owner: calls.append(True)
        ):
            with self.assertRaises(ReviewInputError):
                core._qualified_checkpoint_pricer(object())
        self.assertEqual(calls, [])

    def test_corrupt_candidate_pair_refuses_before_install(self):
        calls = []
        wrapper = core._CHECKPOINT_SOURCE
        graph = core._CHECKPOINT_GRAPH
        for pair in (
            (lambda: calls.append(True), graph[1][0]),
            (graph[3][0], lambda owner: calls.append(True)),
            object(),
        ):
            with patch.dict(core._CHECKPOINT_CANDIDATES, {wrapper: pair}):
                with self.assertRaises(ReviewInputError):
                    core._checkpoint_candidate(wrapper, graph)
        self.assertEqual(calls, [])

    def test_malformed_graph_rows_never_invoke_callbacks(self):
        calls = []

        class Bomb:
            def __getitem__(self, key):
                calls.append(key)
                raise AssertionError(key)

            def __iter__(self):
                calls.append("iter")
                raise AssertionError("iter")

        wrapper = core._CHECKPOINT_SOURCE
        original = core._CHECKPOINT_GRAPH
        malformed = [Bomb(), (Bomb(),) * 4]
        row = original[0]
        malformed.append(
            (
                (row[0], row[1], row[2], row[3], row[4], row[5], row[6], (Bomb(),)),
                *original[1:],
            )
        )
        for graph in malformed:
            with patch.object(core, "_CHECKPOINT_GRAPH", graph):
                with self.assertRaises(ReviewInputError):
                    core._require_checkpoint_graph(wrapper)
                with self.assertRaises(ReviewInputError):
                    wrapper(object())
        self.assertEqual(calls, [])

    def test_direct_wrapper_rejects_replaced_shape_before_callback(self):
        calls = []
        wrapper = core._CHECKPOINT_SOURCE
        with patch.object(core, "_checkpoint_graph_shape", lambda: calls.append(True)):
            with self.assertRaises(ReviewInputError):
                wrapper(object())
            with self.assertRaises(ReviewInputError):
                core._require_checkpoint_graph(wrapper)
        self.assertEqual(calls, [])

    def test_original_inert_prospective_record_declines_without_io(self):
        owner = object.__new__(host.ApiRunSessionLedger)
        owner.caller = object.__new__(host.ApiRunRequestTransport)
        owner.caller._effect_record = object()
        owner._quarantine_required = True
        owner._quarantine_origin_reader = None
        self.assertIsNone(
            owner.preflight_finding_lifecycle_checkpoint_pricing(
                object(),
                added_parts=0,
                added_bytes=0,
                checkpoint_parts=(0,),
                checkpoint_inline=True,
            )
        )

    def test_all_registered_entries_reject_replaced_checker(self):
        calls = []
        wrapper = core._CHECKPOINT_SOURCE
        supported = core._CHECKPOINT_GRAPH[1][0]
        install = core._CHECKPOINT_GRAPH[3][0]
        with patch.object(
            core, "_require_checkpoint_graph", lambda *a, **k: calls.append(True)
        ):
            for entry in (
                lambda: core._qualified_checkpoint_pricer(object()),
                lambda: wrapper(object()),
                lambda: supported(object()),
                install,
            ):
                with self.assertRaises(ReviewInputError):
                    entry()
        self.assertEqual(calls, [])

    def test_definition_entries_reject_replaced_definition_helper(self):
        calls = []
        with patch.object(
            core, "_pricer_definition", lambda *a, **k: calls.append(True)
        ):
            with self.assertRaises(ReviewInputError):
                core._checkpoint_pricer(lambda: None)
            with self.assertRaises(ReviewInputError):
                core._install_checkpoint_pricer(core._CHECKPOINT_SOURCE)
        self.assertEqual(calls, [])

    def test_hostile_frame_refuses_without_attribute_callback(self):
        calls = []

        class Bomb:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        with self.assertRaises(ReviewInputError):
            core._require_checkpoint_graph(core._CHECKPOINT_SOURCE, frame=Bomb())
        self.assertEqual(calls, [])

    def test_original_decline_factory_expires_even_if_active_cell_reactivated(self):
        import sys

        owner = object.__new__(host.ApiRunSessionLedger)
        owner.caller = object.__new__(host.ApiRunRequestTransport)
        owner.caller._effect_record = object()
        owner._quarantine_required = True
        owner._quarantine_origin_reader = None
        record = object()
        captured = []
        original = core._CHECKPOINT_GRAPH[0][0]
        previous = sys.getprofile()

        def observe(frame, event, arg):
            if event == "call" and frame.f_code is original.__code__:
                captured.append(frame.f_locals["_checkpoint_factory"])
            if previous is not None:
                previous(frame, event, arg)

        sys.setprofile(observe)
        try:
            self.assertIsNone(
                owner.preflight_finding_lifecycle_checkpoint_pricing(
                    record,
                    added_parts=0,
                    added_bytes=0,
                    checkpoint_parts=(0,),
                    checkpoint_inline=True,
                )
            )
        finally:
            sys.setprofile(previous)
        self.assertEqual(len(captured), 1)
        issue = captured[0]
        with self.assertRaises(ReviewInputError):
            issue(owner, record)
        cells = dict(zip(issue.__code__.co_freevars, issue.__closure__))
        self.assertIs(cells["active"].cell_contents, False)
        cells["active"].cell_contents = True
        try:
            with self.assertRaises(ReviewInputError):
                issue(owner, record)
        finally:
            cells["active"].cell_contents = False
