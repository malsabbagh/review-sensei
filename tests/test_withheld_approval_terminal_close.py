"""Terminal close accounting for confirmed withheld-approval outcomes (synthetic fixtures only)."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.action_completion import (
    CompletionScope,
    CompletionState,
    FinalizationCompletion,
    FinalizationSubject,
    _confirmed_approval,
    _confirmed_finalization,
    finalization_confirmed,
)
from review_sensei_github.publication import (
    PublicationResult,
    ReviewApprovalFinalizer,
    ReviewPublisher,
)
from review_sensei_github.review_run import (
    _CONFIRMED_WITHHELD_CLOSE_DIAGNOSTICS,
    ReviewRunHooks,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import EntryFixture

ADDED_WITHHELD_DIAGNOSTICS = (
    "qualification_unverified",
    "app_authored",
    "baseline_capacity_exceeded",
    "review_threads_incomplete",
    "historical_assessment_unverified",
    "quarantine_assessment_unverified",
)
UNCONFIRMED_ERROR_DIAGNOSTICS = (
    "finding_index_missing",
    "gate_reevaluation_failed",
    "publication_ambiguous",
    "publication_outcome_incomplete",
)


def _historical_view() -> dict[str, object]:
    return {
        "scope_sha256": "1" * 64,
        "state_sha256": "2" * 64,
        "pending": ["3" * 64],
        "fresh_human_pending": False,
        "review_id": 100,
        "historical_head_sha": "4" * 40,
    }


def _quarantine_view() -> dict[str, object]:
    return {
        "scope_sha256": "1" * 64,
        "state_sha256": "2" * 64,
        "origins": [
            {
                "scope_sha256": "3" * 64,
                "kind": "retained-result",
                "inventory_sha256": "4" * 64,
                "item_count": 1,
            }
        ],
        "pending": [
            {
                "origin_scope_sha256": "3" * 64,
                "instance": "5" * 64,
            }
        ],
        "fresh_human_pending": False,
    }


def _with_finalization_witness(
    publication: PublicationResult,
    *,
    scope: CompletionScope = CompletionScope.FINALIZATION,
) -> PublicationResult:
    """Attach a process-issued witness at the requested completion scope."""

    subject = FinalizationSubject(
        IDENTITY.repository, IDENTITY.pull_request, HEAD, "a" * 64
    )
    approval = PublicationResult(
        status="approval_withheld",
        diagnostic=publication.diagnostic,
    )
    approval = replace(
        approval,
        finalization_completion=_confirmed_approval(approval, subject),
    )
    stripped = replace(publication, finalization_completion=None)
    if scope is CompletionScope.FINALIZATION:
        return replace(
            stripped,
            finalization_completion=_confirmed_finalization(approval, stripped),
        )
    assert approval.finalization_completion is not None
    return replace(
        stripped,
        finalization_completion=FinalizationCompletion(
            subject,
            scope,
            approval.finalization_completion.outcome_sha256,
            CompletionState.CONFIRMED,
        ),
    )


@contextmanager
def _withholding_condition(diagnostic: str):
    """Trigger one real withheld-approval diagnostic via source finalization."""

    with ExitStack() as stack:
        if diagnostic == "baseline_capacity_exceeded":
            from review_sensei import baseline as baseline_module
            from review_sensei import cli as core_cli

            original_doc = core_cli.baseline_history_document

            def capped_history_document(
                baseline_value,
                *,
                max_bytes=baseline_module.MAX_HISTORY_BASELINE_BYTES,
                require_complete=False,
                partition_writer=None,
            ):
                if require_complete:
                    max_bytes = 128
                return original_doc(
                    baseline_value,
                    max_bytes=max_bytes,
                    require_complete=require_complete,
                    partition_writer=partition_writer,
                )

            stack.enter_context(
                patch.object(
                    core_cli,
                    "baseline_history_document",
                    capped_history_document,
                )
            )
            yield
            return

        if diagnostic == "review_threads_incomplete":
            stack.enter_context(
                patch.object(
                    ReviewApprovalFinalizer,
                    "_scan_blocking_threads",
                    return_value=(None, frozenset()),
                )
            )
            yield
            return

        original_finalize = ReviewApprovalFinalizer.finalize

        def wrapped_finalize(self, *args, **kwargs):
            eligibility = kwargs["eligibility"]
            if diagnostic == "qualification_unverified":
                eligibility = replace(
                    eligibility,
                    facts=replace(eligibility.facts, qualification="unverified"),
                )
            elif diagnostic == "app_authored":
                eligibility = replace(
                    eligibility,
                    facts=replace(eligibility.facts, app_authored=True),
                )
            elif diagnostic == "historical_assessment_unverified":
                eligibility = replace(
                    eligibility, historical_assessments=_historical_view()
                )
            elif diagnostic == "quarantine_assessment_unverified":
                eligibility = replace(
                    eligibility, quarantine_assessments=_quarantine_view()
                )
            else:
                raise AssertionError(f"unsupported diagnostic condition: {diagnostic}")
            kwargs["eligibility"] = eligibility
            return original_finalize(self, *args, **kwargs)

        stack.enter_context(
            patch.object(ReviewApprovalFinalizer, "finalize", wrapped_finalize)
        )
        yield


@contextmanager
def _capture_publications():
    """Observe the exact PublicationResult objects returned to ReviewRun.execute."""

    captured: list[PublicationResult] = []
    original_publish = ReviewRunHooks.publish_review

    def recording(self, *args, **kwargs):
        result = original_publish(self, *args, **kwargs)
        captured.append(result)
        return result

    with patch.object(ReviewRunHooks, "publish_review", recording):
        yield captured


class WithheldApprovalTerminalCloseTests(unittest.TestCase):
    def _assert_pending_outcome(
        self,
        fixture: EntryFixture,
        *,
        expected_status: str = "published_terminal_pending",
        expected_publication_status: str = "published",
        expected_reason: str,
        expected_diagnostic: str | None,
    ) -> None:
        outcome = json.loads((fixture.root / "publication.json").read_text())
        self.assertEqual(outcome["status"], expected_status)
        self.assertEqual(outcome["publication_status"], expected_publication_status)
        self.assertFalse(outcome["accounting_terminal"])
        self.assertEqual(outcome["terminal_reason"], expected_reason)
        self.assertEqual(outcome["diagnostic"], expected_diagnostic)
        self.assertIsNone(fixture.accounted.accounting.terminal_transaction)
        record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
        self.assertIsNone(record.api_run_accounting["terminal_transaction"])

    def test_confirmed_withheld_diagnostics_close_accounting_and_allow_rollover_approval(
        self,
    ):
        for diagnostic in ADDED_WITHHELD_DIAGNOSTICS:
            self.assertIn(diagnostic, _CONFIRMED_WITHHELD_CLOSE_DIAGNOSTICS)
            with (
                self.subTest(diagnostic=diagnostic),
                tempfile.TemporaryDirectory() as d,
            ):
                fixture = EntryFixture(d)
                with (
                    _withholding_condition(diagnostic),
                    _capture_publications() as captured,
                ):
                    code = fixture.invoke()
                expected_code = 1 if diagnostic == "baseline_capacity_exceeded" else 0
                self.assertEqual(code, expected_code, fixture.error)
                self.assertEqual(len(captured), 1)
                publication = captured[0]
                self.assertTrue(finalization_confirmed(publication))
                self.assertEqual(publication.status, "published")
                self.assertEqual(publication.diagnostic, diagnostic)
                self.assertIsNone(publication.thread_resolution)

                outcome = json.loads((fixture.root / "publication.json").read_text())
                self.assertEqual(outcome["status"], "published")
                self.assertEqual(outcome["publication_status"], "published")
                self.assertTrue(outcome["accounting_terminal"])
                self.assertIsNone(outcome["terminal_reason"])
                self.assertEqual(outcome["diagnostic"], diagnostic)

                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                tx = record.transaction
                self.assertIsNotNone(tx)
                self.assertEqual(
                    record.api_run_accounting["terminal_transaction"], tx.transaction_id
                )
                self.assertEqual(
                    fixture.accounted.accounting.terminal_transaction, tx.transaction_id
                )
                self.assertFalse(
                    any(review["state"] == "APPROVED" for review in fixture.reviews)
                )

                # Subsequent run on a new head rolls over cleanly and reaches approval.
                fixture.next_run()
                next_code = fixture.invoke()
                self.assertEqual(next_code, 0, fixture.error)
                next_outcome = json.loads(
                    (fixture.root / "publication.json").read_text()
                )
                self.assertTrue(next_outcome["accounting_terminal"])
                self.assertIsNone(next_outcome["diagnostic"])
                next_record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertIsNotNone(next_record.transaction)
                self.assertEqual(
                    fixture.accounted.accounting.terminal_transaction,
                    next_record.transaction.transaction_id,
                )
                self.assertTrue(
                    any(
                        review["state"] == "APPROVED"
                        and review["commit_id"] == fixture.live_head
                        for review in fixture.reviews
                    )
                )

    def test_already_published_and_disabled_statuses_close_when_confirmed(self):
        for status in ("already_published", "auto_approval_disabled"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                fixture = EntryFixture(d)
                confirmed = _with_finalization_witness(
                    PublicationResult(
                        status=status,
                        review_id=100,
                        diagnostic="qualification_unverified",
                    )
                )
                self.assertTrue(finalization_confirmed(confirmed))
                with patch.object(ReviewPublisher, "publish", return_value=confirmed):
                    code = fixture.invoke()
                self.assertEqual(code, 0, fixture.error)
                outcome = json.loads((fixture.root / "publication.json").read_text())
                self.assertEqual(outcome["status"], status)
                self.assertEqual(outcome["publication_status"], status)
                self.assertTrue(outcome["accounting_terminal"])
                self.assertIsNone(outcome["terminal_reason"])
                self.assertEqual(outcome["diagnostic"], "qualification_unverified")
                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertIsNotNone(record.transaction)
                self.assertEqual(
                    fixture.accounted.accounting.terminal_transaction,
                    record.transaction.transaction_id,
                )

    def test_withheld_diagnostics_without_finalization_confirmation_stay_pending(self):
        original_publish = ReviewRunHooks.publish_review

        for mode in ("none", "forged", "publication_scope_only"):
            for diagnostic in ADDED_WITHHELD_DIAGNOSTICS:
                with (
                    self.subTest(mode=mode, diagnostic=diagnostic),
                    tempfile.TemporaryDirectory() as d,
                ):
                    fixture = EntryFixture(d)
                    seen_confirmed: list[bool] = []

                    def unconfirmed(hooks, *args, **kwargs):
                        publication = original_publish(hooks, *args, **kwargs)
                        seen_confirmed.append(finalization_confirmed(publication))
                        if mode == "none":
                            return replace(publication, finalization_completion=None)
                        if mode == "forged":
                            forged = FinalizationCompletion(
                                FinalizationSubject("owner/repo", 7, HEAD, "a" * 64),
                                CompletionScope.FINALIZATION,
                                "b" * 64,
                                CompletionState.CONFIRMED,
                            )
                            return replace(publication, finalization_completion=forged)
                        return _with_finalization_witness(
                            publication, scope=CompletionScope.PUBLICATION
                        )

                    with (
                        _withholding_condition(diagnostic),
                        patch.object(ReviewRunHooks, "publish_review", unconfirmed),
                    ):
                        code = fixture.invoke()
                    self.assertEqual(seen_confirmed, [True])
                    self.assertEqual(code, 1, fixture.error)
                    self._assert_pending_outcome(
                        fixture,
                        expected_reason="publication_finalization_incomplete",
                        expected_diagnostic=diagnostic,
                    )

                    fixture.next_run()
                    self.assertEqual(fixture.invoke(), 1)
                    self.assertIn(
                        "historical executor did not prove successful review exit",
                        fixture.error,
                    )
                    self.assertFalse(
                        any(review["state"] == "APPROVED" for review in fixture.reviews)
                    )

    def test_withheld_diagnostics_with_unconfirmed_thread_resolution_stay_pending(self):
        original_publish = ReviewRunHooks.publish_review

        for keep_proof in (False, True):
            for diagnostic in ADDED_WITHHELD_DIAGNOSTICS:
                with (
                    self.subTest(keep_proof=keep_proof, diagnostic=diagnostic),
                    tempfile.TemporaryDirectory() as d,
                ):
                    fixture = EntryFixture(d)
                    seen_confirmed: list[bool] = []

                    def unconfirmed_cleanup(hooks, *args, **kwargs):
                        publication = original_publish(hooks, *args, **kwargs)
                        unconfirmed = replace(
                            publication,
                            thread_resolution="unconfirmed",
                            finalization_completion=None,
                        )
                        if keep_proof:
                            unconfirmed = _with_finalization_witness(unconfirmed)
                        seen_confirmed.append(finalization_confirmed(unconfirmed))
                        return unconfirmed

                    with (
                        _withholding_condition(diagnostic),
                        patch.object(
                            ReviewRunHooks, "publish_review", unconfirmed_cleanup
                        ),
                    ):
                        code = fixture.invoke()
                    self.assertEqual(seen_confirmed, [keep_proof])
                    self.assertEqual(code, 1, fixture.error)
                    self._assert_pending_outcome(
                        fixture,
                        expected_reason="thread_cleanup_unconfirmed",
                        expected_diagnostic=diagnostic,
                    )

    def test_error_and_unconfirmed_diagnostics_never_close_even_if_confirmed(self):
        for diagnostic in UNCONFIRMED_ERROR_DIAGNOSTICS:
            self.assertNotIn(diagnostic, _CONFIRMED_WITHHELD_CLOSE_DIAGNOSTICS)
            with (
                self.subTest(diagnostic=diagnostic),
                tempfile.TemporaryDirectory() as d,
            ):
                fixture = EntryFixture(d)

                def disallowed_diagnostic(
                    self_finalizer,
                    *,
                    repository,
                    pull_request,
                    head_sha,
                    eligibility,
                    **_,
                ):
                    result = PublicationResult(
                        status="approval_withheld", diagnostic=diagnostic
                    )
                    subject = FinalizationSubject(
                        repository, pull_request, head_sha, eligibility.result_digest
                    )
                    return replace(
                        result,
                        finalization_completion=_confirmed_approval(result, subject),
                    )

                with (
                    patch.object(
                        ReviewApprovalFinalizer, "finalize", disallowed_diagnostic
                    ),
                    _capture_publications() as captured,
                ):
                    code = fixture.invoke()
                self.assertEqual(code, 1, fixture.error)
                self.assertEqual(len(captured), 1)
                self.assertTrue(finalization_confirmed(captured[0]))
                self.assertEqual(captured[0].diagnostic, diagnostic)
                self._assert_pending_outcome(
                    fixture,
                    expected_reason="publication_finalization_incomplete",
                    expected_diagnostic=diagnostic,
                )

    def test_unpublished_status_never_closes_even_with_confirmed_withheld_diagnostic(
        self,
    ):
        with tempfile.TemporaryDirectory() as d:
            fixture = EntryFixture(d)
            unpublished = _with_finalization_witness(
                PublicationResult(
                    status="not_published", diagnostic="qualification_unverified"
                )
            )
            self.assertTrue(finalization_confirmed(unpublished))

            with patch.object(ReviewPublisher, "publish", return_value=unpublished):
                code = fixture.invoke()
            self.assertEqual(code, 1, fixture.error)
            self._assert_pending_outcome(
                fixture,
                expected_status="not_published",
                expected_publication_status="not_published",
                expected_reason="publication_finalization_incomplete",
                expected_diagnostic="qualification_unverified",
            )


if __name__ == "__main__":
    unittest.main()
