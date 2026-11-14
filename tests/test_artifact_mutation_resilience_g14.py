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
                    duplicate_count=0,
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
                duplicate_count=0,
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
                duplicate_count=0,
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
                    duplicate_count=0,
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
                duplicate_count=0,
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
            enrollment_witness_sha256=_sha("verified-enrollment-witness"),
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
                replace(
                    base_req,
                    enrollment_witness_present=False,
                    enrollment_witness_sha256=None,
                ),
                ledger_status="missing",
            ).reason_code,
            "reenroll-missing-enrollment-witness",
        )
        self.assertEqual(
            evaluate_operator_reenroll(
                replace(base_req, enrollment_witness_sha256=None),
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
            replace(
                base_req,
                enrollment_witness_present=False,
                enrollment_witness_sha256=None,
            ),
            now=FIXED_NOW,
        )
        self.assertEqual(receipt_exp.previous_status, "expired")
        self.assertEqual(restored_exp.record_sha256, receipt_exp.restored_record_sha256)
        self.assertEqual(ledger_exp.load(IDENTITY, now=FIXED_NOW).status, "ok")

    def test_evidence_missing_expected_digest_duplicate_projection_and_inflight_ledger_guards(
        self,
    ) -> None:
        # 1) RS-80F54E: evidence artifacts without expected_sha256 fail closed!
        for ev_kind, expected_code in (
            ("owned_readback_receipt", "readback-evidence-corrupted"),
            ("archive_chain_part", "archive-part-corrupted"),
        ):
            missing_expected = evaluate_observed_artifact(
                ObservedGitHubArtifact(
                    artifact_kind=ev_kind,
                    artifact_id="ev-1",
                    present=True,
                    expected_sha256=None,
                    observed_sha256=_sha("some-body"),
                )
            )
            self.assertEqual(missing_expected.verdict, "refused")
            self.assertEqual(missing_expected.reason_code, expected_code)
            self.assertFalse(missing_expected.authority_intact)

        ok_receipt = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="owned_readback_receipt",
                artifact_id="ev-ok",
                present=True,
                expected_sha256=_sha("match"),
                observed_sha256=_sha("match"),
            )
        )
        self.assertEqual(ok_receipt.verdict, "accepted")
        self.assertEqual(ok_receipt.reason_code, "evidence-verified")

        # 2) RS-86E37A: contradictory present=False with nonzero duplicate_count fails closed!
        with self.assertRaisesRegex(
            ReviewInputError, "absent artifact requires duplicate_count == 0"
        ):
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-bad",
                present=False,
                duplicate_count=1,
            )
        with self.assertRaisesRegex(
            ReviewInputError, "present artifact requires duplicate_count >= 1"
        ):
            ObservedGitHubArtifact(
                artifact_kind="session_ledger_marker",
                artifact_id="c-bad-0",
                present=True,
                duplicate_count=0,
            )
        with self.assertRaisesRegex(
            ReviewInputError, "enrollment_witness_sha256 requires"
        ):
            OperatorReenrollRequest(
                identity=IDENTITY,
                head_sha=HEAD_SHA,
                operator_login="repo-admin",
                association="OWNER",
                reason="Contradictory witness fields.",
                enrollment_witness_present=False,
                enrollment_witness_sha256=_sha("unexpected"),
            )

        # 3) RS-304AC2: duplicated projection comment is treated as non-authoritative mutation:
        dup_proj = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="native_review_summary",
                artifact_id="rev-dup",
                present=True,
                duplicate_count=2,
                expected_sha256=_sha("match"),
                observed_sha256=_sha("match"),
            )
        )
        self.assertEqual(dup_proj.verdict, "ignored_projection_mutation")

        # 4) RS-2D07CD: expired ledger carrying an in-flight publication_pending
        #    transaction blocks restricted operator reenroll!
        from review_sensei.models import ReviewTransaction

        pending_tx = ReviewTransaction(
            transaction_id=ReviewTransaction.derive_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest=_sha("pol"),
                configuration_digest=_sha("cfg"),
                evidence_digest=_sha("ev"),
                reservation_id=_sha("resv"),
            ),
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            policy_digest=_sha("pol"),
            configuration_digest=_sha("cfg"),
            evidence_digest=_sha("ev"),
            reservation_id=_sha("resv"),
            generation=1,
            phase="publication_pending",
            result_sha256=_sha("res"),
        )
        expired_pending_rec = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW - timedelta(days=31),
            ttl=timedelta(days=30),
            completed_initial_reviews=1,
            generation=1,
            last_committed_reservation_id=_sha("resv"),
            transaction=pending_tx,
        )
        pending_body = render_session_comment(
            repository_id=99, pull_request=136, record=expired_pending_rec
        )

        def pending_opener(request, timeout):
            del request, timeout
            return FakeHTTPResponse(
                json.dumps(
                    [
                        {
                            "id": 88,
                            "body": pending_body,
                            "user": {"login": "reviewsensei[bot]", "type": "Bot"},
                        }
                    ]
                ).encode("utf-8"),
                status=200,
            )

        ledger_pending = GitHubIssueCommentSessionLedger(
            GitHubHttp(api_url="https://api.github.test", opener=pending_opener),
            token="token",
            app_slug="reviewsensei[bot]",
        )
        with self.assertRaisesRegex(
            ReviewInputError, "reenroll-blocked-by-uncertain-write"
        ):
            execute_restricted_operator_reenroll(
                ledger_pending,
                OperatorReenrollRequest(
                    identity=IDENTITY,
                    head_sha=HEAD_SHA,
                    operator_login="repo-admin",
                    association="OWNER",
                    reason="Attempt reenroll while publication_pending.",
                ),
                now=FIXED_NOW,
            )

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


