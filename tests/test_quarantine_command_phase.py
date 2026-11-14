"""A genuine pre-debit trace authenticates families without admitting a lease."""

import unittest
from collections import Counter

import test_quarantine_command_checkpoint as checkpoint_tests
from review_sensei_github import quarantine_command_namespace as namespace
from review_sensei_github.quarantine_command_phase import classify_original_mutation

from review_sensei.errors import ReviewInputError


class CommandPhaseTests(unittest.TestCase):
    def test_original_plain_mutations_have_closed_source_families(self):
        started = set()
        mutations = []

        def observe(frame, event, arg):
            if event == "return" and frame.f_code is namespace.prepare.__code__:
                started.add(frame.f_locals["run"].caller)
            if (
                event != "call"
                or frame.f_code is not namespace.before_sequence.__code__
            ):
                return
            caller = frame.f_locals["caller"]
            if caller not in started:
                return
            description = classify_original_mutation(caller)
            if description is None:
                return
            original = frame.f_back
            request = original.f_locals["request"]
            data = request.data
            try:
                request.data = b'{"body":"substituted"}'
                with self.assertRaisesRegex(ReviewInputError, "changed"):
                    classify_original_mutation(caller)
            finally:
                request.data = data
            self.assertEqual(classify_original_mutation(caller), description)
            url = request.full_url
            try:
                request.full_url = url.replace(
                    "https://", "https://unrelated.invalid/", 1
                )
                with self.assertRaisesRegex(ReviewInputError, "changed"):
                    classify_original_mutation(caller)
            finally:
                request.full_url = url
            mutations.append(description)

        checkpoint_tests.CommandCheckpointTests().exercise(observer=observe)
        self.assertEqual(
            Counter(item.kind for item in mutations),
            {"root": 4, "event": 1, "review": 1, "receipt": 1, "terminal-root": 1},
        )
        self.assertEqual(
            [item.checkpoint_phase for item in mutations if item.checkpoint_phase],
            [
                "entry",
                "decision-cursor",
                "journal-pending",
                "journal-finish",
                "terminal",
            ],
        )
        with self.assertRaisesRegex(ReviewInputError, "original source frame"):
            classify_original_mutation(next(iter(started)))
