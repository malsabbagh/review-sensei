"""Synthetic HTTP trace for the public P1 profile.

One pull request, four pending instances, one provider request, and two GitHub
checkpoint activations, then operation-comment acceptance and replay. The
comment list is read once and later checks are fenced, so this trace stays
inside 60 ordinary requests. The product default stays ``enabled``.
``disabled`` is the rollback. Caps are not raised.
"""

from __future__ import annotations

import json
import unittest
from urllib.parse import urlparse

from review_sensei_github import GitHubHttp
from review_sensei_github.application import _assessment_bytes
from review_sensei_github.operation_entry import run_actions_analysis

from review_sensei.bounded_evidence import (
    MAX_PART_READS,
    PART_READ_SECONDS,
    EvidenceReadBudget,
)
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.configuration import GitHubSection
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from tests import test_review_transaction as review_fixture
from tests.fake_github_http import json_response
from tests.test_assessment_queue import DocumentStore, fixture
from tests.test_coalesced_admission import CoalescedAdmissionTests
from tests.test_review_work import AssessingProvider


class P1PublicProfileTests(unittest.TestCase):
    def test_p1_profile_stays_within_ordinary_dispatches(self):
        self.assertEqual(MAX_PART_READS, 64)
        self.assertEqual(PART_READ_SECONDS, 60.0)
        self.assertEqual(GitHubSection().operation_entry, "enabled")

        from tests.test_activation_tail import HostedTailTests

        case = HostedTailTests()
        state, broker, ledger = case.setup_host()
        budget = ledger.evidence_budget
        self.assertIsInstance(budget, EvidenceReadBudget)
        case.reserve(ledger)
        self.assertEqual(budget.calls, 7)
        for phase in (0, 1):
            case.fresh(broker, ledger)
            case.activate(ledger, phase=phase)
        self.assertEqual(budget.calls, 25)
        self.assertEqual(len(state.calls), 19)
        self.assertEqual(len(broker.calls), 6)

        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        provider = AssessingProvider()
        work = CoalescedAdmissionTests().run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            checkpoint=CoalescedAdmissionTests().checkpoint(store),
            tracker=ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1)),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(store.writes), 2)
        self.assertEqual(len(work.reply.decisions), 4)

        comments: dict[int, dict[str, object]] = {}
        next_id = {"n": 1}
        executes = {"n": 0}

        def opener(request, timeout):
            budget.consume()
            path = urlparse(request.full_url).path
            method = request.method
            if path == "/user":
                return json_response({"id": 42, "type": "Bot", "login": "sensei[bot]"})
            if (
                method == "GET"
                and path.endswith("/comments")
                and "/issues/comments/" not in path
            ):
                return json_response(list(comments.values()))
            if method == "POST" and path.endswith("/comments"):
                body = json.loads(request.data)["body"]
                comment_id = next_id["n"]
                next_id["n"] += 1
                comment = {
                    "id": comment_id,
                    "body": body,
                    "user": {"id": 42, "type": "Bot"},
                }
                comments[comment_id] = comment
                return json_response(comment, 201)
            if "/issues/comments/" in path:
                comment_id = int(path.rstrip("/").rsplit("/", 1)[-1])
                if method == "PATCH":
                    comments[comment_id]["body"] = json.loads(request.data)["body"]
                return json_response(comments[comment_id])
            return json_response({"message": "unexpected"}, 404)

        def execute():
            executes["n"] += 1
            return work

        values = dict(
            http=GitHubHttp(api_url="https://api.github.test", opener=opener),
            token="synthetic-token",
            repository="owner/repo",
            repository_id=7,
            pull_request=11,
            run_id="100",
            run_attempt="1",
            app_id=99,
            reservation_id="reservation-p1",
            attestation_digest="ab" * 32,
            execute=execute,
            encode=_assessment_bytes,
            trigger="reassessment",
            delivery_id="p1-delivery",
            grant="grant-p1",
        )
        first = run_actions_analysis(**values)
        second = run_actions_analysis(**values)
        self.assertIsNotNone(getattr(first, "fresh", None))
        self.assertIsNone(getattr(second, "fresh", None))
        self.assertIsNotNone(getattr(second, "replay_payload", None))
        self.assertEqual(executes["n"], 1)
        self.assertLessEqual(budget.calls, 60)
        self.assertEqual(budget.calls, 30)

    def _checkpoints(self, comments):
        from tests.test_activation_tail import HostedTailTests

        case = HostedTailTests()
        state, broker, ledger = case.setup_host()
        repository = review_fixture.IDENTITY.repository
        pull_request = review_fixture.IDENTITY.pull_request
        for comment_id, body, bot in comments:
            state.comments[comment_id] = {
                "id": comment_id,
                "body": body,
                "user": {
                    "id": 55 if bot else 7,
                    "type": "Bot" if bot else "User",
                    "login": "sensei[bot]" if bot else "dev",
                },
                "updated_at": "2019-01-01T00:00:00Z",
                "issue_url": (
                    f"https://api.github.test/repos/{repository}/issues/{pull_request}"
                ),
            }
        case.reserve(ledger)
        for phase in (0, 1):
            case.fresh(broker, ledger)
            case.activate(ledger, phase=phase)
        return ledger.evidence_budget.calls, state.calls

    def test_ordinary_comment_threads_stay_inside_the_cap(self):
        for count in (25, 300):
            with self.subTest(count=count):
                calls, _http = self._checkpoints(
                    [(3000 + index, "note", False) for index in range(count)]
                )
                self.assertLessEqual(calls, 60)

    def test_a_run_of_maximum_size_comments_steps_back_to_large_pages(self):
        comments = []
        for index in range(300):
            huge = 100 <= index < 120
            comments.append((3000 + index, "m" * 60_000 if huge else "note", huge))
        calls, http = self._checkpoints(comments)
        self.assertLessEqual(calls, 60)
        sizes = []
        for method, url in http:
            if method == "GET" and "per_page=" in url:
                sizes.append(int(url.split("per_page=", 1)[1].split("&", 1)[0]))
        smaller = next(index for index, size in enumerate(sizes) if size == 5)
        self.assertTrue(any(size == 100 for size in sizes[smaller + 1 :]))
