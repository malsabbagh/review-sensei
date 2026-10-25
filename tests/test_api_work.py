"""First-block accounting value contract; no live store/authority qualification."""

import unittest
from dataclasses import replace

from review_sensei.api_work import (
    ACCOUNTING_VERSION,
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    CallerRequestBlock,
    FrozenApiConfiguration,
    PendingApiRequest,
    StartupPlan,
    StartupSlot,
    initial_accounting,
)
from review_sensei.configuration import ConfigurationError, parse_configuration_text
from review_sensei.errors import ReviewInputError


def fixture_plan():
    # Synthetic finite slots, NOT a claim about production startup cost. The
    # adapter must measure its physical auth/discovery/persistence envelope.
    return StartupPlan(
        tuple(
            StartupSlot(job, attempt, ("auth", "discovery", "write", "readback"))
            for job in ("analyze", "publish")
            for attempt in (1, 2)
        )
    )


def accounting(*, limit=128, prior=0, attempt=1):
    return initial_accounting(
        ApiRunIdentity(123, 456),
        FrozenApiConfiguration("a" * 40, "b" * 64, limit),
        ApiExecutionOwner(attempt, "analyze", 0),
        fixture_plan(),
        prior_nonstartup_liability_upper_bound=prior,
    )


class ApiWorkConfigurationTests(unittest.TestCase):
    def test_single_root_knob_default_declared_value_and_strict_types(self):
        self.assertEqual(parse_configuration_text("schema: 1\n").api_request_limit, 128)
        self.assertEqual(
            parse_configuration_text(
                "schema: 1\napi_request_limit: 200\n"
            ).api_request_limit,
            200,
        )
        for value in ("true", "0", "-1", "1.5", '"128"', "[128]"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                parse_configuration_text(f"schema: 1\napi_request_limit: {value}\n")

    def test_admission_minimum_is_the_measured_plan_not_any_positive_integer(self):
        plan = fixture_plan()
        configured = parse_configuration_text(
            f"schema: 1\napi_request_limit: {plan.liability - 1}\n"
        )
        with self.assertRaisesRegex(ReviewInputError, "measured startup"):
            plan.require_limit(configured.api_request_limit)
        plan.require_limit(plan.liability)


class CallerAccountingTests(unittest.TestCase):
    def test_identity_never_contains_attempt_job_or_owner_generation(self):
        state = accounting().reserve("analysis", 10)
        next_owner = ApiExecutionOwner(1, "publish", 1)
        handed = state.claim_startup(next_owner)
        self.assertEqual(handed.identity, state.identity)
        self.assertEqual(handed.configuration, state.configuration)
        self.assertEqual(handed.remaining, state.remaining)
        self.assertEqual(handed.blocks, state.blocks)

    def test_startup_precharges_initial_and_one_rerun_even_before_first_record(self):
        original = accounting()
        absent_record_after_crash = accounting(attempt=2)
        self.assertEqual(original.startup.liability, 16)
        self.assertEqual(original.remaining, 112)
        self.assertEqual(absent_record_after_crash.remaining, original.remaining)
        with self.assertRaisesRegex(ReviewInputError, "unknown"):
            accounting(prior=None)
        conservative = accounting(attempt=2, prior=7)
        self.assertEqual(conservative.remaining, original.remaining - 7)

    def test_same_slot_restart_and_third_attempt_refuse(self):
        state = accounting()
        with self.assertRaisesRegex(ReviewInputError, "consumed"):
            state.claim_startup(ApiExecutionOwner(1, "analyze", 1))
        resumed = state.claim_startup(ApiExecutionOwner(2, "analyze", 1))
        self.assertEqual(resumed.remaining, state.remaining)
        with self.assertRaises(ReviewInputError):
            ApiExecutionOwner(3, "analyze", 2)
        with self.assertRaises(ReviewInputError):
            resumed.claim_startup(ApiExecutionOwner(1, "publish", 2))

    def test_reservation_dispatch_confirmation_moves_categories_without_refund(self):
        state = accounting().reserve("ordinary", 5)
        room = state.remaining
        request = PendingApiRequest("github-1", 100_000)
        pending = state.dispatch("ordinary", request)
        self.assertEqual(
            (pending.reserved_undispatched, pending.uncertain, pending.confirmed),
            (4, 1, 0),
        )
        confirmed = pending.confirm(request)
        self.assertEqual(
            (confirmed.reserved_undispatched, confirmed.uncertain, confirmed.confirmed),
            (4, 0, 1),
        )
        self.assertEqual(state.remaining, room)
        self.assertEqual(pending.remaining, room)
        self.assertEqual(confirmed.remaining, room)
        with self.assertRaises(ReviewInputError):
            confirmed.confirm(request)

    def test_unknown_response_survives_owner_handoff_with_original_deadline(self):
        pending = (
            accounting()
            .reserve("write", 3)
            .dispatch("write", PendingApiRequest("reservation-write", 42))
        )
        resumed = pending.claim_startup(ApiExecutionOwner(2, "analyze", 1))
        self.assertEqual(resumed.blocks, pending.blocks)
        self.assertEqual(resumed.uncertain, 1)
        self.assertEqual(resumed.blocks[0].pending[0].deadline_unix_ms, 42)
        self.assertEqual(resumed.remaining, pending.remaining)
        # There is deliberately no expire/refund transition, even after 42.
        self.assertFalse(hasattr(resumed, "refund"))

    def test_exact_capacity_boundary_refuses_before_producing_dispatch_state(self):
        state = accounting()
        full = state.reserve("all", state.remaining)
        self.assertEqual(full.remaining, 0)
        with self.assertRaisesRegex(ReviewInputError, "exceeds"):
            full.reserve("extra", 1)
        pending = full.dispatch("all", PendingApiRequest("last", 100))
        self.assertEqual(pending.remaining, 0)

    def test_obsolete_version_and_invalid_ownership_or_block_shapes_refuse(self):
        state = accounting()
        self.assertEqual(state.version, ACCOUNTING_VERSION)
        for version in ("1.0", "operation-128-v1", "unknown"):
            with self.subTest(version=version), self.assertRaises(ReviewInputError):
                replace(state, version=version)
        with self.assertRaises(ReviewInputError):
            state.claim_startup(ApiExecutionOwner(1, "publish", 0))
        with self.assertRaises(ReviewInputError):
            replace(
                state,
                blocks=(
                    CallerRequestBlock("same", state.owner, 1),
                    CallerRequestBlock("same", state.owner, 2),
                ),
            )
        with self.assertRaises(ReviewInputError):
            StartupPlan((StartupSlot("job", 1, ("auth",)),))

    def test_unreserved_or_duplicate_pending_dispatch_refuses(self):
        state = accounting().reserve("one", 1)
        request = PendingApiRequest("r", 100)
        pending = state.dispatch("one", request)
        for block in ("missing", "one"):
            with self.subTest(block=block), self.assertRaises(ReviewInputError):
                pending.dispatch(block, request)

    def test_frozen_configuration_and_repository_run_bindings_do_not_upgrade(self):
        state = accounting()
        state.require_binding(state.identity, state.configuration)
        for identity, configuration in (
            (ApiRunIdentity(124, 456), state.configuration),
            (ApiRunIdentity(123, 457), state.configuration),
            (state.identity, replace(state.configuration, limit=256)),
            (state.identity, replace(state.configuration, commit="c" * 40)),
            (state.identity, replace(state.configuration, digest="d" * 64)),
        ):
            with (
                self.subTest(identity=identity, configuration=configuration),
                self.assertRaises(ReviewInputError),
            ):
                state.require_binding(identity, configuration)

    def test_settled_id_is_lifetime_unique_and_delayed_response_cannot_settle_new_work(
        self,
    ):
        first = PendingApiRequest("same-id", 100)
        state = accounting().reserve("work", 3).dispatch("work", first).confirm(first)
        for proposed in (first, PendingApiRequest("same-id", 200)):
            with (
                self.subTest(proposed=proposed),
                self.assertRaisesRegex(ReviewInputError, "used"),
            ):
                state.dispatch("work", proposed)
        new_request = PendingApiRequest("new-id", 200)
        state = state.dispatch("work", new_request)
        with self.assertRaises(ReviewInputError):
            state.confirm(first)
        with self.assertRaises(ReviewInputError):
            state.confirm(PendingApiRequest("new-id", 201))
        self.assertEqual(state.confirmed, 1)
        self.assertEqual(state.uncertain, 1)
        confirmed = state.confirm(new_request)
        self.assertEqual(confirmed.confirmed, 2)
        self.assertEqual(confirmed.remaining, state.remaining)

    def test_crashed_owner_remainder_stays_charged_but_is_unusable_by_successor(self):
        request = PendingApiRequest("unknown-write", 100)
        state = accounting().reserve("old", 4).dispatch("old", request)
        resumed = state.claim_startup(ApiExecutionOwner(2, "analyze", 1))
        with self.assertRaisesRegex(ReviewInputError, "issuing owner"):
            resumed.dispatch("old", PendingApiRequest("new", 200))
        self.assertEqual(resumed.remaining, state.remaining)
        self.assertEqual(resumed.reserved_undispatched, 3)
        self.assertEqual(resumed.uncertain, 1)
        fresh = resumed.reserve("new-owner", 1)
        self.assertEqual(fresh.remaining, resumed.remaining - 1)
        self.assertEqual(fresh.blocks[-1].issued_by, resumed.owner)
        fresh.dispatch("new-owner", PendingApiRequest("fresh", 200))

    def test_prior_nonstartup_liability_is_neither_confirmed_nor_startup_double_charge(
        self,
    ):
        state = accounting(prior=7)
        self.assertEqual(state.confirmed, 0)
        self.assertEqual(state.prior_nonstartup_liability, 7)
        self.assertEqual(state.remaining, 128 - fixture_plan().liability - 7)
        request = PendingApiRequest("new", 100)
        confirmed = state.reserve("new", 1).dispatch("new", request).confirm(request)
        resumed = confirmed.claim_startup(ApiExecutionOwner(2, "analyze", 1))
        self.assertEqual(resumed.prior_nonstartup_liability, 7)
        self.assertEqual(resumed.confirmed, 1)
        self.assertEqual(resumed.remaining, state.remaining - 1)

    def test_document_roundtrip_cannot_transfer_crashed_owner_capacity(self):
        state = accounting().reserve("old", 4)
        successor = state.claim_startup(ApiExecutionOwner(2, "analyze", 1))
        forged = replace(
            successor,
            blocks=(
                replace(state.blocks[0], undispatched=0),
                CallerRequestBlock("new", successor.owner, 4),
            ),
        )
        self.assertEqual(forged.remaining, state.remaining)
        with self.assertRaisesRegex(ReviewInputError, "transfer"):
            state.require_successor(
                ApiRunAccounting.from_document(forged.to_document())
            )

    def test_completed_and_unknown_request_bindings_survive_document_restore(self):
        settled = PendingApiRequest("settled", 100)
        unknown = PendingApiRequest("unknown", 200)
        state = (
            accounting(prior=3)
            .reserve("work", 4)
            .dispatch("work", settled)
            .confirm(settled)
            .dispatch("work", unknown)
        )
        restored = ApiRunAccounting.from_document(state.to_document())
        self.assertEqual(restored, state)
        self.assertEqual(restored.blocks[0].pending, (unknown,))
        self.assertEqual(restored.completed, (settled,))
        with self.assertRaisesRegex(ReviewInputError, "used"):
            restored.dispatch("work", PendingApiRequest("settled", 300))
