"""G21: Default-branch policy commit drift fail-closed guard and fresh-admission recovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import action_reply
from review_sensei_github.api_run_history import read_previous_run
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from test_action_reply_entry import ReplyEntryFixture
from test_action_run import CommandEntryFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_review_lifecycle_diagnostics import outcome
from test_review_run import EntryFixture
from test_review_run_rescan import partial_fixture, request

from review_sensei import cli as core_cli
from review_sensei.api_work import ApiRunAccounting, FrozenApiConfiguration
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.budgets import LEGACY_ASSESSMENT_PROMPT_BYTES
from review_sensei.configuration import load_configuration
from review_sensei.context import RepositoryContextStore, admit_prompt_context
from review_sensei.diff import analyze_diff
from review_sensei.document_context import DocumentDecision, DocumentSelectionReport
from review_sensei.errors import ReviewInputError
from review_sensei.models import (
    ProviderResponse,
    ReviewLensContext,
    ReviewLimits,
    ReviewRequest,
)
from review_sensei.operational_boundaries import (
    PolicyDriftClassification,
    PolicyFundingPreservationProof,
    classify_policy_commit_drift,
    verify_policy_drift_funding_preservation,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.service import DEFAULT_STAGES, ReviewService
from review_sensei.stages import ContextDocumentSource, ReviewCategory, Stage


def _disable_crlf_conversion(repo_root: Any) -> None:
    """Prevent Windows autocrlf from making the working copy look dirty."""
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )


def _checked_out_head(repo_root: Any) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def _advance_remote_default_branch(
    repo_root: Any,
    *,
    frozen_commit: str,
    filename: str,
    content: bytes,
    message: str,
) -> str:
    """Advance remote main to P' while keeping the runner's frozen checkout at P.

    ``review_run.bootstrap`` authenticates both the local Git checkout in
    ``repo_root`` (``git rev-parse HEAD`` and ``git show <P>:.reviewsensei.yml``)
    and the remote HTTP ``/git/ref/heads/main`` endpoint. Creating ``P'`` in the
    fixture repository and detaching back to ``P`` exercises both boundaries.
    """
    _disable_crlf_conversion(repo_root)
    target = repo_root / filename
    target.write_bytes(content)
    subprocess.run(
        ["git", "-C", str(repo_root), "add", filename],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )
    live_sha = _checked_out_head(repo_root)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "checkout",
            "--force",
            "--quiet",
            "--detach",
            frozen_commit,
        ],
        check=True,
        capture_output=True,
    )
    return live_sha


def _checkout_fresh_policy(repo_root: Any, policy_commit: str) -> None:
    _disable_crlf_conversion(repo_root)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "checkout",
            "--force",
            "--quiet",
            "--detach",
            policy_commit,
        ],
        check=True,
        capture_output=True,
    )


class _RefOverrideFixture(EntryFixture):
    def __init__(self, directory: str, *, ref_payload: Mapping[str, object]) -> None:
        super().__init__(directory)
        self.ref_payload = ref_payload

    def external(self, req: Any, timeout: Any) -> Any:
        path = urlsplit(req.full_url).path
        if path == "/repos/owner/repo/git/ref/heads/main":
            self.trace.append((self.attempt, req.get_method(), path))
            return json_response(self.ref_payload)
        return super().external(req, timeout)


class PolicyCommitDriftG21Tests(unittest.TestCase):
    def test_unrelated_default_branch_advance_with_identical_config_refuses_and_recovers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            _disable_crlf_conversion(fixture.root)
            frozen_p = fixture.policy
            original_config = fixture.config_bytes

            # Advance default branch with an unrelated README edit; .reviewsensei.yml is identical!
            live_p = _advance_remote_default_branch(
                fixture.root,
                frozen_commit=frozen_p,
                filename="README.md",
                content=b"# Unrelated documentation update\n",
                message="docs: unrelated advance",
            )
            self.assertNotEqual(frozen_p, live_p)
            self.assertEqual(_checked_out_head(fixture.root), frozen_p)
            self.assertFalse((fixture.root / "README.md").exists())

            drift = classify_policy_commit_drift(
                frozen_policy_commit=frozen_p,
                default_ref_object={"type": "commit", "sha": live_p},
                reviewed_base_sha=BASE,
                reviewed_head_sha=HEAD,
                frozen_policy_bytes=original_config,
                live_policy_bytes=original_config,
            )
            self.assertFalse(drift.admitted)
            self.assertEqual(drift.drift_kind, "unrelated_default_branch_advance")
            self.assertTrue(drift.policy_content_identical)
            self.assertTrue(drift.policy_distinct_from_reviewed_base)
            self.assertTrue(drift.refuses_before_enrollment)
            self.assertTrue(drift.zero_ledger_mutation)
            self.assertTrue(drift.zero_provider_spend)
            self.assertFalse(drift.rebinds_prior_accounting)
            self.assertEqual(drift.recovery_policy_commit, live_p)

            # Hosted run pinned to stale P while live default branch is at live_p:
            fixture.policy = frozen_p
            fixture.policy_ref = live_p
            self.assertEqual(fixture.invoke(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", fixture.error
            )
            diag = outcome(self, fixture.error)
            self.assertEqual(diag["step"], "default_policy_ref")
            self.assertEqual(diag["scope"]["observed_policy"]["policy_ref"], "main")
            self.assertEqual(diag["scope"]["observed_policy"]["policy_commit"], live_p)
            self.assertIsNone(diag["scope"]["observed_policy"]["policy_content_sha256"])
            self.assertEqual(fixture.provider_calls, 0)
            self.assertIsNone(fixture.comment)
            # Refused BEFORE fetching /contents/.reviewsensei.yml or broker token!
            self.assertFalse(
                any(
                    "/contents/.reviewsensei.yml" in path or "/api/github/token" in path
                    for _, _, path in fixture.trace
                )
            )

            # Deterministic recovery: fresh run dispatched against live_p (P') succeeds cleanly!
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            _checkout_fresh_policy(fixture.root, live_p)
            self.assertEqual(_checked_out_head(fixture.root), live_p)
            fixture.policy = live_p
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            assert record is not None and record.transaction is not None
            self.assertEqual(record.transaction.phase, "publication_succeeded")

    def test_policy_content_mutation_refuses_without_rebinding_and_recovers_on_p_prime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            _disable_crlf_conversion(fixture.root)
            frozen_p = fixture.policy
            old_bytes = fixture.config_bytes
            new_bytes = old_bytes + b"\n# policy comment update\n"
            live_p = _advance_remote_default_branch(
                fixture.root,
                frozen_commit=frozen_p,
                filename=".reviewsensei.yml",
                content=new_bytes,
                message="chore: update .reviewsensei.yml",
            )
            fixture.config_bytes = new_bytes

            drift = classify_policy_commit_drift(
                frozen_policy_commit=frozen_p,
                default_ref_object={"type": "commit", "sha": live_p},
                reviewed_base_sha=BASE,
                reviewed_head_sha=HEAD,
                frozen_policy_bytes=old_bytes,
                live_policy_bytes=new_bytes,
            )
            self.assertFalse(drift.admitted)
            self.assertEqual(drift.drift_kind, "policy_content_changed")
            self.assertIs(drift.policy_content_identical, False)
            self.assertFalse(drift.rebinds_prior_accounting)
            self.assertEqual(drift.recovery_policy_commit, live_p)

            unverified_drift = classify_policy_commit_drift(
                frozen_policy_commit=frozen_p,
                default_ref_object={"type": "commit", "sha": live_p},
                reviewed_base_sha=BASE,
                reviewed_head_sha=HEAD,
            )
            self.assertEqual(unverified_drift.drift_kind, "policy_content_changed")
            self.assertIsNone(unverified_drift.policy_content_identical)

            fixture.policy = frozen_p
            fixture.policy_ref = live_p
            self.assertEqual(fixture.invoke(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", fixture.error
            )
            self.assertEqual(fixture.provider_calls, 0)
            self.assertIsNone(fixture.comment)

            # Successor run pinned to P' loads the updated policy cleanly and settles:
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            _checkout_fresh_policy(fixture.root, live_p)
            self.assertEqual(_checked_out_head(fixture.root), live_p)
            fixture.policy = live_p
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            assert record is not None and record.transaction is not None
            self.assertEqual(record.transaction.phase, "publication_succeeded")

    def test_malformed_non_commit_default_ref_objects_fail_closed(self) -> None:
        valid_p = "1" * 40
        for bad_ref in (
            None,
            "not-a-dict",
            {},
            {"type": "tag", "sha": valid_p},
            {"type": "tree", "sha": valid_p},
            {"type": "commit", "sha": "not-a-40-hex-sha"},
            {"type": "commit", "sha": None},
        ):
            with self.subTest(bad_ref=bad_ref):
                drift = classify_policy_commit_drift(
                    frozen_policy_commit=valid_p,
                    default_ref_object=bad_ref,
                    reviewed_base_sha=BASE,
                    reviewed_head_sha=HEAD,
                )
                self.assertFalse(drift.admitted)
                self.assertEqual(drift.drift_kind, "malformed_policy_ref")
                self.assertIsNone(drift.live_policy_commit)
                self.assertIsNone(drift.recovery_policy_commit)
                self.assertTrue(drift.refuses_before_enrollment)

        for payload in (
            {"object": {"type": "tag", "sha": "a" * 40}},
            {"object": {"type": "commit", "sha": "short-sha"}},
            {"ref": "refs/heads/main"},
        ):
            with (
                self.subTest(payload=payload),
                tempfile.TemporaryDirectory() as directory,
            ):
                drift = classify_policy_commit_drift(
                    frozen_policy_commit=valid_p,
                    default_ref_object=payload.get("object"),
                    reviewed_base_sha=BASE,
                    reviewed_head_sha=HEAD,
                )
                self.assertFalse(drift.admitted)
                self.assertEqual(drift.drift_kind, "malformed_policy_ref")
                self.assertIsNone(drift.live_policy_commit)
                self.assertIsNone(drift.recovery_policy_commit)
                self.assertTrue(drift.refuses_before_enrollment)

                fixture = _RefOverrideFixture(directory, ref_payload=payload)
                self.assertEqual(fixture.invoke(), 1)
                self.assertIn(
                    "trusted policy commit changed before admission", fixture.error
                )
                diag = outcome(self, fixture.error)
                self.assertEqual(diag["step"], "default_policy_ref")
                observed_policy = diag["scope"]["observed_policy"]
                obj = payload.get("object")
                if isinstance(obj, dict) and obj.get("type") == "commit":
                    self.assertIsNotNone(observed_policy)
                    self.assertIsNone(observed_policy["policy_content_sha256"])
                else:
                    self.assertIsNone(observed_policy)
                self.assertTrue(
                    any(
                        path == "/repos/owner/repo/git/ref/heads/main"
                        for _, _, path in fixture.trace
                    )
                )
                self.assertFalse(
                    any(
                        "/contents/.reviewsensei.yml" in path
                        or "/api/github/token" in path
                        for _, _, path in fixture.trace
                    )
                )
                self.assertEqual(fixture.provider_calls, 0)
                self.assertIsNone(fixture.comment)

    def test_policy_commit_p_is_independent_of_reviewed_base_b_and_head_h(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            _disable_crlf_conversion(fixture.root)
            # P == P_live while P != B and P != H: admitted cleanly!
            self.assertNotEqual(fixture.policy, BASE)
            self.assertNotEqual(fixture.policy, HEAD)
            admitted = classify_policy_commit_drift(
                frozen_policy_commit=fixture.policy,
                default_ref_object={"type": "commit", "sha": fixture.policy_ref},
                reviewed_base_sha=BASE,
                reviewed_head_sha=HEAD,
            )
            self.assertTrue(admitted.admitted)
            self.assertEqual(admitted.drift_kind, "admitted")
            self.assertTrue(admitted.policy_distinct_from_reviewed_base)
            self.assertFalse(admitted.to_dict()["rebinds_prior_accounting"])

            # Now keep P == P_live while live PR base_sha drifts (B != B_live):
            # default_policy_ref and policy_content PASS, and live_pr catches B != B_live!
            fixture.live_base = "9" * 40
            self.assertEqual(fixture.invoke(), 1)
            self.assertIn("live reviewed base or head conflicts", fixture.error)
            diag = outcome(self, fixture.error)
            self.assertEqual(diag["step"], "live_pr")
            traced_paths = [path for _, _, path in fixture.trace]
            self.assertTrue(
                any("/contents/.reviewsensei.yml" in path for path in traced_paths)
            )
            self.assertTrue(any(path.endswith("/pulls/7") for path in traced_paths))
            self.assertEqual(fixture.provider_calls, 0)

    def test_prior_settled_and_pending_session_records_survive_drift_and_recover(
        self,
    ) -> None:
        # 1. Settled session + maintainer command drift & recovery:
        with tempfile.TemporaryDirectory() as directory:
            cmd_fixture = CommandEntryFixture(directory)
            _disable_crlf_conversion(cmd_fixture.root)
            self.assertEqual(cmd_fixture.invoke(), 0, cmd_fixture.error)
            settled_snapshot = cmd_fixture.comment["body"]
            settled_record = parse_session_comment(settled_snapshot, identity=IDENTITY)
            assert settled_record is not None and settled_record.transaction is not None
            self.assertEqual(settled_record.transaction.phase, "publication_succeeded")

            stale_p = cmd_fixture.policy
            live_p = _advance_remote_default_branch(
                cmd_fixture.root,
                frozen_commit=stale_p,
                filename="CHANGELOG.md",
                content=b"## Unreleased\n",
                message="docs: changelog",
            )
            cmd_fixture.policy = stale_p
            cmd_fixture.policy_ref = live_p
            cmd_fixture.select_command("@reviewsensei review pause", new_run=True)
            self.assertEqual(cmd_fixture.invoke_command(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", cmd_fixture.error
            )
            # Settled session comment is byte-for-byte untouched!
            self.assertEqual(cmd_fixture.comment["body"], settled_snapshot)
            self.assertEqual(cmd_fixture.business_writes, 0)
            self.assertEqual(cmd_fixture.broker_requests, [])

            # Re-dispatch command pinned to live_p -> recovers and settles cleanly!
            cmd_fixture.select_command("@reviewsensei review pause", new_run=True)
            _checkout_fresh_policy(cmd_fixture.root, live_p)
            cmd_fixture.policy = live_p
            self.assertEqual(cmd_fixture.invoke_command(), 0, cmd_fixture.error)
            after_cmd = parse_session_comment(
                cmd_fixture.comment["body"], identity=IDENTITY
            )
            assert after_cmd is not None
            self.assertTrue(after_cmd.operator_paused)

        # 2. Partial review with 20 retained dispositions survives P != P_live and recovers via rescan on P':
        with tempfile.TemporaryDirectory() as directory:
            part_fixture, _source = partial_fixture(directory)
            _disable_crlf_conversion(part_fixture.root)
            retained_snapshot = part_fixture.comment["body"]
            stale_p = part_fixture.policy
            live_p = _advance_remote_default_branch(
                part_fixture.root,
                frozen_commit=stale_p,
                filename="NOTES.md",
                content=b"Policy branch note\n",
                message="docs: note",
            )
            request(part_fixture, 21, "rescan")
            part_fixture.policy = stale_p
            part_fixture.policy_ref = live_p
            part_fixture.trace.clear()
            calls_before = part_fixture.provider_calls
            self.assertEqual(part_fixture.invoke(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", part_fixture.error
            )
            part_diag = outcome(self, part_fixture.error)
            self.assertEqual(part_diag["step"], "default_policy_ref")
            self.assertEqual(part_fixture.provider_calls, calls_before)
            self.assertEqual(part_fixture.comment["body"], retained_snapshot)
            self.assertFalse(
                any(
                    (method in {"POST", "PATCH"} and "/issues/" in path)
                    or "/api/github/token" in path
                    for _, method, path in part_fixture.trace
                )
            )

            # Re-dispatch rescan pinned to live_p -> preserves all 20 dispositions and completes!
            part_fixture.provider_comments = []
            request(part_fixture, 22, "rescan")
            _checkout_fresh_policy(part_fixture.root, live_p)
            part_fixture.policy = live_p
            self.assertEqual(part_fixture.invoke(), 0, part_fixture.error)
            recovered = parse_session_comment(
                part_fixture.comment["body"], identity=IDENTITY
            )
            assert recovered is not None and recovered.transaction is not None
            self.assertEqual(recovered.transaction.phase, "publication_succeeded")
            self.assertEqual(len(recovered.dispositions), 20)

    def test_policy_drift_validation_guards_fail_closed(self) -> None:
        with self.assertRaises(ReviewInputError):
            classify_policy_commit_drift(
                frozen_policy_commit="invalid",
                default_ref_object={"type": "commit", "sha": "1" * 40},
                reviewed_base_sha=BASE,
                reviewed_head_sha=HEAD,
            )
        with self.assertRaises(ReviewInputError):
            classify_policy_commit_drift(
                frozen_policy_commit="1" * 40,
                default_ref_object={"type": "commit", "sha": "1" * 40},
                reviewed_base_sha=BASE,
                reviewed_head_sha=HEAD,
                frozen_policy_bytes="not-bytes",  # type: ignore[arg-type]
            )
        with self.assertRaises(ReviewInputError):
            classify_policy_commit_drift(
                frozen_policy_commit="1" * 40,
                default_ref_object={"type": "commit", "sha": "1" * 40},
                reviewed_base_sha=BASE,
                reviewed_head_sha=HEAD,
                live_policy_bytes="not-bytes",  # type: ignore[arg-type]
            )

        # Asymmetric policy byte availability (frozen set / live omitted, or vice versa)
        # refuses with policy_content_identical=None rather than claiming identity.
        for frozen_b, live_b in ((b"schema: 1\n", None), (None, b"schema: 1\n")):
            with self.subTest(frozen_b=frozen_b, live_b=live_b):
                asym = classify_policy_commit_drift(
                    frozen_policy_commit="1" * 40,
                    default_ref_object={"type": "commit", "sha": "2" * 40},
                    reviewed_base_sha=BASE,
                    reviewed_head_sha=HEAD,
                    frozen_policy_bytes=frozen_b,
                    live_policy_bytes=live_b,
                )
                self.assertFalse(asym.admitted)
                self.assertEqual(asym.drift_kind, "policy_content_changed")
                self.assertIsNone(asym.policy_content_identical)
                self.assertEqual(asym.recovery_policy_commit, "2" * 40)

        for bad_cls_kwargs in (
            # rebinds_prior_accounting=True is always forbidden
            {
                "drift_kind": "policy_content_changed",
                "admitted": False,
                "frozen_policy_commit": "1" * 40,
                "live_policy_commit": "2" * 40,
                "reviewed_base_sha": BASE,
                "reviewed_head_sha": HEAD,
                "policy_distinct_from_reviewed_base": True,
                "policy_content_identical": False,
                "refuses_before_enrollment": True,
                "zero_ledger_mutation": True,
                "zero_provider_spend": True,
                "rebinds_prior_accounting": True,
                "recovery_policy_commit": "2" * 40,
                "recovery_instruction": "re-run",
                "diagnostic": "forbidden rebind",
            },
            # admitted=True carrying recovery_policy_commit
            {
                "drift_kind": "admitted",
                "admitted": True,
                "frozen_policy_commit": "1" * 40,
                "live_policy_commit": "1" * 40,
                "reviewed_base_sha": BASE,
                "reviewed_head_sha": HEAD,
                "policy_distinct_from_reviewed_base": True,
                "policy_content_identical": True,
                "refuses_before_enrollment": False,
                "zero_ledger_mutation": False,
                "zero_provider_spend": False,
                "rebinds_prior_accounting": False,
                "recovery_policy_commit": "1" * 40,
                "recovery_instruction": None,
                "diagnostic": "admitted must not expose recovery_policy_commit",
            },
            # refused drift with recovery_policy_commit != live_policy_commit
            {
                "drift_kind": "policy_content_changed",
                "admitted": False,
                "frozen_policy_commit": "1" * 40,
                "live_policy_commit": "2" * 40,
                "reviewed_base_sha": BASE,
                "reviewed_head_sha": HEAD,
                "policy_distinct_from_reviewed_base": True,
                "policy_content_identical": False,
                "refuses_before_enrollment": True,
                "zero_ledger_mutation": True,
                "zero_provider_spend": True,
                "rebinds_prior_accounting": False,
                "recovery_policy_commit": "3" * 40,
                "recovery_instruction": "re-run",
                "diagnostic": "mismatched recovery_policy_commit",
            },
            # refused drift missing recovery_instruction
            {
                "drift_kind": "policy_content_changed",
                "admitted": False,
                "frozen_policy_commit": "1" * 40,
                "live_policy_commit": "2" * 40,
                "reviewed_base_sha": BASE,
                "reviewed_head_sha": HEAD,
                "policy_distinct_from_reviewed_base": True,
                "policy_content_identical": False,
                "refuses_before_enrollment": True,
                "zero_ledger_mutation": True,
                "zero_provider_spend": True,
                "rebinds_prior_accounting": False,
                "recovery_policy_commit": "2" * 40,
                "recovery_instruction": None,
                "diagnostic": "missing recovery_instruction",
            },
        ):
            with (
                self.subTest(diagnostic=bad_cls_kwargs["diagnostic"]),
                self.assertRaises(ReviewInputError),
            ):
                PolicyDriftClassification(**bad_cls_kwargs)  # type: ignore[arg-type]

        # PolicyFundingPreservationProof and verify_policy_drift_funding_preservation fail-closed guards:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            baseline_rec = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            assert (
                baseline_rec is not None and baseline_rec.api_run_accounting is not None
            )
            baseline_acc = ApiRunAccounting.from_document(
                baseline_rec.api_run_accounting
            )
            drifted_cfg = FrozenApiConfiguration(
                commit="9" * 40,
                digest="8" * 64,
                limit=baseline_acc.configuration.limit,
            )

            with self.assertRaises(ReviewInputError):
                verify_policy_drift_funding_preservation(
                    "not-a-record",  # type: ignore[arg-type]
                    (baseline_rec,),
                    drifted_configuration=drifted_cfg,
                )
            with self.assertRaises(ReviewInputError):
                verify_policy_drift_funding_preservation(
                    baseline_rec,
                    (),
                    drifted_configuration=drifted_cfg,
                )
            with self.assertRaises(ReviewInputError):
                verify_policy_drift_funding_preservation(
                    baseline_rec,
                    (baseline_rec,),
                    drifted_configuration=baseline_acc.configuration,
                )
            mutated_rec = baseline_rec.evolve(generation=baseline_rec.generation + 1)
            with self.assertRaises(ReviewInputError):
                verify_policy_drift_funding_preservation(
                    baseline_rec,
                    (mutated_rec,),
                    drifted_configuration=drifted_cfg,
                )
            with self.assertRaises(ReviewInputError):
                PolicyFundingPreservationProof(
                    baseline_record_sha256=baseline_rec.record_sha256,
                    frozen_policy_commit=baseline_acc.configuration.commit,
                    frozen_policy_digest=baseline_acc.configuration.digest,
                    frozen_request_limit=baseline_acc.configuration.limit,
                    startup_liability=baseline_acc.startup.liability,
                    completed_receipt_count=len(baseline_acc.completed),
                    remaining_allowance=baseline_acc.remaining,
                    refusal_count=1,
                    zero_funding_mutation=False,
                    zero_receipt_erasure=True,
                    same_run_rebind_refused=True,
                )

    def test_consecutive_double_race_preserves_original_funding_and_converges_on_third_dispatch(
        self,
    ) -> None:
        """Exercise P0 -> P1 (identical policy bytes) -> P2 (changed policy/context bytes) double race."""
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            _disable_crlf_conversion(fixture.root)
            fixture.provider_comments = []
            p0 = fixture.policy
            p0_config_bytes = fixture.config_bytes

            # Run 1 settles cleanly on P0, establishing durable funding + receipts:
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            baseline_comment = fixture.comment["body"]
            rec_p0 = parse_session_comment(baseline_comment, identity=IDENTITY)
            assert (
                rec_p0 is not None
                and rec_p0.transaction is not None
                and rec_p0.api_run_accounting is not None
            )
            acc_p0 = ApiRunAccounting.from_document(rec_p0.api_run_accounting)
            self.assertEqual(acc_p0.configuration.commit, p0)
            self.assertEqual(
                acc_p0.configuration.digest,
                hashlib.sha256(p0_config_bytes).hexdigest(),
            )

            # Race 1: Default branch advances P0 -> P1 with byte-identical .reviewsensei.yml!
            p1 = _advance_remote_default_branch(
                fixture.root,
                frozen_commit=p0,
                filename="README.md",
                content=b"# Default branch docs advance P1\n",
                message="docs: advance default branch to P1",
            )
            self.assertNotEqual(p0, p1)
            request(fixture, 31, "rescan")
            fixture.policy = p0
            fixture.policy_ref = p1
            fixture.trace.clear()
            self.assertEqual(fixture.invoke(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", fixture.error
            )
            self.assertEqual(outcome(self, fixture.error)["step"], "default_policy_ref")
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.comment["body"], baseline_comment)
            self.assertFalse(
                any(
                    "/contents/.reviewsensei.yml" in path or "/api/github/token" in path
                    for _, _, path in fixture.trace
                )
            )
            rec_after_race_1 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            assert rec_after_race_1 is not None

            # Race 2: Operator checks out P1 to retry, while default branch advances
            # AGAIN P1 -> P2 with actual .reviewsensei.yml AND context-doc byte changes!
            _checkout_fresh_policy(fixture.root, p1)
            (fixture.root / "docs" / "architecture").mkdir(parents=True, exist_ok=True)
            (fixture.root / "docs" / "architecture" / "mandatory-rules.md").write_bytes(
                b"# Mandatory Rule v2\nAll mutations MUST fail closed.\n"
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "add",
                    "docs/architecture/mandatory-rules.md",
                ],
                check=True,
                capture_output=True,
            )
            p2_config_bytes = p0_config_bytes + b"\n# P2 policy byte drift\n"
            p2 = _advance_remote_default_branch(
                fixture.root,
                frozen_commit=p1,
                filename=".reviewsensei.yml",
                content=p2_config_bytes,
                message="chore: update .reviewsensei.yml and mandatory-rules on P2",
            )
            self.assertNotEqual(p1, p2)
            self.assertEqual(_checked_out_head(fixture.root), p1)
            fixture.config_bytes = p2_config_bytes

            request(fixture, 32, "rescan")
            fixture.policy = p1
            fixture.policy_ref = p2
            fixture.trace.clear()
            self.assertEqual(fixture.invoke(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", fixture.error
            )
            self.assertEqual(outcome(self, fixture.error)["step"], "default_policy_ref")
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.comment["body"], baseline_comment)
            self.assertFalse(
                any(
                    "/contents/.reviewsensei.yml" in path or "/api/github/token" in path
                    for _, _, path in fixture.trace
                )
            )
            rec_after_race_2 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            assert rec_after_race_2 is not None

            # Verify funding/receipt preservation across BOTH consecutive races:
            p2_cfg = FrozenApiConfiguration(
                commit=p2,
                digest=hashlib.sha256(p2_config_bytes).hexdigest(),
                limit=acc_p0.configuration.limit,
            )
            proof = verify_policy_drift_funding_preservation(
                rec_p0,
                (rec_after_race_1, rec_after_race_2),
                drifted_configuration=p2_cfg,
            )
            self.assertEqual(proof.refusal_count, 2)
            self.assertTrue(proof.zero_funding_mutation)
            self.assertTrue(proof.zero_receipt_erasure)
            self.assertTrue(proof.same_run_rebind_refused)
            self.assertEqual(proof.completed_receipt_count, len(acc_p0.completed))

            # Dispatch 3: Pinned to P2 -> converges cleanly and archives Run 1 funding intact!
            request(fixture, 33, "rescan")
            _checkout_fresh_policy(fixture.root, p2)
            fixture.policy = p2
            fixture.policy_ref = p2
            fixture.trace.clear()
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 2)

            settled_p2 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            assert (
                settled_p2 is not None
                and settled_p2.transaction is not None
                and settled_p2.api_run_accounting is not None
            )
            self.assertEqual(settled_p2.transaction.phase, "publication_succeeded")
            acc_p2 = ApiRunAccounting.from_document(settled_p2.api_run_accounting)
            self.assertEqual(acc_p2.configuration, p2_cfg)
            self.assertNotEqual(acc_p2.identity.run_id, acc_p0.identity.run_id)
            assert acc_p2.previous_record is not None

            # Authenticate archived Run 1 record from caller-run-root-v1:
            reader_ledger = GitHubIssueCommentSessionLedger(
                fixture.selected.http,
                token=fixture.selected.ledger.token,
                app_slug="reviewsensei[bot]",
                evidence_budget=EvidenceReadBudget(),
            )
            reader_ledger.load(IDENTITY)
            archived_p0 = read_previous_run(
                reader_ledger, IDENTITY, acc_p2.previous_record
            )
            self.assertEqual(archived_p0.record_sha256, rec_p0.record_sha256)
            assert archived_p0.api_run_accounting is not None
            archived_acc = ApiRunAccounting.from_document(
                archived_p0.api_run_accounting
            )
            self.assertEqual(archived_acc.configuration, acc_p0.configuration)
            self.assertEqual(archived_acc.completed, acc_p0.completed)
            self.assertEqual(archived_acc.startup.liability, acc_p0.startup.liability)

    def test_reply_entry_and_api_run_accounting_policy_binding_entry_points(
        self,
    ) -> None:
        """Exercise real ReplyEntryFixture (reply-run) and ApiRunAccounting policy-binding guards."""
        with tempfile.TemporaryDirectory() as directory:
            reply_f = ReplyEntryFixture(directory)
            _disable_crlf_conversion(reply_f.root)
            p0 = reply_f.policy
            p0_bytes = reply_f.config_bytes

            # 1. Byte-identical default-branch advance P0 -> P1 refuses reply-run pre-admission:
            p1 = _advance_remote_default_branch(
                reply_f.root,
                frozen_commit=p0,
                filename="CHANGELOG.md",
                content=b"# Unrelated changelog entry\n",
                message="docs: changelog on P1",
            )
            reply_f.policy = p0
            reply_f.policy_ref = p1
            self.assertEqual(reply_f.invoke_reply(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", reply_f.error
            )
            self.assertEqual(reply_f.reply_posts, 0)
            self.assertEqual(len(reply_f.provider_requests), 0)
            self.assertIsNone(reply_f.comment)

            # 2. Policy-byte change P1 -> P2 also refuses reply-run pre-admission:
            _checkout_fresh_policy(reply_f.root, p1)
            p2_bytes = p0_bytes + b"\n# reply policy update\n"
            p2 = _advance_remote_default_branch(
                reply_f.root,
                frozen_commit=p1,
                filename=".reviewsensei.yml",
                content=p2_bytes,
                message="chore: update reply config on P2",
            )
            reply_f.config_bytes = p2_bytes
            reply_f.run_id += 1
            reply_f.trace.clear()
            reply_f.transports.clear()
            reply_f.policy = p1
            reply_f.policy_ref = p2
            self.assertEqual(reply_f.invoke_reply(), 1)
            self.assertIn(
                "trusted policy commit changed before admission", reply_f.error
            )
            self.assertEqual(reply_f.reply_posts, 0)
            self.assertEqual(len(reply_f.provider_requests), 0)
            self.assertIsNone(reply_f.comment)

            # 3. Mid-run `.reviewsensei.yml` byte drift after bootstrap freezes
            # `run.caller.accounting.configuration.digest` is caught by `action_reply._configuration`:
            with tempfile.TemporaryDirectory() as tamper_dir:
                tampered_reply = ReplyEntryFixture(tamper_dir)
                with patch.object(
                    action_reply,
                    "policy_bytes",
                    return_value=tampered_reply.config_bytes + b"\n# tampered\n",
                ):
                    self.assertEqual(tampered_reply.invoke_reply(), 1)
                self.assertIn(
                    "frozen reply policy configuration changed", tampered_reply.error
                )
                self.assertEqual(tampered_reply.reply_posts, 0)
                self.assertEqual(len(tampered_reply.provider_requests), 0)

            # 4. Fresh ReplyEntryFixture dispatch pinned to P2 succeeds and binds P2:
            reply_f.run_id += 1
            reply_f.trace.clear()
            reply_f.transports.clear()
            _checkout_fresh_policy(reply_f.root, p2)
            reply_f.policy = p2
            reply_f.policy_ref = p2
            self.assertEqual(reply_f.invoke_reply(), 0, reply_f.error)
            self.assertEqual(reply_f.reply_posts, 1)
            self.assertEqual(len(reply_f.provider_requests), 1)
            reply_rec = parse_session_comment(
                reply_f.comment["body"], identity=IDENTITY
            )
            assert reply_rec is not None and reply_rec.api_run_accounting is not None
            reply_acc = ApiRunAccounting.from_document(reply_rec.api_run_accounting)
            self.assertEqual(reply_acc.configuration.commit, p2)
            self.assertEqual(
                reply_acc.configuration.digest,
                hashlib.sha256(p2_bytes).hexdigest(),
            )

            # 5. ApiRunAccounting.require_binding and require_successor refuse both
            # commit-only drift and digest drift within the same run:
            for drifted_cfg in (
                FrozenApiConfiguration(
                    commit=p1,
                    digest=reply_acc.configuration.digest,
                    limit=reply_acc.configuration.limit,
                ),
                FrozenApiConfiguration(
                    commit=p2,
                    digest=hashlib.sha256(p0_bytes).hexdigest(),
                    limit=reply_acc.configuration.limit,
                ),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError,
                    "caller accounting run or frozen configuration conflicts",
                ):
                    reply_acc.require_binding(reply_acc.identity, drifted_cfg)
                if drifted_cfg.digest != reply_acc.configuration.digest:
                    # Action accounting also pins policy_digest inside `action` intent:
                    with self.assertRaisesRegex(
                        ReviewInputError,
                        "caller action intent is malformed",
                    ):
                        replace(reply_acc, configuration=drifted_cfg)
                else:
                    # Commit-only drift constructs, then fails closed in require_successor:
                    with self.assertRaisesRegex(
                        ReviewInputError,
                        "caller accounting run or frozen configuration conflicts",
                    ):
                        reply_acc.require_successor(
                            replace(reply_acc, configuration=drifted_cfg)
                        )
                review_acc = replace(reply_acc, action=None)
                with self.assertRaisesRegex(
                    ReviewInputError,
                    "caller accounting run or frozen configuration conflicts",
                ):
                    review_acc.require_successor(
                        replace(review_acc, configuration=drifted_cfg)
                    )

    def test_engine_context_document_and_configuration_drift_on_real_core_entry_points(
        self,
    ) -> None:
        """Exercise RepositoryContextStore, load_configuration, admit_prompt_context, ReviewService, and CLI across P0 -> P1 -> P2."""

        class _RecordingProvider:
            name = "fixture"
            model: str | None = "fixture-model"
            endpoint: str | None = "https://fixture.example/api"
            completion_contract = "bounded-complete-text-v1"

            def __init__(self) -> None:
                self.requests: list[Any] = []

            def complete(self, req: Any) -> ProviderResponse:
                self.requests.append(req)
                return ProviderResponse(
                    json.dumps({"summary": "Reviewed diff.", "comments": []}),
                    self.name,
                    self.model,
                )

        multi_file_diff = (
            "diff --git a/src/first.py b/src/first.py\n"
            "--- /dev/null\n"
            "+++ b/src/first.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+first = 1\n"
            "+first_b = 2\n"
            "diff --git a/src/second.py b/src/second.py\n"
            "--- /dev/null\n"
            "+++ b/src/second.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+second = 1\n"
            "+second_b = 2\n"
        )
        rules_rel = "docs/architecture/mandatory-rules.md"
        sources = (ContextDocumentSource(path=rules_rel, required=True),)

        def _build_request_from_checkout(repo_dir: Path) -> ReviewRequest:
            docs = RepositoryContextStore(repo_dir).for_sources(sources)
            self.assertEqual(len(docs), 1)
            doc = docs[0]
            decision = DocumentDecision(
                doc.path,
                "selected",
                "pinned",
                doc.sha256,
                doc.sha256,
                len(doc.content.encode("utf-8")),
            )
            lens = ReviewLensContext(
                "architecture",
                documents=(doc,),
                document_selection=DocumentSelectionReport((decision,)),
            )
            return ReviewRequest(
                diff=multi_file_diff,
                repository="owner/repo",
                pull_request_number=7,
                base_sha=BASE,
                head_sha=HEAD,
                active_category_ids=("architecture",),
                lens_contexts=(lens,),
                limits=ReviewLimits(max_diff_files=1),
                orchestrate_large_changes=True,
            )

        with tempfile.TemporaryDirectory() as directory:
            repo_dir = Path(directory) / "repo"
            repo_dir.mkdir()
            subprocess.run(
                ["git", "-C", str(repo_dir), "init"],
                check=True,
                capture_output=True,
            )
            _disable_crlf_conversion(repo_dir)
            (repo_dir / ".reviewsensei.yml").write_bytes(
                b"schema: 1\ninference:\n  backend: local-ollama\n"
            )
            (repo_dir / "docs" / "architecture").mkdir(parents=True)
            (repo_dir / rules_rel).write_bytes(
                b"# Architecture Rules v1\nAll adapters MUST validate inputs.\n"
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "."],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "init P0",
                ],
                check=True,
                capture_output=True,
            )
            p0 = _checked_out_head(repo_dir)
            cfg_p0 = load_configuration(repo_dir / ".reviewsensei.yml")
            req_p0 = _build_request_from_checkout(repo_dir)

            category = ReviewCategory.from_dict(
                json.loads(
                    (
                        Path(__file__).resolve().parents[1]
                        / "src/review_sensei/default_categories/03-architecture.json"
                    ).read_text(encoding="utf-8")
                )
            )
            stage = Stage(
                name="review",
                prompt_template=DEFAULT_STAGES[0].prompt_template,
                outputs=("summary", "comments"),
                categories=(category,),
            )

            provider = _RecordingProvider()
            partial_service = ReviewService(
                provider,
                stages=(stage,),
                budget=ResourceBudget.create(max_provider_calls=1),
            )
            run_p0 = partial_service.run(req_p0)
            assert run_p0.result is not None
            self.assertEqual(run_p0.result.review_status, "partial")
            self.assertEqual(len(provider.requests), 1)
            self.assertIn("Architecture Rules v1", provider.requests[0].prompt)

            # Advance to P1 with unrelated README edit; policy & context bytes are identical:
            p1 = _advance_remote_default_branch(
                repo_dir,
                frozen_commit=p0,
                filename="README.md",
                content=b"# Unrelated README on P1\n",
                message="docs: P1",
            )
            _checkout_fresh_policy(repo_dir, p1)
            cfg_p1 = load_configuration(repo_dir / ".reviewsensei.yml")
            req_p1 = _build_request_from_checkout(repo_dir)
            self.assertEqual(cfg_p0, cfg_p1)
            self.assertEqual(
                partial_service.context_cache_key(req_p0),
                partial_service.context_cache_key(req_p1),
            )

            # Advance to P2 modifying BOTH mandatory-rules.md and .reviewsensei.yml:
            (repo_dir / rules_rel).write_bytes(
                b"# Architecture Rules v2\nAll adapters MUST fail closed on policy drift.\n"
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", rules_rel],
                check=True,
                capture_output=True,
            )
            p2 = _advance_remote_default_branch(
                repo_dir,
                frozen_commit=p1,
                filename=".reviewsensei.yml",
                content=b"schema: 1\ninference:\n  backend: cloud-ollama\n",
                message="chore: update rules and backend on P2",
            )
            _checkout_fresh_policy(repo_dir, p2)
            cfg_p2 = load_configuration(repo_dir / ".reviewsensei.yml")
            req_p2 = _build_request_from_checkout(repo_dir)
            self.assertNotEqual(cfg_p0, cfg_p2)
            self.assertNotEqual(
                partial_service.context_cache_key(req_p0),
                partial_service.context_cache_key(req_p2),
            )

            # Stale partial continuation from P0 MUST be refused at P2 before provider calls:
            with self.assertRaisesRegex(
                ReviewInputError,
                "prior partial review continuation scope is missing or does not match current request",
            ):
                partial_service.run(req_p2, prior_partial=run_p0.result)
            self.assertEqual(len(provider.requests), 1)

            # Prompt context admission at P2 stays strictly inside LEGACY_ASSESSMENT_PROMPT_BYTES:
            admitted_ctx = admit_prompt_context(
                req_p2,
                active_category_ids=("architecture",),
                changed_paths=("src/first.py", "src/second.py"),
                render=lambda selected: partial_service._format_prompt(
                    stage,
                    selected,
                    active_categories=(category,),
                    coverage_mode="full",
                    reviewed_paths=("src/first.py", "src/second.py"),
                    related_paths=(),
                    max_prompt_bytes=LEGACY_ASSESSMENT_PROMPT_BYTES,
                    analysis=analyze_diff(selected.diff),
                ),
                fits=lambda prompt: (
                    len(prompt.encode("utf-8")) <= LEGACY_ASSESSMENT_PROMPT_BYTES
                ),
            )
            self.assertNotIsInstance(admitted_ctx, Exception)

            # Fresh ReviewService run and real review_sensei.cli.main at P2 evaluate v2 cleanly:
            full_service = ReviewService(
                provider,
                stages=(stage,),
                budget=ResourceBudget.create(max_provider_calls=4),
            )
            run_p2 = full_service.run(req_p2)
            assert run_p2.result is not None
            self.assertEqual(run_p2.result.review_status, "complete")
            self.assertIn("Architecture Rules v2", provider.requests[-1].prompt)

            diff_file = Path(directory) / "change.patch"
            fixture_resp = Path(directory) / "fixture.json"
            out_json = Path(directory) / "out.json"
            diff_file.write_text(multi_file_diff, encoding="utf-8")
            fixture_resp.write_text(
                json.dumps({"summary": "CLI P2 review.", "comments": []}),
                encoding="utf-8",
            )
            rc_cli = core_cli.main(
                [
                    "--diff",
                    str(diff_file),
                    "--context-root",
                    str(repo_dir),
                    "--config",
                    str(repo_dir / ".reviewsensei.yml"),
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(fixture_resp),
                    "--format",
                    "json",
                    "--output",
                    str(out_json),
                ]
            )
            self.assertEqual(rc_cli, 0)
            cli_payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(cli_payload["review_status"], "complete")


if __name__ == "__main__":
    unittest.main()
