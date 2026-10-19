"""Finite trusted-library 250-path selection, actual ledger and captured HTTP."""

import base64
import copy
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from review_sensei_github import ReviewPublisher
from review_sensei_github.eligibility_parts import (
    BoundPartitionedEligibilityReader,
)
from review_sensei_github.errors import (
    GitHubConversationError,
    GitHubHTTPResponseTooLargeError,
    GitHubPublicationError,
)
from review_sensei_github.human_assessment import HumanAssessmentPublisher
from review_sensei_github.partitioned_assessment import PartitionedAssessmentPlan
from review_sensei_github.prose_publication import CommentReviewProseStore

from review_sensei.assessment_queue import pack_queue_state
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import evidence_digest
from review_sensei.feedback import FeedbackReference, FeedbackSelector
from review_sensei.session import LocalSessionLedger, mutate_commit
from tests.test_evidence_capacity import human_result, publication_diff, varied_comments
from tests.test_human_assessment import APP, BASE, DIFF, HEAD
from tests.test_public_feedback_continuation import LocalFeedbackHost
from tests.test_review_transaction import CONFIGURATION
from tests.test_review_work import AssessingProvider
from tests.test_rich250_public_writer import LocalPublicationHost, RichState


class CapturedFeedbackHost(LocalFeedbackHost):
    def activate(self, request):
        accepted = super().activate(request)
        self.accepted_state = accepted
        self.accepted_manifest_digest = evidence_digest(
            pack_queue_state(self.last_envelope, queue=self.queue)
        )
        return accepted


class LocalResolutionHost:
    """One serialized, terminal local attempt; no fresh-process replay claim."""

    def __init__(self, queue_host, context, prepared, work, transport_owner):
        self.queue_host = queue_host
        self.prepared = prepared
        self.work = work
        self.transport = transport_owner
        self.plan = PartitionedAssessmentPlan(
            context,
            queue_host.accepted_state.root_generation + 1,
            prepared.feedback.event_key,
            prepared.authority_digest,
            7,
            1,
        )
        self.accepted_context = None
        self.unknown = False
        self.lock = threading.Lock()
        self.before_activation = None

    def activate(self, request, *, verify):
        with self.lock:
            if self.unknown:
                raise ReviewInputError("original resolution outcome is unknown")
            if self.accepted_context is not None:
                record = self.queue_host.ledger.load(self.queue_host.identity).record
                if (
                    record is None
                    or record.operator_paused
                    or record.generation != self.accepted_context.activation_generation
                    or record.last_committed_reservation_id
                    != self.queue_host.reservation
                    or record.reservation_id is not None
                    or request != self.last_request
                ):
                    raise ReviewInputError("committed resolution operation changed")
                verify(self.accepted_context)
                return self.accepted_context

            self.last_request = request
            context = None

            def mutate(record):
                nonlocal context
                if (
                    record.operator_paused
                    or record.generation != self.plan.activation_generation - 1
                    or record.reservation_id != self.queue_host.reservation
                    or request.plan != self.plan
                    or request.assessments
                    != tuple(item.to_dict() for item in self.work.reply.decisions)
                    or record.assessment_queue["state_manifest"]["sha256"]
                    != self.queue_host.accepted_manifest_digest
                ):
                    raise ReviewInputError(
                        "original accepted resolution operation changed"
                    )
                if self.before_activation:
                    self.before_activation()
                self.unknown = True
                value = self.transport.request(
                    "POST",
                    "/repos/owner/repo/pulls/1/reviews",
                    {"body": request.body, "event": "COMMENT", "commit_id": HEAD},
                )
                identifier = value.get("id") if isinstance(value, dict) else None
                if type(identifier) is not int or identifier <= 0:
                    raise ReviewInputError("resolution owned root ID is invalid")
                root = self.transport.request(
                    "GET", f"/repos/owner/repo/pulls/1/reviews/{identifier}"
                )
                store = self.prepared_reader.prose_store
                if (
                    store._validate_readback(root, identifier, HEAD).body
                    != request.body
                ):
                    raise ReviewInputError("resolution owned root body changed")
                context = replace(
                    self.plan.context,
                    owned_root_id=identifier,
                    activation_generation=self.plan.activation_generation,
                )
                # Actual publisher performs its own fresh whole-report/prose and
                # source/head checks before this terminal generation is committed.
                verify(context)
                return mutate_commit(
                    record,
                    reservation_id=self.queue_host.reservation,
                    expected_generation=record.generation,
                    now=self.queue_host.now,
                )

            accepted = self.queue_host.ledger.replace(
                self.queue_host.identity, mutate, now=self.queue_host.now
            )
            if accepted.generation != context.activation_generation:
                raise ReviewInputError("resolution activation generation changed")
            self.accepted_context = context
            self.unknown = False
            return context


