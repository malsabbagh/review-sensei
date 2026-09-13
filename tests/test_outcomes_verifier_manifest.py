import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import PromotionRecord, validate_promotion_record
from review_sensei.outcomes import RecoveryArtifact, RunOutcome
from review_sensei.release_manifest import validate_compatibility_manifest
from review_sensei.verifier import CandidateFinding, EvidenceReference, verify_candidate


SHA = "a" * 64


class ContractsTests(unittest.TestCase):
    def test_fixture_promotion_is_rejected(self):
        with self.assertRaises(ReviewInputError):
            PromotionRecord(SHA, SHA, SHA, SHA, "fixture", "fixture-v1", "r1", 3, "2026-01-01", {})

    def test_run_outcome_round_trip(self):
        value = RunOutcome("skipped_policy", diagnostic="policy").to_dict()
        self.assertEqual(value["status"], "skipped_policy")

    def test_recovery_artifact_rejects_tampering_and_identity_mismatch(self):
        artifact = RecoveryArtifact.create(repository="acme/repo", pull_request_number=1, base_sha=SHA, head_sha=SHA, result={"summary": "ok"}, expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        artifact.validate(repository="acme/repo", pull_request_number=1, base_sha=SHA, head_sha=SHA)
        with self.assertRaises(ReviewInputError):
            artifact.validate(repository="other/repo", pull_request_number=1, base_sha=SHA, head_sha=SHA)

    def test_evidence_verifier_requires_exact_snapshot(self):
        text = "line one\nline two\n"
        candidate = CandidateFinding("bug", "when called", "src/app.py", (EvidenceReference("src/app.py", 2, SHA, "line two"),), "causes failure")
        self.assertEqual(verify_candidate(candidate, {"src/app.py": text}, snapshot_sha256=SHA).disposition, "confirmed")
        bad = EvidenceReference("src/app.py", 2, "b" * 64, "line two")
        bad_candidate = CandidateFinding("bug", "when called", "src/app.py", (bad,), "causes failure")
        self.assertEqual(verify_candidate(bad_candidate, {"src/app.py": text}, snapshot_sha256=SHA).disposition, "rejected")

    def test_manifest_rejects_mismatched_release_versions(self):
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {"schema_version": "1.0", "release": "1.0.0", "compatible_worker_range": ">=1", "provenance": "signed", "artifacts": {"workflow": artifact, "python": artifact, "npm": [artifact], "schemas_version": "1.0", "worker": {**artifact, "version": "2.0.0"}}}
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)


if __name__ == "__main__":
    unittest.main()
