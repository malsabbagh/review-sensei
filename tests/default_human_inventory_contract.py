"""Synthetic external GitHub state for the default human-inventory journey.

Fixture closure: test_evidence_capacity, test_publication_recovery,
test_human_assessment, test_review_transaction and fake_github_http. Product
applications, publishers, readers, ledgers and operation stores are not replaced.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from review_sensei_github import GitHubApplication, GitHubWriteOptions, ReviewPublisher
from review_sensei_github.conversation import ConversationPublisher

from review_sensei.baseline import ReviewBaseline, baseline_finding_from_comment
from review_sensei.context import ReviewContextCacheKey
from review_sensei.models import ReviewTransaction
from review_sensei.session import checkpoint_review_analysis, prepare_review_transaction
from tests.fake_github_http import json_response
from tests.test_evidence_capacity import human_result, varied_comments
from tests.test_publication_recovery import (
    APP,
    BASE,
    CONFIGURATION,
    EVIDENCE,
    HEAD,
    IDENTITY,
    POLICY,
    RetentionState,
    _Broker,
    _Noop,
)


def human_checkpoint(ledger, *, count=77, detail=3):
    """Checkpoint the original varied bodies and exact identities publicly."""
    result = human_result(varied_comments(count, detail=detail, unique_paths=32))
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
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
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
                (baseline_finding_from_comment(c) for c in result.comments),
                key=lambda finding: finding.fingerprint,
            )
        ),
        reviewed_paths=tuple(sorted({c.path for c in result.comments})),
    )
    return checkpoint_review_analysis(
        ledger, IDENTITY, prepared, result, baseline=baseline
    ), key


def located_files(comments):
    """Valid additions cover original line numbers; finding IDs never change."""
    files = []
    for path in sorted({comment.path for comment in comments}):
        lines = max(comment.line for comment in comments if comment.path == path)
        patch = (
            f"@@ -0,0 +1,{lines} @@\n+return reject_remote_requests(data)\n"
            + "+pass\n" * (lines - 1)
        )
        files.append(
            {
                "filename": path,
                "status": "added",
                "patch": patch,
                "additions": lines,
                "deletions": 0,
            }
        )
    return files


class DefaultHumanState(RetentionState):
    """Paginated, numeric-Bot-owned external records, including inline prose."""

    def __init__(self, comments):
        super().__init__()
        self.reviews = []
        self.inline = []
        self.files = located_files(comments)
        self.timeouts = []
        self.source["user"]["id"] = 123

    @property
    def diff(self):
        return "".join(
            f"diff --git a/{item['filename']} b/{item['filename']}\n"
            f"new file mode 100644\n--- /dev/null\n+++ b/{item['filename']}\n"
            + item["patch"]
            for item in self.files
        )

    @diff.setter
    def diff(self, value):
        # State initializes its ordinary one-file fallback; this fixture always
        # exposes actual valid files through the public GitHub endpoint instead.
        pass

    def open(self, request, timeout):
        self.timeouts.append(timeout)
        parsed = urlparse(request.full_url)
        path = parsed.path
        body = json.loads(request.data) if request.data else None
        values = None
        if request.method == "GET":
            if path.endswith("/pulls/1/reviews"):
                values = self.reviews
            elif path.endswith("/pulls/1/comments"):
                values = self.inline
            elif path.endswith("/pulls/1/files"):
                values = self.files
            elif path.endswith("/issues/1/comments"):
                values = [self.source, *self.parts, *self.replies]
                if self.session_comment is not None:
                    values.append(self.session_comment)
            elif "/pulls/1/reviews/" in path:
                identifier = int(path.split("/reviews/", 1)[1].split("/", 1)[0])
                if path.endswith("/comments"):
                    values = [
                        item
                        for item in self.inline
                        if item["pull_request_review_id"] == identifier
                    ]
                else:
                    review = next(
                        item for item in self.reviews if item["id"] == identifier
                    )
                    self.calls.append((request.method, path, body))
                    return json_response(review)
        if values is not None:
            self.calls.append((request.method, path, body))
            if self.hook:
                self.hook(self, request.method, path, body)
            query = parse_qs(parsed.query)
            size = int(query.get("per_page", [100])[0])
            offset = (int(query.get("page", [1])[0]) - 1) * size
            return json_response(values[offset : offset + size])
        if request.method == "POST" and path.endswith("/pulls/1/reviews"):
            self.calls.append((request.method, path, body))
            if self.hook:
                self.hook(self, request.method, path, body)
            review = {
                "id": 40 + len(self.reviews),
                "commit_id": body["commit_id"],
                "state": "APPROVED" if body["event"] == "APPROVE" else "COMMENTED",
                "body": body["body"],
                "user": dict(self.app_user),
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
            }
            self.reviews.append(review)
            for item in body.get("comments", []):
                self.inline.append(
                    {
                        **item,
                        "id": 2000 + len(self.inline),
                        "pull_request_review_id": review["id"],
                        "commit_id": body["commit_id"],
                        "original_commit_id": body["commit_id"],
                        "user": dict(self.app_user),
                        "pull_request_url": review["pull_request_url"],
                    }
                )
            return json_response(review, 200 if "comments" in body else 201)
        if request.method == "PATCH" and "/issues/comments/" in path:
            identifier = int(path.rsplit("/", 1)[1])
            item = next((item for item in self.parts if item["id"] == identifier), None)
            if item is not None:
                self.calls.append((request.method, path, body))
                item["body"] = body["body"]
                return json_response(item)
        return super().open(request, timeout)


def application(state, ledger):
    return GitHubApplication(
        broker=_Broker(),
        http=state.http,
        reviewer=ReviewPublisher(http=state.http),
        learner=_Noop(),
        replier=ConversationPublisher(http=state.http),
        session_ledger=ledger,
    )


def publish_human(state, ledger, result, key):
    return application(state, ledger).publish_review(
        options=GitHubWriteOptions(
            github_writes=True, auto_review=True, complete_inventory=True
        ),
        oidc_token="synthetic",
        repository=IDENTITY.repository,
        repository_id=IDENTITY.repository_id,
        pull_request=IDENTITY.pull_request,
        head_sha=HEAD,
        base_sha=BASE,
        base_branch="main",
        result=result,
        diff=state.diff,
        app_slug=APP,
        convergence_policy=POLICY,
        current_key=key,
        configuration_context=CONFIGURATION,
        evidence_context=EVIDENCE,
    )