class Rich250LateFeedbackTests(unittest.TestCase):
    def fixture(
        self,
        directory,
        *,
        before_run=None,
        after_provider=None,
        feedback_budget=None,
        history_count=0,
        review_history_count=0,
    ):
        state = RichState(history_count=history_count)
        comments = varied_comments(250, detail=1, unique_paths=250)
        state.files = [
            {"filename": item.path, "patch": DIFF, "additions": 1, "deletions": 1}
            for item in comments
        ]
        state.source["user"]["id"] = 42
        result = human_result(comments)
        policy = ReviewConvergencePolicy(enforcement="publication")
        config = dict(CONFIGURATION, publication_mode=policy.mode)
        owner = LocalPublicationHost(directory, state, result, policy, config)
        receipt = ReviewPublisher(http=state.http).publish_partitioned(
            token="synthetic",
            host=owner,
            result=result,
            diff=publication_diff(comments),
            configuration_context=config,
            convergence_policy=policy,
            app_slug=APP,
            check_token="synthetic-checks",
        )
        completed_initial = owner.ledger.commit(
            owner.identity,
            reservation_id=owner.reservation,
            expected_generation=receipt.context.activation_generation,
        )
        # New non-App review records are distinct from unrelated issue comments.
        # They arrive after the initial trusted root; the later source must scan
        # all of them, rather than borrowing a prepared-context fixture.
        state.reviews.extend(
            {
                "id": 200000 + index,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": f"Ordinary human review {index}",
                "pull_request_url": state.http.api_url + "/repos/owner/repo/pulls/1",
                "user": {"id": 500, "login": "bob", "type": "User"},
            }
            for index in range(review_history_count)
        )
        state.source["updated_at"] = "assessment-1"
        budget = feedback_budget or EvidenceReadBudget()
        transport = copy.copy(owner)
        transport.budget = budget
        reader = BoundPartitionedEligibilityReader(
            replace(receipt.context, budget=budget),
            transport.read,
            CommentReviewProseStore(
                http=state.http,
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                producer_id=12345,
                app_slug=APP,
                budget=budget,
            ),
            receipt.body,
        )
        publisher = HumanAssessmentPublisher(
            http=state.http, before_read=budget.consume, partitioned_reader=reader
        )
        target = sorted(
            item.fingerprint for item in receipt.eligibility.human_review.findings
        )[-1]
        reference = FeedbackReference("issue", 10, "assessment-1")
        selector = FeedbackSelector(
            "owner/repo", 1, BASE, HEAD, reference, (reference,), (target,)
        )
        conversation = publisher.conversation.prepare_selected_assessment_context(
            token="synthetic", selector=selector, app_slug=APP
        )
        prepared = publisher.prepare_feedback(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=conversation,
            selector=selector,
            app_slug=APP,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        queue_host = CapturedFeedbackHost(
            directory,
            publisher,
            prepared,
            referenced=True,
            existing_session=True,
            existing_generation=completed_initial.generation,
        )
        provider = AssessingProvider()
        self.provider = provider
        self.state = state
        self.queue_host = queue_host
        if before_run is not None:
            before_run(state, publisher, prepared, queue_host)
        if after_provider is not None:
            original_complete = provider.complete

            def complete(request):
                response = original_complete(request)
                after_provider(state, publisher, prepared, queue_host)
                return response

            provider.complete = complete
        work = queue_host.run(provider)
        self.assertEqual(budget.calls, 40 + 4 * ((5 + review_history_count) // 100))
        host = LocalResolutionHost(
            queue_host, reader.context, prepared, work, transport
        )
        host.prepared_reader = reader
        return state, publisher, prepared, work, provider, host

    def test_late_full_id_updates_249_pending_authority_and_exact_replay_under_original_cap(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            state, publisher, prepared, work, provider, host = self.fixture(directory)
            original = prepared.eligibility.human_review.inventory_document()

            def publish():
                return publisher.publish_partitioned(
                    token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    work=work,
                    host=host,
                    app_slug=APP,
                )

            self.assertEqual(len(prepared.eligibility.human_review.findings), 250)
            self.assertEqual(len(prepared.evidence_bundle.records), 250)
            self.assertTrue(prepared.evidence_bundle.enumeration_complete)
            self.assertEqual(
                {item.path for item in prepared.evidence_bundle.records},
                {item.path for item in prepared.eligibility.human_review.findings},
            )
            first = publish()
            self.assertEqual(host.plan.context.budget.calls, 53)
            self.assertIsNone(
                host.queue_host.ledger.load(
                    host.queue_host.identity
                ).record.reservation_id
            )
            self.assertEqual(first.approval_status, "approval_withheld")
            latest = publisher.finalizer.load_eligibility(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                require_valid=True,
            )
            self.assertEqual(len(latest.human_review.pending), 249)
            self.assertEqual(latest.human_review.resolved, prepared.feedback.target_ids)
            self.assertEqual(latest.human_review.inventory_document(), original)
            roots = len(state.reviews)
            second = publish()
            self.assertEqual(second.status, "already_replied")
            self.assertEqual(len(state.reviews), roots)
            self.assertEqual(len(provider.calls), 1)
            self.assertLessEqual(host.plan.context.budget.calls, 60)
            self.assertNotIn("APPROVE", state.events())

    def publish(self, values):
        state, publisher, prepared, work, provider, host = values
        return publisher.publish_partitioned(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            work=work,
            host=host,
            app_slug=APP,
        )

    @staticmethod
    def change_prose(state, fault):
        item = state.reviews[0]
        if fault == "body":
            item["body"] += " changed"
        elif fault == "missing":
            state.reviews.pop(0)
        elif fault == "duplicate":
            state.reviews.insert(1, copy.deepcopy(item))
        elif fault == "owner":
            item["user"]["id"] += 1
        elif fault == "head":
            item["commit_id"] = "c" * 40
        elif fault == "placement":
            item["pull_request_url"] += "0"
        elif fault in {"generation", "resolution", "prefix"}:
            root = state.reviews[-1]
            prefix, marker = root["body"].split("<!-- reviewsensei:eligibility:v1 ")
            document = json.loads(base64.urlsafe_b64decode(marker.removesuffix(" -->")))
            if fault == "generation":
                document["activation_generation"] += 1
            elif fault == "resolution":
                # Schema-valid in-place resolution edit is not a host activation.
                document["human_resolution"]["resolved"] = ["a" * 64]
            else:
                prefix += "unowned prefix edit"
            root["body"] = (
                prefix
                + "<!-- reviewsensei:eligibility:v1 "
                + base64.urlsafe_b64encode(
                    json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                ).decode()
                + " -->"
            )
        else:
            raise AssertionError(fault)

    def test_fresh_whole_listing_faults_before_dispatch_make_zero_provider_calls(self):
        for fault in (
            "body",
            "missing",
            "duplicate",
            "owner",
            "head",
            "placement",
            "generation",
            "prefix",
            "resolution",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:

                def changed(state, publisher, prepared, host):
                    self.change_prose(state, fault)

                with self.assertRaises(
                    (GitHubPublicationError, GitHubConversationError, ReviewInputError)
                ):
                    self.fixture(directory, before_run=changed)
                self.assertEqual(len(self.provider.calls), 0)
                self.assertFalse(hasattr(self.queue_host, "accepted_state"))
                self.assertNotIn("APPROVE", self.state.events())

    def test_fresh_whole_listing_faults_after_provider_do_not_accept_or_publish(self):
        for fault in (
            "body",
            "missing",
            "duplicate",
            "owner",
            "head",
            "placement",
            "generation",
            "prefix",
            "resolution",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:

                def changed(state, publisher, prepared, host):
                    self.change_prose(state, fault)

                with self.assertRaises(
                    (GitHubPublicationError, GitHubConversationError, ReviewInputError)
                ):
                    self.fixture(directory, after_provider=changed)
                self.assertEqual(len(self.provider.calls), 1)
                self.assertNotIn("accepted", self.queue_host.mutations)
                self.assertNotIn("APPROVE", self.state.events())

    def test_changed_source_head_root_and_prose_before_resolution_never_write_a_root(
        self,
    ):
        for fault in (
            "body",
            "missing",
            "duplicate",
            "owner",
            "head",
            "placement",
            "generation",
            "prefix",
            "resolution",
            "source",
            "PRhead",
            "pause",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                values = self.fixture(directory)
                state, publisher, prepared, work, provider, host = values
                if fault == "source":
                    state.source["body"] += " changed"
                elif fault == "PRhead":
                    state.head = "c" * 40
                elif fault == "pause":
                    LocalSessionLedger(Path(directory)).replace(
                        host.queue_host.identity,
                        lambda record: record.evolve(
                            operator_paused=True, generation=record.generation + 1
                        ),
                    )
                else:
                    self.change_prose(state, fault)
                posts = sum(
                    method == "POST" and path.endswith("/reviews")
                    for method, path, _ in state.calls
                )
                with self.assertRaises(
                    (GitHubPublicationError, GitHubConversationError, ReviewInputError)
                ):
                    self.publish(values)
                self.assertEqual(
                    sum(
                        method == "POST" and path.endswith("/reviews")
                        for method, path, _ in state.calls
                    ),
                    posts,
                )
                self.assertNotIn("APPROVE", state.events())

    def test_actual_lost_root_post_or_readback_holds_original_without_fresh_retry(self):
        for phase in ("POST", "GET"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                values = self.fixture(directory)
                state, publisher, prepared, work, provider, host = values
                original = host.transport.request

                def lost(method, path, body=None):
                    value = original(method, path, body)
                    if method == phase and (
                        path.endswith("/pulls/1/reviews")
                        if phase == "POST"
                        else "/pulls/1/reviews/" in path
                    ):
                        raise GitHubPublicationError(
                            "synthetic lost resolution response"
                        )
                    return value

                host.transport.request = lost
                with self.assertRaisesRegex(GitHubPublicationError, "lost resolution"):
                    self.publish(values)
                self.assertTrue(host.unknown)
                record = (
                    LocalSessionLedger(Path(directory))
                    .load(host.queue_host.identity)
                    .record
                )
                self.assertEqual(record.reservation_id, host.queue_host.reservation)
                roots = len(state.reviews)
                with self.assertRaisesRegex(ReviewInputError, "outcome is unknown"):
                    host.activate(host.last_request, verify=lambda context: None)
                self.assertEqual(len(state.reviews), roots)
                self.assertIsNone(host.accepted_context)
                self.assertNotIn("APPROVE", state.events())

    def test_prose_mutation_after_root_post_cannot_acknowledge_terminal_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, publisher, prepared, work, provider, host = values
            original = host.transport.request

            def changed(method, path, body=None):
                value = original(method, path, body)
                if method == "POST" and path.endswith("/pulls/1/reviews"):
                    self.change_prose(state, "body")
                return value

            host.transport.request = changed
            with self.assertRaises(
                (GitHubPublicationError, GitHubConversationError, ReviewInputError)
            ):
                self.publish(values)
            self.assertTrue(host.unknown)
            self.assertIsNone(host.accepted_context)
            record = (
                LocalSessionLedger(Path(directory))
                .load(host.queue_host.identity)
                .record
            )
            self.assertEqual(record.reservation_id, host.queue_host.reservation)
            self.assertNotIn("APPROVE", state.events())

    def test_every_resolution_dispatch_uses_remaining_deadline_and_expiry_dispatches_nothing(
        self,
    ):
        for elapsed in (59.0, 61.0):
            with (
                self.subTest(elapsed=elapsed),
                tempfile.TemporaryDirectory() as directory,
            ):
                clock = [0.0]
                values = self.fixture(
                    directory,
                    feedback_budget=EvidenceReadBudget(clock=lambda: clock[0]),
                )
                state, publisher, prepared, work, provider, host = values
                clock[0] = elapsed
                original = state.http.opener
                timeouts = []

                def capture(request, timeout):
                    timeouts.append(timeout)
                    return original(request, timeout)

                state.http.opener = capture
                before = len(state.calls)
                if elapsed == 59:
                    self.publish(values)
                    self.assertEqual(len(timeouts), 8)
                    self.assertTrue(all(0 < value <= 1 for value in timeouts), timeouts)
                else:
                    with self.assertRaises(ReviewInputError):
                        self.publish(values)
                    self.assertEqual(state.calls[before:], [])
                    self.assertEqual(timeouts, [])
                    self.assertIsNone(host.accepted_context)

    def test_missing_fresh_list_page_and_duplicate_bound_root_refuse_before_provider(
        self,
    ):
        for fault in ("page-failure", "duplicate-root"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:

                def changed(state, publisher, prepared, host):
                    if fault == "duplicate-root":
                        state.reviews.append(copy.deepcopy(state.reviews[-1]))
                    else:
                        original = state.http.opener

                        def fail(request, timeout):
                            if request.method == "GET" and urlparse(
                                request.full_url
                            ).path.endswith("/pulls/1/reviews"):
                                raise GitHubHTTPResponseTooLargeError(
                                    "synthetic oversized individual review"
                                )
                            return original(request, timeout)

                        state.http.opener = fail

                with self.assertRaises((GitHubPublicationError, ReviewInputError)):
                    self.fixture(directory, before_run=changed)
                self.assertEqual(len(self.provider.calls), 0)

    def test_completed_source_cannot_restore_a_fresh_attempt_or_call_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, publisher, prepared, work, provider, host = values
            self.publish(values)
            replacement = AssessingProvider()
            with self.assertRaises((GitHubPublicationError, ReviewInputError)):
                host.queue_host.run(replacement)
            self.assertEqual(replacement.calls, [])
            restored = EvidenceReadBudget(snapshot=host.plan.context.budget.snapshot())
            ledger = LocalSessionLedger(
                Path(directory), evidence_budget=restored, enable_partition_writes=True
            )
            with self.assertRaisesRegex(ReviewInputError, "cannot be restarted"):
                ledger.reserve_for_tail(
                    host.queue_host.identity,
                    operation_binding=host.queue_host.operation,
                    slot="verification",
                    reservation_id=host.queue_host.reservation,
                    expected_generation=host.accepted_context.activation_generation,
                )

    def test_live_whole_listing_after_terminal_ack_rechecks_historical_prose(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, publisher, prepared, work, provider, host = values
            self.publish(values)
            self.change_prose(state, "body")
            roots = len(state.reviews)
            with self.assertRaises((GitHubPublicationError, ReviewInputError)):
                self.publish(values)
            self.assertEqual(len(state.reviews), roots)
            self.assertEqual(len(provider.calls), 1)
            self.assertNotIn("APPROVE", state.events())

    def test_serialized_competing_public_resolution_writers_create_one_root(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, publisher, prepared, work, provider, host = values
            activate = host.activate
            barrier = threading.Barrier(2)

            def together(request, *, verify):
                barrier.wait(timeout=10)
                return activate(request, verify=verify)

            host.activate = together
            results = []
            errors = []

            def run():
                try:
                    results.append(self.publish(values))
                except Exception as exc:
                    errors.append(exc)

            roots = len(state.reviews)
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(len(state.reviews), roots + 1)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(host.plan.context.budget.calls, 60)
            self.assertNotIn("APPROVE", state.events())

    def test_bound_reader_keeps_original_budget_exact_root_bytes_and_copy_isolation(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, publisher, prepared, work, provider, host = values
            reader = publisher.finalizer.partitioned_reader
            manifest = prepared.eligibility.to_dict()["human_inventory"]
            before = reader.context.budget.calls
            first = reader.read(
                manifest,
                expected_binding=reader.context.binding,
                budget=reader.context.budget,
            )
            expected = copy.deepcopy(first)
            first.clear()
            self.assertEqual(
                reader.read(
                    manifest,
                    expected_binding=reader.context.binding,
                    budget=reader.context.budget,
                ),
                expected,
            )
            self.assertEqual(reader.context.budget.calls, before)
            with self.assertRaisesRegex(ReviewInputError, "original budget"):
                reader.read(
                    manifest,
                    expected_binding=reader.context.binding,
                    budget=EvidenceReadBudget(),
                )
            with self.assertRaises(ReviewInputError):
                HumanAssessmentPublisher(
                    http=state.http,
                    before_read=EvidenceReadBudget().consume,
                    partitioned_reader=reader,
                )
            calls = len(state.calls)
            with self.assertRaisesRegex(
                GitHubConversationError, "explicit conditional"
            ):
                publisher.publish(
                    token="synthetic",
                    review_token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=work,
                    app_slug=APP,
                )
            self.assertEqual(len(state.calls), calls)

    def test_unrelated_issue_history_0_100_250_keeps_complete_cold_delivery_and_replay(
        self,
    ):
        for count in (0, 100, 250):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                values = self.fixture(directory, history_count=count)
                state, publisher, prepared, work, provider, host = values
                self.assertEqual(len(state.history), count)
                self.publish(values)
                self.assertEqual(host.plan.context.budget.calls, 53)
                self.publish(values)
                self.assertEqual(host.plan.context.budget.calls, 57)
                self.assertFalse(
                    any(
                        method == "GET" and path.endswith("/issues/1/comments")
                        for method, path, _ in state.calls
                    )
                )
                self.assertIn(
                    "Current pending findings: **249**", state.reviews[-1]["body"]
                )
                self.assertEqual(len(provider.calls), 1)
                self.assertNotIn("APPROVE", state.events())

    def test_actual_review_history_pagination_has_explicit_bounded_completion_and_refusal(
        self,
    ):
        for count in (100, 250):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                values = self.fixture(directory, review_history_count=count)
                state, publisher, prepared, work, provider, host = values
                if count == 100:
                    self.assertEqual(host.plan.context.budget.calls, 44)
                    self.publish(values)
                    self.assertEqual(host.plan.context.budget.calls, 59)
                    self.assertIn(
                        "Current pending findings: **249**", state.reviews[-1]["body"]
                    )
                    roots = len(state.reviews)
                    with self.assertRaisesRegex(ReviewInputError, "cannot stage"):
                        self.publish(values)
                    self.assertEqual(host.plan.context.budget.calls, 59)
                    self.assertEqual(len(state.reviews), roots)
                else:
                    self.assertEqual(host.plan.context.budget.calls, 48)
                    roots = len(state.reviews)
                    with self.assertRaisesRegex(ReviewInputError, "cannot stage"):
                        self.publish(values)
                    self.assertEqual(host.plan.context.budget.calls, 53)
                    self.assertEqual(len(state.reviews), roots)
                    self.assertFalse(host.unknown)
                    self.assertIsNone(host.accepted_context)
                    self.assertEqual(
                        len(prepared.eligibility.human_review.pending), 250
                    )
                self.assertEqual(len(provider.calls), 1)
                self.assertNotIn("APPROVE", state.events())

    def test_actual_concurrent_ledger_generation_after_acceptance_never_acks_a_root(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, publisher, prepared, work, provider, host = values

            def compete():
                LocalSessionLedger(Path(directory)).replace(
                    host.queue_host.identity,
                    lambda record: record.evolve(
                        generation=record.generation + 1, operator_paused=True
                    ),
                )

            host.before_activation = compete
            with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
                self.publish(values)
            self.assertTrue(host.unknown)
            self.assertIsNone(host.accepted_context)
            self.assertEqual(len(provider.calls), 1)
            self.assertNotIn("APPROVE", state.events())

    def test_source_and_head_changes_during_root_write_are_fenced_before_ack(self):
        for fault in ("source", "head"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                values = self.fixture(directory)
                state, publisher, prepared, work, provider, host = values
                original = host.transport.request

                def changed(method, path, body=None):
                    value = original(method, path, body)
                    if method == "POST" and path.endswith("/pulls/1/reviews"):
                        if fault == "source":
                            state.source["body"] += " changed"
                        else:
                            state.head = "c" * 40
                    return value

                host.transport.request = changed
                with self.assertRaises(
                    (GitHubPublicationError, GitHubConversationError, ReviewInputError)
                ):
                    self.publish(values)
                self.assertTrue(host.unknown)
                self.assertIsNone(host.accepted_context)
                self.assertNotIn("APPROVE", state.events())

    def test_operator_pause_before_actual_dispatch_makes_zero_provider_calls(self):
        with tempfile.TemporaryDirectory() as directory:

            def paused(state, publisher, prepared, host):
                LocalSessionLedger(Path(directory)).replace(
                    host.identity,
                    lambda record: record.evolve(
                        operator_paused=True, generation=record.generation + 1
                    ),
                )

            with self.assertRaises(ReviewInputError):
                self.fixture(directory, before_run=paused)
            self.assertEqual(self.provider.calls, [])
            self.assertNotIn("APPROVE", self.state.events())

    def test_original_context_budget_root_policy_and_source_bindings_refuse_before_io(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, publisher, prepared, work, provider, host = values
            original = host.plan
            contexts = [
                replace(
                    original.context, owned_root_id=original.context.owned_root_id + 1
                ),
                replace(
                    original.context,
                    activation_generation=original.context.activation_generation + 1,
                ),
                replace(
                    original.context,
                    binding=dict(
                        original.context.binding, configuration_digest="e" * 64
                    ),
                ),
                replace(original.context, budget=EvidenceReadBudget()),
            ]
            plans = [replace(original, context=context) for context in contexts] + [
                replace(original, source_digest="e" * 64),
                replace(original, authority_digest="e" * 64),
            ]
            before = len(state.calls)
            for plan in plans:
                with self.subTest(plan=plan):
                    host.plan = plan
                    with self.assertRaisesRegex(
                        GitHubConversationError, "original authority"
                    ):
                        self.publish(values)
                    self.assertEqual(len(state.calls), before)
                    self.assertEqual(len(provider.calls), 1)
                    self.assertIsNone(host.accepted_context)
            host.plan = original
