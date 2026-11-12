"""Qualification for narrow unstarted-action cancellation/supersession recovery (G32).

Verifies that:
1. Default fail-closed hold is preserved when no authenticated witness is supplied.
2. Authenticated ``UnstartedCancellationWitness`` settles proven unstarted actions
   (``action.phase == "pending"``, ``execution_started is False``, ``uncertain == 0``)
   cleanly ONCE to ``{"kind": "canceled_unstarted", "reason": "source_edited" | "source_deleted"}``
   both in-run (``cancel_unstarted_action``) and cross-run after attempt exhaustion
   (``rollover_unstarted_and_activate``), preserving all prior charges, blocks,
   dispositions, and immutable ``caller-run-root-v1`` archived history.
3. Subsequent PR reviews and maintainer commands recover and complete end-to-end
   without touching ``controlled_restart`` or ``controlled_restarts.json``.
4. Same-source succession continues to execute normally, while every counterexample
   (started work, accepted result, unknown dispatch, altered intent, foreign owner,
   missing cessation proof, forged witness, HTTP/payload faults) refuses fail-closed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from fake_github_http import json_response
from review_sensei_github import controlled_restart
from review_sensei_github.api_run_history import (
    ActionResultReadContext,
    read_action_result,
    read_ancestor_action_results,
    read_previous_run,
)
from review_sensei_github.api_run_owner import ApiRunActionsEvidence
from review_sensei_github.api_run_requests import (
    ApiRunRequestTransport,
    ApiRunSessionLedger,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from review_sensei_github.unstarted_action_recovery import (
    UnstartedCancellationWitness,
    authenticate_unstarted_cancellation,
    require_witness_issued,
    settle_unstarted_accounting,
    settle_unstarted_record,
)
from test_action_run import CommandEntryFixture
from test_api_work_lifecycle import LifecycleFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.recovery_authority import Access, Funding
from review_sensei.writer_containment import (
    LIVE_CANONICAL_CUTOVER_ENABLED,
    ContainedForwardingCoordinator,
    WireDispatchOutcome,
    WriterContainmentPosture,
)

SOURCE_ID = 41
SOURCE_STAMP = "2026-10-25T12:00:00Z"
INITIAL_BODY = "@reviewsensei review pause"
SOURCE = {
    "kind": "issue_comment",
    "id": SOURCE_ID,
    "updated_at": SOURCE_STAMP,
    "body_sha256": hashlib.sha256(INITIAL_BODY.encode("utf-8")).hexdigest(),
}


class G32LifecycleFixture(LifecycleFixture):
    """Extends ``LifecycleFixture`` with a mutable GitHub source comment endpoint."""

    def __init__(self, directory: str, *, limit: int = 1024) -> None:
        Path(directory).mkdir(parents=True, exist_ok=True)
        super().__init__(directory, limit=limit)
        self.source_status = 200
        self.source_payload: object = {
            "id": SOURCE_ID,
            "updated_at": SOURCE_STAMP,
            "body": INITIAL_BODY,
        }
        self.past_runs: dict[int, tuple[int, str, str]] = {}

    def _external_open(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if method == "GET" and path in {
            f"/repos/owner/repo/issues/comments/{SOURCE_ID}",
            f"/repos/owner/repo/pulls/comments/{SOURCE_ID}",
        }:
            self.trace.append((self.attempt, method, path))
            return json_response(
                copy.deepcopy(self.source_payload), status=self.source_status
            )
        if method == "GET" and "/actions/runs/" in path:
            suffix = path.split("/actions/runs/", 1)[1]
            parts = suffix.split("/")
            queried_run_id = int(parts[0])
            if queried_run_id in self.past_runs:
                attempt_no, job_name, run_status = self.past_runs[queried_run_id]
                self.trace.append((self.attempt, method, path))
                if len(parts) == 1:
                    return json_response(
                        {
                            "id": queried_run_id,
                            "run_attempt": attempt_no,
                            "status": run_status,
                            "conclusion": "failure"
                            if run_status == "completed"
                            else None,
                            "event": "issue_comment",
                            "head_sha": HEAD,
                            "repository": {
                                "id": IDENTITY.repository_id,
                                "full_name": IDENTITY.repository,
                            },
                        }
                    )
                q_attempt = int(parts[2])
                return json_response(
                    {
                        "total_count": 1,
                        "jobs": [
                            {
                                "id": 9000 + queried_run_id * 10 + q_attempt,
                                "run_id": queried_run_id,
                                "run_attempt": q_attempt,
                                "name": f"ReviewSensei / {job_name}",
                                "status": run_status,
                                "conclusion": "failure"
                                if run_status == "completed"
                                else None,
                                "completed_at": "2026-10-25T12:05:00Z"
                                if run_status == "completed"
                                else None,
                            }
                        ],
                    }
                )
        return super()._external_open(request, timeout)


class G32CommandEntryFixture(CommandEntryFixture):
    """Preserves past run jobs and completed attempt numbers across command/review runs."""

    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.past_run_attempts: dict[int, int] = {}
        self.deleted_source_ids: set[int] = set()

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if method == "GET" and path.startswith("/repos/owner/repo/issues/comments/"):
            cid = int(path.rsplit("/", 1)[1])
            if cid in self.deleted_source_ids:
                self._meter(request)
                self.source_reads += 1
                return json_response({"message": "Not Found"}, status=404)
        response = super().external(request, timeout)
        path = urlsplit(request.full_url).path
        if "/actions/runs/" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if path.endswith("/jobs") and run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
            if not path.endswith("/jobs") and run_id in self.past_run_attempts:
                value = json.loads(response.read(524288))
                value["run_attempt"] = self.past_run_attempts[run_id]
                return json_response(value)
        return response


def _make_canonical_coordinator(
    directory: Path, *, epoch: int = 1
) -> tuple[ContainedForwardingCoordinator, object]:
    coordinator = ContainedForwardingCoordinator(
        directory,
        posture=WriterContainmentPosture.fully_contained(),
        wire_forwarder=lambda _m, _t, _b, _d: WireDispatchOutcome(200, b"{}"),
        synthetic_qualification=True,
    )
    funding = Funding(
        reference="a" * 64,
        calls=512,
        protected_calls=50,
        deadline_ms=100_000,
        retained_bytes=0,
        retained_parts=0,
        comments=0,
    )
    access_1 = Access(
        repository=IDENTITY.repository,
        repository_id=IDENTITY.repository_id or 123,
        pull_request=IDENTITY.pull_request,
        store_id="1" * 64,
        epoch=1,
        caller_sha256="2" * 64,
        source_sha256="3" * 64,
        head_sha=HEAD,
        configuration_sha256="4" * 64,
        run_id=456,
        attempt=1,
        actor_id=42,
        actor_role="admin",
        workflow_sha="5" * 40,
        policy_commit="6" * 40,
        funding=funding,
        native_exclusive=True,
    )
    token, _snap = coordinator.enroll_initial_writer(
        base_access=access_1, writer_route="actions_command"
    )
    for idx in range(2, epoch + 1):
        next_access = replace(access_1, caller_sha256=f"{idx:064x}")
        token, _snap = coordinator.advance_epoch(
            predecessor_token=token,
            successor_access=next_access,
            writer_route="actions_hosted",
            predecessor_ceased=True,
        )
    return coordinator, token


class UnstartedActionRecoveryG32Tests(unittest.TestCase):
    def _prepare(
        self, directory: str, *, purpose: str = "command", kind: str = "issue_comment"
    ):
        f = G32LifecycleFixture(directory, limit=1024)
        native, configuration, evidence, owner = f.prelude()
        f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
        ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
        source = {**SOURCE, "kind": kind}
        intent = f.accounted.admit_action(
            ledger,
            IDENTITY,
            purpose=purpose,
            source=source,
            head_sha=HEAD,
            operation_key="d" * 64,
        )
        return f, native, ledger, evidence, configuration, intent

    def test_controlled_restart_registry_remains_empty_and_live_cutover_disabled(
        self,
    ) -> None:
        self.assertFalse(LIVE_CANONICAL_CUTOVER_ENABLED)
        self.assertEqual(controlled_restart.registrations(), [])

    def test_default_without_witness_holds_and_same_source_succession_completes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # 1. Same-source succession: unchanged live source refuses G32 cancellation
            # and executes normally on attempt 2 with prior charges preserved.
            f, _native, ledger, evidence, _cfg, intent = self._prepare(
                str(Path(directory) / "same_source")
            )
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            with self.assertRaisesRegex(
                ReviewInputError, "same-source succession must execute normally"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=record,
                    actions_evidence=evidence,
                )
            old_blocks = ApiRunAccounting.from_document(
                record.api_run_accounting
            ).blocks
            native_2 = f.recover_lifecycle()
            successor = ApiRunSessionLedger(native_2, f.accounted, IDENTITY)
            self.assertEqual(
                f.accounted.admit_action(
                    successor,
                    IDENTITY,
                    purpose="command",
                    source=SOURCE,
                    head_sha=HEAD,
                    operation_key="d" * 64,
                ),
                intent,
            )
            for block in old_blocks:
                self.assertIn(block, f.accounted.accounting.blocks)
            boundary = f.accounted.begin_action_effect(successor, IDENTITY, intent)
            proof = f.accounted.settle_action_effect(
                successor,
                IDENTITY,
                boundary,
                outcome={"kind": "explicit_noop", "reason": "status"},
                result_document={"action": "status", "applied": False},
            )
            closed = f.accounted.close_action(successor, IDENTITY, proof)
            self.assertEqual(
                ApiRunAccounting.from_document(closed.api_run_accounting).action[
                    "phase"
                ],
                "terminal",
            )

            # 2. Without witness, source edit holds both same-run and ordinary rollover.
            f_hold, _n2, ledger_hold, _ev2, cfg_hold, _intent2 = self._prepare(
                str(Path(directory) / "default_hold")
            )
            f_hold.source_payload = {
                "id": SOURCE_ID,
                "updated_at": "2026-10-25T12:01:00Z",
                "body": INITIAL_BODY + " edited",
            }
            with self.assertRaisesRegex(
                ReviewInputError, "source or operation conflicts"
            ):
                f_hold.accounted.admit_action(
                    ledger_hold,
                    IDENTITY,
                    purpose="command",
                    source={
                        **SOURCE,
                        "updated_at": "2026-10-25T12:01:00Z",
                        "body_sha256": "e" * 64,
                    },
                    head_sha=HEAD,
                    operation_key="d" * 64,
                )
            stranded = parse_session_comment(f_hold.comment["body"], identity=IDENTITY)
            f_hold.trace = []
            run2_dir = Path(directory) / "default_hold_run2"
            run2_dir.mkdir()
            caller_2 = ApiRunRequestTransport(
                directory=run2_dir,
                run=ApiRunIdentity(123, 457),
                plan=f_hold.plan,
                attempt=1,
                opener=f_hold._external_open,
            )
            f_hold.accounted = caller_2
            http_2 = GitHubHttp(api_url="https://api.github.test", opener=caller_2.open)
            native_run2 = GitHubIssueCommentSessionLedger(
                http_2, token="synthetic", app_slug="reviewsensei[bot]"
            )
            native_run2.load(IDENTITY)
            with self.assertRaisesRegex(
                ReviewInputError, "previous caller run is not terminal"
            ):
                caller_2.rollover_and_activate(
                    native_run2,
                    IDENTITY,
                    cfg_hold,
                    ApiExecutionOwner(1, "hosted", 0),
                    stranded,
                    lambda: None,
                )

    def test_same_run_cancel_unstarted_action_for_edit_and_delete_preserves_charges_and_allows_rollover(
        self,
    ) -> None:
        for mutation, purpose, kind in (
            ("edited", "command", "issue_comment"),
            ("deleted", "reply", "review_comment"),
        ):
            with (
                self.subTest(mutation=mutation, purpose=purpose),
                tempfile.TemporaryDirectory() as directory,
            ):
                f, _native, ledger, _evidence, config, intent = self._prepare(
                    directory, purpose=purpose, kind=kind
                )
                if mutation == "edited":
                    f.source_payload = {
                        "id": SOURCE_ID,
                        "updated_at": "2026-10-25T12:02:00Z",
                        "body": "@reviewsensei review status",
                    }
                    expected_reason = "source_edited"
                else:
                    f.source_status = 404
                    f.source_payload = {"message": "Not Found"}
                    expected_reason = "source_deleted"

                before_blocks = ApiRunAccounting.from_document(
                    parse_session_comment(
                        f.comment["body"], identity=IDENTITY
                    ).api_run_accounting
                ).blocks
                before_confirmed = f.accounted.accounting.confirmed
                # Exercise attempt-2 same-run successor recovery with predecessor cessation.
                native_2 = f.recover_lifecycle()
                successor = ApiRunSessionLedger(native_2, f.accounted, IDENTITY)
                ev_2 = ApiRunActionsEvidence(
                    successor.http,
                    token="synthetic-actions",
                    command_run_id="456",
                    job="hosted",
                )
                current_record = parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                )
                witness = authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=successor,
                    identity=IDENTITY,
                    record=current_record,
                    actions_evidence=ev_2,
                    expected_intent=intent,
                    mode="same_run",
                )
                self.assertEqual(witness.reason, expected_reason)
                self.assertEqual(witness.authority_mode, "same_run_ceased_predecessor")

                settled = f.accounted.cancel_unstarted_action(
                    successor, IDENTITY, witness
                )
                settled_state = ApiRunAccounting.from_document(
                    settled.api_run_accounting
                )
                self.assertEqual(settled_state.action["phase"], "terminal")
                self.assertFalse(settled_state.action["execution_started"])
                self.assertEqual(
                    settled_state.action["outcome"],
                    {"kind": "canceled_unstarted", "reason": expected_reason},
                )
                self.assertGreaterEqual(settled_state.confirmed, before_confirmed)
                for block in before_blocks:
                    self.assertIn(block, settled_state.blocks)
                decoded = read_action_result(successor, IDENTITY, settled)
                self.assertEqual(
                    decoded["outcome"],
                    {"kind": "canceled_unstarted", "reason": expected_reason},
                )

                # Idempotent re-call with the same witness performs zero extra PATCHes.
                patches_before = sum(m == "PATCH" for _, m, _ in f.trace)
                again = f.accounted.cancel_unstarted_action(
                    successor, IDENTITY, witness
                )
                self.assertEqual(again.record_sha256, settled.record_sha256)
                self.assertEqual(
                    sum(m == "PATCH" for _, m, _ in f.trace), patches_before
                )

                # Now an ordinary new caller run rolls over and archives cleanly!
                f.past_runs[456] = (2, "hosted", "completed")
                f.attempt = 1
                f.trace = []
                next_dir = Path(directory) / "run457"
                next_dir.mkdir()
                caller_next = ApiRunRequestTransport(
                    directory=next_dir,
                    run=ApiRunIdentity(123, 457),
                    plan=f.plan,
                    attempt=1,
                    opener=f._external_open,
                )
                f.accounted = caller_next
                http_next = GitHubHttp(
                    api_url="https://api.github.test", opener=caller_next.open
                )
                native_next = GitHubIssueCommentSessionLedger(
                    http_next, token="synthetic", app_slug="reviewsensei[bot]"
                )
                native_next.load(IDENTITY)
                ev_next = ApiRunActionsEvidence(
                    http_next,
                    token="synthetic-actions",
                    command_run_id="457",
                    job="hosted",
                )
                caller_next.rollover_and_activate(
                    native_next,
                    IDENTITY,
                    config,
                    ApiExecutionOwner(1, "hosted", 0),
                    settled,
                    lambda: ev_next.ceased_run_owner(
                        IDENTITY, 456, settled_state.owner
                    ),
                )
                next_ledger = ApiRunSessionLedger(native_next, caller_next, IDENTITY)
                archived = read_previous_run(
                    next_ledger, IDENTITY, caller_next.accounting.previous_record
                )
                self.assertEqual(archived.to_dict(), settled.to_dict())

    def test_cross_run_rollover_recovers_after_two_attempt_exhaustion_and_canonical_epoch_fence(
        self,
    ) -> None:
        for auth_kind in ("actions", "canonical"):
            with (
                self.subTest(auth_kind=auth_kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                f, _native, _ledger, _ev, config, intent = self._prepare(directory)
                # Exhaust attempt 2 on run 456 while leaving action pending & unstarted.
                f.recover_lifecycle()
                stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
                stranded_state = ApiRunAccounting.from_document(
                    stranded.api_run_accounting
                )
                self.assertEqual(stranded_state.owner.run_attempt, 2)
                self.assertEqual(stranded_state.action["phase"], "pending")
                self.assertFalse(stranded_state.action["execution_started"])

                # Source comment deleted after attempt 2 ceased.
                f.source_status = 404
                f.source_payload = {"message": "Not Found"}
                f.past_runs[456] = (2, "hosted", "completed")
                f.past_runs[457] = (1, "hosted", "in_progress")
                f.run_id = 457
                f.attempt = 1
                f.trace = []

                run457_dir = Path(directory) / "run457"
                run457_dir.mkdir()
                caller_457 = ApiRunRequestTransport(
                    directory=run457_dir,
                    run=ApiRunIdentity(123, 457),
                    plan=f.plan,
                    attempt=1,
                    opener=f._external_open,
                )
                f.accounted = caller_457
                http_457 = GitHubHttp(
                    api_url="https://api.github.test", opener=caller_457.open
                )
                native_457 = GitHubIssueCommentSessionLedger(
                    http_457, token="synthetic", app_slug="reviewsensei[bot]"
                )
                native_457.load(IDENTITY)

                coordinator, epoch_token = (
                    _make_canonical_coordinator(Path(directory) / "coord", epoch=2)
                    if auth_kind == "canonical"
                    else (None, None)
                )
                ev_457 = (
                    ApiRunActionsEvidence(
                        http_457,
                        token="synthetic-actions",
                        command_run_id="457",
                        job="hosted",
                    )
                    if auth_kind == "actions"
                    else None
                )

                caller_457.rollover_unstarted_and_activate(
                    native_457,
                    IDENTITY,
                    config,
                    ApiExecutionOwner(1, "hosted", 0),
                    stranded,
                    lambda rec: authenticate_unstarted_cancellation(
                        caller=caller_457,
                        ledger=native_457,
                        identity=IDENTITY,
                        record=rec,
                        actions_evidence=ev_457,
                        canonical_coordinator=coordinator,
                        canonical_token=epoch_token,  # type: ignore[arg-type]
                        expected_intent=intent,
                        mode="cross_run",
                    ),
                )
                ledger_457 = ApiRunSessionLedger(native_457, caller_457, IDENTITY)
                archived = read_previous_run(
                    ledger_457, IDENTITY, caller_457.accounting.previous_record
                )
                archived_state = ApiRunAccounting.from_document(
                    archived.api_run_accounting
                )
                self.assertEqual(archived_state.identity.run_id, 456)
                self.assertEqual(archived_state.blocks, stranded_state.blocks)
                self.assertEqual(archived_state.completed, stranded_state.completed)
                self.assertEqual(
                    archived_state.action["outcome"],
                    {"kind": "canceled_unstarted", "reason": "source_deleted"},
                )
                retained_result = read_action_result(ledger_457, IDENTITY, archived)
                self.assertEqual(
                    retained_result["result"]["superseded_from_root_sha256"],
                    stranded.record_sha256,
                )
                ancestry = read_ancestor_action_results(
                    ledger_457,
                    IDENTITY,
                    context=ActionResultReadContext(
                        IDENTITY, ledger_457.evidence_budget, max_depth=2
                    ),
                    target_run_id=456,
                )
                self.assertEqual(len(ancestry.ancestors), 1)
                self.assertIsNotNone(ancestry.ancestors[0].result_document)

    def test_end_to_end_pr_review_and_command_recover_after_stranded_unstarted_source_edit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            f = G32CommandEntryFixture(directory)
            # First complete a real partial review so dispositions/transactions exist on the PR.
            self.assertEqual(f.invoke(), 0, f.error)
            review_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(review_record.transaction)
            initial_reviews = len(f.reviews)

            # Next, trigger a command whose source comment is edited before execution entry.
            f.SOURCE_ID = 301
            f.select_command("@reviewsensei review pause", new_run=True)

            def edit_before_fence(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    current.source["body"] = "@reviewsensei review pause edited"
                    current.source["updated_at"] = "2026-10-26T00:05:00Z"

            f.before_source_read = edit_before_fence
            self.assertNotEqual(f.invoke_command(), 0)
            f.before_source_read = None

            stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(stranded.api_run_accounting["action"]["phase"], "pending")
            self.assertFalse(stranded.api_run_accounting["action"]["execution_started"])
            self.assertFalse(stranded.operator_paused)

            # Without recovery, a subsequent command run refuses because the previous run is pending.
            stranded_run_id = f.run_id
            edited_source_301 = copy.deepcopy(f.source)
            f.SOURCE_ID = 302
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertNotEqual(f.invoke_command(), 0)
            self.assertIn("previous caller run is not terminal", f.error)

            # Authenticate narrow unstarted cancellation on same-run attempt 2 of stranded_run_id,
            # then prove BOTH a subsequent command AND a subsequent PR review succeed cleanly!
            f.past_jobs.pop(stranded_run_id, None)
            f.run_id = stranded_run_id
            f.attempt = 2
            f.owner_job = "command"
            f.command_active = True
            f.SOURCE_ID = 301
            f.source = edited_source_301
            f.trace.clear()
            rec_dir = Path(directory) / "recover_attempt2"
            rec_dir.mkdir()
            stranded_state = ApiRunAccounting.from_document(stranded.api_run_accounting)
            caller_rec = ApiRunRequestTransport(
                directory=rec_dir,
                run=stranded_state.identity,
                plan=stranded_state.startup,
                job="command",
                attempt=2,
                opener=f.external,
            )
            f.accounted = caller_rec
            f.transports.append(caller_rec)
            http_rec = GitHubHttp(
                api_url="https://api.github.test", opener=caller_rec.open
            )
            native_rec = GitHubIssueCommentSessionLedger(
                http_rec, token="synthetic-review_session", app_slug="reviewsensei[bot]"
            )
            native_rec.load(IDENTITY)
            ev_rec = ApiRunActionsEvidence(
                http_rec,
                token="synthetic-review_actions",
                command_run_id=str(stranded_run_id),
                job="command",
            )
            from review_sensei_github.api_run_plan import owner_allocations

            alloc = owner_allocations(
                stranded_state.configuration.limit, stranded_state.startup.liability
            )

            def claim_attempt2(rec):
                ev_rec.ceased_owner(IDENTITY, stranded_state.owner)
                prev = ApiRunAccounting.from_document(rec.api_run_accounting)
                claimed = (
                    prev.claim_startup(
                        ApiExecutionOwner(2, "command", prev.owner.generation + 1)
                    )
                    .reserve("command-work-2", alloc.successor_work)
                    .reserve("command-terminal-2", alloc.successor_terminal)
                )
                return rec.evolve(
                    generation=rec.generation + 1,
                    api_run_accounting=claimed.to_document(),
                )

            caller_rec.reserve_and_activate(
                native_rec, IDENTITY, claim_attempt2, block_id="command-work-2"
            )
            caller_rec.bind_terminal_reservation("command-terminal-2")
            ledger_rec = ApiRunSessionLedger(native_rec, caller_rec, IDENTITY)
            active_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            witness = authenticate_unstarted_cancellation(
                caller=caller_rec,
                ledger=ledger_rec,
                identity=IDENTITY,
                record=active_record,
                actions_evidence=ev_rec,
                mode="same_run",
            )
            settled_root = caller_rec.cancel_unstarted_action(
                ledger_rec, IDENTITY, witness
            )
            self.assertEqual(
                settled_root.transaction.transaction_id,
                review_record.transaction.transaction_id,
            )

            # Now run a subsequent maintainer command on a fresh run_id -> succeeds (exit 0)!
            f.attempt = 1
            f.past_jobs[stranded_run_id] = "command"
            f.past_run_attempts[stranded_run_id] = 2
            f.run_id = stranded_run_id + 1
            f.SOURCE_ID = 303
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)

            # And run a full PR review on a new head SHA -> succeeds end-to-end (exit 0)!
            f.command_active = False
            f.past_jobs[f.run_id] = "command"
            f.run_id += 1
            f.owner_job = "hosted"
            f.event_name = "pull_request"
            f.requested_head = f.live_head = "9" * 40
            f.trace.clear()
            f.transports.clear()
            f.worker_observations.clear()
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertGreater(len(f.reviews), initial_reviews)
            self.assertEqual(controlled_restart.registrations(), [])

    def test_started_unknown_tampered_and_foreign_counterexamples_refuse_fail_closed(
        self,
    ) -> None:
        # 1. Started action refuses both authentication and schema/model validation.
        with tempfile.TemporaryDirectory() as directory:
            f, _native, ledger, evidence, _cfg, intent = self._prepare(directory)
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            record_before = parse_session_comment(f.comment["body"], identity=IDENTITY)
            witness_before = authenticate_unstarted_cancellation(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=record_before,
                actions_evidence=evidence,
            )
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            started_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            with self.assertRaisesRegex(ReviewInputError, "started or unresolved work"):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=started_record,
                    actions_evidence=evidence,
                )
            with self.assertRaisesRegex(ReviewInputError, "started or unresolved work"):
                f.accounted.cancel_unstarted_action(ledger, IDENTITY, witness_before)
            # Clear in-memory boundary on a fresh successor to hit pure record guard.
            native_2 = f.recover_lifecycle()
            succ = ApiRunSessionLedger(native_2, f.accounted, IDENTITY)
            ev_2 = ApiRunActionsEvidence(
                succ.http, token="synthetic-actions", command_run_id="456", job="hosted"
            )
            succ_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            with self.assertRaisesRegex(
                ReviewInputError, "started action cannot be canceled as unstarted"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=succ,
                    identity=IDENTITY,
                    record=succ_record,
                    actions_evidence=ev_2,
                )
            with self.assertRaisesRegex(
                ReviewInputError, "started action cannot be canceled as unstarted"
            ):
                replace(
                    f.accounted.accounting,
                    action={
                        **f.accounted.accounting.action,
                        "phase": "terminal",
                        "outcome": {
                            "kind": "canceled_unstarted",
                            "reason": "source_deleted",
                        },
                    },
                )
            self.assertIsNotNone(boundary)

        # 2. Accepted result proof present refuses cancellation.
        with tempfile.TemporaryDirectory() as directory:
            f, _native, ledger, evidence, _cfg, intent = self._prepare(directory)
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            f.accounted.settle_action_effect(
                ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "explicit_noop", "reason": "status"},
                result_document={"applied": False},
            )
            cur = parse_session_comment(f.comment["body"], identity=IDENTITY)
            with self.assertRaisesRegex(ReviewInputError, "started or unresolved work"):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    actions_evidence=evidence,
                )

        # 3. Unknown / uncertain dispatch refuses cancellation both on transport & record.
        with tempfile.TemporaryDirectory() as directory:
            f, _native, ledger, evidence, _cfg, _intent = self._prepare(directory)
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            f.fault = "unknown-absent"
            with self.assertRaises(Exception):
                f.accounted.open(
                    Request(
                        "https://api.github.test/repos/owner/repo/issues/comments/1000000",
                        data=json.dumps({"body": f.comment["body"]}).encode(),
                        method="PATCH",
                    ),
                    timeout=5,
                )
            cur = parse_session_comment(f.comment["body"], identity=IDENTITY)
            with self.assertRaisesRegex(ReviewInputError, "started or unresolved work"):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    actions_evidence=evidence,
                )
            # Persist pending request into record to verify pure record uncertain>0 guard.
            uncertain_doc = f.accounted.accounting.to_document()
            uncertain_rec = cur.evolve(api_run_accounting=uncertain_doc)
            f2 = G32LifecycleFixture(str(Path(directory) / "clean"), limit=1024)
            n2, c2, ev2, own2 = f2.prelude()
            f2.accounted.initialize_and_activate(n2, IDENTITY, c2, own2)
            l2 = ApiRunSessionLedger(n2, f2.accounted, IDENTITY)
            with self.assertRaisesRegex(ReviewInputError, "uncertain caller requests"):
                authenticate_unstarted_cancellation(
                    caller=f2.accounted,
                    ledger=l2,
                    identity=IDENTITY,
                    record=uncertain_rec,
                    actions_evidence=ev2,
                )

        # 4. Altered intent, missing cessation proof, foreign owner, forged witness,
        # HTTP 500 / malformed source payload, active reservation, canonical negatives.
        with tempfile.TemporaryDirectory() as directory:
            f, _native, ledger, evidence, _cfg, intent = self._prepare(directory)
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            cur = parse_session_comment(f.comment["body"], identity=IDENTITY)

            # Altered intent
            with self.assertRaisesRegex(
                ReviewInputError, "intent does not match stranded record"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    actions_evidence=evidence,
                    expected_intent={**intent, "head_sha": "f" * 40},
                )

            # Missing cessation/fence proof
            with self.assertRaisesRegex(
                ReviewInputError, "requires owner cessation or canonical fence proof"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                )

            # Foreign owner in actions_evidence
            foreign_ev = ApiRunActionsEvidence(
                ledger.http,
                token="synthetic-actions",
                command_run_id="999",
                job="hosted",
            )
            with self.assertRaisesRegex(
                ReviewInputError, "actions evidence owner scope conflicts"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    actions_evidence=foreign_ev,
                )

            # Unsupported mode
            with self.assertRaisesRegex(
                ReviewInputError, "unstarted cancellation mode is unsupported"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    actions_evidence=evidence,
                    mode="invalid_mode",
                )

            # HTTP 500 on live source read
            f.source_status = 500
            with self.assertRaisesRegex(
                ReviewInputError, "observation failed with HTTP 500"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    actions_evidence=evidence,
                )

            # Malformed 200 payload on live source read
            f.source_status = 200
            f.source_payload = {
                "id": SOURCE_ID,
                "updated_at": "not-a-timestamp",
                "body": "x",
            }
            with self.assertRaisesRegex(
                ReviewInputError, "live action source payload is malformed"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    actions_evidence=evidence,
                )

            # Forged (unissued) witness object
            f.source_status = 404
            valid_witness = authenticate_unstarted_cancellation(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=cur,
                actions_evidence=evidence,
            )
            forged = UnstartedCancellationWitness(
                identity=valid_witness.identity,
                target_run_id=valid_witness.target_run_id,
                target_root_sha256=valid_witness.target_root_sha256,
                intent_sha256=valid_witness.intent_sha256,
                intent=valid_witness.intent,
                reason="source_deleted",
                authority_mode="same_owner_active",
                authority_proof_sha256=valid_witness.authority_proof_sha256,
                live_source_observation=valid_witness.live_source_observation,
                producer=valid_witness.producer,
            )
            with self.assertRaisesRegex(ReviewInputError, "process-issued witness"):
                require_witness_issued(forged)
            with self.assertRaisesRegex(ReviewInputError, "process-issued witness"):
                f.accounted.cancel_unstarted_action(ledger, IDENTITY, forged)

            # Active review reservation or unfinished review transaction refuses
            reserved_rec = cur.evolve(
                reservation_id="b" * 64,
                reserved_slot="initial",
            )
            with self.assertRaisesRegex(
                ReviewInputError, "conflicts with a review reservation"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=reserved_rec,
                    actions_evidence=evidence,
                )
            unfinished_tx = replace(
                ReviewTransaction.create(
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_digest="e" * 64,
                    configuration_digest="f" * 64,
                    evidence_digest="a" * 64,
                    reservation_id="b" * 64,
                    generation=1,
                ),
                phase="publication_pending",
                result_sha256="c" * 64,
            )
            tx_rec = cur.evolve(
                transaction=unfinished_tx,
                last_committed_reservation_id="b" * 64,
            )
            with self.assertRaisesRegex(
                ReviewInputError, "conflicts with an unfinished review"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=tx_rec,
                    actions_evidence=evidence,
                )

            # Canonical fence incomplete / stale epoch / unsettled dispatch negatives
            coord, tok_1 = _make_canonical_coordinator(
                Path(directory) / "coord_neg", epoch=1
            )
            with self.assertRaisesRegex(
                ReviewInputError, "canonical writer fence proof is incomplete"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    canonical_coordinator=coord,
                    canonical_token=None,
                )
            # Advance to epoch 2 so tok_1 becomes stale -> epoch conflict refusal
            next_acc = replace(
                coord.authenticate(tok_1),  # type: ignore[arg-type]
                store_id=tok_1.base_store_id,  # type: ignore[attr-defined]
                caller_sha256="9" * 64,
            )
            tok_2, _s2 = coord.advance_epoch(
                predecessor_token=tok_1,  # type: ignore[arg-type]
                successor_access=next_acc,
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )
            with self.assertRaisesRegex(
                ReviewInputError, "canonical writer fence scope or epoch conflicts"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    canonical_coordinator=coord,
                    canonical_token=tok_1,  # type: ignore[arg-type]
                )
            # Same-owner canonical fence succeeds with active epoch token
            canonical_witness = authenticate_unstarted_cancellation(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=cur,
                canonical_coordinator=coord,
                canonical_token=tok_2,
            )
            self.assertEqual(canonical_witness.authority_mode, "canonical_epoch_fenced")
            # Expired clock on coordinator triggers AuthorityRefusal -> ReviewInputError
            coord.set_now_ms(200_000)
            with self.assertRaisesRegex(
                ReviewInputError, "canonical writer fence refused"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=cur,
                    canonical_coordinator=coord,
                    canonical_token=tok_2,
                )
            # Settle mismatch negatives on settle_unstarted_accounting / settle_unstarted_record
            with self.assertRaisesRegex(ReviewInputError, "witness binding conflicts"):
                settle_unstarted_accounting(
                    identity=IDENTITY,
                    state=f.accounted.accounting,
                    producer="github-bot:1",
                    witness=valid_witness,
                )
            with self.assertRaisesRegex(
                ReviewInputError, "cross-run unstarted cancellation record binding"
            ):
                settle_unstarted_record(
                    identity=IDENTITY,
                    record=cur,
                    producer=ledger.evidence_producer(),
                    witness=valid_witness,  # same_owner_active is not cross-run
                )

    def test_rollover_failure_preserves_fail_closed_transport_and_terminal_witness_root_binding(
        self,
    ) -> None:
        from review_sensei.api_work import FrozenApiConfiguration

        # 1. RS-4B95FB: terminal branch of cancel_unstarted_action binds witness target_root_sha256.
        with tempfile.TemporaryDirectory() as directory:
            f, _native, ledger, evidence, _cfg, _intent = self._prepare(directory)
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            r0 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            witness_r0 = authenticate_unstarted_cancellation(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=r0,
                actions_evidence=evidence,
            )
            # Advance root generation (R0 -> R0b) while keeping the same pending unstarted intent.
            r0b = ledger.replace(
                IDENTITY, lambda rec: rec.evolve(generation=rec.generation + 1)
            )
            self.assertNotEqual(r0.record_sha256, r0b.record_sha256)
            with self.assertRaisesRegex(
                ReviewInputError, "unstarted cancellation root changed"
            ):
                f.accounted.cancel_unstarted_action(ledger, IDENTITY, witness_r0)

            witness_r0b = authenticate_unstarted_cancellation(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=r0b,
                actions_evidence=evidence,
            )
            settled = f.accounted.cancel_unstarted_action(ledger, IDENTITY, witness_r0b)
            # Same witness succeeds idempotently; stale witness_r0 from R0 is rejected!
            self.assertEqual(
                f.accounted.cancel_unstarted_action(
                    ledger, IDENTITY, witness_r0b
                ).record_sha256,
                settled.record_sha256,
            )
            with self.assertRaisesRegex(
                ReviewInputError,
                "caller action terminal receipt does not match witness target root",
            ):
                f.accounted.cancel_unstarted_action(ledger, IDENTITY, witness_r0)

        # 2. RS-49E71A & RS-6CF594: failed rollover_unstarted_and_activate keeps transport
        #    unstarted (_reservation_started=False, _producer=None, evidence_budget restored).
        with tempfile.TemporaryDirectory() as directory:
            f, _native, _ledger, _ev, config, _intent = self._prepare(directory)
            f.recover_lifecycle()
            stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            f.past_runs[456] = (2, "hosted", "completed")
            f.past_runs[457] = (1, "hosted", "in_progress")
            f.run_id = 457
            f.attempt = 1
            f.trace = []

            run457_dir = Path(directory) / "run457-rollback"
            run457_dir.mkdir()
            caller_457 = ApiRunRequestTransport(
                directory=run457_dir,
                run=ApiRunIdentity(123, 457),
                plan=f.plan,
                attempt=1,
                opener=f._external_open,
            )
            f.accounted = caller_457
            http_457 = GitHubHttp(
                api_url="https://api.github.test", opener=caller_457.open
            )
            native_457 = GitHubIssueCommentSessionLedger(
                http_457, token="synthetic", app_slug="reviewsensei[bot]"
            )
            native_457.load(IDENTITY)
            self.assertIsNone(native_457.evidence_budget)

            # Invalid or underfunded configuration fails before any GitHub write.
            patches_before = sum(m == "PATCH" for _, m, _ in f.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "new caller run configuration is invalid"
            ):
                caller_457.rollover_unstarted_and_activate(
                    native_457,
                    IDENTITY,
                    "not-a-config",
                    ApiExecutionOwner(1, "hosted", 0),
                    stranded,
                    lambda _rec: None,
                )
            underfunded_cfg = FrozenApiConfiguration(BASE, config.digest, 128)
            with self.assertRaisesRegex(
                ReviewInputError, "operation allocation is unfunded"
            ):
                caller_457.rollover_unstarted_and_activate(
                    native_457,
                    IDENTITY,
                    underfunded_cfg,
                    ApiExecutionOwner(1, "hosted", 0),
                    stranded,
                    lambda _rec: None,
                )
            self.assertEqual(sum(m == "PATCH" for _, m, _ in f.trace), patches_before)
            self.assertFalse(caller_457._reservation_started)
            self.assertIsNone(caller_457._producer)
            self.assertIsNone(native_457.evidence_budget)

            # Failing authenticate callback inside settle_prepare rolls back cleanly too.
            with self.assertRaisesRegex(ReviewInputError, "simulated auth refusal"):
                caller_457.rollover_unstarted_and_activate(
                    native_457,
                    IDENTITY,
                    config,
                    ApiExecutionOwner(1, "hosted", 0),
                    stranded,
                    lambda _rec: (_ for _ in ()).throw(
                        ReviewInputError("simulated auth refusal")
                    ),
                )
            self.assertFalse(caller_457._reservation_started)
            self.assertIsNone(caller_457._producer)
            self.assertIsNone(native_457.evidence_budget)

            # Subsequent valid rollover on the clean transport succeeds!
            ev_457 = ApiRunActionsEvidence(
                http_457,
                token="synthetic-actions",
                command_run_id="457",
                job="hosted",
            )
            activated = caller_457.rollover_unstarted_and_activate(
                native_457,
                IDENTITY,
                config,
                ApiExecutionOwner(1, "hosted", 0),
                stranded,
                lambda current: authenticate_unstarted_cancellation(
                    caller=caller_457,
                    ledger=native_457,
                    identity=IDENTITY,
                    record=current,
                    actions_evidence=ev_457,
                    mode="cross_run",
                ),
            )
            self.assertEqual(activated.identity.run_id, 457)
            self.assertTrue(caller_457._reservation_started)
            self.assertIsNotNone(caller_457._producer)


class OrdinaryCallerAuthorityGateG32Tests(unittest.TestCase):
    """Qualify ordinary caller integration and strict positive non-dispatch gate in action_run.py."""

    def setUp(self) -> None:
        from unittest.mock import patch

        for target in ("socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("unexpected network"))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def _invoke_with_gate(
        fixture: G32CommandEntryFixture,
        body: str | None = None,
        *,
        unstarted_recovery: object = None,
        event_override: dict[str, object] | None = None,
        source_status: int = 200,
    ) -> int:
        import io
        import os
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from unittest.mock import patch

        from review_sensei_github import action_run, review_run
        from review_sensei_github.broker_client import BrokerClient
        from test_review_run import fixture_process_environment

        from review_sensei.bounded_evidence import EvidenceReadBudget

        if body is not None or not fixture.command_active:
            fixture.select_command(body or fixture.command_body)
        event = {
            "action": "created",
            "issue": {"number": 7, "pull_request": {}},
            "comment": copy.deepcopy(fixture.source),
        }
        if event_override is not None:
            event = event_override
        event_file = fixture.root / "command-event.json"
        event_file.write_text(json.dumps(event))
        original_bootstrap = action_run.bootstrap
        original_external = fixture.external

        def external_with_404(request, timeout):
            path = urlsplit(request.full_url).path
            method = request.get_method()
            if (
                source_status == 404
                and method == "GET"
                and path == f"/repos/owner/repo/issues/comments/{fixture.SOURCE_ID}"
            ):
                fixture._meter(request)
                fixture.source_reads += 1
                return json_response({"message": "Not Found"}, status=404)
            return original_external(request, timeout)

        def caller_factory(**kwargs):
            caller = ApiRunRequestTransport(**kwargs)
            fixture.accounted = caller
            fixture.transports.append(caller)
            return caller

        def broker_factory(**kwargs):
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token", **kwargs
            )

        def http_factory(**kwargs):
            http = GitHubHttp(api_url="https://api.github.test", **kwargs)
            fixture.http = http
            return http

        def capture_run(*args, **kwargs):
            run = original_bootstrap(*args, **kwargs)
            fixture.selected = run
            return run

        environment = {
            **fixture_process_environment(),
            "GITHUB_RUN_ID": str(fixture.run_id),
            "GITHUB_RUN_ATTEMPT": str(fixture.attempt),
            "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_EVENT_PATH": str(event_file),
            "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
            "GITHUB_ACTOR": "alice",
            "GITHUB_TRIGGERING_ACTOR": "alice",
            "GITHUB_TOKEN": "synthetic-workflow-read",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
            "REVIEWSENSEI_APP_ID": "0",
        }
        output, errors = io.StringIO(), io.StringIO()
        prev_cwd = Path.cwd()
        fixture.exception = None
        try:
            os.chdir(fixture.root)
            with ExitStack() as stack:
                for scope in (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(review_run, "physical_opener", external_with_404),
                    patch.object(
                        review_run, "ApiRunRequestTransport", side_effect=caller_factory
                    ),
                    patch.object(
                        review_run, "BrokerClient", side_effect=broker_factory
                    ),
                    patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                    patch.object(action_run, "bootstrap", side_effect=capture_run),
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("no replacement phase budget"),
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    stack.enter_context(scope)
                return action_run.run_action(
                    [
                        "--policy-commit",
                        fixture.policy,
                        "--run-directory",
                        str(fixture.root),
                        "--",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "123",
                        "--pull-request",
                        "7",
                        "--head-sha",
                        fixture.requested_head,
                        "--source-comment-id",
                        str(fixture.SOURCE_ID),
                        "--comment-body",
                        fixture.command_body,
                        "--actor",
                        "untrusted-hint",
                        "--association",
                        "NONE",
                        "--config",
                        str(fixture.root / ".reviewsensei.yml"),
                        "--github-session-ledger",
                        "--allow-write",
                    ],
                    purpose="command",
                    unstarted_recovery=unstarted_recovery,  # type: ignore[arg-type]
                )
        except Exception as err:
            fixture.exception = err
            errors.write(str(err))
            return 1
        finally:
            os.chdir(prev_cwd)
            fixture.stdout, fixture.error = output.getvalue(), errors.getvalue()

    def test_deletion_only_execution_started_false_only_and_negatives_stay_held(
        self,
    ) -> None:
        from review_sensei_github.action_run import UnstartedCallerRecoveryGate
        from review_sensei_github.unstarted_action_recovery import (
            NoBusinessDispatchEvidence,
            verify_no_business_dispatch_evidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            f = G32CommandEntryFixture(directory)
            # Complete initial review so transaction & history exist.
            self.assertEqual(f.invoke(), 0, f.error)
            base_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert base_rec is not None and base_rec.transaction is not None

            # Admit command 501, then delete comment 501 (404) before authority.fence()!
            f.SOURCE_ID = 501
            f.select_command("@reviewsensei review pause", new_run=True)
            t1_event = {
                "action": "created",
                "issue": {"number": 7, "pull_request": {}},
                "comment": copy.deepcopy(f.source),
            }

            def edit_before_first_fence(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    current.source["body"] = "edited away"
                    current.source["updated_at"] = "2026-10-26T00:09:00Z"

            f.before_source_read = edit_before_first_fence
            self.assertNotEqual(f.invoke_command(event_override=t1_event), 0)
            f.before_source_read = None

            stranded_caller = f.accounted
            stranded_run_id = f.run_id
            stranded_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert stranded_rec is not None
            stranded_state = ApiRunAccounting.from_document(
                stranded_rec.api_run_accounting
            )
            assert stranded_state.action is not None
            self.assertFalse(stranded_state.action["execution_started"])
            self.assertIsNone(stranded_state.action["result"])
            self.assertIsNone(stranded_state.action["outcome"])

            valid_dispatch_ev = NoBusinessDispatchEvidence.from_stranded_caller(
                stranded_caller,
                identity=IDENTITY,
                record=stranded_rec,
            )

            # 1. Comment deletion (404) + execution_started=false + null markers + ceased job ALONE
            #    (unstarted_recovery=None) MUST stay strictly held on same-run attempt 2!
            f.attempt = 2
            self.assertNotEqual(
                self._invoke_with_gate(
                    f,
                    event_override=t1_event,
                    source_status=404,
                    unstarted_recovery=None,
                ),
                0,
            )
            self.assertIn("hold fail-closed", f.error)

            # 2. Missing NoBusinessDispatchEvidence (gate with None) refuses fail-closed!
            gate_missing_ev = UnstartedCallerRecoveryGate(
                target_run_id=stranded_run_id,
                no_business_dispatch_evidence=None,
                mode="same_run",
                use_actions_evidence=True,
            )
            self.assertNotEqual(
                self._invoke_with_gate(
                    f,
                    event_override=t1_event,
                    source_status=404,
                    unstarted_recovery=gate_missing_ev,
                ),
                0,
            )
            self.assertIn("requires explicit NoBusinessDispatchEvidence", f.error)

            # 3. Empty observed_requests=() (null/absent telemetry) refuses fail-closed!
            gate_empty_obs = UnstartedCallerRecoveryGate(
                target_run_id=stranded_run_id,
                no_business_dispatch_evidence=replace(
                    valid_dispatch_ev, observed_requests=()
                ),
                mode="same_run",
                use_actions_evidence=True,
            )
            f.past_jobs.pop(stranded_run_id, None)
            after_a2_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert after_a2_rec is not None
            # Test directly through verify_no_business_dispatch_evidence & gate:
            assert f.selected is not None
            for bad_ev, expected_msg in (
                (
                    replace(valid_dispatch_ev, observed_requests=()),
                    "non-empty observed_requests",
                ),
                (
                    replace(valid_dispatch_ev, provider_dispatches=1),
                    "provider or accepted operation activity",
                ),
                (
                    replace(valid_dispatch_ev, accepted_operation_present=True),
                    "provider or accepted operation activity",
                ),
                (
                    replace(
                        valid_dispatch_ev,
                        observed_requests=(
                            *valid_dispatch_ev.observed_requests,
                            {
                                "phase": "ordinary",
                                "lifecycle_phase": "admission",
                                "request_id": "c-0-99",
                                "outcome": "unknown",
                                "mutation": True,
                            },
                        ),
                    ),
                    "uncertain or unconfirmed request outcome",
                ),
                (
                    replace(
                        valid_dispatch_ev,
                        observed_requests=(
                            *valid_dispatch_ev.observed_requests,
                            {
                                "phase": "ordinary",
                                "lifecycle_phase": "admission",
                                "request_id": "c-0-98",
                                "outcome": "response",
                                "mutation": True,
                                "business_mutation": True,
                            },
                        ),
                    ),
                    "business mutation dispatch",
                ),
                (
                    replace(
                        valid_dispatch_ev,
                        observed_requests=(
                            *valid_dispatch_ev.observed_requests,
                            {
                                "phase": "ordinary",
                                "lifecycle_phase": "publication",
                                "request_id": "c-0-97",
                                "outcome": "response",
                                "mutation": False,
                            },
                        ),
                    ),
                    "post-admission or malformed request phase",
                ),
                (
                    replace(
                        valid_dispatch_ev,
                        target_owner=ApiExecutionOwner(1, "hosted", 0),
                    ),
                    "owner, run, or intent binding conflicts",
                ),
                (
                    replace(valid_dispatch_ev, target_run_id=999999),
                    "owner, run, or intent binding conflicts",
                ),
            ):
                with (
                    self.subTest(expected=expected_msg),
                    self.assertRaisesRegex(ReviewInputError, expected_msg),
                ):
                    verify_no_business_dispatch_evidence(
                        caller=f.selected.caller,
                        ledger=f.selected.ledger,
                        identity=IDENTITY,
                        state=ApiRunAccounting.from_document(
                            after_a2_rec.api_run_accounting
                        ),
                        intent=stranded_state.action["intent"],
                        intent_sha256=valid_dispatch_ev.intent_sha256,
                        evidence=bad_ev,
                    )
            self.assertIsNotNone(gate_empty_obs)

            # 4. Spoofed human comment with command-receipt HTML marker does NOT block,
            #    whereas an App-owned Bot receipt/operation comment DOES block!
            spoof_marker = f"<!-- reviewsensei:command-receipt:v1 source={f.SOURCE_ID} digest={'a' * 64} -->"
            f.extra_comments[88801] = {
                "id": 88801,
                "body": "spoof " + spoof_marker,
                "user": {"id": 42, "login": "mallory", "type": "User"},
            }
            proof_digest = verify_no_business_dispatch_evidence(
                caller=f.selected.caller,
                ledger=f.selected.ledger,
                identity=IDENTITY,
                state=ApiRunAccounting.from_document(after_a2_rec.api_run_accounting),
                intent=stranded_state.action["intent"],
                intent_sha256=valid_dispatch_ev.intent_sha256,
                evidence=valid_dispatch_ev,
            )
            self.assertEqual(len(proof_digest), 64)

            bot_author_id = f.selected.ledger._evidence_author_id
            f.extra_comments[88802] = {
                "id": 88802,
                "body": "real receipt " + spoof_marker,
                "user": {
                    "id": bot_author_id,
                    "login": "reviewsensei[bot]",
                    "type": "Bot",
                },
            }
            with self.assertRaisesRegex(
                ReviewInputError,
                "live GitHub native effect or accepted operation artifact exists",
            ):
                verify_no_business_dispatch_evidence(
                    caller=f.selected.caller,
                    ledger=f.selected.ledger,
                    identity=IDENTITY,
                    state=ApiRunAccounting.from_document(
                        after_a2_rec.api_run_accounting
                    ),
                    intent=stranded_state.action["intent"],
                    intent_sha256=valid_dispatch_ev.intent_sha256,
                    evidence=valid_dispatch_ev,
                )
            f.extra_comments.clear()

            # 5. Cross-run without gate on deleted comment + completed job stays strictly held!
            f.past_jobs[stranded_run_id] = "command"
            f.past_run_attempts[stranded_run_id] = 2
            f.attempt = 1
            f.SOURCE_ID = 502
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertNotEqual(self._invoke_with_gate(f, unstarted_recovery=None), 0)
            self.assertIn("previous caller run is not terminal", f.error)

            # Verify reservation/transaction/charges/history remained intact across all negatives!
            held_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert held_rec is not None and held_rec.transaction is not None
            self.assertEqual(
                held_rec.transaction.transaction_id,
                base_rec.transaction.transaction_id,
            )
            held_state = ApiRunAccounting.from_document(held_rec.api_run_accounting)
            assert held_state.action is not None
            self.assertEqual(held_state.action["phase"], "pending")
            self.assertFalse(held_state.action["execution_started"])

    def test_started_action_refuses_ordinary_caller_recovery_gate_fail_closed(
        self,
    ) -> None:
        from review_sensei_github.action_run import UnstartedCallerRecoveryGate
        from review_sensei_github.unstarted_action_recovery import (
            NoBusinessDispatchEvidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            f = G32CommandEntryFixture(directory)
            f.SOURCE_ID = 550
            f.select_command("@reviewsensei review pause")
            f.action_fault = "business-absent"
            self.assertNotEqual(f.invoke_command(), 0)

            started_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert started_rec is not None
            started_state = ApiRunAccounting.from_document(
                started_rec.api_run_accounting
            )
            assert started_state.action is not None
            self.assertTrue(started_state.action["execution_started"])
            before_blocks = copy.deepcopy(started_state.blocks)

            # Synthetic NoBusinessDispatchEvidence cannot force G32 recovery on started work!
            intent_sha = hashlib.sha256(
                json.dumps(
                    started_state.action["intent"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            synth_ev = NoBusinessDispatchEvidence(
                identity=IDENTITY,
                target_run_id=f.run_id,
                target_owner=ApiExecutionOwner(1, "command", 0),
                intent_sha256=intent_sha,
                observed_requests=(
                    {
                        "phase": "startup",
                        "lifecycle_phase": "startup",
                        "request_id": "s-0",
                        "outcome": "response",
                        "mutation": False,
                    },
                ),
            )
            f.deleted_source_ids.add(550)
            f.past_jobs[f.run_id] = "command"
            f.past_run_attempts[f.run_id] = 1
            stranded_id = f.run_id
            f.SOURCE_ID = 551
            f.select_command("@reviewsensei review status", new_run=True)
            gate = UnstartedCallerRecoveryGate(
                target_run_id=stranded_id,
                no_business_dispatch_evidence=synth_ev,
                mode="cross_run",
                use_actions_evidence=True,
            )
            self.assertNotEqual(self._invoke_with_gate(f, unstarted_recovery=gate), 0)
            self.assertIn("started or uncertain caller work", f.error)
            after_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert after_rec is not None
            after_state = ApiRunAccounting.from_document(after_rec.api_run_accounting)
            self.assertEqual(after_state.blocks, before_blocks)
            self.assertTrue(after_state.action["execution_started"])  # type: ignore[index]

    def test_ordinary_caller_same_run_and_cross_run_recovery_settle_once_with_positive_proof(
        self,
    ) -> None:
        from review_sensei_github.action_run import UnstartedCallerRecoveryGate
        from review_sensei_github.unstarted_action_recovery import (
            NoBusinessDispatchEvidence,
        )

        # 1. Same-run (attempt == 2) recovery via action_run.run_action(..., unstarted_recovery=gate)
        with tempfile.TemporaryDirectory() as d_same:
            f = G32CommandEntryFixture(d_same)
            self.assertEqual(f.invoke(), 0, f.error)
            f.SOURCE_ID = 601
            f.select_command("@reviewsensei review pause", new_run=True)
            t1_ev = {
                "action": "created",
                "issue": {"number": 7, "pull_request": {}},
                "comment": copy.deepcopy(f.source),
            }

            def mutate_on_fence(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    current.source["body"] = "edited"
                    current.source["updated_at"] = "2026-10-26T00:05:00Z"

            f.before_source_read = mutate_on_fence
            self.assertNotEqual(f.invoke_command(event_override=t1_ev), 0)
            f.before_source_read = None

            stranded_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert stranded_rec is not None
            dispatch_ev = NoBusinessDispatchEvidence.from_stranded_caller(
                f.accounted, identity=IDENTITY, record=stranded_rec
            )
            f.attempt = 2
            same_gate = UnstartedCallerRecoveryGate(
                target_run_id=f.run_id,
                no_business_dispatch_evidence=dispatch_ev,
                mode="same_run",
                use_actions_evidence=True,
            )
            self.assertEqual(
                self._invoke_with_gate(
                    f,
                    event_override=t1_ev,
                    source_status=404,
                    unstarted_recovery=same_gate,
                ),
                0,
                f.error,
            )
            settled_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert settled_rec is not None
            settled_state = ApiRunAccounting.from_document(
                settled_rec.api_run_accounting
            )
            assert settled_state.action is not None
            self.assertEqual(settled_state.action["phase"], "terminal")
            self.assertEqual(
                settled_state.action["outcome"],
                {"kind": "canceled_unstarted", "reason": "source_deleted"},
            )
            self.assertFalse(settled_rec.operator_paused)

        # 2. Cross-run (attempt == 1 of successor command run) under BOTH Actions evidence
        #    AND canonical epoch-2 fence settles predecessor AND executes new command!
        for fence_kind in ("actions", "canonical"):
            with (
                self.subTest(fence_kind=fence_kind),
                tempfile.TemporaryDirectory() as d_cross,
            ):
                fc = G32CommandEntryFixture(d_cross)
                self.assertEqual(fc.invoke(), 0, fc.error)
                initial_tx = parse_session_comment(
                    fc.comment["body"], identity=IDENTITY
                ).transaction  # type: ignore[union-attr]
                assert initial_tx is not None

                fc.SOURCE_ID = 701
                fc.select_command("@reviewsensei review pause", new_run=True)

                def delete_on_fence(current: CommandEntryFixture) -> None:
                    if current.source_reads == 1:
                        current.source["body"] = "edited before delete"
                        current.source["updated_at"] = "2026-10-26T00:05:00Z"

                fc.before_source_read = delete_on_fence
                self.assertNotEqual(fc.invoke_command(), 0)
                fc.before_source_read = None

                stranded_id = fc.run_id
                stranded_rec = parse_session_comment(
                    fc.comment["body"], identity=IDENTITY
                )
                assert stranded_rec is not None
                dispatch_ev = NoBusinessDispatchEvidence.from_stranded_caller(
                    fc.accounted, identity=IDENTITY, record=stranded_rec
                )

                # Make old comment 701 return 404 when observed during cross-run rollover!
                orig_ext = fc.external

                def ext_701_deleted(req, timeout, *, orig=orig_ext):
                    p = urlsplit(req.full_url).path
                    if req.get_method() == "GET" and p.endswith("/issues/comments/701"):
                        fc._meter(req)
                        return json_response({"message": "Not Found"}, status=404)
                    return orig(req, timeout)

                fc.external = ext_701_deleted  # type: ignore[method-assign]
                fc.past_jobs[stranded_id] = "command"
                fc.past_run_attempts[stranded_id] = 1
                fc.SOURCE_ID = 702
                fc.select_command("@reviewsensei review pause", new_run=True)

                coord, tok2 = (
                    _make_canonical_coordinator(Path(d_cross) / "coord", epoch=2)
                    if fence_kind == "canonical"
                    else (None, None)
                )
                cross_gate = UnstartedCallerRecoveryGate(
                    target_run_id=stranded_id,
                    no_business_dispatch_evidence=dispatch_ev,
                    mode="cross_run",
                    use_actions_evidence=(fence_kind == "actions"),
                    canonical_coordinator=coord,
                    canonical_token=tok2,  # type: ignore[arg-type]
                )
                self.assertEqual(
                    self._invoke_with_gate(fc, unstarted_recovery=cross_gate),
                    0,
                    fc.error,
                )
                final_rec = parse_session_comment(fc.comment["body"], identity=IDENTITY)
                assert final_rec is not None and final_rec.transaction is not None
                self.assertTrue(final_rec.operator_paused)
                self.assertEqual(
                    final_rec.transaction.transaction_id, initial_tx.transaction_id
                )
                final_state = ApiRunAccounting.from_document(
                    final_rec.api_run_accounting
                )
                assert final_state.action is not None
                self.assertEqual(final_state.action["phase"], "terminal")
                self.assertEqual(
                    final_state.action["outcome"],
                    {"kind": "applied", "reason": None},
                )
                assert fc.selected is not None
                archived = read_previous_run(
                    fc.selected.ledger, IDENTITY, final_state.previous_record
                )
                archived_state = ApiRunAccounting.from_document(
                    archived.api_run_accounting
                )
                self.assertEqual(archived_state.identity.run_id, stranded_id)
                self.assertEqual(
                    archived_state.action["outcome"],  # type: ignore[index]
                    {"kind": "canceled_unstarted", "reason": "source_deleted"},
                )
                self.assertEqual(controlled_restart.registrations(), [])

    def test_gate_validation_rollover_guard_and_review_comment_or_pr_review_effect_fences(
        self,
    ) -> None:
        from review_sensei_github import action_run
        from review_sensei_github.action_run import (
            UnstartedCallerRecoveryGate,
            _cross_run_unstarted_rollover_guard,
            reconcile_action_source_lifecycle,
        )
        from review_sensei_github.unstarted_action_recovery import (
            NoBusinessDispatchEvidence,
            verify_no_business_dispatch_evidence,
        )

        with tempfile.TemporaryDirectory() as d:
            f = G32CommandEntryFixture(d)
            self.assertEqual(f.invoke(), 0, f.error)
            f.SOURCE_ID = 801
            f.select_command("@reviewsensei review pause", new_run=True)

            def edit_once(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    current.source["body"] = "edited"
                    current.source["updated_at"] = "2026-10-26T00:05:00Z"

            f.before_source_read = edit_once
            self.assertNotEqual(f.invoke_command(), 0)
            f.before_source_read = None
            stranded_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert stranded_rec is not None
            ev = NoBusinessDispatchEvidence.from_stranded_caller(
                f.accounted, identity=IDENTITY, record=stranded_rec
            )

            # 1. UnstartedCallerRecoveryGate.validate_preconditions negative branches
            for bad_gate, msg in (
                (
                    UnstartedCallerRecoveryGate(
                        target_run_id=0,
                        no_business_dispatch_evidence=ev,
                        use_actions_evidence=True,
                    ),
                    "target_run_id must be positive",
                ),
                (
                    UnstartedCallerRecoveryGate(
                        target_run_id=f.run_id,
                        no_business_dispatch_evidence=ev,
                        mode="invalid",
                        use_actions_evidence=True,
                    ),
                    "mode is unsupported",
                ),
                (
                    UnstartedCallerRecoveryGate(
                        target_run_id=f.run_id + 99,
                        no_business_dispatch_evidence=ev,
                        use_actions_evidence=True,
                    ),
                    "conflicts with dispatch evidence",
                ),
                (
                    UnstartedCallerRecoveryGate(
                        target_run_id=f.run_id,
                        no_business_dispatch_evidence=ev,
                        use_actions_evidence=False,
                    ),
                    "requires owner cessation or canonical fence proof",
                ),
            ):
                with self.assertRaisesRegex(ReviewInputError, msg):
                    bad_gate.validate_preconditions()

            # 2. reconcile_action_source_lifecycle negative branches
            assert f.selected is not None
            with self.assertRaisesRegex(ReviewInputError, "same_run recovery gate"):
                reconcile_action_source_lifecycle(
                    f.selected,
                    unstarted_recovery=UnstartedCallerRecoveryGate(
                        target_run_id=f.run_id,
                        no_business_dispatch_evidence=ev,
                        mode="cross_run",
                        use_actions_evidence=True,
                    ),
                )
            mismatch_ev = replace(ev, target_run_id=f.run_id + 1)
            with self.assertRaisesRegex(ReviewInputError, "conflicts with active run"):
                reconcile_action_source_lifecycle(
                    f.selected,
                    unstarted_recovery=UnstartedCallerRecoveryGate(
                        target_run_id=f.run_id + 1,
                        no_business_dispatch_evidence=mismatch_ev,
                        mode="same_run",
                        use_actions_evidence=True,
                    ),
                )
            no_tok_run = replace(f.selected, actions_token="")
            with self.assertRaisesRegex(
                ReviewInputError, "authenticated actions_token"
            ):
                reconcile_action_source_lifecycle(
                    no_tok_run,
                    unstarted_recovery=UnstartedCallerRecoveryGate(
                        target_run_id=f.run_id,
                        no_business_dispatch_evidence=ev,
                        mode="same_run",
                        use_actions_evidence=True,
                    ),
                )
            # Unchanged source on same-run gate refuses because route != g32_unstarted_cancellation
            f.source["body"] = "@reviewsensei review pause"
            f.source["updated_at"] = f.SOURCE_STAMP
            with self.assertRaisesRegex(
                ReviewInputError, "requires an unstarted mutated source action"
            ):
                reconcile_action_source_lifecycle(
                    f.selected,
                    unstarted_recovery=UnstartedCallerRecoveryGate(
                        target_run_id=f.run_id,
                        no_business_dispatch_evidence=ev,
                        mode="same_run",
                        use_actions_evidence=True,
                    ),
                )

            # 3. _cross_run_unstarted_rollover_guard unused gate, missing predecessor,
            #    run_id mismatch, and missing actions_token branches
            cross_gate = UnstartedCallerRecoveryGate(
                target_run_id=f.run_id,
                no_business_dispatch_evidence=ev,
                mode="cross_run",
                use_actions_evidence=True,
            )
            with self.assertRaisesRegex(
                ReviewInputError, "did not settle a stranded predecessor"
            ):
                with _cross_run_unstarted_rollover_guard(cross_gate):
                    pass

            with self.assertRaisesRegex(
                ReviewInputError, "requires a predecessor accounting record"
            ):
                with _cross_run_unstarted_rollover_guard(cross_gate) as rollover:
                    assert rollover is not None
                    rollover(
                        f.accounted,
                        f.selected.ledger,
                        IDENTITY,
                        None,  # type: ignore[arg-type]
                        None,  # type: ignore[arg-type]
                        None,
                        lambda: None,
                    )

            # An overlapping ordinary request must not inherit the recovery gate,
            # and same-thread nested work must retain the ordinary transport too.
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier

            original_rollover = ApiRunRequestTransport.rollover_and_activate
            rendezvous = Barrier(2)

            def ordinary_request():
                rendezvous.wait(timeout=5)
                with _cross_run_unstarted_rollover_guard(None) as ordinary:
                    return ordinary, ApiRunRequestTransport.rollover_and_activate

            with ThreadPoolExecutor(max_workers=1) as executor:
                ordinary_result = executor.submit(ordinary_request)
                with self.assertRaisesRegex(
                    ReviewInputError, "requires a predecessor accounting record"
                ):
                    with _cross_run_unstarted_rollover_guard(cross_gate) as scoped:
                        assert scoped is not None
                        rendezvous.wait(timeout=5)
                        dependency, transport_method = ordinary_result.result(timeout=5)
                        self.assertIsNone(dependency)
                        self.assertIs(transport_method, original_rollover)
                        self.assertIs(
                            ApiRunRequestTransport.rollover_and_activate,
                            original_rollover,
                        )
                        scoped(
                            f.accounted,
                            f.selected.ledger,
                            IDENTITY,
                            None,
                            None,
                            None,
                            lambda: None,
                        )
            self.assertIs(
                ApiRunRequestTransport.rollover_and_activate, original_rollover
            )

            wrong_cross_gate = UnstartedCallerRecoveryGate(
                target_run_id=f.run_id + 1,
                no_business_dispatch_evidence=mismatch_ev,
                mode="cross_run",
                use_actions_evidence=True,
            )
            with self.assertRaisesRegex(ReviewInputError, "conflicts with predecessor"):
                with _cross_run_unstarted_rollover_guard(wrong_cross_gate) as rollover:
                    assert rollover is not None
                    rollover(
                        f.accounted,
                        f.selected.ledger,
                        IDENTITY,
                        None,  # type: ignore[arg-type]
                        None,  # type: ignore[arg-type]
                        stranded_rec,
                        lambda: None,
                    )

            with self.assertRaisesRegex(
                ReviewInputError, "requires authenticated actions_token"
            ):
                with _cross_run_unstarted_rollover_guard(cross_gate) as rollover:
                    assert rollover is not None
                    rollover(
                        f.accounted,
                        object(),  # type: ignore[arg-type]
                        IDENTITY,
                        None,  # type: ignore[arg-type]
                        None,  # type: ignore[arg-type]
                        stranded_rec,
                        lambda: None,
                    )
            self.assertIsNotNone(action_run)

            # 4. PR review disposition marker for command_id blocks verify_no_business_dispatch_evidence!
            stranded_state = ApiRunAccounting.from_document(
                stranded_rec.api_run_accounting
            )
            assert stranded_state.action is not None
            bot_id = f.selected.ledger._evidence_author_id
            f.reviews.append(
                {
                    "id": 9991,
                    "commit_id": HEAD,
                    "state": "COMMENTED",
                    "body": f'<!-- reviewsensei:disposition {{"command_id":"{f.SOURCE_ID}"}} -->',
                    "user": {"id": bot_id, "login": "reviewsensei[bot]", "type": "Bot"},
                }
            )
            with self.assertRaisesRegex(
                ReviewInputError,
                "live GitHub native effect or accepted operation artifact exists",
            ):
                verify_no_business_dispatch_evidence(
                    caller=f.selected.caller,
                    ledger=f.selected.ledger,
                    identity=IDENTITY,
                    state=stranded_state,
                    intent=stranded_state.action["intent"],
                    intent_sha256=ev.intent_sha256,
                    evidence=ev,
                )
            f.reviews.pop()

        # 5. review_comment reply native effect on /pulls/7/comments blocks verify_no_business_dispatch_evidence
        #    AND canonical fence rejects identity with repository_id=None!
        with tempfile.TemporaryDirectory() as d_rc:
            helper = UnstartedActionRecoveryG32Tests()
            f_rc, native_rc, ledger_rc, _, _, intent_rc = helper._prepare(
                d_rc, purpose="reply", kind="review_comment"
            )
            rec_rc = parse_session_comment(f_rc.comment["body"], identity=IDENTITY)
            assert rec_rc is not None
            ev_rc = NoBusinessDispatchEvidence.from_stranded_caller(
                f_rc.accounted, identity=IDENTITY, record=rec_rc
            )
            state_rc = ApiRunAccounting.from_document(rec_rc.api_run_accounting)
            f_rc.inline.append(
                {
                    "id": 4444,
                    "body": f"<!-- reviewsensei:reply:v1 source={SOURCE_ID} --> reply text",
                    "user": {
                        "id": native_rc._evidence_author_id,
                        "login": "reviewsensei[bot]",
                        "type": "Bot",
                    },
                }
            )
            with self.assertRaisesRegex(
                ReviewInputError,
                "live GitHub native effect or accepted operation artifact exists",
            ):
                verify_no_business_dispatch_evidence(
                    caller=f_rc.accounted,
                    ledger=ledger_rc,
                    identity=IDENTITY,
                    state=state_rc,
                    intent=intent_rc,
                    intent_sha256=ev_rc.intent_sha256,
                    evidence=ev_rc,
                )

            from review_sensei.session import SessionIdentity

            coord, tok = _make_canonical_coordinator(Path(d_rc) / "coord", epoch=1)
            none_repo_identity = SessionIdentity("owner/repo", 7, repository_id=None)
            with self.assertRaisesRegex(
                ReviewInputError, "positive integer repository_id"
            ):
                from review_sensei_github.unstarted_action_recovery import (
                    _authenticate_authority_proof,
                )

                _authenticate_authority_proof(
                    caller=f_rc.accounted,
                    identity=none_repo_identity,
                    state=state_rc,
                    intent=intent_rc,
                    mode="same_run",
                    actions_evidence=None,
                    canonical_coordinator=coord,
                    canonical_token=tok,  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
