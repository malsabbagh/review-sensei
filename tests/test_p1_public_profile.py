"""Synthetic HTTP trace for the public P1 profile.

One pull request, four pending instances, one provider request, and two GitHub
checkpoint activations, then operation-comment acceptance. Ordinary dispatches
stop at 60. This trace exhausts that allowance before accepted readback, so
``github.operation_entry`` stays disabled. Caps are not raised.
"""

from __future__ import annotations

import json
import unittest
from urllib.parse import urlparse

from review_sensei.bounded_evidence import (
    MAX_PART_READS,
    PART_READ_SECONDS,
    EvidenceReadBudget,
)
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.configuration import GitHubSection
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import GitHubHttp
from review_sensei.hosting.github.application import _assessment_bytes
from review_sensei.hosting.github.operation_entry import run_actions_analysis
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from tests.fake_github_http import json_response
from tests.test_activation_tail import HostedTailTests
from tests.test_assessment_queue import DocumentStore, fixture
from tests.test_coalesced_admission import CoalescedAdmissionTests
from tests.test_review_work import AssessingProvider


class P1PublicProfileTests(unittest.TestCase):
    def test_two_checkpoints_then_acceptance_exhaust_ordinary_dispatches(self):
        self.assertEqual(MAX_PART_READS, 64)
        self.assertEqual(PART_READ_SECONDS, 60.0)
        self.assertEqual(GitHubSection().operation_entry, "disabled")

        case = HostedTailTests()
        state, broker, ledger = case.setup_host()
        budget = ledger.evidence_budget
        self.assertIsInstance(budget, EvidenceReadBudget)
        case.reserve(ledger)
        self.assertEqual(budget.calls, 8)
        for phase in (0, 1):
            case.fresh(broker, ledger)
            case.activate(ledger, phase=phase)
        self.assertEqual(budget.calls, 44)
        self.assertEqual(len(state.calls), 38)
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

        with self.assertRaisesRegex(
            ReviewInputError, "partition shared request budget exhausted"
        ):
            run_actions_analysis(
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
        self.assertEqual(budget.calls, 60)
        self.assertEqual(executes["n"], 1)
        for _ in range(4):
            budget.consume(fence=True)
        self.assertEqual(budget.calls, 64)
        with self.assertRaisesRegex(
            ReviewInputError, "partition shared request budget exhausted"
        ):
            budget.consume(fence=True)
