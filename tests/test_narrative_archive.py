"""Archive-backed narrative fitting preserves finding and accounting authority."""

import base64
import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from review_sensei_github import (
    GitHubPublicationError,
    GitHubWriteOptions,
    ReviewPublisher,
)
from review_sensei_github.operation_host import (
    RESULT_COMMENT_MARKER,
    comment_payload,
    render_marked_comment,
)
from review_sensei_github.publication import (
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    approval_eligibility_from_body,
    parse_finding_index,
)
from review_sensei_github.publication_recovery import (
    confirm_retained_narrative,
    retain_review_result,
)
from test_bounded_v1_publication import comments
from test_human_assessment import APP, BASE, HEAD
from test_publication_recovery import (
    CONFIGURATION,
    EVIDENCE,
    IDENTITY,
    POLICY,
    RetentionState,
    application,
    publish,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    checkpoint_review_analysis,
    prepare_review_transaction,
)


def bound_result(ledger, *, detail=8500, narrative="a" * 24000):
    result = ReviewResult(
        summary=narrative,
        comments=comments(5, body_placed=True, detail=detail),
        provider="fixture",
        review_status="partial",
        coverage=CoverageManifest(
            enumerated_paths=("src/app.py",),
            files=(
                FileCoverage(
                    "src/app.py", "partially-reviewed", "cross-file-relationship"
                ),
            ),
            enumeration_complete=True,
        ),
    )
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
    return checkpoint_review_analysis(ledger, IDENTITY, prepared, result)


def confirm(state, ledger, result, *, budget=None):
    return confirm_retained_narrative(
        http=state.http,
        token="fixture",
        result=result,
        identity=IDENTITY,
        base_sha=BASE,
        head_sha=HEAD,
        app_slug=APP,
        ledger=ledger,
        configuration_context=CONFIGURATION,
        evidence_context=EVIDENCE,
        policy=POLICY,
        budget=budget or EvidenceReadBudget(),
    )


def retain(state, ledger, result):
    retain_review_result(
        http=state.http,
        token="fixture",
        result=result,
        identity=IDENTITY,
        base_sha=BASE,
        head_sha=HEAD,
        app_slug=APP,
        ledger=ledger,
        configuration_context=CONFIGURATION,
        evidence_context=EVIDENCE,
        policy=POLICY,
    )


