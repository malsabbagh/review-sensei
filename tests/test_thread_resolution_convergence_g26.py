"""G26: Manual thread resolve/reopen convergence via bounded ``verify`` tests."""

from __future__ import annotations

import unittest
from typing import Any
from urllib.parse import quote

from review_sensei_github.approval import ApprovalFacts, ReviewApprovalEligibility
from review_sensei_github.event_routing_contract import (
    LivePullRequestTarget,
    ManualThreadFindingState,
    evaluate_github_event,
    reconcile_manual_thread_convergence,
)
from review_sensei_github.thread_resolution import (
    FindingThreadResolver,
    digest,
    resolution_marker,
    resolution_records,
)

from review_sensei.bounded_evidence import EvidenceReadBudget

BASE_SHA = "1" * 40
BASE_SHA_2 = "2" * 40
HEAD_SHA_1 = "a" * 40
HEAD_SHA_2 = "b" * 40
RESULT_DIGEST = "c" * 64
INSTANCE_HEX = "d" * 64
CONCERN_HEX = "e" * 64
EVIDENCE_HEX = "f" * 64


def _app_root_body(
    *,
    repository_id: int = 101,
    pull_request: int = 42,
    head_sha: str = HEAD_SHA_1,
    base_sha: str = BASE_SHA,
    concern: str = CONCERN_HEX,
    instance: str = INSTANCE_HEX,
) -> str:
    return (
        "Blocking issue on line 12.\n"
        f"<!-- reviewsensei:finding:v2 repo={repository_id} pr={pull_request} "
        f"head={head_sha} base={base_sha} fingerprint={concern} "
        f"state=new blocking=true human=false -->\n"
        f"<!-- reviewsensei:instance:v1 instance={instance} concern={concern} -->"
    )


