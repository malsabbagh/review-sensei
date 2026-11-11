"""Pre-entry `no-transaction` abort qualification and recovery tests (G29).

Covers the SiteVault `#142` predecessor boundary (`phase=no-transaction`):
- Proven pre-entry `analysis_abort` rollover (`qualified_pre_entry_abort`)
  retaining the predecessor chain and completing the successor review;
- Strict separation of "missing transaction" from "no operation happened";
- Provider-entered failure after reservation cleanup (`no_transaction_provider_entered`
  and cold `no_transaction_unconfirmed_predecessor`) failing closed with
  conservative caller + provider liability holds;
- Unknown-dispatch failure (`closure-absent` and `uncertain > 0`) failing closed;
- Failed-cleanup refusal (`cleanup-absent` / active reservation) failing closed;
- Late native review/thread effects arriving on an aborted scope failing closed;
- Refusal of unauthorized `reenroll`/reset abuse (`verify_reenroll_forbidden`);
- End-to-end bounded operator recovery via `build_no_transaction_recovery_descriptor`
  + `ControlledRestart` (`restart-run`) archiving the exact opaque `no-transaction`
  predecessor and settling the pull request review.
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import tempfile
import unittest
from dataclasses import asdict, replace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import review_sensei_github.cli as cli
import review_sensei_github.controlled_restart as restart
import review_sensei_github.review_run as review_run
from fake_github_http import json_response
from review_sensei_github.http import GitHubHttp
from review_sensei_github.pre_entry_abort_recovery import (
    NO_TRANSACTION_ATTEMPT_PACKET_SCHEMA,
    RECOVERY_CLASSIFIER_ID,
    SITEVAULT_142_CASE_ID,
    SITEVAULT_142_PULL_REQUEST,
    SITEVAULT_153_CASE_ID,
    SITEVAULT_153_PULL_REQUEST,
    NoTransactionAbortQualification,
    NoTransactionAttemptPacket,
    NoTransactionRecoveryDescriptor,
    build_no_transaction_attempt_packet,
    build_no_transaction_recovery_descriptor,
    inspect_live_no_transaction_attempt_packet,
    inspect_live_no_transaction_predecessor,
    qualify_no_transaction_abort,
    verify_reenroll_forbidden,
)
from review_sensei_github.publication import (
    FINDING_MARKER_PREFIX_V2,
    review_identity_marker,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    StartupPlan,
    StartupSlot,
    initial_accounting,
)
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
POLICY_SHA = "1" * 40
WORKFLOW_SHA = "c" * 40
IDENTITY = SessionIdentity(repository="owner/repo", pull_request=7, repository_id=123)


def _diagnostic_ledger(fixture: Any) -> GitHubIssueCommentSessionLedger:
    """Create a side-effect-free read-only ledger over `fixture` state."""
    bot = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}

    def opener(request: Any, timeout: float | None = None) -> Any:
        target = urlsplit(request.full_url)
        path, method = target.path, request.get_method()
        if path == "/user" or path.startswith("/users/"):
            return json_response(bot)
        if path.endswith("/issues/7/comments") and method == "GET":
            query = parse_qs(target.query)
            size = int(query.get("per_page", [100])[0])
            page = int(query.get("page", [1])[0])
            comment = getattr(fixture, "comment", None)
            extra = getattr(fixture, "extra_comments", {})
            rows = ([comment] if comment else []) + list(extra.values())
            return json_response(copy.deepcopy(rows[(page - 1) * size : page * size]))
        if "/issues/comments/" in path and method == "GET":
            cid = int(path.rsplit("/", 1)[1])
            comment = getattr(fixture, "comment", None)
            if comment is not None and comment.get("id") == cid:
                return json_response(copy.deepcopy(comment))
            extra = getattr(fixture, "extra_comments", {})
            if cid in extra:
                return json_response(copy.deepcopy(extra[cid]))
            return json_response({"message": "Not Found"}, 404)
        if path.endswith("/pulls/7/reviews") and method == "GET":
            return json_response(copy.deepcopy(getattr(fixture, "reviews", [])))
        if path == "/graphql" and method == "POST":
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": copy.deepcopy(
                                        getattr(fixture, "threads", [])
                                    ),
                                }
                            }
                        }
                    }
                }
            )
        if "/actions/runs/" in path and method == "GET":
            parts = path.split("/actions/runs/", 1)[1].split("/")
            queried_run = int(parts[0])
            is_completed = queried_run < int(fixture.run_id) or int(fixture.attempt) > 1
            if len(parts) == 1:
                return json_response(
                    {
                        "id": queried_run,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": "completed" if is_completed else "in_progress",
                        "run_attempt": 1,
                    }
                )
            return json_response(
                {
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 10,
                            "run_id": queried_run,
                            "run_attempt": 1,
                            "status": "completed" if is_completed else "in_progress",
                        }
                    ],
                }
            )
        raise AssertionError(f"unexpected diagnostic request: {method} {path}")

    http = GitHubHttp(
        api_url="https://api.github.test",
        opener=opener,
    )
    return GitHubIssueCommentSessionLedger(http=http, token="synthetic-diagnostic")


def _base_accounting(*, run_id: int = 1001, limit: int = 512) -> ApiRunAccounting:
    ident = ApiRunIdentity(123, run_id)
    cfg = FrozenApiConfiguration(commit=POLICY_SHA, digest="1" * 64, limit=limit)
    plan = StartupPlan(
        (
            StartupSlot("hosted", 1, ("s1", "s2")),
            StartupSlot("hosted", 2, ("s3", "s4")),
        )
    )
    owner = ApiExecutionOwner(run_attempt=1, job="hosted", generation=0)
    return initial_accounting(
        ident,
        cfg,
        owner,
        plan,
        prior_nonstartup_liability_upper_bound=5,
    ).reserve("hosted-review", 20)


def _make_record(
    *,
    accounting: ApiRunAccounting | None,
    transaction: ReviewTransaction | None = None,
    reservation_id: str | None = None,
    reserved_slot: str | None = None,
    generation: int = 2,
    convergence_history: dict[str, Any] | None = None,
) -> SessionRecord:
    eff_res_id = reservation_id
    eff_slot = reserved_slot
    last_committed: str | None = None
    if transaction is not None:
        if transaction.phase == "analysis":
            eff_res_id = eff_res_id or transaction.reservation_id
            eff_slot = eff_slot or "initial"
        else:
            last_committed = transaction.reservation_id
    elif eff_res_id is not None and eff_slot is None:
        eff_slot = "initial"
    return SessionRecord._construct(
        repository="owner/repo",
        pull_request=7,
        repository_id=123,
        completed_initial_reviews=0,
        completed_verification_rounds=0,
        failed_attempts=0,
        generation=generation,
        reservation_id=eff_res_id,
        reserved_slot=eff_slot,
        last_committed_reservation_id=last_committed,
        created_at="2026-11-10T12:00:00Z",
        updated_at="2026-11-10T12:00:00Z",
        expires_at="2026-12-10T12:00:00Z",
        operator_paused=False,
        transaction=transaction,
        convergence_history=convergence_history,
        api_run_accounting=accounting.to_document() if accounting is not None else None,
    )


class NoTransactionAbortG29Tests(unittest.TestCase):
    """Offline synthetic verification of G29 (`phase=no-transaction` recovery)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._socket_patch = patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError(
                "real network sockets are forbidden in G29 tests"
            ),
        )
        cls._socket_patch.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._socket_patch.stop()
        super().tearDownClass()

    def test_proven_pre_entry_abort_qualifies_and_rolls_over_cleanly(self) -> None:
        from review_sensei_github.api_run_history import read_previous_run
        from test_review_run import EntryFixture

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def refuse_pre_entry() -> None:
                raise ReviewInputError("synthetic local pre-entry validation refusal")

            f.before_entry = refuse_pre_entry
            self.assertEqual(f.invoke(), 1)
            aborted = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert aborted is not None
            self.assertIsNone(aborted.transaction)
            self.assertIsNone(aborted.reservation_id)
            self.assertIsNotNone(aborted.api_run_accounting["analysis_abort"])

            # Before owner run ceases in Actions, rollover must wait for cessation.
            diag = _diagnostic_ledger(f)
            active_qual, _ = inspect_live_no_transaction_predecessor(
                ledger=diag,
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            self.assertEqual(active_qual.state_code, "pre_entry_abort_owner_active")
            self.assertFalse(active_qual.qualifies_rollover)
            self.assertFalse(active_qual.missing_transaction_means_no_operation)

            # Advance to successor run so predecessor run 1001 is ceased in fake Actions.
            f.before_entry = None
            f.next_run()
            ceased_diag = _diagnostic_ledger(f)
            qual, comment_ref = inspect_live_no_transaction_predecessor(
                ledger=ceased_diag,
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            self.assertEqual(qual.state_code, "qualified_pre_entry_abort")
            self.assertTrue(qual.qualifies_rollover)
            self.assertTrue(qual.missing_transaction_means_no_operation)
            self.assertEqual(qual.primary_route, "pre_entry_abort_rollover")
            self.assertEqual(qual.conservative_provider_call_hold, 0)
            self.assertFalse(qual.reenroll_permitted)
            self.assertFalse(qual.authority)
            self.assertEqual(qual.to_dict()["authority"], False)

            # Descriptor builder refuses when ordinary rollover is already qualified.
            with self.assertRaisesRegex(ReviewInputError, "ordinary rollover"):
                build_no_transaction_recovery_descriptor(
                    identity=IDENTITY,
                    record=aborted,
                    qualification=qual,
                    old_comments=[comment_ref],
                    designated_run_id="5000",
                    base_sha=f.requested_base,
                    head_sha=f.requested_head,
                    policy_commit=f.policy,
                    workflow_sha=WORKFLOW_SHA,
                    issuance_disabled_at=1000,
                    retire_after=4600,
                )

            # Ordinary rollover succeeds, retains the aborted predecessor, and settles!
            self.assertEqual(f.invoke(), 0, f.error)
            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert settled is not None
            self.assertIsNotNone(settled.api_run_accounting["terminal_transaction"])
            retained = read_previous_run(
                f.selected.ledger,
                IDENTITY,
                f.accounted.accounting.previous_record,
            )
            self.assertEqual(retained.record_sha256, aborted.record_sha256)

    def test_provider_entered_unknown_dispatch_and_failed_cleanup_fail_closed(
        self,
    ) -> None:
        from test_review_run import EntryFixture

        # 1. Provider-entered failure (SiteVault #142 class): reservation released
        #    so `record.transaction is None`, yet `Provider.complete` already entered!
        with tempfile.TemporaryDirectory() as prov_dir:
            fp = EntryFixture(prov_dir)

            def fail_in_provider() -> None:
                raise ProviderError(
                    "synthetic provider outcome unknown", transient=True
                )

            fp.before_provider = fail_in_provider
            self.assertEqual(fp.invoke(), 1)
            rec_prov = parse_session_comment(fp.comment["body"], identity=IDENTITY)
            assert rec_prov is not None
            self.assertIsNone(rec_prov.transaction)
            self.assertIsNone(rec_prov.api_run_accounting["analysis_abort"])

            fp.before_provider = None
            fp.next_run()
            diag = _diagnostic_ledger(fp)

            # Explicit provider-entered knowledge:
            prov_qual, _ = inspect_live_no_transaction_predecessor(
                ledger=diag,
                identity=IDENTITY,
                actions_token="synthetic-actions",
                provider_entry_started=True,
            )
            self.assertEqual(prov_qual.state_code, "no_transaction_provider_entered")
            self.assertFalse(prov_qual.qualifies_rollover)
            self.assertFalse(prov_qual.missing_transaction_means_no_operation)
            self.assertEqual(prov_qual.conservative_provider_call_hold, 1)
            self.assertGreater(prov_qual.conservative_nonstartup_caller_liability, 0)

            # Cold inspection from durable ledger alone (no in-process hint):
            cold_qual, _ = inspect_live_no_transaction_predecessor(
                ledger=diag,
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                cold_qual.state_code, "no_transaction_unconfirmed_predecessor"
            )
            self.assertFalse(cold_qual.qualifies_rollover)
            self.assertFalse(cold_qual.missing_transaction_means_no_operation)
            self.assertEqual(cold_qual.conservative_provider_call_hold, 1)

            # Ordinary successor run fails closed at `phase=no-transaction`!
            self.assertEqual(fp.invoke(), 1)
            self.assertIn("phase=no-transaction", fp.error)

        # 2. Unknown-dispatch refusal (`closure-absent` + `old.uncertain > 0`):
        with tempfile.TemporaryDirectory() as unk_dir:
            fu = EntryFixture(unk_dir)

            def fail_pre() -> None:
                raise ReviewInputError("pre-entry refusal")

            fu.before_entry = fail_pre
            fu.abort_fault = "closure-absent"
            self.assertEqual(fu.invoke(), 1)
            rec_unk = parse_session_comment(fu.comment["body"], identity=IDENTITY)
            assert rec_unk is not None
            self.assertIsNone(rec_unk.transaction)
            self.assertIsNone(rec_unk.api_run_accounting["analysis_abort"])

            unk_qual = qualify_no_transaction_abort(
                rec_unk,
                owner_ceased=True,
                provider_entry_started=False,
                unreconciled_mutation_present=True,
            )
            self.assertEqual(unk_qual.state_code, "no_transaction_unknown_dispatch")
            self.assertFalse(unk_qual.qualifies_rollover)
            self.assertFalse(unk_qual.dispatch_clean)
            self.assertEqual(unk_qual.conservative_provider_call_hold, 0)

            fu.before_entry = None
            fu.abort_fault = None
            fu.next_run()
            self.assertEqual(fu.invoke(), 1)
            self.assertIn("phase=no-transaction", fu.error)

        # 3. Failed-cleanup refusal (`cleanup-absent` leaving active reservation):
        with tempfile.TemporaryDirectory() as clean_dir:
            fc = EntryFixture(clean_dir)

            def fail_cleanup() -> None:
                raise ReviewInputError("pre-entry refusal with lost cleanup")

            fc.before_entry = fail_cleanup
            fc.abort_fault = "cleanup-absent"
            self.assertEqual(fc.invoke(), 1)
            rec_clean = parse_session_comment(fc.comment["body"], identity=IDENTITY)
            assert rec_clean is not None
            self.assertIsNotNone(rec_clean.reservation_id)

            active_res = qualify_no_transaction_abort(rec_clean, owner_ceased=False)
            self.assertEqual(active_res.state_code, "no_transaction_active_reservation")
            self.assertEqual(
                active_res.primary_route, "wait_for_owner_cessation_then_recover"
            )
            fc.before_entry = None
            fc.abort_fault = None
            fc.next_run()
            ceased_res, _ = inspect_live_no_transaction_predecessor(
                ledger=_diagnostic_ledger(fc),
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            self.assertEqual(ceased_res.state_code, "no_transaction_active_reservation")
            self.assertEqual(
                ceased_res.primary_route,
                "reservation_recovery_or_controlled_restart",
            )
            self.assertEqual(fc.invoke(), 1)

    def test_late_effects_corrupt_receipts_and_reenroll_guards(self) -> None:
        from test_review_run import EntryFixture

        # Late native review & thread effects arriving after pre-entry abort closure:
        with tempfile.TemporaryDirectory() as late_dir:
            fl = EntryFixture(late_dir)

            def refuse() -> None:
                raise ReviewInputError("pre-entry abort")

            fl.before_entry = refuse
            self.assertEqual(fl.invoke(), 1)
            aborted = parse_session_comment(fl.comment["body"], identity=IDENTITY)
            assert aborted is not None
            aborted_head = fl.requested_head
            aborted_base = fl.requested_base
            fl.next_run()

            # Inject a late review scoped to the aborted head on the PR:
            marker = review_identity_marker(
                repository_id=123,
                pull_request=7,
                head_sha=aborted_head,
            )
            fl.reviews.append(
                {
                    "id": 9001,
                    "state": "COMMENTED",
                    "commit_id": aborted_head,
                    "body": f"{marker} -->\nLate review",
                }
            )
            late_review_qual, _ = inspect_live_no_transaction_predecessor(
                ledger=_diagnostic_ledger(fl),
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                late_review_qual.state_code, "pre_entry_abort_late_effects"
            )
            self.assertFalse(late_review_qual.qualifies_rollover)
            self.assertFalse(late_review_qual.late_effects_absent)
            self.assertEqual(late_review_qual.conservative_provider_call_hold, 1)

            # Now test late finding thread detection when reviews list is clean:
            fl.reviews.clear()
            fl.threads = [
                {
                    "id": "T_late",
                    "isResolved": False,
                    "comments": {
                        "nodes": [
                            {
                                "id": "C_late",
                                "body": (
                                    f"{FINDING_MARKER_PREFIX_V2} repo=123 pr=7 "
                                    f"head={aborted_head} base={aborted_base} "
                                    f"fingerprint={'d' * 64} state=new blocking=true -->\nLate finding"
                                ),
                            }
                        ]
                    },
                }
            ]
            late_thread_qual, _ = inspect_live_no_transaction_predecessor(
                ledger=_diagnostic_ledger(fl),
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                late_thread_qual.state_code, "pre_entry_abort_late_effects"
            )

        # Synthetic unit checks for corrupt receipts, uncertain dispatch with receipt,
        # non-no-transaction delegation, and `verify_reenroll_forbidden`:
        base_acc = _base_accounting()
        created_tx = ReviewTransaction.create(
            repository="owner/repo",
            pull_request=7,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            reservation_id="5" * 64,
            policy_digest="2" * 64,
            configuration_digest="1" * 64,
            evidence_digest="3" * 64,
            generation=1,
        )
        abort_tx = replace(created_tx, phase="analysis", result_sha256=None)
        valid_abort = {
            "source_root": "1" * 64,
            "cleanup_root": "2" * 64,
            "transaction": abort_tx.to_dict(),
            "entry_state": "not_started",
            "run_id": 1001,
            "owner": asdict(base_acc.owner),
        }
        acc_with_abort = replace(base_acc, analysis_abort=valid_abort)

        # Generation < 2 fails lineage check -> `pre_entry_abort_corrupt_receipt`
        bad_gen_rec = _make_record(accounting=acc_with_abort, generation=1)
        self.assertEqual(
            qualify_no_transaction_abort(bad_gen_rec).state_code,
            "pre_entry_abort_corrupt_receipt",
        )

        # Conflicting `record.transaction` alongside `analysis_abort`
        conflict_rec = _make_record(
            accounting=acc_with_abort,
            transaction=abort_tx,
            reservation_id=abort_tx.reservation_id,
            reserved_slot="initial",
            generation=2,
        )
        self.assertEqual(
            qualify_no_transaction_abort(conflict_rec).state_code,
            "pre_entry_abort_corrupt_receipt",
        )

        # Provider entry started alongside `analysis_abort`
        valid_rec = _make_record(accounting=acc_with_abort, generation=2)
        self.assertEqual(
            qualify_no_transaction_abort(
                valid_rec, provider_entry_started=True
            ).state_code,
            "pre_entry_abort_corrupt_receipt",
        )

        # Uncertain pending caller request alongside `analysis_abort`
        uncertain_acc = replace(
            base_acc.dispatch("hosted-review", PendingApiRequest("c-0-1", 999999999)),
            analysis_abort=valid_abort,
        )
        uncertain_rec = _make_record(accounting=uncertain_acc, generation=2)
        self.assertEqual(
            qualify_no_transaction_abort(uncertain_rec).state_code,
            "no_transaction_unknown_dispatch",
        )

        # Non-`no-transaction` boundary (e.g. `publication_succeeded`) delegates cleanly
        pub_tx = replace(
            created_tx,
            phase="publication_succeeded",
            result_sha256="4" * 64,
        )
        pub_rec = _make_record(accounting=base_acc, transaction=pub_tx, generation=2)
        delegated = qualify_no_transaction_abort(pub_rec)
        self.assertEqual(delegated.state_code, "not_no_transaction_boundary")
        self.assertEqual(
            delegated.primary_route, "delegate_to_recovery_state_classifier"
        )

        # Fresh reenroll/reset is strictly forbidden on any enrolled record:
        with self.assertRaisesRegex(
            ReviewInputError, "fresh reenroll/reset is forbidden"
        ):
            verify_reenroll_forbidden(valid_rec)
        unenrolled = _make_record(accounting=None, generation=0)
        self.assertIs(verify_reenroll_forbidden(unenrolled), False)
        with self.assertRaisesRegex(
            ReviewInputError, "fresh reenroll/reset is forbidden"
        ):
            verify_reenroll_forbidden(_make_record(accounting=None, generation=1))
        with self.assertRaises(ReviewInputError):
            qualify_no_transaction_abort(unenrolled)

        # Invariant guards on dataclasses:
        with self.assertRaises(ReviewInputError):
            NoTransactionAbortQualification(
                state_code="qualified_pre_entry_abort",
                qualifies_rollover=True,
                missing_transaction_means_no_operation=True,
                owner_ceased=True,
                provider_entry_proven_absent=True,
                reservation_release_confirmed=True,
                dispatch_clean=True,
                late_effects_absent=True,
                conservative_nonstartup_caller_liability=0,
                conservative_total_caller_liability=4,
                conservative_provider_call_hold=0,
                missing_predicates=(),
                primary_route="pre_entry_abort_rollover",
                fallback_routes=(),
                operator_action="ok",
                authority=True,
            )
        with self.assertRaises(ReviewInputError):
            NoTransactionAbortQualification(
                state_code="qualified_pre_entry_abort",
                qualifies_rollover=True,
                missing_transaction_means_no_operation=True,
                owner_ceased=True,
                provider_entry_proven_absent=True,
                reservation_release_confirmed=True,
                dispatch_clean=True,
                late_effects_absent=True,
                conservative_nonstartup_caller_liability=0,
                conservative_total_caller_liability=4,
                conservative_provider_call_hold=0,
                missing_predicates=(),
                primary_route="pre_entry_abort_rollover",
                fallback_routes=(),
                operator_action="ok",
                reenroll_permitted=True,
            )

    def test_wedged_no_transaction_predecessor_recovers_to_completion_via_descriptor(
        self,
    ) -> None:
        from test_review_run import EntryFixture
        from test_review_transaction import HostedAnalysisCliTests

        # Reproduce a wedged SiteVault #142-class `phase=no-transaction` PR
        # carrying prior convergence obligations:
        with tempfile.TemporaryDirectory() as wedged_dir:
            fw = EntryFixture(wedged_dir)

            def crash_in_provider() -> None:
                raise ProviderError("provider connection lost mid-call", transient=True)

            fw.before_provider = crash_in_provider
            self.assertEqual(fw.invoke(), 1)
            wedged_rec = parse_session_comment(fw.comment["body"], identity=IDENTITY)
            assert wedged_rec is not None
            self.assertIsNone(wedged_rec.transaction)
            self.assertIsNone(wedged_rec.api_run_accounting["analysis_abort"])

            # Seed prior convergence obligations onto the wedged comment to verify
            # `prior_finding_obligations_count` is preserved in the descriptor.
            prior_history = {
                "state": "recovery-required",
                "progress": [
                    {
                        "event": "completed",
                        "generation": 1,
                        "blocker_set_sha256": "f" * 64,
                        "blocker_count": 3,
                    }
                ],
                "provenance": {"ledger_digest": "e" * 64},
            }
            wedged_with_obligations = wedged_rec.evolve(
                generation=wedged_rec.generation + 1,
                convergence_history=prior_history,
            )
            fw.comment["id"] = 77
            fw.comment["body"] = render_session_comment(
                repository_id=123,
                pull_request=7,
                record=wedged_with_obligations,
            )

            # Before owner ceases, descriptor issuance refuses!
            active_qual, active_ref = inspect_live_no_transaction_predecessor(
                ledger=_diagnostic_ledger(fw),
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            with self.assertRaisesRegex(
                ReviewInputError, "before predecessor Actions owner ceases"
            ):
                build_no_transaction_recovery_descriptor(
                    identity=IDENTITY,
                    record=wedged_with_obligations,
                    qualification=active_qual,
                    old_comments=[active_ref],
                    designated_run_id="9999",
                    base_sha=fw.requested_base,
                    head_sha=fw.requested_head,
                    policy_commit=fw.policy,
                    workflow_sha="a" * 40,
                    issuance_disabled_at=1000,
                    retire_after=4600,
                )

            # Advance to next run so predecessor run ceases in Actions, verify
            # ordinary rollover still fails closed with `phase=no-transaction`:
            fw.before_provider = None
            fw.next_run()
            self.assertEqual(fw.invoke(), 1)
            self.assertIn("phase=no-transaction", fw.error)

            ceased_qual, old_ref = inspect_live_no_transaction_predecessor(
                ledger=_diagnostic_ledger(fw),
                identity=IDENTITY,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                ceased_qual.state_code, "no_transaction_unconfirmed_predecessor"
            )
            old_comment = copy.deepcopy(fw.comment)
            old_body = old_comment["body"]
            old_run_id = wedged_with_obligations.api_run_accounting["identity"][
                "run_id"
            ]

            # Build the authenticated recovery descriptor for designated restart execution:
            with tempfile.TemporaryDirectory() as restart_dir:
                rf = EntryFixture(restart_dir)
                rf.run_id = 5000
                rf.event_name = "workflow_dispatch"
                descriptor = build_no_transaction_recovery_descriptor(
                    identity=IDENTITY,
                    record=wedged_with_obligations,
                    qualification=ceased_qual,
                    old_comments=[old_ref],
                    designated_run_id=str(rf.run_id),
                    base_sha=rf.requested_base,
                    head_sha=rf.requested_head,
                    policy_commit=rf.policy,
                    workflow_sha="a" * 40,
                    issuance_disabled_at=1000,
                    retire_after=4600,
                )
                self.assertIsInstance(descriptor, NoTransactionRecoveryDescriptor)
                self.assertEqual(descriptor.classifier_id, RECOVERY_CLASSIFIER_ID)
                self.assertEqual(descriptor.prior_finding_obligations_count, 3)
                self.assertEqual(descriptor.retained_provider_call_hold, 1)
                self.assertGreater(descriptor.retained_nonstartup_caller_liability, 0)
                self.assertFalse(descriptor.reenroll_permitted)
                self.assertFalse(descriptor.authority)
                self.assertEqual(
                    descriptor.registry_document()["schema_version"],
                    "controlled-restart-registry-v1",
                )
                self.assertEqual(descriptor.to_dict()["authority"], False)

                # Execute the designated ControlledRestart registration end-to-end!
                row_data = descriptor.controlled_restart_registration
                original_external = rf.external
                original_main = cli.main

                def external(request: Any, timeout: float) -> Any:
                    path = urlsplit(request.full_url).path
                    if path.endswith(f"/actions/runs/{old_run_id}"):
                        rf.trace.append((rf.attempt, request.method, path))
                        return json_response(
                            {
                                "id": old_run_id,
                                "repository": {"id": 123, "full_name": "owner/repo"},
                                "status": "completed",
                                "run_attempt": 1,
                            }
                        )
                    if path.endswith(f"/actions/runs/{old_run_id}/attempts/1/jobs"):
                        rf.trace.append((rf.attempt, request.method, path))
                        return json_response(
                            {
                                "total_count": 1,
                                "jobs": [
                                    {
                                        "id": 10,
                                        "run_id": old_run_id,
                                        "run_attempt": 1,
                                        "status": "completed",
                                    }
                                ],
                            }
                        )
                    if path.endswith(f"/issues/comments/{old_comment['id']}"):
                        rf.trace.append((rf.attempt, request.method, path))
                        return json_response(old_comment)
                    response = original_external(request, timeout)
                    if path.endswith("/issues/7/comments") and request.method == "GET":
                        return json_response([old_comment, *json.loads(response.body)])
                    return response

                rf.external = external

                def restart_main(argv: list[str]) -> int:
                    argv = [
                        "restart-run",
                        "--restart-id",
                        descriptor.request_id,
                        *argv[1:],
                    ]
                    if "--diff" in argv:
                        idx = argv.index("--diff")
                        del argv[idx : idx + 2]
                    return original_main(argv)

                def prepare_diff(**kwargs: Any) -> None:
                    kwargs["output"].write_text(
                        HostedAnalysisCliTests.ANALYSIS_DIFF,
                        encoding="utf-8",
                        newline="\n",
                    )

                with (
                    patch.object(restart, "registrations", return_value=[row_data]),
                    patch.object(cli, "main", side_effect=restart_main),
                    patch.object(review_run, "prepare_diff", side_effect=prepare_diff),
                ):
                    self.assertEqual(rf.invoke(), 0, rf.error)
                self.assertIsNotNone(rf.selected.caller.accounting.terminal_transaction)
                archived = json.loads((rf.root / "abandoned-history.json").read_bytes())
                self.assertEqual(archived["comments"][0]["body"], old_body)
                self.assertEqual(
                    hashlib.sha256(
                        archived["comments"][0]["body"].encode("utf-8")
                    ).hexdigest(),
                    old_ref["sha256"],
                )

    def test_adversarial_validation_obligation_sources_and_live_inspection_guards(
        self,
    ) -> None:
        from types import SimpleNamespace

        from review_sensei_github.pre_entry_abort_recovery import (
            _count_prior_obligations,
            _detect_late_native_effects,
            _probe_owner_ceased,
            _validate_analysis_abort_receipt,
        )
        from review_sensei_github.publication import (
            APPROVAL_MARKER_PREFIX,
            REVIEW_MARKER_PREFIX,
        )

        # 1. Dataclass invariant rejections (Qualification & Descriptor)
        for bad_kwargs in (
            {"state_code": "bogus_state"},
            {"primary_route": "bogus_route"},
            {"fallback_routes": ("bogus_route",)},
            {
                "state_code": "no_transaction_unconfirmed_predecessor",
                "qualifies_rollover": False,
                "missing_transaction_means_no_operation": True,
                "primary_route": "controlled_serialized_restart",
            },
            {
                "missing_predicates": ("some_predicate",),
            },
            {"conservative_nonstartup_caller_liability": -1},
            {
                "conservative_nonstartup_caller_liability": 5,
                "conservative_total_caller_liability": 2,
            },
            {"conservative_provider_call_hold": -1},
        ):
            base_valid: dict[str, Any] = {
                "state_code": "qualified_pre_entry_abort",
                "qualifies_rollover": True,
                "missing_transaction_means_no_operation": True,
                "owner_ceased": True,
                "provider_entry_proven_absent": True,
                "reservation_release_confirmed": True,
                "dispatch_clean": True,
                "late_effects_absent": True,
                "conservative_nonstartup_caller_liability": 0,
                "conservative_total_caller_liability": 4,
                "conservative_provider_call_hold": 0,
                "missing_predicates": (),
                "primary_route": "pre_entry_abort_rollover",
                "fallback_routes": (),
                "operator_action": "ok",
            }
            base_valid.update(bad_kwargs)
            with self.assertRaises(ReviewInputError):
                NoTransactionAbortQualification(**base_valid)

        for desc_override in (
            {"authority": True},
            {"reenroll_permitted": True},
            {"classifier_id": "wrong-classifier"},
        ):
            desc_base: dict[str, Any] = {
                "classifier_id": RECOVERY_CLASSIFIER_ID,
                "qualification_state": "no_transaction_unconfirmed_predecessor",
                "predecessor_run_id": 1001,
                "predecessor_root_sha256": "1" * 64,
                "retained_nonstartup_caller_liability": 5,
                "retained_total_caller_liability": 9,
                "retained_provider_call_hold": 1,
                "prior_finding_obligations_count": 2,
                "controlled_restart_registration": {"request_id": "a" * 64},
                "operator_instructions": "recover",
            }
            desc_base.update(desc_override)
            with self.assertRaises(ReviewInputError):
                NoTransactionRecoveryDescriptor(**desc_base)

        # 2. Prior obligation counting across real inline baseline + manifest structures
        from pathlib import Path

        base_acc = _base_accounting()
        sv141 = json.loads(
            (
                Path(__file__).resolve().parent
                / "fixtures/regressions/review-lifecycle-sitevault141.json"
            ).read_text(encoding="utf-8")
        )
        sv141_rec = parse_session_comment(
            sv141["comment"]["body"],
            identity=SessionIdentity(
                sv141["repository"],
                sv141["pull_request"],
                repository_id=sv141["repository_id"],
            ),
        )
        assert sv141_rec is not None
        self.assertGreater(_count_prior_obligations(sv141_rec), 0)

        for stub, expected_count in (
            (
                SimpleNamespace(
                    convergence_history={"baseline": {"item_count": 4}},
                    assessment_queue=None,
                    disposition_snapshot=None,
                ),
                4,
            ),
            (
                SimpleNamespace(
                    convergence_history={
                        "baseline": {"findings": [{"id": "a"}, {"id": "b"}]}
                    },
                    assessment_queue=None,
                    disposition_snapshot=None,
                ),
                2,
            ),
            (
                SimpleNamespace(
                    convergence_history={"baseline": {"encoding": "corrupt"}},
                    assessment_queue={"state_manifest": {"item_count": 6}},
                    disposition_snapshot={"state_manifest": {"item_count": 5}},
                ),
                6,
            ),
        ):
            self.assertEqual(_count_prior_obligations(stub), expected_count)  # type: ignore[arg-type]

        # 3. Malformed `analysis_abort` receipt validation branches
        created_tx = ReviewTransaction.create(
            repository="owner/repo",
            pull_request=7,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            reservation_id="5" * 64,
            policy_digest="2" * 64,
            configuration_digest="1" * 64,
            evidence_digest="3" * 64,
            generation=1,
        )
        plain_rec = _make_record(accounting=base_acc)
        ok_none, _, failures_none = _validate_analysis_abort_receipt(
            plain_rec, base_acc
        )
        self.assertFalse(ok_none)
        self.assertEqual(failures_none, ("authenticated_analysis_abort_receipt",))

        ok_keys, _, failures_keys = _validate_analysis_abort_receipt(
            plain_rec,
            SimpleNamespace(  # type: ignore[arg-type]
                analysis_abort={"unexpected": 1},
                identity=base_acc.identity,
                owner=base_acc.owner,
            ),
        )
        self.assertFalse(ok_keys)
        self.assertEqual(failures_keys, ("valid_analysis_abort_schema",))

        for bad_tx in (
            "not-a-dict",
            {"broken": "tx"},
            replace(
                created_tx, phase="publication_succeeded", result_sha256="4" * 64
            ).to_dict(),
        ):
            malformed = {
                "source_root": "1" * 64,
                "cleanup_root": "1" * 64,  # identical roots -> rejected
                "transaction": bad_tx,
                "entry_state": "entered",
                "run_id": 9999,
                "owner": {"job": "wrong"},
            }
            ok_mal, _, failures_mal = _validate_analysis_abort_receipt(
                plain_rec,
                SimpleNamespace(  # type: ignore[arg-type]
                    analysis_abort=malformed,
                    identity=base_acc.identity,
                    owner=base_acc.owner,
                ),
            )
            self.assertFalse(ok_mal)
            self.assertIn("distinct_abort_source_and_cleanup_roots", failures_mal)
            self.assertIn("pre_entry_not_started_state", failures_mal)
            self.assertIn("matching_abort_run_id", failures_mal)
            self.assertIn("matching_abort_owner_slot", failures_mal)

        # 4. Qualification branches & type guards
        with self.assertRaises(ReviewInputError):
            verify_reenroll_forbidden("not-a-record")  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            qualify_no_transaction_abort("not-a-record")  # type: ignore[arg-type]

        active_res_uncertain = qualify_no_transaction_abort(
            _make_record(
                accounting=base_acc,
                reservation_id="5" * 64,
                reserved_slot="initial",
            ),
            owner_ceased=True,
            unreconciled_mutation_present=True,
        )
        self.assertIn(
            "zero_uncertain_caller_mutations", active_res_uncertain.missing_predicates
        )

        prov_active_owner = qualify_no_transaction_abort(
            plain_rec,
            owner_ceased=False,
            provider_entry_started=True,
        )
        self.assertEqual(
            prov_active_owner.primary_route, "wait_for_owner_cessation_then_recover"
        )
        self.assertIn(
            "predecessor_owner_cessation", prov_active_owner.missing_predicates
        )

        unk_active_owner = qualify_no_transaction_abort(
            plain_rec,
            owner_ceased=False,
            unreconciled_mutation_present=True,
        )
        self.assertEqual(
            unk_active_owner.primary_route, "wait_for_owner_cessation_then_recover"
        )
        self.assertIn(
            "predecessor_owner_cessation", unk_active_owner.missing_predicates
        )

        unconf_late = qualify_no_transaction_abort(
            plain_rec,
            owner_ceased=False,
            late_native_effects_present=True,
        )
        self.assertIn("zero_late_native_effects", unconf_late.missing_predicates)
        self.assertIn("predecessor_owner_cessation", unconf_late.missing_predicates)

        # 5. Descriptor builder guards & `extra_old_runs`
        ceased_unconf = qualify_no_transaction_abort(plain_rec, owner_ceased=True)
        comment_body = render_session_comment(
            repository_id=123, pull_request=7, record=plain_rec
        )
        raw_comment_bytes = comment_body.encode("utf-8")
        ref = {
            "id": 88,
            "bytes": len(raw_comment_bytes),
            "sha256": hashlib.sha256(raw_comment_bytes).hexdigest(),
        }
        with self.assertRaises(ReviewInputError):
            build_no_transaction_recovery_descriptor(
                identity="bad",  # type: ignore[arg-type]
                record=plain_rec,
                qualification=ceased_unconf,
                old_comments=[ref],
                designated_run_id="5000",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_commit=POLICY_SHA,
                workflow_sha=WORKFLOW_SHA,
                issuance_disabled_at=1000,
                retire_after=4600,
            )
        with self.assertRaises(ReviewInputError):
            build_no_transaction_recovery_descriptor(
                identity=IDENTITY,
                record=_make_record(accounting=None, generation=0),
                qualification=ceased_unconf,
                old_comments=[ref],
                designated_run_id="5000",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_commit=POLICY_SHA,
                workflow_sha=WORKFLOW_SHA,
                issuance_disabled_at=1000,
                retire_after=4600,
            )
        with self.assertRaises(ReviewInputError):
            build_no_transaction_recovery_descriptor(
                identity=IDENTITY,
                record=plain_rec,
                qualification=ceased_unconf,
                old_comments=[ref],
                designated_run_id="5000",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_commit=POLICY_SHA,
                workflow_sha=WORKFLOW_SHA,
                issuance_disabled_at=1000,
                retire_after=4600,
                extra_old_runs=[0],
            )
        valid_extra_desc = build_no_transaction_recovery_descriptor(
            identity=IDENTITY,
            record=plain_rec,
            qualification=ceased_unconf,
            old_comments=[ref],
            designated_run_id="5000",
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            policy_commit=POLICY_SHA,
            workflow_sha=WORKFLOW_SHA,
            issuance_disabled_at=1000,
            retire_after=4600,
            extra_old_runs=[1001, 999],
        )
        self.assertEqual(
            valid_extra_desc.controlled_restart_registration["old_runs"],
            [1001, 999],
        )

        # 6. `_probe_owner_ceased` fallback & incomplete jobs page refusal
        reader_ok = SimpleNamespace(
            _run_reader=SimpleNamespace(ceased_owner=lambda *_: None)
        )
        self.assertTrue(_probe_owner_ceased(reader_ok, IDENTITY, base_acc, None))
        reader_fail = SimpleNamespace(
            _run_reader=SimpleNamespace(
                ceased_owner=lambda *_: (_ for _ in ()).throw(
                    ReviewInputError("active")
                )
            )
        )
        self.assertFalse(_probe_owner_ceased(reader_fail, IDENTITY, base_acc, None))
        reader_bug = SimpleNamespace(
            _run_reader=SimpleNamespace(
                ceased_owner=lambda *_: (_ for _ in ()).throw(
                    RuntimeError("unexpected client bug")
                )
            )
        )
        with self.assertRaises(RuntimeError):
            _probe_owner_ceased(reader_bug, IDENTITY, base_acc, None)
        self.assertFalse(
            _probe_owner_ceased(SimpleNamespace(), IDENTITY, base_acc, None)
        )

        def incomplete_jobs_opener(request: Any, timeout: float | None = None) -> Any:
            path = urlsplit(request.full_url).path
            if path.endswith("/actions/runs/1001"):
                return json_response(
                    {
                        "id": 1001,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": "completed",
                        "run_attempt": 1,
                    }
                )
            return json_response(
                {
                    "total_count": 2,  # incomplete page (only 1 job returned) -> fail closed!
                    "jobs": [{"id": 1, "status": "completed"}],
                }
            )

        incomplete_ledger = SimpleNamespace(
            http=GitHubHttp(
                api_url="https://api.github.test", opener=incomplete_jobs_opener
            ),
            token="tok",
        )
        self.assertFalse(
            _probe_owner_ceased(incomplete_ledger, IDENTITY, base_acc, "tok")
        )

        # 7. `_detect_late_native_effects` without `abort_head` & tokenless ledger
        self.assertFalse(
            _detect_late_native_effects(
                SimpleNamespace(), IDENTITY, plain_rec, base_acc
            )
        )
        effect_mode = {"kind": "approval"}

        def late_unscoped_opener(request: Any, timeout: float | None = None) -> Any:
            path = urlsplit(request.full_url).path
            if path.endswith("/pulls/7/reviews"):
                if effect_mode["kind"] == "approval":
                    return json_response(
                        [
                            "skip-non-dict",
                            {"body": 123},
                            {
                                "body": (
                                    f"{APPROVAL_MARKER_PREFIX} repo=123 pr=7 "
                                    f"head={HEAD_SHA} -->\nApproved"
                                )
                            },
                        ]
                    )
                if effect_mode["kind"] == "review":
                    return json_response(
                        [
                            {
                                "body": (
                                    f"{REVIEW_MARKER_PREFIX} repo=123 pr=7 "
                                    f"head={HEAD_SHA} -->\nReview"
                                )
                            }
                        ]
                    )
                return json_response([])
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [
                                        "skip-non-dict",
                                        {"comments": {"nodes": []}},
                                        {"comments": {"nodes": [{"body": 99}]}},
                                        {
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "body": (
                                                            f"{FINDING_MARKER_PREFIX_V2} repo=123 pr=7 "
                                                            f"head={HEAD_SHA} base={BASE_SHA} "
                                                            f"fingerprint={'a' * 64} state=new blocking=true -->\nLate"
                                                        )
                                                    }
                                                ]
                                            }
                                        },
                                    ],
                                }
                            }
                        }
                    }
                }
            )

        late_ledger = SimpleNamespace(
            http=GitHubHttp(
                api_url="https://api.github.test", opener=late_unscoped_opener
            ),
            token="tok",
        )
        self.assertTrue(
            _detect_late_native_effects(late_ledger, IDENTITY, plain_rec, base_acc)
        )
        effect_mode["kind"] = "review"
        self.assertTrue(
            _detect_late_native_effects(late_ledger, IDENTITY, plain_rec, base_acc)
        )
        effect_mode["kind"] = "thread"
        self.assertTrue(
            _detect_late_native_effects(late_ledger, IDENTITY, plain_rec, base_acc)
        )

        # 8. `inspect_live_no_transaction_predecessor` spoofed human comment & conflict guards
        with self.assertRaises(ReviewInputError):
            inspect_live_no_transaction_predecessor(
                ledger=SimpleNamespace(), identity=IDENTITY
            )

        live_state: dict[str, Any] = {"status": 500, "comments": []}

        def live_guard_opener(request: Any, timeout: float | None = None) -> Any:
            path = urlsplit(request.full_url).path
            if path.endswith("/issues/7/comments"):
                return json_response(live_state["comments"], live_state["status"])
            if "/actions/runs/" in path:
                return json_response({"message": "not found"}, 404)
            if path.endswith("/pulls/7/reviews"):
                return json_response([])
            return json_response({"data": {}})

        guard_ledger = SimpleNamespace(
            http=GitHubHttp(
                api_url="https://api.github.test", opener=live_guard_opener
            ),
            token="tok",
        )
        with self.assertRaisesRegex(ReviewInputError, "unable to read issue comments"):
            inspect_live_no_transaction_predecessor(
                ledger=guard_ledger, identity=IDENTITY
            )

        # Human spoofed comment alone -> ignored -> no enrolled session comment
        live_state["status"] = 200
        live_state["comments"] = [
            "not-a-dict",
            {
                "id": 1,
                "body": comment_body,
                "user": {"type": "User", "login": "mallory"},
            },
        ]
        with self.assertRaisesRegex(ReviewInputError, "no enrolled session comment"):
            inspect_live_no_transaction_predecessor(
                ledger=guard_ledger, identity=IDENTITY
            )

        # Human spoof + real Bot comment -> selects real Bot comment cleanly!
        live_state["comments"].append(
            {
                "id": 88,
                "body": comment_body,
                "user": {"type": "Bot", "login": "reviewsensei[bot]"},
            }
        )
        spoof_safe_qual, spoof_safe_ref = inspect_live_no_transaction_predecessor(
            ledger=guard_ledger, identity=IDENTITY
        )
        self.assertEqual(spoof_safe_ref["id"], 88)
        self.assertFalse(spoof_safe_qual.owner_ceased)

        # Multiple conflicting Bot session comments -> fail closed!
        live_state["comments"].append(
            {
                "id": 89,
                "body": comment_body,
                "user": {"type": "Bot", "login": "reviewsensei[bot]"},
            }
        )
        with self.assertRaisesRegex(
            ReviewInputError, "multiple conflicting session comments"
        ):
            inspect_live_no_transaction_predecessor(
                ledger=guard_ledger, identity=IDENTITY
            )

        # Invalid numeric comment ID -> fail closed!
        live_state["comments"] = [
            {"id": 0, "body": comment_body, "user": {"type": "Bot"}}
        ]
        with self.assertRaisesRegex(ReviewInputError, "invalid numeric identifier"):
            inspect_live_no_transaction_predecessor(
                ledger=guard_ledger, identity=IDENTITY
            )

        # Unenrolled (no `api_run_accounting`) Bot comment -> fail closed!
        unenrolled_body = render_session_comment(
            repository_id=123,
            pull_request=7,
            record=_make_record(accounting=None, generation=0),
        )
        live_state["comments"] = [
            {"id": 90, "body": unenrolled_body, "user": {"type": "Bot"}}
        ]
        with self.assertRaisesRegex(ReviewInputError, "not API-accounted"):
            inspect_live_no_transaction_predecessor(
                ledger=guard_ledger, identity=IDENTITY
            )

    def test_multi_page_pagination_bot_identity_and_fail_closed_controls(self) -> None:
        import json
        from types import SimpleNamespace

        from review_sensei_github.pre_entry_abort_recovery import (
            _detect_late_native_effects,
            _probe_owner_ceased,
        )
        from review_sensei_github.publication import (
            FINDING_MARKER_PREFIX_V2,
            REVIEW_MARKER_PREFIX,
        )

        base_acc = _base_accounting(run_id=1001)
        abort_tx = replace(
            ReviewTransaction.create(
                repository="owner/repo",
                pull_request=7,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                reservation_id="5" * 64,
                policy_digest="2" * 64,
                configuration_digest="1" * 64,
                evidence_digest="3" * 64,
                generation=1,
            ),
            phase="analysis",
            result_sha256=None,
        )
        abort_acc = replace(
            base_acc,
            analysis_abort={
                "source_root": "1" * 64,
                "cleanup_root": "2" * 64,
                "transaction": abort_tx.to_dict(),
                "entry_state": "not_started",
                "run_id": 1001,
                "owner": asdict(base_acc.owner),
            },
        )
        abort_rec = _make_record(accounting=abort_acc, generation=2)
        session_body = render_session_comment(
            repository_id=123, pull_request=7, record=abort_rec
        )

        state: dict[str, Any] = {
            "comments_p1": [
                {"id": i, "body": "benign note", "user": {"type": "User", "login": "u"}}
                for i in range(1, 101)
            ],
            "comments_p2": [
                # Authorless comment and foreign bot comment must be ignored!
                {"id": 201, "body": session_body, "user": None},
                {
                    "id": 202,
                    "body": session_body,
                    "user": {"type": "Bot", "login": "foreign-app[bot]", "id": 777},
                },
                {
                    "id": 203,
                    "body": session_body,
                    "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                },
            ],
            "run_conclusion": "cancelled",
            "jobs_p1": [
                {"id": i, "status": "completed", "conclusion": "failure"}
                for i in range(1, 101)
            ],
            "jobs_p2": [{"id": 101, "status": "completed", "conclusion": "cancelled"}],
            "jobs_total": 101,
            "reviews_p1": [{"id": i, "body": "human review"} for i in range(1, 101)],
            "reviews_p2": [],
            "threads_p1": {
                "pageInfo": {"hasNextPage": True, "endCursor": "cur-p1"},
                "nodes": [{"id": "t1", "comments": {"nodes": [{"body": "note"}]}}],
            },
            "threads_p2": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            },
        }

        def paged_opener(request: Any, timeout: float | None = None) -> Any:
            target = urlsplit(request.full_url)
            path, query = target.path, parse_qs(target.query)
            page = int(query.get("page", ["1"])[0])
            if path.endswith("/issues/7/comments"):
                return json_response(
                    state["comments_p1"] if page == 1 else state["comments_p2"]
                )
            if path.endswith("/actions/runs/1001"):
                return json_response(
                    {
                        "id": 1001,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": "completed",
                        "conclusion": state["run_conclusion"],
                        "run_attempt": 1,
                    }
                )
            if path.endswith("/actions/runs/1001/attempts/1/jobs"):
                return json_response(
                    {
                        "total_count": state["jobs_total"],
                        "jobs": state["jobs_p1"] if page == 1 else state["jobs_p2"],
                    }
                )
            if path.endswith("/pulls/7/reviews"):
                return json_response(
                    state["reviews_p1"] if page == 1 else state["reviews_p2"]
                )
            if path == "/graphql":
                payload = json.loads(request.data.decode("utf-8"))
                after = payload["variables"]["after"]
                conn = state["threads_p1"] if after is None else state["threads_p2"]
                return json_response(
                    {"data": {"repository": {"pullRequest": {"reviewThreads": conn}}}}
                )
            raise AssertionError(f"unexpected request: {path}")

        paged_ledger = SimpleNamespace(
            http=GitHubHttp(api_url="https://api.github.test", opener=paged_opener),
            token="tok",
            app_slug="reviewsensei[bot]",
            _evidence_author_id=999,
        )

        # 1. Clean multi-page rollover qualification (comment on p2, cancelled job on p2,
        #    reviews p2 empty, GraphQL threads p2 empty):
        qual_ok, ref_ok = inspect_live_no_transaction_predecessor(
            ledger=paged_ledger, identity=IDENTITY, actions_token="tok"
        )
        self.assertEqual(ref_ok["id"], 203)
        self.assertTrue(qual_ok.qualifies_rollover)
        self.assertTrue(qual_ok.owner_ceased)
        self.assertTrue(qual_ok.late_effects_absent)

        # 2. Multi-page conflicting ReviewSensei Bot comments across p1 and p2 -> fail closed!
        state["comments_p1"][0] = {
            "id": 1,
            "body": session_body,
            "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
        }
        with self.assertRaisesRegex(
            ReviewInputError, "multiple conflicting session comments"
        ):
            inspect_live_no_transaction_predecessor(
                ledger=paged_ledger, identity=IDENTITY, actions_token="tok"
            )
        state["comments_p1"][0] = {
            "id": 1,
            "body": "benign note",
            "user": {"type": "User", "login": "u"},
        }

        # 3. Job on page 2 still `in_progress` -> owner NOT ceased!
        state["jobs_p2"] = [{"id": 101, "status": "in_progress", "conclusion": None}]
        self.assertFalse(_probe_owner_ceased(paged_ledger, IDENTITY, abort_acc, "tok"))
        state["jobs_p2"] = [
            {"id": 101, "status": "completed", "conclusion": "cancelled"}
        ]

        # 4. Late review on page 2 -> detected (`late_native_effects_present=True`)!
        state["reviews_p2"] = [
            {
                "id": 500,
                "body": f"{REVIEW_MARKER_PREFIX} repo=123 pr=7 head={HEAD_SHA} -->\nLate",
            }
        ]
        self.assertTrue(
            _detect_late_native_effects(paged_ledger, IDENTITY, abort_rec, abort_acc)
        )
        state["reviews_p2"] = []

        # 5. Late finding thread on GraphQL page 2 -> detected!
        state["threads_p2"] = {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "id": "t2",
                    "comments": {
                        "nodes": [
                            {
                                "body": (
                                    f"{FINDING_MARKER_PREFIX_V2} repo=123 pr=7 "
                                    f"head={HEAD_SHA} base={BASE_SHA} "
                                    f"fingerprint={'b' * 64} state=new blocking=true -->\nLate"
                                )
                            }
                        ]
                    },
                }
            ],
        }
        self.assertTrue(
            _detect_late_native_effects(paged_ledger, IDENTITY, abort_rec, abort_acc)
        )

        # 6. Non-advancing GraphQL cursor fails closed (`return True`)!
        state["threads_p2"] = {
            "pageInfo": {"hasNextPage": True, "endCursor": "cur-p1"},
            "nodes": [],
        }
        self.assertTrue(
            _detect_late_native_effects(paged_ledger, IDENTITY, abort_rec, abort_acc)
        )

        # 7. Clean live `inspect_live_no_transaction_attempt_packet` readback:
        state["threads_p2"] = {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [],
        }
        live_pkt = inspect_live_no_transaction_attempt_packet(
            ledger=paged_ledger,
            identity=IDENTITY,
            actions_token="tok",
            executor_sha=WORKFLOW_SHA,
            installed_core_version="0.8.20",
            installed_hosted_version="0.3.26",
        )
        self.assertIsInstance(live_pkt, NoTransactionAttemptPacket)
        self.assertFalse(live_pkt.authority)
        self.assertFalse(live_pkt.reenroll_permitted)
        self.assertEqual(live_pkt.schema_version, NO_TRANSACTION_ATTEMPT_PACKET_SCHEMA)
        self.assertTrue(live_pkt.qualification.qualifies_rollover)
        self.assertEqual(live_pkt.entry_classification, "proven_pre_entry_not_started")
        self.assertTrue(live_pkt.missing_transaction_means_no_operation)
        self.assertEqual(live_pkt.missing_proof_holds, ())
        self.assertEqual(live_pkt.session_comment_ref["id"], 203)  # type: ignore[index]

    def test_sitevault_142_and_153_attempt_packets_and_guardrails(self) -> None:
        """G29: Bind distinct offline attempt-evidence packets for SiteVault #142 and #153."""
        plan = StartupPlan(
            (
                StartupSlot("hosted", 1, ("s1", "s2")),
                StartupSlot("hosted", 2, ("s3", "s4")),
            )
        )
        cfg = FrozenApiConfiguration(commit=POLICY_SHA, digest="1" * 64, limit=512)
        owner = ApiExecutionOwner(run_attempt=1, job="hosted", generation=0)

        # 1. SiteVault #142 (`phase=no-transaction`, `record.transaction=None`,
        #    `analysis_abort=None`, released reservation, no live settlement receipt):
        sv_142_id = SessionIdentity(
            repository="org/sitevault",
            pull_request=SITEVAULT_142_PULL_REQUEST,
            repository_id=900142,
        )
        sv_142_acc = initial_accounting(
            ApiRunIdentity(900142, 4100142),
            cfg,
            owner,
            plan,
            prior_nonstartup_liability_upper_bound=6,
        ).reserve("hosted-review", 24)
        sv_142_rec = SessionRecord._construct(
            repository=sv_142_id.repository,
            pull_request=sv_142_id.pull_request,
            repository_id=sv_142_id.repository_id,
            completed_initial_reviews=1,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=3,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id="8" * 64,
            created_at="2026-11-10T12:00:00Z",
            updated_at="2026-11-10T12:05:00Z",
            expires_at="2026-12-10T12:00:00Z",
            operator_paused=False,
            transaction=None,
            convergence_history={
                "state": "recovery-required",
                "progress": [
                    {
                        "event": "completed",
                        "generation": 1,
                        "blocker_set_sha256": "f" * 64,
                        "blocker_count": 2,
                    }
                ],
                "provenance": {"ledger_digest": "e" * 64},
            },
            api_run_accounting=sv_142_acc.to_document(),
        )
        sv_142_orig_sha = sv_142_rec.record_sha256

        # Unconfirmed cold exit on SiteVault #142:
        pkt_142_cold = build_no_transaction_attempt_packet(
            sv_142_rec,
            owner_ceased=True,
            diagnostics_available=False,
            session_comment_ref={"id": 14201, "bytes": 1024, "sha256": "9" * 64},
        )
        self.assertEqual(sv_142_rec.record_sha256, sv_142_orig_sha)
        self.assertFalse(pkt_142_cold.authority)
        self.assertFalse(pkt_142_cold.reenroll_permitted)
        self.assertEqual(pkt_142_cold.case_id, SITEVAULT_142_CASE_ID)
        self.assertEqual(pkt_142_cold.pull_request, SITEVAULT_142_PULL_REQUEST)
        self.assertEqual(pkt_142_cold.entry_classification, "unproven_or_unknown")
        self.assertFalse(pkt_142_cold.missing_transaction_means_no_operation)
        self.assertFalse(pkt_142_cold.provider_entry_proven_absent)
        self.assertTrue(pkt_142_cold.reservation_release_confirmed)
        self.assertEqual(pkt_142_cold.conservative_provider_call_hold, 1)
        self.assertEqual(pkt_142_cold.prior_finding_obligations_count, 2)
        self.assertFalse(pkt_142_cold.historical_settlement_receipt_supplied)
        for hold in (
            "authenticated_analysis_abort_receipt",
            "proven_provider_non_entry",
            "missing_close_diagnostics",
            "installed_executor_provenance",
            "historical_live_settlement_receipt",
        ):
            self.assertIn(hold, pkt_142_cold.missing_proof_holds)
        with self.assertRaisesRegex(
            ReviewInputError, "fresh reenroll/reset is forbidden"
        ):
            verify_reenroll_forbidden(sv_142_rec)

        # Provider-entered variant on SiteVault #142 (missing transaction != no work happened):
        pkt_142_provider = build_no_transaction_attempt_packet(
            sv_142_rec,
            owner_ceased=True,
            provider_entry_started=True,
            diagnostics_available=True,
            executor_sha=WORKFLOW_SHA,
        )
        self.assertEqual(pkt_142_provider.entry_classification, "provider_entered")
        self.assertEqual(
            pkt_142_provider.qualification.state_code, "no_transaction_provider_entered"
        )
        self.assertFalse(pkt_142_provider.missing_transaction_means_no_operation)
        self.assertIn("zero_provider_entry", pkt_142_provider.missing_proof_holds)
        self.assertIn(
            "historical_live_settlement_receipt", pkt_142_provider.missing_proof_holds
        )

        # 2. SiteVault #153 (distinct PR & case_id; unconfirmed abort / unknown dispatch
        #    and active reservation shapes without resetting or reenrolling):
        sv_153_id = SessionIdentity(
            repository="org/sitevault",
            pull_request=SITEVAULT_153_PULL_REQUEST,
            repository_id=900142,
        )
        sv_153_acc_uncertain = (
            initial_accounting(
                ApiRunIdentity(900142, 4100153),
                cfg,
                owner,
                plan,
                prior_nonstartup_liability_upper_bound=4,
            )
            .reserve("hosted-review", 20)
            .dispatch("hosted-review", PendingApiRequest("req-abort-close", 9_999_999))
        )
        sv_153_rec_uncertain = SessionRecord._construct(
            repository=sv_153_id.repository,
            pull_request=sv_153_id.pull_request,
            repository_id=sv_153_id.repository_id,
            completed_initial_reviews=1,
            completed_verification_rounds=1,
            failed_attempts=0,
            generation=5,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id="7" * 64,
            created_at="2026-11-10T13:00:00Z",
            updated_at="2026-11-10T13:08:00Z",
            expires_at="2026-12-10T13:00:00Z",
            operator_paused=False,
            transaction=None,
            api_run_accounting=sv_153_acc_uncertain.to_document(),
        )
        pkt_153_unknown = build_no_transaction_attempt_packet(
            sv_153_rec_uncertain,
            identity=sv_153_id,
            owner_ceased=True,
            diagnostics_available=False,
        )
        self.assertNotEqual(pkt_153_unknown.case_id, pkt_142_cold.case_id)
        self.assertEqual(pkt_153_unknown.case_id, SITEVAULT_153_CASE_ID)
        self.assertEqual(pkt_153_unknown.pull_request, SITEVAULT_153_PULL_REQUEST)
        self.assertFalse(pkt_153_unknown.authority)
        self.assertFalse(pkt_153_unknown.reenroll_permitted)
        self.assertFalse(pkt_153_unknown.dispatch_clean)
        self.assertEqual(pkt_153_unknown.uncertain_requests, 1)
        self.assertEqual(
            pkt_153_unknown.qualification.state_code, "no_transaction_unknown_dispatch"
        )
        for hold in (
            "zero_uncertain_caller_mutations",
            "authenticated_analysis_abort_receipt",
            "missing_close_diagnostics",
            "historical_live_settlement_receipt",
        ):
            self.assertIn(hold, pkt_153_unknown.missing_proof_holds)
        with self.assertRaisesRegex(
            ReviewInputError, "fresh reenroll/reset is forbidden"
        ):
            verify_reenroll_forbidden(sv_153_rec_uncertain)

        # SiteVault #153 unreleased active reservation (`cleanup-absent`) shape:
        sv_153_active_res = SessionRecord._construct(
            repository=sv_153_id.repository,
            pull_request=sv_153_id.pull_request,
            repository_id=sv_153_id.repository_id,
            completed_initial_reviews=0,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=2,
            reservation_id="6" * 64,
            reserved_slot="initial",
            last_committed_reservation_id=None,
            created_at="2026-11-10T13:00:00Z",
            updated_at="2026-11-10T13:02:00Z",
            expires_at="2026-12-10T13:00:00Z",
            operator_paused=False,
            transaction=None,
            api_run_accounting=sv_153_acc_uncertain.to_document(),
        )
        pkt_153_active = build_no_transaction_attempt_packet(
            sv_153_active_res,
            owner_ceased=True,
            diagnostics_available=False,
        )
        self.assertFalse(pkt_153_active.reservation_release_confirmed)
        self.assertEqual(pkt_153_active.active_reservation_id, "6" * 64)
        self.assertIn(
            "confirmed_reservation_release", pkt_153_active.missing_proof_holds
        )
        self.assertIn(
            "historical_live_settlement_receipt", pkt_153_active.missing_proof_holds
        )
        pkt_dict = pkt_153_active.to_dict()
        self.assertIs(pkt_dict["authority"], False)
        self.assertIs(pkt_dict["reenroll_permitted"], False)
        self.assertEqual(pkt_dict["case_id"], SITEVAULT_153_CASE_ID)

        # 3. Outside-boundary delegation & validation guardrails:
        outside_tx = replace(
            ReviewTransaction.create(
                repository="owner/repo",
                pull_request=7,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                reservation_id="5" * 64,
                policy_digest="2" * 64,
                configuration_digest="1" * 64,
                evidence_digest="3" * 64,
                generation=2,
            ),
            phase="publication_succeeded",
            result_sha256="4" * 64,
        )
        outside_rec = _make_record(
            accounting=_base_accounting(), transaction=outside_tx
        )
        outside_pkt = build_no_transaction_attempt_packet(outside_rec)
        self.assertEqual(
            outside_pkt.entry_classification, "outside_no_transaction_boundary"
        )
        self.assertIn("owner/repo#7:gen-2:run-1001", outside_pkt.case_id)

        with self.assertRaisesRegex(ReviewInputError, "strictly diagnostic"):
            replace(pkt_142_cold, authority=True)
        with self.assertRaisesRegex(ReviewInputError, "strictly diagnostic"):
            replace(pkt_142_cold, reenroll_permitted=True)
        with self.assertRaisesRegex(
            ReviewInputError, "NoTransactionAbortQualification"
        ):
            replace(pkt_142_cold, qualification="bad")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "case_id"):
            replace(pkt_142_cold, case_id=" ")
        with self.assertRaisesRegex(ReviewInputError, "record_sha256"):
            replace(pkt_142_cold, record_sha256="bad")
        with self.assertRaisesRegex(ReviewInputError, "entry_classification"):
            replace(pkt_142_cold, entry_classification="bad")
        with self.assertRaisesRegex(ReviewInputError, "executor_sha"):
            replace(pkt_142_cold, executor_sha="bad")
        with self.assertRaisesRegex(ReviewInputError, "installed_core_version"):
            replace(pkt_142_cold, installed_core_version="bad version!")
        with self.assertRaisesRegex(ReviewInputError, "abort_source_root"):
            replace(pkt_142_cold, abort_source_root="bad")
        with self.assertRaisesRegex(ReviewInputError, "abort_base_sha"):
            replace(pkt_142_cold, abort_base_sha="bad")
        with self.assertRaisesRegex(
            ReviewInputError, "historical_live_settlement_receipt"
        ):
            replace(pkt_142_cold, missing_proof_holds=("proven_provider_non_entry",))
        with self.assertRaisesRegex(ReviewInputError, "session_comment_ref"):
            build_no_transaction_attempt_packet(
                sv_142_rec, session_comment_ref={"id": 0, "bytes": 1, "sha256": "a"}
            )
        with self.assertRaisesRegex(ReviewInputError, "identity must be"):
            build_no_transaction_attempt_packet(
                sv_142_rec,
                identity="bad",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ReviewInputError, "conflicts with SessionRecord"):
            build_no_transaction_attempt_packet(sv_142_rec, identity=IDENTITY)


if __name__ == "__main__":
    unittest.main()