class NarrativeArchiveTests(unittest.TestCase):
    def test_distinct_changed_preparation_refuses_before_native_publication(self):
        distinct_preparations = []

        class ChangedPreparation(ReviewPublisher):
            def prepare(self, **arguments):
                prepared = super().prepare(**arguments)
                changed = replace(
                    prepared.result,
                    summary="prepared-only " + prepared.result.summary,
                    transaction=None,
                )
                distinct_preparations.append(changed is not arguments["result"])
                return replace(prepared, result=changed)

        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger)
            original = result.to_dict()
            state = RetentionState()
            state.reviews = []
            app = application(state, ledger)
            app.reviewer = ChangedPreparation(http=state.http)
            with self.assertRaisesRegex(
                GitHubPublicationError, "prepared review does not match result"
            ):
                publish(
                    app,
                    result,
                    None,
                    options=GitHubWriteOptions(
                        auto_review=True, github_writes=True, complete_inventory=True
                    ),
                )
            self.assertTrue(distinct_preparations)
            self.assertTrue(all(distinct_preparations))
            self.assertFalse(state.reviews)
            self.assertEqual(result.to_dict(), original)
            self.assertEqual(
                confirm(state, ledger, result).content_digest, result.content_digest()
            )

    def test_excerpt_includes_carrier_overhead_and_strictly_shrinks_or_refuses(self):
        for summary_bytes, count in ((1030, 1), (1030, 32), (8000, 32)):
            with self.subTest(summary_bytes=summary_bytes, count=count):
                with tempfile.TemporaryDirectory() as directory:
                    ledger = LocalSessionLedger(Path(directory))
                    result = bound_result(
                        ledger, detail=100, narrative="a" * summary_bytes
                    )
                    state = RetentionState()
                    retain(state, ledger, result)
                    documents = [
                        json.loads(comment_payload(p["body"])) for p in state.parts
                    ]
                    payload = b"".join(base64.b64decode(p["data"]) for p in documents)
                    template = state.parts[0]
                    state.parts = []
                    for index in range(count):
                        document = dict(documents[0])
                        document.update(
                            count=count,
                            index=index,
                            data=base64.b64encode(
                                payload[
                                    index * len(payload) // count : (index + 1)
                                    * len(payload)
                                    // count
                                ]
                            ).decode("ascii"),
                        )
                        state.parts.append(
                            dict(
                                template,
                                id=1500 + index,
                                body=render_marked_comment(
                                    RESULT_COMMENT_MARKER, document
                                ),
                            )
                        )
                    receipt = confirm(state, ledger, result)
                    state.calls.clear()
                    before = ledger.load(IDENTITY).record.to_dict()
                    arguments = dict(
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=1,
                        base_sha=BASE,
                        head_sha=HEAD,
                    )
                    if summary_bytes == 1030 and count == 32:
                        with self.assertRaisesRegex(ReviewInputError, "cannot shrink"):
                            receipt.excerpt(result, **arguments)
                    else:
                        excerpt = receipt.excerpt(result, **arguments)
                        self.assertLess(len(excerpt.encode("utf-8")), summary_bytes)
                        self.assertEqual(excerpt.count("#issuecomment-"), count)
                        self.assertIn(receipt.content_digest, excerpt)
                        self.assertIn(receipt.payload_digest, excerpt)
                    self.assertFalse(state.calls)
                    self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)

    def test_malformed_carrier_indexes_refuse_before_ordering(self):
        for index in ("0", True, -1, None):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                ledger = LocalSessionLedger(Path(directory))
                result = bound_result(ledger, detail=100)
                state = RetentionState()
                retain(state, ledger, result)
                document = json.loads(comment_payload(state.parts[0]["body"]))
                document["index"] = index
                state.parts[0]["body"] = render_marked_comment(
                    RESULT_COMMENT_MARKER, document
                )
                state.calls.clear()
                before = ledger.load(IDENTITY).record.to_dict()
                with self.assertRaisesRegex(ValueError, "result part is invalid"):
                    confirm(state, ledger, result)
                self.assertTrue(all(method == "GET" for method, _, _ in state.calls))
                self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)

    def test_public_application_compacts_only_narrative_after_confirmed_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger)
            before = result.to_dict()
            state = RetentionState()
            state.reviews = []
            outcome = publish(
                application(state, ledger),
                result,
                None,
                options=GitHubWriteOptions(
                    auto_review=True, github_writes=True, complete_inventory=True
                ),
            )
            self.assertEqual(outcome.status, "published")
            self.assertEqual(len(state.reviews), 1)
            body = state.reviews[0]["body"]
            self.assertIn("Excerpt; the complete original provider narrative", body)
            self.assertLessEqual(len(body.encode()), MAX_PUBLISHED_REVIEW_BODY_BYTES)
            self.assertEqual(state.reviews[0]["state"], "COMMENTED")
            for finding in result.comments:
                self.assertIn(finding.body, body)
            index = parse_finding_index(
                body, repository_id=1, pull_request=1, head_sha=HEAD
            )
            self.assertEqual(len(index.entries), len(result.comments))
            self.assertIn("partially-reviewed", body)
            self.assertEqual(
                approval_eligibility_from_body(body).facts.review_status, "partial"
            )
            self.assertEqual(result.to_dict(), before)
            self.assertTrue(state.parts)
            self.assertIn("#issuecomment-", body)

    def test_confirmation_is_read_only_and_uses_original_shared_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger)
            state = RetentionState()
            retain(state, ledger, result)
            before = ledger.load(IDENTITY).record.to_dict()
            parts = copy.deepcopy(state.parts)
            state.calls.clear()
            budget = EvidenceReadBudget()
            receipt = confirm(state, ledger, result, budget=budget)
            excerpt = receipt.excerpt(
                result,
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                base_sha=BASE,
                head_sha=HEAD,
            )
            self.assertIn(receipt.content_digest, excerpt)
            self.assertIn(receipt.payload_digest, excerpt)
            self.assertTrue(all(method == "GET" for method, _, _ in state.calls))
            self.assertEqual(budget.calls, len(state.calls))
            self.assertEqual(state.parts, parts)
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)
            self.assertEqual(
                set(receipt.carrier_ids), {part["id"] for part in state.parts}
            )

    def test_missing_foreign_or_changed_parts_do_not_confirm(self):
        for fault in ("missing", "foreign", "changed"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                ledger = LocalSessionLedger(Path(directory))
                result = bound_result(ledger)
                state = RetentionState()
                retain(state, ledger, result)
                if fault == "missing":
                    state.parts.pop()
                elif fault == "foreign":
                    state.parts[0]["user"] = {
                        "id": 12345,
                        "login": "other",
                        "type": "Bot",
                    }
                else:
                    state.parts[0]["body"] = state.parts[0]["body"].replace(
                        '"data":"', '"data":"AA', 1
                    )
                before = ledger.load(IDENTITY).record.to_dict()
                state.calls.clear()
                with self.assertRaises((ValueError, ReviewInputError)):
                    confirm(state, ledger, result)
                self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)
                self.assertTrue(all(method == "GET" for method, _, _ in state.calls))

    def test_foreign_scope_and_forged_confirmation_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger)
            state = RetentionState()
            retain(state, ledger, result)
            receipt = confirm(state, ledger, result)
            for candidate in (
                replace(receipt, _confirmation=object()),
                replace(receipt, head_sha="e" * 40),
                replace(receipt, carrier_ids=(123456789,)),
            ):
                with self.assertRaises(ReviewInputError):
                    candidate.excerpt(
                        result,
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=1,
                        base_sha=BASE,
                        head_sha=HEAD,
                    )

    def test_unicode_excerpt_is_valid_and_complete_original_remains_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger, narrative="a" + "🙂" * 2000)
            state = RetentionState()
            retain(state, ledger, result)
            receipt = confirm(state, ledger, result)
            excerpt = receipt.excerpt(
                result,
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                base_sha=BASE,
                head_sha=HEAD,
            )
            self.assertEqual(excerpt.encode().decode(), excerpt)
            self.assertIn("a" + "🙂" * 255, excerpt)
            self.assertNotIn("�", excerpt)
            self.assertEqual(result.summary, "a" + "🙂" * 2000)

    def test_real_review_run_counts_confirmation_once_without_reset(self):
        from test_api_work_lifecycle import caller_measurement
        from test_review_run import EntryFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory, limit=512)
            before_reviews = len(fixture.reviews)
            fixture.provider_summary = "Narrative. " * 2400
            fixture.provider_comments = [
                {
                    **comment.to_dict(),
                    "needs_human": True,
                    "severity": "high",
                    "blocking": False,
                }
                for comment in comments(5, body_placed=True, detail=8500)
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            new_reviews = fixture.reviews[before_reviews:]
            self.assertEqual(len(new_reviews), 1)
            self.assertEqual(new_reviews[0]["state"], "COMMENTED")
            body = new_reviews[0]["body"]
            self.assertIn("Excerpt; the complete original provider narrative", body)
            self.assertLessEqual(len(body.encode()), MAX_PUBLISHED_REVIEW_BODY_BYTES)
            state = fixture.accounted.accounting
            self.assertEqual(state.configuration.limit, 512)
            self.assertIsNotNone(state.terminal_transaction)
            self.assertEqual(
                fixture.accounted._terminal_reason, "publication-completion"
            )
            self.assertEqual(
                len(fixture.trace),
                sum(len(transport.observations) for transport in fixture.transports),
            )
            measured = caller_measurement(fixture)
            self.assertEqual(measured["configured_limit"], 512)

    def test_changed_ledger_refuses_without_overwriting_the_concurrent_state(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger)
            state = RetentionState()
            retain(state, ledger, result)
            concurrent = []

            def race(_state, method, path, _body):
                if method == "GET" and path.endswith("/pulls/1"):
                    state.hook = None
                    record = ledger.replace(
                        IDENTITY,
                        lambda current: current.evolve(
                            generation=current.generation + 1, operator_paused=True
                        ),
                    )
                    concurrent.append(record.to_dict())

            state.hook = race
            state.calls.clear()
            with self.assertRaisesRegex(ReviewInputError, "ledger changed"):
                confirm(state, ledger, result)
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), concurrent[0])
            self.assertTrue(all(method == "GET" for method, _, _ in state.calls))

    def test_exhausted_original_budget_does_not_dispatch_or_refill(self):
        from review_sensei.errors import EvidenceBudgetExhausted

        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger)
            state = RetentionState()
            retain(state, ledger, result)
            before = ledger.load(IDENTITY).record.to_dict()
            budget = EvidenceReadBudget()
            while budget.ordinary_remaining():
                budget.consume()
            charged = budget.calls
            state.calls.clear()
            with self.assertRaises(EvidenceBudgetExhausted):
                confirm(state, ledger, result, budget=budget)
            self.assertEqual(budget.calls, charged)
            self.assertFalse(state.calls)
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)

    def test_lost_partial_retention_acknowledgement_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger)
            state = RetentionState()
            state.fail_part = "lost"
            with self.assertRaises(GitHubPublicationError):
                retain(state, ledger, result)
            self.assertEqual(len(state.parts), 1)
            before = copy.deepcopy(state.parts)
            root = ledger.load(IDENTITY).record.to_dict()
            state.calls.clear()
            with self.assertRaises(ValueError):
                confirm(state, ledger, result)
            self.assertEqual(state.parts, before)
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), root)
            self.assertTrue(all(method == "GET" for method, _, _ in state.calls))

    def test_remaining_finding_overflow_refuses_after_narrative_reduction(self):
        from review_sensei_github import GitHubPublicationError

        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            result = bound_result(ledger, detail=15000)
            before = result.to_dict()
            state = RetentionState()
            state.reviews = []
            confirmations = []
            prior_profiler = sys.getprofile()

            def observe(frame, event, _argument):
                if (
                    event == "call"
                    and frame.f_code is confirm_retained_narrative.__code__
                ):
                    confirmations.append(frame.f_code)

            # Observe the real continuation without substituting a mock or
            # bypassing any confirmation, transport or publication check.
            sys.setprofile(observe)
            try:
                with self.assertRaises(GitHubPublicationError):
                    publish(
                        application(state, ledger),
                        result,
                        None,
                        options=GitHubWriteOptions(
                            auto_review=True,
                            github_writes=True,
                            complete_inventory=True,
                        ),
                    )
            finally:
                sys.setprofile(prior_profiler)
            self.assertEqual(len(confirmations), 1)
            self.assertFalse(state.reviews)
            self.assertTrue(state.parts)
            self.assertEqual(result.to_dict(), before)
            self.assertEqual(
                ledger.load(IDENTITY).record.transaction.phase, "publication_failed"
            )


if __name__ == "__main__":
    unittest.main()
