"""Current max-active-only continuation never retires its reused native roots."""

import copy
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.application import GitHubApplication
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.session_ledger import parse_session_comment

from tests.test_api_work_persistence import IDENTITY
from tests.test_review_run import EntryFixture


class WindowSupersessionReuseTests(unittest.TestCase):
    def run_two_heads(self, *, simulate_old_guard=False):
        original_publish = GitHubApplication.publish_review
        original_cleanup = ReviewPublisher._supersede_replaced_threads
        seen = []
        published = []

        def policy(app, *args, **kwargs):
            # Supported max-active-only policy, not a legacy/lifecycle opt-out.
            kwargs["options"] = replace(
                kwargs["options"], max_active_findings=2, enable_drift_tracking=False
            )
            result = original_publish(app, *args, **kwargs)
            published.append(
                (app.reviewer, copy.deepcopy(app.reviewer._active_window_states))
            )
            return result

        def cleanup(publisher, **kwargs):
            key = kwargs["repository_id"], kwargs["pull_request"]
            state = publisher._active_window_states[key]
            self.assertNotIn(key, publisher._drift_states)
            before = copy.deepcopy(state)
            # Red control reproduces the prior guard's lack of a window lookup.
            # No persisted state, funding, native response or obligation changes.
            if simulate_old_guard and kwargs["threads"]:
                publisher._active_window_states.pop(key)
            try:
                diagnostic = original_cleanup(publisher, **kwargs)
            finally:
                publisher._active_window_states[key] = state
            self.assertEqual(state, before)
            seen.append((publisher, before, kwargs["threads"], diagnostic))
            return diagnostic

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory, limit=512)
            fixture.provider_comments = [
                dict(
                    path="src/app.py",
                    line=2,
                    side="RIGHT",
                    body=f"Check invariant {i}",
                    symbol=f"s{i}",
                    severity="high",
                    blocking=False,
                    needs_human=True,
                )
                for i in range(2)
            ]
            with (
                patch.object(GitHubApplication, "publish_review", policy),
                patch.object(ReviewPublisher, "_supersede_replaced_threads", cleanup),
            ):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                old_threads = copy.deepcopy(fixture.threads)
                old = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
                self.assertIsNotNone(old.finding_lifecycle)
                fixture.next_run()
                code = fixture.invoke()
                self.assertEqual(code, 1 if simulate_old_guard else 0, fixture.error)
            self.assertEqual(fixture.threads, old_threads)
            self.assertEqual(len(fixture.threads), 2)
            self.assertTrue(all(not t["isResolved"] for t in fixture.threads))
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            # The first publication has a complete empty cleanup candidate set;
            # it intentionally schedules no cleanup closure. The continuation
            # must still inspect both actual predecessor threads.
            self.assertEqual(len(published), 2)
            self.assertIsNot(published[0][0], published[1][0])
            self.assertEqual(len(seen), 1)
            # The application copies its publisher for scoped finalization;
            # compare the authenticated lifecycle, not that private clone ID.
            self.assertIsNot(seen[0][0], published[0][0])
            self.assertEqual(len(seen[0][2]), 2)
            initial = next(iter(published[0][1].values()))
            continued = seen[0][1]
            self.assertEqual(initial.head_sha, old.transaction.head_sha)
            self.assertEqual(continued.head_sha, fixture.live_head)
            self.assertEqual(
                {
                    o.obligation_id
                    for o in initial.authoritative_decision_inventory.obligations
                },
                {
                    o.obligation_id
                    for o in continued.authoritative_decision_inventory.obligations
                },
            )
            self.assertTrue(
                all(
                    o.unresolved
                    for o in continued.authoritative_decision_inventory.obligations
                )
            )
            self.assertEqual(
                {
                    s.target
                    for s in continued.display_active_findings.slots
                    if s.state == "active"
                },
                {t["id"] for t in old_threads},
            )
            current = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(current.finding_lifecycle)
            terminal = current.api_run_accounting["terminal_transaction"]
            if simulate_old_guard:
                self.assertEqual(seen[0][3], "thread_resolution_unconfirmed")
                self.assertIsNone(terminal)
                self.assertIn("thread_cleanup_unconfirmed", fixture.error)
            else:
                self.assertIsNone(seen[0][3])
                self.assertEqual(terminal, current.transaction.transaction_id)
            self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))

    def test_real_hosted_two_head_two_target_reuse_preserves_obligations(self):
        self.run_two_heads()

    def test_old_guard_cannot_complete_the_same_real_continuation(self):
        self.run_two_heads(simulate_old_guard=True)
