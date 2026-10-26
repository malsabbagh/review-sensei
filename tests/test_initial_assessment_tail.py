"""Current run admission/entry/readback, with explicit core library composition.

External HTTP and inference are synthetic. The reassessment test retains the
actual core engine checkpoint contract; entry tests additionally use review-run.
No v1/v2 broker phase grants or one-minute hosted allowance are constructed.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace

import test_review_transaction as fixture
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.session_ledger import parse_session_comment
from test_activation_tail import OPERATION, CurrentHostedFixture
from test_api_work_actions import SOURCE
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import EntryFixture

from review_sensei.errors import ReviewInputError
from review_sensei.session import read_session_assessment_queue


class InitialAssessmentTailTests(unittest.TestCase):
    def test_actual_reassessment_library_persists_complete_admission_before_provider(
        self,
    ):
        from test_assessment_queue import fixture as queue_fixture
        from test_authenticated_partitions import binding
        from test_review_work import HUMAN, AssessingProvider

        from review_sensei.assessment_queue import (
            AssessmentCheckpoint,
            AssessmentQueue,
            FactoredAssessmentJournal,
            pack_queue_state,
            unpack_queue_state,
        )
        from review_sensei.bounded_evidence import inline_manifest
        from review_sensei.budgets import ReviewWorkBudgets
        from review_sensei.evidence import EvidenceBundle, EvidenceSnapshot
        from review_sensei.human_assessment import PendingHumanReview
        from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
        from review_sensei.reassessment_work import reassess
        from review_sensei.session import assessment_queue_manifest_capacity

        pending, bundle, _ = queue_fixture(4)
        snapshot = EvidenceSnapshot(
            IDENTITY.repository,
            IDENTITY.pull_request,
            fixture.BASE_SHA,
            fixture.HEAD_SHA,
        )
        pending = PendingHumanReview(snapshot.base_sha, pending.findings)
        bundle = EvidenceBundle(
            snapshot,
            tuple(replace(record, snapshot=snapshot) for record in bundle.records),
        )
        queue = AssessmentQueue.create(pending, snapshot)
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        operation = {
            **OPERATION,
            "inventory_digest": queue.inventory_digest,
            "execution_identity": tracker.execution_identity,
        }
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        f = CurrentHostedFixture(directory.name)
        initial_ledger = f.ledger
        intent = f.caller.admit_action(
            f.ledger,
            IDENTITY,
            purpose="reply",
            source=SOURCE,
            head_sha=HEAD,
            operation_key=operation["operation_id"],
        )
        f.caller.begin_action_effect(f.ledger, IDENTITY, intent)
        budget = initial_ledger.evidence_budget
        accepted = []
        documents = []

        def activate(document, mutation):
            self.assertEqual(mutation.reason, "admission-dispatch")
            ledger = initial_ledger

            def prepare(reserved):
                journal = FactoredAssessmentJournal(queue).record(
                    queue=queue,
                    source_digest=operation["source_digest"],
                    receipt=document,
                )
                envelope = {
                    "schema_version": "1.0",
                    "kind": "assessment-queue-state",
                    "journal": journal.to_document(),
                }
                manifest = inline_manifest(
                    pack_queue_state(envelope),
                    binding={
                        **binding(
                            producer=ledger.evidence_producer(),
                            generation=0,
                            purpose="queue",
                        ),
                        "repository": IDENTITY.repository,
                        "repository_id": IDENTITY.repository_id,
                        "pull_request": IDENTITY.pull_request,
                    },
                    item_count=4,
                )
                self.assertIsNotNone(manifest)
                self.assertLessEqual(
                    len(json.dumps(manifest).encode()),
                    assessment_queue_manifest_capacity(reserved),
                )
                active = {
                    **{
                        key: value
                        for key, value in operation.items()
                        if not key.startswith("inventory_")
                    },
                    **document["counters"],
                    "deadline_at": document["deadline_at"].replace("+00:00", "Z"),
                    "expires_at": document["expires_at"].replace("+00:00", "Z"),
                    "response_bytes_reserved": document["response_bytes_reserved"],
                    "read_accounting": {
                        "calls": budget.calls,
                        "deadline_at_ms": budget.wall_deadline_ms,
                    },
                }
                return reserved.evolve(
                    generation=reserved.generation + 1,
                    assessment_queue={
                        "schema_version": "1.0",
                        "inventory_digest": queue.inventory_digest,
                        "inventory_generation": 0,
                        "state_manifest": manifest,
                        "active_operation": active,
                    },
                    now=None,
                )

            retained = ledger.replace(IDENTITY, prepare, now=None)
            documents.append(copy.deepcopy(document))
            accepted.append(retained)

        owner = self

        class InterruptedProvider(AssessingProvider):
            def complete(self, request):
                self.calls.append(request)
                current = initial_ledger.load(IDENTITY, now=None).record
                envelope = unpack_queue_state(
                    read_session_assessment_queue(initial_ledger, current)
                )
                receipt = FactoredAssessmentJournal.from_document(
                    envelope["journal"]
                ).receipt(
                    source_digest=operation["source_digest"],
                    operation_id=operation["operation_id"],
                )
                owner.assertEqual(receipt, documents[0])
                owner.assertEqual(receipt["counters"]["provider_calls"], 1)
                owner.assertEqual(len(receipt["admitted_queue"]["inventory_ids"]), 4)
                owner.assertEqual(len(receipt["pending"]), 4)
                owner.assertEqual(
                    current.api_run_accounting["action"]["intent"], intent
                )
                owner.assertTrue(
                    current.api_run_accounting["action"]["execution_started"]
                )
                raise KeyboardInterrupt()

        checkpoint = AssessmentCheckpoint(
            operation_id=operation["operation_id"],
            read=lambda: None,
            write=lambda _: self.fail("guarded callback required"),
            on_mutation=activate,
            root_generation=lambda: 0,
            coalesce_admission_dispatch=True,
        )
        provider = InterruptedProvider()
        with self.assertRaises(KeyboardInterrupt):
            reassess(
                pending=pending,
                bundle=bundle,
                queue=queue,
                source_body=HUMAN,
                authority_digest=operation["authority_digest"],
                provider=provider,
                tracker=tracker,
                work_budgets=ReviewWorkBudgets(mode="unified"),
                checkpoint=checkpoint,
            )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(f.caller.accounting.action["phase"], "pending")
        self.assertEqual(f.caller.accounting.configuration.limit, 512)
        f.assert_physical_accounting(self)

    def test_review_entry_has_owned_analysis_reservation_before_inference_and_full_tail(
        self,
    ):
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def before_provider():
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(current.transaction.phase, "analysis")
                self.assertEqual(
                    current.reservation_id, current.transaction.reservation_id
                )
                self.assertEqual(current.transaction.head_sha, HEAD)
                self.assertIs(
                    f.selected.ledger.evidence_budget, f.accounted.budget_view
                )
                self.assertIsInstance(f.selected.ledger, ApiRunSessionLedger)
                self.assertTrue(f.accounted._operation_entry_started)
                self.assertEqual(f.accounted.accounting.configuration.commit, f.policy)
                self.assertEqual(f.accounted.accounting.configuration.limit, 512)
                observed.append(current.transaction.transaction_id)

            f.before_provider = before_provider
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertEqual(len(observed), 1)
            final = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(final.transaction.transaction_id, observed[0])
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertIsNone(final.reservation_id)
            self.assertEqual(f.accounted.accounting.terminal_transaction, observed[0])
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
            self.assertTrue(f.check_runs)
            self.assertEqual(len(f.accounted.observations), len(f.trace))

    def test_fabricated_or_changed_current_intent_refuses_before_entry_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            intent = f.caller.admit_action(
                f.ledger,
                IDENTITY,
                purpose="reply",
                source=SOURCE,
                head_sha=HEAD,
                operation_key="d" * 64,
            )
            for key, value in (
                ("purpose", "command"),
                ("source", {**SOURCE, "id": 999}),
                ("head_sha", "a" * 40),
                ("run_id", 999),
                ("configuration_digest", "a" * 64),
                ("operation_key", "a" * 64),
                (
                    "owner",
                    {
                        **intent["owner"],
                        "generation": intent["owner"]["generation"] + 1,
                    },
                ),
            ):
                before = len(f.service.trace)
                with (
                    self.subTest(key=key),
                    self.assertRaisesRegex(
                        ReviewInputError, "unavailable or may have started"
                    ),
                ):
                    f.caller.begin_action_effect(
                        f.ledger, IDENTITY, {**intent, key: value}
                    )
                self.assertEqual(len(f.service.trace), before)
                self.assertFalse(f.caller.accounting.action["execution_started"])
            f.caller.begin_action_effect(f.ledger, IDENTITY, intent)
            self.assertTrue(f.caller.accounting.action["execution_started"])
            f.assert_physical_accounting(self)

    def test_cancellation_after_entry_keeps_unknown_operation_and_blocks_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def interrupt():
                raise KeyboardInterrupt()

            f.before_provider = interrupt
            self.assertEqual(f.invoke(), 130, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertIsNone(f.accounted.accounting.analysis_abort)
            self.assertTrue(f.accounted._operation_entry_started)
            f.before_provider = None
            f.next_run()
            self.assertEqual(f.invoke(), 1, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertFalse(f.reviews)

    def test_expired_native_read_or_unfunded_operation_never_dispatches_mutation(self):
        from urllib.request import Request

        for fault in ("deadline", "unfunded"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CurrentHostedFixture(directory)
                if fault == "unfunded":
                    while f.caller.usable > 2:
                        with f.caller.open(
                            Request(
                                "https://api.github.test/repos/owner/repo/pulls/7",
                                method="GET",
                            ),
                            timeout=30,
                        ) as response:
                            response.read(524288)
                else:
                    now = [1_800_000_000.0]
                    mono = [0.0]
                    f.caller._clock = lambda: now[0]
                    f.caller._monotonic = lambda: mono[0]
                    original = f.caller._opener

                    def expire(request, timeout):
                        result = original(request, timeout)
                        now[0] += 61
                        mono[0] += 61
                        return result

                    f.caller._opener = expire
                root = f.service.comment["body"]
                before = len(f.service.trace)
                with self.assertRaises(ReviewInputError):
                    f.checkpoint()
                self.assertTrue(all(m == "GET" for _, m, _ in f.service.trace[before:]))
                self.assertEqual(f.service.comment["body"], root)
                if fault == "deadline":
                    self.assertEqual(len(f.service.trace) - before, 1)
                    self.assertEqual(f.caller.observations[-1]["outcome"], "expired")
                else:
                    self.assertEqual(len(f.service.trace), before)
                f.assert_physical_accounting(self)

    def test_changed_head_or_checkpoint_root_refuses_before_provider_or_candidate(self):
        for fault in ("head", "root"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                if fault == "head":
                    f = EntryFixture(directory)
                    f.live_head = "c" * 40
                    self.assertEqual(f.invoke(), 1, f.error)
                    self.assertEqual(f.provider_calls, 0)
                    self.assertFalse(f.reviews)
                else:
                    f = CurrentHostedFixture(directory)
                    from review_sensei_github.session_ledger import (
                        render_session_comment,
                    )

                    item = f.service.comment
                    original = parse_session_comment(item["body"], identity=IDENTITY)
                    changed = original.evolve(
                        generation=original.generation + 1, operator_paused=True
                    )
                    item["body"] = render_session_comment(
                        repository_id=IDENTITY.repository_id,
                        pull_request=IDENTITY.pull_request,
                        record=changed,
                    )
                    before = len(f.service.trace)
                    with self.assertRaisesRegex(ReviewInputError, "root changed"):
                        f.checkpoint()
                    self.assertTrue(
                        all(m == "GET" for _, m, _ in f.service.trace[before:])
                    )
                    self.assertEqual(
                        parse_session_comment(item["body"], identity=IDENTITY), changed
                    )
                    f.assert_physical_accounting(self)

    def test_lost_and_partial_checkpoint_responses_reconcile_once_without_patch_replay(
        self,
    ):
        from fake_github_http import json_response
        from review_sensei_github.errors import GitHubPublicationTransientError

        for fault in ("lost", "partial"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CurrentHostedFixture(directory)
                original = f.caller._opener

                def ambiguous(request, timeout):
                    response = original(request, timeout)
                    if request.method == "PATCH":
                        if fault == "lost":
                            raise TimeoutError("synthetic committed but lost response")
                        return json_response({})
                    return response

                f.caller._opener = ambiguous
                before = len(f.service.trace)
                with self.assertRaises(
                    GitHubPublicationTransientError
                    if fault == "lost"
                    else ReviewInputError
                ):
                    f.checkpoint(1)
                f.caller._opener = original
                retained = f.caller.reconcile_checkpoint(f.ledger, IDENTITY)
                self.assertTrue(retained.operator_paused)
                used = len(f.service.trace)
                with self.assertRaises(ReviewInputError):
                    f.caller.reconcile_checkpoint(f.ledger, IDENTITY)
                self.assertEqual(len(f.service.trace), used)
                self.assertEqual(
                    sum(m == "PATCH" for _, m, _ in f.service.trace[before:]), 1
                )
                self.assertEqual(
                    f.caller.accounting.uncertain, 1 if fault == "lost" else 0
                )
                f.assert_physical_accounting(self)

    def test_absent_commit_or_incomplete_confirmation_never_refunds_or_retries(self):
        from fake_github_http import json_response
        from review_sensei_github.errors import GitHubPublicationTransientError

        for fault in ("absent-commit", "incomplete-readback"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CurrentHostedFixture(directory)
                root = f.service.comment["body"]
                before = len(f.service.trace)
                if fault == "absent-commit":
                    f.service.fault = "unknown-absent"
                else:
                    original = f.caller._opener
                    wrote = []

                    def incomplete(request, timeout):
                        response = original(request, timeout)
                        if request.method == "PATCH":
                            wrote.append(True)
                            raise TimeoutError("synthetic committed response lost")
                        if wrote and request.method == "GET":
                            return json_response([])
                        return response

                    f.caller._opener = incomplete
                with self.assertRaises(GitHubPublicationTransientError):
                    f.checkpoint(1)
                with self.assertRaises(ReviewInputError):
                    f.caller.reconcile_checkpoint(f.ledger, IDENTITY)
                used = len(f.service.trace)
                charges = f.caller.budget_view.calls
                with self.assertRaises(ReviewInputError):
                    f.caller.reconcile_checkpoint(f.ledger, IDENTITY)
                self.assertEqual(len(f.service.trace), used)
                self.assertEqual(f.caller.budget_view.calls, charges)
                self.assertEqual(f.caller.accounting.uncertain, 1)
                self.assertEqual(
                    sum(m == "PATCH" for _, m, _ in f.service.trace[before:]), 1
                )
                if fault == "absent-commit":
                    self.assertEqual(f.service.comment["body"], root)
                else:
                    self.assertNotEqual(f.service.comment["body"], root)
                f.assert_physical_accounting(self)

    def test_current_human_prose_reads_retained_history_before_provider_and_withholds(
        self,
    ):
        from review_sensei_github.action_completion import require_reply_completion
        from review_sensei_github.operation_host import OPERATION_COMMENT_MARKER
        from test_action_reply_entry import ReplyEntryFixture
        from test_review_run_rescan import partial_fixture

        with tempfile.TemporaryDirectory() as directory:
            prior, _ = partial_fixture(directory)
            original = parse_session_comment(prior.comment["body"], identity=IDENTITY)
            f = ReplyEntryFixture.__new__(ReplyEntryFixture)
            f.__dict__.update(prior.__dict__)
            f.next_run()
            f.live_head = f.requested_head = HEAD
            f.prepare_reply()
            f.thread_rows = [
                {
                    "id": "native-thread-" + str(comment["id"]),
                    "isResolved": True,
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": comment["id"],
                                "body": comment["body"],
                                "path": comment["path"],
                                "line": comment["line"],
                                "author": {
                                    "login": "reviewsensei",
                                    "__typename": "Bot",
                                },
                            }
                        ]
                    },
                }
                for comment in f.inline
            ]
            f.source["body"] = (
                "@reviewsensei The caller contract is intentional; the added change is checked."
            )
            f.reply_document = {
                "body": "The explanation is supported by the current diff.",
                "assessments": [
                    {
                        "fingerprint": decision["fingerprint"],
                        "decision": "dismissed",
                        "rationale": "The current diff contains the intended added change.",
                        "human_evidence": "The caller contract is intentional",
                        "diff_evidence": "@@ -1 +1,2 @@\n keep\n+change",
                    }
                    for decision in original.dispositions
                ],
            }
            observations = []

            def provider_boundary(request):
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(current.dispositions, original.dispositions)
                self.assertTrue(
                    current.api_run_accounting["action"]["execution_started"]
                )
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "pending"
                )
                self.assertTrue(
                    any(
                        OPERATION_COMMENT_MARKER in c["body"]
                        for c in f.extra_comments.values()
                    )
                )
                for decision in original.dispositions:
                    self.assertIn(decision["fingerprint"], request.prompt)
                observations.append(len(f.accounted.observations))
                return f.reply_document

            f.provider_callback = provider_boundary
            before = f.provider_calls
            self.assertEqual(f.invoke_reply(), 0, f.error)
            self.assertEqual(f.provider_calls, before + 1)
            self.assertEqual(len(observations), 1)
            current = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(current.dispositions, original.dispositions)
            self.assertEqual(len(current.dispositions), 20)
            self.assertEqual(
                current.transaction.to_dict(), original.transaction.to_dict()
            )
            self.assertEqual(f.action()["phase"], "terminal")
            self.assertEqual(f.last_result.approval_diagnostic, "review_incomplete")
            self.assertEqual(f.last_result.resolution_status, "resolved")
            self.assertTrue(
                require_reply_completion(
                    f.last_result,
                    f.selected.session_authority,
                    run_id=str(f.run_id),
                    run_attempt=str(f.attempt),
                )
            )
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
