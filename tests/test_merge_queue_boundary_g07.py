"""G07: Explicit merge_group unsupported boundary and SHA-certification tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from pathlib import Path

from review_sensei_github import (
    GitHubMergeGroupUnsupportedError,
    GitHubWebhookError,
    InMemoryDeliveryLedger,
    SetupPlanBuilder,
    WebhookVerifier,
)
from review_sensei_github.checks import REVIEW_CHECK_NAME, required_check_identity
from review_sensei_github.event_routing_contract import (
    CertificationCandidate,
    LivePullRequestTarget,
    ReviewCertificationReceipt,
    inspect_caller_subscription_contract,
    is_merge_queue_ref,
    reconcile_merge_queue_eviction,
    reject_merge_group_event,
    verify_snapshot_certification,
)
from strict_workflow_yaml import load_workflow

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA_1 = "1" * 40
BASE_SHA_2 = "2" * 40
PR_HEAD_SHA = "a" * 40
PR_HEAD_SHA_2 = "b" * 40
QUEUE_SYNTHETIC_SHA = "f" * 40
RESULT_DIGEST = "d" * 64


class _SecretSource:
    def __init__(self, secret: bytes = b"merge-queue-test-secret") -> None:
        self._secret = secret

    def load_webhook_secret(self) -> bytes:
        return self._secret


def _sign(body: bytes, secret: bytes = b"merge-queue-test-secret") -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


class MergeQueueBoundaryG07Tests(unittest.TestCase):
    def test_caller_workflows_and_setup_builder_exclude_merge_group(self) -> None:
        checked_in_path = ROOT / ".github/workflows/review-sensei-review.yml"
        example_path = ROOT / "examples/github-actions/review-sensei-review.yml"

        for path in (checked_in_path, example_path):
            with self.subTest(path=path.as_posix()):
                content = path.read_text(encoding="utf-8")
                parsed = load_workflow(content)
                triggers = parsed.get("on") or parsed[True]
                self.assertNotIn("merge_group", triggers)
                report = inspect_caller_subscription_contract(content)
                self.assertTrue(report.matches_approved_boundary)
                self.assertNotIn("merge_group", report.unapproved_triggers)

        plan = SetupPlanBuilder().build(
            "owner/repo",
            base_branch="main",
            base_sha=BASE_SHA_1,
        )
        generated_caller = {f.path: f.content for f in plan.files}[
            ".github/workflows/review-sensei-review.yml"
        ]
        self.assertNotIn("merge_group", generated_caller)
        self.assertTrue(
            inspect_caller_subscription_contract(
                generated_caller
            ).matches_approved_boundary
        )

    def test_webhook_verifier_and_router_reject_merge_group_with_zero_effects(
        self,
    ) -> None:
        ledger = InMemoryDeliveryLedger(max_entries=100)
        verifier = WebhookVerifier(
            app_id=321,
            secret_source=_SecretSource(),
            ledger=ledger,
        )

        for idx, action in enumerate(("checks_requested", "destroyed"), start=1):
            payload = {
                "action": action,
                "merge_group": {
                    "head_sha": QUEUE_SYNTHETIC_SHA,
                    "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-aaaaaaaa",
                    "base_sha": BASE_SHA_2,
                    "base_ref": "refs/heads/main",
                },
                "installation": {"id": 7, "suspended_at": None},
                "repository": {"id": 101, "full_name": "owner/repo"},
            }
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            delivery_id = f"deliv-mg-{idx}"

            with self.assertRaises(GitHubMergeGroupUnsupportedError) as ctx:
                verifier.verify(
                    body=raw,
                    signature=_sign(raw),
                    event="merge_group",
                    delivery_id=delivery_id,
                )
            self.assertIsInstance(ctx.exception, GitHubWebhookError)
            self.assertEqual(ctx.exception.error_category, "github_webhook")
            self.assertEqual(ctx.exception.outcome_code, "merge_group_unsupported")
            self.assertFalse(ledger.is_known(app_id=321, delivery_id=delivery_id))

            rejection = reject_merge_group_event(raw, delivery_id=delivery_id)
            self.assertEqual(rejection.outcome_code, "merge_group_unsupported")
            self.assertEqual(rejection.merge_group_action, action)
            self.assertEqual(rejection.queue_head_sha, QUEUE_SYNTHETIC_SHA)
            self.assertEqual(rejection.queue_base_sha, BASE_SHA_2)
            self.assertFalse(rejection.token_acquired)
            self.assertFalse(rejection.provider_called)
            self.assertFalse(rejection.ledger_mutated)
            self.assertFalse(rejection.check_published)

        missing_action_mg = reject_merge_group_event({})
        self.assertEqual(missing_action_mg.outcome_code, "merge_group_unsupported")
        self.assertEqual(missing_action_mg.merge_group_action, "<missing>")

        malformed_mg = reject_merge_group_event(b"{invalid-json")
        self.assertEqual(malformed_mg.outcome_code, "merge_group_payload_unparseable")
        self.assertEqual(malformed_mg.merge_group_action, "<invalid>")

    def test_pr_head_receipt_never_certifies_merge_queue_sha_ref_or_multi_pr_group(
        self,
    ) -> None:
        check_id = required_check_identity(app_slug="reviewsensei")
        self.assertEqual(
            check_id, {"name": REVIEW_CHECK_NAME, "producer": "reviewsensei"}
        )

        receipt = ReviewCertificationReceipt(
            repository_id=101,
            pull_request_number=42,
            reviewed_base_sha=BASE_SHA_1,
            reviewed_head_sha=PR_HEAD_SHA,
            result_digest=RESULT_DIGEST,
            approved=True,
            check_conclusion="success",
        )

        # Positive control: exact single-PR (base_sha, head_sha) is certified.
        exact_pr = CertificationCandidate(
            repository_id=101,
            base_sha=BASE_SHA_1,
            head_sha=PR_HEAD_SHA,
            head_ref="feat/safe-branch",
            pull_request_numbers=(42,),
            is_merge_group=False,
        )
        exact_verdict = verify_snapshot_certification(receipt, exact_pr)
        self.assertTrue(exact_verdict.certified)
        self.assertEqual(exact_verdict.outcome_code, "exact_pr_snapshot_certified")
        self.assertEqual(exact_verdict.check_name, REVIEW_CHECK_NAME)

        # Negative 1: synthetic merge-queue commit SHA on merge_group ref.
        self.assertTrue(is_merge_queue_ref("gh-readonly-queue/main/pr-42-aaaaaaaa"))
        self.assertTrue(
            is_merge_queue_ref("refs/heads/gh-readonly-queue/main/pr-42-aaaaaaaa")
        )
        self.assertFalse(is_merge_queue_ref("feat/safe-branch"))
        self.assertFalse(is_merge_queue_ref(None))

        queue_ref_candidate = CertificationCandidate(
            repository_id=101,
            base_sha=BASE_SHA_1,
            head_sha=PR_HEAD_SHA,
            head_ref="gh-readonly-queue/main/pr-42-aaaaaaaa",
            pull_request_numbers=(42,),
            is_merge_group=False,
        )
        queue_ref_verdict = verify_snapshot_certification(receipt, queue_ref_candidate)
        self.assertFalse(queue_ref_verdict.certified)
        self.assertEqual(queue_ref_verdict.outcome_code, "merge_group_unsupported")

        # Negative 2: explicit merge_group flag even with synthetic SHA.
        merge_group_candidate = CertificationCandidate(
            repository_id=101,
            base_sha=BASE_SHA_2,
            head_sha=QUEUE_SYNTHETIC_SHA,
            head_ref="refs/heads/gh-readonly-queue/main/pr-42-bbbbbbbb",
            pull_request_numbers=(42, 43),
            is_merge_group=True,
        )
        mg_verdict = verify_snapshot_certification(receipt, merge_group_candidate)
        self.assertFalse(mg_verdict.certified)
        self.assertEqual(mg_verdict.outcome_code, "merge_group_unsupported")

        # Negative 3: queue-generated commit SHA presented as a branch candidate.
        synthetic_sha_candidate = CertificationCandidate(
            repository_id=101,
            base_sha=BASE_SHA_1,
            head_sha=QUEUE_SYNTHETIC_SHA,
            head_ref="feat/safe-branch",
            pull_request_numbers=(42,),
        )
        sha_verdict = verify_snapshot_certification(receipt, synthetic_sha_candidate)
        self.assertFalse(sha_verdict.certified)
        self.assertEqual(sha_verdict.outcome_code, "head_sha_mismatch_uncertified")

        # Negative 4: multi-PR group without queue ref still fails single-PR binding.
        multi_pr_candidate = CertificationCandidate(
            repository_id=101,
            base_sha=BASE_SHA_1,
            head_sha=PR_HEAD_SHA,
            head_ref="feat/safe-branch",
            pull_request_numbers=(42, 43),
        )
        multi_verdict = verify_snapshot_certification(receipt, multi_pr_candidate)
        self.assertFalse(multi_verdict.certified)
        self.assertEqual(
            multi_verdict.outcome_code, "multi_pr_or_mismatched_pr_unauthenticated"
        )

        # Negative 5: base SHA drift, repository mismatch, and withheld receipt.
        base_drift_verdict = verify_snapshot_certification(
            receipt,
            CertificationCandidate(
                repository_id=101,
                base_sha=BASE_SHA_2,
                head_sha=PR_HEAD_SHA,
                head_ref="feat/safe-branch",
                pull_request_numbers=(42,),
            ),
        )
        self.assertFalse(base_drift_verdict.certified)
        self.assertEqual(
            base_drift_verdict.outcome_code, "base_sha_mismatch_uncertified"
        )

        repo_mismatch_verdict = verify_snapshot_certification(
            receipt,
            CertificationCandidate(
                repository_id=999,
                base_sha=BASE_SHA_1,
                head_sha=PR_HEAD_SHA,
                head_ref="feat/safe-branch",
                pull_request_numbers=(42,),
            ),
        )
        self.assertFalse(repo_mismatch_verdict.certified)
        self.assertEqual(repo_mismatch_verdict.outcome_code, "repository_mismatch")

        withheld_receipt = ReviewCertificationReceipt(
            repository_id=101,
            pull_request_number=42,
            reviewed_base_sha=BASE_SHA_1,
            reviewed_head_sha=PR_HEAD_SHA,
            result_digest=RESULT_DIGEST,
            approved=False,
            check_conclusion="failure",
        )
        withheld_verdict = verify_snapshot_certification(withheld_receipt, exact_pr)
        self.assertFalse(withheld_verdict.certified)
        self.assertEqual(withheld_verdict.outcome_code, "receipt_not_certifying")

        with self.assertRaises(ValueError):
            ReviewCertificationReceipt(
                repository_id=101,
                pull_request_number=42,
                reviewed_base_sha=BASE_SHA_1,
                reviewed_head_sha=PR_HEAD_SHA,
                result_digest="short",
                approved=True,
                check_conclusion="success",
            )

    def test_pr_head_recovery_remains_unaffected_after_merge_queue_removal(
        self,
    ) -> None:
        destroyed_payload = {
            "action": "destroyed",
            "reason": "dequeued",
            "merge_group": {
                "head_sha": QUEUE_SYNTHETIC_SHA,
                "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-aaaaaaaa",
                "base_sha": BASE_SHA_2,
            },
        }
        receipt = ReviewCertificationReceipt(
            repository_id=101,
            pull_request_number=42,
            reviewed_base_sha=BASE_SHA_1,
            reviewed_head_sha=PR_HEAD_SHA,
            result_digest=RESULT_DIGEST,
            approved=True,
            check_conclusion="success",
        )
        unchanged_pr = LivePullRequestTarget(
            repository_id=101,
            pull_request_number=42,
            base_sha=BASE_SHA_1,
            base_ref="main",
            head_sha=PR_HEAD_SHA,
            head_ref="feat/safe-branch",
        )

        intact = reconcile_merge_queue_eviction(
            destroyed_payload=destroyed_payload,
            live_pr_target=unchanged_pr,
            pr_receipt=receipt,
        )
        self.assertEqual(
            intact.outcome_code, "pr_head_certification_intact_after_queue_eviction"
        )
        self.assertTrue(intact.pr_certification_intact)
        self.assertFalse(intact.requires_new_review)
        self.assertIsNone(intact.recovery_command)

        with_open_findings = reconcile_merge_queue_eviction(
            destroyed_payload=destroyed_payload,
            live_pr_target=unchanged_pr,
            pr_receipt=ReviewCertificationReceipt(
                repository_id=101,
                pull_request_number=42,
                reviewed_base_sha=BASE_SHA_1,
                reviewed_head_sha=PR_HEAD_SHA,
                result_digest=RESULT_DIGEST,
                approved=False,
                check_conclusion="failure",
            ),
            open_finding_ids=("rs-open-7",),
        )
        self.assertEqual(
            with_open_findings.outcome_code,
            "pr_head_obligations_preserved_after_queue_eviction",
        )
        self.assertFalse(with_open_findings.pr_certification_intact)
        self.assertFalse(with_open_findings.requires_new_review)
        self.assertEqual(with_open_findings.preserved_open_finding_ids, ("rs-open-7",))
        self.assertEqual(with_open_findings.recovery_command, "@reviewsensei verify")

        advanced_pr = LivePullRequestTarget(
            repository_id=101,
            pull_request_number=42,
            base_sha=BASE_SHA_2,
            base_ref="main",
            head_sha=PR_HEAD_SHA_2,
            head_ref="feat/safe-branch",
        )
        advanced = reconcile_merge_queue_eviction(
            destroyed_payload=destroyed_payload,
            live_pr_target=advanced_pr,
            pr_receipt=receipt,
            open_finding_ids=("rs-open-7",),
        )
        self.assertEqual(
            advanced.outcome_code,
            "pr_head_advanced_after_queue_eviction_requires_review",
        )
        self.assertFalse(advanced.pr_certification_intact)
        self.assertTrue(advanced.requires_new_review)
        self.assertEqual(advanced.preserved_open_finding_ids, ("rs-open-7",))
        self.assertEqual(advanced.recovery_command, "workflow_dispatch")

        unreviewed = reconcile_merge_queue_eviction(
            destroyed_payload=destroyed_payload,
            live_pr_target=unchanged_pr,
            pr_receipt=None,
        )
        self.assertEqual(unreviewed.outcome_code, "pr_unreviewed_after_queue_eviction")
        self.assertTrue(unreviewed.requires_new_review)
        self.assertEqual(unreviewed.recovery_command, "workflow_dispatch")

        with self.assertRaises(ValueError):
            reconcile_merge_queue_eviction(
                destroyed_payload=b"{invalid-json",
                live_pr_target=unchanged_pr,
                pr_receipt=receipt,
            )

    def test_real_trigger_cli_webhook_route_and_action_run_refuse_merge_group(
        self,
    ) -> None:
        import os
        import tempfile
        from unittest.mock import patch

        from review_sensei_github import action_run
        from review_sensei_github.event_routing_contract import (
            map_installed_caller_subscription,
        )

        from review_sensei.hosting.github import trigger as hosting_trigger

        checked_in = (ROOT / ".github/workflows/review-sensei-review.yml").read_text(
            encoding="utf-8"
        )
        mg_payload = {
            "action": "checks_requested",
            "merge_group": {
                "head_sha": QUEUE_SYNTHETIC_SHA,
                "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-aaaaaaaa",
                "base_sha": BASE_SHA_2,
                "base_ref": "refs/heads/main",
            },
            "repository": {"id": 101, "full_name": "owner/repo"},
        }

        # 1. Installed caller mapping marks merge_group unsubscribed with zero effects
        mapped = map_installed_caller_subscription(
            checked_in,
            event_name="merge_group",
            payload=mg_payload,
        )
        self.assertFalse(mapped.caller_job_admitted)
        self.assertEqual(mapped.routing.rule_id, "merge_group.queue")
        self.assertEqual(mapped.routing.outcome_code, "merge_group_unsupported")
        self.assertIsNone(mapped.reusable_workflow_entrypoint)
        self.assertFalse(mapped.performs_native_effects)
        self.assertFalse(mapped.requires_model_inference)

        # 2. WebhookVerifier.verify_event_route supports both truthful classification and strict refusal
        verifier = WebhookVerifier(app_id=321, secret_source=_SecretSource())
        raw = json.dumps(mg_payload, separators=(",", ":")).encode("utf-8")
        sig = _sign(raw)
        routed = verifier.verify_event_route(
            body=raw,
            signature=sig,
            event="merge_group",
            delivery_id="deliv-mg-route-1",
            reject_merge_group=False,
        )
        self.assertFalse(routed.routing.admitted)
        self.assertEqual(routed.routing.outcome_code, "merge_group_unsupported")

        with self.assertRaises(GitHubMergeGroupUnsupportedError):
            verifier.verify_event_route(
                body=raw,
                signature=sig,
                event="merge_group",
                delivery_id="deliv-mg-route-2",
                reject_merge_group=True,
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            pull_file = tmp_dir / "pull.json"
            out_file = tmp_dir / "out.txt"
            pull_file.write_text(
                json.dumps(
                    {
                        "number": 42,
                        "head": {"sha": PR_HEAD_SHA, "ref": "feat/safe-branch"},
                        "base": {"sha": BASE_SHA_1, "ref": "main"},
                    }
                ),
                encoding="utf-8",
            )

            # 3. Real hosting/github/trigger.py CLI refuses merge_group
            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(out_file)}, clear=False),
                self.assertRaises(SystemExit) as exit_ctx,
            ):
                hosting_trigger.main(
                    [
                        "--event",
                        "merge_group",
                        "--pull-json",
                        str(pull_file),
                    ]
                )
            self.assertIn("unsupported event: merge_group", str(exit_ctx.exception))

            # 4. Real action_run.run_action refuses merge_group before bootstrap/OIDC/checks
            mg_file = tmp_dir / "mg-event.json"
            mg_file.write_bytes(raw)
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_EVENT_NAME": "merge_group",
                        "GITHUB_EVENT_PATH": str(mg_file),
                    },
                    clear=False,
                ),
                self.assertRaises(GitHubMergeGroupUnsupportedError) as run_ctx,
            ):
                action_run.run_action(
                    [
                        "--policy-commit",
                        BASE_SHA_1,
                        "--run-directory",
                        str(tmp_dir),
                        "--",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "101",
                        "--pull-request",
                        "42",
                        "--head-sha",
                        QUEUE_SYNTHETIC_SHA,
                        "--source-comment-id",
                        "1",
                        "--comment-body",
                        "@reviewsensei verify",
                        "--actor",
                        "alice",
                        "--association",
                        "OWNER",
                        "--github-session-ledger",
                        "--allow-write",
                    ],
                    purpose="command",
                )
            self.assertEqual(run_ctx.exception.outcome_code, "merge_group_unsupported")


if __name__ == "__main__":
    unittest.main()
