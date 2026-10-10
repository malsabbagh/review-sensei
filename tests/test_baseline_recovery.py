"""Fresh full analysis recovers reusable-context invalidation, never ledger trust."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei.baseline import (
    admission_context_from_document,
    baseline_from_history_document,
    baseline_history_document,
)
from review_sensei.cli import main
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.hosting.github.approval import (
    approval_facts_from_result,
    evaluate_approval_facts,
)
from review_sensei.hosting.github.publication import prepare_publication_review
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.outcomes import ResourceBudget
from review_sensei.planning import MAX_RELATED_PATHS, related_paths_for_change
from review_sensei.providers.base import ProviderResponse
from review_sensei.service import DEFAULT_STAGES
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    complete_review_publication,
    load_review_transaction_for_publication,
)
from review_sensei.validation import DEFAULT_REVIEW_LIMITS


class Provider:
    name = "fixture"
    model = "fixture-model"

    def __init__(self):
        self.calls = 0
        self.error = None
        self.hook = None
        self.comments = []

    def complete(self, request):
        self.calls += 1
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.error:
            raise self.error
        return ProviderResponse(
            text=json.dumps(
                {"summary": "Reviewed synthetic change", "comments": self.comments}
            ),
            provider=self.name,
            model=self.model,
        )


class Registry:
    def __init__(self, provider):
        self.provider = provider

    def create(self, settings):
        return self.provider


class BaselineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = SessionIdentity(repository="example/repo", pull_request=7)
        self.provider = Provider()
        self.ledger = LocalSessionLedger(self.root / "ledger")
        (self.root / "fixture.json").write_text(
            '{"summary":"ok","comments":[]}', encoding="utf-8"
        )
        (self.root / "diff.patch").write_text(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1,2 @@\n keep\n+change\n",
            encoding="utf-8",
        )
        with (self.root / "diff.patch").open("a") as handle:
            handle.write(
                "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n"
                "@@ -1 +1,2 @@\n keep\n+related\n"
            )
        self.argv = [
            "--diff",
            str(self.root / "diff.patch"),
            "--provider",
            "fixture",
            "--fixture-response",
            str(self.root / "fixture.json"),
            "--model",
            "fixture-model",
            "--repository",
            "example/repo",
            "--pull-request",
            "7",
            "--base-sha",
            "a" * 40,
            "--head-sha",
            "b" * 40,
            "--review-mode",
            "merge-focused",
            "--session-ledger",
            str(self.root / "ledger"),
            "--format",
            "json",
            "--output",
            str(self.root / "result.json"),
            "--outcome",
            str(self.root / "outcome.json"),
            "--configuration-context-output",
            str(self.root / "configuration.json"),
            "--admission-context-output",
            str(self.root / "admission.json"),
            "--no-learning-proposals",
            "--exit-semantics",
            "operational",
        ]

    def run_cli(self, *, base="a", head="b", extra=()):
        argv = list(self.argv)
        argv[argv.index("--base-sha") + 1] = base * 40
        argv[argv.index("--head-sha") + 1] = head * 40
        stderr = io.StringIO()
        with (
            patch(
                "review_sensei.cli.default_registry",
                return_value=Registry(self.provider),
            ),
            redirect_stderr(stderr),
            patch(
                "review_sensei.cli.DEFAULT_REVIEW_LIMITS",
                replace(DEFAULT_REVIEW_LIMITS, max_diff_files=1)
                if "--orchestrate-large-changes" in extra
                else DEFAULT_REVIEW_LIMITS,
            ),
        ):
            code = main([*argv, *extra])
        outcome_path = self.root / "outcome.json"
        return (
            code,
            stderr.getvalue(),
            json.loads(outcome_path.read_text()) if outcome_path.exists() else {},
        )

    def record(self):
        return self.ledger.load(self.identity).record

    def baseline(self):
        return baseline_from_history_document(
            self.record().convergence_history["baseline"]
        )

    def seed_overflow(self, *, finding=False, long_paths=False, fits=False):
        if finding:
            self.provider.comments = [
                {
                    "path": "app.py",
                    "line": 2,
                    "body": "Authorization failure; reject the unauthorized request.",
                    "blocking": True,
                    "severity": "high",
                    "defect_kind": "authz-failure",
                }
            ]
        self.assertEqual(self.run_cli()[0], 0)
        self.provider.comments = []
        prior = tuple(f"p{i}.py" for i in range(16))
        baseline = replace(self.baseline(), related_paths=prior)
        history = dict(self.record().convergence_history)
        history["baseline"] = baseline_history_document(baseline)
        self.ledger.replace(
            self.identity, lambda record: record.evolve(convergence_history=history)
        )
        current = prior[: 7 if fits else 8] + tuple(
            f"{'x' * 240 if long_paths else 'n'}{i}.py"
            for i in range(25 if fits else MAX_RELATED_PATHS + 7)
        )
        related = related_paths_for_change(current)
        self.assertEqual(
            (len(prior), len(related), len(set(prior) | set(related))),
            (16, 32, 41) if fits else (16, MAX_RELATED_PATHS, MAX_RELATED_PATHS + 9),
        )
        (self.root / "diff.patch").write_text(
            "".join(
                f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,2 @@\n keep\n+change\n"
                for path in current
            )
        )
        return self.record()

    def test_overflow_runs_one_full_analysis_then_exact_retry_skips(self):
        before = self.seed_overflow()
        prior = self.baseline()
        code, stderr, outcome = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(
            outcome["stage_summary"]["baseline_refresh"], "related-context-overflow"
        )
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertEqual(result.coverage_mode, "fallback-full")
        self.assertEqual(len(result.coverage.enumerated_paths), MAX_RELATED_PATHS + 15)
        self.assertTrue(result.coverage.fully_reviewed)
        self.assertEqual(self.baseline().cache_key.head_sha, "d" * 40)
        self.assertEqual(
            self.record().completed_verification_rounds,
            before.completed_verification_rounds + 1,
        )
        restored, key = admission_context_from_document(
            json.loads((self.root / "admission.json").read_text())
        )
        self.assertEqual(restored, prior)
        self.assertEqual(key.head_sha, "d" * 40)
        generation = self.record().generation
        self.assertEqual(
            self.run_cli(head="d")[2]["diagnostic"], "publication_recovery_required"
        )
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.record().generation, generation)

    def test_original_41_path_case_runs_incremental_once_and_exact_retry_skips(self):
        before = self.seed_overflow(fits=True)
        code, stderr, outcome = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.provider.calls, 2)
        self.assertNotIn("baseline_refresh", outcome["stage_summary"])
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertEqual(result.coverage_mode, "incremental")
        self.assertEqual(len(self.baseline().related_paths), 41)
        self.assertTrue(self.baseline().complete)
        self.assertEqual(
            self.record().completed_verification_rounds,
            before.completed_verification_rounds + 1,
        )
        self.assertEqual(
            self.run_cli(head="d")[2]["diagnostic"], "publication_recovery_required"
        )
        self.assertEqual(self.provider.calls, 2)

    def test_incremental_41_omitted_blocker_retains_exact_history_and_holds_approval(
        self,
    ):
        before = self.seed_overflow(finding=True, fits=True)
        code, stderr, outcome = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(self.provider.calls, 2)
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertEqual(result.coverage_mode, "incremental")
        self.assertIn(
            self.baseline().findings[0].fingerprint,
            {
                item.fingerprint
                for item in result.finding_lifecycles
                if item.state == "uncertain"
            },
        )
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)
        self.assertFalse(
            evaluate_approval_facts(
                approval_facts_from_result(
                    result,
                    enabled=True,
                    app_authored=False,
                    check_published=True,
                )
            ).approved
        )

    def test_incremental_41_downgraded_blocker_cannot_replace_history(self):
        before = self.seed_overflow(finding=True, fits=True)
        self.provider.comments = [
            {
                "path": "app.py",
                "line": 2,
                "body": "Reworded authorization concern.",
                "blocking": False,
                "severity": "low",
                "defect_kind": "authz-failure",
            }
        ]
        code, stderr, outcome = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.provider.calls, 2)

    def test_overflow_omitted_blocker_stays_uncertain_and_history_survives_replay(self):
        before = self.seed_overflow(finding=True)
        code, stderr, outcome = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(self.provider.calls, 2)
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertEqual(result.review_status, "partial")
        self.assertEqual(result.finding_lifecycles[0].state, "uncertain")
        self.assertEqual(
            result.finding_lifecycles[0].fingerprint,
            self.baseline().findings[0].fingerprint,
        )
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)
        eligibility = evaluate_approval_facts(
            approval_facts_from_result(
                result,
                enabled=True,
                app_authored=False,
                check_published=True,
            )
        )
        self.assertFalse(eligibility.approved)
        self.assertIn("review-partial", eligibility.blockers)
        tx = result.transaction
        for _ in range(2):
            loaded = load_review_transaction_for_publication(
                self.ledger,
                self.identity,
                result,
                base_sha="a" * 40,
                head_sha="d" * 40,
                policy_digest=tx.policy_digest,
                configuration_digest=tx.configuration_digest,
                evidence_digest=tx.evidence_digest,
            )
            complete_review_publication(
                self.ledger, self.identity, loaded.transaction, published=True
            )
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.record().failed_attempts, 1)
        self.assertEqual(self.record().convergence_history, before.convergence_history)

    def test_overflow_full_evidence_that_cannot_fit_publishes_partial_keeps_history(
        self,
    ):
        before = self.seed_overflow(long_paths=True)
        # Inject the allocator's capacity seam because this otherwise valid
        # compact fixture now fits. Real high-entropy overflow is separately
        # covered by test_evidence_capacity; this proves CLI cleanup/status.
        with patch(
            "review_sensei.session.checkpoint_baseline_capacity", return_value=128
        ):
            code, stderr, outcome = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(outcome["diagnostic"], "baseline_capacity_exceeded")
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertEqual(result.persistence_status, "capacity-exceeded")
        self.assertIn('"encoded-bytes"', stderr)
        self.assertNotIn('"decoded-bytes"', stderr)
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)
        self.assertIsNone(self.record().reservation_id)

    def oversized_comments(self):
        return [
            {
                "path": "app.py",
                "line": 2,
                "body": f"Complete diagnostic finding {number}: preserve this evidence.",
                "blocking": False,
                "severity": "low",
                "defect_kind": f"distinct-defect-{number}",
            }
            for number in range(3)
        ]

    def checkpoint_outputs(self):
        return (
            "--checkpoint-evidence-output",
            str(self.root / "checkpoint-evidence.json"),
            "--checkpoint-diagnostics-output",
            str(self.root / "checkpoint-diagnostics.json"),
        )

    def test_twelve_findings_establish_complete_baseline_without_rerunning_on_replay(
        self,
    ):
        self.provider.comments = self.oversized_comments() * 4
        # Use distinct stable defect identities, as ordinary reviews do.
        for i, comment in enumerate(self.provider.comments):
            self.provider.comments[i] = {
                **comment,
                "defect_kind": f"distinct-defect-{i}",
                "body": f"Unique concern {i}: preserve this evidence.",
            }
        code, stderr, outcome = self.run_cli(extra=self.checkpoint_outputs())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "reviewed")
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(len(result.comments), 12)
        self.assertEqual(len(self.baseline().findings), 12)
        self.assertEqual(self.record().failed_attempts, 0)
        self.assertEqual(self.record().completed_initial_reviews, 1)
        self.assertIsNone(self.record().reservation_id)
        complete_review_publication(
            self.ledger, self.identity, result.transaction, published=True
        )
        calls = self.provider.calls
        code, stderr, _ = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.provider.calls, calls)

    def test_checkpoint_io_failure_retains_full_evidence_and_cleans_reservation(self):
        self.provider.comments = self.oversized_comments()
        with patch(
            "review_sensei.session.checkpoint_review_analysis",
            side_effect=OSError("synthetic persistence failure"),
        ):
            code, stderr, _ = self.run_cli(extra=self.checkpoint_outputs())
        self.assertEqual(code, 1, stderr)
        self.assertFalse((self.root / "result.json").exists())
        evidence = json.loads((self.root / "checkpoint-evidence.json").read_text())
        self.assertEqual(len(evidence["result"]["comments"]), 3)
        self.assertIsNone(self.record().reservation_id)
        self.assertEqual(self.record().failed_attempts, 1)
        self.assertIsNone(self.record().transaction)

    def test_diagnostic_evidence_write_failure_cleans_reservation_once(self):
        self.provider.comments = self.oversized_comments()
        code, stderr, _ = self.run_cli(
            extra=(
                "--checkpoint-evidence-output",
                str(self.root / "missing" / "evidence.json"),
            )
        )
        self.assertEqual(code, 1, stderr)
        self.assertIsNone(self.record().reservation_id)
        self.assertEqual(self.record().failed_attempts, 1)
        self.assertIsNone(self.record().transaction)

    def test_overflow_diagnostics_do_not_opt_into_content_retention(self):
        self.provider.comments = self.oversized_comments()
        with patch(
            "review_sensei.session.checkpoint_baseline_capacity", return_value=128
        ):
            code, stderr, _ = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertIn('"finding_limit": 512', stderr)
        self.assertNotIn("app.py", stderr)
        self.assertNotIn("preserve this evidence", stderr)
        self.assertFalse((self.root / "checkpoint-evidence.json").exists())
        self.assertFalse((self.root / "checkpoint-diagnostics.json").exists())

    def test_overflow_chunked_review_obeys_budget_without_restarting_analysis(self):
        before = self.seed_overflow()
        calls = self.provider.calls
        with patch(
            "review_sensei.cli.ResourceBudget.for_limits",
            return_value=replace(
                ResourceBudget.for_limits(DEFAULT_REVIEW_LIMITS), max_provider_calls=2
            ),
        ):
            code, stderr, outcome = self.run_cli(
                head="d", extra=("--orchestrate-large-changes",)
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(self.provider.calls - calls, 2)
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)

    def test_overflow_provider_downgrade_cannot_clear_prior_blocker(self):
        before = self.seed_overflow(finding=True)
        self.provider.comments = [
            {
                "path": "app.py",
                "line": 2,
                "body": "Reworded authorization concern.",
                "blocking": False,
                "severity": "low",
                "defect_kind": "authz-failure",
            }
        ]
        code, stderr, outcome = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertIn(
            self.baseline().findings[0].fingerprint,
            {
                item.fingerprint
                for item in result.finding_lifecycles
                if item.state == "uncertain"
            },
        )
        self.assertEqual(self.record().convergence_history, before.convergence_history)

    def test_overflow_failed_or_partial_analysis_cannot_replace_history(self):
        before = self.seed_overflow()
        self.provider.error = RuntimeError("synthetic provider failure")
        self.assertNotEqual(self.run_cli(head="d")[0], 0)
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)
        self.provider.error = None
        with (self.root / "diff.patch").open("a") as handle:
            handle.write(
                "diff --git a/logo.bin b/logo.bin\nBinary files a/logo.bin and b/logo.bin differ\n"
            )
        code, stderr, outcome = self.run_cli(head="e")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(self.record().convergence_history, before.convergence_history)

    def test_overflow_publication_new_finding_still_requires_human(self):
        self.seed_overflow()
        prior = self.baseline()
        self.provider.comments = [
            {
                "path": "n0.py",
                "line": 2,
                "body": "An unchecked request bypasses authorization; reject unauthorized input.",
                "severity": "high",
                "defect_kind": "authz-failure",
                "fix_effort": "small",
            }
        ]
        code, stderr, _ = self.run_cli(head="d")
        self.assertEqual(code, 0, stderr)
        _, key = admission_context_from_document(
            json.loads((self.root / "admission.json").read_text())
        )
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        prepared = prepare_publication_review(
            result=result,
            diff=(self.root / "diff.patch").read_text(),
            head_sha="d" * 40,
            baseline=prior,
            current_key=key,
            related_paths=related_paths_for_change(result.coverage.enumerated_paths),
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertTrue(prepared.result.comments[0].needs_human)

    def test_base_change_runs_full_and_refreshes_only_after_checkpoint(self):
        for orchestrate in (False, True):
            with self.subTest(orchestrate=orchestrate):
                # Separate identity for each direct/chunked lifecycle.
                self.ledger = LocalSessionLedger(self.root / f"ledger-{orchestrate}")
                self.argv[self.argv.index("--session-ledger") + 1] = str(
                    self.ledger.root
                )
                extra = ("--orchestrate-large-changes",) if orchestrate else ()
                self.assertEqual(self.run_cli(extra=extra)[0], 0)
                before = self.record()
                prior = self.baseline()
                calls = self.provider.calls
                code, stderr, outcome = self.run_cli(base="c", head="d", extra=extra)
                self.assertEqual(code, 0, stderr)
                self.assertGreater(self.provider.calls, calls)
                self.assertEqual(self.provider.calls - calls, 2 if orchestrate else 1)
                self.assertEqual(outcome["status"], "reviewed")
                self.assertEqual(
                    outcome["stage_summary"]["baseline_refresh"],
                    "rebase-or-base-change",
                )
                result = json.loads((self.root / "result.json").read_text())
                self.assertEqual(result.get("coverage_mode", "full"), "full")
                self.assertEqual(result["transaction"]["base_sha"], "c" * 40)
                self.assertEqual(result["transaction"]["head_sha"], "d" * 40)
                self.assertEqual(self.baseline().cache_key.base_sha, "c" * 40)
                self.assertEqual(self.baseline().cache_key.head_sha, "d" * 40)
                admission = json.loads((self.root / "admission.json").read_text())
                self.assertEqual(
                    baseline_from_history_document(admission["baseline"]), prior
                )
                self.assertEqual(
                    self.record().completed_initial_reviews,
                    before.completed_initial_reviews,
                )
                self.assertEqual(
                    self.record().completed_verification_rounds,
                    before.completed_verification_rounds + 1,
                )
                self.assertEqual(self.record().dispositions, before.dispositions)

    def test_same_head_new_base_runs_once_then_exact_retry_skips(self):
        self.assertEqual(self.run_cli()[0], 0)
        code, stderr, _ = self.run_cli(base="c")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.baseline().cache_key.base_sha, "c" * 40)
        generation = self.record().generation
        code, _, outcome = self.run_cli(base="c")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["status"], "action_required")
        self.assertEqual(outcome["diagnostic"], "publication_recovery_required")
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.record().generation, generation)

    def test_failed_full_review_preserves_baseline_and_charges_attempt(self):
        self.assertEqual(self.run_cli()[0], 0)
        before = self.record()
        self.provider.error = RuntimeError("synthetic provider failure")
        self.assertNotEqual(self.run_cli(base="c", head="d")[0], 0)
        after = self.record()
        self.assertEqual(after.convergence_history, before.convergence_history)
        self.assertEqual(after.completed_verification_rounds, 0)
        self.assertEqual(after.failed_attempts, 1)
        self.assertIsNone(after.reservation_id)

    def test_wrong_baseline_identity_stays_blocked_even_with_new_base(self):
        self.assertEqual(self.run_cli()[0], 0)
        history = json.loads(json.dumps(self.record().convergence_history))
        history["baseline"]["cache_key"]["repository"] = "example/other"
        self.ledger.replace(
            self.identity, lambda record: record.evolve(convergence_history=history)
        )
        code, stderr, outcome = self.run_cli(base="c", head="d")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["diagnostic"], "durable_baseline_recovery_required")
        self.assertEqual(self.provider.calls, 1)
        self.assertIn("baseline-identity-mismatch", stderr)
        self.assertIsNone(self.record().reservation_id)
        self.assertEqual(self.record().convergence_history, history)

    def test_pause_precedes_recovery_and_diagnostic_survives_disabled_artifacts(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.ledger.replace(
            self.identity, lambda record: record.evolve(operator_paused=True)
        )
        summary = self.root / "summary.md"
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            code, stderr, outcome = self.run_cli(base="c", head="d")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["diagnostic"], "paused")
        self.assertIn("paused", stderr)
        self.assertIn("paused", summary.read_text())
        self.assertEqual(self.provider.calls, 1)

    def test_interrupted_recovery_preserves_history_and_does_not_charge(self):
        self.assertEqual(self.run_cli()[0], 0)
        before = self.record()
        self.provider.error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_cli(base="c", head="d")
        after = self.record()
        self.assertEqual(after.convergence_history, before.convergence_history)
        self.assertEqual(after.failed_attempts, 0)
        self.assertIsNone(after.reservation_id)
        self.provider.error = None
        self.assertEqual(self.run_cli(base="c", head="d")[0], 0)
        self.assertEqual(self.baseline().cache_key.base_sha, "c" * 40)

    def test_partial_full_review_retains_baseline_and_publication_replay_charges_once(
        self,
    ):
        self.assertEqual(self.run_cli()[0], 0)
        before = self.record()
        with (self.root / "diff.patch").open("a") as handle:
            handle.write(
                "diff --git a/logo.bin b/logo.bin\nBinary files a/logo.bin and b/logo.bin differ\n"
            )
        code, stderr, outcome = self.run_cli(base="c", head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        self.assertEqual(result.review_status, "partial")
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)
        tx = result.transaction
        for _ in range(2):
            loaded = load_review_transaction_for_publication(
                self.ledger,
                self.identity,
                result,
                base_sha="c" * 40,
                head_sha="d" * 40,
                policy_digest=tx.policy_digest,
                configuration_digest=tx.configuration_digest,
                evidence_digest=tx.evidence_digest,
            )
            complete_review_publication(
                self.ledger,
                self.identity,
                loaded.transaction,
                published=True,
            )
        self.assertEqual(self.record().failed_attempts, 1)
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.provider.calls, 2)

    def test_concurrent_invocation_remains_paused_and_owner_checkpoints(self):
        self.assertEqual(self.run_cli()[0], 0)
        competing = []
        self.provider.hook = lambda: competing.append(self.run_cli(base="c", head="d"))
        self.assertEqual(self.run_cli(base="c", head="d")[0], 0)
        self.assertEqual(competing[0][0], 1)
        self.assertEqual(competing[0][2]["diagnostic"], "paused")
        self.assertEqual(self.provider.calls, 2)
        self.assertIsNone(self.record().reservation_id)

    def test_same_head_changed_stage_context_runs_full_once(self):
        self.assertEqual(self.run_cli()[0], 0)
        stages = tuple(
            replace(
                stage,
                prompt_template=stage.prompt_template + "\nReview synthetic criterion.",
            )
            for stage in DEFAULT_STAGES
        )
        with (
            patch("review_sensei.cli.DEFAULT_STAGES", stages),
            patch("review_sensei.service.DEFAULT_STAGES", stages),
        ):
            code, stderr, outcome = self.run_cli()
            self.assertEqual(code, 0, stderr)
            self.assertEqual(self.provider.calls, 2)
            self.assertEqual(
                outcome["stage_summary"]["baseline_refresh"],
                "prompt-or-stage-digest-change",
            )
            retry_code, _, retry_outcome = self.run_cli()
            self.assertEqual(retry_code, 1)
            self.assertEqual(
                retry_outcome["diagnostic"], "publication_recovery_required"
            )
            self.assertEqual(self.provider.calls, 2)

    def test_fresh_fallback_findings_keep_human_adjudication(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.run_cli(base="c", head="d")[0], 0)
        baseline, key = admission_context_from_document(
            json.loads((self.root / "admission.json").read_text())
        )
        result = ReviewResult.from_dict(
            json.loads((self.root / "result.json").read_text())
        )
        finding = ReviewComment(
            path="app.py",
            line=2,
            body="An unchecked request bypasses authorization; reject unauthorized input.",
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        prepared = prepare_publication_review(
            result=replace(result, comments=(finding,), transaction=None),
            diff=(self.root / "diff.patch").read_text(),
            head_sha="d" * 40,
            baseline=baseline,
            current_key=key,
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertTrue(prepared.result.comments[0].needs_human)
        eligibility = evaluate_approval_facts(
            approval_facts_from_result(
                prepared.result,
                enabled=True,
                app_authored=False,
                check_published=True,
            )
        )
        self.assertFalse(eligibility.approved)
        self.assertIn("human-adjudication-open", eligibility.blockers)

    def test_incomplete_and_future_baselines_are_blocked_without_inference(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.record().convergence_history
        for future in (False, True):
            change = (
                {"generation": self.record().generation + 1}
                if future
                else {"complete": False}
            )
            history = json.loads(json.dumps(original))
            history["baseline"].update(change)
            self.ledger.replace(
                self.identity, lambda record: record.evolve(convergence_history=history)
            )
            self.assertEqual(self.run_cli(base="c", head="d")[0], 1)
            self.assertEqual(self.provider.calls, 1)
            self.assertEqual(self.record().convergence_history, history)

    def test_narrowed_history_runs_full_without_trusting_truncated_coverage(self):
        self.assertEqual(self.run_cli()[0], 0)
        history = json.loads(json.dumps(self.record().convergence_history))
        history["baseline"].update(complete=False, coverage_complete=False)
        self.ledger.replace(
            self.identity, lambda record: record.evolve(convergence_history=history)
        )
        code, stderr, outcome = self.run_cli(base="c", head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            outcome["stage_summary"]["baseline_refresh"], "coverage-incomplete"
        )
        self.assertTrue(self.baseline().coverage_complete)

    def test_failed_attempt_limit_cannot_be_bypassed_by_same_head_new_context(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.ledger.replace(
            self.identity,
            lambda record: record.evolve(
                failed_attempts=6, failed_attempts_head_sha="b" * 40
            ),
        )
        code, stderr, outcome = self.run_cli(base="c")
        self.assertEqual(code, 1, stderr)
        self.assertEqual(outcome["diagnostic"], "failed-attempt-budget-exhausted")
        self.assertEqual(self.provider.calls, 1)

    def test_empty_diff_does_not_refresh_baseline(self):
        self.assertEqual(self.run_cli()[0], 0)
        before = self.record()
        (self.root / "diff.patch").write_text("")
        self.run_cli(base="c", head="d")
        self.assertEqual(self.record(), before)
        self.assertEqual(self.provider.calls, 1)

    def test_nontransaction_operator_rounds_keep_existing_admission(self):
        for flag in ("--configuration-context-output", "--admission-context-output"):
            index = self.argv.index(flag)
            del self.argv[index : index + 2]
        self.assertEqual(self.run_cli()[0], 0)
        reservation = self.record().reservation_id
        self.assertIsNotNone(reservation)
        code, stderr, outcome = self.run_cli(base="c", head="d")
        self.assertEqual(code, 1, stderr)
        self.assertEqual(outcome["diagnostic"], "paused")
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.record().reservation_id, reservation)
        self.assertIsNone(self.record().transaction)

    def test_unrecognized_or_nonfallback_scope_cannot_authorize_full_review(self):
        self.assertEqual(self.run_cli()[0], 0)
        for mode, reason, diagnostic in (
            ("full", "rebase-or-base-change", "rebase-or-base-change"),
            ("fallback-full", "unexpected-private-detail", "unverifiable-scope"),
        ):
            with self.subTest(mode=mode, reason=reason):
                before = self.record().convergence_history
                scope = SimpleNamespace(
                    status="incompatible",
                    incremental=None,
                    coverage_mode=mode,
                    invalidation_reason=reason,
                )
                with patch(
                    "review_sensei.cli.plan_verification_scope", return_value=scope
                ):
                    code, stderr, outcome = self.run_cli(base="c", head="d")
                self.assertEqual(code, 1, stderr)
                self.assertEqual(
                    outcome["stage_summary"]["baseline_recovery"], diagnostic
                )
                self.assertEqual(self.provider.calls, 1)
                self.assertEqual(self.record().convergence_history, before)
                self.assertNotIn("unexpected-private-detail", stderr)
                self.assertNotIn("unexpected-private-detail", json.dumps(outcome))

    def test_blocked_recovery_prints_reason_without_an_outcome_artifact(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.ledger.replace(
            self.identity, lambda record: record.evolve(convergence_history=None)
        )
        outcome_path = self.root / "outcome.json"
        outcome_path.unlink()
        index = self.argv.index("--outcome")
        del self.argv[index : index + 2]
        summary = self.root / "summary.md"
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            code, stderr, outcome = self.run_cli(base="c", head="d")
        self.assertEqual(code, 1)
        self.assertEqual(outcome, {})
        self.assertFalse(outcome_path.exists())
        self.assertIn("action_required: durable_baseline_recovery_required", stderr)
        self.assertIn("missing-or-unfinished-history", stderr)
        self.assertEqual(stderr.count("missing-or-unfinished-history"), 1)
        self.assertIn("missing-or-unfinished-history", summary.read_text())
        self.assertNotIn("change", summary.read_text())
        self.assertEqual(self.provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
