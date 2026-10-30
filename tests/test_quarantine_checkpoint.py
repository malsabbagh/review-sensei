"""Public checkpoint fences and uncertainty retain the original operation cap."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_quarantine_activation import registered_resource
from test_quarantine_journey import JourneyFixture

from review_sensei.errors import ReviewInputError


class CheckpointFaultFixture(JourneyFixture):
    def __init__(self, directory, fault, fence=1):
        super().__init__(directory)
        self.checkpoint_fault, self.fault_fence = fault, fence
        self.selected_snapshot = None
        self.fences = self.target_writes = self.failed_confirmations = 0
        self.rejected_copy = False

    def external(self, request, timeout):
        snapshot = (
            getattr(self.accounted, "_quarantine_checkpoint_snapshot", None)
            if hasattr(self, "accounted")
            else None
        )
        if not self.command_active or snapshot is None:
            return super().external(request, timeout)
        target = f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
        if urlsplit(request.full_url).path != target:
            return super().external(request, timeout)
        if self.selected_snapshot is None:
            self.selected_snapshot = snapshot
            try:
                replace(snapshot).require()
            except ReviewInputError:
                self.rejected_copy = True
        if snapshot is not self.selected_snapshot:
            return super().external(request, timeout)
        method = request.get_method()
        if method == "PATCH":
            self.target_writes += 1
            if self.checkpoint_fault in {"stored", "absent"}:
                if self.checkpoint_fault == "stored":
                    super().external(request, timeout)
                else:
                    self._meter(request)
                raise TimeoutError("synthetic lost checkpoint acknowledgment")
        if (
            method == "GET"
            and snapshot.operation.write_attempted
            and self.checkpoint_fault == "confirmation"
        ):
            self.failed_confirmations += 1
            self._meter(request)
            raise TimeoutError("synthetic lost exact checkpoint readback")
        response = super().external(request, timeout)
        if method == "GET" and not snapshot.operation.write_attempted:
            self.fences += 1
            if self.fences == self.fault_fence and self.checkpoint_fault in {
                "changed",
                "owner",
                "foreign",
            }:
                payload = json.loads(response.read(524288))
                if self.checkpoint_fault == "owner":
                    payload["user"]["id"] = 998
                elif self.checkpoint_fault == "foreign":
                    payload["id"] += 1
                else:
                    record = parse_session_comment(
                        payload["body"], identity=snapshot.identity
                    )
                    payload["body"] = render_session_comment(
                        repository_id=123,
                        pull_request=7,
                        record=record.evolve(generation=record.generation + 1),
                    )
                return json_response(payload)
        return response


class QuarantineCheckpointTests(unittest.TestCase):
    def start(self, directory, fault, fence=1):
        fixture = CheckpointFaultFixture(directory, fault, fence)
        old, registration = fixture.original_origins()
        with registered_resource([registration]):
            self.assertEqual(fixture.invoke(), 0, fixture.error)
        fixture.quarantine_predecessor_comment_id = None
        fixture.select_command("@reviewsensei verify", new_run=True)
        return fixture, old, registration

    def test_both_exact_fences_reject_changed_foreign_or_wrong_owner_roots_before_patch(
        self,
    ):
        for fault in ("changed", "owner", "foreign"):
            for fence in (1, 2):
                with (
                    self.subTest(fault=fault, fence=fence),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture, old, registration = self.start(directory, fault, fence)
                    with registered_resource([registration]):
                        self.assertEqual(fixture.invoke_command(), 1, fixture.error)
                    self.assertEqual(fixture.target_writes, 0)
                    self.assertTrue(fixture.rejected_copy)
                    self.assertIsNone(fixture.accounted._quarantine_checkpoint_snapshot)
                    with self.assertRaises(ReviewInputError):
                        fixture.selected_snapshot.require()
                    self.assertIsNone(old.api_run_accounting["terminal_transaction"])

    def test_stored_or_unstored_lost_ack_uses_one_patch_and_only_original_exact_readback(
        self,
    ):
        for fault in ("stored", "absent", "confirmation"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture, old, registration = self.start(directory, fault)
                with registered_resource([registration]):
                    self.assertEqual(
                        fixture.invoke_command(),
                        0 if fault == "stored" else 1,
                        fixture.error,
                    )
                    before = len(fixture.trace)
                    if fault != "stored":
                        self.assertEqual(fixture.invoke_command(), 1)
                        self.assertEqual(len(fixture.trace), before)
                    if fault == "confirmation":
                        self.assertEqual(fixture.failed_confirmations, 2)
                        with self.assertRaises(ReviewInputError):
                            fixture.accounted.reconcile_checkpoint(
                                fixture.selected.ledger, fixture.selected.identity
                            )
                        self.assertEqual(len(fixture.trace), before)
                self.assertEqual(fixture.target_writes, 1)
                self.assertTrue(fixture.rejected_copy)
                with self.assertRaises(ReviewInputError):
                    fixture.selected_snapshot.require()
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])

    def test_uncertain_individual_event_never_binds_or_clears_a_hold(self):
        from review_sensei_github.publication import approval_eligibility_from_body

        for stored in (False, True):
            with (
                self.subTest(stored=stored),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = JourneyFixture(directory)
                old, registration = fixture.original_origins()
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                    pending = approval_eligibility_from_body(
                        fixture.reviews[-1]["body"]
                    ).quarantine_assessments["pending"][0]
                    original = fixture.external
                    attempts = []

                    def opened(request, timeout):
                        operation = fixture.accounted._active_operation
                        if (
                            request.get_method() == "POST"
                            and operation is not None
                            and operation.key.startswith("quarantine-event:")
                        ):
                            attempts.append(request.full_url)
                            if stored:
                                original(request, timeout)
                            else:
                                fixture._meter(request)
                            raise TimeoutError("synthetic uncertain individual event")
                        return original(request, timeout)

                    fixture.external = opened
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command(
                        "@reviewsensei dismiss "
                        + pending["instance"]
                        + ' --reason "quarantine:'
                        + pending["origin_scope_sha256"]
                        + ' Exact source evidence disproves this historical assessment independently."',
                        new_run=True,
                    )
                    before = copy.deepcopy(
                        fixture.current_record().quarantine_lineage[
                            "assessment_state_manifest"
                        ]
                    )
                    self.assertEqual(fixture.invoke_command(), 1)
                    self.assertEqual(len(attempts), 1)
                    self.assertEqual(
                        fixture.current_record().quarantine_lineage[
                            "assessment_state_manifest"
                        ],
                        before,
                    )
                    self.assertFalse(
                        any(
                            review.get("state") == "APPROVED"
                            for review in fixture.reviews
                        )
                    )
                    self.assertIsNone(old.api_run_accounting["terminal_transaction"])


class QuarantineCarrierPlanningTests(unittest.TestCase):
    def test_human_wrong_bot_and_foreign_copies_cannot_underfund_public_staging(self):
        self._exercise_staging(funded=False)

    def test_funded_staging_creates_owned_carriers_instead_of_reusing_copies(self):
        self._exercise_staging(funded=True)

    def _exercise_staging(self, *, funded):
        import random
        import string
        from unittest.mock import patch

        from review_sensei_github.api_run_plan import (
            QUARANTINE_CHECKPOINT_REQUESTS,
            QUARANTINE_STAGING_REQUESTS,
        )
        from review_sensei_github.quarantine_creation import _body

        from review_sensei.bounded_evidence import partition_evidence

        for case in ("human", "wrong-bot", "association"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = JourneyFixture(directory)
                old, registration = fixture.original_origins()
                observed = []
                required_bodies = []

                def after_bootstrap(run):
                    ledger, identity = run.ledger, run.identity
                    record = fixture.current_record()
                    self.assertTrue(ledger.enable_disposition_snapshot_writes)
                    binding = {
                        **record.quarantine_lineage["origin_manifest"]["binding"],
                        "generation": record.generation + 1,
                        "purpose": "dispositions",
                    }
                    source = random.Random(387)
                    document = {
                        "data": "".join(
                            source.choices(
                                string.ascii_letters + string.digits, k=100_000
                            )
                        )
                    }
                    parts = partition_evidence(document, binding=binding, item_count=1)
                    self.assertGreaterEqual(len(parts), 3)
                    required_bodies.extend(_body(part) for part in parts)
                    for number, part in enumerate(parts):
                        identifier = 9_000_000 + number
                        fixture.extra_comments[identifier] = {
                            "id": identifier,
                            "body": _body(part),
                            "user": {
                                "id": 999 if case == "association" else 998,
                                "type": "User" if case == "human" else "Bot",
                                "login": "human"
                                if case == "human"
                                else "reviewsensei[bot]",
                            },
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/8"
                            if case == "association"
                            else "https://api.github.test/repos/owner/repo/issues/7",
                        }
                    start = len(fixture.trace)

                    def mutate(current):
                        remaining = (
                            ledger._quarantine_checkpoint_snapshot.operation.remaining
                        )
                        # Exercise both finite windows. A constrained late
                        # checkpoint cannot fund all parts; the current ordinary
                        # window can fund newly owned carriers.
                        if funded:
                            self.assertGreaterEqual(remaining, 2 * len(parts) + 5)
                        else:
                            self.assertLess(remaining, 2 * len(parts) + 5)
                        ledger.stage_evidence(
                            identity,
                            binding=binding,
                            document=document,
                            item_count=1,
                            max_manifest_bytes=2048,
                        )
                        return current.evolve(generation=current.generation + 1)

                    try:
                        with patch(
                            "review_sensei_github.api_run_plan.QUARANTINE_STAGING_REQUESTS",
                            QUARANTINE_STAGING_REQUESTS
                            if funded
                            else QUARANTINE_CHECKPOINT_REQUESTS - 1,
                        ):
                            ledger.replace(identity, mutate)
                    finally:
                        observed.extend(fixture.trace[start:])

                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                    fixture.after_action_bootstrap = after_bootstrap
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command("@reviewsensei verify", new_run=True)
                    expected_success = funded and case != "association"
                    self.assertEqual(
                        fixture.invoke_command(),
                        0 if expected_success else 1,
                        fixture.error,
                    )
                self.assertTrue(observed)
                writes = sum(
                    method == "POST" and path.endswith("/issues/7/comments")
                    for _, method, path in observed
                )
                self.assertEqual(
                    writes, len(required_bodies) if expected_success else 0
                )
                if expected_success:
                    created = [
                        item
                        for identifier, item in fixture.extra_comments.items()
                        if identifier < 9_000_000
                        and item.get("body") in required_bodies
                    ]
                    self.assertEqual(len(created), len(required_bodies))
                    self.assertTrue(
                        all(
                            item["user"]["type"] == "Bot" and item["user"]["id"] == 999
                            for item in created
                        )
                    )
                if not funded and case != "association":
                    self.assertIn(
                        "staging and root confirmation are unfunded", fixture.error
                    )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])


class QuarantineRolloverTests(unittest.TestCase):
    def test_startup_rollover_fences_and_lost_ack_keep_one_write_and_no_old_completion(
        self,
    ):
        from unittest.mock import patch

        from review_sensei_github.quarantine_rollover import _StartupSnapshot

        for fault, fence in (
            ("changed", 1),
            ("changed", 2),
            ("owner", 1),
            ("foreign", 2),
            ("stored", 1),
            ("absent", 1),
            ("confirmation", 1),
            ("head", 1),
            ("base", 1),
        ):
            with (
                self.subTest(fault=fault, fence=fence),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = JourneyFixture(directory)
                old, registration = fixture.original_origins()
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command("@reviewsensei verify", new_run=True)
                state = {
                    "snapshot": None,
                    "gets": 0,
                    "writes": 0,
                    "confirmations": 0,
                    "startup_census": 0,
                    "scope_changed": False,
                }
                original_get = _StartupSnapshot.get
                original_external = fixture.external

                def get(snapshot, record, body):
                    state["snapshot"] = snapshot
                    return original_get(snapshot, record, body)

                def opened(request, timeout):
                    snapshot = state["snapshot"]
                    path = urlsplit(request.full_url).path
                    if (
                        fixture.accounted._state is None
                        and request.get_method() == "POST"
                        and path == "/graphql"
                    ):
                        state["startup_census"] += 1
                    if (
                        snapshot is not None
                        and snapshot.caller._state is None
                        and request.get_method() == "POST"
                        and path == "/repos/owner/repo/issues/7/comments"
                        and fault in {"head", "base"}
                    ):
                        response = original_external(request, timeout)
                        setattr(fixture, "live_" + fault, "e" * 40)
                        state["scope_changed"] = True
                        return response
                    if (
                        snapshot is None
                        or snapshot.caller._state is not None
                        or urlsplit(request.full_url).path
                        != f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
                    ):
                        return original_external(request, timeout)
                    method = request.get_method()
                    if method == "PATCH":
                        state["writes"] += 1
                        if fault in {"stored", "absent"}:
                            if fault == "stored":
                                original_external(request, timeout)
                            else:
                                fixture._meter(request)
                            raise TimeoutError("synthetic lost rollover acknowledgment")
                    if method == "GET" and state["writes"] and fault == "confirmation":
                        state["confirmations"] += 1
                        fixture._meter(request)
                        raise TimeoutError("synthetic lost rollover readback")
                    response = original_external(request, timeout)
                    if method == "GET" and not state["writes"]:
                        state["gets"] += 1
                        if state["gets"] == fence and fault in {
                            "changed",
                            "owner",
                            "foreign",
                        }:
                            payload = json.loads(response.read(524288))
                            if fault == "owner":
                                payload["user"]["id"] += 1
                            elif fault == "foreign":
                                payload["issue_url"] = (
                                    "https://api.github.test/repos/owner/repo/issues/8"
                                )
                            else:
                                record = parse_session_comment(
                                    payload["body"], identity=snapshot.identity
                                )
                                payload["body"] = render_session_comment(
                                    repository_id=123,
                                    pull_request=7,
                                    record=record.evolve(
                                        generation=record.generation + 1
                                    ),
                                )
                            return json_response(payload)
                    return response

                fixture.external = opened
                with (
                    registered_resource([registration]),
                    patch.object(_StartupSnapshot, "get", get),
                ):
                    self.assertEqual(
                        fixture.invoke_command(),
                        0 if fault == "stored" else 1,
                        fixture.error,
                    )
                self.assertEqual(
                    state["writes"],
                    1 if fault in {"stored", "absent", "confirmation"} else 0,
                )
                self.assertGreater(state["startup_census"], 0)
                if fault in {"head", "base"}:
                    self.assertTrue(state["scope_changed"])
                if fault == "confirmation":
                    self.assertEqual(state["confirmations"], 2)
                with self.assertRaises(ReviewInputError):
                    state["snapshot"].require()
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )


class QuarantineDecisionScopeFenceTests(unittest.TestCase):
    def test_head_or_base_changed_during_exact_decision_preparation_never_patches_the_decision(
        self,
    ):
        from review_sensei_github.publication import approval_eligibility_from_body

        for changed_scope in ("head", "base"):
            with (
                self.subTest(scope=changed_scope),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = JourneyFixture(directory)
                old, registration = fixture.original_origins()
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                pending = approval_eligibility_from_body(
                    fixture.reviews[-1]["body"]
                ).quarantine_assessments["pending"][0]
                original_manifest = copy.deepcopy(
                    fixture.current_record().quarantine_lineage[
                        "assessment_state_manifest"
                    ]
                )
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + pending["instance"]
                    + ' --reason "quarantine:'
                    + pending["origin_scope_sha256"]
                    + ' Exact original evidence disproves this individual historical concern."',
                    new_run=True,
                )
                original = fixture.external
                state = {"changed": False, "decision_patches": 0}

                def opened(request, timeout):
                    snapshot = getattr(
                        fixture.accounted, "_quarantine_checkpoint_snapshot", None
                    )
                    if (
                        snapshot is not None
                        and snapshot.candidate is not None
                        and snapshot.candidate.quarantine_lineage
                        != snapshot.record.quarantine_lineage
                        and urlsplit(request.full_url).path
                        == f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
                    ):
                        if request.get_method() == "PATCH":
                            state["decision_patches"] += 1
                        if request.get_method() == "GET" and not state["changed"]:
                            response = original(request, timeout)
                            setattr(fixture, "live_" + changed_scope, "e" * 40)
                            state["changed"] = True
                            return response
                    return original(request, timeout)

                fixture.external = opened
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke_command(), 1, fixture.error)
                    self.assertTrue(state["changed"])
                    self.assertEqual(state["decision_patches"], 0)
                    self.assertEqual(
                        fixture.current_record().quarantine_lineage[
                            "assessment_state_manifest"
                        ],
                        original_manifest,
                    )
                    self.assertEqual(
                        len(fixture.accounted.observations), len(fixture.trace)
                    )
                    self.assertGreater(len(fixture.trace), 0)
                    self.assertEqual(fixture.invoke_command(), 1)
                    self.assertEqual(state["decision_patches"], 0)
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )
