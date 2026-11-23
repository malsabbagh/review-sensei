"""Tests for single-page multi-part checkpoints, pre-Effect-#1 sizing, paged recovery, and advisor parity."""

from __future__ import annotations

import copy
import random
import string
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_byte_aware_comment_pages as byte_pages
import test_finding_lifecycle_parts as lifecycle_parts_tests
import test_historical_recovery_matrix_g03 as g03_tests
import test_publication_failure_recovery as pub_recovery_tests
import test_review_run as review_run_tests
from fake_github_http import json_response
from review_sensei_github import api_run_requests as adapter
from review_sensei_github.action_receipt import publish_command_receipt
from review_sensei_github.api_run_plan import NATIVE_OPERATION_REQUESTS
from review_sensei_github.errors import GitHubHTTPResponseTooLargeError
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.publication_failure_recovery import (
    PreExternalWriteSettlementWitness,
)
from review_sensei_github.recovery_route_advisor import (
    PRE_482_LIFECYCLE_OVERFLOW_DIAGNOSTIC,
    build_attempt_evidence_packet,
    build_live_ledger_attempt_evidence_packet,
    classify_live_ledger_recovery,
    classify_recovery_state,
)
from review_sensei_github.review_run import ActionRunLedger
from review_sensei_github.run_authority import VerifiedRunAction
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import IDENTITY

from review_sensei.bounded_evidence import inline_manifest
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    preflight_finding_lifecycle,
    stage_finding_lifecycle,
)
from review_sensei.models import ReviewTransaction


def _three_part_findings(*, count: int = 7, body_chars: int = 8_000) -> list[dict]:
    alphabet = string.ascii_letters + string.digits + "-_"
    findings: list[dict] = []
    for idx in range(count):
        prose = "".join(random.Random(100 + idx).choices(alphabet, k=body_chars))
        findings.append(
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": f"D{idx}: {prose}",
                "severity": "high" if idx == 0 else "low",
                "blocking": idx == 0,
                "needs_human": False,
                "symbol": f"s{idx}",
                "defect_kind": f"k{idx}",
            }
        )
    return findings


