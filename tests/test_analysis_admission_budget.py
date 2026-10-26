"""Current hosted admission uses one caller owner without hosted phase windows."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.api_run_host import ApiRunReviewHooks
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.review_hooks import GitHubReviewHooks

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from tests.test_api_work_lifecycle import LifecycleFixture
from tests.test_api_work_persistence import BASE, HEAD, IDENTITY


class AnalysisAdmissionBudgetTests(unittest.TestCase):
    def hooks(self, directory, *, actions_token=None):
        fixture = LifecycleFixture(directory, limit=512)
        native, config, _, owner = fixture.prelude()
        fixture.accounted.initialize_and_activate(native, IDENTITY, config, owner)
        ledger = ApiRunSessionLedger(native, fixture.accounted, IDENTITY)
        return fixture, ApiRunReviewHooks(
            ledger,
            IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            actions_token=actions_token,
        )

    def test_rescan_reuses_startup_actions_capability_only_for_dispatch(self):
        for event, expected in (
            ("issue_comment", ""),
            ("workflow_dispatch", "synthetic-startup-actions"),
        ):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                fixture, hooks = self.hooks(
                    directory, actions_token="synthetic-startup-actions"
                )
                before = len(fixture.trace)
                budget = fixture.accounted.budget_view
                args = SimpleNamespace()
                with (
                    patch.dict(os.environ, {"GITHUB_EVENT_NAME": event}),
                    patch(
                        "review_sensei_github.broker_client.BrokerClient",
                        side_effect=AssertionError("no rescan broker exchange"),
                    ),
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("no hosted phase window"),
                    ),
                    patch(
                        "review_sensei_github.rescan.resolve_rescan",
                        return_value="authorized",
                    ) as resolve,
                ):
                    self.assertEqual(
                        hooks.resolve_rescan(args, hooks.ledger), "authorized"
                    )
                positional, keywords = resolve.call_args
                self.assertEqual(positional, (args, hooks.ledger))
                self.assertEqual(keywords["actions_token"], expected)
                request, reference = object(), {"synthetic": "reference"}
                keywords["_source_observer"](request, reference)
                self.assertIs(hooks.rescan_request, request)
                self.assertIs(hooks.prior_inventory_reference, reference)
                self.assertIs(hooks.ledger.evidence_budget, budget)
                self.assertEqual(len(fixture.trace), before)

    def test_dispatch_without_current_startup_capability_refuses_before_http(self):
        for token in (None, "", 123):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as directory:
                fixture, hooks = self.hooks(directory, actions_token=token)
                before = len(fixture.trace)
                with (
                    patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}),
                    patch("review_sensei_github.rescan.resolve_rescan") as resolve,
                    self.assertRaisesRegex(ReviewInputError, "Actions capability"),
                ):
                    hooks.resolve_rescan(SimpleNamespace(), hooks.ledger)
                resolve.assert_not_called()
                self.assertEqual(len(fixture.trace), before)

    def test_foreign_ledger_cannot_borrow_current_rescan_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, hooks = self.hooks(directory, actions_token="synthetic")
            before = len(fixture.trace)
            with (
                patch("review_sensei_github.rescan.resolve_rescan") as resolve,
                self.assertRaisesRegex(ReviewInputError, "admitted caller ledger"),
            ):
                hooks.resolve_rescan(SimpleNamespace(), object())
            resolve.assert_not_called()
            self.assertEqual(len(fixture.trace), before)

    def test_phases_keep_one_budget_with_no_refunds_or_replacement_charges(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, hooks = self.hooks(directory)
            caller, ledger = fixture.accounted, hooks.ledger
            ledger.http.request("GET", "/repos/owner/repo/pulls/7", token=ledger.token)
            before = caller.accounting
            budget = caller.budget_view
            with patch.object(
                EvidenceReadBudget,
                "__init__",
                side_effect=AssertionError("no hosted phase allowance"),
            ):
                self.assertIs(hooks.begin_provider_phase(ledger), ledger)
                self.assertIs(hooks.begin_completion_phase(ledger), ledger)
                self.assertIs(hooks.begin_completion_phase(ledger), ledger)
            self.assertEqual(caller.accounting, before)
            self.assertIs(ledger.evidence_budget, budget)
            self.assertEqual(budget.calls, before.confirmed + before.uncertain)
            with self.assertRaisesRegex(ReviewInputError, "physical transport"):
                budget.calls = 0
            with self.assertRaisesRegex(ReviewInputError, "cannot be replaced"):
                budget.wall_deadline_ms = 1
            self.assertEqual(caller.accounting, before)
            with self.assertRaisesRegex(ReviewInputError, "admitted run"):
                hooks.begin_provider_phase(ledger)

    def test_last_funded_get_then_atomic_refusal_keeps_original_allocations(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, hooks = self.hooks(directory)
            caller, ledger = fixture.accounted, hooks.ledger
            terminal = next(
                b
                for b in caller.accounting.blocks
                if b.block_id == caller._terminal_block
            )
            while caller.usable:
                ledger.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token=ledger.token
                )
            before, count = caller.accounting, len(fixture.trace)
            with self.assertRaises(EvidenceBudgetExhausted):
                caller.budget_view.preflight(1)
            with self.assertRaises(ReviewInputError):
                ledger.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token=ledger.token
                )
            self.assertEqual(caller.accounting, before)
            self.assertEqual(len(fixture.trace), count)
            self.assertEqual(
                next(b for b in before.blocks if b.block_id == terminal.block_id),
                terminal,
            )

    def test_retired_hosted_phase_entries_refuse_before_broker_or_http(self):
        with (
            patch(
                "review_sensei_github.broker_client.BrokerClient",
                side_effect=AssertionError("retired broker"),
            ),
            patch(
                "review_sensei_github.http.GitHubHttp",
                side_effect=AssertionError("retired HTTP"),
            ),
        ):
            hooks = GitHubReviewHooks()
            with self.assertRaisesRegex(
                ReviewInputError, "current review-run lifecycle"
            ):
                hooks.resolve_ledger(SimpleNamespace(), resolved_head_sha=HEAD)
            with self.assertRaisesRegex(
                ReviewInputError, "current review-run lifecycle"
            ):
                hooks.resolve_rescan(SimpleNamespace(), object())

    def test_actual_entry_worker_fanout_never_multiplies_or_refunds_caller_posts(self):
        from tests.test_review_run import EntryFixture

        measurements = []
        for fanout in (9, 60):
            with (
                self.subTest(fanout=fanout),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = EntryFixture(directory)
                fixture.fanout = fanout
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                posts = [
                    observation
                    for (_, method, path), observation in zip(
                        fixture.trace, fixture.accounted.observations, strict=True
                    )
                    if method == "POST" and path == "/api/github/token"
                ]
                self.assertEqual(len(posts), len(fixture.worker_observations))
                self.assertEqual(len(posts), 4)
                self.assertTrue(
                    all(o["service_ceiling"] == 60 for o in fixture.worker_observations)
                )
                self.assertTrue(
                    all(
                        o["observed_dispatches"] == fanout
                        for o in fixture.worker_observations
                    )
                )
                state = fixture.accounted.accounting
                measurements.append(
                    (
                        len(fixture.trace),
                        state.confirmed,
                        state.uncertain,
                        state.remaining,
                        state.reserved_undispatched,
                    )
                )
        self.assertEqual(measurements[0], measurements[1])

    def test_actual_entry_changed_worker_receipt_keeps_post_charge_and_refuses(self):
        from tests.test_review_run import EntryFixture

        for field in ("request_id", "operation_id", "deadline_unix_ms"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = EntryFixture(directory)
                original = fixture.broker_response

                def response(payload):
                    document = original(payload)
                    receipt = document["control_receipt"]
                    receipt[field] = (
                        receipt[field] + 1 if field == "deadline_unix_ms" else "f" * 64
                    )
                    return document

                fixture.broker_response = response
                self.assertEqual(fixture.invoke(), 1)
                self.assertIn("control receipt", fixture.error)
                self.assertEqual(fixture.provider_calls, 0)
                self.assertIsNone(fixture.comment)
                self.assertEqual(len(fixture.worker_observations), 1)
                self.assertEqual(
                    fixture.accounted.startup_dispatches, len(fixture.trace)
                )
                self.assertEqual(
                    len(fixture.accounted.observations), len(fixture.trace)
                )
                self.assertTrue(
                    all(
                        o["outcome"] == "response"
                        for o in fixture.accounted.observations
                    )
                )
