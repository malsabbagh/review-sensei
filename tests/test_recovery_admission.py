from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from review_sensei.errors import ReviewInputError
from review_sensei.execution import WorkExecution, execute_plan
from review_sensei.outcomes import ResourceBudgetTracker
from review_sensei.work_recovery import WorkRecoveryStore
from tests import test_work_recovery as support

NOW = support.NOW
Provider = support.Provider


class AdmissionStore(WorkRecoveryStore):
    def __init__(self, root, *, offset=0, artifacts="diagnostics"):
        super().__init__(
            Path(root),
            key=b"k" * 32,
            artifacts=artifacts,
            now=lambda: NOW + timedelta(seconds=offset),
        )
        self.admission_calls = 0
        self.legacy_calls = 0

    def load_admission(self, *args):
        self.admission_calls += 1
        return super().load_admission(*args)

    def load(self, *args):
        self.legacy_calls += 1
        return super().load(*args)


class RecoveryAdmissionTests(unittest.TestCase):
    def fixture(self):
        return support.WorkRecoveryTests().fixture()

    def run_plan(self, fixture, store, *, provider=None, render=None, semantic=None):
        plan, resource, budgets, original_render = fixture
        tracker = ResourceBudgetTracker(resource, monotonic=lambda: 1000.0)
        provider = provider or Provider()
        result = execute_plan(
            plan,
            provider=provider,
            render=render or original_render,
            validate=lambda response, batch: response.text,
            tracker=tracker,
            budgets=budgets,
            recovery=store,
            encode_recovery=lambda value: {"text": value},
            decode_recovery=lambda value, batch: value["text"],
            revalidate_cached=semantic or (lambda value, batch: value == "{}"),
        )
        return result, tracker, provider

    def test_authenticated_expired_budget_restores_before_render_and_reuses_cache(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            first, original, _ = self.run_plan(fixture, AdmissionStore(root))
            store = AdmissionStore(root, offset=121)
            observed = []

            def render(batch):
                self.assertEqual(store.admission_calls, 1)
                observed.append(batch.batch_id)
                return fixture[3](batch)

            validated = []
            resumed, tracker, provider = self.run_plan(
                fixture,
                store,
                render=render,
                semantic=lambda value, batch: (
                    validated.append(batch.batch_id) or value == "{}"
                ),
            )
            self.assertEqual(resumed.completed, first.completed)
            self.assertEqual(provider.calls, 0)
            self.assertEqual(tracker.execution_identity, original.execution_identity)
            self.assertEqual(tracker.elapsed_ms(), 121000)
            for name in ("provider_calls", "prompt_bytes", "response_bytes"):
                self.assertEqual(getattr(tracker, name), getattr(original, name))
            self.assertEqual(observed, validated)
            self.assertEqual(store.legacy_calls, 0)

    def test_expired_deadline_sensitive_render_precisely_refuses_without_dispatch(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            _, original, _ = self.run_plan(fixture, AdmissionStore(root))
            store = AdmissionStore(root, offset=121)
            tracker = ResourceBudgetTracker(fixture[1], monotonic=lambda: 1000.0)
            provider = Provider()

            def render(batch):
                self.assertEqual(
                    tracker.execution_identity, original.execution_identity
                )
                self.assertEqual(tracker.elapsed_ms(), 121000)
                raise ReviewInputError("deadline-dependent rendering refused")

            with self.assertRaisesRegex(
                ReviewInputError,
                "deadline exceeded before request binding; no dispatch",
            ):
                execute_plan(
                    fixture[0],
                    provider=provider,
                    render=render,
                    validate=lambda response, batch: response.text,
                    tracker=tracker,
                    budgets=fixture[2],
                    recovery=store,
                    encode_recovery=lambda value: {"text": value},
                    decode_recovery=lambda value, batch: value["text"],
                    revalidate_cached=lambda value, batch: True,
                )
            self.assertEqual(provider.calls, 0)
            self.assertEqual(store.legacy_calls, 0)
            self.assertEqual(tracker.provider_calls, original.provider_calls)

    def test_fresh_request_change_refuses_before_cached_validation_or_dispatch(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            self.run_plan(fixture, AdmissionStore(root))
            for change in ("prompt", "max_response_bytes", "model", "json_mode"):
                with self.subTest(change=change):
                    store = AdmissionStore(root, offset=1)
                    provider = Provider()
                    validated = []

                    def render(batch):
                        request = fixture[3](batch)
                        value = {
                            "prompt": request.prompt + " changed",
                            "max_response_bytes": request.max_response_bytes - 1,
                            "model": "changed-model",
                            "json_mode": not request.json_mode,
                        }[change]
                        return replace(request, **{change: value})

                    with self.assertRaisesRegex(
                        ReviewInputError, "work recovery request binding changed"
                    ):
                        self.run_plan(
                            fixture,
                            store,
                            provider=provider,
                            render=render,
                            semantic=lambda value, batch: validated.append(batch),
                        )
                    self.assertEqual(validated, [])
                    self.assertEqual(provider.calls, 0)
                    self.assertEqual(store.legacy_calls, 0)

    def test_provider_identity_change_refuses_after_early_admission(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            self.run_plan(fixture, AdmissionStore(root))
            provider = Provider()
            provider.model = "changed"
            with self.assertRaisesRegex(ReviewInputError, "request binding changed"):
                self.run_plan(fixture, AdmissionStore(root), provider=provider)
            self.assertEqual(provider.calls, 0)

    def test_absent_receipt_dispatches_once_without_second_load(self):
        with tempfile.TemporaryDirectory() as root:
            store = AdmissionStore(root)
            result, _, provider = self.run_plan(self.fixture(), store)
            self.assertEqual(len(result.completed), 2)
            self.assertEqual(provider.calls, 2)
            self.assertEqual((store.admission_calls, store.legacy_calls), (1, 0))

    def test_legacy_optional_protocol_fallback_remains_available(self):
        with tempfile.TemporaryDirectory() as root:
            store = AdmissionStore(root)
            store.load_admission = None
            self.run_plan(self.fixture(), store)
            self.assertEqual((store.admission_calls, store.legacy_calls), (0, 1))

    def test_disabled_diagnostics_do_not_load_admission_or_create_authority(self):
        with tempfile.TemporaryDirectory() as root:
            store = AdmissionStore(Path(root) / "absent", artifacts="none")
            _, _, provider = self.run_plan(self.fixture(), store)
            self.assertEqual((store.admission_calls, store.legacy_calls), (0, 0))
            self.assertEqual(provider.calls, 2)
            self.assertFalse(store.directory.exists())

    def test_invalid_authentication_refuses_before_any_render(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            self.run_plan(fixture, AdmissionStore(root))
            store = AdmissionStore(root)
            store._key = b"different synthetic key" * 2
            rendered = []
            provider = Provider()
            with self.assertRaisesRegex(ReviewInputError, "unauthenticated"):
                self.run_plan(
                    fixture,
                    store,
                    provider=provider,
                    render=lambda batch: rendered.append(batch),
                )
            self.assertEqual(rendered, [])
            self.assertEqual(provider.calls, 0)

    def test_terminal_rejection_does_not_gain_a_new_retry(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            tracker = ResourceBudgetTracker(fixture[1], monotonic=lambda: 1000.0)
            provider = Provider()

            def reject(response, batch):
                raise ReviewInputError("synthetic semantic rejection")

            first = execute_plan(
                fixture[0],
                provider=provider,
                render=fixture[3],
                validate=reject,
                tracker=tracker,
                budgets=fixture[2],
                recovery=AdmissionStore(root),
                encode_recovery=lambda value: {"text": value},
                decode_recovery=lambda value, batch: value["text"],
                revalidate_cached=lambda value, batch: True,
                repairable_validation=lambda error: False,
            )
            resumed, restored, restarted = self.run_plan(
                fixture, AdmissionStore(root, offset=1)
            )
            self.assertEqual(resumed.pending, first.pending)
            self.assertEqual(restarted.calls, 0)
            self.assertEqual(restored.provider_calls, tracker.provider_calls)

    def test_only_original_interrupted_pending_work_may_resume(self):
        fixture = self.fixture()
        for reason, calls in (("interrupted", 2), ("queued", 0)):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                store = AdmissionStore(root)
                self.run_plan(fixture, store)
                tracker = ResourceBudgetTracker(fixture[1], monotonic=lambda: 1000.0)
                _, digests = store.load_admission(
                    fixture[0], tracker, fixture[2], lambda value, batch: value["text"]
                )
                # A synthetic original zero-call diagnostic admission. The
                # historical diagnostic protocol resumes interrupted work only;
                # this does not reinterpret queued public checkpoint authority.
                tracker.provider_calls = 0
                tracker.prompt_bytes = 0
                tracker.response_bytes = 0
                pending = WorkExecution(
                    fixture[0],
                    (),
                    tuple((item.identity, reason) for item in fixture[0].requirements),
                    tracker.execution_identity,
                )
                store.save(pending, tracker, lambda value: {"text": value}, digests)
                resumed, restored, provider = self.run_plan(
                    fixture, AdmissionStore(root, offset=1)
                )
                self.assertEqual(provider.calls, calls)
                self.assertEqual(restored.provider_calls, calls)
                self.assertEqual(len(resumed.completed), calls)

    def test_missing_semantic_contract_refuses_before_render_or_admission(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            store = AdmissionStore(root)
            rendered = []
            with self.assertRaisesRegex(ReviewInputError, "normalized codecs"):
                execute_plan(
                    fixture[0],
                    provider=Provider(),
                    render=lambda batch: rendered.append(batch),
                    validate=lambda response, batch: response.text,
                    tracker=ResourceBudgetTracker(fixture[1]),
                    budgets=fixture[2],
                    recovery=store,
                )
            self.assertEqual(rendered, [])
            self.assertEqual(store.admission_calls, 0)

    def test_invalid_cached_semantics_at_original_deadline_cannot_dispatch(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            self.run_plan(fixture, AdmissionStore(root))
            resumed, tracker, provider = self.run_plan(
                fixture,
                AdmissionStore(root, offset=121),
                semantic=lambda value, batch: False,
            )
            self.assertEqual(resumed.completed, ())
            self.assertEqual(provider.calls, 0)
            self.assertEqual(tracker.elapsed_ms(), 121000)

    def test_checkpoint_path_never_uses_diagnostic_admission(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            store = AdmissionStore(root)
            with patch.object(store, "load_admission") as load:
                with self.assertRaisesRegex(
                    ReviewInputError, "cannot use diagnostic recovery authority"
                ):
                    execute_plan(
                        fixture[0],
                        provider=Provider(),
                        render=fixture[3],
                        validate=lambda response, batch: response.text,
                        tracker=ResourceBudgetTracker(fixture[1]),
                        budgets=fixture[2],
                        recovery=store,
                        checkpoint=object(),
                    )
                load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
