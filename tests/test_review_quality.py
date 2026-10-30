import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_publication_verifier as verifier_fixtures
from review_sensei_github.approval import evaluate_auto_approval

from review_sensei.cli import main
from review_sensei.configuration import (
    ConfigurationError,
    parse_configuration_text,
    render_configuration,
    resolve_inference,
)
from review_sensei.convergence import (
    ReviewConvergencePolicy,
    policy_from_mapping,
    resolve_review_convergence_policy,
)
from review_sensei.errors import ReviewInputError
from review_sensei.hosted import HostedPolicy, plan_hosted_execution
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.verifier import (
    prepare_publishable_review,
)


class ReviewQualityConfigurationTests(unittest.TestCase):
    def test_yaml_only_quality_preserves_default_and_binds_policy_provenance(self):
        default = parse_configuration_text("schema: 1\n")
        self.assertEqual(default.review.quality, "current")
        self.assertNotIn("quality", ReviewConvergencePolicy().identity_fields())
        for quality in ("current", "balanced", "high-signal"):
            with self.subTest(quality=quality):
                configuration = parse_configuration_text(
                    "schema: 1\nreview:\n  quality: " + quality + "\n",
                    source="trusted.yml",
                )
                self.assertEqual(configuration.review.quality, quality)
                self.assertEqual(configuration.line_of("review.quality"), 3)
                hosted = plan_hosted_execution(configuration, environ={})
                self.assertEqual(hosted.policy.review_quality, quality)
                self.assertEqual(
                    HostedPolicy(**json.loads(json.dumps(hosted.policy.to_dict()))),
                    hosted.policy,
                )
                rendered = render_configuration(
                    configuration, resolve_inference(configuration, environ={})
                )
                self.assertIn(
                    f"quality: {quality} [trusted.yml review.quality]", rendered
                )
                policy = resolve_review_convergence_policy(quality=quality)
                self.assertEqual(policy.quality, quality)
                self.assertEqual(policy_from_mapping(policy.to_dict()), policy)
                self.assertEqual(
                    ReviewTransaction.compute_policy_digest(policy.identity_fields()),
                    policy.digest(),
                )
                if quality != "current":
                    self.assertNotEqual(
                        policy.digest(), ReviewConvergencePolicy().digest()
                    )
        for value in ("unknown", "true", "12"):
            with self.subTest(invalid=value), self.assertRaises(ConfigurationError):
                parse_configuration_text(
                    "schema: 1\nreview:\n  quality: " + value + "\n"
                )
        with self.assertRaises(ConfigurationError):
            parse_configuration_text("schema: 1\nreview:\n  max_active_findings: 2\n")

    def test_cli_transaction_uses_yaml_quality_in_accepted_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yml").write_text(
                "schema: 1\nreview:\n  quality: high-signal\n"
            )
            (root / "diff.patch").write_text(
                "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,2 @@\n keep\n+change\n",
                encoding="utf-8",
                newline="\n",
            )
            (root / "response.json").write_text(
                json.dumps({"summary": "ok", "comments": []})
            )
            argv = [
                "--config",
                str(root / "config.yml"),
                "--diff",
                str(root / "diff.patch"),
                "--provider",
                "fixture",
                "--fixture-response",
                str(root / "response.json"),
                "--model",
                "fixture-v1",
                "--repository",
                "owner/repo",
                "--pull-request",
                "1",
                "--base-sha",
                "a" * 40,
                "--head-sha",
                "b" * 40,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(root / "ledger"),
                "--transaction",
                "--format",
                "json",
                "--output",
                str(root / "review.json"),
                "--no-learning-proposals",
            ]
            code = main(argv)
            self.assertEqual(code, 0)
            document = json.loads((root / "review.json").read_text())
            self.assertEqual(
                document["transaction"]["policy_digest"],
                ReviewConvergencePolicy(quality="high-signal").digest(),
            )
            self.assertEqual(
                document["review_verdict"]["policy_digest"],
                document["transaction"]["policy_digest"],
            )
            self.assertFalse(document["review_verdict"]["unselected_blocking_defect"])
            self.assertFalse(document["review_verdict"]["unselected_human_decision"])

            # A compatible empty incremental scope normally reuses the baseline
            # without inference. A fresh high-signal review must discover again.
            from review_sensei.baseline import plan_verification_scope

            def no_changed_paths(**kwargs):
                self.assertEqual(kwargs["baseline"].cache_key.head_sha, "b" * 40)
                self.assertEqual(
                    kwargs["baseline"].policy_digest, kwargs["policy"].digest()
                )
                self.assertEqual(kwargs["policy"].quality, "high-signal")
                scope = plan_verification_scope(**kwargs)
                self.assertIsNotNone(scope.incremental)
                return replace(
                    scope,
                    reviewed_paths=(),
                    incremental=replace(scope.incremental, reviewed_paths=()),
                )

            next_argv = list(argv)
            next_argv[next_argv.index("--head-sha") + 1] = "c" * 40
            next_argv.extend(["--outcome", str(root / "outcome.json")])
            with (
                patch(
                    "review_sensei.cli.plan_verification_scope",
                    side_effect=no_changed_paths,
                ),
                patch(
                    "review_sensei.cli._run_review_provider",
                    wraps=__import__(
                        "review_sensei.cli", fromlist=["_run_review_provider"]
                    )._run_review_provider,
                ) as provider,
            ):
                self.assertEqual(main(next_argv), 0)
            next_document = json.loads((root / "review.json").read_text())
            self.assertEqual(next_document.get("coverage_mode", "full"), "full")
            self.assertEqual(
                json.loads((root / "outcome.json").read_text())["provider_calls"], 1
            )
            self.assertIsNone(provider.call_args.kwargs["incremental"])
            self.assertEqual(provider.call_args.kwargs["budget"].max_provider_calls, 8)
            self.assertFalse(
                next_document["review_verdict"]["unselected_blocking_defect"]
            )


class HighSignalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"src/app.py": "keep\nreturn total / len(values)\n"}
        self.digest = verifier_fixtures.snapshot_digest(self.snapshot)
        self.result = ReviewResult(
            summary="Review complete.",
            comments=(),
            provider="fixture",
            review_status="complete",
        )
        self.policy = ReviewConvergencePolicy(quality="high-signal")

    def prepare(self, candidates, **kwargs):
        return prepare_publishable_review(
            kwargs.pop("result", self.result),
            candidates=candidates,
            snapshot=self.snapshot,
            snapshot_sha256=self.digest,
            evidence_policy="confirmed",
            convergence_policy=kwargs.pop("policy", self.policy),
            **kwargs,
        )

    def test_completed_filtering_is_complete_without_public_candidate_counts(self):
        valid = verifier_fixtures.candidate(snapshot_sha=self.digest)
        disproved = verifier_fixtures.candidate(
            snapshot_sha=self.digest, excerpt="not the source"
        )
        for candidates, extra in (
            ((valid, valid), {}),
            (
                (
                    valid,
                    replace(disproved, claim="Distinct independently disproved claim"),
                ),
                {},
            ),
            ((disproved,), {}),
            ((valid,), {"changed_lines": {"src/other.py": frozenset({2})}}),
        ):
            with self.subTest(candidates=candidates, extra=extra):
                prepared = self.prepare(candidates, **extra)
                self.assertEqual(prepared.result.review_status, "complete")
                self.assertEqual(prepared.result.summary, "Review complete.")
                if not prepared.result.comments:
                    decision = evaluate_auto_approval(
                        enabled=True,
                        app_authored=False,
                        result=prepared.result,
                        has_open_review_threads=False,
                    )
                    self.assertTrue(decision.approved, decision.blockers)
                self.assertTrue(
                    all(v.evaluation_complete for v in prepared.verifications)
                )
                current = self.prepare(
                    candidates, policy=ReviewConvergencePolicy(), **extra
                )
                self.assertEqual(current.result.review_status, "partial")
                self.assertIn("Verification coverage:", current.result.summary)
        superseded = replace(
            self.result,
            comments=(ReviewComment(path="src/app.py", body="old", line=2),),
        )
        self.assertEqual(
            self.prepare((valid,), result=superseded).result.review_status, "complete"
        )

    def test_missing_invalid_and_incomplete_evidence_still_gate_without_counts(self):
        valid = verifier_fixtures.candidate(snapshot_sha=self.digest)
        missing = verifier_fixtures.candidate(
            snapshot_sha=self.digest, path="missing.py"
        )
        wrong_snapshot = verifier_fixtures.candidate(snapshot_sha="a" * 64)
        invalid_line = verifier_fixtures.candidate(snapshot_sha=self.digest, line=99)
        for rejected in (missing, wrong_snapshot, invalid_line):
            with self.subTest(rejected=rejected):
                prepared = self.prepare((rejected,))
                self.assertEqual(prepared.result.review_status, "partial")
                self.assertFalse(prepared.verifications[0].evaluation_complete)
                self.assertFalse(
                    evaluate_auto_approval(
                        enabled=True,
                        app_authored=False,
                        result=prepared.result,
                        has_open_review_threads=False,
                    ).approved
                )
                self.assertEqual(
                    prepared.result.summary,
                    "Review complete.\n\nEvidence verification could not complete against the reviewed source.",
                )
                for candidates in ((rejected, rejected), (valid, rejected)):
                    mixed = self.prepare(candidates)
                    self.assertEqual(mixed.result.review_status, "partial")
                    self.assertEqual(mixed.result.summary, prepared.result.summary)
                    self.assertFalse(
                        evaluate_auto_approval(
                            enabled=True,
                            app_authored=False,
                            result=mixed.result,
                            has_open_review_threads=False,
                        ).approved
                    )
                    if candidates[0] is valid:
                        self.assertTrue(mixed.result.comments)
        for status in ("partial", "incomplete", "summary-only"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.prepare(
                        (valid, valid),
                        result=replace(self.result, review_status=status),
                    ).result.review_status,
                    status,
                )
        with self.assertRaises(ReviewInputError):
            self.prepare((object(),))
        with self.assertRaises(ReviewInputError):
            prepare_publishable_review(
                self.result,
                candidates=(valid,),
                evidence_policy="confirmed",
                convergence_policy=self.policy,
            )


