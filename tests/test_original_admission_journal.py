"""Pure v4 round trips preserve exact old schemas and never enable execution."""

import copy
import hashlib
import unittest

import test_guarded_publication_batch as guarded_fixture
import test_original_admission_projection as admission_fixture
from review_sensei_github.finding_lifecycle import _pack_journal, _validate
from review_sensei_github.original_admission_journal import (
    original_admission_journal,
    pack_original_admission_journal,
    unpack_original_admission_journal,
    validate_original_admission_journal,
)
from test_finding_lifecycle_journal import document
from test_review_transaction import IDENTITY

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import MAX_LIFECYCLE_BYTES


class OriginalAdmissionJournalTests(unittest.TestCase):
    def bases(self):
        return (
            document([]),
            {
                **document([]),
                "schema_version": "finding-lifecycle-journal-v2",
                "accepted_semantic_history": None,
            },
            guarded_fixture.GuardedPairSchemaTests().pair(),
        )

    def test_old_encodings_are_byte_identical_and_v4_round_trips(self):
        source, _, _, _, projection = (
            admission_fixture.OriginalAdmissionProjectionTests().inputs()
        )
        old_hashes = (
            "e1d3d1132851797acb7f13768d230d56f01ac5364818341bdcfe68f161ba061b",
            "667ecfa54634cfaa6fc9b4adeb5e59a2c3fbb1271bc82614637dc2efeeda1eae",
            "9106368d6062f77f19f138b54ca8a246c2d8a194464be3c37658e8b01ea2e258",
        )
        for base, digest in zip(self.bases(), old_hashes, strict=True):
            with self.subTest(base=base["schema_version"]):
                self.assertEqual(
                    hashlib.sha256(canonical_bytes(_pack_journal(base))).hexdigest(),
                    digest,
                )
                framed = original_admission_journal(
                    base, projection, source, identity=IDENTITY
                )
                packed = pack_original_admission_journal(
                    framed, source, identity=IDENTITY
                )
                self.assertEqual(
                    unpack_original_admission_journal(
                        packed, source, identity=IDENTITY
                    ),
                    framed,
                )
                with self.assertRaises(ReviewInputError):
                    _validate(framed)  # No current execution lease inherits v4.
                framed["original_admission"]["result_sha256"] = "f" * 64
                self.assertNotEqual(framed["original_admission"], projection)

    def test_nested_unknown_missing_and_extra_base_fields_are_refused(self):
        source, _, _, _, projection = (
            admission_fixture.OriginalAdmissionProjectionTests().inputs()
        )
        framed = original_admission_journal(
            self.bases()[0], projection, source, identity=IDENTITY
        )
        for change in (
            {"base_schema_version": "finding-lifecycle-journal-v4"},
            {"base_schema_version": "future"},
            {"base_schema_version": None},
            {"base_schema_version": []},
            {"original_admission": None},
            {"extra": True},
            {"accepted_semantic_history": None},
        ):
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                validate_original_admission_journal(
                    {**framed, **change}, source, identity=IDENTITY
                )
        with self.assertRaises(ReviewInputError):
            original_admission_journal(framed, projection, source, identity=IDENTITY)
        with self.assertRaises(ReviewInputError):
            original_admission_journal(
                {"schema_version": []}, projection, source, identity=IDENTITY
            )
        missing = dict(framed)
        del missing["effects"]
        with self.assertRaises(ReviewInputError):
            validate_original_admission_journal(missing, source, identity=IDENTITY)

    def test_old_pair_cannot_be_relabelled_as_a_grouped_inline_pair(self):
        # Grouped inline part intent needs an explicitly new durable contract.
        # Wrapping the old body-only pair in v4 must not broaden that contract.
        from review_sensei_github.finding_lifecycle import _digest, _request_identity

        source, _, _, _, projection = (
            admission_fixture.OriginalAdmissionProjectionTests().inputs()
        )
        base = guarded_fixture.GuardedPairSchemaTests().pair()
        review = base["effects"][2]
        review["request"]["comments"] = _request_identity(
            {"comments": [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "x"}]}
        )["comments"]
        review["request_sha256"] = _digest(
            {key: review[key] for key in ("method", "path", "request")}
        )
        base["guarded_publication_pair"]["review_request_sha256"] = review[
            "request_sha256"
        ]
        with self.assertRaises(ReviewInputError):
            original_admission_journal(base, projection, source, identity=IDENTITY)

    def test_combined_and_decompressed_bytes_share_original_limit(self):
        source, _, _, _, projection = (
            admission_fixture.OriginalAdmissionProjectionTests().inputs()
        )
        framed = original_admission_journal(
            self.bases()[0], projection, source, identity=IDENTITY
        )
        huge = copy.deepcopy(framed)
        huge["original_admission"]["finding_index"] = "X" * MAX_LIFECYCLE_BYTES
        with self.assertRaisesRegex(ReviewInputError, "total byte capacity"):
            pack_original_admission_journal(huge, source, identity=IDENTITY)
        packed = pack_original_admission_journal(framed, source, identity=IDENTITY)
        packed["journal"]["decoded_bytes"] = MAX_LIFECYCLE_BYTES + 1
        with self.assertRaises(ReviewInputError):
            unpack_original_admission_journal(packed, source, identity=IDENTITY)

    def test_repeated_codec_preserves_original_across_later_payload_states(self):
        # Pure transformations and cold codec reads only, not runtime writer or
        # command execution qualification. Current runtime still rejects v4.
        import test_original_admission_projection as fixture
        from review_sensei_github.original_admission_projection import (
            require_preserved_original_admission,
            restore_original_admission,
        )
        from test_finding_lifecycle_journal import effect

        from review_sensei.finding_lifecycle import encode_lifecycle_state
        from review_sensei.models import ReviewResult

        for evaluate in (
            fixture.evaluate_drift_round,
            fixture.evaluate_active_window_round,
        ):
            with self.subTest(mode=evaluate.__name__):
                source, state, lifecycle, index, projection = (
                    admission_fixture.OriginalAdmissionProjectionTests().inputs(
                        evaluate
                    )
                )
                if evaluate is fixture.evaluate_drift_round:
                    oid = state.inventory.obligations[0].obligation_id
                    bound = fixture.commit_drift_publication(
                        state, created_threads={oid: ("THREAD", 71)}
                    )
                    later = fixture.apply_dispositions_to_drift_state(
                        bound,
                        honored_fingerprints=(),
                        honored_instances=(
                            fixture.finding_instance_fingerprint(source.comments[0]),
                        ),
                        actor="maintainer",
                        head_sha=fixture.HEAD_SHA,
                        slot_released=False,
                    )
                else:
                    oid = state.authoritative_decision_inventory.obligations[
                        0
                    ].obligation_id
                    bound = fixture.commit_window_publication(
                        state, created_targets={oid: "THREAD"}
                    )
                    later = fixture.apply_dispositions_to_window_state(
                        bound,
                        honored_fingerprints=(),
                        honored_instances=(
                            fixture.finding_instance_fingerprint(source.comments[0]),
                        ),
                        actor="maintainer",
                        head_sha=fixture.HEAD_SHA,
                        slot_released=False,
                    )
                pending = effect()
                confirmed = effect(status="confirmed", response=[201, {"id": 71}])
                base = {**document([pending]), "lifecycle": lifecycle}
                framed = original_admission_journal(
                    base, projection, source, identity=IDENTITY
                )
                origin = canonical_bytes(projection)
                for current_state, current_effects in (
                    (state, [pending]),
                    (bound, [confirmed]),
                    (later, [confirmed]),
                    (later, [confirmed]),
                ):
                    framed["lifecycle"] = encode_lifecycle_state(
                        current_state,
                        input_identity=(fixture.HEAD_SHA, fixture.BASE_SHA, "d" * 64),
                    )
                    framed["effects"] = copy.deepcopy(current_effects)
                    packed = pack_original_admission_journal(
                        framed, source, identity=IDENTITY
                    )
                    framed = unpack_original_admission_journal(
                        packed,
                        ReviewResult.from_dict(source.to_dict()),
                        identity=IDENTITY,
                    )
                    require_preserved_original_admission(
                        projection, framed["original_admission"]
                    )
                    self.assertEqual(
                        canonical_bytes(framed["original_admission"]), origin
                    )
                    restored, restored_index = restore_original_admission(
                        framed["original_admission"],
                        ReviewResult.from_dict(source.to_dict()),
                        identity=IDENTITY,
                    )
                    self.assertEqual(restored_index, index)
                    self.assertTrue(
                        all(
                            c.effective_blocking and c.needs_human
                            for c in restored.comments
                        )
                    )
                    with self.assertRaises(ReviewInputError):
                        _validate(framed)
