"""Actual CLI admission/recovery of raw rich findings with synthetic APIs.

Installed fixture closure adds default_human_inventory_contract,
test_default_publication_cli_journey and their declared dependencies. Only
external GitHub HTTP/broker are replaced; publication cannot construct inference.
The positive rich journey is POSIX qualification; Windows exercises bounded hold.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from review_sensei.baseline import (
    ReviewBaseline,
    admission_context_document,
    baseline_finding_from_comment,
)
from review_sensei.context import ReviewContextCacheKey
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    checkpoint_review_analysis,
    prepare_review_transaction,
)
from tests import test_default_publication_cli_journey as publication_cli
from tests.default_human_inventory_contract import (
    APP,
    BASE,
    CONFIGURATION,
    EVIDENCE,
    HEAD,
    IDENTITY,
    POLICY,
    DefaultHumanState,
    application,
    human_inventory_parts,
    next_mention,
    reply_human,
)
from tests.test_evidence_capacity import human_result, varied_comments
from tests.test_review_work import AssessingProvider


def checkpoint_raw_proposals(root):
    """Bind exact provider-wire proposals, before any host admission flags exist."""
    raw = replace(
        human_result(varied_comments(77, detail=3, unique_paths=32)),
        model="fixture-model",
    )
    raw = replace(
        raw,
        comments=tuple(
            replace(comment, blocking=True, needs_human=False, effective_blocking=None)
            for comment in raw.comments
        ),
    )
    ledger = LocalSessionLedger(root / "ledger", enable_partition_writes=True)
    prepared = prepare_review_transaction(
        ledger,
        IDENTITY,
        POLICY,
        reservation_id="d" * 64,
        base_sha=BASE,
        head_sha=HEAD,
        configuration_digest=ReviewTransaction.compute_configuration_digest(
            CONFIGURATION
        ),
        evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
    )
    key = ReviewContextCacheKey(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        base_sha=BASE,
        head_sha=HEAD,
        engine=raw.provider,
        model=raw.model,
        profile="default",
        stage_digest="1" * 64,
        context_digest="2" * 64,
        learning_digest="3" * 64,
    )
    baseline = ReviewBaseline(
        cache_key=key,
        policy_digest=POLICY.digest(),
        complete=True,
        coverage_complete=True,
        generation=prepared.record.generation + 1,
        findings=tuple(
            sorted(
                (baseline_finding_from_comment(comment) for comment in raw.comments),
                key=lambda finding: finding.fingerprint,
            )
        ),
        reviewed_paths=tuple(sorted({comment.path for comment in raw.comments})),
    )
    result = checkpoint_review_analysis(
        ledger, IDENTITY, prepared, raw, baseline=baseline
    )
    # These flags affect only synthetic changed-file locations. They are never
    # passed as result, admission evidence, root context or authority to product.
    state = DefaultHumanState(tuple(replace(c, needs_human=True) for c in raw.comments))
    for name, document in {
        "result.json": result.to_dict(),
        "configuration.json": CONFIGURATION,
        "evidence.json": EVIDENCE,
        "admission.json": admission_context_document(None, key),
    }.items():
        (root / name).write_text(json.dumps(document), encoding="utf-8")
    (root / "review.patch").write_text(state.diff, encoding="utf-8", newline="\n")
    publication_cli.DefaultPublicationCliJourneyTests().configuration(root, "complete")
    return result, state


class DefaultHumanInventoryCliTests(unittest.TestCase):
    def invoke(self, root, state, *, recover=False):
        return publication_cli.DefaultPublicationCliJourneyTests().invoke(
            root, state, recover=recover
        )

    def assert_pending(self, root, state, raw, *, count=77):
        reader = application(
            state, LocalSessionLedger(root / "ledger")
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
        self.assertEqual(len(eligibility.human_review.pending), count)
        self.assertEqual(
            {finding.fingerprint for finding in eligibility.human_review.findings},
            {finding_instance_fingerprint(comment) for comment in raw.comments},
        )
        originals = {
            finding_instance_fingerprint(comment): comment for comment in raw.comments
        }
        for finding in eligibility.human_review.findings:
            comment = finding.comment
            original = originals[finding.fingerprint]
            self.assertIsNotNone(comment)
            self.assertEqual(
                (
                    comment.path,
                    comment.line,
                    comment.side,
                    comment.body,
                    comment.symbol,
                    comment.defect_kind,
                    comment.evidence_id,
                ),
                (
                    original.path,
                    original.line,
                    original.side,
                    original.body,
                    original.symbol,
                    original.defect_kind,
                    original.evidence_id,
                ),
            )
            self.assertTrue(comment.needs_human)
            self.assertFalse(comment.blocks_approval)
        self.assertGreater(
            len(
                json.dumps(eligibility.human_review.inventory_document()).encode(
                    "utf-8"
                )
            ),
            24 * 1024,
        )
        self.assertTrue(eligibility.facts.has_human_adjudication_findings)
        self.assertEqual(eligibility.facts.qualification, "not-required")
        # The reused public CLI helper deliberately requests --no-auto-approve.
        self.assertFalse(eligibility.facts.enabled)
        index = reader.finding_index_from_reviews(
            state.reviews,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=eligibility,
        )
        self.assertEqual(len(index.entries), 77)
        self.assertTrue(all(item.needs_human for item in index.entries))
        self.assertTrue(all(not item.blocking for item in index.entries))
        visible = "\n".join(item["body"] for item in [*state.reviews, *state.inline])
        for comment in raw.comments:
            self.assertIn(comment.body, visible)
        self.assertNotIn("omitted for size", visible)
        self.assertEqual(state.check_runs[-1]["conclusion"], "action_required")
        self.assertNotIn("APPROVE", state.events())
        self.assertTrue(human_inventory_parts(state))
        return eligibility

    def assert_windows_hold(self, root, state):
        code, output, error = self.invoke(root, state)
        self.assertNotEqual(code, 0, (output, error))
        self.assertIn("budget", error)
        self.assertNotIn("APPROVE", state.events())

    def test_raw_cli_proposals_become_complete_human_authority_then_fresh_api_reply_is_partial(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, state = checkpoint_raw_proposals(root)
            document = json.loads((root / "result.json").read_text(encoding="utf-8"))
            self.assertTrue(
                all(comment["blocking"] for comment in document["comments"])
            )
            self.assertTrue(
                all(
                    "needs_human" not in comment and "effective_blocking" not in comment
                    for comment in document["comments"]
                )
            )
            self.assertTrue(all(not comment.needs_human for comment in raw.comments))
            if sys.platform == "win32":
                self.assert_windows_hold(root, state)
                return
            code, output, error = self.invoke(root, state)
            self.assertEqual(code, 0, (output, error))
            self.assertIn("published", output)
            before = self.assert_pending(root, state, raw)
            next_mention(state, 1)
            provider = AssessingProvider()
            outcome = reply_human(state, LocalSessionLedger(root / "ledger"), provider)
            self.assertEqual(outcome.status, "replied")
            self.assertGreater(len(provider.calls), 0)
            self.assertLessEqual(len(provider.calls), 8)
            after = self.assert_pending(root, state, raw, count=45)
            self.assertEqual(after.result_digest, before.result_digest)
            self.assertEqual(len(after.human_review.resolved), 32)
            replay_provider = AssessingProvider()
            replay = reply_human(
                state, LocalSessionLedger(root / "ledger"), replay_provider
            )
            self.assertEqual(replay.status, "already_replied")
            self.assertEqual(replay_provider.calls, [])

    def test_fresh_cli_recovers_raw_retained_proposals_without_result_file_or_inference(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, state = checkpoint_raw_proposals(root)
            if sys.platform == "win32":
                self.assert_windows_hold(root, state)
                return

            def interrupt(_state, method, path, body):
                if (
                    method == "POST"
                    and path.endswith("/pulls/1/reviews")
                    and "<!-- reviewsensei:eligibility:v1" in body.get("body", "")
                ):
                    self.assertTrue(human_inventory_parts(state))
                    raise KeyboardInterrupt("synthetic CLI process interruption")

            state.hook = interrupt
            try:
                code, output, error = self.invoke(root, state)
                self.assertEqual(code, 130, (output, error))
                self.assertIn("cancelled", error)
            finally:
                state.hook = None
            self.assertEqual(state.reviews, [])
            parts_before = tuple(item["body"] for item in human_inventory_parts(state))
            (root / "result.json").unlink()
            code, output, error = self.invoke(root, state, recover=True)
            self.assertEqual(code, 0, (output, error))
            self.assertIn("published", output)
            self.assert_pending(root, state, raw)
            self.assertEqual(
                tuple(item["body"] for item in human_inventory_parts(state)),
                parts_before,
            )

    def test_cli_refuses_changed_trusted_current_key_before_any_remote_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _raw, state = checkpoint_raw_proposals(root)
            path = root / "admission.json"
            context = json.loads(path.read_text(encoding="utf-8"))
            context["current_key"]["head_sha"] = "c" * 40
            path.write_text(json.dumps(context), encoding="utf-8")
            code, output, error = self.invoke(root, state)
            self.assertNotEqual(code, 0, (output, error))
            self.assertIn(
                "current cache key does not match publication identity", error
            )
            self.assertEqual(state.calls, [])


if __name__ == "__main__":
    unittest.main()
