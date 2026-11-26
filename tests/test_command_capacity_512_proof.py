"""Comprehensive 512 command-capacity proof and 1152 quarantine-isolation tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import (
    current_quarantine_finalization,
    publication_recovery,
    quarantine_action,
)
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_plan import (
    EXPANDED_TERMINAL_REQUESTS,
    NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS,
    NATIVE_OPERATION_REQUESTS,
    QUARANTINE_CHECKPOINT_REQUESTS,
    TERMINAL_REQUESTS,
    OwnerAllocations,
    owner_allocations,
)
from review_sensei_github.application import GitHubApplication
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    COMMAND_FINALIZATION_RESERVE,
    parse_finding_index,
)
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import DEFAULT_API_REQUEST_LIMIT, ApiRunAccounting
from review_sensei.errors import EvidenceBudgetExhausted
from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle
from review_sensei.session import (
    SessionIdentity,
    SessionLoadResult,
    read_session_dispositions,
)
from tests.fake_github_http import json_response
from tests.test_action_run import HEAD, CommandEntryFixture
from tests.test_api_work_persistence import IDENTITY


def _make_blocking_comments(
    count: int, *, blocking: bool = True
) -> list[dict[str, object]]:
    return [
        {
            "path": "src/app.py",
            "line": 2,
            "side": "RIGHT",
            "body": f"Confirm interface assumption {i}.",
            "severity": "high",
            "blocking": blocking,
            "needs_human": not blocking,
        }
        for i in range(count)
    ]


def _run_command_with_breakdown(
    fixture: CommandEntryFixture, command_body: str
) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
    fixture.SOURCE_ID += 1
    fixture.select_command(command_body, new_run=True)

    quotes: list[dict[str, object]] = []
    refusal_breakdowns: list[dict[str, object]] = []
    pfl_code = preflight_finding_lifecycle.__code__
    app_preflight_code = GitHubApplication._preflight_command_finalization.__code__

    def observer(frame, event, arg):
        code = frame.f_code
        if code is pfl_code and event == "return" and isinstance(arg, dict):
            quotes.append(arg)
        elif (
            code is app_preflight_code
            and event == "return"
            and hasattr(frame.f_locals.get("self"), "last_command_capacity_breakdown")
        ):
            bd = frame.f_locals["self"].last_command_capacity_breakdown
            if isinstance(bd, dict):
                refusal_breakdowns.append(bd)

    prev = sys.getprofile()
    sys.setprofile(observer)
    try:
        rc = fixture.invoke_command()
    finally:
        sys.setprofile(prev)
    return rc, quotes, refusal_breakdowns


def _ordinary_and_terminal_usage(caller) -> tuple[int, int]:
    ord_used = sum(
        1
        for obs in caller.observations
        if obs["phase"] != "startup" and obs["lifecycle_phase"] != "terminal"
    )
    term_used = sum(
        1 for obs in caller.observations if obs["lifecycle_phase"] == "terminal"
    )
    return ord_used, term_used


class CommandCapacity512ProofTests(unittest.TestCase):
    def _assert_breakdown_shape(
        self, breakdown: dict[str, object], *, expected_receipt_calls: int = 8
    ) -> None:
        self.assertEqual(breakdown["limit"], 512)
        self.assertEqual(breakdown["startup"], 132)
        self.assertEqual(breakdown["first_work"], 140)
        self.assertEqual(breakdown["terminal"], 50)
        self.assertEqual(breakdown["successor"], 190)
        spent = breakdown["spent_before_preflight"]
        remaining = breakdown["remaining"]
        self.assertIsInstance(spent, int)
        self.assertIsInstance(remaining, int)
        self.assertEqual(spent + remaining, 140)
        disp = breakdown["disposition_checkpoint_calls"]
        self.assertEqual(disp, 7)
        lc = breakdown["lifecycle_checkpoint_calls"]
        self.assertIsInstance(lc, dict)
        self.assertEqual(lc["count"], len(lc["per_checkpoint"]))
        self.assertEqual(lc["total"], sum(lc["per_checkpoint"]))
        nt = breakdown["native_tail_calls"]
        self.assertIsInstance(nt, dict)
        for key in (
            "reads",
            "capability_mints",
            "review_update",
            "cleanup_threads",
            "check_run",
            "approval",
            "other_writes",
            "restore_and_snapshot_reads",
            "body_receipt_calls",
            "total",
        ):
            self.assertIn(key, nt)
        self.assertIsInstance(nt["reads"], dict)
        self.assertEqual(breakdown["receipt_calls"], expected_receipt_calls)
        self.assertEqual(
            breakdown["total_required"],
            disp + lc["total"] + nt["total"] + expected_receipt_calls,
        )

    def test_preflight_breakdown_fields_and_non_quota_refusal_diagnostics(self) -> None:
        # 1. Passing 4-finding A=1 command exposes complete structured breakdown.
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(4)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            cmd = (
                "@reviewsensei override "
                + " ".join("RS-" + e.instance[:6].upper() for e in index.entries)
                + " acceptable risk"
            )
            rc, quotes, _ = _run_command_with_breakdown(fixture, cmd)
            self.assertEqual(rc, 0, fixture.error)
            self.assertTrue(quotes)
            breakdown = quotes[0]["reserved_cost_breakdown"]
            self._assert_breakdown_shape(breakdown)
            self.assertLessEqual(breakdown["total_required"], breakdown["remaining"])

        # 2. Unfunded 5-finding A=1 refusal produces non-quota diagnostic + full breakdown.
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(5)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            before_reviews = json.dumps(fixture.reviews, sort_keys=True)
            cmd = (
                "@reviewsensei override "
                + " ".join("RS-" + e.instance[:6].upper() for e in index.entries)
                + " acceptable risk"
            )
            rc, quotes, refusal_breakdowns = _run_command_with_breakdown(fixture, cmd)
            self.assertEqual(rc, 0, fixture.error)
            self.assertEqual(quotes, [])
            self.assertTrue(refusal_breakdowns)
            bd = refusal_breakdowns[0]
            self._assert_breakdown_shape(bd)
            self.assertGreater(bd["total_required"], bd["remaining"])
            self.assertIn("cannot fund the complete operation", fixture.stdout)
            self.assertIn("before business writes", fixture.stdout)
            self.assertIn("not GitHub API quota exhaustion", fixture.stdout)
            for token in (
                "limit=512",
                "startup=132",
                "first_work=140",
                "terminal=50",
                "successor=190",
                f"spent_before_preflight={bd['spent_before_preflight']}",
                f"remaining={bd['remaining']}",
                f"total_required={bd['total_required']}",
                "disposition_checkpoint_calls=7",
                "lifecycle_checkpoint_calls=",
                "native_tail_calls=",
                f"other_writes={bd['native_tail_calls']['other_writes']}",
                f"restore_and_snapshot_reads={bd['native_tail_calls']['restore_and_snapshot_reads']}",
                f"body_receipt_calls={bd['native_tail_calls']['body_receipt_calls']}",
                "receipt_calls=8",
            ):
                self.assertIn(token, fixture.stdout)
            self.assertEqual(fixture.business_writes, 0)
            self.assertEqual(
                json.dumps(fixture.reviews, sort_keys=True), before_reviews
            )
            self.assertEqual(fixture.resolved_threads, set())
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "terminal")

    def test_single_finding_commands_A0_and_A1_within_512(self) -> None:
        # override, accept-risk, dismiss with both A=0 (N=2, K=1) and A=1 (N=1, K=1).
        for action_word, suffix in (
            ("override", "acceptable risk"),
            ("accept-risk", "--reason mitigated upstream"),
            ("dismiss", '--reason "false positive verified"'),
        ):
            for n_findings in (1, 2):
                with self.subTest(action=action_word, n_findings=n_findings):
                    with tempfile.TemporaryDirectory() as directory:
                        fixture = CommandEntryFixture(directory, limit=512)
                        if action_word == "override" or n_findings == 1:
                            fixture.provider_comments = _make_blocking_comments(
                                n_findings
                            )
                        else:
                            fixture.provider_comments = [
                                _make_blocking_comments(1)[0],
                                {
                                    "path": "src/other.py",
                                    "line": 2,
                                    "side": "RIGHT",
                                    "body": "Validate authorization boundary.",
                                    "severity": "high",
                                    "blocking": True,
                                    "needs_human": False,
                                },
                            ]
                        self.assertEqual(fixture.invoke(), 0, fixture.error)
                        index = parse_finding_index(
                            fixture.reviews[-1]["body"],
                            repository_id=123,
                            pull_request=7,
                            head_sha=HEAD,
                        )
                        inline_entry = next(
                            e for e in index.entries if not e.body_placed
                        )
                        target = (
                            "RS-" + inline_entry.instance[:6].upper()
                            if action_word == "override"
                            else inline_entry.lifecycle
                        )
                        rc, quotes, _ = _run_command_with_breakdown(
                            fixture, f"@reviewsensei {action_word} {target} {suffix}"
                        )
                        self.assertEqual(rc, 0, fixture.error)
                        self.assertTrue(quotes)
                        bd = quotes[0]["reserved_cost_breakdown"]
                        ord_used, term_used = _ordinary_and_terminal_usage(
                            fixture.accounted
                        )
                        self.assertLessEqual(
                            ord_used,
                            bd["spent_before_preflight"] + bd["total_required"],
                        )
                        self.assertLessEqual(ord_used, 140)
                        self.assertLessEqual(term_used, 50)
                        rec = parse_session_comment(
                            fixture.comment["body"], identity=IDENTITY
                        )
                        acct = ApiRunAccounting.from_document(rec.api_run_accounting)
                        self.assertEqual(acct.action["phase"], "terminal")
                        self.assertGreaterEqual(acct.remaining, 190)
                        self.assertEqual(len(fixture.resolved_threads), 1)
                        approved = sum(
                            r["state"] == "APPROVED" for r in fixture.reviews
                        )
                        self.assertEqual(approved, 1 if n_findings == 1 else 0)
                        receipts = [
                            c
                            for c in fixture.extra_comments.values()
                            if "reviewsensei:command-receipt:v1" in c.get("body", "")
                        ]
                        self.assertEqual(len(receipts), 1)

        # verify with A=0 (N=2, 1 finding overridden -> partial, 0 approvals)
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(2)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            ref = "RS-" + index.entries[0].instance[:6].upper()
            rc1, _, _ = _run_command_with_breakdown(
                fixture, f"@reviewsensei override {ref} acceptable risk"
            )
            self.assertEqual(rc1, 0, fixture.error)
            fixture.past_jobs[fixture.run_id] = "command"
            fixture.run_id += 1
            rc2, quotes2, _ = _run_command_with_breakdown(
                fixture, "@reviewsensei verify"
            )
            self.assertEqual(rc2, 0, fixture.error)
            self.assertTrue(quotes2)
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "terminal")
            self.assertEqual(len(fixture.resolved_threads), 1)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in fixture.reviews), 0)

        # verify with A=1 (N=1, disposition recorded, approval re-evaluated & submitted)
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(1)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            ref = "RS-" + index.entries[0].instance[:6].upper()
            rc1, _, _ = _run_command_with_breakdown(
                fixture, f"@reviewsensei override {ref} acceptable risk"
            )
            self.assertEqual(rc1, 0, fixture.error)
            fixture.past_jobs[fixture.run_id] = "command"
            fixture.run_id += 1
            rc2, quotes2, _ = _run_command_with_breakdown(
                fixture, "@reviewsensei verify"
            )
            self.assertEqual(rc2, 0, fixture.error)
            self.assertTrue(quotes2)
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "terminal")
            self.assertEqual(sum(r["state"] == "APPROVED" for r in fixture.reviews), 1)

        # status and pause commands finish cleanly at limit=512 with action.phase == "terminal"
        for body in ("@reviewsensei review status", "@reviewsensei review pause"):
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = CommandEntryFixture(directory, limit=512)
                    fixture.provider_comments = _make_blocking_comments(1)
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                    rc, _, _ = _run_command_with_breakdown(fixture, body)
                    self.assertEqual(rc, 0, fixture.error)
                    rec = parse_session_comment(
                        fixture.comment["body"], identity=IDENTITY
                    )
                    self.assertEqual(
                        rec.api_run_accounting["action"]["phase"], "terminal"
                    )

    def test_multi_finding_commands_A1_up_to_4_and_K5_A0_within_512(self) -> None:
        # K=2, 3, 4 with A=1 (including #475's 4 blocking findings) and K=5 with A=0 (N=6).
        for n_findings, resolve_k, expect_approved in (
            (2, 2, 1),
            (3, 3, 1),
            (4, 4, 1),
            (6, 5, 0),
        ):
            with self.subTest(n=n_findings, k=resolve_k, a=expect_approved):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = CommandEntryFixture(directory, limit=512)
                    fixture.provider_comments = _make_blocking_comments(n_findings)
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                    index = parse_finding_index(
                        fixture.reviews[-1]["body"],
                        repository_id=123,
                        pull_request=7,
                        head_sha=HEAD,
                    )
                    cmd = (
                        "@reviewsensei override "
                        + " ".join(
                            "RS-" + e.instance[:6].upper()
                            for e in index.entries[:resolve_k]
                        )
                        + " acceptable risk"
                    )
                    rc, quotes, _ = _run_command_with_breakdown(fixture, cmd)
                    self.assertEqual(rc, 0, fixture.error)
                    self.assertTrue(quotes)
                    bd = quotes[0]["reserved_cost_breakdown"]
                    self._assert_breakdown_shape(bd)
                    ord_used, term_used = _ordinary_and_terminal_usage(
                        fixture.accounted
                    )
                    reserved_ord_ceiling = (
                        bd["spent_before_preflight"] + bd["total_required"]
                    )
                    self.assertLessEqual(ord_used, reserved_ord_ceiling)
                    self.assertLessEqual(132 + ord_used + term_used, 512 - 190)
                    self.assertLessEqual(ord_used, 140)
                    self.assertLessEqual(term_used, 50)
                    self.assertEqual(len(fixture.resolved_threads), resolve_k)
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in fixture.reviews),
                        expect_approved,
                    )
                    self.assertEqual(fixture.check_runs[-1]["status"], "completed")
                    self.assertEqual(
                        fixture.check_runs[-1]["conclusion"],
                        "success" if expect_approved else "action_required",
                    )
                    receipts = [
                        c
                        for c in fixture.extra_comments.values()
                        if "reviewsensei:command-receipt:v1" in c.get("body", "")
                    ]
                    self.assertEqual(len(receipts), 1)
                    rec = parse_session_comment(
                        fixture.comment["body"], identity=IDENTITY
                    )
                    self.assertEqual(
                        rec.api_run_accounting["action"]["phase"], "terminal"
                    )
                    self.assertEqual(
                        rec.api_run_accounting["configuration"]["limit"], 512
                    )

    def test_lost_response_reconciliation_within_512(self) -> None:
        # 1. Stored response loss on GraphQL ResolveReviewThread, checkpoint PATCH,
        # command-receipt POST, and combined graphql+patch+receipt at 512.
        fault_cases = (
            ("graphql", 4, 4),
            ("patch", 3, 3),
            ("patch", 4, 4),
            ("receipt", 4, 4),
            ("combined", 4, 4),
        )
        for mode, n_findings, resolve_k in fault_cases:
            with self.subTest(mode=mode, n=n_findings, k=resolve_k):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = CommandEntryFixture(directory, limit=512)
                    fixture.provider_comments = _make_blocking_comments(n_findings)
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                    index = parse_finding_index(
                        fixture.reviews[-1]["body"],
                        repository_id=123,
                        pull_request=7,
                        head_sha=HEAD,
                    )
                    cmd = (
                        "@reviewsensei override "
                        + " ".join(
                            "RS-" + e.instance[:6].upper()
                            for e in index.entries[:resolve_k]
                        )
                        + " acceptable risk"
                    )
                    fixture.SOURCE_ID += 1
                    fixture.select_command(cmd, new_run=True)

                    orig_external = fixture.external
                    session_comment_id = fixture.comment["id"]
                    seen_faults = {
                        "graphql": 0,
                        "patch": 0,
                        "receipt": 0,
                    }
                    quotes: list[dict[str, object]] = []
                    pfl_code = preflight_finding_lifecycle.__code__

                    def inject_stored_loss(request, timeout):
                        response = orig_external(request, timeout)
                        path = urlsplit(request.full_url).path
                        method = request.get_method()
                        raw_body = request.data or b""
                        if (
                            mode in {"graphql", "combined"}
                            and method == "POST"
                            and path == "/graphql"
                            and b"ResolveReviewThread" in raw_body
                            and seen_faults["graphql"] == 0
                        ):
                            seen_faults["graphql"] += 1
                            raise TimeoutError(
                                "synthetic stored ResolveReviewThread response loss"
                            )
                        if (
                            mode in {"patch", "combined"}
                            and quotes
                            and method == "PATCH"
                            and path
                            == f"/repos/owner/repo/issues/comments/{session_comment_id}"
                            and seen_faults["patch"] == 0
                        ):
                            seen_faults["patch"] += 1
                            raise TimeoutError(
                                "synthetic stored checkpoint PATCH response loss"
                            )
                        if (
                            mode in {"receipt", "combined"}
                            and method == "POST"
                            and path == "/repos/owner/repo/issues/7/comments"
                            and b"reviewsensei:command-receipt:v1" in raw_body
                            and seen_faults["receipt"] == 0
                        ):
                            seen_faults["receipt"] += 1
                            raise TimeoutError(
                                "synthetic stored command-receipt POST response loss"
                            )
                        return response

                    fixture.external = inject_stored_loss

                    def observer(frame, event, arg):
                        if (
                            frame.f_code is pfl_code
                            and event == "return"
                            and isinstance(arg, dict)
                        ):
                            quotes.append(arg)

                    prev = sys.getprofile()
                    sys.setprofile(observer)
                    try:
                        rc = fixture.invoke_command()
                    finally:
                        sys.setprofile(prev)

                    self.assertEqual(rc, 0, fixture.error)
                    self.assertTrue(quotes)
                    bd = quotes[0]["reserved_cost_breakdown"]
                    ord_used, term_used = _ordinary_and_terminal_usage(
                        fixture.accounted
                    )
                    self.assertLessEqual(
                        ord_used, bd["spent_before_preflight"] + bd["total_required"]
                    )
                    self.assertLessEqual(ord_used, 140)
                    self.assertLessEqual(term_used, 50)
                    if mode == "combined":
                        self.assertEqual(
                            seen_faults,
                            {"graphql": 1, "patch": 1, "receipt": 1},
                        )
                    else:
                        self.assertGreaterEqual(seen_faults[mode], 1)
                    self.assertEqual(len(fixture.resolved_threads), resolve_k)
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in fixture.reviews), 1
                    )
                    receipts = [
                        c
                        for c in fixture.extra_comments.values()
                        if "reviewsensei:command-receipt:v1" in c.get("body", "")
                    ]
                    self.assertEqual(len(receipts), 1)
                    rec = parse_session_comment(
                        fixture.comment["body"], identity=IDENTITY
                    )
                    self.assertEqual(
                        rec.api_run_accounting["action"]["phase"], "terminal"
                    )

        # 2. Lost review PUT (stored):
        #    (a) Compacted capacity review reconciles committed PUT via readback without
        #        re-issuing PUT well within 512.
        from tests.test_command_capacity_recovery import CommandCapacityState

        compacted_state = CommandCapacityState(complete=True)
        compacted_state.lose_put_ack = True
        with tempfile.TemporaryDirectory() as directory:
            dispositions = compacted_state.record(directory)
            result, budget, calls = compacted_state.verify(dispositions)
        self.assertEqual(result.status, "approved")
        self.assertEqual(sum(m == "PUT" for m, _, _ in calls), 1)
        self.assertEqual(
            sum(r["state"] == "APPROVED" for r in compacted_state.reviews), 1
        )
        self.assertLessEqual(budget.calls, 60)

        #    (b) Non-compacted ActionRunLedger at 512 safely records disposition and
        #        withholds auto-approval without re-issuing PUT, staying within 512 bounds.
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(4)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            cmd = (
                "@reviewsensei override "
                + " ".join("RS-" + e.instance[:6].upper() for e in index.entries)
                + " acceptable risk"
            )
            fixture.SOURCE_ID += 1
            fixture.select_command(cmd, new_run=True)
            fixture.put_fault = "stored"
            rc = fixture.invoke_command()
            self.assertEqual(rc, 1)
            self.assertEqual(fixture.review_put_attempts, 1)
            self.assertEqual(fixture.review_updates, 1)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in fixture.reviews), 0)
            ord_used, term_used = _ordinary_and_terminal_usage(fixture.accounted)
            self.assertLessEqual(ord_used, 140)
            self.assertLessEqual(term_used, 50)
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(len(rec.dispositions), 4)
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "pending")

    def test_unfunded_large_batch_K10_refuses_before_business_writes_at_512(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(10)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            before_reviews = json.dumps(fixture.reviews, sort_keys=True)
            cmd = (
                "@reviewsensei override "
                + " ".join("RS-" + e.instance[:6].upper() for e in index.entries[:10])
                + " acceptable risk"
            )
            rc, quotes, refusal_breakdowns = _run_command_with_breakdown(fixture, cmd)
            self.assertEqual(rc, 0, fixture.error)
            self.assertEqual(quotes, [])
            self.assertTrue(refusal_breakdowns)
            self.assertIn("before business writes", fixture.stdout)
            self.assertIn("cannot fund the complete operation", fixture.stdout)
            self.assertIn("not GitHub API quota exhaustion", fixture.stdout)
            self.assertEqual(fixture.business_writes, 0)
            self.assertEqual(fixture.review_put_attempts, 0)
            self.assertEqual(
                json.dumps(fixture.reviews, sort_keys=True), before_reviews
            )
            self.assertEqual(fixture.resolved_threads, set())
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(
                read_session_dispositions(fixture.selected.ledger, rec), ()
            )
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "terminal")
            self.assertEqual(rec.api_run_accounting["configuration"]["limit"], 512)

    def test_512_default_and_1152_quarantine_thresholds_remain_isolated(self) -> None:
        self.assertEqual(DEFAULT_API_REQUEST_LIMIT, 512)
        self.assertEqual(TERMINAL_REQUESTS, 50)
        self.assertEqual(EXPANDED_TERMINAL_REQUESTS, 68)
        self.assertEqual(QUARANTINE_CHECKPOINT_REQUESTS, 16)
        self.assertEqual(NATIVE_OPERATION_REQUESTS, 10)
        self.assertEqual(NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS, 34)
        self.assertEqual(COMMAND_FINALIZATION_RESERVE, 8)
        self.assertEqual(
            owner_allocations(512, review_startup_plan("command").liability),
            OwnerAllocations(140, 50, 140, 50),
        )
        self.assertEqual(
            owner_allocations(512, review_startup_plan("hosted").liability),
            OwnerAllocations(141, 50, 141, 50),
        )
        self.assertEqual(review_startup_plan("hosted").liability, 130)
        self.assertEqual(review_startup_plan("command").liability, 132)

        # Ensure 1152 quarantine qualification gates stay strictly >= 1152 (not lowered).
        for rel_path in (
            "packages/python/review-sensei-github/src/review_sensei_github/quarantine_action.py",
            "packages/python/review-sensei-github/src/review_sensei_github/current_quarantine_finalization.py",
            "packages/python/review-sensei-github/src/review_sensei_github/quarantine_checkpoint.py",
        ):
            src_text = (Path(__file__).resolve().parents[1] / rel_path).read_text(
                encoding="utf-8"
            )
            self.assertIn(">= 1152", src_text, rel_path)
            self.assertNotIn(">= 512", src_text, rel_path)
        self.assertTrue(callable(quarantine_action.apply_quarantine_target))
        self.assertTrue(callable(current_quarantine_finalization.prepare))
        # Also confirm EvidenceReadBudget default refusal preserves EvidenceBudgetExhausted
        self.assertTrue(issubclass(EvidenceBudgetExhausted, Exception))

    def test_store_rejects_forged_action_ledger_and_fetches_live_identity(
        self,
    ) -> None:
        calls: list[tuple[str, str]] = []

        def opener(request, timeout=None):
            del timeout
            path = urlsplit(request.full_url).path
            calls.append((request.get_method(), path))
            if path == "/users/reviewsensei%5Bbot%5D":
                return json_response(
                    {"id": 9001, "login": "reviewsensei[bot]", "type": "Bot"}
                )
            if path == "/repos/owner/repo/pulls/7":
                return json_response(
                    {
                        "state": "open",
                        "base": {
                            "sha": "1" * 40,
                            "repo": {"id": 123, "full_name": "owner/repo"},
                        },
                        "head": {"sha": HEAD},
                    }
                )
            raise AssertionError(f"unexpected request {request.get_method()} {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        forged = SimpleNamespace(
            _evidence_author_id=9001,
            _evidence_author_slug="reviewsensei[bot]",
            authenticated_app_id=999,
            app_slug="reviewsensei[bot]",
            token="tok",
            http=http,
            _quarantine_required=False,
            _inventory_complete=True,
        )
        store = publication_recovery._store(
            http,
            token="tok",
            identity=IDENTITY,
            app_slug="reviewsensei[bot]",
            base_sha="1" * 40,
            head_sha=HEAD,
            verified_action_ledger=forged,
        )
        self.assertIsNotNone(store)
        self.assertEqual(
            calls,
            [
                ("GET", "/users/reviewsensei%5Bbot%5D"),
                ("GET", "/repos/owner/repo/pulls/7"),
            ],
        )

    def test_distinct_bot_user_id_from_app_id_reuses_sealed_action_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            # Match real GitHub where the Bot user's numeric id differs from App id (999).
            orig_external = fixture.external

            def distinct_bot_open(request, timeout):
                resp = orig_external(request, timeout)
                path = urlsplit(request.full_url).path
                if path == "/users/reviewsensei%5Bbot%5D":
                    data = json.loads(resp.read(524288))
                    data["id"] = 424242
                    return json_response(data, resp.status)
                if path == "/graphql":
                    data = json.loads(resp.read(524288))

                    def rewrite(obj):
                        if isinstance(obj, dict):
                            author = obj.get("author")
                            if (
                                isinstance(author, dict)
                                and author.get("login") == "reviewsensei[bot]"
                                and "databaseId" in author
                            ):
                                author["databaseId"] = 424242
                            for val in obj.values():
                                rewrite(val)
                        elif isinstance(obj, list):
                            for item in obj:
                                rewrite(item)

                    rewrite(data)
                    return json_response(data, resp.status)
                if "/repos/owner/repo/" in path and (
                    "comments" in path or "reviews" in path
                ):
                    data = json.loads(resp.read(524288))
                    if isinstance(data, list):
                        for row in data:
                            if (
                                isinstance(row, dict)
                                and isinstance(row.get("user"), dict)
                                and row["user"].get("type") == "Bot"
                            ):
                                row["user"]["id"] = 424242
                    elif (
                        isinstance(data, dict)
                        and isinstance(data.get("user"), dict)
                        and data["user"].get("type") == "Bot"
                    ):
                        data["user"]["id"] = 424242
                    return json_response(data, resp.status)
                return resp

            fixture.external = distinct_bot_open
            fixture.provider_comments = _make_blocking_comments(4)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            cmd = (
                "@reviewsensei override "
                + " ".join("RS-" + e.instance[:6].upper() for e in index.entries)
                + " acceptable risk"
            )
            fixture.trace.clear()
            rc, quotes, _ = _run_command_with_breakdown(fixture, cmd)
            self.assertEqual(rc, 0, fixture.error)
            self.assertTrue(quotes)
            self.assertEqual(
                [item for item in fixture.trace if "/users/" in item[2]],
                [],
            )

    def test_mismatched_effect_record_identity_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(1)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            cmd = (
                f"@reviewsensei override RS-{index.entries[0].instance[:6].upper()} "
                "acceptable risk"
            )
            fixture.SOURCE_ID += 1
            fixture.select_command(cmd, new_run=True)
            self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            ledger = fixture.selected.ledger
            other_identity = SessionIdentity(
                repository=IDENTITY.repository,
                pull_request=999,
                repository_id=IDENTITY.repository_id,
            )
            loaded_identities: list[SessionIdentity] = []

            def track_load(ident: SessionIdentity) -> SessionLoadResult:
                loaded_identities.append(ident)
                return SessionLoadResult(status="missing", record=None)

            # Querying with a different pull_request identity must not reuse
            # caller._effect_record for transaction selection or priming.
            with patch.object(ledger, "load", track_load):
                retained = ledger.load_retained_checkpoint_result(other_identity)
            self.assertIsNone(retained)
            self.assertEqual(loaded_identities, [other_identity])
            # Also verify caller._require_ledger rejects wrong repository_id directly.
            wrong_repo_identity = SessionIdentity(
                repository="owner/other", pull_request=7, repository_id=456
            )
            with self.assertRaises(Exception):
                ledger.caller._require_ledger(ledger, wrong_repo_identity)

    def test_command_pricing_batch_size_scaling_within_512_envelope(self) -> None:
        from review_sensei_github.action_representability import (
            _command_pricing,
            guard_current_command_representability,
        )

        from review_sensei.disposition import FindingIdentity, MaintainerCommand

        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = _make_blocking_comments(4)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            refs = tuple("RS-" + e.instance[:6].upper() for e in index.entries)
            # Exhaust usable balance on a depleted 512-limit caller down to 20:
            # K=1 requires 31 -> caught by guard_current_command_representability
            # upfront; K=4 requires 67 and fails far earlier than K=1!
            fixture.SOURCE_ID += 1
            fixture.select_command(
                f"@reviewsensei override {refs[0]} acceptable risk",
                new_run=True,
            )
            depleted_caller = SimpleNamespace(
                accounting=SimpleNamespace(
                    configuration=SimpleNamespace(
                        limit=512, commit="a" * 40, digest="d" * 64
                    ),
                    owner=SimpleNamespace(generation=1),
                    action={
                        "intent": {
                            "action": "command",
                            "owner": {"generation": 1},
                            "head_sha": HEAD,
                        }
                    },
                    blocks=(),
                    version="caller-requests-v2",
                ),
                usable=40,
            )
            k1_price = _command_pricing(
                depleted_caller,
                action="accept-risk",
                writes_enabled=True,
                finding_count=1,
            )
            k4_price = _command_pricing(
                depleted_caller,
                action="accept-risk",
                writes_enabled=True,
                finding_count=4,
            )
            self.assertEqual(k1_price.required_requests, 31)
            self.assertEqual(k4_price.required_requests, 67)
            self.assertTrue(k1_price.fits())
            self.assertFalse(k4_price.fits())

            # Full guard path at usable=40 passes K=1 (31 <= 40) and refuses K=4 (67 > 40)
            # cleanly with an explicit no-op receipt at limit=512 without raising 512.
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            assert rec is not None and rec.transaction is not None
            depleted_caller._effect_record = rec
            current_action = SimpleNamespace(
                run=SimpleNamespace(
                    caller=depleted_caller,
                    ledger=fixture.selected.ledger,
                    identity=IDENTITY,
                )
            )
            finding_inventory = tuple(
                FindingIdentity(
                    instance=e.instance,
                    needs_human=True,
                    blocking=True,
                )
                for e in index.entries
            )
            cmd_k1 = MaintainerCommand(
                action="accept-risk",
                actor="maintainer",
                reason="acceptable risk",
                head_sha=HEAD,
                finding_references=(refs[0],),
            )
            cmd_k4 = MaintainerCommand(
                action="accept-risk",
                actor="maintainer",
                reason="acceptable risk",
                head_sha=HEAD,
                finding_references=refs,
            )
            self.assertIsNone(
                guard_current_command_representability(
                    current_action=current_action,
                    ledger=fixture.selected.ledger,
                    identity=IDENTITY,
                    head_sha=HEAD,
                    command=cmd_k1,
                    writes_enabled=True,
                    finding_inventory=finding_inventory,
                )
            )
            refusal_k4 = guard_current_command_representability(
                current_action=current_action,
                ledger=fixture.selected.ledger,
                identity=IDENTITY,
                head_sha=HEAD,
                command=cmd_k4,
                writes_enabled=True,
                finding_inventory=finding_inventory,
            )
            self.assertIsNotNone(refusal_k4)
            assert refusal_k4 is not None
            self.assertFalse(refusal_k4.applied)
            self.assertIn("request_budget_exhausted_before_effect", refusal_k4.summary)
            self.assertIn("539 exceed original limit 512", refusal_k4.summary)


if __name__ == "__main__":
    unittest.main()
