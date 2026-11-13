"""One-part lifecycle carrier on the existing real run-owned checkpoint path."""

import copy
import random
import tempfile
import unittest

from test_activation_tail import CurrentHostedFixture

from review_sensei.errors import ReviewInputError
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
                ledger._replace_inventory(items)
                before = len(fixture.service.trace)
                with self.assertRaises(ReviewInputError):
                    ledger.preflight_finding_lifecycle_retention(
                        fixture.identity,
                        added_parts=3 if defect == "operation" else 1,
                        added_bytes=33000,
                        checkpoint_parts=(3,) if defect == "operation" else (1,),
                    )
                self.assertEqual(len(fixture.service.trace), before)
