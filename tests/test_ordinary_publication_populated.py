"""Fresh populated ordinary admission retains omitted authoritative criteria."""

import json
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import GitHubWriteOptions, ReviewPublisher
from review_sensei_github import ordinary_publication_plan as planning
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.finding_lifecycle import restore_finding_lifecycle
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from tests import test_api_work_lifecycle as lifecycle
from tests.test_activation_tail import CurrentHostedFixture
from tests.test_api_work_persistence import IDENTITY


class OrdinaryPopulatedPublicationTests(unittest.TestCase):
    def test_two_original_criteria_one_active_slot_are_wholly_prepaid(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CurrentHostedFixture(directory, limit=getattr(self, "limit", 512))
            self.fixture = fixture
            if getattr(self, "lose_root", False):
                original_open = fixture.caller._opener
                lost = []

                def stored_loss(request, timeout):
                    # This legacy service's generic check GET returns a list.
                    # Model the actual exact endpoint used by receipt readback,
                    # as the current ordinary-entry fixture already does.
                    from copy import deepcopy
                    from urllib.parse import urlsplit

                    from tests.fake_github_http import json_response

                    path = urlsplit(request.full_url).path
                    if request.get_method() == "GET" and "/check-runs/" in path:
                        fixture.service.trace.append(
                            (fixture.service.attempt, "GET", path)
                        )
                        found = next(
                            (
                                c
                                for c in fixture.service.check_runs
                                if str(c["id"]) == path.rsplit("/", 1)[-1]
                            ),
                            None,
                        )
                        return json_response(deepcopy(found), 200 if found else 404)
                    response = original_open(request, timeout)
                    key = None
                    if request.get_method() == "POST" and request.full_url.endswith(
                        "/pulls/7/reviews"
                    ):
                        key = "root"
                    elif (
                        getattr(self, "lose_checks", False)
                        and request.get_method() in {"POST", "PATCH"}
                        and "/check-runs" in request.full_url
                    ):
                        key = request.get_method()
                    if key is not None and key not in lost:
                        lost.append(key)
                        raise TimeoutError("synthetic populated stored loss")
                    return response

                fixture.caller._opener = stored_loss
            response = lifecycle.ProviderResponse
            admitted = []
            original_admit = planning.admit_current

            def two_comments(**kwargs):
                document = json.loads(kwargs["text"])
                if document["comments"]:
                    second = dict(document["comments"][0])
                    second["body"] = (
                        "Confirm the external retention policy permits keeping this payload; the policy is not present in this repository."
                    )
                    document["comments"].append(second)
                    if getattr(self, "body_only", False):
                        for comment in document["comments"]:
                            comment["line"] = 999
                    kwargs["text"] = json.dumps(document)
                return response(**kwargs)

            def admit(*args, **kwargs):
                result = original_admit(*args, **kwargs)
                admitted.append(len(kwargs["original_result"].comments))
                return result

            with (
                patch.object(lifecycle, "ProviderResponse", side_effect=two_comments),
                patch.object(
                    lifecycle,
                    "GitHubWriteOptions",
                    side_effect=lambda **kw: GitHubWriteOptions(
                        **kw, max_active_findings=1
                    ),
                ),
                patch.object(planning, "admit_current", admit),
            ):
                service, publication, calls = lifecycle.CallerLifecycleTests().journey(
                    directory, prepared=(fixture.service, fixture.native), human=True
                )
            self.assertEqual(admitted, [2])
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                len(service.inline), 0 if getattr(self, "body_only", False) else 1
            )
            self.assertFalse(any(r["state"] == "APPROVED" for r in service.reviews))
            self.assertEqual(service.check_runs[-1]["conclusion"], "action_required")
            selected = parse_session_comment(service.comment["body"], identity=IDENTITY)
            accounting = ApiRunAccounting.from_document(selected.api_run_accounting)
            self.assertEqual(accounting.configuration.limit, 512)
            fixture.assert_physical_accounting(self)
            native = fixture.native
            fresh = ReviewPublisher(http=service.http)
            _, document = restore_finding_lifecycle(
                fresh,
                ApiRunSessionLedger(native, service.accounted, IDENTITY),
                IDENTITY,
            )
            self.assertIsNotNone(document)
            state = fresh._active_window_states[
                (IDENTITY.repository_id, IDENTITY.pull_request)
            ]
            obligations = state.authoritative_decision_inventory.obligations
            self.assertEqual(len(obligations), 2)
            self.assertTrue(all(ob.comment.needs_human for ob in obligations))
            self.assertEqual(len({ob.comment.body for ob in obligations}), 2)
            self.assertEqual(
                len(
                    {(ob.obligation_id, ob.resolution_criterion) for ob in obligations}
                ),
                2,
            )
            self.assertTrue(
                all(ob.resolution_proof_sha256 is None for ob in obligations)
            )
            slots = state.display_active_findings.slots
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].state, "active")
            receipts = document["native_receipts"]
            self.assertEqual(len(receipts), 1)
            if getattr(self, "body_only", False):
                self.assertIsNone(receipts[0]["comment_id"])
                self.assertEqual(receipts[0]["review_id"], service.reviews[0]["id"])
            else:
                self.assertEqual(receipts[0]["comment_id"], service.inline[0]["id"])
            if getattr(self, "lose_root", False):
                self.assertEqual(
                    set(lost),
                    {"root", "POST", "PATCH"}
                    if getattr(self, "lose_checks", False)
                    else {"root"},
                )
                self.assertGreater(service.accounted.accounting.uncertain, 0)

            from review_sensei_github.review_finalization_diagnostics import (
                close_review_run,
            )

            close_review_run(
                service.accounted,
                ApiRunSessionLedger(native, service.accounted, IDENTITY),
                IDENTITY,
                selected.transaction,
            )
            terminal = parse_session_comment(service.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(
                ApiRunAccounting.from_document(
                    terminal.api_run_accounting
                ).terminal_transaction
            )

    def test_two_criteria_stored_lost_root_rebinds_only_one_native_slot(self):
        self.lose_root = True
        self.test_two_original_criteria_one_active_slot_are_wholly_prepaid()

    def test_original_small_allowance_holds_two_criteria_before_native_effects(self):
        from review_sensei.errors import EvidenceBudgetExhausted

        self.limit = lifecycle.QUALIFICATION_LIMIT
        with self.assertRaises(EvidenceBudgetExhausted):
            self.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
        self.assertEqual(self.fixture.service.reviews, [])
        self.assertEqual(self.fixture.service.check_runs, [])

    def test_source_derived_one_short_holds_populated_tail_before_any_native_write(
        self,
    ):
        from review_sensei_github.api_run_budget import CallerRunBudget

        from review_sensei.errors import EvidenceBudgetExhausted

        original = planning.admit_prefix_branches
        quotes = []

        def quote(journal, *args, **kwargs):
            available = journal.ledger.evidence_budget.ordinary_remaining()
            plan = original(journal, *args, **kwargs)
            quotes.append(
                (available, plan["conditional_calls"] + plan["reserved_tail_calls"])
            )
            return plan

        with patch.object(planning, "admit_prefix_branches", quote):
            self.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
        self.assertEqual(len(quotes), 1)
        available, required = quotes[0]
        self.limit = 512 - 2 * (available - required + 1)
        self.assertLess(self.limit, 512)
        real_preflight = CallerRunBudget.preflight
        failures = []
        active = False

        def observe_preflight(budget, calls):
            try:
                return real_preflight(budget, calls)
            except EvidenceBudgetExhausted:
                if active:
                    failures.append((calls, budget.ordinary_remaining()))
                raise

        def short(journal, *args, **kwargs):
            nonlocal active
            active = True
            try:
                return original(journal, *args, **kwargs)
            finally:
                active = False

        with (
            patch.object(planning, "admit_prefix_branches", short),
            patch.object(CallerRunBudget, "preflight", observe_preflight),
            self.assertRaises(EvidenceBudgetExhausted),
        ):
            self.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
        self.assertEqual(failures, [(required, required - 1)])
        self.assertEqual(self.fixture.service.reviews, [])
        self.assertEqual(self.fixture.service.check_runs, [])

    def test_changed_adoption_query_keeps_lost_root_pending_without_repost(self):
        from review_sensei_github import ordinary_native_recovery as recovery

        from review_sensei.errors import ReviewInputError

        self.lose_root = True
        original = recovery.dispatch
        changed = []

        def substitute(caller, request, observation):
            capture = recovery._ACTIVE.get(caller)
            if (
                capture is not None
                and len(capture.reads) == 2
                and request.full_url.endswith("/graphql")
            ):
                body = json.loads(request.data)
                body["variables"]["number"] += 1
                request.data = json.dumps(body).encode()
                changed.append(True)
            return original(caller, request, observation)

        with (
            patch.object(recovery, "dispatch", substitute),
            self.assertRaises(ReviewInputError),
        ):
            self.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
        service = self.fixture.service
        self.assertEqual(changed, [True])
        self.assertEqual(len(service.reviews), 1)
        self.assertEqual(service.reviews[0]["state"], "COMMENTED")
        self.assertEqual(len(service.inline), 1)
        unknown = [
            o
            for o in service.accounted.observations
            if o.get("mutation") and o["outcome"] == "unknown"
        ]
        self.assertTrue(unknown)
        self.assertTrue(
            all(
                o["request_id"] not in service.accounted._reconciled_mutations
                for o in unknown
            )
        )
        record = parse_session_comment(service.comment["body"], identity=IDENTITY)
        self.assertIsNone(
            ApiRunAccounting.from_document(
                record.api_run_accounting
            ).terminal_transaction
        )

    def test_populated_all_three_native_losses_preserve_hidden_criterion(self):
        self.lose_root = self.lose_checks = True
        self.test_two_original_criteria_one_active_slot_are_wholly_prepaid()
