"""The selected v5 root must describe its independently retained publication."""

from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from review_sensei_github.errors import GitHubPublicationError

from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger
from tests import test_default_human_inventory as inventory_acceptance
from tests.default_human_inventory_contract import (
    APP,
    HEAD,
    IDENTITY,
    DefaultHumanState,
    application,
    human_checkpoint,
    publish_human,
    reply_human,
)
from tests.test_review_work import AssessingProvider

MARKER = re.compile(r"<!-- reviewsensei:eligibility:v1 ([A-Za-z0-9_=-]+) -->")


def mismatched_latest_result(state):
    """Keep a valid older root; change only the latest eligibility digest."""
    latest = deepcopy(state.reviews[-1])
    latest["id"] += 100
    match = MARKER.search(latest["body"])
    assert match is not None
    document = json.loads(base64.urlsafe_b64decode(match.group(1)))
    original = document["result_digest"]
    document["result_digest"] = "9" * 64
    assert original != document["result_digest"]
    wire = base64.urlsafe_b64encode(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    latest["body"] = (
        latest["body"][: match.start(1)] + wire + latest["body"][match.end(1) :]
    )
    state.reviews.append(latest)


class DefaultHumanReaderBindingTests(unittest.TestCase):
    def published(self, directory):
        path = Path(directory) / "ledger"
        result, key = human_checkpoint(
            LocalSessionLedger(path, enable_partition_writes=True), count=77, detail=3
        )
        state = DefaultHumanState(result.comments)
        outcome = publish_human(state, LocalSessionLedger(path), result, key)
        self.assertEqual(outcome.status, "published")
        return state, path

    def windows_hold(self):
        if sys.platform != "win32":
            return False
        inventory_acceptance.DefaultHumanInventoryTests().assert_windows_rich_publication_hold()
        return True

    def load(self, state, path, *, validate_visible_prose=True):
        return application(
            state, LocalSessionLedger(path)
        ).reviewer.finalizer.load_eligibility(
            token="synthetic-read",
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            app_slug=APP,
            require_valid=True,
            validate_visible_prose=validate_visible_prose,
        )

    def test_current_publication_is_valid_for_full_and_metadata_fences(self):
        if self.windows_hold():
            return
        with tempfile.TemporaryDirectory() as directory:
            state, path = self.published(directory)
            for full in (True, False):
                with self.subTest(full=full):
                    current = self.load(state, path, validate_visible_prose=full)
                    self.assertEqual(len(current.human_review.pending), 77)
                    self.assertIsNotNone(current.human_inventory_reference)

    def test_large_inline_metadata_preserves_complete_pending_human_gate(self):
        if self.windows_hold():
            return
        with tempfile.TemporaryDirectory() as directory:
            state, path = self.published(directory)
            for item in state.inline:
                item["diff_hunk"] = "x" * 9000
            self.assertGreater(len(json.dumps(state.inline).encode()), 512 * 1024)
            before = len(state.calls)
            current = self.load(state, path)
            self.assertEqual(len(current.human_review.pending), 77)
            self.assertIsNotNone(current.human_inventory_reference)
            self.assertTrue(
                all(method == "GET" for method, _, _ in state.calls[before:])
            )
            self.assertNotIn("APPROVE", state.events())

    def test_latest_digest_mismatch_holds_full_and_metadata_without_older_fallback(
        self,
    ):
        if self.windows_hold():
            return
        with tempfile.TemporaryDirectory() as directory:
            state, path = self.published(directory)
            mismatched_latest_result(state)
            before = len(state.calls)
            for full in (True, False):
                with self.subTest(full=full), self.assertRaises(GitHubPublicationError):
                    self.load(state, path, validate_visible_prose=full)
            self.assertTrue(
                all(method == "GET" for method, _, _ in state.calls[before:])
            )
            self.assertNotIn("APPROVE", state.events())

    def test_public_reply_digest_mismatch_stops_before_inference_or_mutation(self):
        if self.windows_hold():
            return
        with tempfile.TemporaryDirectory() as directory:
            state, path = self.published(directory)
            mismatched_latest_result(state)
            provider = AssessingProvider()
            before = len(state.calls)
            with self.assertRaises((GitHubPublicationError, ReviewInputError)):
                reply_human(state, LocalSessionLedger(path), provider)
            self.assertEqual(provider.calls, [])
            self.assertTrue(
                all(method == "GET" for method, _, _ in state.calls[before:])
            )
            self.assertNotIn("APPROVE", state.events())
