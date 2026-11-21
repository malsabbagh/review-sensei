from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from review_sensei_github import (
    ConversationPublisher,
    GitHubApplication,
    GitHubHttp,
    GitHubWriteOptions,
    LearningPRPublisher,
    ReviewPublisher,
)
from review_sensei_github.application import _publication_round_flags
from review_sensei_github.approval import (
    ApprovalFacts,
    approval_facts_from_result,
    evaluate_approval_facts,
    evaluate_auto_approval,
    has_uncertain_findings,
)
from review_sensei_github.checks import check_outcome_for_result, review_check_outcome
from review_sensei_github.publication import (
    CHANGES_REQUIRED_STATE,
    NO_REQUIRED_FIXES_STATE,
    WITHHELD_STATE,
    _approval_eligible,
    _review_summary_state,
    approval_eligibility_from_body,
)

from review_sensei import ReviewCategory, ReviewService, Stage
from review_sensei.active_finding_window import (
    AuthoritativeDecisionInventory,
    AuthoritativeObligation,
    evaluate_active_window_round,
)
from review_sensei.baseline import (
    BaselineFinding,
    ReviewBaseline,
    baseline_finding_from_comment,
    baseline_from_history_document,
    baseline_from_review,
    baseline_history_document,
    reconcile_overflow_review,
)
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.cli import _reconcile_hosted_result
from review_sensei.context import (
    ReviewContextCacheKey,
    build_review_context_selection,
    stage_applies_to_paths,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.finding_drift import (
    DriftInventory,
    DriftTrackedObligation,
    OriginalFindingCriterion,
    PresentLocationAnchor,
    evaluate_drift_round,
)
from review_sensei.models import (
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
    ReviewTransaction,
)
from review_sensei.outcomes import RunOutcome
from review_sensei.schemas import validate_public_document
from review_sensei.service import ReviewRun
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    complete_review_publication,
    load_review_transaction_for_publication,
    prepare_review_transaction,
)
from review_sensei.verifier import prepare_publishable_review

SRC_DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,4 @@
 def process(x):
-    return x
+    if x is None:
+        raise ValueError("missing")
+    return x + 1
"""

SRC_AND_DOCS_DIFF = (
    SRC_DIFF
    + """diff --git a/docs/readme.md b/docs/readme.md
--- a/docs/readme.md
+++ b/docs/readme.md
@@ -1 +1,2 @@
 docs
