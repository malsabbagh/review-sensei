"""Complete inventories survive storage, publication and bounded reassessment."""

import base64
import hashlib
import json
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path

from review_sensei.baseline import (
    MAX_HISTORY_BASELINE_BYTES,
    BaselineFinding,
    BaselinePersistenceError,
    admission_context_document,
    admission_context_from_document,
    baseline_from_history_document,
    baseline_from_review,
    baseline_history_document,
)
from review_sensei.bounded_evidence import (
    canonical_bytes,
    decode_evidence,
    encode_evidence,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
    approval_facts_from_result,
    evaluate_approval_facts,
)
from review_sensei.hosting.github.checks import check_outcome_for_result
from review_sensei.hosting.github.publication import _review_summary_state
from review_sensei.hosting.github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from review_sensei.human_assessment import (
    HumanAssessmentDecision,
    HumanAssessmentReply,
    HumanAssessmentService,
    HumanReviewFinding,
    PendingHumanReview,
)
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.presentation import WITHHELD_STATE, render_review_text
from review_sensei.session import (
    MAX_SESSION_RECORD_BYTES,
    LocalSessionLedger,
    checkpoint_review_analysis,
    complete_review_publication,
    load_review_transaction_for_publication,
    prepare_review_transaction,
)

try:
    import test_review_transaction as fixture
except ImportError:
    from tests import test_review_transaction as fixture


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def findings(count):
    return tuple(
        sorted(
            (
                BaselineFinding(
                    digest(f"finding-{i}"),
                    digest(f"resolution-{i}"),
                    concern=digest(f"concern-{i}"),
                    path=f"src/file-{i}.py",
                    defect_kind="resource-leak",
                    blocking=True,
                )
                for i in range(count)
            ),
            key=lambda item: item.fingerprint,
        )
    )


