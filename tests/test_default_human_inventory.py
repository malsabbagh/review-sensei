"""Default application acceptance; synthetic external APIs, no trusted host seam."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.errors import GitHubPublicationError

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.session import LocalSessionLedger
from tests.default_human_inventory_contract import (
    APP,
    BASE,
    DEFAULT_INVENTORY_INTENT_MARKER,
    HEAD,
    IDENTITY,
    DefaultHumanState,
    application,
    human_checkpoint,
    human_inventory_parts,
    next_mention,
    publish_human,
    reply_human,
)
from tests.test_publication_recovery import recover
from tests.test_review_work import AssessingProvider


class DefaultHumanInventoryTests(unittest.TestCase):
    def assert_windows_rich_publication_hold(self, *, count=77, detail=3):
        """Windows has no safe local content witness; qualify its finite hold."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger"
            result, key = human_checkpoint(
                LocalSessionLedger(path, enable_partition_writes=True),
                count=count,
                detail=detail,
            )
            state = DefaultHumanState(result.comments)
            budget = EvidenceReadBudget()
            with self.assertRaisesRegex(
                (GitHubPublicationError, ReviewInputError), "budget"
            ):
                publish_human(
                    state,
                    LocalSessionLedger(path),
                    result,
                    key,
                    publication_budget=budget,
                )
            self.assertLessEqual(budget.calls, 64)
            self.assertLessEqual(len(state.calls), 64)
            self.assertNotIn("APPROVE", state.events())
            record = LocalSessionLedger(path).load(IDENTITY).record
            self.assertIsNotNone(record)
            self.assertNotEqual(record.transaction.phase, "publication_succeeded")

    def crash_before_root(self, state, path, result, key, *, before_intent=False):
        def interrupt(_state, method, target, body):
            if method != "POST" or not isinstance(body, dict):
                return
            if before_intent:
                selected = target.endswith(
                    "/issues/1/comments"
                ) and DEFAULT_INVENTORY_INTENT_MARKER in body.get("body", "")
            else:
                selected = target.endswith(
                    "/pulls/1/reviews"
                ) and "<!-- reviewsensei:eligibility:v1" in body.get("body", "")
            if selected:
                if not before_intent:
                    self.assertTrue(human_inventory_parts(state))
                raise KeyboardInterrupt(
                    "synthetic process interruption before root POST"
                )

        state.hook = interrupt
        try:
            with self.assertRaises(KeyboardInterrupt):
                publish_human(state, LocalSessionLedger(path), result, key)
        finally:
            state.hook = None
        self.assertEqual(state.reviews, [])
        self.assertEqual(
            LocalSessionLedger(path).load(IDENTITY).record.transaction.phase,
            "publication_failed",
        )

    def load_current(self, state, path):
        reader = application(state, LocalSessionLedger(path)).reviewer.finalizer
        eligibility = reader.load_eligibility(
            token="synthetic-read",
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            app_slug=APP,
            require_valid=True,
        )
        self.assertIsNotNone(eligibility)
        return eligibility

    def assert_visible_inventory(self, state, result, eligibility, path):
        reader = application(state, LocalSessionLedger(path)).reviewer.finalizer
        index = reader.finding_index_from_reviews(
            state.reviews,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=eligibility,
        )
        self.assertIsNotNone(index)
        self.assertEqual(
            {item.instance: item.lifecycle for item in index.entries},
            {
                finding_instance_fingerprint(comment): finding_lifecycle_for_comment(
                    comment
                ).fingerprint
                for comment in result.comments
            },
        )
        visible = "\n".join(item["body"] for item in [*state.reviews, *state.inline])
        for comment in result.comments:
            self.assertIn(comment.body, visible)
        self.assertNotIn("omitted for size", visible)

    def assert_complete_pending_publication(
        self, *, count, detail, complete_metadata=True
    ):
        if count >= 77 and sys.platform == "win32":
            self.assert_windows_rich_publication_hold(count=count, detail=detail)
            return
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(
                Path(directory) / "ledger", enable_partition_writes=True
            )
            result, key = human_checkpoint(ledger, count=count, detail=detail)
            expected = PendingHumanReview.from_result(result, BASE)
            state = DefaultHumanState(result.comments)
            # Publication is a distinct invocation after completed analysis;
            # reload its actual checkpoint and parts, not an in-memory record.
            ledger = LocalSessionLedger(Path(directory) / "ledger")
            outcome = publish_human(state, ledger, result, key)
            self.assertEqual(outcome.status, "published")
            # A new ordinary application receives the actual persisted ledger;
            # it must derive reader authority from that state, not a test host.
            reader = application(
                state, LocalSessionLedger(Path(directory) / "ledger")
            ).reviewer.finalizer
            eligibility = reader.load_eligibility(
                token="synthetic-read",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                require_valid=True,
            )
            self.assertIsNotNone(eligibility)
            self.assertEqual(eligibility.human_review.findings, expected.findings)
            self.assertEqual(eligibility.human_review.base_sha, BASE)
            if complete_metadata:
                self.assertEqual(
                    eligibility.human_review.inventory_document(),
                    expected.inventory_document(),
                )
            self.assertEqual(len(eligibility.human_review.pending), count)
            self.assertEqual(eligibility.human_review.resolved, ())
            self.assertTrue(eligibility.facts.has_human_adjudication_findings)
            self.assertEqual(eligibility.facts.qualification, "not-required")
            index = reader.finding_index_from_reviews(
                state.reviews,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                eligibility=eligibility,
            )
            self.assertIsNotNone(index)
            self.assertEqual(
                {item.instance: item.lifecycle for item in index.entries},
                {
                    finding_instance_fingerprint(
                        comment
                    ): finding_lifecycle_for_comment(comment).fingerprint
                    for comment in result.comments
                },
            )
            visible = "\n".join(
                item["body"] for item in [*state.reviews, *state.inline]
            )
            for comment in result.comments:
                self.assertIn(comment.body, visible)
            self.assertNotIn("omitted for size", visible)
            self.assertEqual(state.check_runs[-1]["conclusion"], "action_required")
            self.assertNotIn("APPROVE", state.events())
            restored = (
                LocalSessionLedger(Path(directory) / "ledger").load(IDENTITY).record
            )
            self.assertEqual(restored.transaction.phase, "publication_succeeded")

    def test_control_inline_human_findings_use_the_same_default_publication_and_reader(
        self,
    ):
        self.assert_complete_pending_publication(
            count=12, detail=1, complete_metadata=False
        )

    def test_complete_77_rich_human_findings_publish_and_fresh_default_reader_keeps_all_pending(
        self,
    ):
        self.assert_complete_pending_publication(count=77, detail=3)

    def assert_lifecycle(self, *, count, detail, gate=None):
        # The rich lifecycle, extra gates, faults and recovery are POSIX
        # qualification. Windows qualifies only the complete-baseline capacity
        # hold for these original bodies, never those downstream sequences.
        if count >= 77 and sys.platform == "win32":
            self.assert_windows_rich_publication_hold(count=count, detail=detail)
            return
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger"
            result, key = human_checkpoint(
                LocalSessionLedger(path, enable_partition_writes=True),
                count=count,
                detail=detail,
                gate=gate,
            )
            state = DefaultHumanState(result.comments)
            outcome = publish_human(
                state, LocalSessionLedger(path), result, key, gate=gate
            )
            self.assertEqual(outcome.status, "published")
            original = self.load_current(state, path)
            self.assertEqual(len(original.human_review.pending), count)
            self.assert_visible_inventory(state, result, original, path)
            self.assertNotIn("APPROVE", state.events())
            remaining = count
            revision = 0
            while remaining:
                revision += 1
                self.assertLessEqual(revision, count)
                next_mention(state, revision)
                provider = AssessingProvider()
                outcome = reply_human(state, LocalSessionLedger(path), provider)
                self.assertEqual(outcome.status, "replied")
                self.assertGreater(len(provider.calls), 0)
                self.assertLessEqual(len(provider.calls), 8)
                current = self.load_current(state, path)
                self.assertEqual(current.result_digest, original.result_digest)
                self.assertEqual(
                    current.human_review.findings, original.human_review.findings
                )
                self.assertEqual(
                    replace(current.facts, has_human_adjudication_findings=True),
                    original.facts,
                )
                pending = len(current.human_review.pending)
                self.assertLess(pending, remaining)
                self.assertEqual(len(current.human_review.resolved), count - pending)
                if revision == 1:
                    self.assertGreater(
                        pending,
                        0,
                        "first bounded source must leave a durable partial result",
                    )
                if pending or gate:
                    self.assertEqual(outcome.approval_status, "approval_withheld")
                    self.assertNotIn("APPROVE", state.events())
                else:
                    self.assertEqual(outcome.approval_status, "approved")
                # Reconstruct all application/store objects for exact-source
                # replay. It cannot infer again or append another authority.
                reviews_before = len(state.reviews)
                replay_provider = AssessingProvider()
                replay = reply_human(state, LocalSessionLedger(path), replay_provider)
                self.assertEqual(replay.status, "already_replied")
                self.assertEqual(replay_provider.calls, [])
                self.assertEqual(len(state.reviews), reviews_before)
                replayed = self.load_current(state, path)
                self.assertEqual(
                    replayed.human_review.resolved, current.human_review.resolved
                )
                remaining = pending
            self.assert_visible_inventory(state, result, current, path)
            self.assertEqual(len(current.human_review.resolved), count)
            self.assertFalse(current.facts.has_human_adjudication_findings)
            self.assertEqual(state.events().count("APPROVE"), 0 if gate else 1)
            if gate == "partial":
                self.assertEqual(current.facts.coverage_blocker, "partial")
                self.assertEqual(current.facts.review_status, "partial")
            elif gate == "blocker":
                self.assertTrue(current.facts.has_blocking_findings)
            elif gate == "qualification":
                self.assertEqual(current.facts.qualification, "unverified")
            else:
                self.assertEqual(current.facts.qualification, "not-required")
                self.assertEqual(state.check_runs[-1]["conclusion"], "success")

    def test_control_distinct_authorized_mentions_complete_real_default_40_finding_journey(
        self,
    ):
        self.assert_lifecycle(count=40, detail=1)

    def test_control_all_resolved_default_authority_preserves_other_real_gates(self):
        for gate in ("partial", "blocker", "qualification"):
            with self.subTest(gate=gate):
                self.assert_lifecycle(count=40, detail=1, gate=gate)

    def test_77_100_250_original_rich_inventories_complete_across_fresh_default_applications(
        self,
    ):
        for count, detail in ((77, 3), (100, 2), (250, 1)):
            with self.subTest(count=count, detail=detail):
                self.assert_lifecycle(count=count, detail=detail)

    def test_resolving_every_rich_human_finding_preserves_other_real_approval_blockers(
        self,
    ):
        for gate in ("partial", "blocker", "qualification"):
            with self.subTest(gate=gate):
                self.assert_lifecycle(count=77, detail=3, gate=gate)

    def test_raw_retained_result_recovers_original_admission_before_publishing_all_human_findings(
        self,
    ):
        if sys.platform == "win32":
            self.assert_windows_rich_publication_hold()
            return
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger"
            result, key = human_checkpoint(
                LocalSessionLedger(path, enable_partition_writes=True)
            )
            state = DefaultHumanState(result.comments)
            self.crash_before_root(state, path, result, key)
            parts_before = tuple(item["body"] for item in human_inventory_parts(state))
            budget = EvidenceReadBudget()
            fresh = LocalSessionLedger(path)
            raw = recover(application(state, fresh), fresh, budget=budget)
            self.assertEqual(raw.content_digest(), result.content_digest())
            self.assertEqual(
                tuple(c.body for c in raw.comments),
                tuple(c.body for c in result.comments),
            )
            # Wire ReviewResult intentionally drops runtime admission flags.
            # Publication must restore them from the actual retained envelope.
            self.assertTrue(all(not c.needs_human for c in raw.comments))
            outcome = publish_human(state, fresh, raw, key, publication_budget=budget)
            self.assertEqual(outcome.status, "published")
            eligibility = self.load_current(state, path)
            self.assertEqual(len(eligibility.human_review.pending), 77)
            self.assertTrue(eligibility.facts.has_human_adjudication_findings)
            self.assertEqual(
                eligibility.human_review.inventory_document(),
                PendingHumanReview.from_result(result, BASE).inventory_document(),
            )
            self.assertEqual(
                tuple(item["body"] for item in human_inventory_parts(state)),
                parts_before,
            )
            self.assert_visible_inventory(state, result, eligibility, path)
            self.assertEqual(state.check_runs[-1]["conclusion"], "action_required")
            self.assertNotIn("APPROVE", state.events())
            self.assertLessEqual(budget.calls, 64)

    def test_raw_recovery_without_original_inventory_intent_cannot_infer_clean_obligations_or_write_gates(
        self,
    ):
        if sys.platform == "win32":
            self.assert_windows_rich_publication_hold()
            return
        for complete_parts in (False, True):
            with (
                self.subTest(complete_parts=complete_parts),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "ledger"
                result, key = human_checkpoint(
                    LocalSessionLedger(path, enable_partition_writes=True)
                )
                state = DefaultHumanState(result.comments)
                self.crash_before_root(
                    state, path, result, key, before_intent=not complete_parts
                )
                if complete_parts:
                    self.assertTrue(human_inventory_parts(state))
                    state.parts[:] = [
                        item
                        for item in state.parts
                        if DEFAULT_INVENTORY_INTENT_MARKER not in item["body"]
                    ]
                self.assertFalse(
                    any(
                        DEFAULT_INVENTORY_INTENT_MARKER in item["body"]
                        for item in state.parts
                    )
                )
                budget = EvidenceReadBudget()
                fresh = LocalSessionLedger(path)
                raw = recover(application(state, fresh), fresh, budget=budget)
                self.assertTrue(all(not c.needs_human for c in raw.comments))
                before = len(state.calls)
                with self.assertRaisesRegex(
                    (GitHubPublicationError, ReviewInputError),
                    "admission.*missing|intent|original staging",
                ):
                    publish_human(state, fresh, raw, key, publication_budget=budget)
                self.assertEqual(
                    [
                        (method, target)
                        for method, target, _ in state.calls[before:]
                        if method not in {"GET", "HEAD"}
                    ],
                    [],
                )
                self.assertEqual(state.reviews, [])
                self.assertNotIn("APPROVE", state.events())

    def test_changed_missing_or_foreign_authority_and_prose_refuse_before_inference_or_mutation(
        self,
    ):
        if sys.platform == "win32":
            self.assert_windows_rich_publication_hold()
            return
        for fault in (
            "missing-part",
            "changed-part",
            "wrong-part-author",
            "missing-prose",
            "changed-prose",
            "wrong-root-author",
            "stale-head",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ledger"
                result, key = human_checkpoint(
                    LocalSessionLedger(path, enable_partition_writes=True)
                )
                state = DefaultHumanState(result.comments)
                publish_human(state, LocalSessionLedger(path), result, key)
                if fault.endswith("part") or fault == "wrong-part-author":
                    parts = human_inventory_parts(state)
                    if fault == "missing-part":
                        state.parts.remove(parts[0])
                    elif fault == "changed-part":
                        original_body = parts[0]["body"]
                        parts[0]["body"] = original_body.replace(
                            '"data":"', '"data":"eA', 1
                        )
                        self.assertNotEqual(parts[0]["body"], original_body)
                    else:
                        parts[0]["user"] = {**state.app_user, "id": 999}
                elif fault == "missing-prose":
                    state.inline.pop(0)
                elif fault == "changed-prose":
                    original_body = state.inline[0]["body"]
                    state.inline[0]["body"] = original_body.replace(
                        result.comments[0].body, "Changed visible explanation.", 1
                    )
                    self.assertNotEqual(state.inline[0]["body"], original_body)
                elif fault == "wrong-root-author":
                    state.reviews[-1]["user"] = {**state.app_user, "id": 999}
                else:
                    state.head = "c" * 40
                next_mention(state, 1)
                before = len(state.calls)
                provider = AssessingProvider()
                try:
                    reply_human(state, LocalSessionLedger(path), provider)
                except (GitHubPublicationError, ReviewInputError):
                    pass
                self.assertEqual(provider.calls, [])
                self.assertEqual(
                    [
                        (method, target)
                        for method, target, body in state.calls[before:]
                        if method not in {"GET", "HEAD"}
                        and not (
                            target == "/graphql"
                            and isinstance(body, dict)
                            and "mutation" not in body.get("query", "")
                        )
                    ],
                    [],
                )
                self.assertNotIn("APPROVE", state.events())

    def test_windows_without_local_content_witness_holds_rich_profiles_within_original_limits(
        self,
    ):
        # This simulates only the product's cache-eligibility platform predicate;
        # native Windows CI additionally exercises its actual filesystem rules.
        with patch("sys.platform", "win32"):
            for count, detail in ((77, 3), (100, 2), (250, 1)):
                with self.subTest(count=count, detail=detail):
                    self.assert_windows_rich_publication_hold(
                        count=count, detail=detail
                    )


if __name__ == "__main__":
    unittest.main()
