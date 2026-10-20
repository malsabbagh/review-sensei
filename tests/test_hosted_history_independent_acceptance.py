"""Independent finite acceptance probes; synthetic boundaries only."""

import copy
import json
import socket
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.application import GitHubApplication, GitHubWriteOptions
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.operation_host import OPERATION_COMMENT_MARKER

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from tests import test_activation_tail as tail
from tests import test_hosted_history_readers as history
from tests import test_human_assessment as human
from tests.fake_github_http import json_response

OriginalHumanState = human.State


class LargeHumanState(OriginalHumanState):
    """Paginate actual bounded wire bytes while retaining the existing race hooks."""

    def __init__(self, eligibility=None, *, count=None, near_capacity=False):
        super().__init__(eligibility)
        self.history = (
            history.captured_shape()
            if count is None
            else [
                {
                    "id": 5000 + i,
                    "body": "x" * 1200,
                    "user": {"login": "other", "type": "User"},
                }
                for i in range(count)
            ]
        )
        self.reviews = [
            *history.captured_reviews(),
            *(
                [{**history.captured_reviews()[0], "id": 4999, "body": "x" * 60_000}]
                if near_capacity
                else []
            ),
            *self.reviews,
        ]
        self.reads = []

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        if request.method == "GET" and parsed.path.endswith(
            ("/issues/1/comments", "/pulls/1/reviews")
        ):
            self.calls.append((request.method, parsed.path, None))
            if self.hook:
                self.hook(self, request.method, parsed.path, None)
            query = parse_qs(parsed.query)
            size = int(query.get("per_page", [100])[0])
            page = int(query.get("page", [1])[0])
            self.reads.append((parsed.path, size, page))
            items = (
                [*self.history, self.source, *self.replies]
                if parsed.path.endswith("/comments")
                else self.reviews
            )
            return json_response(items[(page - 1) * size : page * size])
        return super().open(request, timeout)


class HostedOperationState(LargeHumanState):
    """Include the real HTTP operation port and its original shared accounting."""

    def __init__(self):
        super().__init__(near_capacity=True)
        self.source["user"]["id"] = 7

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        if request.method == "GET" and path == "/user":
            self.calls.append(("GET", path, None))
            return json_response({"id": 42, "login": human.APP, "type": "Bot"})
        if (
            "/issues/comments/" in path
            and path.rsplit("/", 1)[1].isdigit()
            and not path.endswith("/10")
        ):
            identifier = int(path.rsplit("/", 1)[1])
            found = next((x for x in self.replies if x["id"] == identifier), None)
            if found is not None:
                payload = json.loads(request.data) if request.data else None
                self.calls.append((request.method, path, payload))
                if request.method == "PATCH":
                    found["body"] = payload["body"]
                return json_response(found)
        result = super().open(request, timeout)
        for reply in self.replies:
            reply["user"]["id"] = 42
        return result


class IndependentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("live socket forbidden"),
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_human_verification_recovery_and_replay_with_both_large_histories(self):
        for count in (None, 100, 250):
            with self.subTest(history=count or "captured-27"):
                created = []

                def factory(*args, count=count, created=created, **kwargs):
                    state = LargeHumanState(
                        *args, count=count, near_capacity=count is not None, **kwargs
                    )
                    created.append(state)
                    return state

                with patch.object(human, "State", side_effect=factory):
                    human.HumanAssessmentTests().test_retry_after_durable_reassessment_resumes_approval_without_inference()
                self.assertEqual(len(created), 1)
                state = created[0]
                self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
                if count is not None:
                    self.assertTrue(
                        any(
                            "/reviews" in path and size < 100
                            for path, size, page in state.reads
                        )
                    )
                if count is None:
                    self.assertTrue(
                        any(
                            "/comments" in path and size < 100
                            for path, size, page in state.reads
                        )
                    )

    def test_combined_oversized_human_histories_complete_in_original_shared_budget(
        self,
    ):
        for width in (None, 16_000, 20_000):
            with self.subTest(medium_body_bytes=width):
                state = (
                    LargeHumanState(near_capacity=True)
                    if width is None
                    else LargeHumanState(count=100)
                )
                if width is not None:
                    for item in state.history:
                        item["body"] = "x" * width
                original = copy.deepcopy(state.history)
                provider = human.Provider(human.response_for(state.eligibility))
                outcome, _ = state.application_reply(provider)
                self.assertEqual(outcome.approval_status, "approved")
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
                self.assertEqual(state.history, original)
                self.assertLessEqual(len(state.calls), 64)

    def test_real_http_operation_port_completes_and_replays_both_large_histories(self):
        state = HostedOperationState()
        history_before = copy.deepcopy(state.history)
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                provider = human.Provider(
                    human.response_for(state.eligibility) if attempt == 0 else {}
                )
                budget = EvidenceReadBudget()
                start = len(state.calls)
                outcome = GitHubApplication(
                    broker=human.Broker(),
                    reviewer=None,
                    learner=None,
                    replier=ConversationPublisher(http=state.http),
                    http=state.http,
                ).generate_and_publish_reply(
                    options=GitHubWriteOptions(
                        github_writes=True, mention_replies=True
                    ),
                    oidc_token="synthetic-oidc",
                    read_token="read",
                    repository="owner/repo",
                    pull_request=1,
                    source_comment_id=10,
                    source_updated_at="updated",
                    expected_head_sha=human.HEAD,
                    reply_provider=provider,
                    model="fixture-model",
                    app_slug=human.APP,
                    root_comment_id=10,
                    source_kind="issue",
                    operation_entry="enabled",
                    repository_id=1,
                    app_id=42,
                    run_id="123",
                    run_attempt="1",
                    evidence_budget=budget,
                )
                self.assertEqual(
                    outcome.approval_status,
                    "approved" if attempt == 0 else "already_approved",
                )
                self.assertEqual(len(provider.calls), 1 if attempt == 0 else 0)
                self.assertEqual(budget.calls, len(state.calls) - start)
                self.assertLessEqual(budget.calls, 64)
                if attempt:
                    self.assertFalse(
                        any(
                            method in {"POST", "PATCH"}
                            and body is not None
                            and OPERATION_COMMENT_MARKER in body.get("body", "")
                            for method, path, body in state.calls[start:]
                        )
                    )
                if attempt == 2:
                    self.assertEqual(outcome.status, "already_replied")
        self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
        self.assertEqual(state.history, history_before)
        self.assertEqual(
            sum(OPERATION_COMMENT_MARKER in reply["body"] for reply in state.replies), 1
        )
        self.assertEqual(
            state.reply_count, 3
        )  # one operation receipt and two distinct status replies

    def test_human_source_head_base_and_competing_result_races_with_large_histories(
        self,
    ):
        created = []

        def factory(*args, **kwargs):
            state = LargeHumanState(*args, **kwargs)
            created.append(state)
            return state

        with patch.object(human, "State", side_effect=factory):
            human.HumanAssessmentTests().test_source_head_base_and_same_head_result_races_prevent_approval()
        self.assertEqual(len(created), 4)
        self.assertTrue(all(state.events() == [] for state in created))

    def test_sealed_adaptive_fence_stale_head_keeps_original_root_and_burns_ticket(
        self,
    ):
        case = tail.HostedTailTests()
        state, broker, ledger = case.setup_host()
        for item in history.captured_shape():
            item = {
                **item,
                "id": item["id"] + 5000,
                "created_at": "2020-01-01T00:00:01Z",
                "updated_at": "2020-01-01T00:00:01Z",
            }
            state.comments[item["id"]] = item
        case.reserve(ledger)
        case.fresh(broker, ledger, pages=2)
        original_root = copy.deepcopy(state.comments[1000])
        tickets = []
        reserve = ledger.evidence_budget.reserve_tail

        def stale_after_seal(plan):
            ticket = reserve(plan)
            tickets.append(ticket)
            state.live_head = "c" * 40
            return ticket

        start = len(state.calls)
        with (
            patch.object(
                ledger.evidence_budget, "reserve_tail", side_effect=stale_after_seal
            ),
            self.assertRaises((ReviewInputError, GitHubPublicationError)),
        ):
            case.activate(ledger, pages=2)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(state.comments[1000], original_root)
        self.assertFalse(any(method == "PATCH" for method, url in state.calls[start:]))
        self.assertIsNone(ledger._activation_ticket)
        # Prepaid undispatched writes remain charged after the stale-head refusal.
        self.assertGreater(
            ledger.evidence_budget.calls, len(state.calls) + len(broker.calls)
        )
        self.assertLessEqual(ledger.evidence_budget.calls, 64)
        before = ledger.evidence_budget.calls
        with self.assertRaises(ReviewInputError):
            case.activate(ledger, pages=2)
        self.assertEqual(ledger.evidence_budget.calls, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
