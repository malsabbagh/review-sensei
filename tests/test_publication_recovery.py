"""Public default publication keeps exact checkpoint bytes across process loss."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from review_sensei_github import GitHubApplication, GitHubWriteOptions, ReviewPublisher
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.operation_host import (
    RESULT_COMMENT_MARKER,
    comment_payload,
    render_marked_comment,
)
from review_sensei_github.publication_recovery import PublicationBudgetHttp

from review_sensei.baseline import ReviewBaseline, baseline_finding_from_comment
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    prepare_review_transaction,
)
from tests.fake_github_http import json_response
from tests.test_human_assessment import APP, BASE, DIFF, HEAD, State
from tests.test_review_transaction import CONFIGURATION, EVIDENCE, _Broker, _Noop

IDENTITY = SessionIdentity("owner/repo", 1, repository_id=1)
POLICY = ReviewConvergencePolicy(mode="merge-focused")


class RetentionState(State):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.part_requests = []
        self.fail_part = None
        self.app_user = {"id": 99, "login": APP, "type": "Bot"}

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        path = parsed.path
        if path == "/users/" + APP.replace("[", "%5B").replace("]", "%5D"):
            self.calls.append((request.method, path, None))
            return json_response(self.app_user)
        if path.endswith("/issues/1/comments"):
            self.calls.append((request.method, path, None))
            self.part_requests.append(request.method)
            if request.method == "GET":
                page = int(parse_qs(parsed.query).get("page", [1])[0])
                return json_response(self.parts[(page - 1) * 100 : page * 100])
            body = json.loads(request.data)["body"]
            item = {
                "id": 700 + len(self.parts),
                "body": body,
                "user": self.app_user,
                "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            }
            if self.fail_part == "crash-before":
                raise KeyboardInterrupt()
            if self.fail_part == "missing":
                return json_response({}, 500)
            self.parts.append(item)
            if self.fail_part == "crash-after":
                raise KeyboardInterrupt()
            if self.fail_part == "lost":
                return json_response({}, 500)
            return json_response(item, 201)
        return super().open(request, timeout)


def checkpoint(ledger, count=77):
    comments = tuple(
        ReviewComment(
            path="src/app.py",
            line=2,
            body=f"Independent required decision {i}.",
            symbol=f"operation_{i}",
            defect_kind=f"case-{i}",
            needs_human=False,
            blocking=False,
        )
        for i in range(count)
    )
    result = ReviewResult(
        summary="Complete retained review.",
        comments=comments,
        provider="fixture",
        model="fixture-model",
        review_status="complete",
    )
    prepared = prepare_review_transaction(
        ledger,
        IDENTITY,
        POLICY,
        reservation_id="d" * 64,
        base_sha=BASE,
        head_sha=HEAD,
        configuration_digest=ReviewTransaction.compute_configuration_digest(
            CONFIGURATION
        ),
        evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
    )
    key = ReviewContextCacheKey(
        repository="owner/repo",
        pull_request=1,
        base_sha=BASE,
        head_sha=HEAD,
        engine="fixture",
        model="fixture-model",
        profile="default",
        stage_digest="1" * 64,
        context_digest="2" * 64,
        learning_digest="3" * 64,
    )
    baseline = ReviewBaseline(
        cache_key=key,
        policy_digest=POLICY.digest(),
        complete=True,
        coverage_complete=True,
        generation=prepared.record.generation + 1,
        findings=tuple(
            sorted(
                (baseline_finding_from_comment(c) for c in comments),
                key=lambda f: f.fingerprint,
            )
        ),
        reviewed_paths=("src/app.py",),
    )
    return checkpoint_review_analysis(
        ledger, IDENTITY, prepared, result, baseline=baseline
    ), key


def application(state, ledger):
    return GitHubApplication(
        broker=_Broker(),
        http=state.http,
        reviewer=ReviewPublisher(http=state.http),
        learner=_Noop(),
        replier=_Noop(),
        session_ledger=ledger,
    )


def publish(app, result, key, **kwargs):
    return app.publish_review(
        options=GitHubWriteOptions(auto_review=True, github_writes=True),
        oidc_token="oidc",
        repository="owner/repo",
        repository_id=1,
        pull_request=1,
        head_sha=HEAD,
        base_sha=BASE,
        base_branch="main",
        result=result,
        diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        + DIFF,
        app_slug=APP,
        convergence_policy=POLICY,
        current_key=key,
        configuration_context=CONFIGURATION,
        evidence_context=EVIDENCE,
        **kwargs,
    )


def recover(app, ledger, **kwargs):
    return app.recover_retained_review_result(
        token="read-token",
        repository="owner/repo",
        repository_id=1,
        pull_request=1,
        base_sha=BASE,
        head_sha=HEAD,
        app_slug=APP,
        ledger=ledger,
        configuration_context=CONFIGURATION,
        evidence_context=EVIDENCE,
        convergence_policy=POLICY,
        **kwargs,
    )


class PublicationRecoveryTests(unittest.TestCase):
    def test_default_publication_failure_retains_exact_result_for_fresh_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LocalSessionLedger(root)
            result, key = checkpoint(ledger)
            state = RetentionState()
            before = ledger.load(IDENTITY).record
            with self.assertRaisesRegex(GitHubPublicationError, "finding index"):
                publish(application(state, ledger), result, key)
            self.assertTrue(
                any(RESULT_COMMENT_MARKER in p["body"] for p in state.parts)
            )
            fresh = LocalSessionLedger(root)
            restored = recover(application(state, fresh), fresh)
            self.assertEqual(restored.content_digest(), result.content_digest())
            self.assertEqual(restored.comments, result.comments)
            self.assertEqual(restored.transaction.phase, "publication_failed")
            self.assertEqual(
                fresh.load(IDENTITY).record.completed_initial_reviews,
                before.completed_initial_reviews,
            )
            self.assertEqual(
                fresh.load(IDENTITY).record.completed_verification_rounds, 0
            )
            self.assertFalse(
                any(c[0] == "POST" and c[1].endswith("/reviews") for c in state.calls)
            )
            # The original pending payload remains reusable after the phase advances.
            posts = state.part_requests.count("POST")
            with self.assertRaisesRegex(GitHubPublicationError, "finding index"):
                publish(application(state, fresh), restored, key)
            self.assertEqual(state.part_requests.count("POST"), posts)

    def test_recovery_rejects_missing_foreign_duplicate_malformed_and_wrong_pr_parts(
        self,
    ):
        for mutation in (
            "missing",
            "foreign",
            "duplicate",
            "malformed",
            "wrong-pr",
            "unknown-field",
            "different-result",
        ):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger)
                state = RetentionState()
                app = application(state, ledger)
                with self.assertRaisesRegex(GitHubPublicationError, "finding index"):
                    publish(app, result, key)
                original_posts = state.part_requests.count("POST")
                if mutation == "missing":
                    state.parts.clear()
                elif mutation == "foreign":
                    state.parts[0] = {
                        **state.parts[0],
                        "user": {"id": 12, "login": "other[bot]", "type": "Bot"},
                    }
                elif mutation == "duplicate":
                    state.parts.append(dict(state.parts[0], id=800))
                elif mutation == "malformed":
                    state.parts[0]["body"] = RESULT_COMMENT_MARKER + "invalid"
                elif mutation == "wrong-pr":
                    state.parts[0]["issue_url"] = (
                        "https://api.github.test/repos/owner/repo/issues/2"
                    )
                else:
                    envelope = json.loads(comment_payload(state.parts[0]["body"]))
                    payload = json.loads(base64.b64decode(envelope["data"]))
                    if mutation == "unknown-field":
                        payload["raw_prompt"] = "must never be accepted or republished"
                    else:
                        payload["summary"] = "A different result."
                        payload["transaction"]["result_sha256"] = None
                    raw = json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ).encode()
                    envelope["data"] = base64.b64encode(raw).decode()
                    envelope["result_sha256"] = hashlib.sha256(raw).hexdigest()
                    state.parts[0]["body"] = render_marked_comment(
                        RESULT_COMMENT_MARKER, envelope
                    )
                with self.assertRaises(GitHubPublicationError):
                    recover(app, LocalSessionLedger(Path(directory)))
                self.assertEqual(state.part_requests.count("POST"), original_posts)

    def test_recovery_requires_current_remote_head_and_independent_context(self):
        for mutation in (
            "head",
            "base",
            "configuration",
            "evidence",
            "transaction",
            "owner",
        ):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger)
                state = RetentionState()
                app = application(state, ledger)
                with self.assertRaisesRegex(GitHubPublicationError, "finding index"):
                    publish(app, result, key)
                if mutation == "head":
                    state.head = "c" * 40
                elif mutation == "base":
                    state.base = "c" * 40
                elif mutation == "owner":
                    state.app_user = {"id": 99, "login": APP, "type": "User"}
                elif mutation == "transaction":
                    ledger.replace(
                        IDENTITY, lambda current: current.evolve(transaction=None)
                    )
                kwargs = dict(
                    token="read-token",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    base_sha=BASE,
                    head_sha=HEAD,
                    app_slug=APP,
                    ledger=ledger,
                    convergence_policy=POLICY,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                )
                if mutation == "configuration":
                    kwargs["configuration_context"] = {
                        **CONFIGURATION,
                        "model": "other-model",
                    }
                elif mutation == "evidence":
                    kwargs["evidence_context"] = {
                        "evidence_policy": "confirmed",
                        "snapshot_sha256": "c" * 64,
                    }
                with self.assertRaises(GitHubPublicationError):
                    app.recover_retained_review_result(**kwargs)
                self.assertEqual(
                    ledger.load(IDENTITY).record.completed_initial_reviews, 1
                )

    def test_lost_post_is_reconciled_without_repeating_create(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger)
            state = RetentionState()
            state.fail_part = "lost"
            app = application(state, ledger)
            with self.assertRaisesRegex(GitHubPublicationError, "finding index"):
                publish(app, result, key)
            self.assertEqual(state.part_requests.count("POST"), 1)
            self.assertEqual(
                recover(app, ledger).content_digest(), result.content_digest()
            )

    def test_unknown_missing_post_holds_before_any_review_or_check_write(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger)
            state = RetentionState()
            state.fail_part = "missing"
            with self.assertRaisesRegex(GitHubPublicationError, "outcome unknown"):
                publish(application(state, ledger), result, key)
            self.assertEqual(state.part_requests.count("POST"), 1)
            self.assertTrue(state.check_runs)
            self.assertTrue(
                all(c["conclusion"] == "action_required" for c in state.check_runs)
            )
            self.assertFalse(
                any(
                    method == "POST" and path.endswith("/reviews")
                    for method, path, _ in state.calls
                )
            )

    def test_disabled_operation_entry_does_not_retain(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger)
            state = RetentionState()
            app = application(state, ledger)
            with self.assertRaisesRegex(GitHubPublicationError, "finding index"):
                app.publish_review(
                    options=GitHubWriteOptions(
                        auto_review=True, github_writes=True, operation_entry="disabled"
                    ),
                    oidc_token="oidc",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    head_sha=HEAD,
                    base_sha=BASE,
                    base_branch="main",
                    result=result,
                    diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
                    + DIFF,
                    app_slug=APP,
                    convergence_policy=POLICY,
                    current_key=key,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                )
            self.assertEqual(state.parts, [])

    def test_expired_original_budget_does_not_mutate_or_refill(self):
        clock = [1.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])
        clock[0] = 62.0
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger)
            state = RetentionState()
            with self.assertRaisesRegex(Exception, "deadline"):
                publish(
                    application(state, ledger), result, key, publication_budget=budget
                )
            self.assertEqual(state.calls, [])
            self.assertEqual(budget.calls, 0)

    def test_known_budget_wrapper_charges_once_and_refuses_another_budget(self):
        from review_sensei_github.human_assessment import FeedbackBudgetHttp

        state = RetentionState()
        budget = EvidenceReadBudget()
        wrapper = FeedbackBudgetHttp(http=state.http, before_read=budget.consume)
        wrapped = PublicationBudgetHttp(wrapper, budget)
        wrapped.request(
            "GET", "/users/" + APP.replace("[", "%5B").replace("]", "%5D"), token="read"
        )
        self.assertEqual(budget.calls, 1)
        with self.assertRaises(GitHubPublicationError):
            PublicationBudgetHttp(wrapper, EvidenceReadBudget())
        with self.assertRaises(GitHubPublicationError):
            PublicationBudgetHttp(
                FeedbackBudgetHttp(
                    http=state.http, before_read=budget._remaining_seconds
                ),
                budget,
            )

    def test_crash_fence_allows_only_complete_readback_without_new_post(self):
        for outcome in ("crash-before", "crash-after"):
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger)
                state = RetentionState()
                state.fail_part = outcome
                with self.assertRaises(KeyboardInterrupt):
                    publish(application(state, ledger), result, key)
                self.assertEqual(
                    ledger.load(IDENTITY).record.transaction.phase, "publication_failed"
                )
                fresh = LocalSessionLedger(Path(directory))
                app = application(state, fresh)
                state.fail_part = None
                if outcome == "crash-after":
                    restored = recover(app, fresh)
                    self.assertEqual(restored.content_digest(), result.content_digest())
                    with self.assertRaisesRegex(
                        GitHubPublicationError, "finding index"
                    ):
                        publish(app, restored, key)
                else:
                    with self.assertRaisesRegex(
                        GitHubPublicationError, "do not recreate"
                    ):
                        publish(app, result, key)
                self.assertEqual(state.part_requests.count("POST"), 1)
                self.assertEqual(
                    fresh.load(IDENTITY).record.completed_initial_reviews, 1
                )

    def test_checkpoint_placeholder_recovery_refuses_foreign_progress(self):
        for kind in (
            "generation",
            "future-generation",
            "reservation",
            "foreign-marker",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger)
                state = RetentionState()
                state.fail_part = "crash-after"
                with self.assertRaises(KeyboardInterrupt):
                    publish(application(state, ledger), result, key)

                def mutate(current):
                    if kind == "reservation":
                        return current.evolve(last_committed_reservation_id="e" * 64)
                    history = dict(current.convergence_history)
                    marker = {"event": "completed", "generation": 1}
                    if kind == "future-generation":
                        marker["generation"] = current.generation + 1
                    if kind == "foreign-marker":
                        marker.update(
                            blocker_set_sha256="e" * 64,
                            blocker_count=1,
                            transaction_id="e" * 64,
                        )
                    history["progress"] = [marker]
                    return current.evolve(convergence_history=history)

                if kind == "reservation":
                    with self.assertRaisesRegex(Exception, "reservation"):
                        ledger.replace(IDENTITY, mutate)
                    continue
                ledger.replace(IDENTITY, mutate)
                state.fail_part = None
                with self.assertRaisesRegex(Exception, "progress"):
                    publish(application(state, ledger), result, key)
                self.assertEqual(state.part_requests.count("POST"), 1)
