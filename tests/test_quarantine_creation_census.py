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

    def _exercise_native_creator(self, fault=None):
        import dis
        import sys

        from review_sensei_github import quarantine_creation as creation
        from test_bounded_snapshot_lifecycle import BoundedLifecycleFixture
        from test_quarantine_evidence import git

        class Created(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            fixture = BoundedLifecycleFixture(directory)
            old, registration = fixture.original_origins()
            old_accounting = copy.deepcopy(old.api_run_accounting)
            provider_calls = fixture.provider_calls
            fixture.config_bytes = fixture.config_bytes.replace(
                b"api_request_limit: 512", b"api_request_limit: 1152"
            )
            (fixture.root / ".reviewsensei.yml").write_bytes(fixture.config_bytes)
            fixture.limit = 1152
            git(fixture.root, "add", ".reviewsensei.yml")
            git(
                fixture.root,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Offline bounded profile",
            )
            fixture.policy = fixture.policy_ref = git(fixture.root, "rev-parse", "HEAD")
            fixture.creation_metadata = fixture.creator()
            fixture.creation_fault = fault
            execute = creation._execute_creation.__code__
            returns = {
                item.offset
                for item in dis.get_instructions(execute)
                if item.opname in {"RETURN_VALUE", "RETURN_CONST"}
            }
            observed = []

            def observe(frame, event, arg):
                if frame.f_code is not execute:
                    return
                prepared = frame.f_locals["prepared"]
                plan = frame.f_locals["plan"]
                if event == "call":
                    active = census._ACTIVE[prepared.ledger]
                    issued = active[1]
                    allocation = issued[6]
                    self.assertEqual(
                        (plan["parts"], plan["census_calls"], plan["requests"]),
                        (4, 9, 40),
                    )
                    self.assertEqual(
                        len(allocation.known) + len(allocation.unknown), 65
                    )
                    self.assertEqual(len(allocation.unknown), 49)
                    self.assertEqual(
                        prepared.ledger.evidence_budget.ordinary_remaining(), 40
                    )
                    self.assertIs(allocation, issued[9].comment_plan)
                    start = len(fixture.trace)
                    guard = issued[9]
                    with patch.object(
                        issued[8][4], "_quarantine_capacity_guard", copy.copy(guard)
                    ):
                        with self.assertRaises(ReviewInputError):
                            census._require_scope(prepared, issued)
                    from dataclasses import replace

                    with patch.object(
                        guard,
                        "comment_plan",
                        replace(allocation, unknown=allocation.unknown[:-1]),
                    ):
                        with self.assertRaises(ReviewInputError):
                            census._require_scope(prepared, issued)
                    from review_sensei_github import packed_comment_census

                    with patch.object(
                        packed_comment_census.CommentAllocationPlan,
                        "worst_read_calls",
                        return_value=1,
                    ):
                        with self.assertRaises(ReviewInputError):
                            census._require_scope(prepared, issued)
                    self.assertEqual(len(fixture.trace), start)
                    observed.append((prepared, issued, start))
                elif event == "return" and frame.f_lasti in returns:
                    prepared, issued, start = observed[0]
                    self.assertIsNotNone(prepared.ledger._startup_caller._state)
                    self.assertEqual(census._ACTIVE[prepared.ledger][2], 0)
                    self.assertLessEqual(len(fixture.trace) - start, 40)
                    self.assertEqual(issued[6].unknown, issued[9].comment_plan.unknown)
                    self.assertEqual(old.api_run_accounting, old_accounting)
                    raise Created()

            previous = sys.getprofile()
            try:
                with registered_resource([registration]):
                    sys.setprofile(observe)
                    if fault == "absent":
                        self.assertEqual(fixture.invoke(), 1)
                    else:
                        with self.assertRaises(Created, msg=fixture.error):
                            fixture.invoke()
            finally:
                sys.setprofile(previous)
            self.assertEqual(len(observed), 1, fixture.error)
            self.assertEqual(fixture.provider_calls, provider_calls)
            self.assertEqual(fixture.root_posts, 1)
            prepared, issued, start = observed[0]
            self.assertLessEqual(len(fixture.trace) - start, 40)
            self.assertEqual(old.api_run_accounting, old_accounting)
            if fault == "absent":
                self.assertIsNone(prepared.ledger._startup_caller._state)
            else:
                self.assertEqual(
                    prepared.ledger._startup_caller.accounting.configuration.limit, 1152
                )
            if fault in {"stored", "absent"}:
                self.assertGreaterEqual(
                    sum(
                        row.get("outcome") == "unknown"
                        for row in fixture.accounted.observations
                    ),
                    1,
                )
            self.assertNotIn(prepared.ledger, census._ACTIVE)

    def test_native_creator_uses_the_complete_installed_nine_read_envelope(self):
        self._exercise_native_creator()

    def test_native_creator_stored_and_absent_root_loss_keep_the_full_tail(self):
        for fault in ("stored", "absent"):
            with self.subTest(fault=fault):
                self._exercise_native_creator(fault)
