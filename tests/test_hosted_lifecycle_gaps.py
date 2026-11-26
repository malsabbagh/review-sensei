"""End-to-end regression tests for hosted lifecycle gaps (Gaps 2-6).

All repository identities, tokens, and external HTTP responses are synthetic.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import review_lifecycle_diagnostics
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.publication import ReviewPublisher, parse_finding_index
from review_sensei_github.publication_failure_recovery import (
    SETTLEMENT_WITNESS_MARKER,
)
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.disposition import session_dispositions
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import (
    ConfirmedFixEvidence,
    FileDriftDescriptor,
    evaluate_drift_round,
)
from review_sensei.models import ReviewComment, ReviewResult
from tests.test_action_run import CommandEntryFixture
from tests.test_api_work_persistence import HEAD, IDENTITY
from tests.test_publication_failure_recovery import (
    HEAD_2,
    PublicationFailureRecoveryFixture,
)


def _parse_single_lifecycle_event(stderr: str) -> dict[str, object]:
    prefix = "review-sensei: lifecycle "
    lines = [
        line[len(prefix) :] for line in stderr.splitlines() if line.startswith(prefix)
    ]
    assert len(lines) == 1, stderr
    return json.loads(lines[0])


class Gap2DiagnosticsAndRecoveryTests(unittest.TestCase):
    def test_diagnostics_preserve_publication_incomplete_thread_resolution_and_failure_kind(
        self,
    ) -> None:
        stream = io.StringIO()
        with (
            redirect_stderr(stream),
            review_lifecycle_diagnostics.lifecycle("review") as current,
        ):
            review_lifecycle_diagnostics.publication(
                {
                    "status": "publication_incomplete",
                    "publication_status": "publication_incomplete",
                    "thread_resolution": "unconfirmed",
                    "diagnostic": "publication_ambiguous",
                }
            )
            review_lifecycle_diagnostics.classified_failure(
                review_lifecycle_diagnostics.exception_kind(
                    ReviewInputError("synthetic-private-detail")
                )
            )
            current.returned(1)
        payload = _parse_single_lifecycle_event(stream.getvalue())
        self.assertEqual(
            payload["publication"]["publication_status"], "publication_incomplete"
        )
        self.assertEqual(payload["publication"]["thread_resolution"], "unconfirmed")
        self.assertEqual(payload["publication"]["diagnostic"], "publication_ambiguous")
        self.assertEqual(payload["primary_exception_kind"], "input_or_protocol")
        self.assertNotIn("synthetic-private-detail", json.dumps(payload))

    def test_path_b_legacy_witnessless_publication_failed_recovers_zero_inference(
        self,
    ) -> None:
        def setup_legacy_failed(
            directory: str,
        ) -> tuple[PublicationFailureRecoveryFixture, str]:
            fixture = PublicationFailureRecoveryFixture(directory, limit=1024)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Validate input bounds on retry.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                }
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)

            fixture.next_review_run(head_sha=HEAD_2)
            with patch.object(
                ReviewPublisher,
                "publish",
                side_effect=GitHubPublicationError("synthetic pre-publish interrupt"),
            ):
                self.assertEqual(fixture.invoke(), 1)
            # Strip any witness from Round 2 and stamp failed_attempts=1
            # so the record models a legacy witnessless `publication_failed` state
            # with retained ResultPartStore result.
            fixture.extra_comments = {
                cid: row
                for cid, row in fixture.extra_comments.items()
                if SETTLEMENT_WITNESS_MARKER not in str(row.get("body", ""))
            }
            fixture.rewrite_record(
                mutate=lambda current: current.evolve(
                    generation=current.generation + 1,
                    failed_attempts=1,
                    failed_attempts_head_sha=HEAD_2,
                )
            )
            settled = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(settled.transaction.phase, "publication_failed")
            self.assertEqual(settled.failed_attempts, 1)
            self.assertEqual(settled.failed_attempts_head_sha, HEAD_2)
            fixture.next_review_run(head_sha=HEAD_2)
            return fixture, settled.transaction.transaction_id

        with tempfile.TemporaryDirectory() as directory:
            fixture, _tx_id = setup_legacy_failed(directory)
            # Same-head run_attempt == 1 retry must reconcile via Path B with ZERO new inference.
            calls_before = fixture.provider_calls
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, calls_before)
            recovered = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertEqual(recovered.transaction.phase, "publication_succeeded")
            self.assertGreater(len(fixture.reviews), 1)

        with tempfile.TemporaryDirectory() as directory:
            fixture, tx_id = setup_legacy_failed(directory)
            # Dirty 3-surface census contrast: review already exists for tx_id -> stays fail-closed.
            fixture.reviews.append(
                {
                    "id": 999999,
                    "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                    "body": f"<!-- reviewsensei:review:v1 tx={tx_id} -->\nSynthetic prior review",
                    "commit_id": HEAD_2,
                    "state": "COMMENTED",
                }
            )
            calls_before = fixture.provider_calls
            self.assertEqual(fixture.invoke(), 1)
            self.assertEqual(fixture.provider_calls, calls_before)


class Gap3EvaluationFailedTests(unittest.TestCase):
    def test_refinalize_error_detail_and_clean_settlement_then_verify_approves(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Guard null payload before dereferencing.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                }
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
            )
            label = "RS-" + index.entries[0].instance[:6].upper()

            fixture.SOURCE_ID += 1
            fixture.select_command(
                f"@reviewsensei override {label} acceptable risk", new_run=True
            )
            original_external = fixture.external
            fail_finalization = True
            dispositions_committed = False

            def flaky_refinalize_external(request, timeout=None):
                nonlocal dispositions_committed
                path = urlsplit(request.full_url).path
                if (
                    request.method == "PATCH"
                    and path == "/repos/owner/repo/issues/comments/1000000"
                ):
                    resp = original_external(request, timeout=timeout)
                    rec_now = parse_session_comment(
                        fixture.comment["body"], identity=IDENTITY
                    )
                    if rec_now is not None and len(rec_now.dispositions) > 0:
                        dispositions_committed = True
                    return resp
                if (
                    fail_finalization
                    and dispositions_committed
                    and request.method == "POST"
                    and path == "/graphql"
                ):
                    fixture._meter(request)
                    return json_response(
                        {
                            "errors": [
                                {
                                    "type": "FORBIDDEN",
                                    "message": "synthetic graphql error",
                                }
                            ]
                        }
                    )
                return original_external(request, timeout=timeout)

            fixture.external = flaky_refinalize_external
            rc = fixture.invoke_command()
            self.assertEqual(rc, 1)
            self.assertIn("gate re-evaluation failed", fixture.stdout + fixture.error)
            settled = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            acct = ApiRunAccounting.from_document(settled.api_run_accounting)
            self.assertEqual(acct.action["phase"], "terminal")
            self.assertEqual(
                acct.action["outcome"],
                {"kind": "explicit_noop", "reason": "evaluation_failed"},
            )
            dispositions_before = len(settled.dispositions)
            self.assertGreaterEqual(dispositions_before, 1)

            # Next command (@reviewsensei verify) is admitted cleanly and approves without
            # duplicating already-committed dispositions.
            fail_finalization = False
            fixture.SOURCE_ID += 1
            fixture.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            after_verify = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertEqual(len(after_verify.dispositions), dispositions_before)
            self.assertTrue(
                any(r["state"] == "APPROVED" for r in fixture.reviews),
                fixture.reviews,
            )

    def test_evaluation_failed_with_uncertain_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Validate config timeout.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                }
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
            )
            label = "RS-" + index.entries[0].instance[:6].upper()

            fixture.SOURCE_ID += 1
            fixture.select_command(
                f"@reviewsensei override {label} acceptable risk", new_run=True
            )
            original_external = fixture.external

            def timeout_put_external(request, timeout=None):
                path = urlsplit(request.full_url).path
                if request.method == "PUT" and path.startswith(
                    "/repos/owner/repo/pulls/7/reviews/"
                ):
                    fixture._meter(request)
                    raise TimeoutError("synthetic lost PUT response")
                return original_external(request, timeout=timeout)

            fixture.external = timeout_put_external
            rc = fixture.invoke_command()
            self.assertEqual(rc, 1)
            settled = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            acct = ApiRunAccounting.from_document(settled.api_run_accounting)
            # Fail-closed when unconfirmed native effects exist: verify must not clear blindly.
            self.assertEqual(acct.action["phase"], "pending")

    def test_unfinished_pending_action_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Validate input schema.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                }
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
            )
            label = "RS-" + index.entries[0].instance[:6].upper()

            # Simulate an interrupted command whose receipt publication failed cleanly,
            # leaving action in `phase="pending"`, `execution_started=True`.
            fixture.SOURCE_ID += 1
            fixture.select_command(
                f"@reviewsensei override {label} acceptable risk", new_run=True
            )
            original_external = fixture.external

            def abort_receipt_post(request, timeout=None):
                path = urlsplit(request.full_url).path
                if (
                    request.method == "POST"
                    and path == "/repos/owner/repo/issues/7/comments"
                ):
                    import json as _json

                    body = _json.loads(request.data).get("body", "")
                    if "<!-- reviewsensei:command-receipt:v1" in body:
                        fixture._meter(request)
                        return json_response(
                            {"message": "synthetic receipt post refusal"}, 403
                        )
                return original_external(request, timeout=timeout)

            fixture.external = abort_receipt_post
            rc = fixture.invoke_command()
            self.assertEqual(rc, 1)
            fixture.external = original_external
            saved_body = fixture.comment["body"]
            reviews_before = len(fixture.reviews)
            interrupted = parse_session_comment(saved_body, identity=IDENTITY)
            interrupted_acct = ApiRunAccounting.from_document(
                interrupted.api_run_accounting
            )
            self.assertEqual(interrupted_acct.action["phase"], "pending")
            self.assertIs(interrupted_acct.action["execution_started"], True)

            # Predecessor rollover must remain strictly fail-closed on unfinished pending actions.
            fixture.SOURCE_ID += 1
            fixture.select_command("@reviewsensei verify", new_run=True)
            self.assertNotEqual(fixture.invoke_command(), 0)
            self.assertEqual(fixture.comment["body"], saved_body)
            self.assertEqual(len(fixture.reviews), reviews_before)


class Gap4PartitionedDispositionCapacityTests(unittest.TestCase):
    def test_partitioned_disposition_snapshot_20plus_overrides_and_approves_within_512(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": f"Confirm contract invariant {idx}.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for idx in range(25)
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
            )
            labels = ["RS-" + entry.instance[:6].upper() for entry in index.entries]

            # Record first 20 in batches of 4 so disposition_snapshot crosses into partitioned-json-v1
            # BEFORE the remaining batches run.
            for start in range(0, 20, 4):
                fixture.SOURCE_ID += 1
                fixture.select_command(
                    "@reviewsensei override "
                    + " ".join(labels[start : start + 4])
                    + " acceptable risk",
                    new_run=True,
                )
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)

            mid = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(mid.disposition_snapshot)
            self.assertEqual(
                mid.disposition_snapshot["state_manifest"]["encoding"],
                "partitioned-json-v1",
            )

            # Remaining 5 in 2-ID batches + final 1-ID approval-submitting batch
            # (with conditional_approval active) under limit=512.
            for sl in (slice(20, 22), slice(22, 24), slice(24, 25)):
                fixture.SOURCE_ID += 1
                fixture.select_command(
                    "@reviewsensei override "
                    + " ".join(labels[sl])
                    + " acceptable risk",
                    new_run=True,
                )
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
                self.assertNotIn(
                    "before business writes", fixture.stdout + fixture.error
                )

            final_record = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            decisions = session_dispositions(
                final_record, ledger=fixture.selected.ledger
            )
            self.assertEqual(len(decisions), 25)
            self.assertEqual(
                sum(1 for review in fixture.reviews if review["state"] == "APPROVED"),
                1,
            )


class Gap5SemanticHistoryCapacityNoopTests(unittest.TestCase):
    def test_preflight_capacity_refusals_do_not_burn_limit_2_while_genuine_noops_do(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": f"Confirm contract invariant {idx}.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                }
                for idx in range(6)
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
            )
            labels = ["RS-" + entry.instance[:6].upper() for entry in index.entries]

            # 2 oversized override attempts -> both refuse preflight before business writes.
            oversized = (
                "@reviewsensei override " + " ".join(labels) + " acceptable risk"
            )
            for _ in range(2):
                fixture.SOURCE_ID += 1
                fixture.select_command(oversized, new_run=True)
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
                self.assertIn("before business writes", fixture.stdout)
                rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
                acct = ApiRunAccounting.from_document(rec.api_run_accounting)
                self.assertEqual(
                    acct.action["outcome"],
                    {"kind": "explicit_noop", "reason": "capacity_unavailable"},
                )

            # Smaller feasible batch MUST be admitted (streak was NOT burned to LIMIT=2) and
            # commits real progress!
            fixture.SOURCE_ID += 1
            fixture.select_command(
                f"@reviewsensei override {labels[0]} acceptable risk", new_run=True
            )
            self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertGreaterEqual(len(rec.dispositions), 1)

            # Contrast: genuine repeated no-progress semantic no-ops (verify twice -> third refuses).
            for _ in range(2):
                fixture.SOURCE_ID += 1
                fixture.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            fixture.SOURCE_ID += 1
            fixture.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(fixture.invoke_command(), 1, fixture.error)
            self.assertIn(
                "accepted_semantic_history_finite_no_progress_stop", fixture.error
            )


class Gap6HierarchicalDriftMatchingTests(unittest.TestCase):
    def test_fixed_items_verify_fixed_and_optional_stays_non_blocking(self) -> None:
        head_1 = "a" * 40
        head_2 = "b" * 40
        base = "0" * 40
        validator_coverage = CoverageManifest(
            files=(FileCoverage("src/validator.py", "reviewed"),),
            enumerated_paths=("src/validator.py",),
            enumeration_complete=True,
        )
        r1 = evaluate_drift_round(
            ReviewResult(
                summary="Round 1",
                comments=(
                    ReviewComment(
                        path="src/validator.py",
                        line=10,
                        side="RIGHT",
                        body="Reject empty header token.",
                        severity="high",
                        blocking=True,
                        effective_blocking=True,
                        needs_human=False,
                        symbol="parse_header",
                        evidence_id="ev-header-empty",
                    ),
                    ReviewComment(
                        path="src/validator.py",
                        line=40,
                        side="RIGHT",
                        body="Consider validating optional hint length.",
                        severity="low",
                        blocking=False,
                        effective_blocking=False,
                        needs_human=False,
                        symbol="format_hint",
                        evidence_id="ev-hint-length",
                    ),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
                review_status="complete",
                coverage=validator_coverage,
            ),
            head_sha=head_1,
            base_sha=base,
            max_active_findings=20,
            round_number=1,
            diff_text="diff --git a/src/validator.py b/src/validator.py\n--- a/src/validator.py\n+++ b/src/validator.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        confirmed_fixes = tuple(
            ConfirmedFixEvidence(
                obligation_id=ob.obligation_id,
                resolution_criterion=ob.criterion.resolution_criterion,
                head_sha=head_2,
                evidence_sha256="f" * 64,
                verified_paths=("src/validator.py",),
            )
            for ob in r1.inventory.obligations
        )
        # Commit 2 fixes both prior findings and introduces a completely distinct validation
        # finding on parse_footer -> prior findings must become verified-fixed (0 ambiguous holds).
        r2 = evaluate_drift_round(
            ReviewResult(
                summary="Round 2",
                comments=(
                    ReviewComment(
                        path="src/validator.py",
                        line=85,
                        side="RIGHT",
                        body="Validate footer checksum length.",
                        severity="medium",
                        blocking=True,
                        effective_blocking=True,
                        needs_human=False,
                        symbol="parse_footer",
                        evidence_id="ev-footer-len",
                    ),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
                review_status="complete",
                coverage=validator_coverage,
            ),
            head_sha=head_2,
            base_sha=base,
            max_active_findings=20,
            round_number=2,
            prior_inventory=r1.inventory,
            drift_descriptors=(
                FileDriftDescriptor(
                    drift_kind="line_deletion", source_path="src/validator.py"
                ),
            ),
            confirmed_fixes=confirmed_fixes,
            diff_text="diff --git a/src/validator.py b/src/validator.py\n--- a/src/validator.py\n+++ b/src/validator.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        by_sym = {
            (ob.present_anchor.symbol or ob.criterion.origin_symbol): ob
            for ob in r2.inventory.obligations
        }
        self.assertEqual(by_sym["parse_header"].lifecycle_state, "verified-fixed")
        self.assertEqual(by_sym["format_hint"].lifecycle_state, "verified-fixed")
        self.assertFalse(by_sym["format_hint"].criterion.blocking)
        self.assertEqual(by_sym["parse_footer"].lifecycle_state, "active-unresolved")
        self.assertFalse(
            any(
                ob.lifecycle_state == "ambiguous-multi-target"
                for ob in r2.inventory.obligations
            )
        )

    def test_same_file_distinct_unmatched_findings_do_not_collapse_to_duplicate_code(
        self,
    ) -> None:
        head_1 = "a" * 40
        base = "0" * 40
        r1 = evaluate_drift_round(
            ReviewResult(
                summary="Round 1",
                comments=(
                    ReviewComment(
                        path="src/helper.py",
                        line=12,
                        side="RIGHT",
                        body="Validate missing API key.",
                        severity="medium",
                        effective_blocking=True,
                        needs_human=False,
                        symbol="helper",
                        evidence_id="ev-key",
                    ),
                    ReviewComment(
                        path="src/helper.py",
                        line=28,
                        side="RIGHT",
                        body="Validate negative timeout.",
                        severity="medium",
                        effective_blocking=True,
                        needs_human=False,
                        symbol="helper",
                        evidence_id="ev-timeout",
                    ),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
                review_status="complete",
            ),
            head_sha=head_1,
            base_sha=base,
            max_active_findings=20,
            round_number=1,
            diff_text="diff --git a/src/helper.py b/src/helper.py\n--- a/src/helper.py\n+++ b/src/helper.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        self.assertEqual(len(r1.inventory.obligations), 2)
        self.assertTrue(
            all(
                ob.lifecycle_state == "active-unresolved"
                for ob in r1.inventory.obligations
            )
        )


if __name__ == "__main__":
    unittest.main()