class CheckpointOperationAndPagedRecoveryTests(unittest.TestCase):
    def test_end_to_end_three_part_review_and_subsequent_head_rollover_at_512(
        self,
    ) -> None:
        self.assertEqual(NATIVE_OPERATION_REQUESTS, 10)
        # (a) Strict 512 ceiling: 3-part finding lifecycle publishes end-to-end on head 1;
        # when head 2 genuinely cannot afford full check_1 + review + check_2 pre-flight,
        # pre-Effect-#1 guard refuses BEFORE external writes and emits a settlement witness.
        with tempfile.TemporaryDirectory() as directory:
            f = pub_recovery_tests.PublicationFailureRecoveryFixture(
                directory, limit=512
            )
            f.provider_comments = _three_part_findings()
            self.assertEqual(f.invoke(), 0, f.error)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert r1 is not None and r1.transaction is not None
            self.assertEqual(r1.transaction.phase, "publication_succeeded")
            self.assertEqual(len(f.reviews), 1)
            self.assertGreaterEqual(len(f.inline), 1)
            self.assertIsNotNone(r1.finding_lifecycle)
            manifest = r1.finding_lifecycle["manifest"]
            self.assertEqual(manifest["encoding"], "partitioned-json-v1")
            self.assertGreaterEqual(len(manifest["parts"]), 3)

            f.next_review_run(head_sha=pub_recovery_tests.HEAD_2)
            f.provider_comments = []
            self.assertEqual(f.invoke(), 1)
            self.assertIn("cannot fund the complete operation", f.error)
            self.assertEqual(len(f.reviews), 1)
            self.assertEqual(len(f.check_runs), 1)
            witnesses = pub_recovery_tests._witnesses_from_extra(f.extra_comments)
            self.assertEqual(len(witnesses), 1)
            self.assertEqual(witnesses[0][2].settled_phase, "publication_failed")

        # (b) Subsequent-head rollover from a 3-part finding_lifecycle (still with
        # NATIVE_OPERATION_REQUESTS=10) completes end-to-end when caller budget funds both.
        with tempfile.TemporaryDirectory() as directory:
            f = pub_recovery_tests.PublicationFailureRecoveryFixture(
                directory, limit=1024
            )
            orig_ext = f.external

            def stamped_ext(request, timeout=30):
                response = orig_ext(request, timeout)
                if f.comment:
                    f.comment.setdefault("updated_at", "2026-11-23T15:00:00Z")
                for row in f.extra_comments.values():
                    row.setdefault("updated_at", "2026-11-23T15:00:00Z")
                return response

            f.external = stamped_ext
            f.provider_comments = _three_part_findings()
            self.assertEqual(f.invoke(), 0, f.error)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert r1 is not None and r1.finding_lifecycle is not None
            self.assertGreaterEqual(len(r1.finding_lifecycle["manifest"]["parts"]), 3)

            f.next_review_run(head_sha=pub_recovery_tests.HEAD_2)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Follow-up resolved on head 2",
                    "severity": "low",
                    "blocking": False,
                    "needs_human": False,
                    "symbol": "s0",
                    "defect_kind": "k0",
                }
            ]
            self.assertEqual(f.invoke(), 0, f.error)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert r2 is not None and r2.transaction is not None
            self.assertEqual(r2.transaction.phase, "publication_succeeded")
            self.assertEqual(r2.transaction.head_sha, pub_recovery_tests.HEAD_2)
            self.assertEqual(len(f.reviews), 2)

    def test_pre_effect_one_starved_budget_refuses_before_external_write_and_allows_rollover(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            f = pub_recovery_tests.PublicationFailureRecoveryFixture(
                directory, limit=512
            )
            f.provider_comments = _three_part_findings()
            original_publish = ReviewPublisher.publish

            def starve_right_before_publish(pub_self, *args, **kwargs):
                journal = getattr(pub_self, "_finding_lifecycle_journal", None)
                drain = 0
                budget = None
                if journal is not None and journal.ledger.evidence_budget is not None:
                    budget = journal.ledger.evidence_budget
                    # Leave 42 calls at entry to publish(): pre-Effect-#1 guard refuses
                    # BEFORE any check-run / review write or lifecycle mutation.
                    drain = max(0, budget.ordinary_remaining() - 42)
                    budget._ordinary_hold += drain
                try:
                    return original_publish(pub_self, *args, **kwargs)
                finally:
                    if budget is not None and drain > 0:
                        budget._ordinary_hold -= drain

            with patch.object(ReviewPublisher, "publish", starve_right_before_publish):
                self.assertEqual(f.invoke(), 1)
            self.assertIn("cannot fund the complete operation", f.error)
            # Zero external GitHub writes dispatched!
            self.assertEqual(len(f.reviews), 0)
            self.assertEqual(len(f.check_runs), 0)
            self.assertFalse(f.accounted._external_business_write_attempted)

            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert settled is not None and settled.transaction is not None
            self.assertEqual(settled.transaction.phase, "publication_failed")
            # finding_lifecycle must NOT be dirtied for the failed transaction!
            self.assertTrue(
                settled.finding_lifecycle is None
                or settled.finding_lifecycle.get("operation_id")
                != settled.transaction.transaction_id
            )
            witnesses = pub_recovery_tests._witnesses_from_extra(f.extra_comments)
            self.assertEqual(len(witnesses), 1)
            self.assertEqual(witnesses[0][2].settled_phase, "publication_failed")

            # Subsequent head rolls over cleanly at 512 without manual intervention.
            f.next_review_run(head_sha=pub_recovery_tests.HEAD_2)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Follow-up finding on head 2",
                    "severity": "low",
                    "blocking": False,
                    "needs_human": False,
                    "symbol": "s0",
                    "defect_kind": "k0",
                }
            ]
            self.assertEqual(f.invoke(), 0, f.error)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert r2 is not None and r2.transaction is not None
            self.assertEqual(r2.transaction.phase, "publication_succeeded")
            self.assertEqual(r2.transaction.head_sha, pub_recovery_tests.HEAD_2)
            self.assertGreaterEqual(len(f.reviews), 1)

    def test_action_run_fences_accounting_and_single_page_tight_pricing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs = (
                lifecycle_parts_tests.CurrentRunFindingPartsTests().fixture(directory)
            )
            alphabet = string.ascii_letters + string.digits + "-_"
            kwargs["document"] = {
                "data": "".join(random.Random(7).choices(alphabet, k=72000))
            }
            action_ledger = ActionRunLedger(
                fixture.native, fixture.caller, fixture.identity
            )
            action_ledger.enable_finding_lifecycle_writes = True
            action_ledger._replace_inventory(
                copy.deepcopy(fixture.ledger._cached_comments())
            )
            action_ledger._inventory_complete = True
            with patch(
                "review_sensei_github.caller_operation.prewrite_fence_requests",
                return_value=1,
            ):
                plan = preflight_finding_lifecycle(action_ledger, record, **kwargs)
                n_parts = plan["checkpoints"][0]["parts"]
                self.assertGreaterEqual(n_parts, 3)
                page_plan = adapter._PLAIN_FINDING_PLANS[action_ledger]
                self.assertEqual(len(page_plan.page_bounds), 1)
                self.assertEqual(page_plan.fences, 1)
                self.assertEqual(
                    plan["checkpoints"][0]["conditional_calls"],
                    17 + 8 * n_parts,
                )
                self.assertEqual(plan["conditional_calls"], 17 + 8 * n_parts)

                observed_preflights: list[int] = []
                original_preflight = action_ledger.evidence_budget.preflight

                def capture_preflight(calls: int) -> None:
                    observed_preflights.append(calls)
                    original_preflight(calls)

                with patch.object(
                    action_ledger.evidence_budget,
                    "preflight",
                    side_effect=capture_preflight,
                ):
                    staged = stage_finding_lifecycle(action_ledger, record, **kwargs)
                self.assertIsNotNone(staged.finding_lifecycle)
                staged_parts = len(staged.finding_lifecycle["manifest"]["parts"])
                self.assertGreaterEqual(staged_parts, 3)
                expected_op = 4 + 3 * staged_parts + staged_parts * 1
                # _take_plain_finding_operation preflights (4 + 3*n + n*fences) + fences.
                self.assertIn(expected_op + 1, observed_preflights)
                adapter._PLAIN_FINDING_ACTIVE.pop(action_ledger, None)
                adapter._PLAIN_FINDING_PLANS.pop(action_ledger, None)

        # Verify tight pricing (`guard_calls` omitted when len(page_plan.page_bounds) == 1)
        # for ActionRunLedger with valid business_guard.
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs = (
                lifecycle_parts_tests.CurrentRunFindingPartsTests().fixture(directory)
            )
            alphabet = string.ascii_letters + string.digits + "-_"
            kwargs["document"] = {
                "data": "".join(random.Random(7).choices(alphabet, k=72000))
            }
            ledger = ActionRunLedger(fixture.native, fixture.caller, fixture.identity)
            ledger._replace_inventory(copy.deepcopy(fixture.ledger._cached_comments()))
            ledger._inventory_complete = True
            fake_action = object.__new__(VerifiedRunAction)
            fake_action.run = SimpleNamespace(
                caller=fixture.caller,
                ledger=ledger,
                identity=fixture.identity,
                http=fixture.native,
            )
            fake_action.owner = fixture.caller.accounting.owner
            fake_action.document = {}
            ledger.business_guard = fake_action.fence
            tight_plan = preflight_finding_lifecycle(ledger, record, **kwargs)
            tight_parts = tight_plan["checkpoints"][0]["parts"]
            self.assertGreaterEqual(tight_parts, 3)
            self.assertEqual(len(adapter._PLAIN_FINDING_PLANS[ledger].page_bounds), 1)
            # Tight single-page cost omits extra guard_calls (`4 + 3 * n`, not `4 + 3 * n + 2`).
            self.assertEqual(
                tight_plan["checkpoints"][0]["conditional_calls"],
                4 + 3 * tight_parts,
            )
            adapter._PLAIN_FINDING_PLANS.pop(ledger, None)

    def test_action_receipt_enforces_per_page_byte_bound(self) -> None:
        comments = [byte_pages._human_comment(10 + i, 32_000) for i in range(14)]
        server = byte_pages._FakeIssueCommentServer()
        with tempfile.TemporaryDirectory() as tmp:
            helper = byte_pages.ByteAwareCommentPageTests()
            (
                _identity,
                _caller,
                _ledger,
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            ) = helper._build_action_harness(tmp, server, existing_comments=comments)
            for idx in range(3):
                server.comments.append(byte_pages._human_comment(500 + idx, 28_000))
            quote = journal.preflight_command_finalization(
                plan,
                preview_record=preview_record,
                capacity_details=details,
                http=run.http,
                budget=run.caller.budget_view,
            )
            self.assertGreater(quote["reserved_cost_breakdown"]["receipt_calls"], 8)

            # Mutate server response after preflight: same count, extra bytes
            # exceeding the planned per-page bound while staying < 512 KiB HTTP wire!
            server.comments[0]["body"] = "b" * 40_000
            posts_before = [
                req for req in server.requests if req[0] in {"POST", "PATCH"}
            ]
            with self.assertRaisesRegex(
                ReviewInputError, "plain action receipt page exceeded planned bound"
            ):
                publish_command_receipt(run, authority, command_body, result)
            posts_after = [
                req for req in server.requests if req[0] in {"POST", "PATCH"}
            ]
            self.assertEqual(posts_after, posts_before)

    def test_step_down_paging_above_512kib_in_witness_and_native_census_scans(
        self,
    ) -> None:
        # (a) publication_failure_recovery: witness scan, PR surfaces, & check-runs > 512 KiB
        with tempfile.TemporaryDirectory() as directory:
            helper = pub_recovery_tests.PublicationFailureRecoveryTests()
            f = helper._round2_post_checkpoint_pre_write_failure(directory, limit=1024)
            witness_cid, witness_row, _ = pub_recovery_tests._witnesses_from_extra(
                f.extra_comments
            )[0]
            f.extra_comments.pop(witness_cid)
            for idx in range(25):
                cid = 3_100_000 + idx
                f.extra_comments[cid] = {
                    "id": cid,
                    "body": "discussion " + ("z" * 23_000),
                    "user": {"id": 42, "login": "maintainer", "type": "User"},
                    "issue_url": f"{f.api_url}/repos/owner/repo/issues/7",
                }
            # Put witness after the 25 discussion comments so stepped page=2 finds it.
            new_witness_cid = 3_200_000
            witness_row["id"] = new_witness_cid
            f.extra_comments[new_witness_cid] = witness_row

            for idx in range(25):
                f.reviews.append(
                    {
                        "id": 880_000 + idx,
                        "commit_id": "f" * 40,
                        "state": "COMMENTED",
                        "body": "unrelated review " + ("r" * 23_000),
                        "user": {"type": "User", "login": "reviewer", "id": 77},
                    }
                )
                f.inline.append(
                    {
                        "id": 890_000 + idx,
                        "pull_request_review_id": 880_000 + idx,
                        "commit_id": "f" * 40,
                        "original_commit_id": "f" * 40,
                        "body": "unrelated inline " + ("i" * 23_000),
                        "user": {"type": "User", "login": "reviewer", "id": 77},
                    }
                )
                f.check_runs.append(
                    {
                        "id": 990_000 + idx,
                        "name": "ReviewSensei",
                        "head_sha": pub_recovery_tests.HEAD_2,
                        "status": "completed",
                        "conclusion": "success",
                        "app": {"id": 555, "slug": "other-ci"},
                        "output": {"title": "other", "summary": "c" * 23_000},
                    }
                )

            seen_queries: list[tuple[str, str]] = []
            orig_external = f.external

            def paged_external(request, timeout):
                parsed = urlsplit(request.full_url)
                seen_queries.append(
                    (request.get_method(), f"{parsed.path}?{parsed.query}")
                )
                qs = parse_qs(parsed.query)
                if (
                    request.get_method() == "GET"
                    and parsed.path.endswith("/issues/7/comments")
                    and "since" not in qs
                ):
                    f._meter_if_unmetered(request)
                    per_page = int(qs.get("per_page", [100])[0])
                    page = int(qs.get("page", [1])[0])
                    all_comments = [copy.deepcopy(f.comment)] + [
                        copy.deepcopy(f.extra_comments[k])
                        for k in sorted(f.extra_comments)
                    ]
                    start = (page - 1) * per_page
                    return json_response(all_comments[start : start + per_page])
                if request.get_method() == "GET" and parsed.path.endswith(
                    "/pulls/7/reviews"
                ):
                    f._meter_if_unmetered(request)
                    per_page = int(qs.get("per_page", [100])[0])
                    page = int(qs.get("page", [1])[0])
                    start = (page - 1) * per_page
                    return json_response(
                        copy.deepcopy(f.reviews[start : start + per_page])
                    )
                if request.get_method() == "GET" and parsed.path.endswith(
                    "/pulls/7/comments"
                ):
                    f._meter_if_unmetered(request)
                    per_page = int(qs.get("per_page", [100])[0])
                    page = int(qs.get("page", [1])[0])
                    start = (page - 1) * per_page
                    rows = copy.deepcopy(f.inline[start : start + per_page])
                    for row in rows:
                        row.setdefault("original_commit_id", row["commit_id"])
                    return json_response(rows)
                if (
                    request.get_method() == "GET"
                    and "/commits/" in parsed.path
                    and parsed.path.endswith("/check-runs")
                ):
                    f._meter_if_unmetered(request)
                    per_page = int(qs.get("per_page", [100])[0])
                    page = int(qs.get("page", [1])[0])
                    head_sha = parsed.path.split("/commits/")[1].split("/")[0]
                    matching = [
                        c for c in f.check_runs if c.get("head_sha") == head_sha
                    ]
                    start = (page - 1) * per_page
                    return json_response(
                        {
                            "total_count": len(matching),
                            "check_runs": copy.deepcopy(
                                matching[start : start + per_page]
                            ),
                        }
                    )
                return orig_external(request, timeout)

            with patch.object(f, "external", side_effect=paged_external):
                f.next_review_run(head_sha=pub_recovery_tests.HEAD_3)
                self.assertEqual(f.invoke(), 0, f.error)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert r3 is not None and r3.transaction is not None
            self.assertEqual(r3.transaction.phase, "publication_succeeded")
            for suffix in (
                "/issues/7/comments",
                "/pulls/7/reviews",
                "/pulls/7/comments",
                "/check-runs",
            ):
                self.assertTrue(
                    any(suffix in q and "per_page=20" in q for _m, q in seen_queries),
                    (suffix, seen_queries),
                )

        # (a2) Fail closed when per_page=1 itself still exceeds 512 KiB.
        with tempfile.TemporaryDirectory() as directory:
            helper = pub_recovery_tests.PublicationFailureRecoveryTests()
            f = helper._round2_post_checkpoint_pre_write_failure(directory, limit=1024)
            orig_external = f.external

            def always_oversized_reviews(request, timeout):
                parsed = urlsplit(request.full_url)
                if request.get_method() == "GET" and parsed.path.endswith(
                    "/pulls/7/reviews"
                ):
                    raise GitHubHTTPResponseTooLargeError("single review row > 512 KiB")
                return orig_external(request, timeout)

            with patch.object(f, "external", side_effect=always_oversized_reviews):
                f.next_review_run(head_sha=pub_recovery_tests.HEAD_3)
                self.assertEqual(f.invoke(), 1)
                self.assertIn("native effect census is unavailable", f.error)

        # (b) pre_entry_abort_admission step-down paging on > 512 KiB reviews/comments
        with tempfile.TemporaryDirectory() as directory:
            af = review_run_tests.EntryFixture(directory)

            def refuse_pre_entry() -> None:
                raise ReviewInputError("synthetic pre-entry refusal")

            af.before_entry = refuse_pre_entry
            self.assertEqual(af.invoke(), 1)
            af.before_entry = None
            af.next_run()
            big_reviews = [
                {
                    "id": 700_000 + idx,
                    "commit_id": "f" * 40,
                    "state": "COMMENTED",
                    "body": "unrelated " + ("r" * 23_000),
                    "user": {"type": "User", "login": "reviewer", "id": 77},
                }
                for idx in range(25)
            ]
            abort_queries: list[str] = []
            orig_abort_ext = af.external

            def paged_abort_ext(request, timeout):
                parsed = urlsplit(request.full_url)
                abort_queries.append(f"{parsed.path}?{parsed.query}")
                if (
                    af.accounted._state is None
                    and request.get_method() == "GET"
                    and parsed.path.endswith("/pulls/7/reviews")
                ):
                    if (
                        len(af.accounted.observations)
                        == sum(a == af.attempt for a, _, _ in af.trace) + 1
                    ):
                        af.trace.append((af.attempt, request.get_method(), parsed.path))
                    qs = parse_qs(parsed.query)
                    per_page = int(qs.get("per_page", [100])[0])
                    page = int(qs.get("page", [1])[0])
                    start = (page - 1) * per_page
                    return json_response(
                        copy.deepcopy(big_reviews[start : start + per_page])
                    )
                return orig_abort_ext(request, timeout)

            with patch.object(af, "external", side_effect=paged_abort_ext):
                af.invoke()
            rolled = parse_session_comment(af.comment["body"], identity=IDENTITY)
            assert rolled is not None
            self.assertEqual(rolled.api_run_accounting["identity"]["run_id"], af.run_id)
            self.assertTrue(
                any(
                    "/pulls/7/reviews" in q and "per_page=20" in q
                    for q in abort_queries
                )
            )
            self.assertTrue(
                any("/pulls/7/reviews" in q and "page=2" in q for q in abort_queries)
            )

    def test_recovery_route_advisor_witness_and_pre_482_classifications(self) -> None:
        # (a) Offline `pre-external-write-settlement-v1` classification & negative controls
        acc_failed = g03_tests._make_accounting(run_id=901, attempt=1, generation=0)
        tx_failed = g03_tests._make_tx(
            phase="publication_failed",
            base_sha=g03_tests.BASE_A,
            head_sha=g03_tests.HEAD_A,
        )
        rec_failed = g03_tests._make_record(
            accounting=acc_failed, transaction=tx_failed, generation=2
        )
        witness = PreExternalWriteSettlementWitness.create(
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            run_id=901,
            owner={"run_attempt": 1, "job": "hosted", "generation": 0},
            settled_root_sha256=rec_failed.record_sha256,
            settled_generation=rec_failed.generation,
            settled_phase="publication_failed",
            boundary="post_checkpoint_pre_write",
            transaction_id=tx_failed.transaction_id,
            base_sha=tx_failed.base_sha,
            head_sha=tx_failed.head_sha,
            producer="github-bot:999",
        )
        ready = classify_recovery_state(
            rec_failed,
            current_base=g03_tests.BASE_A,
            current_head=g03_tests.HEAD_B,
            executor_status="failed",
            native_effects_confirmed=True,
            evidence_producer="github-bot:999",
            settlement_witness=witness,
        )
        self.assertFalse(ready.authority)
        self.assertTrue(ready.terminal)
        self.assertEqual(ready.state_code, "pre_external_write_settlement_ready")
        self.assertEqual(ready.primary_route, "pre_external_write_rollover")
        self.assertEqual(ready.missing_witnesses, ())

        # Missing or unverified witness controls
        missing_w = classify_recovery_state(
            rec_failed,
            current_base=g03_tests.BASE_A,
            current_head=g03_tests.HEAD_B,
            executor_status="failed",
            native_effects_confirmed=True,
            settlement_witness=False,
        )
        self.assertFalse(missing_w.authority)
        self.assertFalse(missing_w.terminal)
        self.assertEqual(
            missing_w.state_code, "pre_external_write_settlement_unverified"
        )
        self.assertEqual(missing_w.primary_route, "controlled_serialized_restart")
        self.assertIn(
            "valid_pre_external_write_settlement_witness", missing_w.missing_witnesses
        )

        tampered = witness.to_dict()
        tampered["settled_root_sha256"] = "e" * 64
        tampered_w = classify_recovery_state(
            rec_failed,
            current_base=g03_tests.BASE_A,
            current_head=g03_tests.HEAD_B,
            executor_status="failed",
            native_effects_confirmed=True,
            settlement_witness=tampered,
        )
        self.assertEqual(
            tampered_w.state_code, "pre_external_write_settlement_unverified"
        )
        self.assertIn(
            "valid_pre_external_write_settlement_witness",
            tampered_w.missing_witnesses,
        )

        # no-transaction with valid witness also classifies as settlement ready
        rec_notx = g03_tests._make_record(
            accounting=acc_failed, transaction=None, generation=1
        ).evolve(failed_attempts=1, failed_attempts_head_sha=g03_tests.HEAD_A)
        notx_witness = PreExternalWriteSettlementWitness.create(
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            run_id=901,
            owner={"run_attempt": 1, "job": "hosted", "generation": 0},
            settled_root_sha256=rec_notx.record_sha256,
            settled_generation=rec_notx.generation,
            settled_phase="no-transaction",
            boundary="pre_checkpoint_validation",
            transaction_id=None,
            base_sha=g03_tests.BASE_A,
            head_sha=g03_tests.HEAD_A,
            producer="github-bot:999",
        )
        notx_ready = classify_recovery_state(
            rec_notx,
            current_base=g03_tests.BASE_A,
            current_head=g03_tests.HEAD_B,
            executor_status="failed",
            native_effects_confirmed=True,
            evidence_producer="github-bot:999",
            settlement_witness=notx_witness,
        )
        self.assertTrue(notx_ready.terminal)
        self.assertEqual(notx_ready.state_code, "pre_external_write_settlement_ready")
        self.assertEqual(notx_ready.primary_route, "pre_external_write_rollover")
        self.assertFalse(notx_ready.authority)

        # Live read-only inspection detects pre-external-write witness with GETs only!
        def _ro_external_for(fixture_obj):
            def ro_ext(request, timeout):
                parsed = urlsplit(request.full_url)
                if request.get_method() != "GET":
                    raise AssertionError(
                        f"unexpected non-GET {request.get_method()} {parsed.path}"
                    )
                if parsed.path.endswith("/pulls/7/reviews"):
                    return json_response(copy.deepcopy(fixture_obj.reviews))
                if parsed.path.endswith("/pulls/7/comments"):
                    rows = copy.deepcopy(fixture_obj.inline)
                    for row in rows:
                        row.setdefault("original_commit_id", row["commit_id"])
                    return json_response(rows)
                if "/commits/" in parsed.path and parsed.path.endswith("/check-runs"):
                    hsha = parsed.path.split("/commits/")[1].split("/")[0]
                    rows = [
                        copy.deepcopy(c)
                        for c in fixture_obj.check_runs
                        if c.get("head_sha") == hsha
                    ]
                    return json_response({"total_count": len(rows), "check_runs": rows})
                if parsed.path.endswith(f"/actions/runs/{fixture_obj.run_id}"):
                    return json_response(
                        {
                            "id": fixture_obj.run_id,
                            "run_attempt": fixture_obj.attempt,
                            "repository": {"id": 123, "full_name": "owner/repo"},
                            "status": "completed",
                            "conclusion": "failure",
                            "event": "pull_request",
                        }
                    )
                if parsed.path.endswith(
                    f"/actions/runs/{fixture_obj.run_id}/attempts/1/jobs"
                ):
                    return json_response(
                        {
                            "total_count": 1,
                            "jobs": [
                                {
                                    "id": 1,
                                    "run_id": fixture_obj.run_id,
                                    "run_attempt": fixture_obj.attempt,
                                    "name": "hosted",
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "completed_at": "2026-10-25T22:01:00Z",
                                }
                            ],
                        }
                    )
                return json_response({"message": "unhandled"}, 404)

            return ro_ext

        with tempfile.TemporaryDirectory() as directory:
            helper = pub_recovery_tests.PublicationFailureRecoveryTests()
            f = helper._round2_post_checkpoint_pre_write_failure(directory, limit=512)
            f.past_jobs[f.run_id] = f.owner_job
            f.external = _ro_external_for(f)
            live_ledger, obs = g03_tests._diagnostic_ledger(f)
            live_ready = classify_live_ledger_recovery(
                ledger=live_ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=pub_recovery_tests.HEAD_3,
                actions_token="synthetic-actions",
                inspect_settlement_witness=True,
            )
            self.assertFalse(live_ready.authority)
            self.assertEqual(
                live_ready.state_code, "pre_external_write_settlement_ready"
            )
            self.assertEqual(live_ready.primary_route, "pre_external_write_rollover")
            self.assertTrue(all(m == "GET" for m, _ in obs), obs)

        # (b) Distinct pre-#482 `#485`/`#486`/`#487`-shaped records: unpublished zero-effect
        # vs genuine recorded/uncertain effects, strictly read-only (`authority=False`).
        def _valid_inline_lifecycle(
            pr_num: int, gen: int, tx: ReviewTransaction, *, op_id: str
        ) -> dict[str, object]:
            binding = {
                "repository": "owner/repo",
                "repository_id": 123,
                "pull_request": pr_num,
                "base_sha": tx.base_sha,
                "head_sha": tx.head_sha,
                "policy_digest": tx.policy_digest,
                "configuration_digest": tx.configuration_digest,
                "generation": gen,
                "producer": "github-bot:999",
                "purpose": "finding-lifecycle",
                "schema_version": "1.0",
            }
            return {
                "schema_version": "finding-lifecycle-reference-v1",
                "operation_id": op_id,
                "previous_root_sha256": "1" * 64,
                "previous_snapshot_sha256": None,
                "manifest": inline_manifest(
                    {"schema_version": "1.0", "items": []},
                    binding=binding,
                    item_count=1,
                ),
            }

        specs = [
            (
                485,
                81001,
                3,
                False,
                "ceased",
                True,
                "pre_482_lifecycle_operation_overflow_zero_effect",
            ),
            (
                486,
                81002,
                5,
                True,
                "ceased",
                True,
                "pre_482_lifecycle_operation_overflow_zero_effect",
            ),
            (
                487,
                81003,
                9,
                False,
                "ceased",
                True,
                "pre_482_lifecycle_operation_overflow_zero_effect",
            ),
        ]
        case_ids: set[str] = set()
        for (
            pr_num,
            run_id,
            gen,
            has_prior_fl,
            exec_status,
            census_clean,
            expected_state,
        ) in specs:
            tx_pr = replace(
                ReviewTransaction.create(
                    repository="owner/repo",
                    pull_request=pr_num,
                    base_sha=g03_tests.BASE_A,
                    head_sha=g03_tests.HEAD_A,
                    reservation_id="b" * 64,
                    policy_digest="2" * 64,
                    configuration_digest="1" * 64,
                    evidence_digest="3" * 64,
                    generation=gen,
                ),
                phase="publication_failed",
                result_sha256="4" * 64,
            )
            fl_ref = (
                _valid_inline_lifecycle(pr_num, gen, tx_pr, op_id="0" * 64)
                if has_prior_fl
                else None
            )
            acc_pr = g03_tests._make_accounting(run_id=run_id, attempt=1, generation=0)
            base_rec = g03_tests._make_record(
                accounting=acc_pr, transaction=None, generation=gen
            )
            raw_rec = base_rec.to_dict()
            del raw_rec["schema_version"], raw_rec["record_sha256"]
            raw_rec["transaction"] = tx_pr
            raw_rec["last_committed_reservation_id"] = tx_pr.reservation_id
            raw_rec["repository"] = "owner/repo"
            raw_rec["pull_request"] = pr_num
            raw_rec["repository_id"] = 123
            raw_rec["finding_lifecycle"] = fl_ref
            rec_pr = type(base_rec)._construct(**raw_rec)
            before_sha = rec_pr.record_sha256
            pkt = build_attempt_evidence_packet(
                rec_pr,
                current_base=g03_tests.BASE_A,
                current_head=g03_tests.HEAD_B,
                executor_status=exec_status,
                native_effects_confirmed=census_clean,
                diagnostics_available=True,
                diagnostic_cause="confirmed_pre_write_overflow",
                failure_diagnostic=PRE_482_LIFECYCLE_OVERFLOW_DIAGNOSTIC,
            )
            self.assertEqual(rec_pr.record_sha256, before_sha)
            self.assertFalse(pkt.authority)
            self.assertFalse(pkt.classification.authority)
            self.assertEqual(pkt.classification.state_code, expected_state)
            self.assertEqual(pkt.inferred_cause, "confirmed_pre_write_overflow")
            self.assertEqual(
                pkt.classification.missing_witnesses,
                ("valid_pre_external_write_settlement_witness",),
            )
            self.assertIn(f"#{pr_num}:gen-{gen}:run-{run_id}", pkt.case_id)
            case_ids.add(pkt.case_id)
        self.assertEqual(len(case_ids), 3)

        # Contrast #487 with genuine recorded finding_lifecycle / native effects present
        tx_dirty = g03_tests._make_tx(
            phase="publication_failed",
            base_sha=g03_tests.BASE_A,
            head_sha=g03_tests.HEAD_A,
        )
        acc_dirty = g03_tests._make_accounting(run_id=81004, attempt=1, generation=0)
        rec_dirty = g03_tests._make_record(
            accounting=acc_dirty, transaction=tx_dirty, generation=9
        ).evolve(
            finding_lifecycle=_valid_inline_lifecycle(
                7, 9, tx_dirty, op_id=tx_dirty.transaction_id
            )
        )
        dirty_pkt = build_attempt_evidence_packet(
            rec_dirty,
            current_base=g03_tests.BASE_A,
            current_head=g03_tests.HEAD_B,
            executor_status="ceased",
            native_effects_confirmed=False,
            diagnostics_available=True,
            diagnostic_cause="confirmed_pre_write_overflow",
            failure_diagnostic=PRE_482_LIFECYCLE_OVERFLOW_DIAGNOSTIC,
        )
        self.assertFalse(dirty_pkt.authority)
        self.assertEqual(
            dirty_pkt.classification.state_code,
            "pre_482_lifecycle_operation_overflow_effects_present",
        )
        self.assertIn(
            "unpublished_zero_effect_finding_lifecycle",
            dirty_pkt.classification.missing_witnesses,
        )
        self.assertIn(
            "clean_native_effect_census",
            dirty_pkt.classification.missing_witnesses,
        )

        # Live read-only pre-#482 packet builder performs GETs only (`authority=False`).
        with tempfile.TemporaryDirectory() as directory:
            helper = pub_recovery_tests.PublicationFailureRecoveryTests()
            f = helper._round2_post_checkpoint_pre_write_failure(directory, limit=512)
            f.extra_comments.clear()
            f.past_jobs[f.run_id] = f.owner_job
            f.external = _ro_external_for(f)
            ro_ledger, ro_obs = g03_tests._diagnostic_ledger(f)
            live_pkt = build_live_ledger_attempt_evidence_packet(
                ledger=ro_ledger,
                identity=IDENTITY,
                current_base=f.requested_base,
                current_head=pub_recovery_tests.HEAD_3,
                actions_token="synthetic-actions",
                diagnostics_available=True,
                diagnostic_cause="confirmed_pre_write_overflow",
                failure_diagnostic=PRE_482_LIFECYCLE_OVERFLOW_DIAGNOSTIC,
            )
            self.assertFalse(live_pkt.authority)
            self.assertEqual(
                live_pkt.classification.state_code,
                "pre_482_lifecycle_operation_overflow_zero_effect",
            )
            self.assertEqual(live_pkt.inferred_cause, "confirmed_pre_write_overflow")
            self.assertTrue(all(m == "GET" for m, _ in ro_obs), ro_obs)


if __name__ == "__main__":
    unittest.main()