class _FakeThreadHttp:
    """In-memory HTTP fake exercising real FindingThreadResolver GraphQL + REST fences."""

    api_url = "https://api.github.com"

    def __init__(
        self,
        *,
        thread_id: str,
        is_resolved: bool,
        root_id: int,
        root_body: str,
        source_id: int,
        source_body: str,
        source_updated: str,
        repository: str = "owner/repo",
        pull_request: int = 42,
        app_slug: str = "reviewsensei[bot]",
    ) -> None:
        self.thread_id = thread_id
        self.is_resolved = is_resolved
        self.root_id = root_id
        self.root_body = root_body
        self.source_id = source_id
        self.source_body = source_body
        self.source_updated = source_updated
        self.repository = repository
        self.pull_request = pull_request
        self.app_slug = app_slug
        self.graphql_queries = 0
        self.graphql_mutations = 0
        self.rest_reads = 0

    def repository_path(self, repository: str, suffix: str = "") -> str:
        owner, _, name = repository.partition("/")
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}{suffix}"

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        assert token
        expected_owner, _, expected_name = self.repository.partition("/")
        if method == "POST" and path == "/graphql":
            assert body is not None
            op_name = body.get("operationName")
            variables = body.get("variables")
            assert isinstance(variables, dict)
            if op_name == "FindingResolutionThreads":
                assert variables.get("owner") == expected_owner
                assert variables.get("name") == expected_name
                assert variables.get("number") == self.pull_request
                assert variables.get("after") is None
                self.graphql_queries += 1
                return (
                    200,
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": self.thread_id,
                                                "isResolved": self.is_resolved,
                                                "comments": {
                                                    "nodes": [
                                                        {
                                                            "databaseId": self.root_id,
                                                            "body": self.root_body,
                                                            "author": {
                                                                "login": "reviewsensei",
                                                                "__typename": "Bot",
                                                            },
                                                        }
                                                    ]
                                                },
                                            }
                                        ],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    },
                )
            if op_name == "ResolveReviewThread":
                assert variables.get("input") == {"threadId": self.thread_id}
                self.graphql_mutations += 1
                self.is_resolved = True
                return (
                    200,
                    {
                        "data": {
                            "resolveReviewThread": {
                                "thread": {
                                    "id": self.thread_id,
                                    "isResolved": True,
                                }
                            }
                        }
                    },
                )
        root_comment_path = self.repository_path(
            self.repository, f"/pulls/comments/{self.root_id}"
        )
        source_comment_path = self.repository_path(
            self.repository, f"/pulls/comments/{self.source_id}"
        )
        pr_api_url = (
            f"https://api.github.com/repos/{self.repository}/pulls/{self.pull_request}"
        )
        if method == "GET" and path == root_comment_path:
            self.rest_reads += 1
            return (
                200,
                {
                    "id": self.root_id,
                    "body": self.root_body,
                    "pull_request_url": pr_api_url,
                    "user": {"login": self.app_slug, "type": "Bot"},
                },
            )
        if method == "GET" and path == source_comment_path:
            self.rest_reads += 1
            return (
                200,
                {
                    "id": self.source_id,
                    "in_reply_to_id": self.root_id,
                    "pull_request_url": pr_api_url,
                    "updated_at": self.source_updated,
                    "body": self.source_body,
                    "author_association": "MEMBER",
                    "user": {"login": "maintainer", "type": "User"},
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def _make_eligibility(
    *,
    head_sha: str = HEAD_SHA_1,
    has_blocking: bool = True,
    has_human: bool = False,
) -> ReviewApprovalEligibility:
    return ReviewApprovalEligibility(
        head_sha=head_sha,
        result_digest=RESULT_DIGEST,
        facts=ApprovalFacts(
            enabled=True,
            app_authored=False,
            review_status="complete",
            evidence_policy="confirmed",
            has_blocking_findings=has_blocking,
            has_human_adjudication_findings=has_human,
            coverage_blocker="reviewed",
            qualification="not-required",
        ),
    )


def _live_target(
    *, head_sha: str = HEAD_SHA_1, base_sha: str = BASE_SHA
) -> LivePullRequestTarget:
    return LivePullRequestTarget(
        repository_id=101,
        pull_request_number=42,
        base_sha=base_sha,
        base_ref="main",
        head_sha=head_sha,
        head_ref="feat/thread-convergence",
    )


class ThreadResolutionConvergenceG26Tests(unittest.TestCase):
    def test_manual_human_resolve_without_fix_withholds_approval_and_preserves_obligation(
        self,
    ) -> None:
        # Duplicate native thread `resolved` events trigger zero inference and zero loss.
        for delivery_id in ("deliv-thread-1", "deliv-thread-1-dup"):
            routed = evaluate_github_event(
                "pull_request_review_thread",
                {
                    "action": "resolved",
                    "pull_request": {"number": 42},
                    "thread": {"node_id": "PRRT_1"},
                    "sender": {"login": "dev-author", "type": "User"},
                },
                delivery_id=delivery_id,
            )
            self.assertFalse(routed.caller_subscribed)
            self.assertFalse(routed.admitted)
            self.assertFalse(routed.requires_model_inference)
            self.assertEqual(routed.outcome_code, "thread_event_manual_verify_only")
            self.assertEqual(routed.recovery_command, "@reviewsensei verify")

        # Human resolved the GitHub thread (`native_thread_resolved=True`),
        # but the finding is neither fixed nor overridden (`disposition_proven=False`).
        finding = ManualThreadFindingState(
            finding_id="RS-100001",
            instance_fingerprint=INSTANCE_HEX,
            blocking=True,
            needs_human=False,
            disposition_proven=False,
            thread_id="PRRT_1",
            thread_base_sha=BASE_SHA,
            thread_head_sha=HEAD_SHA_1,
            native_thread_resolved=True,
        )
        outcome = reconcile_manual_thread_convergence(
            live_target=_live_target(),
            eligibility=_make_eligibility(has_blocking=True),
            reviewed_base_sha=BASE_SHA,
            findings=(finding,),
            finite_api_reads_charged=2,
        )
        self.assertFalse(outcome.approved)
        self.assertEqual(outcome.status, "withheld")
        self.assertEqual(
            outcome.outcome_code, "manual_thread_resolve_withheld_unfixed_obligation"
        )
        self.assertEqual(outcome.withheld_diagnostic, "required_fixes_open")
        self.assertIn("blocking-findings-open", outcome.approval_decision.blockers)
        self.assertFalse(outcome.requires_model_inference)
        self.assertEqual(outcome.preserved_open_finding_ids, ("RS-100001",))
        self.assertEqual(outcome.finite_api_reads_charged, 2)

        # Same guarantee for human-adjudication findings manually resolved without decision.
        human_finding = ManualThreadFindingState(
            finding_id="RS-100002",
            instance_fingerprint=INSTANCE_HEX,
            blocking=False,
            needs_human=True,
            disposition_proven=False,
            thread_id="PRRT_2",
            thread_base_sha=BASE_SHA,
            thread_head_sha=HEAD_SHA_1,
            native_thread_resolved=True,
        )
        human_outcome = reconcile_manual_thread_convergence(
            live_target=_live_target(),
            eligibility=_make_eligibility(has_blocking=False, has_human=True),
            reviewed_base_sha=BASE_SHA,
            findings=(human_finding,),
        )
        self.assertFalse(human_outcome.approved)
        self.assertEqual(human_outcome.withheld_diagnostic, "human_adjudication_open")
        self.assertEqual(human_outcome.preserved_open_finding_ids, ("RS-100002",))

        # Unified open-obligation predicate preserves BOTH natively-resolved-unproven
        # findings and reopened-decided findings while excluding fully converged and
        # non-blocking advisory items.
        mixed_findings = (
            ManualThreadFindingState(
                finding_id="RS-100010",
                instance_fingerprint="1" * 64,
                blocking=True,
                needs_human=False,
                disposition_proven=False,
                thread_id="PRRT_10",
                thread_base_sha=BASE_SHA,
                thread_head_sha=HEAD_SHA_1,
                native_thread_resolved=True,
            ),
            ManualThreadFindingState(
                finding_id="RS-100011",
                instance_fingerprint="2" * 64,
                blocking=True,
                needs_human=False,
                disposition_proven=True,
                thread_id="PRRT_11",
                thread_base_sha=BASE_SHA,
                thread_head_sha=HEAD_SHA_1,
                native_thread_resolved=False,
            ),
            ManualThreadFindingState(
                finding_id="RS-100012",
                instance_fingerprint="3" * 64,
                blocking=True,
                needs_human=False,
                disposition_proven=True,
                thread_id="PRRT_12",
                thread_base_sha=BASE_SHA,
                thread_head_sha=HEAD_SHA_1,
                native_thread_resolved=True,
            ),
            ManualThreadFindingState(
                finding_id="RS-100013",
                instance_fingerprint="4" * 64,
                blocking=False,
                needs_human=False,
                disposition_proven=False,
                thread_id="PRRT_13",
                thread_base_sha=BASE_SHA,
                thread_head_sha=HEAD_SHA_1,
                native_thread_resolved=False,
            ),
        )
        mixed_outcome = reconcile_manual_thread_convergence(
            live_target=_live_target(),
            eligibility=_make_eligibility(has_blocking=True),
            reviewed_base_sha=BASE_SHA,
            findings=mixed_findings,
        )
        self.assertFalse(mixed_outcome.approved)
        self.assertEqual(
            mixed_outcome.outcome_code,
            "manual_thread_resolve_withheld_unfixed_obligation",
        )
        self.assertEqual(
            mixed_outcome.preserved_open_finding_ids,
            ("RS-100010", "RS-100011"),
        )

    def test_manual_reopen_reconciles_via_bounded_verify_and_converges_idempotently(
        self,
    ) -> None:
        # Native `unresolved` event triggers zero hidden inference.
        reopen_event = evaluate_github_event(
            "pull_request_review_thread",
            {
                "action": "unresolved",
                "pull_request": {"number": 42},
                "sender": {"login": "reviewer", "type": "User"},
            },
        )
        self.assertFalse(reopen_event.caller_subscribed)
        self.assertFalse(reopen_event.admitted)
        self.assertFalse(reopen_event.requires_model_inference)
        self.assertEqual(reopen_event.outcome_code, "thread_event_manual_verify_only")
        self.assertEqual(reopen_event.recovery_command, "@reviewsensei verify")

        # Maintainer legitimately overrode RS-100001, then someone manually clicked "Unresolve".
        reopened_finding = ManualThreadFindingState(
            finding_id="RS-100001",
            instance_fingerprint=INSTANCE_HEX,
            blocking=True,
            needs_human=False,
            disposition_proven=True,
            thread_id="PRRT_1",
            thread_base_sha=BASE_SHA,
            thread_head_sha=HEAD_SHA_1,
            native_thread_resolved=False,
        )

        # Before resolver executes, open thread still withholds approval and preserves ID.
        pre_resolve = reconcile_manual_thread_convergence(
            live_target=_live_target(),
            eligibility=_make_eligibility(has_blocking=False),
            reviewed_base_sha=BASE_SHA,
            findings=(reopened_finding,),
        )
        self.assertFalse(pre_resolve.approved)
        self.assertEqual(
            pre_resolve.outcome_code,
            "reopened_decided_thread_requires_verify_resolution",
        )
        self.assertIn("review-threads-open", pre_resolve.approval_decision.blockers)
        self.assertEqual(pre_resolve.preserved_open_finding_ids, ("RS-100001",))

        # Run real FindingThreadResolver under `@reviewsensei verify` with finite budget.
        source_body = (
            f"@reviewsensei accept-risk {INSTANCE_HEX[:16]} --reason verified safe"
        )
        source_updated = "2026-11-11T02:00:00Z"
        record_payload = {
            "repo": 101,
            "pr": 42,
            "head": HEAD_SHA_1,
            "base": BASE_SHA,
            "result": RESULT_DIGEST,
            "instance": INSTANCE_HEX,
            "root": 3001,
            "source": 3002,
            "updated": source_updated,
            "source_digest": digest(source_body),
            "actor": "maintainer",
            "evidence": EVIDENCE_HEX,
        }
        reviews = [
            {
                "user": {"login": "reviewsensei[bot]"},
                "commit_id": HEAD_SHA_1,
                "body": f"Disposition recorded.\n{resolution_marker(record_payload)}",
            }
        ]
        parsed_records = resolution_records(reviews, "reviewsensei[bot]")
        self.assertEqual(len(parsed_records), 1)

        fake_http = _FakeThreadHttp(
            thread_id="PRRT_1",
            is_resolved=False,
            root_id=3001,
            root_body=_app_root_body(),
            source_id=3002,
            source_body=source_body,
            source_updated=source_updated,
        )
        resolver = FindingThreadResolver(fake_http)  # type: ignore[arg-type]
        budget = EvidenceReadBudget()
        cleanup_token_calls = 0

        def issue_cleanup_token() -> str:
            nonlocal cleanup_token_calls
            cleanup_token_calls += 1
            return "cleanup-cap-token"

        first_res = resolver.resolve(
            token="read-token",
            repository="owner/repo",
            repository_id=101,
            pull_request=42,
            head_sha=HEAD_SHA_1,
            base_sha=BASE_SHA,
            app_slug="reviewsensei[bot]",
            instances=frozenset({INSTANCE_HEX}),
            fence=lambda: True,
            records=parsed_records,
            skip_resolved=True,
            cleanup_budget=budget,
            cleanup_token_provider=issue_cleanup_token,
        )
        self.assertEqual(first_res.resolved, (INSTANCE_HEX,))
        self.assertEqual(first_res.unconfirmed, ())
        self.assertEqual(cleanup_token_calls, 1)
        self.assertEqual(fake_http.graphql_queries, 1)
        self.assertEqual(fake_http.graphql_mutations, 1)
        self.assertEqual(fake_http.rest_reads, 4)

        first_verify = reconcile_manual_thread_convergence(
            live_target=_live_target(),
            eligibility=_make_eligibility(has_blocking=False),
            reviewed_base_sha=BASE_SHA,
            findings=(reopened_finding,),
            thread_resolution_result=first_res,
            finite_api_reads_charged=fake_http.graphql_queries + fake_http.rest_reads,
        )
        self.assertTrue(first_verify.approved)
        self.assertEqual(first_verify.status, "converged_approved")
        self.assertEqual(first_verify.outcome_code, "verify_converged_to_approval")
        self.assertFalse(first_verify.requires_model_inference)
        self.assertEqual(first_verify.threads_reconciled_by_mutation, (INSTANCE_HEX,))
        self.assertEqual(first_verify.preserved_open_finding_ids, ())
        self.assertEqual(first_verify.finite_api_reads_charged, 5)

        # Second `@reviewsensei verify` against a fresh already-resolved server snapshot:
        # 1 discovery query, 0 REST reads, 0 mutations, 0 cleanup tokens.
        second_fake_http = _FakeThreadHttp(
            thread_id="PRRT_1",
            is_resolved=True,
            root_id=3001,
            root_body=_app_root_body(),
            source_id=3002,
            source_body=source_body,
            source_updated=source_updated,
        )
        second_resolver = FindingThreadResolver(second_fake_http)  # type: ignore[arg-type]
        second_res = second_resolver.resolve(
            token="read-token",
            repository="owner/repo",
            repository_id=101,
            pull_request=42,
            head_sha=HEAD_SHA_1,
            base_sha=BASE_SHA,
            app_slug="reviewsensei[bot]",
            instances=frozenset({INSTANCE_HEX}),
            fence=lambda: True,
            records=parsed_records,
            skip_resolved=True,
            cleanup_token_provider=issue_cleanup_token,
        )
        self.assertEqual(second_res.resolved, (INSTANCE_HEX,))
        self.assertEqual(cleanup_token_calls, 1)  # unchanged!
        self.assertEqual(second_fake_http.graphql_queries, 1)
        self.assertEqual(second_fake_http.graphql_mutations, 0)
        self.assertEqual(second_fake_http.rest_reads, 0)

        already_resolved_finding = ManualThreadFindingState(
            finding_id="RS-100001",
            instance_fingerprint=INSTANCE_HEX,
            blocking=True,
            needs_human=False,
            disposition_proven=True,
            thread_id="PRRT_1",
            thread_base_sha=BASE_SHA,
            thread_head_sha=HEAD_SHA_1,
            native_thread_resolved=True,
        )
        second_verify = reconcile_manual_thread_convergence(
            live_target=_live_target(),
            eligibility=_make_eligibility(has_blocking=False),
            reviewed_base_sha=BASE_SHA,
            findings=(already_resolved_finding,),
            thread_resolution_result=second_res,
            finite_api_reads_charged=1,
        )
        self.assertTrue(second_verify.approved)
        self.assertEqual(
            second_verify.threads_observed_already_resolved, (INSTANCE_HEX,)
        )
        self.assertEqual(second_verify.threads_reconciled_by_mutation, ())

    def test_changed_head_and_stale_thread_mapping_refuse_cleanly(self) -> None:
        finding = ManualThreadFindingState(
            finding_id="RS-100001",
            instance_fingerprint=INSTANCE_HEX,
            blocking=True,
            needs_human=False,
            disposition_proven=False,
            thread_id="PRRT_1",
            thread_base_sha=BASE_SHA,
            thread_head_sha=HEAD_SHA_1,
            native_thread_resolved=True,
        )

        # Changed head on live PR refuses stale snapshot approval.
        changed_head = reconcile_manual_thread_convergence(
            live_target=_live_target(head_sha=HEAD_SHA_2),
            eligibility=_make_eligibility(head_sha=HEAD_SHA_1),
            reviewed_base_sha=BASE_SHA,
            findings=(finding,),
        )
        self.assertFalse(changed_head.approved)
        self.assertEqual(changed_head.status, "held_head_or_base_changed")
        self.assertEqual(changed_head.outcome_code, "verify_head_or_base_changed")
        self.assertEqual(changed_head.preserved_open_finding_ids, ("RS-100001",))
        self.assertEqual(changed_head.recovery_command, "workflow_dispatch")

        # Stale thread mapping (thread bound to HEAD_SHA_1 while eligibility/live is HEAD_SHA_2).
        stale_thread = ManualThreadFindingState(
            finding_id="RS-100001",
            instance_fingerprint=INSTANCE_HEX,
            blocking=True,
            needs_human=False,
            disposition_proven=True,
            thread_id="PRRT_OLD",
            thread_base_sha=BASE_SHA,
            thread_head_sha=HEAD_SHA_1,
            native_thread_resolved=True,
        )
        stale_outcome = reconcile_manual_thread_convergence(
            live_target=_live_target(head_sha=HEAD_SHA_2),
            eligibility=_make_eligibility(head_sha=HEAD_SHA_2, has_blocking=False),
            reviewed_base_sha=BASE_SHA,
            findings=(stale_thread,),
        )
        self.assertFalse(stale_outcome.approved)
        self.assertEqual(
            stale_outcome.status, "held_stale_or_unconfirmed_thread_mapping"
        )
        self.assertEqual(
            stale_outcome.outcome_code, "stale_or_unconfirmed_thread_mapping"
        )
        self.assertEqual(stale_outcome.withheld_diagnostic, "review_threads_incomplete")
        self.assertEqual(stale_outcome.unconfirmed_instances, (INSTANCE_HEX,))

        with self.assertRaises(ValueError):
            ManualThreadFindingState(
                finding_id="",
                instance_fingerprint=INSTANCE_HEX,
                blocking=True,
                needs_human=False,
                disposition_proven=False,
                thread_id="PRRT_1",
                thread_base_sha=BASE_SHA,
                thread_head_sha=HEAD_SHA_1,
                native_thread_resolved=False,
            )
        with self.assertRaises(ValueError):
            reconcile_manual_thread_convergence(
                live_target=_live_target(),
                eligibility=_make_eligibility(),
                reviewed_base_sha="invalid",
                findings=(),
            )
        with self.assertRaises(ValueError):
            reconcile_manual_thread_convergence(
                live_target=_live_target(),
                eligibility=_make_eligibility(),
                reviewed_base_sha=BASE_SHA,
                findings=(),
                finite_api_reads_charged=-1,
            )

    def test_verify_keeps_fix_and_ambiguity_gates_separate_from_human_only(self):
        import tempfile
        from contextlib import nullcontext
        from dataclasses import replace
        from unittest.mock import patch

        from review_sensei_github.publication import ReviewPublisher
        from test_action_run import CommandEntryFixture

        for scenario, conclusion in (
            ("human", "action_required"),
            ("mixed", "failure"),
            ("ambiguous", "failure"),
        ):
            with (
                self.subTest(scenario=scenario),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CommandEntryFixture(directory, limit=512)
                f.provider_comments = [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Confirm caller contract.",
                        "symbol": "caller_contract",
                        "severity": "high",
                        "blocking": False,
                        "needs_human": True,
                    },
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Confirm authorization contract.",
                        "symbol": "authorization",
                        "severity": "high",
                        "blocking": False,
                        "needs_human": True,
                    },
                ]
                original = ReviewPublisher._plan_drift_round
                witnessed = []

                def qualified_plan(publisher, **kwargs):
                    # Domain classification seam only: generate a mixed/ambiguous
                    # plan before the ordinary producer persists it. The later
                    # command uses only its actual authenticated carrier and native
                    # projections, with no consumer-state or authority injection.
                    state, key = original(publisher, **kwargs)
                    item = state.inventory.obligations[1]
                    if scenario == "mixed":
                        item = replace(
                            item, criterion=replace(item.criterion, blocking=True)
                        )
                    else:
                        from review_sensei.finding_drift import (
                            FileDriftDescriptor,
                            evaluate_drift_round,
                        )

                        state = evaluate_drift_round(
                            kwargs["result"],
                            head_sha=kwargs["head_sha"],
                            base_sha=kwargs["base_sha"],
                            max_active_findings=20,
                            round_number=state.round_number + 1,
                            prior_inventory=state.inventory,
                            drift_descriptors=(
                                FileDriftDescriptor(
                                    "cross_file_move",
                                    "src/app.py",
                                    ("src/app.py", "src/other.py"),
                                ),
                            ),
                            diff_text=kwargs["diff"],
                        )
                        witnessed.extend(state.inventory.obligations)
                        return state, key
                    witnessed.append(item)
                    return replace(
                        state,
                        inventory=replace(
                            state.inventory,
                            obligations=(state.inventory.obligations[0], item),
                        ),
                    ), key

                scope = (
                    patch.object(ReviewPublisher, "_plan_drift_round", qualified_plan)
                    if scenario != "human"
                    else nullcontext()
                )
                with scope:
                    self.assertEqual(f.invoke(human=True), 0, f.error)
                f.SOURCE_ID += 1
                f.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(f.invoke_command(), 0, f.error)
                self.assertEqual(f.check_runs[-1]["conclusion"], conclusion)
                self.assertEqual(f.provider_calls, 1)
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                self.assertEqual(f.accounted.accounting.configuration.limit, 512)
                if scenario != "human":
                    self.assertTrue(witnessed)
                    self.assertTrue(
                        all(item.effective_blocking_now for item in witnessed)
                    )

    def test_ordinary_caller_verify_command_performs_native_effects_with_zero_inference(
        self,
    ) -> None:
        import hashlib
        import hmac
        import json
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from review_sensei_github.event_routing_contract import (
            map_installed_caller_subscription,
        )
        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.webhooks import WebhookVerifier
        from test_action_run import CommandEntryFixture

        from review_sensei.hosting.github import trigger as hosting_trigger

        repo_root = Path(__file__).resolve().parents[1]
        checked_in = (
            repo_root / ".github/workflows/review-sensei-review.yml"
        ).read_text(encoding="utf-8")

        class _Secret:
            def load_webhook_secret(self) -> bytes:
                return b"g26-secret"

        verifier = WebhookVerifier(app_id=91, secret_source=_Secret())

        with tempfile.TemporaryDirectory() as directory:
            for target in ("socket.create_connection", "socket.socket.connect"):
                guard = patch(target, side_effect=AssertionError("unexpected network"))
                guard.start()
                self.addCleanup(guard.stop)

            f = CommandEntryFixture(directory)
            # 1. Initial review publishes 1 open finding thread; 1 provider call.
            self.assertEqual(f.invoke(human=True), 0, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))

            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=f.requested_head,
            )
            assert index is not None
            finding_ref = "RS-" + index.entries[0].instance[:6].upper()
            self.assertEqual(len(f.threads), 1)
            native_thread_id = f.threads[0]["id"]

            # 2. Native Resolve/Unresolve UI events are unsubscribed no-ops on trigger.py & webhooks.py
            pull_file = Path(directory) / "pull.json"
            out_file = Path(directory) / "out.txt"
            pull_json = {
                "number": 7,
                "state": "open",
                "draft": False,
                "head": {"sha": f.requested_head, "ref": "feat/g26"},
                "base": {"sha": f.live_base, "ref": "main"},
            }
            pull_file.write_text(json.dumps(pull_json), encoding="utf-8")

            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(out_file)}, clear=False),
                self.assertRaises(SystemExit),
            ):
                hosting_trigger.main(
                    [
                        "--event",
                        "pull_request_review_thread",
                        "--pull-json",
                        str(pull_file),
                    ]
                )

            for idx, act in enumerate(("resolved", "resolved", "unresolved"), start=1):
                raw_ev = json.dumps(
                    {
                        "action": act,
                        "pull_request": {"number": 7},
                        "thread": {"node_id": native_thread_id},
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                sig = (
                    "sha256="
                    + hmac.new(b"g26-secret", raw_ev, hashlib.sha256).hexdigest()
                )
                vr = verifier.verify_event_route(
                    body=raw_ev,
                    signature=sig,
                    event="pull_request_review_thread",
                    delivery_id=f"deliv-thread-{idx}",
                )
                self.assertFalse(vr.routing.admitted)
                self.assertFalse(vr.routing.requires_model_inference)

            # 3. Verify installed caller subscription maps `@reviewsensei verify` to
            #    action_run(purpose='command') with performs_native_effects=True & requires_model_inference=False!
            verify_payload = {
                "action": "created",
                "issue": {
                    "number": 7,
                    "pull_request": {
                        "url": "https://api.github.com/repos/owner/repo/pulls/7"
                    },
                },
                "comment": {
                    "id": 9101,
                    "body": "@reviewsensei verify",
                    "updated_at": "2026-11-11T00:00:00Z",
                    "author_association": "OWNER",
                    "user": {"login": "alice", "type": "User"},
                },
            }
            mapped = map_installed_caller_subscription(
                checked_in,
                event_name="issue_comment",
                payload=verify_payload,
                live_pull_request=pull_json,
            )
            self.assertTrue(mapped.caller_job_admitted)
            self.assertEqual(
                mapped.reusable_workflow_entrypoint,
                "action_run.run_action(purpose='command')",
            )
            self.assertTrue(mapped.performs_native_effects)
            self.assertFalse(mapped.requires_model_inference)

            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(out_file)}, clear=False):
                out_file.write_text("", encoding="utf-8")
                self.assertEqual(
                    hosting_trigger.main(
                        [
                            "--event",
                            "issue_comment",
                            "--comment-body",
                            "@reviewsensei verify",
                            "--pull-json",
                            str(pull_file),
                        ]
                    ),
                    0,
                )
            self.assertIn("operation=command", out_file.read_text(encoding="utf-8"))

            # 4. Human clicks native Resolve WITHOUT disposition; `@reviewsensei verify` keeps
            #    approval withheld and charges 0 model inference!
            f.resolved_threads.add(native_thread_id)
            f.threads[0]["isResolved"] = True
            f.SOURCE_ID += 1
            f.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            self.assertEqual(f.check_runs[-1]["conclusion"], "action_required")

            # 5. Legitimate override resolves obligation, then human clicks native Unresolve;
            #    `@reviewsensei verify` performs real native GraphQL ResolveReviewThread + check
            #    effects with 0 model inference!
            f.SOURCE_ID += 1
            f.select_command(
                f"@reviewsensei override {finding_ref} acceptable risk",
                new_run=True,
            )
            self.assertEqual(f.invoke_command(), 0, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)

            # Simulate human manually unresolving the decided thread in GitHub UI
            f.resolved_threads.clear()
            f.threads[0]["isResolved"] = False
            f.SOURCE_ID += 1
            f.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)
            self.assertEqual(f.provider_calls, 1)  # ZERO new inference!
            self.assertEqual(
                len(f.resolved_threads), 1
            )  # Native GraphQL effect performed!
            self.assertEqual(f.check_runs[-1]["conclusion"], "success")


if __name__ == "__main__":
    unittest.main()
