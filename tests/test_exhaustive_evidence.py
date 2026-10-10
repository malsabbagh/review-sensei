"""Synthetic acquisition and group regressions through the public evidence seam."""

from __future__ import annotations

import io
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import (
    EvidenceBundle,
    EvidenceGroup,
    EvidenceRecord,
    EvidenceSnapshot,
)
from review_sensei.hosting.github.http import (
    MAX_GITHUB_RESPONSE_BYTES,
    GitHubHttp,
    GitHubHTTPError,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPResponseTooLargeError,
)
from review_sensei.models import ProviderRequest, ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget
from review_sensei.planning import WorkRequirement, plan_work
from review_sensei.service import ReviewService
from review_sensei.stages import Stage
from review_sensei.validation import ReviewLimits

SNAPSHOT = EvidenceSnapshot("owner/repo", 1, "a" * 40, "b" * 40)
PATCH = "@@ -1 +1 @@\n-old\n+new\n"


class Response(io.BytesIO):
    status = 200


def file_item(index):
    return {
        "filename": f"src/file-{index}.py",
        "patch": PATCH,
        "additions": 1,
        "deletions": 1,
    }


class ExhaustiveAcquisitionTests(unittest.TestCase):
    def test_high_uptime_acquisition_preserves_fresh_and_restored_shared_deadlines(
        self,
    ):
        start = float.fromhex("0x1.ffff100000003p+22")
        self.assertGreater(start + 60 - start, 60)
        for shared in ("none", "fresh", "restored"):
            with self.subTest(shared=shared):
                clock = [start]
                snapshot = (
                    {"schema_version": "1.0", "calls": 7, "deadline_unix_ms": 1060000}
                    if shared == "restored"
                    else None
                )
                budget = EvidenceReadBudget(
                    clock=lambda: clock[0], wall_clock=lambda: 1000, snapshot=snapshot
                )
                original_deadline = budget.deadline
                original_snapshot = budget.snapshot()
                http, _, calls, _ = self.fixture(clock=clock, timeout=60)
                before_read = None if shared == "none" else budget.consume
                bundle = self.load(
                    http, monotonic=lambda: clock[0], before_read=before_read
                )
                self.assertTrue(bundle.enumeration_complete)
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[0][2], 60)
                self.assertTrue(all(0 < timeout <= 60 for _, _, timeout in calls))
                self.assertLess(calls[-1][2], calls[0][2])
                self.assertEqual(budget.deadline, original_deadline)
                self.assertEqual(
                    budget.wall_deadline_ms, original_snapshot["deadline_unix_ms"]
                )
                if shared != "none":
                    self.assertEqual(budget.calls, original_snapshot["calls"] + 3)
                    clock[0] = original_deadline
                    with self.assertRaisesRegex(ReviewInputError, "deadline"):
                        self.load(
                            http, monotonic=lambda: clock[0], before_read=budget.consume
                        )
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(budget.calls, original_snapshot["calls"] + 3)

    def test_shared_read_guard_charges_each_attempt_and_does_not_reset(self):
        http, _, calls, _ = self.fixture(
            102, oversized=lambda size, offset: offset == 100 and size > 1
        )
        charged = []

        def before_read():
            charged.append(len(calls))
            return 12.0

        self.load(http, before_read=before_read)
        self.assertEqual(charged, list(range(len(calls))))
        self.assertTrue(all(timeout <= 12 for _, _, timeout in calls))
        # The host's exhausted allowance refuses before even the metadata GET.
        for allowance in (0, -1, True, float("nan"), 61):
            with self.assertRaises(GitHubHTTPPaginationLimitError):
                self.load(http, before_read=lambda: allowance)
        self.assertEqual(len(calls), 10)

    def test_shared_original_deadline_also_checks_response_completion(self):
        clock = [0.0]
        http, _, calls, _ = self.fixture(clock=clock)
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            self.load(http, monotonic=lambda: clock[0], before_read=lambda: 1.0)
        self.assertEqual(len(calls), 2)

    def fixture(self, count=1, *, oversized=None, mutate=None, clock=None, timeout=30):
        files = [file_item(index) for index in range(count)]
        calls = []
        fences = []

        def opener(request, timeout):
            parsed = urlsplit(request.full_url)
            calls.append((parsed.path, parse_qs(parsed.query), timeout))
            if parsed.path.endswith("/files"):
                query = parse_qs(parsed.query)
                size, page = int(query["per_page"][0]), int(query["page"][0])
                offset = (page - 1) * size
                if oversized is not None and oversized(size, offset):
                    return Response(b"x" * (MAX_GITHUB_RESPONSE_BYTES + 1))
                body = files[offset : offset + size]
                if clock is not None:
                    clock[0] += 1
            else:
                body = {
                    "base": {"sha": SNAPSHOT.base_sha},
                    "head": {"sha": SNAPSHOT.head_sha},
                    "changed_files": count,
                }
                fences.append(body)
                if mutate is not None:
                    mutate(body, len(fences))
            return Response(json.dumps(body, ensure_ascii=False).encode())

        return GitHubHttp(opener=opener, timeout=timeout), files, calls, fences

    def load(self, http, **kwargs):
        return http.load_review_evidence(
            token="synthetic",
            snapshot=SNAPSHOT,
            required_paths=(),
            include_all_changed=True,
            **kwargs,
        )

    def test_count_bound_999_and_1000_are_complete_and_1001_refuses(self):
        for count in (999, 1000):
            http, _, calls, fences = self.fixture(count)
            bundle = self.load(http)
            self.assertEqual(len(bundle.records), count)
            self.assertTrue(bundle.enumeration_complete)
            self.assertTrue(all(record.complete for record in bundle.records))
            self.assertEqual(len(fences), 2)
            self.assertEqual(len(calls), 12 if count == 999 else 13)
        http, _, calls, _ = self.fixture(1001)
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            self.load(http)
        self.assertEqual(len(calls), 1)

    def test_all_fallbacks_preserve_late_exact_offset_after_100_items(self):
        def oversized(size, offset):
            return offset == 100 and size > 1

        http, _, calls, _ = self.fixture(102, oversized=oversized)
        bundle = self.load(http)
        self.assertEqual(len(bundle.records), 102)
        pages = [
            (int(q["per_page"][0]), int(q["page"][0]))
            for path, q, _ in calls
            if path.endswith("/files")
        ]
        self.assertEqual(
            pages,
            [
                (100, 1),
                (100, 2),
                (50, 3),
                (25, 5),
                (5, 21),
                (1, 101),
                (1, 102),
                (1, 103),
            ],
        )
        self.assertIn("src/file-101.py", bundle.by_path())

    def test_oversized_single_item_with_late_required_file_never_certifies(self):
        http, _, calls, _ = self.fixture(
            102, oversized=lambda size, offset: offset == 100
        )
        with self.assertRaises(GitHubHTTPResponseTooLargeError):
            http.load_review_evidence(
                token="synthetic",
                snapshot=SNAPSHOT,
                required_paths=("src/file-101.py",),
            )
        self.assertEqual(len(calls), 7)

    def test_response_boundary_counts_utf8_wire_bytes_including_framing(self):
        for delta in (-1, 0, 1):
            raw = b'{"v":"' + b"x" * (MAX_GITHUB_RESPONSE_BYTES - 8 + delta) + b'"}'
            self.assertEqual(len(raw), MAX_GITHUB_RESPONSE_BYTES + delta)
            http = GitHubHttp(opener=lambda request, timeout: Response(raw))
            if delta == 1:
                with self.assertRaises(GitHubHTTPResponseTooLargeError):
                    http.request("GET", "/bounded", token="synthetic")
            else:
                self.assertEqual(
                    http.request("GET", "/bounded", token="synthetic")[0], 200
                )

    def test_required_only_reads_are_fenced_and_zero_required_all_changed_reads(self):
        http, _, calls, fences = self.fixture(2)
        bundle = http.load_review_evidence(
            token="synthetic", snapshot=SNAPSHOT, required_paths=("src/file-1.py",)
        )
        self.assertEqual(tuple(bundle.by_path()), ("src/file-1.py",))
        self.assertEqual(len(fences), 2)
        self.assertEqual(len(calls), 3)
        http, _, _, _ = self.fixture(2)
        self.assertEqual(len(self.load(http).records), 2)

    def test_base_head_and_count_changes_on_either_fence_refuse(self):
        for field in ("base", "head", "changed_files"):
            for fence_index in (1, 2):

                def mutate(body, index):
                    if index == fence_index:
                        if field == "changed_files":
                            body[field] = 2
                        else:
                            body[field]["sha"] = "c" * 40

                http, _, _, _ = self.fixture(mutate=mutate)
                with (
                    self.subTest(field=field, fence=fence_index),
                    self.assertRaises(GitHubHTTPError),
                ):
                    self.load(http)

    def test_absent_bool_or_invalid_counts_never_certify_enumeration(self):
        for count in (None, True, -1, "1"):
            http, _, _, _ = self.fixture(
                mutate=lambda body, _: body.update(changed_files=count)
            )
            with self.assertRaises(GitHubHTTPError):
                self.load(http)

    def test_duplicate_unrelated_paths_are_detected_before_required_selection(self):
        http, files, _, _ = self.fixture(3)
        files[1] = files[0].copy()
        with self.assertRaisesRegex(GitHubHTTPError, "duplicated"):
            http.load_review_evidence(
                token="synthetic", snapshot=SNAPSHOT, required_paths=("src/file-2.py",)
            )

    def test_read_and_deadline_exhaustion_include_retries_and_fences(self):
        http, _, calls, _ = self.fixture(100, oversized=lambda size, offset: size > 1)
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            self.load(http)
        self.assertEqual(len(calls), 63)
        clock = [0.0]
        http, _, calls, _ = self.fixture(clock=clock)
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            self.load(http, timeout_seconds=1, monotonic=lambda: clock[0])
        self.assertEqual(len(calls), 2)

    def test_missing_binary_and_conflicting_patch_counts_remain_incomplete(self):
        http, files, _, _ = self.fixture(3)
        files[0].pop("patch")
        files[1]["patch"] = "Binary files differ"
        files[2]["additions"] = 2
        bundle = self.load(http)
        self.assertTrue(bundle.enumeration_complete)
        self.assertFalse(any(record.complete for record in bundle.records))

    def test_rename_deletion_unicode_and_literal_glob_citations_preserve_bytes(self):
        http, files, _, _ = self.fixture(2)
        files[0].update(filename='src/é [*]".py', previous_filename="src/old.py")
        files[1].update(
            filename="src/deleted.py", patch="@@ -1 +0,0 @@\n-old\n", additions=0
        )
        bundle = self.load(http)
        self.assertTrue(all(record.complete for record in bundle.records))
        self.assertEqual(bundle.by_path()['src/é [*]".py'].old_path, "src/old.py")
        self.assertEqual(bundle.by_path()['src/é [*]".py'].patch, PATCH)


