"""Installed complete feedback selector, identity and source budget contracts."""

import json
import sys
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei.bounded_evidence import EvidenceReadBudget  # noqa: E402
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.feedback import (  # noqa: E402
    MAX_FEEDBACK_SELECTOR_BYTES,
    FeedbackAdmissionError,
    FeedbackReference,
    FeedbackSelection,
    FeedbackSelector,
    FeedbackSource,
)
from review_sensei.hosting.github.conversation import PreparedConversation  # noqa: E402
from review_sensei.hosting.github.errors import (  # noqa: E402
    GitHubHTTPResponseTooLargeError,
)
from review_sensei.hosting.github.human_assessment import (  # noqa: E402
    FeedbackBudgetHttp,
    HumanAssessmentPublisher,
)
from review_sensei.models import ConversationContext  # noqa: E402


class SyntheticSources:
    api_url = "https://api.github.test"
    timeout = 30

    def __init__(self):
        self.calls = []
        self.sources = {
            10: {
                "id": 10,
                "updated_at": "exact",
                "body": "@reviewsensei " + "界🙂" * 1200,
                "user": {"login": "alice", "id": 42, "type": "User"},
                "author_association": "MEMBER",
                "issue_url": self.api_url + "/repos/synthetic/repo/issues/1",
            }
        }

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs["timeout_seconds"]))
        if "/issues/comments/" in path:
            source = self.sources.get(int(path.rsplit("/", 1)[1]))
            return (200, source) if source is not None else (404, None)
        return 201, {"id": 99}


