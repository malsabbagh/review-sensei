"""Phase 0 executable contract: state/policy precedence matrix and failing journey tests.

This module freezes the Phase 0 lifecycle, policy, and precedence contract into
executable tests organized by implementation phase:
- Phase0PreservedInvariantsTests: existing security/correctness guarantees that
  must remain green across all phases (generated-file mixed coverage, live file
  confirmation permission-revocation checks, non-partitioned lost-APPROVE
  readback reconciliation, unsubstantiated-excerpt blocker rejection, and full
  approval evaluator invariants).
- Phase1BlockerAdmissionAndTaxonomyJourneyTests: structured source-based causal
  defect verification, separation of grounding vs. causal verification, and unified
  3-tier presentation ("Required fix", "Required decision", "Suggestion").
- Phase2CheckAndPrecedenceMatrixJourneyTests: explicit precedence across
  execution failure, unsatisfied coverage, mixed (required fixes + required
  decisions), required fixes only, required decisions only, eligible for
  approval, and confirmed GitHub approval across auto-approve/blocking/advisory.
- Phase3LocalLifecycleAndReplayIdentityJourneyTests: full local CLI admission,
  full-context replay identity (diff + base + config/policy + model + context +
  dispositions), and offline reassess/resolve/verify parity.
- Phase4MixedCoverageRecoveryJourneyTests: actionable coverage recovery
  guidance and CLI/PR confirmation parity without regressing generated files.
- Phase5PartitionedFinalizerAndReconciliationJourneyTests: v4 partitioned
  finalization plus failure/reconciliation tests (exhausted shared budget without
  retry refill, same-head mutation immediately before approval, lost APPROVE
  response reconciliation, and explicit permission-revocation policy).
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from review_sensei_github.approval import (
    ApprovalFacts,
    approval_eligibility_from_result,
    evaluate_approval_facts,
    has_blocking_findings,
    has_human_adjudication_findings,
)
from review_sensei_github.checks import (
    check_outcome_for_result,
)
from review_sensei_github.errors import (
    GitHubHTTPTransientError,
    GitHubPublicationError,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    ReviewApprovalFinalizer,
    approval_eligibility_marker,
    approval_marker,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.cli import _prepare_local_session
from review_sensei.convergence import (
    ReviewConvergencePolicy,
    admit_review_result,
    derive_blocker_candidate,
)
from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
from review_sensei.human_file_review import UnsupportedFile
from review_sensei.mixed_coverage import BinaryConfirmation, evaluate_mixed_coverage
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.outcomes import (
    REVIEW_EXIT_CLEAN,
    REVIEW_EXIT_INCOMPLETE,
    REVIEW_EXIT_REQUIRED_FIXES,
    review_exit_code,
)
from review_sensei.presentation import build_finding_view, render_finding
from review_sensei.verifier import (
    CandidateFinding,
    EvidenceReference,
    _snapshot_digest,
    prepare_publishable_review,
)
from tests.fake_github_http import json_response
from tests.test_human_assessment import APP, BASE, HEAD
from tests.test_partitioned_eligibility_reader import Bundle


def _unsupported_file(
    path: str,
    *,
    change: str = "modified",
    old: str = "c" * 40,
    new: str = "d" * 40,
) -> UnsupportedFile:
    old_blob = None if change == "added" else old
    new_blob = None if change == "removed" else new
    old_mode = None if old_blob is None else "100644"
    new_mode = None if new_blob is None else "100644"
    return UnsupportedFile(
        path=path,
        change=change,
        old_blob=old_blob,
        new_blob=new_blob,
        old_path=None,
        old_mode=old_mode,
        new_mode=new_mode,
    )


class Phase0PreservedInvariantsTests(unittest.TestCase):
    """Existing invariants that already pass and must never regress in Phases 1-5."""

    def test_mixed_coverage_preserves_generated_file_exclusions_with_confirmed_binaries(
        self,
    ) -> None:
        """Generated files excluded by policy + confirmed binaries satisfy mixed coverage."""
        files = (
            FileCoverage(path="src/app.py", outcome="reviewed"),
            FileCoverage(
                path="src/schema.pb.py",
                outcome="excluded-by-policy",
                reason="generated",
            ),
            FileCoverage(
                path="assets/logo.png",
                outcome="unsupported",
                reason="binary",
            ),
        )
        coverage = CoverageManifest(
            files=files,
            hunks=(
                HunkCoverage(1, "src/app.py", "reviewed"),
                HunkCoverage(2, "src/schema.pb.py", "excluded-by-policy", "generated"),
            ),
            enumerated_paths=tuple(item.path for item in files),
        )
        result = ReviewResult(
            summary="Completed text review; binary and generated files confirmed by human.",
            comments=(),
            provider="fixture",
            review_status="partial",
            coverage=coverage,
            coverage_only_partial=True,
        )
        generated_item = _unsupported_file(
            "src/schema.pb.py", old="1" * 40, new="2" * 40
        )
        binary_item = _unsupported_file("assets/logo.png", old="3" * 40, new="4" * 40)
        inventory = (generated_item, binary_item)
        confirmations = tuple(
            BinaryConfirmation(
                file=item,
                event_id="event-1",
                valid=True,
                permission_current=True,
                current=True,
            )
            for item in inventory
        )
        decision = evaluate_mixed_coverage(
            result,
            inventory,
            confirmations,
            text_completed=True,
            allow_confirmations=True,
            event_id="event-1",
        )
        self.assertEqual(decision.ai_review_status, "partial")
        self.assertTrue(decision.coverage_satisfied)
        self.assertTrue(decision.approved)
        self.assertFalse(decision.binary_blocker_remains)
        self.assertEqual(decision.blockers, ())

    def test_revoked_permission_on_live_file_confirmation_withholds_mixed_coverage(
        self,
    ) -> None:
        """A live binary/generated file confirmation fails closed if permission is revoked."""
        files = (
            FileCoverage(path="src/app.py", outcome="reviewed"),
            FileCoverage(
                path="assets/logo.png",
                outcome="unsupported",
                reason="binary",
            ),
        )
        coverage = CoverageManifest(
            files=files,
            hunks=(HunkCoverage(1, "src/app.py", "reviewed"),),
            enumerated_paths=tuple(item.path for item in files),
        )
        result = ReviewResult(
            summary="Binary file awaits valid maintainer confirmation.",
            comments=(),
            provider="fixture",
            review_status="partial",
            coverage=coverage,
            coverage_only_partial=True,
        )
        binary_item = _unsupported_file("assets/logo.png")
        decision = evaluate_mixed_coverage(
            result,
            (binary_item,),
            (
                BinaryConfirmation(
                    file=binary_item,
                    event_id="event-1",
                    valid=True,
                    permission_current=False,
                    current=True,
                ),
            ),
            text_completed=True,
            allow_confirmations=True,
            event_id="event-1",
        )
        self.assertFalse(decision.approved)
        self.assertFalse(decision.coverage_satisfied)
        self.assertTrue(decision.binary_blocker_remains)
        self.assertIn("permission-revoked", decision.blockers)

    def test_full_approval_evaluator_enforces_head_qualification_threads_and_human_obligations(
        self,
    ) -> None:
        """Approval eligibility continues to enforce every ApprovalFacts precondition."""
        base_facts = ApprovalFacts(
            enabled=True,
            app_authored=False,
            review_status="complete",
            evidence_policy="confirmed",
            has_blocking_findings=False,
            has_human_adjudication_findings=False,
            coverage_blocker=None,
            qualification="qualified",
            check_published=True,
            has_open_review_threads=False,
        )
        self.assertTrue(evaluate_approval_facts(base_facts).approved)
        for override, expected_blocker in (
            ({"has_blocking_findings": True}, "blocking-findings-open"),
            ({"has_human_adjudication_findings": True}, "human-adjudication-open"),
            ({"has_open_review_threads": True}, "review-threads-open"),
            ({"coverage_blocker": "partial"}, "coverage-partial"),
            ({"review_status": "partial"}, "review-partial"),
            ({"qualification": "unverified"}, "qualification-unverified"),
            ({"app_authored": True}, "app-authored-pull-request"),
        ):
            with self.subTest(override=override):
                decision = evaluate_approval_facts(replace(base_facts, **override))
                self.assertFalse(decision.approved)
                self.assertIn(expected_blocker, decision.blockers)

    def test_lost_approve_response_reconciles_remote_approval_without_duplicate_post(
        self,
    ) -> None:
        """If POST APPROVE succeeds remotely on GitHub but the HTTP response is lost, readback reconciles as already_approved."""
        clean = approval_eligibility_from_result(
            ReviewResult(
                summary="Clean.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        marker = approval_marker(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha=BASE,
        )
        reviews_on_server = [
            {
                "id": 10,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": "Initial.\n" + approval_eligibility_marker(clean),
                "user": {"login": APP, "type": "Bot"},
            }
        ]
        post_attempts = []

        def open_handler(request, timeout):
            path = urlparse(request.full_url).path
            if path.endswith("/pulls/1"):
                return json_response(
                    {
                        "state": "open",
                        "draft": False,
                        "user": {"login": "alice", "type": "User"},
                        "head": {
                            "sha": HEAD,
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": BASE,
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path == "/graphql":
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            if path.endswith("/pulls/1/reviews") and request.method == "GET":
                return json_response(reviews_on_server)
            if path.endswith("/pulls/1/reviews") and request.method == "POST":
                post_attempts.append(1)
                reviews_on_server.append(
                    {
                        "id": 11,
                        "commit_id": HEAD,
                        "state": "APPROVED",
                        "body": marker,
                        "user": {"login": APP, "type": "Bot"},
                    }
                )
                raise GitHubHTTPTransientError("connection reset after remote commit")
            raise AssertionError(f"Unexpected request: {request.method} {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=open_handler)
        outcome = ReviewApprovalFinalizer(http=http).finalize(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=clean,
            require_persisted=True,
        )
        self.assertEqual(outcome.status, "already_approved")
        self.assertEqual(len(post_attempts), 1)

    def test_exhausted_shared_evidence_budget_refuses_finalization_without_refill(
        self,
    ) -> None:
        """When an explicit shared EvidenceReadBudget is exhausted, finalizer fails closed without refilling."""
        budget = EvidenceReadBudget()
        for _ in range(60):
            budget.consume()
        for _ in range(4):
            budget.consume(fence=True)
        clean = approval_eligibility_from_result(
            ReviewResult(
                summary="Clean.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )

        def open_handler(request, timeout):
            path = urlparse(request.full_url).path
            if path.endswith("/pulls/1"):
                return json_response(
                    {
                        "state": "open",
                        "draft": False,
                        "user": {"login": "alice", "type": "User"},
                        "head": {
                            "sha": HEAD,
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": BASE,
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path == "/graphql":
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            if path.endswith("/pulls/1/reviews"):
                return json_response([])
            raise AssertionError(f"Unexpected request: {request.method} {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=open_handler)
        finalizer = ReviewApprovalFinalizer(http=http, evidence_budget=budget)
        with self.assertRaises(GitHubPublicationError):
            finalizer.finalize(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                eligibility=clean,
                require_persisted=True,
            )

    def test_excerpt_match_without_substantiated_causal_chain_is_not_admitted_as_required_fix(
        self,
    ) -> None:
        """Location/excerpt grounding alone must not admit an unsubstantiated claim as a Required fix."""
        comment = ReviewComment(
            path="src/auth.py",
            line=2,
            body="Verify whether upstream gateway could pass an expired token here.",
            blocking=True,
            severity="high",
            category="security",
            fix_effort="medium",
            evidence_id="E1",
        )
        derived = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=False,
        )
        admitted_result = admit_review_result(
            ReviewResult(
                summary="Uncertain external assumption.",
                comments=(comment,),
                provider="ollama",
                review_status="complete",
            ),
            policy=ReviewConvergencePolicy(mode="merge-focused"),
            candidates=(derived,),
            changed_paths=("src/auth.py",),
        )
        self.assertFalse(admitted_result.comments[0].effective_blocking)
        self.assertTrue(admitted_result.comments[0].needs_human)


class Phase1BlockerAdmissionAndTaxonomyJourneyTests(unittest.TestCase):
    """Phase 1 failing tests: causal blocker verification and 3-tier taxonomy."""

    def test_required_decision_never_renders_as_optional_improvement(self) -> None:
        """A needs_human finding must render as 'Required decision', never 'Optional improvement'."""
        comment = ReviewComment(
            path="src/auth.py",
            line=14,
            body="Verify whether external callers can pass an unvalidated tenant_id.",
            severity="high",
            category="security",
            blocking=False,
            effective_blocking=False,
            needs_human=True,
        )
        view = build_finding_view(
            comment,
            fingerprint=hashlib.sha256(b"finding-1").hexdigest(),
        )
        rendered = render_finding(view)
        self.assertIn("Required decision", rendered)
        self.assertNotIn("Optional improvement", rendered)
        self.assertNotIn("This is not required for this PR", rendered)

    def test_source_grounded_causal_defect_admits_as_required_fix_without_manual_candidate_injection(
        self,
    ) -> None:
        """A candidate with verified excerpt + source-based causal evidence admits as Required fix."""
        snapshot = {
            "src/auth.py": "def load_token(user):\n    return user.token.strip()\n    return None\n"
        }
        snapshot_sha256 = _snapshot_digest(snapshot)
        candidate = CandidateFinding(
            claim="load_token dereferences user.token without guarding against None.",
            triggering_conditions="Call load_token(user) when user.token is None.",
            impacted_path="src/auth.py",
            severity_rationale="High-severity correctness defect: raises AttributeError during authentication.",
            evidence=(
                EvidenceReference(
                    path="src/auth.py",
                    line=2,
                    snapshot_sha256=snapshot_sha256,
                    excerpt="return user.token.strip()",
                ),
            ),
        )
        result = ReviewResult(
            summary="Found null dereference.",
            comments=(),
            provider="ollama",
            review_status="complete",
        )
        prepared = prepare_publishable_review(
            result,
            candidates=(candidate,),
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy="confirmed",
            changed_lines={"src/auth.py": frozenset({2})},
            changed_paths=("src/auth.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertEqual(len(prepared.result.comments), 1)
        admitted = prepared.result.comments[0]
        self.assertTrue(
            admitted.effective_blocking,
            "Well-grounded source-based causal evidence should admit as Required fix (effective_blocking=True).",
        )
        self.assertFalse(
            admitted.needs_human,
            "Well-grounded source-based causal defect must not be demoted to needs_human=True.",
        )
        negated = replace(
            candidate,
            severity_rationale="Not a high severity issue: minor cleanup.",
        )
        negated_prepared = prepare_publishable_review(
            result,
            candidates=(negated,),
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy="confirmed",
            changed_lines={"src/auth.py": frozenset({2})},
            changed_paths=("src/auth.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertFalse(negated_prepared.result.comments[0].effective_blocking)


class Phase2CheckAndPrecedenceMatrixJourneyTests(unittest.TestCase):
    """Phase 2 contract tests: state/policy precedence matrix across Check, Review, and CLI."""

    def test_completed_review_with_required_decisions_only_produces_action_required_check(
        self,
    ) -> None:
        """Row 5: Required decisions only must produce action_required in auto-approve and blocking modes."""
        human_only_result = ReviewResult(
            summary="Human decision required.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=1,
                    body="Confirm external contract compatibility.",
                    severity="high",
                    blocking=False,
                    effective_blocking=False,
                    needs_human=True,
                ),
            ),
            provider="ollama",
            review_status="complete",
        )
        self.assertFalse(has_blocking_findings(human_only_result))
        self.assertTrue(has_human_adjudication_findings(human_only_result))

        for policy in ("auto-approve", "blocking"):
            with self.subTest(policy=policy):
                outcome = check_outcome_for_result(human_only_result, policy=policy)
                self.assertEqual(
                    outcome.conclusion,
                    "action_required",
                    f"Policy {policy!r} must not report a green check when required decisions remain.",
                )

    def test_auto_approve_mode_reports_failing_check_when_required_fixes_remain(
        self,
    ) -> None:
        """Row 3 & Row 4: Required fixes must produce conclusion='failure' in auto-approve mode."""
        blocking_result = ReviewResult(
            summary="Blocking defect.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="Proven null dereference.",
                    blocking=True,
                    effective_blocking=True,
                    needs_human=False,
                ),
            ),
            provider="ollama",
            review_status="complete",
        )
        outcome = check_outcome_for_result(blocking_result, policy="auto-approve")
        self.assertEqual(outcome.conclusion, "failure")

    def test_cli_exit_precedence_distinguishes_clean_required_fixes_and_required_decisions(
        self,
    ) -> None:
        """Precedence rule: incomplete/human-decision (2) > required-fixes-only (1) > clean (0)."""
        sig = inspect.signature(review_exit_code)
        self.assertIn(
            "required_decisions",
            sig.parameters,
            "review_exit_code must accept required_decisions to distinguish exit 2 (human decision) from exit 0/1.",
        )
        self.assertEqual(
            review_exit_code(
                status="reviewed",
                required_fixes=False,
                required_decisions=False,
            ),
            REVIEW_EXIT_CLEAN,
        )
        self.assertEqual(
            review_exit_code(
                status="reviewed",
                required_fixes=True,
                required_decisions=False,
            ),
            REVIEW_EXIT_REQUIRED_FIXES,
        )
        self.assertEqual(
            review_exit_code(
                status="reviewed",
                required_fixes=False,
                required_decisions=True,
            ),
            REVIEW_EXIT_INCOMPLETE,
        )
        self.assertEqual(
            review_exit_code(
                status="reviewed",
                required_fixes=True,
                required_decisions=True,
            ),
            REVIEW_EXIT_INCOMPLETE,
        )


class Phase3LocalLifecycleAndReplayIdentityJourneyTests(unittest.TestCase):
    """Phase 3 contract tests: local replay identity binding and local resolution lifecycle."""

    def test_local_session_revision_binds_config_model_base_and_context_not_only_diff(
        self,
    ) -> None:
        """Changing model, policy, or config on the same diff must produce a distinct local session identity."""
        diff_text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        with tempfile.TemporaryDirectory() as tmp:
            diff_path = Path(tmp) / "change.diff"
            diff_path.write_text(diff_text, encoding="utf-8")
            cfg_path = Path(tmp) / ".reviewsensei.yml"
            cfg_path.write_text("review:\n  mode: enforced\n", encoding="utf-8")

            def make_args(
                *,
                model: str = "qwen2.5-coder:7b",
                review_mode: str = "enforced",
                config: Path | None = None,
            ) -> tuple[str, str]:
                ns = argparse.Namespace(
                    session_ledger=Path(tmp) / "ledger",
                    repository="local/workspace",
                    pull_request=1,
                    head_sha=None,
                    base_sha=None,
                    diff=diff_path,
                    model=model,
                    review_mode=review_mode,
                    config=config,
                )
                _prepare_local_session(ns, hosted_session_ledger=False)
                return (ns.base_sha, ns.head_sha)

            baseline = make_args()
            self.assertEqual(baseline, make_args())
            self.assertNotEqual(baseline, make_args(model="deepseek-r1:70b"))
            self.assertNotEqual(baseline, make_args(review_mode="shadow"))
            self.assertNotEqual(baseline, make_args(config=cfg_path))


class Phase4MixedCoverageRecoveryJourneyTests(unittest.TestCase):
    """Phase 4 contract tests: actionable recovery guidance for unsatisfied coverage obligations."""

    def test_unsatisfied_coverage_check_summary_names_specific_recovery_actions(
        self,
    ) -> None:
        """Partial coverage check output must name the specific recovery path (confirm-file or rescan)."""
        files = (
            FileCoverage(
                path="assets/diagram.png",
                outcome="unsupported",
                reason="binary",
            ),
        )
        partial_result = ReviewResult(
            summary="Partial coverage.",
            comments=(),
            provider="ollama",
            review_status="partial",
            coverage=CoverageManifest(
                files=files,
                enumerated_paths=("assets/diagram.png",),
            ),
        )
        outcome = check_outcome_for_result(partial_result, policy="auto-approve")
        self.assertEqual(outcome.conclusion, "action_required")
        self.assertIn("assets/diagram.png", outcome.summary)
        self.assertTrue(
            "confirm" in outcome.summary.lower() or "rescan" in outcome.summary.lower(),
            "Incomplete check summary must provide actionable recovery instructions for uncovered files.",
        )


class Phase5PartitionedFinalizerAndReconciliationJourneyTests(unittest.TestCase):
    """Phase 5 contract tests: v4 partitioned finalizer and failure/reconciliation guarantees."""

    def test_partitioned_v4_root_finalizes_to_approved_when_all_obligations_are_resolved(
        self,
    ) -> None:
        """A clean/resolved v4 partitioned root must finalize to 'approved' once the v4 guard is lifted."""
        bundle = Bundle(100)
        parsed = bundle.parse()
        resolved_inventory = replace(
            parsed.human_review,
            resolved=tuple(item.fingerprint for item in parsed.human_review.findings),
        )
        complete_eligibility = replace(
            parsed,
            human_review=resolved_inventory,
            facts=replace(
                parsed.facts,
                has_human_adjudication_findings=False,
                has_blocking_findings=False,
                review_status="complete",
                coverage_blocker=None,
                qualification="qualified",
            ),
        )
        outcome = ReviewApprovalFinalizer(
            http=bundle.reviews.http,
            evidence_budget=bundle.budget,
        ).finalize(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=complete_eligibility,
        )
        self.assertEqual(outcome.status, "approved")

    def test_same_head_mutation_to_authoritative_review_immediately_before_approve_withholds(
        self,
    ) -> None:
        """Even when require_persisted=False (direct publish path), a same-head authoritative root change before APPROVE must withhold."""
        clean = approval_eligibility_from_result(
            ReviewResult(
                summary="Clean.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        concurrent_blocking = approval_eligibility_from_result(
            ReviewResult(
                summary="Concurrent blocker on same head.",
                comments=(
                    ReviewComment(
                        path="src/app.py",
                        line=1,
                        body="Blocker.",
                        blocking=True,
                        effective_blocking=True,
                    ),
                ),
                provider="ollama",
                review_status="complete",
            ),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        reviews_on_server = [
            {
                "id": 99,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": "Concurrent review.\n"
                + approval_eligibility_marker(concurrent_blocking),
                "user": {"login": APP, "type": "Bot"},
            }
        ]

        def open_handler(request, timeout):
            path = urlparse(request.full_url).path
            if path.endswith("/pulls/1"):
                return json_response(
                    {
                        "state": "open",
                        "draft": False,
                        "user": {"login": "alice", "type": "User"},
                        "head": {
                            "sha": HEAD,
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": BASE,
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path == "/graphql":
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            if path.endswith("/pulls/1/reviews") and request.method == "GET":
                return json_response(reviews_on_server)
            if path.endswith("/pulls/1/reviews") and request.method == "POST":
                return json_response({"id": 100}, 200)
            raise AssertionError(f"Unexpected request: {request.method} {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=open_handler)
        outcome = ReviewApprovalFinalizer(http=http).finalize(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=clean,
            require_persisted=False,
        )
        self.assertEqual(outcome.status, "approval_withheld")

    def test_finalizer_shares_single_default_budget_across_all_review_scans_within_one_finalize_call(
        self,
    ) -> None:
        """A finalizer invoked without an explicit evidence_budget must still share one finite budget across its review scans and lost-APPROVE reconciliation."""
        clean = approval_eligibility_from_result(
            ReviewResult(
                summary="Clean.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        marker = approval_marker(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha=BASE,
        )
        reviews_on_server = [
            {
                "id": idx,
                "commit_id": "e" * 40,
                "state": "COMMENTED",
                "body": "x" * 12000,
                "user": {"login": "other", "type": "User"},
            }
            for idx in range(1, 800)
        ] + [
            {
                "id": 800,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": "Current.\n" + approval_eligibility_marker(clean),
                "user": {"login": APP, "type": "Bot"},
            }
        ]
        review_get_calls = []

        def open_handler(request, timeout):
            path = urlparse(request.full_url).path
            if path.endswith("/pulls/1"):
                return json_response(
                    {
                        "state": "open",
                        "draft": False,
                        "user": {"login": "alice", "type": "User"},
                        "head": {
                            "sha": HEAD,
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": BASE,
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path == "/graphql":
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            if path.endswith("/pulls/1/reviews") and request.method == "GET":
                review_get_calls.append(1)
                args = parse_qs(urlparse(request.full_url).query)
                size, page = int(args["per_page"][0]), int(args["page"][0])
                offset = (page - 1) * size
                return json_response(reviews_on_server[offset : offset + size])
            if path.endswith("/pulls/1/reviews") and request.method == "POST":
                reviews_on_server.append(
                    {
                        "id": 801,
                        "commit_id": HEAD,
                        "state": "APPROVED",
                        "body": marker,
                        "user": {"login": APP, "type": "Bot"},
                    }
                )
                raise GitHubHTTPTransientError("lost APPROVE response")
            raise AssertionError(f"Unexpected request: {request.method} {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=open_handler)
        with self.assertRaises(GitHubPublicationError):
            ReviewApprovalFinalizer(http=http).finalize(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                eligibility=clean,
                require_persisted=True,
            )
        self.assertEqual(len(review_get_calls), 60)


if __name__ == "__main__":
    unittest.main()