class _BrokerSessionStub:
    def __init__(self, token: str, state: str) -> None:
        self.token = token
        self.state = state
        self.reservation_owner = None


class _ArtifactFakeBroker:
    def __init__(self, *, session_state: str = "known") -> None:
        self.session_state = session_state

    def request_oidc_token(self) -> str:
        return "oidc-artifact-test"

    def exchange(self, _oidc: str, *, capability: str) -> str:
        return f"tok-{capability}"

    def open_session(
        self,
        _oidc: str,
        *,
        repository_id: int,
        pull_request: int,
        head_sha: str,
    ) -> _BrokerSessionStub:
        del repository_id, pull_request, head_sha
        return _BrokerSessionStub(token="tok-session", state=self.session_state)


class _ArtifactGitHubHost:
    """Stateful fake GitHub host exercising real ReviewPublisher & GitHubApplication."""

    def __init__(
        self,
        *,
        head_sha: str = HEAD_SHA,
        base_sha: str = BASE_SHA,
        app_slug: str = "reviewsensei[bot]",
    ) -> None:
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.app_slug = app_slug
        self.reviews: list[dict[str, object]] = []
        self.threads: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []
        self.issue_comments: list[dict[str, object]] = []
        self.drop_response_after_review_post = False
        self.tamper_during_response_loss = False
        self.fail_before_review_post = False
        self.calls: list[tuple[str, str, object]] = []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self._open)

    def _resp(self, payload: object, status: int = 200):
        return json_response(payload, status)

    def _open(self, request, timeout=None):
        del timeout
        from urllib.parse import urlparse

        from review_sensei_github.errors import GitHubHTTPTransientError

        parsed = urlparse(request.full_url)
        path = parsed.path
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append((request.method, path, body))

        if request.method == "GET":
            if path.endswith("/pulls/1"):
                return self._resp(
                    {
                        "state": "open",
                        "draft": False,
                        "changed_files": 1,
                        "user": {"login": "dev-author", "type": "User"},
                        "head": {
                            "sha": self.head_sha,
                            "ref": "feature",
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": self.base_sha,
                            "ref": "main",
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path.endswith("/pulls/1/reviews"):
                return self._resp(list(self.reviews))
            if "/reviews/" in path and path.endswith("/comments"):
                rid = int(path.split("/reviews/")[1].split("/")[0])
                matched = [
                    c
                    for t in self.threads
                    for c in t["comments"]["nodes"]  # type: ignore[index]
                    if c.get("pull_request_review_id") == rid
                ]
                return self._resp(matched)
            if "/reviews/" in path and not path.endswith("/comments"):
                rid = int(path.rsplit("/", 1)[-1])
                return self._resp(next((r for r in self.reviews if r["id"] == rid), {}))
            if path.endswith("/pulls/1/comments"):
                return self._resp([])
            if "/pulls/comments/" in path:
                cid = int(path.rsplit("/", 1)[-1])
                for t in self.threads:
                    for c in t["comments"]["nodes"]:  # type: ignore[index]
                        if c.get("databaseId") == cid:
                            return self._resp(
                                {
                                    "id": cid,
                                    "body": c["body"],
                                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                                    "user": {
                                        "id": 9001,
                                        "login": self.app_slug,
                                        "type": "Bot",
                                    },
                                }
                            )
                return self._resp({"message": "Not Found"}, 404)
            if path.endswith("/issues/1/comments"):
                return self._resp(list(self.issue_comments))
            if path.endswith("/check-runs"):
                matching = [
                    r for r in self.check_runs if r.get("head_sha") == self.head_sha
                ]
                return self._resp({"check_runs": matching})
            if "/rules/branches/" in path:
                return self._resp([])
            if path.endswith("/protection"):
                return self._resp({"message": "Not Found"}, 404)
            if path == "/user" or path.startswith("/users/"):
                return self._resp({"id": 9001, "login": self.app_slug, "type": "Bot"})
            if "/collaborators/" in path and path.endswith("/permission"):
                return self._resp({"permission": "maintain"})

        if request.method == "POST":
            if path.endswith("/pulls/1/reviews"):
                if self.fail_before_review_post:
                    self.fail_before_review_post = False
                    raise GitHubHTTPTransientError(
                        "simulated transport loss before GitHub stored review"
                    )
                assert isinstance(body, dict)
                review_id = 100 + len(self.reviews)
                state_map = {
                    "APPROVE": "APPROVED",
                    "REQUEST_CHANGES": "CHANGES_REQUESTED",
                    "COMMENT": "COMMENTED",
                }
                ev = str(body.get("event", "COMMENT"))
                stored_body = str(body.get("body", ""))
                edited_flag = False
                if (
                    self.drop_response_after_review_post
                    and self.tamper_during_response_loss
                ):
                    stored_body = stored_body + "\nHuman edited during response loss."
                    edited_flag = True
                record: dict[str, object] = {
                    "id": review_id,
                    "commit_id": body["commit_id"],
                    "state": state_map.get(ev, "COMMENTED"),
                    "body": stored_body,
                    "edited_by_human": edited_flag,
                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                    "user": {"id": 9001, "login": self.app_slug, "type": "Bot"},
                }
                self.reviews.append(record)
                for idx, comment in enumerate(body.get("comments") or ()):
                    thread_id = f"PRRT_{review_id}_{idx}"
                    self.threads.append(
                        {
                            "id": thread_id,
                            "isResolved": False,
                            "path": comment["path"],
                            "line": comment["line"],
                            "comments": {
                                "nodes": [
                                    {
                                        "id": f"PRRC_{review_id}_{idx}",
                                        "databaseId": review_id * 100 + idx,
                                        "pull_request_review_id": review_id,
                                        "pullRequestReview": {"databaseId": review_id},
                                        "commit": {"oid": body["commit_id"]},
                                        "body": comment["body"],
                                        "edited_by_human": False,
                                        "author": {
                                            "login": self.app_slug,
                                            "__typename": "Bot",
                                            "databaseId": 9001,
                                        },
                                    }
                                ]
                            },
                        }
                    )
                if self.drop_response_after_review_post:
                    self.drop_response_after_review_post = False
                    self.tamper_during_response_loss = False
                    raise GitHubHTTPTransientError(
                        "simulated response lost after GitHub accepted review POST"
                    )
                return self._resp(record, 200)
            if path.endswith("/check-runs"):
                assert isinstance(body, dict)
                run_id = 500 + len(self.check_runs)
                run: dict[str, object] = {
                    "id": run_id,
                    "name": body["name"],
                    "head_sha": body["head_sha"],
                    "status": body.get("status"),
                    "conclusion": body.get("conclusion"),
                    "output": body.get("output", {}),
                    "app": {"slug": "reviewsensei"},
                }
                self.check_runs.append(run)
                return self._resp(run, 201)
            if path == "/graphql":
                assert isinstance(body, dict)
                query = str(body.get("query", ""))
                variables = body.get("variables") or {}
                if "resolveReviewThread" in query:
                    tid = (variables.get("input") or {}).get(
                        "threadId"
                    ) or variables.get("threadId")
                    for thread in self.threads:
                        if thread["id"] == tid:
                            thread["isResolved"] = True
                            return self._resp(
                                {
                                    "data": {
                                        "resolveReviewThread": {
                                            "thread": {"id": tid, "isResolved": True}
                                        }
                                    }
                                }
                            )
                    return self._resp(
                        {"errors": [{"message": "thread not found"}]}, 200
                    )
                if body.get("operationName") == "ResolutionReadback":
                    tid = variables.get("id")
                    for thread in self.threads:
                        if thread["id"] == tid:
                            return self._resp(
                                {
                                    "data": {
                                        "node": {
                                            "id": tid,
                                            "isResolved": thread["isResolved"],
                                        }
                                    }
                                }
                            )
                    return self._resp({"data": {"node": None}}, 200)
                return self._resp(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": list(self.threads),
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            if path.endswith("/issues/1/comments"):
                assert isinstance(body, dict)
                cid = 800 + len(self.issue_comments)
                item: dict[str, object] = {
                    "id": cid,
                    "body": body["body"],
                    "edited_by_human": False,
                    "user": {"id": 9001, "login": self.app_slug, "type": "Bot"},
                }
                self.issue_comments.append(item)
                return self._resp(item, 201)

        if request.method == "PUT" and "/reviews/" in path:
            assert isinstance(body, dict)
            rid = int(path.rsplit("/", 1)[-1])
            for rev in self.reviews:
                if rev["id"] == rid:
                    rev["body"] = body["body"]
                    return self._resp(rev, 200)

        if request.method == "PATCH":
            if "/check-runs/" in path:
                assert isinstance(body, dict)
                cid = int(path.rsplit("/", 1)[-1])
                for run in self.check_runs:
                    if run["id"] == cid:
                        run.update(body)
                        return self._resp(run, 200)
            if "/issues/comments/" in path:
                assert isinstance(body, dict)
                cid = int(path.rsplit("/", 1)[-1])
                for cmt in self.issue_comments:
                    if cmt["id"] == cid:
                        cmt["body"] = body["body"]
                        return self._resp(cmt, 200)

        raise AssertionError(f"Unhandled request: {request.method} {path}")


class OrdinaryPublisherArtifactResilienceTests(unittest.TestCase):
    """End-to-end ordinary ReviewPublisher & GitHubApplication G14 tests."""

    DIFF_1 = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -1,1 +1,5 @@\n"
        "-def check_role(role): return False\n"
        "+def check_role(role):\n"
        "+    if role == 'admin':\n"
        "+        return True\n"
        "+    return True\n"
        "+# trailing\n"
    )
    DIFF_CLEAN = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -1,5 +1,5 @@\n"
        " def check_role(role):\n"
        "     if role == 'admin':\n"
        "         return True\n"
        "-    return True\n"
        "+    return False\n"
        " # trailing\n"
    )

    @staticmethod
    def _make_app(
        host: _ArtifactGitHubHost,
        *,
        github_session_ledger: bool = False,
        local_ledger=None,
    ):
        from review_sensei_github import (
            ConversationPublisher,
            GitHubApplication,
            GitHubWriteOptions,
            LearningPRPublisher,
            ReviewPublisher,
        )

        publisher = ReviewPublisher(http=host.http)
        app = GitHubApplication(
            http=host.http,
            broker=_ArtifactFakeBroker(),  # type: ignore[arg-type]
            reviewer=publisher,
            learner=LearningPRPublisher(http=host.http),
            replier=ConversationPublisher(http=host.http),
            session_ledger=local_ledger,
        )
        options = GitHubWriteOptions(
            auto_review=True,
            auto_approve=True,
            github_writes=True,
            github_session_ledger=github_session_ledger,
            operation_entry="disabled",
            max_active_findings=3,
        )
        return app, publisher, options

    @staticmethod
    def _blocking_result() -> tuple[ReviewResult, tuple]:
        from review_sensei.convergence import derive_blocker_candidate

        comment = ReviewComment(
            path="src/auth.py",
            line=3,
            body="Unauthenticated role fallback grants admin access.",
            blocking=True,
            effective_blocking=True,
            severity="critical",
            symbol="check_role",
            defect_kind="authz",
        )
        result = ReviewResult(
            summary="Found 1 critical authz blocker.",
            comments=(comment,),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/auth.py", "reviewed"),),
                enumerated_paths=("src/auth.py",),
                enumeration_complete=True,
            ),
        )
        blockers = (
            derive_blocker_candidate(
                comment,
                on_changed_path=True,
                evidence_locations_validated=True,
                has_failure_condition=True,
                has_actionable_remedy=True,
                has_specific_violation=True,
                named_mandatory_rule="SEC-AUTH-01",
                has_required_contract=True,
            ),
        )
        return result, blockers

    @staticmethod
    def _cache_key(head_sha: str):
        from review_sensei.context import ReviewContextCacheKey

        return ReviewContextCacheKey(
            repository="owner/repo",
            pull_request=1,
            base_sha=BASE_SHA,
            head_sha=head_sha,
            engine="fixture",
            model="fixture-v1",
            profile="default",
            stage_digest=_sha("stage"),
            context_digest=_sha("ctx"),
            learning_digest=_sha("learn"),
        )

    @staticmethod
    def _clean_result() -> ReviewResult:
        return ReviewResult(
            summary="All blocking concerns fixed.",
            comments=(),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/auth.py", "reviewed"),),
                enumerated_paths=("src/auth.py",),
                enumeration_complete=True,
            ),
        )

    def test_ordinary_publisher_projection_mutations_and_deletions_never_clear_obligations_or_recreate_threads(
        self,
    ) -> None:
        from review_sensei.convergence import ReviewConvergencePolicy

        host = _ArtifactGitHubHost(head_sha=HEAD_SHA)
        app, _pub, options = self._make_app(host)
        policy = ReviewConvergencePolicy(mode="strict")
        res1, blockers1 = self._blocking_result()

        pub1 = app.publish_review(
            options=options,
            oidc_token="oidc-1",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=res1,
            diff=self.DIFF_1,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
            blocker_candidates=blockers1,
            current_key=self._cache_key(HEAD_SHA),
        )
        self.assertEqual(pub1.status, "published")
        self.assertEqual(pub1.diagnostic, "required_fixes_open")
        self.assertEqual(len(host.threads), 1)
        self.assertEqual(len(host.reviews), 1)

        # Human edits ReviewSensei summary prose, injects a foreign "APPROVED" review,
        # edits the open inline thread in Round 2a, and tampers check text + reaction.
        host.reviews[0]["edited_by_human"] = True
        host.reviews[0]["body"] = str(host.reviews[0]["body"]) + "\nLGTM waived!"
        host.reviews.append(
            {
                "id": 999,
                "commit_id": HEAD_SHA_2,
                "state": "APPROVED",
                "body": "Human reviewer approves!",
                "edited_by_human": True,
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                "user": {"id": 4242, "login": "human-maintainer", "type": "User"},
            }
        )
        host.threads[0]["comments"]["nodes"][0]["edited_by_human"] = True  # type: ignore[index]
        host.threads[0]["comments"]["nodes"][0]["body"] = "Edited inline prose: fixed!"  # type: ignore[index]

        # Push HEAD_SHA_2 with an advisory note on the added line 4 (still NOT providing confirmed_fixes or override!):
        advisory = ReviewComment(
            path="src/auth.py",
            line=4,
            body="Consider adding audit log entry on role rejection.",
            blocking=False,
            effective_blocking=False,
            severity="low",
            symbol="check_role",
            defect_kind="observability",
        )
        res_advisory = ReviewResult(
            summary="Advisory follow-up while critical authz blocker remains open.",
            comments=(advisory,),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/auth.py", "reviewed"),),
                enumerated_paths=("src/auth.py",),
                enumeration_complete=True,
            ),
        )
        host.head_sha = HEAD_SHA_2
        pub2 = app.publish_review(
            options=options,
            oidc_token="oidc-2",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA_2,
            base_branch="main",
            base_sha=BASE_SHA,
            result=res_advisory,
            diff=self.DIFF_CLEAN,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
            observed_artifacts=(
                ObservedGitHubArtifact(
                    artifact_kind="check_run_text",
                    artifact_id="check-500",
                    present=True,
                    edited_by_human=True,
                ),
                ObservedGitHubArtifact(
                    artifact_kind="comment_reaction",
                    artifact_id="react-1",
                    present=False,
                    duplicate_count=0,
                ),
            ),
        )
        # Obligation stays open; no APPROVE emitted; projection mutations ignored!
        self.assertEqual(pub2.status, "published")
        self.assertEqual(pub2.diagnostic, "required_fixes_open")
        app_reviews = [
            r
            for r in host.reviews
            if r["user"]["login"] == "reviewsensei[bot]"  # type: ignore[index]
        ]
        self.assertTrue(all(r["state"] != "APPROVED" for r in app_reviews))

        # Now human DELETES the inline review comment threads on GitHub completely!
        host.threads.clear()
        head_3 = "3" * 40
        host.head_sha = head_3
        pub3 = app.publish_review(
            options=options,
            oidc_token="oidc-3",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=head_3,
            base_branch="main",
            base_sha=BASE_SHA,
            result=self._clean_result(),
            diff=self.DIFF_CLEAN,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
        )
        # Deleted original blocking inline thread is NOT auto-recreated (0 threads) and obligation remains open!
        self.assertEqual(pub3.status, "published")
        self.assertEqual(pub3.diagnostic, "required_fixes_open")
        self.assertEqual(len(host.threads), 0)

        decisions = app.artifact_decisions(repository_id=1, pull_request=1)
        projection_decisions = [
            d for d in decisions if d.artifact_class == "projection"
        ]
        self.assertTrue(
            any(
                d.artifact_kind == "inline_review_comment"
                and d.verdict == "ignored_projection_mutation"
                and not d.auto_recreate
                for d in projection_decisions
            )
        )
        self.assertTrue(
            any(
                d.artifact_kind == "native_review_summary"
                and d.verdict == "ignored_projection_mutation"
                and not d.auto_recreate
                for d in projection_decisions
            )
        )
        self.assertTrue(
            any(
                d.artifact_kind == "check_run_text"
                and d.verdict == "ignored_projection_mutation"
                for d in projection_decisions
            )
        )

    def test_ordinary_publisher_authority_deletion_tamper_recovery_and_archive_fail_closed(
        self,
    ) -> None:
        from review_sensei_github.errors import GitHubPublicationError
        from review_sensei_github.publication import APPROVAL_ELIGIBILITY_MARKER_PREFIX

        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.outcomes import RecoveryArtifact
        from tests.test_review_transaction import CONFIGURATION

        host = _ArtifactGitHubHost(head_sha=HEAD_SHA)
        app, _pub, options = self._make_app(host, github_session_ledger=True)
        policy = ReviewConvergencePolicy(mode="strict")
        res1, blockers1 = self._blocking_result()

        from datetime import datetime, timezone

        live_now = datetime.now(timezone.utc)
        # Enroll PR session marker in Round 1 via ordinary GitHubIssueCommentSessionLedger:
        ledger_enroll = GitHubIssueCommentSessionLedger(
            host.http, token="tok-session", app_slug="reviewsensei[bot]"
        )
        ledger_enroll.initialize(
            SessionIdentity(repository="owner/repo", pull_request=1, repository_id=1),
            now=live_now,
        )
        self.assertEqual(len(host.issue_comments), 1)
        enrolled_comment_snapshot = dict(host.issue_comments[0])

        pub1 = app.publish_review(
            options=options,
            oidc_token="oidc-1",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=res1,
            configuration_context=CONFIGURATION,
            diff=self.DIFF_1,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
            blocker_candidates=blockers1,
            current_key=self._cache_key(HEAD_SHA),
        )
        self.assertEqual(pub1.status, "published")
        self.assertEqual(len(host.issue_comments), 1)
        latest_marker_snapshot = dict(host.issue_comments[0])

        # 1) Deleted session_ledger_marker on Round 2 fails closed with ZERO auto-recreation!
        host.issue_comments.clear()
        host.head_sha = HEAD_SHA_2
        reviews_before = len(host.reviews)
        with self.assertRaisesRegex(
            GitHubPublicationError, "session ledger marker is missing"
        ):
            app.publish_review(
                options=options,
                oidc_token="oidc-2",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
        self.assertEqual(len(host.issue_comments), 0)
        self.assertEqual(len(host.reviews), reviews_before)
        self.assertEqual(
            app.artifact_decisions(repository_id=1, pull_request=1)[-1].reason_code,
            "authority-marker-deleted",
        )
        self.assertFalse(
            app.artifact_decisions(repository_id=1, pull_request=1)[-1].auto_recreate
        )

        # 2) Human-edited session_ledger_marker fails closed!
        edited_marker = dict(latest_marker_snapshot)
        edited_marker["edited_by_human"] = True
        host.issue_comments[:] = [edited_marker]
        with self.assertRaisesRegex(
            GitHubPublicationError, "session ledger marker was edited by a human"
        ):
            app.publish_review(
                options=options,
                oidc_token="oidc-2b",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
        self.assertTrue(
            any(
                d.artifact_kind == "session_ledger_marker"
                and d.reason_code == "authority-marker-tampered"
                for d in app.artifact_decisions(repository_id=1, pull_request=1)
            )
        )

        # 3) Duplicate session_ledger_marker fails closed!
        dup1 = dict(enrolled_comment_snapshot)
        dup2 = dict(latest_marker_snapshot)
        dup2["id"] = 899
        host.issue_comments[:] = [dup1, dup2]
        with self.assertRaises(Exception):
            app.publish_review(
                options=options,
                oidc_token="oidc-2c",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
        self.assertTrue(
            any(
                d.artifact_kind == "session_ledger_marker"
                and d.reason_code == "authority-duplicate-conflict"
                for d in app.artifact_decisions(repository_id=1, pull_request=1)
            )
        )

        # Restore valid marker for archive & recovery tests:
        host.issue_comments[:] = [dict(latest_marker_snapshot)]

        # 4) Corrupted or missing archive_chain on publish_review fails closed before writes!
        part0 = _sha("archive-part-0")
        part1 = _sha("archive-part-1")
        expected_chain = (
            ArchiveChainPartRef(comment_id=900, expected_sha256=part0),
            ArchiveChainPartRef(comment_id=901, expected_sha256=part1),
        )
        with self.assertRaisesRegex(
            GitHubPublicationError,
            "archive chain integrity refused: archive-part-missing",
        ):
            app.publish_review(
                options=options,
                oidc_token="oidc-arch-missing",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                archive_chain=(
                    expected_chain,
                    {
                        900: ObservedGitHubArtifact(
                            artifact_kind="archive_chain_part",
                            artifact_id="900",
                            present=True,
                            expected_sha256=part0,
                            observed_sha256=part0,
                        )
                    },
                ),
            )

        with self.assertRaisesRegex(
            GitHubPublicationError,
            "archive chain integrity refused: archive-part-corrupted",
        ):
            app.publish_review(
                options=options,
                oidc_token="oidc-arch-tamper",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                archive_chain=(
                    expected_chain,
                    {
                        900: ObservedGitHubArtifact(
                            artifact_kind="archive_chain_part",
                            artifact_id="900",
                            present=True,
                            expected_sha256=part0,
                            observed_sha256=part0,
                        ),
                        901: ObservedGitHubArtifact(
                            artifact_kind="archive_chain_part",
                            artifact_id="901",
                            present=True,
                            expected_sha256=part1,
                            observed_sha256=_sha("tampered-part-1"),
                        ),
                    },
                ),
            )

        # 5) Missing or tampered RecoveryArtifact in app.recover_review fails closed!
        with self.assertRaisesRegex(ReviewInputError, "recovery artifact is missing"):
            app.recover_review(
                options=options,
                oidc_token="oidc-rec-none",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                artifact=None,
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                now=live_now,
            )
        self.assertEqual(
            app.artifact_decisions(repository_id=1, pull_request=1)[-1].reason_code,
            "retained-part-missing",
        )

        valid_recovery = RecoveryArtifact.create(
            repository="owner/repo",
            pull_request_number=1,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA_2,
            result=self._clean_result().to_dict(),
            created_at=live_now.isoformat(),
            expires_at=(live_now + timedelta(hours=2)).isoformat(),
        )
        with self.assertRaisesRegex(
            ReviewInputError,
            "retained recovery artifact refused: retained-part-corrupted",
        ):
            app.recover_review(
                options=options,
                oidc_token="oidc-rec-tamper",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                artifact=valid_recovery,
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                now=live_now,
                expected_result_sha256=_sha("different-expected-digest"),
            )

        # 6) Tampered APPROVAL_ELIGIBILITY_MARKER_PREFIX on native review fails closed!
        host.reviews.append(
            {
                "id": 777,
                "commit_id": HEAD_SHA_2,
                "state": "COMMENTED",
                "body": f"{APPROVAL_ELIGIBILITY_MARKER_PREFIX} {{corrupt-json -->",
                "edited_by_human": False,
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                "user": {"id": 9001, "login": "reviewsensei[bot]", "type": "Bot"},
            }
        )
        no_ledger_options = replace(options, github_session_ledger=False)
        with self.assertRaises(GitHubPublicationError):
            app.publish_review(
                options=no_ledger_options,
                oidc_token="oidc-bad-elig",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
            )
        self.assertTrue(
            any(
                d.artifact_kind == "session_ledger_marker"
                and d.reason_code == "authority-marker-tampered"
                for d in app.artifact_decisions(repository_id=1, pull_request=1)
            )
        )

    def test_ordinary_publisher_response_loss_owned_readback_vs_uncertain_hold_and_restricted_reenroll(
        self,
    ) -> None:
        from review_sensei_github.errors import (
            GitHubPublicationError,
            GitHubPublicationTransientError,
        )

        from review_sensei.convergence import ReviewConvergencePolicy

        # Case A: Response lost AFTER GitHub stored exact unedited App-owned review ->
        # reconciles once via owned readback with zero duplicate review POSTs!
        host_ok = _ArtifactGitHubHost(head_sha=HEAD_SHA)
        app_ok, _pub_ok, options_ok = self._make_app(host_ok)
        host_ok.drop_response_after_review_post = True
        res_ok = app_ok.publish_review(
            options=options_ok,
            oidc_token="oidc-rl-ok",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=self._clean_result(),
            diff=self.DIFF_CLEAN,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(res_ok.status, "already_published")
        self.assertIsNone(
            app_ok.response_loss_hold(
                repository_id=1, pull_request=1, head_sha=HEAD_SHA
            )
        )
        self.assertTrue(
            any(
                d.verdict == "reconciled"
                and d.reason_code == "reconciled-via-owned-readback"
                for d in app_ok.artifact_decisions(repository_id=1, pull_request=1)
            )
        )

        # Case B: Response lost AND human edited review during loss (or review missing) ->
        # holds uncertain, withholds approval, refuses blind retry, blocks reenroll!
        from datetime import datetime, timezone

        live_now = datetime.now(timezone.utc)
        host_hold = _ArtifactGitHubHost(head_sha=HEAD_SHA)
        ledger_hold = GitHubIssueCommentSessionLedger(
            host_hold.http, token="tok-hold", app_slug="reviewsensei[bot]"
        )
        enrolled_rec = ledger_hold.initialize(
            SessionIdentity(repository="owner/repo", pull_request=1, repository_id=1),
            now=live_now,
        )
        app_hold, pub_hold, options_hold = self._make_app(host_hold)
        from review_sensei_github import (
            ConversationPublisher,
            GitHubApplication,
            LearningPRPublisher,
        )

        cmd_app_hold = GitHubApplication(
            http=host_hold.http,
            broker=_ArtifactFakeBroker(),  # type: ignore[arg-type]
            reviewer=pub_hold,
            learner=LearningPRPublisher(http=host_hold.http),
            replier=ConversationPublisher(http=host_hold.http),
            session_ledger=ledger_hold,
        )
        policy = ReviewConvergencePolicy(mode="strict")
        res1, blockers1 = self._blocking_result()

        # Publish Round 1 blocking finding cleanly:
        r1 = app_hold.publish_review(
            options=options_hold,
            oidc_token="oidc-r1",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=res1,
            diff=self.DIFF_1,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
            blocker_candidates=blockers1,
            current_key=self._cache_key(HEAD_SHA),
        )
        self.assertEqual(r1.status, "published")

        # Maintainer command "reenroll" is refused while active blocking obligations > 0!
        cmd_blocked_ob = cmd_app_hold.apply_maintainer_command(
            options=options_hold,
            oidc_token="oidc-cmd-1",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA,
            body="@reviewsensei review reenroll",
            actor_login="repo-admin",
            association="OWNER",
            app_slug="reviewsensei[bot]",
            actor_permission="admin",
        )
        self.assertFalse(cmd_blocked_ob.applied)
        self.assertTrue(cmd_blocked_ob.operator_paused)
        self.assertEqual(
            cmd_blocked_ob.summary, "reenroll-blocked-by-active-obligations"
        )

        # Round 2 on HEAD_SHA_2 suffers response loss before GitHub stores the review:
        host_hold.head_sha = HEAD_SHA_2
        host_hold.fail_before_review_post = True
        with self.assertRaises(GitHubPublicationTransientError):
            app_hold.publish_review(
                options=options_hold,
                oidc_token="oidc-r2-loss",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                current_key=self._cache_key(HEAD_SHA_2),
            )
        hold_decision = app_hold.response_loss_hold(
            repository_id=1, pull_request=1, head_sha=HEAD_SHA_2
        )
        assert hold_decision is not None
        self.assertEqual(hold_decision.verdict, "held_uncertain")
        self.assertEqual(hold_decision.reason_code, "uncertain-after-response-loss")
        self.assertFalse(hold_decision.auto_recreate)
        self.assertTrue(hold_decision.charges_held)
        self.assertFalse(hold_decision.approval_permitted)

        # Blind retry is REFUSED fail-closed with zero extra review POSTs!
        reviews_after_loss = len(host_hold.reviews)
        with self.assertRaisesRegex(
            GitHubPublicationError,
            "review publication held uncertain after response loss; reconcile before retrying",
        ):
            app_hold.publish_review(
                options=options_hold,
                oidc_token="oidc-r2-blind-retry",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
        self.assertEqual(len(host_hold.reviews), reviews_after_loss)

        # Finalizer also withholds approval while response-loss hold is active!
        elig_head2 = pub_hold._eligibility_with_carried(
            self._clean_result(),
            head_sha=HEAD_SHA_2,
            base_sha=BASE_SHA,
            enabled=True,
            app_authored=False,
            qualification="not-required",
        )
        fin_held = pub_hold.finalizer.finalize(
            token="tok-review",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD_SHA_2,
            app_slug="reviewsensei[bot]",
            eligibility=elig_head2,
        )
        self.assertEqual(fin_held.status, "approval_withheld")

        # Reconcile response-loss hold with explicit absent proof, reconcile uncertain slot,
        # prove fix on Round 2, then verify witness-backed execute_operator_reenroll succeeds!
        cleared = app_hold.reconcile_response_loss_hold(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA_2,
            absent_proof_sha256=_sha("absent-proof-head-2"),
        )
        self.assertEqual(cleared.verdict, "accepted")
        self.assertIsNone(
            app_hold.response_loss_hold(
                repository_id=1, pull_request=1, head_sha=HEAD_SHA_2
            )
        )

        from review_sensei.active_finding_window import ConfirmedFixEvidence

        wstate = app_hold.active_window_state(repository_id=1, pull_request=1)
        assert wstate is not None
        for slot in wstate.display_active_findings.slots:
            if slot.state == "uncertain":
                app_hold.reconcile_active_window_slot(
                    repository_id=1,
                    pull_request=1,
                    target=slot.target,
                    resolved_state="active",
                    proof_sha256=_sha("slot-active-after-loss"),
                )
        wstate = app_hold.active_window_state(repository_id=1, pull_request=1)
        assert wstate is not None
        ob = wstate.authoritative_decision_inventory.obligations[0]
        host_hold.threads[0]["isResolved"] = True
        r2_fixed = app_hold.publish_review(
            options=options_hold,
            oidc_token="oidc-r2-fixed",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA_2,
            base_branch="main",
            base_sha=BASE_SHA,
            result=self._clean_result(),
            diff=self.DIFF_CLEAN,
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
            current_key=self._cache_key(HEAD_SHA_2),
            confirmed_fixes=(
                ConfirmedFixEvidence(
                    obligation_id=ob.obligation_id,
                    resolution_criterion=ob.resolution_criterion,
                    head_sha=HEAD_SHA_2,
                    evidence_sha256=_sha("role-fallback-fix-proof"),
                    verified_paths=("src/auth.py",),
                ),
            ),
        )
        self.assertEqual(r2_fixed.status, "published")
        self.assertTrue(any(r["state"] == "APPROVED" for r in host_hold.reviews))
        self.assertEqual(
            pub_hold.active_blocking_obligations_count(repository_id=1, pull_request=1),
            0,
        )

        # Delete issue comment marker; witness-backed execute_operator_reenroll now succeeds!
        host_hold.issue_comments.clear()
        restored_rec, receipt = app_hold.execute_operator_reenroll(
            ledger=ledger_hold,
            request=OperatorReenrollRequest(
                identity=SessionIdentity(
                    repository="owner/repo", pull_request=1, repository_id=1
                ),
                head_sha=HEAD_SHA_2,
                operator_login="repo-admin",
                association="OWNER",
                permission="admin",
                reason="Restore deleted session marker after obligations resolved.",
                enrollment_witness_present=True,
                enrollment_witness_sha256=enrolled_rec.record_sha256,
            ),
            now=live_now,
        )
        self.assertEqual(receipt.previous_status, "missing")
        self.assertEqual(restored_rec.record_sha256, receipt.restored_record_sha256)
        self.assertEqual(len(host_hold.issue_comments), 1)


if __name__ == "__main__":
    unittest.main()
