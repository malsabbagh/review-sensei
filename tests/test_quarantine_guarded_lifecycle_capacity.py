"""Installed capacity extension, not an alternate recovery authority."""

import copy
import dis
import sys
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

            def probe_extension(proof, ledger, identity, *, known, added_parts):
                guard = capacity.installed_capacity(ledger, identity)
                plan = guard.comment_plan
                state = capacity._guard_state(guard)
                before = len(fixture.trace)
                self.assertEqual(proof.plan.unknown[: len(plan.unknown)], plan.unknown)
                for key, amount in plan.known.items():
                    self.assertGreaterEqual(proof.plan.known[key], amount)
                self.assertEqual(proof.plan.metadata_unknown, plan.metadata_unknown)
                aliases = {
                    ledger.app_slug.casefold(),
                    ledger.app_slug.removesuffix("[bot]").casefold(),
                }
                for row in ledger._cached_comments():
                    identifier, author = row["id"], row.get("user")
                    if identifier in plan.metadata_known:
                        self.assertEqual(
                            proof.plan.metadata_known[identifier],
                            plan.metadata_known[identifier],
                        )
                    elif (
                        isinstance(author, dict)
                        and author.get("type") == "Bot"
                        and isinstance(author.get("login"), str)
                        and author["login"].isascii()
                        and author["login"].casefold() in aliases
                    ):
                        self.assertEqual(
                            proof.plan.metadata_known[identifier],
                            plan.metadata_unknown,
                        )
                    else:
                        self.assertNotIn(identifier, proof.plan.metadata_known)
                self.assertEqual(guard.comment_plan, plan)
                self.assertEqual(capacity._guard_state(guard), state)
                # Packing depends on authenticated insertion order, not just
                # the unordered set of retained maxima.
                self.assertGreaterEqual(len(plan.known), 2)
                original_known = plan.known
                try:
                    object.__setattr__(
                        plan,
                        "known",
                        MappingProxyType(dict(reversed(tuple(plan.known.items())))),
                    )
                    with self.assertRaisesRegex(ReviewInputError, "source changed"):
                        capacity.installed_capacity(ledger, identity)
                    with self.assertRaisesRegex(ReviewInputError, "source changed"):
                        guard.before_request("GET", "/unused", None)
                finally:
                    object.__setattr__(plan, "known", original_known)
                keys = tuple(plan.known)[:2]
                metadata_plan = replace(plan, metadata_known={keys[0]: 1, keys[1]: 2})
                detached = copy.copy(guard)
                detached.comment_plan = metadata_plan
                metadata_state = capacity._guard_state(detached)
                object.__setattr__(
                    metadata_plan,
                    "metadata_known",
                    MappingProxyType({keys[1]: 2, keys[0]: 1}),
                )
                self.assertNotEqual(capacity._guard_state(detached), metadata_state)
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

            def probe_activation(proof, **kwargs):
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

            extension_code, extension_globals = real.__code__, real.__globals__
            activation_code = real_activate.__code__
            activation_globals = real_activate.__globals__
            return_offsets = {
                code: {
                    instruction.offset
                    for instruction in dis.get_instructions(code)
                    if instruction.opname in {"RETURN_VALUE", "RETURN_CONST"}
                }
                for code in (extension_code, activation_code)
            }
            previous = sys.getprofile()
            probing = False
            activation_frame = None

            def observe(frame, event, value):
                nonlocal probing, activation_frame
                if previous is not None:
                    previous(frame, event, value)
                if probing:
                    return
                extension_return = (
                    event == "return"
                    and frame.f_code is extension_code
                    and frame.f_globals is extension_globals
                    and frame.f_lasti in return_offsets[extension_code]
                    and not observed
                )
                activation_call = (
                    event == "call"
                    and frame.f_code is activation_code
                    and frame.f_globals is activation_globals
                    and frame.f_locals["capability"] is not None
                    and not activated
                    and activation_frame is None
                )
                activation_return = (
                    event == "return"
                    and frame is activation_frame
                    and frame.f_code is activation_code
                    and frame.f_globals is activation_globals
                )
                if not (extension_return or activation_call or activation_return):
                    return
                probing = True
                try:
                    if extension_return:
                        probe_extension(
                            value,
                            frame.f_locals["ledger"],
                            frame.f_locals["identity"],
                            known=frame.f_locals["known"],
                            added_parts=frame.f_locals["added_parts"],
                        )
                    else:
                        proof = frame.f_locals["proof"]
                        kwargs = {
                            name: frame.f_locals[name]
                            for name in ("capability", "operation")
                        }
                        if activation_call:
                            probe_activation(proof, **kwargs)
                            activation_frame = frame
                        else:
                            activation_frame = None
                            if frame.f_lasti in return_offsets[activation_code]:
                                activated.append(proof)
                                with self.assertRaisesRegex(
                                    ReviewInputError, "capability is unissued"
                                ):
                                    real_activate(proof, **kwargs)
                finally:
                    probing = False

            try:
                with registered_resource([registration]):
                    sys.setprofile(observe)
                    result = fixture.invoke()
            finally:
                sys.setprofile(previous)
                activation_frame = None
            self.assertIs(capacity.lifecycle_capacity_extension, real)
            self.assertIs(capacity.activate_lifecycle_extension, real_activate)
            self.assertTrue(observed, fixture.error)
            self.assertTrue(activated, fixture.error)
            # The closed empty-history producer now funds its complete tail.
            # Its success does not settle the original historical transaction
            # or authorize any copied/stale capacity extension tested above.
            self.assertEqual(result, 0, fixture.error)
            self.assertFalse(
                any(review.get("state") == "APPROVED" for review in fixture.reviews)
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
                    self.assertEqual(
                        fixture.invoke(), 0 if stored else 1, fixture.error
                    )
                # Exact readback may confirm a stored checkpoint. That is not
                # a refund of the unknown physical caller mutation or the
                # installed native allocation pool.
                self.assertGreaterEqual(fixture.accounted.accounting.uncertain, 1)
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
