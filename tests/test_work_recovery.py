from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.execution import execute_plan
from review_sensei.models import ProviderResponse
from review_sensei.outcomes import ResourceBudgetTracker
from review_sensei.planning import plan_work
from review_sensei.work_recovery import WorkRecoveryStore
from tests import test_review_work

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class Provider:
    name = "fixture"
    model = "fixture"

    def __init__(self):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ProviderResponse("{}", self.name)


class WorkRecoveryTests(unittest.TestCase):
    def test_restoration_uses_one_tick_and_preserves_current_elapsed_time(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            store = WorkRecoveryStore(
                Path(root), key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW
            )
            self.run_plan(plan, resource, budgets, render, Provider(), store)
            manifest = json.loads(next(Path(root).glob("*.json")).read_text())[
                "document"
            ]["request_digests"]
            ticks = iter((10000.0, 10010.0, 10012.0, 10012.0))
            tracker = ResourceBudgetTracker(resource, monotonic=lambda: next(ticks))
            resumed = WorkRecoveryStore(
                Path(root),
                key=b"k" * 32,
                artifacts="diagnostics",
                now=lambda: NOW + timedelta(seconds=1),
            )
            resumed.load(
                plan, tracker, budgets, lambda value, batch: value["text"], manifest
            )
            self.assertEqual(tracker.elapsed_ms(), 12000)
            self.assertEqual(tracker.provider_calls, 2)

    def test_restart_does_not_refresh_the_original_recovery_expiry(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            provider = Provider()
            for offset in (0, 5):
                self.run_plan(
                    plan,
                    resource,
                    budgets,
                    render,
                    provider,
                    WorkRecoveryStore(
                        Path(root),
                        key=b"k" * 32,
                        artifacts="diagnostics",
                        now=lambda: NOW + timedelta(hours=offset),
                    ),
                )
            with self.assertRaises(ReviewInputError):
                self.run_plan(
                    plan,
                    resource,
                    budgets,
                    render,
                    provider,
                    WorkRecoveryStore(
                        Path(root),
                        key=b"k" * 32,
                        artifacts="diagnostics",
                        now=lambda: NOW + timedelta(hours=7),
                    ),
                )
            self.assertEqual(provider.calls, 2)

    def test_restart_does_not_retry_a_terminal_semantic_rejection(self):
        plan, resource, budgets, render = self.fixture()
        plan = plan_work(
            "reassessment",
            plan.bundle,
            plan.requirements[:1],
            budgets=budgets,
            render=render,
            authority_digest="f" * 64,
        )

        def reject(response, batch):
            raise ReviewInputError("synthetic invalid finding identity")

        with tempfile.TemporaryDirectory() as root:
            store = WorkRecoveryStore(
                Path(root), key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW
            )
            provider = Provider()
            for _ in range(2):
                result = execute_plan(
                    plan,
                    provider=provider,
                    render=render,
                    validate=reject,
                    tracker=ResourceBudgetTracker(resource),
                    budgets=budgets,
                    recovery=store,
                    repairable_validation=lambda error: False,
                    encode_recovery=lambda value: {"text": value},
                    decode_recovery=lambda value, batch: value["text"],
                    revalidate_cached=lambda value, batch: value == "{}",
                )
                self.assertEqual(result.pending, (("0", "invalid_provider_output"),))
            self.assertEqual(provider.calls, 1)

    def test_changed_provider_identity_or_key_never_reuses_or_dispatches(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            store = WorkRecoveryStore(
                Path(root), key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW
            )
            self.run_plan(plan, resource, budgets, render, Provider(), store)
            changed = Provider()
            changed.model = "another-model"
            with self.assertRaises(ReviewInputError):
                self.run_plan(plan, resource, budgets, render, changed, store)
            self.assertEqual(changed.calls, 0)
            changed = Provider()
            with self.assertRaises(ReviewInputError):
                self.run_plan(
                    plan,
                    resource,
                    budgets,
                    render,
                    changed,
                    WorkRecoveryStore(
                        Path(root),
                        key=b"x" * 32,
                        artifacts="diagnostics",
                        now=lambda: NOW,
                    ),
                )
            self.assertEqual(changed.calls, 0)

    def test_deeply_nested_and_oversized_artifacts_fail_with_bounded_diagnostic(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            store = WorkRecoveryStore(
                Path(root), key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW
            )
            path = Path(root) / (plan.plan_id + ".json")
            for raw in (
                ("[" * 1500 + "0" + "]" * 1500).encode(),
                b"x" * (2 * 1024 * 1024 + 1),
            ):
                path.write_bytes(raw)
                provider = Provider()
                with self.assertRaisesRegex(
                    ReviewInputError, "expired, unauthenticated or incompatible"
                ):
                    self.run_plan(plan, resource, budgets, render, provider, store)
                self.assertEqual(provider.calls, 0)

    def test_aggregate_store_limits_do_not_create_a_ninth_artifact(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            for index in range(8):
                (Path(root) / (f"{index:064x}" + ".json")).write_text("{}")
            provider = Provider()
            with self.assertRaisesRegex(ReviewInputError, "storage budget exhausted"):
                self.run_plan(
                    plan,
                    resource,
                    budgets,
                    render,
                    provider,
                    WorkRecoveryStore(
                        Path(root),
                        key=b"k" * 32,
                        artifacts="diagnostics",
                        now=lambda: NOW,
                    ),
                )
            self.assertEqual(provider.calls, 0)
            self.assertEqual(len(list(Path(root).glob("*.json"))), 8)

    def test_actual_child_process_interruption_charges_dispatch_before_resume(self):
        plan, resource, budgets, render = self.fixture()
        code = """
import importlib, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
fixture = importlib.import_module(sys.argv[2])
WorkRecoveryTests, Provider, NOW = fixture.WorkRecoveryTests, fixture.Provider, fixture.NOW
from review_sensei.work_recovery import WorkRecoveryStore
def interrupted(self, request):
    os._exit(23)
Provider.complete = interrupted
test = WorkRecoveryTests()
plan, resource, budgets, render = test.fixture()
test.run_plan(plan, resource, budgets, render, Provider(), WorkRecoveryStore(Path(sys.argv[1]), key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW))
"""
        with tempfile.TemporaryDirectory() as root:
            child = subprocess.run(
                [sys.executable, "-c", code, root, __name__],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
            )
            self.assertEqual(child.returncode, 23)
            provider = Provider()
            work, tracker = self.run_plan(
                plan,
                resource,
                budgets,
                render,
                provider,
                WorkRecoveryStore(
                    Path(root), key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW
                ),
            )
            self.assertEqual(provider.calls, 1)
            self.assertEqual(tracker.provider_calls, 2)
            self.assertEqual(len(work.completed), 1)
            self.assertEqual(work.pending_ids, ("1",))

    def fixture(self):
        bundle, requirements, resource, budgets, render = (
            test_review_work.SharedWorkTests().fixture()
        )
        plan = plan_work(
            "reassessment",
            bundle,
            requirements,
            budgets=budgets,
            render=render,
            authority_digest="f" * 64,
        )
        return plan, resource, budgets, render

    def run_plan(self, plan, resource, budgets, render, provider, store):
        tracker = ResourceBudgetTracker(resource)
        result = execute_plan(
            plan,
            provider=provider,
            render=render,
            validate=lambda response, batch: response.text,
            tracker=tracker,
            budgets=budgets,
            recovery=store,
            encode_recovery=lambda value: {"text": value},
            decode_recovery=lambda value, batch: value["text"],
            revalidate_cached=lambda value, batch: value == "{}",
        )
        return result, tracker

    def test_restart_reuses_receipts_without_resetting_calls_or_deadline(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            store = WorkRecoveryStore(
                Path(root), key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW
            )
            provider = Provider()
            first, tracker = self.run_plan(
                plan, resource, budgets, render, provider, store
            )
            self.assertEqual(provider.calls, 2)
            restarted = WorkRecoveryStore(
                Path(root),
                key=b"k" * 32,
                artifacts="diagnostics",
                now=lambda: NOW + timedelta(seconds=30),
            )
            resumed, restored = self.run_plan(
                plan, resource, budgets, render, provider, restarted
            )
            self.assertEqual(provider.calls, 2)
            self.assertEqual(restored.provider_calls, tracker.provider_calls)
            self.assertGreaterEqual(restored.elapsed_ms(), 30000)
            self.assertEqual(resumed.completed, first.completed)

    def test_default_artifacts_off_writes_nothing(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "not-created"
            self.run_plan(
                plan,
                resource,
                budgets,
                render,
                Provider(),
                WorkRecoveryStore(directory, key=b"k" * 32),
            )
            self.assertFalse(directory.exists())

    def test_authentication_and_expiration_reject_modified_receipts(self):
        plan, resource, budgets, render = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            store = WorkRecoveryStore(
                directory, key=b"k" * 32, artifacts="diagnostics", now=lambda: NOW
            )
            self.run_plan(plan, resource, budgets, render, Provider(), store)
            path = next(directory.glob("*.json"))
            original = path.read_text()
            value = json.loads(original)
            value["document"]["counters"]["provider_calls"] = 0
            path.write_text(json.dumps(value))
            provider = Provider()
            with self.assertRaises(ReviewInputError):
                self.run_plan(plan, resource, budgets, render, provider, store)
            self.assertEqual(provider.calls, 0)
            path.write_text(original)
            expired = WorkRecoveryStore(
                directory,
                key=b"k" * 32,
                artifacts="diagnostics",
                now=lambda: NOW + timedelta(hours=7),
            )
            with self.assertRaises(ReviewInputError):
                self.run_plan(plan, resource, budgets, render, provider, expired)


if __name__ == "__main__":
    unittest.main()