class HighSignalPublicationBindingTests(unittest.TestCase):
    def test_prepared_quality_cannot_be_rebound_to_another_policy(self):
        import test_github_publication as publication_fixtures
        from review_sensei_github.publication import (
            GitHubPublicationError,
            ReviewPublisher,
        )

        http, calls = publication_fixtures.make_http([])
        publisher = ReviewPublisher(http=http)
        original = publication_fixtures.result()
        prepared = publisher.prepare(
            result=original,
            diff=publication_fixtures.DIFF,
            head_sha="b" * 40,
            convergence_policy=ReviewConvergencePolicy(
                mode="legacy", quality="high-signal"
            ),
        )
        for quality in ("current", "balanced"):
            with (
                self.subTest(quality=quality),
                patch.object(
                    http,
                    "request",
                    side_effect=AssertionError("HTTP before context validation"),
                ) as request,
                self.assertRaisesRegex(GitHubPublicationError, "publication context"),
            ):
                publisher.publish(
                    token="token",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=2,
                    head_sha="b" * 40,
                    base_branch="main",
                    base_sha="a" * 40,
                    result=original,
                    diff=publication_fixtures.DIFF,
                    app_slug="reviewsensei[bot]",
                    prepared_review=prepared,
                    convergence_policy=ReviewConvergencePolicy(
                        mode="legacy", quality=quality
                    ),
                    allow_retired_legacy_policy=True,
                )
            request.assert_not_called()
        self.assertEqual(calls, [])


class HighSignalFallbackTests(unittest.TestCase):
    def test_harmless_unmatched_optional_candidate_does_not_become_human_on_fallback(
        self,
    ):
        import test_verification_baseline as baseline_fixtures

        from review_sensei.baseline import baseline_from_review
        from review_sensei.convergence import admit_review_result

        original = baseline_fixtures._comment()
        baseline = baseline_from_review(
            baseline_fixtures._result(original),
            cache_key=baseline_fixtures._key(),
            policy=ReviewConvergencePolicy(),
        )
        optional = baseline_fixtures._comment(
            path="src/helper.py",
            category="style",
            blocking=False,
            severity="low",
            symbol="format_label",
            defect_kind="style",
        )
        for quality, expected_human in (
            ("current", True),
            ("balanced", True),
            ("high-signal", False),
        ):
            with self.subTest(quality=quality):
                admitted = admit_review_result(
                    baseline_fixtures._result(optional),
                    ReviewConvergencePolicy(quality=quality),
                    baseline=baseline,
                    current_key=baseline_fixtures._key(
                        head_sha=baseline_fixtures.SHA_C, model="different-model"
                    ),
                    changed_paths=("src/helper.py",),
                    changed_lines={"src/helper.py": frozenset({2})},
                )
                self.assertEqual(admitted.comments[0].path, optional.path)
                self.assertEqual(admitted.comments[0].symbol, optional.symbol)
                self.assertEqual(admitted.comments[0].category, optional.category)
                self.assertEqual(admitted.comments[0].needs_human, expected_human)
                self.assertFalse(admitted.comments[0].effective_blocking)
        for concern in (
            original,
            replace(original, category="style", blocking=False, severity="low"),
            replace(optional, category="correctness", blocking=True, severity="high"),
        ):
            admitted = admit_review_result(
                baseline_fixtures._result(concern),
                ReviewConvergencePolicy(quality="high-signal"),
                baseline=baseline,
                current_key=baseline_fixtures._key(
                    head_sha=baseline_fixtures.SHA_C, model="different-model"
                ),
                changed_paths=(concern.path,),
            )
            self.assertEqual(admitted.comments[0].path, concern.path)
            self.assertEqual(admitted.comments[0].category, concern.category)
            self.assertTrue(admitted.comments[0].needs_human)
            self.assertFalse(admitted.comments[0].effective_blocking)


if __name__ == "__main__":
    unittest.main()
