"""Manifest framing and conflicting owned sources never reveal a clean subset."""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.native_manifest import (
    MANIFEST_WIRE_BOUND,
    manifest_body,
    read_manifest_body,
)
from review_sensei_github.native_union import NativeUnionReceipt
from review_sensei_github.packed_comment_census import wire
from test_native_group_publication import GroupWriterBackend

from review_sensei.errors import ReviewInputError


class NativeManifestTests(unittest.TestCase):
    def fixture(self):
        backend = GroupWriterBackend()
        state = backend.host.publish_parts()
        receipt = NativeUnionReceipt.from_staging(backend.plan, state, producer_id=999)
        store = backend.host.manifest_store
        store.create(receipt)
        return backend, receipt, store

    def test_complete_round_trip_is_original_source_bound_and_small(self):
        backend, receipt, store = self.fixture()
        body = backend.manifest_comment["body"]
        self.assertEqual(
            read_manifest_body(body, source_sha256=store.source_sha256), receipt
        )
        self.assertLessEqual(wire({"body": body}), MANIFEST_WIRE_BOUND)
        found = store.find(receipt)
        self.assertEqual(found.native_id, "issue:1234000")
        for changed in (body + " ", body[:-1], body.replace("immutable", "other", 1)):
            with (
                self.subTest(changed=changed[:80]),
                self.assertRaises(ReviewInputError),
            ):
                read_manifest_body(changed, source_sha256=store.source_sha256)
        with self.assertRaises(ReviewInputError):
            read_manifest_body(body, source_sha256="b" * 64)

    def test_duplicate_conflicting_and_malformed_owned_frames_withhold(self):
        for kind in ("duplicate", "source", "parts", "malformed"):
            backend, receipt, store = self.fixture()
            extra = copy.deepcopy(backend.manifest_comment)
            extra["id"] += 1
            if kind == "source":
                extra["body"] = manifest_body(receipt, "b" * 64)
            elif kind == "parts":
                altered = replace(
                    receipt, parts=(replace(receipt.parts[0], native_id="42"),)
                )
                extra["body"] = manifest_body(altered, store.source_sha256)
            elif kind == "malformed":
                extra["body"] = extra["body"][:-2]
            backend.extra_manifest_comments.append(extra)
            with (
                self.subTest(kind=kind),
                self.assertRaises((ReviewInputError, GitHubPublicationTransientError)),
            ):
                store.find(receipt)
            self.assertEqual(backend.manifest_posts, 1)

    def test_wrong_receipt_scope_refuses_before_another_native_effect(self):
        backend, receipt, store = self.fixture()
        for altered in (
            replace(receipt, repository_id=2),
            replace(receipt, pull_request=8),
            replace(receipt, producer_id=777),
        ):
            with self.subTest(altered=altered), self.assertRaises(ReviewInputError):
                store.create(altered)
        self.assertEqual(backend.manifest_posts, 1)
