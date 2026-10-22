"""Hosted media reconstruction uses real canonical retained result records."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from review_sensei_github import (
    GitHubApplication,
    GitHubHttp,
    GitHubWriteOptions,
    ReviewPublisher,
)
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.hosted_media import prepare_hosted_media_context
from review_sensei_github.operation_host import RESULT_COMMENT_MARKER
from review_sensei_github.publication import (
    IndexedFinding,
    finding_index_marker,
    finding_instance_marker,
    finding_marker,
    review_result_digest,
)
from review_sensei_github.publication_recovery import _event_key, _store
from review_sensei_github.session_ledger import render_session_comment

from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewComment, ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord
from tests.fake_github_http import json_response
from tests.test_github_human_file_review import ALICE, API, BASE, BOT, HEAD, REPO, State

BODY = "@reviewsensei I reviewed the media files and I approve"
IDENTITY = SessionIdentity("owner/repo", 2, repository_id=1)


class HostedMediaState(State):
    def __init__(
        self,
        *,
        provider="fixture",
        media_count=2,
        text_count=1,
        long_text_paths=False,
        blocking_finding=False,
        resolved_inline=False,
        finding_index=True,
    ):
        super().__init__()
        if media_count != 2 or text_count != 1 or long_text_paths:
            media = tuple(f"media-{index:03}.png" for index in range(media_count))
            text_prefix = "text/" + "documentation/" * 25 if long_text_paths else ""
            text = tuple(
                f"{text_prefix}module-{index:03}.py" for index in range(text_count)
            )
            self.old = {}
            self.new = {
                path: (f"{index + 10:040x}", "100644")
                for index, path in enumerate(media)
            }
            self.files = [
                {"filename": path, "status": "added"} for path in (*media, *text)
            ]
            self.set_result(
                replace(
                    self.result,
                    coverage=CoverageManifest(
                        files=tuple(
                            FileCoverage(path, "unsupported", "binary")
                            for path in media
                        )
                        + tuple(FileCoverage(path, "reviewed") for path in text),
                        enumerated_paths=(*media, *text),
                    ),
                )
            )
        self.set_result(
            replace(
                self.result,
                provider=provider,
                model="synthetic-model" if provider == "openrouter" else None,
            )
        )
        comment = None
        if blocking_finding:
            comment = ReviewComment(
                path=next(
                    item.path
                    for item in self.result.coverage.files
                    if item.outcome == "reviewed"
                ),
                line=1,
                body="Synthetic inline blocking finding.",
                blocking=True,
            )
            self.set_result(replace(self.result, comments=(comment,)))
        self.permission = "maintain"
        self.draft = False
        self.fork = False
        self.author = "contributor"
        self.physical = []
        self.threads = []
        self.http = GitHubHttp(api_url=API, opener=self.open)
        transaction = ReviewTransaction.create(
            repository="owner/repo",
            pull_request=2,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest="1" * 64,
            configuration_digest="2" * 64,
            evidence_digest="3" * 64,
            reservation_id="4" * 64,
            generation=1,
        )
        transaction = replace(
            transaction,
            phase="publication_succeeded",
            result_sha256=self.result.content_digest(),
        )
        self.set_result(replace(self.result, transaction=transaction))
        if comment is not None:
            # Native publication freezes body blockers; inline obligations
            # are independently authenticated from their exact indexed thread.
            self.set_result(self.result, has_blocking_findings=False)
            if finding_index:
                self.root["body"] += "\n" + finding_index_marker(
                    repository_id=1,
                    pull_request=2,
                    head_sha=HEAD,
                    base_sha=BASE,
                    result_digest=review_result_digest(self.result),
                    entries=(
                        IndexedFinding(
                            finding_instance_fingerprint(comment),
                            finding_lifecycle_for_comment(comment).fingerprint,
                            blocking=True,
                            needs_human=False,
                            body_placed=False,
                        ),
                    ),
                )
            self.threads = [
                {
                    "id": "synthetic-inline",
                    "isResolved": resolved_inline,
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": 77,
                                "author": {"login": BOT["login"], "__typename": "Bot"},
                                "body": finding_instance_marker(comment)
                                + "\n"
                                + finding_marker(
                                    repository_id=1,
                                    pull_request=2,
                                    head_sha=HEAD,
                                    base_sha=BASE,
                                    result=self.result,
                                    blocking=True,
                                    fingerprint=finding_lifecycle_for_comment(
                                        comment
                                    ).fingerprint,
                                ),
                            }
                        ]
                    },
                }
            ]
        record = SessionRecord.create(
            IDENTITY,
            now=datetime.now(timezone.utc),
            generation=1,
            transaction=transaction,
            completed_initial_reviews=1,
            last_committed_reservation_id=transaction.reservation_id,
        )
        self.comments[600] = {
            "id": 600,
            "user": BOT,
            "issue_url": f"{API}{REPO}/issues/2",
            "body": render_session_comment(
                repository_id=1, pull_request=2, record=record
            ),
        }
        self.comments[20] = {
            "id": 20,
            "user": ALICE,
            "author_association": "COLLABORATOR",
            "issue_url": f"{API}{REPO}/issues/2",
            "updated_at": "2026-10-22T01:00:00Z",
            "body": BODY,
        }
        # This is synthetic fixture setup, outside the command invocation.
        # The actual factory reads the real canonical store and ledger below.
        store = _store(
            self.http,
            token="synthetic-read",
            identity=IDENTITY,
            app_slug=BOT["login"],
            base_sha=BASE,
            head_sha=HEAD,
        )
        store.write(
            event_key=_event_key(IDENTITY, transaction),
            result=canonical_bytes(self.result.to_dict()),
        )
        self.physical.clear()
        self.calls.clear()

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        path = unquote(parsed.path)
        self.physical.append(
            (request.method, path, request.get_header("Authorization"), timeout)
        )
        if path == "/users/" + BOT["login"]:
            return json_response(copy.deepcopy(BOT))
        if path == REPO + "/pulls/2" and request.method == "GET":
            repo = {"id": 1, "full_name": "owner/repo", "fork": self.fork}
            return json_response(
                {
                    "state": "open",
                    "number": 2,
                    "draft": self.draft,
                    "user": {"login": self.author},
                    "base": {"sha": self.base, "repo": repo},
                    "head": {"sha": self.head, "repo": repo},
                }
            )
        if path == "/graphql":
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": copy.deepcopy(self.threads),
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
        status, payload = self.request(
            request.method,
            parsed.path + ("?" + parsed.query if parsed.query else ""),
            body=json.loads(request.data) if request.data else None,
            timeout_seconds=timeout,
        )
        if request.method == "GET" and isinstance(payload, list):
            query = parse_qs(parsed.query)
            size = int(query.get("per_page", ["100"])[0])
            page = int(query.get("page", ["1"])[0])
            payload = payload[(page - 1) * size : page * size]
        return json_response(payload, status)


class HostedMediaContextTests(unittest.TestCase):
    def setUp(self):
        self.setup_state()

    def setup_state(self, **kwargs):
        self.state = HostedMediaState(**kwargs)
        self.budget = EvidenceReadBudget()
        self.application = GitHubApplication(
            broker=object(),
            http=self.state.http,
            reviewer=ReviewPublisher(http=self.state.http),
            learner=object(),
            replier=object(),
        )

    def context(self, **changes):
        arguments = {
            "application": self.application,
            "options": GitHubWriteOptions(
                github_writes=True, github_session_ledger=True
            ),
            "http": self.state.http,
            "budget": self.budget,
            "session_token": "synthetic-session",
            "read_token": "synthetic-read",
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": HEAD,
            "source_comment_id": 20,
            "actor_login": ALICE["login"],
            "body": BODY,
            "app_slug": BOT["login"],
            "reviews_policy": "auto-approve",
        }
        return prepare_hosted_media_context(**(arguments | changes))

    def test_actual_retained_result_request_and_every_physical_read_share_budget(self):
        context = self.context()
        self.assertEqual(context.result.to_dict(), self.state.result.to_dict())
        self.assertIn(context.request_comment_id, self.state.comments)
        self.assertEqual(self.budget.calls, len(self.state.physical))
        self.assertEqual(
            sum(path.startswith("/users/") for _, path, _, _ in self.state.physical), 1
        )
        for method, _path, authorization, timeout in self.state.physical:
            self.assertEqual(
                authorization,
                "Bearer synthetic-read"
                if method == "GET"
                else "Bearer synthetic-session",
            )
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 60)
        self.assertEqual(self.state.result.review_status, "partial")

    def test_source_and_permission_refuse_before_request_write(self):
        for field, value in (
            ("body", BODY + " edited"),
            ("issue_url", f"{API}{REPO}/issues/3"),
            ("user", ALICE | {"id": 987}),
            ("author_association", "NONE"),
        ):
            with self.subTest(field=field):
                self.setUp()
                self.state.comments[20][field] = value
                with self.assertRaises(ReviewInputError):
                    self.context()
                self.assertFalse(
                    any(method != "GET" for method, _, _, _ in self.state.physical)
                )
        self.setUp()
        self.state.permission = "write"
        with self.assertRaisesRegex(ReviewInputError, "live maintainer"):
            self.context()
        self.assertFalse(
            any(method != "GET" for method, _, _, _ in self.state.physical)
        )

    def test_live_draft_fork_author_base_and_head_fences_precede_request_write(self):
        for field, value in (
            ("draft", True),
            ("fork", True),
            ("author", BOT["login"]),
            ("base", "c" * 40),
            ("head", "d" * 40),
        ):
            with self.subTest(field=field):
                self.setUp()
                setattr(self.state, field, value)
                with self.assertRaises(ReviewInputError):
                    self.context()
                self.assertFalse(
                    any(method != "GET" for method, _, _, _ in self.state.physical)
                )

    def test_missing_tampered_or_foreign_retention_never_creates_request(self):
        for mutation in ("missing", "altered", "foreign"):
            with self.subTest(mutation=mutation):
                self.setUp()
                retained = next(
                    value
                    for value in self.state.comments.values()
                    if RESULT_COMMENT_MARKER in value["body"]
                )
                if mutation == "missing":
                    del self.state.comments[retained["id"]]
                elif mutation == "altered":
                    retained["body"] = retained["body"].replace(
                        "result_sha256", "wrong_sha256"
                    )
                else:
                    retained["user"] = BOT | {"id": 999}
                with self.assertRaises((ReviewInputError, ValueError)):
                    self.context()
                self.assertFalse(
                    any(method != "GET" for method, _, _, _ in self.state.physical)
                )

    def test_exhausted_original_budget_cannot_be_replaced_by_factory(self):
        for _ in range(59):
            self.budget.consume()
        with self.assertRaises(ReviewInputError):
            self.context()
        self.assertEqual(len(self.state.physical), 1)
        self.assertEqual(self.budget.calls, 60)

    def test_current_policy_and_missing_provider_qualification_withhold_native_approval(
        self,
    ):
        for policy, provider in (
            ("blocking", "fixture"),
            ("auto-approve", "openrouter"),
        ):
            with self.subTest(policy=policy, provider=provider):
                self.setup_state(provider=provider)
                context = self.context(reviews_policy=policy)
                assessment = context.publisher.approve_reviewed_media(
                    request_comment_id=context.request_comment_id,
                    source_comment_id=20,
                    result=context.result,
                )
                self.assertFalse(assessment.approval.approved)
                self.assertFalse(
                    any(item["state"] == "APPROVED" for item in self.state.reviews)
                )
                self.assertEqual(self.budget.calls, len(self.state.physical))
                self.assertEqual(self.state.result.review_status, "partial")

    def test_frozen_qualified_marker_cannot_replace_current_provider_authority(self):
        self.setup_state(provider="openrouter")
        self.state.set_result(self.state.result, qualification="qualified")
        self.assertIsNone(self.application.reviewer.finalizer.qualification_authority)
        context = self.context()
        assessment = context.publisher.approve_reviewed_media(
            request_comment_id=context.request_comment_id,
            source_comment_id=20,
            result=context.result,
        )
        self.assertFalse(assessment.approval.approved)
        self.assertIn("qualification-unverified", assessment.approval.blockers)
        self.assertFalse(
            any(item["state"] == "APPROVED" for item in self.state.reviews)
        )

    def test_live_snapshot_change_after_factory_refuses_native_approval(self):
        context = self.context()
        self.state.draft = True
        with self.assertRaises(ReviewInputError):
            context.publisher.approve_reviewed_media(
                request_comment_id=context.request_comment_id,
                source_comment_id=20,
                result=context.result,
            )
        self.assertFalse(
            any(item["state"] == "APPROVED" for item in self.state.reviews)
        )

    def test_latest_owned_review_must_match_the_retained_transaction(self):
        self.state.reviews.append(
            self.state.root
            | {"id": 11, "body": "<!-- reviewsensei:eligibility:v1 malformed -->"}
        )
        with self.assertRaises((ReviewInputError, GitHubPublicationError)):
            self.context()
        self.assertFalse(
            any(method != "GET" for method, _, _, _ in self.state.physical)
        )

    def test_41_media_102_files_and_two_retained_parts_keep_original_accounting(self):
        self.setup_state(media_count=41, text_count=61, long_text_paths=True)
        retained = [
            item
            for item in self.state.comments.values()
            if RESULT_COMMENT_MARKER in item["body"]
        ]
        self.assertEqual(len(retained), 2)
        context = self.context()
        assessment = context.publisher.approve_reviewed_media(
            request_comment_id=context.request_comment_id,
            source_comment_id=20,
            result=context.result,
        )
        self.assertTrue(assessment.approval.approved)
        self.assertEqual(len(assessment.human_reviewed_ids), 41)
        self.assertEqual(self.budget.calls, len(self.state.physical))
        self.assertLessEqual(self.budget.calls, 60)
        self.assertEqual(self.state.result.review_status, "partial")

    def test_only_exact_index_and_positive_live_thread_resolution_clear_inline_finding(
        self,
    ):
        for resolved, indexed in ((True, True), (False, True), (True, False)):
            with self.subTest(resolved=resolved, indexed=indexed):
                self.setup_state(
                    blocking_finding=True,
                    resolved_inline=resolved,
                    finding_index=indexed,
                )
                original = self.state.result.to_dict()
                context = self.context()
                assessment = context.publisher.approve_reviewed_media(
                    request_comment_id=context.request_comment_id,
                    source_comment_id=20,
                    result=context.result,
                )
                self.assertEqual(assessment.approval.approved, resolved and indexed)
                self.assertTrue(context.result.comments[0].blocks_approval)
                self.assertEqual(self.state.result.to_dict(), original)
                self.assertEqual(self.budget.calls, len(self.state.physical))

    def test_missing_or_other_head_thread_cannot_clear_indexed_inline_finding(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.setup_state(blocking_finding=True, resolved_inline=True)
                if missing:
                    self.state.threads = []
                else:
                    root = self.state.threads[0]["comments"]["nodes"][0]
                    root["body"] = root["body"].replace(
                        "head=" + HEAD, "head=" + "c" * 40
                    )
                context = self.context()
                assessment = context.publisher.approve_reviewed_media(
                    request_comment_id=context.request_comment_id,
                    source_comment_id=20,
                    result=context.result,
                )
                self.assertFalse(assessment.approval.approved)
                self.assertTrue(assessment.current_findings.has_blocking_findings)

    def test_unfunded_native_tail_retains_receipt_and_explicit_retry_finishes(self):
        # Earlier broker/metadata work has already spent this invocation's
        # allowance; neither the factory nor adapter may replace it.
        for _ in range(22):
            self.budget.consume()
        context = self.context()
        with self.assertRaisesRegex(
            ReviewInputError, "retry the same supported media command"
        ):
            context.publisher.approve_reviewed_media(
                request_comment_id=context.request_comment_id,
                source_comment_id=20,
                result=context.result,
            )
        self.assertFalse(
            any(item["state"] == "APPROVED" for item in self.state.reviews)
        )
        receipts = [
            item["id"]
            for item in self.state.comments.values()
            if "reviewsensei:human-files:receipt:" in item["body"]
        ]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(self.budget.calls, len(self.state.physical) + 22)
        # A separate explicit invocation starts its own allowance and reads the
        # same real source/request/receipt. It is not an automatic phase refill.
        self.budget = EvidenceReadBudget()
        self.state.physical.clear()
        for _ in range(22):
            self.budget.consume()
        retry = self.context()
        assessment = retry.publisher.approve_reviewed_media(
            request_comment_id=retry.request_comment_id,
            source_comment_id=20,
            result=retry.result,
        )
        self.assertTrue(assessment.approval.approved)
        self.assertEqual(
            sum(item["state"] == "APPROVED" for item in self.state.reviews), 1
        )
        self.assertEqual(
            [
                item["id"]
                for item in self.state.comments.values()
                if "reviewsensei:human-files:receipt:" in item["body"]
            ],
            receipts,
        )
        self.assertEqual(self.budget.calls, len(self.state.physical) + 22)


if __name__ == "__main__":
    unittest.main()
