"""Issuance negatives for the stopped route, without fabricated capabilities.

Actual native-route qualification is performed with the original retained-result
producer fixture. These tests never populate the private mode registry.
"""

import copy
import unittest
from types import SimpleNamespace

from review_sensei_github import ordinary_drift_execution as execution
from review_sensei_github import ordinary_publication_reads, publication

from review_sensei.errors import ReviewInputError


class NeverTransport:
    calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("unissued mode must not dispatch")


class OrdinaryDriftExecutionTests(unittest.TestCase):
    def unissued_observer(self):
        # Construction is deliberately not a live source context. Only the
        # original context manager may establish _ACTIVE and bound transports.
        ledger = object()
        journal = SimpleNamespace(
            ledger=ledger,
            identity=object(),
            operation_id="unissued",
            head_sha="a" * 40,
            base_sha="b" * 40,
        )
        observer = ordinary_publication_reads._Observation(object(), journal, object())
        observer.family = "drift"
        return observer

    def arguments(self):
        return dict(
            original_result=None,
            result=None,
            assembled=None,
            eligibility=None,
            marker=None,
            outcome=None,
            policy=None,
            complete_inventory=True,
            checks_enabled=True,
            grouped=False,
            planned_window=None,
            planned_drift=None,
            convergence_policy=None,
            auto_approval=False,
            preserve_explanations=False,
            window_non_inline_blocking=False,
        )

    def test_actual_installer_cannot_be_replayed_or_supplied(self):
        self.assertIsNotNone(execution._SOURCE)
        for cls, query in (
            (publication.ReviewPublisher, publication._REVIEW_THREADS_QUERY),
            (object, publication._REVIEW_THREADS_QUERY),
            (publication.ReviewPublisher, "query ReviewThreads() {}"),
        ):
            with (
                self.subTest(cls=cls, query=query),
                self.assertRaises(ReviewInputError),
            ):
                execution._install_sources(cls, query)

    def test_checkpoint_installer_rejects_direct_and_foreign_registration(self):
        from review_sensei_github.finding_lifecycle import FindingLifecycleJournal

        for cls in (FindingLifecycleJournal, object):
            with self.subTest(cls=cls), self.assertRaises(ReviewInputError):
                execution._install_checkpoint_source(cls)

    def test_constructed_and_copied_observers_cannot_issue(self):
        observer = self.unissued_observer()
        for candidate in (observer, copy.copy(observer)):
            with self.subTest(copied=candidate is not observer):
                with self.assertRaises(ReviewInputError):
                    execution.prepare_current(
                        candidate.publisher,
                        candidate,
                        candidate.journal,
                        **self.arguments(),
                    )
                self.assertNotIn(candidate, execution._MODES)

    def test_supplied_preparation_is_not_an_issuer_parameter(self):
        observer = self.unissued_observer()
        with self.assertRaises(TypeError):
            execution.prepare_current(
                observer.publisher,
                observer,
                observer.journal,
                prepared={"success_frame_bound": True, "quote": {}},
                **self.arguments(),
            )
        self.assertNotIn(observer, execution._MODES)

    def test_unissued_request_stays_with_original_transport_owner(self):
        observer = self.unissued_observer()
        transport = NeverTransport()
        for method, path, kwargs in (
            ("GET", "/repos/owner/repo/pulls/7", {}),
            ("POST", "/repos/owner/repo/pulls/7/reviews", {"body": {}}),
        ):
            self.assertEqual(
                execution.handle_request(observer, transport, method, path, kwargs),
                (False, None),
            )
        self.assertEqual(transport.calls, 0)
        self.assertIsNone(
            execution.suffix_native_calls(
                observer, planned=None, prepared=(), requests=()
            )
        )
        self.assertFalse(execution.empty_cleanup(observer))

    def test_unissued_finish_stop_and_close_grant_nothing(self):
        observer = self.unissued_observer()
        execution.stop(observer, RuntimeError("nothing dispatched"))
        with execution.finishing(observer):
            self.assertNotIn(observer, execution._MODES)
        execution.close(observer)
        self.assertNotIn(observer, execution._MODES)

    def test_optional_generic_none_and_nonweakref_values_are_noops(self):
        for observer in (None, object(), 1, {}):
            with self.subTest(observer=observer):
                self.assertEqual(
                    execution.handle_request(
                        observer, NeverTransport(), "GET", "/", {}
                    ),
                    (False, None),
                )
                self.assertIsNone(
                    execution.suffix_native_calls(
                        observer, planned=None, prepared=(), requests=()
                    )
                )
                self.assertFalse(execution.empty_cleanup(observer))
                execution.stop(observer)
                with execution.finishing(observer):
                    pass
                execution.close(observer)

    def test_snapshot_pins_nested_values_not_mutable_model_references(self):
        value = {"record": b"root", "requests": [("PATCH", {"body": "old"})]}
        snapshot = execution._snapshot(value)
        self.assertEqual(snapshot, execution._snapshot(copy.deepcopy(value)))
        value["requests"][0][1]["body"] = "new"
        self.assertNotEqual(snapshot, execution._snapshot(value))

    def test_snapshot_rejects_callback_objects_without_invocation(self):
        calls = []

        class Supplied:
            def __iter__(self):
                calls.append("iter")
                return iter(())

            def to_dict(self):
                calls.append("codec")
                return {}

        with self.assertRaises(ReviewInputError):
            execution._snapshot({"request": Supplied()})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
