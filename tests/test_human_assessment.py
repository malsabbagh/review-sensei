"""Human-review reassessment starts with real needs_human publication evidence."""

from __future__ import annotations

import base64
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlparse

from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewFormatError, ReviewInputError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubHttp,
    GitHubWriteOptions,
    ReviewPublisher,
)
from review_sensei.hosting.github.approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
)
from review_sensei.hosting.github.conversation import (
    ConversationPublisher,
    PreparedConversation,
)
from review_sensei.hosting.github.errors import (
    GitHubConversationError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from review_sensei.hosting.github.human_assessment import HumanAssessmentPublisher
from review_sensei.hosting.github.publication import (
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from review_sensei.human_assessment import (
    HumanAssessmentReply,
    HumanAssessmentService,
    PendingHumanReview,
)
from review_sensei.models import (
    ConversationContext,
    ConversationMessage,
    ProviderResponse,
    ReviewComment,
    ReviewResult,
)
from tests.fake_github_http import json_response

HEAD = "b" * 40
BASE = "a" * 40
APP = "review-sensei[bot]"
HUMAN = "@sensei This path is intentionally local-only; the branch rejects remote requests before any data leaves the machine."
DIFF = "@@ -1 +1 @@\n-return send(data)\n+return reject_remote_requests(data)"


def prior(*, extra_human=False, blocking=False, **facts):
    comments = [
        ReviewComment(
            path="src/app.py",
            line=1,
            body="Confirm that this path cannot send private input remotely.",
            severity="high",
            effective_blocking=False,
            needs_human=True,
        )
    ]
    if extra_human:
        comments.append(
            replace(
                comments[0],
                path="src/other.py",
                body="Confirm that the other path cannot send input remotely.",
            )
        )
    if blocking:
        comments.append(
            ReviewComment(
                path="src/broken.py", line=1, body="A proven blocker.", blocking=True
            )
        )
    result = ReviewResult(
        summary="Human assessment is required.",
        comments=tuple(comments),
        provider="ollama",
        review_status="complete",
    )
    eligibility = approval_eligibility_from_result(
        result, head_sha=HEAD, base_sha=BASE, enabled=True, app_authored=False
    )
    return replace(eligibility, facts=replace(eligibility.facts, **facts))


class Provider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        return ProviderResponse(text=json.dumps(self.payload), provider="fixture")


class Broker:
    def __init__(self):
        self.capabilities = []

    def exchange(self, _token, *, capability):
        self.capabilities.append(capability)
        return "synthetic-" + capability


def response_for(eligibility, *, decision="dismissed"):
    finding = next(
        item for item in eligibility.human_review.pending if item.path == "src/app.py"
    )
    return {
        "body": "The explanation is supported by the current diff.",
        "assessments": [
            {
                "fingerprint": finding.fingerprint,
                "decision": decision,
                "rationale": "The cited current diff rejects the remote requests before sending the input.",
                "human_evidence": "This path is intentionally local-only",
                "diff_evidence": "reject_remote_requests(data)",
            }
        ],
    }


class State:
    """Synthetic API state with race hooks; no network or live approvals."""

    def __init__(self, eligibility=None):
        self.eligibility = eligibility or prior()
        self.reviews = [self.review(self.eligibility, 20)]
        self.head = HEAD
        self.base = BASE
        self.source = {
            "id": 10,
            "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            "body": HUMAN,
            "updated_at": "updated",
            "created_at": "created",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
        }
        self.calls = []
        self.reply_count = 0
        self.hook = None
        self.threads = []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    @staticmethod
    def review(eligibility, identifier):
        return {
            "id": identifier,
            "commit_id": HEAD,
            "state": "COMMENTED",
            "body": "Human findings.\n" + approval_eligibility_marker(eligibility),
            "user": {"login": APP, "type": "Bot"},
        }

    def pr(self):
        return {
            "state": "open",
            "draft": False,
            "user": {"login": "alice", "type": "User"},
            "head": {
                "sha": self.head,
                "ref": "feature",
                "repo": {"full_name": "owner/repo", "fork": False},
            },
            "base": {
                "sha": self.base,
                "ref": "main",
                "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
            },
        }

    def open(self, request, timeout):
        path = urlparse(request.full_url).path
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.method, path, body))
        if self.hook:
            self.hook(self, request.method, path, body)
        if request.method == "GET":
            if path.endswith("/issues/comments/10"):
                return json_response(self.source)
            if path.endswith("/pulls/1"):
                return json_response(self.pr())
            if path.endswith("/pulls/1/reviews"):
                return json_response(self.reviews)
            if path.endswith("/pulls/1/files"):
                return json_response([{"filename": "src/app.py", "patch": DIFF}])
            if path.endswith("/issues/1/comments"):
                return json_response([self.source])
            if path.endswith("/pulls/1/comments"):
                return json_response([])
            if "/contents/" in path:
                return json_response({}, 404)
        if request.method == "POST":
            if path.endswith("/issues/1/comments"):
                self.reply_count += 1
                return json_response({"id": 30}, 201)
            if path.endswith("/issues/comments/10/reactions"):
                return json_response({"id": 99}, 201)
            if path.endswith("/pulls/1/reviews"):
                value = {
                    "id": 40 + len(self.reviews),
                    "commit_id": body["commit_id"],
                    "state": "APPROVED" if body["event"] == "APPROVE" else "COMMENTED",
                    "body": body["body"],
                    "user": {"login": APP, "type": "Bot"},
                }
                self.reviews.append(value)
                return json_response(
                    value,
                    200 if body["event"] == "APPROVE" or "comments" in body else 201,
                )
            if path == "/graphql":
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": self.threads,
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
        if request.method == "DELETE" and path.endswith("/reactions/99"):
            return json_response({}, 204)
        raise AssertionError((request.method, path, body))

    def prepared(self):
        return PreparedConversation(
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="updated",
            head_sha=HEAD,
            root_comment_id=10,
            context=ConversationContext(
                messages=(ConversationMessage("alice", HUMAN, "created"),),
                head_sha=HEAD,
                base_sha=BASE,
                diff_context="path=src/app.py\n" + DIFF,
            ),
        )

    def bridge(self):
        publisher = HumanAssessmentPublisher(http=self.http)
        prepared = publisher.prepare(
            token="read",
            repository="owner/repo",
            pull_request=1,
            prepared=self.prepared(),
            app_slug=APP,
        )
        return publisher, prepared

    def events(self):
        return [
            body["event"]
            for method, path, body in self.calls
            if method == "POST" and path.endswith("/pulls/1/reviews")
        ]

    def application_reply(self, provider):
        broker = Broker()
        application = GitHubApplication(
            broker=broker,
            reviewer=None,
            learner=None,
            replier=ConversationPublisher(http=self.http),
            http=self.http,
        )
        result = application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="synthetic-oidc",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=HEAD,
            reply_provider=provider,
            model="fixture-model",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
        )
        return result, broker


