"""One-part lifecycle carrier on the existing real run-owned checkpoint path."""

import copy
import random
import string
import tempfile
import unittest

from review_sensei_github import api_run_requests as adapter
from review_sensei_github.api_run_plan import NATIVE_OPERATION_REQUESTS
from test_activation_tail import CurrentHostedFixture

from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.finding_lifecycle_store import (
    preflight_finding_lifecycle,
    read_finding_lifecycle,
    stage_finding_lifecycle,
)
from review_sensei.session import session_reservation_id


class CurrentRunFindingPartsTests(unittest.TestCase):
    def fixture(self, directory):
        fixture = CurrentHostedFixture(directory)
        ledger = fixture.ledger
        ledger.enable_finding_lifecycle_writes = True
        head = "b" * 40
        operation = session_reservation_id(
            repository="owner/repo", pull_request=7, head_sha=head, kind="publish"
        )
        record = ledger.replace(
            fixture.identity,
            lambda r: r.evolve(
                generation=r.generation + 1,
                reservation_id=operation,
                reserved_slot="initial",
                failed_attempts_head_sha=head,
            ),
        )
        document = {
            "data": "".join(random.Random(1).choices("0123456789abcdef", k=12000))
        }
        kwargs = dict(
            document=document,
            operation_id=operation,
            base_sha="a" * 40,
            head_sha=head,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=1,
        )
        return fixture, record, kwargs

    def test_actual_part_stage_readback_keeps_original_funds_and_single_page(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs = self.fixture(directory)
            ledger = fixture.ledger
            before = len(fixture.service.trace)
            plan = preflight_finding_lifecycle(
                ledger, record, future_documents=(kwargs["document"],), **kwargs
            )
            self.assertEqual(len(fixture.service.trace), before)
            self.assertEqual([p["parts"] for p in plan["checkpoints"]], [1, 1])
            result = stage_finding_lifecycle(ledger, record, **kwargs)
            dispatched = fixture.service.trace[before:]
            self.assertLessEqual(len(dispatched), 8)
            self.assertIsNone(getattr(ledger, "_finding_lifecycle_page_size", None))
            self.assertEqual(
                result.finding_lifecycle["manifest"]["encoding"], "partitioned-json-v1"
            )
            self.assertEqual(read_finding_lifecycle(ledger, result), kwargs["document"])
            fixture.assert_physical_accounting(self)

    def test_growth_after_proof_leaves_orphan_part_without_root_attach_or_fallback(
        self,
    ):
        from fake_github_http import json_response

        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs = self.fixture(directory)
            ledger = fixture.ledger
            preflight_finding_lifecycle(ledger, record, **kwargs)
            original = fixture.caller._opener
            attempts = []

            def grown(request, timeout):
                attempts.append((request.get_method(), request.full_url))
                if (
                    request.get_method() == "GET"
                    and "/issues/7/comments?" in request.full_url
                ):
                    return json_response(
                        [{"id": i, "body": "discussion"} for i in range(100)]
                    )
                return original(request, timeout)

            fixture.caller._opener = grown
            with self.assertRaisesRegex(
                ReviewInputError, "single-page frontier changed"
            ):
                stage_finding_lifecycle(ledger, record, **kwargs)
            # The source-owned selected input avoids a redundant initial list.
            # The immutable part is staged/read back before the independent
            # preactivation frontier fence refuses; no root can attach it.
            self.assertEqual([method for method, _ in attempts], ["POST", "GET", "GET"])
            self.assertIn("per_page=100&page=1", attempts[-1][1])
            self.assertFalse(any(method == "PATCH" for method, _ in attempts))
            self.assertEqual(
                fixture.caller._effect_record.finding_lifecycle,
                record.finding_lifecycle,
            )
            self.assertIsNone(getattr(ledger, "_finding_lifecycle_page_size", None))

    def test_count_bytes_root_producer_and_operation_caps_refuse_without_dispatch(self):
        for defect in ("count", "bytes", "root", "producer", "operation"):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, record, kwargs = self.fixture(directory)
                ledger = fixture.ledger
                items = copy.deepcopy(ledger._cached_comments())
                if defect == "count":
                    items += [
                        {
                            "id": 9000 + i,
                            "body": "discussion",
                            "user": {"id": 2, "type": "User"},
                        }
                        for i in range(199)
                    ]
                if defect == "bytes":
                    items += [
                        {
                            "id": 9000,
                            "body": "x" * (512 * 1024),
                            "user": {"id": 2, "type": "User"},
                        }
                    ]
                if defect == "root":
                    items[0]["body"] = "not the selected session"
                if defect == "producer":
                    items[0]["user"]["id"] += 1
                if defect == "operation":
                    fixture.caller._effect_record = None
                ledger._replace_inventory(items)
                before = len(fixture.service.trace)
                expected_exc = (
                    EvidenceBudgetExhausted
                    if defect == "operation"
                    else ReviewInputError
                )
                with self.assertRaises(expected_exc) as ctx:
                    ledger.preflight_finding_lifecycle_retention(
                        fixture.identity,
                        added_parts=3 if defect == "operation" else 1,
                        added_bytes=33000,
                        checkpoint_parts=(3,) if defect == "operation" else (1,),
                    )
                if defect == "operation":
                    self.assertIn(
                        "finding lifecycle parts exceed original checkpoint operation",
                        str(ctx.exception),
                    )
                self.assertEqual(len(fixture.service.trace), before)

    def _three_part_document(self, seed: int) -> dict[str, str]:
        alphabet = string.ascii_letters + string.digits + "-_"
        return {"data": "".join(random.Random(seed).choices(alphabet, k=52000))}

    def test_qualified_single_page_three_part_checkpoint_succeeds_within_512_envelope(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs = self.fixture(directory)
            ledger = fixture.ledger
            kwargs["document"] = self._three_part_document(2)
            before_preflight = len(fixture.service.trace)
            plan = preflight_finding_lifecycle(ledger, record, **kwargs)
            self.assertEqual(len(fixture.service.trace), before_preflight)
            self.assertEqual(plan["checkpoints"][0]["parts"], 3)
            page_plan = adapter._PLAIN_FINDING_PLANS[ledger]
            self.assertEqual(page_plan.page_requests, ((100, 1),))
            self.assertEqual(len(page_plan.page_bounds), 1)
            self.assertGreater(plan["conditional_calls"], NATIVE_OPERATION_REQUESTS)
            before_stage = len(fixture.service.trace)
            updated = stage_finding_lifecycle(ledger, record, **kwargs)
            dispatched = fixture.service.trace[before_stage:]
            self.assertLessEqual(len(dispatched), plan["conditional_calls"])
            self.assertEqual(
                read_finding_lifecycle(ledger, updated), kwargs["document"]
            )
            self.assertNotIn(ledger, adapter._PLAIN_FINDING_PLANS)
            self.assertNotIn(ledger, adapter._PLAIN_FINDING_ACTIVE)
            fixture.assert_physical_accounting(self)

    def test_qualified_single_page_three_part_one_short_budget_refuses_without_dispatch(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs = self.fixture(directory)
            ledger = fixture.ledger
            kwargs["document"] = self._three_part_document(3)
            quoted = preflight_finding_lifecycle(ledger, record, **kwargs)[
                "conditional_calls"
            ]
            # Clear any cached page plan and drain original budget down to quoted - 1.
            adapter._PLAIN_FINDING_PLANS.pop(ledger, None)
            budget = ledger.evidence_budget
            drain = budget.ordinary_remaining() - (quoted - 1)
            self.assertGreater(drain, 0)
            budget._ordinary_hold += drain
            self.assertEqual(budget.ordinary_remaining(), quoted - 1)
            before = len(fixture.service.trace)
            with self.assertRaises(EvidenceBudgetExhausted):
                preflight_finding_lifecycle(ledger, record, **kwargs)
            self.assertEqual(len(fixture.service.trace), before)
            self.assertEqual(fixture.caller.accounting.configuration.limit, 512)

    def test_qualified_single_page_three_part_monotonicity_trap_refuses_growth(
        self,
    ):
        from fake_github_http import json_response

        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs = self.fixture(directory)
            ledger = fixture.ledger
            kwargs["document"] = self._three_part_document(4)
            plan = preflight_finding_lifecycle(ledger, record, **kwargs)
            self.assertEqual(plan["checkpoints"][0]["parts"], 3)
            # Adding an external discussion comment between preflight and root PATCH
            # makes the single-page frontier larger than pinned inventory_ids; it must
            # refuse rather than re-paging or falling back to stale capacity.
            original = fixture.caller._opener
            growth_attempts: list[tuple[str, str]] = []
            in_growth_window = False

            def grown(request, timeout):
                if in_growth_window:
                    growth_attempts.append((request.get_method(), request.full_url))
                if (
                    request.get_method() == "GET"
                    and "/issues/7/comments?" in request.full_url
                ):
                    return json_response(
                        [{"id": i, "body": "discussion"} for i in range(1, 101)]
                    )
                return original(request, timeout)

            fixture.caller._opener = grown
            in_growth_window = True
            try:
                with self.assertRaisesRegex(
                    ReviewInputError, "single-page frontier changed"
                ):
                    stage_finding_lifecycle(ledger, record, **kwargs)
            finally:
                in_growth_window = False
            self.assertTrue(any(method == "GET" for method, _ in growth_attempts))
            self.assertFalse(any(method == "PATCH" for method, _ in growth_attempts))
            self.assertEqual(
                fixture.caller._effect_record.finding_lifecycle,
                record.finding_lifecycle,
            )
