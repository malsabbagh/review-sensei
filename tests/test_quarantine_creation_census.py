"""Actual startup owns two bounded walks, not a reusable cheaper census flag."""

import copy
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from urllib.request import Request

from review_sensei_github import quarantine_creation_census as census
from test_quarantine_activation import event, registered_resource
from test_quarantine_journey import JourneyFixture

from review_sensei.errors import ReviewInputError


class QuarantineCreationCensusTests(unittest.TestCase):
    def exercise(self, *, one_short=False, fault=None):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            fixture.revision(fixture.live_head, binary=True)
            registration["head_sha"] = fixture.live_head
            fixture.event_document = event(registration)
            fixture.creation_fault = fault if fault in {"stored", "absent"} else None
            if fault in {"carrier-stored", "carrier-absent"}:
                fixture.carrier_fault = fault.removeprefix("carrier-")
            activate, before_request = (
                census.creation_census,
                census.before_creation_request,
            )
            observed, checked = [], []

            @contextmanager
            def exact_funding(prepared, plan):
                issued = census._PLANS[prepared]
                self.assertIsNotNone(issued[6])
                self.assertLess(plan["census_calls"], 10)
                self.assertGreater(
                    plan["lifetime_allocations"],
                    len(issued[6].known) + len(issued[6].unknown),
                )
                caller = prepared.ledger._startup_caller
                target = plan["requests"] - int(one_short)
                # Spend actual original startup reads; never replenish a hold.
                while caller.budget_view.ordinary_remaining() > target:
                    prepared.ledger.http.request(
                        "GET",
                        prepared.ledger.http.repository_path(
                            "owner/repo", "/git/ref/heads/main"
                        ),
                        token=prepared.scope.metadata_token,
                    )
                trace = len(fixture.trace)
                with self.assertRaises(ReviewInputError):
                    with activate(copy.copy(prepared), plan):
                        self.fail("copied preparation entered")
                self.assertEqual(len(fixture.trace), trace)
                changed = dict(plan, requests=plan["requests"] - 1)
                # A changed plan consumes only its sizing witness, not a request.
                with self.assertRaises(ReviewInputError):
                    with activate(prepared, changed):
                        self.fail("changed price entered")
                self.assertEqual(census.prepare_census(prepared), plan)
                with activate(prepared, plan):
                    observed.append((prepared, plan, trace))
                    yield
                    self.assertLessEqual(len(fixture.trace) - trace, plan["requests"])
                    if prepared.ledger._startup_caller._state is not None:
                        self.assertEqual(census._ACTIVE[prepared.ledger][2], 0)
                with self.assertRaises(ReviewInputError):
                    with activate(prepared, plan):
                        self.fail("consumed phase entered")

            def inspect(caller, request):
                ledger = census._CALLERS.get(caller)
                active = None if ledger is None else census._ACTIVE.get(ledger)
                if (
                    active is not None
                    and active[3]
                    and active[4] is not None
                    and not checked
                ):
                    checked.append(True)
                    trace = len(fixture.trace)
                    for method, suffix, data in (
                        ("POST", "/pulls/7/reviews", b'{"event":"APPROVE"}'),
                        ("POST", "/check-runs", b'{"name":"future"}'),
                        ("POST", "/issues/7/comments", b'{"body":"future reply"}'),
                        (
                            "PATCH",
                            "/issues/comments/1000000",
                            b'{"body":"future decision"}',
                        ),
                    ):
                        forged = Request(
                            ledger.http.api_url + "/repos/owner/repo" + suffix,
                            data=data,
                            method=method,
                        )
                        with self.assertRaisesRegex(ReviewInputError, "premature"):
                            caller.open(forged, timeout=30)
                    self.assertEqual(len(fixture.trace), trace)
                    with patch.object(caller, "_state", object()):
                        with self.assertRaisesRegex(ReviewInputError, "phase changed"):
                            before_request(caller, request)
                    source_scope = active[0].scope
                    head = source_scope.head_sha
                    object.__setattr__(source_scope, "head_sha", "f" * 40)
                    try:
                        with self.assertRaisesRegex(ReviewInputError, "phase changed"):
                            before_request(caller, request)
                    finally:
                        object.__setattr__(source_scope, "head_sha", head)
                    from review_sensei_github.publication_recovery import (
                        _publication_transport,
                    )

                    for target, attribute, replacement in (
                        (ledger, "http", copy.copy(ledger.http)),
                        (
                            caller,
                            "run",
                            type(caller.run)(
                                caller.run.repository_id, caller.run.run_id + 1
                            ),
                        ),
                        (
                            _publication_transport(ledger.http),
                            "_quarantine_capacity_guard",
                            object(),
                        ),
                    ):
                        with patch.object(target, attribute, replacement):
                            with self.assertRaisesRegex(
                                ReviewInputError, "phase changed"
                            ):
                                before_request(caller, request)
                    if fault == "growth":
                        for index in range(201):
                            fixture.extra_comments[9_000_000 + index] = {
                                "id": 9_000_000 + index,
                                "body": "unexpected concurrent allocation",
                                "user": {
                                    "id": 999,
                                    "login": "reviewsensei[bot]",
                                    "type": "Bot",
                                },
                                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                            }
                return before_request(caller, request)

            with (
                registered_resource([registration]),
                patch.object(census, "creation_census", exact_funding),
                patch.object(census, "before_creation_request", inspect),
            ):
                outcome = fixture.invoke()
            self.assertEqual(len(observed), 1, fixture.error)
            prepared, plan, trace = observed[0]
            if one_short:
                self.assertEqual(outcome, 1)
                self.assertFalse(checked)
                self.assertEqual(len(fixture.trace), trace)
                self.assertEqual(fixture.root_posts, 0)
            elif fault in {"absent", "carrier-stored", "carrier-absent", "growth"}:
                self.assertEqual(outcome, 1)
                self.assertEqual(fixture.root_posts, int(fault == "absent"))
                self.assertIsNone(prepared.ledger._startup_caller._state)
            else:
                self.assertTrue(checked)
                self.assertEqual(fixture.root_posts, 1)
                self.assertIsNotNone(
                    prepared.ledger._startup_caller._state, fixture.error
                )
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            self.assertNotIn(prepared.ledger, census._ACTIVE)
            self.assertNotIn(prepared.ledger._startup_caller, census._CALLERS)

    def test_exact_original_funding_and_premature_native_mutations(self):
        self.exercise()

    def test_one_short_refuses_before_any_creation_effect(self):
        self.exercise(one_short=True)

    def test_stored_and_absent_root_ack_preserve_one_shot_phase(self):
        for fault in ("stored", "absent"):
            with self.subTest(fault=fault):
                self.exercise(fault=fault)

    def test_interrupted_carriers_and_unbounded_concurrent_growth_stop_before_root(
        self,
    ):
        for fault in ("carrier-stored", "carrier-absent", "growth"):
            with self.subTest(fault=fault):
                self.exercise(fault=fault)

    def test_custom_executor_and_factory_injection_cannot_issue_requests(self):
        from review_sensei_github import quarantine_creation as creation

        @census.creation_executor
        def custom(prepared, **kwargs):
            self.fail("custom executor received source request factory")

        with patch.object(creation, "create_quarantine", custom):
            with self.assertRaisesRegex(ReviewInputError, "cannot be replaced"):
                census.install_creation_executor(
                    custom, creation._execute_creation, creation._stage
                )
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                custom(None, actions_token="synthetic")
        with self.assertRaisesRegex(ReviewInputError, "source changed"):
            creation.create_quarantine(
                None, actions_token="synthetic", _creation_dispatch=lambda: None
            )

    def test_original_52_obligation_creation_preserves_full_lifetime_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins(
                failed_count=80, published_count=23
            )
            original = census.creation_census
            observed = []

            @contextmanager
            def inspect(prepared, plan):
                before = len(fixture.trace)
                with original(prepared, plan):
                    self.assertLessEqual(
                        plan["requests"],
                        prepared.ledger.evidence_budget.ordinary_remaining(),
                    )
                    self.assertEqual(plan["future_parts"], 109)
                    self.assertLessEqual(plan["lifetime_allocations"], 200)
                    self.assertLessEqual(plan["lifetime_body_bytes"], 8 * 1024 * 1024)
                    yield
                    self.assertIsNotNone(prepared.ledger._startup_caller._state)
                    self.assertEqual(
                        len(prepared.ledger._quarantine_authenticated.pending), 52
                    )
                    self.assertEqual(census._ACTIVE[prepared.ledger][2], 0)
                    self.assertLessEqual(len(fixture.trace) - before, plan["requests"])
                    observed.append(plan)

            with (
                registered_resource([registration]),
                patch.object(census, "creation_census", inspect),
            ):
                fixture.invoke()
            # This qualifies creation, not the later publication/decision journey.
            self.assertEqual(len(observed), 1, fixture.error)
            self.assertEqual(fixture.root_posts, 1)
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])

    def test_lifetime_wire_admission_keeps_original_8mib_boundary(self):
        from review_sensei_github.packed_comment_census import CommentAllocationPlan

        from review_sensei.bounded_evidence import MAX_STORED_PART_BYTES

        unit = MAX_STORED_PART_BYTES // 128
        exact = CommentAllocationPlan({}, (unit,) * 128)
        self.assertEqual(
            census._require_lifetime_capacity(exact), MAX_STORED_PART_BYTES
        )
        over = CommentAllocationPlan({}, (unit,) * 127 + (unit + 1,))
        with self.assertRaisesRegex(ReviewInputError, "lifetime storage capacity"):
            census._require_lifetime_capacity(over)
