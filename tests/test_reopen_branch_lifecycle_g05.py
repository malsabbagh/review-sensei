"""G05 qualification: PR close/reopen and deleted/recreated branch lifecycle."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

try:
    from fake_github_http import json_response
    from test_reply_freshness_g33 import FreshnessReplyFixture
    from test_review_run import IDENTITY, EntryFixture
except ModuleNotFoundError:
    from tests.fake_github_http import json_response
    from tests.test_reply_freshness_g33 import FreshnessReplyFixture
    from tests.test_review_run import IDENTITY, EntryFixture

from review_sensei_github import branch_lifecycle
from review_sensei_github.branch_lifecycle import (
    PullRequestLifecycleSnapshot,
    classify_reopen_branch_transition,
    evaluate_finalizer_pr_gate,
    evaluate_live_pr_write_gate,
    session_has_unresolved_obligations,
)
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewApprovalFinalizer, ReviewPublisher
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)

from review_sensei.models import ReviewResult
from review_sensei.session import SessionIdentity, failed_attempts_for_head

BASE_SHA = "b" * 40
HEAD_SHA_1 = "a" * 40
HEAD_SHA_2 = "d" * 40
SAMPLE_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1 +1,2 @@\n"
    " keep\n"
    "+change\n"
)


def _sample_pr_payload(
    *,
    number: int = 7,
    state: str = "open",
    draft: object = False,
    base_ref: str = "main",
    base_sha: str = BASE_SHA,
    head_ref: str = "feature",
    head_sha: str = HEAD_SHA_1,
    head_repo_missing: bool = False,
    head_fork: bool = False,
    base_fork: bool = False,
    repository: str = "owner/repo",
    repository_id: int = 123,
    author_login: str = "alice",
) -> dict[str, object]:
    head_repo: object
    if head_repo_missing:
        head_repo = None
    else:
        head_repo = {
            "id": repository_id,
            "full_name": repository,
            "fork": head_fork,
        }
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "title": "Synthetic PR",
        "user": {"login": author_login, "id": 1001, "type": "User"},
        "head": {
            "ref": head_ref,
            "sha": head_sha,
            "repo": head_repo,
        },
        "base": {
            "ref": base_ref,
            "sha": base_sha,
            "repo": {
                "id": repository_id,
                "full_name": repository,
                "fork": base_fork,
            },
        },
    }


class LifecycleEntryFixture(EntryFixture):
    """Stateful EntryFixture supporting close/reopen, head deletion, and PR #8."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.pr_state = "open"
        self.pr_draft: object = False
        self.head_ref = "feature"
        self.base_ref = "main"
        self.head_repo_deleted = False
        self.review_posts: list[dict[str, object]] = []

    def advance_run_same_snapshot(self) -> None:
        self.run_id += 1
        self.trace.clear()
        self.transports.clear()
        self.worker_observations.clear()

    def advance_run_to_head(self, new_head: str) -> None:
        self.run_id += 1
        self.live_head = self.requested_head = new_head
        self.trace.clear()
        self.transports.clear()
        self.worker_observations.clear()

    def run_and_read(
        self, *, human: bool = False
    ) -> tuple[int, dict[str, object] | None]:
        out_file = self.root / "publication.json"
        out_file.unlink(missing_ok=True)
        code = self.invoke(human=human)
        if not out_file.exists():
            return code, None
        return code, json.loads(out_file.read_text(encoding="utf-8"))

    def retained_finding_obligations(self):
        """Inspect the actual source-produced inline carrier, without new HTTP.

        This is a test observation of the authenticated writer output, not an
        alternate runtime authority loader or another caller budget.
        """
        from review_sensei_github.finding_lifecycle import _unpack_journal

        from review_sensei.bounded_evidence import read_inline_evidence
        from review_sensei.finding_lifecycle import decode_lifecycle_state

        record = parse_session_comment(self.comment["body"], identity=IDENTITY)
        manifest = record.finding_lifecycle["manifest"]
        envelope = read_inline_evidence(manifest, expected_binding=manifest["binding"])
        journal = _unpack_journal(envelope["lifecycle"])
        return decode_lifecycle_state(journal["lifecycle"]).state.inventory.obligations

    def assert_preserved_omitted_obligations(self, testcase, previous):
        current = {
            item.obligation_id: item for item in self.retained_finding_obligations()
        }
        testcase.assertTrue(previous)
        for prior in previous:
            retained = current[prior.obligation_id]
            testcase.assertEqual(retained.criterion, prior.criterion)
            testcase.assertEqual(retained.lifecycle_state, "omitted-uncertain")
            testcase.assertTrue(retained.unresolved)

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if path.endswith("/pulls/7/reviews") and method == "POST":
            self.review_posts.append(json.loads(request.data))
        response = super().external(request, timeout)
        if path.endswith("/pulls/7") and method == "GET":
            if response.status != 200:
                raise AssertionError(
                    f"expected 200 OK for GET {path}, got {response.status}"
                )
            pull = json.loads(response.read(524288))
            if not isinstance(pull, dict) or pull.get("number") != 7:
                raise AssertionError(f"unexpected PR #7 payload: {pull!r}")
            pull["state"] = self.pr_state
            pull["draft"] = self.pr_draft
            pull["head"]["ref"] = self.head_ref
            pull["base"]["ref"] = self.base_ref
            if self.head_repo_deleted:
                pull["head"]["repo"] = None
            return json_response(pull)
        return response


