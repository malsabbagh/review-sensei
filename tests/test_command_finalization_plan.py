"""Capacity preparation observes source data without issuing business effects."""

import copy
import json
import sys
import unittest
from contextlib import contextmanager

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from tests import test_plain_cleanup_fence as cleanup_fixtures


@contextmanager
def observe_original_calls(observer):
    """Inspect source-owned frames without replacing authenticated methods."""
    previous = sys.getprofile()
    try:
        sys.setprofile(observer)
        yield
    finally:
        sys.setprofile(previous)


class CommandFinalizationPlanTests(unittest.TestCase):
    def prepare(self, finalizer, args):
        return finalizer.prepare_command_capacity(
            token="synthetic",
            check_token="synthetic-check",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=args["head_sha"],
            app_slug=args["app_slug"],
            actor="maintainer",
            finding_ids=(),
            dispositions=(),
        )

    def test_actual_closed_preparation_only_reads_and_keeps_original_state(self):
        state, finalizer, args, calls = (
            cleanup_fixtures.PlainCleanupFenceTests().fixture()
        )
        finalizer.evidence_budget = EvidenceReadBudget()
        before = copy.deepcopy(state.reviews)
        trust = finalizer._trust_decisions
        plan = self.prepare(finalizer, args)
        plan.require(
            finalizer._finding_lifecycle_journal,
            http=finalizer.http,
            budget=finalizer.evidence_budget,
        )
        self.assertEqual(state.reviews, before)
        self.assertIs(finalizer._trust_decisions, trust)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        self.assertEqual(json.loads(plan.payload)["cleanup_requests"], [])
        self.assertFalse(finalizer._command_capacity_requested)
        with self.assertRaises(ReviewInputError):
            copy.copy(plan).require(
                finalizer._finding_lifecycle_journal,
                http=finalizer.http,
                budget=finalizer.evidence_budget,
            )
        finalizer._finding_lifecycle_journal.operation_id = "changed"
        with self.assertRaises(ReviewInputError):
            plan.require(
                finalizer._finding_lifecycle_journal,
                http=finalizer.http,
                budget=finalizer.evidence_budget,
            )

    def test_execution_read_guard_never_requests_a_second_page(self):
        from types import SimpleNamespace

        from review_sensei_github.command_finalization_plan import ClosedCommandReads

        for payload in (
            [{"id": 1}] * 100,
            {"data": {"threads": {"pageInfo": {"hasNextPage": True}}}},
        ):
            calls = []

            def request(*args, **kwargs):
                calls.append((args, kwargs))
                return 200, payload

            bounded = ClosedCommandReads(SimpleNamespace(request=request))
            with self.assertRaises(ReviewInputError):
                if isinstance(payload, list):
                    bounded.paginate(path="/reviews", token="synthetic")
                else:
                    bounded.request(
                        "POST",
                        "/graphql",
                        token="synthetic",
                        body={"query": "query Test { viewer { id } }"},
                    )
            self.assertEqual(len(calls), 1)

    def test_unknown_wrapper_and_narrower_pagination_never_get_widened(self):
        from types import SimpleNamespace

        from review_sensei_github.command_finalization_plan import (
            ClosedCommandReads,
            _underlying_http,
        )
        from review_sensei_github.http import GitHubHttp

        base = GitHubHttp(api_url="https://api.github.test")
        with self.assertRaises(ReviewInputError):
            _underlying_http(SimpleNamespace(transport=base), budget=None)
        calls = []
        bounded = ClosedCommandReads(
            SimpleNamespace(request=lambda *a, **k: calls.append(a))
        )
        for options in (
            {"page_sizes": (50, 25)},
            {"max_requests": 0},
            {"max_items": 5},
            {"expected_items": 5},
        ):
            with self.subTest(options=options), self.assertRaises(ReviewInputError):
                bounded.paginate(path="/reviews", token="synthetic", **options)
        self.assertEqual(calls, [])

    def test_custom_wrapper_cannot_register_same_journal_and_inner_http(self):
        from review_sensei_github.command_finalization_plan import (
            register_journal_transport,
        )
        from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
        from review_sensei_github.http import GitHubHttp

        calls = []

        class Foreign:
            def request(self, *args, **kwargs):
                calls.append(True)

            def preflight_native_batch(self, *args, **kwargs):
                calls.append(True)

        journal = object.__new__(FindingLifecycleJournal)
        with self.assertRaises(ReviewInputError):
            register_journal_transport(
                Foreign(), journal, GitHubHttp(api_url="https://api.github.test")
            )
        self.assertEqual(calls, [])

    def test_unknown_callback_refuses_before_it_can_run_or_read(self):
        for name in (
            "historical_assessment_reader_factory",
            "quarantine_reader_factory",
            "_preflight",
            "attach_carried",
            "historical_reader",
            "load_eligibility",
        ):
            with self.subTest(name=name):
                state, finalizer, args, calls = (
                    cleanup_fixtures.PlainCleanupFenceTests().fixture()
                )
                invoked = []
                setattr(finalizer, name, lambda *a, **k: invoked.append(True))
                with self.assertRaisesRegex(ReviewInputError, "source is unsupported"):
                    self.prepare(finalizer, args)
                self.assertEqual(invoked, [])
                self.assertEqual(calls, [])

    def test_prepaid_check_credential_is_exact_and_never_replaced_by_review_token(self):
        from review_sensei_github.command_finalization_plan import (
            attach_execution_reads,
        )

        _, finalizer, args, calls = cleanup_fixtures.PlainCleanupFenceTests().fixture()
        finalizer.evidence_budget = EvidenceReadBudget()
        plan = self.prepare(finalizer, args)
        observed = len(calls)
        for credential in (None, "synthetic", "foreign-check"):
            with (
                self.subTest(credential=credential),
                self.assertRaisesRegex(
                    ReviewInputError, "execution read scope changed"
                ),
            ):
                attach_execution_reads(
                    plan,
                    finalizer,
                    finalizer._finding_lifecycle_journal,
                    dispositions=(),
                    check_token=credential,
                )
        self.assertEqual(len(calls), observed)

    def test_actual_owned_cas_reuse_rejects_clones_foreign_ledger_and_changed_document(
        self,
    ):
        from unittest.mock import patch

        from review_sensei_github.command_finalization_plan import (
            CommandFinalizationPlan,
        )

        from review_sensei.session import SessionRecord
        from tests.test_command_capacity_binding import CommandCapacityBindingTests

        original = CommandFinalizationPlan.reuse_retained_lifecycle
        seen = []

        def checked(plan, ledger, record, *, dispositions):
            seen.append(record.record_sha256)
            for altered_plan, altered_ledger, altered_record, altered_dispositions in (
                (copy.copy(plan), ledger, record, dispositions),
                (plan, object(), record, dispositions),
                (plan, ledger, SessionRecord.from_dict(record.to_dict()), dispositions),
                (plan, ledger, record, ()),
            ):
                with self.assertRaises(ReviewInputError):
                    original(
                        altered_plan,
                        altered_ledger,
                        altered_record,
                        dispositions=altered_dispositions,
                    )
            document = copy.deepcopy(plan.journal._document())
            document["head_sha"] = "f" * 40
            with patch.object(plan.journal, "_document", return_value=document):
                with self.assertRaisesRegex(ReviewInputError, "custody changed"):
                    original(plan, ledger, record, dispositions=dispositions)
            result = original(plan, ledger, record, dispositions=dispositions)
            self.assertIs(result[0], record)
            self.assertEqual(result[1], plan.journal._document())
            result[1]["head_sha"] = "e" * 40
            self.assertNotEqual(result[1], plan.journal._document())
            return original(plan, ledger, record, dispositions=dispositions)

        def observe(frame, event, arg):
            if frame.f_code is original.__code__ and event == "call":
                values = frame.f_locals
                checked(
                    values["self"],
                    values["ledger"],
                    values["record"],
                    dispositions=values["dispositions"],
                )

        with observe_original_calls(observe):
            CommandCapacityBindingTests(
                "test_default_budget_two_finding_command_and_cold_verify_use_one_check_capability"
            ).test_default_budget_two_finding_command_and_cold_verify_use_one_check_capability()
        self.assertEqual(len(seen), 2)

    def test_source_command_cleanup_failure_never_uses_old_green_or_approval(self):
        import tempfile
        from unittest.mock import patch

        from review_sensei_github.publication import (
            ReviewApprovalFinalizer,
            parse_finding_index,
        )
        from review_sensei_github.thread_resolution import ThreadResolutionResult

        from tests.test_action_run import HEAD, CommandEntryFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                dict(
                    path="src/app.py",
                    line=2,
                    side="RIGHT",
                    body=f"Confirm interface assumption {number}.",
                    severity="high",
                    blocking=False,
                    needs_human=True,
                )
                for number in range(2)
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            instances = tuple(entry.instance for entry in index.entries)
            fixture.SOURCE_ID += 1
            fixture.select_command(
                "@reviewsensei override "
                + " ".join("RS-" + instance[:6].upper() for instance in instances)
                + " acceptable risk",
                new_run=True,
            )
            observed = []

            def unresolved(finalizer, **kwargs):
                from review_sensei_github.ordered_native_batch import (
                    issue_ordered_batch,
                )

                journal = finalizer._finding_lifecycle_journal
                requests = tuple(("POST", "/graphql", {"target": i}) for i in range(2))
                complete = (
                    issue_ordered_batch(journal, requests).approve().bind(requests)
                )
                for request in requests:
                    complete.before_request(*request)
                    complete.confirm_current()
                complete.close()
                self.assertFalse(getattr(journal, "_command_native_stopped", False))
                stopped = (
                    issue_ordered_batch(journal, requests).approve().bind(requests)
                )
                stopped.stop()
                self.assertTrue(journal._command_native_stopped)
                observed.append(copy.deepcopy((fixture.reviews, fixture.check_runs)))
                return ThreadResolutionResult(unconfirmed=instances)

            with (
                patch.object(
                    ReviewApprovalFinalizer, "reconcile_decided_threads", unresolved
                ),
                patch.object(
                    ReviewApprovalFinalizer,
                    "_reviews_have_app_head_state",
                    return_value=True,
                ) as old_approval,
                patch.object(
                    ReviewApprovalFinalizer,
                    "finalize",
                    side_effect=AssertionError("approval after stopped cleanup"),
                ),
            ):
                self.assertEqual(fixture.invoke_command(), 1, fixture.error)
                old_approval.assert_not_called()
            self.assertEqual(len(observed), 1)
            self.assertEqual((fixture.reviews, fixture.check_runs), observed[0])
            self.assertIn("gate re-evaluation failed", fixture.error)
            self.assertNotIn('"approval_state":"already_published"', fixture.error)
            self.assertEqual(fixture.resolved_threads, set())

    def test_real_dictionary_plan_custody_rejects_tampering_without_io(self):
        from review_sensei_github import command_finalization_plan as plans
        from review_sensei_github.finding_lifecycle import _unpack_journal

        from tests.test_command_capacity_binding import CommandCapacityBindingTests

        original_bind = plans.CommandFinalizationPlan.bind_dictionary_base
        original_attach = plans.attach_execution_reads
        bound = []
        exercised = []

        def bind(plan, base):
            before = len(plan.journal.ledger.caller.observations)
            with self.assertRaises(ReviewInputError):
                original_bind(copy.copy(plan), base)
            changed = copy.deepcopy(base)
            changed["lifecycle"]["schema_version"] = "changed"
            with self.assertRaises(ReviewInputError):
                original_bind(plan, changed)
            self.assertEqual(len(plan.journal.ledger.caller.observations), before)

        def attach(plan, finalizer, journal, **kwargs):
            if plan not in bound:
                return
            before = len(journal.ledger.caller.observations)
            with self.assertRaises(ReviewInputError):
                original_attach(plan, finalizer, journal, **kwargs)
            pending = dict(plans._PENDING[plan])
            if not pending:
                self.assertIsNone(
                    plans.pack_authorized_pending(journal, journal._document())
                )
                self.assertEqual(len(journal.ledger.caller.observations), before)
                exercised.append(plan)
                return
            packed = next(iter(pending.values()))
            logical = _unpack_journal(json.loads(packed))
            expected = plans.pack_authorized_pending(journal, logical)
            self.assertEqual(expected["schema_version"], "finding-lifecycle-packed-v4")
            self.assertIsNone(
                plans.pack_authorized_pending(copy.copy(journal), logical)
            )
            changed = copy.deepcopy(logical)
            changed["unknown_trust_effect"] = not changed["unknown_trust_effect"]
            self.assertIsNone(plans.pack_authorized_pending(journal, changed))
            for name, foreign in (
                ("ledger", object()),
                ("head_sha", "e" * 40),
                ("operation_id", "f" * 64),
            ):
                current = getattr(journal, name)
                try:
                    setattr(journal, name, foreign)
                    with self.assertRaises(ReviewInputError):
                        plans.pack_authorized_pending(journal, logical)
                finally:
                    setattr(journal, name, current)
            original_payload = plan.payload
            try:
                object.__setattr__(plan, "payload", b"changed")
                with self.assertRaises(ReviewInputError):
                    plans.pack_authorized_pending(journal, logical)
            finally:
                object.__setattr__(plan, "payload", original_payload)
            expected["dictionary_manifest"]["sha256"] = "e" * 64
            self.assertNotEqual(
                plans.pack_authorized_pending(journal, logical), expected
            )
            self.assertEqual(len(journal.ledger.caller.observations), before)
            exercised.append(plan)

        def observe(frame, event, arg):
            values = frame.f_locals
            if frame.f_code is original_bind.__code__:
                plan, base = values["self"], values["base"]
                if event == "call":
                    bind(plan, base)
                elif event == "return":
                    before = len(plan.journal.ledger.caller.observations)
                    with self.assertRaises(ReviewInputError):
                        original_bind(plan, base)
                    self.assertEqual(
                        len(plan.journal.ledger.caller.observations), before
                    )
                    bound.append(plan)
            elif frame.f_code is original_attach.__code__ and event == "return":
                attach(
                    values["plan"],
                    values["finalizer"],
                    values["journal"],
                    dispositions=values["dispositions"],
                    check_token=values["check_token"],
                )

        with observe_original_calls(observe):
            CommandCapacityBindingTests(
                "test_default_budget_two_finding_command_and_cold_verify_use_one_check_capability"
            ).test_default_budget_two_finding_command_and_cold_verify_use_one_check_capability()
        self.assertTrue(bound)
        self.assertEqual(exercised, bound)
