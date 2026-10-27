"""Public publication boundaries for literal paths, never glob selectors."""

from dataclasses import replace

from review_sensei_github.approval import evaluate_auto_approval
from review_sensei_github.publication import prepare_publication_review

from review_sensei.baseline import (
    baseline_from_history_document,
    baseline_from_review,
    baseline_history_document,
)
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import (
    BlockerCandidate,
    ReviewConvergencePolicy,
    admit_review_result,
    comment_targets_pr_change,
    derive_blocker_candidate,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewComment, ReviewResult

HEAD = "b" * 40
DIFF = """diff --git a/src/changed.py b/src/changed.py
--- a/src/changed.py
+++ b/src/changed.py
@@ -1 +1,2 @@
 keep
+change
"""


class LiteralBlockerPathsContract:
    def test_retained_result_shape_preserves_all_findings_at_publication_boundary(self):
        # Sanitized shape of the retained failure: complete 31-path analysis,
        # 80 findings, and an unknown FILE target at index 29. No live identity,
        # historical decisions, or incident payload becomes a fixture authority.
        paths = tuple(f"src/file{index}.py" for index in range(31))
        result = ReviewResult(
            summary="Complete synthetic analysis.",
            comments=tuple(
                ReviewComment(
                    path="*" if index == 29 else paths[index % len(paths)],
                    line=None,
                    side="FILE",
                    body=f"Retained finding {index}.",
                    severity="high",
                    category="tests",
                    fix_effort="large",
                    blocking=True,
                )
                for index in range(80)
            ),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=tuple(FileCoverage(path, "reviewed") for path in paths),
                enumerated_paths=paths,
                enumeration_complete=True,
            ),
        )
        # The actual caller restores the retained JSON before preparation.
        result = ReviewResult.from_dict(result.to_dict())
        identities = tuple(finding_instance_fingerprint(c) for c in result.comments)
        key = ReviewContextCacheKey(
            repository="owner/repo",
            pull_request=7,
            base_sha="a" * 40,
            head_sha=HEAD,
            engine="fixture",
            model="fixture-model",
            profile="default",
            stage_digest="a" * 64,
            context_digest="b" * 64,
            learning_digest="c" * 64,
        )
        for mode in ("merge-focused", "strict", "advisory"):
            policy = ReviewConvergencePolicy(mode=mode)
            baseline = baseline_from_history_document(
                baseline_history_document(
                    baseline_from_review(
                        replace(result, comments=result.comments[:29]),
                        cache_key=replace(key, head_sha="c" * 40),
                        policy=policy,
                    ),
                    require_complete=True,
                )
            )
            for context in ({}, {"baseline": baseline, "current_key": key}):
                with self.subTest(mode=mode, baseline=bool(context)):
                    prepared = prepare_publication_review(
                        result=result,
                        diff=DIFF,
                        head_sha=HEAD,
                        convergence_policy=policy,
                        **context,
                    )
                    self.assertEqual(len(prepared.result.comments), 80)
                    self.assertEqual(prepared.result.coverage, result.coverage)
                    self.assertEqual(prepared.result.transaction, result.transaction)
                    self.assertEqual(
                        tuple(
                            finding_instance_fingerprint(c)
                            for c in prepared.result.comments
                        ),
                        identities,
                    )
                    unknown = prepared.result.comments[29]
                    self.assertEqual(
                        (unknown.path, unknown.side, unknown.line), ("*", "FILE", None)
                    )
                    self.assertTrue(unknown.needs_human)
                    self.assertFalse(unknown.effective_blocking)
                    approval = evaluate_auto_approval(
                        result=prepared.result, app_authored=True
                    )
                    self.assertFalse(approval.approved)
                    self.assertIn("human-adjudication-open", approval.blockers)

    def test_literal_git_names_bind_exactly_without_pattern_expansion(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        for path in ("src/[draft]*.py", "src/?.py", "src/[a].py", "*"):
            with self.subTest(path=path):
                comment = ReviewComment(path=path, line=2, body="Finding.")
                facts = derive_blocker_candidate(comment, on_changed_path=True)
                self.assertEqual(facts.path, path)
                self.assertTrue(
                    comment_targets_pr_change(
                        comment, changed_lines={path: frozenset({2})}
                    )
                )
                self.assertFalse(
                    comment_targets_pr_change(
                        comment, changed_lines={"src/a.py": frozenset({2})}
                    )
                )
                other = replace(comment, path="src/a.py")
                with self.assertRaisesRegex(
                    ReviewInputError, "must match review comment"
                ):
                    admit_review_result(
                        ReviewResult(
                            summary="Review.", comments=(other,), provider="fixture"
                        ),
                        policy,
                        candidates=(facts,),
                    )

    def test_literal_path_does_not_supply_evidence_or_changed_scope(self):
        comment = ReviewComment(
            path="src/[draft]*.py",
            line=2,
            body="Finding.",
            severity="high",
            blocking=True,
            fix_effort="small",
        )
        facts = derive_blocker_candidate(comment)
        self.assertEqual(facts.attribution, "unattributed")
        self.assertFalse(facts.evidence_locations_validated)
        self.assertFalse(facts.has_failure_condition)
        self.assertFalse(facts.has_specific_violation)
        admitted = admit_review_result(
            ReviewResult(summary="Review.", comments=(comment,), provider="fixture"),
            ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertTrue(admitted.comments[0].needs_human)
        self.assertFalse(admitted.comments[0].effective_blocking)

    def test_literal_target_survives_real_diff_preparation(self):
        for path in ("src/[draft]*.py", "src/?.py", "src/[a].py", "*"):
            with self.subTest(path=path):
                comment = ReviewComment(
                    path=path,
                    line=2,
                    body="Unverified high-impact finding.",
                    severity="high",
                    blocking=True,
                )
                result = ReviewResult(
                    summary="Review.",
                    comments=(comment,),
                    provider="fixture",
                    review_status="complete",
                )
                prepared = prepare_publication_review(
                    result=result,
                    diff=DIFF.replace("src/changed.py", path),
                    head_sha=HEAD,
                    convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
                )
                retained = prepared.result.comments[0]
                self.assertEqual(
                    (retained.path, retained.line, retained.side), (path, 2, "RIGHT")
                )
                self.assertEqual(
                    finding_instance_fingerprint(retained),
                    finding_instance_fingerprint(comment),
                )
                self.assertTrue(retained.needs_human)
                self.assertFalse(retained.effective_blocking)

    def test_noncanonical_or_unsafe_candidate_paths_still_refuse(self):
        for path in (
            "../[draft]*.py",
            "/src/?.py",
            "src/../*.py",
            "src//*.py",
            "src\\*.py",
            "C:/src/?.py",
            "src/e\u0301?.py",
            "src/\u202e*.py",
            "src/?.py ",
            "src/" + "*" * 256,
        ):
            with self.subTest(path=repr(path)):
                with self.assertRaises(ReviewInputError):
                    BlockerCandidate(path=path)
