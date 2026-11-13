"""Installed capacity extension, not an alternate recovery authority."""

import copy
import tempfile
import unittest
from dataclasses import replace
from types import MappingProxyType
from unittest.mock import patch

from review_sensei_github import quarantine_capacity as capacity
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture

from review_sensei.errors import ReviewInputError


class GuardedLifecycleCapacityTests(unittest.TestCase):
    def test_real_install_preserves_original_promises_and_rejects_forged_or_stale_extension(
        self,
    ):
        observed = []
        real = capacity.lifecycle_capacity_extension
        real_activate = capacity.activate_lifecycle_extension
        activated = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )

            def inspect(ledger, identity, *, known, added_parts):
                proof = real(ledger, identity, known=known, added_parts=added_parts)
                if observed:
                    return proof
                guard = capacity.installed_capacity(ledger, identity)
                plan = guard.comment_plan
                state = capacity._guard_state(guard)
                before = len(fixture.trace)
                self.assertEqual(proof.plan.unknown[: len(plan.unknown)], plan.unknown)
                for key, amount in plan.known.items():
                    self.assertGreaterEqual(proof.plan.known[key], amount)
                self.assertEqual(proof.plan.metadata_unknown, plan.metadata_unknown)
                self.assertEqual(guard.comment_plan, plan)
                self.assertEqual(capacity._guard_state(guard), state)
                with self.assertRaisesRegex(ReviewInputError, "unissued"):
                    capacity.activate_lifecycle_extension(copy.copy(proof))
                transport = capacity._INSTALLED_GUARDS[guard][1]()
                with patch.object(
                    transport, "_quarantine_capacity_guard", copy.copy(guard)
                ):
                    with self.assertRaisesRegex(ReviewInputError, "source changed"):
                        capacity.installed_capacity(ledger, identity)
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    capacity.installed_capacity(copy.copy(ledger), identity)
                for helper in ("_before_request", "_bind_immutable_comment_capacity"):
                    with patch.object(
                        capacity.NativeCapacityGuard, helper, lambda *a, **k: None
                    ):
                        with self.assertRaisesRegex(ReviewInputError, "source changed"):
                            capacity.installed_capacity(ledger, identity)
                        with self.assertRaisesRegex(ReviewInputError, "source changed"):
                            guard.before_request("GET", "/unused", None)
                authenticated = ledger._quarantine_authenticated
                with self.assertRaisesRegex(ReviewInputError, "read custody changed"):
                    capacity.install_retained_capacity(
                        copy.copy(ledger),
                        identity,
                        authenticated,
                        token=ledger._quarantine_origin_reader.metadata_token,
                    )
                original_ledger = authenticated.original_ledger
                try:
                    object.__setattr__(
                        authenticated, "original_ledger", copy.copy(ledger)
                    )
                    with self.assertRaisesRegex(
                        ReviewInputError, "read custody changed"
                    ):
                        capacity.install_retained_capacity(
                            ledger,
                            identity,
                            authenticated,
                            token=ledger._quarantine_origin_reader.metadata_token,
                        )
                finally:
                    object.__setattr__(
                        authenticated, "original_ledger", original_ledger
                    )
                stale = real(ledger, identity, known=known, added_parts=added_parts)
                with patch.object(guard, "comment_bounds", [*guard.comment_bounds, 1]):
                    with self.assertRaisesRegex(
                        ReviewInputError, "capability is unissued"
                    ):
                        capacity.activate_lifecycle_extension(stale)
                missing = dict(known)
                missing.pop(next(iter(plan.known)))
                with self.assertRaisesRegex(ReviewInputError, "disappeared"):
                    real(ledger, identity, known=missing, added_parts=0)
                self.assertEqual(capacity._guard_state(guard), state)
                self.assertEqual(len(fixture.trace), before)
                observed.append((guard, plan))
                return proof

            def inspect_activation(proof, **kwargs):
                if kwargs.get("capability") is None or activated:
                    return real_activate(proof, **kwargs)
                before = len(fixture.trace)
                guard, plan = proof.guard, proof.plan
                state = capacity._guard_state(guard)
                altered = {
                    "known": MappingProxyType(
                        {
                            **plan.known,
                            next(iter(plan.known)): next(iter(plan.known.values())) + 1,
                        }
                    ),
                    "unknown": (*plan.unknown, 1),
                    "metadata_known": MappingProxyType(
                        {**plan.metadata_known, next(iter(plan.known)): 1}
                    ),
                    "metadata_unknown": plan.metadata_unknown + 1,
                }
                for field, changed in altered.items():
                    original = getattr(plan, field)
                    try:
                        object.__setattr__(plan, field, changed)
                        with self.assertRaisesRegex(
                            ReviewInputError, "extension is unissued"
                        ):
                            real_activate(proof, **kwargs)
                    finally:
                        object.__setattr__(plan, field, original)
                with patch.object(
                    guard,
                    "identity",
                    replace(guard.identity, repository="foreign/repo"),
                ):
                    with self.assertRaisesRegex(ReviewInputError, "source changed"):
                        guard.before_request(
                            "PATCH",
                            "/repos/owner/repo/issues/comments/1",
                            {"body": "x" * (guard.closure_wire + 1)},
                        )
                wrong = {**kwargs, "operation": copy.copy(kwargs["operation"])}
                with self.assertRaisesRegex(ReviewInputError, "operation changed"):
                    real_activate(proof, **wrong)
                ledger = proof.ledger
                with patch.object(type(ledger), "replace", lambda *a, **k: None):
                    with self.assertRaisesRegex(
                        ReviewInputError, "writer source changed"
                    ):
                        real_activate(proof, **kwargs)
                with patch.object(
                    type(ledger.caller), "_operation", lambda *a, **k: None
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "writer source changed"
                    ):
                        real_activate(proof, **kwargs)
                with self.assertRaisesRegex(ReviewInputError, "cannot be supplied"):
                    ledger.replace_finding_lifecycle_parts(
                        proof.identity,
                        lambda record: record,
                        record=ledger.caller._effect_record,
                        parts=0,
                        retained_bytes=0,
                        _guarded_authorize=lambda scope: None,
                    )
                forged_writer = capacity._guarded_lifecycle_writer(lambda *a, **k: None)
                with patch.object(
                    type(ledger), "replace_finding_lifecycle_parts", forged_writer
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "writer source changed"
                    ):
                        ledger.replace_finding_lifecycle_parts(
                            proof.identity,
                            lambda record: record,
                            record=ledger.caller._effect_record,
                            parts=0,
                            retained_bytes=0,
                        )
                self.assertEqual(capacity._guard_state(guard), state)
                self.assertEqual(len(fixture.trace), before)
                result = real_activate(proof, **kwargs)
                activated.append(proof)
                with self.assertRaisesRegex(ReviewInputError, "capability is unissued"):
                    real_activate(proof, **kwargs)
                return result

            with (
                registered_resource([registration]),
                patch.object(capacity, "lifecycle_capacity_extension", inspect),
                patch.object(
                    capacity, "activate_lifecycle_extension", inspect_activation
                ),
            ):
                result = fixture.invoke()
            self.assertTrue(observed, fixture.error)
            self.assertTrue(activated, fixture.error)
            # This source-only regression does not erase the independently
            # measured full publication tail hold under the original budget.
            self.assertEqual(result, 1, fixture.error)
            self.assertIn(
                "caller run cannot fund the complete operation", fixture.error
            )
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            for guard, original in observed:
                self.assertEqual(
                    guard.comment_plan.unknown[: len(original.unknown)],
                    original.unknown,
                )

    def test_unknown_inline_checkpoint_never_refunds_installed_native_pool(self):
        from review_sensei_github.publication_recovery import _publication_transport

        for stored in (False, True):
            with (
                self.subTest(stored=stored),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = PublishedTailJourneyFixture(directory)
                old, registration = fixture.original_origins(
                    failed_count=1, published_count=1
                )
                opened = fixture.external
                attempts = []

                def lose(request, timeout):
                    operation = fixture.accounted._active_operation
                    if (
                        not attempts
                        and request.get_method() == "PATCH"
                        and operation is not None
                        and "stage_finding_lifecycle" in operation.key
                    ):
                        guard = _publication_transport(
                            fixture.selected.ledger.http
                        )._quarantine_capacity_guard
                        attempts.append(
                            (
                                guard,
                                tuple(guard.comment_bounds),
                                guard.issue_update_bytes,
                                guard.comment_plan.unknown,
                            )
                        )
                        if stored:
                            opened(request, timeout)
                        else:
                            fixture._meter(request)
                        raise TimeoutError(
                            "synthetic lifecycle checkpoint acknowledgment loss"
                        )
                    return opened(request, timeout)

                fixture.external = lose
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 1, fixture.error)
                self.assertEqual(len(attempts), 1, fixture.error)
                guard, remaining, growth, unknown = attempts[0]
                self.assertEqual(tuple(guard.comment_bounds), remaining)
                self.assertLessEqual(guard.issue_update_bytes, growth)
                self.assertEqual(guard.comment_plan.unknown[: len(unknown)], unknown)
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