class ExhaustiveGroupTests(unittest.TestCase):
    def test_discovery_cannot_certify_repeated_or_oversized_atomic_hunks(self):
        class Provider:
            name = "fixture"
            model = "fixture"
            calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse(
                    '{"summary":"Reviewed.","comments":[]}', self.name
                )

        stage = Stage(
            name="review",
            prompt_template="Review {diff}",
            outputs=("summary", "comments"),
        )
        for patch in (PATCH + PATCH, "@@ -1 +1 @@\n-old\n+" + "x" * 1000 + "\n"):
            provider = Provider()
            service = ReviewService(
                provider,
                stages=(stage,),
                work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=250),
            )
            run = service.run(ReviewRequest(diff=self.record(patch=patch).diff))
            self.assertEqual(provider.calls, 0)
            self.assertFalse(run.result.coverage.fully_reviewed)

    def record(self, path="src/a.py", patch=PATCH):
        return EvidenceRecord(
            path,
            patch,
            SNAPSHOT,
            expected_additions=patch.count("\n+"),
            expected_deletions=patch.count("\n-"),
        )

    def group(self, count=1):
        records = tuple(self.record(f"src/file-{index}.py") for index in range(count))
        return EvidenceGroup(
            EvidenceBundle(SNAPSHOT, records), tuple(record.path for record in records)
        )

    def test_multi_hunk_and_giant_hunk_materialize_exhaustive_bytes_and_counts(self):
        for patch in (
            "@@ -1 +1 @@\n-old\n+" + "é" * 16000 + "\n",
            "@@ -1 +1 @@\n-old\n+one\n@@ -3 +3 @@\n-old\n+two\n",
        ):
            record = self.record(patch=patch)
            group = EvidenceGroup(EvidenceBundle(SNAPSHOT, (record,)), (record.path,))
            restored = EvidenceGroup.from_document(group.to_document())
            self.assertTrue(restored.complete)
            self.assertEqual(restored.group_id, group.group_id)
            self.assertEqual(restored.records[0].patch, patch)
            self.assertEqual(
                restored.coverage_document()["files"][0]["additions"],
                patch.count("\n+"),
            )

    def test_zero_length_adjacent_insertions_deletions_and_rename_are_complete(self):
        for patch in (
            "@@ -1,0 +2 @@\n+one\n@@ -2,0 +4 @@\n+two\n",
            "@@ -1 +0,0 @@\n-one\n@@ -2 +0,0 @@\n-two\n",
            "@@ -1 +1 @@\n-one\n+two\n@@ -2 +2 @@\n-three\n+four\n",
        ):
            record = replace(self.record(patch=patch), old_path="src/old.py")
            self.assertTrue(record.complete)
        for patch in (
            PATCH + PATCH,
            "@@ -3 +3 @@\n-one\n+two\n@@ -1 +1 @@\n-three\n+four\n",
        ):
            self.assertFalse(self.record(patch=patch).complete)

    def test_cross_file_1_4_5_8_count_findings_independently_and_cite_each_path(self):
        budgets = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(), resource=ResourceBudget.create(), mode="reassessment"
        )
        for count in (1, 4, 5, 8):
            group = self.group(count)
            requirement = WorkRequirement(
                "finding", tuple(record.evidence_id for record in group.records)
            )
            plan = plan_work(
                "reassessment",
                group.bundle,
                (requirement,),
                budgets=budgets,
                render=lambda batch: ProviderRequest(prompt=batch.diff_context),
            )
            self.assertEqual(len(plan.batches), 1)
            self.assertEqual(plan.unprocessed, ())
            for record in group.records:
                self.assertIn(
                    f"path={record.path}\n{record.patch}", plan.batches[0].diff_context
                )

    def test_atomic_oversized_group_stays_pending_and_never_renders_fragments(self):
        group = self.group(8)
        budgets = ReviewWorkBudgets(mode="unified", batch_diff_bytes=100).effective(
            limits=ReviewLimits(), resource=ResourceBudget.create(), mode="reassessment"
        )
        render_calls = []

        def render(batch):
            render_calls.append(batch)
            return ProviderRequest(prompt=batch.diff_context)

        requirement = WorkRequirement(
            "finding", tuple(record.evidence_id for record in group.records)
        )
        plan = plan_work(
            "reassessment", group.bundle, (requirement,), budgets=budgets, render=render
        )
        self.assertTrue(group.complete)
        self.assertEqual(plan.batches, ())
        self.assertEqual(
            plan.unprocessed, (("finding", "required-evidence-oversized"),)
        )
        self.assertEqual(render_calls, [])

    def test_missing_incomplete_and_uneumerated_groups_have_precise_pending(self):
        record = self.record()
        cases = (
            (EvidenceBundle(SNAPSHOT, ()), "required-evidence-missing"),
            (
                EvidenceBundle(SNAPSHOT, (replace(record, patch=""),)),
                "required-evidence-incomplete",
            ),
            (EvidenceBundle(SNAPSHOT, (record,), False), "incomplete-enumeration"),
        )
        for bundle, reason in cases:
            group = EvidenceGroup(bundle, (record.path,))
            self.assertFalse(group.complete)
            self.assertEqual(group.pending, ((record.path, reason),))
            self.assertEqual(
                EvidenceGroup.from_document(group.to_document()).pending, group.pending
            )
            with self.assertRaises(ReviewInputError):
                _ = group.diff_context

    def test_snapshot_part_loss_counts_and_framing_changes_invalidate_group(self):
        group = self.group(5)
        document = group.to_document()
        changes = []
        changed = deepcopy(document)
        changed["records"].pop()
        changes.append(changed)
        changed = deepcopy(document)
        changed["coverage"]["files"][0]["additions"] = True
        changes.append(changed)
        changed = deepcopy(document)
        changed["coverage"]["snapshot"]["base_sha"] = "c" * 40
        changes.append(changed)
        changed = deepcopy(document)
        changed["records"][0]["patch"] += "\n"
        changes.append(changed)
        changed = deepcopy(document)
        changed["extra"] = "unsupported"
        changes.append(changed)
        for changed in changes:
            with self.assertRaises(ReviewInputError):
                EvidenceGroup.from_document(changed)

    def test_materialization_has_real_canonical_byte_bound(self):
        record = self.record(
            patch="@@ -1 +1 @@\n-old\n+" + "x" * (2 * 1024 * 1024) + "\n"
        )
        group = EvidenceGroup(EvidenceBundle(SNAPSHOT, (record,)), (record.path,))
        self.assertTrue(group.complete)
        with self.assertRaisesRegex(ReviewInputError, "materialization byte bound"):
            group.to_document()


if __name__ == "__main__":
    unittest.main()
