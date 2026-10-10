"""Independent hosted acceptance for the delivered #239 packing correction."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.hosting.github.publication import approval_eligibility_from_body
from review_sensei.human_assessment import HumanReviewFinding, PendingHumanReview
from review_sensei.models import ProviderResponse
from tests import test_human_assessment as host
from tests.test_review_work import AssessingProvider, large_patch


def grouped_state(path_count, finding_count=1):
    original = host.prior()
    paths = ("src/app.py",) + tuple(
        f"src/related_{index}.py" for index in range(1, path_count)
    )
    inventory = PendingHumanReview(
        host.BASE,
        tuple(
            HumanReviewFinding(
                f"{index:064x}",
                paths[0],
                f"Concern {index}: confirm local-only behavior.",
                required_paths=paths,
            )
            for index in range(finding_count)
        ),
    )
    state = host.State(replace(original, human_review=inventory))
    state.files = [
        {"filename": path, "patch": host.DIFF, "additions": 1, "deletions": 1}
        for path in paths
    ]
    return state


def request_document(request):
    return json.JSONDecoder().raw_decode(
        request.prompt[request.prompt.index('{"head_sha"') :]
    )[0]


class PackingLifecycleTests(unittest.TestCase):
    def test_one_atomic_finding_with_1_4_5_8_paths_reaches_hosted_approval(self):
        for count in (1, 4, 5, 8):
            with self.subTest(paths=count):
                state = grouped_state(count)
                provider = AssessingProvider()
                outcome, broker = state.application_reply(
                    provider, work_budgets=ReviewWorkBudgets(mode="unified")
                )
                self.assertEqual(outcome.approval_status, "approved")
                self.assertEqual(len(provider.calls), 1)
                request = provider.calls[0]
                self.assertLessEqual(len(request.prompt.encode()), 48 * 1024)
                self.assertLessEqual(request.max_response_bytes, 16 * 1024)
                document = request_document(request)
                self.assertEqual(len(document["pending"]), 1)
                for file in state.files:
                    self.assertIn(
                        "path=" + file["filename"] + "\n" + file["patch"],
                        document["current_diff"],
                    )
                self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
                self.assertEqual(state.reply_count, 1)
                self.assertEqual(broker.capabilities, ["issue_reply", "review_publish"])
                refreshed = approval_eligibility_from_body(state.reviews[-2]["body"])
                self.assertEqual(
                    refreshed.human_review.findings,
                    state.eligibility.human_review.findings,
                )
                self.assertFalse(refreshed.human_review.pending)
                self.assertEqual(
                    refreshed.result_digest, state.eligibility.result_digest
                )
                reads = [
                    index
                    for index, (method, path, _) in enumerate(state.calls)
                    if method == "GET" and path.endswith("/pulls/1/reviews")
                ]
                writes = [
                    index
                    for index, (method, path, _) in enumerate(state.calls)
                    if method == "POST" and path.endswith("/pulls/1/reviews")
                ]
                self.assertTrue(any(writes[0] < read < writes[1] for read in reads))
                replay_provider = AssessingProvider()
                replay, _ = state.application_reply(
                    replay_provider, work_budgets=ReviewWorkBudgets(mode="unified")
                )
                self.assertEqual(replay.approval_status, "already_approved")
                self.assertFalse(replay_provider.calls)
                self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
                self.assertEqual(state.reply_count, 1)

    def test_shared_eight_paths_count_as_five_decisions_and_output_cap_tightens(self):
        for output_tokens, expected in ((None, (4, 1)), (2048, (3, 2))):
            with self.subTest(output_tokens=output_tokens):
                state = grouped_state(8, 5)
                provider = AssessingProvider()
                if output_tokens is not None:
                    provider.max_output_tokens = output_tokens
                outcome, _ = state.application_reply(
                    provider, work_budgets=ReviewWorkBudgets(mode="unified")
                )
                self.assertEqual(outcome.approval_status, "approved")
                self.assertEqual(
                    tuple(len(request_document(r)["pending"]) for r in provider.calls),
                    expected,
                )
                for request in provider.calls:
                    for file in state.files:
                        self.assertIn(file["filename"], request.prompt)
                self.assertEqual(state.events(), ["COMMENT", "APPROVE"])

    def test_eight_call_cap_retains_every_unprocessed_obligation(self):
        state = grouped_state(1, 33)
        provider = AssessingProvider()
        outcome, _ = state.application_reply(
            provider, work_budgets=ReviewWorkBudgets(mode="unified")
        )
        self.assertEqual(len(provider.calls), 8)
        self.assertNotEqual(outcome.approval_status, "approved")
        refreshed = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertEqual(
            refreshed.human_review.findings, state.eligibility.human_review.findings
        )
        self.assertEqual(len(refreshed.human_review.resolved), 32)
        self.assertEqual(len(refreshed.human_review.pending), 1)
        self.assertEqual(state.events(), ["COMMENT"])

    def test_missing_or_oversized_complete_group_never_dispatches_or_approves(self):
        for case in ("missing", "single-oversized", "collectively-oversized"):
            with self.subTest(case=case):
                state = grouped_state(8)
                if case == "missing":
                    state.files.pop()
                elif case == "single-oversized":
                    state.files[-1]["patch"] = large_patch(60 * 1024)
                else:
                    for file in state.files:
                        file["patch"] = large_patch(7 * 1024)
                provider = AssessingProvider()
                outcome, _ = state.application_reply(
                    provider, work_budgets=ReviewWorkBudgets(mode="unified")
                )
                self.assertFalse(provider.calls)
                self.assertNotEqual(outcome.approval_status, "approved")
                self.assertEqual(state.events(), [])
                self.assertEqual(
                    approval_eligibility_from_body(state.reviews[-1]["body"]),
                    state.eligibility,
                )

    def test_each_related_path_requires_a_valid_literal_citation(self):
        class MissingCitationProvider(AssessingProvider):
            def complete(self, request):
                response = super().complete(request)
                payload = json.loads(response.text)
                payload["assessments"][0]["related_diff_evidence"].pop()
                return ProviderResponse(json.dumps(payload), self.name, self.model)

        state = grouped_state(8)
        provider = MissingCitationProvider()
        outcome, _ = state.application_reply(
            provider, work_budgets=ReviewWorkBudgets(mode="unified")
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertNotEqual(outcome.approval_status, "approved")
        self.assertEqual(state.events(), [])
        self.assertEqual(
            approval_eligibility_from_body(state.reviews[-1]["body"]), state.eligibility
        )


if __name__ == "__main__":
    unittest.main()