class ReopenBranchLifecycleG05Tests(unittest.TestCase):
    """Stateful G05 qualification for close/reopen and branch delete/recreate."""

    def test_unresolved_finding_close_blocks_writes_and_reopen_same_sha_recovers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            # 1. Initial open review publishes an unresolved finding and withholds approval.
            code, out = fixture.run_and_read(human=True)
            self.assertEqual(code, 0)
            self.assertEqual(out["status"], "published")
            self.assertTrue(out["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual([r["event"] for r in fixture.review_posts], ["COMMENT"])

            record_open = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_open))
            open_snapshot = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(head_sha=fixture.live_head)
            )

            # 2. Close PR #7: newly admitted runs and pre-effect write gates block,
            # while already-dispatched review state and session obligations remain intact.
            fixture.pr_state = "closed"
            closed_snapshot = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="closed", head_sha=fixture.live_head)
            )
            closed_decision = classify_reopen_branch_transition(
                open_snapshot,
                closed_snapshot,
                prior_record=record_open,
            )
            self.assertEqual(closed_decision.transition_kind, "closed_blocking_writes")
            self.assertFalse(closed_decision.admits_live_writes)
            self.assertTrue(closed_decision.preserves_prior_obligations)
            self.assertTrue(closed_decision.unresolved_obligations_present)
            self.assertFalse(closed_decision.revokes_in_flight_dispatches)
            self.assertEqual(
                closed_decision.recovery_action, "await_reopen_preserve_ledger"
            )

            # Startup gate rejects new review execution while closed with zero provider calls.
            fixture.advance_run_same_snapshot()
            code_closed, out_closed = fixture.run_and_read(human=True)
            self.assertEqual(code_closed, 1)
            self.assertIsNone(out_closed)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(len(fixture.review_posts), 1)

            # Direct publisher and finalizer pre-effect checks also skip cleanly while closed.
            publisher = ReviewPublisher(http=fixture.http)
            closed_pub = publisher.publish(
                token="installation-token",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                head_sha=fixture.live_head,
                base_branch="main",
                base_sha=fixture.live_base,
                diff=SAMPLE_DIFF,
                result=ReviewResult(
                    summary="Blocked while closed.",
                    comments=(),
                    provider="ollama",
                    review_status="complete",
                ),
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(closed_pub.status, "skipped_pr_state")
            finalizer = ReviewApprovalFinalizer(http=fixture.http)
            closed_fin = finalizer._preflight(
                token="installation-token",
                repository="owner/repo",
                pull_request=7,
                head_sha=fixture.live_head,
                app_slug="reviewsensei[bot]",
            )
            assert closed_fin.result is not None
            self.assertEqual(closed_fin.result.status, "skipped_pr_state")
            # Already-dispatched review from step 1 was NOT revoked by closing.
            self.assertEqual(len(fixture.review_posts), 1)

            # 3. Reopen PR #7 at the exact same SHA: obligations remain active,
            # zero fresh failed-attempt allowance is minted, and zero fresh inference runs.
            fixture.pr_state = "open"
            reopened_snapshot = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=fixture.live_head)
            )
            reopen_decision = classify_reopen_branch_transition(
                closed_snapshot,
                reopened_snapshot,
                prior_record=record_open,
            )
            self.assertEqual(reopen_decision.transition_kind, "reopened_same_sha")
            self.assertTrue(reopen_decision.shares_session_identity)
            self.assertTrue(reopen_decision.admits_live_writes)
            self.assertTrue(reopen_decision.preserves_prior_obligations)
            self.assertTrue(reopen_decision.unresolved_obligations_present)
            self.assertFalse(reopen_decision.mints_fresh_failed_attempt_allowance)
            self.assertFalse(reopen_decision.prior_approval_valid)
            self.assertEqual(
                reopen_decision.recovery_action,
                "reuse_completed_or_retained_snapshot",
            )

            fixture.advance_run_same_snapshot()
            code_reopen, out_reopen = fixture.run_and_read(human=True)
            self.assertEqual(code_reopen, 0)
            self.assertEqual(out_reopen["status"], "already_published")
            self.assertTrue(out_reopen["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 1)
            # Still only the single COMMENT review; no spurious APPROVE was emitted.
            self.assertEqual([r["event"] for r in fixture.review_posts], ["COMMENT"])
            record_after_reopen = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_after_reopen))

    def test_same_sha_reopen_mints_zero_fresh_failed_attempt_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            code, _ = fixture.run_and_read(human=True)
            self.assertEqual(code, 0)
            fixture.rewrite_record(
                mutate=lambda rec: rec.evolve(
                    failed_attempts=2,
                    failed_attempts_head_sha=fixture.live_head,
                    generation=rec.generation + 1,
                )
            )
            persisted = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertEqual(failed_attempts_for_head(persisted, fixture.live_head), 2)

            closed = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="closed", head_sha=fixture.live_head)
            )
            reopened = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=fixture.live_head)
            )
            decision = classify_reopen_branch_transition(
                closed,
                reopened,
                prior_record=persisted,
            )
            self.assertEqual(decision.transition_kind, "reopened_same_sha")
            self.assertEqual(decision.effective_failed_attempts, 2)
            self.assertFalse(decision.mints_fresh_failed_attempt_allowance)

            # Drive close -> reopen through the live CLI path and verify the
            # persisted session ledger preserves failed_attempts=2 on the same SHA.
            fixture.pr_state = "closed"
            fixture.advance_run_same_snapshot()
            code_closed, out_closed = fixture.run_and_read(human=True)
            self.assertEqual(code_closed, 1)
            self.assertIsNone(out_closed)

            fixture.pr_state = "open"
            fixture.advance_run_same_snapshot()
            code_reopen, out_reopen = fixture.run_and_read(human=True)
            self.assertEqual(code_reopen, 0)
            assert out_reopen is not None
            self.assertEqual(out_reopen["status"], "already_published")
            after_reopen = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            assert after_reopen is not None
            self.assertEqual(
                failed_attempts_for_head(after_reopen, fixture.live_head), 2
            )
            self.assertEqual(after_reopen.failed_attempts_head_sha, fixture.live_head)

    def test_reopen_with_changed_sha_invalidates_stale_approval_and_recovers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            # 1. Clean review at H1 publishes COMMENT + APPROVE.
            code_h1, out_h1 = fixture.run_and_read(human=False)
            self.assertEqual(code_h1, 0)
            self.assertEqual(out_h1["status"], "published")
            self.assertEqual(
                [r["event"] for r in fixture.review_posts], ["COMMENT", "APPROVE"]
            )
            record_h1 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )

            # 2. PR closes and head advances to H2 while closed, then reopens at H2.
            closed_h1 = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="closed", head_sha=HEAD_SHA_1)
            )
            reopened_h2 = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=HEAD_SHA_2)
            )
            decision = classify_reopen_branch_transition(
                closed_h1,
                reopened_h2,
                prior_record=record_h1,
                prior_approved_binding=(BASE_SHA, HEAD_SHA_1),
            )
            self.assertEqual(decision.transition_kind, "reopened_changed_sha")
            self.assertFalse(decision.prior_approval_valid)
            self.assertEqual(
                decision.recovery_action,
                "rebind_obligations_and_verify_new_snapshot",
            )

            # Reopened run on H2 does NOT treat H1's approval as valid for H2:
            # it runs analysis for H2 and publishes fresh H2-bound review + approval.
            fixture.advance_run_to_head(HEAD_SHA_2)
            code_h2, out_h2 = fixture.run_and_read(human=False)
            self.assertEqual(code_h2, 0)
            self.assertEqual(out_h2["status"], "published")
            self.assertTrue(out_h2["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 2)
            self.assertEqual(
                [r["event"] for r in fixture.review_posts],
                ["COMMENT", "APPROVE", "COMMENT", "APPROVE"],
            )
            self.assertEqual(fixture.review_posts[-1]["commit_id"], HEAD_SHA_2)

    def test_deleted_and_recreated_branch_same_sha_vs_changed_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            code_initial, out_initial = fixture.run_and_read(human=True)
            self.assertEqual(code_initial, 0)
            self.assertEqual(out_initial["status"], "published")
            self.assertEqual(fixture.provider_calls, 1)
            record_initial = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )

            # Head branch deleted -> GitHub sets head.repo = None (or branch ref absent).
            fixture.head_repo_deleted = True
            deleted_payload = _sample_pr_payload(
                state="open",
                head_sha=HEAD_SHA_1,
                head_repo_missing=True,
            )
            deleted_snapshot = PullRequestLifecycleSnapshot.from_pull_request_payload(
                deleted_payload,
                branch_ref_exists=False,
            )
            open_snapshot = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=HEAD_SHA_1)
            )
            deleted_decision = classify_reopen_branch_transition(
                open_snapshot,
                deleted_snapshot,
                prior_record=record_initial,
            )
            self.assertEqual(
                deleted_decision.transition_kind,
                "head_branch_deleted_blocking_writes",
            )
            self.assertFalse(deleted_decision.admits_live_writes)
            self.assertTrue(deleted_decision.preserves_prior_obligations)

            # Live gate and ReviewPublisher._preflight_pr both refuse head.repo=None
            # cleanly, including through the fixture's mutated HTTP response path.
            with self.assertRaises(GitHubPublicationError):
                evaluate_live_pr_write_gate(
                    deleted_payload,
                    repository="owner/repo",
                    repository_id=123,
                    head_sha=HEAD_SHA_1,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    app_slug="reviewsensei[bot]",
                )
            with self.assertRaises(GitHubPublicationError):
                ReviewPublisher(http=fixture.http)._preflight_pr(
                    token="installation-token",
                    repository="owner/repo",
                    repository_id=123,
                    pull_request=7,
                    head_sha=fixture.live_head,
                    base_branch="main",
                    base_sha=fixture.live_base,
                    app_slug="reviewsensei[bot]",
                )

            # Live ReviewRun startup refuses deleted head.repo without AttributeError
            # and makes zero provider calls.
            fixture.advance_run_same_snapshot()
            code_del, out_del = fixture.run_and_read(human=True)
            self.assertEqual(code_del, 1)
            self.assertIsNone(out_del)
            self.assertEqual(fixture.provider_calls, 1)

            # Case A: Recreate branch `feature` with the EXACT SAME commit SHA H1.
            fixture.head_repo_deleted = False
            recreated_same = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(
                    state="open", head_ref="feature", head_sha=HEAD_SHA_1
                )
            )
            same_decision = classify_reopen_branch_transition(
                deleted_snapshot,
                recreated_same,
                prior_record=record_initial,
            )
            self.assertEqual(same_decision.transition_kind, "branch_recreated_same_sha")
            self.assertTrue(same_decision.preserves_prior_obligations)
            self.assertFalse(same_decision.mints_fresh_failed_attempt_allowance)
            self.assertEqual(
                same_decision.recovery_action,
                "reuse_completed_or_retained_snapshot",
            )

            fixture.advance_run_same_snapshot()
            code_same, out_same = fixture.run_and_read(human=True)
            self.assertEqual(code_same, 0)
            self.assertEqual(out_same["status"], "already_published")
            self.assertEqual(fixture.provider_calls, 1)

            # Case B: Delete and recreate branch `feature` at a DIFFERENT commit SHA H2.
            # Because PR #7 had an unresolved baseline obligation from H1, omission on H2
            # is NOT treated as a silent wipe: H2 requests changes for retained uncertainty,
            # withholds spurious APPROVE, and closes caller accounting terminally.
            recreated_changed = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(
                    state="open", head_ref="feature", head_sha=HEAD_SHA_2
                )
            )
            changed_decision = classify_reopen_branch_transition(
                deleted_snapshot,
                recreated_changed,
                prior_record=record_initial,
                prior_approved_binding=(BASE_SHA, HEAD_SHA_1),
            )
            self.assertEqual(
                changed_decision.transition_kind, "branch_recreated_changed_sha"
            )
            self.assertTrue(changed_decision.preserves_prior_obligations)
            self.assertFalse(changed_decision.prior_approval_valid)
            self.assertEqual(
                changed_decision.recovery_action,
                "rebind_obligations_and_verify_new_snapshot",
            )

            original_obligations = fixture.retained_finding_obligations()
            fixture.advance_run_to_head(HEAD_SHA_2)
            code_changed, out_changed = fixture.run_and_read(human=False)
            self.assertEqual(code_changed, 0)
            assert out_changed is not None
            self.assertEqual(out_changed["status"], "published")
            self.assertTrue(out_changed["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 2)
            self.assertEqual(fixture.review_posts[-1]["event"], "REQUEST_CHANGES")
            fixture.assert_preserved_omitted_obligations(self, original_obligations)
            self.assertEqual(fixture.review_posts[-1]["commit_id"], HEAD_SHA_2)
            record_h2 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_h2))

    def test_distinct_pull_request_on_recreated_branch_is_completely_isolated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            # PR #7 on branch `feature` has an unresolved concern and session ledger.
            code_pr7, _ = fixture.run_and_read(human=True)
            self.assertEqual(code_pr7, 0)
            record_pr7 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )

            pr7_closed = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(
                    number=7,
                    state="closed",
                    head_ref="feature",
                    head_sha=HEAD_SHA_1,
                    head_repo_missing=True,
                ),
                branch_ref_exists=False,
            )
            pr8_opened = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(
                    number=8,
                    state="open",
                    head_ref="feature",
                    head_sha=HEAD_SHA_1,
                )
            )
            decision = classify_reopen_branch_transition(
                pr7_closed,
                pr8_opened,
                prior_record=record_pr7,
                prior_approved_binding=(BASE_SHA, HEAD_SHA_1),
            )
            self.assertEqual(
                decision.transition_kind,
                "distinct_pull_request_on_recreated_branch",
            )
            self.assertFalse(decision.shares_session_identity)
            self.assertFalse(decision.preserves_prior_obligations)
            self.assertFalse(decision.prior_approval_valid)
            self.assertEqual(
                decision.recovery_action, "isolated_new_pull_request_session"
            )
            self.assertEqual(pr7_closed.session_identity.pull_request, 7)
            self.assertEqual(pr8_opened.session_identity.pull_request, 8)
            self.assertNotEqual(
                pr7_closed.session_identity, pr8_opened.session_identity
            )

            # Distinct PR on a different branch (or while predecessor branch was
            # never closed/deleted) uses generic `distinct_pull_request`.
            pr8_other_branch = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(
                    number=8,
                    state="open",
                    head_ref="other-feature",
                    head_sha=HEAD_SHA_1,
                )
            )
            other_branch_decision = classify_reopen_branch_transition(
                pr7_closed,
                pr8_other_branch,
                prior_record=record_pr7,
            )
            self.assertEqual(
                other_branch_decision.transition_kind, "distinct_pull_request"
            )
            self.assertFalse(other_branch_decision.shares_session_identity)
            self.assertEqual(
                other_branch_decision.recovery_action,
                "isolated_new_pull_request_session",
            )

            # Even if PR #7's session comment body were inspected under PR #8's
            # identity, numeric PR binding rejects it (`None`).
            self.assertIsNone(
                parse_session_comment(
                    fixture.comment["body"], identity=pr8_opened.session_identity
                )
            )

            pr8_comments: list[dict[str, object]] = []
            pr8_reviews: list[dict[str, object]] = []
            pr8_paths: list[tuple[str, str]] = []
            pr7_trace_snapshot = list(fixture.trace)

            def pr8_opener(request, timeout=None):
                path = urlsplit(request.full_url).path
                method = request.get_method()
                pr8_paths.append((method, path))
                if path.endswith("/pulls/8") and method == "GET":
                    return json_response(
                        _sample_pr_payload(
                            number=8,
                            state="open",
                            draft=False,
                            base_ref="main",
                            base_sha=BASE_SHA,
                            head_ref="feature",
                            head_sha=HEAD_SHA_1,
                        )
                    )
                if path.endswith("/issues/8/comments") and method == "GET":
                    return json_response(list(pr8_comments))
                if path.endswith("/issues/8/comments") and method == "POST":
                    payload = json.loads(request.data)
                    created_comment = {
                        "id": 9000 + len(pr8_comments) + 1,
                        "issue_url": "https://api.github.test/repos/owner/repo/issues/8",
                        "body": payload.get("body", ""),
                        "user": {
                            "id": 999,
                            "login": "reviewsensei[bot]",
                            "type": "Bot",
                        },
                    }
                    pr8_comments.append(created_comment)
                    return json_response(created_comment, status=201)
                if path.endswith("/pulls/8/reviews") and method == "GET":
                    return json_response(list(pr8_reviews))
                if path.endswith("/pulls/8/reviews") and method == "POST":
                    payload = json.loads(request.data)
                    created = {
                        "id": 8000 + len(pr8_reviews) + 1,
                        "state": "APPROVED"
                        if payload.get("event") == "APPROVE"
                        else "COMMENTED",
                        "body": payload.get("body", ""),
                        "commit_id": payload.get("commit_id", HEAD_SHA_1),
                        "submitted_at": "2026-11-11T00:00:00Z",
                        "user": {
                            "id": 999,
                            "login": "reviewsensei[bot]",
                            "type": "Bot",
                        },
                    }
                    pr8_reviews.append(created)
                    return json_response(created, status=200)
                if path.endswith("/pulls/8/comments") and method == "GET":
                    return json_response([])
                if path == "/graphql" and method == "POST":
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
                raise AssertionError(f"unexpected PR #8 request: {method} {path}")

            pr8_http = GitHubHttp(api_url="https://api.github.test", opener=pr8_opener)
            ledger = GitHubIssueCommentSessionLedger(
                http=pr8_http,
                token="installation-token",
                app_slug="reviewsensei[bot]",
            )
            loaded_pr8 = ledger.load(pr8_opened.session_identity)
            self.assertEqual(loaded_pr8.status, "missing")
            self.assertIsNone(loaded_pr8.record)

            # Publishing on PR #8 writes exclusively to PR #8 without reusing PR #7 markers
            # and without issuing any request against PR #7's endpoints.
            publisher = ReviewPublisher(http=pr8_http)
            pub_pr8 = publisher.publish(
                token="installation-token",
                repository="owner/repo",
                repository_id=123,
                pull_request=8,
                head_sha=HEAD_SHA_1,
                base_branch="main",
                base_sha=BASE_SHA,
                diff=SAMPLE_DIFF,
                result=ReviewResult(
                    summary="Clean PR #8 review.",
                    comments=(),
                    provider="ollama",
                    review_status="complete",
                ),
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(pub_pr8.status, "published")
            self.assertEqual(len(pr8_reviews), 2)
            self.assertIn(("POST", "/repos/owner/repo/pulls/8/reviews"), pr8_paths)
            self.assertTrue(
                all("/pulls/7" not in p and "/issues/7" not in p for _, p in pr8_paths)
            )
            self.assertEqual(fixture.trace, pr7_trace_snapshot)
            # PR #7 ledger remains untouched and still holds its unresolved obligation.
            still_pr7 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertEqual(still_pr7.generation, record_pr7.generation)
            self.assertTrue(session_has_unresolved_obligations(still_pr7))

    def test_duplicate_and_reordered_reopen_deliveries_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            code_1, out_1 = fixture.run_and_read(human=False)
            self.assertEqual(code_1, 0)
            self.assertEqual(out_1["status"], "published")
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(len(fixture.review_posts), 2)

            # Deliver intervening close event through the live CLI path, then reopen
            # and deliver two duplicate open runs followed by a real SHA advance.
            fixture.pr_state = "closed"
            fixture.advance_run_same_snapshot()
            code_closed, out_closed = fixture.run_and_read(human=False)
            self.assertEqual(code_closed, 1)
            self.assertIsNone(out_closed)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(len(fixture.review_posts), 2)

            fixture.pr_state = "open"
            for _ in range(2):
                fixture.advance_run_same_snapshot()
                code_dup, out_dup = fixture.run_and_read(human=False)
                self.assertEqual(code_dup, 0)
                assert out_dup is not None
                self.assertEqual(out_dup["status"], "already_published")
                self.assertTrue(out_dup["accounting_terminal"])
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(len(fixture.review_posts), 2)

            # Subsequent real state transition (new head SHA) triggers fresh evaluation.
            fixture.advance_run_to_head(HEAD_SHA_2)
            code_next, out_next = fixture.run_and_read(human=False)
            self.assertEqual(code_next, 0)
            assert out_next is not None
            self.assertEqual(out_next["status"], "published")
            self.assertEqual(fixture.provider_calls, 2)
            self.assertEqual(len(fixture.review_posts), 4)

            s_open = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=HEAD_SHA_1)
            )
            dup_decision = classify_reopen_branch_transition(
                s_open,
                s_open,
                prior_approved_binding=(BASE_SHA, HEAD_SHA_1),
            )
            self.assertEqual(dup_decision.transition_kind, "duplicate_open_delivery")
            self.assertTrue(dup_decision.prior_approval_valid)
            changed_open = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=HEAD_SHA_2)
            )
            open_advance = classify_reopen_branch_transition(s_open, changed_open)
            self.assertEqual(open_advance.transition_kind, "open_head_or_base_changed")

    def test_snapshot_and_write_gate_validation_edges(self) -> None:
        base_payload = _sample_pr_payload()
        snap = PullRequestLifecycleSnapshot.from_pull_request_payload(base_payload)
        self.assertTrue(snap.admits_live_writes)
        self.assertEqual(
            snap.session_identity,
            SessionIdentity("owner/repo", 7, repository_id=123),
        )

        # G24 malformed draft values fail closed in both helpers.
        for bad_draft in (None, 0, 1, "false", "true"):
            bad = _sample_pr_payload(draft=bad_draft)
            with self.assertRaisesRegex(
                GitHubPublicationError, "review preflight draft was invalid"
            ):
                evaluate_live_pr_write_gate(
                    bad,
                    repository="owner/repo",
                    repository_id=123,
                    head_sha=HEAD_SHA_1,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    app_slug="reviewsensei[bot]",
                )
            with self.assertRaisesRegex(
                GitHubPublicationError, "review preflight draft was invalid"
            ):
                PullRequestLifecycleSnapshot.from_pull_request_payload(bad)

        missing_draft = copy.deepcopy(base_payload)
        del missing_draft["draft"]
        with self.assertRaisesRegex(
            GitHubPublicationError, "review preflight draft was invalid"
        ):
            evaluate_live_pr_write_gate(
                missing_draft,
                repository="owner/repo",
                repository_id=123,
                head_sha=HEAD_SHA_1,
                base_branch="main",
                base_sha=BASE_SHA,
                app_slug="reviewsensei[bot]",
            )

        # Draft True skips cleanly with reason pr_draft.
        draft_decision = evaluate_live_pr_write_gate(
            _sample_pr_payload(draft=True),
            repository="owner/repo",
            repository_id=123,
            head_sha=HEAD_SHA_1,
            base_branch="main",
            base_sha=BASE_SHA,
            app_slug="reviewsensei[bot]",
        )
        self.assertFalse(draft_decision.admitted)
        self.assertEqual(draft_decision.skip_status, "skipped_pr_state")
        self.assertEqual(draft_decision.reason, "pr_draft")

        # Stale head, fork, repo mismatch, stale base SHA, stale base ref, and admitted.
        self.assertEqual(
            evaluate_live_pr_write_gate(
                base_payload,
                repository="owner/repo",
                repository_id=123,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                app_slug="reviewsensei[bot]",
            ).reason,
            "stale_head_sha",
        )
        self.assertEqual(
            evaluate_live_pr_write_gate(
                _sample_pr_payload(head_fork=True),
                repository="owner/repo",
                repository_id=123,
                head_sha=HEAD_SHA_1,
                base_branch="main",
                base_sha=BASE_SHA,
                app_slug="reviewsensei[bot]",
            ).skip_status,
            "skipped_fork",
        )
        self.assertEqual(
            evaluate_live_pr_write_gate(
                base_payload,
                repository="owner/repo",
                repository_id=999,
                head_sha=HEAD_SHA_1,
                base_branch="main",
                base_sha=BASE_SHA,
                app_slug="reviewsensei[bot]",
            ).skip_status,
            "skipped_repository_mismatch",
        )
        self.assertEqual(
            evaluate_live_pr_write_gate(
                _sample_pr_payload(base_fork=True),
                repository="owner/repo",
                repository_id=123,
                head_sha=HEAD_SHA_1,
                base_branch="main",
                base_sha=BASE_SHA,
                app_slug="reviewsensei[bot]",
            ).reason,
            "base_repository_fork",
        )
        self.assertEqual(
            evaluate_live_pr_write_gate(
                base_payload,
                repository="owner/repo",
                repository_id=123,
                head_sha=HEAD_SHA_1,
                base_branch="main",
                base_sha="c" * 40,
                app_slug="reviewsensei[bot]",
            ).reason,
            "stale_base_sha",
        )
        self.assertEqual(
            evaluate_live_pr_write_gate(
                base_payload,
                repository="owner/repo",
                repository_id=123,
                head_sha=HEAD_SHA_1,
                base_branch="release/1.0",
                base_sha=BASE_SHA,
                app_slug="reviewsensei[bot]",
            ).reason,
            "stale_base_ref",
        )
        admitted = evaluate_live_pr_write_gate(
            _sample_pr_payload(author_login="reviewsensei[bot]"),
            repository="owner/repo",
            repository_id=123,
            head_sha=HEAD_SHA_1,
            base_branch="main",
            base_sha=BASE_SHA,
            app_slug="reviewsensei[bot]",
        )
        self.assertTrue(admitted.admitted)
        self.assertTrue(admitted.app_authored)

        other_repo = PullRequestLifecycleSnapshot.from_pull_request_payload(
            _sample_pr_payload(repository="other/repo", repository_id=456)
        )
        with self.assertRaisesRegex(
            GitHubPublicationError, "lifecycle snapshots span different repositories"
        ):
            classify_reopen_branch_transition(snap, other_repo)

        # Malformed live-write-gate and snapshot payload rejection coverage.
        gate_kwargs = {
            "repository": "owner/repo",
            "repository_id": 123,
            "head_sha": HEAD_SHA_1,
            "base_branch": "main",
            "base_sha": BASE_SHA,
            "app_slug": "reviewsensei[bot]",
        }
        with self.assertRaisesRegex(
            GitHubPublicationError, "review preflight response was invalid"
        ):
            evaluate_live_pr_write_gate("not-a-dict", **gate_kwargs)
        for mutate_gate, expected in (
            (lambda p: p.update(head=None), "review preflight head was invalid"),
            (
                lambda p: p["head"]["repo"].update(fork="no"),
                "review preflight head repository was invalid",
            ),
            (lambda p: p.update(base=None), "review preflight base was invalid"),
            (
                lambda p: p["base"].update(repo=None),
                "review preflight repository was invalid",
            ),
            (
                lambda p: p["base"]["repo"].update(id=0),
                "review preflight repository was invalid",
            ),
            (
                lambda p: p["base"]["repo"].update(fork="no"),
                "review preflight repository was invalid",
            ),
            (
                lambda p: p["base"].update(sha="bad-sha"),
                "review preflight base binding was invalid",
            ),
            (lambda p: p.update(user=None), "review preflight author was invalid"),
            (
                lambda p: p["user"].update(login=" "),
                "review preflight author was invalid",
            ),
        ):
            bad_gate = copy.deepcopy(base_payload)
            mutate_gate(bad_gate)
            with self.assertRaisesRegex(GitHubPublicationError, expected):
                evaluate_live_pr_write_gate(bad_gate, **gate_kwargs)

        with self.assertRaisesRegex(
            GitHubPublicationError, "pull request payload was invalid"
        ):
            PullRequestLifecycleSnapshot.from_pull_request_payload([])
        with self.assertRaisesRegex(
            GitHubPublicationError, "branch_ref_exists must be a boolean"
        ):
            PullRequestLifecycleSnapshot.from_pull_request_payload(
                base_payload,
                branch_ref_exists=1,  # type: ignore[arg-type]
            )
        for mutate_snap, expected in (
            (lambda p: p.update(number=0), "pull request number was invalid"),
            (lambda p: p.update(state="merged"), "pull request state was invalid"),
            (lambda p: p.update(head=None), "review preflight head was invalid"),
            (
                lambda p: p["head"].update(sha="not-hex"),
                "pull request head binding was invalid",
            ),
            (
                lambda p: p["head"]["repo"].update(fork="no"),
                "review preflight head repository was invalid",
            ),
            (
                lambda p: p["head"].update(repo="bad"),
                "review preflight head repository was invalid",
            ),
            (lambda p: p.update(base=None), "review preflight base was invalid"),
            (
                lambda p: p["base"].update(ref=""),
                "review preflight base binding was invalid",
            ),
            (
                lambda p: p["base"].update(repo=None),
                "review preflight repository was invalid",
            ),
            (
                lambda p: p["base"]["repo"].update(id=-1),
                "review preflight repository was invalid",
            ),
            (lambda p: p.update(user=None), "review preflight author was invalid"),
            (
                lambda p: p["user"].update(login=""),
                "review preflight author was invalid",
            ),
        ):
            bad_snap = copy.deepcopy(base_payload)
            mutate_snap(bad_snap)
            with self.assertRaisesRegex(GitHubPublicationError, expected):
                PullRequestLifecycleSnapshot.from_pull_request_payload(bad_snap)

    def test_transition_matrix_cold_start_base_ref_retarget_and_finalizer_gate(
        self,
    ) -> None:
        # Smoke check public module exports (no import cycle with publication.py).
        for export_name in branch_lifecycle.__all__:
            self.assertTrue(hasattr(branch_lifecycle, export_name), export_name)

        s_open = PullRequestLifecycleSnapshot.from_pull_request_payload(
            _sample_pr_payload(state="open", base_ref="main", head_sha=HEAD_SHA_1)
        )
        s_closed = PullRequestLifecycleSnapshot.from_pull_request_payload(
            _sample_pr_payload(state="closed", base_ref="main", head_sha=HEAD_SHA_1)
        )
        s_deleted = PullRequestLifecycleSnapshot.from_pull_request_payload(
            _sample_pr_payload(
                state="open",
                base_ref="main",
                head_sha=HEAD_SHA_1,
                head_repo_missing=True,
            ),
            branch_ref_exists=False,
        )

        # 1. Cold-start (prior_record=None) and closed -> closed cells.
        cold_reopen = classify_reopen_branch_transition(
            s_closed, s_open, prior_record=None
        )
        self.assertEqual(cold_reopen.transition_kind, "reopened_same_sha")
        self.assertFalse(cold_reopen.unresolved_obligations_present)
        self.assertEqual(cold_reopen.effective_failed_attempts, 0)
        self.assertFalse(cold_reopen.prior_approval_valid)

        closed_to_closed = classify_reopen_branch_transition(
            s_closed, s_closed, prior_record=None
        )
        self.assertEqual(closed_to_closed.transition_kind, "closed_blocking_writes")
        self.assertFalse(closed_to_closed.admits_live_writes)

        # 2. Reopen with changed base_sha only, and branch_recreated_* with no prior approval.
        s_open_new_base_sha = PullRequestLifecycleSnapshot.from_pull_request_payload(
            _sample_pr_payload(
                state="open", base_ref="main", base_sha="c" * 40, head_sha=HEAD_SHA_1
            )
        )
        reopen_base_drift = classify_reopen_branch_transition(
            s_closed,
            s_open_new_base_sha,
            prior_record=None,
            prior_approved_binding=(BASE_SHA, HEAD_SHA_1),
        )
        self.assertEqual(reopen_base_drift.transition_kind, "reopened_changed_sha")
        self.assertFalse(reopen_base_drift.prior_approval_valid)

        rec_no_binding = classify_reopen_branch_transition(
            s_deleted, s_open, prior_record=None, prior_approved_binding=None
        )
        self.assertEqual(rec_no_binding.transition_kind, "branch_recreated_same_sha")
        self.assertFalse(rec_no_binding.prior_approval_valid)

        # 3. Same-SHA base_ref retarget invalidates same_snapshot & prior_approved_binding.
        s_retargeted = PullRequestLifecycleSnapshot.from_pull_request_payload(
            _sample_pr_payload(
                state="open",
                base_ref="release/1.0",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA_1,
            )
        )
        for binding in ((BASE_SHA, HEAD_SHA_1), ("main", BASE_SHA, HEAD_SHA_1)):
            open_retarget = classify_reopen_branch_transition(
                s_open,
                s_retargeted,
                prior_record=None,
                prior_approved_binding=binding,
            )
            self.assertEqual(open_retarget.transition_kind, "open_head_or_base_changed")
            self.assertFalse(open_retarget.prior_approval_valid)
            self.assertEqual(
                open_retarget.recovery_action,
                "rebind_obligations_and_verify_new_snapshot",
            )
            reopen_retarget = classify_reopen_branch_transition(
                s_closed,
                s_retargeted,
                prior_record=None,
                prior_approved_binding=binding,
            )
            self.assertEqual(reopen_retarget.transition_kind, "reopened_changed_sha")
            self.assertFalse(reopen_retarget.prior_approval_valid)

        # Matching 3-tuple binding validates when base_ref, base_sha, and head_sha all match.
        matching_3tuple = classify_reopen_branch_transition(
            s_retargeted,
            s_retargeted,
            prior_record=None,
            prior_approved_binding=("release/1.0", BASE_SHA, HEAD_SHA_1),
        )
        self.assertEqual(matching_3tuple.transition_kind, "duplicate_open_delivery")
        self.assertTrue(matching_3tuple.prior_approval_valid)

        with self.assertRaises(GitHubPublicationError):
            classify_reopen_branch_transition(
                s_open,
                s_open,
                prior_approved_binding=("only-one",),  # type: ignore[arg-type]
            )

        # 4. Direct coverage of evaluate_finalizer_pr_gate and ReviewPublisher._preflight_pr.
        fin_ok = evaluate_finalizer_pr_gate(
            _sample_pr_payload(author_login="reviewsensei[bot]"),
            repository="owner/repo",
            head_sha=HEAD_SHA_1,
            app_slug="reviewsensei[bot]",
        )
        self.assertTrue(fin_ok.admitted)
        self.assertTrue(fin_ok.app_authored)
        self.assertEqual(fin_ok.repository_id, 123)
        self.assertEqual(fin_ok.base_sha, BASE_SHA)
        self.assertEqual(fin_ok.base_ref, "main")

        for payload_fin, expected_skip in (
            (_sample_pr_payload(state="closed"), "skipped_pr_state"),
            (_sample_pr_payload(draft=True), "skipped_pr_state"),
            (_sample_pr_payload(head_sha=HEAD_SHA_2), "skipped_stale_head"),
            (_sample_pr_payload(head_fork=True), "skipped_fork"),
        ):
            fin_skip = evaluate_finalizer_pr_gate(
                payload_fin,
                repository="owner/repo",
                head_sha=HEAD_SHA_1,
                app_slug="reviewsensei[bot]",
            )
            self.assertFalse(fin_skip.admitted)
            self.assertEqual(fin_skip.skip_status, expected_skip)

        for bad_fin in (
            "not-a-dict",
            _sample_pr_payload(draft="false"),
            _sample_pr_payload(head_repo_missing=True),
        ):
            with self.assertRaises(GitHubPublicationError):
                evaluate_finalizer_pr_gate(
                    bad_fin,
                    repository="owner/repo",
                    head_sha=HEAD_SHA_1,
                    app_slug="reviewsensei[bot]",
                )

        # Exercise ReviewPublisher._preflight_pr on both app_authored and not-admitted paths.
        responses = [
            json_response(_sample_pr_payload(author_login="reviewsensei[bot]")),
            json_response(_sample_pr_payload(draft=True)),
        ]
        pub_http = GitHubHttp(
            api_url="https://api.github.test",
            opener=lambda _req, timeout=None: responses.pop(0),
        )
        pub = ReviewPublisher(http=pub_http)
        pf_admitted = pub._preflight_pr(
            token="installation-token",
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            head_sha=HEAD_SHA_1,
            base_branch="main",
            base_sha=BASE_SHA,
            app_slug="reviewsensei[bot]",
        )
        self.assertIsNone(pf_admitted.result)
        self.assertTrue(pf_admitted.app_authored)
        pf_skipped = pub._preflight_pr(
            token="installation-token",
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            head_sha=HEAD_SHA_1,
            base_branch="main",
            base_sha=BASE_SHA,
            app_slug="reviewsensei[bot]",
        )
        assert pf_skipped.result is not None
        self.assertEqual(pf_skipped.result.status, "skipped_pr_state")

    def test_late_effects_after_close_and_after_changed_sha_reopen_preserve_obligations(
        self,
    ) -> None:
        """G05 exact-scope: delayed review, finalizer, and mention-reply effects after close/reopen."""

        class _CloseReopenReplyFixture(FreshnessReplyFixture):
            live_state = "open"

            def external(self, request, timeout):
                response = super().external(request, timeout)
                target = urlsplit(request.full_url)
                if request.get_method() == "GET" and target.path.endswith("/pulls/7"):
                    pull = json.loads(response.read(524288))
                    pull["state"] = self.live_state
                    return json_response(pull)
                return response

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "review").mkdir()
            (root / "reply_closed").mkdir()
            (root / "reply_stale").mkdir()

            fixture = LifecycleEntryFixture(root / "review")
            h1 = fixture.live_head
            h2 = HEAD_SHA_2

            # 1. Initial open review on H1 records an unresolved human obligation and emits COMMENT.
            code_init, out_init = fixture.run_and_read(human=True)
            self.assertEqual(code_init, 0)
            assert out_init is not None
            self.assertEqual(out_init["status"], "published")
            self.assertEqual([r["event"] for r in fixture.review_posts], ["COMMENT"])
            record_h1 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_h1))

            # 2. Close PR #7: late review, late approval finalization, and late mention reply
            # all skip cleanly (`skipped_pr_state`) without revoking the confirmed H1 COMMENT
            # review or mutating session obligations.
            fixture.pr_state = "closed"
            publisher = ReviewPublisher(http=fixture.http)
            finalizer = ReviewApprovalFinalizer(http=fixture.http)

            late_closed_pub = publisher.publish(
                token="installation-token",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                head_sha=h1,
                base_branch="main",
                base_sha=fixture.live_base,
                diff=SAMPLE_DIFF,
                result=ReviewResult(
                    summary="Delayed clean review arriving after close.",
                    comments=(),
                    provider="ollama",
                    review_status="complete",
                ),
                auto_approve=True,
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(late_closed_pub.status, "skipped_pr_state")
            late_closed_fin = finalizer._preflight(
                token="installation-token",
                repository="owner/repo",
                pull_request=7,
                head_sha=h1,
                app_slug="reviewsensei[bot]",
            )
            assert late_closed_fin.result is not None
            self.assertEqual(late_closed_fin.result.status, "skipped_pr_state")
            self.assertEqual([r["event"] for r in fixture.review_posts], ["COMMENT"])

            reply_closed = _CloseReopenReplyFixture(root / "reply_closed")
            reply_closed.initialize_freshness()
            orig_reply_publish = ConversationPublisher._publish

            def close_before_reply_post(conv_pub, **kwargs):
                reply_closed.live_state = "closed"
                return orig_reply_publish(conv_pub, **kwargs)

            with patch.object(
                ConversationPublisher, "_publish", close_before_reply_post
            ):
                code_reply_closed = reply_closed.invoke_reply()
            self.assertEqual(code_reply_closed, 1)
            self.assertEqual(reply_closed.reply_posts, 0)
            self.assertEqual(
                getattr(reply_closed.last_result, "status", None), "skipped_pr_state"
            )
            self.assertEqual(reply_closed.action()["phase"], "pending")

            # 3. Reopen PR #7 at changed SHA H2: delayed old-head (H1) review, finalizer,
            # mention reply, and stale-event ReviewRun all fence as `skipped_stale_head` / refused.
            fixture.pr_state = "open"
            fixture.live_head = h2

            late_stale_pub = publisher.publish(
                token="installation-token",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                head_sha=h1,
                base_branch="main",
                base_sha=fixture.live_base,
                diff=SAMPLE_DIFF,
                result=ReviewResult(
                    summary="Delayed H1 review arriving after changed-SHA reopen.",
                    comments=(),
                    provider="ollama",
                    review_status="complete",
                ),
                auto_approve=True,
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(late_stale_pub.status, "skipped_stale_head")
            late_stale_fin = finalizer._preflight(
                token="installation-token",
                repository="owner/repo",
                pull_request=7,
                head_sha=h1,
                app_slug="reviewsensei[bot]",
            )
            assert late_stale_fin.result is not None
            self.assertEqual(late_stale_fin.result.status, "skipped_stale_head")
            self.assertEqual([r["event"] for r in fixture.review_posts], ["COMMENT"])

            reply_stale = _CloseReopenReplyFixture(root / "reply_stale")
            reply_stale.initialize_freshness()

            def reopen_at_h2_before_reply_post(conv_pub, **kwargs):
                reply_stale.live_state = "open"
                reply_stale.live_head = h2
                return orig_reply_publish(conv_pub, **kwargs)

            with patch.object(
                ConversationPublisher, "_publish", reopen_at_h2_before_reply_post
            ):
                code_reply_stale = reply_stale.invoke_reply()
            self.assertEqual(code_reply_stale, 1)
            self.assertEqual(reply_stale.reply_posts, 0)
            self.assertEqual(
                getattr(reply_stale.last_result, "status", None), "skipped_stale_head"
            )
            self.assertEqual(reply_stale.action()["phase"], "pending")

            # Delayed ReviewRun still targeting H1 fails closed at bootstrap with 0 provider calls.
            fixture.run_id += 1
            fixture.requested_head = h1
            fixture.trace.clear()
            fixture.transports.clear()
            fixture.worker_observations.clear()
            code_stale_run, out_stale_run = fixture.run_and_read(human=False)
            self.assertEqual(code_stale_run, 1)
            self.assertIsNone(out_stale_run)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual([r["event"] for r in fixture.review_posts], ["COMMENT"])

            # Current H2 run converges cleanly while preserving H1's unresolved obligation
            # and withholding spurious APPROVE.
            original_obligations = fixture.retained_finding_obligations()
            fixture.advance_run_to_head(h2)
            code_h2, out_h2 = fixture.run_and_read(human=False)
            self.assertEqual(code_h2, 0, fixture.error)
            assert out_h2 is not None
            self.assertEqual(out_h2["status"], "published")
            self.assertTrue(out_h2["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 2)
            self.assertEqual(
                [r["event"] for r in fixture.review_posts],
                ["COMMENT", "REQUEST_CHANGES"],
            )
            self.assertEqual(fixture.review_posts[-1]["commit_id"], h2)
            fixture.assert_preserved_omitted_obligations(self, original_obligations)
            record_h2 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_h2))

    def test_reordered_close_reopen_synchronize_and_deleted_branch_ref_combinations(
        self,
    ) -> None:
        """G05 exact-scope: out-of-order reopen/close/synchronize deliveries and branch_ref_exists."""

        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            h1 = fixture.live_head
            h2 = HEAD_SHA_2

            code_1, out_1 = fixture.run_and_read(human=True)
            self.assertEqual(code_1, 0)
            assert out_1 is not None
            self.assertEqual(out_1["status"], "published")
            record_1 = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertTrue(session_has_unresolved_obligations(record_1))

            # 1. Out-of-order `reopened(H1)` event processed AFTER `synchronize(H2)` already
            # advanced live GitHub PR #7 to H2: stale event fails closed pre-inference.
            fixture.live_head = h2
            fixture.requested_head = h1
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            fixture.worker_observations.clear()
            code_reordered_sync, out_reordered_sync = fixture.run_and_read(human=False)
            self.assertEqual(code_reordered_sync, 1)
            self.assertIsNone(out_reordered_sync)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(len(fixture.review_posts), 1)

            # 2. Out-of-order `reopened(H2)` event processed AFTER rapid re-`closed` took effect
            # on live GitHub PR #7: live PR truth (`state="closed"`) blocks execution pre-inference.
            fixture.pr_state = "closed"
            fixture.advance_run_to_head(h2)
            code_reordered_close, out_reordered_close = fixture.run_and_read(
                human=False
            )
            self.assertEqual(code_reordered_close, 1)
            self.assertIsNone(out_reordered_close)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(len(fixture.review_posts), 1)

            # 3. Branch ref deleted while `head.repo` object remains populated (`branch_ref_exists=False`
            # with `head_repo_present=True`), closed -> open while branch ref still absent,
            # and branch ref recreation at same vs changed SHA.
            s_open_h1 = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=h1),
                branch_ref_exists=True,
            )
            s_ref_deleted_repo_present = (
                PullRequestLifecycleSnapshot.from_pull_request_payload(
                    _sample_pr_payload(
                        state="open", head_sha=h1, head_repo_missing=False
                    ),
                    branch_ref_exists=False,
                )
            )
            self.assertTrue(s_ref_deleted_repo_present.head_repo_present)
            self.assertFalse(s_ref_deleted_repo_present.has_live_same_repo_head)
            self.assertFalse(s_ref_deleted_repo_present.admits_live_writes)

            ref_del_dec = classify_reopen_branch_transition(
                s_open_h1,
                s_ref_deleted_repo_present,
                prior_record=record_1,
            )
            self.assertEqual(
                ref_del_dec.transition_kind, "head_branch_deleted_blocking_writes"
            )
            self.assertFalse(ref_del_dec.admits_live_writes)
            self.assertTrue(ref_del_dec.preserves_prior_obligations)
            self.assertTrue(ref_del_dec.unresolved_obligations_present)
            self.assertFalse(ref_del_dec.mints_fresh_failed_attempt_allowance)

            # Closed with deleted ref -> reopened while ref still deleted -> restored ref.
            s_closed_ref_deleted = (
                PullRequestLifecycleSnapshot.from_pull_request_payload(
                    _sample_pr_payload(state="closed", head_sha=h1),
                    branch_ref_exists=False,
                )
            )
            closed_del_dec = classify_reopen_branch_transition(
                s_ref_deleted_repo_present,
                s_closed_ref_deleted,
                prior_record=record_1,
            )
            self.assertEqual(closed_del_dec.transition_kind, "closed_blocking_writes")
            self.assertFalse(closed_del_dec.admits_live_writes)

            reopen_still_deleted = classify_reopen_branch_transition(
                s_closed_ref_deleted,
                s_ref_deleted_repo_present,
                prior_record=record_1,
            )
            self.assertEqual(
                reopen_still_deleted.transition_kind,
                "head_branch_deleted_blocking_writes",
            )
            self.assertFalse(reopen_still_deleted.admits_live_writes)

            restored_same_dec = classify_reopen_branch_transition(
                s_ref_deleted_repo_present,
                s_open_h1,
                prior_record=record_1,
            )
            self.assertEqual(
                restored_same_dec.transition_kind, "branch_recreated_same_sha"
            )
            self.assertTrue(restored_same_dec.admits_live_writes)
            self.assertTrue(restored_same_dec.unresolved_obligations_present)
            self.assertFalse(restored_same_dec.prior_approval_valid)

            s_open_h2 = PullRequestLifecycleSnapshot.from_pull_request_payload(
                _sample_pr_payload(state="open", head_sha=h2),
                branch_ref_exists=True,
            )
            restored_changed_dec = classify_reopen_branch_transition(
                s_ref_deleted_repo_present,
                s_open_h2,
                prior_record=record_1,
                prior_approved_binding=(BASE_SHA, h1),
            )
            self.assertEqual(
                restored_changed_dec.transition_kind, "branch_recreated_changed_sha"
            )
            self.assertTrue(restored_changed_dec.admits_live_writes)
            self.assertFalse(restored_changed_dec.prior_approval_valid)


if __name__ == "__main__":
    unittest.main()
