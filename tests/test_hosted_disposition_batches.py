"""Assigned-ID preparation under unchanged bounds; production remains v1.

The bounded test ledger below supplies one-page fixture tail scope only. It is
not a broker receipt or proof of the real caller's complete command lifecycle.
The actual ApiRun override separately requires its issued checkpoint snapshot.
"""

import copy
import json
import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_review_transaction as fixture
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    _disposition_index_bounds,
    _immutable_part_body,
)
from test_authenticated_partitions import GitHubState, binding
from test_paged_disposition_snapshots import decision

from review_sensei.bounded_evidence import (
    EvidenceReadBudget,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
)
from review_sensei.disposition_snapshot import stage_paged_snapshot
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import read_session_dispositions


class BoundedFixtureLedger(GitHubIssueCommentSessionLedger):
    @property
    def supports_paged_disposition_snapshot_writes(self):
        return True


def seeded():
    state = GitHubState()
    seed = state.ledger()
    seed.initialize(fixture.IDENTITY, now=fixture.NOW)
    transaction = ReviewTransaction.create(
        repository=fixture.IDENTITY.repository,
        pull_request=fixture.IDENTITY.pull_request,
        base_sha=fixture.BASE_SHA,
        head_sha=fixture.HEAD_SHA,
        policy_digest=fixture.POLICY.digest(),
        configuration_digest=fixture.CONFIGURATION_DIGEST,
        evidence_digest=fixture.EVIDENCE_DIGEST,
        reservation_id="f" * 64,
        generation=1,
    ).with_result("d" * 64)
    record = seed.replace(
        fixture.IDENTITY,
        lambda current: current.evolve(
            generation=1,
            transaction=transaction,
            last_committed_reservation_id=transaction.reservation_id,
            now=fixture.NOW,
        ),
        now=fixture.NOW,
    )
    ledger = BoundedFixtureLedger(
        state.http,
        token="synthetic",
        app_slug="sensei[bot]",
        evidence_budget=EvidenceReadBudget(),
        enable_partition_writes=True,
        enable_disposition_snapshot_writes=True,
    )
    return state, ledger, record


def stage(ledger, count, *, allowance=2048):
    values = tuple({**decision(i), "head_sha": fixture.HEAD_SHA} for i in range(count))

    def mutation(record):
        # Only the fixture's bounded preparation scope is selected here.
        # The one-page backend and complete real batch/readback remain active.
        ledger._tail_preparing, ledger._tail_scan_pages = True, 1
        try:
            manifest = stage_paged_snapshot(
                ledger,
                record,
                values,
                binding=binding(
                    producer=ledger.evidence_producer(),
                    generation=record.generation + 1,
                    purpose="dispositions",
                ),
                identity=fixture.IDENTITY,
                max_manifest_bytes=allowance,
            )
        finally:
            ledger._tail_preparing, ledger._tail_scan_pages = False, None
        return record.evolve(
            generation=record.generation + 1,
            dispositions=(),
            disposition_snapshot={
                "schema_version": "2.0",
                "result_digest": "d" * 64,
                "state_manifest": manifest,
            },
            now=fixture.NOW,
        )

    return ledger.replace(fixture.IDENTITY, mutation, now=fixture.NOW), values


def index_document(count):
    context = binding(producer="github-bot:55", generation=2, purpose="dispositions")
    values = tuple({**decision(i), "head_sha": fixture.HEAD_SHA} for i in range(count))
    pages = []
    for offset in range(0, count - count % 8, 8):
        page = {
            "schema_version": "disposition-page-v1",
            "result_digest": "d" * 64,
            "offset": offset,
            "dispositions": list(values[offset : offset + 8]),
        }
        parts = partition_evidence(page, binding=context, item_count=8)
        pages.append(
            evidence_manifest(
                page,
                binding=context,
                item_count=8,
                parts=parts,
                storage_ids=tuple(str(10**19 - 1 - i) for i in range(len(parts))),
            )
        )
    return context, {
        "schema_version": "disposition-pages-v1",
        "result_digest": "d" * 64,
        "pages": pages,
        "tail": list(values[len(pages) * 8 :]),
    }


