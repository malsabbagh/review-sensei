"""An invocation read plan never releases future allocation liabilities."""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from urllib.parse import urlsplit
from urllib.request import Request

from review_sensei_github import materialized_publication_phase as phase
from review_sensei_github.packed_comment_census import METADATA_QUERY, wire
from review_sensei_github.publication import approval_eligibility_from_body
from review_sensei_github.quarantine_census import (
    _LifecycleCapacity,
    lifecycle_capacity_plan,
)

from review_sensei.errors import ReviewInputError
from tests.test_quarantine_activation import registered_resource
from tests.test_quarantine_journey import JourneyFixture


class FixedMembershipFixture(JourneyFixture):
    def __init__(self, directory, fault=None):
        super().__init__(directory)
        self.fault = fault
        self.injected = False
        self.at_injection = None
        self.native_effects = []
        self.growth = None

    def external(self, request, timeout):
        scope = getattr(
            getattr(getattr(self, "selected", None), "ledger", None),
            "_quarantine_lifecycle_capacity",
            None,
        )
        if (
            self.fault in {"new-member", "member-after-effect", "body-growth"}
            and (self.fault != "member-after-effect" or self.native_effects)
            and not self.injected
            and scope is not None
            and scope.read_plan is not None
            and request.get_method() == "POST"
            and urlsplit(request.full_url).path == "/graphql"
            and json.loads(request.data).get("query") == METADATA_QUERY
        ):
            self.at_injection = (
                copy.deepcopy(self.reviews),
                copy.deepcopy(self.check_runs),
                scope.plan.unknown,
                tuple(self.native_effects),
            )
            self.injected = True
            if self.fault == "body-growth":
                identifier = next(
                    value
                    for value in scope.read_plan.known
                    if value in self.extra_comments
                )
                self.extra_comments[identifier]["body"] += (
                    "A" * scope.read_plan.known[identifier]
                )
                self.growth = (
                    identifier,
                    scope.read_plan.known[identifier],
                    wire({"body": self.extra_comments[identifier]["body"]}),
                )
            else:
                self.extra_comments[9_999_999] = {
                    "id": 9_999_999,
                    "body": "A late historical allocation remains a real liability.",
                    "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                }
        response = super().external(request, timeout)
        ledger = getattr(getattr(self, "selected", None), "ledger", None)
        if ledger is not None:
            observed = phase.phase_observation(ledger)
            if (
                observed is not None
                and observed["closed"]
                and request.get_method() in {"POST", "PATCH"}
                and any(
                    marker in urlsplit(request.full_url).path
                    for marker in ("/check-runs", "/reviews")
                )
            ):
                self.native_effects.append(
                    (request.get_method(), urlsplit(request.full_url).path)
                )
        return response


class MaterializedFixedMembershipTests(unittest.TestCase):
    def test_actual_inline_source_keeps_lifetime_promises_and_expires_reader(self):
        observed = []
        diagnostics = []
        code = lifecycle_capacity_plan.__code__

        def observe(frame, event, value):
            if (
                frame.f_code is code
                and event == "return"
                and type(value) is _LifecycleCapacity
            ):
                snapshot = phase.phase_observation(value.ledger)
                if snapshot is not None:
                    diagnostics.append(snapshot)
                if value.read_plan is not None:
                    # Diagnostic dictionaries are detached from the issuer's
                    # source-owned bytes and cannot authorize another branch.
                    self.assertIsNotNone(snapshot["projection"])
                    original = copy.deepcopy(snapshot["projection"])
                    snapshot["projection"]["branches"].clear()
                    self.assertEqual(
                        phase.phase_observation(value.ledger)["projection"], original
                    )
                    before = len(value.caller.observations)
                    with self.assertRaisesRegex(
                        ReviewInputError, "forbids GraphQL mutations"
                    ):
                        value.caller.open(
                            Request(
                                "https://api.github.test/graphql",
                                data=b'{"query":"mutation { addComment(input:{subjectId:"x",body:"x"}) { clientMutationId } }"}',
                                method="POST",
                            ),
                            timeout=10,
                        )
                    self.assertEqual(len(value.caller.observations), before)
                    # Invalid namespace metadata must refuse before charging or
                    # sending, including foreign owner/block and sequence growth.
                    namespace = original["request_namespace"]
                    caller = value.caller
                    for name, changed in (
                        ("_sequence", namespace["last_sequence"] + 1),
                        ("_block", "foreign-unfunded-block"),
                        (
                            "_state",
                            replace(
                                caller.accounting,
                                owner=replace(
                                    caller.accounting.owner,
                                    generation=caller.accounting.owner.generation + 1,
                                ),
                            ),
                        ),
                    ):
                        saved = getattr(caller, name)
                        try:
                            setattr(caller, name, changed)
                            with self.assertRaisesRegex(
                                ReviewInputError, "request namespace"
                            ):
                                caller.open(
                                    Request(
                                        "https://api.github.test/repos/owner/repo/pulls/7"
                                    ),
                                    timeout=10,
                                )
                        finally:
                            setattr(caller, name, saved)
                        self.assertEqual(len(caller.observations), before)
                    for branch in original["branches"]:
                        self.assertEqual(
                            len(branch["loss_and_stop_sequences"]), branch["effects"]
                        )
                        for index, sequence in enumerate(
                            branch["loss_and_stop_sequences"]
                        ):
                            self.assertEqual(len(sequence), 2 * (index + 1) + 1)
                            self.assertTrue(
                                all(
                                    stage["inline"] and stage["accounting_root_fits"]
                                    for stage in sequence
                                )
                            )
                    self.assertGreater(len(value.plan.unknown), 0)
                    self.assertEqual(value.read_plan.unknown, ())
                    self.assertEqual(
                        dict(value.read_plan.known), dict(value.plan.known)
                    )
                    self.assertLessEqual(value.read_plan.worst_read_calls(), 10)
                    with self.assertRaises(ReviewInputError):
                        replace(value).require(value.ledger, value.identity)
                    observed.append(value)
            return observe if frame.f_code is code else None

        with tempfile.TemporaryDirectory() as directory:
            fixture = FixedMembershipFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            prior = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
            finally:
                sys.settrace(prior)
            self.assertTrue(
                observed, diagnostics[-1:] if diagnostics else "no source census"
            )
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            for scope in observed:
                with self.assertRaisesRegex(ReviewInputError, "expired or foreign"):
                    phase.require_invocation_read_plan(scope)

    def test_new_native_membership_refuses_without_releasing_reserved_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FixedMembershipFixture(directory, "new-member")
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 1)
            self.assertTrue(fixture.injected)
            self.assertIn("exceeds reserved old/current allocations", fixture.error)
            reviews, checks, promised, prefix = fixture.at_injection
            self.assertTrue(promised)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertIsNone(
                fixture.current_record().api_run_accounting["terminal_transaction"]
            )

    def test_late_membership_after_native_effect_keeps_nonterminal_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FixedMembershipFixture(directory, "member-after-effect")
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 1)
            self.assertTrue(fixture.injected)
            self.assertIn("exceeds reserved old/current allocations", fixture.error)
            reviews, checks, promised, prefix = fixture.at_injection
            self.assertTrue(prefix)
            self.assertEqual(tuple(fixture.native_effects), prefix)
            self.assertTrue(promised)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertIsNone(
                fixture.current_record().api_run_accounting["terminal_transaction"]
            )
            self.assertFalse(
                any(review.get("state") == "APPROVED" for review in fixture.reviews)
            )

    def test_same_identity_body_growth_retains_allocation_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FixedMembershipFixture(directory, "body-growth")
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 1)
            self.assertTrue(fixture.injected)
            self.assertIn(
                "packed comment complete body page is malformed", fixture.error
            )
            identifier, admitted, grown = fixture.growth
            self.assertIn(identifier, fixture.extra_comments)
            self.assertGreater(grown, admitted)
            reviews, checks, promised, prefix = fixture.at_injection
            self.assertTrue(promised)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            self.assertEqual(fixture.native_effects, [])
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertIsNone(
                fixture.current_record().api_run_accounting["terminal_transaction"]
            )

    def test_original_eighty_candidate_hot_publication_fits_without_new_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FixedMembershipFixture(directory)
            old, registration = fixture.original_origins(
                failed_count=80, published_count=23
            )
            calls = fixture.provider_calls
            old_body = fixture.comment["body"]
            reviews_before = len(fixture.reviews)
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            current = fixture.current_record()
            self.assertEqual(fixture.provider_calls, calls + 1)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            self.assertTrue(current.finding_lifecycle)
            self.assertEqual(current.quarantine_lineage["origin_count"], 2)
            self.assertEqual(
                current.completed_initial_reviews, old.completed_initial_reviews
            )
            self.assertEqual(fixture.comment["body"], old_body)
            self.assertEqual(len(fixture.reviews), reviews_before + 1)
            eligibility = approval_eligibility_from_body(fixture.reviews[-1]["body"])
            self.assertEqual(len(eligibility.quarantine_assessments["pending"]), 52)
            self.assertEqual(eligibility.facts.review_status, "complete")
            self.assertEqual(fixture.check_runs[-1]["conclusion"], "action_required")
            self.assertFalse(
                any(review.get("state") == "APPROVED" for review in fixture.reviews)
            )
            self.assertEqual(current.transaction.phase, "publication_succeeded")
            self.assertEqual(
                current.api_run_accounting["terminal_transaction"],
                current.transaction.transaction_id,
            )
            self.assertLessEqual(
                max(
                    wire({"body": row["body"]})
                    for row in fixture.extra_comments.values()
                ),
                524288,
            )