class CompleteEvidenceTests(unittest.TestCase):
    def test_realistic_twelve_and_twenty_four_finding_inventory_round_trips(self):
        for count in (12, 24):
            baseline = replace(fixture._baseline(1), findings=findings(count))
            encoded = baseline_history_document(baseline, require_complete=True)
            self.assertLessEqual(
                len(canonical_bytes(encoded)), MAX_HISTORY_BASELINE_BYTES
            )
            self.assertEqual(baseline_from_history_document(encoded), baseline)
            self.assertEqual(
                admission_context_from_document(
                    admission_context_document(baseline, baseline.cache_key)
                )[0],
                baseline,
            )
            self.assertEqual(
                baseline_history_document(
                    replace(baseline, findings=tuple(reversed(baseline.findings)))
                ),
                encoded,
            )

    def test_legacy_baseline_and_encoded_boundary_remain_explicit(self):
        small = replace(fixture._baseline(1), findings=findings(2))
        legacy = baseline_history_document(small, require_complete=True)
        self.assertNotIn("encoding", legacy)
        self.assertEqual(baseline_from_history_document(legacy), small)
        large = replace(small, findings=findings(12))
        encoded = baseline_history_document(large, require_complete=True)
        self.assertEqual(encoded["encoding"], "zlib-json-v1")
        size = len(canonical_bytes(encoded))
        self.assertEqual(
            baseline_history_document(large, max_bytes=size, require_complete=True),
            encoded,
        )
        with self.assertRaises(BaselinePersistenceError):
            baseline_history_document(large, max_bytes=size - 1, require_complete=True)

    def test_broader_discovery_retains_twenty_resolved_findings_and_capacity_reason(
        self,
    ):
        result = replace(
            fixture._result(),
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=1,
                    body="New distinct human concern.",
                    needs_human=True,
                ),
            ),
        )
        old = PendingHumanReview(
            fixture.BASE_SHA,
            tuple(
                HumanReviewFinding(
                    digest(f"retained-{i}"), "src/app.py", f"Concern {i}."
                )
                for i in range(20)
            ),
            tuple(digest(f"retained-{i}") for i in range(20)),
        )
        facts = replace(
            approval_facts_from_result(result, enabled=True, app_authored=False),
            review_status="partial",
            persistence_status="capacity-exceeded",
            has_human_adjudication_findings=False,
        )
        retained = ReviewApprovalEligibility(
            fixture.HEAD_SHA, result.content_digest(), facts, old
        )
        merged = approval_eligibility_from_result(
            result,
            head_sha=fixture.HEAD_SHA,
            base_sha=fixture.BASE_SHA,
            enabled=True,
            app_authored=False,
            retained_eligibility=retained,
        )
        self.assertEqual(len(merged.human_review.findings), 21)
        self.assertEqual(len(merged.human_review.pending), 1)
        self.assertEqual(len(merged.human_review.resolved), 20)
        self.assertEqual(merged.facts.persistence_status, "capacity-exceeded")
        self.assertEqual(ReviewApprovalEligibility.from_dict(merged.to_dict()), merged)

    def test_five_hundred_paths_plus_thirteen_duplicate_finding_paths_count_once(self):
        paths = tuple(f"src/file-{i}.py" for i in range(500))
        result = ReviewResult(
            summary="Complete review.",
            comments=tuple(
                ReviewComment(path=path, line=1, body="Check this concern.")
                for path in paths[:13]
            ),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=tuple(FileCoverage(path, "reviewed") for path in paths),
                enumerated_paths=paths,
            ),
        )
        baseline = baseline_from_review(
            result, cache_key=fixture._baseline(1).cache_key, policy=fixture.POLICY
        )
        self.assertEqual(len(baseline.reviewed_paths), 500)
        restored = baseline_from_history_document(
            baseline_history_document(baseline, require_complete=True)
        )
        self.assertEqual(set(restored.reviewed_paths), set(paths))
        self.assertEqual(len(restored.findings), 13)
        self.assertTrue(restored.complete)

    def test_valid_long_unicode_path_and_defect_kind_preserve_exact_values(self):
        path = "src/" + "/".join("é" * 90 for _ in range(4)) + "/file.py"
        baseline = replace(
            fixture._baseline(1),
            findings=(replace(findings(1)[0], path=path, defect_kind="d" * 200),),
            reviewed_paths=(path,),
            related_paths=(path,),
        )
        restored = baseline_from_history_document(
            baseline_history_document(baseline, require_complete=True)
        )
        self.assertEqual(restored, baseline)
        self.assertGreater(len(path), 257)

    def test_complete_capacity_failure_does_not_return_a_projection(self):
        baseline = replace(fixture._baseline(1), findings=findings(250))
        with self.assertRaises(BaselinePersistenceError) as caught:
            baseline_history_document(baseline, require_complete=True)
        self.assertEqual(
            caught.exception.persistence_diagnostics["reasons"], ["encoded-bytes"]
        )
        self.assertEqual(caught.exception.persistence_diagnostics["finding_count"], 250)
        self.assertNotIn("src/", json.dumps(caught.exception.persistence_diagnostics))

    def test_large_baseline_local_and_github_lifecycle_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            prepared = prepare_review_transaction(
                ledger,
                fixture.IDENTITY,
                fixture.POLICY,
                reservation_id="f" * 64,
                base_sha=fixture.BASE_SHA,
                head_sha=fixture.HEAD_SHA,
                configuration_digest=fixture.CONFIGURATION_DIGEST,
                evidence_digest=fixture.EVIDENCE_DIGEST,
                now=fixture.NOW,
            )
            baseline = replace(
                fixture._baseline(prepared.record.generation + 1), findings=findings(24)
            )
            result = checkpoint_review_analysis(
                ledger,
                fixture.IDENTITY,
                prepared,
                fixture._result(),
                baseline=baseline,
                now=fixture.NOW,
            )
            restarted = LocalSessionLedger(Path(directory))
            record = load_review_transaction_for_publication(
                restarted,
                fixture.IDENTITY,
                result,
                base_sha=fixture.BASE_SHA,
                head_sha=fixture.HEAD_SHA,
                policy_digest=fixture.POLICY.digest(),
                configuration_digest=fixture.CONFIGURATION_DIGEST,
                evidence_digest=fixture.EVIDENCE_DIGEST,
                now=fixture.NOW,
            )
            failed = complete_review_publication(
                restarted,
                fixture.IDENTITY,
                record.transaction,
                published=False,
                now=fixture.NOW,
            )
            succeeded = complete_review_publication(
                restarted,
                fixture.IDENTITY,
                failed.transaction,
                published=True,
                now=fixture.NOW,
            )
            body = render_session_comment(
                repository_id=99,
                pull_request=fixture.IDENTITY.pull_request,
                record=succeeded,
            )
            parsed = parse_session_comment(body, identity=fixture.IDENTITY)
            self.assertEqual(
                baseline_from_history_document(parsed.convergence_history["baseline"]),
                baseline,
            )
            self.assertLessEqual(
                len(canonical_bytes(succeeded.to_dict())), MAX_SESSION_RECORD_BYTES
            )
            self.assertEqual(succeeded.completed_initial_reviews, 1)
            self.assertEqual(succeeded.failed_attempts, 0)
            tampered = succeeded.to_dict()
            tampered["convergence_history"]["baseline"]["sha256"] = "a" * 64
            from review_sensei.session import SessionRecord

            with self.assertRaises(ReviewInputError):
                SessionRecord.from_dict(tampered)

    def test_human_inventory_reserves_resolution_growth_and_remains_fail_closed(self):
        inventory = PendingHumanReview(
            fixture.BASE_SHA,
            tuple(
                HumanReviewFinding(
                    digest(f"human-{i}"), "src/app.py", f"Concern {i}: " + "é" * 4000
                )
                for i in range(25)
            ),
        )
        self.assertIn("inventory", inventory.to_dict())
        for resolved in (
            (),
            tuple(item.fingerprint for item in inventory.findings[:20]),
            tuple(item.fingerprint for item in inventory.findings),
        ):
            current = replace(inventory, resolved=resolved)
            self.assertEqual(PendingHumanReview.from_dict(current.to_dict()), current)
            self.assertEqual(len(current.pending), 25 - len(resolved))
        result = ReviewResult(
            summary="Human obligations remain.",
            comments=tuple(
                ReviewComment(
                    path="src/app.py",
                    line=1,
                    body=item.body,
                    needs_human=True,
                    defect_kind=f"concern-{i}",
                )
                for i, item in enumerate(inventory.findings)
            ),
            provider="fixture",
            review_status="complete",
        )
        eligibility = ReviewApprovalEligibility(
            fixture.HEAD_SHA,
            result.content_digest(),
            approval_facts_from_result(result, enabled=True, app_authored=False),
            inventory,
        )
        self.assertEqual(eligibility.to_dict()["schema_version"], "3")
        self.assertEqual(
            ReviewApprovalEligibility.from_dict(eligibility.to_dict()), eligibility
        )
        self.assertIn(
            "human-adjudication-open",
            evaluate_approval_facts(eligibility.facts).blockers,
        )
        decisions = tuple(
            HumanAssessmentDecision(item.fingerprint, "unresolved", "", "", "")
            for item in inventory.findings
        )
        self.assertEqual(
            len(
                HumanAssessmentReply(
                    "All concerns remain unresolved.", decisions
                ).decisions
            ),
            25,
        )
        with self.assertRaisesRegex(ReviewInputError, "batches"):
            HumanAssessmentService._request(
                head_sha=fixture.HEAD_SHA,
                pending=inventory,
                source_body="Please reassess.",
                diff_context="synthetic",
            )

    def test_capacity_status_survives_exact_digest_and_user_facing_surfaces(self):
        result = replace(
            fixture._result(),
            review_status="partial",
            persistence_status="capacity-exceeded",
        )
        self.assertEqual(ReviewResult.from_dict(result.to_dict()), result)
        self.assertNotEqual(
            result.content_digest(),
            replace(result, persistence_status=None).content_digest(),
        )
        facts = approval_facts_from_result(result, enabled=True, app_authored=False)
        self.assertIn(
            "baseline-capacity-exceeded", evaluate_approval_facts(facts).blockers
        )
        with self.assertRaises(ReviewInputError):
            check_outcome_for_result(result, policy="invalid")
        self.assertIn(
            "Analysis complete",
            check_outcome_for_result(result, policy="auto-approve").title,
        )
        self.assertIn("analysis: complete", render_review_text(result))
        state, reason = _review_summary_state(
            result, policy=fixture.POLICY, auto_approve=True
        )
        self.assertEqual(state, WITHHELD_STATE)
        self.assertIn("persistence", reason)


