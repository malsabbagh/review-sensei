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
    """Phase 1 contract tests: causal blocker verification, premise checking, and 3-tier taxonomy."""

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
        """A candidate with verified excerpt + structured severity/remedy + unguarded dereference admits as Required fix."""
        snapshot = {
            "src/auth.py": "def load_token(user):\n    return user.token.strip()\n    return None\n"
        }
        snapshot_sha256 = _snapshot_digest(snapshot)
        candidate = CandidateFinding(
            claim="load_token dereferences user.token without guarding against None.",
            triggering_conditions="Call load_token(user) when user.token is None.",
            impacted_path="src/auth.py",
            severity_rationale="Raises AttributeError during authentication when user.token is None.",
            evidence=(
                EvidenceReference(
                    path="src/auth.py",
                    line=2,
                    snapshot_sha256=snapshot_sha256,
                    excerpt="return user.token.strip()",
                ),
            ),
            severity="high",
            remedy="Return an empty string when user is None or user.token is None.",
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
        # Prose severity words alone without structured severity/remedy must NOT admit as blocker
        prose_only = CandidateFinding(
            claim="load_token dereferences user.token without guarding against None.",
            triggering_conditions="Call load_token(user) when user.token is None.",
            impacted_path="src/auth.py",
            severity_rationale="High-severity correctness defect: raises AttributeError.",
            evidence=candidate.evidence,
        )
        prose_prepared = prepare_publishable_review(
            result,
            candidates=(prose_only,),
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy="confirmed",
            changed_lines={"src/auth.py": frozenset({2})},
            changed_paths=("src/auth.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertFalse(prose_prepared.result.comments[0].effective_blocking)

    def test_fabricated_blocker_on_harmless_code_is_rejected_or_demoted(self) -> None:
        """A candidate with a matching excerpt, empty assumptions, and high severity on harmless code must not become a blocker."""
        snapshot = {"src/config.py": "DEFAULT_TIMEOUT = 30\n"}
        snapshot_sha256 = _snapshot_digest(snapshot)
        fabricated = CandidateFinding(
            claim="DEFAULT_TIMEOUT dereferences None and crashes the process.",
            triggering_conditions="Import src.config when user is None.",
            impacted_path="src/config.py",
            severity_rationale="High severity crash.",
            evidence=(
                EvidenceReference(
                    path="src/config.py",
                    line=1,
                    snapshot_sha256=snapshot_sha256,
                    excerpt="DEFAULT_TIMEOUT = 30",
                ),
            ),
            severity="high",
            remedy="Guard DEFAULT_TIMEOUT against None.",
        )
        prepared = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(fabricated,),
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy="confirmed",
            changed_lines={"src/config.py": frozenset({1})},
            changed_paths=("src/config.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        comment = prepared.result.comments[0]
        self.assertFalse(comment.effective_blocking)
        self.assertFalse(comment.needs_human)

    def test_contrary_evidence_in_same_function_prevents_blocker_admission(
        self,
    ) -> None:
        """A candidate alleging null dereference when the same function already guards against None must not admit as a blocker."""
        snapshot = {
            "src/auth.py": (
                "def load_token(user):\n"
                "    if user is None or user.token is None:\n"
                "        return ''\n"
                "    return user.token.strip()\n"
            )
        }
        snapshot_sha256 = _snapshot_digest(snapshot)
        guarded_claim = CandidateFinding(
            claim="load_token dereferences user.token without guarding against None.",
            triggering_conditions="Call load_token(user) when user.token is None.",
            impacted_path="src/auth.py",
            severity_rationale="Raises AttributeError when user.token is None.",
            evidence=(
                EvidenceReference(
                    path="src/auth.py",
                    line=4,
                    snapshot_sha256=snapshot_sha256,
                    excerpt="return user.token.strip()",
                ),
            ),
            severity="high",
            remedy="Check whether user.token is None before calling strip().",
        )
        prepared = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(guarded_claim,),
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy="confirmed",
            changed_lines={"src/auth.py": frozenset({4})},
            changed_paths=("src/auth.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertTrue(prepared.verifications[0].contrary_evidence)
        comment = prepared.result.comments[0]
        self.assertFalse(comment.effective_blocking)
        self.assertFalse(comment.needs_human)

    def test_ambiguous_high_impact_concern_with_external_assumption_becomes_required_decision(
        self,
    ) -> None:
        """A plausible high-impact concern that depends on an unverified assumption becomes Required decision, action_required, and exit 2."""
        snapshot = {
            "src/auth.py": "def load_token(user):\n    return user.token.strip()\n"
        }
        snapshot_sha256 = _snapshot_digest(snapshot)
        ambiguous = CandidateFinding(
            claim="load_token may raise AttributeError if upstream middleware passes an anonymous user with token=None.",
            triggering_conditions="Anonymous request reaches load_token without token initialization.",
            impacted_path="src/auth.py",
            severity_rationale="Authentication handler fails with 500 error.",
            assumptions=(
                "Upstream auth middleware can invoke load_token with user.token=None.",
            ),
            evidence=(
                EvidenceReference(
                    path="src/auth.py",
                    line=2,
                    snapshot_sha256=snapshot_sha256,
                    excerpt="return user.token.strip()",
                ),
            ),
            severity="high",
            remedy="Verify middleware contract or guard user.token against None.",
        )
        prepared = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(ambiguous,),
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy="confirmed",
            changed_lines={"src/auth.py": frozenset({2})},
            changed_paths=("src/auth.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        comment = prepared.result.comments[0]
        self.assertFalse(comment.effective_blocking)
        self.assertTrue(comment.needs_human)
        check = check_outcome_for_result(prepared.result, policy="auto-approve")
        self.assertEqual(check.conclusion, "action_required")
        exit_code = review_exit_code(
            status="reviewed",
            required_fixes=has_blocking_findings(prepared.result),
            required_decisions=has_human_adjudication_findings(prepared.result),
        )
        self.assertEqual(exit_code, REVIEW_EXIT_INCOMPLETE)

    def test_verifier_rejects_ungrounded_causal_premises_and_partial_or_invalidated_guards(
        self,
    ) -> None:
        """Items 2 & 3: Verifier must not synthesize causal premises from model labels or treat non-terminating/partial/reassigned guards as contrary evidence."""
        # 1. Safe subprocess list call with shell-injection claim -> contrary_evidence=True, not blocking
        snap_subproc = {
            "src/runner.py": "import subprocess\ndef status():\n    return subprocess.run(['git', 'status'], check=True)\n"
        }
        sha_subproc = _snapshot_digest(snap_subproc)
        cand_subproc = CandidateFinding(
            claim="Command injection via subprocess.run in status().",
            triggering_conditions="User triggers status().",
            impacted_path="src/runner.py",
            severity_rationale="High severity remote command injection.",
            evidence=(
                EvidenceReference(
                    path="src/runner.py",
                    line=3,
                    snapshot_sha256=sha_subproc,
                    excerpt="return subprocess.run(['git', 'status'], check=True)",
                ),
            ),
            severity="high",
            remedy="Avoid shell execution in subprocess.run.",
        )
        prep_subproc = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(cand_subproc,),
            snapshot=snap_subproc,
            snapshot_sha256=sha_subproc,
            evidence_policy="confirmed",
            changed_lines={"src/runner.py": frozenset({3})},
            changed_paths=("src/runner.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertTrue(prep_subproc.verifications[0].contrary_evidence)
        self.assertFalse(prep_subproc.result.comments[0].effective_blocking)
        self.assertFalse(prep_subproc.result.comments[0].needs_human)

        # 2. Unverified required_contract / named_mandatory_rule on harmless assignment -> not blocking
        snap_simple = {"src/const.py": "DEFAULT_TIMEOUT = 1\n"}
        sha_simple = _snapshot_digest(snap_simple)
        cand_contract = CandidateFinding(
            claim="DEFAULT_TIMEOUT violates contract.",
            triggering_conditions="Importing src/const.py.",
            impacted_path="src/const.py",
            severity_rationale="High severity contract mismatch.",
            evidence=(
                EvidenceReference(
                    path="src/const.py",
                    line=1,
                    snapshot_sha256=sha_simple,
                    excerpt="DEFAULT_TIMEOUT = 1",
                ),
            ),
            severity="high",
            remedy="Update DEFAULT_TIMEOUT.",
            required_contract="api-contract",
            named_mandatory_rule="R1",
        )
        prep_contract = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(cand_contract,),
            snapshot=snap_simple,
            snapshot_sha256=sha_simple,
            evidence_policy="confirmed",
            changed_lines={"src/const.py": frozenset({1})},
            changed_paths=("src/const.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertFalse(prep_contract.result.comments[0].effective_blocking)

        # 3. Generic filler remedy on an unguarded dereference -> actionable-remedy not supported, stays Required decision
        snap_deref = {
            "src/auth.py": "def load_token(user):\n    return user.token.strip()\n"
        }
        sha_deref = _snapshot_digest(snap_deref)
        cand_generic_remedy = CandidateFinding(
            claim="load_token dereferences user.token when None.",
            triggering_conditions="Call load_token(user) with user.token=None.",
            impacted_path="src/auth.py",
            severity_rationale="Raises AttributeError on None dereference.",
            evidence=(
                EvidenceReference(
                    path="src/auth.py",
                    line=2,
                    snapshot_sha256=sha_deref,
                    excerpt="return user.token.strip()",
                ),
            ),
            severity="high",
            remedy="Consider refactoring this module for maintainability.",
        )
        prep_generic = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(cand_generic_remedy,),
            snapshot=snap_deref,
            snapshot_sha256=sha_deref,
            evidence_policy="confirmed",
            changed_lines={"src/auth.py": frozenset({2})},
            changed_paths=("src/auth.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertNotIn(
            "actionable-remedy", prep_generic.verifications[0].supported_premises
        )
        self.assertFalse(prep_generic.result.comments[0].effective_blocking)
        self.assertTrue(prep_generic.result.comments[0].needs_human)

        # 4. Non-terminating guard (`if user is None: log(...)`), partial guard (`if user is None: return None` before `user.profile.token`),
        # and reassigned guard (`user = maybe_clear(user)`) must NOT set contrary_evidence=True
        for case_name, code, line_no, excerpt, claim_text, remedy_text in (
            (
                "non_terminating_if",
                "def load_token(user):\n    if user is None:\n        log('missing')\n    return user.token.strip()\n",
                4,
                "return user.token.strip()",
                "load_token dereferences user when user is None after logging.",
                "Return early when user is None before accessing user.token.",
            ),
            (
                "partial_guard_on_root_only",
                "def load_token(user):\n    if user is None:\n        return None\n    return user.profile.token.strip()\n",
                4,
                "return user.profile.token.strip()",
                "load_token dereferences user.profile.token when user.profile is None.",
                "Guard user.profile against None before accessing token.",
            ),
            (
                "reassigned_after_guard",
                "def load_token(user):\n    if user is not None:\n        user = maybe_clear(user)\n        return user.token.strip()\n    return ''\n",
                4,
                "return user.token.strip()",
                "load_token dereferences user.token after maybe_clear(user) can return None.",
                "Check whether user is None after maybe_clear(user) before accessing user.token.",
            ),
        ):
            with self.subTest(case=case_name):
                snap = {"src/auth.py": code}
                sha = _snapshot_digest(snap)
                cand = CandidateFinding(
                    claim=claim_text,
                    triggering_conditions="Call load_token(user) when target expression evaluates to None.",
                    impacted_path="src/auth.py",
                    severity_rationale="Raises AttributeError at runtime.",
                    evidence=(
                        EvidenceReference(
                            path="src/auth.py",
                            line=line_no,
                            snapshot_sha256=sha,
                            excerpt=excerpt,
                        ),
                    ),
                    severity="high",
                    remedy=remedy_text,
                )
                prep = prepare_publishable_review(
                    ReviewResult(
                        summary="Review.",
                        comments=(),
                        provider="ollama",
                        review_status="complete",
                    ),
                    candidates=(cand,),
                    snapshot=snap,
                    snapshot_sha256=sha,
                    evidence_policy="confirmed",
                    changed_lines={"src/auth.py": frozenset({line_no})},
                    changed_paths=("src/auth.py",),
                    convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
                )
                self.assertFalse(prep.verifications[0].contrary_evidence)
                self.assertTrue(prep.result.comments[0].effective_blocking)


class Phase2CheckAndPrecedenceMatrixJourneyTests(unittest.TestCase):
    """Phase 2 contract tests: state/policy precedence matrix and human-resolution reconciliation."""

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

    def test_human_resolution_reconciles_check_and_review_with_partial_failure_retry(
        self,
    ) -> None:
        """Resolving the last human decision updates the check run and approves, and retries cleanly after partial check failure."""
        import json as _json

        from review_sensei_github.publication import (
            IndexedFinding,
            finding_index_marker,
        )

        comment = ReviewComment(
            path="src/app.py",
            line=1,
            body="Confirm external contract compatibility.",
            severity="high",
            blocking=False,
            effective_blocking=False,
            needs_human=True,
        )
        result = ReviewResult(
            summary="Human decision required.",
            comments=(comment,),
            provider="ollama",
            review_status="complete",
        )
        eligibility = approval_eligibility_from_result(
            result,
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        assert eligibility.human_review is not None
        fp = eligibility.human_review.findings[0].fingerprint
        idx_marker = finding_index_marker(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha=BASE,
            result_digest=eligibility.result_digest,
            entries=(
                IndexedFinding(
                    instance=fp,
                    lifecycle=fp,
                    blocking=False,
                    needs_human=True,
                    body_placed=True,
                ),
            ),
        )
        reviews_on_server = [
            {
                "id": 10,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": (
                    "Initial review.\n"
                    + idx_marker
                    + "\n"
                    + approval_eligibility_marker(eligibility)
                ),
                "user": {"login": APP, "type": "Bot"},
            }
        ]
        check_updates: list[dict[str, object]] = []
        approve_posts: list[dict[str, object]] = []
        fail_check_once = {"fail": True}

        class _Disposition:
            def __init__(self, fingerprint: str, head_sha: str) -> None:
                self.fingerprint = fingerprint
                self.head_sha = head_sha

            def honors(self, candidate_fp: str, *, head_sha: str) -> bool:
                return candidate_fp == self.fingerprint and head_sha == self.head_sha

        def open_handler(request, timeout):
            path = urlparse(request.full_url).path
            payload = (
                _json.loads(request.data.decode("utf-8")) if request.data else None
            )
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
            if path.endswith("/pulls/1/reviews/10") and request.method == "PATCH":
                reviews_on_server[0]["body"] = payload["body"]
                return json_response(reviews_on_server[0], 200)
            if path.endswith("/pulls/1/reviews") and request.method == "POST":
                approve_posts.append(payload)
                created = {
                    "id": 11,
                    "commit_id": HEAD,
                    "state": "APPROVED",
                    "body": payload["body"],
                    "user": {"login": APP, "type": "Bot"},
                }
                reviews_on_server.append(created)
                return json_response(created, 200)
            if path.endswith(f"/commits/{HEAD}/check-runs") and request.method == "GET":
                return json_response({"check_runs": []})
            if path.endswith("/check-runs") and request.method == "POST":
                if fail_check_once["fail"]:
                    fail_check_once["fail"] = False
                    raise GitHubHTTPTransientError("check update failed temporarily")
                check_updates.append(payload)
                return json_response(
                    {"id": 500, "html_url": "https://github.test/checks/500"}, 201
                )
            raise AssertionError(f"Unexpected request: {request.method} {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=open_handler)
        finalizer = ReviewApprovalFinalizer(http=http)
        # First attempt: check update fails, review approval succeeds -> reports partial failure
        first = finalizer.refinalize_with_dispositions(
            token="synthetic",
            check_token="check-token",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            actor="maintainer",
            finding_ids=(f"RS-{fp[:8].upper()}",),
            dispositions=(_Disposition(fp, HEAD),),
            reviews_policy="auto-approve",
        )
        self.assertEqual(first.status, "gate_reevaluation_failed")
        self.assertEqual(first.diagnostic, "check_reconciliation_failed")
        self.assertEqual(len(approve_posts), 1)
        self.assertEqual(len(check_updates), 0)

        # Retry: completes the check run update to 'success' without duplicating APPROVE
        second = finalizer.refinalize_with_dispositions(
            token="synthetic",
            check_token="check-token",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            actor="maintainer",
            finding_ids=(f"RS-{fp[:8].upper()}",),
            dispositions=(_Disposition(fp, HEAD),),
            reviews_policy="auto-approve",
        )
        self.assertEqual(second.status, "already_approved")
        self.assertEqual(len(approve_posts), 1)
        self.assertEqual(len(check_updates), 1)
        self.assertEqual(check_updates[0]["conclusion"], "success")

    def test_public_human_assessment_reply_updates_check_run_across_auto_approve_blocking_and_remaining_fix(
        self,
    ) -> None:
        """Item 1: Public GitHubApplication.generate_and_publish_reply must pass check_token and reviews_policy and update the check run."""
        from review_sensei_github.application import (
            GitHubApplication,
            GitHubWriteOptions,
        )

        from tests.test_human_assessment import (
            Broker,
            ConversationPublisher,
            Provider,
            State,
            prior,
            response_for,
        )

        # 1. auto-approve mode: updates check to 'success' and posts APPROVE
        state_auto = State()
        broker_auto = Broker()
        app_auto = GitHubApplication(
            broker=broker_auto,
            reviewer=None,
            learner=None,
            replier=ConversationPublisher(http=state_auto.http),
            http=state_auto.http,
        )
        out_auto = app_auto.generate_and_publish_reply(
            options=GitHubWriteOptions(
                github_writes=True, mention_replies=True, auto_approve=True
            ),
            oidc_token="synthetic-oidc",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=HEAD,
            reply_provider=Provider(response_for(state_auto.eligibility)),
            model="fixture-model",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(out_auto.status, "replied")
        self.assertEqual(out_auto.approval_status, "approved")
        self.assertEqual(
            broker_auto.capabilities,
            ["issue_reply", "review_publish", "check_publish"],
        )
        self.assertEqual(state_auto.events(), ["COMMENT", "APPROVE"])
        self.assertEqual(len(state_auto.check_runs), 1)
        self.assertEqual(state_auto.check_runs[-1]["conclusion"], "success")

        # 2. blocking mode (auto_approve=False): updates check to 'success' without posting APPROVE
        state_blocking = State()
        broker_blocking = Broker()
        app_blocking = GitHubApplication(
            broker=broker_blocking,
            reviewer=None,
            learner=None,
            replier=ConversationPublisher(http=state_blocking.http),
            http=state_blocking.http,
        )
        out_blocking = app_blocking.generate_and_publish_reply(
            options=GitHubWriteOptions(
                github_writes=True, mention_replies=True, auto_approve=False
            ),
            oidc_token="synthetic-oidc",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=HEAD,
            reply_provider=Provider(response_for(state_blocking.eligibility)),
            model="fixture-model",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(out_blocking.status, "replied")
        self.assertEqual(out_blocking.approval_status, "auto_approval_disabled")
        self.assertEqual(
            broker_blocking.capabilities,
            ["issue_reply", "review_publish", "check_publish"],
        )
        self.assertEqual(state_blocking.events(), ["COMMENT"])
        self.assertEqual(len(state_blocking.check_runs), 1)
        self.assertEqual(state_blocking.check_runs[-1]["conclusion"], "success")

        # 3. Remaining required fix alongside resolved human decision: check updated to 'failure', approval withheld
        base_elig = prior()
        with_fix = replace(
            base_elig,
            facts=replace(base_elig.facts, has_blocking_findings=True),
        )
        state_fix = State(with_fix)
        broker_fix = Broker()
        app_fix = GitHubApplication(
            broker=broker_fix,
            reviewer=None,
            learner=None,
            replier=ConversationPublisher(http=state_fix.http),
            http=state_fix.http,
        )
        out_fix = app_fix.generate_and_publish_reply(
            options=GitHubWriteOptions(
                github_writes=True, mention_replies=True, auto_approve=True
            ),
            oidc_token="synthetic-oidc",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=HEAD,
            reply_provider=Provider(response_for(state_fix.eligibility)),
            model="fixture-model",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(out_fix.status, "replied")
        self.assertEqual(out_fix.approval_status, "approval_withheld")
        self.assertEqual(state_fix.events(), ["COMMENT"])
        self.assertEqual(len(state_fix.check_runs), 1)
        self.assertEqual(state_fix.check_runs[-1]["conclusion"], "failure")


class Phase3LocalLifecycleAndReplayIdentityJourneyTests(unittest.TestCase):
    """Phase 3 contract tests: local replay identity binding and local resolution lifecycle."""

    def test_local_session_revision_binds_config_model_base_and_context_not_only_diff(
        self,
    ) -> None:
        """Changing model, policy, implicit/explicit config, or context files on the same diff must produce a distinct local session identity."""
        import os

        diff_text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            diff_path = tmp_path / "change.diff"
            diff_path.write_text(diff_text, encoding="utf-8")
            cfg_path = tmp_path / ".reviewsensei.yml"
            cfg_path.write_text("review:\n  mode: enforced\n", encoding="utf-8")
            ctx_dir = tmp_path / "context"
            ctx_dir.mkdir()
            (ctx_dir / "module.py").write_text("x = 1\n", encoding="utf-8")

            def make_args(
                *,
                model: str = "qwen2.5-coder:7b",
                review_mode: str = "enforced",
                config: Path | None = None,
                context_root: Path | None = None,
            ) -> tuple[str, str]:
                ns = argparse.Namespace(
                    session_ledger=tmp_path / "ledger",
                    repository="local/workspace",
                    pull_request=1,
                    head_sha=None,
                    base_sha=None,
                    diff=diff_path,
                    model=model,
                    review_mode=review_mode,
                    config=config,
                    context_root=context_root,
                )
                _prepare_local_session(ns, hosted_session_ledger=False)
                return (ns.base_sha, ns.head_sha)

            old_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                baseline = make_args()
                self.assertEqual(baseline, make_args())
                self.assertNotEqual(baseline, make_args(model="deepseek-r1:70b"))
                self.assertNotEqual(baseline, make_args(review_mode="shadow"))
                # Editing implicit .reviewsensei.yml in cwd (without --config) must invalidate replay!
                cfg_path.write_text("review:\n  mode: advisory\n", encoding="utf-8")
                after_implicit_cfg_edit = make_args()
                self.assertNotEqual(baseline, after_implicit_cfg_edit)
                # Editing context_root files must invalidate replay!
                with_ctx_1 = make_args(context_root=ctx_dir)
                (ctx_dir / "module.py").write_text("x = 2\n", encoding="utf-8")
                with_ctx_2 = make_args(context_root=ctx_dir)
                self.assertNotEqual(with_ctx_1, with_ctx_2)
            finally:
                os.chdir(old_cwd)

    def test_local_session_revision_hashes_past_64_files_64kb_and_ignores_unused_context_config(
        self,
    ) -> None:
        """Item 4: Local replay identity must detect same-size edits beyond 64 files / 64 KB and ignore unused context_root/.reviewsensei.yml."""
        import os

        diff_text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            diff_path = tmp_path / "change.diff"
            diff_path.write_text(diff_text, encoding="utf-8")
            (tmp_path / ".reviewsensei.yml").write_text(
                "review:\n  mode: enforced\n", encoding="utf-8"
            )
            ctx_dir = tmp_path / "context"
            ctx_dir.mkdir()
            for idx in range(65):
                (ctx_dir / f"file_{idx:03d}.py").write_text(
                    ("a" * 1200) + f"\n# {idx:03d}\n", encoding="utf-8"
                )

            def make_args() -> tuple[str, str]:
                ns = argparse.Namespace(
                    session_ledger=tmp_path / "ledger",
                    repository="local/workspace",
                    pull_request=1,
                    head_sha=None,
                    base_sha=None,
                    diff=diff_path,
                    model="qwen2.5-coder:7b",
                    review_mode="enforced",
                    config=None,
                    context_root=ctx_dir,
                )
                _prepare_local_session(ns, hosted_session_ledger=False)
                return (ns.base_sha, ns.head_sha)

            old_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                before_65th = make_args()
                # Same-size edit on the 65th file (beyond 64 files and >64 KB into the tree)
                (ctx_dir / "file_064.py").write_text(
                    ("b" * 1200) + "\n# 064\n", encoding="utf-8"
                )
                after_65th = make_args()
                self.assertNotEqual(before_65th, after_65th)

                # Adding or editing an unused context_root/.reviewsensei.yml must NOT alter replay identity
                (ctx_dir / ".reviewsensei.yml").write_text(
                    "review:\n  mode: shadow\n", encoding="utf-8"
                )
                with_unused_ctx_cfg = make_args()
                self.assertEqual(after_65th, with_unused_ctx_cfg)
            finally:
                os.chdir(old_cwd)


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

    @staticmethod
    def _configure_partitioned_bundle(
        count: int = 100,
        *,
        open_blocking_thread: bool = False,
        tamper_root_after_stage: bool = False,
    ):
        bundle = Bundle(count)
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
        persisted_doc = complete_eligibility.to_dict()
        if tamper_root_after_stage:
            persisted_doc = dict(persisted_doc)
            persisted_doc["human_resolution"] = dict(persisted_doc["human_resolution"])
            persisted_doc["human_resolution"]["resolved"] = []
        bundle.reviews.items.append(
            bundle.reviews.item(
                10000, "Partitioned root.\n" + bundle.marker(persisted_doc)
            )
        )
        approve_calls: list[dict[str, object]] = []
        orig_open = bundle.reviews.open

        def custom_open(request, timeout):
            parsed_url = urlparse(request.full_url)
            if (
                request.method == "GET"
                and parsed_url.path == "/repos/owner/repo/pulls/1/reviews"
                and parsed_url.query
            ):
                qs = parse_qs(parsed_url.query)
                page = int(qs.get("page", ["1"])[0])
                per_page = int(qs.get("per_page", ["1"])[0])
                start = (page - 1) * per_page
                return json_response(bundle.reviews.items[start : start + per_page])
            return orig_open(request, timeout)

        def hook(method, path, payload):
            if path == "/repos/owner/repo/pulls/1" and method == "GET":
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
            if path == "/graphql" and method == "POST":
                nodes = []
                if open_blocking_thread:
                    nodes.append(
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {"login": APP},
                                        "body": "Blocking thread",
                                    }
                                ]
                            },
                        }
                    )
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": nodes,
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
            if path == "/repos/owner/repo/pulls/1/reviews" and method == "POST":
                if isinstance(payload, dict) and payload.get("event") == "APPROVE":
                    approve_calls.append(payload)
                    item = {
                        "id": 20000,
                        "body": payload["body"],
                        "state": "APPROVED",
                        "commit_id": HEAD,
                        "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                        "user": {"type": "Bot", "id": 7, "login": APP},
                    }
                    bundle.reviews.items.append(item)
                    return json_response(item, 200)
            return None

        bundle.reviews.hook = hook
        bundle.reviews.http = GitHubHttp(
            api_url="https://api.github.test", opener=custom_open
        )
        return bundle, complete_eligibility, approve_calls

    def test_partitioned_v4_root_finalizes_to_approved_when_all_obligations_are_resolved(
        self,
    ) -> None:
        """A clean/resolved v4 partitioned root must run live preflight, thread scan, readback, and POST APPROVE."""
        bundle, complete_eligibility, approve_calls = (
            self._configure_partitioned_bundle(100)
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
            partition_reader=bundle.read,
            visible_reader=bundle.visible,
        )
        self.assertEqual(outcome.status, "approved")
        self.assertEqual(outcome.review_id, 20000)
        self.assertEqual(len(approve_calls), 1)
        self.assertEqual(approve_calls[0]["event"], "APPROVE")

        # Also verify fail-closed when an open blocking thread exists on GitHub
        bundle_blocked, elig_blocked, calls_blocked = (
            self._configure_partitioned_bundle(100, open_blocking_thread=True)
        )
        withheld_thread = ReviewApprovalFinalizer(
            http=bundle_blocked.reviews.http,
            evidence_budget=bundle_blocked.budget,
        ).finalize(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=elig_blocked,
            partition_reader=bundle_blocked.read,
            visible_reader=bundle_blocked.visible,
        )
        self.assertEqual(withheld_thread.status, "approval_withheld")
        self.assertEqual(len(calls_blocked), 0)

        # Also verify fail-closed when the persisted v4 root on the server differs from eligibility
        bundle_tampered, elig_tampered, calls_tampered = (
            self._configure_partitioned_bundle(100, tamper_root_after_stage=True)
        )
        withheld_tampered = ReviewApprovalFinalizer(
            http=bundle_tampered.reviews.http,
            evidence_budget=bundle_tampered.budget,
        ).finalize(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=elig_tampered,
            partition_reader=bundle_tampered.read,
            visible_reader=bundle_tampered.visible,
        )
        self.assertEqual(withheld_tampered.status, "approval_withheld")
        self.assertEqual(len(calls_tampered), 0)

        # Also verify fail-closed when the shared EvidenceReadBudget is exhausted during readback
        bundle_exhausted, elig_exhausted, calls_exhausted = (
            self._configure_partitioned_bundle(100)
        )
        while bundle_exhausted.budget.calls < 59:
            bundle_exhausted.budget.consume()
        with self.assertRaises(GitHubPublicationError):
            ReviewApprovalFinalizer(
                http=bundle_exhausted.reviews.http,
                evidence_budget=bundle_exhausted.budget,
            ).finalize(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                eligibility=elig_exhausted,
                partition_reader=bundle_exhausted.read,
                visible_reader=bundle_exhausted.visible,
            )
        self.assertEqual(len(calls_exhausted), 0)

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

    def test_five_disposition_session_ledger_with_consumed_grant_loads_cleanly(
        self,
    ) -> None:
        """Sanitized regression fixture for PR #308: a 5-disposition session comment with a consumed-grant header loads cleanly under the current schema."""
        from datetime import datetime, timezone

        from review_sensei_github.session_ledger import (
            GitHubIssueCommentSessionLedger,
            render_session_comment,
        )

        from review_sensei.disposition import FindingDisposition
        from review_sensei.session import SessionIdentity, SessionRecord

        identity = SessionIdentity(
            repository="owner/repo",
            pull_request=308,
            repository_id=1,
        )
        now = datetime(2026, 10, 17, 16, 55, 58, tzinfo=timezone.utc)
        dispositions = tuple(
            FindingDisposition(
                fingerprint=f"{idx:064x}",
                action="accept-risk",
                reason="acceptable risk",
                actor="maintainer",
                head_sha=HEAD,
            ).to_dict()
            for idx in range(1, 6)
        )
        record = SessionRecord.create(identity, now=now).evolve(
            now=now,
            completed_initial_reviews=1,
            generation=5,
            dispositions=dispositions,
        )
        comment_body = render_session_comment(
            repository_id=1,
            pull_request=308,
            record=record,
            consumed_grants=(
                "5643617f0d4e7612ecba3aaa1364a625a1c9d75798738f719ebb901eb45e81e4",
            ),
        )

        def open_handler(request, timeout):
            path = urlparse(request.full_url).path
            if (
                path == "/repos/owner/repo/issues/308/comments"
                and request.method == "GET"
            ):
                return json_response(
                    [
                        {
                            "id": 6236936357,
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/308",
                            "body": comment_body,
                            "updated_at": "2026-10-17T16:55:59Z",
                            "user": {"id": 7, "login": APP, "type": "Bot"},
                        }
                    ]
                )
            raise AssertionError(f"Unexpected request: {request.method} {path}")

        http = GitHubHttp(api_url="https://api.github.test", opener=open_handler)
        ledger = GitHubIssueCommentSessionLedger(http, token="synthetic", app_slug=APP)
        loaded = ledger.load(identity, now=now)
        self.assertEqual(loaded.status, "ok")
        assert loaded.record is not None
        self.assertEqual(loaded.record.generation, 5)
        self.assertEqual(len(loaded.record.dispositions), 5)

    def test_mixed_source_check_state_agrees_with_persisted_and_retained_decisions(
        self,
    ) -> None:
        """Item 1: ReviewPublisher check conclusion must use merged/persisted required_decisions rather than raw result."""
        from review_sensei_github.publication import (
            ReviewPublisher,
            approval_eligibility_from_body,
        )

        from tests import test_human_assessment

        diff_text = (
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            + test_human_assessment.DIFF
            + "\n"
        )
        clean_result = ReviewResult(
            summary="The broader scope was reviewed.",
            comments=(),
            provider="fixture",
            review_status="complete",
        )

        # 1. Complete fresh result with no human findings, while retained_eligibility still has a pending decision
        state_retained = test_human_assessment.State()
        pub_retained = ReviewPublisher(http=state_retained.http)
        out_retained = pub_retained.publish(
            token="synthetic-review",
            check_token="synthetic-check",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=test_human_assessment.HEAD,
            base_branch="main",
            base_sha=test_human_assessment.BASE,
            result=clean_result,
            diff=diff_text,
            app_slug=test_human_assessment.APP,
            retained_eligibility=state_retained.eligibility,
            continuation_digest="f" * 64,
            source_guard=lambda: True,
        )
        self.assertEqual(out_retained.status, "published")
        self.assertEqual(out_retained.diagnostic, "human_adjudication_open")
        self.assertEqual(state_retained.check_runs[-1]["conclusion"], "action_required")
        self.assertNotIn("APPROVE", state_retained.events())

        # 2. Replaying an older human-bearing result after its durable decision has been resolved
        human_comment = ReviewComment(
            path="src/app.py",
            line=1,
            body="Confirm that this path cannot send private input remotely.",
            severity="high",
            effective_blocking=False,
            needs_human=True,
        )
        human_result = ReviewResult(
            summary="Human assessment is required.",
            comments=(human_comment,),
            provider="ollama",
            review_status="complete",
        )
        state_replay = test_human_assessment.State()
        state_replay.reviews = []
        pub_replay = ReviewPublisher(http=state_replay.http)
        first_pub = pub_replay.publish(
            token="synthetic-review",
            check_token="synthetic-check",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=test_human_assessment.HEAD,
            base_branch="main",
            base_sha=test_human_assessment.BASE,
            result=human_result,
            diff=diff_text,
            app_slug=test_human_assessment.APP,
        )
        self.assertEqual(first_pub.status, "published")
        self.assertEqual(state_replay.check_runs[-1]["conclusion"], "action_required")

        persisted = approval_eligibility_from_body(state_replay.reviews[-1]["body"])
        assert persisted is not None and persisted.human_review is not None
        fp = persisted.human_review.findings[0].fingerprint
        resolved_elig = replace(
            persisted,
            facts=replace(persisted.facts, has_human_adjudication_findings=False),
            human_review=replace(persisted.human_review, resolved=(fp,)),
        )
        old_marker = approval_eligibility_marker(persisted)
        new_marker = approval_eligibility_marker(resolved_elig)
        state_replay.reviews[-1]["body"] = state_replay.reviews[-1]["body"].replace(
            old_marker, new_marker
        )

        replay_out = pub_replay.publish(
            token="synthetic-review",
            check_token="synthetic-check",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=test_human_assessment.HEAD,
            base_branch="main",
            base_sha=test_human_assessment.BASE,
            result=human_result,
            diff=diff_text,
            app_slug=test_human_assessment.APP,
        )
        self.assertEqual(replay_out.status, "already_published")
        self.assertIsNone(replay_out.diagnostic)
        self.assertEqual(state_replay.check_runs[-1]["conclusion"], "success")
        self.assertIn("APPROVE", state_replay.events())

        # 3. Partial/incomplete coverage retains non-passing action_required precedence even when decisions are resolved
        state_partial = test_human_assessment.State(
            eligibility=test_human_assessment.prior(
                review_status="partial", coverage_blocker="partial"
            )
        )
        assert state_partial.eligibility.human_review is not None
        partial_fp = state_partial.eligibility.human_review.findings[0].fingerprint
        cleared_partial = replace(
            state_partial.eligibility,
            facts=replace(
                state_partial.eligibility.facts,
                has_human_adjudication_findings=False,
            ),
            human_review=replace(
                state_partial.eligibility.human_review,
                resolved=(partial_fp,),
            ),
        )
        state_partial.eligibility = cleared_partial
        state_partial.reviews = [state_partial.review(cleared_partial, 20)]
        pub_partial = ReviewPublisher(http=state_partial.http)
        out_partial = pub_partial.publish(
            token="synthetic-review",
            check_token="synthetic-check",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=test_human_assessment.HEAD,
            base_branch="main",
            base_sha=test_human_assessment.BASE,
            result=clean_result,
            diff=diff_text,
            app_slug=test_human_assessment.APP,
            retained_eligibility=cleared_partial,
            continuation_digest="e" * 64,
            source_guard=lambda: True,
        )
        self.assertEqual(out_partial.status, "published")
        self.assertEqual(out_partial.diagnostic, "review_incomplete")
        self.assertEqual(state_partial.check_runs[-1]["conclusion"], "action_required")
        self.assertNotIn("APPROVE", state_partial.events())

    def test_three_way_presentation_classification_across_summary_terminal_and_omission_budget(
        self,
    ) -> None:
        """Item 3: Summary, review body, terminal output, and omission budgets must keep Required fix, Required decision, and Optional suggestion distinct."""
        from review_sensei_github.publication import (
            AssembledReview,
            BodyExplanation,
            IndexedFinding,
            fit_assembled_review,
        )

        from review_sensei.presentation import (
            CHANGES_REQUIRED_STATE,
            NO_REQUIRED_FIXES_STATE,
            OPTIONAL_FINDING,
            FindingView,
            build_finding_view,
            build_review_summary_view,
            render_review_summary,
            render_review_text,
        )

        req_fix = ReviewComment(
            path="src/a.py",
            line=10,
            body="Blocking defect.",
            severity="high",
            blocking=True,
            effective_blocking=True,
            needs_human=False,
        )
        req_decision = ReviewComment(
            path="src/b.py",
            line=20,
            body="Ambiguous external contract.",
            severity="high",
            blocking=False,
            effective_blocking=False,
            needs_human=True,
        )
        opt_suggestion = ReviewComment(
            path="src/c.py",
            line=30,
            body="Rename helper for clarity.",
            severity="low",
            blocking=False,
            effective_blocking=False,
            needs_human=False,
        )

        fix_view = build_finding_view(
            req_fix,
            fingerprint="1" * 64,
            identifier="RS-FIX001",
            placement="inline",
        )
        decision_view = build_finding_view(
            req_decision,
            fingerprint="2" * 64,
            identifier="RS-DEC001",
            placement="body",
        )
        opt_view = build_finding_view(
            opt_suggestion,
            fingerprint="3" * 64,
            identifier="RS-OPT001",
            placement="inline",
        )

        # 1. Decision-only review summary
        decision_summary = render_review_summary(
            build_review_summary_view(
                state=NO_REQUIRED_FIXES_STATE,
                head_sha=HEAD,
                optional=(decision_view,),
            )
        )
        self.assertIn("**No required fixes · 1 required decision**", decision_summary)
        self.assertIn("### Required decisions", decision_summary)
        self.assertIn(
            "Resolve RS-DEC001 before approval can proceed.",
            decision_summary,
        )
        self.assertNotIn("optional improvement", decision_summary.lower())

        # 2. Mixed fix + decision + suggestion summary and terminal output
        mixed_result = ReviewResult(
            summary="Mixed findings.",
            comments=(req_fix, req_decision, opt_suggestion),
            provider="ollama",
            review_status="complete",
        )
        mixed_summary = render_review_summary(
            build_review_summary_view(
                state=CHANGES_REQUIRED_STATE,
                head_sha=HEAD,
                required=(fix_view,),
                optional=(decision_view, opt_view),
            )
        )
        self.assertIn(
            "**1 required fix · 1 required decision · 1 optional improvement**",
            mixed_summary,
        )
        self.assertIn("### Required\n", mixed_summary)
        self.assertIn("### Required decisions\n", mixed_summary)
        self.assertIn("### Optional\n", mixed_summary)
        self.assertIn(
            "Address RS-FIX001, resolve RS-DEC001, and run verification. Optional suggestions do not affect the review gate.",
            mixed_summary,
        )

        terminal_out = render_review_text(mixed_result)
        self.assertIn("required fixes: 1", terminal_out)
        self.assertIn("required decisions: 1", terminal_out)
        self.assertIn("optional findings: 1", terminal_out)
        self.assertIn("Required fixes:", terminal_out)
        self.assertIn("Required decisions:", terminal_out)
        self.assertIn("Optional findings:", terminal_out)
        self.assertIn("[required fix]", terminal_out)
        self.assertIn("[required decision]", terminal_out)
        self.assertIn("[optional]", terminal_out)

        # 3. Omission under body size limit keeps required decisions intact and only omits optional explanations
        large_opt_view = FindingView(
            identifier="RS-OPT999",
            requirement=OPTIONAL_FINDING,
            severity="low",
            title="Optional refactor note.",
            detail="Optional refactor note: " + ("x" * 2400),
            placement="body",
            needs_human=False,
            location="`src/outside.py:99`",
        )
        body_decision_view = FindingView(
            identifier="RS-DEC999",
            requirement=OPTIONAL_FINDING,
            severity="high",
            title="Confirm cross-service contract before merge.",
            detail="Confirm cross-service contract before merge.",
            placement="body",
            needs_human=True,
            location="`src/outside.py:50`",
        )
        assembled = AssembledReview(
            inline_comments=(),
            index_entries=(
                IndexedFinding(
                    instance="a" * 64,
                    lifecycle="a" * 64,
                    blocking=False,
                    needs_human=True,
                    body_placed=True,
                ),
                IndexedFinding(
                    instance="b" * 64,
                    lifecycle="b" * 64,
                    blocking=False,
                    needs_human=False,
                    body_placed=True,
                ),
            ),
            head_sha=HEAD,
            state=NO_REQUIRED_FIXES_STATE,
            reason=None,
            overview="Size limit test.",
            coverage_line=None,
            required_views=(),
            optional_views=(body_decision_view, large_opt_view),
            limitations=(),
            placement_note=None,
            body_explanations=(
                BodyExplanation(
                    instance="a" * 64,
                    severity="high",
                    obligatory=True,
                    view=body_decision_view,
                ),
                BodyExplanation(
                    instance="b" * 64,
                    severity="low",
                    obligatory=False,
                    view=large_opt_view,
                ),
            ),
            inline_blocker_instances=(),
            requests_changes=False,
        )
        fitted_summary, _ = fit_assembled_review(
            assembled,
            review_marker="<!-- reviewsensei:review:v1 repo=1 pr=1 head="
            + HEAD
            + " result="
            + ("0" * 64)
            + " -->",
            eligibility_marker="<!-- reviewsensei:approval-eligibility:v3 {} -->",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha=BASE,
            result_digest="0" * 64,
            summary_limit=2200,
        )
        self.assertIn("Confirm cross-service contract before merge.", fitted_summary)
        self.assertIn("### Required decisions", fitted_summary)
        self.assertIn(
            "1 finding explanation was omitted for size. That finding remains open.",
            fitted_summary,
        )

    def test_required_contract_enum_matches_public_schema_and_round_trips_every_value(
        self,
    ) -> None:
        """Item 4: REQUIRED_CONTRACT_KINDS and candidate-finding.schema.json must accept the exact same 8 contract kinds."""
        import json as _json

        from review_sensei.convergence import REQUIRED_CONTRACT_KINDS
        from review_sensei.schemas import validate_public_document

        schema_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "review_sensei"
            / "schemas"
            / "candidate-finding.schema.json"
        )
        schema = _json.loads(schema_path.read_text(encoding="utf-8"))
        schema_enum = frozenset(schema["properties"]["required_contract"]["enum"])
        self.assertEqual(REQUIRED_CONTRACT_KINDS, schema_enum)
        self.assertEqual(
            REQUIRED_CONTRACT_KINDS,
            frozenset(
                {
                    "api-contract",
                    "changed-interface",
                    "compatibility-contract",
                    "persisted-state",
                    "public-api",
                    "required-contract",
                    "security-boundary",
                    "test-expectation",
                }
            ),
        )
        for contract_kind in sorted(REQUIRED_CONTRACT_KINDS):
            with self.subTest(contract_kind=contract_kind):
                candidate = CandidateFinding(
                    claim="Null dereference on user.token.",
                    triggering_conditions="Call load_token(None).",
                    impacted_path="src/auth.py",
                    severity_rationale="Raises AttributeError at runtime.",
                    evidence=(
                        EvidenceReference(
                            path="src/auth.py",
                            line=2,
                            snapshot_sha256="a" * 64,
                            excerpt="return user.token",
                        ),
                    ),
                    severity="high",
                    remedy="Return early if user is None.",
                    required_contract=contract_kind,
                )
                doc = candidate.to_dict()
                validate_public_document(doc, "candidate-finding")
                round_tripped = CandidateFinding.from_dict(doc)
                self.assertEqual(round_tripped.required_contract, contract_kind)

    def test_local_replay_tree_digest_streams_and_fails_closed_when_file_or_byte_bounds_exceeded(
        self,
    ) -> None:
        """Item 5: Local replay hashing must enforce finite file-count and total-byte bounds and fail closed to a non-reusable digest when exceeded."""
        import tempfile
        from unittest.mock import patch

        from review_sensei.cli import _replay_file_digest, _replay_tree_digest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("alpha", encoding="utf-8")
            d1 = _replay_tree_digest(root)
            d2 = _replay_tree_digest(root)
            self.assertEqual(d1, d2)
            self.assertEqual(len(d1), 64)

            # Exceeding file-count bound fails closed with unique non-reusable digest
            with patch("review_sensei.cli._MAX_REPLAY_TREE_FILES", 0):
                over_files_1 = _replay_tree_digest(root)
                over_files_2 = _replay_tree_digest(root)
            self.assertTrue(over_files_1.startswith("unbounded:"))
            self.assertNotEqual(over_files_1, over_files_2)

            # Exceeding byte bound fails closed with unique non-reusable digest
            with patch("review_sensei.cli._MAX_REPLAY_FILE_BYTES", 2):
                over_bytes_1 = _replay_file_digest(root / "a.txt")
                over_bytes_2 = _replay_file_digest(root / "a.txt")
            self.assertTrue(over_bytes_1.startswith("unbounded:"))
            self.assertNotEqual(over_bytes_1, over_bytes_2)

    def test_verifier_target_specific_guard_matching_and_unsubstantiated_severity_demotion(
        self,
    ) -> None:
        """Item 6: Guarded dereference in a method calling self.format(user.token) sets contrary_evidence=True, and unsubstantiated high-severity claims do not become Required decisions."""
        # 1. Method calling self.format(user.token) after guarding user and user.token
        snap_method = {
            "src/service.py": (
                "class TokenService:\n"
                "    def render(self, user):\n"
                "        if user is None or user.token is None:\n"
                "            return ''\n"
                "        return self.format(user.token.strip())\n"
            )
        }
        sha_method = _snapshot_digest(snap_method)
        cand_method = CandidateFinding(
            claim="render dereferences user.token without checking for None.",
            triggering_conditions="Call render(user) when user.token is None.",
            impacted_path="src/service.py",
            severity_rationale="Raises AttributeError when user.token is None.",
            evidence=(
                EvidenceReference(
                    path="src/service.py",
                    line=5,
                    snapshot_sha256=sha_method,
                    excerpt="return self.format(user.token.strip())",
                ),
            ),
            severity="high",
            remedy="Check whether user.token is None before calling strip().",
        )
        prep_method = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(cand_method,),
            snapshot=snap_method,
            snapshot_sha256=sha_method,
            evidence_policy="confirmed",
            changed_lines={"src/service.py": frozenset({5})},
            changed_paths=("src/service.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertTrue(prep_method.verifications[0].contrary_evidence)
        self.assertFalse(prep_method.result.comments[0].effective_blocking)
        self.assertFalse(prep_method.result.comments[0].needs_human)

        # 2. Unsubstantiated high-severity claim on non-trivial code with no grounded failure mechanism
        snap_call = {
            "src/service.py": ("def compute_total(items):\n    return sum(items)\n")
        }
        sha_call = _snapshot_digest(snap_call)
        cand_unsubstantiated = CandidateFinding(
            claim="compute_total could be clearer if refactored into a helper.",
            triggering_conditions="Calling compute_total(items).",
            impacted_path="src/service.py",
            severity_rationale="High severity maintainability concern.",
            evidence=(
                EvidenceReference(
                    path="src/service.py",
                    line=2,
                    snapshot_sha256=sha_call,
                    excerpt="return sum(items)",
                ),
            ),
            severity="high",
            remedy="Extract a helper function for summing items.",
        )
        prep_unsubstantiated = prepare_publishable_review(
            ReviewResult(
                summary="Review.",
                comments=(),
                provider="ollama",
                review_status="complete",
            ),
            candidates=(cand_unsubstantiated,),
            snapshot=snap_call,
            snapshot_sha256=sha_call,
            evidence_policy="confirmed",
            changed_lines={"src/service.py": frozenset({2})},
            changed_paths=("src/service.py",),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        comment = prep_unsubstantiated.result.comments[0]
        self.assertFalse(comment.effective_blocking)
        self.assertFalse(comment.needs_human)


if __name__ == "__main__":
    unittest.main()
