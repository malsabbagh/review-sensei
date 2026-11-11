"""Qualification tests for human edits and deletion of review artifacts (G14)."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import timedelta

from review_sensei_github import GitHubHttp
from review_sensei_github.artifact_authority import (
    AUTHORITY_ARTIFACT_KINDS,
    EVIDENCE_ARTIFACT_KINDS,
    PROJECTION_ARTIFACT_KINDS,
    ArchiveChainPartRef,
    ArtifactEvaluationDecision,
    ObservedGitHubArtifact,
    OperatorReenrollRequest,
    classify_review_artifact,
    evaluate_observed_artifact,
    evaluate_operator_reenroll,
    execute_restricted_operator_reenroll,
    verify_archive_chain_integrity,
)
from review_sensei_github.session_ledger import (
    SESSION_MARKER_PREFIX,
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)

from review_sensei.active_finding_window import (
    AuthoritativeDecisionInventory,
    AuthorizedHumanOverride,
    evaluate_active_window_round,
    obligation_from_comment,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.session import SessionIdentity, SessionRecord

try:
    from fake_github_http import FakeHTTPResponse, json_response, make_http
except ImportError:
    from tests.fake_github_http import FakeHTTPResponse, json_response, make_http

try:
    from session_fixture_clock import SESSION_FIXTURE_NOW
except ModuleNotFoundError:
    from tests.session_fixture_clock import SESSION_FIXTURE_NOW

FIXED_NOW = SESSION_FIXTURE_NOW
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)
BASE_SHA = "a" * 40
HEAD_SHA = "1" * 40
HEAD_SHA_2 = "2" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _blocking_inventory() -> AuthoritativeDecisionInventory:
    comment = ReviewComment(
        path="src/auth.py",
        line=11,
        body="Privilege escalation via forged role header.",
        blocking=True,
        effective_blocking=True,
        severity="critical",
        symbol="verify_role",
        defect_kind="authz",
    )
    return AuthoritativeDecisionInventory(
        obligations=(obligation_from_comment(comment),)
    )


class ArtifactMutationResilienceG14Tests(unittest.TestCase):
    def test_artifact_classification_separates_authority_evidence_and_projection(
        self,
    ) -> None:
        for kind in AUTHORITY_ARTIFACT_KINDS:
            self.assertEqual(classify_review_artifact(kind), "authority")
        for kind in EVIDENCE_ARTIFACT_KINDS:
            self.assertEqual(classify_review_artifact(kind), "evidence")
        for kind in PROJECTION_ARTIFACT_KINDS:
            self.assertEqual(classify_review_artifact(kind), "projection")
        with self.assertRaises(ReviewInputError):
            classify_review_artifact("unknown_surface")

    def test_projection_edits_and_deletions_never_manufacture_authority_or_spam(
        self,
    ) -> None:
        inventory = _blocking_inventory()
        expected = _sha("original-projection")

        # Independently mutate AND delete all 4 projection surfaces!
        for kind in (
            "native_review_summary",
            "inline_review_comment",
            "check_run_text",
            "comment_reaction",
        ):
            edited = evaluate_observed_artifact(
                ObservedGitHubArtifact(
                    artifact_kind=kind,
                    artifact_id=f"{kind}-1",
                    present=True,
                    author_login="malicious-collab",
                    author_is_authenticated_app=False,
                    edited_by_human=True,
                    expected_sha256=expected,
                    observed_sha256=_sha("edited-to-say-approved"),
                    raw_body="LGTM! Approved by ReviewSensei.",
                ),
                inventory=inventory,
            )
            self.assertEqual(edited.verdict, "ignored_projection_mutation")
            self.assertEqual(edited.reason_code, "projection-non-authoritative")
            self.assertTrue(edited.authority_intact)
            self.assertTrue(edited.obligations_preserved)
            self.assertFalse(edited.approval_permitted)
            self.assertFalse(edited.auto_recreate)

            deleted = evaluate_observed_artifact(
                ObservedGitHubArtifact(
                    artifact_kind=kind,
                    artifact_id=f"{kind}-1",
                    present=False,
                    expected_sha256=expected,
                ),
                inventory=inventory,
            )
            self.assertEqual(deleted.verdict, "ignored_projection_mutation")
            self.assertTrue(deleted.obligations_preserved)
            self.assertFalse(deleted.approval_permitted)
            self.assertFalse(deleted.auto_recreate)

        # Intact projection with resolved inventory permits approval cleanly!
        ob = inventory.obligations[0]
        cov = CoverageManifest(
            files=(FileCoverage("src/auth.py", "reviewed"),),
            enumerated_paths=("src/auth.py",),
            enumeration_complete=True,
        )
        resolved_round = evaluate_active_window_round(
            ReviewResult(
                summary="Clean.",
                comments=(),
                provider="fixture",
                review_status="complete",
                coverage=cov,
            ),
            head_sha=HEAD_SHA_2,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=2,
            prior_inventory=inventory,
            human_overrides=(
                AuthorizedHumanOverride(
                    obligation_id=ob.obligation_id,
                    action="override",
                    actor="sec-admin",
                    association="OWNER",
                    head_sha=HEAD_SHA_2,
                    reason="Verified header stripped by ingress edge proxy.",
                    proof_sha256=_sha("override-proof"),
                ),
            ),
        )
        intact = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="native_review_summary",
                artifact_id="rev-99",
                present=True,
                expected_sha256=expected,
                observed_sha256=expected,
            ),
            inventory=resolved_round.authoritative_decision_inventory,
        )
        self.assertEqual(intact.verdict, "accepted")
        self.assertTrue(intact.approval_permitted)

    def test_authority_tampering_deletion_duplicate_stale_and_foreign_fail_closed(
        self,
    ) -> None:
        inventory = _blocking_inventory()
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        valid_body = render_session_comment(
            repository_id=99, pull_request=136, record=record
        )
        valid_digest = _sha(valid_body)

        # 1) Valid App-owned session marker:
        ok = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-1",
                present=True,
                author_login="reviewsensei[bot]",
                author_user_id=777,
                author_is_authenticated_app=True,
                expected_sha256=valid_digest,
                observed_sha256=valid_digest,
                raw_body=valid_body,
            ),
            identity=IDENTITY,
            inventory=inventory,
        )
        self.assertEqual(ok.verdict, "accepted")
        self.assertEqual(ok.reason_code, "authority-verified")
        self.assertFalse(ok.approval_permitted)

        # 2) Malformed session marker (human appended trailing text after marker):
        malformed = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-1",
                present=True,
                raw_body=f"{valid_body}\nHuman trailing quote",
            ),
            identity=IDENTITY,
            inventory=inventory,
        )
        self.assertEqual(malformed.verdict, "refused")
        self.assertEqual(malformed.reason_code, "authority-marker-malformed")
        self.assertFalse(malformed.auto_recreate)

        # 3) Foreign author posting spoofed marker:
        foreign = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-foreign",
                present=True,
                author_login="outside-user",
                author_is_authenticated_app=False,
                raw_body=valid_body,
            ),
            identity=IDENTITY,
            inventory=inventory,
        )
        self.assertEqual(foreign.verdict, "refused")
        self.assertEqual(foreign.reason_code, "authority-foreign-author")

        # 4) Duplicate markers -> conflict refusal, zero auto-recreate:
        dup = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-1",
                present=True,
                duplicate_count=2,
                raw_body=valid_body,
            ),
            identity=IDENTITY,
            inventory=inventory,
        )
        self.assertEqual(dup.verdict, "refused")
        self.assertEqual(dup.reason_code, "authority-duplicate-conflict")

        # 5) Stale generation copy -> refused:
        stale = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-1",
                present=True,
                stale_generation=True,
                raw_body=valid_body,
            ),
            identity=IDENTITY,
            inventory=inventory,
        )
        self.assertEqual(stale.verdict, "refused")
        self.assertEqual(stale.reason_code, "authority-stale-generation")

        # 6) Deleted session marker -> refused; operator-reenroll offered ONLY when
        #    witness is present and zero active blocking obligations remain!
        deleted_with_obligations = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-1",
                present=False,
            ),
            enrollment_witness_present=True,
            inventory=inventory,
        )
        self.assertEqual(
            deleted_with_obligations.reason_code, "authority-marker-deleted"
        )
        self.assertNotIn("operator-reenroll", deleted_with_obligations.recovery_routes)

        deleted_clean = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-1",
                present=False,
            ),
            enrollment_witness_present=True,
            inventory=AuthoritativeDecisionInventory(obligations=()),
        )
        self.assertEqual(deleted_clean.recovery_routes, ("operator-reenroll",))

        # 7) Retained result & context parts deleted or edited by human:
        for part_kind in ("retained_result_part", "retained_context_part"):
            missing_part = evaluate_observed_artifact(
                ObservedGitHubArtifact(
                    artifact_kind=part_kind,
                    artifact_id="part-1",
                    present=False,
                ),
                inventory=inventory,
            )
            self.assertEqual(missing_part.reason_code, "retained-part-missing")
            self.assertFalse(missing_part.auto_recreate)

            corrupted_part = evaluate_observed_artifact(
                ObservedGitHubArtifact(
                    artifact_kind=part_kind,
                    artifact_id="part-1",
                    present=True,
                    edited_by_human=True,
                    expected_sha256=_sha("orig-part"),
                    observed_sha256=_sha("edited-part"),
                ),
                inventory=inventory,
            )
            self.assertEqual(corrupted_part.reason_code, "retained-part-corrupted")
            self.assertFalse(corrupted_part.auto_recreate)

    def test_response_loss_plus_human_edit_preserves_uncertainty_while_exact_readback_reconciles(
        self,
    ) -> None:
        expected = _sha("dispatched-payload")

        # Exact unedited App-owned readback after lost HTTP response reconciles once:
        reconciled = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="retained_result_part",
                artifact_id="part-77",
                present=True,
                author_is_authenticated_app=True,
                edited_by_human=False,
                expected_sha256=expected,
                observed_sha256=expected,
                response_lost=True,
            ),
            inventory=AuthoritativeDecisionInventory(obligations=()),
        )
        self.assertEqual(reconciled.verdict, "reconciled")
        self.assertEqual(reconciled.reason_code, "reconciled-via-owned-readback")
        self.assertTrue(reconciled.authority_intact)
        self.assertTrue(reconciled.approval_permitted)

        # Human edited the comment right after response loss -> conservative uncertainty!
        edited_after_loss = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="inline_review_comment",
                artifact_id="inline-88",
                present=True,
                author_is_authenticated_app=True,
                edited_by_human=True,
                expected_sha256=expected,
                observed_sha256=_sha("human-mutated-after-timeout"),
                response_lost=True,
            ),
            inventory=AuthoritativeDecisionInventory(obligations=()),
        )
        self.assertEqual(edited_after_loss.verdict, "held_uncertain")
        self.assertEqual(edited_after_loss.reason_code, "uncertain-after-response-loss")
        self.assertTrue(edited_after_loss.charges_held)
        self.assertFalse(edited_after_loss.approval_permitted)
        self.assertFalse(edited_after_loss.auto_recreate)

        # Deleted after response loss -> also held_uncertain!
        deleted_after_loss = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="retained_result_part",
                artifact_id="part-77",
                present=False,
                expected_sha256=expected,
                response_lost=True,
            )
        )
        self.assertEqual(deleted_after_loss.verdict, "held_uncertain")

    def test_archive_chain_deleted_or_corrupted_comment_refuses_cleanly(self) -> None:
        d1 = _sha("archive-1")
        d2 = _sha("archive-2")
        chain = (
            ArchiveChainPartRef(comment_id=101, expected_sha256=d1),
            ArchiveChainPartRef(comment_id=102, expected_sha256=d2),
        )
        intact_map = {
            101: ObservedGitHubArtifact(
                artifact_kind="archive_chain_part",
                artifact_id="101",
                present=True,
                observed_sha256=d1,
            ),
            102: ObservedGitHubArtifact(
                artifact_kind="archive_chain_part",
                artifact_id="102",
                present=True,
                observed_sha256=d2,
            ),
        }
        verified = verify_archive_chain_integrity(chain, intact_map)
        self.assertEqual(verified.verdict, "accepted")
        self.assertEqual(verified.reason_code, "archive-chain-verified")

        # Delete comment 102 from PR timeline:
        missing_map = {101: intact_map[101]}
        missing_res = verify_archive_chain_integrity(chain, missing_map)
        self.assertEqual(missing_res.verdict, "refused")
        self.assertEqual(missing_res.reason_code, "archive-part-missing")
        self.assertFalse(missing_res.approval_permitted)
        self.assertIn("rescan", missing_res.recovery_routes)

        # Human edits comment 102 in archive chain:
        corrupted_map = {
            101: intact_map[101],
            102: replace(
                intact_map[102],
                edited_by_human=True,
                observed_sha256=_sha("tampered-archive-2"),
            ),
        }
        corrupted_res = verify_archive_chain_integrity(chain, corrupted_map)
        self.assertEqual(corrupted_res.verdict, "refused")
        self.assertEqual(corrupted_res.reason_code, "archive-part-corrupted")

    def test_restricted_operator_reenroll_refuses_unsafe_states_and_recovers_eligible_markers(
        self,
    ) -> None:
        base_req = OperatorReenrollRequest(
            identity=IDENTITY,
            head_sha=HEAD_SHA,
            operator_login="repo-admin",
            association="OWNER",
            reason="Marker accidentally deleted by repo cleanup bot.",
            enrollment_witness_present=True,
        )

        # Gate 1: Non-maintainer refused
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, association="COLLABORATOR", permission="write"),
                ledger_status="missing",
            ).reason_code,
            "reenroll-unauthorized-actor",
        )
        # Gate 2: Draft or closed PR refused
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, pr_draft=True), ledger_status="missing"
            ).reason_code,
            "reenroll-pr-not-ready-open",
        )
        # Gate 3: Uncertain write / active reservation / active obligations / quarantine refused!
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, has_uncertain_write=True), ledger_status="missing"
            ).reason_code,
            "reenroll-blocked-by-uncertain-write",
        )
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, has_active_reservation=True), ledger_status="missing"
            ).reason_code,
            "reenroll-blocked-by-active-reservation",
        )
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, active_blocking_obligations=2),
                ledger_status="missing",
            ).reason_code,
            "reenroll-blocked-by-active-obligations",
        )
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, quarantine_lineage_present=True),
                ledger_status="expired",
            ).reason_code,
            "reenroll-blocked-by-quarantine-or-queue",
        )
        # Gate 4: Live, conflict, integrity-failed, or witnessless missing refused!
        self.assertEqual(
            evaluate_operator_reenroll(base_req, ledger_status="ok").reason_code,
            "reenroll-refused-live-marker",
        )
        self.assertEqual(
            evaluate_operator_reenroll(base_req, ledger_status="conflict").reason_code,
            "reenroll-refused-conflict",
        )
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, enrollment_witness_present=False),
                ledger_status="missing",
            ).reason_code,
            "reenroll-missing-enrollment-witness",
        )

        # End-to-end Recovery A: Witness-backed deleted marker restored via GitHubIssueCommentSessionLedger!
        posted: dict[str, str | None] = {"body": None}
        calls: list[str] = []

        def deleted_marker_opener(request, timeout):
            del timeout
            calls.append(request.method)
            if request.method == "POST":
                posted["body"] = json.loads(request.data.decode("utf-8"))["body"]
                return FakeHTTPResponse(
                    json.dumps({"id": 501, "body": posted["body"]}).encode("utf-8"),
                    status=201,
                )
            comments = (
                []
                if posted["body"] is None
                else [
                    {
                        "id": 501,
                        "body": posted["body"],
                        "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                    }
                ]
            )
            return FakeHTTPResponse(json.dumps(comments).encode("utf-8"), status=200)

        http = GitHubHttp(
            api_url="https://api.github.test", opener=deleted_marker_opener
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="token", app_slug="reviewsensei[bot]"
        )
        # Refused if active_blocking_obligations > 0 (zero POSTs!):
        with self.assertRaisesRegex(
            ReviewInputError, "reenroll-blocked-by-active-obligations"
        ):
            execute_restricted_operator_reenroll(
                ledger,
                replace(base_req, active_blocking_obligations=1),
                now=FIXED_NOW,
            )
        self.assertNotIn("POST", calls)

        # Eligible once obligations are resolved:
        restored, receipt = execute_restricted_operator_reenroll(
            ledger, base_req, now=FIXED_NOW
        )
        self.assertEqual(restored.generation, 0)
        self.assertEqual(receipt.previous_status, "missing")
        self.assertEqual(receipt.restored_record_sha256, restored.record_sha256)
        self.assertEqual(ledger.load(IDENTITY, now=FIXED_NOW).status, "ok")

        # End-to-end Recovery B: Expired marker rewritten in-place via PATCH!
        expired_record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW - timedelta(days=31),
            ttl=timedelta(days=30),
        )
        state = {
            "body": render_session_comment(
                repository_id=99, pull_request=136, record=expired_record
            )
        }

        def expired_marker_opener(request, timeout):
            del timeout
            if request.method == "PATCH":
                state["body"] = json.loads(request.data.decode("utf-8"))["body"]
                return FakeHTTPResponse(
                    json.dumps({"id": 77, "body": state["body"]}).encode("utf-8"),
                    status=200,
                )
            if "/pulls/136" in request.full_url:
                return FakeHTTPResponse(
                    json.dumps(
                        {
                            "number": 136,
                            "state": "open",
                            "draft": False,
                            "head": {"sha": HEAD_SHA},
                            "base": {"repo": {"id": 99}},
                        }
                    ).encode("utf-8"),
                    status=200,
                )
            return FakeHTTPResponse(
                json.dumps(
                    [
                        {
                            "id": 77,
                            "body": state["body"],
                            "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                        }
                    ]
                ).encode("utf-8"),
                status=200,
            )

        http_exp = GitHubHttp(
            api_url="https://api.github.test", opener=expired_marker_opener
        )
        ledger_exp = GitHubIssueCommentSessionLedger(
            http_exp,
            token="token",
            app_slug="reviewsensei[bot]",
        )
        restored_exp, receipt_exp = execute_restricted_operator_reenroll(
            ledger_exp,
            replace(base_req, enrollment_witness_present=False),
            now=FIXED_NOW,
        )
        self.assertEqual(receipt_exp.previous_status, "expired")
        self.assertEqual(restored_exp.record_sha256, receipt_exp.restored_record_sha256)
        self.assertEqual(ledger_exp.load(IDENTITY, now=FIXED_NOW).status, "ok")

    def test_validation_error_boundaries(self) -> None:
        with self.assertRaises(ReviewInputError):
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id=" ",
                present=True,
            )
        with self.assertRaises(ReviewInputError):
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="1",
                present=True,
                duplicate_count=-1,
            )
        with self.assertRaises(ReviewInputError):
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="1",
                present=True,
                author_user_id=0,
            )
        with self.assertRaises(ReviewInputError):
            ArtifactEvaluationDecision(
                artifact_kind="native_review_summary",
                artifact_class="projection",
                verdict="accepted",
                reason_code="ok",
                authority_intact=True,
                obligations_preserved=True,
                charges_held=False,
                approval_permitted=True,
                auto_recreate=True,
                recovery_routes=("none-needed",),
            )
        with self.assertRaises(ReviewInputError):
            ArtifactEvaluationDecision(
                artifact_kind="native_review_summary",
                artifact_class="projection",
                verdict="accepted",
                reason_code="ok",
                authority_intact=True,
                obligations_preserved=False,
                charges_held=False,
                approval_permitted=True,
                auto_recreate=False,
                recovery_routes=("none-needed",),
            )
        with self.assertRaises(ReviewInputError):
            evaluate_observed_artifact(
                ObservedGitHubArtifact(
                    artifact_kind="session_ledger_marker",
                    artifact_id="1",
                    present=True,
                    raw_body=f"{SESSION_MARKER_PREFIX} bad -->",
                ),
                identity=None,
            )
        with self.assertRaises(ReviewInputError):
            verify_archive_chain_integrity((), {})
        http, _ = make_http([json_response([])])
        self.assertIsNotNone(http)


if __name__ == "__main__":
    unittest.main()
