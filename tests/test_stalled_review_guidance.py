"""Consistent next-safe-action guidance across publication, review status, and job summaries."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from fake_github_http import json_response
from review_sensei_github import (
    GitHubApplication,
    GitHubHttp,
    GitHubWriteOptions,
    PublicationResult,
    ReviewPublisher,
)
from review_sensei_github.cli import (
    _maybe_write_step_summary_for_error,
)
from review_sensei_github.cli import main as github_main
from review_sensei_github.errors import (
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from review_sensei_github.operation_diagnostics import (
    OperationDiagnostics,
    OperationFailure,
    PhaseFailures,
)
from review_sensei_github.rescan import event_requests_remaining
from review_sensei_github.review_guidance import (
    _clean_text,
    extract_forbidden_lookup,
    resolve_next_action_guidance,
)
from review_sensei_github.review_run import _get as review_run_get
from test_human_assessment import APP, BASE, HEAD
from test_review_transaction import CONFIGURATION, EVIDENCE, _Broker, _Noop

from review_sensei.baseline import (
    ReviewBaseline,
    ReviewContextCacheKey,
    baseline_finding_from_comment,
)
from review_sensei.convergence import (
    DEFAULT_MAX_FAILED_ATTEMPTS,
    ReviewConvergencePolicy,
)
from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
from review_sensei.disposition import (
    apply_session_command,
    parse_maintainer_command,
    session_dispositions,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.presentation import NO_REQUIRED_FIXES_STATE, ReviewSummaryView
from review_sensei.providers.base import ReviewProvider
from review_sensei.service import ReviewService
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    complete_review_publication,
    prepare_review_transaction,
    record_session_failed_attempt,
    session_reservation_id,
)

IDENTITY = SessionIdentity("owner/repo", 77, repository_id=123)
POLICY = ReviewConvergencePolicy(mode="merge-focused")
SMALL_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-return old()\n"
    "+return new()\n"
)
TWO_FILE_DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-return 1\n"
    "+return 2\n"
    "diff --git a/src/b.py b/src/b.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/src/b.py\n"
    "+++ b/src/b.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-return 3\n"
    "+return 4\n"
)


class _SimulatedGitHub:
    """In-memory fake GitHub endpoint modeling crash/403/reconciliation/head-change states."""

    def __init__(self, *, head_sha: str = HEAD, base_sha: str = BASE) -> None:
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.reviews: list[dict[str, object]] = []
        self.inline_comments: list[dict[str, object]] = []
        self.issue_comments: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []
        self.resolved_threads: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.crash_after_review_post = False
        self.fail_review_post_502 = False
        self.lose_response_recorded_502 = False
        self.forced_status: dict[tuple[str, str], int] = {}
        self.graphql_403 = False

    def opener(self, request, timeout):  # noqa: ARG001
        parsed = urlparse(request.full_url)
        method = request.method
        path = parsed.path
        self.requests.append((method, path))

        for (m, suffix), code in self.forced_status.items():
            if method == m and path.endswith(suffix):
                return json_response({"message": "Forbidden"}, status=code)

        if (
            method == "GET"
            and path.casefold()
            == ("/users/" + APP.replace("[", "%5B").replace("]", "%5D")).casefold()
        ):
            return json_response({"id": 99, "login": APP, "type": "Bot"})

        if method == "GET" and path.endswith(f"/pulls/{IDENTITY.pull_request}"):
            return json_response(
                {
                    "id": 7000 + IDENTITY.pull_request,
                    "number": IDENTITY.pull_request,
                    "state": "open",
                    "draft": False,
                    "head": {
                        "ref": "feature",
                        "sha": self.head_sha,
                        "repo": {
                            "id": IDENTITY.repository_id,
                            "full_name": IDENTITY.repository,
                            "fork": False,
                        },
                    },
                    "base": {
                        "ref": "main",
                        "sha": self.base_sha,
                        "repo": {
                            "id": IDENTITY.repository_id,
                            "full_name": IDENTITY.repository,
                            "fork": False,
                        },
                    },
                    "user": {"id": 10, "login": "contributor", "type": "User"},
                }
            )

        if path.endswith("/rules/branches/main") or path.endswith(
            "/branches/main/protection"
        ):
            return json_response({}, status=404)

        if method == "GET" and path.endswith(f"/pulls/{IDENTITY.pull_request}/reviews"):
            return json_response(deepcopy(self.reviews))

        if method == "GET" and path.endswith(
            f"/pulls/{IDENTITY.pull_request}/comments"
        ):
            return json_response(deepcopy(self.inline_comments))

        if method == "POST" and path.endswith(
            f"/pulls/{IDENTITY.pull_request}/reviews"
        ):
            payload = json.loads(request.data)
            if self.fail_review_post_502:
                return json_response({"message": "bad gateway"}, status=502)
            review_id = 400 + len(self.reviews)
            record = {
                "id": review_id,
                "body": payload.get("body", ""),
                "state": (
                    "APPROVED"
                    if payload.get("event") == "APPROVE"
                    else (
                        "CHANGES_REQUESTED"
                        if payload.get("event") == "REQUEST_CHANGES"
                        else "COMMENTED"
                    )
                ),
                "commit_id": payload.get("commit_id", self.head_sha),
                "user": {"id": 99, "login": APP, "type": "Bot"},
                "submitted_at": "2026-11-25T10:00:00Z",
                "edited_by_human": False,
            }
            self.reviews.append(record)
            if self.lose_response_recorded_502:
                self.lose_response_recorded_502 = False
                return json_response({"message": "gateway timeout"}, status=502)
            if self.crash_after_review_post:
                self.crash_after_review_post = False
                raise KeyboardInterrupt(
                    "simulated crash after GitHub accepted review POST"
                )
            return json_response(deepcopy(record), status=200)

        if method == "POST" and path.endswith("/check-runs"):
            payload = json.loads(request.data)
            check_id = 900 + len(self.check_runs)
            entry = {
                "id": check_id,
                "name": payload.get("name"),
                "head_sha": payload.get("head_sha"),
                "status": payload.get("status", "completed"),
                "conclusion": payload.get("conclusion"),
                "app": {"slug": APP.removesuffix("[bot]"), "id": 99},
            }
            self.check_runs.append(entry)
            return json_response(deepcopy(entry), status=201)

        if method == "PATCH" and "/check-runs/" in path:
            cid = int(path.rsplit("/", 1)[1])
            payload = json.loads(request.data)
            for entry in self.check_runs:
                if entry["id"] == cid:
                    entry.update(payload)
                    return json_response(deepcopy(entry), status=200)
            return json_response({}, status=404)

        if method == "GET" and "/check-runs" in path:
            return json_response({"check_runs": deepcopy(self.check_runs)})

        if method == "POST" and path == "/graphql":
            if self.graphql_403:
                return json_response(
                    {"message": "Resource not accessible by integration"}, status=403
                )
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [],
                                }
                            }
                        }
                    }
                }
            )

        if path.endswith(f"/issues/{IDENTITY.pull_request}/comments"):
            if request.method == "GET":
                return json_response(deepcopy(self.issue_comments))
            if request.method == "POST":
                payload = json.loads(request.data)
                comment = {
                    "id": 800 + len(self.issue_comments),
                    "body": payload.get("body", ""),
                    "user": {"id": 99, "login": APP, "type": "Bot"},
                    "issue_url": (
                        f"https://api.github.test/repos/{IDENTITY.repository}"
                        f"/issues/{IDENTITY.pull_request}"
                    ),
                }
                self.issue_comments.append(comment)
                return json_response(deepcopy(comment), status=201)

        if "/issues/comments/" in path:
            cid = int(path.rsplit("/", 1)[1])
            for item in self.issue_comments:
                if item["id"] == cid:
                    if request.method == "PATCH":
                        item["body"] = json.loads(request.data)["body"]
                    return json_response(deepcopy(item))
            return json_response({}, status=404)

        raise AssertionError(f"unexpected GitHub request: {method} {path}")


def _partial_remaining_text_result() -> ReviewResult:
    return ReviewResult(
        summary="Partial analysis completed for src/a.py; src/b.py remains unreviewed.",
        comments=(),
        provider="fixture",
        model="fixture",
        review_status="partial",
        coverage_only_partial=False,
        coverage=CoverageManifest(
            enumeration_complete=True,
            enumerated_paths=("src/a.py", "src/b.py"),
            files=(
                FileCoverage(path="src/a.py", outcome="reviewed"),
                FileCoverage(
                    path="src/b.py",
                    outcome="budget-exhausted",
                    reason="provider-call-budget",
                ),
            ),
            hunks=(
                HunkCoverage(index=1, path="src/a.py", outcome="reviewed"),
                HunkCoverage(
                    index=2,
                    path="src/b.py",
                    outcome="budget-exhausted",
                    reason="provider-call-budget",
                ),
            ),
        ),
    )


def _complete_human_finding_result() -> ReviewResult:
    return ReviewResult(
        summary="One finding requires human review before approval.",
        comments=(
            ReviewComment(
                path="src/app.py",
                line=1,
                body="Confirm this call path preserves caller authorization.",
                severity="high",
                effective_blocking=False,
                needs_human=True,
            ),
        ),
        provider="fixture",
        model="fixture",
        review_status="complete",
        coverage=CoverageManifest(
            enumeration_complete=True,
            enumerated_paths=("src/app.py",),
            files=(FileCoverage(path="src/app.py", outcome="reviewed"),),
            hunks=(HunkCoverage(index=1, path="src/app.py", outcome="reviewed"),),
        ),
    )


def _make_cache_key(head_sha: str = HEAD) -> ReviewContextCacheKey:
    return ReviewContextCacheKey(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        base_sha=BASE,
        head_sha=head_sha,
        engine="fixture",
        model="fixture-model",
        profile="default",
        stage_digest="1" * 64,
        context_digest="2" * 64,
        learning_digest="3" * 64,
    )


def _make_app(state: _SimulatedGitHub, ledger: LocalSessionLedger) -> GitHubApplication:
    http = GitHubHttp(
        api_url="https://api.github.test",
        opener=state.opener,
    )
    reviewer = ReviewPublisher(http=http)
    return GitHubApplication(
        broker=_Broker(),
        http=http,
        reviewer=reviewer,
        learner=_Noop(),
        replier=_Noop(),
        session_ledger=ledger,
    )


class StalledReviewGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger = LocalSessionLedger(Path(self.tmpdir.name) / "ledger")

    def test_remaining_text_same_commit_consistent_across_publication_and_status(self):
        state = _SimulatedGitHub()
        app = _make_app(state, self.ledger)
        partial = _partial_remaining_text_result()
        cfg_digest = ReviewTransaction.compute_configuration_digest(CONFIGURATION)
        ev_digest = ReviewTransaction.compute_evidence_digest(EVIDENCE)
        tx = prepare_review_transaction(
            self.ledger,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind="res-partial",
            ),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=cfg_digest,
            evidence_digest=ev_digest,
        )
        partial = checkpoint_review_analysis(self.ledger, IDENTITY, tx, partial)

        pub = app.publish_review(
            options=GitHubWriteOptions(
                github_writes=True, auto_review=True, auto_approve=True
            ),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            base_branch="main",
            base_sha=BASE,
            result=partial,
            diff=TWO_FILE_DIFF,
            app_slug=APP,
            convergence_policy=POLICY,
            current_key=_make_cache_key(HEAD),
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
        )
        self.assertIsNotNone(pub.next_action_guidance)
        self.assertEqual(pub.next_action_guidance.state, "remaining_text_coverage")
        expected_cmd = f"@reviewsensei review remaining commit {HEAD}"
        self.assertEqual(pub.next_action_guidance.command, expected_cmd)
        self.assertTrue(pub.next_action_guidance.requires_model_call)
        self.assertIn("bounded analysis window", pub.next_action_guidance.next_action)

        # Published PR review body embeds the exact same guidance and command.
        posted_body = str(state.reviews[0]["body"])
        self.assertIn(expected_cmd, posted_body)
        self.assertIn("bounded analysis window", posted_body)
        self.assertIn("Requires model calls", posted_body)
        self.assertTrue(
            event_requests_remaining(
                {"comment": {"body": expected_cmd}}, "issue_comment"
            )
        )

        # `review status` produces one consistent read-only answer with zero mutations/model calls.
        before_ledger = self.ledger.load(IDENTITY).record.to_dict()
        req_before = len(state.requests)
        status = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=False),
            oidc_token=None,
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            body="@reviewsensei review status",
            actor_login="alice",
            association="OWNER",
            app_slug=APP,
            review_result=partial,
        )
        self.assertEqual(self.ledger.load(IDENTITY).record.to_dict(), before_ledger)
        self.assertEqual(len(state.requests), req_before)
        self.assertIn("state=remaining_text_coverage", status.summary)
        self.assertIn(expected_cmd, status.summary)
        self.assertIn("bounded analysis window", status.summary)
        self.assertIn("model_calls=required", status.summary)

    def test_decisions_recorded_only_finalization_remains_shows_verify_zero_model_calls(
        self,
    ):
        state = _SimulatedGitHub()
        app = _make_app(state, self.ledger)
        result = _complete_human_finding_result()
        cfg_digest = ReviewTransaction.compute_configuration_digest(CONFIGURATION)
        ev_digest = ReviewTransaction.compute_evidence_digest(EVIDENCE)
        tx = prepare_review_transaction(
            self.ledger,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind="res-verify",
            ),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=cfg_digest,
            evidence_digest=ev_digest,
        )
        result = checkpoint_review_analysis(self.ledger, IDENTITY, tx, result)
        complete_review_publication(
            self.ledger, IDENTITY, result.transaction, published=True
        )
        from review_sensei.context import finding_lifecycle_for_comment

        fp = finding_lifecycle_for_comment(result.comments[0]).fingerprint
        disp = parse_maintainer_command(
            f"@reviewsensei accept-risk {fp} --reason intentional boundary",
            actor="alice",
            head_sha=HEAD,
        )
        self.assertIsNotNone(disp)
        assert disp is not None
        apply_session_command(self.ledger, IDENTITY, disp)

        # Publication whose finalization still needs verify (e.g. gate re-evaluation / check pending).
        diag = OperationDiagnostics(
            eligibility=None,
            check=PhaseFailures(
                first=OperationFailure(
                    stage="check.publication",
                    reason="http",
                    http_status=503,
                    graphql_categories=(),
                    budget_calls=None,
                ),
                count=1,
            ),
            cleanup=None,
            approval=None,
            budget_calls=1,
            approval_request_attempted=False,
            transport_request_attempts=1,
            next_action="retry_verify",
        )
        pub_result = PublicationResult(
            status="gate_reevaluation_failed",
            diagnostic="check_reconciliation_failed",
            review_id=400,
            operation_diagnostics=diag,
        )
        record = self.ledger.load(IDENTITY).record
        pub_guidance = resolve_next_action_guidance(
            head_sha=HEAD,
            record=record,
            retained_result=result,
            publication=pub_result,
            dispositions=session_dispositions(record, ledger=self.ledger),
            auto_approve=True,
        )
        status = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=False),
            oidc_token=None,
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            body="@reviewsensei review status",
            actor_login="alice",
            association="OWNER",
            app_slug=APP,
            review_result=result,
        )
        self.assertEqual(pub_guidance.state, "ready_for_verify")
        self.assertEqual(pub_guidance.command, "@reviewsensei verify")
        self.assertFalse(pub_guidance.requires_model_call)
        self.assertIsNotNone(
            parse_maintainer_command(
                "@reviewsensei verify", actor="alice", head_sha=HEAD
            )
        )
        self.assertIn("state=ready_for_verify", status.summary)
        self.assertIn("@reviewsensei verify", status.summary)
        self.assertIn("model_calls=0", status.summary)

    def test_required_403_lookups_identify_failed_lookup_and_hold_without_retry(self):
        for label, setup_fn, needle in (
            (
                "pr_metadata",
                lambda s: s.forced_status.__setitem__(
                    ("GET", f"/pulls/{IDENTITY.pull_request}"), 403
                ),
                f"/pulls/{IDENTITY.pull_request}",
            ),
            (
                "pr_reviews",
                lambda s: s.forced_status.__setitem__(
                    ("GET", f"/pulls/{IDENTITY.pull_request}/reviews"), 403
                ),
                "/reviews",
            ),
            (
                "graphql_threads",
                lambda s: setattr(s, "graphql_403", True),
                "ReviewThreads",
            ),
        ):
            with self.subTest(label=label):
                state = _SimulatedGitHub()
                setup_fn(state)
                app = _make_app(state, self.ledger)
                result = _complete_human_finding_result()
                with self.assertRaises(GitHubPublicationError) as ctx:
                    app.publish_review(
                        options=GitHubWriteOptions(
                            github_writes=True, auto_review=True, auto_approve=True
                        ),
                        oidc_token="oidc",
                        repository=IDENTITY.repository,
                        repository_id=IDENTITY.repository_id,
                        pull_request=IDENTITY.pull_request,
                        head_sha=HEAD,
                        base_branch="main",
                        base_sha=BASE,
                        result=result,
                        diff=SMALL_DIFF,
                        app_slug=APP,
                        convergence_policy=POLICY,
                        current_key=_make_cache_key(HEAD),
                        configuration_context=CONFIGURATION,
                        evidence_context=EVIDENCE,
                    )
                pub_guidance = resolve_next_action_guidance(
                    head_sha=HEAD,
                    retained_result=result,
                    error=ctx.exception,
                )
                self.assertEqual(pub_guidance.state, "lookup_forbidden_hold")
                self.assertIsNone(pub_guidance.command)
                self.assertFalse(pub_guidance.requires_model_call)
                self.assertIn("403", pub_guidance.blocker or "")
                self.assertIn(needle, pub_guidance.failed_lookup or "")
                self.assertNotIn("retry", pub_guidance.next_action.lower())

        # Check-run 403 diagnostic produces the exact same lookup_forbidden_hold on both surfaces.
        op_diag = OperationDiagnostics(
            eligibility=None,
            check=PhaseFailures(
                first=OperationFailure(
                    stage="check.publication",
                    reason="http",
                    http_status=403,
                    graphql_categories=(),
                    budget_calls=None,
                ),
                count=1,
            ),
            cleanup=None,
            approval=None,
            budget_calls=1,
            approval_request_attempted=False,
            transport_request_attempts=1,
            next_action="retry_verify",
        )
        check_pub = PublicationResult(
            status="published",
            review_id=401,
            diagnostic="check_access_denied",
            operation_diagnostics=op_diag,
        )
        state = _SimulatedGitHub()
        app = _make_app(state, self.ledger)
        app._last_publication_result = check_pub
        app._last_publication_context = (
            IDENTITY.repository,
            IDENTITY.pull_request,
            HEAD,
        )
        pub_g = resolve_next_action_guidance(
            head_sha=HEAD,
            operation_diagnostics=op_diag,
            publication=check_pub,
        )
        status = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=False),
            oidc_token=None,
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            body="@reviewsensei review status",
            actor_login="alice",
            association="OWNER",
            app_slug=APP,
        )
        self.assertEqual(pub_g.state, "lookup_forbidden_hold")
        self.assertIn("check-run publication (HTTP 403)", pub_g.failed_lookup or "")
        self.assertIn("state=lookup_forbidden_hold", status.summary)
        self.assertIn("failed_lookup=check-run publication (HTTP 403)", status.summary)
        self.assertNotIn("retry", status.summary.lower())

    def test_actions_job_summary_written_on_command_publish_failure(self):
        state = _SimulatedGitHub()
        state.forced_status[("POST", f"/pulls/{IDENTITY.pull_request}/reviews")] = 403
        result = _complete_human_finding_result()
        result = replace(result, comments=(), summary="Clean review.")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            diff_file = tmp_path / "pr.diff"
            diff_file.write_text(SMALL_DIFF, encoding="utf-8", newline="\n")
            result_file = tmp_path / "result.json"
            result_file.write_text(
                json.dumps(result.to_dict()), encoding="utf-8", newline="\n"
            )
            summary_file = tmp_path / "step-summary.md"

            http = GitHubHttp(
                api_url="https://api.github.test",
                opener=state.opener,
            )
            reviewer = ReviewPublisher(http=http)
            app = GitHubApplication(
                broker=_Broker(),
                http=http,
                reviewer=reviewer,
                learner=_Noop(),
                replier=_Noop(),
                session_ledger=None,
            )
            with (
                patch.dict(
                    os.environ,
                    {"GITHUB_STEP_SUMMARY": str(summary_file), "GITHUB_RUN_ID": ""},
                ),
                patch(
                    "review_sensei_github.BrokerClient",
                    return_value=_Broker(),
                ),
                patch(
                    "review_sensei_github.GitHubHttp",
                    return_value=http,
                ),
                patch(
                    "review_sensei_github.GitHubApplication",
                    return_value=app,
                ),
            ):
                rc = github_main(
                    [
                        "publish",
                        "--allow-write",
                        "--enable-review",
                        "--repository",
                        IDENTITY.repository,
                        "--repository-id",
                        str(IDENTITY.repository_id),
                        "--pull-request",
                        str(IDENTITY.pull_request),
                        "--head-sha",
                        HEAD,
                        "--base-branch",
                        "main",
                        "--base-sha",
                        BASE,
                        "--diff",
                        str(diff_file),
                        "--result",
                        str(result_file),
                        "--app-slug",
                        APP,
                        "--oidc-token",
                        "synthetic-oidc",
                    ]
                )
            self.assertNotEqual(rc, 0)
            written = summary_file.read_text(encoding="utf-8")
            self.assertIn("ReviewSensei GitHub publication did not complete", written)
            section = written.split(
                "## ReviewSensei GitHub publication did not complete", 1
            )[1]
            self.assertIn("HTTP 403", section)
            self.assertIn("POST /pulls/77/reviews", section)
            self.assertNotIn("retry", section.lower())

        # Startup GET 403 in review_run attaches exact failed_lookup and flows to step summary.
        state403 = _SimulatedGitHub()
        state403.forced_status[("GET", f"/pulls/{IDENTITY.pull_request}")] = 403
        http403 = GitHubHttp(
            api_url="https://api.github.test",
            opener=state403.opener,
        )
        with self.assertRaises(ReviewInputError) as startup_ctx:
            review_run_get(
                http403,
                http403.repository_path(
                    IDENTITY.repository, f"/pulls/{IDENTITY.pull_request}"
                ),
                "tok",
            )
        startup_guidance = resolve_next_action_guidance(
            head_sha=HEAD,
            error=startup_ctx.exception,
        )
        self.assertEqual(startup_guidance.state, "lookup_forbidden_hold")
        self.assertIn(
            f"/pulls/{IDENTITY.pull_request}", startup_guidance.failed_lookup or ""
        )
        self.assertNotIn("retry", startup_guidance.next_action.lower())
        with tempfile.TemporaryDirectory() as tmp2:
            startup_summary = Path(tmp2) / "startup-summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(startup_summary)}):
                _maybe_write_step_summary_for_error(startup_ctx.exception)
            written_startup = startup_summary.read_text(encoding="utf-8")
            self.assertIn(f"/pulls/{IDENTITY.pull_request}", written_startup)
            self.assertIn("HTTP 403", written_startup)
            self.assertNotIn("retry", written_startup.lower())

    def test_acceptance_crash_after_github_accept_and_edge_cases(self):
        # 1a. Qualifying clean commit: crash right after GitHub accepts POST /reviews,
        # BEFORE Sensei records `publication_succeeded` in the durable ledger.
        clean_result = replace(
            _complete_human_finding_result(),
            summary="Clean change verified.",
            comments=(),
        )
        cfg_digest = ReviewTransaction.compute_configuration_digest(CONFIGURATION)
        ev_digest = ReviewTransaction.compute_evidence_digest(EVIDENCE)
        tx = prepare_review_transaction(
            self.ledger,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind="res-crash-clean",
            ),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=cfg_digest,
            evidence_digest=ev_digest,
        )
        key_clean = _make_cache_key(HEAD)
        baseline_clean = ReviewBaseline(
            cache_key=key_clean,
            policy_digest=POLICY.digest(),
            complete=True,
            coverage_complete=True,
            generation=tx.record.generation + 1,
            findings=(),
            reviewed_paths=("src/app.py",),
        )
        retained = checkpoint_review_analysis(
            self.ledger, IDENTITY, tx, clean_result, baseline=baseline_clean
        )

        state = _SimulatedGitHub()
        state.crash_after_review_post = True
        app1 = _make_app(state, self.ledger)
        with self.assertRaises(KeyboardInterrupt):
            app1.publish_review(
                options=GitHubWriteOptions(
                    github_writes=True, auto_review=True, auto_approve=True
                ),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_branch="main",
                base_sha=BASE,
                result=retained,
                diff=SMALL_DIFF,
                app_slug=APP,
                convergence_policy=POLICY,
                current_key=_make_cache_key(HEAD),
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
            )

        # Durable state did NOT record publication_succeeded before the crash.
        loaded_mid = LocalSessionLedger(self.ledger.root).load(IDENTITY)
        self.assertEqual(loaded_mid.record.transaction.phase, "publication_failed")
        self.assertEqual(len([r for r in state.reviews if r["state"] != "APPROVED"]), 1)
        mid_guidance = resolve_next_action_guidance(
            head_sha=HEAD,
            record=loaded_mid.record,
            retained_result=clean_result,
        )
        self.assertEqual(mid_guidance.state, "response_loss_unknown_hold")
        self.assertFalse(mid_guidance.requires_model_call)

        # Restart from a completely fresh process/ledger/app using ONLY durable state;
        # zero model calls allowed.
        fresh_ledger = LocalSessionLedger(self.ledger.root)
        loaded_restart = fresh_ledger.load(IDENTITY)
        restored_result = fresh_ledger.load_retained_checkpoint_result(
            IDENTITY, transaction=loaded_restart.record.transaction, head_sha=HEAD
        )
        self.assertIsNotNone(restored_result)
        app2 = _make_app(state, fresh_ledger)
        with (
            patch.object(
                ReviewService,
                "review",
                side_effect=AssertionError(
                    "model analysis must not re-run on crash restart"
                ),
            ),
            patch.object(
                ReviewService,
                "run",
                side_effect=AssertionError(
                    "model analysis must not re-run on crash restart"
                ),
            ),
            patch.object(
                ReviewProvider,
                "complete",
                side_effect=AssertionError(
                    "provider must not be called on crash restart"
                ),
            ),
        ):
            restarted = app2.publish_review(
                options=GitHubWriteOptions(
                    github_writes=True, auto_review=True, auto_approve=True
                ),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_branch="main",
                base_sha=BASE,
                result=restored_result,
                diff=SMALL_DIFF,
                app_slug=APP,
                convergence_policy=POLICY,
                current_key=_make_cache_key(HEAD),
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
            )
        self.assertEqual(restarted.status, "already_published")
        non_approve_reviews = [r for r in state.reviews if r["state"] != "APPROVED"]
        approve_reviews = [r for r in state.reviews if r["state"] == "APPROVED"]
        self.assertEqual(len(non_approve_reviews), 1)
        self.assertEqual(len(approve_reviews), 1)
        loaded_after_restart = fresh_ledger.load(IDENTITY)
        self.assertEqual(
            loaded_after_restart.record.transaction.phase,
            "publication_succeeded",
        )
        self.assertEqual(
            loaded_after_restart.record.failed_attempts,
            loaded_mid.record.failed_attempts,
        )

        # 1b. Non-qualifying commit (open blocking finding): restart reconciles the 1 review
        # with 0 repeated model calls, preserves accounting, and does NOT approve.
        dir2 = tempfile.TemporaryDirectory()
        self.addCleanup(dir2.cleanup)
        ledger2 = LocalSessionLedger(Path(dir2.name) / "ledger")
        blocking_result = replace(
            clean_result,
            summary="Blocking issue found.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=1,
                    body="Must fix.",
                    blocking=True,
                    effective_blocking=True,
                    severity="high",
                ),
            ),
        )
        tx2 = prepare_review_transaction(
            ledger2,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind="res-crash-block",
            ),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=cfg_digest,
            evidence_digest=ev_digest,
        )
        baseline2 = ReviewBaseline(
            cache_key=key_clean,
            policy_digest=POLICY.digest(),
            complete=True,
            coverage_complete=True,
            generation=tx2.record.generation + 1,
            findings=(baseline_finding_from_comment(blocking_result.comments[0]),),
            reviewed_paths=("src/app.py",),
        )
        retained2 = checkpoint_review_analysis(
            ledger2, IDENTITY, tx2, blocking_result, baseline=baseline2
        )
        state2 = _SimulatedGitHub()
        state2.crash_after_review_post = True
        with self.assertRaises(KeyboardInterrupt):
            _make_app(state2, ledger2).publish_review(
                options=GitHubWriteOptions(
                    github_writes=True, auto_review=True, auto_approve=True
                ),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_branch="main",
                base_sha=BASE,
                result=retained2,
                diff=SMALL_DIFF,
                app_slug=APP,
                convergence_policy=POLICY,
                current_key=_make_cache_key(HEAD),
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
            )
        restarted2 = _make_app(state2, LocalSessionLedger(ledger2.root)).publish_review(
            options=GitHubWriteOptions(
                github_writes=True, auto_review=True, auto_approve=True
            ),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            base_branch="main",
            base_sha=BASE,
            result=retained2,
            diff=SMALL_DIFF,
            app_slug=APP,
            convergence_policy=POLICY,
            current_key=_make_cache_key(HEAD),
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
        )
        self.assertEqual(
            len([r for r in state2.reviews if r["state"] != "APPROVED"]), 1
        )
        self.assertEqual(
            len([r for r in state2.reviews if r["state"] == "APPROVED"]), 0
        )
        self.assertEqual(restarted2.status, "already_published")
        self.assertEqual(restarted2.diagnostic, "human_adjudication_open")

        # 1b-bis. Lost response where GitHub DID persist the review -> reconciles & reuses it.
        dir_rec = tempfile.TemporaryDirectory()
        self.addCleanup(dir_rec.cleanup)
        ledger_rec = LocalSessionLedger(Path(dir_rec.name) / "ledger")
        tx_rec = prepare_review_transaction(
            ledger_rec,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind="res-reconcile",
            ),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=cfg_digest,
            evidence_digest=ev_digest,
        )
        baseline_rec = ReviewBaseline(
            cache_key=key_clean,
            policy_digest=POLICY.digest(),
            complete=True,
            coverage_complete=True,
            generation=tx_rec.record.generation + 1,
            findings=(),
            reviewed_paths=("src/app.py",),
        )
        retained_rec = checkpoint_review_analysis(
            ledger_rec, IDENTITY, tx_rec, clean_result, baseline=baseline_rec
        )
        state_rec = _SimulatedGitHub()
        state_rec.lose_response_recorded_502 = True
        reconciled_pub = _make_app(state_rec, ledger_rec).publish_review(
            options=GitHubWriteOptions(
                github_writes=True, auto_review=True, auto_approve=False
            ),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            base_branch="main",
            base_sha=BASE,
            result=retained_rec,
            diff=SMALL_DIFF,
            app_slug=APP,
            convergence_policy=POLICY,
            current_key=_make_cache_key(HEAD),
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
        )
        self.assertEqual(reconciled_pub.status, "already_published")
        self.assertIsNotNone(reconciled_pub.next_action_guidance)
        self.assertEqual(
            reconciled_pub.next_action_guidance.state, "response_loss_reconciled"
        )
        self.assertEqual(len(state_rec.reviews), 1)

        # 1c. Unverifiable write (response lost + absent on GitHub): holds & names missing evidence.
        dir_unv = tempfile.TemporaryDirectory()
        self.addCleanup(dir_unv.cleanup)
        ledger_unv = LocalSessionLedger(Path(dir_unv.name) / "ledger")
        tx_unv = prepare_review_transaction(
            ledger_unv,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind="res-unv",
            ),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=cfg_digest,
            evidence_digest=ev_digest,
        )
        baseline_unv = ReviewBaseline(
            cache_key=key_clean,
            policy_digest=POLICY.digest(),
            complete=True,
            coverage_complete=True,
            generation=tx_unv.record.generation + 1,
            findings=(),
            reviewed_paths=("src/app.py",),
        )
        retained_unv = checkpoint_review_analysis(
            ledger_unv, IDENTITY, tx_unv, clean_result, baseline=baseline_unv
        )
        state_unverifiable = _SimulatedGitHub()
        state_unverifiable.fail_review_post_502 = True
        app_unv = _make_app(state_unverifiable, ledger_unv)
        with self.assertRaises(GitHubPublicationTransientError) as ctx_unv:
            app_unv.publish_review(
                options=GitHubWriteOptions(
                    github_writes=True, auto_review=True, auto_approve=True
                ),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_branch="main",
                base_sha=BASE,
                result=retained_unv,
                diff=SMALL_DIFF,
                app_slug=APP,
                convergence_policy=POLICY,
                current_key=_make_cache_key(HEAD),
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
            )
        unv_guidance = resolve_next_action_guidance(
            head_sha=HEAD,
            record=ledger_unv.load(IDENTITY).record,
            retained_result=retained_unv,
            response_loss_hold=app_unv.reviewer.response_loss_hold(
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
            ),
            error=ctx_unv.exception,
        )
        self.assertEqual(unv_guidance.state, "response_loss_unknown_hold")
        self.assertTrue(unv_guidance.missing_evidence)
        self.assertIsNone(unv_guidance.command)
        self.assertEqual(state_unverifiable.reviews, [])

        # 1d. Changed head: old commit evidence never qualifies new head.
        new_head = "c" * 40
        changed_guidance = resolve_next_action_guidance(
            head_sha=new_head,
            record=fresh_ledger.load(IDENTITY).record,
            retained_result=restored_result,
        )
        self.assertEqual(changed_guidance.state, "changed_head")
        self.assertEqual(
            changed_guidance.command, f"@reviewsensei rescan commit {new_head}"
        )

        # 1e. Exhausted budget (failed-attempt ceiling + API/evidence read budget): preserves budget.
        dir3 = tempfile.TemporaryDirectory()
        self.addCleanup(dir3.cleanup)
        ledger3 = LocalSessionLedger(Path(dir3.name) / "ledger")
        for idx in range(DEFAULT_MAX_FAILED_ATTEMPTS):
            res_id = session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind=f"res-budget-{idx}",
            )
            prepare_review_transaction(
                ledger3,
                IDENTITY,
                POLICY,
                reservation_id=res_id,
                base_sha=BASE,
                head_sha=HEAD,
                configuration_digest=cfg_digest,
                evidence_digest=ev_digest,
            )
            rec3 = record_session_failed_attempt(
                ledger3, IDENTITY, reservation_id=res_id, head_sha=HEAD
            )
        exhausted_guidance = resolve_next_action_guidance(
            head_sha=HEAD,
            record=rec3,
            retained_result=None,
        )
        self.assertEqual(exhausted_guidance.state, "budget_exhausted")
        self.assertEqual(exhausted_guidance.command, "@reviewsensei review continue")
        # Guidance read must leave durable failed-attempt accounting completely intact.
        self.assertEqual(
            ledger3.load(IDENTITY).record.failed_attempts, DEFAULT_MAX_FAILED_ATTEMPTS
        )
        evidence_exhausted_guidance = resolve_next_action_guidance(
            head_sha=HEAD,
            error=ReviewInputError("partition shared request budget exhausted"),
        )
        self.assertEqual(evidence_exhausted_guidance.state, "budget_exhausted")
        self.assertIsNone(evidence_exhausted_guidance.command)
        self.assertIn("preserved", evidence_exhausted_guidance.blocker or "")
        self.assertIn("preserve", evidence_exhausted_guidance.next_action)

    def test_transient_403_and_error_classification_boundaries(self):
        # Transient / rate-limit 403 must never be misclassified as a static permission 403
        # even if its __cause__ mentions "HTTP 403".
        try:
            try:
                raise RuntimeError("upstream returned HTTP 403 primary rate limit")
            except RuntimeError as inner:
                raise GitHubHTTPTransientError(
                    "GitHub rate limit exceeded (HTTP 403)"
                ) from inner
        except GitHubHTTPTransientError as rate_exc:
            self.assertIsNone(extract_forbidden_lookup(error=rate_exc))
            rate_guidance = resolve_next_action_guidance(head_sha=HEAD, error=rate_exc)
            self.assertNotEqual(rate_guidance.state, "lookup_forbidden_hold")

        # Generic substrings containing "caller api" or "allowance" must not trigger budget_exhausted.
        non_budget = ReviewInputError("caller API version mismatch; no allowance field")
        non_budget_guidance = resolve_next_action_guidance(
            head_sha=HEAD, error=non_budget
        )
        self.assertEqual(non_budget_guidance.state, "publication_failed_hold")

        # Explicit caller API budget exhaustion IS classified as budget_exhausted.
        caller_budget = ReviewInputError("caller API request budget exhausted")
        self.assertEqual(
            resolve_next_action_guidance(head_sha=HEAD, error=caller_budget).state,
            "budget_exhausted",
        )

        # Empty/whitespace next_action_text normalizes to None; small max_bytes never overflows.
        view = ReviewSummaryView(
            state=NO_REQUIRED_FIXES_STATE, head_sha=HEAD, next_action_text="   "
        )
        self.assertIsNone(view.next_action_text)
        for limit in (0, 1, 2, 3, 4, 8):
            truncated = _clean_text("éclairabcdef", max_bytes=limit)
            self.assertLessEqual(len(truncated.encode("utf-8")), limit)