+more docs
"""
)

BASE_SHA = "a" * 40
HEAD_1 = "b" * 40
HEAD_2 = "c" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _cache_key(*, head_sha: str = HEAD_1) -> ReviewContextCacheKey:
    return ReviewContextCacheKey(
        repository="owner/repo",
        pull_request=1,
        base_sha=BASE_SHA,
        head_sha=head_sha,
        engine="fake",
        model="fake-model",
        profile="default",
        stage_digest=_sha("stage"),
        context_digest=_sha("ctx"),
        learning_digest=_sha("learn"),
    )


def _complete_coverage(*paths: str) -> CoverageManifest:
    target_paths = paths or ("src/app.py",)
    return CoverageManifest(
        files=tuple(FileCoverage(path=p, outcome="reviewed") for p in target_paths),
        hunks=(),
        enumeration_complete=True,
        enumerated_paths=target_paths,
    )


class _CleanProvider:
    name = "fake"
    model = "fake-model"

    def __init__(
        self, payload: str = '{"summary":"Looks good.","comments":[]}'
    ) -> None:
        self.payload = payload
        self.requests: list[ReviewRequest] = []

    def complete(self, request: ReviewRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(text=self.payload, provider=self.name, model=self.model)


class _FakeBroker:
    def request_oidc_token(self) -> str:
        return "oidc-test"

    def exchange(self, _oidc: str, *, capability: str) -> str:
        return f"tok-{capability}"


class _MinimalDriftHost:
    def __init__(self, *, head_sha: str = HEAD_1) -> None:
        self.head_sha = head_sha
        self.reviews: list[dict[str, object]] = []
        self.threads: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []
        self.issue_comments: list[dict[str, object]] = []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self._open)

    def _resp(self, payload: object, status: int = 200):
        from tests.fake_github_http import json_response

        return json_response(payload, status)

    def _open(self, request, timeout=None):
        path = urlparse(request.full_url).path
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        if request.method == "GET":
            if path.endswith("/pulls/1"):
                return self._resp(
                    {
                        "state": "open",
                        "draft": False,
                        "changed_files": 2,
                        "user": {"login": "dev-author", "type": "User"},
                        "head": {
                            "sha": self.head_sha,
                            "ref": "feature",
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": BASE_SHA,
                            "ref": "main",
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path.endswith("/pulls/1/reviews"):
                return self._resp(list(self.reviews))
            if "/reviews/" in path and path.endswith("/comments"):
                rid = int(path.split("/reviews/")[1].split("/")[0])
                matched = [
                    c
                    for t in self.threads
                    for c in t["comments"]["nodes"]  # type: ignore[index]
                    if c.get("pull_request_review_id") == rid
                ]
                return self._resp(matched)
            if "/reviews/" in path and not path.endswith("/comments"):
                rid = int(path.rsplit("/", 1)[-1])
                return self._resp(next((r for r in self.reviews if r["id"] == rid), {}))
            if path.endswith("/pulls/1/comments"):
                return self._resp([])
            if path.endswith("/issues/1/comments"):
                return self._resp(list(self.issue_comments))
            if path.endswith("/check-runs"):
                return self._resp(
                    {
                        "check_runs": [
                            r
                            for r in self.check_runs
                            if r.get("head_sha") == self.head_sha
                        ]
                    }
                )
            if "/rules/branches/" in path:
                return self._resp([])
            if path.endswith("/protection"):
                return self._resp({"message": "Not Found"}, 404)
            if path == "/user" or path.startswith("/users/"):
                return self._resp(
                    {"id": 9001, "login": "reviewsensei[bot]", "type": "Bot"}
                )
            if "/collaborators/" in path and path.endswith("/permission"):
                return self._resp({"permission": "maintain"})
        if request.method == "POST":
            if path.endswith("/pulls/1/reviews"):
                assert isinstance(body, dict)
                rid = 100 + len(self.reviews)
                state_map = {
                    "APPROVE": "APPROVED",
                    "REQUEST_CHANGES": "CHANGES_REQUESTED",
                    "COMMENT": "COMMENTED",
                }
                rec: dict[str, object] = {
                    "id": rid,
                    "commit_id": body["commit_id"],
                    "state": state_map.get(
                        str(body.get("event", "COMMENT")), "COMMENTED"
                    ),
                    "body": body.get("body", ""),
                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                    "user": {"id": 9001, "login": "reviewsensei[bot]", "type": "Bot"},
                }
                self.reviews.append(rec)
                for idx, comment in enumerate(body.get("comments") or ()):
                    self.threads.append(
                        {
                            "id": f"PRRT_{rid}_{idx}",
                            "isResolved": False,
                            "path": comment["path"],
                            "line": comment["line"],
                            "comments": {
                                "nodes": [
                                    {
                                        "id": f"PRRC_{rid}_{idx}",
                                        "databaseId": rid * 100 + idx,
                                        "pull_request_review_id": rid,
                                        "pullRequestReview": {"databaseId": rid},
                                        "commit": {"oid": body["commit_id"]},
                                        "body": comment["body"],
                                        "path": comment["path"],
                                        "line": comment["line"],
                                        "author": {
                                            "login": "reviewsensei[bot]",
                                            "__typename": "Bot",
                                            "databaseId": 9001,
                                        },
                                    }
                                ]
                            },
                        }
                    )
                return self._resp(rec, 200)
            if path.endswith("/check-runs"):
                assert isinstance(body, dict)
                run_obj: dict[str, object] = {
                    "id": 500 + len(self.check_runs),
                    "name": body["name"],
                    "head_sha": body["head_sha"],
                    "status": body.get("status"),
                    "conclusion": body.get("conclusion"),
                    "output": body.get("output", {}),
                    "app": {"slug": "reviewsensei"},
                }
                self.check_runs.append(run_obj)
                return self._resp(run_obj, 201)
            if path.endswith("/issues/1/comments"):
                assert isinstance(body, dict)
                cid = 800 + len(self.issue_comments)
                item: dict[str, object] = {
                    "id": cid,
                    "body": body["body"],
                    "user": {"id": 9001, "login": "reviewsensei[bot]", "type": "Bot"},
                }
                self.issue_comments.append(item)
                return self._resp(item, 201)
            if path == "/graphql":
                assert isinstance(body, dict)
                query = str(body.get("query", ""))
                variables = body.get("variables") or {}
                if "resolveReviewThread" in query:
                    tid = (variables.get("input") or {}).get(
                        "threadId"
                    ) or variables.get("threadId")
                    for thread in self.threads:
                        if thread["id"] == tid:
                            reopening = "unresolveReviewThread" in query
                            thread["isResolved"] = not reopening
                            return self._resp(
                                {
                                    "data": {
                                        (
                                            "unresolveReviewThread"
                                            if reopening
                                            else "resolveReviewThread"
                                        ): {
                                            "thread": {
                                                "id": tid,
                                                "isResolved": not reopening,
                                            }
                                        }
                                    }
                                }
                            )
                return self._resp(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": list(self.threads),
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
        if request.method == "PUT" and "/reviews/" in path:
            assert isinstance(body, dict)
            rid = int(path.rsplit("/", 1)[-1])
            for rev in self.reviews:
                if rev["id"] == rid:
                    rev["body"] = body["body"]
                    return self._resp(rev, 200)
        if request.method == "PATCH" and "/issues/comments/" in path:
            assert isinstance(body, dict)
            cid = int(path.rsplit("/", 1)[-1])
            for item in self.issue_comments:
                if item["id"] == cid:
                    item["body"] = body["body"]
                    return self._resp(item, 200)
        if request.method == "PATCH" and "/check-runs/" in path:
            assert isinstance(body, dict)
            cid = int(path.rsplit("/", 1)[-1])
            for run_obj in self.check_runs:
                if run_obj["id"] == cid:
                    run_obj.update(body)
                    return self._resp(run_obj, 200)
        raise AssertionError(f"Unhandled request: {request.method} {path}")


class FalseApprovalBlocksTests(unittest.TestCase):
    """Regression coverage for false approval blocks (#3 and #5) and reconstruction parity."""

    # ── A1: Inapplicable stages vs suppressed applicable stages ─────────────

    def test_inapplicable_stage_skip_stays_complete_in_both_runners(self) -> None:
        docs_cat = ReviewCategory(
            id="docs",
            title="Documentation",
            focus=("Accuracy",),
            applies_to=("docs/**",),
        )
        code_cat = ReviewCategory(
            id="code",
            title="Code",
            focus=("Correctness",),
            applies_to=("src/**",),
        )
        docs_stage = Stage(
            name="Docs",
            prompt_template="Categories: {review_categories}\n{diff}",
            outputs=("comments",),
            categories=(docs_cat,),
        )
        code_stage = Stage(
            name="Code",
            prompt_template="Categories: {review_categories}\n{diff}",
            outputs=("summary", "comments"),
            categories=(code_cat,),
        )
        self.assertFalse(stage_applies_to_paths(docs_stage, ("src/app.py",)))
        self.assertTrue(stage_applies_to_paths(code_stage, ("src/app.py",)))
        self.assertTrue(
            stage_applies_to_paths(Stage("Summary", "{diff}", ("summary",)), ())
        )

        for work_mode in ("legacy", "unified"):
            with self.subTest(work_mode=work_mode):
                provider = _CleanProvider()
                service = ReviewService(
                    provider,
                    stages=(docs_stage, code_stage),
                    work_budgets=ReviewWorkBudgets(mode=work_mode),
                )
                active_ids = build_review_context_selection(
                    (docs_cat, code_cat),
                    changed_paths=("src/app.py",),
                ).active_category_ids
                self.assertEqual(active_ids, ("code",))
                run = service.run(
                    ReviewRequest(diff=SRC_DIFF, active_category_ids=active_ids)
                )
                self.assertEqual(run.outcome.stage_summary["Docs"], "skipped")
                self.assertEqual(run.outcome.stage_summary["Code"], "complete")
                self.assertEqual(run.outcome.status, "reviewed")
                assert run.result is not None
                self.assertEqual(run.result.review_status, "complete")
                decision = evaluate_auto_approval(
                    enabled=True,
                    app_authored=False,
                    result=run.result,
                    has_open_review_threads=False,
                )
                self.assertTrue(decision.approved, decision.blockers)
                self.assertEqual(
                    check_outcome_for_result(
                        run.result, policy="auto-approve"
                    ).conclusion,
                    "success",
                )

    def test_zero_comment_stage_stays_summary_only_and_suppressed_applicable_stays_partial(
        self,
    ) -> None:
        docs_cat = ReviewCategory(
            id="docs",
            title="Documentation",
            focus=("Accuracy",),
            applies_to=("docs/**",),
        )
        code_cat = ReviewCategory(
            id="code",
            title="Code",
            focus=("Correctness",),
            applies_to=("src/**",),
        )
        docs_only_stages = (
            Stage(
                name="Docs",
                prompt_template="Categories: {review_categories}\n{diff}",
                outputs=("comments",),
                categories=(docs_cat,),
            ),
            Stage(
                name="Summary",
                prompt_template="Summarize: {diff}",
                outputs=("summary",),
            ),
        )
        both_stages = (
            docs_only_stages[0],
            Stage(
                name="Code",
                prompt_template="Categories: {review_categories}\n{diff}",
                outputs=("summary", "comments"),
                categories=(code_cat,),
            ),
        )

        for work_mode in ("legacy", "unified"):
            with self.subTest(work_mode=work_mode):
                summary_run = ReviewService(
                    _CleanProvider('{"summary":"Overview only."}'),
                    stages=docs_only_stages,
                    work_budgets=ReviewWorkBudgets(mode=work_mode),
                ).run(ReviewRequest(diff=SRC_DIFF, active_category_ids=()))
                assert summary_run.result is not None
                self.assertEqual(summary_run.result.review_status, "summary-only")
                assert summary_run.result.coverage is not None
                self.assertTrue(
                    all(
                        entry.outcome == "unsupported"
                        and entry.reason == "no-review-stage"
                        for entry in summary_run.result.coverage.files
                    )
                )
                self.assertFalse(
                    evaluate_auto_approval(
                        enabled=True,
                        app_authored=False,
                        result=summary_run.result,
                        has_open_review_threads=False,
                    ).approved
                )
                self.assertEqual(
                    check_outcome_for_result(
                        summary_run.result, policy="auto-approve"
                    ).conclusion,
                    "action_required",
                )

                suppressed_run = ReviewService(
                    _CleanProvider(),
                    stages=both_stages,
                    work_budgets=ReviewWorkBudgets(mode=work_mode),
                ).run(
                    ReviewRequest(diff=SRC_AND_DOCS_DIFF, active_category_ids=("code",))
                )
                assert suppressed_run.result is not None
                self.assertEqual(suppressed_run.outcome.status, "partial")
                self.assertEqual(suppressed_run.result.review_status, "partial")
                self.assertFalse(
                    evaluate_auto_approval(
                        enabled=True,
                        app_authored=False,
                        result=suppressed_run.result,
                        has_open_review_threads=False,
                    ).approved
                )

    # ── A2: Provably advisory omissions vs unknown / required findings ──────

    def test_provably_advisory_omission_stays_active_unresolved_in_drift_and_window(
        self,
    ) -> None:
        advisory_crit = OriginalFindingCriterion(
            obligation_id=_sha("adv-ob"),
            original_fingerprint=_sha("adv-fp"),
            resolution_criterion=_sha("adv-rc"),
            origin_head_sha=HEAD_1,
            origin_path="src/app.py",
            origin_line=2,
            origin_side="RIGHT",
            origin_symbol="process",
            defect_kind="style-note",
            concern=_sha("adv-concern"),
            blocking=False,
            needs_human=False,
            impact=None,
            evidence="advisory",
        )
        drift_ob = DriftTrackedObligation(
            criterion=advisory_crit,
            present_anchor=PresentLocationAnchor(
                head_sha=HEAD_1,
                path="src/app.py",
                line=2,
                side="RIGHT",
                symbol="process",
                anchor_location="right",
            ),
            lifecycle_state="active-unresolved",
            generation=0,
        )
        clean_r2 = ReviewResult(
            summary="Clean.",
            comments=(),
            provider="fake",
            review_status="complete",
            coverage=_complete_coverage("src/app.py"),
        )
        drift_r2 = evaluate_drift_round(
            clean_r2,
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=2,
            coverage_mode="full",
            prior_inventory=DriftInventory((drift_ob,)),
            diff_text=SRC_DIFF,
            convergence_policy=ReviewConvergencePolicy(),
        )
        self.assertEqual(
            drift_r2.inventory.obligations[0].lifecycle_state, "active-unresolved"
        )
        self.assertFalse(drift_r2.inventory.has_omitted_uncertainty)
        self.assertFalse(drift_r2.inventory.obligations[0].blocks_approval_now)
        self.assertFalse(drift_r2.inventory.obligations[0].effective_blocking_now)
        self.assertTrue(drift_r2.approved)
        self.assertFalse(drift_ob.to_baseline_finding().requires_gate)

        window_ob = AuthoritativeObligation(
            obligation_id=_sha("adv-ob-w"),
            fingerprint=_sha("adv-fp-w"),
            resolution_criterion=_sha("adv-rc-w"),
            path="src/app.py",
            line=2,
            side="RIGHT",
            symbol="process",
            defect_kind="style-note",
            concern=_sha("adv-concern-w"),
            blocking=False,
            needs_human=False,
            impact=None,
            evidence="advisory",
            lifecycle_state="active-unresolved",
            generation=0,
        )
        window_r2 = evaluate_active_window_round(
            clean_r2,
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=2,
            coverage_mode="full",
            prior_inventory=AuthoritativeDecisionInventory((window_ob,)),
        )
        self.assertEqual(
            window_r2.authoritative_decision_inventory.obligations[0].lifecycle_state,
            "active-unresolved",
        )
        self.assertTrue(window_r2.approval_eligibility.approved)
        self.assertFalse(window_ob.to_baseline_finding().requires_gate)

    def test_overflow_omission_advisory_passes_while_unknown_and_required_gate_without_partial_coverage(
        self,
    ) -> None:
        policy = ReviewConvergencePolicy()
        key = _cache_key(head_sha=HEAD_1)
        clean_r2 = ReviewResult(
            summary="Clean.",
            comments=(),
            provider="fake",
            review_status="complete",
            coverage=_complete_coverage("src/app.py"),
        )

        # 1) Provably advisory prior omitted -> approved, complete, not uncertain.
        adv_comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Optional note.",
            blocking=False,
            effective_blocking=False,
            needs_human=False,
            defect_kind="style-note",
        )
        adv_finding = baseline_finding_from_comment(adv_comment)
        self.assertTrue(adv_finding.is_provably_advisory)
        self.assertFalse(adv_finding.requires_gate)
        adv_baseline = ReviewBaseline(
            cache_key=key,
            policy_digest=policy.digest(),
            complete=True,
            findings=(adv_finding,),
            reviewed_paths=("src/app.py",),
            coverage_complete=True,
        )
        adv_reconciled = reconcile_overflow_review(
            clean_r2, adv_baseline, coverage_mode="fallback-full"
        )
        self.assertEqual(adv_reconciled.review_status, "complete")
        self.assertEqual(adv_reconciled.finding_lifecycles, ())
        self.assertFalse(has_uncertain_findings(adv_reconciled))
        self.assertTrue(
            evaluate_auto_approval(
                enabled=True,
                app_authored=False,
                result=adv_reconciled,
                has_open_review_threads=False,
            ).approved
        )
        self.assertEqual(
            check_outcome_for_result(adv_reconciled, policy="auto-approve").conclusion,
            "success",
        )
        self.assertTrue(
            baseline_from_review(
                adv_reconciled, cache_key=_cache_key(head_sha=HEAD_2), policy=policy
            ).complete
        )

        # 2) Unadmitted comment cannot claim needs_human=False.
        unadmitted_comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Unadmitted note.",
            blocking=False,
            effective_blocking=None,
            needs_human=False,
            defect_kind="authz-note",
        )
        self.assertIsNone(baseline_finding_from_comment(unadmitted_comment).needs_human)
        self.assertTrue(baseline_finding_from_comment(unadmitted_comment).requires_gate)

        # 3) Legacy 8-field record (needs_human=None) + explicit needs_human=True + blocking=True:
        legacy_finding = BaselineFinding(
            fingerprint=_sha("legacy-fp"),
            resolution_criterion=_sha("legacy-rc"),
            concern=_sha("legacy-concern"),
            path="src/app.py",
            symbol=None,
            defect_kind="authz-failure",
            generation=0,
            blocking=False,
            needs_human=None,
        )
        legacy_baseline = ReviewBaseline(
            cache_key=key,
            policy_digest=policy.digest(),
            complete=True,
            findings=(legacy_finding,),
            reviewed_paths=("src/app.py",),
            coverage_complete=True,
        )
        legacy_doc = baseline_history_document(legacy_baseline)
        self.assertNotIn("needs_human", legacy_doc["findings"][0])
        validate_public_document(legacy_doc, "baseline-evidence")
        restored_legacy = baseline_from_history_document(legacy_doc)
        self.assertIsNone(restored_legacy.findings[0].needs_human)
        self.assertTrue(restored_legacy.findings[0].requires_gate)

        human_finding = BaselineFinding(
            fingerprint=_sha("human-fp"),
            resolution_criterion=_sha("human-rc"),
            concern=_sha("human-concern"),
            path="src/app.py",
            symbol=None,
            defect_kind="human-check",
            generation=0,
            blocking=False,
            needs_human=True,
        )
        human_baseline = ReviewBaseline(
            cache_key=key,
            policy_digest=policy.digest(),
            complete=True,
            findings=(human_finding,),
            reviewed_paths=("src/app.py",),
            coverage_complete=True,
        )
        human_doc = baseline_history_document(human_baseline)
        self.assertIs(human_doc["findings"][0]["needs_human"], True)
        validate_public_document(human_doc, "baseline-evidence")
        self.assertIs(
            baseline_from_history_document(human_doc).findings[0].needs_human, True
        )

        # Omission OR downgrade on legacy unknown & needs_human=True gates without review_status="partial":
        downgraded_result = replace(clean_r2, comments=(adv_comment,))
        for label, base, cand in (
            ("legacy-omitted", restored_legacy, clean_r2),
            (
                "legacy-downgraded",
                restored_legacy,
                replace(
                    downgraded_result,
                    comments=(
                        replace(
                            adv_comment,
                            defect_kind="authz-failure",
                        ),
                    ),
                ),
            ),
            ("human-omitted", human_baseline, clean_r2),
            (
                "human-downgraded",
                human_baseline,
                replace(
                    downgraded_result,
                    comments=(
                        replace(
                            adv_comment,
                            defect_kind="human-check",
                        ),
                    ),
                ),
            ),
        ):
            with self.subTest(case=label):
                reconciled = reconcile_overflow_review(
                    cand, base, coverage_mode="fallback-full"
                )
                self.assertEqual(reconciled.review_status, "complete")
                self.assertIn(
                    "uncertain", {item.state for item in reconciled.finding_lifecycles}
                )
                self.assertTrue(has_uncertain_findings(reconciled))
                decision = evaluate_auto_approval(
                    enabled=True,
                    app_authored=False,
                    result=reconciled,
                    has_open_review_threads=False,
                )
                self.assertFalse(decision.approved)
                self.assertIn("uncertain-finding-lifecycle", decision.blockers)
                facts = approval_facts_from_result(
                    reconciled, enabled=True, app_authored=False
                )
                self.assertTrue(facts.has_uncertain_findings)
                self.assertIs(facts.to_dict()["has_uncertain_findings"], True)
                self.assertEqual(ApprovalFacts.from_dict(facts.to_dict()), facts)
                direct_decision = evaluate_approval_facts(facts)
                self.assertFalse(direct_decision.approved)
                self.assertIn("uncertain-finding-lifecycle", direct_decision.blockers)
                check = check_outcome_for_result(reconciled, policy="auto-approve")
                self.assertEqual(check.conclusion, "action_required")
                self.assertEqual(check.title, "Review requires attention")
                self.assertEqual(
                    review_check_outcome(
                        policy="auto-approve",
                        review_status="complete",
                        required_fixes=False,
                        has_uncertain_findings=True,
                    ).title,
                    "Review requires attention",
                )
                self.assertFalse(_approval_eligible(reconciled))
                summary_state, _ = _review_summary_state(
                    reconciled,
                    policy=policy,
                    auto_approve=True,
                )
                self.assertEqual(summary_state, WITHHELD_STATE)
                self.assertFalse(
                    _publication_round_flags(reconciled)[
                        "independently_approval_eligible"
                    ]
                )
                self.assertFalse(
                    baseline_from_review(
                        reconciled,
                        cache_key=_cache_key(head_sha=HEAD_2),
                        policy=policy,
                    ).complete
                )

    def test_uncertain_complete_result_does_not_overwrite_baseline_or_advance_session_round(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = LocalSessionLedger(Path(tmpdir) / "ledger")
            identity = SessionIdentity(
                repository="owner/repo", pull_request=1, repository_id=1
            )
            ledger.initialize(identity)
            policy = ReviewConvergencePolicy()
            key1 = _cache_key(head_sha=HEAD_1)
            r1 = ReviewResult(
                summary="Initial review.",
                comments=(
                    ReviewComment(
                        path="src/app.py",
                        line=2,
                        body="Required fix.",
                        blocking=True,
                        effective_blocking=True,
                        needs_human=False,
                        defect_kind="authz",
                    ),
                ),
                provider="fake",
                review_status="complete",
                coverage=_complete_coverage("src/app.py"),
            )
            prep1 = prepare_review_transaction(
                ledger,
                identity,
                policy,
                reservation_id=_sha("res-1"),
                base_sha=BASE_SHA,
                head_sha=HEAD_1,
                configuration_digest=_sha("cfg"),
                evidence_digest=_sha("ev"),
            )
            b1 = baseline_from_review(
                r1,
                cache_key=key1,
                policy=policy,
                generation=prep1.record.generation + 1,
            )
            self.assertTrue(b1.complete)
            r1_chk = checkpoint_review_analysis(
                ledger, identity, prep1, r1, baseline=b1
            )
            assert r1_chk.transaction is not None
            complete_review_publication(
                ledger, identity, r1_chk.transaction, published=True
            )

            # Round 2 omits the blocking finding in overflow reconciliation -> complete coverage,
            # uncertain finding lifecycle -> must NOT advance completed_verification_rounds.
            r2_raw = ReviewResult(
                summary="Clean on R2.",
                comments=(),
                provider="fake",
                review_status="complete",
                coverage=_complete_coverage("src/app.py"),
            )
            r2 = reconcile_overflow_review(r2_raw, b1, coverage_mode="fallback-full")
            self.assertEqual(r2.review_status, "complete")
            prep2 = prepare_review_transaction(
                ledger,
                identity,
                policy,
                reservation_id=_sha("res-2"),
                base_sha=BASE_SHA,
                head_sha=HEAD_2,
                configuration_digest=_sha("cfg"),
                evidence_digest=_sha("ev"),
            )
            b2 = baseline_from_review(
                r2,
                cache_key=_cache_key(head_sha=HEAD_2),
                policy=policy,
                generation=prep2.record.generation + 1,
            )
            self.assertFalse(b2.complete)
            r2_chk = checkpoint_review_analysis(
                ledger, identity, prep2, r2, baseline=None
            )
            after = load_review_transaction_for_publication(
                ledger,
                identity,
                r2_chk,
                base_sha=BASE_SHA,
                head_sha=HEAD_2,
                policy_digest=r2_chk.transaction.policy_digest,  # type: ignore[union-attr]
                configuration_digest=r2_chk.transaction.configuration_digest,  # type: ignore[union-attr]
                evidence_digest=r2_chk.transaction.evidence_digest,  # type: ignore[union-attr]
            )
            self.assertEqual(after.completed_verification_rounds, 0)
            self.assertEqual(after.failed_attempts, 1)
            self.assertEqual(
                after.convergence_history["baseline"], baseline_history_document(b1)
            )

    # ── A3: Fresh vs reconstructed result parity + drift publication symmetry ─

    def test_fresh_and_reconstructed_result_parity_for_current_and_balanced_policies(
        self,
    ) -> None:
        cov = _complete_coverage("src/app.py")
        human_comment = ReviewComment(
            path="src/app.py",
            line=3,
            body="Unverified high-severity authorization contract question.",
            severity="high",
            blocking=True,
            defect_kind="authz-contract",
        )
        advisory_comment = ReviewComment(
            path="src/app.py",
            line=4,
            body="Consider renaming helper.",
            severity="low",
            category="style",
            blocking=False,
            defect_kind="naming",
        )
        raw = ReviewResult(
            summary="Mixed findings.",
            comments=(human_comment, advisory_comment),
            provider="fake",
            review_status="complete",
            coverage=cov,
        )
        for quality in ("current", "balanced"):
            with self.subTest(quality=quality):
                policy = ReviewConvergencePolicy(quality=quality)
                fresh = prepare_publishable_review(
                    raw,
                    convergence_policy=policy,
                    changed_lines={"src/app.py": frozenset({2, 3, 4})},
                ).result
                restored = ReviewResult.from_dict(fresh.to_dict())
                self.assertEqual(restored.comments, fresh.comments)
                self.assertEqual(
                    [(c.effective_blocking, c.needs_human) for c in restored.comments],
                    [(c.effective_blocking, c.needs_human) for c in fresh.comments],
                )
                self.assertEqual(
                    evaluate_auto_approval(
                        enabled=True,
                        app_authored=False,
                        result=restored,
                        has_open_review_threads=False,
                    ),
                    evaluate_auto_approval(
                        enabled=True,
                        app_authored=False,
                        result=fresh,
                        has_open_review_threads=False,
                    ),
                )
                self.assertEqual(
                    check_outcome_for_result(restored, policy="auto-approve"),
                    check_outcome_for_result(fresh, policy="auto-approve"),
                )
                self.assertEqual(
                    baseline_from_review(
                        restored, cache_key=_cache_key(), policy=policy
                    ),
                    baseline_from_review(fresh, cache_key=_cache_key(), policy=policy),
                )

                # Hosted CLI reconciliation also admits & binds default quality policies
                # before checkpointing.
                reconciled = _reconcile_hosted_result(
                    ReviewService,
                    admission_baseline=None,
                    verification_scope=SimpleNamespace(coverage_mode="full"),
                    convergence_policy=policy,
                    changed_lines={"src/app.py": frozenset({2, 3, 4})},
                    changed_paths=("src/app.py",),
                )(
                    ReviewRun(
                        result=raw,
                        outcome=RunOutcome("reviewed", provider_calls=1),
                    )
                ).result
                self.assertIsNotNone(reconciled.review_verdict)
                self.assertEqual(
                    ReviewResult.from_dict(reconciled.to_dict()).comments,
                    reconciled.comments,
                )

    def test_drift_publication_matches_window_preflight_and_round_flags(self) -> None:
        host = _MinimalDriftHost(head_sha=HEAD_1)
        publisher = ReviewPublisher(http=host.http)
        app = GitHubApplication(
            http=host.http,
            broker=_FakeBroker(),  # type: ignore[arg-type]
            reviewer=publisher,
            learner=LearningPRPublisher(http=host.http),
            replier=ConversationPublisher(http=host.http),
            session_ledger=None,
        )
        options = GitHubWriteOptions(
            auto_review=True,
            auto_approve=True,
            github_writes=True,
            operation_entry="disabled",
            max_active_findings=2,
            enable_drift_tracking=True,
        )
        policy = ReviewConvergencePolicy()
        # Round 1: un-inlineable file-level human-adjudicated finding on src/app.py.
        human_comment = ReviewComment(
            path="src/app.py",
            line=None,
            side="FILE",
            body="Confirm file-level caller invariants.",
            severity="high",
            blocking=True,
            defect_kind="caller-contract",
        )
        res1 = ReviewResult(
            summary="Review R1.",
            comments=(human_comment,),
            provider="fake",
            review_status="complete",
            coverage=_complete_coverage("src/app.py"),
        )
        pub1 = app.publish_review(
            options=options,
            oidc_token="oidc-1",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_1,
            base_branch="main",
            base_sha=BASE_SHA,
            result=res1,
            diff=SRC_DIFF,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
        )
        self.assertEqual(pub1.status, "published")
        self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))

        # Round 2 omits the prior finding on an otherwise clean reviewed path ->
        # drift tracks it as omitted-uncertain; drift publication & round flags stay blocked,
        # while a clean drift pass produces NO_REQUIRED_FIXES_STATE.
        host.head_sha = HEAD_2
        clean_r2 = ReviewResult(
            summary="Clean R2.",
            comments=(),
            provider="fake",
            review_status="complete",
            coverage=_complete_coverage("src/app.py"),
        )
        pub2 = app.publish_review(
            options=options,
            oidc_token="oidc-2",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_2,
            base_branch="main",
            base_sha=BASE_SHA,
            result=clean_r2,
            diff=SRC_DIFF,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
        )
        self.assertEqual(pub2.status, "published")
        dstate = app.drift_state(repository_id=1, pull_request=1)
        assert dstate is not None
        self.assertFalse(dstate.approved)
        self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))
        self.assertEqual(host.check_runs[-1]["conclusion"], "failure")

        assembled_clean = publisher._assemble_review_body(
            result=clean_r2,
            analysis=publisher._validate_locations(clean_r2, SRC_DIFF),
            convergence_policy=policy,
            read_facts=lambda: publisher._conversation_resolution_facts(
                token="tok", repository="owner/repo", branch="main"
            ),
            auto_approve=True,
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
        )
        self.assertEqual(assembled_clean.state, NO_REQUIRED_FIXES_STATE)
        adjusted = publisher._adjust_assembled_for_drift(
            assembled_clean,
            dstate,
            convergence_policy=policy,
            auto_approval=False,
        )
        self.assertEqual(adjusted.state, CHANGES_REQUIRED_STATE)
        self.assertTrue(adjusted.requests_changes)

        # Standalone session-backed application check verifies drift_after withholding
        # prevents independently_approval_eligible from claiming True when drift is unapproved.
        head_3 = "d" * 40
        host.head_sha = head_3
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_ctx = {
                "provider": {
                    "name": "fake",
                    "profile": "default",
                    "base_url": None,
                    "timeout_seconds": 30,
                    "max_output_tokens": 1024,
                    "allow_custom_endpoint": False,
                    "openrouter_policy": None,
                },
                "model": "fake-model",
                "stages": [
                    {
                        "name": "review",
                        "outputs": ["summary", "comments"],
                        "categories": [],
                        "provider_profile": None,
                    }
                ],
                "category_policy": [],
                "orchestration": {"enabled": False},
                "publication_mode": "merge-focused",
            }
            ev_ctx = {"evidence_policy": "legacy", "snapshot_sha256": None}
            ledger = LocalSessionLedger(Path(tmpdir) / "ledger")
            identity = SessionIdentity(
                repository="owner/repo", pull_request=1, repository_id=1
            )
            ledger.initialize(identity)
            ledger_app = GitHubApplication(
                http=host.http,
                broker=_FakeBroker(),  # type: ignore[arg-type]
                reviewer=publisher,
                learner=LearningPRPublisher(http=host.http),
                replier=ConversationPublisher(http=host.http),
                session_ledger=ledger,
            )
            prep = prepare_review_transaction(
                ledger,
                identity,
                policy,
                reservation_id=_sha("res-drift-check"),
                base_sha=BASE_SHA,
                head_sha=head_3,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    cfg_ctx
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(ev_ctx),
            )
            clean_b = baseline_from_review(
                clean_r2,
                cache_key=_cache_key(head_sha=head_3),
                policy=policy,
                generation=prep.record.generation + 1,
            )
            chk = checkpoint_review_analysis(
                ledger, identity, prep, clean_r2, baseline=clean_b
            )
            pub_session = ledger_app.publish_review(
                options=options,
                oidc_token="oidc-3",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=head_3,
                base_branch="main",
                base_sha=BASE_SHA,
                result=chk,
                diff=SRC_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                configuration_context=cfg_ctx,
            )
            self.assertEqual(pub_session.status, "published")
            self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))
            dstate_after = publisher.drift_state(repository_id=1, pull_request=1)
            assert dstate_after is not None
            self.assertFalse(dstate_after.approved)

    def test_ambiguous_advisory_obligation_stays_held_when_omitted_adr0134(
        self,
    ) -> None:
        adv_comment = ReviewComment(
            path="src/app.py",
            line=4,
            body="Optional rename suggestion.",
            severity="low",
            category="style",
            blocking=False,
            effective_blocking=False,
            needs_human=False,
            defect_kind="naming",
        )
        clean_r2 = ReviewResult(
            summary="Clean R2.",
            comments=(),
            provider="fake",
            review_status="complete",
            coverage=_complete_coverage("src/app.py"),
        )
        non_adv_comment = ReviewComment(
            path="src/app.py",
            line=3,
            body="Required security check.",
            severity="high",
            blocking=True,
            effective_blocking=True,
            needs_human=False,
            defect_kind="authz-check",
        )
        d1 = evaluate_drift_round(
            ReviewResult(
                summary="R1",
                comments=(non_adv_comment,),
                provider="fake",
                review_status="complete",
                coverage=_complete_coverage("src/app.py"),
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=1,
            coverage_mode="full",
            diff_text=SRC_DIFF,
        )
        ambiguous_drift = DriftInventory(
            obligations=(
                replace(
                    d1.inventory.obligations[0],
                    lifecycle_state="ambiguous-multi-target",
                ),
            )
        )
        d2 = evaluate_drift_round(
            clean_r2,
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=2,
            coverage_mode="fallback-full",
            prior_inventory=ambiguous_drift,
            diff_text=SRC_DIFF,
        )
        self.assertEqual(
            d2.inventory.obligations[0].lifecycle_state, "ambiguous-multi-target"
        )
        self.assertFalse(d2.approved)

        w1 = evaluate_active_window_round(
            ReviewResult(
                summary="R1",
                comments=(adv_comment,),
                provider="fake",
                review_status="complete",
                coverage=_complete_coverage("src/app.py"),
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=1,
            coverage_mode="full",
        )
        ambiguous_window = AuthoritativeDecisionInventory(
            obligations=(
                replace(
                    w1.authoritative_decision_inventory.obligations[0],
                    lifecycle_state="ambiguous-identity",
                ),
            )
        )
        w2 = evaluate_active_window_round(
            clean_r2,
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=2,
            coverage_mode="fallback-full",
            prior_inventory=ambiguous_window,
        )
        self.assertEqual(
            w2.authoritative_decision_inventory.obligations[0].lifecycle_state,
            "ambiguous-identity",
        )
        self.assertFalse(w2.approval_eligibility.approved)

    def test_finalizer_preserves_baseline_uncertain_when_overriding_separate_drift_finding(
        self,
    ) -> None:
        policy = ReviewConvergencePolicy()
        omitted_comment = ReviewComment(
            path="src/app.py",
            line=3,
            body="Baseline required check omitted in R2.",
            severity="high",
            blocking=True,
            effective_blocking=True,
            needs_human=False,
            defect_kind="authz-check",
        )
        drift_comment = ReviewComment(
            path="src/app.py",
            line=None,
            side="FILE",
            body="Separate tracked caller invariant.",
            severity="high",
            blocking=True,
            effective_blocking=True,
            needs_human=True,
            defect_kind="caller-contract",
        )
        baseline = ReviewBaseline(
            cache_key=_cache_key(head_sha=HEAD_1),
            policy_digest=policy.digest(),
            complete=True,
            findings=(baseline_finding_from_comment(omitted_comment),),
            reviewed_paths=("src/app.py",),
            coverage_complete=True,
        )
        candidate = ReviewResult(
            summary="R2",
            comments=(drift_comment,),
            provider="fake",
            review_status="complete",
            coverage=_complete_coverage("src/app.py"),
        )
        reconciled = reconcile_overflow_review(
            candidate, baseline, coverage_mode="fallback-full"
        )
        self.assertTrue(has_uncertain_findings(reconciled))

        with tempfile.TemporaryDirectory() as tmpdir:
            host = _MinimalDriftHost(head_sha=HEAD_2)
            publisher = ReviewPublisher(http=host.http)
            ledger = LocalSessionLedger(Path(tmpdir) / "ledger")
            ledger.initialize(
                SessionIdentity(
                    repository="owner/repo", pull_request=1, repository_id=1
                )
            )
            app = GitHubApplication(
                http=host.http,
                broker=_FakeBroker(),  # type: ignore[arg-type]
                reviewer=publisher,
                learner=LearningPRPublisher(http=host.http),
                replier=ConversationPublisher(http=host.http),
                session_ledger=None,
            )
            cmd_app = GitHubApplication(
                http=host.http,
                broker=_FakeBroker(),  # type: ignore[arg-type]
                reviewer=publisher,
                learner=LearningPRPublisher(http=host.http),
                replier=ConversationPublisher(http=host.http),
                session_ledger=ledger,
            )
            pub_opts = GitHubWriteOptions(
                auto_review=True,
                auto_approve=True,
                github_writes=True,
                operation_entry="disabled",
                max_active_findings=None,
                enable_drift_tracking=False,
            )
            pub = app.publish_review(
                options=pub_opts,
                oidc_token="oidc-u1",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=reconciled,
                diff=SRC_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
            self.assertEqual(pub.status, "published")
            drift_only = evaluate_drift_round(
                candidate,
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=4,
                round_number=1,
                coverage_mode="full",
                diff_text=SRC_DIFF,
            )
            publisher._drift_states[(1, 1)] = drift_only
            ob = drift_only.inventory.obligations[0]

            cmd_opts = GitHubWriteOptions(
                auto_review=True,
                auto_approve=True,
                github_writes=True,
                operation_entry="disabled",
                max_active_findings=4,
                enable_drift_tracking=True,
            )
            cmd_res = cmd_app.apply_maintainer_command(
                options=cmd_opts,
                oidc_token="oidc-u2",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_2,
                app_slug="reviewsensei[bot]",
                actor_login="sec-owner",
                actor_type="User",
                association="OWNER",
                body=(
                    f"@reviewsensei accept-risk {ob.criterion.original_fingerprint} "
                    "--reason Safe in test"
                ),
            )
            self.assertTrue(cmd_res.applied)
            eligibility = approval_eligibility_from_body(str(host.reviews[-1]["body"]))
            assert eligibility is not None
            self.assertTrue(eligibility.facts.has_uncertain_findings)
            decision = evaluate_approval_facts(eligibility.facts)
            self.assertFalse(decision.approved)
            self.assertIn("uncertain-finding-lifecycle", decision.blockers)
            self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))


if __name__ == "__main__":
    unittest.main()
