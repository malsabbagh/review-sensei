import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
    evaluate_approval_facts,
    evaluate_auto_approval,
)

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.review_verdict import bind_review_verdict, project_review_comments
from review_sensei.schemas import validate_public_document
from review_sensei.verifier import prepare_publishable_review


class ReviewVerdictTests(unittest.TestCase):
    def setUp(self):
        self.policy = ReviewConvergencePolicy(quality="high-signal")
        self.blocker = ReviewComment(
            path="a.py",
            line=1,
            body="Required fix",
            blocking=True,
            effective_blocking=True,
        )
        self.human = ReviewComment(
            path="b.py",
            line=1,
            body="Consequential decision",
            blocking=False,
            effective_blocking=False,
            needs_human=True,
        )
        self.note = ReviewComment(
            path="c.py",
            line=1,
            body="Optional note",
            blocking=False,
            effective_blocking=False,
        )
        self.result = ReviewResult(
            summary="Complete.",
            comments=(self.blocker, self.human, self.note),
            provider="fixture",
            review_status="complete",
        )

    def test_unselected_requirements_roundtrip_without_candidate_queue(self):
        selected = project_review_comments(
            self.result, (self.note,), policy=self.policy
        )
        self.assertEqual(selected.comments, (self.note,))
        self.assertEqual(selected.review_status, "complete")
        document = selected.to_dict()
        validate_public_document(document, "review-result")
        verdict = document["review_verdict"]
        self.assertTrue(verdict["unselected_blocking_defect"])
        self.assertTrue(verdict["unselected_human_decision"])
        self.assertNotIn("Required fix", json.dumps(document))
        self.assertNotIn("Consequential decision", json.dumps(document))
        self.assertEqual(
            set(verdict),
            {
                "schema_version",
                "policy_digest",
                "selected_comments_sha256",
                "selected_admission",
                "unselected_blocking_defect",
                "unselected_human_decision",
            },
        )
        restored = ReviewResult.from_dict(document)
        self.assertEqual(
            json.dumps(restored.to_dict(), sort_keys=True),
            json.dumps(document, sort_keys=True),
        )
        self.assertEqual(restored.content_digest(), selected.content_digest())
        eligibility = approval_eligibility_from_result(
            restored,
            head_sha="b" * 40,
            base_sha="a" * 40,
            enabled=True,
            app_authored=False,
        )
        self.assertIsNone(eligibility.human_review)
        cold = ReviewApprovalEligibility.from_dict(eligibility.to_dict())
        decision = evaluate_approval_facts(
            replace(cold.facts, has_open_review_threads=False)
        )
        self.assertFalse(decision.approved)
        self.assertIn("review-verdict-blocking", decision.blockers)
        self.assertIn("review-verdict-human", decision.blockers)
        self.assertNotIn("review-partial", decision.blockers)

    def test_selected_resolutions_and_fresh_clean_review_can_converge(self):
        selected = project_review_comments(
            self.result, self.result.comments, policy=self.policy
        )
        facts = approval_eligibility_from_result(
            selected,
            head_sha="b" * 40,
            base_sha="a" * 40,
            enabled=True,
            app_authored=False,
        ).facts
        settled = replace(
            facts,
            has_blocking_findings=False,
            has_human_adjudication_findings=False,
            has_open_review_threads=False,
        )
        self.assertTrue(evaluate_approval_facts(settled).approved)
        held = project_review_comments(self.result, (), policy=self.policy)
        reprojected = project_review_comments(held, (), policy=self.policy)
        self.assertEqual(reprojected.review_verdict, held.review_verdict)
        self.assertFalse(
            evaluate_auto_approval(
                result=held,
                enabled=True,
                app_authored=False,
                has_open_review_threads=False,
            ).approved
        )
        clean = bind_review_verdict(
            replace(self.result, comments=()), policy=self.policy
        )
        self.assertTrue(
            evaluate_auto_approval(
                result=clean,
                enabled=True,
                app_authored=False,
                has_open_review_threads=False,
            ).approved
        )

    def test_projection_requires_trusted_admission_and_unchanged_policy(self):
        with self.assertRaises(ReviewInputError):
            project_review_comments(
                replace(
                    self.result,
                    comments=(replace(self.blocker, effective_blocking=None),),
                ),
                (),
                policy=self.policy,
            )
        with self.assertRaises(ReviewInputError):
            project_review_comments(
                self.result, (replace(self.note, body="invented"),), policy=self.policy
            )
        held = project_review_comments(self.result, (), policy=self.policy)
        with self.assertRaises(ReviewInputError):
            bind_review_verdict(held, policy=ReviewConvergencePolicy())
        default = bind_review_verdict(self.result, policy=ReviewConvergencePolicy())
        self.assertEqual(default.to_dict(), self.result.to_dict())
        self.assertNotIn("review_verdict", default.to_dict())

    def test_selection_binding_and_prior_lifecycle_are_preserved(self):
        from review_sensei.context import finding_lifecycle_for_comment
        from review_sensei.models import FindingLifecycleRecord

        bound = bind_review_verdict(self.result, policy=self.policy)
        with self.assertRaises(ReviewInputError):
            replace(bound, comments=())
        fingerprint = finding_lifecycle_for_comment(self.blocker).fingerprint
        prior = replace(
            self.result,
            finding_lifecycles=(FindingLifecycleRecord(fingerprint, "still-present"),),
        )
        with self.assertRaises(ReviewInputError):
            project_review_comments(prior, (), policy=self.policy)
        fresh = replace(
            self.result,
            finding_lifecycles=(FindingLifecycleRecord(fingerprint, "new"),),
        )
        self.assertEqual(
            project_review_comments(fresh, (), policy=self.policy).finding_lifecycles,
            (),
        )

    def test_verification_transformation_preserves_unselected_gates(self):
        held = project_review_comments(self.result, (), policy=self.policy)
        restored = ReviewResult.from_dict(held.to_dict())
        prepared = prepare_publishable_review(restored, convergence_policy=self.policy)
        self.assertEqual(prepared.result.review_verdict, held.review_verdict)
        self.assertFalse(
            evaluate_auto_approval(
                result=prepared.result,
                enabled=True,
                app_authored=False,
                has_open_review_threads=False,
            ).approved
        )
        tampered = held.to_dict()
        tampered["review_verdict"]["unselected_blocking_defect"] = False
        self.assertNotEqual(
            ReviewResult.from_dict(tampered).content_digest(), held.content_digest()
        )
        malformed = held.to_dict()
        malformed["review_verdict"]["unselected_human_decision"] = 1
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(malformed)

    def test_preparation_rejects_verdict_from_a_different_policy(self):
        held = project_review_comments(self.result, (), policy=self.policy)
        for policy in (
            ReviewConvergencePolicy(),
            ReviewConvergencePolicy(quality="high-signal", mode="legacy"),
        ):
            with self.subTest(policy=policy), self.assertRaises(ReviewInputError):
                prepare_publishable_review(held, convergence_policy=policy)

    def test_unselected_human_check_explains_qualifying_review_without_listed_findings(
        self,
    ):
        from review_sensei_github.checks import review_check_outcome

        held = project_review_comments(
            replace(self.result, comments=(self.human,)), (), policy=self.policy
        )
        outcome = review_check_outcome(
            policy="auto-approve",
            review_status="complete",
            required_fixes=False,
            required_decisions=False,
            review_verdict=held.review_verdict,
        )
        self.assertEqual(outcome.conclusion, "action_required")
        self.assertIn("qualifying review", outcome.summary.lower())
        self.assertNotIn("listed", outcome.summary)
        mixed = review_check_outcome(
            policy="auto-approve",
            review_status="complete",
            required_fixes=True,
            required_decisions=False,
            review_verdict=held.review_verdict,
        )
        self.assertIn("review comments", mixed.summary)
        self.assertIn("qualifying review", mixed.summary.lower())

    def test_replay_cannot_change_enforcement_or_selected_admission_cardinality(self):
        from review_sensei.convergence import admit_review_result

        policy = ReviewConvergencePolicy(
            mode="legacy", enforcement="publication", quality="high-signal"
        )
        bound = bind_review_verdict(self.result, policy=policy)
        with self.assertRaises(ReviewInputError):
            admit_review_result(bound, replace(policy, enforcement="display-only"))
        document = bound.to_dict()
        document["review_verdict"]["selected_admission"] = ""
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(document)

    def test_provider_and_selected_comment_order_remain_in_result_identity(self):
        import hashlib

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.review_verdict import comment_selection_digest

        note = replace(self.note, body="ملاحظة λ", effective_blocking=False)
        comments = (self.human, note)
        self.assertEqual(
            comment_selection_digest(comments),
            hashlib.sha256(
                canonical_bytes([c.to_dict() for c in comments])
            ).hexdigest(),
        )
        bound = bind_review_verdict(
            replace(self.result, comments=comments), policy=self.policy
        )
        self.assertNotEqual(
            replace(bound, provider="other").content_digest(), bound.content_digest()
        )
        reordered = bound.to_dict()
        reordered["comments"].reverse()
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(reordered)

    def test_matched_downgrade_stays_uncertain_during_full_high_signal_reconciliation(
        self,
    ):
        from test_verification_baseline import _key

        from review_sensei.baseline import (
            baseline_from_review,
            reconcile_overflow_review,
        )
        from review_sensei.context import finding_lifecycle_for_comment
        from review_sensei.models import FindingLifecycleRecord

        initial = replace(self.result, comments=(self.blocker,))
        baseline = baseline_from_review(initial, cache_key=_key(), policy=self.policy)
        fingerprint = finding_lifecycle_for_comment(self.blocker).fingerprint
        downgraded = replace(
            initial,
            comments=(replace(self.blocker, blocking=False, effective_blocking=False),),
            finding_lifecycles=(FindingLifecycleRecord(fingerprint, "new"),),
        )
        reconciled = reconcile_overflow_review(
            downgraded, baseline, coverage_mode="full", retain_matched_obligations=True
        )
        self.assertEqual(reconciled.review_status, "partial")
        self.assertEqual(
            reconciled.finding_lifecycles,
            (FindingLifecycleRecord(fingerprint, "uncertain"),),
        )

    def test_broader_review_cannot_clear_retained_unselected_requirement(self):
        previous = project_review_comments(
            self.result, (self.human,), policy=self.policy
        )
        retained = approval_eligibility_from_result(
            previous,
            head_sha="b" * 40,
            base_sha="a" * 40,
            enabled=True,
            app_authored=False,
        )
        clean = bind_review_verdict(
            replace(self.result, comments=()), policy=self.policy
        )
        combined = approval_eligibility_from_result(
            clean,
            head_sha="b" * 40,
            base_sha="a" * 40,
            enabled=True,
            app_authored=False,
            retained_eligibility=retained,
        )
        self.assertTrue(combined.facts.review_verdict.unselected_blocking_defect)

    def test_explicit_override_only_clears_the_selected_inventory(self):
        from review_sensei_github.checks import CheckPublicationResult
        from review_sensei_github.publication import (
            ReviewApprovalFinalizer,
            approval_eligibility_marker,
        )
        from test_thread_resolution_recovery import HEAD, ResolutionHost

        from review_sensei.disposition import FindingDisposition
        from review_sensei.review_verdict import ReviewVerdict, comment_selection_digest

        for unselected in (False, True):
            with self.subTest(unselected=unselected):
                host = ResolutionHost()
                verdict = ReviewVerdict(
                    self.policy.digest(),
                    comment_selection_digest(host.result.comments),
                    unselected_blocking_defect=unselected,
                    selected_admission="B",
                )
                host.eligibility = replace(
                    host.eligibility,
                    facts=replace(host.eligibility.facts, review_verdict=verdict),
                )
                host.reviews[0]["body"] = (
                    approval_eligibility_marker(host.eligibility) + "\n" + host.index
                )
                disposition = FindingDisposition(
                    fingerprint=host.instance,
                    action="accept-risk",
                    reason="acceptable risk",
                    actor="alice",
                    head_sha=HEAD,
                )
                with patch(
                    "review_sensei_github.publication.ReviewCheckPublisher.complete",
                    return_value=CheckPublicationResult("updated", check_run_id=7),
                ) as check:
                    outcome = ReviewApprovalFinalizer(
                        http=host
                    ).refinalize_with_dispositions(
                        token="token",
                        check_token="check",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=2,
                        head_sha=HEAD,
                        app_slug="reviewsensei[bot]",
                        actor="alice",
                        finding_ids=(),
                        dispositions=(disposition,),
                    )
                self.assertEqual(
                    check.call_args.kwargs["outcome"].conclusion,
                    "failure" if unselected else "success",
                )
                self.assertEqual(
                    outcome.status, "approval_withheld" if unselected else "approved"
                )
                self.assertTrue(host.resolved)

    def test_checks_keep_unselected_requirements_after_selected_resolution(self):
        from review_sensei_github.checks import check_outcome_for_result

        for comments, expected in (
            ((self.blocker,), "failure"),
            ((self.human,), "action_required"),
            (self.result.comments, "failure"),
        ):
            with self.subTest(expected=expected, comments=comments):
                result = project_review_comments(
                    replace(self.result, comments=comments), (), policy=self.policy
                )
                for policy in ("blocking", "auto-approve"):
                    outcome = check_outcome_for_result(
                        result,
                        policy=policy,
                        required_fixes=False,
                        required_decisions=False,
                    )
                    self.assertEqual(outcome.conclusion, expected)
                    self.assertNotIn("listed", outcome.summary)
                    self.assertNotIn("Required fix", outcome.summary)
                    self.assertNotIn("Consequential decision", outcome.summary)
                    self.assertNotIn("deferred", outcome.summary)
                self.assertEqual(
                    check_outcome_for_result(result, policy="advisory").conclusion,
                    "neutral",
                )

    def test_no_provider_continuation_cannot_clear_unselected_requirements(self):
        from types import SimpleNamespace

        from review_sensei.cli import _reconcile_hosted_result
        from review_sensei.outcomes import RunOutcome
        from review_sensei.service import ReviewRun, ReviewService

        prior = project_review_comments(self.result, (), policy=self.policy)
        run = ReviewRun(
            result=replace(self.result, comments=()),
            outcome=RunOutcome("reviewed", provider_calls=0),
        )
        resumed = _reconcile_hosted_result(
            ReviewService,
            admission_baseline=None,
            verification_scope=None,
            convergence_policy=self.policy,
            prior_partial=prior,
        )(run)
        self.assertTrue(resumed.result.review_verdict.unselected_blocking_defect)
        self.assertTrue(resumed.result.review_verdict.unselected_human_decision)
        full = _reconcile_hosted_result(
            ReviewService,
            admission_baseline=None,
            verification_scope=SimpleNamespace(coverage_mode="full"),
            convergence_policy=self.policy,
            prior_partial=prior,
        )(replace(run, outcome=RunOutcome("reviewed", provider_calls=1)))
        self.assertEqual(full.result.review_verdict, resumed.result.review_verdict)
        self.assertFalse(
            evaluate_auto_approval(
                result=resumed.result,
                enabled=True,
                app_authored=False,
                has_open_review_threads=False,
            ).approved
        )

    def test_selected_admission_survives_cold_preparation_and_tamper_is_bound(self):
        comments = (replace(self.blocker, blocking=False), self.human, self.note)
        selected = project_review_comments(
            replace(self.result, comments=comments), comments, policy=self.policy
        )
        cold = ReviewResult.from_dict(selected.to_dict())
        self.assertEqual(cold.comments, selected.comments)
        prepared = prepare_publishable_review(
            cold, convergence_policy=self.policy
        ).result
        self.assertEqual(prepared.comments, selected.comments)
        self.assertEqual(prepared.content_digest(), selected.content_digest())
        decision = evaluate_auto_approval(
            result=prepared,
            enabled=True,
            app_authored=False,
            has_open_review_threads=False,
        )
        self.assertFalse(decision.approved)
        self.assertIn("blocking-findings-open", decision.blockers)
        self.assertIn("human-adjudication-open", decision.blockers)
        with self.assertRaises(ReviewInputError):
            replace(
                selected,
                comments=(
                    replace(comments[0], effective_blocking=False),
                    *comments[1:],
                ),
            )

    def test_confirmed_reverification_preserves_selected_required_admission(self):
        from test_publication_verifier import candidate, snapshot_digest

        from review_sensei.verifier import _candidate_to_comment

        snapshot = {"src/app.py": "keep\nreturn total / len(values)\n"}
        item = candidate(snapshot_sha=snapshot_digest(snapshot))
        original = replace(
            _candidate_to_comment(item), effective_blocking=False, needs_human=True
        )
        result = project_review_comments(
            replace(self.result, comments=(original,)), (original,), policy=self.policy
        )
        prepared = prepare_publishable_review(
            ReviewResult.from_dict(result.to_dict()),
            candidates=(item,),
            snapshot=snapshot,
            snapshot_sha256=snapshot_digest(snapshot),
            evidence_policy="confirmed",
            convergence_policy=self.policy,
        )
        self.assertEqual(prepared.result.comments[0], original)
        self.assertEqual(prepared.result.review_verdict.selected_admission, "H")
        with self.assertRaises(ReviewInputError):
            prepare_publishable_review(
                result,
                candidates=(item,),
                snapshot=snapshot,
                snapshot_sha256=snapshot_digest(snapshot),
                evidence_policy="confirmed",
                convergence_policy=self.policy,
                changed_lines={"src/app.py": frozenset({99})},
            )

    def test_full_discovery_rediscovery_cannot_relabel_prior_obligation_new(self):
        from test_verification_baseline import _key

        from review_sensei.baseline import baseline_from_review
        from review_sensei.cli import _reconcile_hosted_result
        from review_sensei.context import finding_lifecycle_for_comment
        from review_sensei.models import FindingLifecycleRecord
        from review_sensei.outcomes import RunOutcome
        from review_sensei.service import ReviewRun, ReviewService

        initial = replace(self.result, comments=(self.blocker,))
        baseline = baseline_from_review(initial, cache_key=_key(), policy=self.policy)
        fingerprint = finding_lifecycle_for_comment(self.blocker).fingerprint
        fresh = replace(
            initial, finding_lifecycles=(FindingLifecycleRecord(fingerprint, "new"),)
        )
        run = ReviewRun(result=fresh, outcome=RunOutcome("reviewed", provider_calls=1))
        reconciled = _reconcile_hosted_result(
            ReviewService,
            admission_baseline=baseline,
            verification_scope=None,
            convergence_policy=self.policy,
            current_key=_key(head_sha="c" * 40),
            changed_paths=("a.py",),
        )(run).result
        self.assertEqual(reconciled.finding_lifecycles[0].state, "still-present")
        with self.assertRaisesRegex(ReviewInputError, "retained obligation"):
            project_review_comments(reconciled, (), policy=self.policy)

    def test_full_baseline_reconciliation_preserves_default_quality_behavior(self):
        from types import SimpleNamespace

        from test_verification_baseline import _key

        from review_sensei.baseline import baseline_from_review
        from review_sensei.cli import _reconcile_hosted_result
        from review_sensei.outcomes import RunOutcome
        from review_sensei.service import ReviewRun, ReviewService

        initial = replace(self.result, comments=(self.blocker,))
        clean = replace(initial, comments=())
        for quality in ("current", "balanced", "high-signal"):
            policy = ReviewConvergencePolicy(quality=quality)
            baseline = baseline_from_review(initial, cache_key=_key(), policy=policy)
            run = ReviewRun(
                result=clean, outcome=RunOutcome("reviewed", provider_calls=1)
            )
            with self.subTest(quality=quality):
                reconciled = _reconcile_hosted_result(
                    ReviewService,
                    admission_baseline=baseline,
                    verification_scope=SimpleNamespace(coverage_mode="full"),
                    convergence_policy=policy,
                    current_key=_key(head_sha="c" * 40),
                )(run).result
                if quality == "high-signal":
                    self.assertEqual(reconciled.review_status, "partial")
                    self.assertEqual(
                        reconciled.finding_lifecycles[0].state, "uncertain"
                    )
                else:
                    self.assertEqual(reconciled.to_dict(), clean.to_dict())

    def test_reverification_preserves_new_and_previous_lifecycle_obligations(self):
        from review_sensei.context import finding_lifecycle_for_comment
        from review_sensei.models import FindingLifecycleRecord
        from review_sensei.review_verdict import preserve_verified_verdict

        old_record = FindingLifecycleRecord(
            finding_lifecycle_for_comment(self.note).fingerprint, "still-present"
        )
        new_record = FindingLifecycleRecord(
            finding_lifecycle_for_comment(self.blocker).fingerprint, "uncertain"
        )
        previous = bind_review_verdict(
            replace(
                self.result, comments=(self.note,), finding_lifecycles=(old_record,)
            ),
            policy=self.policy,
        )
        verified = bind_review_verdict(
            replace(
                self.result,
                comments=(self.note, self.blocker),
                finding_lifecycles=(replace(old_record, state="new"), new_record),
            ),
            policy=self.policy,
        )
        preserved = preserve_verified_verdict(previous, verified, policy=self.policy)
        self.assertEqual(preserved.finding_lifecycles, (old_record, new_record))

    def test_schema5_carries_versioned_verdict_and_rejects_stripping(self):
        from pathlib import Path

        from review_sensei_github.approval import ApprovalFacts

        path = Path(__file__).parent / "fixtures/schemas/golden/eligibility-v5.json"
        document = json.loads(path.read_text())
        result = project_review_comments(self.result, (), policy=self.policy)
        document["facts"].update(
            schema_version="3",
            provider="fixture",
            review_verdict=result.review_verdict.to_dict(),
        )
        validate_public_document(document, "eligibility-v5")
        self.assertEqual(
            ApprovalFacts.from_dict(document["facts"]).review_verdict,
            result.review_verdict,
        )
        del document["facts"]["review_verdict"]
        with self.assertRaises(ReviewInputError):
            validate_public_document(document, "eligibility-v5")

    def test_sealed_result_rejects_removed_or_changed_verdict(self):
        held = project_review_comments(
            replace(
                self.result,
                comments=(*self.result.comments, replace(self.human, path="extra.py")),
            ),
            (self.human,),
            policy=self.policy,
        )
        transaction = ReviewTransaction.create(
            repository="owner/repo",
            pull_request=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest=self.policy.digest(),
            configuration_digest="c" * 64,
            evidence_digest="d" * 64,
            reservation_id="e" * 64,
            generation=1,
        ).with_result(held.content_digest())
        sealed = replace(held, transaction=transaction)
        self.assertEqual(
            ReviewResult.from_dict(sealed.to_dict()).content_digest(),
            held.content_digest(),
        )
        for field in (
            "unselected_blocking_defect",
            "unselected_human_decision",
            "selected_admission",
            "remove",
        ):
            document = sealed.to_dict()
            if field == "remove":
                document.pop("review_verdict")
            elif field == "selected_admission":
                document["review_verdict"][field] = "A"
            else:
                document["review_verdict"][field] = False
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                ReviewResult.from_dict(document)

    def test_accepted_operation_replays_frozen_verdict_without_reselection(self):
        from types import SimpleNamespace

        from review_sensei_github.operation_comments import ResultPartStore
        from review_sensei_github.operation_entry import (
            analysis_payload,
            run_hosted_provider,
        )
        from review_sensei_github.operation_host import GitHubCommentOperationStore
        from test_operation_entry import MemoryComments, _hex

        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        parts = ResultPartStore(comments, app_user_id=42)
        calls = []

        selected = (replace(self.blocker, blocking=False), self.human)
        source = replace(
            self.result,
            comments=(
                *selected,
                self.note,
                replace(self.blocker, path="unselected.py"),
                replace(self.human, path="unselected-human.py"),
            ),
        )

        def execute():
            calls.append(True)
            return SimpleNamespace(
                result=project_review_comments(source, selected, policy=self.policy)
            )

        kwargs = dict(
            trigger="full-review",
            repository_id=7,
            pull_request=11,
            attestation_digest=_hex("attestation"),
            run_id="100",
            run_attempt="1",
            app_id=99,
            reservation_id="reservation-1",
            delivery_id="delivery-1",
            grant="grant-1",
            server_started_ms=1_000,
            execute=execute,
            encode=analysis_payload,
            parts=parts,
            now=lambda: 1_000,
        )
        first = run_hosted_provider(store, **kwargs)
        second = run_hosted_provider(store, **kwargs)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(second.fresh)
        restored = ReviewResult.from_dict(json.loads(second.replay_payload))
        self.assertEqual(restored.content_digest(), first.fresh.result.content_digest())
        self.assertTrue(restored.review_verdict.unselected_blocking_defect)
        self.assertTrue(restored.review_verdict.unselected_human_decision)
        self.assertEqual(restored.comments, selected)
        self.assertEqual(
            prepare_publishable_review(
                restored, convergence_policy=self.policy
            ).result.comments,
            selected,
        )


if __name__ == "__main__":
    unittest.main()