class InstalledFeedbackTests(unittest.TestCase):
    def selection(self):
        trigger = FeedbackReference("issue", 10, "exact")
        return FeedbackSelection(
            "synthetic/repo",
            1,
            "a" * 40,
            "b" * 40,
            trigger,
            (
                FeedbackSource(
                    trigger, "alice", 42, "MEMBER", "@reviewsensei factual explanation"
                ),
            ),
        )

    def test_event_lookup_retains_same_trigger_under_mutable_authority(self):
        original = self.selection()
        changed_source = replace(
            original.sources[0],
            reference=replace(original.trigger, updated_at="edited"),
            body="@reviewsensei edited explanation",
            author="bob",
            author_id=43,
        )
        changed = replace(
            original,
            trigger=changed_source.reference,
            sources=(changed_source,),
            head_sha="c" * 40,
            target_ids=("d" * 64,),
        )
        self.assertEqual(original.event_key, changed.event_key)
        self.assertNotEqual(original.digest, changed.digest)
        self.assertNotEqual(
            original.event_key, replace(original, pull_request=2).event_key
        )
        authorization = original.authorization_document()
        self.assertEqual(authorization["event_key"], original.event_key)
        self.assertEqual(authorization["selection_digest"], original.digest)
        self.assertNotIn("body", authorization["sources"][0])
        self.assertEqual(
            authorization["sources"][0]["body_bytes"], original.total_bytes
        )

    def test_closed_explicit_selector_round_trip_and_refusal(self):
        original = self.selection()
        selector = FeedbackSelector(
            original.repository,
            1,
            original.base_sha,
            original.head_sha,
            original.trigger,
            (original.trigger, FeedbackReference("inline", 11, "exact")),
            ("d" * 64,),
        )
        document = selector.to_dict()
        self.assertEqual(
            FeedbackSelector.from_json(json.dumps(document).encode()), selector
        )
        for mutation in (
            lambda value: value.update(url="https://untrusted.invalid/source"),
            lambda value: value.update(target_ids=["d" * 16]),
            lambda value: value.update(target_ids=[]),
            lambda value: value["sources"][0].update(comment_id=True),
            lambda value: value["sources"][0].update(body="select arbitrary targets"),
            lambda value: value.update(sources=value["sources"] * 17),
        ):
            value = deepcopy(document)
            mutation(value)
            with self.assertRaises(FeedbackAdmissionError):
                FeedbackSelector.from_json(json.dumps(value).encode())
        for raw in (b'{"interface":"a","interface":"b"}', b"\xff", b"[" * 10000):
            with self.assertRaises(FeedbackAdmissionError):
                FeedbackSelector.from_json(raw)
        with self.assertRaises(FeedbackAdmissionError) as caught:
            FeedbackSelector.from_json(b" " * (MAX_FEEDBACK_SELECTOR_BYTES + 1))
        self.assertEqual(caught.exception.diagnostic, "feedback_selector_oversized")

    def test_complete_trigger_two_fences_and_write_share_original_budget(self):
        transport = SyntheticSources()
        budget = EvidenceReadBudget()
        guard = budget.consume
        publisher = HumanAssessmentPublisher(http=transport, before_read=guard)
        prepared = PreparedConversation(
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="exact",
            head_sha="b" * 40,
            root_comment_id=10,
            context=ConversationContext(
                messages=(), head_sha="b" * 40, base_sha="a" * 40
            ),
        )
        feedback = publisher.load_selected_feedback(
            token="synthetic",
            repository="synthetic/repo",
            pull_request=1,
            prepared=prepared,
            app_slug="sensei",
        )
        self.assertEqual(feedback.sources[0].body, transport.sources[10]["body"])
        self.assertGreater(feedback.total_bytes, 4096)
        for _ in range(2):
            publisher.revalidate_feedback(
                token="synthetic",
                feedback=feedback,
                app_slug="sensei",
                before_read=guard,
            )
        publisher.http.request(
            "POST",
            "/repos/synthetic/repo/issues/1/comments",
            token="synthetic",
            body={"body": "reply"},
        )
        self.assertEqual(budget.calls, 4)
        self.assertEqual(len(transport.calls), 4)
        self.assertTrue(all(0 < timeout <= 60 for _, _, timeout in transport.calls))
        transport.sources[10]["body"] += " edited"
        with self.assertRaises(FeedbackAdmissionError):
            publisher.revalidate_feedback(
                token="synthetic",
                feedback=feedback,
                app_slug="sensei",
                before_read=guard,
            )
        del transport.sources[10]
        with self.assertRaises(FeedbackAdmissionError):
            publisher.revalidate_feedback(
                token="synthetic",
                feedback=feedback,
                app_slug="sensei",
                before_read=guard,
            )

    def test_restored_expiry_and_exhaustion_never_dispatch_or_refund(self):
        now = [0.0]
        budget = EvidenceReadBudget(
            clock=lambda: now[0], wall_clock=lambda: 1000 + now[0]
        )
        snapshot = budget.snapshot()
        now[0] = 59.0
        restored = EvidenceReadBudget(
            snapshot=snapshot, clock=lambda: now[0], wall_clock=lambda: 1000 + now[0]
        )
        transport = SyntheticSources()
        guarded = FeedbackBudgetHttp(http=transport, before_read=restored.consume)
        guarded.request("POST", "/write", token="synthetic", body={})
        self.assertLessEqual(transport.calls[-1][2], 1.0)
        now[0] = 60.0
        with self.assertRaises(ReviewInputError):
            guarded.request("POST", "/write", token="synthetic", body={})
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(restored.calls, 1)

    def test_evidence_pagination_attempts_share_transport_charge(self):
        transport = SyntheticSources()
        budget = EvidenceReadBudget()

        def request(method, path, **kwargs):
            transport.calls.append((method, path, kwargs["timeout_seconds"]))
            if "per_page=100" in path:
                raise GitHubHTTPResponseTooLargeError("synthetic page exceeded")
            return 200, ["retained original item"]

        transport.request = request
        guarded = FeedbackBudgetHttp(http=transport, before_read=budget.consume)
        self.assertEqual(
            guarded.paginate(
                path="/files", token="synthetic", page_sizes=(100, 50, 25, 5, 1)
            ),
            ["retained original item"],
        )
        self.assertEqual(budget.calls, 2)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("page=1", transport.calls[0][1])
        self.assertIn("page=1", transport.calls[1][1])


if __name__ == "__main__":
    unittest.main()
