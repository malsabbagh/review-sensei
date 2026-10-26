"""Synthetic external GitHub state for the default human-inventory journey.

Fixture closure: test_evidence_capacity, test_publication_recovery,
test_human_assessment, test_review_transaction and fake_github_http. Product
applications, publishers, readers, ledgers and operation stores are not replaced.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

from fake_github_http import json_response
from review_sensei_github import GitHubApplication, GitHubWriteOptions, ReviewPublisher
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.operation_host import RESULT_COMMENT_MARKER, comment_payload
from test_evidence_capacity import human_result, varied_comments
from test_human_assessment import HUMAN
from test_publication_recovery import (
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

from review_sensei.baseline import ReviewBaseline, baseline_finding_from_comment
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import derive_blocker_candidate
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.models import ReviewTransaction
from review_sensei.session import checkpoint_review_analysis, prepare_review_transaction

DEFAULT_INVENTORY_INTENT_MARKER = (
    "<!-- reviewsensei:default-human-inventory-intent:v1 -->"
)


def human_checkpoint(
    ledger, *, count=77, detail=3, gate=None, summary=None, body_findings=0
):
    """Checkpoint the original varied bodies and exact identities publicly."""
    result = human_result(varied_comments(count, detail=detail, unique_paths=32))
    if body_findings:
        result = replace(
            result,
            comments=tuple(
                replace(comment, side="FILE", line=None)
                if index < body_findings
                else comment
                for index, comment in enumerate(result.comments)
            ),
        )
    if summary is not None:
        result = replace(result, summary=summary)
    configuration = trusted_configuration(gate)
    if gate == "partial":
        paths = tuple(sorted({comment.path for comment in result.comments}))
        result = replace(
            result,
            review_status="partial",
            coverage=CoverageManifest(
                files=tuple(
                    FileCoverage(path, "reviewed")
                    if path != paths[-1]
                    else FileCoverage(path, "budget-exhausted", "provider-call-budget")
                    for path in paths
                ),
                enumerated_paths=paths,
            ),
        )
    elif gate == "blocker":
        blocker = replace(
            result.comments[0],
            body="The new file transmits input with send(data) before validation. Reject the input before transmission.",
            path="src/services/required_fix.py",
            symbol="separate_required_fix",
            line=None,
            side="FILE",
            blocking=True,
            needs_human=False,
        )
        result = replace(result, comments=(*result.comments, blocker))
    elif gate == "qualification":
        result = replace(result, provider="openrouter", model=configuration["model"])
    prepared = prepare_review_transaction(
        ledger,
        IDENTITY,
        POLICY,
        reservation_id="d" * 64,
        base_sha=BASE,
        head_sha=HEAD,
        configuration_digest=ReviewTransaction.compute_configuration_digest(
            configuration
        ),
        evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
    )
    key = ReviewContextCacheKey(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        base_sha=BASE,
        head_sha=HEAD,
        engine=result.provider,
        model=configuration["model"],
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
        ledger,
        IDENTITY,
        prepared,
        result,
        baseline=None if gate == "partial" else baseline,
    ), key


def trusted_configuration(gate=None):
    if gate != "qualification":
        return CONFIGURATION
    return {
        **CONFIGURATION,
        "provider": {
            **CONFIGURATION["provider"],
            "name": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
        },
        "model": "synthetic/unqualified-model",
    }


def located_files(comments):
    """Valid additions cover original line numbers; finding IDs never change."""
    files = []
    for path in sorted({comment.path for comment in comments}):
        lines = max(comment.line or 1 for comment in comments if comment.path == path)
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
        # The separate file-level blocker remains in the review body and its
        # durable gate flag; this profile does not activate native FILE comments.
        self.files = located_files(tuple(c for c in comments if c.needs_human))
        if any(c.blocks_approval and not c.needs_human for c in comments):
            self.files.append(
                {
                    "filename": "src/services/required_fix.py",
                    "status": "added",
                    "patch": "@@ -0,0 +1 @@\n+return send(data)\n",
                    "additions": 1,
                    "deletions": 0,
                }
            )
        self.timeouts = []
        self.source["user"]["id"] = 123
        self.source["created_at"] = "2026-10-19T00:00:00Z"
        self.source["updated_at"] = "2026-10-19T00:00:00Z"

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
        if (
            request.method == "POST"
            and path.endswith("/issues/1/comments")
            and self.hook
        ):
            self.hook(self, request.method, path, body)
        if request.method == "GET" and path == "/user":
            self.calls.append((request.method, path, body))
            return json_response(self.app_user)
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
            return json_response(
                review,
                200 if "comments" in body or body["event"] == "APPROVE" else 201,
            )
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


def publish_human(state, ledger, result, key, *, gate=None, **kwargs):
    candidates = None
    if gate == "blocker":
        # Independent synthetic code evidence: the separate added file above
        # actually sends before validation. Use public structured C2 admission;
        # a model's blocking boolean alone must not mint a blocker.
        candidates = tuple(
            derive_blocker_candidate(
                comment,
                on_changed_path=True,
                evidence_locations_validated=not comment.needs_human,
                has_failure_condition=not comment.needs_human,
                has_specific_violation=not comment.needs_human,
                has_actionable_remedy=not comment.needs_human,
            )
            for comment in result.comments
        )
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
        current_key=None if gate == "partial" else key,
        configuration_context=trusted_configuration(gate),
        evidence_context=EVIDENCE,
        blocker_candidates=candidates,
        **kwargs,
    )


def reply_human(state, ledger, provider, *, evidence_budget=None):
    """Fresh real application; its actual default comment store owns replay."""
    return application(state, ledger).generate_and_publish_reply(
        options=GitHubWriteOptions(github_writes=True, mention_replies=True),
        oidc_token="synthetic-oidc",
        read_token="synthetic-read",
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        source_comment_id=state.source["id"],
        source_updated_at=state.source["updated_at"],
        expected_head_sha=HEAD,
        reply_provider=provider,
        model="fixture",
        app_slug=APP,
        root_comment_id=state.source["id"],
        source_kind="issue",
        work_budgets=ReviewWorkBudgets(mode="unified"),
        operation_entry="enabled",
        repository_id=IDENTITY.repository_id,
        app_id=7,
        run_id="synthetic-source:" + state.source["updated_at"],
        **({"evidence_budget": evidence_budget} if evidence_budget is not None else {}),
    )


def next_mention(state, revision):
    """A real new source revision, never a fresh allowance for the old event."""
    state.source["updated_at"] = (
        f"2026-10-19T00:{revision // 60:02}:{revision % 60:02}Z"
    )
    state.source["body"] = HUMAN + f" This is authorized source revision {revision}."


def human_inventory_parts(state):
    """Select actual public envelope parts, never unrelated retained result bytes."""
    groups = {}
    for comment in state.parts:
        if RESULT_COMMENT_MARKER not in comment["body"]:
            continue
        document = json.loads(comment_payload(comment["body"]))
        groups.setdefault(document["event_key"], []).append((comment, document))
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item[1]["index"])
        raw = b"".join(base64.b64decode(item[1]["data"]) for item in ordered)
        document = json.loads(raw)
        if (
            document.get("purpose") == "default-human-inventory"
            and isinstance(document.get("document"), dict)
            and isinstance(document["document"].get("human_inventory"), dict)
            and document["document"]["human_inventory"].get("kind") == "human-inventory"
        ):
            return [item[0] for item in ordered]
    raise AssertionError(
        "default publication did not retain a human-inventory envelope"
    )
