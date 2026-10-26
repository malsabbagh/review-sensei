"""Public command reconciliation preserves a near-capacity legacy publication."""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlsplit

from fake_github_http import json_response
from review_sensei_github.approval import ApprovalFacts, ReviewApprovalEligibility
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    AssembledReview,
    BodyExplanation,
    IndexedFinding,
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    approval_eligibility_marker,
    finding_index_marker,
    finding_instance_marker,
    finding_marker,
    fit_assembled_review,
    parse_finding_index,
    review_marker,
)
from review_sensei_github.publication_recovery import publication_http

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.disposition import (
    FindingIdentity,
    apply_session_command,
    parse_maintainer_command,
    session_dispositions,
)
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.presentation import (
    assign_finding_identifiers,
    build_finding_view,
    render_finding,
    render_finding_reference,
)
from review_sensei.session import LocalSessionLedger, SessionIdentity

HEAD, BASE, APP = "b" * 40, "a" * 40, "reviewsensei[bot]"
IDENTITY = SessionIdentity("owner/repo", 1, 1)


class CommandCapacityState:
    """Actual rendered legacy root and synthetic external owned GitHub records."""

    def __init__(
        self,
        *,
        complete=False,
        schema="3",
        compact=False,
        inline_range=(8, 21),
        padded=True,
        human_count=None,
    ):
        comments = []
        for number in range(56):
            human = number < (
                human_count
                if human_count is not None
                else (20 if schema in {"1", "2.0"} else 21)
            )
            comments.append(
                ReviewComment(
                    path=f"src/file{number}.py",
                    line=1 if inline_range[0] <= number < inline_range[1] else None,
                    side="RIGHT"
                    if inline_range[0] <= number < inline_range[1]
                    else "FILE",
                    body=(
                        f"Concern {number}: verify the complete original evidence before allowing this operation to progress because an interrupted response can otherwise hide the unresolved condition in this exact file.\n\n"
                        + (
                            f"Original detailed explanation {number} remains necessary. "
                            * (30 if schema == "3" else 2)
                        )
                    )
                    if human
                    else f"Optional detail {number} remains exactly preserved.",
                    needs_human=human,
                    effective_blocking=False,
                    severity="medium" if human else "low",
                )
            )
        self.comments = tuple(comments)
        self.result = ReviewResult(
            summary="Original review.", comments=self.comments, provider="fixture"
        )
        self.human = PendingHumanReview.from_result(self.result, BASE)
        if schema == "2.0":
            self.human = replace(
                self.human,
                findings=tuple(
                    replace(finding, required_paths=(finding.path, "src/shared.py"))
                    for finding in self.human.findings
                ),
            )
        self.eligibility = ReviewApprovalEligibility(
            HEAD,
            self.result.content_digest(),
            ApprovalFacts(
                enabled=True,
                app_authored=False,
                review_status="complete" if complete else "partial",
                evidence_policy="legacy",
                has_blocking_findings=False,
                has_human_adjudication_findings=True,
                coverage_blocker=None if complete else "partial",
                qualification="not-required",
                check_published=True,
                # Bind the synthetic backend; legacy facts without a provider
                # intentionally cannot authorize delayed approval.
                provider=self.result.provider,
            ),
            self.human,
        )
        self.labels = assign_finding_identifiers(
            finding_instance_fingerprint(c) for c in self.comments
        )
        views = []
        entries = []
        self.inline = []
        for comment in self.comments:
            instance = finding_instance_fingerprint(comment)
            concern = finding_lifecycle_for_comment(comment).fingerprint
            placement = "inline" if comment.line else "body"
            view = build_finding_view(
                comment,
                fingerprint=concern,
                identifier=self.labels[instance],
                placement=placement,
            )
            views.append(view)
            entries.append(
                IndexedFinding(
                    instance,
                    concern,
                    False,
                    comment.needs_human,
                    placement == "body",
                    overridable=comment.needs_human,
                )
            )
            if placement == "inline":
                self.inline.append(
                    {
                        "id": 100 + len(self.inline),
                        "pull_request_review_id": 40,
                        "commit_id": HEAD,
                        "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                        "user": {"id": 99, "login": APP, "type": "Bot"},
                        "path": comment.path,
                        "line": comment.line,
                        "side": comment.side,
                        "body": render_finding(view)
                        + "\n\n"
                        + finding_marker(
                            repository_id=1,
                            pull_request=1,
                            head_sha=HEAD,
                            base_sha=BASE,
                            result=self.result,
                            blocking=False,
                            fingerprint=concern,
                            needs_human=human,
                        )
                        + "\n\n"
                        + finding_instance_marker(comment),
                    }
                )
        self.entries = tuple(entries)
        self.quoted = (
            "Historical quoted reference:\n"
            + render_finding_reference(views[0])
            + "\nUnrecognized original narrative stays exact."
        )
        assembled = AssembledReview(
            (),
            self.entries,
            HEAD,
            "incomplete" if not complete else "withheld",
            None,
            self.quoted,
            None,
            (),
            tuple(views),
            (),
            None,
            tuple(
                BodyExplanation(e.instance, c.severity, c.needs_human, v)
                for c, e, v in zip(self.comments, entries, views, strict=True)
                if e.body_placed
            ),
            (),
            False,
            compact_references=compact,
        )
        marker = review_marker(
            repository_id=1, pull_request=1, head_sha=HEAD, result=self.result
        )
        eligibility_marker = approval_eligibility_marker(self.eligibility)

        def render(value):
            summary, index = fit_assembled_review(
                value,
                review_marker=marker,
                eligibility_marker=eligibility_marker,
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                base_sha=BASE,
                result_digest=self.eligibility.result_digest,
                summary_limit=65536,
                complete_inventory=True,
            )
            return "\n\n".join((summary, marker, index, eligibility_marker))

        initial = render(assembled)
        self.unknown = (
            "\n\nOpaque historical prose: "
            + "x"
            * max(
                0, 64971 - len(initial.encode()) - len("\n\nOpaque historical prose: ")
            )
            if padded
            else ""
        )
        self.body = initial + self.unknown
        if padded:
            assert len(self.body.encode()) == 64971
        assert self.eligibility.to_dict()["schema_version"] == schema
        self.reviews = [
            {
                "id": 40,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": self.body,
                "user": {"id": 99, "login": APP, "type": "Bot"},
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
            }
        ]
        self.resolved_threads = {item["id"] for item in self.inline}
        self.graphql_author = {"login": APP}
        self.inline_history = []
        self.comment_pages = []
        self.head, self.base = HEAD, BASE
        self.calls = []
        self.checks = []
        self.hook = None
        self.lose_put_ack = False
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        data = json.loads(request.data) if request.data else None
        self.calls.append((request.method, path, copy.deepcopy(data)))
        if self.hook:
            self.hook(self, request.method, path, data)
        if request.method == "GET":
            if path == "/users/" + quote(APP, safe=""):
                return json_response({"id": 99, "login": APP, "type": "Bot"})
            if "/pulls/comments/" in path:
                return json_response(
                    next(
                        item
                        for item in self.inline
                        if item["id"] == int(path.rsplit("/", 1)[1])
                    )
                )
            if path.endswith("/pulls/1/reviews"):
                return json_response(self.reviews)
            if path.endswith("/reviews/40/comments"):
                # GitHub's review-specific listing is a legacy shape even
                # under the pinned API version; native locations are absent.
                return json_response(
                    [
                        {
                            key: value
                            for key, value in item.items()
                            if key not in {"line", "side"}
                        }
                        for item in self.inline
                    ]
                )
            if path.endswith("/pulls/1/comments"):
                query = parse_qs(urlsplit(request.full_url).query)
                page = int(query.get("page", ["1"])[0])
                size = int(query.get("per_page", ["100"])[0])
                self.comment_pages.append((page, size))
                records = self.inline_history + self.inline
                return json_response(records[(page - 1) * size : page * size])
            if path.endswith("/pulls/1"):
                return json_response(
                    {
                        "state": "open",
                        "draft": False,
                        "head": {
                            "sha": self.head,
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": self.base,
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                        "user": {"login": "alice", "type": "User"},
                    }
                )
            if path.endswith("/check-runs"):
                return json_response({"check_runs": self.checks})
        if request.method == "PUT" and path.endswith("/reviews/40"):
            self.reviews[0]["body"] = data["body"]
            if self.lose_put_ack:
                self.lose_put_ack = False
                raise URLError("synthetic committed PUT response lost")
            return json_response(self.reviews[0])
        if request.method in {"POST", "PATCH"} and (
            path.endswith("/check-runs") or "/check-runs/" in path
        ):
            check = {"id": 50, **data}
            self.checks.append(check)
            return json_response(check, 201 if request.method == "POST" else 200)
        if path == "/graphql":
            if data.get("operationName") == "ResolveReviewThread":
                thread = data["variables"]["input"]["threadId"]
                self.resolved_threads.add(int(thread.removeprefix("PRRT_")))
                return json_response(
                    {
                        "data": {
                            "resolveReviewThread": {
                                "thread": {"id": thread, "isResolved": True}
                            }
                        }
                    }
                )
            if data.get("operationName") == "ResolutionReadback":
                thread = data["variables"]["id"]
                return json_response(
                    {
                        "data": {
                            "node": {
                                "id": thread,
                                "isResolved": int(thread.removeprefix("PRRT_"))
                                in self.resolved_threads,
                            }
                        }
                    }
                )
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": f"PRRT_{item['id']}",
                                            "isResolved": item["id"]
                                            in self.resolved_threads,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "databaseId": item["id"],
                                                        "body": item["body"],
                                                        "author": self.graphql_author,
                                                    }
                                                ]
                                            },
                                        }
                                        for item in self.inline
                                    ],
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
        if request.method == "POST" and path.endswith("/reviews"):
            review = {
                "id": 41,
                "commit_id": HEAD,
                "state": "APPROVED" if data["event"] == "APPROVE" else "COMMENTED",
                "body": data["body"],
                "user": {"id": 99, "login": APP, "type": "Bot"},
            }
            self.reviews.append(review)
            return json_response(review, 200)
        raise AssertionError((request.method, path))

    def record(self, directory):
        ledger = LocalSessionLedger(Path(directory) / "ledger")
        inventory = tuple(
            FindingIdentity(
                e.instance,
                e.lifecycle,
                overridable=e.overridable,
                needs_human=e.needs_human,
            )
            for e in self.entries
        )
        ids = [self.labels[e.instance] for e in self.entries if e.needs_human]
        for start in range(0, len(ids), 10):
            command = parse_maintainer_command(
                "@reviewsensei override "
                + " ".join(ids[start : start + 10])
                + " acceptable risk",
                actor="alice",
                head_sha=HEAD,
            )
            apply_session_command(
                ledger, IDENTITY, command, finding_inventory=inventory
            )
            ledger = LocalSessionLedger(Path(directory) / "ledger")
        return session_dispositions(ledger.load(IDENTITY).record)

    def verify(self, dispositions, *, raw=False):
        budget = EvidenceReadBudget()
        finalizer = ReviewApprovalFinalizer(
            http=self.http if raw else publication_http(self.http, budget),
            evidence_budget=budget,
        )
        before = len(self.calls)
        outcome = finalizer.refinalize_with_dispositions(
            token="synthetic",
            check_token="synthetic-check",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            actor="alice",
            finding_ids=(),
            dispositions=dispositions,
        )
        return outcome, budget, self.calls[before:]


