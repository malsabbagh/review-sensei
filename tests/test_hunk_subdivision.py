"""Exact, bounded discovery segments never become complete file evidence."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei.budgets import LEGACY_ASSESSMENT_PROMPT_BYTES, ReviewWorkBudgets
from review_sensei.diff import analyze_diff
from review_sensei.document_context import DocumentDecision, DocumentSelectionReport
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot
from review_sensei.hunk_subdivision import (
    HUNK_SUBDIVISION_POLICY,
    SubdivisionAllowance,
    subdivide_hunk,
)
from review_sensei.models import (
    ProviderResponse,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.service import (
    _PROVIDER_OUTPUT_CORRECTION,
    ReviewService,
)
from review_sensei.stages import Stage
from review_sensei.work_recovery import WorkRecoveryStore

SOURCE_SEPARATORS = (
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)


class HunkSubdivisionTests(unittest.TestCase):
    def fixture(self, patch):
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        source = EvidenceRecord("src/example.py", patch, snapshot)
        record = source.analysis.file_records[0]
        return snapshot, source, record, record.hunks[0]

    def test_every_body_byte_and_current_line_survives_balanced_subdivision(self):
        patch = "@@ -7,3 +11,4 @@ function\n context\n-old\n+新\n+next\n tail\n"
        snapshot, source, record, hunk = self.fixture(patch)
        parts = subdivide_hunk(
            record,
            hunk,
            snapshot,
            admit=lambda part: (
                len(part.analysis.hunk_records[0].text.splitlines()) <= 3
            ),
        )
        self.assertGreater(len(parts), 1)
        body = hunk.text.partition("\n")[2].encode()
        cursor = 0
        rebuilt = []
        added, deleted = set(), set()
        for part in parts:
            proof = part.fragment
            self.assertEqual(
                proof.source_evidence_id,
                EvidenceRecord.from_diff_record(record, snapshot).evidence_id,
            )
            self.assertEqual(proof.body_start, cursor)
            self.assertFalse(part.complete)
            self.assertEqual(part.snapshot, snapshot)
            parsed = analyze_diff(part.diff)
            piece_body = parsed.hunk_records[0].text.partition("\n")[2].encode()
            self.assertEqual(piece_body, body[proof.body_start : proof.body_end])
            rebuilt.append(piece_body)
            cursor = proof.body_end
            added.update(parsed.file_records[0].added_lines)
            deleted.update(parsed.file_records[0].deleted_lines)
        self.assertEqual(b"".join(rebuilt), body)
        self.assertEqual(cursor, len(body))
        self.assertEqual(added, set(record.added_lines))
        self.assertEqual(deleted, set(record.deleted_lines))
        EvidenceBundle(snapshot, parts)

    def test_no_newline_marker_stays_with_its_exact_source_line(self):
        snapshot, _, record, hunk = self.fixture(
            "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        )
        parts = subdivide_hunk(record, hunk, snapshot, admit=lambda part: False)
        self.assertEqual(len(parts), 2)
        for part in parts:
            lines = part.analysis.hunk_records[0].text.splitlines()
            self.assertEqual(lines[-1], "\\ No newline at end of file")
            self.assertEqual(len(lines), 3)

    def test_empty_canonical_body_is_rejected_before_admission(self):
        snapshot, _, record, hunk = self.fixture("@@ -0,0 +1 @@\n+line\n")
        hunk = replace(
            hunk,
            old_start=0,
            old_count=0,
            new_start=0,
            new_count=0,
            text="@@ -0,0 +0,0 @@\n",
            added_lines=frozenset(),
            deleted_lines=frozenset(),
        )
        record = replace(
            record,
            text=record.header + hunk.text,
            hunks=(hunk,),
            added_lines=frozenset(),
            deleted_lines=frozenset(),
        )
        probes = []
        with self.assertRaisesRegex(ReviewInputError, "source body is missing"):
            subdivide_hunk(
                record, hunk, snapshot, admit=lambda part: probes.append(part) or True
            )
        self.assertFalse(probes)

    def test_single_part_and_zero_shared_allowance_preserve_the_whole_body(self):
        snapshot, _, record, hunk = self.fixture("@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n")
        for options in ({"max_parts": 1}, {"allowance": SubdivisionAllowance(0)}):
            with self.subTest(options=options):
                probes = []
                parts = subdivide_hunk(
                    record,
                    hunk,
                    snapshot,
                    admit=lambda part: probes.append(part) or False,
                    **options,
                )
                self.assertEqual(len(probes), 1)
                self.assertEqual(len(parts), 1)
                self.assertEqual(parts[0].fragment.body_start, 0)
                self.assertEqual(
                    parts[0].fragment.body_end, parts[0].fragment.body_bytes
                )
                self.assertEqual(
                    parts[0].analysis.hunk_records[0].text.partition("\n")[2],
                    hunk.text.partition("\n")[2],
                )
                self.assertFalse(parts[0].complete)

    def test_invalid_partition_and_shared_allowances_are_rejected(self):
        snapshot, _, record, hunk = self.fixture("@@ -1 +1 @@\n-old\n+new\n")
        for value in (0, -1, True, 1.5):
            with self.subTest(max_parts=value), self.assertRaises(ReviewInputError):
                subdivide_hunk(
                    record, hunk, snapshot, admit=lambda part: True, max_parts=value
                )
        for value in (-1, 128, True, 1.5):
            with self.subTest(remaining=value), self.assertRaises(ReviewInputError):
                SubdivisionAllowance(value)

    def test_crlf_source_and_multibyte_lines_conserve_exact_utf8_bytes(self):
        patch = "@@ -0,0 +1,2 @@\n+新\r\n+é\r\n"
        snapshot, source, record, hunk = self.fixture(patch)
        self.assertEqual(hunk.text, patch)
        lf_source = self.fixture(patch.replace("\r\n", "\n"))[1]
        self.assertNotEqual(source.evidence_id, lf_source.evidence_id)
        parts = subdivide_hunk(record, hunk, snapshot, admit=lambda part: False)
        bodies = [
            part.analysis.hunk_records[0].text.partition("\n")[2].encode("utf-8")
            for part in parts
        ]
        self.assertEqual(b"".join(bodies), hunk.text.partition("\n")[2].encode())
        self.assertEqual(
            [(part.fragment.body_start, part.fragment.body_end) for part in parts],
            [(0, 6), (6, 11)],
        )

    def test_source_separators_and_prefixlike_text_stay_in_physical_line_units(self):
        for separator in SOURCE_SEPARATORS:
            for following in ("+addition", "-deletion", "\\ No newline at end of file"):
                with self.subTest(separator=repr(separator), following=following):
                    units = [
                        f" context{separator}{following}\n",
                        f"-old{separator}{following}\n\\ No newline at end of file\n",
                        f"+新{separator}{following}\n\\ No newline at end of file\n",
                        " tail\n",
                    ]
                    patch = "@@ -7,3 +11,3 @@ function\n" + "".join(units)
                    snapshot, _, record, hunk = self.fixture(patch)
                    probes = []
                    parts = subdivide_hunk(
                        record,
                        hunk,
                        snapshot,
                        admit=lambda part: probes.append(part) or False,
                    )
                    self.assertEqual(len(parts), 4)
                    self.assertEqual(len(probes), 7)
                    body = "".join(units).encode()
                    cursor = 0
                    added, deleted = set(), set()
                    for part, expected, counts in zip(
                        parts, units, ((1, 1), (1, 0), (0, 1), (1, 1)), strict=True
                    ):
                        proof = part.fragment
                        piece = part.analysis.hunk_records[0]
                        actual = piece.text.partition("\n")[2].encode()
                        self.assertEqual(actual, expected.encode())
                        self.assertEqual((piece.old_count, piece.new_count), counts)
                        self.assertEqual(proof.body_start, cursor)
                        cursor += len(actual)
                        self.assertEqual(proof.body_end, cursor)
                        self.assertEqual(proof.body_bytes, len(body))
                        self.assertEqual(
                            proof.body_sha256, hashlib.sha256(actual).hexdigest()
                        )
                        self.assertEqual(
                            proof.source_hunk_sha256,
                            hashlib.sha256(hunk.text.encode()).hexdigest(),
                        )
                        self.assertEqual(
                            proof.source_evidence_id,
                            EvidenceRecord.from_diff_record(
                                record, snapshot
                            ).evidence_id,
                        )
                        self.assertEqual(
                            actual, body[proof.body_start : proof.body_end]
                        )
                        added.update(piece.added_lines)
                        deleted.update(piece.deleted_lines)
                        self.assertFalse(part.complete)
                    self.assertEqual(cursor, len(body))
                    self.assertEqual(added, {12})
                    self.assertEqual(deleted, {8})
                    EvidenceBundle(snapshot, parts)

    def test_giant_line_and_part_ceiling_keep_unadmitted_evidence(self):
        snapshot, _, record, hunk = self.fixture(
            "@@ -0,0 +1,9 @@\n" + "+" + "x" * 200_000 + "\n" + "+small\n" * 8
        )
        probes = []
        parts = subdivide_hunk(
            record,
            hunk,
            snapshot,
            admit=lambda part: probes.append(part) or False,
            max_parts=4,
        )
        self.assertLessEqual(len(probes), 7)
        self.assertEqual(len(parts), 4)
        self.assertEqual(
            sum(part.fragment.body_end - part.fragment.body_start for part in parts),
            len(hunk.text.partition("\n")[2].encode()),
        )
        self.assertTrue(any("x" * 200_000 in part.patch for part in parts))

    def test_identity_is_repeatable_and_bound_to_source_and_current_head(self):
        snapshot, _, record, hunk = self.fixture("@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n")

        def build(head):
            return subdivide_hunk(record, hunk, head, admit=lambda part: False)

        first = build(snapshot)
        self.assertEqual(first, build(snapshot))
        changed = build(replace(snapshot, head_sha="c" * 40))
        self.assertTrue(
            set(part.evidence_id for part in first).isdisjoint(
                part.evidence_id for part in changed
            )
        )

    def test_unsplit_evidence_ids_and_fragment_authority_are_separate(self):
        snapshot, source, record, hunk = self.fixture("@@ -1 +1 @@\n-old\n+new\n")
        before = source.evidence_id
        parts = subdivide_hunk(record, hunk, snapshot, admit=lambda part: True)
        self.assertEqual(source.evidence_id, before)
        self.assertNotIn("fragment", source.to_dict())
        with self.assertRaises(ReviewInputError):
            replace(parts[0], provenance="github-files", supplied_complete=True)
        with self.assertRaises(ReviewInputError):
            EvidenceBundle(snapshot, parts).by_path()

    def test_multiple_hunks_share_allowance_and_keep_remaining_body_unsplit(self):
        snapshot, _, record, hunk = self.fixture("@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n")
        allowance = SubdivisionAllowance(remaining=2)
        first = subdivide_hunk(
            record, hunk, snapshot, admit=lambda part: False, allowance=allowance
        )
        second = subdivide_hunk(
            record, hunk, snapshot, admit=lambda part: False, allowance=allowance
        )
        self.assertEqual((len(first), len(second), allowance.remaining), (3, 1, 0))
        self.assertEqual(
            second[0].fragment.body_end, len(hunk.text.partition("\n")[2].encode())
        )

    def test_expired_deadline_stops_admission_before_any_probe_without_omission(self):
        from review_sensei.context import PromptContextDeadline

        snapshot, _, record, hunk = self.fixture("@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n")

        def expired():
            raise PromptContextDeadline("original review planning deadline exhausted")

        calls = []
        parts = subdivide_hunk(
            record,
            hunk,
            snapshot,
            admit=lambda part: calls.append(part) or False,
            before_probe=expired,
        )
        self.assertFalse(calls)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment.body_end, parts[0].fragment.body_bytes)

    def test_boundary_aware_split_prefers_top_level_def_and_falls_back_to_midpoint(
        self,
    ):
        self.assertEqual(HUNK_SUBDIVISION_POLICY, "canonical-line-segments-v2")
        lines = []
        for idx in range(1, 302):
            lines.append(f"+HELPER_{idx:03d} = {idx}\n")
        lines.append("+def _build_action_harness(flag):\n")
        for idx in range(303, 521):
            lines.append(f"+    step_{idx} = {idx}\n")
        lines.append("+    return (\n")
        lines.append("+        flag,\n")
        lines.append("+        True,\n")
        for idx in range(524, 537):
            lines.append(f"+        {idx},\n")
        lines.append("+    )\n")
        lines.append("+\n")
        lines.append("+def _preflight_and_publish():\n")
        for idx in range(540, 1001):
            lines.append(f"+    published_{idx} = {idx}\n")
        self.assertEqual(len(lines), 1000)

        patch = "@@ -0,0 +1,1000 @@\n" + "".join(lines)
        snapshot, _, record, hunk = self.fixture(patch)
        parts = subdivide_hunk(
            record,
            hunk,
            snapshot,
            admit=lambda part: (
                len(part.analysis.hunk_records[0].text.splitlines()) <= 600
            ),
        )
        self.assertEqual(len(parts), 2)
        first_body = parts[0].analysis.hunk_records[0].text.partition("\n")[2]
        second_body = parts[1].analysis.hunk_records[0].text.partition("\n")[2]
        self.assertEqual(len(first_body.splitlines()), 538)
        self.assertEqual(len(second_body.splitlines()), 462)
        self.assertTrue(second_body.startswith("+def _preflight_and_publish():\n"))
        self.assertEqual(
            (first_body + second_body).encode("utf-8"),
            hunk.text.partition("\n")[2].encode("utf-8"),
        )

        # Contiguous 1,000-line indented block with no boundary falls back to midpoint 500.
        contiguous_lines = ["+def _monolithic_block():\n"] + [
            f"+    value_{idx} = {idx}\n" for idx in range(2, 1001)
        ]
        contiguous_patch = "@@ -0,0 +1,1000 @@\n" + "".join(contiguous_lines)
        _, _, cont_record, cont_hunk = self.fixture(contiguous_patch)
        cont_parts = subdivide_hunk(
            cont_record,
            cont_hunk,
            snapshot,
            admit=lambda part: (
                len(part.analysis.hunk_records[0].text.splitlines()) <= 600
            ),
        )
        self.assertEqual(len(cont_parts), 2)
        cont_first = cont_parts[0].analysis.hunk_records[0].text.partition("\n")[2]
        cont_second = cont_parts[1].analysis.hunk_records[0].text.partition("\n")[2]
        self.assertEqual(len(cont_first.splitlines()), 500)
        self.assertEqual(len(cont_second.splitlines()), 500)
        self.assertEqual(
            (cont_first + cont_second).encode("utf-8"),
            cont_hunk.text.partition("\n")[2].encode("utf-8"),
        )


class RecordingProvider:
    name = "fixture"
    model = "fixture"

    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse('{"summary":"Reviewed.","comments":[]}', self.name)


class DiscoverySubdivisionTests(unittest.TestCase):
    def fixture(self, *, calls=8, giant=False):
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        lines = [f"+line{index}:" + "x" * 35 + "\n" for index in range(100)]
        if giant:
            lines[50] = "+" + "x" * 200_000 + "\n"
        record = EvidenceRecord(
            "src/large.py", "@@ -0,0 +1,100 @@\n" + "".join(lines), snapshot
        )
        provider = RecordingProvider()
        service = ReviewService(
            provider,
            stages=(
                Stage(
                    name="review",
                    prompt_template="<diff>{diff}</diff>",
                    outputs=("summary", "comments"),
                ),
            ),
            budget=ResourceBudget.create(max_provider_calls=calls),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=2000),
        )
        return record, provider, service

    def test_complete_large_hunk_is_exhaustively_reviewed_in_bounded_calls(self):
        record, provider, service = self.fixture()
        run = service.run(
            ReviewRequest(diff=record.diff, base_sha="a" * 40, head_sha="b" * 40)
        )
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertTrue(run.result.coverage.fully_reviewed)
        self.assertGreater(len(provider.requests), 1)
        observed = []
        for request in provider.requests:
            diff = request.prompt.split("<diff>", 1)[1].split("</diff>", 1)[0]
            self.assertLessEqual(len(diff.encode()), 2000)
            parsed = analyze_diff(diff)
            for piece in parsed.file_records:
                observed.extend(piece.added_lines)
        self.assertEqual(sorted(observed), list(range(1, 101)))
        self.assertEqual(len(set(observed)), len(observed))

    def test_call_exhaustion_and_giant_line_never_claim_hunk_completion(self):
        for options in ({"calls": 1}, {"giant": True}):
            with self.subTest(options=options):
                record, provider, service = self.fixture(**options)
                run = service.run(ReviewRequest(diff=record.diff))
                self.assertEqual(run.outcome.status, "partial")
                self.assertFalse(run.result.coverage.fully_reviewed)
                self.assertEqual(
                    run.result.coverage.hunks[0].outcome, "budget-exhausted"
                )
                self.assertLessEqual(
                    len(provider.requests), service.budget.max_provider_calls
                )
                self.assertGreaterEqual(len(provider.requests), 1)

    def test_authenticated_replay_reuses_all_segments_and_original_counters(self):
        record, provider, service = self.fixture()
        request = ReviewRequest(diff=record.diff, base_sha="a" * 40, head_sha="b" * 40)
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "private"
            store = WorkRecoveryStore(directory, key=b"k" * 32, artifacts="diagnostics")
            first = service.run(request, work_recovery=store)
            calls = len(provider.requests)
            files = list(directory.glob("*.json"))
            self.assertEqual(len(files), 1)
            document = json.loads(files[0].read_text())["document"]
            self.assertTrue(
                all(item["fragment"] for item in document["plan"]["records"])
            )
            second = service.run(
                request,
                work_recovery=WorkRecoveryStore(
                    directory, key=b"k" * 32, artifacts="diagnostics"
                ),
            )
            self.assertEqual(second.result, first.result)
            self.assertEqual(len(provider.requests), calls)
            self.assertEqual(
                second.outcome.provider_calls, first.outcome.provider_calls
            )
            self.assertEqual(second.outcome.prompt_bytes, first.outcome.prompt_bytes)

    def test_previous_work_policy_is_rejected_before_cache_or_provider_work(self):
        record, provider, service = self.fixture()
        with patch(
            "review_sensei.hunk_subdivision.HUNK_SUBDIVISION_POLICY", "previous-policy"
        ):
            previous = service.work_policy_digest
        self.assertNotEqual(previous, service.work_policy_digest)
        with self.assertRaisesRegex(ReviewInputError, "work policy identity"):
            service.run(ReviewRequest(diff=record.diff, work_policy_digest=previous))
        self.assertFalse(provider.requests)

    def test_all_stages_use_one_subdivision_allowance(self):
        record, provider, service = self.fixture()
        stages = (service.stages[0], replace(service.stages[0], name="second"))
        service = ReviewService(
            provider,
            stages=stages,
            budget=service.budget,
            work_budgets=service.work_budgets,
        )
        allowance = SubdivisionAllowance(remaining=2)
        with patch(
            "review_sensei.discovery_work.SubdivisionAllowance", return_value=allowance
        ) as construct:
            run = service.run(ReviewRequest(diff=record.diff))
        construct.assert_called_once_with()
        self.assertEqual(allowance.remaining, 0)
        self.assertEqual(run.outcome.status, "partial")
        self.assertFalse(run.result.coverage.fully_reviewed)
        self.assertLessEqual(len(provider.requests), service.budget.max_provider_calls)

    def test_optional_document_omission_stays_recorded_after_all_segments_are_reviewed(
        self,
    ):
        from review_sensei.stages import ReviewCategory

        record, provider, service = self.fixture()
        category = ReviewCategory(
            "documents", "Document requirements", ("correctness",)
        )
        stage = replace(
            service.stages[0],
            prompt_template="<diff>{diff}</diff>\n{review_context}\n{review_categories}",
            categories=(category,),
        )
        service = ReviewService(
            provider,
            stages=(stage,),
            budget=service.budget,
            work_budgets=service.work_budgets,
        )
        document = ReviewDocument(
            "docs/selected.md",
            "requirements",
            hashlib.sha256(b"requirements").hexdigest(),
        )
        context = ReviewLensContext(
            "documents",
            documents=(document,),
            document_selection=DocumentSelectionReport(
                (
                    DocumentDecision("docs/omitted.md", "omitted", "file-budget"),
                    DocumentDecision(
                        document.path,
                        "selected",
                        "configured-source",
                        document.sha256,
                        document.sha256,
                        len(document.content.encode()),
                    ),
                )
            ),
        )
        run = service.run(ReviewRequest(diff=record.diff, lens_contexts=(context,)))
        self.assertTrue(run.result.coverage.fully_reviewed)
        self.assertEqual(run.result.review_status, "complete")
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertIsNone(run.outcome.diagnostic)
        self.assertEqual(run.context_admission["original_inventories"][0]["omitted"], 1)
        self.assertFalse(run.context_admission["batches"][0]["complete"])

    def test_pr488_shaped_multifile_and_split_test_injects_scope_and_labels_summary(
        self,
    ):
        prod_paths = (
            "packages/python/review-sensei-github/src/review_sensei_github/action_receipt.py",
            "packages/python/review-sensei-github/src/review_sensei_github/comment_pages.py",
            "packages/python/review-sensei-github/src/review_sensei_github/fence.py",
            "packages/python/review-sensei-github/src/review_sensei_github/page_planner.py",
            "src/review_sensei/page_budgets.py",
        )
        prod_diffs = []
        for idx, path in enumerate(prod_paths):
            body_lines = "".join(
                f"+prod_{idx}_line_{line_no} = 'padding_{'x' * 34}_{line_no}'\n"
                for line_no in range(1, 79)
            )
            prod_diffs.append(
                f"diff --git a/{path} b/{path}\n"
                "--- /dev/null\n"
                f"+++ b/{path}\n"
                "@@ -0,0 +1,78 @@\n"
                f"{body_lines}"
            )
        prod_total_bytes = sum(len(item.encode("utf-8")) for item in prod_diffs)
        self.assertGreater(prod_total_bytes, 24_000)
        self.assertLess(prod_total_bytes, 29_000)

        test_path = "tests/test_byte_aware_comment_pages.py"
        test_lines = []
        for line_no in range(1, 302):
            test_lines.append(
                f"+CONST_{line_no:03d} = 'setup_fixture_{'a' * 16}_{line_no}'\n"
            )
        test_lines.append("+def _build_action_harness():\n")
        for line_no in range(303, 520):
            test_lines.append(
                f"+    harness_{line_no} = 'entry_{'b' * 16}_{line_no}'\n"
            )
        test_lines.append("+    return (\n")
        for line_no in range(521, 537):
            test_lines.append(
                f"+        ('arg_{line_no}', {'True' if line_no % 2 else 'False'}),\n"
            )
        test_lines.append("+    )\n")
        test_lines.append("+\n")
        test_lines.append("+def _preflight_and_publish():\n")
        for line_no in range(540, 1043):
            test_lines.append(
                f"+    assert_page_{line_no} = 'check_{'c' * 16}_{line_no}'\n"
            )
        self.assertEqual(len(test_lines), 1042)
        test_diff = (
            f"diff --git a/{test_path} b/{test_path}\n"
            "--- /dev/null\n"
            f"+++ b/{test_path}\n"
            "@@ -0,0 +1,1042 @@\n" + "".join(test_lines)
        )
        self.assertGreater(len(test_diff.encode("utf-8")), 41_000)
        full_diff = "".join(prod_diffs) + test_diff

        class ScopedProvider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    summary = "Reviewed 5 production modules."
                elif len(self.requests) == 2:
                    summary = "Reviewed _build_action_harness segment."
                else:
                    summary = "Reviewed _preflight_and_publish segment."
                return ProviderResponse(
                    json.dumps({"summary": summary, "comments": []}), self.name
                )

        provider = ScopedProvider()
        service = ReviewService(
            provider,
            budget=ResourceBudget.create(max_provider_calls=8),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        run = service.run(
            ReviewRequest(diff=full_diff, base_sha="a" * 40, head_sha="b" * 40)
        )
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertIsNotNone(run.result)
        self.assertTrue(run.result.coverage.fully_reviewed)
        self.assertEqual(len(run.result.coverage.files), 6)
        self.assertEqual(len(provider.requests), 3)
        for req in provider.requests:
            self.assertLessEqual(
                len(
                    (req.prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION).encode("utf-8")
                ),
                LEGACY_ASSESSMENT_PROMPT_BYTES,
            )

        batch1_scope = (
            provider.requests[0]
            .prompt.split("<review-batch-scope>", 1)[1]
            .split("</review-batch-scope>", 1)[0]
        )
        self.assertIn(test_path, batch1_scope)
        for path in prod_paths:
            self.assertIn(path, provider.requests[0].prompt)

        for req, expected_range in zip(
            provider.requests[1:],
            ("lines +1..+538", "lines +539..+1042"),
            strict=True,
        ):
            scope = req.prompt.split("<review-batch-scope>", 1)[1].split(
                "</review-batch-scope>", 1
            )[0]
            for path in prod_paths:
                self.assertIn(path, scope)
            self.assertIn(f"{test_path} hunk #6 {expected_range}", scope)
            self.assertIn("bytes ", scope)
            self.assertIn("Lossless canonical hunk segment", scope)
            self.assertIn("NOT truncated or missing from the PR", scope)
            self.assertIn("Do NOT report absent sibling files", scope)

        self.assertIn("+def _preflight_and_publish():", provider.requests[2].prompt)
        self.assertIn("**Batch 1/3 (", run.result.summary)
        self.assertIn(
            f"**Batch 2/3 ({test_path} lines 1-538):** Reviewed _build_action_harness segment.",
            run.result.summary,
        )
        self.assertIn(
            f"**Batch 3/3 ({test_path} lines 539-1042):** Reviewed _preflight_and_publish segment.",
            run.result.summary,
        )


if __name__ == "__main__":
    unittest.main()
