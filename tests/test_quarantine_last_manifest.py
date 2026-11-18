"""Pure negative-entry controls; no original LAST invocation is executed."""

import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_command_finalization as producer
from review_sensei_github import quarantine_last_manifest as source

from review_sensei.errors import ReviewInputError


class LastManifestTests(unittest.TestCase):
    def test_unissued_and_copyable_data_cannot_measure(self):
        for value in (object(), source.LastManifest(b"{}")):
            with self.assertRaises(ReviewInputError):
                source.measure(value)

    def test_replaced_producer_refuses_before_callback(self):
        calls = []
        with patch.object(
            producer, "measure_inline_branches", lambda *a, **k: calls.append(1)
        ):
            with self.assertRaises(ReviewInputError):
                source.measure(object())
        self.assertEqual(calls, [])

    def test_manifest_is_only_detached_data(self):
        value = source.LastManifest(b'{"admission":false}')
        self.assertEqual(value.payload, b'{"admission":false}')
        self.assertFalse(hasattr(value, "lease"))
        with self.assertRaises(ReviewInputError):
            producer.require_observation(value)

    def test_selector_rejects_bool_callback_before_observation(self):
        calls = []

        class Bomb:
            def __bool__(self):
                calls.append(True)
                raise AssertionError("bool invoked")

        for function in (
            producer.measure_inline_branches,
            producer._measure_inline_branches,
        ):
            with self.assertRaises(ReviewInputError):
                function(object(), _retain_manifest=Bomb())
        self.assertEqual(calls, [])

    def test_accounting_shadow_refuses_before_serialization(self):
        from review_sensei.api_work import ApiRunAccounting

        calls = []
        value = object.__new__(ApiRunAccounting)
        object.__setattr__(value, "to_document", lambda: calls.append(True))
        with self.assertRaises(ReviewInputError):
            source.accounting_bytes(value)
        self.assertEqual(calls, [])

    def test_nested_accounting_impostor_has_no_callbacks(self):
        from review_sensei.api_work import ApiRunAccounting

        calls = []

        class Bomb:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        value = object.__new__(ApiRunAccounting)
        for name in vars(ApiRunAccounting)["__dataclass_fields__"]:
            object.__setattr__(value, name, None)
        object.__setattr__(value, "action", {"nested": Bomb()})
        with self.assertRaises(ReviewInputError):
            source.accounting_bytes(value)
        self.assertEqual(calls, [])

    def test_inline_impostor_never_traverses_attributes(self):
        calls = []

        class Bomb:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        with self.assertRaises(ReviewInputError):
            source.require_inline_participants(Bomb(), Bomb())
        self.assertEqual(calls, [])

    def test_inline_owner_method_and_property_shadows_refuse_before_callback(self):
        from review_sensei_github import api_run_budget, api_run_requests, review_run

        calls = []
        for role, names in (
            (
                "ledger",
                (
                    "evidence_producer",
                    "preflight_finding_lifecycle_inline",
                    "preflight_finding_lifecycle_checkpoint_pricing",
                ),
            ),
            ("caller", ("accounting", "usable")),
            ("budget", ("preflight", "check", "ordinary_remaining")),
        ):
            for name in names:
                ledger = object.__new__(review_run.ActionRunLedger)
                caller = object.__new__(api_run_requests.ApiRunRequestTransport)
                budget = object.__new__(api_run_budget.CallerRunBudget)
                caller.__dict__["_budget_view"] = budget
                ledger.__dict__.update(caller=caller, evidence_budget=budget)
                budget.__dict__["caller"] = caller
                owner = {"ledger": ledger, "caller": caller, "budget": budget}[role]
                owner.__dict__[name] = lambda: calls.append(True)
                with self.assertRaisesRegex(ReviewInputError, "instance shadow"):
                    source.require_inline_participants(ledger, caller)
        self.assertEqual(calls, [])

    def test_inline_missing_original_state_refuses(self):
        from review_sensei_github import api_run_budget, api_run_requests, review_run

        ledger = object.__new__(review_run.ActionRunLedger)
        caller = object.__new__(api_run_requests.ApiRunRequestTransport)
        budget = object.__new__(api_run_budget.CallerRunBudget)
        caller.__dict__.update(_budget_view=budget, _state=None)
        ledger.__dict__.update(
            caller=caller, evidence_budget=budget, _quarantine_required=True
        )
        budget.__dict__.update(caller=caller, _ordinary_hold=0)
        with self.assertRaises(ReviewInputError):
            source.require_inline_participants(ledger, caller)

    def test_issued_shape_late_action_poison_refuses_before_full_observation(self):
        from review_sensei_github import api_run_budget, api_run_requests, review_run

        from review_sensei.api_work import ApiRunAccounting

        calls = []

        class Bomb:
            def get(self, *args):
                calls.append(True)
                raise AssertionError("action.get")

        run = object.__new__(review_run.ReviewRun)
        ledger = object.__new__(review_run.ActionRunLedger)
        caller = object.__new__(api_run_requests.ApiRunRequestTransport)
        budget = object.__new__(api_run_budget.CallerRunBudget)
        state = object.__new__(ApiRunAccounting)
        for name in vars(ApiRunAccounting)["__dataclass_fields__"]:
            object.__setattr__(state, name, None)
        object.__setattr__(state, "action", Bomb())
        run.__dict__.update(ledger=ledger, caller=caller)
        caller.__dict__.update(_budget_view=budget, _state=state, _block="ordinary")
        budget.__dict__.update(caller=caller, _ordinary_hold=0)
        ledger.__dict__.update(
            caller=caller,
            evidence_budget=budget,
            _quarantine_required=True,
            app_slug="app",
            _evidence_author_id=1,
        )
        token = producer.QuarantineFinalizerPreparation()
        observed = producer.QuarantineFinalizerObservation(b"{}", b"[]")
        # Issued-shaped data is only for negative ordering; it is NOT an issued
        # source preparation and must never reach full observation authority.
        producer._OBSERVATIONS[observed] = (token, b"{}", b"[]")
        producer._PREPARED[token] = {"run": run}
        try:
            with self.assertRaises(ReviewInputError):
                source.measure(observed)
        finally:
            producer._OBSERVATIONS.pop(observed)
            producer._PREPARED.pop(token)
        self.assertEqual(calls, [])

    def test_unused_ledger_export_cannot_hash_hostile_metaclass(self):
        from review_sensei_github import api_run_requests, review_run

        calls = []

        class Meta(type):
            def __hash__(cls):
                calls.append(True)
                raise AssertionError("class hash")

        class Bomb(metaclass=Meta):
            pass

        ledger = object.__new__(review_run.ActionRunLedger)
        caller = object.__new__(api_run_requests.ApiRunRequestTransport)
        with patch.object(review_run, "ReviewRunLedger", Bomb):
            with self.assertRaises(ReviewInputError):
                source.require_inline_participants(ledger, caller)
        self.assertEqual(calls, [])

    def test_preliminary_run_property_replacement_never_executes(self):
        from review_sensei_github import review_run

        calls = []
        run = object.__new__(review_run.ReviewRun)
        token = producer.QuarantineFinalizerPreparation()
        observed = producer.QuarantineFinalizerObservation(b"{}", b"[]")
        producer._OBSERVATIONS[observed] = (token, b"{}", b"[]")
        producer._PREPARED[token] = {"run": run}
        try:
            with patch.object(
                review_run.ReviewRun,
                "caller",
                property(lambda self: calls.append(True)),
                create=True,
            ):
                with self.assertRaises(ReviewInputError):
                    source.preliminary_participants(observed)
        finally:
            producer._OBSERVATIONS.pop(observed)
            producer._PREPARED.pop(token)
        self.assertEqual(calls, [])

    def test_late_record_serializer_shadow_never_executes(self):
        from review_sensei import session

        calls = []
        record = object.__new__(session.SessionRecord)
        record.__dict__["to_dict"] = lambda: calls.append(True)
        with self.assertRaises(ReviewInputError):
            source.record_bytes(record)
        self.assertEqual(calls, [])
