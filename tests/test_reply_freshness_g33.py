"""G33 hosted-route characterization; ordinary base freshness is undecided."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

try:
    from fake_github_http import json_response
    from test_action_reply_entry import ReplyEntryFixture
    from test_api_work_persistence import BASE, HEAD, IDENTITY
    from test_review_run import EntryFixture
    from test_review_run_rescan import partial_fixture
except ModuleNotFoundError:
    from tests.fake_github_http import json_response
    from tests.test_action_reply_entry import ReplyEntryFixture
    from tests.test_api_work_persistence import BASE, HEAD, IDENTITY
    from tests.test_review_run import EntryFixture
    from tests.test_review_run_rescan import partial_fixture

from review_sensei_github.human_assessment import HumanAssessmentPublisher
from review_sensei_github.session_ledger import parse_session_comment

CHANGED_BASE = "c" * 40
CHANGED_HEAD = "d" * 40
OLD_LEARNING = "Use the original base contract."
NEW_LEARNING = "Use the advanced base contract."
LEARNING_DIRECTORY = "/contents/.github/review-sensei/learnings"


class _FreshnessResponses:
    live_base_ref = "main"
    include_learning = False

    def external(self, request, timeout):
        target = urlsplit(request.full_url)
        if self.include_learning and LEARNING_DIRECTORY in target.path:
            assert (
                len(self.accounted.observations)
                == sum(attempt == self.attempt for attempt, _, _ in self.trace) + 1
            )
            self.trace.append((self.attempt, request.get_method(), target.path))
            reference = parse_qs(target.query).get("ref", [None])[0]
            self.learning_refs.append(reference)
            learning = {
                "id": "g33.base-contract",
                "title": "Base contract",
                "rule": OLD_LEARNING if reference == BASE else NEW_LEARNING,
                "scope": ["src/**"],
                "status": "active",
            }
            raw = json.dumps(learning).encode()
            if target.path.endswith(LEARNING_DIRECTORY):
                return json_response(
                    [
                        {
                            "type": "file",
                            "path": ".github/review-sensei/learnings/rule.json",
                            "size": len(raw),
                        }
                    ]
                )
            encoded = base64.b64encode(raw).decode()
            return json_response({"content": encoded, "encoding": "base64"})
        response = super().external(request, timeout)
        if request.get_method() == "GET" and target.path.endswith("/pulls/7"):
            pull = json.loads(response.read(524288))
            pull["base"]["ref"] = self.live_base_ref
            return json_response(pull)
        return response


class FreshnessReplyFixture(_FreshnessResponses, ReplyEntryFixture):
    def initialize_freshness(self):
        self.live_base_ref = "main"
        self.include_learning = True
        self.learning_refs = []


class FreshnessReviewFixture(_FreshnessResponses, EntryFixture):
    pass


def mutate_after_context(fixture, change):
    if change == "base-sha":
        fixture.live_base = CHANGED_BASE
    elif change == "base-ref":
        fixture.live_base_ref = "release"
    elif change == "source-edit":
        fixture.source["body"] = "@reviewsensei A changed source question."
        fixture.source["updated_at"] = "2026-11-09T00:00:00Z"
    elif change == "head":
        fixture.live_head = CHANGED_HEAD
    elif change != "unchanged":
        raise AssertionError("unknown synthetic change")


def arm_reply(fixture, change):
    def response(_request):
        mutate_after_context(fixture, change)
        return fixture.reply_document

    fixture.provider_callback = response


def reply_observation(fixture, code, *, initial_provider_calls=0):
    prompt = fixture.provider_requests[-1].prompt if fixture.provider_requests else ""
    return {
        "exit": code,
        "provider_calls": fixture.provider_calls - initial_provider_calls,
        "reply_posts": fixture.reply_posts,
        "review_states": [review.get("state") for review in fixture.reviews],
        "resolved_threads": list(fixture.resolved_threads),
        "live_head": fixture.live_head,
        "live_base": fixture.live_base,
        "live_base_ref": fixture.live_base_ref,
        "prompt_original_base": "base_sha=" + BASE in prompt,
        "prompt_original_ref": "base_ref=main" in prompt,
        "prompt_original_learning": OLD_LEARNING in prompt,
        "prompt_diff": "@@ -1 +1,2 @@" in prompt,
        "learning_refs": list(fixture.learning_refs),
        "last_status": getattr(fixture.last_result, "status", None),
        "last_diagnostic": getattr(fixture.last_result, "diagnostic", None),
    }


def human_fixture(directory):
    """Reuse the retained20-decision hosted positive control without loosening it."""
    prior, _transaction = partial_fixture(directory)
    original = parse_session_comment(prior.comment["body"], identity=IDENTITY)
    fixture = FreshnessReplyFixture.__new__(FreshnessReplyFixture)
    fixture.__dict__.update(prior.__dict__)
    fixture.next_run()
    fixture.live_head = fixture.requested_head = HEAD
    fixture.prepare_reply()
    fixture.initialize_freshness()
    fixture.include_learning = False
    fixture.thread_rows = [
        {
            "id": "native-thread-" + str(comment["id"]),
            "isResolved": True,
            "comments": {
                "nodes": [
                    {
                        "databaseId": comment["id"],
                        "body": comment["body"],
                        "path": comment["path"],
                        "line": comment["line"],
                        "author": {"login": "reviewsensei", "__typename": "Bot"},
                    }
                ]
            },
        }
        for comment in fixture.inline
    ]
    fixture.source["body"] = (
        "@reviewsensei The caller contract is intentional; the added change is checked."
    )
    fixture.reply_document = {
        "body": "The explanation is supported by the current diff.",
        "assessments": [
            {
                "fingerprint": decision["fingerprint"],
                "decision": "dismissed",
                "rationale": "The current diff contains the intended added change.",
                "human_evidence": "The caller contract is intentional",
                "diff_evidence": "@@ -1 +1,2 @@\n keep\n+change",
            }
            for decision in original.dispositions
        ],
    }
    return fixture, original


def invoke_human_with_late_change(fixture, change):
    """Inject after capability acquisition; preserve the real human write fences."""
    original = HumanAssessmentPublisher.publish

    def publish(publisher, **arguments):
        mutate_after_context(fixture, change)
        return original(publisher, **arguments)

    with patch.object(HumanAssessmentPublisher, "publish", publish):
        return fixture.invoke_reply()


class HostedReplyFreshnessCharacterizationTests(unittest.TestCase):
    def test_ordinary_reply_current_boundary_after_context_changes(self):
        # These base/ref POST outcomes characterize G33, not an adopted policy.
        for change, code, posts, status in (
            ("unchanged", 0, 1, "replied"),
            ("base-sha", 0, 1, "replied"),
            ("base-ref", 0, 1, "replied"),
            ("source-edit", 1, 0, "skipped_edited_source"),
            ("head", 1, 0, "skipped_stale_head"),
        ):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = FreshnessReplyFixture(directory)
                fixture.initialize_freshness()
                arm_reply(fixture, change)
                self.assertEqual(fixture.invoke_reply(), code, fixture.error)
                observed = reply_observation(fixture, code)
                self.assertIs(fixture.reply_document["resolve"], False)
                self.assertEqual(observed["provider_calls"], 1)
                self.assertEqual(observed["reply_posts"], posts)
                self.assertEqual(observed["last_status"], status)
                self.assertTrue(observed["prompt_original_base"])
                self.assertTrue(observed["prompt_original_ref"])
                self.assertTrue(observed["prompt_original_learning"])
                self.assertTrue(observed["prompt_diff"])
                self.assertEqual(observed["learning_refs"], [BASE, BASE])
                self.assertFalse(observed["review_states"])
                self.assertFalse(observed["resolved_threads"])
                self.assertEqual(
                    len(fixture.accounted.observations), len(fixture.trace)
                )
                self.assertGreaterEqual(fixture.accounted.accounting.remaining, 0)
                self.assertEqual(
                    fixture.action()["phase"], "terminal" if posts else "pending"
                )

    def test_normal_review_blocks_base_sha_ref_and_head_changes(self):
        for change in ("unchanged", "base-sha", "base-ref", "head"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = FreshnessReviewFixture(directory)
                fixture.before_publication = lambda: mutate_after_context(
                    fixture, change
                )
                self.assertEqual(
                    fixture.invoke(), 0 if change == "unchanged" else 1, fixture.error
                )
                self.assertEqual(fixture.provider_calls, 1)
                if change == "unchanged":
                    self.assertEqual(
                        [r["state"] for r in fixture.reviews], ["COMMENTED", "APPROVED"]
                    )
                else:
                    self.assertFalse(fixture.reviews)

    def test_human_reassessment_retains_its_stricter_base_head_source_guards(self):
        for change in ("unchanged", "base-sha", "base-ref", "source-edit", "head"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, original = human_fixture(directory)
                before_calls, before_reviews = (
                    fixture.provider_calls,
                    len(fixture.reviews),
                )
                allowed = change in {"unchanged", "base-ref"}
                self.assertEqual(
                    invoke_human_with_late_change(fixture, change),
                    0 if allowed else 1,
                    fixture.error,
                )
                self.assertEqual(fixture.provider_calls - before_calls, 1)
                self.assertEqual(fixture.reply_posts, 1 if allowed else 0)
                self.assertEqual(
                    len(fixture.reviews) - before_reviews, 1 if allowed else 0
                )
                self.assertTrue(
                    fixture.provider_requests[0].prompt.startswith(
                        "Reassess the pending ReviewSensei"
                    )
                )
                self.assertFalse(fixture.resolved_threads)
                self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))
                current = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(current.dispositions, original.dispositions)
                self.assertEqual(len(current.dispositions), 20)
