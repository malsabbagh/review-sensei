"""Distinct read-only native capacity producer; not the old finalizer experiment."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.action_run import execute_command


class PreentryCapacityTests(unittest.TestCase):
    def test_original_v1_role_remains_unavailable_for_branch_pricing(self):
        self._exercise_source_capacity(original_role=False)

    def test_fresh_source_v2_original_prices_detached_branches_only(self):
        self._exercise_source_capacity(original_role=True)

    def test_branch_quote_exact_original_remaining_104(self):
        self._exercise_source_capacity(original_role=True, remaining=104)

    def test_branch_quote_one_short_103_refuses_before_any_business_write(self):
        self._exercise_source_capacity(original_role=True, remaining=103)

    def test_original_role_source_option_requires_opted_v4_before_bootstrap(self):
        import argparse
        from pathlib import Path

        from review_sensei_github.review_run import bootstrap

        from review_sensei.errors import ReviewInputError

        for value in (True, "true"):
            with self.assertRaisesRegex(ReviewInputError, "requires opted v4"):
                bootstrap(
                    argparse.Namespace(),
                    run_directory=Path("unused"),
                    policy_commit="a" * 40,
                    observe_original_actor_role=value,
                )

    def test_changed_observed_plan_expires_without_price_or_effect(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="payload")

    def test_current_record_virtual_method_is_not_invoked(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="record")

    def test_nested_state_virtual_method_is_not_invoked(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="nested")

    def test_closed_unknown_effect_holds_rejects_callbacks_before_sizing(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_unknown_effect_holds"
        )

    def test_closed_native_finding_context_rejects_callbacks_before_sizing(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_native_finding_context"
        )

    def test_closed_input_identity_rejects_callbacks_before_sizing(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_input_identity"
        )

    def test_closed_accepted_semantic_history_rejects_callbacks_before_sizing(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_accepted_semantic_history"
        )

    def test_closed_review_update_capacity_states_rejects_callbacks_before_sizing(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_review_update_capacity_states"
        )

    def test_closed_batch_body_updates_rejects_callbacks_before_sizing(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_batch_body_updates"
        )

    def test_closed_guarded_publication_pair_rejects_callbacks_before_sizing(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_guarded_publication_pair"
        )

    def test_copy_protocol__reduce_ex___rejects_callback_before_sizing(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="__reduce_ex__")

    def test_copy_protocol__reduce___rejects_callback_before_sizing(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="__reduce__")

    def test_copy_protocol__getstate___rejects_callback_before_sizing(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="__getstate__")

    def test_copy_protocol__setstate___rejects_callback_before_sizing(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="__setstate__")

    def test_copy_protocol__copy___rejects_callback_before_sizing(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="__copy__")

    def test_copy_protocol__deepcopy___rejects_callback_before_sizing(self):
        self._exercise_source_capacity(original_role=True, tamper_plan="__deepcopy__")

    def test_reviewer_copy_reduce_callback_is_not_invoked(self):
        self._exercise_source_capacity(
            original_role=True, tamper_plan="_reviewer_reduce_ex"
        )

    def _exercise_source_capacity(
        self, *, original_role, remaining=None, tamper_plan=False
    ):
        from contextlib import ExitStack
        from dataclasses import replace

        from review_sensei_github import action_run, review_run
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.trust_disposition import (
            DispositionPreparation,
            observe_disposition_capacity,
            prepare_source_disposition,
        )
        from review_sensei_github.trust_effects import enable

        from review_sensei.errors import ReviewInputError
        from tests.test_trust_pause_cold import CurrentRoleEntryFixture

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentRoleEntryFixture(directory)
            f.provider_comments = [
                dict(
                    path="src/app.py",
                    line=2,
                    side="RIGHT",
                    body="Confirm this exact interface assumption.",
                    severity="high",
                    blocking=False,
                    needs_human=True,
                )
            ]
            source_bootstrap = review_run.bootstrap

            def role_bootstrap(*a, **kw):
                return source_bootstrap(*a, observe_original_actor_role=True, **kw)

            with ExitStack() as stack:
                if original_role:
                    stack.enter_context(
                        patch.object(review_run, "bootstrap", role_bootstrap)
                    )
                self.assertEqual(f.opted(f.invoke), 0, f.error)
            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=f.requested_head,
            )
            finding = "RS-" + index.entries[0].instance[:6].upper()
            f.select_command(
                "@reviewsensei override " + finding + " acceptable risk", new_run=True
            )
            captured = []
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        action_run,
                        "execute_command",
                        lambda run, args: captured.append((run, args)) or 0,
                    )
                )
                if original_role:
                    stack.enter_context(
                        patch.object(action_run, "bootstrap", role_bootstrap)
                    )
                self.assertEqual(f.opted(f.invoke_command), 0, f.error)
            run, args = captured[0]
            broker = BrokerClient(
                broker_url=run.broker.broker_url,
                control=run.broker.control,
                opener=run.caller.open,
                authority={**run.broker.authority, "version": 4},
            )
            session = broker.open_session(
                "synthetic-oidc",
                repository_id=123,
                pull_request=7,
                head_sha=run.head_sha,
                observe_actor_role=True,
            )
            run = replace(
                run, session_authority=session.attestation, session_grant=session.grant
            )
            enable(run.caller, broker, session, api_url=run.http.api_url)
            before, root, start = (
                copy.deepcopy(f.comment),
                run.caller._root,
                len(f.trace),
            )
            invoked = []

            def forged_intent(**kwargs):
                invoked.append(True)
                return {"purpose": "forged"}

            with patch.object(run.caller, "action_intent_document", forged_intent):
                with self.assertRaises(ReviewInputError):
                    prepare_source_disposition(run, args)
            self.assertEqual(invoked, [])
            self.assertEqual(f.comment, before)
            self.assertEqual(run.caller._root, root)
            self.assertIsNone(run.caller.accounting.action)
            # Separate paid collection for the subsequent actual source receipt.
            start = len(f.trace)
            provider_calls = f.provider_calls
            observation_start = len(run.caller.observations)
            confirmed_before = run.caller.accounting.confirmed
            terminal_before = next(
                block
                for block in run.caller.accounting.blocks
                if block.block_id == run.caller._terminal_block
            )
            self.assertGreater(terminal_before.undispatched, 0)
            receipt = prepare_source_disposition(run, args)
            for clone in (
                copy.copy(receipt),
                DispositionPreparation(receipt.root, receipt.intent_sha256),
            ):
                with self.assertRaises(ReviewInputError):
                    observe_disposition_capacity(clone)
            for field, changed in (
                ("app_slug", "foreign[bot]"),
                ("source_comment_id", -1),
            ):
                before_reads = len(run.caller.observations)
                with patch.object(args, field, changed):
                    with self.assertRaisesRegex(ReviewInputError, "custody changed"):
                        observe_disposition_capacity(receipt)
                self.assertEqual(len(run.caller.observations), before_reads)
            object.__setattr__(receipt, "root", "f" * 64)
            try:
                with self.assertRaisesRegex(ReviewInputError, "custody changed"):
                    observe_disposition_capacity(receipt)
            finally:
                object.__setattr__(receipt, "root", root)
            before_record_reads = len(run.caller.observations)
            record_callbacks = []
            current_record = run.caller._effect_record
            object.__setattr__(
                current_record, "to_dict", lambda: record_callbacks.append(True)
            )
            try:
                with self.assertRaises(ReviewInputError):
                    observe_disposition_capacity(receipt)
                self.assertEqual(record_callbacks, [])
                self.assertEqual(len(run.caller.observations), before_record_reads)
            finally:
                object.__delattr__(current_record, "to_dict")
            plan = observe_disposition_capacity(receipt)
            plan.require(plan.journal, http=plan.http, budget=plan.budget)
            before_sizing = len(run.caller.observations)
            original_effects = copy.deepcopy(plan.journal.effects)
            branches, pending, item_count, requests, dictionary_base = (
                plan.journal._command_branch_documents(
                    plan, http=plan.http, budget=plan.budget
                )
            )
            self.assertEqual(
                plan.journal._command_branch_documents(
                    plan, http=plan.http, budget=plan.budget
                ),
                (branches, pending, item_count, requests, dictionary_base),
            )
            self.assertTrue(branches)
            self.assertTrue(pending)
            self.assertTrue(requests)
            self.assertGreater(item_count, len(original_effects))
            self.assertEqual(len(plan.journal.effects), len(original_effects))
            self.assertEqual(plan.journal.effects, original_effects)
            self.assertEqual(len(run.caller.observations), before_sizing)
            self.assertIsNone(run.caller.accounting.action)
            with self.assertRaises(ReviewInputError):
                plan.journal._preflight_preentry_command_branches(copy.copy(plan))
            plan_callbacks = []

            def forged_require(*a, **kw):
                plan_callbacks.append(True)

            object.__setattr__(plan, "require", forged_require)
            try:
                with self.assertRaises(ReviewInputError):
                    plan.journal._preflight_preentry_command_branches(plan)
            finally:
                object.__delattr__(plan, "require")
            from review_sensei_github import command_finalization_plan

            with patch.object(
                command_finalization_plan.CommandFinalizationPlan,
                "require",
                forged_require,
            ):
                with self.assertRaises(ReviewInputError):
                    plan.journal._preflight_preentry_command_branches(plan)
            self.assertEqual(plan_callbacks, [])
            body_callbacks = []
            for name in (
                "_body_receipts",
                "_pending_wire",
                "_state",
                "_check_commitment",
            ):
                before_callbacks = len(run.caller.observations)
                with patch.object(
                    plan.journal, name, lambda *a: body_callbacks.append(True)
                ):
                    with self.assertRaises(ReviewInputError):
                        plan.journal._preflight_preentry_command_branches(plan)
                self.assertEqual(len(run.caller.observations), before_callbacks)
            self.assertEqual(body_callbacks, [])
            ledger_callbacks = []
            with patch.object(
                run.ledger, "evidence_producer", lambda: ledger_callbacks.append(True)
            ):
                before_ledger = len(run.caller.observations)
                with self.assertRaises(ReviewInputError):
                    plan.journal._preflight_preentry_command_branches(plan)
                self.assertEqual(len(run.caller.observations), before_ledger)
            self.assertEqual(ledger_callbacks, [])
            original_quote = plan.journal._preflight_preentry_command_branches
            substituted = []
            with patch.object(
                type(plan.journal),
                "_preflight_preentry_command_branches",
                lambda *a, **k: substituted.append(True),
            ):
                with self.assertRaises(ReviewInputError):
                    original_quote(plan)
            self.assertEqual(substituted, [])
            guard_calls = []

            def foreign_guard():
                guard_calls.append(True)

            with patch.object(run.ledger, "business_guard", foreign_guard):
                with self.assertRaisesRegex(ReviewInputError, "existing guard"):
                    plan.journal._preflight_preentry_command_branches(plan)
                self.assertIs(run.ledger.business_guard, foreign_guard)
            self.assertEqual(guard_calls, [])
            if remaining is not None:
                budget = run.caller.budget_view
                self.assertGreaterEqual(budget.ordinary_remaining(), remaining)
                while budget.ordinary_remaining() > remaining:
                    run.http.request("GET", "/repos/owner/repo", token=run.ledger.token)
                self.assertEqual(budget.ordinary_remaining(), remaining)
            if tamper_plan:
                import json

                missing = object()
                callbacks = []
                if tamper_plan == "payload":
                    target, attribute, previous = plan, "payload", plan.payload
                    value = json.loads(previous)
                    value["execution_read_tail"] = {
                        key: 0 for key in value["execution_read_tail"]
                    }
                    replacement = json.dumps(value).encode()
                elif tamper_plan.startswith("_"):
                    target = (
                        plan.journal.reviewer
                        if tamper_plan
                        in (
                            "_unknown_effect_holds",
                            "_native_finding_context",
                            "_reviewer_reduce_ex",
                        )
                        else plan.journal
                    )
                    attribute = (
                        "__reduce_ex__"
                        if tamper_plan == "_reviewer_reduce_ex"
                        else tamper_plan
                    )
                    previous = getattr(target, attribute, missing)

                    class HostileValue:
                        def __contains__(self, key):
                            callbacks.append(True)
                            return False

                        def __iter__(self):
                            callbacks.append(True)
                            return iter(())

                    if attribute.startswith("__"):
                        previous = vars(target).get(attribute, missing)

                        def replacement(*a, **kw):
                            callbacks.append(True)
                            return None
                    else:
                        replacement = HostileValue()
                else:
                    target = (
                        plan.record
                        if tamper_plan == "record"
                        else plan.states[0].inventory.obligations[0].latest_comment
                    )
                    attribute, previous = "to_dict", None
                    self.assertIsNotNone(target)

                    def replacement(*a, **k):
                        return callbacks.append(True)

                object.__setattr__(target, attribute, replacement)
                before_requests = len(run.caller.observations)
                try:
                    with self.assertRaises(ReviewInputError):
                        plan.journal._preflight_preentry_command_branches(plan)
                finally:
                    if previous is missing or (
                        previous is None and attribute == "to_dict"
                    ):
                        object.__delattr__(target, attribute)
                    else:
                        object.__setattr__(target, attribute, previous)
                self.assertEqual(callbacks, [])
                self.assertEqual(len(run.caller.observations), before_requests)
            elif original_role and remaining == 103:
                from review_sensei.errors import EvidenceBudgetExhausted

                with self.assertRaises(EvidenceBudgetExhausted):
                    plan.journal._preflight_preentry_command_branches(plan)
            elif original_role:
                quote = plan.journal._preflight_preentry_command_branches(plan)
                self.assertFalse(quote["whole_route_admitted"])
                self.assertEqual(
                    quote["scope"], "finalization-checkpoint-branches-only"
                )
                self.assertGreater(quote["conditional_calls"], 0)
                self.assertGreater(quote["reserved_tail_calls"], 6)
                self.assertEqual(quote["native_requests"], len(requests))
                self.assertEqual(
                    (quote["conditional_calls"], quote["reserved_tail_calls"]), (57, 47)
                )
            else:
                # A fresh v2 grant cannot invent the missing original role.
                with self.assertRaisesRegex(
                    ReviewInputError, "original actor role is unavailable"
                ):
                    plan.journal._preflight_preentry_command_branches(plan)
            self.assertIsNone(run.ledger.business_guard)
            with self.assertRaises(ReviewInputError):
                plan.journal._preflight_preentry_command_branches(plan)
            count = len(run.caller.observations)
            with self.assertRaisesRegex(ReviewInputError, "unissued"):
                observe_disposition_capacity(receipt)
            self.assertEqual(len(run.caller.observations), count)
            self.assertEqual(plan.record.record_sha256, root)
            self.assertIsNone(plan.record.api_run_accounting.get("action"))
            self.assertEqual(f.provider_calls, provider_calls)
            observations = run.caller.observations[observation_start:]
            self.assertTrue(observations)
            self.assertTrue(all(item["outcome"] == "response" for item in observations))
            # The existing action-source verification and capability exchange
            # are the two control POSTs. Neither is a native mutation.
            # The actual source GraphQL discovery is charged, but not a mutation.
            self.assertEqual(sum(item["mutation"] for item in observations), 2)
            self.assertEqual(len(observations), len(f.trace[start:]))
            self.assertEqual(
                run.caller.accounting.confirmed - confirmed_before, len(observations)
            )
            self.assertEqual(
                next(
                    block
                    for block in run.caller.accounting.blocks
                    if block.block_id == run.caller._terminal_block
                ),
                terminal_before,
            )
            self.assertIsNone(run.caller._terminal_reason)
            self.assertEqual(
                [
                    path
                    for _, method, path in f.trace[start:]
                    if method == "POST" and path != "/graphql"
                ],
                ["/api/github/session-grant", "/api/github/token"],
            )
            self.assertTrue(
                any(
                    method == "POST" and path == "/graphql"
                    for _, method, path in f.trace[start:]
                )
            )
            self.assertEqual(f.comment, before)
            self.assertEqual(run.caller._root, root)
            self.assertIsNone(run.caller.accounting.action)
            self.assertFalse(
                any(
                    method != "GET" and path.startswith("/repos/")
                    for _, method, path in f.trace[start:]
                )
            )
            self.assertEqual(run.caller.accounting.configuration.limit, 512)
            # A truthful detached preview never creates a native permit.
            with self.assertRaises(ReviewInputError):
                execute_command(run, args)
            self.assertEqual(f.comment, before)
