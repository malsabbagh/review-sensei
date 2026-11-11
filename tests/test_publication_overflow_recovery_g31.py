"""Historical publication overflow and nonterminal failure recovery (G31).

Verifies offline with synthetic GitHub services that:
1. TaskAlong `#41`-class (`be1` executor) prepublication body-limit overflows keep
   the original `ReviewResult`, `ReviewConvergencePolicy`, and all byte/call/part
   allowances (`65,536` review body bytes, `512` caller limit, `64`/`60` read
   budget, `32` result parts) strictly immutable while comparing exact historical
   pre-archive vs archive-backed formatter outputs.
2. Failure-before-write (`tx.phase == "publication_failed"`, `uncertain == 0`,
   zero remote review write) is strictly separated from response-loss / uncertain
   remote write (`tx.phase == "publication_pending"` or `uncertain > 0`), and
   persisted exact-head review echoes reconcile without duplicate review writes.
3. Both TaskAlong `#41` (`be1` executor, never silently appended to `c7`) and
   response-loss nonterminal sessions have deterministic, bounded end-to-end
   recovery paths that reach terminal completion without enlarging allowances.
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse, urlsplit

import review_sensei_github.cli as cli
import review_sensei_github.controlled_restart as restart
import review_sensei_github.review_run as review_run
from fake_github_http import json_response
from review_sensei_github import (
    GitHubPublicationError,
    GitHubWriteOptions,
    ReviewPublisher,
)
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.errors import GitHubHTTPTransientError
from review_sensei_github.publication import (
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    ORDINARY_EVIDENCE_READ_LIMIT,
    approval_eligibility_from_body,
    parse_finding_index,
    review_marker,
)
from review_sensei_github.publication_overflow_recovery import (
    PUBLICATION_OVERFLOW_CLASSIFIER_ID,
    PUBLICATION_OVERFLOW_PACKET_SCHEMA,
    TASKALONG_41_CASE_ID,
    TASKALONG_41_PULL_REQUEST,
    FormatterComparisonReport,
    PublicationOverflowAttemptPacket,
    PublicationOverflowRecoveryDescriptor,
    build_overflow_recovery_descriptor,
    build_publication_overflow_attempt_packet,
    compare_publication_formatter_outputs,
    qualify_nonterminal_publication_failure,
)
from review_sensei_github.publication_recovery import (
    confirm_retained_narrative,
    retain_review_result,
)
from review_sensei_github.quarantine_published_tail import (
    BODY_CALLER_BLOB,
    BODY_CONTRACT,
    EXECUTOR,
    normalize_tail_contract,
)
from review_sensei_github.recovery_route_advisor import classify_recovery_state
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_bounded_v1_publication import comments as make_body_comments
from test_human_assessment import APP, BASE, DIFF, HEAD
from test_publication_recovery import (
    CONFIGURATION,
    EVIDENCE,
    IDENTITY,
    POLICY,
    RetentionState,
    application,
    publish,
)
from test_quarantine_published_tail_isolation import contract_fixture
from test_review_run import EntryFixture

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    initial_accounting,
)
from review_sensei.bounded_evidence import (
    MAX_PART_READS,
    EvidenceReadBudget,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    checkpoint_review_analysis,
    prepare_review_transaction,
)

TASKALONG_41_BE1_EXECUTOR = "be104100" + "1" * 32
TASKALONG_41_CALLER_BLOB = "41cafe00" + "2" * 32
FULL_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n" + DIFF
)


def _make_accounting(
    *,
    run_id: int = 4101,
    attempt: int = 1,
    limit: int = 512,
    uncertain: int = 0,
    reserved_undispatched: int = 0,
) -> ApiRunAccounting:
    identity = ApiRunIdentity(1, run_id)
    config = FrozenApiConfiguration(
        commit="c" * 40,
        digest=ReviewTransaction.compute_configuration_digest(CONFIGURATION),
        limit=limit,
    )
    owner = ApiExecutionOwner(attempt, "hosted", 0)
    plan = review_startup_plan("hosted")
    total_block = 2 + uncertain + reserved_undispatched
    state = initial_accounting(
        identity,
        config,
        owner,
        plan,
        prior_nonstartup_liability_upper_bound=0,
    ).reserve("hosted-review", total_block)
    for idx in range(2):
        req = PendingApiRequest(f"req-confirmed-{idx}", 10_000)
        state = state.dispatch("hosted-review", req).confirm(req)
    for idx in range(uncertain):
        req_u = PendingApiRequest(f"req-uncertain-{idx}", 10_000)
        state = state.dispatch("hosted-review", req_u)
    return state


_UNSET = object()


def _with_accounting(
    record: SessionRecord,
    accounting: ApiRunAccounting,
    *,
    transaction: Any = _UNSET,
) -> SessionRecord:
    eff_tx = record.transaction if transaction is _UNSET else transaction
    eff_res_id = record.reservation_id if transaction is _UNSET else None
    eff_slot = record.reserved_slot if transaction is _UNSET else None
    eff_last_committed = (
        record.last_committed_reservation_id
        if transaction is _UNSET
        else (eff_tx.reservation_id if eff_tx is not None else None)
    )
    return SessionRecord._construct(
        repository=record.repository,
        pull_request=record.pull_request,
        repository_id=record.repository_id,
        completed_initial_reviews=record.completed_initial_reviews,
        completed_verification_rounds=record.completed_verification_rounds,
        failed_attempts=record.failed_attempts,
        generation=record.generation,
        reservation_id=eff_res_id,
        reserved_slot=eff_slot,
        last_committed_reservation_id=eff_last_committed,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
        operator_paused=record.operator_paused,
        failed_attempts_head_sha=record.failed_attempts_head_sha,
        dispositions=record.dispositions,
        continuation_grants=record.continuation_grants,
        transaction=eff_tx,
        convergence_history=record.convergence_history,
        reservation_owner=record.reservation_owner if transaction is _UNSET else None,
        rescan_state=record.rescan_state,
        assessment_queue=record.assessment_queue,
        api_run_accounting=accounting.to_document(),
        quarantine_lineage=record.quarantine_lineage,
        publication_staging=record.publication_staging,
    )


def _build_taskalong_41_result(
    ledger: LocalSessionLedger,
    *,
    narrative_bytes: int = 19_000,
    candidate_count: int = 80,
    human_hold_count: int = 23,
    detail_repeat: int = 11,
) -> ReviewResult:
    """Construct TaskAlong #41-shaped partial 80-candidate review with 23 holds."""
    narrative = (
        "TaskAlong #41 architectural synthesis and cross-module boundary overview. "
        * (narrative_bytes // 74 + 1)
    )[:narrative_bytes]
    raw_comments = tuple(
        ReviewComment(
            path="src/app.py",
            side="FILE",
            line=None,
            body=(
                f"Historical candidate {idx:02d}: "
                + ("bounded invariant explanation " * detail_repeat)
            ),
            symbol=f"handler_{idx:02d}",
            defect_kind=f"contract-{idx:02d}",
            severity="high" if idx < human_hold_count else "low",
            blocking=False,
            needs_human=idx < human_hold_count,
        )
        for idx in range(candidate_count)
    )
    raw = ReviewResult(
        summary=narrative,
        comments=raw_comments,
        provider="fixture",
        model="fixture-model",
        review_status="partial",
        coverage=CoverageManifest(
            enumerated_paths=("src/app.py",),
            files=(
                FileCoverage(
                    "src/app.py", "partially-reviewed", "cross-file-relationship"
                ),
            ),
            enumeration_complete=True,
        ),
    )
    prepared = prepare_review_transaction(
        ledger,
        IDENTITY,
        POLICY,
        reservation_id="d" * 64,
        base_sha=BASE,
        head_sha=HEAD,
        configuration_digest=ReviewTransaction.compute_configuration_digest(
            CONFIGURATION
        ),
        evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
    )
    return checkpoint_review_analysis(ledger, IDENTITY, prepared, raw)


class PublicationOverflowRecoveryG31Tests(unittest.TestCase):
    """Offline adversarial verification of G31 overflow & nonterminal failure."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._socket_patch = patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError(
                "real network sockets are forbidden in G31 tests"
            ),
        )
        cls._socket_patch.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._socket_patch.stop()
        super().tearDownClass()

    def test_taskalong_41_immutable_result_policy_and_exact_formatter_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = _build_taskalong_41_result(ledger)
            frozen_result_dict = copy.deepcopy(result.to_dict())
            frozen_result_digest = result.content_digest()
            frozen_policy_dict = copy.deepcopy(POLICY.to_dict())
            frozen_policy_digest = POLICY.digest()

            state = RetentionState()
            state.reviews = []
            app = application(state, ledger)

            # 1. Reproduce TaskAlong #41 pre-archive formatter failure BEFORE write:
            orig_publish = ReviewPublisher.publish

            def pre_archive_formatter(publisher: ReviewPublisher, **kwargs: Any) -> Any:
                kwargs["narrative_archive"] = None
                return orig_publish(publisher, **kwargs)

            with (
                patch.object(ReviewPublisher, "publish", pre_archive_formatter),
                self.assertRaises(GitHubPublicationError),
            ):
                publish(
                    app,
                    result,
                    None,
                    options=GitHubWriteOptions(
                        auto_review=True,
                        github_writes=True,
                    ),
                )

            # Zero native review writes occurred and ledger recorded `publication_failed`:
            self.assertEqual(state.reviews, [])
            failed_loaded = ledger.load(IDENTITY)
            assert failed_loaded.record is not None
            assert failed_loaded.record.transaction is not None
            self.assertEqual(
                failed_loaded.record.transaction.phase, "publication_failed"
            )

            # Original ReviewResult and ReviewConvergencePolicy remain 100% immutable:
            self.assertEqual(result.to_dict(), frozen_result_dict)
            self.assertEqual(result.content_digest(), frozen_result_digest)
            self.assertEqual(POLICY.to_dict(), frozen_policy_dict)
            self.assertEqual(POLICY.digest(), frozen_policy_digest)

            # 2. Confirm retained original narrative parts under shared read budget
            #    and compare exact historical vs archive-backed formatter outputs:
            if not state.parts:
                retain_review_result(
                    http=state.http,
                    token="fixture",
                    result=result,
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    app_slug=APP,
                    ledger=ledger,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                    policy=POLICY,
                )
            read_budget = EvidenceReadBudget()
            confirmed_archive = confirm_retained_narrative(
                http=state.http,
                token="fixture",
                result=result,
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                ledger=ledger,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
                policy=POLICY,
                budget=read_budget,
            )
            self.assertLessEqual(read_budget.calls, ORDINARY_EVIDENCE_READ_LIMIT)

            comparison = compare_publication_formatter_outputs(
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                result=result,
                diff=FULL_DIFF,
                policy=POLICY,
                narrative_archive=confirmed_archive,
            )
            self.assertIsInstance(comparison, FormatterComparisonReport)
            self.assertFalse(comparison.authority)
            self.assertEqual(comparison.to_dict()["authority"], False)
            self.assertEqual(
                comparison.review_body_byte_ceiling, MAX_PUBLISHED_REVIEW_BODY_BYTES
            )
            self.assertEqual(comparison.raw_candidate_count, 80)
            self.assertEqual(comparison.retained_review_status, "partial")
            self.assertTrue(comparison.historical_exceeded_ceiling)
            self.assertEqual(comparison.historical_failure_check, "framed-review-body")
            self.assertGreater(
                comparison.historical_uncompacted_frame_bytes,
                MAX_PUBLISHED_REVIEW_BODY_BYTES,
            )
            self.assertIsNone(comparison.historical_framed_body)
            self.assertTrue(comparison.archive_excerpt_fits_ceiling)
            assert comparison.archive_framed_bytes is not None
            assert comparison.archive_framed_body is not None
            assert comparison.bytes_saved_by_excerpt is not None
            self.assertLessEqual(
                comparison.archive_framed_bytes,
                MAX_PUBLISHED_REVIEW_BODY_BYTES - comparison.disposition_reserve_bytes,
            )
            self.assertGreater(comparison.bytes_saved_by_excerpt, 10_000)
            self.assertTrue(comparison.finding_index_identical)
            self.assertTrue(comparison.eligibility_digest_identical)
            self.assertTrue(comparison.finding_explanations_preserved)
            self.assertTrue(comparison.carrier_links_verified)
            self.assertTrue(comparison.result_immutable_verified)
            self.assertTrue(comparison.policy_immutable_verified)

            # 3. Qualify TaskAlong #41 failure-before-write on same head ->
            #    `same_run_attempt2_retained_replay` (with `is_c7_body_limit_qualified=False`!):
            failed_rec = _with_accounting(
                failed_loaded.record, _make_accounting(run_id=4101, limit=512)
            )
            qual_same = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=failed_rec,
                result=result,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=state.reviews,
                app_slug=APP,
                owner_ceased=True,
                formatter_comparison=comparison,
                executor_sha=TASKALONG_41_BE1_EXECUTOR,
                caller_blob=TASKALONG_41_CALLER_BLOB,
            )
            self.assertEqual(
                qual_same.state_code,
                "confirmed_prepublication_overflow_before_write",
            )
            self.assertEqual(qual_same.failure_boundary, "before_write")
            self.assertTrue(qual_same.confirmed_failure_before_write)
            self.assertFalse(qual_same.remote_effect_uncertain)
            self.assertFalse(qual_same.is_c7_body_limit_qualified)
            self.assertEqual(qual_same.caller_allowance_limit, 512)
            self.assertEqual(qual_same.evidence_read_ceiling, MAX_PART_READS)
            self.assertEqual(
                qual_same.primary_route, "same_run_attempt2_retained_replay"
            )
            self.assertFalse(qual_same.authority)

            # Cross-check G03 `classify_recovery_state` agrees on same-head replay:
            g03 = classify_recovery_state(
                failed_rec,
                current_base=BASE,
                current_head=HEAD,
                retained_context_available=True,
            )
            self.assertEqual(
                g03.state_code, "publication_failed_same_head_replay_eligible"
            )
            self.assertEqual(g03.primary_route, "same_run_attempt2_retained_replay")

            # 4. Execute real archive-backed replay on same head without enlarging
            #    any allowance or mutating `result` / `POLICY`:
            replayed = publish(
                app,
                result,
                None,
                options=GitHubWriteOptions(
                    auto_review=True,
                    github_writes=True,
                    complete_inventory=True,
                    operation_entry="enabled",
                ),
            )
            self.assertEqual(replayed.status, "published")
            self.assertEqual(len(state.reviews), 1)
            live_body = state.reviews[0]["body"]
            self.assertLessEqual(
                len(live_body.encode("utf-8")), MAX_PUBLISHED_REVIEW_BODY_BYTES
            )
            live_idx = parse_finding_index(
                live_body, repository_id=1, pull_request=1, head_sha=HEAD
            )
            comp_idx = parse_finding_index(
                comparison.archive_framed_body,
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
            )
            assert live_idx is not None and comp_idx is not None
            self.assertEqual(live_idx.entries, comp_idx.entries)
            self.assertEqual(len(live_idx.entries), 80)
            from review_sensei.human_assessment import PendingHumanReview

            live_elig = approval_eligibility_from_body(
                live_body
            ) or approval_eligibility_from_body(
                live_body,
                human_inventory_reader=lambda _ref: PendingHumanReview.from_result(
                    result, BASE
                ),  # type: ignore[arg-type,return-value]
            )
            assert live_elig is not None
            self.assertEqual(live_elig.result_digest, comparison.result_digest)
            self.assertEqual(result.to_dict(), frozen_result_dict)
            self.assertEqual(POLICY.to_dict(), frozen_policy_dict)

    def test_allowance_preservation_and_be1_executor_non_expansion_controls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            # Construct a review whose required finding explanations ALONE exceed
            # 65,536 bytes when framed + reserved even after narrative compaction:
            huge_findings = tuple(
                replace(c, needs_human=True, blocking=True, severity="high")
                for c in make_body_comments(6, body_placed=True, detail=10_200)
            )
            unfit_raw = ReviewResult(
                summary="Narrative header " * 120,
                comments=huge_findings,
                provider="fixture",
                model="fixture-model",
                review_status="partial",
                coverage=CoverageManifest(
                    enumerated_paths=("src/app.py",),
                    files=(
                        FileCoverage(
                            "src/app.py",
                            "partially-reviewed",
                            "cross-file-relationship",
                        ),
                    ),
                    enumeration_complete=True,
                ),
            )
            prep = prepare_review_transaction(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id="e" * 64,
                base_sha=BASE,
                head_sha=HEAD,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            unfit_result = checkpoint_review_analysis(ledger, IDENTITY, prep, unfit_raw)

            state = RetentionState()
            state.reviews = []
            app = application(state, ledger)
            with self.assertRaises(GitHubPublicationError):
                publish(
                    app,
                    unfit_result,
                    None,
                    options=GitHubWriteOptions(
                        auto_review=True,
                        github_writes=True,
                        complete_inventory=True,
                        operation_entry="enabled",
                    ),
                )
            self.assertEqual(state.reviews, [])
            if not state.parts:
                retain_review_result(
                    http=state.http,
                    token="fixture",
                    result=unfit_result,
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    app_slug=APP,
                    ledger=ledger,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                    policy=POLICY,
                )
            confirmed_archive = confirm_retained_narrative(
                http=state.http,
                token="fixture",
                result=unfit_result,
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                ledger=ledger,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
                policy=POLICY,
                budget=EvidenceReadBudget(),
            )

            # 1. Both live ReviewPublisher AND formatter comparison refuse without
            #    enlarging `65,536` or truncating required finding prose:
            comp_unfit = compare_publication_formatter_outputs(
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                result=unfit_result,
                diff=FULL_DIFF,
                policy=POLICY,
                narrative_archive=confirmed_archive,
            )
            self.assertTrue(comp_unfit.historical_exceeded_ceiling)
            self.assertFalse(comp_unfit.archive_excerpt_fits_ceiling)
            self.assertIsNotNone(comp_unfit.archive_failure_check)
            self.assertIsNone(comp_unfit.archive_framed_body)

            failed_rec = _with_accounting(
                ledger.load(IDENTITY).record, _make_accounting(run_id=4102, limit=512)
            )
            # Even on same head, when archive_excerpt_fits_ceiling is False,
            # qualification refuses same_run_attempt2_retained_replay and routes
            # to controlled_serialized_restart!
            qual_unfit = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=failed_rec,
                result=unfit_result,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
                formatter_comparison=comp_unfit,
                executor_sha=TASKALONG_41_BE1_EXECUTOR,
                caller_blob=TASKALONG_41_CALLER_BLOB,
            )
            self.assertEqual(qual_unfit.primary_route, "controlled_serialized_restart")

            # 2. Prove `be1` executor is rejected by `c7` `normalize_tail_contract`
            #    and never conflated with `c7` on superseded heads:
            _, scope, _, _, _ = contract_fixture()
            be1_manifest = copy.deepcopy(scope["published_tail"])
            be1_manifest.update(
                {
                    "schema_version": BODY_CONTRACT,
                    "executor_sha": TASKALONG_41_BE1_EXECUTOR,
                    "caller_blob": BODY_CALLER_BLOB,
                    "executions": [
                        {
                            **be1_manifest["executions"][0],
                            "route": "failed-review",
                            "conclusion": "failure",
                        }
                    ],
                }
            )
            with self.assertRaisesRegex(
                ReviewInputError, "execution contract is unsupported"
            ):
                normalize_tail_contract(be1_manifest)

            # Contrast superseded head:
            # - `be1` -> `controlled_serialized_restart` even when archive fits
            # - `c7` with unfit archive -> fails closed to `controlled_serialized_restart` (RS-E537F9)
            # - `c7` with fitting archive -> `quarantine_prepublication_isolation`
            comp_fit = replace(
                comp_unfit,
                archive_excerpt_fits_ceiling=True,
                archive_failure_check=None,
                archive_framed_body="archived-excerpt-body",
                archive_framed_bytes=4096,
                bytes_saved_by_excerpt=62_000,
                finding_index_identical=True,
                eligibility_digest_identical=True,
                finding_explanations_preserved=True,
                carrier_links_verified=True,
            )
            qual_be1_new_head = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=failed_rec,
                result=unfit_result,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha="f" * 40,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
                formatter_comparison=comp_fit,
                executor_sha=TASKALONG_41_BE1_EXECUTOR,
                caller_blob=TASKALONG_41_CALLER_BLOB,
            )
            self.assertFalse(qual_be1_new_head.is_c7_body_limit_qualified)
            self.assertEqual(
                qual_be1_new_head.primary_route, "controlled_serialized_restart"
            )

            qual_c7_new_head_unfit = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=failed_rec,
                result=unfit_result,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha="f" * 40,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
                formatter_comparison=comp_unfit,
                executor_sha=EXECUTOR,
                caller_blob=BODY_CALLER_BLOB,
            )
            self.assertTrue(qual_c7_new_head_unfit.is_c7_body_limit_qualified)
            self.assertEqual(
                qual_c7_new_head_unfit.primary_route, "controlled_serialized_restart"
            )

            qual_c7_new_head = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=failed_rec,
                result=unfit_result,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha="f" * 40,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
                formatter_comparison=comp_fit,
                executor_sha=EXECUTOR,
                caller_blob=BODY_CALLER_BLOB,
            )
            self.assertTrue(qual_c7_new_head.is_c7_body_limit_qualified)
            self.assertEqual(
                qual_c7_new_head.primary_route, "quarantine_prepublication_isolation"
            )

            # 3. Descriptor builder strictly rejects enlarged caller API limit or
            #    enlarged 65,536-byte review body ceiling:
            raw_comment = render_session_comment(
                repository_id=1, pull_request=1, record=failed_rec
            ).encode("utf-8")
            old_ref = {
                "id": 77,
                "bytes": len(raw_comment),
                "sha256": hashlib.sha256(raw_comment).hexdigest(),
            }
            for bad_kwargs, pattern in (
                ({"requested_caller_limit": 1024}, "caller API allowance"),
                ({"requested_body_ceiling": 131_072}, "65536"),
            ):
                with self.assertRaisesRegex(ReviewInputError, pattern):
                    build_overflow_recovery_descriptor(
                        identity=IDENTITY,
                        record=failed_rec,
                        qualification=qual_be1_new_head,
                        old_comments=[old_ref],
                        designated_run_id="9100",
                        base_sha=BASE,
                        head_sha="f" * 40,
                        policy_commit="c" * 40,
                        workflow_sha="a" * 40,
                        issuance_disabled_at=1000,
                        retire_after=4600,
                        **bad_kwargs,  # type: ignore[arg-type]
                    )

    def test_failure_before_write_vs_response_loss_controls_and_echo_reconciliation(
        self,
    ) -> None:
        # Subcase A1: Process crash / response-loss mid-POST (`KeyboardInterrupt`
        # during `POST /pulls/1/reviews` before GitHub persists review) leaves
        # `tx.phase == "publication_pending"` -> `response_loss_unobserved_remote_hold`!
        with tempfile.TemporaryDirectory() as dir_a1:
            ledger_a1 = LocalSessionLedger(Path(dir_a1))
            res_a1 = _build_taskalong_41_result(
                ledger_a1, narrative_bytes=4_000, candidate_count=6, human_hold_count=2
            )
            state_a1 = RetentionState()
            state_a1.reviews = []
            orig_open_a1 = state_a1.open

            def crash_mid_post(req: Any, timeout: float) -> Any:
                path = urlparse(req.full_url).path
                if req.method == "POST" and path.endswith("/pulls/1/reviews"):
                    raise KeyboardInterrupt("runner terminated mid-POST")
                return orig_open_a1(req, timeout)

            state_a1.http.opener = crash_mid_post
            app_a1 = application(state_a1, ledger_a1)
            with self.assertRaises(KeyboardInterrupt):
                publish(
                    app_a1,
                    res_a1,
                    None,
                    options=GitHubWriteOptions(
                        auto_review=True,
                        github_writes=True,
                        operation_entry="disabled",
                    ),
                )
            rec_a1 = ledger_a1.load(IDENTITY).record
            assert rec_a1 is not None and rec_a1.transaction is not None
            self.assertEqual(rec_a1.transaction.phase, "publication_pending")
            self.assertEqual(state_a1.reviews, [])
            qual_pending = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=_with_accounting(
                    rec_a1, _make_accounting(run_id=4200, uncertain=1)
                ),
                result=res_a1,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=state_a1.reviews,
                app_slug=APP,
                owner_ceased=True,
                executor_sha=EXECUTOR,
                caller_blob=BODY_CALLER_BLOB,
            )
            self.assertEqual(
                qual_pending.state_code, "response_loss_unobserved_remote_hold"
            )
            self.assertTrue(qual_pending.remote_effect_uncertain)
            self.assertFalse(qual_pending.confirmed_failure_before_write)

        # Subcase A2: Socket reset (`GitHubHTTPTransientError`) on `POST /pulls/1/reviews`
        # marks `tx.phase == "publication_failed"` while `uncertain == 1` in caller
        # accounting -> STILL fails closed into `response_loss_unobserved_remote_hold`!
        with tempfile.TemporaryDirectory() as dir_a:
            ledger_a = LocalSessionLedger(Path(dir_a))
            res_a = _build_taskalong_41_result(
                ledger_a, narrative_bytes=4_000, candidate_count=6, human_hold_count=2
            )
            state_a = RetentionState()
            state_a.reviews = []
            orig_open_a = state_a.open

            def drop_before_persist(req: Any, timeout: float) -> Any:
                path = urlparse(req.full_url).path
                if req.method == "POST" and path.endswith("/pulls/1/reviews"):
                    raise GitHubHTTPTransientError(
                        "connection reset while awaiting POST /pulls/1/reviews response"
                    )
                return orig_open_a(req, timeout)

            state_a.http.opener = drop_before_persist
            app_a = application(state_a, ledger_a)
            with self.assertRaises(GitHubPublicationError):
                publish(
                    app_a,
                    res_a,
                    None,
                    options=GitHubWriteOptions(
                        auto_review=True,
                        github_writes=True,
                    ),
                )

            rec_a = ledger_a.load(IDENTITY).record
            assert rec_a is not None and rec_a.transaction is not None
            self.assertEqual(rec_a.transaction.phase, "publication_failed")
            self.assertEqual(state_a.reviews, [])

            acct_a = _with_accounting(rec_a, _make_accounting(run_id=4201, uncertain=1))
            qual_loss_no_echo = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=acct_a,
                result=res_a,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=state_a.reviews,
                app_slug=APP,
                owner_ceased=True,
                executor_sha=EXECUTOR,
                caller_blob=BODY_CALLER_BLOB,
            )
            self.assertEqual(
                qual_loss_no_echo.state_code, "response_loss_unobserved_remote_hold"
            )
            self.assertEqual(
                qual_loss_no_echo.failure_boundary, "response_loss_uncertain"
            )
            self.assertTrue(qual_loss_no_echo.remote_effect_uncertain)
            self.assertFalse(qual_loss_no_echo.confirmed_failure_before_write)
            self.assertFalse(qual_loss_no_echo.is_c7_body_limit_qualified)
            self.assertEqual(
                qual_loss_no_echo.primary_route, "controlled_serialized_restart"
            )

        # Subcase B: Response-loss WITH persisted remote echo (`POST /pulls/1/reviews`
        # persists review id=40 on GitHub, then drops HTTP response) ->
        # `response_loss_echo_observed_reconcilable` + zero duplicate review POST on reconcile!
        with tempfile.TemporaryDirectory() as dir_b:
            ledger_b = LocalSessionLedger(Path(dir_b))
            res_b = _build_taskalong_41_result(
                ledger_b, narrative_bytes=4_000, candidate_count=6, human_hold_count=2
            )
            state_b = RetentionState()
            state_b.reviews = []
            orig_open_b = state_b.open
            drop_once = {"armed": True}
            post_review_attempts = {"count": 0}

            def drop_after_persist(req: Any, timeout: float) -> Any:
                path = urlparse(req.full_url).path
                if req.method == "POST" and path.endswith("/pulls/1/reviews"):
                    post_review_attempts["count"] += 1
                    resp = orig_open_b(req, timeout)
                    if drop_once["armed"]:
                        drop_once["armed"] = False
                        raise KeyboardInterrupt(
                            "lost HTTP response after GitHub persisted review id=40"
                        )
                    return resp
                return orig_open_b(req, timeout)

            state_b.http.opener = drop_after_persist
            app_b = application(state_b, ledger_b)
            with self.assertRaises(KeyboardInterrupt):
                publish(
                    app_b,
                    res_b,
                    None,
                    options=GitHubWriteOptions(
                        auto_review=True,
                        github_writes=True,
                    ),
                )

            rec_b = ledger_b.load(IDENTITY).record
            assert rec_b is not None and rec_b.transaction is not None
            self.assertIn(
                rec_b.transaction.phase, {"publication_pending", "publication_failed"}
            )
            self.assertEqual(len(state_b.reviews), 1)
            self.assertEqual(post_review_attempts["count"], 1)

            acct_b = _with_accounting(rec_b, _make_accounting(run_id=4202, uncertain=0))
            qual_echo = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=acct_b,
                result=res_b,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=state_b.reviews,
                app_slug=APP,
                owner_ceased=True,
            )
            self.assertEqual(
                qual_echo.state_code, "response_loss_echo_observed_reconcilable"
            )
            self.assertEqual(qual_echo.failure_boundary, "response_loss_committed_echo")
            self.assertEqual(qual_echo.observed_review_id, 40)
            self.assertFalse(qual_echo.confirmed_failure_before_write)
            self.assertEqual(qual_echo.primary_route, "reconcile_observed_exact_review")
            self.assertEqual(qual_echo.fallback_routes, ())

            # RS-9F052C & RS-A97A39: `[bot]`-suffixed `app_slug` normalizes cleanly,
            # and observed echo under drifted head holds (`reconcile_observed_review_hold`)
            # and refuses blind controlled-restart descriptor issuance!
            qual_echo_drifted = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=acct_b,
                result=res_b,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha="f" * 40,
                live_reviews=state_b.reviews,
                app_slug=f"{APP}[bot]",
                owner_ceased=True,
            )
            self.assertEqual(
                qual_echo_drifted.state_code,
                "response_loss_echo_observed_reconcilable",
            )
            self.assertEqual(qual_echo_drifted.observed_review_id, 40)
            self.assertEqual(
                qual_echo_drifted.primary_route, "reconcile_observed_review_hold"
            )
            self.assertEqual(qual_echo_drifted.fallback_routes, ())
            echo_comment = render_session_comment(
                repository_id=1, pull_request=1, record=acct_b
            ).encode("utf-8")
            with self.assertRaisesRegex(
                ReviewInputError, "exact App review echo is already persisted"
            ):
                build_overflow_recovery_descriptor(
                    identity=IDENTITY,
                    record=acct_b,
                    qualification=qual_echo_drifted,
                    old_comments=[
                        {
                            "id": 40,
                            "bytes": len(echo_comment),
                            "sha256": hashlib.sha256(echo_comment).hexdigest(),
                        }
                    ],
                    designated_run_id="5001",
                    base_sha=BASE,
                    head_sha="f" * 40,
                    policy_commit=BASE,
                    workflow_sha=EXECUTOR,
                    issuance_disabled_at=100,
                    retire_after=200,
                )

            # Reconcile on same head: reuses existing review id=40 with ZERO new
            # `POST /pulls/1/reviews` writes and settles `publication_succeeded`!
            reconciled = publish(
                app_b,
                res_b,
                None,
                options=GitHubWriteOptions(
                    auto_review=True,
                    github_writes=True,
                    complete_inventory=True,
                    operation_entry="enabled",
                ),
            )
            self.assertEqual(reconciled.status, "already_published")
            self.assertEqual(state_b.reviews[0]["id"], 40)
            self.assertEqual(post_review_attempts["count"], 1)
            self.assertEqual(len(state_b.reviews), 1)
            settled_rec = ledger_b.load(IDENTITY).record
            assert settled_rec is not None and settled_rec.transaction is not None
            self.assertEqual(settled_rec.transaction.phase, "publication_succeeded")

    def test_end_to_end_bounded_recovery_for_taskalong_41_and_response_loss_via_restart(
        self,
    ) -> None:
        from test_review_transaction import HostedAnalysisCliTests

        # Prove Universal Review Recovery: a PR wedged in `publication_failed`
        # (TaskAlong #41 `be1` prepublication overflow) recovers end-to-end via
        # `build_overflow_recovery_descriptor` + `ControlledRestart` without
        # enlarging any byte or caller API allowance!
        repo_id_7 = SessionIdentity("owner/repo", 7, repository_id=123)
        with tempfile.TemporaryDirectory() as fail_dir:
            bf = EntryFixture(fail_dir)
            bf.run_id = 4141

            def simulate_be1_overflow(publisher: ReviewPublisher, **kwargs: Any) -> Any:
                raise GitHubPublicationError("framed review body exceeds 65536 bytes")

            with patch.object(ReviewPublisher, "publish", simulate_be1_overflow):
                self.assertEqual(bf.invoke(), 1)

            wedged_rec = parse_session_comment(bf.comment["body"], identity=repo_id_7)
            assert wedged_rec.transaction is not None
            self.assertEqual(wedged_rec.transaction.phase, "publication_failed")
            assert wedged_rec.api_run_accounting is not None
            self.assertIsNone(wedged_rec.api_run_accounting["terminal_transaction"])
            self.assertEqual(
                wedged_rec.api_run_accounting["configuration"]["limit"], 512
            )

            # Use a local checkpoint result bound to wedged_rec's exact digests:
            sample_result = ReviewResult(
                summary="TaskAlong #41 overflow narrative.",
                comments=(),
                provider="fixture",
                model="fixture-model",
                review_status="complete",
                transaction=replace(
                    wedged_rec.transaction, phase="analysis", result_sha256=None
                ),
            )
            matched_tx = replace(
                wedged_rec.transaction,
                result_sha256=sample_result.content_digest(),
                policy_digest=POLICY.digest(),
            )
            matched_rec = SessionRecord._construct(
                repository="owner/repo",
                pull_request=7,
                repository_id=123,
                completed_initial_reviews=wedged_rec.completed_initial_reviews,
                completed_verification_rounds=wedged_rec.completed_verification_rounds,
                failed_attempts=wedged_rec.failed_attempts,
                generation=wedged_rec.generation,
                reservation_id=wedged_rec.reservation_id,
                reserved_slot=wedged_rec.reserved_slot,
                last_committed_reservation_id=wedged_rec.last_committed_reservation_id,
                created_at=wedged_rec.created_at,
                updated_at=wedged_rec.updated_at,
                expires_at=wedged_rec.expires_at,
                failed_attempts_head_sha=wedged_rec.failed_attempts_head_sha,
                transaction=matched_tx,
                api_run_accounting=wedged_rec.api_run_accounting,
            )

            qual = qualify_nonterminal_publication_failure(
                identity=repo_id_7,
                record=matched_rec,
                result=sample_result,
                policy=POLICY,
                current_base_sha=bf.requested_base,
                current_head_sha="e" * 40,
                live_reviews=[],
                app_slug="review-sensei",
                owner_ceased=True,
                executor_sha=TASKALONG_41_BE1_EXECUTOR,
                caller_blob=TASKALONG_41_CALLER_BLOB,
            )
            self.assertEqual(qual.primary_route, "controlled_serialized_restart")
            self.assertFalse(qual.is_c7_body_limit_qualified)

            old_comment = copy.deepcopy(bf.comment)
            old_comment["id"] = 77
            old_raw = old_comment["body"].encode("utf-8")
            old_ref = {
                "id": old_comment["id"],
                "bytes": len(old_raw),
                "sha256": hashlib.sha256(old_raw).hexdigest(),
            }
            old_run_id = bf.run_id

            with tempfile.TemporaryDirectory() as restart_dir:
                rf = EntryFixture(restart_dir)
                rf.run_id = 8100
                rf.event_name = "workflow_dispatch"
                descriptor = build_overflow_recovery_descriptor(
                    identity=repo_id_7,
                    record=wedged_rec,
                    qualification=qual,
                    old_comments=[old_ref],
                    designated_run_id=str(rf.run_id),
                    base_sha=rf.requested_base,
                    head_sha=rf.requested_head,
                    policy_commit=rf.policy,
                    workflow_sha="a" * 40,
                    issuance_disabled_at=1000,
                    retire_after=4600,
                )
                self.assertIsInstance(descriptor, PublicationOverflowRecoveryDescriptor)
                self.assertFalse(descriptor.authority)
                self.assertEqual(
                    descriptor.classifier_id, PUBLICATION_OVERFLOW_CLASSIFIER_ID
                )
                self.assertEqual(descriptor.caller_allowance_limit, 512)
                self.assertEqual(
                    descriptor.review_body_byte_ceiling, MAX_PUBLISHED_REVIEW_BODY_BYTES
                )
                self.assertFalse(descriptor.is_c7_body_limit_qualified)
                self.assertEqual(descriptor.to_dict()["authority"], False)
                self.assertEqual(
                    descriptor.registry_document()["schema_version"],
                    "controlled-restart-registry-v1",
                )

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
                                        "id": 9901,
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
                        row_data["request_id"],
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
                self.assertEqual(archived["comments"][0]["body"], old_comment["body"])

    def test_adversarial_invariants_active_owner_and_guard_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            small_res = _build_taskalong_41_result(
                ledger, narrative_bytes=500, candidate_count=2, human_hold_count=0
            )
            rec = _with_accounting(
                ledger.load(IDENTITY).record, _make_accounting(run_id=4301, limit=512)
            )
            # Advance tx to `publication_failed` for non-overflow test:
            assert rec.transaction is not None
            failed_tx = replace(rec.transaction, phase="publication_failed")
            failed_rec = SessionRecord._construct(
                repository=rec.repository,
                pull_request=rec.pull_request,
                repository_id=rec.repository_id,
                completed_initial_reviews=0,
                completed_verification_rounds=0,
                failed_attempts=0,
                generation=rec.generation,
                reservation_id=None,
                reserved_slot=None,
                last_committed_reservation_id=failed_tx.reservation_id,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
                expires_at=rec.expires_at,
                transaction=failed_tx,
                api_run_accounting=rec.api_run_accounting,
            )

            # 1. Small review fits historical formatter without needing archive:
            comp_fits = compare_publication_formatter_outputs(
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                result=small_res,
                diff=FULL_DIFF,
                policy=POLICY,
                narrative_archive=None,
            )
            self.assertFalse(comp_fits.historical_exceeded_ceiling)
            self.assertIsNotNone(comp_fits.historical_framed_body)

            # 2. Active predecessor owner (`owner_ceased=False`) holds all recovery
            #    and blocks descriptor issuance:
            qual_active = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=failed_rec,
                result=small_res,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=False,
                formatter_comparison=comp_fits,
            )
            self.assertEqual(qual_active.state_code, "predecessor_owner_active_hold")
            self.assertEqual(qual_active.primary_route, "wait_for_owner_cessation")
            self.assertEqual(qual_active.to_dict()["authority"], False)
            with self.assertRaisesRegex(ReviewInputError, "owner is active"):
                build_overflow_recovery_descriptor(
                    identity=IDENTITY,
                    record=failed_rec,
                    qualification=qual_active,
                    old_comments=[],
                    designated_run_id="9200",
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_commit="c" * 40,
                    workflow_sha="a" * 40,
                    issuance_disabled_at=100,
                    retire_after=200,
                )

            # 3. Foreign review author (`type="User"` or wrong login) is ignored by
            #    exact-review echo lookup; non-overflow failure-before-write classifies
            #    as `confirmed_prepublication_failure_before_write`:
            foreign_reviews: list[Any] = [
                "not-a-dict",
                {"id": 10, "commit_id": "0" * 40},
                {
                    "id": 11,
                    "commit_id": HEAD,
                    "user": {"login": APP, "type": "User"},
                    "body": review_marker(
                        repository_id=1, pull_request=1, head_sha=HEAD, result=small_res
                    ),
                },
                {
                    "id": 12,
                    "commit_id": HEAD,
                    "user": {"login": "impostor-bot", "type": "Bot"},
                    "body": review_marker(
                        repository_id=1, pull_request=1, head_sha=HEAD, result=small_res
                    ),
                },
            ]
            qual_non_overflow = qualify_nonterminal_publication_failure(
                identity=IDENTITY,
                record=failed_rec,
                result=small_res,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=foreign_reviews,
                app_slug=APP,
                owner_ceased=True,
                formatter_comparison=comp_fits,
            )
            self.assertEqual(
                qual_non_overflow.state_code,
                "confirmed_prepublication_failure_before_write",
            )

            # 4. Digest mismatch & authority=True negative controls fail closed:
            other_policy = ReviewConvergencePolicy(
                mode="merge-focused", max_failed_attempts=3
            )
            with self.assertRaisesRegex(ReviewInputError, "policy_digest"):
                qualify_nonterminal_publication_failure(
                    identity=IDENTITY,
                    record=failed_rec,
                    result=small_res,
                    policy=other_policy,
                    current_base_sha=BASE,
                    current_head_sha=HEAD,
                    live_reviews=[],
                    app_slug=APP,
                    owner_ceased=True,
                )
            with self.assertRaises(ReviewInputError):
                replace(comp_fits, authority=True)
            with self.assertRaises(ReviewInputError):
                replace(qual_non_overflow, authority=True)
            with self.assertRaises(ReviewInputError):
                replace(
                    qual_non_overflow,
                    confirmed_failure_before_write=True,
                    remote_effect_uncertain=True,
                )
            with self.assertRaises(ReviewInputError):
                PublicationOverflowRecoveryDescriptor(
                    classifier_id=PUBLICATION_OVERFLOW_CLASSIFIER_ID,
                    qualification_state="confirmed_prepublication_failure_before_write",
                    failure_boundary="before_write",
                    predecessor_run_id=1,
                    predecessor_root_sha256="1" * 64,
                    result_content_digest="2" * 64,
                    policy_digest="3" * 64,
                    caller_allowance_limit=512,
                    review_body_byte_ceiling=MAX_PUBLISHED_REVIEW_BODY_BYTES,
                    retained_nonstartup_caller_liability=1,
                    retained_total_caller_liability=5,
                    is_c7_body_limit_qualified=False,
                    controlled_restart_registration={"request_id": "a" * 64},
                    operator_instructions="ok",
                    authority=True,
                )
            with self.assertRaises(ReviewInputError):
                compare_publication_formatter_outputs(
                    identity=SessionIdentity("owner/repo", 1, repository_id=None),
                    base_sha=BASE,
                    head_sha=HEAD,
                    result=small_res,
                    diff=FULL_DIFF,
                    policy=POLICY,
                )
            with self.assertRaises(ReviewInputError):
                compare_publication_formatter_outputs(
                    identity=IDENTITY,
                    base_sha="bad",
                    head_sha=HEAD,
                    result=small_res,
                    diff=FULL_DIFF,
                    policy=POLICY,
                )
            with self.assertRaises(ReviewInputError):
                compare_publication_formatter_outputs(
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    result=small_res,
                    diff=FULL_DIFF,
                    policy=ReviewConvergencePolicy(mode="advisory"),
                )
            big_summary_res = replace(small_res, summary="N" * 2048, transaction=None)
            with self.assertRaises(ReviewInputError):
                compare_publication_formatter_outputs(
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    result=big_summary_res,
                    diff=FULL_DIFF,
                    policy=POLICY,
                    narrative_archive=object(),  # type: ignore[arg-type]
                )
            with self.assertRaises(ReviewInputError):
                qualify_nonterminal_publication_failure(
                    identity=SessionIdentity("owner/repo", 1, repository_id=None),
                    record=failed_rec,
                    result=small_res,
                    policy=POLICY,
                    current_base_sha=BASE,
                    current_head_sha=HEAD,
                    live_reviews=[],
                    app_slug=APP,
                    owner_ceased=True,
                )
            with self.assertRaises(ReviewInputError):
                qualify_nonterminal_publication_failure(
                    identity=IDENTITY,
                    record=ledger.load(IDENTITY).record,  # type: ignore[arg-type]
                    result=small_res,
                    policy=POLICY,
                    current_base_sha=BASE,
                    current_head_sha=HEAD,
                    live_reviews=[],
                    app_slug=APP,
                    owner_ceased=True,
                )
            with self.assertRaises(ReviewInputError):
                qualify_nonterminal_publication_failure(
                    identity=IDENTITY,
                    record=failed_rec,
                    result=replace(small_res, summary="tampered", transaction=None),
                    policy=POLICY,
                    current_base_sha=BASE,
                    current_head_sha=HEAD,
                    live_reviews=[],
                    app_slug=APP,
                    owner_ceased=True,
                )
            raw_c = render_session_comment(
                repository_id=1, pull_request=1, record=failed_rec
            ).encode("utf-8")
            old_ref = {
                "id": 77,
                "bytes": len(raw_c),
                "sha256": hashlib.sha256(raw_c).hexdigest(),
            }
            desc_extra = build_overflow_recovery_descriptor(
                identity=IDENTITY,
                record=failed_rec,
                qualification=qual_non_overflow,
                designated_run_id="9900",
                base_sha=BASE,
                head_sha="f" * 40,
                policy_commit="c" * 40,
                workflow_sha="a" * 40,
                issuance_disabled_at=1000,
                retire_after=4600,
                old_comments=(old_ref,),
                extra_old_runs=(4099,),
            )
            self.assertEqual(
                desc_extra.controlled_restart_registration["old_runs"], [4301, 4099]
            )
            self.assertEqual(desc_extra.to_dict()["authority"], False)
            with self.assertRaises(ReviewInputError):
                build_overflow_recovery_descriptor(
                    identity=IDENTITY,
                    record=failed_rec,
                    qualification=qual_non_overflow,
                    designated_run_id="9901",
                    base_sha=BASE,
                    head_sha="f" * 40,
                    policy_commit="c" * 40,
                    workflow_sha="a" * 40,
                    issuance_disabled_at=1000,
                    retire_after=4600,
                    old_comments=(old_ref,),
                    extra_old_runs=(0,),
                )

    def test_taskalong_41_attempt_evidence_packet_and_boundary_separation(
        self,
    ) -> None:
        taskalong_id = SessionIdentity(
            repository="org/taskalong",
            pull_request=TASKALONG_41_PULL_REQUEST,
            repository_id=4100,
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            # Prepare TaskAlong #41 directly under taskalong_id:
            narrative = (
                "TaskAlong #41 architectural synthesis and cross-module boundary overview. "
                * (19_000 // 74 + 1)
            )[:19_000]
            raw_comments = tuple(
                ReviewComment(
                    path="src/app.py",
                    side="FILE",
                    line=None,
                    body=(
                        f"Historical candidate {idx:02d}: "
                        + ("bounded invariant explanation " * 11)
                    ),
                    symbol=f"handler_{idx:02d}",
                    defect_kind=f"contract-{idx:02d}",
                    severity="high" if idx < 23 else "low",
                    blocking=False,
                    needs_human=idx < 23,
                )
                for idx in range(80)
            )
            raw_res = ReviewResult(
                summary=narrative,
                comments=raw_comments,
                provider="fixture",
                model="fixture-model",
                review_status="partial",
                coverage=CoverageManifest(
                    enumerated_paths=("src/app.py",),
                    files=(
                        FileCoverage(
                            "src/app.py",
                            "partially-reviewed",
                            "cross-file-relationship",
                        ),
                    ),
                    enumeration_complete=True,
                ),
            )
            prepared = prepare_review_transaction(
                ledger,
                taskalong_id,
                POLICY,
                reservation_id="d" * 64,
                base_sha=BASE,
                head_sha=HEAD,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            result_41 = checkpoint_review_analysis(
                ledger, taskalong_id, prepared, raw_res
            )
            loaded_41 = ledger.load(taskalong_id)
            assert loaded_41.record is not None
            assert loaded_41.record.transaction is not None

            state = RetentionState()
            state.reviews = []
            # Re-bind state opener to serve org/taskalong PR #41 issue comments:
            orig_opener = state.http.opener

            def _rebind_comment(obj: Any) -> Any:
                if isinstance(obj, dict):
                    out = dict(obj)
                    if out.get("issue_url") == (
                        "https://api.github.test/repos/owner/repo/issues/1"
                    ):
                        out["issue_url"] = (
                            "https://api.github.test/repos/org/taskalong/issues/41"
                        )
                    if isinstance(out.get("base"), dict) and isinstance(
                        out["base"].get("repo"), dict
                    ):
                        out["base"] = {
                            **out["base"],
                            "repo": {
                                **out["base"]["repo"],
                                "id": 4100,
                                "full_name": "org/taskalong",
                            },
                        }
                    if isinstance(out.get("head"), dict) and isinstance(
                        out["head"].get("repo"), dict
                    ):
                        out["head"] = {
                            **out["head"],
                            "repo": {
                                **out["head"]["repo"],
                                "full_name": "org/taskalong",
                            },
                        }
                    return out
                if isinstance(obj, list):
                    return [_rebind_comment(x) for x in obj]
                return obj

            def taskalong_opener(request: Any, timeout: float | None = None) -> Any:
                rewritten_url = (
                    request.full_url.replace(
                        "/repos/org/taskalong/", "/repos/owner/repo/"
                    )
                    .replace("/issues/41/", "/issues/1/")
                    .replace("/pulls/41", "/pulls/1")
                )
                import urllib.request

                re_req = urllib.request.Request(
                    rewritten_url,
                    data=request.data,
                    headers=dict(request.header_items()),
                    method=request.get_method(),
                )
                resp = orig_opener(re_req, timeout)
                return json_response(
                    _rebind_comment(json.loads(resp.body)), resp.status
                )

            from review_sensei_github.http import GitHubHttp

            ta_http = GitHubHttp(
                api_url="https://api.github.test", opener=taskalong_opener
            )
            retain_review_result(
                http=ta_http,
                token="fixture",
                result=result_41,
                identity=taskalong_id,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                ledger=ledger,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
                policy=POLICY,
            )
            archive_41 = confirm_retained_narrative(
                http=ta_http,
                token="fixture",
                result=result_41,
                identity=taskalong_id,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                ledger=ledger,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
                policy=POLICY,
                budget=EvidenceReadBudget(),
            )

            failed_tx = replace(
                loaded_41.record.transaction, phase="publication_failed"
            )
            acc_clean = ApiRunAccounting.from_document(
                {
                    **_make_accounting(run_id=4101).to_document(),
                    "identity": {"repository_id": 4100, "run_id": 4101},
                }
            )
            rec_41 = _with_accounting(
                SessionRecord._construct(
                    repository=taskalong_id.repository,
                    pull_request=taskalong_id.pull_request,
                    repository_id=taskalong_id.repository_id,
                    completed_initial_reviews=0,
                    completed_verification_rounds=0,
                    failed_attempts=1,
                    generation=1,
                    reservation_id=None,
                    reserved_slot=None,
                    last_committed_reservation_id="d" * 64,
                    created_at="2026-10-20T09:00:00Z",
                    updated_at="2026-10-20T09:14:00Z",
                    expires_at="2026-11-20T09:00:00Z",
                    transaction=failed_tx,
                ),
                acc_clean,
            )

            pkt_41 = build_publication_overflow_attempt_packet(
                identity=taskalong_id,
                record=rec_41,
                result=result_41,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha="f" * 40,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
                diff=FULL_DIFF,
                narrative_archive=archive_41,
                executor_sha=TASKALONG_41_BE1_EXECUTOR,
                caller_blob=TASKALONG_41_CALLER_BLOB,
                installed_core_version="0.8.11",
                installed_hosted_version="0.3.12",
                cessation_job_id=41009,
                cessation_conclusion="failure",
                cessation_completed_at="2026-10-20T09:15:00Z",
            )
            self.assertIsInstance(pkt_41, PublicationOverflowAttemptPacket)
            self.assertEqual(pkt_41.case_id, TASKALONG_41_CASE_ID)
            self.assertFalse(pkt_41.authority)
            self.assertTrue(pkt_41.formatter_fit_is_not_settlement)
            self.assertFalse(pkt_41.historical_settlement_receipt_supplied)
            self.assertEqual(pkt_41.raw_candidate_count, 80)
            self.assertEqual(pkt_41.human_hold_count, 23)
            self.assertEqual(pkt_41.retained_review_status, "partial")
            self.assertEqual(
                pkt_41.review_body_byte_ceiling, MAX_PUBLISHED_REVIEW_BODY_BYTES
            )
            self.assertEqual(pkt_41.caller_allowance_limit, 512)
            self.assertEqual(pkt_41.evidence_read_ceiling, MAX_PART_READS)
            self.assertEqual(pkt_41.ordinary_read_limit, ORDINARY_EVIDENCE_READ_LIMIT)
            self.assertFalse(pkt_41.is_c7_body_limit_qualified)
            self.assertEqual(pkt_41.failure_boundary, "before_write")
            self.assertEqual(
                pkt_41.state_code, "confirmed_prepublication_overflow_before_write"
            )
            assert pkt_41.formatter_comparison is not None
            self.assertTrue(pkt_41.formatter_comparison.historical_exceeded_ceiling)
            self.assertTrue(pkt_41.formatter_comparison.archive_excerpt_fits_ceiling)
            # Fitting today's archive excerpt formatter is NOT live settlement!
            self.assertIn("historical_live_settlement_receipt", pkt_41.missing_proofs)
            self.assertIn("published_native_github_review", pkt_41.missing_proofs)
            self.assertIn("terminal_transaction_settlement", pkt_41.missing_proofs)
            self.assertNotIn("cessation_job_receipt", pkt_41.missing_proofs)
            self.assertNotIn(
                "confirmed_narrative_archive_receipt", pkt_41.missing_proofs
            )
            d41 = pkt_41.to_dict()
            self.assertEqual(d41["schema_version"], PUBLICATION_OVERFLOW_PACKET_SCHEMA)
            self.assertFalse(d41["authority"])
            self.assertTrue(d41["formatter_fit_is_not_settlement"])

            # Boundary 2: Unknown dispatch / lost response (`publication_pending`, no echo):
            pending_tx = replace(failed_tx, phase="publication_pending")
            acc_uncertain = ApiRunAccounting.from_document(
                {
                    **_make_accounting(run_id=4102, uncertain=1).to_document(),
                    "identity": {"repository_id": 4100, "run_id": 4102},
                }
            )
            rec_uncertain = _with_accounting(
                rec_41, acc_uncertain, transaction=pending_tx
            )
            pkt_uncertain = build_publication_overflow_attempt_packet(
                identity=taskalong_id,
                record=rec_uncertain,
                result=result_41,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
            )
            self.assertEqual(pkt_uncertain.failure_boundary, "response_loss_uncertain")
            self.assertIn(
                "remote_review_dispatch_outcome", pkt_uncertain.missing_proofs
            )
            self.assertIn("cessation_job_receipt", pkt_uncertain.missing_proofs)
            self.assertIn(
                "installed_executor_or_caller_provenance", pkt_uncertain.missing_proofs
            )
            self.assertIn(
                "confirmed_narrative_archive_receipt", pkt_uncertain.missing_proofs
            )

            # Boundary 3: Exact observed-effect echo persisted on GitHub:
            echo_marker = review_marker(
                repository_id=4100,
                pull_request=41,
                head_sha=HEAD,
                result=result_41,
            )
            pkt_echo = build_publication_overflow_attempt_packet(
                identity=taskalong_id,
                record=_with_accounting(rec_41, acc_clean, transaction=pending_tx),
                result=result_41,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[
                    {
                        "id": 9001,
                        "commit_id": HEAD,
                        "user": {"login": APP, "type": "Bot"},
                        "body": f"Observed review\n{echo_marker}",
                    }
                ],
                app_slug=APP,
                owner_ceased=True,
                executor_sha=TASKALONG_41_BE1_EXECUTOR,
                caller_blob=TASKALONG_41_CALLER_BLOB,
                cessation_job_id=41010,
                diagnostics_available=True,
            )
            self.assertEqual(pkt_echo.failure_boundary, "response_loss_committed_echo")
            self.assertEqual(pkt_echo.observed_review_id, 9001)
            self.assertIn(
                "reconciled_observed_review_settlement", pkt_echo.missing_proofs
            )
            self.assertNotIn("missing_close_diagnostics", pkt_echo.missing_proofs)

            # Boundary 4: Active / unceased predecessor owner hold:
            pkt_active = build_publication_overflow_attempt_packet(
                identity=taskalong_id,
                record=rec_41,
                result=result_41,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=False,
            )
            self.assertEqual(pkt_active.failure_boundary, "active_owner_unknown")
            self.assertIn("predecessor_owner_cessation", pkt_active.missing_proofs)

    def test_cross_incident_historical_packet_distinctness_and_validation_guards(
        self,
    ) -> None:
        from review_sensei_github.pending_reassessment_recovery import (
            REVIEWSENSEI_408_CASE_ID,
            REVIEWSENSEI_408_PULL_REQUEST,
            REVIEWSENSEI_408_REPOSITORY,
            PendingReceiptObservation,
            build_pending_reassessment_attempt_packet,
        )
        from review_sensei_github.pre_entry_abort_recovery import (
            SITEVAULT_142_CASE_ID,
            SITEVAULT_153_CASE_ID,
            build_no_transaction_attempt_packet,
        )
        from review_sensei_github.recovery_route_advisor import (
            REVIEWSENSEI_443_CASE_ID,
            REVIEWSENSEI_443_GENERATION,
            REVIEWSENSEI_443_PULL_REQUEST,
            REVIEWSENSEI_443_REPOSITORY,
            REVIEWSENSEI_443_RUN_ID,
            build_attempt_evidence_packet,
        )
        from test_human_assessment import HUMAN, prior

        # 1. ReviewSensei #443 gen-11 run 43074015855 (G03):
        acc_443 = ApiRunAccounting.from_document(
            {
                **_make_accounting(run_id=REVIEWSENSEI_443_RUN_ID).to_document(),
                "identity": {
                    "repository_id": 987654,
                    "run_id": REVIEWSENSEI_443_RUN_ID,
                },
            }
        )
        tx_443 = replace(
            ReviewTransaction.create(
                repository=REVIEWSENSEI_443_REPOSITORY,
                pull_request=REVIEWSENSEI_443_PULL_REQUEST,
                base_sha=BASE,
                head_sha=HEAD,
                reservation_id="5" * 64,
                policy_digest=POLICY.digest(),
                configuration_digest=acc_443.configuration.digest,
                evidence_digest="3" * 64,
                generation=REVIEWSENSEI_443_GENERATION,
            ),
            phase="publication_succeeded",
            result_sha256="4" * 64,
        )
        rec_443 = SessionRecord._construct(
            repository=REVIEWSENSEI_443_REPOSITORY,
            pull_request=REVIEWSENSEI_443_PULL_REQUEST,
            repository_id=987654,
            completed_initial_reviews=1,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=REVIEWSENSEI_443_GENERATION,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id="5" * 64,
            created_at="2026-11-10T12:00:00Z",
            updated_at="2026-11-10T12:05:00Z",
            expires_at="2026-12-10T12:00:00Z",
            transaction=tx_443,
            api_run_accounting=acc_443.to_document(),
        )
        pkt_443 = build_attempt_evidence_packet(
            rec_443, current_base=BASE, current_head=HEAD
        )

        # 2. SiteVault #142 & #153 (G29):
        sv_142_id = SessionIdentity("org/sitevault", 142, repository_id=1420)
        sv_142_acc = ApiRunAccounting.from_document(
            {
                **_make_accounting(run_id=14201).to_document(),
                "identity": {"repository_id": 1420, "run_id": 14201},
            }
        )
        sv_142_rec = SessionRecord._construct(
            repository="org/sitevault",
            pull_request=142,
            repository_id=1420,
            completed_initial_reviews=0,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=1,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id=None,
            created_at="2026-10-10T12:00:00Z",
            updated_at="2026-10-10T12:01:00Z",
            expires_at="2026-11-10T12:00:00Z",
            api_run_accounting=sv_142_acc.to_document(),
        )
        pkt_142 = build_no_transaction_attempt_packet(
            identity=sv_142_id,
            record=sv_142_rec,
            owner_ceased=True,
            provider_entry_started=False,
        )

        sv_153_id = SessionIdentity("org/sitevault", 153, repository_id=1420)
        sv_153_acc = ApiRunAccounting.from_document(
            {
                **_make_accounting(run_id=15301, uncertain=1).to_document(),
                "identity": {"repository_id": 1420, "run_id": 15301},
            }
        )
        sv_153_rec = SessionRecord._construct(
            repository="org/sitevault",
            pull_request=153,
            repository_id=1420,
            completed_initial_reviews=0,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=1,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id=None,
            created_at="2026-10-11T12:00:00Z",
            updated_at="2026-10-11T12:01:00Z",
            expires_at="2026-11-11T12:00:00Z",
            api_run_accounting=sv_153_acc.to_document(),
        )
        pkt_153 = build_no_transaction_attempt_packet(
            identity=sv_153_id,
            record=sv_153_rec,
            owner_ceased=True,
            provider_entry_started=True,
        )

        # 3. ReviewSensei #408 (G30):
        elig_408 = prior()
        pkt_408 = build_pending_reassessment_attempt_packet(
            identity=SessionIdentity(
                REVIEWSENSEI_408_REPOSITORY,
                REVIEWSENSEI_408_PULL_REQUEST,
                repository_id=987654,
            ),
            original_eligibility=elig_408,
            latest_eligibility=elig_408,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=hashlib.sha256(HUMAN.encode("utf-8")).hexdigest(),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pr_merged=True,
            pending_receipt=PendingReceiptObservation(
                source_comment_id=40810,
                source_updated_digest=hashlib.sha256(
                    b"2026-10-15T14:22:00Z"
                ).hexdigest(),
                pull_request=REVIEWSENSEI_408_PULL_REQUEST,
                head_sha=HEAD,
                report_digest="c" * 64,
                expected_pages=1,
                part_comment_ids=((1, 40899),),
                complete=True,
            ),
        )

        # 4. Generic non-TaskAlong overflow packet + validation guardrails (G31):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            res_small = _build_taskalong_41_result(
                ledger, narrative_bytes=500, candidate_count=2, human_hold_count=1
            )
            loaded = ledger.load(IDENTITY)
            assert loaded.record is not None and loaded.record.transaction is not None
            rec_generic = _with_accounting(
                loaded.record,
                _make_accounting(run_id=7700),
                transaction=replace(
                    loaded.record.transaction, phase="publication_failed"
                ),
            )
            pkt_generic = build_publication_overflow_attempt_packet(
                identity=IDENTITY,
                record=rec_generic,
                result=res_small,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
                diff=FULL_DIFF,
            )

        # Prove strict cross-incident distinctness and universal authority=False:
        self.assertEqual(
            {
                pkt_443.case_id,
                pkt_142.case_id,
                pkt_153.case_id,
                pkt_408.case_id,
                TASKALONG_41_CASE_ID,
                pkt_generic.case_id,
            },
            {
                REVIEWSENSEI_443_CASE_ID,
                SITEVAULT_142_CASE_ID,
                SITEVAULT_153_CASE_ID,
                REVIEWSENSEI_408_CASE_ID,
                TASKALONG_41_CASE_ID,
                "owner/repo#1:gen-2:run-7700",
            },
        )
        for pkt in (pkt_443, pkt_142, pkt_153, pkt_408, pkt_generic):
            self.assertFalse(pkt.authority)
        self.assertEqual(pkt_443.inferred_cause, "unknown")
        self.assertIn("historical_live_settlement_receipt", pkt_443.missing_proofs)
        for pkt_sub in (pkt_142, pkt_153, pkt_408, pkt_generic):
            self.assertIn(
                "historical_live_settlement_receipt",
                getattr(
                    pkt_sub,
                    "missing_proofs",
                    getattr(pkt_sub, "missing_proof_holds", ()),
                ),
            )

        # Validation error branches on PublicationOverflowAttemptPacket & builder:
        with self.assertRaises(ReviewInputError):
            build_publication_overflow_attempt_packet(
                identity=SessionIdentity("owner/repo", 1, repository_id=None),
                record=rec_generic,
                result=res_small,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
            )
        with self.assertRaises(ReviewInputError):
            build_publication_overflow_attempt_packet(
                identity=IDENTITY,
                record=_with_accounting(
                    rec_generic, _make_accounting(run_id=7700), transaction=None
                ),
                result=res_small,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
            )
        with self.assertRaisesRegex(ReviewInputError, "record identity conflicts"):
            build_publication_overflow_attempt_packet(
                identity=SessionIdentity("other/repo", 1, repository_id=1),
                record=rec_generic,
                result=res_small,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
            )
        with self.assertRaises(ReviewInputError):
            build_publication_overflow_attempt_packet(
                identity=IDENTITY,
                record=rec_generic,
                result=res_small,
                policy=POLICY,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                live_reviews=[],
                app_slug=APP,
                owner_ceased=True,
                narrative_archive=object(),  # type: ignore[arg-type]
            )
        for bad_over in (
            {"authority": True},
            {"formatter_fit_is_not_settlement": False},
            {"qualification": "bad"},
            {"formatter_comparison": "bad"},
            {"case_id": " "},
            {"review_body_byte_ceiling": 70000},
            {"failure_boundary": "bogus"},
            {"record_sha256": "bad"},
            {"diff_sha256": "bad"},
            {"base_sha": "bad"},
            {"executor_sha": "bad"},
            {"installed_core_version": "bad version!"},
            {
                "owner_ceased": False,
                "missing_proofs": ("historical_live_settlement_receipt",),
            },
            {"missing_proofs": ()},
        ):
            with self.assertRaises(ReviewInputError):
                replace(pkt_generic, **bad_over)


if __name__ == "__main__":
    unittest.main()