class AssignedIdBoundTests(unittest.TestCase):
    def test_random_assigned_ids_and_index_compression_never_exceed_prepared_bounds(
        self,
    ):
        randomizer = random.Random(818)
        for count in (33, 64, 250):
            context, maximum = index_document(count)
            bounds = _disposition_index_bounds(
                maximum, binding=context, item_count=count, max_manifest_bytes=2048
            )
            self.assertFalse(bounds.inline)
            for _ in range(30):
                actual = copy.deepcopy(maximum)
                for page in actual["pages"]:
                    for ref in page["parts"]:
                        ref["storage_id"] = str(randomizer.randrange(1, 10**19))
                self.assertLessEqual(len(canonical_bytes(actual)), bounds.decoded_bytes)
                parts = partition_evidence(actual, binding=context, item_count=count)
                self.assertLessEqual(len(parts), len(bounds.part_bytes))
                for part, raw, body, wire in zip(
                    parts, bounds.part_bytes, bounds.body_bytes, bounds.wire_bytes
                ):
                    self.assertLessEqual(len(canonical_bytes(part)), raw)
                    self.assertLessEqual(len(_immutable_part_body(part).encode()), body)
                    self.assertLessEqual(
                        len(canonical_bytes({"body": _immutable_part_body(part)})), wire
                    )
                manifest = evidence_manifest(
                    actual,
                    binding=context,
                    item_count=count,
                    parts=parts,
                    storage_ids=tuple(str(10**19 - 1 - i) for i in range(len(parts))),
                )
                self.assertLessEqual(
                    len(canonical_bytes(manifest)), bounds.manifest_bytes
                )
        self.assertEqual(len(bounds.part_bytes), 2)

    def test_inline_boundary_and_escape_heavy_tail_use_raw_length(self):
        context, document = index_document(33)
        document["tail"][0]["reason"] = '"\\🙂' * 100
        inline = inline_manifest(document, binding=context, item_count=33)
        exact = len(canonical_bytes(inline))
        self.assertTrue(
            _disposition_index_bounds(
                document, binding=context, item_count=33, max_manifest_bytes=exact
            ).inline
        )
        self.assertFalse(
            _disposition_index_bounds(
                document, binding=context, item_count=33, max_manifest_bytes=exact - 1
            ).inline
        )
        with self.assertRaisesRegex(ReviewInputError, "root allocation"):
            _disposition_index_bounds(
                document, binding=context, item_count=33, max_manifest_bytes=100
            )


class HostedBatchAdapterTests(unittest.TestCase):
    def test_real_adapter_33_and_64_assigned_ids_roundtrip_in_bounded_fixture(self):
        for count in (33, 64):
            with self.subTest(count=count):
                state, ledger, _ = seeded()
                before = copy.deepcopy(state.comments)
                written, values = stage(ledger, count)
                fresh = state.ledger()
                loaded = fresh.load(fixture.IDENTITY, now=fixture.NOW)
                self.assertEqual(loaded.record, written)
                self.assertEqual(read_session_dispositions(fresh, written), values)
                self.assertLessEqual(ledger.evidence_budget.calls, 60)
                for key, value in before.items():
                    if "evidence-part:v1" in value["body"]:
                        self.assertEqual(state.comments[key], value)
                manifest = written.disposition_snapshot["state_manifest"]
                self.assertTrue(
                    all(
                        ref["storage_id"].isdigit() for ref in manifest.get("parts", [])
                    )
                )
                self.assertNotIn("x000000000000000000", json.dumps(written.to_dict()))

    def test_aggregate_refusals_leave_every_remote_comment_unchanged(self):
        for reason in ("manifest", "count", "bytes", "funding"):
            with self.subTest(reason=reason):
                state, ledger, _ = seeded()
                before = copy.deepcopy(state.comments)
                ledger.evidence_budget.calls = 50 if reason == "funding" else 0
                target = (
                    ("MAX_STORED_PARTS", 3)
                    if reason == "count"
                    else ("MAX_STORED_PART_BYTES", 1)
                    if reason == "bytes"
                    else ("MAX_STORED_PARTS", 256)
                )
                with patch(
                    "review_sensei_github.session_ledger." + target[0], target[1]
                ):
                    with self.assertRaises(ReviewInputError):
                        stage(
                            ledger, 33, allowance=100 if reason == "manifest" else 2048
                        )
                self.assertEqual(state.comments, before)

    def test_command_receipt_promise_cannot_be_borrowed_by_snapshot(self):
        for count in (33, 64):
            with self.subTest(count=count):
                state, ledger, _ = seeded()
                before = copy.deepcopy(state.comments)
                ledger._quarantine_command_receipt_marker = "synthetic current command"
                guard = SimpleNamespace(
                    comment_bounds=[32768, 65536], command_receipt_update_id=None
                )
                with patch(
                    "review_sensei_github.publication_recovery._publication_transport",
                    return_value=SimpleNamespace(_quarantine_capacity_guard=guard),
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "existing comment promises"
                    ):
                        stage(ledger, count)
                self.assertEqual(guard.comment_bounds, [32768, 65536])
                self.assertEqual(state.comments, before)

    def test_lost_post_response_retains_created_part_without_root_activation(self):
        state, ledger, record = seeded()
        original = state.open
        lost = [False]

        def opener(request, timeout):
            response = original(request, timeout)
            if (
                request.method == "POST"
                and b"evidence-part:v1" in (request.data or b"")
                and not lost[0]
            ):
                lost[0] = True
                from fake_github_http import json_response

                return json_response({}, 503)
            return response

        with patch.object(state.http, "opener", side_effect=opener):
            with self.assertRaises(GitHubPublicationTransientError):
                stage(ledger, 33)
        self.assertTrue(lost[0])
        parts = [
            row for row in state.comments.values() if "evidence-part:v1" in row["body"]
        ]
        self.assertEqual(len(parts), 1)
        self.assertGreater(ledger.evidence_budget.calls, 0)
        self.assertEqual(
            state.ledger().load(fixture.IDENTITY, now=fixture.NOW).record, record
        )

    def test_unissued_run_owned_scope_refuses_before_dispatch(self):
        ledger = object.__new__(ApiRunSessionLedger)
        ledger._part_staging_identity = fixture.IDENTITY
        ledger._quarantine_checkpoint_snapshot = None
        with self.assertRaisesRegex(ReviewInputError, "exact checkpoint scope"):
            ledger._preflight_disposition_snapshot_batch(
                fixture.IDENTITY, None, missing_parts=5, read_parts=5, retained_parts=0
            )
