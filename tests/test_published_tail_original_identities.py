"""Real original producers retain criteria, not prose-count display guesses."""

import copy
import dis
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.finding_lifecycle import _unpack_journal
from review_sensei_github.publication_recovery import retain_checkpoint_review_result
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.bounded_evidence import (
    read_inline_evidence,
    read_partitioned_evidence,
)
from review_sensei.finding_lifecycle import decode_lifecycle_state
from tests.test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class ProseOnlyOrigins(PublishedTailJourneyFixture):
    stable_assessment_identities = False


class MovedExplicitOrigins(PublishedTailJourneyFixture):
    # Preserve the separate five-body-update scenario; it currently holds at
    # the original native-batch funding gate rather than silently raising512.
    unchanged_original_overlap = False


class PublishedTailOriginalIdentityTests(unittest.TestCase):
    def produce(self, fixture_type):
        snapshots, retained, carriers = [], {}, {}
        self.native_writes = []
        self.accounting_receipts = []
        invoke_code = PublishedTailJourneyFixture.invoke.__code__
        result_code = retain_checkpoint_review_result.__code__
        partition_code = read_partitioned_evidence.__code__
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = fixture_type(directory)

            def observe(frame, event, value):
                if frame.f_code not in (result_code, partition_code, invoke_code):
                    return
                if event != "return" or not dis.opname[
                    frame.f_code.co_code[frame.f_lasti]
                ].startswith("RETURN"):
                    return
                if frame.f_code is result_code and value is not None:
                    retained[fixture.run_id] = value
                if frame.f_code is partition_code and isinstance(value, dict):
                    # Actual authenticated native part readbacks already paid
                    # by the producer; inspection adds no reader or allowance.
                    carriers[frame.f_locals["manifest"]["sha256"]] = copy.deepcopy(
                        value
                    )
                if frame.f_code is not invoke_code:
                    return
                record = parse_session_comment(
                    fixture.comment["body"], identity=fixture.selected.identity
                )
                manifest = record.finding_lifecycle["manifest"]
                if manifest["encoding"] == "inline-json-v1":
                    carrier = read_inline_evidence(
                        manifest, expected_binding=manifest["binding"]
                    )
                else:
                    self.assertIn(manifest["sha256"], carriers)
                    carrier = carriers[manifest["sha256"]]
                document = _unpack_journal(carrier["lifecycle"])
                state = decode_lifecycle_state(document["lifecycle"]).state
                snapshots.append(
                    (
                        record,
                        state,
                        retained[fixture.run_id],
                        fixture.provider_calls,
                        value,
                    )
                )
                self.native_writes.append(
                    [
                        (method, path)
                        for attempt, method, path in fixture.trace
                        if attempt == fixture.attempt
                        and method in ("POST", "PUT", "PATCH")
                        and ("/pulls/" in path or "/check-runs" in path)
                    ]
                )
                state = fixture.accounted.accounting
                self.assertEqual(state.configuration.limit, 512)
                self.accounting_receipts.append(
                    {
                        "limit": state.configuration.limit,
                        "confirmed": state.confirmed,
                        "uncertain": state.uncertain,
                        "remaining": state.remaining,
                        "reserved_undispatched": state.reserved_undispatched,
                        "physical_attempts": sum(
                            a == fixture.attempt for a, _, _ in fixture.trace
                        ),
                        "durable": copy.deepcopy(record.api_run_accounting),
                    }
                )

            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                fixture.original_origins(failed_count=9, published_count=5)
            finally:
                sys.setprofile(previous)
            self.assertEqual(fixture.provider_calls, 2)
            self.assertEqual(len(snapshots), 2)
            self.assertEqual([s[3:] for s in snapshots], [(1, 0), (2, 130)])
            self.assertEqual([len(s[2].comments) for s in snapshots], [5, 9])
            self.assertEqual(
                [c.body for c in snapshots[1][2].comments],
                [
                    (
                        f"Earlier published assessment {n} requires independent human judgment."
                        if fixture.stable_assessment_identities
                        and fixture.unchanged_original_overlap
                        and n < 5
                        else f"Current published assessment {n} requires independent human judgment."
                    )
                    for n in range(9)
                ],
            )
            return snapshots

    def test_explicit_criteria_preserve_five_plus_nine_original_assessments(self):
        first, second = self.produce(PublishedTailJourneyFixture)
        self.assertEqual(len(self.native_writes[0]), 3)
        writes = self.native_writes[1]
        self.assertEqual(len(writes), 8)
        self.assertEqual([m for m, _ in writes], ["POST"] + ["PATCH"] * 6 + ["POST"])
        self.assertEqual(
            {p for _, p in writes[2:-1]},
            {f"/repos/owner/repo/pulls/comments/{i}" for i in range(200, 205)},
        )
        self.assertEqual(writes[-1], ("POST", "/repos/owner/repo/pulls/7/reviews"))
        print(
            "ORIGINAL_512_ACCOUNTING",
            json.dumps(self.accounting_receipts, sort_keys=True),
            flush=True,
        )
        old = first[1].inventory.obligations
        new = second[1].inventory.obligations
        self.assertEqual((len(old), len(new)), (5, 9))
        old_ids = {o.criterion.obligation_id for o in old}
        new_ids = {o.criterion.obligation_id for o in new}
        self.assertLess(old_ids, new_ids)
        self.assertEqual(len(new_ids - old_ids), 4)
        self.assertEqual(len(old) + len(new), 14)
        self.assertEqual(
            [c.evidence_id for c in first[2].comments],
            [f"published-tail-assessment-{n}" for n in range(5)],
        )
        self.assertEqual(
            [c.evidence_id for c in second[2].comments],
            [f"published-tail-assessment-{n}" for n in range(9)],
        )
        for prior in old:
            carried = next(
                o
                for o in new
                if o.criterion.obligation_id == prior.criterion.obligation_id
            )
            self.assertEqual(
                carried.criterion.resolution_criterion,
                prior.criterion.resolution_criterion,
            )
            self.assertEqual(
                carried.criterion.origin_head_sha, prior.criterion.origin_head_sha
            )
            self.assertNotEqual(carried.lifecycle_state, "ambiguous-multi-target")

    def test_prose_only_overlap_remains_ambiguous_without_dropping_raw_nine(self):
        first, second = self.produce(ProseOnlyOrigins)
        old = first[1].inventory.obligations
        new = second[1].inventory.obligations
        self.assertEqual((len(old), len(new)), (5, 5))
        self.assertEqual(
            {o.criterion.obligation_id for o in old},
            {o.criterion.obligation_id for o in new},
        )
        self.assertEqual({o.lifecycle_state for o in old}, {"active-unresolved"})
        self.assertEqual({o.lifecycle_state for o in new}, {"ambiguous-multi-target"})
        self.assertEqual({o.present_anchor.drift_kind for o in new}, {"duplicate_code"})
        self.assertTrue(all(c.evidence_id is None for c in second[2].comments))
        self.assertIn("ambiguous-drift-target", second[1].withheld_reasons)
