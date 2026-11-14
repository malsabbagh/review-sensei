"""A closed review walk cannot replace lifetime native allocation promises."""

import copy
import unittest

import test_historical_invocation_census as comments
from review_sensei_github import historical_review_census as reviews
from review_sensei_github import quarantine_scan_capacity as scan

from review_sensei.errors import ReviewInputError


class HistoricalReviewCensusTests(unittest.TestCase):
    def test_original_c4_command_uses_complete_one_page_native_reads(self):
        completed, diagnostic = [], []

        def observe(frame, event, result):
            if event == "return" and frame.f_code is scan.capacity_review_walk.__code__:
                diagnostic.append(
                    (
                        "scan",
                        tuple(frame.f_locals),
                        str(result),
                        str(type(frame.f_locals.get("reader"))),
                        str(type(frame.f_locals.get("base"))),
                        str(frame.f_locals.get("path")),
                    )
                )
            if event == "return" and frame.f_code in (
                reviews.issue.__code__,
                reviews.choose.__code__,
            ):
                diagnostic.append(
                    (
                        frame.f_code.co_name,
                        tuple(frame.f_locals),
                        str(result),
                        frame.f_locals.get("maximum"),
                        frame.f_locals.get("largest"),
                    )
                )
            if event != "return" or frame.f_code is not reviews.finish.__code__:
                return
            values = frame.f_locals
            if "raw" not in values:
                return
            token, rows = values["token"], values["rows"]
            self.assertEqual(len(values["observed"]), 1)
            self.assertEqual(token.requests, 1)
            self.assertEqual(len(rows), len(values["saved"]["known"]))
            self.assertEqual(
                values["entry"]["guard"].certificates["reviews"][
                    "physical_reads_upper_bound"
                ],
                3,
            )
            for candidate in (copy.copy(token), token):
                with self.assertRaisesRegex(ReviewInputError, "copied or foreign"):
                    candidate.validate(rows)
            completed.append(token)

        comments.HistoricalInvocationCensusTests().exercise(extra_observer=observe)
        self.assertEqual(len(completed), 3, diagnostic)

    def exercise_fault(self, fault):
        import json
        import sys
        import tempfile
        from unittest.mock import patch
        from urllib.parse import parse_qs, urlsplit

        from fake_github_http import json_response
        from review_sensei_github import quarantine_historical_execution as historical
        from test_quarantine_historical_execution import GuardedHistoricalFixture
        from test_quarantine_journey import registered_resource
        from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS

        class Fixture(GuardedHistoricalFixture):
            native_attempts = 0
            fake_calls = 0

            def external(self, request, timeout):
                response = super().external(request, timeout)
                url = urlsplit(request.full_url)
                if (
                    self.closed_started
                    and request.get_method() == "GET"
                    and url.path.endswith("/pulls/7/reviews")
                    and parse_qs(url.query).get("per_page") == ["6"]
                ):
                    self.native_attempts += 1
                    if self.native_attempts == 1:
                        if fault == "loss":
                            raise TimeoutError("lost closed native review response")
                        if fault == "export":

                            def fake(*args):
                                self.fake_calls += 1

                            reviews.finish = fake
                            return response
                        rows = json.loads(response.read(512 * 1024))
                        if fault == "foreign":
                            extra = copy.deepcopy(rows[-1])
                            extra["id"] = 999999999
                            rows.append(extra)
                        elif fault == "missing":
                            rows.pop()
                        elif fault == "malformed":
                            rows[0]["id"] = []
                        elif fault == "wire":
                            rows[0]["extra_metadata"] = "x" * 400000
                        else:
                            raise AssertionError(fault)
                        return json_response(rows)
                return response

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = Fixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                origin, inventory = next(
                    (key, value)
                    for key, value in fixture.selected.ledger._quarantine_authenticated.inventories.items()
                    if value["assessments"]
                )
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + inventory["assessments"][0]["instance"]
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                runs, errors = [], []
                original_finish = reviews.finish
                from review_sensei_github import publication_recovery

                original_resolver = publication_recovery._publication_transport
                early_injected = []
                from review_sensei_github import (
                    finding_lifecycle,
                    publication,
                    quarantine_capacity,
                )

                original_owner = quarantine_capacity.review_walk_owner
                original_journal_class = finding_lifecycle.FindingLifecycleJournal
                restored_fields = []

                class FakeLedger:
                    @property
                    def caller(self):
                        fixture.fake_calls += 1
                        return runs[0].caller

                class FakeJournal:
                    @property
                    def ledger(self):
                        fixture.fake_calls += 1
                        return FakeLedger()

                def restore_inner():
                    finding_lifecycle.FindingLifecycleJournal = original_journal_class
                    for owner, field, value in restored_fields:
                        setattr(owner, field, value)

                def observe(frame, event, result):
                    if (
                        fault in ("inner_class", "inner_ledger")
                        and runs
                        and frame.f_code is scan.capacity_review_walk.__code__
                    ):
                        if event == "call" and not early_injected:
                            http = frame.f_locals["http"]
                            base = (
                                http.transport
                                if type(http) is publication._BudgetedReviewPagination
                                else http
                            )
                            adapter = original_resolver(base)
                            journal = vars(adapter)["journal"]
                            if fault == "inner_class":
                                restored_fields.append((adapter, "journal", journal))
                                finding_lifecycle.FindingLifecycleJournal = FakeJournal
                                adapter.journal = FakeJournal()
                            else:
                                restored_fields.append(
                                    (journal, "ledger", journal.ledger)
                                )
                                journal.ledger = FakeLedger()
                            early_injected.append(True)
                        elif event == "return":
                            restore_inner()
                    if (
                        fault in ("early_export", "capacity_export")
                        and runs
                        and frame.f_code is scan.capacity_review_walk.__code__
                    ):
                        if event == "call" and not early_injected:

                            def fake(*args):
                                fixture.fake_calls += 1

                            if fault == "capacity_export":
                                quarantine_capacity.review_walk_owner = fake
                            else:
                                publication_recovery._publication_transport = fake
                            early_injected.append(True)
                        elif event == "return":
                            quarantine_capacity.review_walk_owner = original_owner
                            publication_recovery._publication_transport = (
                                original_resolver
                            )
                    if (
                        event == "return"
                        and frame.f_code is historical.activate.__code__
                        and result is not None
                    ):
                        fixture.closed_started = True
                        runs.append(frame.f_locals["run"])

                def trace(frame, event, arg):
                    if frame.f_code in (
                        original_finish.__code__,
                        reviews.resolve_journal.__code__,
                    ):
                        if event == "exception":
                            errors.append(str(arg[1]))
                        return trace
                    return None

                previous, previous_trace = sys.getprofile(), sys.gettrace()
                try:
                    sys.setprofile(observe)
                    sys.settrace(trace)
                    code = fixture.invoke_command()
                finally:
                    sys.settrace(previous_trace)
                    sys.setprofile(previous)
                    restore_inner()
                    quarantine_capacity.review_walk_owner = original_owner
                    reviews.finish = original_finish
                    publication_recovery._publication_transport = original_resolver
                self.assertEqual(len(runs), 1)
                caller = runs[0].caller
                self.assertEqual(code, 1, fixture.error)
                self.assertEqual(
                    fixture.native_attempts,
                    0
                    if fault
                    in (
                        "early_export",
                        "capacity_export",
                        "inner_class",
                        "inner_ledger",
                    )
                    else 1,
                )
                if fault in (
                    "early_export",
                    "capacity_export",
                    "inner_class",
                    "inner_ledger",
                ):
                    self.assertEqual(early_injected, [True])
                self.assertEqual(fixture.fake_calls, 0)
                self.assertEqual(fixture.native_puts, 0)
                self.assertEqual(caller.accounting.action["phase"], "pending")
                self.assertIsNone(caller.accounting.action["result"])
                self.assertEqual(caller.accounting.uncertain, int(fault == "loss"))
                self.assertEqual(caller.accounting.remaining, 190)
                self.assertEqual(
                    next(
                        block.undispatched
                        for block in caller.accounting.blocks
                        if block.block_id == caller._terminal_block
                    ),
                    50,
                )
                self.assertFalse(
                    any(row["state"] == "APPROVED" for row in fixture.reviews)
                )
                self.assertFalse(
                    any(
                        "reviewsensei:command-receipt:v1" in row.get("body", "")
                        for row in fixture.extra_comments.values()
                    )
                )
                if fault == "inner_ledger":
                    self.assertIn("historical journal reader raw owner changed", errors)
                elif fault == "inner_class":
                    self.assertTrue(any("source" in error for error in errors), errors)
                if fault in ("missing", "foreign", "malformed"):
                    self.assertIn("historical review membership changed", errors)
                elif fault == "wire":
                    self.assertIn(
                        "historical review charged complete page changed", errors
                    )

    def test_foreign_review_is_not_adopted(self):
        self.exercise_fault("foreign")

    def test_missing_review_is_not_complete(self):
        self.exercise_fault("missing")

    def test_complete_wire_overflow_refuses_before_put(self):
        self.exercise_fault("wire")

    def test_lost_response_retains_unknown_without_retry(self):
        self.exercise_fault("loss")

    def test_response_cannot_replace_native_completion_export(self):
        self.exercise_fault("export")

    def test_preselection_resolver_cannot_be_replaced(self):
        self.exercise_fault("early_export")

    def test_malformed_identity_is_a_membership_hold(self):
        self.exercise_fault("malformed")

    def test_simultaneous_walk_refuses_without_a_second_debit(self):
        from unittest.mock import patch
        from urllib.parse import parse_qs, urlsplit

        from review_sensei_github import quarantine_historical_execution as historical

        from review_sensei.bounded_evidence import canonical_bytes

        runs, refused, pagers = [], [], []
        outer_test = self

        class Fixture(comments.CensusFixture):
            def external(self, request, timeout):
                response = super().external(request, timeout)
                url = urlsplit(request.full_url)
                if (
                    runs
                    and not refused
                    and request.get_method() == "GET"
                    and url.path.endswith("/pulls/7/reviews")
                    and parse_qs(url.query).get("per_page") == ["6"]
                ):
                    run = runs[0]
                    before = canonical_bytes(run.caller.accounting.to_document())
                    try:
                        pagers[-1].paginate(path=url.path, token="synthetic-unused")
                    except ReviewInputError as error:
                        refused.append(str(error))
                    else:
                        refused.append("unexpected successful nested read")
                    outer_test.assertEqual(
                        before, canonical_bytes(run.caller.accounting.to_document())
                    )
                return response

        def observe(frame, event, result):
            if (
                event == "return"
                and frame.f_code is scan.capacity_review_walk.__code__
                and type(result) is reviews.ReviewWalk
            ):
                pagers.append(frame.f_locals["http"])
            if (
                event == "return"
                and frame.f_code is historical.activate.__code__
                and result is not None
            ):
                runs.append(frame.f_locals["run"])

        with patch.object(comments, "CensusFixture", Fixture):
            comments.HistoricalInvocationCensusTests().exercise(extra_observer=observe)
        self.assertEqual(
            refused, ["historical review reader has simultaneous or foreign scope"]
        )

    def test_preselection_inner_class_substitution_executes_no_fake_properties(self):
        self.exercise_fault("inner_class")

    def test_preselection_ledger_substitution_executes_no_fake_caller(self):
        self.exercise_fault("inner_ledger")

    def test_preselection_capacity_owner_export_is_authenticated(self):
        self.exercise_fault("capacity_export")
