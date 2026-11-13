"""G06 qualification: force-push, ancestry, rebase, and base-ref vs base-SHA identity."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

try:
    from fake_github_http import json_response
    from g04_merge_support import G04MergeFixture, git
    from test_controlled_restart import registration as restart_registration
    from test_controlled_restart_journeys import RestartJourney
    from test_reopen_branch_lifecycle_g05 import (
        SAMPLE_DIFF,
        LifecycleEntryFixture,
        _sample_pr_payload,
    )
    from test_reply_freshness_g33 import FreshnessReplyFixture
    from test_review_run import IDENTITY
except ModuleNotFoundError:
    from tests.fake_github_http import json_response
    from tests.g04_merge_support import G04MergeFixture, git
    from tests.test_controlled_restart import registration as restart_registration
    from tests.test_controlled_restart_journeys import RestartJourney
    from tests.test_reopen_branch_lifecycle_g05 import (
        SAMPLE_DIFF,
        LifecycleEntryFixture,
        _sample_pr_payload,
    )
    from tests.test_reply_freshness_g33 import FreshnessReplyFixture
    from tests.test_review_run import IDENTITY

from review_sensei_github.branch_lifecycle import (
    GitAncestryEvidence,
    classify_source_identity_transition,
    evaluate_live_pr_write_gate,
    inspect_git_ancestry,
    session_has_unresolved_obligations,
)
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewApprovalFinalizer, ReviewPublisher
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.baseline import (
    evaluate_baseline_compatibility,
)
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
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
            anc_3_from_orphan = inspect_git_ancestry(
                repo,
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h_orphan"],
                current_base_sha=shas["b1"],
                current_head_sha=shas["h_rewritten"],
            )
            self.assertEqual(anc_3_from_orphan.merge_base_sha, shas["b1"])
            self.assertIsNone(anc_3_from_orphan.diff_bytes_identical)
            dec_3_from_orphan = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h_orphan"],
                current_base_ref="main-b1",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h_rewritten"],
                ancestry=anc_3_from_orphan,
            )
            self.assertEqual(dec_3_from_orphan.transition_kind, "unrelated_history")
            self.assertFalse(dec_3_from_orphan.baseline_cache_compatible)
            self.assertEqual(
                dec_3_from_orphan.recovery_mode,
                "refuse_prepare_diff_retarget_or_rescan",
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

            # Combined head change + same-SHA base_ref retarget invalidates baseline cache
            # with `base-ref-retarget` so branch-scoped context is not silently reused.
            dec_ff_retarget = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h1"],
                current_base_ref="release/1.0",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h2_ff"],
                ancestry=anc_1,
            )
            self.assertEqual(
                dec_ff_retarget.transition_kind, "fast_forward_head_advance"
            )
            self.assertFalse(dec_ff_retarget.baseline_cache_compatible)
            self.assertEqual(
                dec_ff_retarget.baseline_invalidation_reason, "base-ref-retarget"
            )
            self.assertEqual(dec_ff_retarget.recovery_mode, "fallback_full_review")

            dec_revert_retarget = classify_source_identity_transition(
                previous_base_ref="main-b1",
                previous_base_sha=shas["b1"],
                previous_head_sha=shas["h2_ff"],
                current_base_ref="release/1.0",
                current_base_sha=shas["b1"],
                current_head_sha=shas["h1"],
                ancestry=anc_4,
                previously_reviewed_head_shas={shas["h1"], shas["h2_ff"]},
            )
            self.assertEqual(
                dec_revert_retarget.transition_kind, "force_push_revert_to_prior_sha"
            )
            self.assertFalse(dec_revert_retarget.baseline_cache_compatible)
            self.assertEqual(
                dec_revert_retarget.baseline_invalidation_reason, "base-ref-retarget"
            )
            self.assertEqual(
                dec_revert_retarget.recovery_mode, "verify_against_latest_obligations"
            )

            # Two-signal OR unit matrix for `force_push_identical_tree_new_sha`:
            # (a) tree SHA match only, (b) diff-byte match only, (c) neither.
            tree_a = "1" * 40
            tree_b = "2" * 40
            for label, p_tree, n_tree, diff_eq, expected_kind in (
                (
                    "tree_match_only",
                    tree_a,
                    tree_a,
                    False,
                    "force_push_identical_tree_new_sha",
                ),
                (
                    "diff_match_only",
                    tree_a,
                    tree_b,
                    True,
                    "force_push_identical_tree_new_sha",
                ),
                ("neither_signal", tree_a, tree_b, False, "force_push_changed_content"),
            ):
                with self.subTest(two_signal_case=label):
                    sig_dec = classify_source_identity_transition(
                        previous_base_ref="main-b1",
                        previous_base_sha=shas["b1"],
                        previous_head_sha=shas["h1"],
                        current_base_ref="main-b1",
                        current_base_sha=shas["b1"],
                        current_head_sha=shas["h_rewritten"],
                        ancestry=GitAncestryEvidence(
                            merge_base_sha=shas["b1"],
                            prior_head_is_ancestor_of_new_head=False,
                            prior_base_is_ancestor_of_new_base=True,
                            prior_head_tree_sha=p_tree,
                            new_head_tree_sha=n_tree,
                            diff_bytes_identical=diff_eq,
                        ),
                    )
                    self.assertEqual(sig_dec.transition_kind, expected_kind)

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
            original_obligations = fixture.retained_finding_obligations()
            fixture.advance_run_to_head(h1)
            code_3, out_3 = fixture.run_and_read(human=False)
            self.assertEqual(code_3, 0)
            assert out_3 is not None
            self.assertTrue(out_3["accounting_terminal"])
            record_revert = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(record_revert))
            # Only the original H1 approval from step 1 exists; step 3 requests changes for retained uncertainty, NOT APPROVE.
            self.assertEqual(
                [r["event"] for r in fixture.review_posts],
                ["COMMENT", "APPROVE", "COMMENT", "REQUEST_CHANGES"],
            )
            fixture.assert_preserved_omitted_obligations(self, original_obligations)

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
            retargeted = False

            def retarget_on_final_preflight(publisher, **kwargs):
                nonlocal retargeted
                # Anchor on the post-staging pre-mutation fence (immediately after
                # `human_inventory_store.stage` records the default human inventory
                # part comment), rather than a brittle preflight call counter.
                if not retargeted and any(
                    "default-human-inventory" in str(item.get("body", ""))
                    for item in fixture.extra_comments.values()
                ):
                    retargeted = True
                    fixture.base_ref = "release/1.0"
                return original_preflight(publisher, **kwargs)

            with patch.object(
                ReviewPublisher, "_preflight_pr", retarget_on_final_preflight
            ):
                code_mid, out_mid = fixture.run_and_read(human=False)
            self.assertTrue(retargeted)
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
            initial_head = fx_mid.live_head
            prior_run_id = fx_mid.run_id
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

            # Snapshot the consistent non-terminal predecessor state immediately
            # after the interrupted run, before exercising unauthenticated rollover.
            assert fx_mid.comment is not None
            prior_comment = copy.deepcopy(fx_mid.comment)
            prior_parts = copy.deepcopy(fx_mid.extra_comments)
            prior_record = parse_session_comment(
                prior_comment["body"], identity=IDENTITY
            )
            assert prior_record is not None
            assert prior_record.api_run_accounting is not None
            self.assertIsNone(
                ApiRunAccounting.from_document(
                    prior_record.api_run_accounting
                ).terminal_transaction
            )

            # Unauthenticated new-run rollover fails closed with zero inference.
            fx_mid.before_provider = None
            fx_mid.advance_run_to_head(rewritten_head)
            code_blocked, _ = fx_mid.run_and_read(human=False)
            self.assertEqual(code_blocked, 1)
            self.assertIn("previous caller run is not terminal", fx_mid.error)
            self.assertEqual(fx_mid.provider_calls, 1)
            self.assertEqual(fx_mid.review_posts, [])

            # Designated controlled restart archives the captured non-terminal
            # predecessor snapshot and completes review on `rewritten_head`.
            for item in prior_parts.values():
                item["issue_url"] = "https://api.github.test/repos/owner/repo/issues/7"
            journey = RestartJourney(mid_root / "restart", body=prior_comment["body"])
            r_fx = journey.fixture
            r_fx.run_id = prior_run_id + 10
            r_fx.live_head = r_fx.requested_head = rewritten_head
            r_fx.extra_comments = copy.deepcopy(prior_parts)
            archived = [journey.old, *prior_parts.values()]
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
                    value.update(conclusion="failure", head_sha=initial_head)
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
            # COMMENT succeeded on H1, APPROVE skipped as stale head; H1 COMMENT was NOT revoked,
            # zero APPROVED reviews exist, and the new head `"f" * 40` has no review/eligibility yet.
            self.assertEqual([r["event"] for r in fx_post.review_posts], ["COMMENT"])
            self.assertEqual([r["state"] for r in fx_post.reviews], ["COMMENTED"])
            self.assertEqual(fx_post.review_posts[0]["commit_id"], "b" * 40)
            self.assertIsNone(
                ReviewApprovalFinalizer(http=fx_post.http).load_eligibility(
                    token="installation-token",
                    repository="owner/repo",
                    pull_request=7,
                    head_sha="f" * 40,
                    app_slug="reviewsensei[bot]",
                    require_valid=True,
                )
            )

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
            self.assertEqual(g04.provider_calls, 1)
            self.assertEqual(row_g04["provider_has_exact_diff"], [True])
            self.assertIsNotNone(row_g04["terminal_transaction"])
            self.assertEqual(len(g04.acquisitions), 1)
            self.assertEqual(g04.acquisitions[0]["base"], g04.conflict_base)
            self.assertEqual(g04.acquisitions[0]["head"], g04.original_head)
            self.assertEqual(
                g04.acquisitions[0]["diff_sha256"],
                g04.acquisitions[0]["expected_diff_sha256"],
            )
            self.assertIn("-value = 0\n+value = 1\n", g04.analysis_diffs[0])
            self.assertNotIn("value = 2", g04.analysis_diffs[0])
            self.assertEqual(g04.review_payloads[-1]["commit_id"], g04.original_head)

    def test_supersedes_prior_clean_review_reconciliation_matrix(self) -> None:
        """Table test for `supersedes_prior_clean_review` control flow (RS-808BF5, RS-0C080F)."""

        head = "a" * 40
        base = "b" * 40
        clean_res = ReviewResult(
            summary="Clean complete review.",
            comments=(),
            provider="ollama",
            review_status="complete",
        )
        other_clean_res = ReviewResult(
            summary="Clean complete review with optional suggestion.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="Optional style suggestion.",
                    severity="low",
                    category="maintainability",
                    effective_blocking=False,
                    needs_human=False,
                ),
            ),
            provider="ollama",
            review_status="complete",
        )
        partial_res_1 = ReviewResult(
            summary="Partial review carrying incomplete coverage.",
            comments=(),
            provider="ollama",
            review_status="partial",
        )
        partial_res_2 = ReviewResult(
            summary="Different partial review.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="Optional note on partial run.",
                    severity="low",
                    category="maintainability",
                    effective_blocking=False,
                    needs_human=False,
                ),
            ),
            provider="ollama",
            review_status="partial",
        )
        human_res = ReviewResult(
            summary="Superseding review carrying human decision obligation.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="Verify behavior after reverting head commit.",
                    severity="high",
                    category="correctness",
                    effective_blocking=False,
                    needs_human=True,
                ),
            ),
            provider="ollama",
            review_status="complete",
        )

        def make_harness():
            stored_reviews: list[dict[str, object]] = []
            review_comments: dict[int, list[dict[str, object]]] = {}
            posted_events: list[str] = []
            next_id = 5000

            def opener(request, timeout=None):
                nonlocal next_id
                path = urlsplit(request.full_url).path
                method = request.get_method()
                if path.endswith("/pulls/7") and method == "GET":
                    return json_response(
                        _sample_pr_payload(number=7, base_sha=base, head_sha=head)
                    )
                if path.endswith("/pulls/7/reviews") and method == "GET":
                    return json_response(list(stored_reviews))
                if path.endswith("/pulls/7/reviews") and method == "POST":
                    payload = json.loads(request.data)
                    event = str(payload.get("event", "COMMENT"))
                    posted_events.append(event)
                    next_id += 1
                    rid = next_id
                    bot_user = {
                        "id": 999,
                        "login": "reviewsensei[bot]",
                        "type": "Bot",
                    }
                    created = {
                        "id": rid,
                        "state": (
                            "APPROVED"
                            if event == "APPROVE"
                            else (
                                "CHANGES_REQUESTED"
                                if event == "REQUEST_CHANGES"
                                else "COMMENTED"
                            )
                        ),
                        "body": payload.get("body", ""),
                        "commit_id": payload.get("commit_id", head),
                        "submitted_at": "2026-11-11T00:00:00Z",
                        "user": bot_user,
                    }
                    stored_reviews.append(created)
                    inline_list: list[dict[str, object]] = []
                    for idx, raw_comment in enumerate(
                        payload.get("comments") or [], start=1
                    ):
                        inline_list.append(
                            {
                                "id": rid * 100 + idx,
                                "pull_request_review_id": rid,
                                "body": raw_comment.get("body", ""),
                                "path": raw_comment.get("path", ""),
                                "line": raw_comment.get("line"),
                                "side": raw_comment.get("side", "RIGHT"),
                                "commit_id": payload.get("commit_id", head),
                                "user": bot_user,
                            }
                        )
                    review_comments[rid] = inline_list
                    return json_response(created, status=200)
                if (
                    "/pulls/7/reviews/" in path
                    and path.endswith("/comments")
                    and method == "GET"
                ):
                    rid_str = path.rsplit("/reviews/", 1)[1].split("/", 1)[0]
                    return json_response(list(review_comments.get(int(rid_str), [])))
                if path.endswith("/pulls/7/comments") and method == "GET":
                    all_comments = [
                        c for items in review_comments.values() for c in items
                    ]
                    return json_response(all_comments)
                if path.endswith("/rules/branches/main") and method == "GET":
                    return json_response([])
                if path.endswith("/branches/main/protection") and method == "GET":
                    return json_response(
                        {"message": "Branch not protected"}, status=404
                    )
                if path == "/graphql" and method == "POST":
                    return json_response(
                        {
                            "data": {
                                "repository": {
                                    "pullRequest": {
                                        "reviewThreads": {
                                            "nodes": [],
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
                raise AssertionError(f"unexpected request: {method} {path}")

            http = GitHubHttp(api_url="https://api.github.test", opener=opener)
            return (
                ReviewPublisher(http=http),
                stored_reviews,
                review_comments,
                posted_events,
            )

        def run_pub(
            publisher: ReviewPublisher, res: ReviewResult, *, auto_approve: bool = True
        ):
            return publisher.publish(
                token="installation-token",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                head_sha=head,
                base_branch="main",
                base_sha=base,
                diff=SAMPLE_DIFF,
                result=res,
                auto_approve=auto_approve,
                app_slug="reviewsensei[bot]",
                complete_inventory=True,
            )

        # 1. {clean persisted, non-clean prospective, empty result_states} across
        # COMMENTED-only, APPROVED+COMMENTED, and APPROVED-only identity states:
        # all publish a superseding COMMENT review without emitting APPROVE.
        for mode in ("commented_only", "mixed_approved_commented", "approved_only"):
            with self.subTest(case="clean_to_non_clean_empty_result_states", mode=mode):
                pub, stored, _rcomments, events = make_harness()
                init_out = run_pub(
                    pub, clean_res, auto_approve=(mode != "commented_only")
                )
                self.assertEqual(init_out.status, "published")
                if mode == "approved_only":
                    # Retain the clean eligibility body on a single APPROVED review.
                    comment_body = str(stored[0]["body"])
                    stored[:] = [
                        {**stored[-1], "state": "APPROVED", "body": comment_body}
                    ]
                events.clear()
                sup_out = run_pub(pub, human_res, auto_approve=True)
                self.assertEqual(sup_out.status, "published")
                self.assertEqual(events, ["COMMENT"])

        # 2. {clean persisted AFTER an earlier identical non-clean review (`result_states`
        # non-empty!), non-clean prospective}: STILL publishes superseding COMMENT rather
        # than short-circuiting on the stale pre-approval `result_states` match (RS-0C080F).
        with self.subTest(case="clean_after_prior_dirty_with_nonempty_result_states"):
            pub_pre, stored_pre, rcomments_pre, _ = make_harness()
            self.assertEqual(
                run_pub(pub_pre, human_res, auto_approve=True).status, "published"
            )
            earlier_dirty_review = copy.deepcopy(stored_pre[0])
            earlier_dirty_review["id"] = 4901
            earlier_dirty_comments = [
                {**c, "pull_request_review_id": 4901}
                for c in copy.deepcopy(rcomments_pre[int(stored_pre[0]["id"])])
            ]

            pub, stored, rcomments, events = make_harness()
            self.assertEqual(
                run_pub(pub, clean_res, auto_approve=True).status, "published"
            )
            # Prepend the earlier dirty review before the latest clean+APPROVED reviews.
            stored.insert(0, earlier_dirty_review)
            rcomments[4901] = earlier_dirty_comments
            events.clear()
            sup_nonempty = run_pub(pub, human_res, auto_approve=True)
            self.assertEqual(sup_nonempty.status, "published")
            self.assertEqual(events, ["COMMENT"])

        # 3. {clean persisted, clean prospective}:
        # - identical clean result short-circuits as `already_published` with zero POSTs;
        # - different clean result (`empty result_states`) fails closed under complete_inventory.
        with self.subTest(case="clean_to_clean"):
            pub, _stored, _rcomments, events = make_harness()
            self.assertEqual(
                run_pub(pub, clean_res, auto_approve=True).status, "published"
            )
            events.clear()
            same_clean = run_pub(pub, clean_res, auto_approve=True)
            self.assertEqual(same_clean.status, "already_published")
            self.assertEqual(events, [])
            with self.assertRaisesRegex(
                GitHubPublicationError,
                "existing review does not prove complete publication of this result",
            ):
                run_pub(pub, other_clean_res, auto_approve=True)

        # 4. {non-clean persisted, non-clean prospective, empty result_states}:
        # fails closed rather than treating an incomplete/non-clean review as superseded.
        with self.subTest(case="non_clean_to_non_clean_fails_closed"):
            pub, _stored, _rcomments, events = make_harness()
            self.assertEqual(
                run_pub(pub, partial_res_1, auto_approve=True).status, "published"
            )
            events.clear()
            with self.assertRaisesRegex(
                GitHubPublicationError,
                "existing review does not prove complete publication of this result",
            ):
                run_pub(pub, partial_res_2, auto_approve=True)
            self.assertEqual(events, [])

        # 5. {inline-blocking persisted, idempotent replay OR different non-clean prospective}:
        # inline blockers set `facts.has_blocking_findings=False` (not body-placed),
        # yet MUST replay as `already_published` and MUST NOT be misclassified as clean!
        inline_blocker_res = ReviewResult(
            summary="Review with an inline blocking defect.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="Null dereference on return value.",
                    severity="high",
                    category="correctness",
                    effective_blocking=True,
                    needs_human=False,
                ),
            ),
            provider="ollama",
            review_status="complete",
        )
        with self.subTest(case="inline_blocking_replay_and_non_supersession"):
            pub, _stored, _rcomments, events = make_harness()
            self.assertEqual(
                run_pub(pub, inline_blocker_res, auto_approve=True).status, "published"
            )
            events.clear()
            replay_blocker = run_pub(pub, inline_blocker_res, auto_approve=True)
            self.assertEqual(replay_blocker.status, "already_published")
            self.assertEqual(events, [])
            with self.assertRaisesRegex(
                GitHubPublicationError,
                "existing review does not prove complete publication of this result",
            ):
                run_pub(pub, human_res, auto_approve=True)
            self.assertEqual(events, [])

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
                    prior_head_tree_sha="c" * 40,
                    new_head_tree_sha="c" * 40,
                    diff_bytes_identical=True,
                ),
            )

        # `GitAncestryEvidence.unavailable()` fails closed to `unrelated_history`.
        unavail_ev = GitAncestryEvidence.unavailable()
        self.assertIsNone(unavail_ev.merge_base_sha)
        self.assertIsNone(unavail_ev.diff_bytes_identical)
        unavail = classify_source_identity_transition(
            previous_base_ref="main",
            previous_base_sha="a" * 40,
            previous_head_sha="b" * 40,
            current_base_ref="main",
            current_base_sha="a" * 40,
            current_head_sha="d" * 40,
            ancestry=unavail_ev,
        )
        self.assertEqual(unavail.transition_kind, "unrelated_history")
        self.assertFalse(unavail.baseline_cache_compatible)
        self.assertEqual(
            unavail.recovery_mode, "refuse_prepare_diff_retarget_or_rescan"
        )

        # Force-push classification fails closed when either tree SHA is missing,
        # even if diff_bytes_identical is True (RS-A0F9EA).
        for missing_tree_ancestry in (
            GitAncestryEvidence(
                merge_base_sha="a" * 40,
                prior_head_is_ancestor_of_new_head=False,
                prior_base_is_ancestor_of_new_base=True,
                prior_head_tree_sha=None,
                new_head_tree_sha=None,
                diff_bytes_identical=False,
            ),
            GitAncestryEvidence(
                merge_base_sha="a" * 40,
                prior_head_is_ancestor_of_new_head=False,
                prior_base_is_ancestor_of_new_base=True,
                prior_head_tree_sha=None,
                new_head_tree_sha="c" * 40,
                diff_bytes_identical=True,
            ),
        ):
            with self.assertRaisesRegex(
                GitHubPublicationError,
                "force-push ancestry classification requires head tree SHA or diff parity evidence",
            ):
                classify_source_identity_transition(
                    previous_base_ref="main",
                    previous_base_sha="a" * 40,
                    previous_head_sha="b" * 40,
                    current_base_ref="main",
                    current_base_sha="a" * 40,
                    current_head_sha="d" * 40,
                    ancestry=missing_tree_ancestry,
                )

    def test_hosted_unrelated_history_refuses_before_inference_and_confirms_clone_free_boundary(
        self,
    ) -> None:
        """G06 exact-scope: hosted ReviewRun unrelated-history refusal + clone-free boundary."""

        from review_sensei_github import cli as hosted_cli

        from review_sensei import workflow

        with tempfile.TemporaryDirectory() as tmp:
            g04 = G04MergeFixture(Path(tmp))
            # 1. Seed an active unresolved obligation on (common, original_head) via normal run.
            code_seed = g04.invoke(human=True)
            self.assertEqual(code_seed, 0, g04.error)
            self.assertEqual(g04.provider_calls, 1)
            assert g04.comment is not None
            seeded_record = parse_session_comment(
                g04.comment["body"], identity=IDENTITY
            )
            self.assertTrue(session_has_unresolved_obligations(seeded_record))
            seeded_generation = seeded_record.generation

            # 2. Construct a rootless orphan commit (zero parents -> no merge-base with common)
            # without disturbing the policy checkout HEAD.
            blob_oid = git(
                g04.root,
                "hash-object",
                "-w",
                "--stdin",
                input="orphan = True\n",
            ).stdout.strip()
            orphan_tree = git(
                g04.root,
                "mktree",
                input=f"100644 blob {blob_oid}\torphan.py\n",
            ).stdout.strip()
            orphan_head = git(
                g04.root,
                "commit-tree",
                orphan_tree,
                input="Unrelated synthetic root commit\n",
            ).stdout.strip()

            anc_orphan = inspect_git_ancestry(
                g04.root,
                previous_base_sha=g04.common,
                previous_head_sha=g04.original_head,
                current_base_sha=g04.common,
                current_head_sha=orphan_head,
            )
            self.assertIsNone(anc_orphan.merge_base_sha)
            dec_orphan = classify_source_identity_transition(
                previous_base_ref="main",
                previous_base_sha=g04.common,
                previous_head_sha=g04.original_head,
                current_base_ref="main",
                current_base_sha=g04.common,
                current_head_sha=orphan_head,
                ancestry=anc_orphan,
                previously_reviewed_head_shas={g04.original_head},
            )
            self.assertEqual(dec_orphan.transition_kind, "unrelated_history")
            self.assertTrue(dec_orphan.preserves_newer_session_obligations)
            self.assertFalse(dec_orphan.reuses_superseded_approval_without_verification)

            # 3. Execute real hosted `review-run` acquisition (`--diff` omitted so ReviewRun
            # invokes production `prepare_diff` against `g04.root`): must fail closed before
            # provider inference and leave the seeded obligation intact.
            g04.run_id += 1
            g04.live_base = g04.requested_base = g04.common
            g04.live_head = g04.requested_head = orphan_head
            g04.trace.clear()
            g04.transports.clear()
            g04.worker_observations.clear()
            original_main = hosted_cli.main

            def strip_prebuilt_diff(argv):
                args = list(argv)
                idx = args.index("--diff")
                del args[idx : idx + 2]
                return original_main(args)

            with (
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("live socket forbidden"),
                ),
                patch.object(workflow, "_fetch_ref", lambda *_a, **_kw: None),
                patch.object(hosted_cli, "main", strip_prebuilt_diff),
            ):
                code_orphan = g04.invoke(human=False)

            self.assertEqual(code_orphan, 1)
            self.assertEqual(g04.provider_calls, 1)
            self.assertIn("base and head commits do not share a merge base", g04.error)
            self.assertEqual(len(g04.review_payloads), 1)
            after_orphan = parse_session_comment(g04.comment["body"], identity=IDENTITY)
            assert after_orphan is not None
            self.assertTrue(session_has_unresolved_obligations(after_orphan))
            self.assertGreaterEqual(after_orphan.generation, seeded_generation)

            # 4. Clone-free hosted follow-up boundary confirmation at 07179c0f:
            # When no local checkout exists, `GitAncestryEvidence.unavailable()` fails closed
            # to `unrelated_history` while preserving session obligations without unrequested
            # network compare calls.
            clone_free_dec = classify_source_identity_transition(
                previous_base_ref="main",
                previous_base_sha=g04.common,
                previous_head_sha=g04.original_head,
                current_base_ref="main",
                current_base_sha=g04.common,
                current_head_sha=orphan_head,
                ancestry=GitAncestryEvidence.unavailable(),
                previously_reviewed_head_shas={g04.original_head},
            )
            self.assertEqual(clone_free_dec.transition_kind, "unrelated_history")
            self.assertFalse(clone_free_dec.baseline_cache_compatible)
            self.assertTrue(clone_free_dec.preserves_newer_session_obligations)
            self.assertEqual(
                clone_free_dec.recovery_mode,
                "refuse_prepare_diff_retarget_or_rescan",
            )

    def test_rebase_and_force_push_delayed_old_effects_and_zero_inference_retry_preserve_obligations(
        self,
    ) -> None:
        """G06 exact-scope: rebase vs ref-retarget, delayed old effects, and attempt=2 retry."""

        class _TransientReadRetryFixture(LifecycleEntryFixture):
            fail_next_pull_get = False

            def __init__(self, directory: Path) -> None:
                super().__init__(directory)
                self.run_attempts: dict[int, int] = {}

            def external(self, request, timeout):
                self.run_attempts[self.run_id] = max(
                    self.attempt, self.run_attempts.get(self.run_id, 1)
                )
                path = urlsplit(request.full_url).path
                if (
                    self.fail_next_pull_get
                    and request.get_method() == "GET"
                    and path.endswith("/pulls/7")
                ):
                    self.fail_next_pull_get = False
                    self.trace.append((self.attempt, "GET", path))
                    raise urllib.error.HTTPError(
                        request.full_url,
                        502,
                        "Bad Gateway",
                        {},  # type: ignore[arg-type]
                        io.BytesIO(b'{"message":"Bad Gateway"}'),
                    )
                resp = super().external(request, timeout)
                if (
                    request.get_method() == "GET"
                    and "/actions/runs/" in path
                    and not path.endswith("/jobs")
                ):
                    queried_run = int(path.split("/actions/runs/")[1].split("/")[0])
                    if queried_run != self.run_id and queried_run in self.run_attempts:
                        body = json.loads(resp.read(524288))
                        body["run_attempt"] = self.run_attempts[queried_run]
                        return json_response(body)
                return resp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "review").mkdir()
            (root / "reply").mkdir()

            fx = _TransientReadRetryFixture(root / "review")
            b1 = fx.live_base
            b2 = "c" * 40
            h1 = fx.live_head
            h1_rebased = "d" * 40
            h2_force_push = "e" * 40

            # 1. Seed source-bound obligation on (main@B1, H1).
            code_h1, out_h1 = fx.run_and_read(human=True)
            self.assertEqual(code_h1, 0, fx.error)
            assert out_h1 is not None
            self.assertEqual(out_h1["status"], "published")
            self.assertEqual([r["event"] for r in fx.review_posts], ["COMMENT"])
            rec_h1 = parse_session_comment(fx.comment["body"], identity=IDENTITY)
            self.assertTrue(session_has_unresolved_obligations(rec_h1))
            original_obligations = fx.retained_finding_obligations()

            # 2. Rebase PR #7 from (main@B1, H1) onto (main@B2, H1_rebased), explicitly
            # preserving the distinction between `same_sha_base_ref_retarget` and `base_sha` drift.
            fx.live_base = fx.requested_base = b2
            fx.live_head = fx.requested_head = h1_rebased

            # Delayed old-head (H1) review, finalizer, and mention-reply writes skip as
            # `skipped_stale_head`; delayed stale-base-SHA write on H1_rebased skips as
            # `skipped_stale_base` (`reason="stale_base_sha"`).
            pub = ReviewPublisher(http=fx.http)
            fin = ReviewApprovalFinalizer(http=fx.http)
            stale_head_pub = pub.publish(
                token="installation-token",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                head_sha=h1,
                base_branch="main",
                base_sha=b1,
                diff=SAMPLE_DIFF,
                result=ReviewResult(
                    summary="Delayed H1 review after rebase.",
                    comments=(),
                    provider="ollama",
                    review_status="complete",
                ),
                auto_approve=True,
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(stale_head_pub.status, "skipped_stale_head")
            stale_base_pub = pub.publish(
                token="installation-token",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                head_sha=h1_rebased,
                base_branch="main",
                base_sha=b1,
                diff=SAMPLE_DIFF,
                result=ReviewResult(
                    summary="Delayed B1 base-SHA review on H1_rebased.",
                    comments=(),
                    provider="ollama",
                    review_status="complete",
                ),
                auto_approve=True,
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(stale_base_pub.status, "skipped_stale_base")
            stale_fin = fin._preflight(
                token="installation-token",
                repository="owner/repo",
                pull_request=7,
                head_sha=h1,
                app_slug="reviewsensei[bot]",
            )
            assert stale_fin.result is not None
            self.assertEqual(stale_fin.result.status, "skipped_stale_head")
            self.assertEqual([r["event"] for r in fx.review_posts], ["COMMENT"])

            reply_fx = FreshnessReplyFixture(root / "reply")
            reply_fx.initialize_freshness()
            orig_reply_pub = ConversationPublisher._publish

            def rebase_before_reply(conv_pub, **kwargs):
                reply_fx.live_base = b2
                reply_fx.live_head = h1_rebased
                return orig_reply_pub(conv_pub, **kwargs)

            with patch.object(ConversationPublisher, "_publish", rebase_before_reply):
                code_reply = reply_fx.invoke_reply()
            self.assertEqual(code_reply, 1)
            self.assertEqual(reply_fx.reply_posts, 0)
            self.assertEqual(
                getattr(reply_fx.last_result, "status", None), "skipped_stale_head"
            )

            # 3. Rebased run on (B2, H1_rebased): attempt 1 encounters transient HTTP 502
            # on the post-staging pre-mutation read AFTER retaining analysis; attempt 2
            # replays retained analysis with ZERO additional model inference while
            # preserving H1's unresolved obligation and withholding APPROVE!
            orig_preflight = ReviewPublisher._preflight_pr

            def run_with_final_preflight_502() -> tuple[int, dict[str, object] | None]:
                armed = False
                before_reviews = len(fx.review_posts)

                def arm_final_preflight(publisher, **kwargs):
                    nonlocal armed
                    # Observe the actual source-owned lifecycle checkpoint for
                    # this exact new head/base/accepted result. The old detached
                    # human-inventory part is no longer this route's producer.
                    record = parse_session_comment(
                        fx.comment["body"], identity=IDENTITY
                    )
                    reference = record.finding_lifecycle
                    transaction = record.transaction
                    if (
                        not armed
                        and reference is not None
                        and transaction is not None
                        and transaction.result_sha256 is not None
                        and reference["operation_id"] == transaction.transaction_id
                        and reference["manifest"]["binding"]["head_sha"]
                        == kwargs["head_sha"]
                        and reference["manifest"]["binding"]["base_sha"]
                        == kwargs["base_sha"]
                    ):
                        self.assertEqual(len(fx.review_posts), before_reviews)
                        self.assertIn(
                            transaction.phase,
                            {"publication_pending", "publication_failed"},
                        )
                        armed = True
                        fx.fail_next_pull_get = True
                    return orig_preflight(publisher, **kwargs)

                with patch.object(
                    ReviewPublisher, "_preflight_pr", arm_final_preflight
                ):
                    result = fx.run_and_read(human=False)
                self.assertTrue(
                    armed, "current lifecycle checkpoint fault was not exercised"
                )
                self.assertFalse(
                    fx.fail_next_pull_get, "fault did not reach physical PR read"
                )
                self.assertEqual(len(fx.review_posts), before_reviews)
                return result

            fx.run_id += 1
            fx.attempt = 1
            fx.trace.clear()
            fx.transports.clear()
            fx.worker_observations.clear()
            code_rb_1, out_rb_1 = run_with_final_preflight_502()
            self.assertEqual(code_rb_1, 1)
            self.assertEqual(out_rb_1["status"], "publication_incomplete")
            self.assertEqual(out_rb_1["publication_status"], "unknown")
            self.assertFalse(out_rb_1["accounting_terminal"])
            self.assertIsNone(out_rb_1["review_id"])
            self.assertEqual(fx.provider_calls, 2)
            self.assertEqual([r["event"] for r in fx.review_posts], ["COMMENT"])

            fx.attempt = 2
            fx.trace.clear()
            fx.transports.clear()
            fx.worker_observations.clear()
            code_rb_2, out_rb_2 = fx.run_and_read(human=False)
            self.assertEqual(code_rb_2, 0, fx.error)
            assert out_rb_2 is not None
            self.assertEqual(out_rb_2["status"], "published")
            self.assertTrue(out_rb_2["accounting_terminal"])
            self.assertEqual(fx.provider_calls, 2)
            self.assertEqual(
                [r["event"] for r in fx.review_posts], ["COMMENT", "REQUEST_CHANGES"]
            )
            self.assertEqual(fx.review_posts[-1]["commit_id"], h1_rebased)
            rec_rebased = parse_session_comment(fx.comment["body"], identity=IDENTITY)
            self.assertTrue(session_has_unresolved_obligations(rec_rebased))
            fx.assert_preserved_omitted_obligations(self, original_obligations)

            # 4. Non-fast-forward force-push (B2, H1_rebased) -> (B2, H2_force_push) with
            # transient pre-mutation read error on attempt 1 + zero-inference retry on attempt 2.
            fx.attempt = 1
            fx.advance_run_to_head(h2_force_push)
            code_fp_1, _ = run_with_final_preflight_502()
            self.assertEqual(code_fp_1, 1)
            self.assertEqual(fx.provider_calls, 3)

            fx.attempt = 2
            fx.trace.clear()
            fx.transports.clear()
            fx.worker_observations.clear()
            code_fp_2, out_fp_2 = fx.run_and_read(human=False)
            self.assertEqual(code_fp_2, 0, fx.error)
            assert out_fp_2 is not None
            self.assertEqual(out_fp_2["status"], "published")
            self.assertTrue(out_fp_2["accounting_terminal"])
            self.assertEqual(fx.provider_calls, 3)
            self.assertEqual(
                [r["event"] for r in fx.review_posts],
                ["COMMENT", "REQUEST_CHANGES", "REQUEST_CHANGES"],
            )
            self.assertEqual(fx.review_posts[-1]["commit_id"], h2_force_push)
            rec_fp = parse_session_comment(fx.comment["body"], identity=IDENTITY)
            self.assertTrue(session_has_unresolved_obligations(rec_fp))
            fx.assert_preserved_omitted_obligations(self, original_obligations)


if __name__ == "__main__":
    unittest.main()
