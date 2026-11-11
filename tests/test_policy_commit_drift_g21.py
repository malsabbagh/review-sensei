"""G21: Default-branch policy commit drift fail-closed guard and fresh-admission recovery."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.session_ledger import parse_session_comment
from test_action_run import CommandEntryFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_review_lifecycle_diagnostics import outcome
from test_review_run import EntryFixture
from test_review_run_rescan import partial_fixture, request

from review_sensei.errors import ReviewInputError
from review_sensei.operational_boundaries import (
    PolicyDriftClassification,
    classify_policy_commit_drift,
)


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


if __name__ == "__main__":
    unittest.main()
