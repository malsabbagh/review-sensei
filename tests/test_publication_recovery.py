"""Public default publication keeps exact checkpoint bytes across process loss."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from review_sensei_github import GitHubApplication, GitHubWriteOptions, ReviewPublisher
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.operation_host import (
    RESULT_COMMENT_MARKER,
    comment_payload,
    render_marked_comment,
)
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
    finding_index_marker,
    finding_instance_marker,
    finding_marker,
    parse_finding_index,
)
from review_sensei_github.publication_recovery import PublicationBudgetHttp

from review_sensei.baseline import ReviewBaseline, baseline_finding_from_comment
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import ReviewConvergencePolicy, derive_blocker_candidate
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
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
        self.session_comment = None
        self.retention_fault_event = None

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        path = parsed.path
        if (
            path.casefold()
            == ("/users/" + APP.replace("[", "%5B").replace("]", "%5D")).casefold()
        ):
            self.calls.append((request.method, path, None))
            return json_response(self.app_user)
        if path.endswith("/issues/1/comments"):
            self.calls.append((request.method, path, None))
            self.part_requests.append(request.method)
            if request.method == "GET":
                page = int(parse_qs(parsed.query).get("page", [1])[0])
                comments = (
                    [self.session_comment] if self.session_comment else []
                ) + self.parts
                return json_response(comments[(page - 1) * 100 : page * 100])
            body = json.loads(request.data)["body"]
            item = {
                "id": 700 + len(self.parts),
                "body": body,
                "user": self.app_user,
                "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            }
            # This fault targets raw result retention. Admission intent and
            # evidence have a distinct public fault matrix.
            fault = None
            if RESULT_COMMENT_MARKER in body:
                event = json.loads(comment_payload(body))["event_key"]
                if self.retention_fault_event is None:
                    self.retention_fault_event = event
                if event == self.retention_fault_event:
                    fault = self.fail_part
            if fault == "crash-before":
                raise KeyboardInterrupt()
            if fault == "missing":
                return json_response({}, 500)
            self.parts.append(item)
            if fault == "crash-after":
                raise KeyboardInterrupt()
            if fault == "lost":
                return json_response({}, 500)
            if fault == "echo-missing":
                return json_response({"id": item["id"]}, 201)
            return json_response(item, 201)
        if request.method == "GET" and "/issues/comments/" in path:
            identifier = int(path.rsplit("/", 1)[1])
            for part in self.parts:
                if part["id"] == identifier:
                    self.calls.append(("GET", path, None))
                    return json_response(part)
        if self.session_comment is not None and path.endswith("/issues/comments/600"):
            self.calls.append((request.method, path, None))
            if request.method == "PATCH":
                self.session_comment["body"] = json.loads(request.data)["body"]
            return json_response(self.session_comment)
        return super().open(request, timeout)


class SupersessionState(RetentionState):
    """Evolving native threads, exact inline readback, and a remote session."""

    def __init__(self, result, *, old_count=79):
        super().__init__()
        self.reviews = []
        self.inline = []
        self.resolved = []
        self.cleanup_hook = None
        self.mutation_mode = None
        self.mutation_attempts = []
        self.fail_completion = False
        comment = result.comments[0]
        concern = finding_lifecycle_for_comment(comment).fingerprint
        body = (
            finding_instance_marker(comment)
            + "\n"
            + finding_marker(
                repository_id=1,
                pull_request=1,
                head_sha="c" * 40,
                base_sha=BASE,
                result=result,
                blocking=comment.blocks_approval,
                fingerprint=concern,
                needs_human=comment.needs_human,
            )
        )
        self.roots = {
            1000 + i: {
                "id": 1000 + i,
                "body": body,
                "user": self.app_user,
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
            }
            for i in range(old_count)
        }
        self.threads = [
            {
                "id": f"OLD_{i}",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": identifier,
                            "body": root["body"],
                            "author": {"login": APP, "__typename": "Bot"},
                        }
                    ]
                },
            }
            for i, (identifier, root) in enumerate(self.roots.items())
        ]

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        path = parsed.path
        body = json.loads(request.data) if request.data else None
        if (
            self.fail_completion
            and self.reviews
            and request.method == "PATCH"
            and path.endswith("/issues/comments/600")
        ):
            self.calls.append((request.method, path, body))
            return json_response({}, 500)
        if path == "/graphql":
            self.calls.append((request.method, path, body))
            operation = body["operationName"]
            if operation == "ResolveReviewThread":
                thread_id = body["variables"]["input"]["threadId"]
                self.mutation_attempts.append(thread_id)
                if self.mutation_mode != "unconfirmed":
                    self.resolved.append(thread_id)
                    for node in self.threads:
                        if node["id"] == thread_id:
                            node["isResolved"] = True
                if self.mutation_mode is not None:
                    return json_response({}, 500)
                return json_response(
                    {
                        "data": {
                            "resolveReviewThread": {
                                "thread": {"id": thread_id, "isResolved": True}
                            }
                        }
                    }
                )
            if operation == "ResolutionReadback":
                thread_id = body["variables"]["id"]
                node = next(n for n in self.threads if n["id"] == thread_id)
                return json_response(
                    {
                        "data": {
                            "node": {"id": thread_id, "isResolved": node["isResolved"]}
                        }
                    }
                )
            first = body["variables"].get("first", 100)
            start = int(body["variables"].get("after") or 0)
            end = start + first
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": copy.deepcopy(self.threads[start:end]),
                                    "pageInfo": {
                                        "hasNextPage": end < len(self.threads),
                                        "endCursor": str(end),
                                    },
                                }
                            }
                        }
                    }
                }
            )
        if request.method == "GET" and "/pulls/comments/" in path:
            self.calls.append((request.method, path, body))
            if self.cleanup_hook:
                self.cleanup_hook(self)
            return json_response(self.roots[int(path.rsplit("/", 1)[1])])
        if (
            request.method == "GET"
            and "/reviews/" in path
            and path.endswith("/comments")
        ):
            self.calls.append((request.method, path, body))
            review_id = int(path.split("/")[-2])
            return json_response(
                [c for c in self.inline if c["pull_request_review_id"] == review_id]
            )
        response = super().open(request, timeout)
        if (
            request.method == "POST"
            and path.endswith("/reviews")
            and "comments" in body
        ):
            review_id = self.reviews[-1]["id"]
            for value in body["comments"]:
                identifier = 2000 + len(self.inline)
                root = {
                    "id": identifier,
                    "body": value["body"],
                    "user": self.app_user,
                    "pull_request_review_id": review_id,
                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                }
                self.inline.append(root)
                self.roots[identifier] = root
                self.threads.append(
                    {
                        "id": f"CURRENT_{identifier}",
                        "isResolved": False,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": identifier,
                                    "body": value["body"],
                                    "author": {"login": APP, "__typename": "Bot"},
                                }
                            ]
                        },
                    }
                )
        return response


def checkpoint(
    ledger, count=77, *, body_size=0, blocking=False, line=2, needs_human=False
):
    comments = tuple(
        ReviewComment(
            path="src/app.py",
            line=line,
            body=f"Independent required decision {i}." + "x" * body_size,
            symbol=f"operation_{i}",
            defect_kind=f"case-{i}",
            needs_human=needs_human,
            blocking=blocking,
            severity="high" if blocking else None,
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


def publish(app, result, key, *, options=None, app_slug=APP, **kwargs):
    return app.publish_review(
        options=options or GitHubWriteOptions(auto_review=True, github_writes=True),
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
        app_slug=app_slug,
        convergence_policy=POLICY,
        current_key=key,
        configuration_context=CONFIGURATION,
        evidence_context=EVIDENCE,
        **kwargs,
    )


def recover(app, ledger, *, app_slug=APP, **kwargs):
    return app.recover_retained_review_result(
        token="read-token",
        repository="owner/repo",
        repository_id=1,
        pull_request=1,
        base_sha=BASE,
        head_sha=HEAD,
        app_slug=app_slug,
        ledger=ledger,
        configuration_context=CONFIGURATION,
        evidence_context=EVIDENCE,
        convergence_policy=POLICY,
        **kwargs,
    )


class PublicationRecoveryTests(unittest.TestCase):
    def test_optional_supersession_cannot_starve_durable_completion(self):
        from review_sensei_github.session_ledger import (
            GitHubIssueCommentSessionLedger,
            render_session_comment,
        )

        with tempfile.TemporaryDirectory() as directory:
            seed = LocalSessionLedger(Path(directory))
            result, key = checkpoint(seed, count=1, line=1, blocking=True)
            state = SupersessionState(result)
            state.session_comment = {
                "id": 600,
                "body": render_session_comment(
                    repository_id=1, pull_request=1, record=seed.load(IDENTITY).record
                ),
                "user": state.app_user,
                "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            }
            ledger = GitHubIssueCommentSessionLedger(
                state.http, token="read", app_slug=APP
            )
            budget = EvidenceReadBudget()
            outcome = publish(
                application(state, ledger),
                result,
                key,
                publication_budget=budget,
                blocker_candidates=(
                    derive_blocker_candidate(
                        result.comments[0],
                        on_changed_path=True,
                        evidence_locations_validated=True,
                        has_failure_condition=True,
                        has_actionable_remedy=True,
                        has_specific_violation=True,
                    ),
                ),
                options=GitHubWriteOptions(
                    auto_review=True, github_writes=True, complete_inventory=True
                ),
            )
            self.assertEqual(outcome.status, "published")
            self.assertEqual(outcome.thread_resolution, "unconfirmed")
            self.assertEqual(
                ledger.load(IDENTITY).record.transaction.phase, "publication_succeeded"
            )
            self.assertEqual(budget.calls, len(state.calls) - 1)
            self.assertLessEqual(budget.calls, 60)
            self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_same_head_terminal_retries_resume_cleanup_without_duplicate_publication(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger, count=1, line=1, blocking=True)
            candidate = derive_blocker_candidate(
                result.comments[0],
                on_changed_path=True,
                evidence_locations_validated=True,
                has_failure_condition=True,
                has_actionable_remedy=True,
                has_specific_violation=True,
            )
            state = SupersessionState(result, old_count=9)
            previous = 9
            initial_posts = None
            for attempt in range(10):
                state.calls.clear()
                budget = EvidenceReadBudget()
                outcome = publish(
                    application(state, ledger),
                    result,
                    key,
                    publication_budget=budget,
                    blocker_candidates=(candidate,),
                    options=GitHubWriteOptions(
                        auto_review=True, github_writes=True, complete_inventory=True
                    ),
                )
                self.assertEqual(
                    outcome.status, "published" if attempt == 0 else "already_published"
                )
                self.assertEqual(
                    ledger.load(IDENTITY).record.transaction.phase,
                    "publication_succeeded",
                )
                self.assertEqual(
                    ledger.load(IDENTITY).record.completed_initial_reviews, 1
                )
                self.assertEqual(budget.calls, len(state.calls))
                self.assertLessEqual(budget.calls, 60)
                pending = sum(
                    not n["isResolved"]
                    for n in state.threads
                    if n["id"].startswith("OLD_")
                )
                self.assertLess(
                    pending, previous, "an explicit retry must make bounded progress"
                )
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
                self.assertEqual(outcome.diagnostic, "required_fixes_open")
                self.assertTrue(
                    all(
                        not n["isResolved"]
                        for n in state.threads
                        if n["id"].startswith("CURRENT_")
                    )
                )
                self.assertEqual(len(state.reviews), 1)
                self.assertEqual(len(state.inline), 1)
                posts = state.part_requests.count("POST")
                if initial_posts is None:
                    initial_posts = posts
                self.assertEqual(posts, initial_posts)
                self.assertEqual(len(state.resolved), len(set(state.resolved)))
                if not pending:
                    self.assertIsNone(outcome.thread_resolution)
                    break
                self.assertEqual(outcome.thread_resolution, "unconfirmed")
                previous = pending
            else:
                self.fail("bounded explicit retries did not finish the finite backlog")
            self.assertGreater(attempt, 0)

    def test_optional_cleanup_keeps_root_head_base_and_latest_eligibility_fences(self):
        for fault in ("root", "owner", "head", "base", "eligibility"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger, count=1, line=1, blocking=True)
                candidate = derive_blocker_candidate(
                    result.comments[0],
                    on_changed_path=True,
                    evidence_locations_validated=True,
                    has_failure_condition=True,
                    has_actionable_remedy=True,
                    has_specific_violation=True,
                )
                state = SupersessionState(result, old_count=1)

                def race(host):
                    host.cleanup_hook = None
                    if fault == "root":
                        host.roots[1000]["body"] += "\nEdited old root"
                    elif fault == "owner":
                        host.roots[1000]["user"] = {"login": "alice", "type": "User"}
                    elif fault == "head":
                        host.head = "d" * 40
                    elif fault == "base":
                        host.base = "d" * 40
                    else:
                        review = host.reviews[-1]
                        eligibility = approval_eligibility_from_body(review["body"])
                        self.assertIsNotNone(eligibility)
                        changed = replace(
                            eligibility, facts=replace(eligibility.facts, enabled=False)
                        )
                        review["body"] = review["body"].replace(
                            approval_eligibility_marker(eligibility),
                            approval_eligibility_marker(changed),
                        )

                state.cleanup_hook = race
                budget = EvidenceReadBudget()
                outcome = publish(
                    application(state, ledger),
                    result,
                    key,
                    publication_budget=budget,
                    blocker_candidates=(candidate,),
                    options=GitHubWriteOptions(
                        auto_review=True, github_writes=True, complete_inventory=True
                    ),
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(outcome.thread_resolution, "unconfirmed")
                self.assertEqual(
                    ledger.load(IDENTITY).record.transaction.phase,
                    "publication_succeeded",
                )
                self.assertEqual(state.mutation_attempts, [])
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
                self.assertEqual(budget.calls, len(state.calls))
                self.assertLessEqual(budget.calls, 60)

    def test_optional_cleanup_reconciles_ambiguous_mutation_once(self):
        for mode, confirmed in (("lost", True), ("unconfirmed", False)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger, count=1, line=1, blocking=True)
                candidate = derive_blocker_candidate(
                    result.comments[0],
                    on_changed_path=True,
                    evidence_locations_validated=True,
                    has_failure_condition=True,
                    has_actionable_remedy=True,
                    has_specific_violation=True,
                )
                state = SupersessionState(result, old_count=1)
                state.mutation_mode = mode
                budget = EvidenceReadBudget()
                outcome = publish(
                    application(state, ledger),
                    result,
                    key,
                    publication_budget=budget,
                    blocker_candidates=(candidate,),
                    options=GitHubWriteOptions(
                        auto_review=True, github_writes=True, complete_inventory=True
                    ),
                )
                self.assertEqual(state.mutation_attempts, ["OLD_0"])
                self.assertEqual(state.threads[0]["isResolved"], confirmed)
                self.assertEqual(
                    outcome.thread_resolution, None if confirmed else "unconfirmed"
                )
                self.assertEqual(outcome.diagnostic, "required_fixes_open")
                self.assertEqual(
                    ledger.load(IDENTITY).record.transaction.phase,
                    "publication_succeeded",
                )
                self.assertEqual(
                    sum(
                        body.get("operationName") == "ResolutionReadback"
                        for _, _, body in state.calls
                        if isinstance(body, dict)
                    ),
                    1,
                )
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
                self.assertEqual(budget.calls, len(state.calls))

    def test_terminal_cleanup_retry_requires_exact_original_root_index_and_inline_prose(
        self,
    ):
        for fault in ("index", "inline", "owner"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger, count=1, line=1, blocking=True)
                candidate = derive_blocker_candidate(
                    result.comments[0],
                    on_changed_path=True,
                    evidence_locations_validated=True,
                    has_failure_condition=True,
                    has_actionable_remedy=True,
                    has_specific_violation=True,
                )
                state = SupersessionState(result, old_count=9)
                options = GitHubWriteOptions(
                    auto_review=True, github_writes=True, complete_inventory=True
                )
                publish(
                    application(state, ledger),
                    result,
                    key,
                    publication_budget=EvidenceReadBudget(),
                    blocker_candidates=(candidate,),
                    options=options,
                )
                self.assertGreater(len(state.resolved), 0)
                previous = tuple(state.resolved)
                parts = state.part_requests.count("POST")
                if fault == "index":
                    state.reviews[0]["body"] = state.reviews[0]["body"].replace(
                        "reviewsensei:finding-index:v1", "removed-index", 1
                    )
                elif fault == "inline":
                    state.inline[0]["body"] += "\nEdited explanation"
                else:
                    state.reviews[0]["user"] = {"login": APP, "type": "User"}
                state.calls.clear()
                with self.assertRaises(GitHubPublicationError):
                    publish(
                        application(state, ledger),
                        result,
                        key,
                        publication_budget=EvidenceReadBudget(),
                        blocker_candidates=(candidate,),
                        options=options,
                    )
                self.assertEqual(tuple(state.resolved), previous)
                self.assertEqual(state.part_requests.count("POST"), parts)
                self.assertEqual(len(state.reviews), 1)
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
                self.assertEqual(
                    ledger.load(IDENTITY).record.transaction.phase,
                    "publication_succeeded",
                )

    def test_publication_completion_preserves_current_body_and_human_obligations(self):
        for line, human in ((2, False), (1, True)):
            with (
                self.subTest(line=line, human=human),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger, count=1, line=line, blocking=True)
                candidate = derive_blocker_candidate(
                    result.comments[0],
                    on_changed_path=True,
                    evidence_locations_validated=True,
                    has_failure_condition=True,
                    has_actionable_remedy=True,
                    has_specific_violation=True,
                )
                if human:
                    candidate = replace(candidate, needs_human=True)
                state = SupersessionState(result, old_count=1)
                outcome = publish(
                    application(state, ledger),
                    result,
                    key,
                    publication_budget=EvidenceReadBudget(),
                    blocker_candidates=(candidate,),
                    options=GitHubWriteOptions(
                        auto_review=True, github_writes=True, complete_inventory=True
                    ),
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(
                    ledger.load(IDENTITY).record.transaction.phase,
                    "publication_succeeded",
                )
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
                self.assertEqual(
                    outcome.diagnostic,
                    "human_adjudication_open" if human else "required_fixes_open",
                )
                eligibility = approval_eligibility_from_body(state.reviews[0]["body"])
                self.assertIsNotNone(eligibility)
                if human:
                    self.assertTrue(eligibility.facts.has_human_adjudication_findings)
                else:
                    self.assertTrue(eligibility.facts.has_blocking_findings)

    def test_cleanup_admission_preserves_mutation_readback_liability(self):
        from review_sensei_github.thread_resolution import FindingThreadResolver

        for phase in ("entry", "after-fence"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                ledger = LocalSessionLedger(Path(directory))
                result, _ = checkpoint(ledger, count=1, line=1, blocking=True)
                state = SupersessionState(result, old_count=1)
                budget = EvidenceReadBudget()
                # These charges represent work already performed by this caller.
                if phase == "entry":
                    for _ in range(55):
                        budget.consume()

                def fence():
                    if phase == "after-fence":
                        while budget.calls < 59:
                            budget.consume()
                    return True

                outcome = FindingThreadResolver(
                    PublicationBudgetHttp(state.http, budget)
                ).resolve(
                    token="token",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    head_sha=HEAD,
                    base_sha=BASE,
                    app_slug=APP,
                    instances=frozenset(),
                    supersede={
                        "OLD_0": finding_lifecycle_for_comment(
                            result.comments[0]
                        ).fingerprint
                    },
                    fence=fence,
                    skip_resolved=True,
                    cleanup_budget=budget,
                )
                self.assertEqual(outcome.unconfirmed, ("OLD_0",))
                self.assertEqual(outcome.resolved, ())
                self.assertEqual(state.mutation_attempts, [])
                self.assertEqual(budget.calls, 56 if phase == "entry" else 59)
                self.assertFalse(state.threads[0]["isResolved"])

    def test_mandatory_durable_completion_failure_never_runs_optional_cleanup(self):
        from review_sensei_github.session_ledger import (
            GitHubIssueCommentSessionLedger,
            render_session_comment,
        )

        with tempfile.TemporaryDirectory() as directory:
            seed = LocalSessionLedger(Path(directory))
            result, key = checkpoint(seed, count=1, line=1, blocking=True)
            candidate = derive_blocker_candidate(
                result.comments[0],
                on_changed_path=True,
                evidence_locations_validated=True,
                has_failure_condition=True,
                has_actionable_remedy=True,
                has_specific_violation=True,
            )
            state = SupersessionState(result, old_count=1)
            state.session_comment = {
                "id": 600,
                "body": render_session_comment(
                    repository_id=1, pull_request=1, record=seed.load(IDENTITY).record
                ),
                "user": state.app_user,
                "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            }
            state.fail_completion = True
            ledger = GitHubIssueCommentSessionLedger(
                state.http, token="read", app_slug=APP
            )
            with self.assertRaises((GitHubPublicationError, ReviewInputError)):
                publish(
                    application(state, ledger),
                    result,
                    key,
                    publication_budget=EvidenceReadBudget(),
                    blocker_candidates=(candidate,),
                    options=GitHubWriteOptions(
                        auto_review=True, github_writes=True, complete_inventory=True
                    ),
                )
            self.assertEqual(state.mutation_attempts, [])
            self.assertEqual(state.resolved, [])
            self.assertEqual(len(state.reviews), 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
            self.assertNotEqual(
                ledger.load(IDENTITY).record.transaction.phase, "publication_succeeded"
            )

    def test_terminal_cleanup_does_not_borrow_a_newer_results_eligibility(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger, count=1, line=1, blocking=True)
            candidate = derive_blocker_candidate(
                result.comments[0],
                on_changed_path=True,
                evidence_locations_validated=True,
                has_failure_condition=True,
                has_actionable_remedy=True,
                has_specific_violation=True,
            )
            state = SupersessionState(result, old_count=9)
            options = GitHubWriteOptions(
                auto_review=True, github_writes=True, complete_inventory=True
            )
            publish(
                application(state, ledger),
                result,
                key,
                publication_budget=EvidenceReadBudget(),
                blocker_candidates=(candidate,),
                options=options,
            )
            previous = tuple(state.resolved)
            original = state.reviews[0]
            eligibility = approval_eligibility_from_body(original["body"])
            index = parse_finding_index(
                original["body"], repository_id=1, pull_request=1, head_sha=HEAD
            )
            changed = replace(eligibility, result_digest="e" * 64)
            later = copy.deepcopy(original)
            later["id"] = 9999
            later["body"] = (
                approval_eligibility_marker(changed)
                + "\n"
                + finding_index_marker(
                    repository_id=1,
                    pull_request=1,
                    head_sha=HEAD,
                    base_sha=BASE,
                    result_digest=changed.result_digest,
                    entries=index.entries,
                    complete_inventory=True,
                )
            )
            state.reviews.append(later)
            outcome = publish(
                application(state, ledger),
                result,
                key,
                publication_budget=EvidenceReadBudget(),
                blocker_candidates=(candidate,),
                options=options,
            )
            self.assertEqual(outcome.status, "already_published")
            self.assertEqual(outcome.diagnostic, "approval_withheld")
            self.assertEqual(tuple(state.resolved), previous)
            self.assertEqual(outcome.thread_resolution, "unconfirmed")
            self.assertEqual(len(state.reviews), 2)
            self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_configured_bot_login_case_preserves_numeric_owned_publication(self):
        for configured in (
            "REVIEW-SENSEI[bot]",
            "ReViEw-SeNsEi[bot]",
            APP.upper(),
            "ReViEw-SeNsEi[BoT]",
        ):
            with (
                self.subTest(configured=configured),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger, 1)
                state = RetentionState()
                state.reviews = []
                self.assertEqual(state.app_user["login"], "review-sensei[bot]")
                outcome = publish(
                    application(state, ledger), result, key, app_slug=configured
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(
                    len(state.parts), 3
                )  # result, intent and admission evidence
                self.assertTrue(all(item["user"]["id"] == 99 for item in state.parts))
                self.assertEqual(state.parts[0]["user"]["id"], 99)
                self.assertEqual(state.parts[0]["user"]["type"], "Bot")
                self.assertEqual(
                    state.parts[0]["issue_url"],
                    "https://api.github.test/repos/owner/repo/issues/1",
                )
                self.assertEqual(
                    recover(
                        application(state, ledger), ledger, app_slug=configured
                    ).content_digest(),
                    result.content_digest(),
                )

    def test_multipart_fallback_readbacks_are_preflighted_and_charged(self):
        for initial_calls in (0, 20):
            with (
                self.subTest(initial_calls=initial_calls),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger, body_size=2000)
                state = RetentionState()
                state.fail_part = "echo-missing"
                budget = EvidenceReadBudget()
                for _ in range(initial_calls):
                    budget.consume()
                with self.assertRaisesRegex(
                    GitHubPublicationError,
                    "liability" if initial_calls else "finding index",
                ):
                    publish(
                        application(state, ledger),
                        result,
                        key,
                        publication_budget=budget,
                    )
                self.assertEqual(budget.calls - initial_calls, len(state.calls))
                self.assertLessEqual(budget.calls, 60)
                if initial_calls:
                    self.assertEqual(state.part_requests.count("POST"), 0)
                else:
                    self.assertGreaterEqual(len(state.parts), 4)
                    self.assertEqual(
                        len(
                            [
                                p
                                for m, p, _ in state.calls
                                if m == "GET" and "/issues/comments/" in p
                            ]
                        ),
                        sum(
                            RESULT_COMMENT_MARKER in item["body"]
                            and json.loads(comment_payload(item["body"]))["event_key"]
                            == state.retention_fault_event
                            for item in state.parts
                        ),
                    )
                    self.assertEqual(
                        recover(application(state, ledger), ledger).content_digest(),
                        result.content_digest(),
                    )

    def test_partial_checkpoint_publishes_prose_and_replays_without_clean_authority(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
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
            result = checkpoint_review_analysis(
                ledger,
                IDENTITY,
                prepared,
                ReviewResult(
                    summary="Partial validated work.",
                    comments=(
                        ReviewComment(
                            path="src/app.py", line=2, body="Retained partial finding."
                        ),
                    ),
                    provider="fixture",
                    model="fixture-model",
                    review_status="partial",
                    coverage=CoverageManifest(
                        files=(
                            FileCoverage(path="src/app.py", outcome="reviewed"),
                            FileCoverage(
                                path="other.bin",
                                outcome="budget-exhausted",
                                reason="provider-call-budget",
                            ),
                        ),
                        enumerated_paths=("src/app.py", "other.bin"),
                    ),
                ),
            )
            state = RetentionState()
            state.reviews = []

            def interrupt_after_complete_admission(_state, method, path, body):
                if method == "POST" and path.endswith("/reviews"):
                    raise KeyboardInterrupt()

            state.hook = interrupt_after_complete_admission
            with self.assertRaises(KeyboardInterrupt):
                publish(application(state, ledger), result, None)
            state.hook = None
            fresh = LocalSessionLedger(Path(directory))
            app = application(state, fresh)
            budget = EvidenceReadBudget()
            restored = recover(app, fresh, budget=budget)
            self.assertEqual(restored.review_status, "partial")
            outcome = publish(
                app,
                restored,
                None,
                publication_budget=budget,
                options=GitHubWriteOptions(
                    auto_review=True, github_writes=True, complete_inventory=True
                ),
            )
            self.assertEqual(outcome.status, "published")
            self.assertTrue(
                any("Retained partial finding." in r["body"] for r in state.reviews)
            )
            self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
            self.assertTrue(
                all(c.get("conclusion") != "success" for c in state.check_runs)
            )
            record = fresh.load(IDENTITY).record
            self.assertEqual(record.failed_attempts, 1)
            self.assertEqual(record.completed_initial_reviews, 0)
            self.assertIsNone(record.convergence_history)
            self.assertEqual(record.transaction.phase, "publication_succeeded")

    def test_remote_session_reads_and_actual_broker_share_one_physical_budget(self):
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.session_ledger import (
            GitHubIssueCommentSessionLedger,
            render_session_comment,
        )

        for exhausted in (False, True):
            with (
                self.subTest(exhausted=exhausted),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger)
                state = RetentionState()
                state.reviews = []
                state.session_comment = {
                    "id": 600,
                    "body": render_session_comment(
                        repository_id=1,
                        pull_request=1,
                        record=ledger.load(IDENTITY).record,
                    ),
                    "user": state.app_user,
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
                }
                broker_calls = []

                def broker_open(request, timeout):
                    broker_calls.append(request)
                    body = json.loads(request.data)
                    if body.get("capability") == "review_session":
                        return json_response(
                            {
                                "token": "session-token",
                                "capability": "review_session",
                                "session_state": "known",
                            }
                        )
                    return json_response({"token": "capability-token"})

                broker = BrokerClient(
                    broker_url="https://broker.github.test/token", opener=broker_open
                )
                app = GitHubApplication(
                    broker=broker,
                    http=state.http,
                    reviewer=ReviewPublisher(http=state.http),
                    learner=_Noop(),
                    replier=_Noop(),
                )
                budget = EvidenceReadBudget()
                if exhausted:
                    for _ in range(60):
                        budget.consume()
                with self.assertRaisesRegex(
                    Exception, "budget" if exhausted else "finding index"
                ):
                    publish(
                        app,
                        result,
                        key,
                        publication_budget=budget,
                        options=GitHubWriteOptions(
                            auto_review=True,
                            github_writes=True,
                            github_session_ledger=True,
                        ),
                    )
                if exhausted:
                    self.assertEqual(state.calls, [])
                    self.assertEqual(broker_calls, [])
                    clock = [0.0]
                    expired = EvidenceReadBudget(clock=lambda: clock[0])
                    clock[0] = 61.0
                    remote = GitHubIssueCommentSessionLedger(
                        state.http, token="read", app_slug=APP
                    )
                    with self.assertRaisesRegex(GitHubPublicationError, "deadline"):
                        recover(app, remote, budget=expired)
                    self.assertEqual(state.calls, [])
                else:
                    self.assertEqual(budget.calls, len(state.calls) + len(broker_calls))
                    self.assertTrue(state.parts)
                    self.assertGreater(len(broker_calls), 1)
                    self.assertLessEqual(budget.calls, 60)
                self.assertIsNone(broker.before_request)

    def test_fresh_owner_completes_retained_77_without_provider_or_budget_refill(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger)
            state = RetentionState()
            state.reviews = []
            with self.assertRaisesRegex(GitHubPublicationError, "finding index"):
                publish(application(state, ledger), result, key)
            first_posts = state.part_requests.count("POST")
            state.calls.clear()
            fresh = LocalSessionLedger(Path(directory))
            app = application(state, fresh)
            budget = EvidenceReadBudget()
            restored = recover(app, fresh, budget=budget)
            reads_before_publish = budget.calls
            outcome = publish(
                app,
                restored,
                key,
                publication_budget=budget,
                options=GitHubWriteOptions(
                    auto_review=True, github_writes=True, complete_inventory=True
                ),
            )
            self.assertEqual(outcome.status, "published")
            self.assertEqual(state.part_requests.count("POST"), first_posts)
            self.assertGreater(reads_before_publish, 0)
            self.assertEqual(budget.calls, len(state.calls))
            self.assertLessEqual(budget.calls, 60)
            roots = [
                body
                for method, path, body in state.calls
                if method == "POST" and path.endswith("/reviews") and "comments" in body
            ]
            self.assertEqual(len(roots), 1)
            for comment in result.comments:
                self.assertIn(comment.body, roots[0]["body"])
            self.assertEqual(
                fresh.load(IDENTITY).record.transaction.phase, "publication_succeeded"
            )
            self.assertEqual(fresh.load(IDENTITY).record.completed_initial_reviews, 1)
            self.assertEqual(
                fresh.load(IDENTITY).record.completed_verification_rounds, 0
            )

    def test_lost_review_post_fresh_owner_reuses_exact_root_with_or_without_retention(
        self,
    ):
        for operation in ("enabled", "disabled"):
            with (
                self.subTest(operation=operation),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, key = checkpoint(ledger)
                state = RetentionState()
                state.reviews = []
                original = state.http.opener
                lost = False

                def transport(request, timeout):
                    nonlocal lost
                    path = urlparse(request.full_url).path
                    if lost and request.method == "GET" and path.endswith("/reviews"):
                        state.calls.append(("GET", path, None))
                        raise URLError("synthetic unavailable readback")
                    response = original(request, timeout)
                    if (
                        request.method == "POST"
                        and path.endswith("/reviews")
                        and not lost
                    ):
                        lost = True
                        raise URLError("synthetic lost review acknowledgement")
                    return response

                state.http.opener = transport
                opts = GitHubWriteOptions(
                    auto_review=True,
                    github_writes=True,
                    complete_inventory=True,
                    operation_entry=operation,
                )
                budget = EvidenceReadBudget()
                with self.assertRaisesRegex(GitHubPublicationError, "reconciliation"):
                    publish(
                        application(state, ledger),
                        result,
                        key,
                        options=opts,
                        publication_budget=budget,
                    )
                self.assertEqual(budget.calls, len(state.calls))
                self.assertEqual(len(state.reviews), 1)
                self.assertEqual(
                    ledger.load(IDENTITY).record.transaction.phase, "publication_failed"
                )
                state.http.opener = original
                state.calls.clear()
                fresh = LocalSessionLedger(Path(directory))
                app = application(state, fresh)
                retry_budget = EvidenceReadBudget()
                retry_result = (
                    recover(app, fresh, budget=retry_budget)
                    if operation == "enabled"
                    else result
                )
                outcome = publish(
                    app,
                    retry_result,
                    key,
                    options=opts,
                    publication_budget=retry_budget,
                )
                self.assertEqual(outcome.status, "already_published")
                self.assertEqual(retry_budget.calls, len(state.calls))
                self.assertLessEqual(retry_budget.calls, 60)
                self.assertFalse(
                    [
                        body
                        for method, path, body in state.calls
                        if method == "POST"
                        and path.endswith("/reviews")
                        and "comments" in body
                    ]
                )
                self.assertEqual(
                    len(
                        [r for r in state.reviews if "Findings explained" in r["body"]]
                    ),
                    1,
                )
                self.assertEqual(
                    fresh.load(IDENTITY).record.completed_initial_reviews, 1
                )

    def test_disabled_retention_still_honors_exhausted_publication_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result, key = checkpoint(ledger, 1)
            state = RetentionState()
            budget = EvidenceReadBudget()
            for _ in range(60):
                budget.consume()
            with self.assertRaisesRegex(Exception, "budget"):
                publish(
                    application(state, ledger),
                    result,
                    key,
                    publication_budget=budget,
                    options=GitHubWriteOptions(
                        auto_review=True,
                        github_writes=True,
                        operation_entry="disabled",
                        complete_inventory=True,
                    ),
                )
            self.assertEqual(state.calls, [])
            self.assertEqual(budget.calls, 60)

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
            # The raw lost POST reconciles once; independent admission adds
            # one intent and one evidence part, each also created once.
            self.assertEqual(state.part_requests.count("POST"), 3)
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
                    before = len(state.calls)
                    with self.assertRaisesRegex(
                        ReviewInputError, "admission evidence is missing"
                    ):
                        publish(app, restored, key)
                    # Raw retention alone must not reach either gate or review
                    # mutation; admission absence is its own recovery refusal.
                    self.assertEqual(
                        [
                            (method, path)
                            for method, path, _ in state.calls[before:]
                            if method not in {"GET", "HEAD"}
                        ],
                        [],
                    )
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
                state.reviews = []
                options = GitHubWriteOptions(
                    auto_review=True, github_writes=True, complete_inventory=True
                )

                def interrupt_after_admission(_state, method, path, body):
                    if method == "POST" and path.endswith("/pulls/1/reviews"):
                        # This fault is after original runtime admission, not
                        # the separate raw-result crash/missing-admission case.
                        self.assertTrue(
                            any(
                                "<!-- reviewsensei:default-human-inventory-intent:v1 -->"
                                in part["body"]
                                for part in state.parts
                            )
                        )
                        raise KeyboardInterrupt()

                state.hook = interrupt_after_admission
                with self.assertRaises(KeyboardInterrupt):
                    publish(application(state, ledger), result, key, options=options)
                state.hook = None
                self.assertEqual(state.reviews, [])
                self.assertEqual(state.part_requests.count("POST"), 3)
                fresh = LocalSessionLedger(Path(directory))
                app = application(state, fresh)
                restored = recover(app, fresh)
                self.assertEqual(restored.content_digest(), result.content_digest())

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
                        fresh.replace(IDENTITY, mutate)
                    continue
                fresh.replace(IDENTITY, mutate)
                before_checks = len(state.check_runs)
                with self.assertRaisesRegex(
                    ReviewInputError,
                    "^durable convergence progress does not match transaction$",
                ):
                    publish(app, restored, key, options=options)
                self.assertEqual(state.part_requests.count("POST"), 3)
                self.assertEqual(state.reviews, [])
                self.assertEqual(
                    state.check_runs[-1].get("conclusion"), "action_required"
                )
                self.assertTrue(
                    all(
                        check.get("conclusion") == "action_required"
                        for check in state.check_runs[before_checks:]
                    )
                )