class CommandCapacityRecoveryTests(unittest.TestCase):
    def test_unpadded_twenty_thread_retries_finish_without_revisiting_resolved_prefix(
        self,
    ):
        state = CommandCapacityState(
            complete=True, schema="1", inline_range=(0, 20), padded=False
        )
        state.resolved_threads.clear()
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            for _ in range(5):
                outcome, budget, calls = state.verify(dispositions)
                progress.append(len(state.resolved_threads))
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)
                if outcome.status == "approved":
                    break
            self.assertEqual(outcome.status, "approved", progress)
            self.assertEqual(len(state.resolved_threads), 20)
            replay, budget, calls = state.verify(dispositions)
        self.assertEqual(replay.status, "already_approved")
        self.assertLessEqual(budget.calls, 60)
        self.assertFalse(
            any(m == "GET" and "/pulls/comments/" in p for m, p, _ in calls)
        )
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)
        self.assertEqual(state.checks[-1]["conclusion"], "success")

    def test_thread_progress_does_not_clear_missing_coverage_or_stale_head(self):
        state = CommandCapacityState(schema="1", inline_range=(0, 20), padded=False)
        state.resolved_threads.clear()
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            for _ in range(5):
                outcome, budget, _ = state.verify(dispositions)
                self.assertLessEqual(budget.calls, 60)
                if len(state.resolved_threads) == 20:
                    break
            self.assertEqual(len(state.resolved_threads), 20)
            self.assertEqual(outcome.diagnostic, "review_incomplete")
            self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
            state.head = "c" * 40
            stale, _, _ = state.verify(dispositions)
        self.assertEqual(stale.status, "skipped_stale_head")
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_reopened_thread_reenters_cleanup_without_another_approval(self):
        state = CommandCapacityState(
            complete=True, schema="1", inline_range=(0, 20), padded=False
        )
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            first, _, _ = state.verify(dispositions)
            self.assertEqual(first.status, "approved")
            state.resolved_threads.remove(state.inline[-1]["id"])
            replay, budget, _ = state.verify(dispositions)
        self.assertEqual(len(state.resolved_threads), 20)
        self.assertEqual(replay.status, "already_approved")
        self.assertLessEqual(budget.calls, 60)
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)

    def test_resolved_observations_do_not_hide_base_or_eligibility_drift(self):
        for fault in ("base", "eligibility"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state = CommandCapacityState(
                    complete=True, schema="1", inline_range=(0, 20), padded=False
                )

                def change_after_discovery(state, method, path, data):
                    if (
                        path == "/graphql"
                        and data.get("operationName") == "FindingResolutionThreads"
                    ):
                        if fault == "base":
                            state.base = "d" * 40
                        else:
                            current = approval_eligibility_from_body(
                                state.reviews[0]["body"]
                            )
                            changed = replace(
                                current,
                                facts=replace(
                                    current.facts, qualification="unverified"
                                ),
                            )
                            state.reviews[0]["body"] = state.reviews[0]["body"].replace(
                                approval_eligibility_marker(current),
                                approval_eligibility_marker(changed),
                            )
                        state.hook = None

                state.hook = change_after_discovery
                outcome, budget, _ = state.verify(state.record(directory))
                self.assertNotIn(outcome.status, {"approved", "already_approved"})
                self.assertLessEqual(budget.calls, 60)
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_unreadable_expected_native_actor_holds_installed_recovery(self):
        for discriminator in ({}, {"__typename": None}, {"__typename": False}):
            with (
                self.subTest(discriminator=discriminator),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = CommandCapacityState(complete=True)
                state.resolved_threads.clear()
                state.graphql_author = {"login": "reviewsensei", **discriminator}
                result, budget, calls = state.verify(state.record(directory))
                self.assertEqual(result.status, "approval_withheld")
                self.assertEqual(result.thread_resolution, "unconfirmed")
                self.assertFalse(state.resolved_threads)
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)

    def test_durable_all21_verify_fits_without_losing_original_prose_or_coverage(self):
        state = CommandCapacityState()
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            self.assertEqual(len(dispositions), 21)
            outcome, budget, calls = state.verify(dispositions)
        self.assertNotEqual(outcome.status, "gate_reevaluation_failed")
        current = approval_eligibility_from_body(state.reviews[0]["body"])
        self.assertEqual(len(current.human_review.pending), 0)
        self.assertEqual(current.facts.coverage_blocker, "partial")
        self.assertEqual(current.facts.review_status, "partial")
        self.assertLessEqual(len(state.reviews[0]["body"].encode()), 65536)
        self.assertIn(state.quoted, state.reviews[0]["body"])
        self.assertIn(state.unknown, state.reviews[0]["body"])
        self.assertEqual(
            parse_finding_index(
                state.reviews[0]["body"], repository_id=1, pull_request=1, head_sha=HEAD
            ).entries,
            state.entries,
        )
        for comment in state.comments:
            self.assertIn(
                comment.body,
                state.reviews[0]["body"]
                + "\n"
                + "\n".join(x["body"] for x in state.inline),
            )
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(budget.calls, 60)
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
        self.assertEqual(state.checks[-1]["conclusion"], "action_required")

    def test_fully_eligible_control_approves_after_complete_owned_readback(self):
        state = CommandCapacityState(complete=True)
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            outcome, budget, calls = state.verify(dispositions)
            replay, replay_budget, replay_calls = state.verify(dispositions)
        self.assertEqual(replay.status, "already_approved")
        self.assertFalse(
            any(
                m == "PUT" or (m == "POST" and p.endswith("/reviews"))
                for m, p, _ in replay_calls
            )
        )
        self.assertEqual(replay_budget.calls, len(replay_calls))
        self.assertLessEqual(replay_budget.calls, 60)
        self.assertEqual(outcome.status, "approved")
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)
        self.assertEqual(state.checks[-1]["conclusion"], "success")
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(budget.calls, 60)

    def test_supported_legacy_schemas_and_exact_replay_preserve_durable_union_without_another_put(
        self,
    ):
        for schema in ("1", "2.0", "3"):
            with (
                self.subTest(schema=schema),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = CommandCapacityState(schema=schema)
                dispositions = state.record(directory)
                first, _, _ = state.verify(dispositions)
                self.assertNotEqual(first.status, "gate_reevaluation_failed")
                exact = state.reviews[0]["body"]
                if schema == "2.0":
                    current = approval_eligibility_from_body(exact)
                    self.assertTrue(
                        all(
                            "src/shared.py" in finding.required_paths
                            for finding in current.human_review.findings
                        )
                    )
                replay, budget, calls = state.verify(dispositions)
                self.assertNotEqual(replay.status, "gate_reevaluation_failed")
                self.assertEqual(state.reviews[0]["body"], exact)
                self.assertFalse(any(method == "PUT" for method, _, _ in calls))
                self.assertEqual(budget.calls, len(calls))
                self.assertEqual(
                    len(
                        session_dispositions(
                            LocalSessionLedger(Path(directory) / "ledger")
                            .load(IDENTITY)
                            .record
                        )
                    ),
                    20 if schema in {"1", "2.0"} else 21,
                )
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_lost_committed_put_is_read_back_without_reissuing_the_write(self):
        state = CommandCapacityState()
        state.lose_put_ack = True
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            result, budget, calls = state.verify(dispositions)
        self.assertNotEqual(result.status, "gate_reevaluation_failed")
        self.assertEqual(sum(method == "PUT" for method, _, _ in calls), 1)
        self.assertEqual(
            len(
                approval_eligibility_from_body(
                    state.reviews[0]["body"]
                ).human_review.resolved
            ),
            21,
        )
        self.assertEqual(budget.calls, len(calls))
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_changed_new_body_with_same_eligibility_does_not_update_gate_or_approve(
        self,
    ):
        state = CommandCapacityState(complete=True)

        def alter(state, method, path, data):
            if (
                method == "GET"
                and path.endswith("/reviews")
                and any(m == "PUT" for m, _, _ in state.calls)
            ):
                state.reviews[0]["body"] += "\nUnconfirmed changed prose."
                state.hook = None

        state.hook = alter
        with tempfile.TemporaryDirectory() as directory:
            result, _, _ = state.verify(state.record(directory))
        self.assertEqual(result.status, "gate_reevaluation_failed")
        self.assertEqual(result.diagnostic, "command_body_capacity_unverifiable")
        self.assertEqual(state.checks, [])
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_missing_prose_ambiguous_summary_and_mutable_authority_refuse_before_put(
        self,
    ):
        for fault in (
            "body",
            "extra-index",
            "inline",
            "inline-author",
            "inline-line",
            "inline-side",
            "body-location",
            "narrative-only",
            "code-fence",
            "ambiguous",
            "unknown-row",
            "head",
            "base",
            "root",
            "latest",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state = CommandCapacityState()
                if fault == "extra-index":
                    extra = (
                        *state.entries[:-1],
                        replace(state.entries[-1], needs_human=True, overridable=True),
                    )
                    marker = finding_index_marker(
                        repository_id=1,
                        pull_request=1,
                        head_sha=HEAD,
                        base_sha=BASE,
                        result_digest=state.eligibility.result_digest,
                        entries=extra,
                        complete_inventory=True,
                    )
                    state.reviews[0]["body"] = re.sub(
                        r"<!-- reviewsensei:finding-index:v1 .*? -->",
                        lambda match: marker,
                        state.body,
                    )
                elif fault == "body":
                    state.reviews[0]["body"] = state.body.replace(
                        state.comments[0].body, "z" * len(state.comments[0].body), 1
                    )
                elif fault == "inline":
                    state.inline[0]["body"] = "Full explanation removed"
                elif fault == "inline-author":
                    state.inline[0]["user"]["id"] = 100
                elif fault == "inline-line":
                    state.inline[0]["line"] = 2
                elif fault == "inline-side":
                    state.inline[0]["side"] = "LEFT"
                elif fault == "body-location":
                    state.reviews[0]["body"] = state.body.replace(
                        "`src/file0.py`", "`src/file0.py:2`", 1
                    )
                elif fault == "narrative-only":
                    comment = state.comments[0]
                    view = build_finding_view(
                        comment,
                        fingerprint=finding_lifecycle_for_comment(comment).fingerprint,
                        identifier=state.labels[finding_instance_fingerprint(comment)],
                        placement="body",
                    )
                    block = render_finding(view)
                    moved = state.body.replace(block, "", 1)
                    state.reviews[0]["body"] = moved.replace(
                        "## Findings explained in this review body",
                        block + "\n\n## Findings explained in this review body",
                        1,
                    )
                elif fault == "code-fence":
                    state.reviews[0]["body"] = state.body.replace(
                        "### Required decisions", "```\n### Required decisions", 1
                    )
                elif fault == "ambiguous":
                    state.reviews[0]["body"] += "\n### Required decisions\n"
                elif fault == "unknown-row":
                    state.reviews[0]["body"] = state.body.replace(
                        "### Required decisions\n",
                        "### Required decisions\nUnknown historical text must not be deleted.\n",
                        1,
                    )
                elif fault == "head":
                    state.head = "c" * 40
                elif fault == "base":
                    state.base = "c" * 40
                else:

                    def race(state, method, path, data):
                        if method == "GET" and path.endswith("/pulls/1"):
                            if fault == "root":
                                state.reviews[0]["body"] += "\nEdited original root."
                            else:
                                state.reviews.append({**state.reviews[0], "id": 41})
                            state.hook = None

                    state.hook = race
                result, budget, calls = state.verify(state.record(directory))
                self.assertNotEqual(result.status, "approved")
                self.assertFalse(
                    any(method in {"PUT", "POST", "PATCH"} for method, _, _ in calls)
                )
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)

    def test_fresh_bounded_verifies_resume_native_thread_progress_without_repeating_mutations(
        self,
    ):
        state = CommandCapacityState(complete=True)
        state.resolved_threads.clear()
        state.inline_history = [
            {
                **copy.deepcopy(state.inline[0]),
                "id": 900 + n,
                "pull_request_review_id": 39,
                "body": "z" * 9000,
            }
            for n in range(61)
        ]
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            progress = []
            mutations = []
            for attempt in range(3):
                result, budget, calls = state.verify(dispositions)
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)
                progress.append(len(state.resolved_threads))
                mutations.extend(
                    data["variables"]["input"]["threadId"]
                    for m, p, data in calls
                    if p == "/graphql"
                    and data.get("operationName") == "ResolveReviewThread"
                )
                if result.status == "approved":
                    break
                self.assertEqual(result.thread_resolution, "unconfirmed")
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
            self.assertGreater(progress[0], 0)
            self.assertLess(progress[0], 13)
            self.assertEqual(progress[-1], 13)
            self.assertEqual(result.status, "approved")
            self.assertEqual(len(mutations), 13)
            self.assertEqual(len(set(mutations)), 13)
            self.assertEqual(
                len(
                    session_dispositions(
                        LocalSessionLedger(Path(directory) / "ledger")
                        .load(IDENTITY)
                        .record
                    )
                ),
                21,
            )
            replay, budget, calls = state.verify(dispositions)
        self.assertEqual(replay.status, "already_approved")
        self.assertFalse(
            any(
                m == "PUT"
                or (
                    p == "/graphql"
                    and data.get("operationName") == "ResolveReviewThread"
                )
                for m, p, data in calls
            )
        )
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(budget.calls, 60)

    def test_native_whole_pr_location_shape_recovers_without_individual_hydration(self):
        state = CommandCapacityState(complete=True)
        state.inline_history = [
            {**copy.deepcopy(state.inline[0]), "id": 900, "pull_request_review_id": 39}
        ]
        with tempfile.TemporaryDirectory() as directory:
            outcome, budget, calls = state.verify(state.record(directory))
        self.assertEqual(outcome.status, "approved")
        self.assertFalse(any(p.endswith("/reviews/40/comments") for _, p, _ in calls))
        self.assertTrue(state.comment_pages)
        self.assertEqual(set(state.comment_pages), {(1, 100)})
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(budget.calls, 60)

    def test_foreign_review_cannot_replace_a_missing_owned_native_explanation(self):
        state = CommandCapacityState(complete=True)
        missing = state.inline.pop(0)
        state.inline_history = [{**missing, "pull_request_review_id": 39}]
        with tempfile.TemporaryDirectory() as directory:
            outcome, budget, calls = state.verify(state.record(directory))
        self.assertEqual(outcome.diagnostic, "command_body_capacity_unverifiable")
        self.assertFalse(any(m in {"PUT", "POST", "PATCH"} for m, _, _ in calls))
        self.assertEqual(budget.calls, len(calls))

    def test_oversized_whole_pr_page_retries_smaller_pages_in_the_same_allowance(self):
        state = CommandCapacityState()
        state.inline_history = [
            {
                **copy.deepcopy(state.inline[0]),
                "id": 900 + n,
                "pull_request_review_id": 39,
                "body": "Unrelated retained history. " + "z" * 9000,
            }
            for n in range(61)
        ]
        with tempfile.TemporaryDirectory() as directory:
            result, budget, calls = state.verify(state.record(directory))
        self.assertEqual(result.status, "approval_withheld")
        self.assertEqual(state.comment_pages[0], (1, 100))
        self.assertIn((1, 50), state.comment_pages)
        self.assertIn((2, 50), state.comment_pages)
        self.assertEqual(
            len(
                approval_eligibility_from_body(
                    state.reviews[0]["body"]
                ).human_review.resolved
            ),
            21,
        )
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(budget.calls, 60)
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))

    def test_indivisible_native_item_over_response_limit_refuses_without_losing_decisions(
        self,
    ):
        state = CommandCapacityState()
        state.inline_history = [
            {
                **copy.deepcopy(state.inline[0]),
                "id": 900,
                "pull_request_review_id": 39,
                "body": "z" * (512 * 1024 + 1),
            }
        ]
        original = state.reviews[0]["body"]
        with tempfile.TemporaryDirectory() as directory:
            result, budget, calls = state.verify(state.record(directory))
            retained = session_dispositions(
                LocalSessionLedger(Path(directory) / "ledger").load(IDENTITY).record
            )
        self.assertEqual(result.diagnostic, "command_body_capacity_unverifiable")
        self.assertEqual(
            state.comment_pages, [(1, size) for size in (100, 50, 25, 10, 5, 1)]
        )
        self.assertEqual(state.reviews[0]["body"], original)
        self.assertEqual(len(retained), 21)
        self.assertFalse(any(m in {"PUT", "POST", "PATCH"} for m, _, _ in calls))
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(budget.calls, 60)

    def test_whole_pr_history_paginates_and_never_refills_the_allowance(self):
        for complete in (False, True):
            with (
                self.subTest(complete=complete),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = CommandCapacityState(complete=complete)
                state.inline_history = [
                    {
                        **copy.deepcopy(state.inline[0]),
                        "id": 900 + n,
                        "pull_request_review_id": 39,
                    }
                    for n in range(101)
                ]
                outcome, budget, calls = state.verify(state.record(directory))
                self.assertIn((2, 100), state.comment_pages)
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)
                if not complete:
                    self.assertFalse(
                        any(r["state"] == "APPROVED" for r in state.reviews)
                    )
                    self.assertEqual(outcome.status, "approval_withheld")
                    current = approval_eligibility_from_body(state.reviews[0]["body"])
                    self.assertEqual(len(current.human_review.resolved), 21)
                else:
                    self.assertEqual(outcome.status, "approved")
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in state.reviews), 1
                    )

    def test_late_prose_mutation_during_finalization_never_approves(self):
        for replay in (False, True):
            with (
                self.subTest(replay=replay),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = CommandCapacityState(complete=True)
                dispositions = state.record(directory)
                if replay:
                    first, _, _ = state.verify(dispositions)
                    self.assertEqual(first.status, "approved")
                attempt_start = len(state.calls)
                reads_after_check = 0

                def race(state, method, path, data):
                    nonlocal reads_after_check
                    attempt = state.calls[attempt_start:]
                    if (
                        method == "GET"
                        and path.endswith("/pulls/1")
                        and any(
                            m in {"POST", "PATCH"} and "/check-runs" in p
                            for m, p, _ in attempt
                        )
                    ):
                        reads_after_check += 1
                        if reads_after_check == 1:
                            state.inline[0]["body"] = (
                                "Explanation removed during later thread/finalization reads"
                            )
                            state.hook = None

                state.hook = race
                result, budget, calls = state.verify(dispositions)
                self.assertEqual(reads_after_check, 1)
                self.assertNotIn(result.status, {"approved", "already_approved"})
                self.assertEqual(
                    result.diagnostic, "command_body_capacity_unverifiable"
                )
                self.assertFalse(
                    any(m == "POST" and p.endswith("/reviews") for m, p, _ in calls)
                )
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)

    def test_compacted_replay_requires_current_full_prose_even_after_approval(self):
        for already_approved in (False, True):
            with (
                self.subTest(already_approved=already_approved),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = CommandCapacityState(complete=already_approved)
                dispositions = state.record(directory)
                first, _, _ = state.verify(dispositions)
                self.assertEqual(
                    first.status,
                    "approved" if already_approved else "approval_withheld",
                )
                state.inline[0]["body"] = (
                    "Original full explanation removed before replay"
                )
                result, budget, calls = state.verify(dispositions)
                self.assertNotIn(result.status, {"approved", "already_approved"})
                self.assertFalse(
                    any(m in {"PUT", "POST", "PATCH"} for m, _, _ in calls)
                )
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)

    def test_expired_or_foreign_head_dispositions_never_resolve_the_current_inventory(
        self,
    ):
        for fault in ("expired", "foreign-head"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state = CommandCapacityState(complete=True)
                dispositions = tuple(
                    replace(item, expires_at="2000-01-01T00:00:00+00:00")
                    if fault == "expired"
                    else replace(item, head_sha="c" * 40)
                    for item in state.record(directory)
                )
                result, budget, calls = state.verify(dispositions)
                self.assertNotEqual(result.status, "approved")
                current = approval_eligibility_from_body(state.reviews[0]["body"])
                self.assertEqual(current.human_review.resolved, ())
                self.assertEqual(len(current.human_review.pending), 21)
                self.assertFalse(any(m == "PUT" for m, _, _ in calls))
                self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
                self.assertTrue(state.checks)
                self.assertEqual(state.checks[-1]["conclusion"], "action_required")
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)

    def test_fresh_capacity_authority_races_hold_before_the_next_mutation(self):
        for fault in (
            "inline-after-proof",
            "head-after-preflight",
            "base-after-preflight",
            "foreign-root",
            "foreign-readback",
            "inline-after-put",
            "head-after-put",
            "base-after-put",
            "producer-after-put",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state = CommandCapacityState(complete=True)
                if fault == "foreign-root":
                    state.reviews[0]["pull_request_url"] = (
                        "https://api.github.test/repos/owner/repo/pulls/999"
                    )
                preflight_seen = False
                mutated = fault == "foreign-root"

                def race(state, method, path, data):
                    nonlocal preflight_seen, mutated
                    if method != "GET":
                        return
                    committed = any(m == "PUT" for m, _, _ in state.calls)
                    if path.endswith("/pulls/1") and not committed:
                        preflight_seen = True
                        if fault == "inline-after-proof":
                            mutated = True
                            state.inline[0]["body"] += " Changed original explanation."
                            state.hook = None
                    if path.endswith("/reviews") and preflight_seen and not committed:
                        if fault in {"head-after-preflight", "base-after-preflight"}:
                            mutated = True
                            setattr(
                                state,
                                "head" if fault.startswith("head") else "base",
                                "c" * 40,
                            )
                            state.hook = None
                    if path.endswith("/reviews") and committed:
                        if fault == "foreign-readback":
                            state.reviews[0]["pull_request_url"] = (
                                "https://api.github.test/repos/owner/repo/pulls/999"
                            )
                        elif fault == "inline-after-put":
                            state.inline[0]["body"] += " Changed after PUT."
                        elif fault in {"head-after-put", "base-after-put"}:
                            setattr(
                                state,
                                "head" if fault.startswith("head") else "base",
                                "c" * 40,
                            )
                        elif fault == "producer-after-put":
                            state.reviews[0]["user"]["id"] = 100
                        else:
                            return
                        mutated = True
                        state.hook = None

                state.hook = race
                result, budget, calls = state.verify(state.record(directory))
                self.assertTrue(mutated)
                self.assertNotEqual(result.status, "approved")
                before_put = fault in {
                    "inline-after-proof",
                    "head-after-preflight",
                    "base-after-preflight",
                    "foreign-root",
                }
                if before_put:
                    self.assertFalse(
                        any(m in {"PUT", "POST", "PATCH"} for m, _, _ in calls)
                    )
                else:
                    self.assertEqual(sum(m == "PUT" for m, _, _ in calls), 1)
                    self.assertFalse(any(m in {"POST", "PATCH"} for m, _, _ in calls))
                self.assertEqual(state.checks, [])
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(budget.calls, 60)

    def test_raw_transport_uses_original_allowance_without_mutating_caller(self):
        state = CommandCapacityState()
        with tempfile.TemporaryDirectory() as directory:
            result, budget, calls = state.verify(state.record(directory), raw=True)
        self.assertNotEqual(result.status, "gate_reevaluation_failed")
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(budget.calls, 60)
        self.assertIs(type(state.http), GitHubHttp)

    def test_canonical_already_compact_root_still_exceeding_capacity_holds_without_writes(
        self,
    ):
        state = CommandCapacityState(compact=True)
        original = state.reviews[0]["body"]
        with tempfile.TemporaryDirectory() as directory:
            dispositions = state.record(directory)
            result, budget, calls = state.verify(dispositions)
            self.assertEqual(
                len(
                    session_dispositions(
                        LocalSessionLedger(Path(directory) / "ledger")
                        .load(IDENTITY)
                        .record
                    )
                ),
                21,
            )
        self.assertEqual(result.status, "gate_reevaluation_failed")
        self.assertEqual(result.diagnostic, "command_body_capacity_exceeded")
        self.assertEqual(state.reviews[0]["body"], original)
        self.assertEqual(len(original.encode()), 64971)
        self.assertFalse(
            any(method in {"PUT", "POST", "PATCH"} for method, _, _ in calls)
        )
        self.assertEqual(budget.calls, len(calls))
