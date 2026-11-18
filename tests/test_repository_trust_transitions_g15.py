"""Qualification tests for repository and trust-boundary transitions (G15)."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import timedelta

from review_sensei_github import artifact_authority
from review_sensei_github.trust_transitions import (
    DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS,
    TRANSITION_PHASES,
    TRUST_TRANSITION_KINDS,
    AuthenticatedTrustMigration,
    AuthenticatedTrustSnapshot,
    TrustTransitionDecision,
    compute_transfer_attestation_sha256,
    detect_trust_transitions,
    evaluate_trust_boundary,
    verify_and_migrate_trust_boundary,
)

from review_sensei.active_finding_window import (
    AuthoritativeDecisionInventory,
    AuthorizedHumanOverride,
    ConfirmedFixEvidence,
    evaluate_active_window_round,
    obligation_from_comment,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.session import SessionIdentity, SessionRecord

try:
    from session_fixture_clock import SESSION_FIXTURE_NOW
except ModuleNotFoundError:
    from tests.session_fixture_clock import SESSION_FIXTURE_NOW

FIXED_NOW = SESSION_FIXTURE_NOW
BASE_SHA = "a" * 40
HEAD_SHA = "1" * 40
HEAD_SHA_2 = "2" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _enrolled_snapshot() -> AuthenticatedTrustSnapshot:
    return AuthenticatedTrustSnapshot(
        repository_id=9001,
        owner_id=501,
        owner_login="acme",
        repository_slug="acme/widgets",
        is_fork=False,
        installation_id=3001,
        repository_selected=True,
        granted_permissions=dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS),
        actor_id=42,
        actor_login="alice-maintainer",
        actor_association="MEMBER",
        actor_permission="maintain",
        pull_request=88,
        head_sha=HEAD_SHA,
        permission_epoch=1,
    )


def _session_with_charges(snapshot: AuthenticatedTrustSnapshot) -> SessionRecord:
    return SessionRecord.create(
        snapshot.to_session_identity(),
        now=FIXED_NOW,
        ttl=timedelta(days=30),
        completed_initial_reviews=1,
        completed_verification_rounds=1,
        failed_attempts=1,
        generation=3,
        reservation_id=_sha("active-reservation"),
        reserved_slot="verification",
        last_committed_reservation_id=_sha("committed-initial"),
        failed_attempts_head_sha=snapshot.head_sha,
    )


def _blocking_inventory() -> AuthoritativeDecisionInventory:
    comment = ReviewComment(
        path="src/security.py",
        line=24,
        body="Missing signature verification on webhook payload.",
        blocking=True,
        effective_blocking=True,
        severity="critical",
        symbol="verify_webhook",
        defect_kind="authn",
    )
    return AuthoritativeDecisionInventory(
        obligations=(obligation_from_comment(comment),)
    )


class RepositoryTrustTransitionsG15Tests(unittest.TestCase):
    def test_artifact_authority_reexports_trust_transition_contracts(self) -> None:
        self.assertIs(
            artifact_authority.AuthenticatedTrustSnapshot,
            AuthenticatedTrustSnapshot,
        )
        self.assertIs(
            artifact_authority.AuthenticatedTrustMigration,
            AuthenticatedTrustMigration,
        )
        self.assertIs(
            artifact_authority.evaluate_trust_boundary,
            evaluate_trust_boundary,
        )
        self.assertIs(
            artifact_authority.verify_and_migrate_trust_boundary,
            verify_and_migrate_trust_boundary,
        )
        self.assertEqual(len(TRUST_TRANSITION_KINDS), 7)

    def test_unchanged_repository_id_never_proves_ownership_transfer_trust(
        self,
    ) -> None:
        enrolled = _enrolled_snapshot()
        # Same numeric repository_id=9001, but transferred from acme (501) -> newcorp (777)!
        transferred_same_repo_id = replace(
            enrolled,
            owner_id=777,
            owner_login="newcorp",
            repository_slug="newcorp/widgets",
            installation_id=4002,
        )
        self.assertEqual(enrolled.repository_id, transferred_same_repo_id.repository_id)

        transitions = detect_trust_transitions(enrolled, transferred_same_repo_id)
        self.assertIn("ownership_transfer", transitions)
        self.assertNotIn("repository_rename", transitions)
        self.assertIn("app_reinstall", transitions)

        # Synthetic native write gate: ZERO cross-owner writes permitted across all 3 phases!
        dispatched_writes: list[str] = []

        def guarded_publish(
            decision: TrustTransitionDecision, target_slug: str
        ) -> None:
            if decision.allowed:
                dispatched_writes.append(target_slug)

        for phase in (
            "before_admission",
            "before_native_effect",
            "after_unknown_effect",
        ):
            decision = evaluate_trust_boundary(
                enrolled,
                transferred_same_repo_id,
                phase=phase,
                has_prior_charges=True,
            )
            guarded_publish(decision, transferred_same_repo_id.repository_slug)
            self.assertFalse(decision.allowed)
            self.assertTrue(decision.cross_repository_write_blocked)
            self.assertFalse(decision.fresh_allowance_minted)
            self.assertTrue(decision.obligations_preserved)
            self.assertTrue(decision.charges_held)
            self.assertEqual(
                decision.reason_code, "trust-transition-refused:ownership_transfer"
            )
            self.assertIn("authenticated-trust-migration", decision.recovery_routes)
            if phase == "after_unknown_effect":
                self.assertTrue(decision.unknown_effect_held)
                self.assertIn("reconcile-unknown-effect", decision.recovery_routes)

        self.assertEqual(dispatched_writes, [])

    def test_all_seven_transitions_across_all_three_injection_phases(self) -> None:
        enrolled = _enrolled_snapshot()
        revoked_perms = dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS)
        revoked_perms["checks"] = "none"

        scenarios: tuple[tuple[str, AuthenticatedTrustSnapshot, str, str], ...] = (
            (
                "repository_rename",
                replace(enrolled, repository_slug="acme/widgets-next"),
                "review",
                "authenticated-trust-migration",
            ),
            (
                "ownership_transfer",
                replace(
                    enrolled,
                    owner_id=888,
                    owner_login="otherorg",
                    repository_slug="otherorg/widgets",
                ),
                "review",
                "authenticated-trust-migration",
            ),
            (
                "fork_status_change",
                replace(enrolled, is_fork=True),
                "review",
                "unsupported-fork-boundary",
            ),
            (
                "app_reinstall",
                replace(enrolled, installation_id=9999),
                "review",
                "authenticated-trust-migration",
            ),
            (
                "repository_selection_change",
                replace(enrolled, repository_selected=False),
                "review",
                "restore-repository-selection",
            ),
            (
                "permission_revoke_or_restore",
                replace(enrolled, granted_permissions=revoked_perms),
                "review",
                "restore-app-permissions",
            ),
            (
                "actor_privilege_downgrade",
                replace(
                    enrolled,
                    actor_association="COLLABORATOR",
                    actor_permission="write",
                ),
                "override",
                "authorized-maintainer-command",
            ),
        )

        self.assertEqual(
            {kind for kind, _, _, _ in scenarios}, set(TRUST_TRANSITION_KINDS)
        )

        for expected_kind, observed, operation, expected_route in scenarios:
            for phase in TRANSITION_PHASES:
                with self.subTest(transition=expected_kind, phase=phase):
                    has_prior = phase == "before_admission"
                    decision = evaluate_trust_boundary(
                        enrolled,
                        observed,
                        phase=phase,
                        operation=operation,
                        has_prior_charges=has_prior,
                    )
                    self.assertFalse(decision.allowed)
                    self.assertIn(expected_kind, decision.detected_transitions)
                    self.assertEqual(
                        decision.reason_code,
                        f"trust-transition-refused:{expected_kind}",
                    )
                    self.assertFalse(decision.fresh_allowance_minted)
                    self.assertTrue(decision.obligations_preserved)
                    self.assertEqual(
                        decision.charges_held,
                        has_prior or phase == "after_unknown_effect",
                    )
                    self.assertEqual(
                        decision.cross_repository_write_blocked,
                        expected_kind == "ownership_transfer",
                    )
                    self.assertIn(expected_route, decision.recovery_routes)
                    if phase == "after_unknown_effect":
                        self.assertTrue(decision.unknown_effect_held)
                        self.assertIn(
                            "reconcile-unknown-effect", decision.recovery_routes
                        )

    def test_verified_boundary_unknown_effect_and_cross_pr_guards(self) -> None:
        enrolled = _enrolled_snapshot()

        # Clean verified boundary across admission and native-effect gates:
        for phase in ("before_admission", "before_native_effect"):
            ok = evaluate_trust_boundary(
                enrolled,
                enrolled,
                phase=phase,
                operation="reply",
                has_prior_charges=True,
            )
            self.assertTrue(ok.allowed)
            self.assertEqual(ok.detected_transitions, ())
            self.assertEqual(ok.reason_code, "trust-boundary-verified")
            self.assertFalse(ok.cross_repository_write_blocked)
            self.assertTrue(ok.charges_held)
            self.assertEqual(ok.recovery_routes, ("none-needed",))

        # Unchanged snapshot after unknown effect still holds until reconciliation!
        held_unknown = evaluate_trust_boundary(
            enrolled,
            enrolled,
            phase="after_unknown_effect",
        )
        self.assertFalse(held_unknown.allowed)
        self.assertEqual(
            held_unknown.reason_code, "unknown-effect-reconciliation-required"
        )
        self.assertTrue(held_unknown.charges_held)
        self.assertTrue(held_unknown.unknown_effect_held)
        self.assertEqual(
            held_unknown.recovery_routes, ("reconcile-unknown-effect", "verify")
        )

        # Cross-PR mismatch on identical repository is blocked as cross-target risk!
        other_pr = replace(enrolled, pull_request=89)
        pr_mismatch = evaluate_trust_boundary(
            enrolled, other_pr, phase="before_admission"
        )
        self.assertFalse(pr_mismatch.allowed)
        self.assertTrue(pr_mismatch.cross_repository_write_blocked)
        self.assertEqual(
            pr_mismatch.reason_code,
            "trust-transition-refused:cross_pull_request_mismatch",
        )
        self.assertEqual(pr_mismatch.recovery_routes, ("operator-reconcile",))

    def test_end_to_end_rename_and_reinstall_migration_recovers_to_approval(
        self,
    ) -> None:
        enrolled = _enrolled_snapshot()
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()
        obligation = inventory.obligations[0]

        # Legitimate repository rename + GitHub App reinstall + restored permission epoch:
        renamed_reinstalled = replace(
            enrolled,
            repository_slug="acme/widgets-v2",
            installation_id=3099,
            permission_epoch=2,
        )

        # Before migration, writes are refused and obligations/charges stay held:
        pre_migration = evaluate_trust_boundary(
            enrolled,
            renamed_reinstalled,
            phase="before_native_effect",
            has_prior_charges=True,
        )
        self.assertFalse(pre_migration.allowed)
        self.assertEqual(
            pre_migration.detected_transitions,
            ("repository_rename", "app_reinstall", "permission_revoke_or_restore"),
        )

        migration = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=renamed_reinstalled.digest(),
            operator_id=42,
            operator_login="alice-maintainer",
            operator_association="MEMBER",
            operator_permission="maintain",
            reason="Verified repository rename to acme/widgets-v2 and reinstalled App.",
            operator_attestation_sha256=_sha("maintainer-rename-reinstall-attest"),
        )
        receipt = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=renamed_reinstalled,
            session_record=session,
            inventory=inventory,
            migration=migration,
        )

        # 100% of prior charges, reservations, and blocking obligations survive!
        self.assertFalse(receipt.fresh_allowance_minted)
        self.assertEqual(receipt.migrated_session.repository, "acme/widgets-v2")
        self.assertEqual(receipt.migrated_session.repository_id, 9001)
        self.assertEqual(
            receipt.migrated_session.completed_initial_reviews,
            session.completed_initial_reviews,
        )
        self.assertEqual(
            receipt.migrated_session.completed_verification_rounds,
            session.completed_verification_rounds,
        )
        self.assertEqual(
            receipt.migrated_session.failed_attempts,
            session.failed_attempts,
        )
        self.assertEqual(
            receipt.migrated_session.reservation_id,
            session.reservation_id,
        )
        self.assertEqual(receipt.migrated_session.generation, session.generation + 1)
        self.assertEqual(receipt.preserved_inventory.digest(), inventory.digest())

        # Rebound trust snapshot now passes admission cleanly:
        post_migration = evaluate_trust_boundary(
            receipt.migrated_snapshot,
            renamed_reinstalled,
            phase="before_admission",
            has_prior_charges=True,
        )
        self.assertTrue(post_migration.allowed)

        # Drive preserved blocking obligation to verified fix and clean PR approval!
        clean_result = ReviewResult(
            summary="Webhook signature verification verified on renamed repository.",
            comments=(),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/security.py", "reviewed"),),
                enumerated_paths=("src/security.py",),
                enumeration_complete=True,
            ),
        )
        fix = ConfirmedFixEvidence(
            obligation_id=obligation.obligation_id,
            resolution_criterion=obligation.resolution_criterion,
            head_sha=HEAD_SHA_2,
            evidence_sha256=_sha("webhook-hmac-fix-proof"),
            verified_paths=("src/security.py",),
        )
        outcome = evaluate_active_window_round(
            clean_result,
            head_sha=HEAD_SHA_2,
            base_sha=BASE_SHA,
            prior_inventory=receipt.preserved_inventory,
            max_active_findings=3,
            coverage_mode="incremental",
            confirmed_fixes=(fix,),
        )
        self.assertTrue(outcome.approval_eligibility.approved)
        self.assertEqual(outcome.approval_eligibility.outcome, "approved")

    def test_ownership_transfer_requires_dual_attestation_and_effect_reconciliation(
        self,
    ) -> None:
        enrolled = _enrolled_snapshot()
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()
        obligation = inventory.obligations[0]

        transferred = replace(
            enrolled,
            owner_id=777,
            owner_login="newcorp",
            repository_slug="newcorp/widgets",
            installation_id=4002,
            actor_id=84,
            actor_login="newcorp-admin",
            actor_association="OWNER",
            actor_permission="admin",
        )
        transfer_attest = compute_transfer_attestation_sha256(
            repository_id=enrolled.repository_id,
            previous_owner_id=enrolled.owner_id,
            previous_owner_login=enrolled.owner_login,
            new_owner_id=transferred.owner_id,
            new_owner_login=transferred.owner_login,
            pull_request=enrolled.pull_request,
            head_sha=enrolled.head_sha,
            permission_epoch=enrolled.permission_epoch,
            previous_owner_witness="Verified prior-owner transfer approval.",
        )

        # 1. Refused without previous_owner_transfer_attestation_sha256 even though repository_id==9001!
        missing_prev_owner = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=transferred.digest(),
            operator_id=84,
            operator_login="newcorp-admin",
            operator_association="OWNER",
            operator_permission="admin",
            reason="Attempt transfer migration relying only on repository_id.",
            operator_attestation_sha256=_sha("newcorp-only-attest"),
        )
        with self.assertRaisesRegex(
            ReviewInputError, "unchanged numeric repository_id alone never proves"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=transferred,
                session_record=session,
                inventory=inventory,
                migration=missing_prev_owner,
            )

        # 2. Refused after unknown effect when reconciled_effect_sha256 is absent!
        unreconciled_transfer = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=transferred.digest(),
            operator_id=84,
            operator_login="newcorp-admin",
            operator_association="OWNER",
            operator_permission="admin",
            reason="Dual-attested transfer before reconciling unknown effect.",
            operator_attestation_sha256=_sha("newcorp-attest"),
            previous_owner_transfer_attestation_sha256=transfer_attest,
        )
        with self.assertRaisesRegex(
            ReviewInputError, "unknown native effect must be reconciled"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=transferred,
                session_record=session,
                inventory=inventory,
                migration=unreconciled_transfer,
                has_unknown_effect=True,
                expected_transfer_attestation_sha256=transfer_attest,
            )

        # 3. Succeeds with dual owner attestation + reconciled unknown-effect receipt,
        #    then recovers to approval via explicit authorized maintainer override!
        valid_transfer = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=transferred.digest(),
            operator_id=84,
            operator_login="newcorp-admin",
            operator_association="OWNER",
            operator_permission="admin",
            reason="Dual-attested org transfer with reconciled readback.",
            operator_attestation_sha256=_sha("newcorp-attest"),
            previous_owner_transfer_attestation_sha256=transfer_attest,
            reconciled_effect_sha256=_sha("owned-readback-reconciled"),
        )
        receipt = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=transferred,
            session_record=session,
            inventory=inventory,
            migration=valid_transfer,
            has_unknown_effect=True,
            expected_transfer_attestation_sha256=transfer_attest,
        )
        self.assertIn("ownership_transfer", receipt.migrated_transitions)
        self.assertEqual(receipt.migrated_session.repository, "newcorp/widgets")
        self.assertFalse(receipt.fresh_allowance_minted)

        # Also verify actor_privilege_downgrade recovery: downgraded actor is refused,
        # while another maintainer/admin authority succeeds and overrides cleanly!
        downgraded_actor = replace(
            receipt.migrated_snapshot,
            actor_id=99,
            actor_login="junior-dev",
            actor_association="COLLABORATOR",
            actor_permission="read",
        )
        self.assertFalse(
            evaluate_trust_boundary(
                receipt.migrated_snapshot,
                downgraded_actor,
                phase="before_admission",
                operation="override",
            ).allowed
        )
        maintainer_actor = replace(
            receipt.migrated_snapshot,
            actor_id=85,
            actor_login="newcorp-sec-lead",
            actor_association="MEMBER",
            actor_permission="maintain",
        )
        self.assertTrue(
            evaluate_trust_boundary(
                receipt.migrated_snapshot,
                maintainer_actor,
                phase="before_admission",
                operation="override",
            ).allowed
        )

        override = AuthorizedHumanOverride(
            obligation_id=obligation.obligation_id,
            action="accept-risk",
            actor="newcorp-admin",
            association="OWNER",
            permission="admin",
            reason="Webhook endpoint behind mTLS gateway; tracked for follow-up.",
            head_sha=HEAD_SHA,
            proof_sha256=_sha("newcorp-admin-override-proof"),
        )
        clean_result = ReviewResult(
            summary="Post-transfer maintainer disposition round.",
            comments=(),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/security.py", "reviewed"),),
                enumerated_paths=("src/security.py",),
                enumeration_complete=True,
            ),
        )
        outcome = evaluate_active_window_round(
            clean_result,
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            prior_inventory=receipt.preserved_inventory,
            max_active_findings=3,
            coverage_mode="incremental",
            human_overrides=(override,),
        )
        self.assertTrue(outcome.approval_eligibility.approved)

    def test_migration_and_snapshot_validation_refuse_invalid_boundaries(self) -> None:
        enrolled = _enrolled_snapshot()
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()
        renamed = replace(enrolled, repository_slug="acme/widgets-2")

        def make_migration(
            old_snap: AuthenticatedTrustSnapshot,
            new_snap: AuthenticatedTrustSnapshot,
        ) -> AuthenticatedTrustMigration:
            return AuthenticatedTrustMigration(
                old_snapshot_sha256=old_snap.digest(),
                new_snapshot_sha256=new_snap.digest(),
                operator_id=new_snap.actor_id,
                operator_login=new_snap.actor_login,
                operator_association=new_snap.actor_association,
                operator_permission=new_snap.actor_permission,
                reason="Boundary check.",
                operator_attestation_sha256=_sha("attest"),
            )

        # Cross-repository_id, cross-PR, and cross-head_sha migrations unconditionally fail:
        for bad_target in (
            replace(renamed, repository_id=9999),
            replace(renamed, pull_request=999),
            replace(renamed, head_sha=HEAD_SHA_2),
        ):
            with self.assertRaisesRegex(
                ReviewInputError,
                "cannot cross repository_id, pull_request, or head_sha",
            ):
                verify_and_migrate_trust_boundary(
                    enrolled=enrolled,
                    target=bad_target,
                    session_record=session,
                    inventory=inventory,
                    migration=make_migration(enrolled, bad_target),
                )

        # Fork target, unselected repo, and missing permissions fail migration:
        fork_target = replace(renamed, is_fork=True)
        with self.assertRaisesRegex(ReviewInputError, "fork repositories"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=fork_target,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, fork_target),
            )

        unselected_target = replace(renamed, repository_selected=False)
        with self.assertRaisesRegex(ReviewInputError, "not selected"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=unselected_target,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, unselected_target),
            )

        bad_perms = dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS)
        bad_perms["pull_requests"] = "read"
        low_perm_target = replace(renamed, granted_permissions=bad_perms)
        with self.assertRaisesRegex(ReviewInputError, "lacks required permissions"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=low_perm_target,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, low_perm_target),
            )

        # No transition -> refused:
        with self.assertRaisesRegex(
            ReviewInputError, "no trust transition requires migration"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=enrolled,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, enrolled),
            )

        # Mismatched session_record identity -> refused:
        foreign_session = SessionRecord.create(
            SessionIdentity("other/repo", 88, repository_id=9001),
            now=FIXED_NOW,
        )
        with self.assertRaisesRegex(
            ReviewInputError, "session_record identity does not match"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record=foreign_session,
                inventory=inventory,
                migration=make_migration(enrolled, renamed),
            )

        # Digest mismatches and invalid types in verify_and_migrate_trust_boundary:
        valid_mig = make_migration(enrolled, renamed)
        with self.assertRaisesRegex(ReviewInputError, "old_snapshot_sha256"):
            verify_and_migrate_trust_boundary(
                enrolled=renamed,
                target=renamed,
                session_record=session,
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaisesRegex(ReviewInputError, "new_snapshot_sha256"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=enrolled,
                session_record=session,
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled="bad",  # type: ignore[arg-type]
                target=renamed,
                session_record=session,
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record="bad",  # type: ignore[arg-type]
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record=session,
                inventory="bad",  # type: ignore[arg-type]
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record=session,
                inventory=inventory,
                migration="bad",  # type: ignore[arg-type]
            )

        # Snapshot, Decision, and Migration constructor guardrails:
        with self.assertRaises(ReviewInputError):
            replace(enrolled, repository_slug="otherowner/widgets")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, is_fork=1)  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            replace(enrolled, repository_id=0)
        with self.assertRaises(ReviewInputError):
            replace(enrolled, owner_login="bad login")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, repository_slug="badslug")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, granted_permissions="bad")  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            replace(enrolled, granted_permissions={"": "read"})
        with self.assertRaises(ReviewInputError):
            replace(enrolled, granted_permissions={"checks": "superadmin"})
        with self.assertRaises(ReviewInputError):
            replace(enrolled, actor_login="bad actor")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, actor_association="GUEST")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, actor_permission="owner")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, head_sha="not-a-sha")
        with self.assertRaises(ReviewInputError):
            enrolled.actor_meets_operation("merge")
        with self.assertRaises(ReviewInputError):
            detect_trust_transitions("bad", enrolled)  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            detect_trust_transitions(enrolled, enrolled, operation="merge")
        with self.assertRaises(ReviewInputError):
            evaluate_trust_boundary(enrolled, enrolled, phase="post_merge")

        # Invariant guards on TrustTransitionDecision:
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="before_admission",
                allowed=False,
                detected_transitions=("repository_rename",),
                reason_code="x",
                cross_repository_write_blocked=True,
                fresh_allowance_minted=True,
                obligations_preserved=True,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("authenticated-trust-migration",),
            )
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="before_admission",
                allowed=False,
                detected_transitions=("repository_rename",),
                reason_code="x",
                cross_repository_write_blocked=True,
                fresh_allowance_minted=False,
                obligations_preserved=False,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("authenticated-trust-migration",),
            )
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="invalid",
                allowed=False,
                detected_transitions=(),
                reason_code="x",
                cross_repository_write_blocked=False,
                fresh_allowance_minted=False,
                obligations_preserved=True,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("operator-reconcile",),
            )
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="before_admission",
                allowed=False,
                detected_transitions=("unknown_kind",),
                reason_code="x",
                cross_repository_write_blocked=False,
                fresh_allowance_minted=False,
                obligations_preserved=True,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("operator-reconcile",),
            )

        # Non-maintainer cannot construct AuthenticatedTrustMigration:
        with self.assertRaisesRegex(
            ReviewInputError, "requires maintainer or admin authority"
        ):
            AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed.digest(),
                operator_id=99,
                operator_login="contributor-bob",
                operator_association="CONTRIBUTOR",
                operator_permission="read",
                reason="Unauthorized migration attempt.",
                operator_attestation_sha256=_sha("bob"),
            )
        with self.assertRaises(ReviewInputError):
            AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed.digest(),
                operator_id=42,
                operator_login="bad login",
                operator_association="MEMBER",
                operator_permission="maintain",
                reason="Invalid login.",
                operator_attestation_sha256=_sha("attest"),
            )

    def test_actor_handoff_unfork_epoch_operator_binding_and_audit_digests(
        self,
    ) -> None:
        enrolled = replace(
            _enrolled_snapshot(),
            actor_association="OWNER",
            actor_permission="admin",
            permission_epoch=2,
        )
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()

        # 1) RS-366E18: Different actor (actor_id=77) who meets operation ("override")
        #    is NOT flagged as actor_privilege_downgrade even though admin -> maintain!
        peer_maintainer = replace(
            enrolled,
            actor_id=77,
            actor_login="peer-maintainer",
            actor_association="MEMBER",
            actor_permission="maintain",
        )
        self.assertEqual(
            detect_trust_transitions(enrolled, peer_maintainer, operation="override"),
            (),
        )
        peer_dec = evaluate_trust_boundary(
            enrolled,
            peer_maintainer,
            phase="before_native_effect",
            operation="override",
            has_prior_charges=False,
        )
        self.assertTrue(peer_dec.allowed)
        self.assertFalse(peer_dec.charges_held)

        # Same actor (actor_id=42) dropping admin -> maintain IS flagged as downgrade!
        same_actor_drop = replace(
            enrolled,
            actor_association="MEMBER",
            actor_permission="maintain",
        )
        self.assertIn(
            "actor_privilege_downgrade",
            detect_trust_transitions(enrolled, same_actor_drop, operation="override"),
        )

        # 2) RS-377BA7: Unfork transition (True -> False) is blocked from migration
        #    and routed to unsupported-fork-boundary!
        fork_enrolled = replace(enrolled, is_fork=True)
        unforked_target = replace(enrolled, is_fork=False)
        unfork_dec = evaluate_trust_boundary(
            fork_enrolled, unforked_target, phase="before_admission"
        )
        self.assertFalse(unfork_dec.allowed)
        self.assertIn("unsupported-fork-boundary", unfork_dec.recovery_routes)
        self.assertNotIn("authenticated-trust-migration", unfork_dec.recovery_routes)
        with self.assertRaisesRegex(ReviewInputError, "fork repositories"):
            verify_and_migrate_trust_boundary(
                enrolled=fork_enrolled,
                target=unforked_target,
                session_record=_session_with_charges(fork_enrolled),
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=fork_enrolled.digest(),
                    new_snapshot_sha256=unforked_target.digest(),
                    operator_id=unforked_target.actor_id,
                    operator_login=unforked_target.actor_login,
                    operator_association=unforked_target.actor_association,
                    operator_permission=unforked_target.actor_permission,
                    reason="Attempt unfork migration.",
                    operator_attestation_sha256=_sha("unfork"),
                ),
            )

        # 3) RS-CB5C9C: permission_epoch regression is blocked from migration!
        regressed_epoch = replace(enrolled, permission_epoch=1)
        epoch_dec = evaluate_trust_boundary(
            enrolled, regressed_epoch, phase="before_admission"
        )
        self.assertFalse(epoch_dec.allowed)
        self.assertIn("operator-reconcile", epoch_dec.recovery_routes)
        self.assertNotIn("authenticated-trust-migration", epoch_dec.recovery_routes)
        with self.assertRaisesRegex(
            ReviewInputError, "permission_epoch cannot regress"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=regressed_epoch,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=regressed_epoch.digest(),
                    operator_id=regressed_epoch.actor_id,
                    operator_login=regressed_epoch.actor_login,
                    operator_association=regressed_epoch.actor_association,
                    operator_permission=regressed_epoch.actor_permission,
                    reason="Epoch regression.",
                    operator_attestation_sha256=_sha("epoch-reg"),
                ),
            )

        # Changing granted_permissions without advancing permission_epoch fails closed:
        expanded_perms = dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS)
        expanded_perms["metadata"] = "read"
        stale_epoch_perms = replace(enrolled, granted_permissions=expanded_perms)
        with self.assertRaisesRegex(
            ReviewInputError, "strictly newer permission_epoch"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=stale_epoch_perms,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=stale_epoch_perms.digest(),
                    operator_id=stale_epoch_perms.actor_id,
                    operator_login=stale_epoch_perms.actor_login,
                    operator_association=stale_epoch_perms.actor_association,
                    operator_permission=stale_epoch_perms.actor_permission,
                    reason="Expanded perms without epoch bump.",
                    operator_attestation_sha256=_sha("perm-epoch"),
                ),
            )

        # 4) RS-A6D906: Spoofed operator identity not matching target snapshot fails!
        renamed_target = replace(enrolled, repository_slug="acme/widgets-renamed")
        with self.assertRaisesRegex(
            ReviewInputError, "operator identity and authority must match"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed_target,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=renamed_target.digest(),
                    operator_id=9999,
                    operator_login="spoofed-admin",
                    operator_association="OWNER",
                    operator_permission="admin",
                    reason="Spoofed operator id/login.",
                    operator_attestation_sha256=_sha("spoof"),
                ),
            )

        # 5) RS-6D39F3 & RS-98D766: Transfer attestation mismatch fails closed, and
        #    audit_sha256 binds installation_id and operator authority!
        transferred_target = replace(
            enrolled,
            owner_id=900,
            owner_login="destorg",
            repository_slug="destorg/widgets",
            installation_id=7001,
        )
        expected_attest = compute_transfer_attestation_sha256(
            repository_id=enrolled.repository_id,
            previous_owner_id=enrolled.owner_id,
            previous_owner_login=enrolled.owner_login,
            new_owner_id=transferred_target.owner_id,
            new_owner_login=transferred_target.owner_login,
            pull_request=enrolled.pull_request,
            head_sha=enrolled.head_sha,
            permission_epoch=enrolled.permission_epoch,
            previous_owner_witness="Verified prior-owner transfer approval.",
        )
        with self.assertRaisesRegex(
            ReviewInputError, "does not match verified transfer attestation"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=transferred_target,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=transferred_target.digest(),
                    operator_id=transferred_target.actor_id,
                    operator_login=transferred_target.actor_login,
                    operator_association=transferred_target.actor_association,
                    operator_permission=transferred_target.actor_permission,
                    reason="Wrong transfer attestation.",
                    operator_attestation_sha256=_sha("op"),
                    previous_owner_transfer_attestation_sha256=_sha("wrong-attest"),
                ),
                expected_transfer_attestation_sha256=expected_attest,
            )

        rec_a = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=renamed_target,
            session_record=session,
            inventory=inventory,
            migration=AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed_target.digest(),
                operator_id=renamed_target.actor_id,
                operator_login=renamed_target.actor_login,
                operator_association=renamed_target.actor_association,
                operator_permission=renamed_target.actor_permission,
                reason="Audit check A.",
                operator_attestation_sha256=_sha("op-a"),
            ),
        )
        renamed_other_install = replace(renamed_target, installation_id=8888)
        rec_b = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=renamed_other_install,
            session_record=session,
            inventory=inventory,
            migration=AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed_other_install.digest(),
                operator_id=renamed_other_install.actor_id,
                operator_login=renamed_other_install.actor_login,
                operator_association=renamed_other_install.actor_association,
                operator_permission=renamed_other_install.actor_permission,
                reason="Audit check A.",
                operator_attestation_sha256=_sha("op-a"),
            ),
        )
        self.assertNotEqual(rec_a.audit_sha256, rec_b.audit_sha256)


class _TrustFakeBroker:
    def __init__(self) -> None:
        self.exchanges: list[str] = []

    def request_oidc_token(self) -> str:
        return "oidc-trust-test"

    def exchange(self, _oidc: str, *, capability: str) -> str:
        self.exchanges.append(capability)
        return f"tok-{capability}"


class _TrustGitHubHost:
    """Stateful fake GitHub host exercising ordinary G15 admission & preflight."""

    def __init__(
        self,
        *,
        repository_id: int = 9001,
        owner_id: int = 501,
        owner_login: str = "acme",
        repository_slug: str = "acme/widgets",
        is_fork: bool = False,
        pull_request: int = 88,
        head_sha: str = HEAD_SHA,
        base_sha: str = BASE_SHA,
        app_slug: str = "reviewsensei[bot]",
    ) -> None:
        import json

        from review_sensei_github import GitHubHttp

        try:
            from fake_github_http import json_response
        except ImportError:
            from tests.fake_github_http import json_response

        self._json = json
        self._json_response = json_response
        self.repository_id = repository_id
        self.owner_id = owner_id
        self.owner_login = owner_login
        self.repository_slug = repository_slug
        self.is_fork = is_fork
        self.pull_request = pull_request
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.app_slug = app_slug
        self.reviews: list[dict[str, object]] = []
        self.threads: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []
        self.issue_comments: list[dict[str, object]] = []
        self.fail_next_review_post_transient = False
        self.calls: list[tuple[str, str, object]] = []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self._open)

    def _resp(self, payload: object, status: int = 200):
        return self._json_response(payload, status)

    def _open(self, request, timeout=None):
        del timeout
        from urllib.parse import urlparse

        from review_sensei_github.errors import GitHubHTTPTransientError

        parsed = urlparse(request.full_url)
        path = parsed.path
        body = self._json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append((request.method, path, body))

        pr_suffix = f"/pulls/{self.pull_request}"
        if request.method == "GET":
            if path.endswith(pr_suffix):
                return self._resp(
                    {
                        "state": "open",
                        "draft": False,
                        "changed_files": 1,
                        "user": {"login": "dev-author", "type": "User"},
                        "head": {
                            "sha": self.head_sha,
                            "ref": "feature",
                            "repo": {
                                "full_name": self.repository_slug,
                                "fork": self.is_fork,
                            },
                        },
                        "base": {
                            "sha": self.base_sha,
                            "ref": "main",
                            "repo": {
                                "id": self.repository_id,
                                "full_name": self.repository_slug,
                                "fork": self.is_fork,
                                "owner": {
                                    "id": self.owner_id,
                                    "login": self.owner_login,
                                },
                            },
                        },
                    }
                )
            if path.endswith(f"{pr_suffix}/reviews"):
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
            if path.endswith(f"{pr_suffix}/comments"):
                return self._resp([])
            if path.endswith(f"/issues/{self.pull_request}/comments"):
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
            if path.endswith(f"{pr_suffix}/reviews"):
                if self.fail_next_review_post_transient:
                    self.fail_next_review_post_transient = False
                    raise GitHubHTTPTransientError("simulated review POST timeout")
                assert isinstance(body, dict)
                review_id = 100 + len(self.reviews)
                state_map = {
                    "APPROVE": "APPROVED",
                    "REQUEST_CHANGES": "CHANGES_REQUESTED",
                    "COMMENT": "COMMENTED",
                }
                ev = str(body.get("event", "COMMENT"))
                record: dict[str, object] = {
                    "id": review_id,
                    "commit_id": body["commit_id"],
                    "state": state_map.get(ev, "COMMENTED"),
                    "body": body.get("body", ""),
                    "pull_request_url": (
                        f"https://api.github.test/repos/{self.repository_slug}/pulls/{self.pull_request}"
                    ),
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
                                        "body": comment["body"],
                                        "author": {
                                            "login": self.app_slug,
                                            "__typename": "Bot",
                                        },
                                    }
                                ]
                            },
                        }
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

        if request.method == "PATCH" and "/check-runs/" in path:
            assert isinstance(body, dict)
            cid = int(path.rsplit("/", 1)[-1])
            for run in self.check_runs:
                if run["id"] == cid:
                    run.update(body)
                    return self._resp(run, 200)

        raise AssertionError(f"Unhandled request: {request.method} {path}")


class OrdinaryPublisherTrustTransitionTests(unittest.TestCase):
    """End-to-end ordinary GitHubApplication & ReviewPublisher G15 tests."""

    DIFF_1 = (
        "diff --git a/src/security.py b/src/security.py\n"
        "--- a/src/security.py\n"
        "+++ b/src/security.py\n"
        "@@ -1,1 +1,5 @@\n"
        "-def verify_webhook(sig): return False\n"
        "+def verify_webhook(sig):\n"
        "+    if not sig:\n"
        "+        return True\n"
        "+    return True\n"
        "+# trailing\n"
    )
    DIFF_CLEAN = (
        "diff --git a/src/security.py b/src/security.py\n"
        "--- a/src/security.py\n"
        "+++ b/src/security.py\n"
        "@@ -1,5 +1,5 @@\n"
        " def verify_webhook(sig):\n"
        "     if not sig:\n"
        "-        return True\n"
        "+        return False\n"
        "     return True\n"
        " # trailing\n"
    )

    @staticmethod
    def _make_apps(host: _TrustGitHubHost, ledger_dir: str):
        from pathlib import Path

        from review_sensei_github import (
            ConversationPublisher,
            GitHubApplication,
            GitHubWriteOptions,
            LearningPRPublisher,
            ReviewPublisher,
        )

        from review_sensei.session import LocalSessionLedger

        broker = _TrustFakeBroker()
        publisher = ReviewPublisher(http=host.http)
        ledger = LocalSessionLedger(Path(ledger_dir))
        ledger.initialize(
            SessionIdentity(
                repository="acme/widgets", pull_request=88, repository_id=9001
            )
        )
        app = GitHubApplication(
            http=host.http,
            broker=broker,  # type: ignore[arg-type]
            reviewer=publisher,
            learner=LearningPRPublisher(http=host.http),
            replier=ConversationPublisher(http=host.http),
            session_ledger=None,
        )
        cmd_app = GitHubApplication(
            http=host.http,
            broker=broker,  # type: ignore[arg-type]
            reviewer=publisher,
            learner=LearningPRPublisher(http=host.http),
            replier=ConversationPublisher(http=host.http),
            session_ledger=ledger,
        )
        options = GitHubWriteOptions(
            auto_review=True,
            auto_approve=True,
            github_writes=True,
            mention_replies=True,
            operation_entry="disabled",
            max_active_findings=3,
        )
        return app, cmd_app, publisher, broker, ledger, options

    @staticmethod
    def _blocking_result():
        from review_sensei.convergence import derive_blocker_candidate

        comment = ReviewComment(
            path="src/security.py",
            line=3,
            body="Missing signature check allows unauthenticated webhook payloads.",
            blocking=True,
            effective_blocking=True,
            severity="critical",
            symbol="verify_webhook",
            defect_kind="auth_bypass",
        )
        res = ReviewResult(
            summary="Found 1 critical webhook signature blocker.",
            comments=(comment,),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/security.py", "reviewed"),),
                enumerated_paths=("src/security.py",),
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
                named_mandatory_rule="SEC-WEBHOOK-01",
                has_required_contract=True,
            ),
        )
        return res, blockers

    @staticmethod
    def _clean_result() -> ReviewResult:
        return ReviewResult(
            summary="Webhook signature verification enforced.",
            comments=(),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/security.py", "reviewed"),),
                enumerated_paths=("src/security.py",),
                enumeration_complete=True,
            ),
        )

    def test_ordinary_admission_and_live_preflight_refuse_all_untrusted_transitions_with_zero_writes(
        self,
    ) -> None:
        import tempfile

        from review_sensei_github.errors import GitHubPublicationError

        from review_sensei.convergence import ReviewConvergencePolicy

        policy = ReviewConvergencePolicy(mode="strict")
        with tempfile.TemporaryDirectory() as tmpdir:
            host = _TrustGitHubHost()
            app, cmd_app, _pub, broker, _ledger, options = self._make_apps(host, tmpdir)
            enrolled = _enrolled_snapshot()
            app.enroll_trust_snapshot(snapshot=enrolled)
            self.assertEqual(
                app.trust_snapshot(repository_id=9001, pull_request=88), enrolled
            )

            # Round 1 succeeds under matching enrolled trust boundary:
            res1, blockers1 = self._blocking_result()
            pub1 = app.publish_review(
                observed_trust=enrolled,
                options=options,
                oidc_token="oidc-1",
                repository="acme/widgets",
                repository_id=9001,
                pull_request=88,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res1,
                diff=self.DIFF_1,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=blockers1,
            )
            self.assertEqual(pub1.status, "published")
            self.assertEqual(pub1.diagnostic, "required_fixes_open")
            self.assertEqual(len(host.reviews), 1)
            self.assertEqual(len(host.threads), 1)

            # Negative controls at before_admission (zero HTTP calls & zero broker exchanges!):
            exchanges_before = len(broker.exchanges)
            http_calls_before = len(host.calls)
            low_perms = dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS)
            low_perms["checks"] = "none"

            admission_cases = (
                # 1. repository_rename
                (
                    "acme/widgets-renamed",
                    9001,
                    replace(enrolled, repository_slug="acme/widgets-renamed"),
                    "trust-transition-refused:repository_rename",
                ),
                # 2. ownership_transfer with IDENTICAL numeric repository_id=9001!
                (
                    "newcorp/widgets",
                    9001,
                    replace(
                        enrolled,
                        owner_id=888,
                        owner_login="newcorp",
                        repository_slug="newcorp/widgets",
                    ),
                    "trust-transition-refused:ownership_transfer",
                ),
                # 3. fork_status_change
                (
                    "acme/widgets",
                    9001,
                    replace(enrolled, is_fork=True),
                    "trust-transition-refused:fork_status_change",
                ),
                # 4. app_reinstall (installation_id changed)
                (
                    "acme/widgets",
                    9001,
                    replace(enrolled, installation_id=7777),
                    "trust-transition-refused:app_reinstall",
                ),
                # 5. repository_selection_change
                (
                    "acme/widgets",
                    9001,
                    replace(enrolled, repository_selected=False),
                    "trust-transition-refused:repository_selection_change",
                ),
                # 6. permission_revoke_or_restore
                (
                    "acme/widgets",
                    9001,
                    replace(enrolled, granted_permissions=low_perms),
                    "trust-transition-refused:permission_revoke_or_restore",
                ),
            )
            for slug, rid, obs_snap, expected_code in admission_cases:
                with self.assertRaisesRegex(GitHubPublicationError, expected_code):
                    app.publish_review(
                        options=options,
                        oidc_token="oidc-refused",
                        repository=slug,
                        repository_id=rid,
                        pull_request=88,
                        head_sha=HEAD_SHA_2,
                        base_branch="main",
                        base_sha=BASE_SHA,
                        result=self._clean_result(),
                        diff=self.DIFF_CLEAN,
                        app_slug="reviewsensei[bot]",
                        convergence_policy=policy,
                        observed_trust=obs_snap,
                    )
            self.assertEqual(len(broker.exchanges), exchanges_before)
            self.assertEqual(len(host.calls), http_calls_before)
            self.assertEqual(len(host.reviews), 1)

            # 7. actor_privilege_downgrade on apply_maintainer_command & generate_and_publish_reply:
            cmd_res = cmd_app.apply_maintainer_command(
                options=options,
                oidc_token="oidc-cmd-low",
                repository="acme/widgets",
                repository_id=9001,
                pull_request=88,
                head_sha=HEAD_SHA,
                body="@reviewsensei verify",
                actor_login="alice-maintainer",
                actor_id=42,
                association="CONTRIBUTOR",
                actor_permission="read",
                app_slug="reviewsensei[bot]",
            )
            self.assertFalse(cmd_res.applied)
            self.assertEqual(cmd_res.summary, "trust-observation-incomplete")

            with self.assertRaisesRegex(
                GitHubPublicationError, "trust-transition-refused:app_reinstall"
            ):
                app.generate_and_publish_reply(
                    options=options,
                    oidc_token="oidc-reply",
                    read_token="tok-read",
                    repository="acme/widgets",
                    repository_id=9001,
                    pull_request=88,
                    source_comment_id=10,
                    source_updated_at="2026-11-11T00:00:00Z",
                    expected_head_sha=HEAD_SHA,
                    reply_provider=None,  # type: ignore[arg-type]
                    model=None,
                    app_slug="reviewsensei[bot]",
                    root_comment_id=None,
                    observed_trust=replace(enrolled, installation_id=9999),
                )

            # Live preflight (before_native_effect) catches mid-run ownership transfer on
            # identical repository_id=9001 even when caller passed enrolled slug at admission!
            host.owner_id = 999
            host.owner_login = "shadow-org"
            host.repository_slug = "shadow-org/widgets"
            host.head_sha = HEAD_SHA_2
            with self.assertRaisesRegex(
                GitHubPublicationError,
                r"repository trust transition refused \(before_native_effect\): trust-transition-refused:ownership_transfer",
            ):
                app.publish_review(
                    observed_trust=enrolled,
                    options=options,
                    oidc_token="oidc-live-transfer",
                    repository="acme/widgets",
                    repository_id=9001,
                    pull_request=88,
                    head_sha=HEAD_SHA_2,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    result=self._clean_result(),
                    diff=self.DIFF_CLEAN,
                    app_slug="reviewsensei[bot]",
                    convergence_policy=policy,
                )
            # Zero new reviews posted across cross-owner boundary!
            self.assertEqual(len(host.reviews), 1)
            last_dec = app.trust_decisions(repository_id=9001, pull_request=88)[-1]
            self.assertEqual(last_dec.phase, "before_native_effect")
            self.assertTrue(last_dec.cross_repository_write_blocked)
            self.assertFalse(last_dec.fresh_allowance_minted)
            self.assertTrue(last_dec.obligations_preserved)

    def test_ordinary_unknown_effect_hold_and_dual_attested_transfer_migration_recover_to_approval(
        self,
    ) -> None:
        import tempfile

        from review_sensei_github.errors import (
            GitHubPublicationError,
            GitHubPublicationTransientError,
        )

        from review_sensei.convergence import ReviewConvergencePolicy

        policy = ReviewConvergencePolicy(mode="strict")
        with tempfile.TemporaryDirectory() as tmpdir:
            host = _TrustGitHubHost()
            app, _cmd_app, pub, _broker, _ledger, options = self._make_apps(
                host, tmpdir
            )
            enrolled = _enrolled_snapshot()
            app.enroll_trust_snapshot(snapshot=enrolled)

            # Round 1: initial blocking review on acme/widgets (repository_id=9001)
            res1, blockers1 = self._blocking_result()
            pub1 = app.publish_review(
                observed_trust=enrolled,
                options=options,
                oidc_token="oidc-1",
                repository="acme/widgets",
                repository_id=9001,
                pull_request=88,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res1,
                diff=self.DIFF_1,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=blockers1,
            )
            self.assertEqual(pub1.status, "published")

            # Round 2a: review POST fails transiently -> records after_unknown_effect hold!
            host.head_sha = HEAD_SHA_2
            host.fail_next_review_post_transient = True
            with self.assertRaises(GitHubPublicationTransientError):
                app.publish_review(
                    observed_trust=enrolled,
                    options=options,
                    oidc_token="oidc-2-loss",
                    repository="acme/widgets",
                    repository_id=9001,
                    pull_request=88,
                    head_sha=HEAD_SHA_2,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    result=self._clean_result(),
                    diff=self.DIFF_CLEAN,
                    app_slug="reviewsensei[bot]",
                    convergence_policy=policy,
                )
            self.assertTrue(
                app.has_unknown_trust_effect(repository_id=9001, pull_request=88)
            )
            self.assertTrue(
                any(
                    d.phase == "after_unknown_effect"
                    and d.reason_code == "unknown-effect-reconciliation-required"
                    and d.unknown_effect_held
                    for d in app.trust_decisions(repository_id=9001, pull_request=88)
                )
            )

            # Repository transfers to newcorp/widgets (SAME numeric repository_id=9001!)
            # and installs new GitHub App installation_id=6002 at permission_epoch=2:
            host.owner_id = 777
            host.owner_login = "newcorp"
            host.repository_slug = "newcorp/widgets"
            transferred = AuthenticatedTrustSnapshot(
                repository_id=9001,
                owner_id=777,
                owner_login="newcorp",
                repository_slug="newcorp/widgets",
                is_fork=False,
                installation_id=6002,
                repository_selected=True,
                granted_permissions=dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS),
                actor_id=84,
                actor_login="newcorp-admin",
                actor_association="OWNER",
                actor_permission="admin",
                pull_request=88,
                head_sha=HEAD_SHA,
                permission_epoch=2,
            )

            # Negative control: migration WITHOUT previous_owner_transfer_attestation_sha256 fails closed!
            with self.assertRaisesRegex(
                ReviewInputError,
                "unchanged numeric repository_id alone never proves transfer trust",
            ):
                app.migrate_trust_boundary(
                    repository_id=9001,
                    pull_request=88,
                    target=transferred,
                    migration=AuthenticatedTrustMigration(
                        old_snapshot_sha256=enrolled.digest(),
                        new_snapshot_sha256=transferred.digest(),
                        operator_id=84,
                        operator_login="newcorp-admin",
                        operator_association="OWNER",
                        operator_permission="admin",
                        reason="Missing prior-owner attestation.",
                        operator_attestation_sha256=_sha("newcorp-only"),
                        reconciled_effect_sha256=_sha("reconciled-effect"),
                    ),
                )

            # Negative control: migration WITHOUT reconciled_effect_sha256 while unknown_effect held fails!
            transfer_attest = compute_transfer_attestation_sha256(
                repository_id=9001,
                previous_owner_id=501,
                previous_owner_login="acme",
                new_owner_id=777,
                new_owner_login="newcorp",
                pull_request=88,
                head_sha=HEAD_SHA,
                permission_epoch=2,
                previous_owner_witness="Acme owner approved transfer to NewCorp.",
            )
            with self.assertRaisesRegex(
                ReviewInputError, "unknown native effect must be reconciled"
            ):
                app.migrate_trust_boundary(
                    repository_id=9001,
                    pull_request=88,
                    target=transferred,
                    migration=AuthenticatedTrustMigration(
                        old_snapshot_sha256=enrolled.digest(),
                        new_snapshot_sha256=transferred.digest(),
                        operator_id=84,
                        operator_login="newcorp-admin",
                        operator_association="OWNER",
                        operator_permission="admin",
                        reason="Dual-attested transfer missing reconciled effect.",
                        operator_attestation_sha256=_sha("newcorp-op"),
                        previous_owner_transfer_attestation_sha256=transfer_attest,
                    ),
                    expected_transfer_attestation_sha256=transfer_attest,
                )

            # Valid dual-attested migration + response-loss/slot reconciliation succeeds,
            # preserving 100% of blocking obligations and session charges!
            session_before = _session_with_charges(enrolled)
            receipt = app.migrate_trust_boundary(
                repository_id=9001,
                pull_request=88,
                target=transferred,
                session_record=session_before,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=transferred.digest(),
                    operator_id=84,
                    operator_login="newcorp-admin",
                    operator_association="OWNER",
                    operator_permission="admin",
                    reason="Dual-attested org transfer with reconciled effect.",
                    operator_attestation_sha256=_sha("newcorp-op"),
                    previous_owner_transfer_attestation_sha256=transfer_attest,
                    reconciled_effect_sha256=_sha("reconciled-effect"),
                ),
                expected_transfer_attestation_sha256=transfer_attest,
            )
            self.assertFalse(receipt.fresh_allowance_minted)
            self.assertEqual(receipt.migrated_session.repository, "newcorp/widgets")
            self.assertEqual(
                receipt.migrated_session.completed_initial_reviews,
                session_before.completed_initial_reviews,
            )
            self.assertEqual(len(receipt.preserved_inventory.blocking_obligations), 1)
            self.assertFalse(
                app.has_unknown_trust_effect(repository_id=9001, pull_request=88)
            )

            # Also clear G14 response-loss hold and uncertain active-window slot from Round 2a:
            app.reconcile_response_loss_hold(
                repository_id=9001,
                pull_request=88,
                head_sha=HEAD_SHA_2,
                absent_proof_sha256=_sha("absent-proof-88-head2"),
            )
            wstate = app.active_window_state(repository_id=9001, pull_request=88)
            assert wstate is not None
            for slot in wstate.display_active_findings.slots:
                if slot.state == "uncertain":
                    app.reconcile_active_window_slot(
                        repository_id=9001,
                        pull_request=88,
                        target=slot.target,
                        resolved_state="active",
                        proof_sha256=_sha("slot-active-88"),
                    )
            wstate = app.active_window_state(repository_id=9001, pull_request=88)
            assert wstate is not None
            ob = wstate.authoritative_decision_inventory.obligations[0]

            # Old slug acme/widgets is now refused because snapshot migrated to newcorp/widgets!
            with self.assertRaisesRegex(
                GitHubPublicationError, "trust-transition-refused:ownership_transfer"
            ):
                app.publish_review(
                    observed_trust=enrolled,
                    options=options,
                    oidc_token="oidc-stale-slug",
                    repository="acme/widgets",
                    repository_id=9001,
                    pull_request=88,
                    head_sha=HEAD_SHA_2,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    result=self._clean_result(),
                    diff=self.DIFF_CLEAN,
                    app_slug="reviewsensei[bot]",
                    convergence_policy=policy,
                )

            # Migrated repository newcorp/widgets publishes verified fix and converges to APPROVED!
            host.threads[0]["isResolved"] = True
            pub_recovered = app.publish_review(
                options=options,
                oidc_token="oidc-recovered",
                repository="newcorp/widgets",
                repository_id=9001,
                pull_request=88,
                head_sha=HEAD_SHA_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=self._clean_result(),
                diff=self.DIFF_CLEAN,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                observed_trust=replace(transferred, head_sha=HEAD_SHA_2),
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob.obligation_id,
                        resolution_criterion=ob.resolution_criterion,
                        head_sha=HEAD_SHA_2,
                        evidence_sha256=_sha("webhook-sig-fix-proof"),
                        verified_paths=("src/security.py",),
                    ),
                ),
            )
            self.assertEqual(pub_recovered.status, "published")
            self.assertEqual(
                pub.active_blocking_obligations_count(
                    repository_id=9001, pull_request=88
                ),
                0,
            )
            self.assertTrue(any(r["state"] == "APPROVED" for r in host.reviews))


if __name__ == "__main__":
    unittest.main()
