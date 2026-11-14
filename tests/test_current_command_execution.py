"""Unissued current contexts never authorize an effect or a copied caller."""

import copy
import unittest
from unittest.mock import patch

from review_sensei_github import current_command_execution as execution

from review_sensei.errors import ReviewInputError


class Caller:
    pass


class CurrentCommandExecutionTests(unittest.TestCase):
    def test_replaced_dynamic_verifier_cannot_claim_no_trust(self):
        from review_sensei_github import trust_effects

        with patch.object(trust_effects, "verification_profile", lambda caller: None):
            with self.assertRaisesRegex(
                ReviewInputError, "verification source changed"
            ):
                execution._trust_profile(Caller())

    def test_mutated_captured_verifier_cannot_claim_no_trust(self):
        from review_sensei_github import api_run_requests

        original = api_run_requests._original_trust_verification_profile
        code = original.__code__
        replacement = code.replace(co_name="changed_verifier")
        try:
            original.__code__ = replacement
            with self.assertRaises(ReviewInputError):
                execution._trust_profile(Caller())
        finally:
            original.__code__ = code
        self.assertIsNone(execution._trust_profile(Caller()))

    def test_direct_issue_has_no_original_preflight_custody(self):
        with self.assertRaisesRegex(ReviewInputError, "original preflight"):
            execution.issue(None)

    def test_constructed_and_copied_contexts_are_not_writer_authority(self):
        value = execution._Context()
        for token in (value, copy.copy(value), copy.deepcopy(value)):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ReviewInputError, "unissued"):
                    execution.consume_context(token, None, None, {}, 0)

    def test_unissued_invocation_marker_is_only_a_negative_fence(self):
        caller = Caller()
        self.assertFalse(execution.namespace(caller, 1))
        caller._current_command_execution = execution._Invocation()
        for candidate in (caller, copy.copy(caller)):
            with self.assertRaisesRegex(ReviewInputError, "copied or expired"):
                execution.namespace(candidate, 1)

    def test_no_active_lease_does_not_issue_a_runtime_proposal(self):
        self.assertIsNone(execution.lifecycle_context(None, None, {}, 0))


