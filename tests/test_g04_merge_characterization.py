"""G04 characterization; no production merge-readiness policy is imposed."""

from __future__ import annotations

import tempfile
import unittest

from g04_merge_support import G04MergeFixture


class G04HostedMergeCharacterization(unittest.TestCase):
    def test_generated_caller_keeps_automatic_and_manual_trigger_contracts(self):
        from pathlib import Path

        for relative in (
            ".github/workflows/review-sensei-review.yml",
            "examples/github-actions/review-sensei-review.yml",
        ):
            caller = Path(relative).read_text()
            self.assertIn("  pull_request:\n", caller)
            self.assertIn("  workflow_dispatch:\n", caller)
            self.assertNotIn("  pull_request_target:\n", caller)

    def test_reported_conflict_changes_do_not_change_bound_branch_review(self):
        for timing in ("before_acquisition", "during_analysis", "before_publication"):
            for direction in ("appears", "clears"):
                with (
                    self.subTest(timing=timing, direction=direction),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f = G04MergeFixture(directory)
                    before = {
                        "mergeable": direction == "appears",
                        "mergeable_state": "clean"
                        if direction == "appears"
                        else "dirty",
                    }
                    after = {
                        "mergeable": direction == "clears",
                        "mergeable_state": "dirty"
                        if direction == "appears"
                        else "clean",
                    }
                    f.merge_fields = before

                    def change():
                        f.merge_fields = after

                    if timing == "before_acquisition":
                        f.before_acquisition = change
                    elif timing == "during_analysis":
                        f.before_provider = change
                    else:
                        f.before_publication = change
                    code = f.invoke_fresh()
                    row = f.observation(
                        case=f"metadata_{direction}_{timing}", code=code
                    )
                    self.assertEqual(code, 0, f.error)
                    self.assertTrue(row["accounting_available"])
                    self.assertEqual(f.provider_calls, 1)
                    self.assertEqual(
                        row["analyzed_diff_sha256"],
                        [f.acquisitions[0]["expected_diff_sha256"]],
                    )
                    self.assertEqual(row["provider_has_exact_diff"], [True])
                    self.assertEqual(len(f.acquisitions), 1)
                    self.assertTrue(f.reviews)
                    self.assertEqual(row["phase"], "publication_succeeded")
                    self.assertEqual(f.merge_fields, after)
                    self.assertTrue(
                        any(r["mergeable"] == after["mergeable"] for r in f.pull_reads)
                    )

    def test_static_real_conflict_and_resolved_graph_both_acquire_exact_branch_diff(
        self,
    ):
        for graph in ("conflicted", "resolved"):
            for metadata in (
                {"mergeable": False, "mergeable_state": "dirty"},
                {"mergeable": None, "mergeable_state": "unknown"},
                {},
            ):
                with (
                    self.subTest(graph=graph, metadata=metadata),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f = G04MergeFixture(directory)
                    # GitHub does not schedule automatic pull_request runs for
                    # already-conflicted PRs. Exercise the existing dispatch lane.
                    f.event_name = "workflow_dispatch"
                    f.live_base = f.requested_base = f.conflict_base
                    if graph == "resolved":
                        f.live_head = f.requested_head = f.resolved_head
                    f.merge_fields = metadata
                    self.assertEqual(f.conflicts(), graph == "conflicted")
                    code = f.invoke_fresh()
                    row = f.observation(case=f"{graph}_{metadata}", code=code)
                    self.assertEqual(code, 0, f.error)
                    self.assertTrue(row["accounting_available"])
                    self.assertEqual(f.provider_calls, 1)
                    self.assertEqual(
                        row["analyzed_diff_sha256"],
                        [f.acquisitions[0]["expected_diff_sha256"]],
                    )
                    self.assertEqual(row["provider_has_exact_diff"], [True])
                    self.assertEqual(len(f.acquisitions), 1)
                    self.assertEqual(
                        f.acquisitions[0]["actual_conflict"], graph == "conflicted"
                    )
                    self.assertEqual(set(row["review_commits"]), {f.requested_head})
                    self.assertEqual(row["api_limit"], 512)
                    self.assertIsNotNone(row["terminal_transaction"])

    def test_real_graph_change_before_admission_refuses_stale_source_before_acquisition(
        self,
    ):
        for direction in ("appears", "clears"):
            with (
                self.subTest(direction=direction),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = G04MergeFixture(directory)
                if direction == "appears":
                    f.live_base = f.conflict_base
                else:
                    f.live_base = f.requested_base = f.conflict_base
                    f.live_head = f.resolved_head
                self.assertEqual(f.conflicts(), direction == "appears")
                code = f.invoke_fresh()
                f.observation(
                    case=f"stale_source_{direction}_before_acquisition", code=code
                )
                self.assertEqual(code, 1)
                self.assertIn("live reviewed base or head conflicts", f.error)
                self.assertEqual(f.provider_calls, 0)
                self.assertEqual(f.acquisitions, [])
                self.assertFalse(f.reviews)
                self.assertIsNone(f.comment)

    def test_real_graph_changes_refuse_publication_without_terminal_settlement(
        self,
    ):
        for timing in ("before_acquisition", "during_analysis", "before_publication"):
            for direction in ("appears", "clears"):
                with (
                    self.subTest(timing=timing, direction=direction),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f = G04MergeFixture(directory)
                    if direction == "appears":

                        def change():
                            f.live_base = f.conflict_base
                    else:
                        f.live_base = f.requested_base = f.conflict_base

                        def change():
                            f.live_head = f.resolved_head

                    if timing == "before_acquisition":
                        f.before_acquisition = change
                    elif timing == "during_analysis":
                        f.before_provider = change
                    else:
                        f.before_publication = change
                    code = f.invoke_fresh()
                    row = f.observation(case=f"source_{direction}_{timing}", code=code)
                    self.assertEqual(code, 1)
                    if timing == "before_acquisition" and direction == "clears":
                        self.assertEqual(
                            f.broker_head_rejections,
                            [(f.original_head, f.resolved_head)],
                        )
                        self.assertIn("Broker token request was rejected", f.error)
                        self.assertIn('"step":"broker_actions"', f.error)
                        self.assertEqual(f.provider_calls, 0)
                        self.assertEqual(row["analyzed_diff_sha256"], [])
                        self.assertEqual(row["provider_has_exact_diff"], [])
                        self.assertEqual(len(f.acquisitions), 1)
                        self.assertFalse(f.reviews)
                        self.assertFalse(f.check_runs)
                        self.assertIsNone(f.comment)
                        self.assertFalse(row["accounting_available"])
                        self.assertIsNone(row["phase"])
                        self.assertIsNone(row["terminal_transaction"])
                        self.assertFalse(row["final_actual_conflict"])
                        continue
                    self.assertTrue(row["accounting_available"], row)
                    self.assertEqual(f.provider_calls, 1, row)
                    self.assertEqual(
                        row["analyzed_diff_sha256"],
                        [f.acquisitions[0]["expected_diff_sha256"]],
                    )
                    self.assertEqual(row["provider_has_exact_diff"], [True])
                    self.assertEqual(len(f.acquisitions), 1)
                    self.assertFalse(f.reviews)
                    expected_phase = (
                        None
                        if timing != "before_publication"
                        else (
                            "publication_failed"
                            if direction == "appears"
                            else "publication_pending"
                        )
                    )
                    self.assertEqual(row["phase"], expected_phase)
                    reason = (
                        "caller workflow publication head changed"
                        if expected_phase == "publication_pending"
                        else "retention current pull request identity mismatches"
                    )
                    self.assertIn(reason, f.error)
                    self.assertIsNone(row["terminal_transaction"])
                    self.assertEqual(
                        row["final_actual_conflict"], direction == "appears"
                    )

    def test_conflict_metadata_does_not_bypass_initial_draft_or_writer_authority(self):
        for guard in (
            "draft",
            "closed",
            "broker_refusal",
            "repository_id",
            "fork",
            "overlap",
        ):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory() as directory:
                f = G04MergeFixture(directory)
                f.merge_fields = {"mergeable": True, "mergeable_state": "clean"}
                if guard == "draft":
                    f.pr_overrides["draft"] = True
                elif guard == "closed":
                    f.pr_overrides["state"] = "closed"
                elif guard == "broker_refusal":
                    f.refuse_broker = True
                elif guard == "overlap":
                    f.overlap = True
                else:
                    f.source_fault = guard
                code = f.invoke_fresh()
                row = f.observation(case=f"initial_guard_{guard}", code=code)
                self.assertEqual(code, 1)
                self.assertEqual(f.provider_calls, 0)
                self.assertEqual(
                    len(f.acquisitions),
                    1 if guard in {"broker_refusal", "overlap"} else 0,
                )
                self.assertEqual(row["analyzed_diff_sha256"], [])
                self.assertEqual(row["provider_has_exact_diff"], [])
                self.assertFalse(f.reviews)
                self.assertFalse(f.check_runs)
                self.assertIsNone(f.comment)
                self.assertIsNone(row["terminal_transaction"])

    def test_late_draft_or_closed_state_is_not_overridden_by_clean_mergeability(self):
        for timing in ("during_analysis", "before_publication"):
            for field, value in (("draft", True), ("state", "closed")):
                with (
                    self.subTest(timing=timing, field=field),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f = G04MergeFixture(directory)

                    def change():
                        f.pr_overrides.update({field: value})

                    if timing == "during_analysis":
                        f.before_provider = change
                    else:
                        f.before_publication = change
                    code = f.invoke_fresh()
                    row = f.observation(case=f"late_{field}_{timing}", code=code)
                    self.assertEqual(code, 1)
                    self.assertTrue(row["accounting_available"])
                    self.assertEqual(f.provider_calls, 1)
                    self.assertEqual(
                        row["analyzed_diff_sha256"],
                        [f.acquisitions[0]["expected_diff_sha256"]],
                    )
                    self.assertEqual(row["provider_has_exact_diff"], [True])
                    self.assertFalse(f.reviews)
                    expected_phase = (
                        None
                        if field == "state" and timing == "during_analysis"
                        else "publication_failed"
                    )
                    self.assertEqual(row["phase"], expected_phase)
                    if field == "state":
                        self.assertIn(
                            "retention current pull request identity mismatches",
                            f.error,
                        )
                    else:
                        self.assertIn("publication_finalization_incomplete", f.error)
                        self.assertIn("skipped_pr_state", row["publisher_preflight"])
