"""Exhaustive historical publication/finalization recovery matrix & routing (G03).

Covers every unclosed and terminal predecessor state across head/base relations,
attempt counters, executor qualification, transaction phases, coverage/rescan
flags, reservations, pauses, pre-entry aborts, uncertain terminal writes, and
authenticated terminal actions with `terminal_transaction=null` (#430). Proves
that eligible states close once with zero inference and zero duplicate native
writes, while every ineligible state yields its exact missing witness plus a
bounded recovery route that reaches terminal completion in offline fixtures.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fake_github_http import json_response
from review_sensei_github import cli, review_run
from review_sensei_github import controlled_restart as restart
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_plan import owner_allocations
from review_sensei_github.historical_completion import (
    ATTEMPT_EVIDENCE_PACKET_SCHEMA as REEXPORTED_PACKET_SCHEMA,
)
from review_sensei_github.historical_completion import (
    AttemptEvidencePacket as ReexportedAttemptEvidencePacket,
)
from review_sensei_github.historical_completion import (
    RecoveryClassification as ReexportedHistoricalClassification,
)
from review_sensei_github.historical_completion import (
    build_attempt_evidence_packet as reexported_packet_builder,
)
from review_sensei_github.historical_completion import (
    build_live_ledger_attempt_evidence_packet as reexported_live_packet_builder,
)
from review_sensei_github.historical_completion import (
    classify_live_ledger_recovery as reexported_live_classifier,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.recovery_route_advisor import (
    ATTEMPT_EVIDENCE_PACKET_SCHEMA,
    ATTEMPT_PACKET_MISSING_PROOFS,
    MISSING_WITNESSES,
    RECOVERY_STATES,
    REVIEWSENSEI_443_CASE_ID,
    REVIEWSENSEI_443_GENERATION,
    REVIEWSENSEI_443_PULL_REQUEST,
    REVIEWSENSEI_443_REPOSITORY,
    REVIEWSENSEI_443_RUN_ID,
    SUPPORTED_RECOVERY_ROUTES,
    AttemptEvidencePacket,
    RecoveryClassification,
    build_attempt_evidence_packet,
    build_live_ledger_attempt_evidence_packet,
    classify_live_ledger_recovery,
    classify_recovery_state,
)
from review_sensei_github.review_completion_recovery import (
    classify_recovery_state as reexported_state_classifier,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)
from test_action_run import (
    HEAD as ACTION_HEAD,
)
from test_action_run import (
    IDENTITY,
    CommandEntryFixture,
)
from test_review_run import EntryFixture, _CompletionFixture

from review_sensei.api_work import (
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    initial_accounting,
)
from review_sensei.bounded_evidence import (
    MAX_PARTITION_DECODED_BYTES,
    EvidenceReadBudget,
    canonical_bytes,
    encode_evidence,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionRecord

BASE_A = "a" * 40
BASE_B = "9" * 40
HEAD_A = "b" * 40
HEAD_B = "e" * 40
POLICY_SHA = "c" * 40
PRODUCER = "github-bot:999"


def _make_accounting(
    *,
    run_id: int = 101,
    attempt: int = 1,
    job: str = "hosted",
    generation: int = 0,
    limit: int = 512,
    version: str = "caller-requests-v2",
) -> ApiRunAccounting:
    identity = ApiRunIdentity(123, run_id)
    config = FrozenApiConfiguration(commit=POLICY_SHA, digest="1" * 64, limit=limit)
    owner = ApiExecutionOwner(attempt, job, generation)
    plan = review_startup_plan(job)
    allocation = owner_allocations(limit, plan.liability)
    state = (
        initial_accounting(
            identity,
            config,
            owner,
            plan,
            prior_nonstartup_liability_upper_bound=0,
        )
        .reserve("hosted-review", allocation.first_work)
        .reserve("hosted-terminal", allocation.first_terminal)
    )
    if version != state.version:
        doc = state.to_document()
        doc["version"] = version
        return ApiRunAccounting.from_document(doc)
    return state


def _make_tx(
    *,
    phase: str = "publication_succeeded",
    base_sha: str = BASE_A,
    head_sha: str = HEAD_A,
    reservation_id: str = "5" * 64,
    generation: int = 1,
) -> ReviewTransaction:
    created = ReviewTransaction.create(
        repository="owner/repo",
        pull_request=7,
        base_sha=base_sha,
        head_sha=head_sha,
        reservation_id=reservation_id,
        policy_digest="2" * 64,
        configuration_digest="1" * 64,
        evidence_digest="3" * 64,
        generation=generation,
    )
    if phase in {"analysis", "analysis_failed"}:
        return replace(created, phase=phase, result_sha256=None)
    return replace(created, phase=phase, result_sha256="4" * 64)


def _make_record(
    *,
    accounting: ApiRunAccounting | None,
    transaction: ReviewTransaction | None = None,
    operator_paused: bool = False,
    reservation_id: str | None = None,
    reserved_slot: str | None = None,
    rescan_state: dict | None = None,
    generation: int = 1,
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
        operator_paused=operator_paused,
        transaction=transaction,
        rescan_state=rescan_state,
        api_run_accounting=(
            accounting.to_document() if accounting is not None else None
        ),
    )


def _diagnostic_ledger(
    fixture: object,
) -> tuple[GitHubIssueCommentSessionLedger, list[tuple[str, str]]]:
    """Build a pure read-only ledger over `fixture` (never mutating `fixture.trace`)."""
    observed: list[tuple[str, str]] = []
    bot = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}

    def opener(request, timeout=None):
        target = urlsplit(request.full_url)
        path, method = target.path, request.get_method()
        observed.append((method, path))
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
        trace = getattr(fixture, "trace", None)
        saved_trace = list(trace) if isinstance(trace, list) else None
        try:
            ext = getattr(fixture, "external")
            return ext(request, timeout)
        finally:
            if saved_trace is not None and isinstance(trace, list):
                trace[:] = saved_trace

    http = GitHubHttp(api_url="https://api.github.test", opener=opener)
    ledger = GitHubIssueCommentSessionLedger(
        http=http,
        token="synthetic-read",
        app_slug="reviewsensei[bot]",
        evidence_budget=EvidenceReadBudget(),
    )
    ledger.authenticated_app_id = 999
    return (ledger, observed)


def _make_terminal_action_accounting(
    *,
    head_sha: str = HEAD_A,
    run_id: int = 101,
    job: str = "hosted",
    partitioned: bool = False,
    corrupt_binding: bool = False,
) -> tuple[ApiRunAccounting, list[dict]]:
    version = (
        PARTITIONED_ACTION_ACCOUNTING_VERSION if partitioned else "caller-requests-v2"
    )
    state = _make_accounting(run_id=run_id, job=job, version=version)
    intent = {
        "purpose": "command",
        "source": {
            "kind": "issue_comment",
            "id": 201,
            "updated_at": "2026-11-10T12:00:00Z",
            "body_sha256": "5" * 64,
        },
        "run_id": state.identity.run_id,
        "owner": {"run_attempt": 1, "job": job, "generation": 0},
        "head_sha": head_sha,
        "configuration_digest": state.configuration.digest,
        "operation_key": "a" * 64,
        "source_root": "b" * 64,
    }
    intent_digest = hashlib.sha256(canonical_bytes(intent)).hexdigest()
    outcome = {"kind": "applied", "reason": None}
    binding = {
        "repository": "owner/repo",
        "repository_id": 123,
        "pull_request": 7,
        "base_sha": state.configuration.commit,
        "head_sha": "f" * 40 if corrupt_binding else head_sha,
        "policy_digest": intent_digest,
        "configuration_digest": state.configuration.digest,
        "generation": 0,
        "producer": PRODUCER,
        "purpose": "evidence",
        "schema_version": "1.0",
    }
    envelope = {
        "schema_version": "caller-action-result-v1",
        "intent_sha256": intent_digest,
        "outcome": outcome,
        "result": {"status": "ok"},
    }
    extra_parts: list[dict] = []
    if partitioned:
        parts = partition_evidence(envelope, binding=binding, item_count=1)
        storage_id = "801"
        manifest = evidence_manifest(
            envelope,
            binding=binding,
            item_count=1,
            parts=parts,
            storage_ids=(storage_id,),
        )
        body = (
            "ReviewSensei immutable evidence part v1\n```json\n"
            + canonical_bytes(parts[0]).decode()
            + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
        )
        extra_parts.append(
            {
                "id": 801,
                "body": body,
                "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
        )
    else:
        carrier = {
            "schema_version": "caller-action-result-v1",
            "payload": encode_evidence(
                envelope, max_decoded_bytes=MAX_PARTITION_DECODED_BYTES
            ),
        }
        manifest = inline_manifest(carrier, binding=binding, item_count=1)
        assert manifest is not None
    doc = state.to_document()
    for block in doc["blocks"]:
        block["pending"] = []
    doc["terminal_transaction"] = None
    doc["action"] = {
        "phase": "terminal",
        "execution_started": True,
        "intent": intent,
        "outcome": outcome,
        "result": manifest,
    }
    return ApiRunAccounting.from_document(doc), extra_parts


class HistoricalRecoveryMatrixG03Tests(unittest.TestCase):
    """Comprehensive verification of G03 classification, refusals, and recovery."""

    def setUp(self) -> None:
        for target in ("socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("unexpected network"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_reexports_and_classification_guardrails(self) -> None:
        self.assertIs(ReexportedHistoricalClassification, RecoveryClassification)
        self.assertIs(reexported_live_classifier, classify_live_ledger_recovery)
        self.assertIs(reexported_state_classifier, classify_recovery_state)

        with self.assertRaisesRegex(ReviewInputError, "never grants authority"):
            RecoveryClassification(
                state_code="unenrolled_ready",
                terminal=True,
                automatic_zero_inference=True,
                head_relation="no_transaction_head",
                base_relation="no_transaction_base",
                run_attempt=None,
                executor_status="not_applicable",
                transaction_phase="no-transaction",
                action_phase=None,
                dispatch_status="known_clean",
                missing_witnesses=(),
                primary_route="ordinary_rollover",
                fallback_routes=(),
                operator_action="ready",
                authority=True,
            )

        for bad_kwargs in (
            {"state_code": "invented_state"},
            {"primary_route": "live_ledger_edit"},
            {"fallback_routes": ("ordinary_rollover",)},
            {"missing_witnesses": ("invented_witness",)},
        ):
            base_kwargs = dict(
                state_code="unenrolled_ready",
                terminal=True,
                automatic_zero_inference=True,
                head_relation="no_transaction_head",
                base_relation="no_transaction_base",
                run_attempt=None,
                executor_status="not_applicable",
                transaction_phase="no-transaction",
                action_phase=None,
                dispatch_status="known_clean",
                missing_witnesses=(),
                primary_route="ordinary_rollover",
                fallback_routes=(),
                operator_action="ready",
            )
            base_kwargs.update(bad_kwargs)
            with self.subTest(bad=bad_kwargs), self.assertRaises(ReviewInputError):
                RecoveryClassification(**base_kwargs)  # type: ignore[arg-type]

        for bad_call in (
            {"record": "not-a-record", "current_base": BASE_A, "current_head": HEAD_A},
            {
                "record": _make_record(accounting=None),
                "current_base": "bad",
                "current_head": HEAD_A,
            },
            {
                "record": _make_record(accounting=None),
                "current_base": BASE_A,
                "current_head": HEAD_A,
                "executor_status": "bogus",
            },
            {
                "record": _make_record(accounting=None),
                "current_base": BASE_A,
                "current_head": HEAD_A,
                "dispatch_status": "bogus",
            },
        ):
            with self.subTest(bad_call=bad_call), self.assertRaises(ReviewInputError):
                classify_recovery_state(**bad_call)  # type: ignore[arg-type]

    def test_exhaustive_state_table_covers_every_g03_dimension(self) -> None:
        tx = _make_tx(phase="publication_succeeded", base_sha=BASE_A, head_sha=HEAD_A)
        unclosed_acc = _make_accounting(attempt=1, job="hosted", generation=0)
        settled_doc = unclosed_acc.to_document()
        for b in settled_doc["blocks"]:
            b["pending"] = []
        settled_doc["terminal_transaction"] = tx.transaction_id
        settled_acc = ApiRunAccounting.from_document(settled_doc)

        # 1. Settled review states (normal, coverage-only partial, paused).
        settled = classify_recovery_state(
            _make_record(accounting=settled_acc, transaction=tx),
            current_base=BASE_A,
            current_head=HEAD_A,
        )
        self.assertEqual(settled.state_code, "terminal_review_settled")
        self.assertTrue(settled.terminal)
        self.assertTrue(settled.automatic_zero_inference)
        self.assertEqual(settled.primary_route, "ordinary_rollover")
        self.assertIn("operator_verify_convergence", settled.fallback_routes)
        self.assertFalse(settled.to_dict()["authority"])

        settled_partial_same = classify_recovery_state(
            _make_record(accounting=settled_acc, transaction=tx),
            current_base=BASE_A,
            current_head=HEAD_A,
            coverage_only_partial=True,
        )
        self.assertEqual(
            settled_partial_same.state_code, "terminal_review_coverage_only_partial"
        )
        self.assertEqual(
            settled_partial_same.primary_route, "operator_review_remaining_or_rollover"
        )

        settled_partial_diff = classify_recovery_state(
            _make_record(accounting=settled_acc, transaction=tx),
            current_base=BASE_A,
            current_head=HEAD_B,
            coverage_only_partial=True,
        )
        self.assertEqual(settled_partial_diff.primary_route, "ordinary_rollover")

        settled_paused = classify_recovery_state(
            _make_record(accounting=settled_acc, transaction=tx, operator_paused=True),
            current_base=BASE_A,
            current_head=HEAD_A,
        )
        self.assertEqual(settled_paused.state_code, "terminal_review_operator_paused")
        self.assertTrue(settled_paused.terminal)
        self.assertFalse(settled_paused.automatic_zero_inference)
        self.assertEqual(
            settled_paused.primary_route, "operator_resume_then_ordinary_rollover"
        )

        # 2. Terminal command action with `terminal_transaction=None` (#430):
        #    (a) null review marker (`transaction=None`), (b) prior review tx,
        #    (c) paused, (d) corrupt inline carrier.
        term_action_acc, _ = _make_terminal_action_accounting(head_sha=HEAD_A)
        null_tx_action = classify_recovery_state(
            _make_record(accounting=term_action_acc, transaction=None),
            current_base=BASE_A,
            current_head=HEAD_A,
            evidence_producer=PRODUCER,
        )
        self.assertEqual(
            null_tx_action.state_code, "terminal_action_without_review_transaction"
        )
        self.assertTrue(null_tx_action.terminal)
        self.assertTrue(null_tx_action.automatic_zero_inference)
        self.assertEqual(null_tx_action.primary_route, "ordinary_rollover")
        self.assertEqual(null_tx_action.missing_witnesses, ())

        with_tx_action = classify_recovery_state(
            _make_record(accounting=term_action_acc, transaction=tx),
            current_base=BASE_A,
            current_head=HEAD_A,
            coverage_only_partial=True,
            evidence_producer=PRODUCER,
        )
        self.assertEqual(with_tx_action.state_code, "terminal_action_settled")
        self.assertTrue(with_tx_action.terminal)
        self.assertEqual(with_tx_action.primary_route, "ordinary_rollover")

        paused_action = classify_recovery_state(
            _make_record(
                accounting=term_action_acc, transaction=None, operator_paused=True
            ),
            current_base=BASE_A,
            current_head=HEAD_A,
            evidence_producer=PRODUCER,
        )
        self.assertEqual(paused_action.state_code, "terminal_action_operator_paused")
        self.assertTrue(paused_action.terminal)
        self.assertEqual(
            paused_action.primary_route, "operator_resume_then_ordinary_rollover"
        )

        corrupt_action_acc, _ = _make_terminal_action_accounting(
            head_sha=HEAD_A, corrupt_binding=True
        )
        corrupt_action = classify_recovery_state(
            _make_record(accounting=corrupt_action_acc, transaction=tx),
            current_base=BASE_A,
            current_head=HEAD_A,
            evidence_producer=PRODUCER,
        )
        self.assertEqual(
            corrupt_action.state_code, "terminal_action_result_unauthenticated"
        )
        self.assertFalse(corrupt_action.terminal)
        self.assertEqual(
            corrupt_action.missing_witnesses, ("authenticated_terminal_action_result",)
        )
        self.assertEqual(corrupt_action.primary_route, "controlled_serialized_restart")

        # 3. Pending action states (pre-entry vs started same-head vs started head-drift).
        pending_doc = unclosed_acc.to_document()
        pending_doc["action"] = {
            "phase": "pending",
            "execution_started": False,
            "intent": copy.deepcopy(term_action_acc.to_document()["action"]["intent"]),
            "result": None,
            "outcome": None,
        }
        pending_pre = classify_recovery_state(
            _make_record(
                accounting=ApiRunAccounting.from_document(pending_doc), transaction=tx
            ),
            current_base=BASE_A,
            current_head=HEAD_A,
        )
        self.assertEqual(pending_pre.state_code, "pending_action_pre_entry")
        self.assertFalse(pending_pre.terminal)
        self.assertEqual(pending_pre.primary_route, "controlled_serialized_restart")

        pending_doc["action"]["execution_started"] = True
        pending_started_same = classify_recovery_state(
            _make_record(
                accounting=ApiRunAccounting.from_document(pending_doc), transaction=tx
            ),
            current_base=BASE_A,
            current_head=HEAD_A,
        )
        self.assertEqual(
            pending_started_same.state_code, "pending_action_started_unfinished"
        )
        self.assertEqual(
            pending_started_same.primary_route, "pending_reassessment_continuation"
        )

        pending_started_drift = classify_recovery_state(
            _make_record(
                accounting=ApiRunAccounting.from_document(pending_doc), transaction=tx
            ),
            current_base=BASE_A,
            current_head=HEAD_B,
        )
        self.assertEqual(
            pending_started_drift.primary_route, "controlled_serialized_restart"
        )

        # 4. Unclosed `publication_succeeded` negative controls across head/base/attempt/executor/flags.
        cases = [
            # Eligible different-head automatic historical completion
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_B,
                    current_head=HEAD_B,
                    executor_status="qualified_success",
                    native_effects_confirmed=True,
                ),
                "historical_publication_completion_eligible",
                True,
                "automatic_historical_completion",
                (),
            ),
            # Same-head, same-base unclosed `publication_succeeded`: `retained_analysis`
            # refuses unverified published tails on attempt 2 -> isolate via published-tail quarantine.
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_A,
                    executor_status="failed",
                    native_effects_confirmed=True,
                ),
                "same_head_unclosed_publication_succeeded",
                False,
                "quarantine_published_tail_isolation",
                (
                    "head_advance_for_historical_completion",
                    "terminal_transaction_settlement",
                ),
            ),
            # Same-head + base drift -> fail-closed
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_B,
                    current_head=HEAD_A,
                    executor_status="qualified_success",
                ),
                "same_head_base_drift_publication_succeeded",
                False,
                "quarantine_published_tail_isolation",
                (
                    "base_sha_invariance_for_same_run_replay",
                    "head_advance_for_historical_completion",
                ),
            ),
            # Same-head + attempt > 1 -> fail-closed
            (
                dict(
                    record=_make_record(
                        accounting=unclosed_acc.claim_startup(
                            ApiExecutionOwner(2, "hosted", 1)
                        ),
                        transaction=tx,
                        generation=2,
                    ),
                    current_base=BASE_A,
                    current_head=HEAD_A,
                    executor_status="failed",
                ),
                "same_head_publication_attempts_exhausted",
                False,
                "quarantine_published_tail_isolation",
                ("unused_attempt2_startup_slot", "terminal_transaction_settlement"),
            ),
            # Same-head + missing retained context / active owner -> reports all missing witnesses
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_A,
                    executor_status="active_or_unconfirmed",
                    retained_context_available=False,
                    native_effects_confirmed=False,
                ),
                "same_head_unclosed_publication_succeeded",
                False,
                "quarantine_published_tail_isolation",
                (
                    "head_advance_for_historical_completion",
                    "terminal_transaction_settlement",
                    "predecessor_owner_cessation",
                    "retained_review_result_and_context",
                    "confirmed_native_publication_effects",
                ),
            ),
            # Different-head + failed executor
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="failed",
                ),
                "unclosed_publication_failed_executor",
                False,
                "quarantine_published_tail_isolation",
                ("qualified_executor_successful_exit",),
            ),
            # Different-head + unrecognized executor
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="unrecognized",
                ),
                "unclosed_publication_unrecognized_executor",
                False,
                "quarantine_published_tail_isolation",
                ("qualified_historical_executor_contract",),
            ),
            # Different-head + active owner
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="active_or_unconfirmed",
                ),
                "unclosed_publication_owner_active",
                False,
                "quarantine_published_tail_isolation",
                ("predecessor_owner_cessation", "qualified_executor_successful_exit"),
            ),
            # Different-head + attempt > 1
            (
                dict(
                    record=_make_record(
                        accounting=unclosed_acc.claim_startup(
                            ApiExecutionOwner(2, "hosted", 1)
                        ),
                        transaction=tx,
                        generation=2,
                    ),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="qualified_success",
                ),
                "unclosed_publication_non_initial_owner",
                False,
                "quarantine_published_tail_isolation",
                ("initial_attempt1_hosted_or_local_owner",),
            ),
            # Different-head + coverage-only partial
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="qualified_success",
                    coverage_only_partial=True,
                ),
                "unclosed_publication_coverage_only_partial",
                False,
                "quarantine_published_tail_isolation",
                ("complete_non_partial_review_result",),
            ),
            # Different-head + explicit rescan
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="qualified_success",
                    explicit_rescan=True,
                ),
                "unclosed_publication_explicit_rescan",
                False,
                "quarantine_published_tail_isolation",
                ("explicit_rescan_historical_tail_receipt",),
            ),
            # Different-head + missing retained evidence
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="qualified_success",
                    retained_context_available=False,
                    native_effects_confirmed=False,
                ),
                "unclosed_publication_missing_historical_evidence",
                False,
                "quarantine_published_tail_isolation",
                (
                    "retained_review_result_and_frozen_policy",
                    "confirmed_native_publication_effects",
                ),
            ),
            # Uncertain terminal write
            (
                dict(
                    record=_make_record(accounting=unclosed_acc, transaction=tx),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="qualified_success",
                    dispatch_status="uncertain_terminal_write",
                ),
                "unclosed_publication_uncertain_terminal_write",
                False,
                "quarantine_published_tail_isolation",
                (
                    "confirmed_terminal_write_settlement",
                    "deterministic_root_write_non_entry",
                ),
            ),
            # Paused unclosed publication_succeeded
            (
                dict(
                    record=_make_record(
                        accounting=unclosed_acc, transaction=tx, operator_paused=True
                    ),
                    current_base=BASE_A,
                    current_head=HEAD_B,
                    executor_status="qualified_success",
                ),
                "unclosed_publication_operator_paused",
                False,
                "quarantine_published_tail_isolation",
                ("operator_pause_cleared", "terminal_transaction_settlement"),
            ),
        ]
        for (
            kwargs,
            expected_state,
            expected_auto,
            expected_route,
            expected_missing,
        ) in cases:
            with self.subTest(state=expected_state):
                classified = classify_recovery_state(**kwargs)
                self.assertEqual(classified.state_code, expected_state)
                self.assertFalse(classified.terminal)
                self.assertEqual(classified.automatic_zero_inference, expected_auto)
                self.assertEqual(classified.primary_route, expected_route)
                self.assertEqual(classified.missing_witnesses, expected_missing)
                self.assertIn(classified.primary_route, SUPPORTED_RECOVERY_ROUTES)
                self.assertIn(classified.state_code, RECOVERY_STATES)
                self.assertTrue(
                    set(classified.missing_witnesses).issubset(MISSING_WITNESSES)
                )

    def test_reservations_aborts_and_other_phases_classify_bounded_routes(self) -> None:
        acc = _make_accounting()
        tx_pub = _make_tx(phase="publication_succeeded")

        # Active reservation with & without attempt-2 replay
        tx_analysis = _make_tx(phase="analysis")
        res_same = classify_recovery_state(
            _make_record(accounting=acc, transaction=tx_analysis),
            current_base=BASE_A,
            current_head=HEAD_A,
            executor_status="failed",
        )
        self.assertEqual(res_same.state_code, "active_reservation_unreleased")
        self.assertEqual(res_same.primary_route, "same_run_attempt2_retained_replay")

        res_no_tx = classify_recovery_state(
            _make_record(accounting=acc, transaction=None, reservation_id="6" * 64),
            current_base=BASE_A,
            current_head=HEAD_A,
            executor_status="failed",
        )
        self.assertEqual(res_no_tx.primary_route, "controlled_serialized_restart")

        # Pre-entry abort (confirmed vs uncertain vs conflicting with tx) & plain no-transaction
        abort_doc = acc.to_document()
        abort_doc["analysis_abort"] = {
            "source_root": "5" * 64,
            "cleanup_root": "6" * 64,
            "transaction": tx_analysis.to_dict(),
            "entry_state": "not_started",
            "run_id": acc.identity.run_id,
            "owner": {"run_attempt": 1, "job": "hosted", "generation": 0},
        }
        abort_acc = ApiRunAccounting.from_document(abort_doc)
        confirmed_abort = classify_recovery_state(
            _make_record(accounting=abort_acc, transaction=None),
            current_base=BASE_A,
            current_head=HEAD_A,
        )
        self.assertEqual(confirmed_abort.state_code, "pre_entry_analysis_abort_settled")
        self.assertTrue(confirmed_abort.terminal)
        self.assertEqual(confirmed_abort.primary_route, "pre_entry_abort_rollover")

        uncertain_abort = classify_recovery_state(
            _make_record(accounting=abort_acc, transaction=None),
            current_base=BASE_A,
            current_head=HEAD_A,
            dispatch_status="uncertain_terminal_write",
        )
        self.assertEqual(uncertain_abort.state_code, "no_transaction_abort_unconfirmed")
        self.assertFalse(uncertain_abort.terminal)

        conflicting_abort = classify_recovery_state(
            _make_record(accounting=abort_acc, transaction=tx_pub),
            current_base=BASE_A,
            current_head=HEAD_A,
        )
        self.assertEqual(
            conflicting_abort.state_code, "conflicting_abort_with_transaction"
        )

        no_tx = classify_recovery_state(
            _make_record(accounting=acc, transaction=None),
            current_base=BASE_A,
            current_head=HEAD_A,
        )
        self.assertEqual(no_tx.state_code, "no_transaction_unclosed")
        self.assertEqual(no_tx.primary_route, "controlled_serialized_restart")

        # publication_failed (same-head replay vs new-head quarantine vs controlled restart)
        tx_failed = _make_tx(phase="publication_failed")
        failed_same = classify_recovery_state(
            _make_record(accounting=acc, transaction=tx_failed),
            current_base=BASE_A,
            current_head=HEAD_A,
            executor_status="failed",
        )
        self.assertEqual(
            failed_same.state_code, "publication_failed_same_head_replay_eligible"
        )
        self.assertTrue(failed_same.automatic_zero_inference)
        self.assertEqual(failed_same.primary_route, "same_run_attempt2_retained_replay")

        failed_diff = classify_recovery_state(
            _make_record(accounting=acc, transaction=tx_failed),
            current_base=BASE_A,
            current_head=HEAD_B,
            executor_status="failed",
        )
        self.assertEqual(
            failed_diff.state_code, "publication_failed_isolation_required"
        )
        self.assertEqual(
            failed_diff.primary_route, "quarantine_prepublication_isolation"
        )

        failed_uncertain = classify_recovery_state(
            _make_record(accounting=acc, transaction=tx_failed),
            current_base=BASE_A,
            current_head=HEAD_B,
            executor_status="failed",
            dispatch_status="uncertain_terminal_write",
        )
        self.assertEqual(
            failed_uncertain.primary_route, "controlled_serialized_restart"
        )
        self.assertEqual(failed_uncertain.fallback_routes, ())

        failed_paused = classify_recovery_state(
            _make_record(accounting=acc, transaction=tx_failed, operator_paused=True),
            current_base=BASE_A,
            current_head=HEAD_B,
            executor_status="failed",
        )
        self.assertEqual(failed_paused.primary_route, "controlled_serialized_restart")
        self.assertEqual(
            failed_paused.fallback_routes, ("quarantine_prepublication_isolation",)
        )

        # publication_pending, publication_suppressed, and analysis
        self.assertEqual(
            classify_recovery_state(
                _make_record(
                    accounting=acc, transaction=_make_tx(phase="publication_pending")
                ),
                current_base=BASE_A,
                current_head=HEAD_A,
                executor_status="failed",
            ).state_code,
            "publication_pending_same_head_replay_eligible",
        )
        self.assertEqual(
            classify_recovery_state(
                _make_record(
                    accounting=acc, transaction=_make_tx(phase="publication_suppressed")
                ),
                current_base=BASE_A,
                current_head=HEAD_A,
                executor_status="failed",
            ).state_code,
            "nonterminal_transaction_isolation_required",
        )
        self.assertEqual(
            classify_recovery_state(
                _make_record(
                    accounting=acc, transaction=_make_tx(phase="analysis_failed")
                ),
                current_base=BASE_A,
                current_head=HEAD_B,
                executor_status="failed",
            ).state_code,
            "nonterminal_transaction_isolation_required",
        )

        # Legacy accounting & ambiguous rescan
        self.assertEqual(
            classify_recovery_state(
                _make_record(accounting=None),
                current_base=BASE_A,
                current_head=HEAD_A,
            ).state_code,
            "legacy_or_corrupt_accounting",
        )
        ambiguous_rescan = {
            "issue_comment": 2,
            "workflow_dispatch": 0,
            "receipts": [
                {
                    "kind": "issue_comment",
                    "request_id": 1,
                    "reservation": tx_pub.reservation_id,
                    "source": tx_pub.to_dict(),
                    "review_id": 1,
                    "ordinary_reservation": tx_pub.reservation_id,
                },
                {
                    "kind": "issue_comment",
                    "request_id": 2,
                    "reservation": tx_pub.reservation_id,
                    "source": tx_pub.to_dict(),
                    "review_id": 1,
                    "ordinary_reservation": tx_pub.reservation_id,
                },
            ],
        }
        self.assertEqual(
            classify_recovery_state(
                _make_record(
                    accounting=acc, transaction=tx_pub, rescan_state=ambiguous_rescan
                ),
                current_base=BASE_A,
                current_head=HEAD_B,
                executor_status="qualified_success",
            ).state_code,
            "unclosed_publication_explicit_rescan",
        )

    def test_eligible_historical_completion_closes_once_without_inference_or_duplicate_writes(
        self,
    ) -> None:
        from test_review_run import ConsecutiveReviewRunTests

        with tempfile.TemporaryDirectory() as directory:
            f = ConsecutiveReviewRunTests.historical_case(directory)
            old_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            old_head = old_record.transaction.head_sha
            old_reviews_on_old_head = [
                r for r in f.reviews if r.get("commit_id") == old_head
            ]
            old_provider_calls = f.provider_calls

            trace_before_diag = list(f.trace)
            ledger, observed = _diagnostic_ledger(f)
            classified = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=f.requested_head,
                actions_token="synthetic-actions",
                native_effects_confirmed=True,
            )
            self.assertEqual(f.trace, trace_before_diag)
            self.assertEqual(
                classified.state_code, "historical_publication_completion_eligible"
            )
            self.assertTrue(classified.automatic_zero_inference)
            self.assertEqual(
                classified.primary_route, "automatic_historical_completion"
            )
            self.assertTrue(observed)
            self.assertTrue(all(method == "GET" for method, _ in observed))

            # Prove zero provider calls happen before historical completion settles,
            # and verify the intermediate `inherited_preprovider_unsettled` state!
            rollover_verified = []

            def before_entry() -> None:
                self.assertEqual(f.provider_calls, old_provider_calls)
                mid = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(
                    mid.api_run_accounting["previous_record"]["schema_version"],
                    "caller-run-reference-v2",
                )
                preprovider_rec = _make_record(
                    accounting=ApiRunAccounting.from_document(mid.api_run_accounting),
                    transaction=old_record.transaction,
                    generation=mid.generation,
                )
                inherited = classify_recovery_state(
                    preprovider_rec,
                    current_base=f.requested_base,
                    current_head=f.requested_head,
                )
                self.assertEqual(
                    inherited.state_code, "inherited_preprovider_unsettled"
                )
                rollover_verified.append(True)

            f.before_entry = before_entry
            self.assertEqual(f.invoke(human=True), 0, f.error)
            self.assertEqual(rollover_verified, [True])
            # Zero duplicate native reviews were written for the historical head.
            self.assertEqual(
                [r for r in f.reviews if r.get("commit_id") == old_head],
                old_reviews_on_old_head,
            )
            after = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(
                after.api_run_accounting["terminal_transaction"],
                after.transaction.transaction_id,
            )

    def test_same_head_same_base_replay_vs_base_drift_refusal_and_controlled_restart_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Build an unclosed `publication_succeeded` on run 2 attempt 1.
            f = _CompletionFixture(directory, mode="refused")
            patch_path = f.root / "review.patch"
            original_patch = patch_path.read_text(encoding="utf-8")
            patch_path.write_text(
                original_patch
                + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\n"
                "index 0000000..1111111\nBinary files /dev/null and b/image.bin differ\n",
                encoding="utf-8",
                newline="\n",
            )
            self.assertEqual(f.invoke(human=True), 0, f.error)
            patch_path.write_text(original_patch, encoding="utf-8", newline="\n")
            f.next_run()
            self.assertEqual(f.invoke(human=True), 1, f.error)

            # Resolve superseded threads externally (no ReviewSensei write yet).
            from review_sensei_github.publication import finding_publication_scope

            for thread in f.threads:
                scope = finding_publication_scope(
                    thread["comments"]["nodes"][0]["body"]
                )
                if scope is not None and scope[2] != f.requested_head:
                    thread["isResolved"] = True

            saved_comment = copy.deepcopy(f.comment)
            saved_reviews = copy.deepcopy(f.reviews)
            saved_calls = f.provider_calls
            saved_trace = list(f.trace)
            same_head = f.requested_head
            same_base = f.requested_base

            # Advance fixture attempt to 2 so attempt 1 is ceased in fake Actions.
            f.attempt = 2
            ledger, _ = _diagnostic_ledger(f)

            # Control A: Same-head + base drift refuses both attempt-2 replay and new-run rollover.
            drifted_base = "9" * 40
            drift_class = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=drifted_base,
                current_head=same_head,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                drift_class.state_code, "same_head_base_drift_publication_succeeded"
            )
            self.assertFalse(drift_class.automatic_zero_inference)
            self.assertEqual(
                drift_class.primary_route, "quarantine_published_tail_isolation"
            )

            f.live_base = f.requested_base = drifted_base
            f.attempt = 2
            f.mode = "confirmed"
            self.assertEqual(f.invoke(human=True), 1, f.error)
            self.assertIn(
                "original reviewed transaction cannot be established", f.error
            )
            self.assertEqual(f.provider_calls, saved_calls)
            self.assertEqual(f.comment, saved_comment)
            self.assertEqual(
                parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).record_sha256,
                parse_session_comment(
                    saved_comment["body"], identity=IDENTITY
                ).record_sha256,
            )

            # Control B: Restore exact same base on unclosed `publication_succeeded`:
            # `retained_analysis` refuses unverified published tails on attempt 2, so
            # the classifier fails closed and routes to `quarantine_published_tail_isolation`.
            f.trace[:] = saved_trace
            f.live_base = f.requested_base = same_base
            f.attempt = 2
            f.mode = "confirmed"
            ledger_same_head, _ = _diagnostic_ledger(f)
            same_head_class = classify_live_ledger_recovery(
                ledger=ledger_same_head,
                identity=IDENTITY,
                current_base=same_base,
                current_head=same_head,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                same_head_class.state_code, "same_head_unclosed_publication_succeeded"
            )
            self.assertFalse(same_head_class.automatic_zero_inference)
            self.assertEqual(
                same_head_class.primary_route, "quarantine_published_tail_isolation"
            )
            claims = list(f.root.glob("reviewsensei-api-*-2-*.claim"))
            self.assertTrue(claims)
            for claim in claims:
                claim.unlink()
            self.assertEqual(f.invoke(human=True), 1, f.error)
            self.assertIn(
                "successor publication tail is unverified; authenticated predecessor completion is required",
                f.error,
            )
            self.assertEqual(f.provider_calls, saved_calls)
            self.assertEqual(f.reviews, saved_reviews)

        # Control C: Same-head, same-base `publication_pending` replays retained result
        # on attempt 2 with zero new inference and settles `terminal_transaction`!
        from test_review_run import EntryFixture

        with tempfile.TemporaryDirectory() as pending_dir:
            fp = EntryFixture(pending_dir, limit=512)

            def interrupted() -> None:
                raise KeyboardInterrupt()

            fp.before_publication = interrupted
            self.assertEqual(fp.invoke(), 130, fp.error)
            fp.attempt = 2
            fp.before_publication = None
            pending_ledger, _ = _diagnostic_ledger(fp)
            pending_class = classify_live_ledger_recovery(
                ledger=pending_ledger,
                identity=IDENTITY,
                current_base=fp.requested_base,
                current_head=fp.requested_head,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                pending_class.state_code,
                "publication_pending_same_head_replay_eligible",
            )
            self.assertTrue(pending_class.automatic_zero_inference)
            self.assertEqual(
                pending_class.primary_route, "same_run_attempt2_retained_replay"
            )
            self.assertEqual(fp.invoke(), 0, fp.error)
            self.assertEqual(fp.provider_calls, 1)
            settled = parse_session_comment(fp.comment["body"], identity=IDENTITY)
            self.assertEqual(settled.transaction.phase, "publication_succeeded")
            self.assertEqual(
                settled.api_run_accounting["terminal_transaction"],
                settled.transaction.transaction_id,
            )
            settled_acc = ApiRunAccounting.from_document(settled.api_run_accounting)
            self.assertEqual(settled_acc.uncertain, 0)
            self.assertEqual(
                [p.request_id for b in settled_acc.blocks for p in b.pending], []
            )
            self.assertIn(
                (settled_acc.owner.job, 2),
                set(settled_acc.startup_used),
            )
            settled_ledger, _ = _diagnostic_ledger(fp)
            self.assertEqual(
                classify_live_ledger_recovery(
                    ledger=settled_ledger,
                    identity=IDENTITY,
                    current_base=fp.requested_base,
                    current_head=fp.requested_head,
                    actions_token="synthetic-actions",
                ).state_code,
                "terminal_review_settled",
            )

    def test_negative_controls_fail_closed_and_recover_via_controlled_restart(
        self,
    ) -> None:
        from test_review_run import ConsecutiveReviewRunTests
        from test_review_transaction import HostedAnalysisCliTests

        # 1. Coverage-only partial predecessor refuses automatic historical completion.
        with tempfile.TemporaryDirectory() as directory:
            f_partial = ConsecutiveReviewRunTests.historical_case(
                directory, partial_predecessor=True
            )
            ledger, _ = _diagnostic_ledger(f_partial)
            partial_class = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=f_partial.requested_base,
                current_head=f_partial.requested_head,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                partial_class.state_code, "unclosed_publication_coverage_only_partial"
            )
            self.assertFalse(partial_class.automatic_zero_inference)
            calls_before = f_partial.provider_calls
            self.assertEqual(f_partial.invoke(human=True), 1, f_partial.error)
            self.assertIn(
                "original retained result or tail is unsupported", f_partial.error
            )
            self.assertEqual(f_partial.provider_calls, calls_before)

        # 2. Failed executor on unclosed `publication_succeeded` refuses automatic
        #    historical completion, then recovers to completion via designated
        #    controlled serialized restart!
        with tempfile.TemporaryDirectory() as directory:
            f_failed = ConsecutiveReviewRunTests.historical_case(directory)
            # Remove the synthetic old soft-cleanup qualification so the predecessor
            # run remains a genuine failed executor (`conclusion == "failure"`).
            self.assertTrue(f_failed.qualified_old_soft_cleanup_successes)
            f_failed.qualified_old_soft_cleanup_successes.clear()
            ledger, _ = _diagnostic_ledger(f_failed)
            failed_class = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=f_failed.requested_base,
                current_head=f_failed.requested_head,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                failed_class.state_code, "unclosed_publication_failed_executor"
            )
            self.assertIn("controlled_serialized_restart", failed_class.fallback_routes)
            calls_before = f_failed.provider_calls
            self.assertEqual(f_failed.invoke(human=True), 1, f_failed.error)
            self.assertIn("did not prove successful review exit", f_failed.error)
            self.assertEqual(f_failed.provider_calls, calls_before)

            # Now demonstrate bounded recovery to completion on a clean ControlledRestart
            # execution preserving the opaque predecessor comment verbatim!
            old_comment = copy.deepcopy(f_failed.comment)
            old_comment["id"] = 77
            old_body = old_comment["body"]
            old_rec = parse_session_comment(old_body, identity=IDENTITY)
            old_run_id = old_rec.api_run_accounting["identity"]["run_id"]

            with tempfile.TemporaryDirectory() as restart_dir:
                rf = EntryFixture(restart_dir)
                rf.event_name = "workflow_dispatch"
                row_data = dict(
                    repository="owner/repo",
                    repository_id=123,
                    pull_request=7,
                    base_sha=rf.requested_base,
                    head_sha=rf.requested_head,
                    policy_commit=rf.policy,
                    workflow_sha="a" * 40,
                    run_id=str(rf.run_id),
                    job="hosted",
                    issuance_disabled_at=1000,
                    retire_after=4600,
                    old_runs=[old_run_id],
                    old_comments=[
                        dict(
                            id=old_comment["id"],
                            bytes=len(old_body.encode("utf-8")),
                            sha256=hashlib.sha256(old_body.encode("utf-8")).hexdigest(),
                        )
                    ],
                )
                row_data["request_id"] = hashlib.sha256(
                    canonical_bytes(row_data)
                ).hexdigest()

                original_external = rf.external
                original_main = cli.main
                comments_list_calls: list[str] = []
                prepare_diff_calls: list[dict[str, object]] = []

                def external(request, timeout):
                    path = urlsplit(request.full_url).path
                    if path.endswith(f"/actions/runs/{old_run_id}"):
                        rf.trace.append((rf.attempt, request.method, path))
                        return json_response(
                            dict(
                                id=old_run_id,
                                repository=dict(id=123, full_name="owner/repo"),
                                status="completed",
                                run_attempt=1,
                            )
                        )
                    if path.endswith(f"/actions/runs/{old_run_id}/attempts/1/jobs"):
                        rf.trace.append((rf.attempt, request.method, path))
                        return json_response(
                            dict(
                                total_count=1,
                                jobs=[
                                    dict(
                                        id=10,
                                        run_id=old_run_id,
                                        run_attempt=1,
                                        status="completed",
                                    )
                                ],
                            )
                        )
                    if path.endswith(f"/issues/comments/{old_comment['id']}"):
                        rf.trace.append((rf.attempt, request.method, path))
                        return json_response(old_comment)
                    response = original_external(request, timeout)
                    if path.endswith("/issues/7/comments") and request.method == "GET":
                        comments_list_calls.append(request.full_url)
                        return json_response([old_comment, *json.loads(response.body)])
                    return response

                rf.external = external

                def restart_main(argv):
                    argv = [
                        "restart-run",
                        "--restart-id",
                        row_data["request_id"],
                        *argv[1:],
                    ]
                    if "--diff" in argv:
                        idx = argv.index("--diff")
                        del argv[idx : idx + 2]
                    return original_main(argv)

                def prepare_diff(**kwargs):
                    prepare_diff_calls.append(dict(kwargs))
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
                self.assertTrue(comments_list_calls)
                self.assertEqual(len(prepare_diff_calls), 1)
                self.assertEqual(prepare_diff_calls[0]["head_ref"], rf.requested_head)
                self.assertEqual(prepare_diff_calls[0]["base_ref"], rf.requested_base)
                self.assertIsNotNone(rf.selected.caller.accounting.terminal_transaction)
                archived = json.loads((rf.root / "abandoned-history.json").read_bytes())
                self.assertEqual(archived["comments"][0]["body"], old_body)

    def test_terminal_action_null_review_marker_and_corrupt_carrier_live_ledger(
        self,
    ) -> None:
        # Exercise both v2 inline and v3 partitioned terminal actions with
        # `terminal_transaction=None` and `transaction=None` via live read-only ledger,
        # plus end-to-end CommandEntryFixture admission.
        for partitioned in (False, True):
            with (
                self.subTest(partitioned=partitioned),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CommandEntryFixture(directory, limit=512)
                acc, extra_parts = _make_terminal_action_accounting(
                    head_sha=ACTION_HEAD,
                    run_id=f.run_id,
                    job=f.owner_job,
                    partitioned=partitioned,
                )
                record = _make_record(accounting=acc, transaction=None)
                f.comment = {
                    "id": 1_000_000,
                    "body": render_session_comment(
                        repository_id=123, pull_request=7, record=record
                    ),
                    "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                }
                for item in extra_parts:
                    f.extra_comments[item["id"]] = item

                ledger, _ = _diagnostic_ledger(f)
                classified = classify_live_ledger_recovery(
                    ledger=ledger,
                    identity=IDENTITY,
                    current_base=f.requested_base,
                    current_head=ACTION_HEAD,
                )
                if partitioned:
                    # Intentional negative control: `caller-requests-v3` without the
                    # matching writer version contract is rejected by
                    # `ApiRunAccounting.require_writer_version()` and fails closed
                    # to `legacy_or_corrupt_accounting`.
                    self.assertEqual(
                        classified.state_code, "legacy_or_corrupt_accounting"
                    )
                    self.assertFalse(classified.terminal)
                    self.assertEqual(
                        classified.primary_route, "controlled_serialized_restart"
                    )
                else:
                    self.assertEqual(
                        classified.state_code,
                        "terminal_action_without_review_transaction",
                    )
                    self.assertTrue(classified.terminal)
                    self.assertTrue(classified.automatic_zero_inference)
                    self.assertEqual(classified.primary_route, "ordinary_rollover")
                    # Prove subsequent command rolls over this null-transaction root cleanly!
                    f.SOURCE_ID = 205
                    f.select_command("@reviewsensei review status", new_run=True)
                    self.assertEqual(f.invoke_command(), 0, f.error)
                    rolled = parse_session_comment(f.comment["body"], identity=IDENTITY)
                    self.assertGreater(rolled.generation, record.generation)
                    self.assertEqual(
                        rolled.api_run_accounting["identity"]["run_id"], f.run_id
                    )
                    self.assertEqual(
                        rolled.api_run_accounting["action"]["phase"], "terminal"
                    )

        # Also verify real CommandEntryFixture terminal action + corrupt control via live classifier!
        from test_action_run import PublishedPartialCommandAdmissionTests

        helper = PublishedPartialCommandAdmissionTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            f, instance = helper.partial_review(directory, closed=True)
            f.SOURCE_ID = 201
            f.select_command(
                "@reviewsensei override RS-"
                + instance[:6].upper()
                + " acceptable risk",
                new_run=True,
            )
            self.assertEqual(f.invoke_command(), 0, f.error)
            ledger, _ = _diagnostic_ledger(f)
            valid_action = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=ACTION_HEAD,
            )
            self.assertEqual(valid_action.state_code, "terminal_action_settled")
            self.assertTrue(valid_action.terminal)
            self.assertEqual(valid_action.primary_route, "ordinary_rollover")

            # Corrupt the inline action result binding -> live classifier flags unauthenticated!
            def corrupt_binding(rec: SessionRecord) -> SessionRecord:
                value = copy.deepcopy(rec.api_run_accounting)
                value["action"]["result"]["binding"]["head_sha"] = "f" * 40
                doc = rec.to_dict()
                doc["api_run_accounting"] = value
                doc["transaction"] = rec.transaction
                doc.pop("schema_version")
                doc.pop("record_sha256")
                return SessionRecord._construct(**doc)

            f.rewrite_record(mutate=corrupt_binding)
            corrupt_ledger, _ = _diagnostic_ledger(f)
            corrupt_classified = classify_live_ledger_recovery(
                ledger=corrupt_ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=ACTION_HEAD,
            )
            self.assertEqual(
                corrupt_classified.state_code, "terminal_action_result_unauthenticated"
            )
            self.assertFalse(corrupt_classified.terminal)
            self.assertEqual(
                corrupt_classified.missing_witnesses,
                ("authenticated_terminal_action_result",),
            )

    def test_live_ledger_unenrolled_corrupt_and_missing_retained_result_controls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            # 1. Unenrolled PR
            ledger, _ = _diagnostic_ledger(f)
            unenrolled = classify_live_ledger_recovery(
                ledger=ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=f.requested_head,
            )
            self.assertEqual(unenrolled.state_code, "unenrolled_ready")
            self.assertTrue(unenrolled.terminal)
            self.assertEqual(unenrolled.primary_route, "ordinary_rollover")

            # 2. Corrupt ledger root comment (real session marker with broken JSON)
            seed_rec = _make_record(accounting=_make_accounting(), transaction=None)
            valid_body = render_session_comment(
                repository_id=123, pull_request=7, record=seed_rec
            )
            f.comment = {
                "id": 500,
                "body": valid_body.replace('"schema_version":"1.0"', "{not-json"),
                "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
            corrupt_ledger, _ = _diagnostic_ledger(f)
            corrupt_root = classify_live_ledger_recovery(
                ledger=corrupt_ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=f.requested_head,
            )
            self.assertEqual(corrupt_root.state_code, "unauthenticated_ledger_root")
            self.assertFalse(corrupt_root.terminal)
            self.assertEqual(
                corrupt_root.primary_route, "controlled_serialized_restart"
            )

            # 3. Missing retained ReviewResult carrier on unclosed `publication_succeeded`
            acc = _make_accounting()
            tx = _make_tx(
                phase="publication_succeeded",
                base_sha=f.requested_base,
                head_sha=f.requested_head,
            )
            rec = _make_record(accounting=acc, transaction=tx)
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=rec
            )
            missing_ledger, _ = _diagnostic_ledger(f)
            missing_retained = classify_live_ledger_recovery(
                ledger=missing_ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=f.requested_head,
            )
            self.assertEqual(
                missing_retained.state_code, "same_head_unclosed_publication_succeeded"
            )
            self.assertIn(
                "retained_review_result_and_context",
                missing_retained.missing_witnesses,
            )

            with self.assertRaises(ReviewInputError):
                classify_live_ledger_recovery(
                    ledger="not-a-ledger",  # type: ignore[arg-type]
                    identity=IDENTITY,
                    current_base=BASE_A,
                    current_head=HEAD_A,
                )

    def test_probe_executor_status_readonly_and_projected_checkpoint_store_controls(
        self,
    ) -> None:
        from review_sensei_github.accepted_result import selected_accepted_result
        from review_sensei_github.checkpoint_result_store import CheckpointResultStore
        from review_sensei_github.operation_comments import (
            GitHubIssueCommentOperationPort,
            ResultPartStore,
        )
        from review_sensei_github.publication_recovery import (
            _event_key,
            _publication_transport,
        )
        from review_sensei_github.recovery_route_advisor import (
            _probe_executor_status_readonly,
        )
        from review_sensei_github.review_completion_recovery import _ReadOnlyHttp

        from review_sensei.historical_completion import QUALIFIED_EXECUTORS
        from review_sensei.models import ReviewResult

        # Part 1: Direct matrix over `_probe_executor_status_readonly` (RS-DC1058, RS-F5D8A2, RS-8D77D7)
        owner = ApiExecutionOwner(1, "hosted", 0)
        catalog_sha, catalog_contract = next(iter(QUALIFIED_EXECUTORS.items()))
        probe_state: dict[str, object] = {
            "job_status": "in_progress",
            "job_conclusion": None,
            "run_http_status": 200,
            "run_status": "completed",
            "run_conclusion": "success",
            "run_attempt": 1,
            "workflow_sha": "f" * 40,
            "workflow_path": catalog_contract["workflow_path"],
            "workflow_ref": catalog_contract["workflow_ref"],
        }

        def probe_opener(request, timeout=None):
            path = urlsplit(request.full_url).path
            if path.endswith("/actions/runs/777"):
                if probe_state["run_http_status"] != 200:
                    return json_response(
                        {"message": "unavailable"}, int(probe_state["run_http_status"])
                    )
                return json_response(
                    {
                        "id": 777,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": probe_state["run_status"],
                        "conclusion": probe_state["run_conclusion"],
                        "run_attempt": probe_state["run_attempt"],
                        "head_sha": HEAD_A,
                        "event": "pull_request",
                        "referenced_workflows": [
                            {
                                "sha": probe_state["workflow_sha"],
                                "path": probe_state["workflow_path"],
                                "ref": probe_state["workflow_ref"],
                            }
                        ],
                    }
                )
            if path.endswith("/actions/runs/777/attempts/1/jobs"):
                return json_response(
                    {
                        "total_count": 1,
                        "jobs": [
                            {
                                "id": 10,
                                "name": "hosted",
                                "run_id": 777,
                                "run_attempt": 1,
                                "status": probe_state["job_status"],
                                "conclusion": probe_state["job_conclusion"],
                                "completed_at": "2026-11-10T12:05:00Z",
                                "steps": [
                                    {
                                        "number": 1,
                                        "name": "Wrong step",
                                        "status": "completed",
                                        "conclusion": "success",
                                    }
                                ],
                            }
                        ],
                    }
                )
            raise AssertionError(f"unexpected probe path: {path}")

        ro_http = _ReadOnlyHttp(
            GitHubHttp(api_url="https://api.github.test", opener=probe_opener)
        )
        self.assertEqual(
            _probe_executor_status_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=0,
                owner=owner,
                tx_head_sha=HEAD_A,
            ),
            "active_or_unconfirmed",
        )
        # 1a. Active job -> `active_or_unconfirmed`
        self.assertEqual(
            _probe_executor_status_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha=HEAD_A,
            ),
            "active_or_unconfirmed",
        )
        # 1b. Failed job -> `failed`
        probe_state["job_status"] = "completed"
        probe_state["job_conclusion"] = "failure"
        self.assertEqual(
            _probe_executor_status_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha=HEAD_A,
            ),
            "failed",
        )
        # 1c. Successful job + unreadable `run_doc` -> `ceased` (RS-F5D8A2)
        probe_state["job_conclusion"] = "success"
        probe_state["run_http_status"] = 404
        self.assertEqual(
            _probe_executor_status_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha=HEAD_A,
            ),
            "ceased",
        )
        # 1d. Successful job + non-success overall run conclusion -> `ceased` (RS-F5D8A2)
        probe_state["run_http_status"] = 200
        probe_state["run_conclusion"] = "failure"
        self.assertEqual(
            _probe_executor_status_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha=HEAD_A,
            ),
            "ceased",
        )
        # 1e. Successful job & run + uncatalogued workflow SHA -> `unrecognized`
        probe_state["run_conclusion"] = "success"
        self.assertEqual(
            _probe_executor_status_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha=HEAD_A,
            ),
            "unrecognized",
        )
        # 1f. Catalogued workflow SHA whose step/head contract failed -> `failed`
        probe_state["workflow_sha"] = catalog_sha
        self.assertEqual(
            _probe_executor_status_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha=HEAD_A,
            ),
            "failed",
        )

        # Part 2: `classify_live_ledger_recovery` over a projected `CheckpointResultStore`
        # carrier (`CHECKPOINT_VERSION`) and `app_id` validation (RS-E917E3, RS-7B4178):
        with tempfile.TemporaryDirectory() as directory:
            fp = EntryFixture(directory, limit=512)

            def stop_before_pub() -> None:
                raise KeyboardInterrupt()

            fp.before_publication = stop_before_pub
            self.assertEqual(fp.invoke(), 130, fp.error)
            rec = parse_session_comment(fp.comment["body"], identity=IDENTITY)
            assert rec.transaction is not None
            tx = rec.transaction
            captured = selected_accepted_result(fp.selected.http, tx, app_id=999)
            bound = replace(
                ReviewResult.from_dict(json.loads(captured.payload)),
                transaction=tx,
            )
            key = _event_key(IDENTITY, tx)
            # Replace the raw result parts in `fp.extra_comments` with a compact
            # `operation-checkpoint-projection-v1` carrier written by `CheckpointResultStore`!
            port = GitHubIssueCommentOperationPort(
                fp.selected.http,
                token="installation-token",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                app_user_id=999,
                strict_association=True,
            )
            # Clear raw checkpoint parts for `key`, keeping the accepted source event comments.
            raw_store = ResultPartStore(port, app_user_id=999)
            existing = raw_store.read_available_with_carriers(event_key=key)
            assert existing is not None
            for cid in existing[2]:
                fp.extra_comments.pop(cid, None)

            physical = _publication_transport(fp.selected.http)
            compact_store = CheckpointResultStore(
                port,
                app_user_id=999,
                http=fp.selected.http,
                identity=IDENTITY,
                transaction=tx,
                app_id=999,
                compact_write=True,
            )
            with (
                patch.object(
                    physical,
                    "_quarantine_capacity_guard",
                    SimpleNamespace(
                        before_request=lambda *_a, **_kw: None,
                        after_response=lambda *_a, **_kw: None,
                        admit_comment_batch=lambda *_a, **_kw: None,
                    ),
                ),
                patch(
                    "review_sensei_github.checkpoint_result_store.selected_accepted_result",
                    return_value=replace(
                        captured, native_format="literal-malformed-v1"
                    ),
                ),
            ):
                compact_store.write(
                    event_key=key,
                    result=canonical_bytes(bound.to_dict()),
                    _check_existing=False,
                )

            fp.attempt = 2
            ledger_proj, _ = _diagnostic_ledger(fp)
            proj_class = classify_live_ledger_recovery(
                ledger=ledger_proj,
                identity=IDENTITY,
                current_base=fp.requested_base,
                current_head=fp.requested_head,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                proj_class.state_code,
                "publication_pending_same_head_replay_eligible",
            )
            self.assertEqual(
                proj_class.primary_route, "same_run_attempt2_retained_replay"
            )

            # Without `app_id` (`authenticated_app_id=None`), compact checkpoint
            # source authentication fails closed -> `nonterminal_transaction_isolation_required`!
            ledger_no_app, _ = _diagnostic_ledger(fp)
            ledger_no_app.authenticated_app_id = None
            no_app_class = classify_live_ledger_recovery(
                ledger=ledger_no_app,
                identity=IDENTITY,
                current_base=fp.requested_base,
                current_head=fp.requested_head,
                actions_token="synthetic-actions",
            )
            self.assertEqual(
                no_app_class.state_code,
                "nonterminal_transaction_isolation_required",
            )
            with self.assertRaisesRegex(ReviewInputError, "positive integer"):
                classify_live_ledger_recovery(
                    ledger=ledger_no_app,
                    identity=IDENTITY,
                    current_base=fp.requested_base,
                    current_head=fp.requested_head,
                    app_id=0,
                )

    def test_reviewsensei_443_run_43074015855_gen11_packet_and_null_marker_contrast(
        self,
    ) -> None:
        """G03: `#443` run `43074015855` gen-11 stays unknown/held while valid command actions stay terminal."""
        self.assertEqual(REEXPORTED_PACKET_SCHEMA, ATTEMPT_EVIDENCE_PACKET_SCHEMA)
        self.assertIs(ReexportedAttemptEvidencePacket, AttemptEvidencePacket)
        self.assertIs(reexported_packet_builder, build_attempt_evidence_packet)
        self.assertIs(
            reexported_live_packet_builder, build_live_ledger_attempt_evidence_packet
        )

        # 1. Construct the exact ReviewSensei #443 run 43074015855 generation 11 shape:
        # `publication_succeeded` phase with `terminal_transaction=None`, `action=None`,
        # `analysis_abort=None`, released reservation, and missing close diagnostics.
        identity_443 = ApiRunIdentity(700443, REVIEWSENSEI_443_RUN_ID)
        config_443 = FrozenApiConfiguration(
            commit=POLICY_SHA, digest="1" * 64, limit=512
        )
        owner_443 = ApiExecutionOwner(1, "hosted", 0)
        plan_443 = review_startup_plan("hosted")
        alloc_443 = owner_allocations(512, plan_443.liability)
        accounting_443 = (
            initial_accounting(
                identity_443,
                config_443,
                owner_443,
                plan_443,
                prior_nonstartup_liability_upper_bound=14,
            )
            .reserve("hosted-review", alloc_443.first_work)
            .reserve("hosted-terminal", alloc_443.first_terminal)
        )
        tx_443 = replace(
            ReviewTransaction.create(
                repository=REVIEWSENSEI_443_REPOSITORY,
                pull_request=REVIEWSENSEI_443_PULL_REQUEST,
                base_sha=BASE_A,
                head_sha=HEAD_A,
                reservation_id="5" * 64,
                policy_digest="2" * 64,
                configuration_digest="1" * 64,
                evidence_digest="3" * 64,
                generation=REVIEWSENSEI_443_GENERATION,
            ),
            phase="publication_succeeded",
            result_sha256="4" * 64,
        )
        record_443 = SessionRecord._construct(
            repository=REVIEWSENSEI_443_REPOSITORY,
            pull_request=REVIEWSENSEI_443_PULL_REQUEST,
            repository_id=700443,
            completed_initial_reviews=1,
            completed_verification_rounds=4,
            failed_attempts=0,
            generation=REVIEWSENSEI_443_GENERATION,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id=tx_443.reservation_id,
            created_at="2026-11-10T12:00:00Z",
            updated_at="2026-11-10T12:15:00Z",
            expires_at="2026-12-10T12:00:00Z",
            operator_paused=False,
            transaction=tx_443,
            api_run_accounting=accounting_443.to_document(),
        )
        original_record_digest = record_443.record_sha256
        original_serialized = render_session_comment(
            repository_id=700443,
            pull_request=REVIEWSENSEI_443_PULL_REQUEST,
            record=record_443,
        )

        packet_443_same_head = build_attempt_evidence_packet(
            record_443,
            current_base=BASE_A,
            current_head=HEAD_A,
            diagnostics_available=False,
        )
        # Strict read-only invariant: ledger record is bit-for-bit untouched.
        self.assertEqual(record_443.record_sha256, original_record_digest)
        self.assertEqual(
            render_session_comment(
                repository_id=700443,
                pull_request=REVIEWSENSEI_443_PULL_REQUEST,
                record=record_443,
            ),
            original_serialized,
        )
        self.assertFalse(packet_443_same_head.authority)
        self.assertFalse(packet_443_same_head.null_marker_universal_failure)
        self.assertEqual(packet_443_same_head.case_id, REVIEWSENSEI_443_CASE_ID)
        self.assertEqual(packet_443_same_head.repository, REVIEWSENSEI_443_REPOSITORY)
        self.assertEqual(
            packet_443_same_head.pull_request, REVIEWSENSEI_443_PULL_REQUEST
        )
        self.assertEqual(
            packet_443_same_head.session_generation, REVIEWSENSEI_443_GENERATION
        )
        self.assertEqual(packet_443_same_head.run_id, REVIEWSENSEI_443_RUN_ID)
        self.assertEqual(
            packet_443_same_head.transaction_phase, "publication_succeeded"
        )
        self.assertIsNone(packet_443_same_head.terminal_transaction)
        self.assertIsNone(packet_443_same_head.action_phase)
        self.assertIsNone(packet_443_same_head.terminal_action_authenticated)
        self.assertIsNone(packet_443_same_head.abort_cleanup_entry_state)
        self.assertEqual(packet_443_same_head.executor_sha, "unknown")
        self.assertEqual(packet_443_same_head.installed_core_version, "unknown")
        self.assertEqual(packet_443_same_head.installed_hosted_version, "unknown")
        self.assertEqual(packet_443_same_head.terminal_settlement_form, "unsettled")
        self.assertFalse(packet_443_same_head.historical_settlement_receipt_supplied)
        self.assertFalse(packet_443_same_head.diagnostics_available)
        self.assertEqual(packet_443_same_head.inferred_cause, "unknown")
        self.assertFalse(packet_443_same_head.classification.terminal)
        self.assertEqual(
            packet_443_same_head.classification.state_code,
            "same_head_unclosed_publication_succeeded",
        )
        for required_hold in (
            "terminal_transaction_settlement",
            "missing_close_diagnostics",
            "installed_executor_provenance",
            "publication_receipt_id",
            "check_receipt_id",
            "thread_cleanup_receipt",
            "historical_live_settlement_receipt",
        ):
            self.assertIn(required_hold, packet_443_same_head.missing_proofs)

        # Different-head classification of #443 gen-11 with unknown executor also stays
        # unsettled with `inferred_cause == "unknown"` and explicit missing proofs.
        packet_443_diff_head = build_attempt_evidence_packet(
            record_443,
            current_base=BASE_A,
            current_head=HEAD_B,
            executor_status="unrecognized",
            diagnostics_available=False,
        )
        self.assertFalse(packet_443_diff_head.authority)
        self.assertEqual(packet_443_diff_head.inferred_cause, "unknown")
        self.assertEqual(packet_443_diff_head.terminal_settlement_form, "unsettled")
        self.assertIn(
            "qualified_historical_executor_contract",
            packet_443_diff_head.missing_proofs,
        )
        self.assertIn(
            "terminal_transaction_settlement",
            packet_443_diff_head.missing_proofs,
        )
        self.assertIn(
            "missing_close_diagnostics",
            packet_443_diff_head.missing_proofs,
        )
        self.assertIn(
            "historical_live_settlement_receipt",
            packet_443_diff_head.missing_proofs,
        )

        # Inventing a diagnosis from missing logs is rejected outright!
        with self.assertRaisesRegex(
            ReviewInputError, "cannot infer a cause from missing diagnostics"
        ):
            build_attempt_evidence_packet(
                record_443,
                current_base=BASE_A,
                current_head=HEAD_A,
                diagnostics_available=False,
                diagnostic_cause="confirmed_response_loss",
            )

        # When diagnostics and native receipt IDs are explicitly supplied, missing-close
        # and receipt holds narrow accurately while live settlement remains open.
        packet_443_with_receipts = build_attempt_evidence_packet(
            record_443,
            current_base=BASE_A,
            current_head=HEAD_A,
            executor_sha="d" * 40,
            executor_commit="e" * 40,
            workflow_blob="f" * 40,
            installed_core_version="0.8.20",
            installed_hosted_version="0.3.26",
            publication_receipt_id=9101,
            check_receipt_id=9102,
            thread_cleanup_receipt_state="confirmed_clean",
            native_approval_receipt_id=9103,
            diagnostics_available=True,
            diagnostic_cause="confirmed_response_loss",
        )
        self.assertEqual(
            packet_443_with_receipts.inferred_cause, "confirmed_response_loss"
        )
        self.assertNotIn(
            "missing_close_diagnostics", packet_443_with_receipts.missing_proofs
        )
        self.assertNotIn(
            "publication_receipt_id", packet_443_with_receipts.missing_proofs
        )
        self.assertNotIn("check_receipt_id", packet_443_with_receipts.missing_proofs)
        self.assertNotIn(
            "thread_cleanup_receipt", packet_443_with_receipts.missing_proofs
        )
        self.assertIn(
            "terminal_transaction_settlement",
            packet_443_with_receipts.missing_proofs,
        )
        self.assertIn(
            "historical_live_settlement_receipt",
            packet_443_with_receipts.missing_proofs,
        )
        serialized_packet = packet_443_with_receipts.to_dict()
        self.assertIs(serialized_packet["authority"], False)
        self.assertIs(serialized_packet["null_marker_universal_failure"], False)
        self.assertEqual(serialized_packet["case_id"], REVIEWSENSEI_443_CASE_ID)

        # 2. Contrast directly with valid terminal command actions having a null
        # review terminal marker (`terminal_transaction=None`):
        cmd_acc, _ = _make_terminal_action_accounting(head_sha=HEAD_A, job="command")
        cmd_no_tx = _make_record(accounting=cmd_acc, transaction=None, generation=4)
        cmd_packet_no_tx = build_attempt_evidence_packet(
            cmd_no_tx,
            current_base=BASE_A,
            current_head=HEAD_A,
            evidence_producer=PRODUCER,
        )
        self.assertFalse(cmd_packet_no_tx.authority)
        self.assertFalse(cmd_packet_no_tx.null_marker_universal_failure)
        self.assertIsNone(cmd_packet_no_tx.terminal_transaction)
        self.assertTrue(cmd_packet_no_tx.terminal_action_authenticated)
        self.assertTrue(cmd_packet_no_tx.classification.terminal)
        self.assertEqual(
            cmd_packet_no_tx.terminal_settlement_form,
            "authenticated_terminal_action_null_review_marker",
        )
        self.assertEqual(cmd_packet_no_tx.inferred_cause, "not_applicable_settled")
        self.assertEqual(cmd_packet_no_tx.missing_proofs, ())

        prior_tx = _make_tx(phase="publication_succeeded", head_sha=HEAD_A)
        cmd_with_tx = _make_record(
            accounting=cmd_acc, transaction=prior_tx, generation=5
        )
        cmd_packet_with_tx = build_attempt_evidence_packet(
            cmd_with_tx,
            current_base=BASE_A,
            current_head=HEAD_A,
            evidence_producer=PRODUCER,
        )
        self.assertIsNone(cmd_packet_with_tx.terminal_transaction)
        self.assertTrue(cmd_packet_with_tx.classification.terminal)
        self.assertEqual(
            cmd_packet_with_tx.terminal_settlement_form,
            "authenticated_terminal_action_with_prior_review_marker",
        )
        self.assertEqual(cmd_packet_with_tx.missing_proofs, ())

        # 3. Also verify pre-entry abort and settled review settlement forms:
        abort_base = _make_accounting()
        abort_tx = _make_tx(phase="analysis", head_sha=HEAD_A)
        abort_doc = abort_base.to_document()
        for blk in abort_doc["blocks"]:
            blk["pending"] = []
        abort_doc["analysis_abort"] = {
            "source_root": "6" * 64,
            "cleanup_root": "7" * 64,
            "transaction": abort_tx.to_dict(),
            "entry_state": "not_started",
            "run_id": abort_base.identity.run_id,
            "owner": {"run_attempt": 1, "job": "hosted", "generation": 0},
        }
        abort_record = _make_record(
            accounting=ApiRunAccounting.from_document(abort_doc),
            transaction=None,
        )
        abort_packet = build_attempt_evidence_packet(
            abort_record, current_base=BASE_A, current_head=HEAD_A
        )
        self.assertTrue(abort_packet.classification.terminal)
        self.assertEqual(
            abort_packet.terminal_settlement_form, "pre_entry_analysis_abort"
        )
        self.assertEqual(abort_packet.abort_cleanup_entry_state, "not_started")
        self.assertEqual(abort_packet.abort_cleanup_root_sha256, "7" * 64)

        settled_tx = _make_tx(phase="publication_succeeded", head_sha=HEAD_A)
        settled_doc = _make_accounting().to_document()
        settled_doc["terminal_transaction"] = settled_tx.transaction_id
        settled_rec = _make_record(
            accounting=ApiRunAccounting.from_document(settled_doc),
            transaction=settled_tx,
        )
        settled_packet = build_attempt_evidence_packet(
            settled_rec, current_base=BASE_A, current_head=HEAD_A
        )
        self.assertTrue(settled_packet.classification.terminal)
        self.assertEqual(
            settled_packet.terminal_settlement_form, "review_terminal_transaction"
        )
        self.assertEqual(settled_packet.inferred_cause, "not_applicable_settled")

    def test_attempt_evidence_packet_live_readonly_and_guardrails(self) -> None:
        """Verify `build_live_ledger_attempt_evidence_packet` read-only modes & validation."""
        # 1. Validation guardrails on `AttemptEvidencePacket` and `build_attempt_evidence_packet`:
        settled_tx = _make_tx(phase="publication_succeeded", head_sha=HEAD_A)
        settled_doc = _make_accounting().to_document()
        settled_doc["terminal_transaction"] = settled_tx.transaction_id
        settled_rec = _make_record(
            accounting=ApiRunAccounting.from_document(settled_doc),
            transaction=settled_tx,
        )
        valid_packet = build_attempt_evidence_packet(
            settled_rec, current_base=BASE_A, current_head=HEAD_A
        )
        with self.assertRaisesRegex(ReviewInputError, "strictly diagnostic"):
            replace(valid_packet, authority=True)
        with self.assertRaisesRegex(ReviewInputError, "strictly diagnostic"):
            replace(valid_packet, null_marker_universal_failure=True)
        with self.assertRaisesRegex(ReviewInputError, "RecoveryClassification"):
            replace(valid_packet, classification="bad")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "case_id"):
            replace(valid_packet, case_id="   ")
        with self.assertRaisesRegex(ReviewInputError, "run_attempt"):
            replace(valid_packet, run_attempt=3)
        with self.assertRaisesRegex(ReviewInputError, "40-hex SHA"):
            replace(valid_packet, executor_sha="not-a-sha")
        with self.assertRaisesRegex(ReviewInputError, "version token"):
            replace(valid_packet, installed_core_version="bad version!")
        with self.assertRaisesRegex(ReviewInputError, "64-hex SHA-256"):
            replace(valid_packet, record_sha256="xyz")
        with self.assertRaisesRegex(ReviewInputError, "positive integer"):
            replace(valid_packet, run_id=0)
        with self.assertRaisesRegex(ReviewInputError, "non-negative integer"):
            replace(valid_packet, confirmed_requests=-1)
        with self.assertRaisesRegex(ReviewInputError, "thread_cleanup_receipt_state"):
            replace(valid_packet, thread_cleanup_receipt_state="bogus")
        with self.assertRaisesRegex(ReviewInputError, "vocabulary is invalid"):
            replace(valid_packet, terminal_settlement_form="bogus")
        with self.assertRaisesRegex(ReviewInputError, "missing diagnostics"):
            replace(
                valid_packet,
                diagnostics_available=False,
                inferred_cause="confirmed_response_loss",
            )
        with self.assertRaisesRegex(ReviewInputError, "record its settlement form"):
            replace(valid_packet, terminal_settlement_form="unsettled")
        with self.assertRaisesRegex(ReviewInputError, "missing_proofs are invalid"):
            replace(valid_packet, missing_proofs=("not_a_proof",))
        with self.assertRaisesRegex(ReviewInputError, "cannot carry a failure cause"):
            build_attempt_evidence_packet(
                settled_rec,
                current_base=BASE_A,
                current_head=HEAD_A,
                diagnostics_available=True,
                diagnostic_cause="confirmed_response_loss",
            )
        with self.assertRaisesRegex(ReviewInputError, "boolean flags"):
            build_attempt_evidence_packet(
                settled_rec,
                current_base=BASE_A,
                current_head=HEAD_A,
                diagnostics_available="yes",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(
            ReviewInputError, "diagnostic_cause is unsupported"
        ):
            build_attempt_evidence_packet(
                settled_rec,
                current_base=BASE_A,
                current_head=HEAD_A,
                diagnostic_cause="invented_cause",
            )
        no_repo_id_rec = SessionRecord._construct(
            repository="owner/repo",
            pull_request=7,
            repository_id=None,
            completed_initial_reviews=0,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=1,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id=None,
            created_at="2026-11-10T12:00:00Z",
            updated_at="2026-11-10T12:00:00Z",
            expires_at="2026-12-10T12:00:00Z",
            operator_paused=False,
        )
        with self.assertRaisesRegex(ReviewInputError, "repository_id-bound"):
            build_attempt_evidence_packet(
                no_repo_id_rec, current_base=BASE_A, current_head=HEAD_A
            )
        self.assertTrue(MISSING_WITNESSES.issubset(ATTEMPT_PACKET_MISSING_PROOFS))

        # 2. Read-only live ledger packet inspection across missing, corrupt, and qualified states:
        with tempfile.TemporaryDirectory() as directory:
            empty_fx = EntryFixture(directory, limit=512)
            empty_ledger, empty_obs = _diagnostic_ledger(empty_fx)
            missing_pkt = build_live_ledger_attempt_evidence_packet(
                ledger=empty_ledger,
                identity=IDENTITY,
                current_base=BASE_A,
                current_head=HEAD_A,
            )
            self.assertFalse(missing_pkt.authority)
            self.assertEqual(missing_pkt.terminal_settlement_form, "unenrolled_session")
            self.assertTrue(all(method == "GET" for method, _ in empty_obs), empty_obs)

            seed_rec = _make_record(accounting=_make_accounting(), transaction=None)
            valid_body = render_session_comment(
                repository_id=123, pull_request=7, record=seed_rec
            )
            empty_fx.comment = {
                "id": 500,
                "body": valid_body.replace('"schema_version":"1.0"', "{not-json"),
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
            corrupt_ledger, corrupt_obs = _diagnostic_ledger(empty_fx)
            corrupt_pkt = build_live_ledger_attempt_evidence_packet(
                ledger=corrupt_ledger,
                identity=IDENTITY,
                current_base=BASE_A,
                current_head=HEAD_A,
            )
            self.assertFalse(corrupt_pkt.classification.terminal)
            self.assertEqual(corrupt_pkt.terminal_settlement_form, "unsettled")
            self.assertIn("authenticated_session_root", corrupt_pkt.missing_proofs)
            with self.assertRaisesRegex(
                ReviewInputError, "cannot claim terminal settlement"
            ):
                replace(
                    corrupt_pkt,
                    terminal_settlement_form="review_terminal_transaction",
                )
            self.assertTrue(
                all(method == "GET" for method, _ in corrupt_obs), corrupt_obs
            )

        from test_review_run import ConsecutiveReviewRunTests

        with tempfile.TemporaryDirectory() as directory:
            comp_fx = ConsecutiveReviewRunTests.historical_case(directory)
            comp_ledger, comp_obs = _diagnostic_ledger(comp_fx)
            comp_pkt = build_live_ledger_attempt_evidence_packet(
                ledger=comp_ledger,
                identity=IDENTITY,
                current_base=comp_fx.requested_base,
                current_head=comp_fx.requested_head,
                actions_token="synthetic-actions",
            )
            self.assertFalse(comp_pkt.authority)
            self.assertEqual(comp_pkt.executor_status, "qualified_success")
            self.assertNotEqual(comp_pkt.executor_sha, "unknown")
            self.assertNotEqual(comp_pkt.executor_commit, "unknown")
            self.assertNotEqual(comp_pkt.workflow_blob, "unknown")
            self.assertEqual(comp_pkt.terminal_settlement_form, "unsettled")
            self.assertEqual(comp_pkt.inferred_cause, "unknown")
            self.assertIn("historical_live_settlement_receipt", comp_pkt.missing_proofs)
            self.assertTrue(all(method == "GET" for method, _ in comp_obs), comp_obs)

        with tempfile.TemporaryDirectory() as directory:
            cmd_fx = CommandEntryFixture(directory)
            live_cmd_acc, extra_parts = _make_terminal_action_accounting(
                head_sha=ACTION_HEAD, job="command"
            )
            live_cmd_rec = _make_record(accounting=live_cmd_acc, transaction=None)
            cmd_fx.comment = {
                "id": 601,
                "body": render_session_comment(
                    repository_id=123, pull_request=7, record=live_cmd_rec
                ),
                "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
            for item in extra_parts:
                cmd_fx.extra_comments[item["id"]] = item
            cmd_ledger, cmd_obs = _diagnostic_ledger(cmd_fx)
            cmd_live_pkt = build_live_ledger_attempt_evidence_packet(
                ledger=cmd_ledger,
                identity=IDENTITY,
                current_base=cmd_fx.requested_base,
                current_head=ACTION_HEAD,
                actions_token="synthetic-actions",
            )
            self.assertTrue(cmd_live_pkt.classification.terminal)
            self.assertIsNone(cmd_live_pkt.terminal_transaction)
            self.assertEqual(
                cmd_live_pkt.terminal_settlement_form,
                "authenticated_terminal_action_null_review_marker",
            )
            self.assertTrue(all(method == "GET" for method, _ in cmd_obs), cmd_obs)

        from review_sensei_github.historical_completion import HistoricalSnapshotLedger

        snap_ledger = HistoricalSnapshotLedger(
            SimpleNamespace(
                ledger=SimpleNamespace(),
                identity=IDENTITY,
                record=settled_rec,
                require_process=lambda: None,
            )
        )
        with self.assertRaisesRegex(ReviewInputError, "permits reads only"):
            snap_ledger.replace()
        with self.assertRaisesRegex(ReviewInputError, "permits reads only"):
            snap_ledger._request("POST", "/repos/owner/repo/issues/7/comments")
        with self.assertRaisesRegex(ReviewInputError, "historical ledger identity"):
            snap_ledger.load(replace(IDENTITY, pull_request=99))
        self.assertEqual(snap_ledger.load(IDENTITY).record, settled_rec)


if __name__ == "__main__":
    unittest.main()