class OriginalCurrentCommandExecutionTests(unittest.TestCase):
    def test_actual_first_command_keeps_negative_custody_and_terminal_funds(self):
        self._run_first_command(losses=False)

    def test_actual_first_command_retains_combined_stored_losses(self):
        self._run_first_command(losses=True)

    def _run_first_command(self, *, losses):
        import dis
        import inspect
        import sys

        from review_sensei_github import action_run
        from test_action_run import CommandEntryFixture
        from test_bounded_snapshot_lifecycle import BoundedSnapshotLifecycleTests

        from review_sensei.api_work import ApiRunAccounting

        class Captured(BaseException):
            pass

        previous = sys.getprofile()
        completed = []
        controls = []
        context_controls = {"copied": 0, "reused": 0}
        returned = []
        preflight_refusals = []
        original_pending = []
        lost_request_ids = set()
        initial_provider_calls = []
        fault_state = {
            "root_patches": 0,
            "root_get_losses": 0,
            "receipt_posts": 0,
            "receipt_losses": 0,
            "lost_roots": set(),
            "root_writes": {},
            "root_roles": {},
            "role_by_root": {},
            "terminal_lost_id": None,
        }
        execute_code = inspect.unwrap(action_run.execute_command).__code__
        returns = {
            instruction.offset
            for instruction in dis.get_instructions(execute_code)
            if instruction.opname in {"RETURN_VALUE", "RETURN_CONST"}
        }

        def observe(frame, event, value):
            if previous is not None:
                previous(frame, event, value)
            if (
                losses
                and event == "call"
                and frame.f_code is CommandEntryFixture.invoke_command.__code__
            ):
                fixture = frame.f_locals["self"]
                external = fixture.external

                def lose_stored_response(request, timeout):
                    from urllib.parse import urlsplit

                    active = fixture.accounted in execution._ACTIVE
                    path, method = urlsplit(request.full_url).path, request.get_method()
                    snapshot = getattr(
                        fixture.accounted, "_quarantine_checkpoint_snapshot", None
                    )
                    root = (
                        snapshot.root
                        if active
                        and snapshot is not None
                        and path
                        == f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
                        else None
                    )
                    if root is not None and method == "PATCH":
                        fault_state["root_patches"] += 1
                        writes = fault_state["root_writes"]
                        writes[root] = writes.get(root, 0) + 1
                        caller = fixture.accounted
                        entry = execution._ACTIVE[caller]
                        if caller._terminal_reason is not None:
                            role = "terminal"
                        elif not entry["own_confirmed"]:
                            role = "own-decision"
                        else:
                            from review_sensei_github.finding_lifecycle import (
                                FindingLifecycleJournal,
                            )

                            checkpoint = execution._frame(
                                FindingLifecycleJournal.checkpoint
                            )
                            self.assertIsNotNone(checkpoint)
                            self.assertIs(checkpoint.f_locals["self"], entry["journal"])
                            role = (
                                "pending-intent"
                                if checkpoint.f_locals["pending_intent"]
                                else "journal-finish"
                            )
                        roles = fault_state["root_roles"]
                        roles[role] = roles.get(role, 0) + 1
                        fault_state["role_by_root"][root] = role
                    response = external(request, timeout)
                    if not active:
                        return response
                    if (
                        root is not None
                        and method == "GET"
                        and snapshot.operation.write_attempted
                        and root not in fault_state["lost_roots"]
                    ):
                        fault_state["lost_roots"].add(root)
                        fault_state["root_get_losses"] += 1
                        lost_identifier = fixture.accounted.observations[-1][
                            "request_id"
                        ]
                        lost_request_ids.add(lost_identifier)
                        if fault_state["role_by_root"][root] == "terminal":
                            fault_state["terminal_lost_id"] = lost_identifier
                        raise TimeoutError("synthetic stored current root final read")
                    if (
                        method == "POST"
                        and path == "/repos/owner/repo/issues/7/comments"
                        and b"reviewsensei:command-receipt:v1" in (request.data or b"")
                    ):
                        fault_state["receipt_posts"] += 1
                        fault_state["receipt_losses"] += 1
                        lost_request_ids.add(
                            fixture.accounted.observations[-1]["request_id"]
                        )
                        raise TimeoutError("synthetic stored current command receipt")
                    return response

                fixture.external = lose_stored_response
            if (
                event == "call"
                and frame.f_code is CommandEntryFixture.invoke_command.__code__
            ):
                initial_provider_calls.append(frame.f_locals["self"].provider_calls)
            if event != "return":
                return
            if frame.f_code.co_name == "_preflight_command_finalization":
                if getattr(value, "applied", None) is False:
                    preflight_refusals.append(value.summary)
            if frame.f_code is execution.issue.__code__ and isinstance(value, dict):
                entry = value
                self.assertEqual(entry["parts"], 3)
                self.assertEqual(entry["limits"], {"ordinary": 161, "terminal": 17})
                blocks = {block.block_id: block for block in entry["state"].blocks}
                self.assertEqual(blocks[entry["ordinary_block"]].undispatched, 380)
                self.assertEqual(blocks[entry["terminal_block"]].undispatched, 68)
                caller = entry["ledger"].caller
                before = caller._sequence, len(caller.observations)
                original_pending.append(
                    {
                        request.request_id
                        for block in caller.accounting.blocks
                        for request in block.pending
                    }
                )
                with self.assertRaises(ReviewInputError):
                    execution._require(copy.copy(caller))
                controls.append("copied-caller")
                mutations = (
                    (caller, "_root", "0" * 64, "foreign-root"),
                    (entry["plan"], "payload", b"{}", "changed-intent-plan"),
                    (
                        entry["state"],
                        "prior_nonstartup_liability",
                        entry["state"].prior_nonstartup_liability + 1,
                        "changed-frozen-funding",
                    ),
                    (
                        entry["allocation"],
                        "metadata_unknown",
                        entry["allocation"].metadata_unknown + 1,
                        "changed-full-plan",
                    ),
                    (
                        entry["guard"],
                        "future_bytes",
                        entry["guard"].future_bytes + 1,
                        "changed-native-guard",
                    ),
                    (
                        action_run.execute_command,
                        "__wrapped__",
                        lambda: None,
                        "forged-source-wrapper",
                    ),
                )
                for owner, name, altered, label in mutations:
                    old = getattr(owner, name)
                    try:
                        object.__setattr__(owner, name, altered)
                        with self.assertRaises(ReviewInputError, msg=label):
                            execution._require(caller)
                    finally:
                        object.__setattr__(owner, name, old)
                    controls.append(label)
                    self.assertIs(execution._require(caller), entry)
                self.assertEqual((caller._sequence, len(caller.observations)), before)
            if (
                frame.f_code is execution.lifecycle_context.__code__
                and type(value) is execution._Context
            ):
                with self.assertRaisesRegex(ReviewInputError, "unissued"):
                    execution.consume_context(copy.copy(value), None, None, {}, 0)
                context_controls["copied"] += 1
            if frame.f_code is execution.consume_context.__code__ and value is None:
                token = frame.f_locals["token"]
                if token not in execution._CONTEXTS:
                    with self.assertRaisesRegex(ReviewInputError, "unissued"):
                        execution.consume_context(token, None, None, {}, 0)
                    context_controls["reused"] += 1
            if frame.f_code is execute_code and frame.f_lasti in returns:
                caller = frame.f_locals["caller"]
                self.assertEqual(value, 0)
                self.assertTrue(frame.f_locals["result"].applied)
                self.assertEqual(caller.accounting.action["phase"], "terminal")
                self.assertNotIn(caller, execution._ACTIVE)
                with self.assertRaises(ReviewInputError):
                    execution.namespace(caller, 1)
                returned.append(caller.accounting)
            if frame.f_code is CommandEntryFixture.invoke_command.__code__:
                fixture = frame.f_locals["self"]
                self.assertEqual(
                    value,
                    0,
                    str(
                        (controls, preflight_refusals, fixture.error, fixture.exception)
                    ),
                )
                self.assertEqual(fixture.provider_calls, initial_provider_calls[0])
                accounting = ApiRunAccounting.from_document(
                    fixture.current_record().api_run_accounting
                )
                self.assertEqual(accounting.action["phase"], "terminal")
                self.assertIsNotNone(accounting.action["result"])
                self.assertEqual(accounting.configuration.limit, 1152)
                blocks = {block.block_id: block for block in accounting.blocks}
                self.assertGreaterEqual(blocks["hosted-review"].undispatched, 219)
                self.assertGreaterEqual(blocks["hosted-terminal"].undispatched, 51)
                completed.append(accounting)
                raise Captured()

        try:
            sys.setprofile(observe)
            with self.assertRaises(Captured):
                BoundedSnapshotLifecycleTests(
                    "test_current_and_historical_decisions_finish_with_all_late_carriers"
                ).test_current_and_historical_decisions_finish_with_all_late_carriers()
        finally:
            sys.setprofile(previous)
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(returned), 1)
        self.assertEqual(len(controls), 7)
        self.assertGreaterEqual(context_controls["copied"], 3)
        self.assertEqual(context_controls["reused"], 3)
        if losses:
            self.assertEqual(fault_state["root_patches"], 5, fault_state)
            self.assertEqual(fault_state["root_get_losses"], 5)
            self.assertEqual(set(fault_state["root_writes"].values()), {1})
            self.assertEqual(
                fault_state["root_roles"],
                {
                    "own-decision": 1,
                    "pending-intent": 2,
                    "journal-finish": 1,
                    "terminal": 1,
                },
            )
            self.assertEqual(fault_state["receipt_posts"], 1)
            self.assertEqual(fault_state["receipt_losses"], 1)
            self.assertEqual(len(lost_request_ids), 6)
            self.assertEqual(len(original_pending), 1)
            from dataclasses import replace

            from review_sensei.api_work import PendingApiRequest

            live, durable = returned[0], completed[0]

            def pending(accounting):
                return {
                    request.request_id
                    for block in accounting.blocks
                    for request in block.pending
                }

            def terminal(accounting):
                return next(
                    b for b in accounting.blocks if b.block_id == "hosted-terminal"
                )

            terminal_lost = fault_state["terminal_lost_id"]
            self.assertIsNotNone(terminal_lost)
            self.assertEqual(pending(live), original_pending[0] | lost_request_ids)
            self.assertEqual(pending(durable), pending(live) - {terminal_lost})
            self.assertTrue(original_pending[0] <= pending(durable))
            self.assertNotIn(terminal_lost, {p.request_id for p in live.completed})
            self.assertNotIn(terminal_lost, {p.request_id for p in durable.completed})
            self.assertIn(terminal_lost, {p.request_id for p in terminal(live).pending})
            self.assertEqual(terminal(durable).issued_by, live.owner)
            self.assertEqual(terminal(live).issued_by, live.owner)
            self.assertEqual(
                (terminal(durable).undispatched, terminal(live).undispatched), (63, 58)
            )
            self.assertEqual((durable.remaining, live.remaining), (510, 510))
            durable.require_successor(live)
            newly_confirmed = set(live.completed) - set(durable.completed)
            self.assertEqual(len(newly_confirmed), 4)
            self.assertEqual(
                terminal(durable).undispatched - terminal(live).undispatched,
                len(newly_confirmed) + len(pending(live) - pending(durable)),
            )
            # Semantic action terminal is not complete physical settlement.
            # The final GET was issued after the immutable root serialization:
            # its exact live unknown ID remains covered durably by the original
            # owner's nonrefundable terminal slot, not a fabricated completion.
            with self.assertRaisesRegex(ReviewInputError, "startup slot"):
                durable.claim_startup(durable.owner)
            next_owner = replace(durable.owner, run_attempt=2, generation=1)
            successor = durable.claim_startup(next_owner)
            durable.require_successor(successor)
            self.assertEqual(successor.blocks, durable.blocks)
            self.assertEqual(successor.remaining, durable.remaining)
            with self.assertRaisesRegex(ReviewInputError, "issuing owner"):
                successor.dispatch(
                    "hosted-terminal", PendingApiRequest("forbidden-terminal", 1)
                )
            reduced = replace(terminal(successor), undispatched=62)
            refund = replace(
                successor,
                blocks=tuple(
                    reduced if b.block_id == reduced.block_id else b
                    for b in successor.blocks
                ),
            )
            with self.assertRaisesRegex(ReviewInputError, "discard prior charges"):
                durable.require_successor(refund)
            lost_request = next(
                p for p in terminal(live).pending if p.request_id == terminal_lost
            )
            invented_completion = replace(
                refund, completed=(*refund.completed, lost_request)
            )
            self.assertEqual(invented_completion.remaining, durable.remaining)
            with self.assertRaisesRegex(ReviewInputError, "discard owner liability"):
                durable.require_successor(invented_completion)
            with self.assertRaisesRegex(ReviewInputError, "frozen run allowance"):
                successor.reserve("forbidden-borrow", successor.remaining + 1)