class HumanAssessmentTests(unittest.TestCase):
    def human_result(self, comments):
        return ReviewResult(
            summary="Human assessment is required.",
            comments=tuple(comments),
            provider="fixture",
            review_status="complete",
        )

    def test_collision_identity_preserves_distinct_comments_and_coalesces_exact_duplicates(
        self,
    ):
        comment = ReviewComment(
            path="src/app.py",
            line=1,
            body="First concern.",
            needs_human=True,
            effective_blocking=False,
        )
        variants = (
            comment,
            replace(comment, body="A different concern."),
            replace(comment, line=2),
            replace(comment, side="LEFT"),
        )
        lifecycle_id = finding_lifecycle_for_comment(comment).fingerprint
        self.assertEqual(
            {finding_lifecycle_for_comment(c).fingerprint for c in variants},
            {lifecycle_id},
        )
        inventory = PendingHumanReview.from_result(
            self.human_result(variants + (comment,)), BASE
        )
        self.assertEqual(len(inventory.findings), len(variants))
        self.assertEqual(
            len({item.fingerprint for item in inventory.findings}), len(variants)
        )
        self.assertEqual(
            PendingHumanReview.from_result(self.human_result(reversed(variants)), BASE),
            inventory,
        )
        single = PendingHumanReview.from_result(
            self.human_result((comment, comment)), BASE
        )
        self.assertEqual(len(single.findings), 1)
        self.assertEqual(single.findings[0].fingerprint, lifecycle_id)
        document = inventory.to_dict()
        document["findings"].append(document["findings"][0])
        with self.assertRaisesRegex(ReviewInputError, "duplicated"):
            PendingHumanReview.from_dict(document)

    def test_invalid_or_unbounded_inventory_is_visible_before_publication_writes(self):
        comment = ReviewComment(
            path="src/app.py",
            line=1,
            body="Human concern.",
            needs_human=True,
            severity="high",
        )
        cases = (
            (self.human_result((comment,)), None),
            (self.human_result((comment,)), "invalid"),
            (self.human_result((replace(comment, body="x" * 2100),)), BASE),
            (self.human_result((replace(comment, body="é" * 1025),)), BASE),
            (
                self.human_result(
                    replace(comment, body=f"Concern {i}") for i in range(21)
                ),
                BASE,
            ),
            (
                self.human_result(
                    replace(comment, body=f"{i}" + "x" * 1500) for i in range(20)
                ),
                BASE,
            ),
        )
        for result, base in cases:
            with (
                self.subTest(base=base, count=len(result.comments)),
                self.assertRaises(ReviewInputError),
            ):
                PendingHumanReview.from_result(result, base)
        state = State()
        with self.assertRaises(ReviewInputError):
            self.publish_initial(state, cases[2][0])
        self.assertFalse(any(method == "POST" for method, _, _ in state.calls))

    def publish_initial(self, state, result):
        return ReviewPublisher(http=state.http).publish(
            token="review",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_branch="main",
            base_sha=BASE,
            result=result,
            diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            + DIFF
            + "\n",
            app_slug=APP,
            convergence_policy=ReviewConvergencePolicy(enforcement="publication"),
        )

    def test_duplicates_to_persisted_review_to_human_reassessment_and_one_approval(
        self,
    ):
        comment = ReviewComment(
            path="src/app.py",
            line=1,
            body="Confirm this path cannot send private input remotely.",
            needs_human=True,
            effective_blocking=False,
            severity="high",
        )
        result = self.human_result(
            (
                comment,
                comment,
                replace(comment, body="Confirm the remote branch is rejected locally."),
            )
        )
        state = State()
        state.reviews = []
        self.publish_initial(state, result)
        state.eligibility = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertEqual(len(state.eligibility.human_review.pending), 2)
        self.assertEqual(state.events(), ["COMMENT"])
        payload = response_for(state.eligibility)
        assessment = payload["assessments"][0]
        payload["assessments"] = [
            dict(assessment, fingerprint=f.fingerprint)
            for f in state.eligibility.human_review.pending
        ]
        provider = Provider(payload)
        outcome, broker = state.application_reply(provider)
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(broker.capabilities, ["issue_reply", "review_publish"])
        self.assertEqual(state.events(), ["COMMENT", "COMMENT", "APPROVE"])
        refreshed = approval_eligibility_from_body(state.reviews[-2]["body"])
        self.assertFalse(refreshed.human_review.pending)
        self.assertEqual(refreshed.result_digest, state.eligibility.result_digest)
        self.assertEqual(
            replace(refreshed.facts, has_human_adjudication_findings=True),
            state.eligibility.facts,
        )
        replay_provider = Provider({})
        replay, _ = state.application_reply(replay_provider)
        self.assertEqual(replay.status, "already_replied")
        self.assertFalse(replay_provider.calls)
        self.assertEqual(state.events(), ["COMMENT", "COMMENT", "APPROVE"])

    def test_retry_after_durable_reassessment_resumes_approval_without_inference(self):
        state = State()
        provider = Provider(response_for(state.eligibility))
        finalize = ReviewApprovalFinalizer.finalize
        with patch.object(
            ReviewApprovalFinalizer,
            "finalize",
            side_effect=GitHubPublicationTransientError("interrupted"),
        ):
            with self.assertRaises(GitHubPublicationTransientError):
                state.application_reply(provider)
        self.assertEqual(state.events(), ["COMMENT"])
        retry_provider = Provider({})
        outcome, broker = state.application_reply(retry_provider)
        self.assertEqual(outcome.status, "already_replied")
        self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
        self.assertFalse(retry_provider.calls)
        self.assertEqual(broker.capabilities, ["issue_reply", "review_publish"])
        self.assertIs(ReviewApprovalFinalizer.finalize, finalize)

    def test_collision_assessment_clears_only_the_cited_identity(self):
        comment = ReviewComment(
            path="src/app.py", line=1, body="First concern.", needs_human=True
        )
        result = self.human_result((comment, replace(comment, body="Second concern.")))
        eligibility = approval_eligibility_from_result(
            result, head_sha=HEAD, base_sha=BASE, enabled=True, app_authored=False
        )
        state = State(eligibility)
        publisher, prepared = state.bridge()
        payload = response_for(eligibility)
        original_fingerprint = finding_lifecycle_for_comment(comment).fingerprint
        with self.assertRaises(ReviewFormatError):
            HumanAssessmentService(
                Provider(
                    {
                        **payload,
                        "assessments": [
                            dict(
                                payload["assessments"][0],
                                fingerprint=original_fingerprint,
                            )
                        ],
                    }
                )
            ).reply(
                context=prepared.conversation.context,
                pending=eligibility.human_review,
                source_body=HUMAN,
            )
        reply = HumanAssessmentService(Provider(payload)).reply(
            context=prepared.conversation.context,
            pending=eligibility.human_review,
            source_body=HUMAN,
        )
        publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        refreshed = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertEqual(len(refreshed.human_review.pending), 1)
        self.assertEqual(
            refreshed.human_review.resolved, (reply.decisions[0].fingerprint,)
        )
        self.assertEqual(state.events(), ["COMMENT"])

    def test_same_prepared_retry_recognizes_only_its_exact_persisted_refresh(self):
        state = State()
        publisher, prepared = state.bridge()
        reply = HumanAssessmentService(Provider(response_for(state.eligibility))).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=HUMAN,
        )
        with patch.object(
            publisher.finalizer,
            "finalize",
            side_effect=GitHubPublicationTransientError("interrupted"),
        ):
            with self.assertRaises(GitHubPublicationTransientError):
                publisher.publish(
                    token="issue",
                    review_token="review",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=reply,
                    app_slug=APP,
                )
        self.assertEqual(state.events(), ["COMMENT"])
        for _ in range(2):
            outcome = publisher.publish(
                token="issue",
                review_token="review",
                repository="owner/repo",
                pull_request=1,
                prepared=prepared,
                reply=reply,
                app_slug=APP,
            )
            self.assertEqual(outcome.status, "already_replied")
        self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
        state.reviews.append(
            state.review(replace(state.eligibility, result_digest="f" * 64), 100)
        )
        outcome = publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        self.assertEqual(outcome.status, "skipped_stale_head")
        self.assertEqual(state.events(), ["COMMENT", "APPROVE"])

    def test_resumed_approval_keeps_gates_and_fails_without_review_capability(self):
        for facts in (
            {"enabled": False},
            {"qualification": "missing"},
            {"coverage_blocker": "unknown"},
            {"has_blocking_findings": True},
        ):
            with self.subTest(facts=facts):
                original = prior(**facts)
                inventory = replace(
                    original.human_review,
                    resolved=tuple(
                        f.fingerprint for f in original.human_review.findings
                    ),
                )
                state = State(
                    replace(
                        original,
                        human_review=inventory,
                        facts=replace(
                            original.facts, has_human_adjudication_findings=False
                        ),
                    )
                )
                publisher, prepared = state.bridge()
                reply = HumanAssessmentReply(
                    body="Previously reassessed.", decisions=()
                )
                with self.assertRaisesRegex(
                    GitHubConversationError, "review capability"
                ):
                    publisher.publish(
                        token="issue",
                        review_token=None,
                        repository="owner/repo",
                        pull_request=1,
                        prepared=prepared,
                        reply=reply,
                        app_slug=APP,
                    )
                outcome, _ = state.application_reply(Provider({}))
                self.assertEqual(outcome.status, "already_replied")
                self.assertEqual(state.events(), [])

    def test_resumed_approval_rechecks_stale_head_base_and_latest_result(self):
        for race in ("head", "base", "result"):
            with self.subTest(race=race):
                original = prior()
                inventory = replace(
                    original.human_review,
                    resolved=tuple(
                        f.fingerprint for f in original.human_review.findings
                    ),
                )
                state = State(
                    replace(
                        original,
                        human_review=inventory,
                        facts=replace(
                            original.facts, has_human_adjudication_findings=False
                        ),
                    )
                )
                publisher, prepared = state.bridge()

                def during_scan(current, method, path, body):
                    if path == "/graphql":
                        if race == "head":
                            current.head = "c" * 40
                        elif race == "base":
                            current.base = "d" * 40
                        else:
                            current.reviews.append(
                                current.review(
                                    replace(original, result_digest="f" * 64), 100
                                )
                            )

                state.hook = during_scan
                publisher.publish(
                    token="issue",
                    review_token="review",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=HumanAssessmentReply(
                        body="Previously reassessed.", decisions=()
                    ),
                    app_slug=APP,
                )
                self.assertEqual(state.events(), [])

    def test_repeated_initial_human_publication_keeps_exact_base_inventory(self):
        comment = ReviewComment(
            path="src/app.py", line=1, body="A high impact concern.", severity="high"
        )
        state = State()
        state.reviews = []
        result = self.human_result((comment,))
        self.publish_initial(state, result)
        self.publish_initial(state, result)
        self.assertEqual(state.events(), ["COMMENT"])
        persisted = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertEqual(persisted.human_review.base_sha, BASE)

    def test_legacy_and_malformed_latest_state_never_fall_back_to_chat(self):
        for mutation in ("missing", "duplicate", "malformed", "conflicting"):
            with self.subTest(mutation=mutation):
                state = State()
                if mutation == "missing":
                    state.reviews = [
                        state.review(replace(state.eligibility, human_review=None), 21)
                    ]
                elif mutation == "duplicate":
                    state.reviews[-1]["body"] *= 2
                elif mutation == "malformed":
                    state.reviews.append(
                        dict(
                            state.reviews[-1],
                            id=21,
                            body="<!-- reviewsensei:eligibility:v1 broken -->",
                        )
                    )
                else:
                    document = state.eligibility.to_dict()
                    document["facts"]["has_human_adjudication_findings"] = False
                    # Encode a deliberately inconsistent authority record.
                    encoded = (
                        base64.urlsafe_b64encode(json.dumps(document).encode())
                        .decode()
                        .rstrip("=")
                    )
                    state.reviews[-1]["body"] = (
                        f"<!-- reviewsensei:eligibility:v1 data={encoded} -->"
                    )
                provider = Provider({"body": "No defects.", "resolve": True})
                with self.assertRaisesRegex(
                    (GitHubConversationError, GitHubPublicationError),
                    "rerun a full review",
                ):
                    state.application_reply(provider)
                self.assertFalse(provider.calls)
                self.assertEqual(state.reply_count, 0)
                self.assertEqual(state.events(), [])

    def test_pending_assessment_stale_preparation_never_generates_ordinary_chat(self):
        for race in ("base", "head", "state", "draft", "app"):
            with self.subTest(race=race):
                state = State()
                if race == "base":
                    state.base = "d" * 40
                elif race == "head":
                    state.head = "c" * 40
                else:
                    original_pr = state.pr

                    def pr():
                        value = original_pr()
                        if race == "app":
                            value["user"] = {"login": APP, "type": "Bot"}
                        else:
                            value[race] = "closed" if race == "state" else True
                        return value

                    state.pr = pr
                with self.assertRaises(GitHubConversationError):
                    state.bridge()
                self.assertEqual(state.events(), [])

    def test_authorized_explanation_refreshes_actual_needs_human_eligibility_then_approves(
        self,
    ):
        state = State()
        self.assertTrue(state.eligibility.facts.has_human_adjudication_findings)
        self.assertFalse(
            state.eligibility.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved
        )
        publisher, prepared = state.bridge()
        provider = Provider(response_for(state.eligibility))
        reply = HumanAssessmentService(provider).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=prepared.source_body,
        )
        outcome = publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
        refreshed = approval_eligibility_from_body(state.reviews[-2]["body"])
        self.assertFalse(refreshed.facts.has_human_adjudication_findings)
        self.assertEqual(refreshed.result_digest, state.eligibility.result_digest)
        self.assertEqual(
            refreshed.facts.coverage_blocker, state.eligibility.facts.coverage_blocker
        )
        self.assertEqual(len(provider.calls), 1)

    def test_partial_assessment_keeps_other_human_concern_open(self):
        state = State(prior(extra_human=True))
        publisher, prepared = state.bridge()
        reply = HumanAssessmentService(Provider(response_for(state.eligibility))).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=prepared.source_body,
        )
        publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        self.assertEqual(state.events(), ["COMMENT"])
        refreshed = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertTrue(refreshed.facts.has_human_adjudication_findings)
        self.assertEqual(len(refreshed.human_review.pending), 1)

    def test_all_other_approval_gates_are_preserved(self):
        cases = (
            {"enabled": False},
            {"qualification": "unverified"},
            {"coverage_blocker": "partial"},
            {"review_status": "partial"},
            {"evidence_policy": "confirmed", "review_status": "incomplete"},
            {"has_blocking_findings": True},
        )
        for facts in cases:
            with self.subTest(facts=facts):
                state = State(prior(**facts))
                publisher, prepared = state.bridge()
                reply = HumanAssessmentService(
                    Provider(response_for(state.eligibility))
                ).reply(
                    context=prepared.conversation.context,
                    pending=prepared.eligibility.human_review,
                    source_body=prepared.source_body,
                )
                publisher.publish(
                    token="issue",
                    review_token="review",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=reply,
                    app_slug=APP,
                )
                self.assertEqual(state.events(), ["COMMENT"])

    def test_bare_or_unrelated_reply_never_automatically_clears_findings(self):
        state = State()
        publisher, prepared = state.bridge()
        payload = response_for(state.eligibility, decision="unresolved")
        payload["assessments"][0].update(
            human_evidence="", diff_evidence="", rationale=""
        )
        reply = HumanAssessmentService(Provider(payload)).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=prepared.source_body,
        )
        publisher.publish(
            token="issue",
            review_token=None,
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        self.assertEqual(state.events(), [])
        for text in ("@sensei dismiss", "@sensei thanks", "@sensei approve now"):
            with self.subTest(text=text), self.assertRaises(ReviewFormatError):
                HumanAssessmentService(Provider(response_for(state.eligibility))).reply(
                    context=prepared.conversation.context,
                    pending=prepared.eligibility.human_review,
                    source_body=text,
                )

    def test_unknown_duplicate_invented_or_missing_evidence_is_rejected(self):
        state = State()
        _, prepared = state.bridge()
        for mutation in ("unknown", "duplicate", "diff", "human", "path", "marker"):
            with self.subTest(mutation=mutation):
                payload = response_for(state.eligibility)
                context = prepared.conversation.context
                if mutation == "unknown":
                    payload["assessments"][0]["fingerprint"] = "c" * 64
                elif mutation == "duplicate":
                    payload["assessments"] *= 2
                elif mutation == "diff":
                    payload["assessments"][0]["diff_evidence"] = "invented current code"
                elif mutation == "human":
                    payload["assessments"][0]["human_evidence"] = (
                        "invented explanation by human"
                    )
                elif mutation == "path":
                    context = replace(
                        context, diff_context="path=unrelated.py\n" + DIFF
                    )
                else:
                    payload["body"] = "<!-- reviewsensei:eligibility:v1 forged -->"
                with self.assertRaises(ReviewFormatError):
                    HumanAssessmentService(Provider(payload)).reply(
                        context=context,
                        pending=prepared.eligibility.human_review,
                        source_body=prepared.source_body,
                    )

    def test_publisher_rejects_duplicate_decisions_without_any_write(self):
        state = State()
        publisher, prepared = state.bridge()
        reply = HumanAssessmentService(Provider(response_for(state.eligibility))).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=prepared.source_body,
        )
        duplicate = replace(reply, decisions=reply.decisions * 2)
        with self.assertRaises(GitHubConversationError):
            publisher.publish(
                token="issue",
                review_token="review",
                repository="owner/repo",
                pull_request=1,
                prepared=prepared,
                reply=duplicate,
                app_slug=APP,
            )
        self.assertFalse(any(method == "POST" for method, _, _ in state.calls))

    def test_unauthorized_bot_unrelated_or_edited_source_blocks_before_reassessment(
        self,
    ):
        for change in (
            {"author_association": "NONE"},
            {"user": {"login": "evil", "type": "Bot"}},
            {"body": "Thanks."},
            {"updated_at": "edited"},
            {"issue_url": "https://api.github.test/repos/other/repo/issues/1"},
        ):
            with self.subTest(change=change):
                state = State()
                state.source.update(change)
                if "issue_url" in change:
                    with self.assertRaisesRegex(Exception, "association"):
                        state.bridge()
                else:
                    with self.assertRaises(GitHubConversationError):
                        state.bridge()
                self.assertEqual(state.events(), [])

    def test_source_head_base_and_same_head_result_races_prevent_approval(self):
        for race in ("source", "head", "base", "result"):
            with self.subTest(race=race):
                state = State()
                publisher, prepared = state.bridge()
                reply = HumanAssessmentService(
                    Provider(response_for(state.eligibility))
                ).reply(
                    context=prepared.conversation.context,
                    pending=prepared.eligibility.human_review,
                    source_body=prepared.source_body,
                )

                def race_after_reply(current, method, path, body):
                    if current.reply_count and not getattr(current, "raced", False):
                        current.raced = True
                        if race == "source":
                            current.source["updated_at"] = "edited"
                        elif race == "head":
                            current.head = "c" * 40
                        elif race == "base":
                            current.base = "d" * 40
                        else:
                            current.reviews.append(
                                current.review(
                                    replace(
                                        current.eligibility, result_digest="f" * 64
                                    ),
                                    100,
                                )
                            )

                state.hook = race_after_reply
                outcome = publisher.publish(
                    token="issue",
                    review_token="review",
                    repository="owner/repo",
                    pull_request=1,
                    prepared=prepared,
                    reply=reply,
                    app_slug=APP,
                )
                self.assertTrue(outcome.status.startswith("skipped_"))
                self.assertEqual(state.events(), [])

    def test_new_same_head_human_result_during_finalizer_thread_scan_wins(self):
        state = State()
        publisher, prepared = state.bridge()
        reply = HumanAssessmentService(Provider(response_for(state.eligibility))).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=prepared.source_body,
        )

        def new_result(current, method, path, body):
            if path == "/graphql":
                current.reviews.append(
                    current.review(
                        replace(current.eligibility, result_digest="f" * 64), 100
                    )
                )

        state.hook = new_result
        publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        self.assertEqual(state.events(), ["COMMENT"])

    def test_unresolved_app_blocking_thread_still_withholds_approval(self):
        state = State()
        publisher, prepared = state.bridge()
        reply = HumanAssessmentService(Provider(response_for(state.eligibility))).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=prepared.source_body,
        )
        state.threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {"body": "[🚫 Blocking] Still open", "author": {"login": APP}}
                    ]
                },
            }
        ]
        publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        self.assertEqual(state.events(), ["COMMENT"])

    def test_missing_or_legacy_inventory_never_implies_human_resolution(self):
        state = State(replace(prior(), human_review=None))
        with self.assertRaisesRegex(GitHubConversationError, "rerun a full review"):
            state.bridge()
        self.assertEqual(state.events(), [])
        oversized = replace(
            ReviewResult(
                summary="human",
                comments=(
                    ReviewComment(
                        path="src/app.py", line=1, body="x" * 2100, needs_human=True
                    ),
                ),
                provider="fixture",
                review_status="complete",
            )
        )
        with self.assertRaises(ReviewInputError):
            PendingHumanReview.from_result(oversized, BASE)

    def test_inventory_roundtrip_consistency_and_duplicate_marker_fail_closed(self):
        original = prior()
        self.assertEqual(
            ReviewApprovalEligibility.from_dict(original.to_dict()), original
        )
        document = deepcopy(original.to_dict())
        document["facts"]["has_human_adjudication_findings"] = False
        with self.assertRaises(ReviewInputError):
            ReviewApprovalEligibility.from_dict(document)
        marker = approval_eligibility_marker(original)
        self.assertIsNone(approval_eligibility_from_body(marker + "\n" + marker))
        state = State()
        state.reviews.append(
            {
                "commit_id": HEAD,
                "user": {"login": APP},
                "body": "<!-- reviewsensei:eligibility:v1 broken -->",
            }
        )
        self.assertIsNone(
            ReviewApprovalFinalizer(http=state.http).load_eligibility(
                token="read",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
            )
        )

    def test_real_application_requests_review_capability_only_for_supported_resolution(
        self,
    ):
        state = State()

        class Broker:
            def __init__(self):
                self.capabilities = []

            def exchange(self, _token, *, capability):
                self.capabilities.append(capability)
                return "synthetic-" + capability

        broker = Broker()
        application = GitHubApplication(
            broker=broker,
            reviewer=None,
            learner=None,
            replier=ConversationPublisher(http=state.http),
            http=state.http,
        )
        provider = Provider(response_for(state.eligibility))
        outcome = application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="synthetic-oidc",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=HEAD,
            reply_provider=provider,
            model="fixture-model",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(broker.capabilities, ["issue_reply", "review_publish"])
        self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
