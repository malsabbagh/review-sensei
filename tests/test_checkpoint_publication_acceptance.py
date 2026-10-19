"""Actual accepted analysis bytes survive independently validated checkpoints."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.commands import (
    require_accepted_result as _require_accepted_result,
)
from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_entry import (
    HostedProviderResult,
    analysis_payload,
    run_hosted_provider,
)
from review_sensei_github.operation_host import GitHubCommentOperationStore

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    complete_review_publication,
    prepare_review_transaction,
)
from tests.fake_github_http import json_response, make_http
from tests.test_operation_entry import MemoryComments
from tests.test_review_transaction import CONFIGURATION

APP = "reviewsensei[bot]"
BASE = "a" * 40
HEAD = "b" * 40
IDENTITY = SessionIdentity("synthetic/repo", 11, repository_id=7)
POLICY = ReviewConvergencePolicy(mode="merge-focused")
EVIDENCE = {"evidence_policy": "legacy", "snapshot_sha256": None}


class CheckpointPublicationAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ledger = LocalSessionLedger(self.root / "owned")
        self.original = ReviewResult(
            summary="Complete synthetic review 深🙂.",
            comments=(
                ReviewComment(
                    path="src/worker.py",
                    line=1,
                    body="Validate this specific generation before acknowledging 深🙂.",
                    blocking=False,
                    severity="low",
                ),
            ),
            provider="fixture",
            model="fixture",
            review_status="complete",
        )
        self.comments = self.accept(self.original)
        self.bound = self.checkpoint(self.ledger, "reservation-owned")
        self.context = {
            "ledger": self.ledger,
            "identity": IDENTITY,
            "base_sha": BASE,
            "head_sha": HEAD,
            "policy_digest": POLICY.digest(),
            "configuration_digest": ReviewTransaction.compute_configuration_digest(
                CONFIGURATION
            ),
            "evidence_digest": ReviewTransaction.compute_evidence_digest(EVIDENCE),
        }

    def checkpoint(self, ledger, reservation):
        prepared = prepare_review_transaction(
            ledger,
            IDENTITY,
            POLICY,
            reservation_id=hashlib.sha256(reservation.encode()).hexdigest(),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=ReviewTransaction.compute_configuration_digest(
                CONFIGURATION
            ),
            evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
        )
        return checkpoint_review_analysis(ledger, IDENTITY, prepared, self.original)

    def accept(self, result):
        comments = MemoryComments()
        calls = []

        def execute():
            calls.append("analysis")
            return SimpleNamespace(result=result)

        accepted = run_hosted_provider(
            GitHubCommentOperationStore(comments, app_user_id=42),
            trigger="full-review",
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            attestation_digest=hashlib.sha256(b"synthetic attestation").hexdigest(),
            run_id="100",
            run_attempt="1",
            app_id=99,
            reservation_id="accepted-analysis-reservation",
            delivery_id="synthetic-analysis-delivery",
            grant="synthetic-one-use-grant",
            server_started_ms=1000,
            execute=execute,
            encode=analysis_payload,
            parts=ResultPartStore(comments, app_user_id=42),
            now=lambda: 1000,
        )
        self.assertIsInstance(accepted, HostedProviderResult)
        self.assertEqual(calls, ["analysis"])
        return comments

    def guard(self, result, *, checkpoint=None, comments=None, actor=APP):
        port = comments if comments is not None else self.comments
        receipt = [
            {
                "id": comment.comment_id,
                "body": comment.body,
                "user": {"id": comment.user_id, "login": actor, "type": "Bot"},
            }
            for comment in port.comments.values()
        ]
        http, requests = make_http(json_response(receipt))
        before = self.ledger.load(IDENTITY).record.to_dict()
        port_requests = port.requests
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_RUN_ID": "100", "GITHUB_TOKEN": "synthetic-metadata-token"},
                clear=True,
            ),
            patch("socket.socket.connect", side_effect=AssertionError("live network")),
        ):
            try:
                _require_accepted_result(
                    result,
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    app_slug=APP,
                    operation_entry="enabled",
                    publication_http=http,
                    checkpoint=checkpoint,
                )
            finally:
                self.assertEqual(self.ledger.load(IDENTITY).record.to_dict(), before)
                self.assertEqual(port.requests, port_requests)
                self.assertTrue(requests)
                self.assertTrue(all(method == "GET" for method, _, _ in requests))

    def test_exact_original_hash_preserves_the_existing_acceptance_path(self):
        self.guard(self.original)

    def test_precheckpoint_receipt_accepts_real_checkpoint_without_changing_authority(
        self,
    ):
        self.assertIsNone(self.original.transaction)
        self.assertIsNotNone(self.bound.transaction)
        self.assertNotEqual(
            analysis_payload(SimpleNamespace(result=self.original)),
            analysis_payload(SimpleNamespace(result=self.bound)),
        )
        self.guard(self.bound, checkpoint=self.context)
        self.assertEqual(self.ledger.load(IDENTITY).record.completed_initial_reviews, 1)

    def test_failed_phase_and_older_envelope_reuse_the_same_real_acceptance(self):
        record = complete_review_publication(
            self.ledger, IDENTITY, self.bound.transaction, published=False
        )
        current = replace(self.bound, transaction=record.transaction)
        self.assertEqual(current.transaction.phase, "publication_failed")
        self.assertEqual(current.content_digest(), self.bound.content_digest())
        self.guard(current, checkpoint=self.context)
        self.guard(self.bound, checkpoint=self.context)
        self.assertEqual(self.ledger.load(IDENTITY).record.completed_initial_reviews, 1)
        self.assertEqual(
            self.ledger.load(IDENTITY).record.completed_verification_rounds, 0
        )

    def test_checkpoint_envelope_requires_independent_validation_context(self):
        with self.assertRaisesRegex(ReviewInputError, "does not match"):
            self.guard(self.bound)

    def test_changed_content_cannot_borrow_the_original_transaction_or_acceptance(self):
        changed = replace(self.original, summary="Different review content.")
        altered = replace(
            changed,
            transaction=replace(
                self.bound.transaction, result_sha256=changed.content_digest()
            ),
        )
        with self.assertRaisesRegex(ReviewInputError, "durable result digest"):
            self.guard(altered, checkpoint=self.context)

    def test_mismatched_trusted_scope_and_context_refuse(self):
        for field, value in (
            ("base_sha", "c" * 40),
            ("head_sha", "c" * 40),
            ("policy_digest", "c" * 64),
            ("configuration_digest", "c" * 64),
            ("evidence_digest", "c" * 64),
            ("identity", SessionIdentity("synthetic/other", 11, repository_id=8)),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ReviewInputError, "identity or trusted context"),
            ):
                self.guard(self.bound, checkpoint={**self.context, field: value})

    def test_missing_and_foreign_durable_ledgers_cannot_supply_normalization(self):
        missing = LocalSessionLedger(self.root / "missing")
        foreign = LocalSessionLedger(self.root / "foreign")
        foreign_result = self.checkpoint(foreign, "reservation-foreign")
        self.assertNotEqual(
            foreign_result.transaction.transaction_id,
            self.bound.transaction.transaction_id,
        )
        for ledger in (missing, foreign):
            with self.subTest(ledger=ledger), self.assertRaises(ReviewInputError):
                self.guard(self.bound, checkpoint={**self.context, "ledger": ledger})

    def test_valid_checkpoint_does_not_replace_missing_acceptance_of_exact_bytes(self):
        different = self.accept(
            replace(self.original, summary="Other accepted analysis.")
        )
        with self.assertRaisesRegex(ReviewInputError, "does not match"):
            self.guard(self.bound, checkpoint=self.context, comments=different)

    def test_foreign_author_cannot_supply_the_precheckpoint_receipt(self):
        # Keep a real owned index present so absence cannot invoke legacy fallback.
        different = self.accept(
            replace(self.original, summary="Other accepted analysis.")
        )
        # The original digest carried by another author is not owned acceptance.
        # A genuine owned different-result index still forces exact refusal.
        receipt = [
            *[
                {"body": item.body, "user": {"login": "mallory"}}
                for item in self.comments.comments.values()
            ],
            *[
                {"body": item.body, "user": {"login": APP}}
                for item in different.comments.values()
            ],
        ]
        http, requests = make_http(json_response(receipt))
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_RUN_ID": "100", "GITHUB_TOKEN": "synthetic-metadata-token"},
                clear=True,
            ),
            self.assertRaisesRegex(ReviewInputError, "does not match"),
        ):
            _require_accepted_result(
                self.bound,
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                app_slug=APP,
                operation_entry="enabled",
                publication_http=http,
                checkpoint=self.context,
            )
        self.assertTrue(all(method == "GET" for method, _, _ in requests))


if __name__ == "__main__":
    unittest.main()
