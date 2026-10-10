"""Opt-in admission/charge coalescing through complete durable receipt seams."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from review_sensei.assessment_queue import (
    AssessmentCheckpoint,
    AssessmentQueueHostAdapter,
)
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ReviewInputError
from review_sensei.execution import CheckpointMutation
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from tests.test_assessment_queue import DocumentStore, QueueFixture, fixture
from tests.test_review_work import AssessingProvider


class CoalescedAdmissionTests(QueueFixture, unittest.TestCase):
    def checkpoint(self, store, **options):
        original = store.checkpoint()
        return AssessmentCheckpoint(
            operation_id=original.operation_id,
            read=original.read,
            write=original.write,
            coalesce_admission_dispatch=True,
            **options,
        )

    def test_success_two_saves_keep_exact_admission_and_charge_before_provider(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        case = self

        class ObservingProvider(AssessingProvider):
            def complete(self, request):
                case.assertEqual(len(store.writes), 1)
                document = store.document
                case.assertEqual(document["counters"]["provider_calls"], 1)
                case.assertEqual(document["counters"]["response_bytes"], 0)
                case.assertGreater(document["response_bytes_reserved"], 0)
                case.assertEqual(
                    set(document["attempted_ids"]), set(queue.pending_order)
                )
                case.assertEqual(document["admitted_queue"], queue.to_document())
                case.assertTrue(document["plan_id"])
                case.assertTrue(all(document["request_digests"].values()))
                case.assertEqual(
                    {why for _, why in document["pending"]},
                    {"dispatch-outcome-unknown"},
                )
                return super().complete(request)

        provider = ObservingProvider()
        work = self.run_work(
            pending, bundle, queue, provider=provider, checkpoint=self.checkpoint(store)
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(store.writes), 2)
        self.assertEqual(len(work.reply.decisions), 4)
        self.assertEqual(store.document["response_bytes_reserved"], 0)
        self.assertGreater(store.document["counters"]["response_bytes"], 0)
        self.assertTrue(store.document["completed"])

    def test_kill_after_combined_activation_before_request_never_redispatches(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        original_now = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def kill(_):
            raise KeyboardInterrupt()

        store.after_write = kill
        provider = AssessingProvider()
        tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1))
        with self.assertRaises(KeyboardInterrupt):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                tracker=tracker,
                checkpoint=self.checkpoint(store, now=lambda: original_now),
            )
        self.assertFalse(provider.calls)
        self.assertEqual(len(store.writes), 1)
        original = copy.deepcopy(store.document)
        self.assertEqual(original["counters"]["provider_calls"], 1)
        self.assertGreater(original["response_bytes_reserved"], 0)
        store.after_write = None
        fresh = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1))
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            tracker=fresh,
            checkpoint=self.checkpoint(
                store, now=lambda: original_now + timedelta(seconds=1)
            ),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(fresh.provider_calls, 1)
        self.assertEqual(
            work.execution.response_bytes_reserved, original["response_bytes_reserved"]
        )
        self.assertEqual(fresh.execution_identity, original["execution_identity"])
        for field in (
            "created_at",
            "deadline_at",
            "expires_at",
            "resource_budget",
            "request_digests",
        ):
            self.assertEqual(store.document[field], original[field])
        self.assertFalse(work.reply.decisions)
        self.assertEqual(
            {why for _, why in work.execution.pending}, {"dispatch-outcome-unknown"}
        )

    def test_terminal_no_admissible_work_keeps_zero_call_admission(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        provider = AssessingProvider()
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            checkpoint=self.checkpoint(store),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_prompt_bytes=32),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(len(store.writes), 1)
        self.assertEqual(store.document["counters"]["provider_calls"], 0)
        self.assertFalse(store.document["attempted_ids"])
        self.assertEqual(store.document["response_bytes_reserved"], 0)
        self.assertEqual(store.document["admitted_queue"], queue.to_document())
        self.assertTrue(work.execution.pending)

    def test_combined_mutation_has_exact_fenced_metadata_and_refusal_precedes_request(
        self,
    ):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        contexts = []
        checkpoint = self.checkpoint(store)

        def activate(document, context):
            contexts.append(context)
            if context.reason == "admission-dispatch":
                self.assertEqual(context.root_generation, 0)
                self.assertTrue(context.batch_id)
                self.assertTrue(context.request_digest)
                self.assertTrue(context.dispatch_digest)
                self.assertEqual(document["counters"]["provider_calls"], 1)
                raise ReviewInputError("fresh exact grant refused")
            checkpoint.write(document)

        checkpoint.on_mutation = activate
        checkpoint.root_generation = lambda: store.generation
        provider = AssessingProvider()
        with self.assertRaisesRegex(ReviewInputError, "grant refused"):
            self.run_work(
                pending, bundle, queue, provider=provider, checkpoint=checkpoint
            )
        self.assertFalse(provider.calls)
        self.assertFalse(store.writes)
        self.assertEqual(len(contexts), 1)

    def test_capability_is_explicit_boolean_and_default_remains_three_saves(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=store.checkpoint(),
        )
        self.assertEqual(len(store.writes), 3)
        adapter = AssessmentQueueHostAdapter(
            queue=queue,
            binding={"purpose": "queue"},
            source_digest="e" * 64,
            operation_id="c" * 64,
            attempt_reservation_id="original-attempt",
            read=lambda: None,
            activate=lambda request: None,
        )
        self.assertFalse(adapter.checkpoint().coalesce_admission_dispatch)
        self.assertTrue(
            adapter.checkpoint(
                coalesce_admission_dispatch=True
            ).coalesce_admission_dispatch
        )
        for invalid in (None, 1, "yes"):
            with self.assertRaises(ReviewInputError):
                AssessmentCheckpoint(
                    operation_id="a" * 64,
                    read=lambda: None,
                    write=lambda value: None,
                    coalesce_admission_dispatch=invalid,
                )
            with self.assertRaises(ReviewInputError):
                adapter.checkpoint(coalesce_admission_dispatch=invalid)

    def test_combined_closed_metadata_requires_both_exact_digests(self):
        for reason in (["admission-dispatch"], None, 42, {}):
            with self.assertRaises(ReviewInputError):
                CheckpointMutation(
                    reason, request_digest="a" * 64, dispatch_digest="b" * 64
                )
        for values in (
            {},
            {"request_digest": "a" * 64},
            {"dispatch_digest": "b" * 64},
            {"request_digest": "short", "dispatch_digest": "b" * 64},
            {"request_digest": "a" * 64, "dispatch_digest": []},
        ):
            with self.assertRaises(ReviewInputError):
                CheckpointMutation("admission-dispatch", **values)
        CheckpointMutation(
            "admission-dispatch", request_digest="a" * 64, dispatch_digest="b" * 64
        )

    def test_partial_accepted_receipt_remains_durable_before_later_rejection(self):
        import json

        from review_sensei.models import ProviderResponse

        pending, bundle, queue = fixture(8)
        store = DocumentStore()

        class PartialProvider(AssessingProvider):
            def complete(self, request):
                response = super().complete(request)
                if len(self.calls) == 2:
                    value = json.loads(response.text)
                    value["assessments"][0]["diff_evidence"] = "fabricated citation"
                    return ProviderResponse(json.dumps(value), self.name, self.model)
                return response

        provider = PartialProvider()
        resource = ResourceBudget.create(max_provider_calls=2)
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            checkpoint=self.checkpoint(store),
            tracker=ResourceBudgetTracker(resource),
        )
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(work.reply.decisions), 4)
        self.assertEqual(len(work.execution.pending_ids), 4)
        self.assertEqual(len(store.writes), 4)
        self.assertTrue(store.writes[1]["completed"])
        self.assertFalse(store.writes[1]["response_bytes_reserved"])
        self.assertTrue(store.writes[-1]["completed"])
        replay = PartialProvider()
        restored = self.run_work(
            pending,
            bundle,
            queue,
            provider=replay,
            checkpoint=self.checkpoint(store),
            tracker=ResourceBudgetTracker(resource),
        )
        self.assertFalse(replay.calls)
        self.assertEqual(len(restored.reply.decisions), 4)
        self.assertEqual(store.document["counters"]["provider_calls"], 2)

    def test_response_crash_preserves_reserved_unknown_bytes_with_no_refund(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        cp = self.checkpoint(store)
        write = cp.write

        def capture(value):
            if value["counters"]["response_bytes"]:
                raise KeyboardInterrupt()
            write(value)

        cp.write = capture
        resource = ResourceBudget.create(max_provider_calls=1)
        provider = AssessingProvider()
        with self.assertRaises(KeyboardInterrupt):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                checkpoint=cp,
                tracker=ResourceBudgetTracker(resource),
            )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(store.writes), 1)
        reserved = store.document["response_bytes_reserved"]
        self.assertGreater(reserved, 0)
        self.assertEqual(store.document["counters"]["response_bytes"], 0)
        replay = AssessingProvider()
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=replay,
            checkpoint=self.checkpoint(store),
            tracker=ResourceBudgetTracker(resource),
        )
        self.assertFalse(replay.calls)
        self.assertFalse(work.reply.decisions)
        self.assertEqual(work.execution.response_bytes_reserved, reserved)
        self.assertEqual(store.document["counters"]["response_bytes"], 0)

    def test_combined_charge_is_not_early_attempted_work_when_zero_admission_saved(
        self,
    ):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        contexts = []
        cp = self.checkpoint(store)
        cp.on_mutation = lambda document, context: (
            contexts.append(context),
            cp.write(document),
        )
        cp.root_generation = lambda: store.generation
        self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=cp,
            work_budgets=ReviewWorkBudgets(mode="unified", batch_prompt_bytes=32),
        )
        self.assertEqual([context.reason for context in contexts], ["admission"])
        self.assertFalse(store.document["attempted_ids"])

    def test_indexed_actual_codec_complete_varied_feedback_100_250_two_checkpoints(
        self,
    ):
        from tests.test_assessment_history_index import IndexedHistoryTests

        self.measurements = []
        for count in (100, 250):
            case = IndexedHistoryTests()
            run = case.run_work

            def coalesced(*args, **kwargs):
                kwargs["checkpoint"].coalesce_admission_dispatch = True
                return run(*args, **kwargs)

            case.run_work = coalesced
            result = case.profile(count, full_feedback=True, frozen_inventory=True)
            self.assertLessEqual(result["parts"], 2 * result["sources"] + 2)
            self.assertEqual(result["direct_source_references"], result["sources"] - 1)
            self.measurements.append(result)

    def test_real_parent_kill_before_after_combined_save_and_original_receipt_restart(
        self,
    ):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        import time
        from pathlib import Path

        for boundary in ("before-combined-save", "after-combined-save"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                environment = {
                    key: os.environ[key]
                    for key in ("PATH", "HOME", "TMPDIR", "PYTHONPATH")
                    if key in os.environ
                }
                command = [
                    sys.executable,
                    "-m",
                    "tests.test_coalesced_admission",
                    directory,
                    boundary,
                ]
                with subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                ) as child:
                    deadline = time.monotonic() + 20
                    while not (root / "signal.json").exists():
                        if child.poll() is not None:
                            output, error = child.communicate()
                            self.fail(
                                (child.returncode, output.decode(), error.decode())
                            )
                        if time.monotonic() >= deadline:
                            child.kill()
                            child.communicate()
                            self.fail("synthetic combined-save barrier timeout")
                        time.sleep(0.01)
                    child.kill()
                    child.communicate(timeout=5)
                self.assertFalse((root / "provider-dispatched.json").exists())
                attempt = json.loads((root / "original-attempt.json").read_text())
                self.assertEqual(attempt["counters"]["provider_calls"], 1)
                self.assertGreater(attempt["response_bytes_reserved"], 0)
                if boundary == "before-combined-save":
                    # A real consuming-host restart still requires its original
                    # witness; receipt absence is not evidence of zero charges.
                    self.assertFalse((root / "receipt.json").exists())
                    continue
                original = json.loads((root / "receipt.json").read_text())
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.test_coalesced_admission",
                        directory,
                        "",
                    ],
                    capture_output=True,
                    text=True,
                    env=environment,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                final = json.loads((root / "receipt.json").read_text())
                self.assertFalse((root / "provider-dispatched.json").exists())
                self.assertEqual(final["counters"]["provider_calls"], 1)
                self.assertEqual(final["counters"]["response_bytes"], 0)
                self.assertEqual(
                    final["response_bytes_reserved"],
                    original["response_bytes_reserved"],
                )
                for field in (
                    "created_at",
                    "deadline_at",
                    "expires_at",
                    "execution_identity",
                    "resource_budget",
                    "request_digests",
                ):
                    self.assertEqual(final[field], original[field])


def _combined_process(directory, boundary):
    """Synthetic fsynced checkpoint transport, never an original broker grant."""
    import json
    import os
    import sys
    from pathlib import Path

    root = Path(directory)

    def save(name, value):
        path = root / name
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def barrier(name):
        if boundary == name:
            save("signal.json", {"boundary": name})
            sys.stdin.readline()
            raise AssertionError("killed barrier must not resume")

    def read():
        path = root / "receipt.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write(document):
        if not (root / "original-attempt.json").exists():
            save("original-attempt.json", document)
        barrier("before-combined-save")
        save("receipt.json", document)
        barrier("after-combined-save")

    class ObservingProvider(AssessingProvider):
        def complete(self, request):
            save("provider-dispatched.json", {"calls": len(self.calls) + 1})
            return super().complete(request)

    pending, bundle, queue = fixture(4)
    checkpoint = AssessmentCheckpoint(
        operation_id="c" * 64, read=read, write=write, coalesce_admission_dispatch=True
    )
    CoalescedAdmissionTests().run_work(
        pending,
        bundle,
        queue,
        provider=ObservingProvider(),
        checkpoint=checkpoint,
        tracker=ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1)),
    )


if __name__ == "__main__":
    import sys

    _combined_process(sys.argv[1], sys.argv[2])
