"""Independent record, rendered-comment and immutable carrier capacity bounds."""

from __future__ import annotations

import hashlib
import json
import random
import string
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.api_run_history import read_previous_run, retain_previous_run
from review_sensei_github.api_run_host import HOSTED_API_STARTUP_PLAN
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    initial_accounting,
)
from review_sensei.bounded_evidence import canonical_bytes, partition_evidence
from review_sensei.errors import ReviewInputError
from review_sensei.session import (
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    SessionRecord,
)
from tests.test_api_work_persistence import IDENTITY
from tests.test_review_run import EntryFixture


class CallerRootBoundsTests(unittest.TestCase):
    def test_record_and_rendered_comment_refuse_at_their_independent_bounds(self):
        state = initial_accounting(
            ApiRunIdentity(123, 456),
            FrozenApiConfiguration("a" * 40, "b" * 64, 10000),
            ApiExecutionOwner(1, "hosted", 0),
            HOSTED_API_STARTUP_PLAN,
            prior_nonstartup_liability_upper_bound=0,
        ).reserve("bounded-probe", 9000)
        record = SessionRecord.create(
            IDENTITY,
            dispositions=(
                {
                    "fingerprint": "c" * 64,
                    "action": "defer",
                    "reason": 'quote" slash\\ 😀 汉字 ' * 12,
                    "actor": "fixture",
                    "head_sha": None,
                    "expires_at": None,
                },
            ),
            api_run_accounting=state.to_document(),
        )
        previous = record
        for i in range(500):
            pending = PendingApiRequest(
                hashlib.sha256(str(i).encode()).hexdigest(), 253402300799999
            )
            state = state.dispatch("bounded-probe", pending).confirm(pending)
            try:
                record = record.evolve(api_run_accounting=state.to_document())
            except ReviewInputError as error:
                self.assertIn("size limit", str(error))
                break
            previous = record
        else:
            self.fail("bounded request identity growth did not hit the root limit")
        raw = json.dumps(
            previous.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode()
        self.assertLessEqual(len(raw), MAX_SESSION_RECORD_BYTES)
        body = render_session_comment(
            repository_id=123, pull_request=7, record=previous
        )
        self.assertLessEqual(len(body.encode()), MAX_SESSION_COMMENT_BYTES)
        self.assertEqual(
            parse_session_comment(body, identity=IDENTITY).to_dict(), previous.to_dict()
        )
        with self.assertRaisesRegex(ReviewInputError, "session comment exceeds"):
            render_session_comment(
                repository_id=123,
                pull_request=7,
                record=previous,
                consumed_grants=tuple(
                    hashlib.sha256(f"g{i}".encode()).hexdigest() for i in range(600)
                ),
            )
        oversized = "😀" * (MAX_SESSION_COMMENT_BYTES // 4 + 1) + "\n" + body
        self.assertIsNone(parse_session_comment(oversized, identity=IDENTITY))

    def test_exact_archived_root_round_trips_and_large_codec_document_refuses_before_write(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=1000)
            self.assertEqual(f.invoke(), 0, f.error)
            original = parse_session_comment(f.comment["body"], identity=IDENTITY)
            canonical_size = len(canonical_bytes(original.to_dict()))
            comment_size = len(f.comment["body"].encode())
            f.next_run()
            self.assertEqual(f.invoke(), 0, f.error)
            reference = f.accounted.accounting.previous_record
            restored = read_previous_run(f.selected.ledger, IDENTITY, reference)
            self.assertEqual(restored.to_dict(), original.to_dict())
            self.assertEqual(len(reference["manifest"]["parts"]), 1)
            archive = f.extra_comments[
                int(reference["manifest"]["parts"][0]["storage_id"])
            ]["body"]
            self.assertLessEqual(len(archive.encode()), 32768)
            self.measurement = {
                "canonical_root_bytes": canonical_size,
                "rendered_comment_bytes": comment_size,
                "archived_part_bytes": len(archive.encode()),
            }
            # Stress the carrier guard with actual compression and an opaque
            # poorly compressible document. Public root parsing is separately
            # bounded above; this injection supplies no authenticated authority.
            rng = random.Random(19)
            payload = "".join(
                rng.choices(
                    string.ascii_letters + string.digits + string.punctuation, k=32700
                )
            )
            native = f.selected.ledger
            before = len(f.trace)
            actual_partition = partition_evidence

            def large_document(_document, **kwargs):
                return actual_partition({"opaque_root": payload}, **kwargs)

            with (
                patch(
                    "review_sensei_github.api_run_history.partition_evidence",
                    side_effect=large_document,
                ),
                patch.object(native, "_mutation_active", True),
            ):
                with self.assertRaisesRegex(ReviewInputError, "one-part carrier"):
                    retain_previous_run(native, IDENTITY, restored)
            self.assertEqual(len(f.trace), before)
