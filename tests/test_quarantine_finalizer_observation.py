"""Real guarded roots may be sized without authorizing a decision or effect."""

import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_action
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_journey import JourneyFixture, registered_resource
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture


class QuarantineFinalizerObservationTests(unittest.TestCase):
    def test_intermediate_decision_sizes_hold_without_approval(self):
        self.exercise(published_count=1, approval=False)

    def test_last_decision_sizes_possible_approval_without_dispatch(self):
        self.exercise(published_count=0, approval=True)

    def test_duplicate_and_conflict_keep_prior_decision_during_refinalization_sizing(
        self,
    ):
        for action in ("dismiss", "accept-risk"):
            with self.subTest(action=action):
                self.exercise(published_count=1, approval=False, repeated_action=action)

    def test_last_decision_all_inline_prefixes_fit_original_root_and_history_caps(self):
        self.exercise(published_count=0, approval=True, measure_inline=True)

    def test_intermediate_and_repeated_decision_inline_prefixes_preserve_holds(self):
        self.exercise(published_count=1, approval=False, measure_inline=True)
        for action in ("dismiss", "accept-risk"):
            with self.subTest(action=action):
                self.exercise(
                    published_count=1,
                    approval=False,
                    repeated_action=action,
                    measure_inline=True,
                )

    def exercise(
        self, *, published_count, approval, repeated_action=None, measure_inline=False
    ):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = (
                JourneyFixture
                if repeated_action is not None
                else PublishedTailJourneyFixture
            )(directory)
            _old, registration = fixture.original_origins(
                failed_count=1, published_count=published_count
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                inventories = (
                    fixture.selected.ledger._quarantine_authenticated.inventories
                )
                origin, inventory = next(
                    (scope, value)
                    for scope, value in inventories.items()
                    if value["assessments"]
                )
                instance = inventory["assessments"][0]["instance"]
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + instance
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                if repeated_action is not None:
                    self.assertEqual(fixture.invoke_command(), 0, fixture.error)
                    if measure_inline:
                        # A new source comment repeats the settlement, not an
                        # invalid second semantic-history row for the same event.
                        fixture.SOURCE_ID += 1
                    fixture.select_command(
                        "@reviewsensei "
                        + repeated_action
                        + " "
                        + instance
                        + ' --reason "quarantine:'
                        + origin
                        + ' Exact source disproves this historical concern."',
                        new_run=True,
                    )
                observations = []
                original = quarantine_action._require_installed_command_tail.__code__

                def observe(frame, event, arg):
                    if frame.f_code is original and event == "call":
                        prepared = frame.f_back.f_locals
                        run = frame.f_locals["run"]
                        before = len(fixture.trace)
                        record = run.caller._effect_record
                        from review_sensei_github.quarantine_command_finalization import (
                            prepare,
                        )

                        from review_sensei.errors import ReviewInputError

                        finalizer = prepared["app"].reviewer.finalizer
                        preparation = prepare(
                            run,
                            prepared["authority"],
                            prepared["target"],
                            prepared["app"],
                        )
                        shadows = []

                        def shadow(*args, **kwargs):
                            shadows.append(True)
                            raise AssertionError("substituted copy hook was invoked")

                        for name in (
                            "__copy__",
                            "__deepcopy__",
                            "__reduce__",
                            "__reduce_ex__",
                            "__getstate__",
                            "__setstate__",
                            "prepare_command_capacity",
                        ):
                            with (
                                self.subTest(copy_hook=name),
                                patch.object(finalizer, name, shadow, create=True),
                            ):
                                with self.assertRaises(ReviewInputError):
                                    prepared[
                                        "app"
                                    ]._observe_quarantine_command_finalization(
                                        run, prepared["authority"], prepared["target"]
                                    )
                                with self.assertRaises(ReviewInputError):
                                    type(finalizer).prepare_quarantine_command_capacity(
                                        finalizer, preparation
                                    )
                        self.assertEqual(shadows, [])
                        self.assertEqual(len(fixture.trace), before)
                        plan = prepared["app"]._observe_quarantine_command_finalization(
                            run, prepared["authority"], prepared["target"]
                        )
                        from review_sensei_github.command_finalization_plan import (
                            CommandFinalizationPlan,
                        )
                        from review_sensei_github.quarantine_command_finalization import (
                            QuarantineFinalizerObservation,
                            require_observation,
                        )

                        from review_sensei.errors import ReviewInputError

                        self.assertIs(type(plan), QuarantineFinalizerObservation)
                        self.assertNotIsInstance(plan, CommandFinalizationPlan)
                        observed_source = require_observation(plan)
                        if measure_inline:
                            from review_sensei_github.quarantine_command_finalization import (
                                measure_inline_branches,
                            )

                            from review_sensei.session import MAX_SESSION_RECORD_BYTES

                            prior_witness = getattr(
                                run.caller, "_accepted_semantic_capacity", None
                            )
                            measurements = measure_inline_branches(plan)
                            self.assertIs(
                                getattr(
                                    run.caller, "_accepted_semantic_capacity", None
                                ),
                                prior_witness,
                            )
                            self.compiler_receipt = {
                                "frozen_limit": run.caller.accounting.configuration.limit,
                                "startup_liability": run.caller.accounting.startup.liability,
                                "prior_liability": run.caller.accounting.prior_nonstartup_liability,
                                "confirmed": run.caller.accounting.confirmed,
                                "uncertain": run.caller.accounting.uncertain,
                                "remaining_unallocated": run.caller.accounting.remaining,
                                "ordinary_remaining": run.caller.budget_view.ordinary_remaining(),
                                "ordinary_hold": run.caller.budget_view._ordinary_hold,
                                "sizing_observation_requests": len(fixture.trace)
                                - before,
                                "blocks": [
                                    {
                                        "role": "ordinary"
                                        if block.block_id == run.caller._block
                                        else "terminal"
                                        if block.block_id == run.caller._terminal_block
                                        else "other",
                                        "undispatched": block.undispatched,
                                    }
                                    for block in run.caller.accounting.blocks
                                ],
                                "branch_checkpoints": [
                                    len(branch) for branch in measurements["branches"]
                                ],
                                "native_requests": len(measurements["native_requests"]),
                                "guard_certificates": None
                                if observed_source["guard"] is None
                                else copy.deepcopy(
                                    observed_source["guard"].certificates
                                ),
                            }
                            from review_sensei_github import (
                                quarantine_command_finalization,
                            )
                            from review_sensei_github.command_finalization_plan import (
                                attach_execution_reads,
                            )

                            private_plan = (
                                quarantine_command_finalization._NATIVE_PLANS[plan]
                            )
                            with self.assertRaisesRegex(
                                ReviewInputError, "read_tail_unqualified"
                            ):
                                private_plan.journal.preflight_command_finalization(
                                    private_plan,
                                    preview_record=private_plan.record,
                                    capacity_details={
                                        "original_root": private_plan.record.record_sha256,
                                        "preview_root": private_plan.record.record_sha256,
                                        "intent_sha256": "0" * 64,
                                        "staged_parts": (),
                                    },
                                    http=private_plan.http,
                                    budget=private_plan.budget,
                                )
                            with self.assertRaisesRegex(
                                ReviewInputError, "cannot authorize execution"
                            ):
                                attach_execution_reads(
                                    private_plan,
                                    None,
                                    private_plan.journal,
                                    dispositions=(),
                                )
                            original_private_payload = private_plan.payload
                            for altered in (
                                b'{"read_contract":"closed-plain-single-complete-page-v1"}',
                                b"malformed-json",
                            ):
                                try:
                                    object.__setattr__(private_plan, "payload", altered)
                                    with self.assertRaisesRegex(
                                        ReviewInputError, "changed"
                                    ):
                                        measure_inline_branches(plan)
                                    with self.assertRaisesRegex(
                                        ReviewInputError, "cannot authorize execution"
                                    ):
                                        attach_execution_reads(
                                            private_plan,
                                            None,
                                            private_plan.journal,
                                            dispositions=(),
                                        )
                                    with self.assertRaisesRegex(
                                        ReviewInputError, "observation changed"
                                    ):
                                        private_plan.require(
                                            private_plan.journal,
                                            http=private_plan.http,
                                            budget=private_plan.budget,
                                        )
                                finally:
                                    object.__setattr__(
                                        private_plan,
                                        "payload",
                                        original_private_payload,
                                    )
                            self.assertGreater(measurements["max_checkpoints"], 0)
                            for branch in measurements["branches"]:
                                for checkpoint in branch:
                                    self.assertLessEqual(
                                        checkpoint["root_bytes"],
                                        MAX_SESSION_RECORD_BYTES,
                                    )
                                    self.assertLessEqual(
                                        checkpoint["reference_bytes"], 8192
                                    )
                        with self.assertRaisesRegex(ReviewInputError, "unissued"):
                            require_observation(copy.copy(plan))
                        original_payload = plan.payload
                        try:
                            object.__setattr__(plan, "payload", b"{}")
                            with self.assertRaisesRegex(ReviewInputError, "changed"):
                                require_observation(plan)
                        finally:
                            object.__setattr__(plan, "payload", original_payload)
                        authenticated = prepared["target"].authenticated
                        archive = authenticated.archive_bytes
                        try:
                            object.__setattr__(authenticated, "archive_bytes", b"{}")
                            with self.assertRaisesRegex(
                                ReviewInputError, "custody changed"
                            ):
                                require_observation(plan)
                        finally:
                            object.__setattr__(authenticated, "archive_bytes", archive)
                        original_frontier = prepared["target"].native_frontier_bytes
                        try:
                            object.__setattr__(
                                prepared["target"], "native_frontier_bytes", b"[]"
                            )
                            with self.assertRaisesRegex(
                                ReviewInputError, "custody changed"
                            ):
                                require_observation(plan)
                        finally:
                            object.__setattr__(
                                prepared["target"],
                                "native_frontier_bytes",
                                original_frontier,
                            )
                        original_command = prepared["target"].command
                        try:
                            object.__setattr__(prepared["target"], "command", None)
                            with self.assertRaisesRegex(
                                ReviewInputError, "command changed"
                            ):
                                require_observation(plan)
                        finally:
                            object.__setattr__(
                                prepared["target"], "command", original_command
                            )
                        if repeated_action is not None:
                            self.assertFalse(
                                observed_source["projection"].creates_event
                            )
                            self.assertEqual(
                                observed_source["projection"].state_bytes,
                                prepared["target"].authenticated.state_bytes,
                            )
                        self.assertIs(run.caller._effect_record, record)
                        self.assertIsNone(run.caller._action_effect_boundary)
                        self.assertIsNone(run.caller._checkpoint)
                        changes = [
                            (method, path)
                            for _, method, path in fixture.trace[before:]
                            if method not in {"GET", "HEAD"}
                            and path != "/graphql"
                            and not path.startswith("/api/github/")
                        ]
                        self.assertEqual(changes, [])
                        observations.append(json.loads(plan.payload))
                        if repeated_action is not None:
                            from review_sensei.errors import ReviewInputError

                            raise ReviewInputError(
                                "synthetic repeated-decision observation complete"
                            )

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    self.assertNotEqual(fixture.invoke_command(), 0)
                finally:
                    sys.setprofile(previous)
                self.assertIn(
                    "synthetic repeated-decision observation complete"
                    if repeated_action is not None
                    else "installed command tail has no complete lifecycle proof",
                    fixture.error,
                )
                self.assertEqual(len(observations), 1)
                payload = observations[0]
                self.assertEqual(
                    payload["read_contract"], "quarantine-sizing-observation-only-v1"
                )
                self.assertEqual(payload["conditional_approval"] is not None, approval)
                self.assertEqual(payload["cleanup_requests"], [])
                self.assertIsNone(payload["execution_read_tail"])
                self.assertIsNone(payload["execution_capability_tail"])
                if repeated_action is not None:
                    # An unchanged immutable decision can still require a native
                    # body re-render. Its full finalizer must not be priced away.
                    self.assertEqual(payload["review_update"][0], "PUT")


class QuarantineInstallerTests(unittest.TestCase):
    def test_external_first_install_and_public_reset_cannot_grant_an_issuer(self):
        import subprocess

        source = """
from review_sensei_github import quarantine_lifecycle_transfer as transfer
from review_sensei.errors import ReviewInputError
try:
    transfer.install_source(lambda *args, **kwargs: None)
except ReviewInputError as error:
    assert 'defining module' in str(error), str(error)
else:
    raise AssertionError('external first install accepted')
from review_sensei_github import quarantine_action
from review_sensei.source_registry import require_module_seal
require_module_seal(transfer)
transfer._INSTALLED = False
transfer._installation_state = {'installed': False}
try:
    transfer.install_source(quarantine_action._current_publication_digest)
except ReviewInputError as error:
    assert 'already installed' in str(error), str(error)
else:
    raise AssertionError('public reset replay accepted')
require_module_seal(transfer)
"""
        completed = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
