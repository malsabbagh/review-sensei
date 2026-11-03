"""Native synthetic finalization, preserving review and accounting semantics."""

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.review_finalization_diagnostics import close_review_run
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import IDENTITY
from test_review_run import EntryFixture, _CompletionFixture

from review_sensei.errors import ReviewInputError


def observations(text):
    prefix = "review-sensei: finalization "
    return [
        json.loads(line[len(prefix) :])
        for line in text.splitlines()
        if line.startswith(prefix)
    ]


class ReviewFinalizationDiagnosticsTests(unittest.TestCase):
    def test_diff_fixtures_preserve_lf_under_crlf_platform_defaults(self):
        write_text = Path.write_text

        def platform_write(path, data, encoding=None, errors=None, newline=None):
            if path.name == "review.patch" and newline is None:
                newline = "\r\n"
            return write_text(
                path, data, encoding=encoding, errors=errors, newline=newline
            )

        with patch.object(Path, "write_text", platform_write):
            # Prove that this host-independent control really translates LF.
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "review.patch"
                path.write_text("control\n")
                self.assertEqual(path.read_bytes(), b"control\r\n")
            for case in (
                "test_partial_publishes_and_closes_without_completing_review",
                "test_unconfirmed_cleanup_does_not_enter_accounting_close",
            ):
                with self.subTest(case=case):
                    getattr(self, case)()

    def test_partial_publishes_and_closes_without_completing_review(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            patch = f.root / "review.patch"
            patch.write_text(
                patch.read_text()
                + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\nindex 0000000..1111111\nBinary files /dev/null and b/image.bin differ\n",
                encoding="utf-8",
                newline="\n",
            )
            self.assertEqual(f.invoke(), 0, f.error)
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(record.transaction.phase, "publication_succeeded")
            self.assertEqual(
                record.api_run_accounting["terminal_transaction"],
                record.transaction.transaction_id,
            )
            self.assertIn("Review incomplete", f.reviews[-1]["body"])
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            observed = observations(f.error)
            self.assertEqual([o["stage"] for o in observed], ["entered", "returned"])
            self.assertTrue(observed[-1]["close_returned"])
            self.assertGreaterEqual(observed[-1]["mutation_dispatches"], 1)

    def test_lost_terminal_response_stays_unknown_and_blocks_fresh_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            f = _CompletionFixture(directory, mode="confirmed")
            f.terminal_write_fault = True
            self.assertEqual(f.invoke(human=True), 1, f.error)
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(record.transaction.phase, "publication_succeeded")
            self.assertIsNone(record.api_run_accounting["terminal_transaction"])
            observed = observations(f.error)[-1]
            self.assertEqual(observed["stage"], "raised")
            self.assertGreaterEqual(observed["unknown_mutations"], 1)
            previous_calls, previous_reviews = f.provider_calls, len(f.reviews)
            f.next_run()
            self.assertEqual(f.invoke(human=True), 1, f.error)
            self.assertIn("successful review exit", f.error)
            self.assertEqual(f.provider_calls, previous_calls)
            self.assertEqual(len(f.reviews), previous_reviews)
            self.assertEqual(
                parse_session_comment(f.comment["body"], identity=IDENTITY), record
            )

    def test_unconfirmed_cleanup_does_not_enter_accounting_close(self):
        with tempfile.TemporaryDirectory() as directory:
            f = _CompletionFixture(directory, mode="refused")
            patch = f.root / "review.patch"
            original = patch.read_text()
            patch.write_text(
                original
                + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\nindex 0000000..1111111\nBinary files /dev/null and b/image.bin differ\n",
                encoding="utf-8",
                newline="\n",
            )
            self.assertEqual(f.invoke(human=True), 0, f.error)
            patch.write_text(original, encoding="utf-8", newline="\n")
            f.next_run()
            self.assertEqual(f.invoke(human=True), 1, f.error)
            observed = observations(f.error)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["stage"], "not_entered")
            self.assertEqual(observed[0]["reason"], "thread_cleanup_unconfirmed")
            self.assertIsNone(observed[0]["mutation_dispatches"])

    def test_refusal_before_dispatch_and_interrupt_are_distinct_from_no_entry(self):
        for error in (ReviewInputError("private credential"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):

                def refuse(*args):
                    raise error

                caller = SimpleNamespace(observations=[], finish_review_run=refuse)
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(type(error)):
                    close_review_run(caller, None, None, None)
                values = observations(output.getvalue())
                self.assertIsNone(values[0]["mutation_dispatches"])
                self.assertEqual(values[-1]["mutation_dispatches"], 0)
                self.assertEqual(values[-1]["stage"], "raised")
                self.assertNotIn("private credential", output.getvalue())

    def test_sink_failure_preserves_successful_close(self):
        class BrokenSink:
            def write(self, value):
                raise OSError("sink unavailable")

        marker = object()
        caller = SimpleNamespace(
            observations=[], finish_review_run=lambda *args: marker
        )
        with redirect_stderr(BrokenSink()):
            self.assertIs(close_review_run(caller, None, None, None), marker)

    @unittest.skipUnless(os.name == "posix", "SIGKILL requires POSIX")
    def test_process_death_retains_entry_with_unknown_dispatch_count(self):
        script = """
import os, signal
from review_sensei_github.review_finalization_diagnostics import close_review_run
class Caller:
    observations = []
    def finish_review_run(self, *args):
        os.kill(os.getpid(), signal.SIGKILL)
close_review_run(Caller(), None, None, None)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(completed.returncode, -signal.SIGKILL)
        value = observations(completed.stderr)
        self.assertEqual(len(value), 1)
        self.assertEqual(value[0]["stage"], "entered")
        self.assertIsNone(value[0]["mutation_dispatches"])
        self.assertIsNone(value[0]["unknown_mutations"])

    def test_already_published_terminal_setup_refusal_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            self.assertEqual(f.invoke(), 0, f.error)
            head = f.live_head
            f.next_run()
            f.requested_head = f.live_head = head
            with patch.object(
                ApiRunRequestTransport,
                "begin_terminal",
                side_effect=ReviewInputError("private setup failure"),
            ):
                self.assertEqual(f.invoke(), 1, f.error)
            value = observations(f.error)
            self.assertEqual(len(value), 1)
            self.assertEqual(value[0]["stage"], "not_entered")
            self.assertEqual(value[0]["reason"], "terminal_entry_failed")
            self.assertNotIn("private setup failure", f.error)


if __name__ == "__main__":
    unittest.main()
