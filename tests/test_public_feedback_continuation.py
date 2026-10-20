"""Trusted library composition, actual local persistence, synthetic GitHub/provider.

This is deliberately not CLI/Worker activation or consuming remote broker proof.
The test host owns the local ledger and authenticates the exact selected sources
before each prepaid activation. It never interprets human prose as a selector.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from review_sensei_github.errors import GitHubConversationError, GitHubPublicationError
from review_sensei_github.human_assessment import HumanAssessmentPublisher
from review_sensei_github.pending import pending_reply_pages

from review_sensei.assessment_queue import (
    AssessmentQueue,
    AssessmentQueueHostAdapter,
    FactoredAssessmentJournal,
    QueueHostState,
    assessment_operation_id,
    pack_queue_state,
    unpack_queue_state,
)
from review_sensei.bounded_evidence import (
    EvidenceReadBudget,
    canonical_bytes,
    inline_manifest,
)
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceSnapshot, evidence_digest
from review_sensei.feedback import (
    FeedbackAdmissionError,
    FeedbackReference,
    FeedbackSelector,
)
from review_sensei.models import ProviderResponse
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.reassessment_work import reassess
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    assessment_queue_manifest_capacity,
    read_session_assessment_queue,
)
from tests.test_human_assessment import APP
from tests.test_large_public_lifecycle import PagedState
from tests.test_review_work import AssessingProvider


class LocalFeedbackHost:
    """Caller-owned glue around the existing public host/ledger boundaries."""

    def __init__(
        self, directory, publisher, prepared, *, tracker=None, referenced=False
    ):
        # Fixture enrollment is separate from this assessment attempt.
        LocalSessionLedger(Path(directory)).initialize(
            SessionIdentity("owner/repo", 1, repository_id=1)
        )
        self.publisher, self.prepared = publisher, prepared
        self.referenced = referenced
        self.tracker = tracker or ResourceBudgetTracker(ResourceBudget.create())
        self.read_budget = publisher.before_read.__self__
        self.ledger = LocalSessionLedger(
            Path(directory),
            evidence_budget=self.read_budget,
            enable_partition_writes=True,
        )
        self.now = datetime.now(timezone.utc)
        self.identity = SessionIdentity("owner/repo", 1, repository_id=1)
        self.queue = AssessmentQueue.create(
            prepared.eligibility.human_review, prepared.evidence_bundle.snapshot
        )
        self.binding = {
            **prepared.evidence_bundle.snapshot.to_dict(),
            "repository_id": 1,
            "policy_digest": evidence_digest({"policy": "fixture"}),
            "configuration_digest": evidence_digest({"work": "unified"}),
            "generation": 0,
            "producer": self.ledger.evidence_producer(),
            "purpose": "queue",
            "schema_version": "1.0",
        }
        self.source = prepared.feedback.event_key
        self.operation_id = assessment_operation_id(
            snapshot=self.queue.snapshot,
            inventory=self.queue.inventory_digest,
            authority=prepared.authority_digest,
            source=self.source,
            provider=evidence_digest({"provider": "fixture"}),
            policy=self.binding["policy_digest"],
            evidence=prepared.evidence_bundle.digest,
            targets=prepared.feedback.target_ids or None,
        )
        self.reservation = self.operation_id
        self.operation = {
            "operation_id": self.operation_id,
            "source_digest": self.source,
            "authority_digest": prepared.authority_digest,
            "execution_identity": self.tracker.execution_identity,
            "inventory_digest": self.queue.inventory_digest,
            "inventory_generation": 0,
        }
        self.ledger.reserve_for_tail(
            self.identity,
            operation_binding=self.operation,
            slot="verification",
            reservation_id=self.reservation,
            expected_generation=0,
            now=self.now,
        )
        self.mutations = []
        self.before_activation = None
        self.adapter = self.new_adapter()

    def new_adapter(self):
        return AssessmentQueueHostAdapter(
            queue=self.queue,
            binding=self.binding,
            source_digest=self.source,
            operation_id=self.operation_id,
            attempt_reservation_id=self.reservation,
            read=self.read,
            activate=self.activate,
        )

    def read(self):
        record = self.ledger.load(self.identity, now=self.now).record
        if record is None:
            raise ReviewInputError("owned local queue root is unavailable")
        if (
            record.assessment_queue is not None
            and record.assessment_queue["state_manifest"]["binding"] != self.binding
        ):
            raise ReviewInputError("local queue binding changed")
        envelope = (
            unpack_queue_state(
                read_session_assessment_queue(self.ledger, record),
                queue=self.queue if self.referenced else None,
            )
            if record.assessment_queue is not None
            else None
        )
        return QueueHostState(
            envelope,
            record.generation,
            self.binding,
            len(self.queue.inventory_ids) if envelope is not None else 0,
            self.reservation,
        )

    def activate(self, request):
        self.fence_authority()
        if (
            request.source_digest != self.source
            or request.operation_id != self.operation_id
        ):
            raise ReviewInputError("source operation changed")

        def prepare(record):
            if record.generation != request.expected_generation:
                raise ReviewInputError("stale local generation")
            receipt = FactoredAssessmentJournal.from_document(
                request.envelope["journal"]
            ).receipt(source_digest=self.source, operation_id=self.operation_id)
            counters = receipt["counters"]
            active = {
                **{
                    key: value
                    for key, value in self.operation.items()
                    if not key.startswith("inventory_")
                },
                **counters,
                "deadline_at": receipt["deadline_at"].replace("+00:00", "Z"),
                "expires_at": receipt["expires_at"].replace("+00:00", "Z"),
                "response_bytes_reserved": receipt["response_bytes_reserved"],
                "read_accounting": {
                    "calls": self.read_budget.calls,
                    "deadline_at_ms": self.read_budget.wall_deadline_ms,
                },
            }
            carrier = pack_queue_state(
                request.envelope, queue=self.queue if self.referenced else None
            )
            manifest = inline_manifest(
                carrier, binding=self.binding, item_count=request.item_count
            )
            if manifest is None or len(
                canonical_bytes(manifest)
            ) > assessment_queue_manifest_capacity(record):
                manifest = self.ledger.stage_evidence(
                    self.identity,
                    binding=self.binding,
                    document=carrier,
                    item_count=request.item_count,
                    max_manifest_bytes=assessment_queue_manifest_capacity(record),
                )
            if self.before_activation is not None:
                self.before_activation()
            return record.evolve(
                assessment_queue={
                    "schema_version": "1.0",
                    "inventory_digest": self.queue.inventory_digest,
                    "inventory_generation": 0,
                    "state_manifest": manifest,
                    "active_operation": active,
                },
                generation=record.generation + 1,
                now=self.now,
            )

        def seal(record, accounting):
            root = copy.deepcopy(record.assessment_queue)
            root["active_operation"]["read_accounting"] = dict(accounting)
            return record.evolve(assessment_queue=root, now=self.now)

        accepted = self.ledger.replace_with_tail(
            self.identity,
            prepare,
            operation_binding=self.operation,
            attempt_reservation_id=self.reservation,
            seal_accounting=seal,
            now=self.now,
        )
        self.mutations.append(request.mutation.reason)
        self.last_envelope = copy.deepcopy(request.envelope)
        manifest = accepted.assessment_queue["state_manifest"]
        if (
            manifest["sha256"]
            != evidence_digest(
                pack_queue_state(
                    request.envelope, queue=self.queue if self.referenced else None
                )
            )
            or manifest["binding"] != self.binding
            or manifest["item_count"] != request.item_count
        ):
            raise ReviewInputError("local readback does not bind queue envelope")
        return QueueHostState(
            copy.deepcopy(request.envelope),
            accepted.generation,
            self.binding,
            request.item_count,
            self.reservation,
        )

    def fence_authority(self):
        self.publisher.revalidate_feedback(
            token="synthetic",
            feedback=self.prepared.feedback,
            app_slug=APP,
            before_read=self.publisher.before_read,
        )
        status, current_pr = self.publisher.http.request(
            "GET",
            self.publisher.http.repository_path("owner/repo", "/pulls/1"),
            token="synthetic",
        )
        if (
            status != 200
            or not isinstance(current_pr, dict)
            or current_pr.get("state") != "open"
            or current_pr.get("draft") is not False
            or current_pr.get("head", {}).get("sha") != self.prepared.feedback.head_sha
            or current_pr.get("base", {}).get("sha") != self.prepared.feedback.base_sha
        ):
            raise ReviewInputError("current PR snapshot changed")
        latest = self.publisher.finalizer.load_eligibility(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha=self.prepared.feedback.head_sha,
            app_slug=APP,
            require_valid=True,
        )
        if latest != self.prepared.eligibility:
            raise ReviewInputError("latest assessment authority changed")
        if self.queue is None:
            # The fresh reader obtains the required complete inventory from
            # this actual authenticated retained authority read, not a cache.
            self.queue = AssessmentQueue.create(
                latest.human_review,
                EvidenceSnapshot(
                    "owner/repo",
                    1,
                    current_pr["base"]["sha"],
                    current_pr["head"]["sha"],
                ),
            )

    def run(self, provider):
        self.fence_authority()
        if self.adapter is None:
            self.adapter = self.new_adapter()
        return reassess(
            provider=provider,
            pending=self.prepared.eligibility.human_review,
            bundle=self.prepared.evidence_bundle,
            source_body=self.prepared.source_body,
            authority_digest=self.prepared.authority_digest,
            work_budgets=ReviewWorkBudgets(mode="unified"),
            tracker=self.tracker,
            queue=self.queue,
            checkpoint=self.adapter.checkpoint(coalesce_admission_dispatch=True),
            feedback=self.prepared.feedback,
        )


class PublicFeedbackContinuationTests(unittest.TestCase):
    def prepare(self, count=40, *, targets=None, history_count=0, cold_context=False):
        state = PagedState(count, history_count=history_count)
        state.source["user"]["id"] = 42
        publisher = HumanAssessmentPublisher(
            http=state.http, before_read=EvidenceReadBudget().consume
        )
        inventory = state.eligibility.human_review
        ids = sorted(item.fingerprint for item in inventory.findings)
        if callable(targets):
            targets = targets(ids)
        selector = FeedbackSelector(
            "owner/repo",
            1,
            state.base,
            state.head,
            FeedbackReference("issue", 10, "updated"),
            (FeedbackReference("issue", 10, "updated"),),
            (ids[-1],) if targets is None else targets,
        )
        conversation = (
            publisher.conversation.prepare_selected_assessment_context(
                token="synthetic", selector=selector, app_slug=APP, root_comment_id=10
            )
            if cold_context == "selected"
            else publisher.conversation.prepare_context(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at="updated",
                app_slug=APP,
                root_comment_id=10,
                source_kind="issue",
                expected_head_sha=state.head,
                defer_evidence=cold_context == "deferred",
            )
            if cold_context
            else state.prepared()
        )
        prepared = publisher.prepare_feedback(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=conversation,
            app_slug=APP,
            selector=selector,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        return state, publisher, prepared

    def test_explicit_late_target_actual_local_receipt_before_publication(self):
        state, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)
            provider = AssessingProvider()
            work = host.run(provider)
            target = prepared.feedback.target_ids[0]
            self.assertEqual(work.selected_ids, (target,))
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])
            retained = host.last_envelope
            receipt = FactoredAssessmentJournal.from_document(
                retained["journal"]
            ).receipt(source_digest=host.source, operation_id=host.operation_id)
            self.assertEqual(len(receipt["completed"]), 1)
            result = publisher.publish(
                token="synthetic",
                review_token="synthetic",
                repository="owner/repo",
                pull_request=1,
                prepared=prepared,
                reply=work,
                app_slug=APP,
            )
            self.assertEqual(result.approval_status, "approval_withheld")
            self.assertEqual(state.latest_inventory().resolved, (target,))
            self.assertEqual(len(state.latest_inventory().pending), 39)
            self.assertLessEqual(host.read_budget.calls, 60)

    def test_accepted_interruption_replays_original_accounting_and_bounded_delivery(
        self,
    ):
        state, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)
            first = host.run(AssessingProvider())
            original_deadline = host.read_budget.wall_deadline_ms
            original_identity = host.tracker.execution_identity
            original_counters = (
                host.tracker.provider_calls,
                host.tracker.prompt_bytes,
                host.tracker.response_bytes,
            )
            # Reconstruct the C checkpoint and A root/part reader, retaining the
            # SAME original control budget and durable reservation. No new grant.
            host.adapter = AssessmentQueueHostAdapter(
                queue=host.queue,
                binding=host.binding,
                source_digest=host.source,
                operation_id=host.operation_id,
                attempt_reservation_id=host.reservation,
                read=host.read,
                activate=host.activate,
            )
            host.tracker = ResourceBudgetTracker(ResourceBudget.create())
            replay_provider = AssessingProvider()
            replay = host.run(replay_provider)
            self.assertEqual(replay_provider.calls, [])
            self.assertEqual(replay.reply, first.reply)
            self.assertEqual(host.tracker.execution_identity, original_identity)
            self.assertEqual(
                (
                    host.tracker.provider_calls,
                    host.tracker.prompt_bytes,
                    host.tracker.response_bytes,
                ),
                original_counters,
            )
            self.assertEqual(host.read_budget.wall_deadline_ms, original_deadline)
            self.assertEqual(
                host.mutations, ["admission-dispatch", "accepted", "replay"]
            )
            before_publication = len(state.calls)
            accepted_calls = host.read_budget.calls
            outcome = publisher.publish(
                token="synthetic",
                review_token="synthetic",
                repository="owner/repo",
                pull_request=1,
                prepared=prepared,
                reply=replay,
                app_slug=APP,
            )
            self.assertEqual(outcome.status, "replied")
            self.assertEqual(outcome.approval_status, "approval_withheld")
            self.assertEqual(state.latest_inventory().resolved, replay.selected_ids)
            self.assertEqual(len(state.latest_inventory().pending), 39)
            self.assertEqual(host.read_budget.calls, 60)
            self.assertEqual(
                len(state.calls) - before_publication,
                host.read_budget.calls - accepted_calls,
            )
            self.assertEqual(host.read_budget.wall_deadline_ms, original_deadline)
            self.assertEqual(host.tracker.execution_identity, original_identity)
            self.assertEqual(replay_provider.calls, [])
            self.assertNotIn("APPROVE", state.events())

    def test_unknown_or_foreign_target_has_no_provider_or_checkpoint_mutation(self):
        for target in ("e" * 64, evidence_digest({"repository": "foreign/repo"})):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as directory,
            ):
                state, publisher, prepared = self.prepare(targets=(target,))
                host = LocalFeedbackHost(directory, publisher, prepared)
                before = host.ledger.load(host.identity).record
                provider = AssessingProvider()
                with self.assertRaises(ReviewInputError):
                    host.run(provider)
                after = host.ledger.load(host.identity).record
                self.assertEqual(before, after)
                self.assertEqual(provider.calls, [])
                self.assertEqual(host.mutations, [])
                self.assertEqual(state.reply_count, 0)

    def test_short_or_duplicate_target_selector_refuses(self):
        for targets in (("RS-123456",), ("e" * 64, "e" * 64)):
            with (
                self.subTest(targets=targets),
                self.assertRaises(FeedbackAdmissionError),
            ):
                self.prepare(targets=targets)

    def test_stale_snapshot_queue_refuses_before_provider_or_checkpoint_mutation(self):
        state, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)
            host.queue = replace(
                host.queue, snapshot=replace(host.queue.snapshot, head_sha="c" * 40)
            )
            provider = AssessingProvider()
            with self.assertRaises(ReviewInputError):
                host.run(provider)
            self.assertEqual(provider.calls, [])
            self.assertEqual(host.mutations, [])
            self.assertEqual(state.reply_count, 0)

    def test_changed_owned_carrier_binding_cannot_replay_accepted_receipt(self):
        _, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)
            host.run(AssessingProvider())

            def change_binding(record):
                root = copy.deepcopy(record.assessment_queue)
                root["state_manifest"]["binding"]["configuration_digest"] = "e" * 64
                return record.evolve(
                    assessment_queue=root, generation=record.generation + 1
                )

            LocalSessionLedger(Path(directory)).replace(host.identity, change_binding)
            provider = AssessingProvider()
            with self.assertRaisesRegex(ReviewInputError, "binding changed"):
                host.run(provider)
            self.assertEqual(provider.calls, [])
            self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])

    def test_source_changes_between_dispatch_and_acceptance_preserve_unresolved_union(
        self,
    ):
        state, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)

            class EditingProvider(AssessingProvider):
                def complete(self, request):
                    result = super().complete(request)
                    state.source["body"] += " changed after dispatch"
                    return result

            provider = EditingProvider()
            with self.assertRaises(FeedbackAdmissionError):
                host.run(provider)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(host.mutations, ["admission-dispatch"])
            observer = LocalSessionLedger(Path(directory))
            record = observer.load(host.identity).record
            envelope = unpack_queue_state(
                read_session_assessment_queue(observer, record)
            )
            journal = FactoredAssessmentJournal.from_document(envelope["journal"])
            receipt = journal.receipt(
                source_digest=host.source, operation_id=host.operation_id
            )
            self.assertEqual(receipt["completed"], [])
            self.assertEqual(receipt["counters"]["provider_calls"], 1)
            self.assertGreater(receipt["response_bytes_reserved"], 0)
            self.assertEqual(state.latest_inventory().resolved, ())
            self.assertEqual(state.reply_count, 0)

    def test_head_base_or_latest_inventory_changes_cannot_accept_receipt(self):
        for change in ("head", "base", "inventory"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                state, publisher, prepared = self.prepare()
                host = LocalFeedbackHost(directory, publisher, prepared)

                class MovingAuthority(AssessingProvider):
                    def complete(self, request):
                        result = super().complete(request)
                        if change in {"head", "base"}:
                            setattr(state, change, "c" * 40)
                        else:
                            inventory = state.eligibility.human_review
                            changed = replace(
                                inventory,
                                findings=(
                                    replace(
                                        inventory.findings[0],
                                        body="Changed immutable concern",
                                    ),
                                    *inventory.findings[1:],
                                ),
                            )
                            state.reviews.append(
                                state.review(
                                    replace(state.eligibility, human_review=changed),
                                    100,
                                )
                            )
                        return result

                provider = MovingAuthority()
                with self.assertRaises(ReviewInputError):
                    host.run(provider)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(host.mutations, ["admission-dispatch"])
                observer = LocalSessionLedger(Path(directory))
                root = observer.load(host.identity).record
                packet = unpack_queue_state(
                    read_session_assessment_queue(observer, root)
                )
                journal = FactoredAssessmentJournal.from_document(packet["journal"])
                receipt = journal.receipt(
                    source_digest=host.source, operation_id=host.operation_id
                )
                self.assertEqual(receipt["completed"], [])
                self.assertEqual(packet["journal"]["queue"]["resolved_ids"], [])
                self.assertEqual(state.reply_count, 0)

    def test_read_only_accepted_replay_still_refuses_changed_source_or_head(self):
        for change in ("source", "head"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                state, publisher, prepared = self.prepare()
                host = LocalFeedbackHost(directory, publisher, prepared)
                host.run(AssessingProvider())
                observer = LocalSessionLedger(Path(directory))
                before = observer.load(host.identity).record
                if change == "source":
                    state.source["body"] += " changed after durable acceptance"
                else:
                    state.head = "c" * 40
                provider = AssessingProvider()
                with self.assertRaises(ReviewInputError):
                    host.run(provider)
                self.assertEqual(provider.calls, [])
                self.assertEqual(observer.load(host.identity).record, before)
                self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])
                self.assertEqual(state.reply_count, 0)

    def test_expired_original_control_deadline_cannot_replay_or_publish(self):
        state, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)
            work = host.run(AssessingProvider())
            before = host.read_budget.snapshot()
            before_calls = len(state.calls)
            host.read_budget.clock = lambda: host.read_budget.deadline + 1
            provider = AssessingProvider()
            with self.assertRaises(ReviewInputError):
                host.run(provider)
            with self.assertRaises(ReviewInputError):
                publisher.publish(
                    token="synthetic",
                    review_token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=work,
                    app_slug=APP,
                )
            self.assertEqual(provider.calls, [])
            self.assertEqual(host.read_budget.snapshot(), before)
            self.assertEqual(len(state.calls), before_calls)
            self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])
            self.assertEqual(state.reply_count, 0)

    def test_fresh_owner_without_original_attempt_witness_cannot_activate_replay(self):
        state, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)
            host.run(AssessingProvider())
            observer = LocalSessionLedger(Path(directory))
            retained = observer.load(host.identity).record
            original_deadline = host.read_budget.wall_deadline_ms
            restored = EvidenceReadBudget(snapshot=host.read_budget.snapshot())
            host.publisher = HumanAssessmentPublisher(
                http=state.http, before_read=restored.consume
            )
            host.read_budget = restored
            host.ledger = LocalSessionLedger(
                Path(directory), evidence_budget=restored, enable_partition_writes=True
            )
            host.adapter = AssessmentQueueHostAdapter(
                queue=host.queue,
                binding=host.binding,
                source_digest=host.source,
                operation_id=host.operation_id,
                attempt_reservation_id=host.reservation,
                read=host.read,
                activate=host.activate,
            )
            host.tracker = ResourceBudgetTracker(ResourceBudget.create())
            provider = AssessingProvider()
            with self.assertRaisesRegex(ReviewInputError, "witness"):
                host.run(provider)
            self.assertEqual(provider.calls, [])
            self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])
            self.assertEqual(restored.wall_deadline_ms, original_deadline)
            self.assertEqual(observer.load(host.identity).record, retained)
            self.assertEqual(state.reply_count, 0)

    def test_varied_100_falls_back_to_parts_and_does_not_claim_publication_capacity(
        self,
    ):
        state, publisher, prepared = self.prepare(100)
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)
            provider = AssessingProvider()
            work = host.run(provider)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(work.selected_ids, prepared.feedback.target_ids)
            record = LocalSessionLedger(Path(directory)).load(host.identity).record
            self.assertEqual(
                record.assessment_queue["state_manifest"]["encoding"],
                "partitioned-json-v1",
            )
            with self.assertRaises((ReviewInputError, GitHubPublicationError)):
                publisher.publish(
                    token="synthetic",
                    review_token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=work,
                    app_slug=APP,
                )
            self.assertEqual(state.latest_inventory().resolved, ())
            self.assertEqual(state.reply_count, 0)

    def test_referenced_varied_100_complete_publication_inside_original_budget(self):
        state, publisher, prepared = self.prepare(100)
        original = prepared.eligibility.human_review.inventory_document()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared, referenced=True)
            provider = AssessingProvider()
            work = host.run(provider)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(work.selected_ids, prepared.feedback.target_ids)
            self.assertEqual(host.read_budget.calls, 32)
            record = LocalSessionLedger(Path(directory)).load(host.identity).record
            self.assertNotEqual(
                record.assessment_queue["state_manifest"]["encoding"],
                "partitioned-json-v1",
            )
            outcome = publisher.publish(
                token="synthetic",
                review_token="synthetic",
                repository="owner/repo",
                pull_request=1,
                prepared=prepared,
                reply=work,
                app_slug=APP,
                pending_report_layout="shared-guidance-v1",
            )
            self.assertEqual(outcome.approval_status, "approval_withheld")
            self.assertEqual(
                state.latest_inventory().resolved, prepared.feedback.target_ids
            )
            self.assertEqual(len(state.latest_inventory().pending), 99)
            self.assertEqual(host.read_budget.calls, 51)
            self.assertEqual(state.reply_count, 1)
            self.assertNotIn("APPROVE", state.events())
            self.assertEqual(state.latest_inventory().inventory_document(), original)
            for item in state.latest_inventory().pending:
                self.assertIn(item.fingerprint, state.replies[0]["body"])

    def test_ordinary_cold_history_preserves_bounded_completion_or_retained_recovery(
        self,
    ):
        # Unlike the prepared-context positive control, ordinary conversation
        # preparation consumes this SAME original physical-work allowance.
        for history_count, accepted_reads in ((100, 39), (250, 40)):
            with self.subTest(history_count=history_count):
                state, publisher, prepared = self.prepare(
                    100, history_count=history_count, cold_context=True
                )
                original = prepared.eligibility.human_review.inventory_document()
                with tempfile.TemporaryDirectory() as directory:
                    host = LocalFeedbackHost(
                        directory, publisher, prepared, referenced=True
                    )
                    provider = AssessingProvider()
                    work = host.run(provider)
                    self.assertEqual(len(provider.calls), 1)
                    self.assertEqual(host.read_budget.calls, accepted_reads)
                    self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])
                    accepted = copy.deepcopy(host.last_envelope)
                    counters = host.tracker.provider_calls
                    deadline = host.read_budget.wall_deadline_ms
                    before_publication = len(state.calls)

                    def publish():
                        return publisher.publish(
                            token="synthetic",
                            review_token="synthetic",
                            repository="owner/repo",
                            pull_request=1,
                            prepared=prepared,
                            reply=work,
                            app_slug=APP,
                            pending_report_layout="shared-guidance-v1",
                        )

                    if history_count == 100:
                        outcome = publish()
                        self.assertEqual(outcome.status, "replied")
                        self.assertEqual(outcome.approval_status, "approval_withheld")
                    else:
                        with self.assertRaises(
                            (ReviewInputError, GitHubPublicationError)
                        ):
                            publish()
                    self.assertEqual(host.read_budget.calls, 60)
                    self.assertEqual(
                        len(state.calls) - before_publication, 60 - accepted_reads
                    )
                    self.assertEqual(host.read_budget.wall_deadline_ms, deadline)
                    self.assertEqual(host.tracker.provider_calls, counters)
                    observer = LocalSessionLedger(Path(directory))
                    root = observer.load(host.identity).record
                    retained = unpack_queue_state(
                        read_session_assessment_queue(observer, root), queue=host.queue
                    )
                    self.assertEqual(retained, accepted)
                    receipt = FactoredAssessmentJournal.from_document(
                        retained["journal"]
                    ).receipt(source_digest=host.source, operation_id=host.operation_id)
                    self.assertEqual(len(receipt["completed"]), 1)
                    self.assertEqual(receipt["counters"]["provider_calls"], 1)
                    self.assertEqual(
                        state.latest_inventory().resolved,
                        prepared.feedback.target_ids if history_count == 100 else (),
                    )
                    self.assertEqual(
                        len(state.latest_inventory().pending),
                        99 if history_count == 100 else 100,
                    )
                    self.assertEqual(
                        state.latest_inventory().inventory_document(), original
                    )
                    self.assertEqual(state.reply_count, 1)
                    # A provisional receipt cannot advance authority when the
                    # remaining history exhausts the original allowance.
                    self.assertEqual(
                        state.events(), ["COMMENT"] if history_count == 100 else []
                    )

    def test_selected_cold_100_with_0_100_250_history_completes_original_budget(self):
        for history_count, final_calls in ((0, 53), (100, 56), (250, 59)):
            with self.subTest(history_count=history_count):
                state, publisher, prepared = self.prepare(
                    100, history_count=history_count, cold_context="selected"
                )
                original = prepared.eligibility.human_review.inventory_document()
                with tempfile.TemporaryDirectory() as directory:
                    host = LocalFeedbackHost(
                        directory, publisher, prepared, referenced=True
                    )
                    deadline = host.read_budget.wall_deadline_ms
                    provider = AssessingProvider()
                    work = host.run(provider)
                    self.assertEqual(host.read_budget.calls, 34)
                    result = publisher.publish(
                        token="synthetic",
                        review_token="synthetic",
                        repository="owner/repo",
                        pull_request=1,
                        prepared=prepared,
                        reply=work,
                        app_slug=APP,
                        pending_report_layout="shared-guidance-v1",
                    )
                    self.assertEqual(result.approval_status, "approval_withheld")
                    self.assertEqual(len(provider.calls), 1)
                    self.assertEqual(host.read_budget.calls, final_calls)
                    self.assertEqual(host.read_budget.wall_deadline_ms, deadline)
                    self.assertEqual(
                        state.latest_inventory().resolved, prepared.feedback.target_ids
                    )
                    self.assertEqual(len(state.latest_inventory().pending), 99)
                    self.assertEqual(
                        state.latest_inventory().inventory_document(), original
                    )
                    self.assertEqual(state.events(), ["COMMENT"])
                    self.assertNotIn("APPROVE", state.events())

    def test_ordinary_cold_100_without_history_completes_within_original_budget(self):
        state, publisher, prepared = self.prepare(100, cold_context=True)
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared, referenced=True)
            work = host.run(AssessingProvider())
            result = publisher.publish(
                token="synthetic",
                review_token="synthetic",
                repository="owner/repo",
                pull_request=1,
                prepared=prepared,
                reply=work,
                app_slug=APP,
                pending_report_layout="shared-guidance-v1",
            )
            self.assertEqual(result.approval_status, "approval_withheld")
            self.assertEqual(host.read_budget.calls, 57)
            self.assertEqual(
                state.latest_inventory().resolved, prepared.feedback.target_ids
            )
            self.assertEqual(len(state.latest_inventory().pending), 99)

    def test_selected_context_keeps_identical_complete_provider_inputs(self):
        inputs = []
        context_calls = []
        for context in (True, "selected", "deferred"):
            state, publisher, prepared = self.prepare(
                100, history_count=250, cold_context=context
            )
            context_calls.append(len(state.calls))
            with tempfile.TemporaryDirectory() as directory:
                host = LocalFeedbackHost(
                    directory, publisher, prepared, referenced=True
                )
                provider = AssessingProvider()
                host.run(provider)
                inputs.append(
                    (
                        prepared.eligibility,
                        prepared.evidence_bundle,
                        prepared.feedback,
                        prepared.source_body,
                        prepared.authority_digest,
                        [
                            replace(request, timeout_seconds=None)
                            for request in provider.calls
                        ],
                    )
                )
        self.assertEqual(inputs[0], inputs[1])
        self.assertEqual(inputs[0], inputs[2])
        self.assertLess(context_calls[1], context_calls[0])
        self.assertEqual(context_calls[1], context_calls[2])

    def test_selected_context_requires_explicit_target_and_live_exact_scope(self):
        for fault in ("targets", "base", "head", "identity", "revision", "author"):
            with self.subTest(fault=fault):
                state, publisher, prepared = self.prepare(100)
                feedback = prepared.feedback
                selector = FeedbackSelector(
                    feedback.repository,
                    feedback.pull_request,
                    feedback.base_sha,
                    feedback.head_sha,
                    feedback.trigger,
                    tuple(item.reference for item in feedback.sources),
                    () if fault == "targets" else feedback.target_ids,
                )
                if fault == "base":
                    state.base = "e" * 40
                elif fault == "head":
                    state.head = "e" * 40
                elif fault == "identity":
                    state.source["id"] = 11
                elif fault == "revision":
                    state.source["updated_at"] = "edited"
                elif fault == "author":
                    state.source["author_association"] = "NONE"
                before = publisher.before_read.__self__.calls

                def prepare():
                    return publisher.conversation.prepare_selected_assessment_context(
                        token="synthetic", selector=selector, app_slug=APP
                    )

                if fault in {"identity", "targets"}:
                    with self.assertRaises(GitHubConversationError):
                        prepare()
                    if fault == "targets":
                        self.assertEqual(publisher.before_read.__self__.calls, before)
                else:
                    self.assertTrue(prepare().status.startswith("skipped_"))
                self.assertEqual(state.events(), [])
                self.assertEqual(state.reply_count, 0)

    def test_selected_context_retains_complete_multiple_source_closure(self):
        state, _, original = self.prepare(100)
        auxiliary = copy.deepcopy(state.source)
        auxiliary.update(
            id=11,
            body="Complete selected context 深🙂. " + "Unabridged detail. " * 100,
            updated_at="auxiliary",
        )
        auxiliary["user"]["id"] = 43
        original_request = state.http.request

        def request(method, path, **arguments):
            if method == "GET" and path.endswith("/issues/comments/11"):
                return 200, copy.deepcopy(auxiliary)
            return original_request(method, path, **arguments)

        state.http.request = request
        publisher = HumanAssessmentPublisher(
            http=state.http, before_read=EvidenceReadBudget().consume
        )
        feedback = original.feedback
        selector = FeedbackSelector(
            feedback.repository,
            feedback.pull_request,
            feedback.base_sha,
            feedback.head_sha,
            feedback.trigger,
            (feedback.trigger, FeedbackReference("issue", 11, "auxiliary")),
            feedback.target_ids,
        )
        context = publisher.conversation.prepare_selected_assessment_context(
            token="synthetic", selector=selector, app_slug=APP
        )
        prepared = publisher.prepare_feedback(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=context,
            app_slug=APP,
            selector=selector,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertEqual(len(prepared.feedback.sources), 2)
        self.assertEqual(prepared.eligibility, original.eligibility)
        self.assertEqual(prepared.evidence_bundle, original.evidence_bundle)
        self.assertEqual(
            {item.body for item in prepared.feedback.sources},
            {state.source["body"], auxiliary["body"]},
        )
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared, referenced=True)
            provider = AssessingProvider()
            host.run(provider)
            self.assertEqual(len(provider.calls), 1)
            self.assertIn(auxiliary["body"], provider.calls[0].prompt)
            self.assertEqual(state.events(), [])

    def test_referenced_fresh_reader_reconstructs_inventory_but_not_attempt_witness(
        self,
    ):
        state, publisher, prepared = self.prepare(100)
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared, referenced=True)
            host.run(AssessingProvider())
            observer = LocalSessionLedger(Path(directory))
            retained = observer.load(host.identity).record
            original_deadline = host.read_budget.wall_deadline_ms
            restored = EvidenceReadBudget(snapshot=host.read_budget.snapshot())
            host.publisher = HumanAssessmentPublisher(
                http=state.http, before_read=restored.consume
            )
            host.read_budget = restored
            host.ledger = LocalSessionLedger(
                Path(directory), evidence_budget=restored, enable_partition_writes=True
            )
            host.queue, host.adapter = None, None
            host.tracker = ResourceBudgetTracker(ResourceBudget.create())
            before_reads = len(state.calls)
            provider = AssessingProvider()
            with self.assertRaisesRegex(ReviewInputError, "witness"):
                host.run(provider)
            self.assertEqual(provider.calls, [])
            self.assertTrue(
                any(
                    path.endswith("/pulls/1/reviews")
                    for _, path, _ in state.calls[before_reads:]
                )
            )
            self.assertEqual(len(host.queue.inventory_ids), 100)
            self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])
            self.assertEqual(observer.load(host.identity).record, retained)
            self.assertEqual(restored.wall_deadline_ms, original_deadline)
            self.assertEqual(state.reply_count, 0)

    def test_referenced_fresh_reader_refuses_missing_original_retained_authority(self):
        state, publisher, prepared = self.prepare(100)
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared, referenced=True)
            host.run(AssessingProvider())
            observer = LocalSessionLedger(Path(directory))
            before = observer.load(host.identity).record
            host.queue, host.adapter = None, None
            state.reviews.clear()
            provider = AssessingProvider()
            with self.assertRaises(ReviewInputError):
                host.run(provider)
            self.assertIsNone(host.queue)
            self.assertEqual(provider.calls, [])
            self.assertEqual(host.mutations, ["admission-dispatch", "accepted"])
            self.assertEqual(observer.load(host.identity).record, before)
            self.assertEqual(state.reply_count, 0)

    def test_referenced_shared_report_source_head_base_and_latest_races_between_pages(
        self,
    ):
        class UnresolvedProvider(AssessingProvider):
            def complete(self, request):
                value = json.loads(super().complete(request).text)
                # Keep one valid resolution: missing the later authority fence
                # would otherwise allow a real inventory mutation.
                for decision in value["assessments"][1:]:
                    decision.update(
                        decision="unresolved",
                        rationale="Distinct unresolved basis 深🙂 " + "evidence " * 100,
                        human_evidence="",
                        diff_evidence="",
                    )
                return ProviderResponse(json.dumps(value), self.name, self.model)

        for race in ("source", "head", "base", "latest"):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as directory:
                state, publisher, prepared = self.prepare(
                    104, targets=lambda ids: tuple(ids[-4:])
                )
                host = LocalFeedbackHost(
                    directory, publisher, prepared, referenced=True
                )
                work = host.run(UnresolvedProvider())
                pages = pending_reply_pages(
                    body=work.reply.body,
                    head_sha=state.head,
                    eligibility=replace(
                        prepared.eligibility,
                        human_review=prepared.eligibility.human_review.apply(
                            work.reply.decisions
                        ),
                    ),
                    decision_reasons={
                        item.fingerprint: item.rationale
                        for item in work.reply.decisions
                    },
                    layout="shared-guidance-v1",
                )
                self.assertEqual(len(pages), 2)
                original_request = state.http.request

                def racing_request(method, path, **arguments):
                    result = original_request(method, path, **arguments)
                    if method == "POST" and path.endswith("/issues/1/comments"):
                        if race == "source":
                            state.source["updated_at"] = "edited"
                        elif race == "head":
                            state.head = "c" * 40
                        elif race == "base":
                            state.base = "c" * 40
                        else:
                            state.reviews.append(
                                state.review(
                                    replace(state.eligibility, result_digest="f" * 64),
                                    100,
                                )
                            )
                    return result

                state.http.request = racing_request

                def publish():
                    return publisher.publish(
                        token="synthetic",
                        review_token="synthetic",
                        repository="owner/repo",
                        pull_request=1,
                        prepared=prepared,
                        reply=work,
                        app_slug=APP,
                        pending_report_layout="shared-guidance-v1",
                    )

                if race == "source":
                    with self.assertRaises(FeedbackAdmissionError):
                        publish()
                else:
                    self.assertTrue(publish().status.startswith("skipped_"))
                self.assertEqual(state.reply_count, 1)
                self.assertEqual(state.latest_inventory().resolved, ())
                self.assertNotIn("APPROVE", state.events())
                self.assertLessEqual(host.read_budget.calls, 60)

    def test_referenced_delivery_conflict_concurrent_lost_and_unreadable_pages_keep_all_pending(
        self,
    ):
        for fault in (
            "same-id-content",
            "concurrent",
            "lost-post",
            "partial-post",
            "readback",
            "direct-readback",
            "direct-identity",
            "direct-owner",
            "direct-kind",
            "direct-association",
            "direct-body",
            "direct-missing",
            "whole-readback",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state, publisher, prepared = self.prepare(100)
                host = LocalFeedbackHost(
                    directory, publisher, prepared, referenced=True
                )
                work = host.run(AssessingProvider())
                original_request = state.http.request
                scans_after_post = 0

                def failing_request(method, path, **arguments):
                    nonlocal scans_after_post
                    if (
                        fault == "direct-readback"
                        and method == "GET"
                        and "/issues/comments/" in path
                        and not path.endswith("/10")
                    ):
                        raise GitHubConversationError("owned page unavailable")
                    if (
                        method == "POST"
                        and path.endswith("/issues/1/comments")
                        and fault == "concurrent"
                    ):
                        state.replies.append(
                            {
                                "id": 999,
                                "body": arguments["body"]["body"],
                                "created_at": "created",
                                "updated_at": "replied",
                                "user": {"login": APP, "type": "Bot"},
                                "issue_url": state.source["issue_url"],
                            }
                        )
                    if (
                        method == "GET"
                        and "/issues/1/comments" in path
                        and state.reply_count
                    ):
                        scans_after_post += 1
                        if fault == "readback" or (
                            fault == "whole-readback" and scans_after_post == 1
                        ):
                            raise GitHubConversationError(
                                "owned whole page readback unavailable"
                            )
                    result = original_request(method, path, **arguments)
                    if (
                        fault.startswith("direct-")
                        and method == "GET"
                        and "/issues/comments/" in path
                        and not path.endswith("/10")
                    ):
                        if fault == "direct-missing":
                            return 404, None
                        status, comment = result
                        comment = copy.deepcopy(comment)
                        if fault == "direct-identity":
                            comment["id"] = str(comment["id"])
                        elif fault == "direct-owner":
                            comment["user"]["login"] = "other[bot]"
                        elif fault == "direct-kind":
                            comment["user"]["type"] = "User"
                        elif fault == "direct-association":
                            comment["issue_url"] += "0"
                        elif fault == "direct-body":
                            comment["body"] += " changed"
                        return status, comment
                    if method == "POST" and path.endswith("/issues/1/comments"):
                        if fault == "lost-post":
                            raise GitHubConversationError("post outcome unknown")
                        if fault in {"same-id-content", "partial-post"}:
                            state.replies[-1]["body"] = (
                                "changed " + state.replies[-1]["body"]
                                if fault == "same-id-content"
                                else state.replies[-1]["body"][:100]
                            )
                    return result

                state.http.request = failing_request
                with self.assertRaises(
                    (ReviewInputError, GitHubConversationError, GitHubPublicationError)
                ):
                    publisher.publish(
                        token="synthetic",
                        review_token="synthetic",
                        repository="owner/repo",
                        pull_request=1,
                        prepared=prepared,
                        reply=work,
                        app_slug=APP,
                        pending_report_layout="shared-guidance-v1",
                    )
                self.assertEqual(state.latest_inventory().resolved, ())
                self.assertEqual(len(state.latest_inventory().pending), 100)
                self.assertNotIn("APPROVE", state.events())
                self.assertLessEqual(host.read_budget.calls, 60)

    def test_referenced_original_owner_replay_persists_accounting_without_new_publication_allowance(
        self,
    ):
        state, publisher, prepared = self.prepare(100)
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared, referenced=True)
            first = host.run(AssessingProvider())
            deadline = host.read_budget.wall_deadline_ms
            host.adapter = host.new_adapter()
            host.tracker = ResourceBudgetTracker(ResourceBudget.create())
            provider = AssessingProvider()
            replay = host.run(provider)
            self.assertEqual(replay.reply, first.reply)
            self.assertEqual(provider.calls, [])
            self.assertEqual(
                host.mutations, ["admission-dispatch", "accepted", "replay"]
            )
            self.assertEqual(host.read_budget.calls, 44)
            self.assertEqual(host.read_budget.wall_deadline_ms, deadline)
            with self.assertRaises((ReviewInputError, GitHubPublicationError)):
                publisher.publish(
                    token="synthetic",
                    review_token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=replay,
                    app_slug=APP,
                    pending_report_layout="shared-guidance-v1",
                )
            self.assertEqual(len(state.latest_inventory().pending), 100)
            self.assertNotIn("APPROVE", state.events())
            self.assertLessEqual(host.read_budget.calls, 60)

    def test_competing_local_generation_refuses_before_provider_dispatch(self):
        state, publisher, prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)

            def compete():
                other = LocalSessionLedger(Path(directory))
                other.replace(
                    host.identity,
                    lambda current: current.evolve(generation=current.generation + 1),
                )

            host.before_activation = compete
            provider = AssessingProvider()
            with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
                host.run(provider)
            observed = LocalSessionLedger(Path(directory)).load(host.identity).record
            self.assertIsNone(observed.assessment_queue)
            self.assertEqual(provider.calls, [])
            self.assertEqual(host.mutations, [])
            self.assertEqual(state.latest_inventory().resolved, ())
            self.assertEqual(state.reply_count, 0)

    def test_early_semantic_failure_does_not_starve_late_admitted_target(self):
        state, publisher, prepared = self.prepare(
            targets=lambda ids: tuple([*ids[:4], ids[-1]])
        )
        with tempfile.TemporaryDirectory() as directory:
            host = LocalFeedbackHost(directory, publisher, prepared)

            class EarlyInvalidEvidence(AssessingProvider):
                def complete(self, request):
                    result = super().complete(request)
                    if len(self.calls) == 1:
                        value = json.loads(result.text)
                        value["assessments"][0]["human_evidence"] = "not in this source"
                        return ProviderResponse(
                            json.dumps(value), self.name, self.model
                        )
                    return result

            provider = EarlyInvalidEvidence()
            work = host.run(provider)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(
                [item.fingerprint for item in work.reply.decisions],
                [prepared.feedback.target_ids[-1]],
            )
            self.assertEqual(
                len(work.apply_to(prepared.eligibility.human_review).pending), 39
            )
            observer = LocalSessionLedger(Path(directory))
            root = observer.load(host.identity).record
            envelope = unpack_queue_state(read_session_assessment_queue(observer, root))
            journal = FactoredAssessmentJournal.from_document(envelope["journal"])
            receipt = journal.receipt(
                source_digest=host.source, operation_id=host.operation_id
            )
            self.assertEqual(len(receipt["completed"]), 1)
            self.assertEqual(len(receipt["attempted_ids"]), 5)
            self.assertEqual(host.tracker.structural_retries, 0)
            self.assertLessEqual(host.read_budget.calls, 60)
            self.assertEqual(state.latest_inventory().resolved, ())


if __name__ == "__main__":
    unittest.main()
