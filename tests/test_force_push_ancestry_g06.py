"""G06 qualification: force-push, ancestry, rebase, and base-ref vs base-SHA identity."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from g04_merge_support import G04MergeFixture, git
from review_sensei_github.branch_lifecycle import (
    GitAncestryEvidence,
    classify_source_identity_transition,
    evaluate_live_pr_write_gate,
    inspect_git_ancestry,
    session_has_unresolved_obligations,
)
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.session_ledger import parse_session_comment
from test_controlled_restart import registration as restart_registration
from test_controlled_restart_journeys import RestartJourney
from test_reopen_branch_lifecycle_g05 import LifecycleEntryFixture, _sample_pr_payload
from test_reply_freshness_g33 import FreshnessReplyFixture
from test_review_run import IDENTITY

from review_sensei.baseline import (
    evaluate_baseline_compatibility,
)
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.session import read_session_baseline
from review_sensei.workflow import prepare_diff


def _init_git_repo(root: Path) -> dict[str, str]:
    """Create a deterministic synthetic Git commit graph covering all 7 G06 shapes."""

    git(root, "init", "-b", "main")
    for key, value in (
        ("core.autocrlf", "false"),
        ("core.abbrev", "7"),
        ("diff.context", "3"),
    ):
        git(root, "config", "--local", key, value)

    src = root / "src"
    src.mkdir()
    app = src / "app.py"
    app.write_text("x = 0\nkeep = True\n", encoding="utf-8")
    git(root, "add", "src/app.py")
    git(root, "commit", "-m", "base B1")
    b1 = git(root, "rev-parse", "HEAD").stdout.strip()
    # Second base ref pointing at identical commit SHA B1 (Transition 7).
    git(root, "branch", "release/1.0", b1)

    # Feature H1 branched from B1.
    git(root, "checkout", "-b", "feature", b1)
    app.write_text("x = 1\nkeep = True\n", encoding="utf-8")
    git(root, "commit", "-am", "feature H1")
    h1 = git(root, "rev-parse", "HEAD").stdout.strip()

    # Transition 1: Fast-forward head advance H1 -> H2_ff.
    (src / "extra.py").write_text("flag = 1\n", encoding="utf-8")
    git(root, "add", "src/extra.py")
    git(root, "commit", "-m", "feature H2 fast-forward")
    h2_ff = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "branch", "feature-ff", h2_ff)

    # Transition 5: Force-push to new commit SHA with byte-identical tree/diff as H1.
    git(root, "checkout", "--detach", h1)
    git(root, "commit", "--amend", "-m", "feature H1 amended identical tree")
    h1_identical_tree = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "branch", "feature-identical-tree", h1_identical_tree)

    # Non-fast-forward force-push with modified tree on constant base B1.
    git(root, "checkout", "--detach", b1)
    app.write_text("x = 99\nkeep = True\n", encoding="utf-8")
    git(root, "commit", "-am", "feature rewritten content")
    h_rewritten = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "branch", "feature-rewritten", h_rewritten)

    # Advance main to B2 (non-conflicting file) for Transitions 2 & 6.
    git(root, "checkout", "main")
    (src / "base_only.py").write_text("base_step = 2\n", encoding="utf-8")
    git(root, "add", "src/base_only.py")
    git(root, "commit", "-m", "base B2")
    b2 = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "branch", "main-b1", b1)

    # Transition 2: Rebase feature H1 onto B2 -> H1_rebased.
    git(root, "checkout", "-b", "feature-rebased", b2)
    app.write_text("x = 1\nkeep = True\n", encoding="utf-8")
    git(root, "commit", "-am", "feature H1 rebased onto B2")
    h1_rebased = git(root, "rev-parse", "HEAD").stdout.strip()

    # Transition 3: Orphan branch with unrelated history (no merge-base).
    git(root, "checkout", "--orphan", "unrelated-branch")
    git(root, "rm", "-rf", ".")
    (root / "orphan.txt").write_text("unrelated root\n", encoding="utf-8")
    git(root, "add", "orphan.txt")
    git(root, "commit", "-m", "orphan commit")
    h_orphan = git(root, "rev-parse", "HEAD").stdout.strip()

    # Point `origin` to root itself so `prepare_diff` fetches local refs without network.
    git(root, "remote", "add", "origin", str(root))
    git(root, "checkout", "main")

    return {
        "b1": b1,
        "b2": b2,
        "h1": h1,
        "h2_ff": h2_ff,
        "h1_identical_tree": h1_identical_tree,
        "h_rewritten": h_rewritten,
        "h1_rebased": h1_rebased,
        "h_orphan": h_orphan,
    }


class ForcePushAncestryG06Tests(unittest.TestCase):
    """End-to-end G06 qualification across Git ancestry, cache, preflight, and recovery."""

    def test_all_seven_source_transitions_git_ancestry_diff_and_cache_compatibility(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            shas = _init_git_repo(repo)
            out_patch = Path(tmp) / "pr.patch"

            # Build a baseline and cache key anchored at (B1, H1) using real engine types.
            with tempfile.TemporaryDirectory() as fx_dir:
                fx = LifecycleEntryFixture(Path(fx_dir))
                fx.live_base = fx.requested_base = shas["b1"]
                fx.live_head = fx.requested_head = shas["h1"]
                code, _ = fx.run_and_read(human=False)
                self.assertEqual(code, 0)
                record = parse_session_comment(fx.comment["body"], identity=IDENTITY)
                assert record is not None
                baseline = read_session_baseline(None, record)
                assert baseline is not None
            policy = ReviewConvergencePolicy()

            def key_for(base_sha: str, head_sha: str) -> ReviewContextCacheKey:
                k = baseline.cache_key
                return ReviewContextCacheKey(
                    repository=k.repository,
                    pull_request=k.pull_request,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    engine=k.engine,
                    model=k.model,
                    profile=k.profile,
                    stage_digest=k.stage_digest,
                    context_digest=k.context_digest,
                    learning_digest=k.learning_digest,
                )

            # 1. Normal fast-forward head advance: (main@B1, H1) -> (main@B1, H2_ff)
            anc_1 = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_sha=shas["b1"],
                current_head_sha=shas["h2_ff"],
            )
            self.assertEqual(anc_1.merge_base_sha, shas["b1"])
            self.assertTrue(anc_1.prior_head_is_ancestor_of_new_head)
            dec_1 = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="main-b1",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h2_ff"],
                ancestry=anc_1,
            )
            self.assertEqual(dec_1.transition_kind, "fast_forward_head_advance")
            self.assertTrue(dec_1.baseline_cache_compatible)
            self.assertIsNone(
                evaluate_baseline_compatibility(
                    baseline,
                    current_key=key_for(shas["b1"], shas["h2_ff"]),
                    policy=policy,
                )
            )
            patch_1 = prepare_diff(
                base_ref="main-b1",
                head_ref="feature-ff",
                repository=repo,
                output=out_patch,
            )
            self.assertIn("src/extra.py", patch_1.read_text(encoding="utf-8"))

            # 2. Rebase onto advanced base SHA: (main@B1, H1) -> (main@B2, H1_rebased)
            anc_2 = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_sha=shas["b2"],
                current_head_sha=shas["h1_rebased"],
            )
            self.assertEqual(anc_2.merge_base_sha, shas["b2"])
            self.assertFalse(anc_2.prior_head_is_ancestor_of_new_head)
            dec_2 = classify_source_identity_transition(
                previous_base_ref="main",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="main",
                current_base_sha=shas["b2"],
                current_head_sha=shas["h1_rebased"],
                ancestry=anc_2,
            )
            self.assertEqual(dec_2.transition_kind, "rebase_with_base_sha_drift")
            self.assertFalse(dec_2.baseline_cache_compatible)
            self.assertEqual(
                dec_2.baseline_invalidation_reason, "rebase-or-base-change"
            )
            self.assertEqual(
                evaluate_baseline_compatibility(
                    baseline,
                    current_key=key_for(shas["b2"], shas["h1_rebased"]),
                    policy=policy,
                ),
                "rebase-or-base-change",
            )
            patch_2 = prepare_diff(
                base_ref="main",
                head_ref="feature-rebased",
                repository=repo,
                output=out_patch,
            )
            self.assertIn("x = 1", patch_2.read_text(encoding="utf-8"))

            # 3. Unrelated history (no merge base): explicit prepare_diff refusal + recovery guidance
            anc_3 = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_sha=shas["b1"],
                current_head_sha=shas["h_orphan"],
            )
            self.assertIsNone(anc_3.merge_base_sha)
            dec_3 = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="main-b1",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h_orphan"],
                ancestry=anc_3,
            )
            self.assertEqual(dec_3.transition_kind, "unrelated_history")
            self.assertEqual(
                dec_3.recovery_mode, "refuse_prepare_diff_retarget_or_rescan"
            )
            with self.assertRaises(ReviewInputError) as unrelated_err:
                prepare_diff(
                    base_ref="main-b1",
                    head_ref="unrelated-branch",
                    repository=repo,
                    output=out_patch,
                )
            self.assertIn(
                "base and head commits do not share a merge base",
                str(unrelated_err.exception),
            )
            self.assertIn("@reviewsensei rescan", str(unrelated_err.exception))

            # 4. Force-push back to previously reviewed old SHA: H2_ff -> H1
            anc_4 = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h2_ff"],
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1"],
            )
            self.assertFalse(anc_4.prior_head_is_ancestor_of_new_head)
            dec_4 = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h2_ff"],
                current_base_ref="main-b1",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1"],
                ancestry=anc_4,
                previously_reviewed_head_shas={shas["h1"], shas["h2_ff"]},
            )
            self.assertEqual(dec_4.transition_kind, "force_push_revert_to_prior_sha")
            self.assertTrue(dec_4.preserves_newer_session_obligations)
            self.assertFalse(dec_4.reuses_superseded_approval_without_verification)
            self.assertEqual(dec_4.recovery_mode, "verify_against_latest_obligations")

            # 5. Force-push to new commit SHA with byte-identical tree/diff: H1 -> H1_identical_tree
            self.assertNotEqual(shas["h1"], shas["h1_identical_tree"])
            anc_5 = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1_identical_tree"],
            )
            self.assertEqual(anc_5.prior_head_tree_sha, anc_5.new_head_tree_sha)
            self.assertTrue(anc_5.diff_bytes_identical)
            self.assertFalse(anc_5.prior_head_is_ancestor_of_new_head)
            dec_5 = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="main-b1",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1_identical_tree"],
                ancestry=anc_5,
            )
            self.assertEqual(dec_5.transition_kind, "force_push_identical_tree_new_sha")
            self.assertTrue(dec_5.baseline_cache_compatible)
            self.assertFalse(dec_5.reuses_superseded_approval_without_verification)
            self.assertEqual(
                dec_5.in_flight_publication_skip_status, "skipped_stale_head"
            )

            # Also verify non-FF changed-content force-push classification.
            anc_5b = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_sha=shas["b1"],
                current_head_sha=shas["h_rewritten"],
            )
            self.assertFalse(anc_5b.diff_bytes_identical)
            dec_5b = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="main-b1",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h_rewritten"],
                ancestry=anc_5b,
            )
            self.assertEqual(dec_5b.transition_kind, "force_push_changed_content")

            # 6. Base branch advances while head stays constant: (main@B1, H1) -> (main@B2, H1)
            anc_6 = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_sha=shas["b2"],
                current_head_sha=shas["h1"],
            )
            self.assertTrue(anc_6.prior_base_is_ancestor_of_new_base)
            dec_6 = classify_source_identity_transition(
                previous_base_ref="main",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="main",
                current_base_sha=shas["b2"],
                current_head_sha=shas["h1"],
                ancestry=anc_6,
            )
            self.assertEqual(dec_6.transition_kind, "base_sha_advance_constant_head")
            self.assertFalse(dec_6.baseline_cache_compatible)
            self.assertEqual(
                dec_6.baseline_invalidation_reason, "rebase-or-base-change"
            )
            self.assertEqual(
                evaluate_baseline_compatibility(
                    baseline,
                    current_key=key_for(shas["b2"], shas["h1"]),
                    policy=policy,
                ),
                "rebase-or-base-change",
            )
            self.assertEqual(
                dec_6.in_flight_publication_skip_status, "skipped_stale_base"
            )
            self.assertEqual(dec_6.in_flight_reply_skip_status, "skipped_stale_base")

            # 7. Same-SHA base-ref retarget: (main-b1@B1, H1) -> (release/1.0@B1, H1)
            # Explicitly distinguished from base_sha drift!
            anc_7 = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1"],
            )
            dec_7 = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="release/1.0",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1"],
                ancestry=anc_7,
            )
            self.assertEqual(dec_7.transition_kind, "same_sha_base_ref_retarget")
            self.assertTrue(dec_7.baseline_cache_compatible)
            self.assertIsNone(dec_7.baseline_invalidation_reason)
            self.assertIsNone(
                evaluate_baseline_compatibility(
                    baseline,
                    current_key=key_for(shas["b1"], shas["h1"]),
                    policy=policy,
                )
            )
            self.assertEqual(
                dec_7.in_flight_publication_skip_status, "skipped_stale_base"
            )
            self.assertEqual(dec_7.in_flight_reply_skip_status, "skipped_stale_base")
            self.assertEqual(
                dec_7.recovery_mode,
                "rebind_base_ref_without_base_sha_invalidation",
            )

            # Unchanged control.
            dec_same = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="main-b1",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1"],
                ancestry=anc_7,
            )
            self.assertEqual(dec_same.transition_kind, "unchanged_snapshot")
            self.assertIsNone(dec_same.in_flight_publication_skip_status)

    def test_force_push_back_to_approved_sha_preserves_newer_obligations(
        self,
    ) -> None:
        """Transition 4: H1 (approved) -> H2 (new obligation) -> force-push back to H1."""

        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            h1 = fixture.live_head
            h2 = "d" * 40

            # 1. H1 is cleanly reviewed and approved.
            code_1, out_1 = fixture.run_and_read(human=False)
            self.assertEqual(code_1, 0)
            assert out_1 is not None
            self.assertEqual(out_1["status"], "published")
            self.assertEqual(
                [r["event"] for r in fixture.review_posts], ["COMMENT", "APPROVE"]
            )

            # 2. H2 introduces an unresolved concern recorded in the session baseline.
            fixture.advance_run_to_head(h2)
            code_2, out_2 = fixture.run_and_read(human=True)
            self.assertEqual(code_2, 0)
            assert out_2 is not None
            self.assertEqual(out_2["status"], "published")
            record_h2 = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_h2))

            # 3. Force-push reverts head back to previously approved H1.
            # Must NOT discard H2's obligation or blindly reuse H1's superseded approval!
            fixture.advance_run_to_head(h1)
            code_3, out_3 = fixture.run_and_read(human=False)
            self.assertEqual(code_3, 0)
            assert out_3 is not None
            self.assertTrue(out_3["accounting_terminal"])
            record_revert = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_revert))
            # Only the original H1 approval from step 1 exists; step 3 emitted COMMENT, NOT APPROVE.
            self.assertEqual(
                [r["event"] for r in fixture.review_posts],
                ["COMMENT", "APPROVE", "COMMENT", "COMMENT"],
            )

    def test_force_push_identical_tree_new_sha_invalidates_prior_sha_approval(
        self,
    ) -> None:
        """Transition 5: H1 approved -> force-push H2 with identical diff/tree."""

        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            code_1, out_1 = fixture.run_and_read(human=False)
            self.assertEqual(code_1, 0)
            assert out_1 is not None
            self.assertEqual(out_1["status"], "published")
            self.assertEqual(fixture.provider_calls, 1)

            # New commit SHA H2 has the exact same diff content in EntryFixture,
            # so H1's approval marker MUST NOT certify H2 without reviewing H2.
            h2_same_tree = "e" * 40
            fixture.advance_run_to_head(h2_same_tree)
            code_2, out_2 = fixture.run_and_read(human=False)
            self.assertEqual(code_2, 0)
            assert out_2 is not None
            self.assertEqual(out_2["status"], "published")
            self.assertEqual(fixture.provider_calls, 2)
            self.assertEqual(fixture.review_posts[-1]["event"], "APPROVE")
            self.assertEqual(fixture.review_posts[-1]["commit_id"], h2_same_tree)

    def test_same_sha_base_ref_retarget_gates_in_flight_writes_and_recovers_zero_inference(
        self,
    ) -> None:
        """Transition 7 + G33 control: base_ref changes while base_sha stays identical."""

        # Part A: Mid-flight review publication skips as `skipped_stale_base` on final
        # pre-mutation preflight when `base_ref` retargets at identical `base_sha`,
        # then same-run attempt 2 on `release/1.0` recovers from retained analysis with
        # ZERO fresh model inference!
        with tempfile.TemporaryDirectory() as tmp:
            fixture = LifecycleEntryFixture(Path(tmp))
            original_preflight = ReviewPublisher._preflight_pr
            preflight_calls = 0

            def retarget_on_final_preflight(publisher, **kwargs):
                nonlocal preflight_calls
                preflight_calls += 1
                if preflight_calls == 3:
                    fixture.base_ref = "release/1.0"
                return original_preflight(publisher, **kwargs)

            with patch.object(
                ReviewPublisher, "_preflight_pr", retarget_on_final_preflight
            ):
                code_mid, out_mid = fixture.run_and_read(human=False)
            self.assertEqual(code_mid, 1)
            assert out_mid is not None
            self.assertEqual(out_mid["status"], "skipped_stale_base")
            self.assertFalse(out_mid["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.review_posts, [])

            # Live gate explicitly classifies same-SHA base-ref retarget as `stale_base_ref`.
            gate = evaluate_live_pr_write_gate(
                _sample_pr_payload(
                    base_ref="release/1.0",
                    base_sha=fixture.live_base,
                    head_sha=fixture.live_head,
                ),
                repository="owner/repo",
                repository_id=123,
                head_sha=fixture.live_head,
                base_branch="main",
                base_sha=fixture.live_base,
                app_slug="reviewsensei[bot]",
            )
            self.assertFalse(gate.admitted)
            self.assertEqual(gate.skip_status, "skipped_stale_base")
            self.assertEqual(gate.reason, "stale_base_ref")

            # Same-run attempt 2 on `release/1.0` at the SAME (base_sha, head_sha)
            # rebinds `base_branch="release/1.0"` and publishes retained analysis with
            # ZERO fresh model inference (`provider_calls` stays 1)!
            fixture.attempt = 2
            fixture.trace.clear()
            fixture.transports.clear()
            fixture.worker_observations.clear()
            code_rec, out_rec = fixture.run_and_read(human=False)
            self.assertEqual(code_rec, 0, fixture.error)
            assert out_rec is not None
            self.assertEqual(out_rec["status"], "published")
            self.assertTrue(out_rec["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(
                [r["event"] for r in fixture.review_posts], ["COMMENT", "APPROVE"]
            )

        # Part B: G33 reply freshness control — mid-flight same-SHA base_ref retarget
        # skips business reply before POST (`skipped_stale_base`).
        with tempfile.TemporaryDirectory() as reply_tmp:
            reply_fx = FreshnessReplyFixture(Path(reply_tmp))
            reply_fx.initialize_freshness()
            original_publish = ConversationPublisher._publish

            def retarget_before_reply(publisher, **kwargs):
                reply_fx.live_base_ref = "release/1.0"
                return original_publish(publisher, **kwargs)

            with patch.object(ConversationPublisher, "_publish", retarget_before_reply):
                reply_code = reply_fx.invoke_reply()
            self.assertEqual(reply_code, 1)
            self.assertEqual(reply_fx.reply_posts, 0)
            self.assertEqual(
                getattr(reply_fx.last_result, "status", None), "skipped_stale_base"
            )
            self.assertEqual(reply_fx.action()["phase"], "pending")

    def test_mid_analysis_rewrite_post_dispatch_fence_and_g04_conflict_control(
        self,
    ) -> None:
        # 1. History rewrite during provider inference (`before_provider`) refuses
        # retention, blocks stale publication, holds unauthenticated rollover, and
        # recovers cleanly on the new head via authenticated controlled restart.
        with tempfile.TemporaryDirectory() as tmp_mid:
            mid_root = Path(tmp_mid)
            for sub in ("prior", "restart"):
                (mid_root / sub).mkdir()
            fx_mid = LifecycleEntryFixture(mid_root / "prior")
            rewritten_head = "c" * 40
            fx_mid.before_provider = lambda: setattr(
                fx_mid, "live_head", rewritten_head
            )
            code_stale, _ = fx_mid.run_and_read(human=False)
            self.assertEqual(code_stale, 1)
            self.assertEqual(fx_mid.provider_calls, 1)
            self.assertEqual(fx_mid.review_posts, [])
            self.assertIn(
                "retention current pull request identity mismatches", fx_mid.error
            )

            # Unauthenticated new-run rollover fails closed with zero inference.
            fx_mid.before_provider = None
            fx_mid.advance_run_to_head(rewritten_head)
            code_blocked, _ = fx_mid.run_and_read(human=False)
            self.assertEqual(code_blocked, 1)
            self.assertIn("previous caller run is not terminal", fx_mid.error)
            self.assertEqual(fx_mid.provider_calls, 1)
            self.assertEqual(fx_mid.review_posts, [])

            # Designated controlled restart archives the non-terminal predecessor and
            # completes review on `rewritten_head`.
            prior_run_id = 456
            old_comment = copy.deepcopy(fx_mid.comment)
            parts = copy.deepcopy(fx_mid.extra_comments)
            for item in parts.values():
                item["issue_url"] = "https://api.github.test/repos/owner/repo/issues/7"
            journey = RestartJourney(mid_root / "restart", body=old_comment["body"])
            r_fx = journey.fixture
            r_fx.run_id = fx_mid.run_id + 1
            r_fx.live_head = r_fx.requested_head = rewritten_head
            r_fx.extra_comments = copy.deepcopy(parts)
            archived = [journey.old, *parts.values()]
            journey.row = restart_registration(
                policy_commit=r_fx.policy,
                workflow_sha="a" * 40,
                base_sha=r_fx.live_base,
                head_sha=rewritten_head,
                run_id=str(r_fx.run_id),
                job="hosted",
                old_runs=[prior_run_id],
                old_comments=[
                    {
                        "id": item["id"],
                        "bytes": len(item["body"].encode()),
                        "sha256": hashlib.sha256(item["body"].encode()).hexdigest(),
                    }
                    for item in archived
                ],
            )

            def failed_prior_owner(request, timeout, original):
                response = original(request, timeout)
                path = urlsplit(request.full_url).path
                if path == f"/repos/owner/repo/actions/runs/{prior_run_id}":
                    value = json.loads(response.read(512 * 1024 + 1))
                    value.update(conclusion="failure", head_sha="b" * 40)
                    return json_response(value)
                if (
                    path
                    == f"/repos/owner/repo/actions/runs/{prior_run_id}/attempts/1/jobs"
                ):
                    value = json.loads(response.read(512 * 1024 + 1))
                    for job in value["jobs"]:
                        job["conclusion"] = "failure"
                    return json_response(value)
                return response

            journey.external_hook = failed_prior_owner
            self.assertEqual(journey.invoke(human=False), 0, r_fx.error)
            self.assertEqual(r_fx.provider_calls, 1)
            self.assertIsNotNone(r_fx.selected.caller.accounting.terminal_transaction)
            self.assertEqual(r_fx.reviews[-1]["commit_id"], rewritten_head)

        # 2. Post-dispatch stale fence limit (G02 boundary): rewrite after COMMENT review POST
        # leaves already-sent COMMENT review intact, withholds APPROVE on stale head,
        # and recovers cleanly on the new head.
        with tempfile.TemporaryDirectory() as tmp_post:

            class PostCommentRewriteFixture(LifecycleEntryFixture):
                rewrite_to: str | None = None

                def external(self, request, timeout):
                    resp = super().external(request, timeout)
                    path = urlsplit(request.full_url).path
                    if (
                        self.rewrite_to is not None
                        and request.get_method() == "POST"
                        and path.endswith("/pulls/7/reviews")
                        and json.loads(request.data).get("event") == "COMMENT"
                        and resp.status == 200
                    ):
                        self.live_head = self.rewrite_to
                    return resp

            fx_post = PostCommentRewriteFixture(Path(tmp_post))
            fx_post.rewrite_to = "f" * 40
            code_partial, out_partial = fx_post.run_and_read(human=False)
            self.assertEqual(code_partial, 0)
            assert out_partial is not None
            self.assertEqual(out_partial["status"], "published")
            self.assertTrue(out_partial["accounting_terminal"])
            # COMMENT succeeded on H1, APPROVE skipped as stale head; H1 COMMENT was NOT revoked.
            self.assertEqual([r["event"] for r in fx_post.review_posts], ["COMMENT"])
            self.assertEqual(fx_post.review_posts[0]["commit_id"], "b" * 40)

            fx_post.rewrite_to = None
            fx_post.advance_run_to_head("f" * 40)
            code_post_rec, out_post_rec = fx_post.run_and_read(human=False)
            self.assertEqual(code_post_rec, 0)
            assert out_post_rec is not None
            self.assertEqual(out_post_rec["status"], "published")
            self.assertTrue(out_post_rec["accounting_terminal"])
            self.assertEqual(
                [r["event"] for r in fx_post.review_posts],
                ["COMMENT", "COMMENT", "APPROVE"],
            )

        # 3. G04 dirty/conflicting base regression control: three-dot diff still reviews cleanly.
        with tempfile.TemporaryDirectory() as tmp_g04:
            g04 = G04MergeFixture(Path(tmp_g04))
            g04.event_name = "workflow_dispatch"
            g04.live_base = g04.requested_base = g04.conflict_base
            g04.merge_fields = {"mergeable": False, "mergeable_state": "dirty"}
            self.assertTrue(g04.conflicts())
            code_g04 = g04.invoke_fresh()
            row_g04 = g04.observation(case="g06_dirty_conflict_control", code=code_g04)
            self.assertEqual(code_g04, 0, g04.error)
            self.assertEqual(row_g04["provider_has_exact_diff"], [True])
            self.assertIsNotNone(row_g04["terminal_transaction"])

    def test_ancestry_and_source_classifier_input_validation(self) -> None:
        with self.assertRaisesRegex(
            GitHubPublicationError, "repository root must be an existing directory"
        ):
            inspect_git_ancestry(
                Path("/nonexistent/rs-g06-dir"),
                previous_base_sha="a" * 40,
                previous_head_sha="b" * 40,
                current_base_sha="a" * 40,
                current_head_sha="b" * 40,
            )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                GitHubPublicationError, "must be a 40-hex commit SHA"
            ):
                inspect_git_ancestry(
                    Path(tmp),
                    previous_base_sha="short",
                    previous_head_sha="b" * 40,
                    current_base_sha="a" * 40,
                    current_head_sha="b" * 40,
                )
        with self.assertRaisesRegex(
            GitHubPublicationError, "must be a non-empty ref name"
        ):
            classify_source_identity_transition(
                previous_base_ref=" ",
                previous_base_sha="a" * 40,
                previous_head_sha="b" * 40,
                current_base_ref="main",
                current_base_sha="a" * 40,
                current_head_sha="b" * 40,
                ancestry=GitAncestryEvidence(
                    merge_base_sha="a" * 40,
                    prior_head_is_ancestor_of_new_head=True,
                    prior_base_is_ancestor_of_new_base=True,
                ),
            )


if __name__ == "__main__":
    unittest.main()