class BoundedEncodingTests(unittest.TestCase):
    def test_decode_rejects_tampering_truncation_trailing_stream_and_expansion_bomb(
        self,
    ):
        document = {"finding": "x" * 1000}
        encoded = encode_evidence(document, max_decoded_bytes=2048)
        self.assertEqual(
            decode_evidence(encoded, max_encoded_bytes=2048, max_decoded_bytes=2048),
            document,
        )
        packed = base64.b64decode(encoded["data"])
        for changed in (
            {**encoded, "sha256": "0" * 64},
            {**encoded, "decoded_bytes": 2},
            {**encoded, "data": "!!!"},
            {**encoded, "data": base64.b64encode(packed[:-1]).decode()},
            {**encoded, "data": base64.b64encode(packed + packed).decode()},
            {**encoded, "extra": True},
            {**encoded, "decoded_bytes": 4096},
        ):
            with self.assertRaises(ReviewInputError):
                decode_evidence(changed, max_encoded_bytes=2048, max_decoded_bytes=2048)

    def test_noncanonical_duplicate_keys_and_nonfinite_values_are_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"b":1, "a":2}', b'{"a":NaN}'):
            envelope = {
                "encoding": "zlib-json-v1",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "decoded_bytes": len(raw),
                "data": base64.b64encode(zlib.compress(raw)).decode(),
            }
            with self.assertRaises(ReviewInputError):
                decode_evidence(
                    envelope, max_encoded_bytes=2048, max_decoded_bytes=2048
                )
