"""Actual plain command boundary qualification for receipt source issuance."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import quarantine_command_execution
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_journey import JourneyFixture, registered_resource

from review_sensei.errors import ReviewInputError


class ReceiptFaultFixture(JourneyFixture):
    def __init__(self, directory, fault):
        super().__init__(directory)
        self.fault = fault
        self.receipt_active = False
        self.receipt_attempts = 0
        self.rest_owner_reads = 0

    def external(self, request, timeout):
        body = json.loads(request.data).get("body", "") if request.data else ""
        receipt = (
            request.get_method() in {"POST", "PATCH"}
            and "reviewsensei:command-receipt:v1" in body
        )
        if receipt:
            self.receipt_attempts += 1
            if self.fault == "absent":
                self._meter(request)
                raise TimeoutError("synthetic absent receipt response")
            response = super().external(request, timeout)
            if self.fault == "stored":
                raise TimeoutError("synthetic stored receipt response loss")
            return response
        if (
            self.receipt_active
            and self.fault == "growth"
            and request.get_method() == "GET"
            and urlsplit(request.full_url).path.endswith("/issues/7/comments")
        ):
            row = next(iter(self.extra_comments.values()))
            row["body"] += " unplanned growth"
            self.receipt_active = False
        response = super().external(request, timeout)
        if (
            self.fault == "stored"
            and request.get_method() == "GET"
            and "/issues/comments/" in urlsplit(request.full_url).path
            and response.status == 200
        ):
            from fake_github_http import json_response

            row = json.loads(response.read(524288))
            if isinstance(row, dict) and isinstance(row.get("user"), dict):
                row["user"].update(
                    node_id="BOT_NODE_1",
                    avatar_url="https://example.invalid/avatar.png",
                )
                self.rest_owner_reads += 1
                return json_response(row)
        return response


class ReceiptIssuerTests(unittest.TestCase):
    def test_actual_plain_command_activates_source_lease_and_rejects_copy(self):
        self.exercise()

    def test_stored_receipt_loss_reconciles_without_retry(self):
        self.exercise("stored")

    def test_absent_receipt_loss_keeps_unknown_without_retry(self):
        self.exercise("absent")

    def test_late_body_growth_refuses_receipt_before_write(self):
        self.exercise("growth")

    def exercise(self, fault=None):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = ReceiptFaultFixture(directory, fault)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                inventories = (
                    fixture.selected.ledger._quarantine_authenticated.inventories
                )
                origin = next(iter(inventories))
                instance = inventories[origin]["assessments"][0]["instance"]
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + instance
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                issued = []
                phases = []
                replayed = []

                def observe(frame, event, arg):
                    if (
                        frame.f_code
                        is quarantine_command_execution.close_receipt_phase.__code__
                        and event == "return"
                    ):
                        with self.assertRaisesRegex(
                            ReviewInputError, "unissued or consumed"
                        ):
                            quarantine_command_execution.validate_receipt_phase(
                                frame.f_locals["lease"],
                                frame.f_locals["run"],
                                frame.f_locals["authority"],
                            )
                        replayed.append(True)
                    if event == "return" and frame.f_code in (
                        quarantine_command_execution.prepare_receipt_phase.__code__,
                        quarantine_command_execution.require_receipt_phase.__code__,
                    ):
                        phases.append((frame.f_code.co_name, sorted(frame.f_locals)))
                    if (
                        frame.f_code
                        is quarantine_command_execution.require_receipt_phase.__code__
                        and event == "return"
                        and arg is not None
                    ):
                        issued.append(arg)
                        fixture.receipt_active = True
                        run, authority = (
                            frame.f_locals["run"],
                            frame.f_locals["authority"],
                        )
                        with self.assertRaises(ReviewInputError):
                            quarantine_command_execution.validate_receipt_phase(
                                copy.copy(arg), run, authority
                            )
                        with self.assertRaises(ReviewInputError):
                            quarantine_command_execution.validate_receipt_phase(
                                arg, copy.copy(run), authority
                            )
                        ordinary = run.caller._block
                        try:
                            run.caller._block = run.caller._terminal_block
                            with self.assertRaises(ReviewInputError):
                                quarantine_command_execution.validate_receipt_phase(
                                    arg, run, authority
                                )
                        finally:
                            run.caller._block = ordinary
                        root = run.caller._root
                        try:
                            run.caller._root = "0" * 64
                            with self.assertRaises(ReviewInputError):
                                quarantine_command_execution.validate_receipt_phase(
                                    arg, run, authority
                                )
                        finally:
                            run.caller._root = root
                        quarantine_command_execution.validate_receipt_phase(
                            arg, run, authority
                        )

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    result = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(
                    result == 0, fault not in {"absent", "growth"}, fixture.error
                )
                self.assertEqual(
                    fixture.receipt_attempts, 0 if fault == "growth" else 1
                )
                if fault in {"stored", "absent"}:
                    unknown = [
                        item
                        for item in fixture.accounted.observations
                        if item.get("mutation") and item.get("outcome") == "unknown"
                    ]
                    self.assertTrue(unknown)
                    self.assertGreater(fixture.accounted.accounting.uncertain, 0)
                self.assertEqual(
                    len(issued),
                    1,
                    "command silently used conservative receipt fallback: "
                    + repr(phases),
                )

                self.assertEqual(replayed, [True])
                if fault == "stored":
                    self.assertGreater(fixture.rest_owner_reads, 0)
                current = fixture.current_record()
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"],
                    "pending" if fault in {"absent", "growth"} else "terminal",
                )
                if fault not in {"absent", "growth"}:
                    from review_sensei_github.api_run_history import read_action_result

                    from review_sensei.disposition import OverrideReceipt
                    from review_sensei.presentation import render_command_receipt

                    retained = read_action_result(
                        fixture.selected.ledger, fixture.selected.identity, current
                    )
                    self.assertEqual(
                        retained["outcome"], {"kind": "applied", "reason": None}
                    )
                    durable = retained["result"]
                    self.assertTrue(durable["applied"])
                    self.assertFalse(durable["evaluation_failed"])
                    digest = hashlib.sha256(fixture.command_body.encode()).hexdigest()
                    text = render_command_receipt(
                        actor=fixture.source["user"]["login"],
                        source_comment_id=fixture.source["id"],
                        command_digest=digest,
                        receipts=tuple(
                            OverrideReceipt(**row) for row in durable["receipts"]
                        ),
                        ledger_written=True,
                    )
                    receipts = [
                        row
                        for row in fixture.extra_comments.values()
                        if row.get("body") == text
                    ]
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(
                        receipts[0]["user"]["id"],
                        fixture.selected.ledger._evidence_author_id,
                    )
                    self.assertEqual(receipts[0]["user"]["type"], "Bot")
