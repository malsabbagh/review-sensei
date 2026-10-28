"""Compact physical descriptors keep original source and unknown writes intact."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.accepted_result import selected_accepted_result
from review_sensei_github.checkpoint_result_store import (
    CheckpointResultStore,
    uses_literal_publication,
)
from review_sensei_github.operation_host import (
    OperationComment,
    OperationWriteUnconfirmed,
    comment_payload,
)
from review_sensei_github.publication_recovery import _event_key, _publication_transport
from review_sensei_github.session_ledger import parse_session_comment
from test_accepted_result import FreshNativeComments
from test_review_run import EntryFixture

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult


class IsolatedCheckpointPort:
    """Synthetic native storage; the public operation source remains unchanged."""

    def __init__(self, fixture, checkpoint_key, *, fault=None):
        self.rows = []
        for row in FreshNativeComments(fixture).list_comments():
            if "<!-- review-sensei-result -->" in row.body:
                if (
                    json.loads(comment_payload(row.body)).get("event_key")
                    == checkpoint_key
                ):
                    continue
            self.rows.append(row)
        self.requests, self.creates, self.fault = 0, 0, fault

    def list_comments(self):
        self.requests += 1
        return tuple(self.rows)

    def create(self, body):
        self.creates += 1
        row = OperationComment(8_000_000 + self.creates, body, 999, "Bot")
        if self.fault != "absent":
            self.rows.append(row)
        if self.fault is not None:
            raise OperationWriteUnconfirmed(
                "synthetic descriptor acknowledgement unknown"
            )
        return row

    def update(self, comment_id, body):
        raise AssertionError("checkpoint descriptor is immutable")


class CheckpointResultStoreTests(unittest.TestCase):
    def test_roundtrip_unknown_ack_and_exact_original_carrier_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=512)
            self.assertEqual(f.invoke(), 0, f.error)
            tx = parse_session_comment(
                f.comment["body"], identity=f.selected.identity
            ).transaction
            captured = selected_accepted_result(f.selected.http, tx, app_id=999)
            self.assertEqual(captured.native_format, "legacy")
            pending = replace(tx, phase="publication_pending")
            bound = replace(
                ReviewResult.from_dict(json.loads(captured.payload)),
                transaction=pending,
            )
            canonical = canonical_bytes(bound.to_dict())
            key = _event_key(f.selected.identity, tx)
            physical = _publication_transport(f.selected.http)
            self.assertFalse(uses_literal_publication(f.selected.http, bound))
            legacy_port = IsolatedCheckpointPort(f, key)
            legacy_store = CheckpointResultStore(
                legacy_port,
                app_user_id=999,
                http=f.selected.http,
                identity=f.selected.identity,
                transaction=pending,
                app_id=999,
                compact_write=True,
            )
            with (
                patch.object(physical, "_quarantine_capacity_guard", object()),
                self.assertRaisesRegex(ReviewInputError, "original producer format"),
            ):
                legacy_store.write(
                    event_key=key, result=canonical, _check_existing=False
                )
            self.assertEqual(legacy_port.creates, 0)
            for fault in (None, "stored", "absent"):
                with self.subTest(fault=fault):
                    port = IsolatedCheckpointPort(f, key, fault=fault)
                    store = CheckpointResultStore(
                        port,
                        app_user_id=999,
                        http=f.selected.http,
                        identity=f.selected.identity,
                        transaction=pending,
                        app_id=999,
                        compact_write=True,
                    )
                    # Select only this codec with an explicit producer-mode
                    # test double. This does NOT qualify protected capture or
                    # capacity; the source event/parts are still checked fresh.
                    # The isolated port issues no HTTP or actual PR mutation.
                    with (
                        patch.object(physical, "_quarantine_capacity_guard", object()),
                        patch(
                            "review_sensei_github.checkpoint_result_store.selected_accepted_result",
                            return_value=replace(
                                captured, native_format="literal-malformed-v1"
                            ),
                        ),
                    ):
                        if fault:
                            with self.assertRaises(OperationWriteUnconfirmed):
                                store.write(
                                    event_key=key,
                                    result=canonical,
                                    _check_existing=False,
                                )
                        else:
                            self.assertEqual(
                                store.write(
                                    event_key=key,
                                    result=canonical,
                                    _check_existing=False,
                                ),
                                hashlib.sha256(canonical).hexdigest(),
                            )
                    self.assertEqual(port.creates, 1)
                    available = store.read_available(event_key=key)
                    if fault == "absent":
                        self.assertIsNone(available)
                        self.assertIsNone(store.read_available(event_key=key))
                    else:
                        self.assertEqual(
                            available,
                            (hashlib.sha256(canonical).hexdigest(), canonical),
                        )
                        self.assertTrue(
                            uses_literal_publication(f.selected.http, bound)
                        )
                        unbound = replace(
                            bound, transaction=None, summary="Another complete result."
                        )
                        other = replace(
                            unbound,
                            transaction=pending.with_result(unbound.content_digest()),
                        )
                        self.assertFalse(
                            uses_literal_publication(f.selected.http, other)
                        )
                        ref, restored = store.original_reference(event_key=key)
                        self.assertEqual(restored, canonical)
                        self.assertEqual(ref["carrier_ids"], [8_000_001])
                        self.assertNotEqual(
                            ref["storage_payload_sha256"], ref["result_payload_sha256"]
                        )
                        port.rows.append(replace(port.rows[-1], comment_id=8_000_002))
                        with self.assertRaises((ValueError, ReviewInputError)):
                            store.read_available(event_key=key)
                    self.assertEqual(port.creates, 1)


if __name__ == "__main__":
    unittest.main()
