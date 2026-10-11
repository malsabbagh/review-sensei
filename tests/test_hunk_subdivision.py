"""Exact, bounded discovery segments never become complete file evidence."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.diff import analyze_diff
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot
from review_sensei.hunk_subdivision import SubdivisionAllowance, subdivide_hunk
from review_sensei.models import ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget
from review_sensei.service import ReviewService
from review_sensei.stages import Stage
from review_sensei.work_recovery import WorkRecoveryStore


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

    def test_document_omission_remains_partial_after_all_segments_are_reviewed(self):
        from review_sensei.stages import ReviewCategory
        from tests.test_prompt_context_admission import lens

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
        run = service.run(
            ReviewRequest(
                diff=record.diff, lens_contexts=(lens(size=10, count=1, omitted=1),)
            )
        )
        self.assertTrue(run.result.coverage.fully_reviewed)
        self.assertEqual(run.outcome.status, "partial")
        self.assertEqual(run.context_admission["original_inventories"][0]["omitted"], 1)


if __name__ == "__main__":
    unittest.main()
