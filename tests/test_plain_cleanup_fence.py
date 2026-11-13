"""Closed inline cleanup fences do not paginate or invoke opaque callbacks."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    ReviewApprovalFinalizer,
    parse_finding_index,
)
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
from review_sensei_github.thread_resolution import (
    FindingThreadResolver,
    ThreadResolutionResult,
)

from review_sensei.session import SessionIdentity
from tests.test_command_capacity_recovery import APP, HEAD, CommandCapacityState


class PlainCleanupFenceTests(unittest.TestCase):
    def fixture(self):
        state = CommandCapacityState(
            schema="1", inline_range=(0, 3), human_count=3, padded=False
        )
        calls = []

        def opener(request, timeout):
            calls.append((request.method, request.full_url))
            return state.open(request, timeout)

        finalizer = ReviewApprovalFinalizer(
            http=GitHubHttp(api_url="https://api.github.test", opener=opener)
        )
        identity = SessionIdentity("owner/repo", 1, repository_id=1)
        ledger = object.__new__(GitHubIssueCommentSessionLedger)
        ledger.app_slug, ledger._evidence_author_id = APP, 99
        journal = object.__new__(FindingLifecycleJournal)
        journal.ledger, journal.identity, journal.head_sha = ledger, identity, HEAD
        journal.operation_id = "test-operation"
        journal._record = SimpleNamespace(
            identity=lambda: identity,
            to_dict=lambda: {"test_selected_root": "original"},
            quarantine_lineage=None,
            finding_lifecycle={
                "manifest": {
                    "binding": {
                        "producer": "github-bot:99",
                        "repository": "owner/repo",
                        "repository_id": 1,
                        "pull_request": 1,
                    }
                }
            },
        )
        finalizer._finding_lifecycle_journal = journal
        args = dict(
            eligibility=state.eligibility,
            index=parse_finding_index(
                state.body, repository_id=1, pull_request=1, head_sha=HEAD
            ),
            reviews=copy.deepcopy(state.reviews),
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        return state, finalizer, args, calls

    def test_exact_frontier_fence_has_two_physical_reads(self):
        state, finalizer, args, calls = self.fixture()
        self.assertIsNotNone(finalizer._plain_cleanup_frontier(**args))
        seen = []

        def resolve(resolver, **kwargs):
            seen.append(kwargs)
            self.assertEqual(kwargs["batch_fence_calls"], 2)
            self.assertTrue(kwargs["fence"]())
            return ThreadResolutionResult()

        with patch.object(FindingThreadResolver, "resolve", resolve):
            finalizer.reconcile_decided_threads(
                token="synthetic",
                repository="owner/repo",
                honored_instances=frozenset(
                    entry.instance for entry in args["index"].entries
                ),
                **args,
            )
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(method == "GET" for method, _ in calls))

    def test_changed_frontier_refuses_without_pr_read_or_pagination(self):
        for change in ("body", "member", "owner", "full"):
            with self.subTest(change=change):
                state, finalizer, args, calls = self.fixture()
                if change == "body":
                    state.reviews[0]["body"] += "edited"
                elif change == "member":
                    state.reviews.append({"id": 900})
                elif change == "owner":
                    state.reviews[0]["user"]["id"] = 100
                else:
                    state.reviews *= 100

                def resolve(resolver, **kwargs):
                    self.assertEqual(kwargs["batch_fence_calls"], 2)
                    self.assertFalse(kwargs["fence"]())
                    return ThreadResolutionResult()

                with patch.object(FindingThreadResolver, "resolve", resolve):
                    finalizer.reconcile_decided_threads(
                        token="synthetic",
                        repository="owner/repo",
                        honored_instances=frozenset(
                            entry.instance for entry in args["index"].entries
                        ),
                        **args,
                    )
                self.assertEqual(len(calls), 1)

    def test_post_read_frontier_is_immutable_single_use_and_scope_bound(self):
        state, finalizer, args, calls = self.fixture()
        state.reviews[0]["body"] += "\nSource-observed updated prose."
        observed = finalizer._load_head_reviews(
            token="synthetic", repository="owner/repo", pull_request=1
        )
        self.assertEqual(len(calls), 1)
        expected = finalizer._plain_review_frontier[8]
        observed[0]["body"] = "mutated returned list"
        self.assertEqual(finalizer._plain_cleanup_frontier(**args), expected)
        self.assertIsNone(finalizer._plain_review_frontier)
        for change in ("operation", "record", "http", "budget"):
            with self.subTest(change=change):
                state, finalizer, args, calls = self.fixture()
                finalizer._load_head_reviews(
                    token="synthetic", repository="owner/repo", pull_request=1
                )
                journal = finalizer._finding_lifecycle_journal
                if change == "operation":
                    journal.operation_id = "different"
                elif change == "record":
                    journal._record.to_dict = lambda: {"test_selected_root": "changed"}
                elif change == "http":
                    finalizer.http = GitHubHttp(api_url="https://api.github.test")
                else:
                    finalizer.evidence_budget = object()
                self.assertIsNone(finalizer._plain_cleanup_frontier(**args))
                self.assertIsNone(finalizer._plain_review_frontier)

    def test_unknown_scope_decoder_and_callback_are_unqualified(self):
        for change in (
            "quarantine",
            "foreign",
            "preflight",
            "trust",
            "producer",
            "full",
            "body",
        ):
            with self.subTest(change=change):
                state, finalizer, args, calls = self.fixture()
                if change == "quarantine":
                    finalizer.quarantine_reader_factory = lambda **kw: None
                elif change == "foreign":
                    finalizer._finding_lifecycle_journal._record.finding_lifecycle[
                        "manifest"
                    ]["binding"]["producer"] = "github-bot:100"
                elif change in {"preflight", "trust"}:
                    setattr(
                        finalizer,
                        "_preflight"
                        if change == "preflight"
                        else "_evaluate_live_pr_trust_boundary",
                        lambda **kw: None,
                    )
                elif change == "producer":
                    finalizer._finding_lifecycle_journal.ledger.evidence_producer = (
                        lambda: "github-bot:99"
                    )
                elif change == "full":
                    args["reviews"] *= 100
                else:
                    args["reviews"][0]["body"] = "external-only evidence"
                self.assertIsNone(finalizer._plain_cleanup_frontier(**args))
                self.assertEqual(calls, [])
