"""Publication-scoped occupancy uses original native evidence, never a census retry."""

import copy
import unittest
from dataclasses import replace

import test_native_union as union_fixture
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import finding_index_marker
from review_sensei_github.publication_occupancy import (
    native_publication_occupancy,
    require_publication_occupancy,
)
from test_native_review_groups import maximum_mixture

from review_sensei.errors import ReviewInputError


def fixture(*, resolved=False, body_placed=False, long_ids=False):
    backend, _, _, _, _ = union_fixture.NativeUnionTests().fixture()
    _, assembled = maximum_mixture()
    entries = assembled.index_entries
    if body_placed:
        entries = (replace(entries[0], body_placed=True), *entries[1:])
    backend.main_body += "\n\n" + finding_index_marker(
        repository_id=backend.plan.repository_id,
        pull_request=backend.plan.pull_request,
        head_sha=backend.plan.head_sha,
        base_sha=backend.plan.base_sha,
        result_digest=backend.plan.result_sha256,
        entries=entries,
        complete_inventory=True,
    )
    if long_ids:
        backend.thread_ids = {
            int(row["x"]["nodes"][0]["n"]): (
                "opaque-" + row["x"]["nodes"][0]["n"]
            ).ljust(2048, "X")
            for row in backend.roots
            if int(row["x"]["nodes"][0]["n"]) not in backend.old_nodes
        }
    if resolved:
        backend.roots[0]["s"] = True

        def close_old(query, payload):
            for thread in payload.get("data", {}).get("old", []):
                if thread["i"] == backend.old_nodes[1_000_000]:
                    thread["s"] = True

        backend.change = close_old
    return backend, backend.read()


class NativePublicationOccupancyTests(unittest.TestCase):
    def test_full_opaque_target_is_bound_without_expanding_numeric_slot_identity(self):
        backend, evidence = fixture(long_ids=True)
        before = len(backend.calls)
        occupancy = native_publication_occupancy(
            backend.http, token="synthetic", evidence=evidence, producer_id=999
        )
        targets = [
            target
            for target in occupancy.targets
            if target.comment_id not in backend.old_nodes
        ]
        self.assertTrue(targets)
        self.assertTrue(all(len(target.thread_id) == 2048 for target in targets))
        self.assertTrue(all(len(target.slot.target) < 256 for target in targets))
        self.assertEqual(len(backend.calls), before)

    def test_legacy_or_missing_current_index_is_not_a_complete_occupancy_claim(self):
        backend, evidence, _, _, _ = union_fixture.NativeUnionTests().fixture()
        before = len(backend.calls)
        with self.assertRaisesRegex(ReviewInputError, "inline inventory"):
            native_publication_occupancy(
                backend.http, token="synthetic", evidence=evidence, producer_id=999
            )
        self.assertEqual(len(backend.calls), before)

    def test_complete_target_projection_uses_no_new_http_and_keeps_opaque_ids(self):
        backend, evidence = fixture()
        before = len(backend.calls)
        occupancy = native_publication_occupancy(
            backend.http, token="synthetic", evidence=evidence, producer_id=999
        )
        self.assertEqual(len(backend.calls), before)
        self.assertEqual(len(occupancy.slots), len(evidence.roots))
        self.assertEqual(
            {item.comment_id: item.thread_id for item in occupancy.targets},
            dict(evidence.thread_ids),
        )
        self.assertTrue(all(slot.reserved for slot in occupancy.slots))
        require_publication_occupancy(
            occupancy,
            backend.http,
            token="synthetic",
            evidence=evidence,
            producer_id=999,
        )
        self.assertEqual(len(backend.calls), before)

    def test_resolved_ui_state_cannot_release_an_unsettled_target(self):
        backend, evidence = fixture(resolved=True)
        occupancy = native_publication_occupancy(
            backend.http, token="synthetic", evidence=evidence, producer_id=999
        )
        resolved = [target for target in occupancy.targets if target.resolved]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].slot.state, "uncertain")
        self.assertTrue(resolved[0].slot.reserved)

    def test_foreign_transport_token_producer_or_reconstructed_proof_refuses(self):
        backend, evidence = fixture()
        before = len(backend.calls)
        for http, token, proof, producer in (
            (GitHubHttp(), "synthetic", evidence, 999),
            (backend.http, "foreign", evidence, 999),
            (backend.http, "synthetic", copy.deepcopy(evidence), 999),
            (backend.http, "synthetic", evidence, 1000),
            (backend.http, "synthetic", evidence, True),
        ):
            with self.subTest(producer=producer), self.assertRaises(ReviewInputError):
                native_publication_occupancy(
                    http, token=token, evidence=proof, producer_id=producer
                )
        self.assertEqual(len(backend.calls), before)

    def test_body_placed_inventory_is_not_silently_counted_as_thread_occupancy(self):
        backend, evidence = fixture(body_placed=True)
        with self.assertRaisesRegex(ReviewInputError, "inline inventory"):
            native_publication_occupancy(
                backend.http, token="synthetic", evidence=evidence, producer_id=999
            )

    def test_changed_target_or_subset_cannot_retain_source_authority(self):
        backend, evidence = fixture()
        original = native_publication_occupancy(
            backend.http, token="synthetic", evidence=evidence, producer_id=999
        )
        changed = replace(original.targets[0], thread_id="foreign-thread")
        for snapshot in (
            replace(original, targets=original.targets[1:]),
            replace(original, targets=(changed, *original.targets[1:])),
            replace(original, source_sha256="f" * 64),
        ):
            with self.assertRaisesRegex(ReviewInputError, "occupancy source"):
                require_publication_occupancy(
                    snapshot,
                    backend.http,
                    token="synthetic",
                    evidence=evidence,
                    producer_id=999,
                )

    def test_mutated_original_evidence_refuses_without_a_new_read(self):
        backend, evidence = fixture()
        before = len(backend.calls)
        evidence.root_bodies[0]["b"] += " changed"
        with self.assertRaisesRegex(ReviewInputError, "reconstructed or changed"):
            native_publication_occupancy(
                backend.http, token="synthetic", evidence=evidence, producer_id=999
            )
        self.assertEqual(len(backend.calls), before)
