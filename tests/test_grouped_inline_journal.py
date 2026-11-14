"""New explicit grouped variant never broadens an old pair or grants replay."""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.finding_lifecycle import _digest, _request_identity, _validate
from review_sensei_github.grouped_inline_journal import (
    cold_grouped_status,
    grouped_inline_journal,
    validate_grouped_inline_journal,
)
from review_sensei_github.original_admission_journal import (
    pack_original_admission_journal,
    unpack_original_admission_journal,
)

from review_sensei.errors import ReviewInputError
from tests.test_guarded_publication_batch import GuardedPairSchemaTests
from tests.test_native_group_publication import GroupWriterBackend
from tests.test_original_admission_projection import OriginalAdmissionProjectionTests
from tests.test_review_transaction import IDENTITY


class GroupedInlineJournalTests(unittest.TestCase):
    def inputs(self):
        source, _, lifecycle, _, projection = (
            OriginalAdmissionProjectionTests().inputs()
        )
        plan = GroupWriterBackend().plan
        base = GuardedPairSchemaTests().pair()
        base.pop("guarded_publication_pair")
        base["schema_version"] = "finding-lifecycle-journal-v2"
        base["lifecycle"] = lifecycle
        prefix = f"/repos/{IDENTITY.repository}"
        head = source.transaction.head_sha
        for e in base["effects"][:2]:
            e["path"] = e["path"].replace("/repos/o/r", prefix)
            if "head_sha" in e["request"]:
                e["request"]["head_sha"] = head
            if "check_update_precondition" in e:
                e["check_update_precondition"]["head_sha"] = head
        base["effects"][0]["response"][1].update(producer_id=None, mutation=None)
        part = base["effects"][2]
        part["path"] = f"{prefix}/pulls/{IDENTITY.pull_request}/reviews"
        part["request"] = _request_identity(plan.part_payload(0))
        for e in base["effects"]:
            e["request_sha256"] = _digest(
                {k: e[k] for k in ("method", "path", "request")}
            )
        return source, projection, plan, base

    def test_exact_inline_pair_roundtrip_and_no_cold_replay(self):
        source, projection, plan, base = self.inputs()
        doc = grouped_inline_journal(
            base, projection, source, identity=IDENTITY, plan=plan
        )
        packed = pack_original_admission_journal(
            doc, source, identity=IDENTITY, plan=plan
        )
        self.assertEqual(
            unpack_original_admission_journal(
                packed, source, identity=IDENTITY, plan=plan
            ),
            doc,
        )
        self.assertEqual(
            cold_grouped_status(doc, source, identity=IDENTITY, plan=plan),
            {"mode": "read-only", "unconfirmed": (1, 2)},
        )
        with self.assertRaises(ReviewInputError):
            _validate(doc)
        with self.assertRaises(ReviewInputError):
            unpack_original_admission_journal(packed, source, identity=IDENTITY)

    def test_plan_request_descriptor_and_order_changes_fail(self):
        source, projection, plan, base = self.inputs()
        doc = grouped_inline_journal(
            base, projection, source, identity=IDENTITY, plan=plan
        )
        for key, value in (
            ("part_ordinal", 1),
            ("transaction_id", "f" * 64),
            ("original_admission_sha256", "f" * 64),
            ("extra", 1),
        ):
            changed = copy.deepcopy(doc)
            changed["grouped_inline_pair"][key] = value
            with self.subTest(key=key), self.assertRaises(ReviewInputError):
                validate_grouped_inline_journal(
                    changed, source, identity=IDENTITY, plan=plan
                )
        for position in (0, 1):
            changed = copy.deepcopy(base)
            changed["effects"][position]["status"] = "pending"
            changed["effects"][position]["response"] = None
            changed["effects"][2]["status"] = "confirmed"
            changed["effects"][2]["response"] = [201, {"id": 9, "producer_id": 7}]
            with self.assertRaises(ReviewInputError):
                grouped_inline_journal(
                    changed, projection, source, identity=IDENTITY, plan=plan
                )
        changed = copy.deepcopy(base)
        changed["effects"][2]["request"]["comments"][0]["line"] += 1
        e = changed["effects"][2]
        e["request_sha256"] = _digest({k: e[k] for k in ("method", "path", "request")})
        with self.assertRaises(ReviewInputError):
            grouped_inline_journal(
                changed, projection, source, identity=IDENTITY, plan=plan
            )
        with self.assertRaises(ReviewInputError):
            validate_grouped_inline_journal(
                doc, source, identity=IDENTITY, plan=replace(plan, sha256="f" * 64)
            )

    def test_receipts_advance_in_order_without_dropping_pending_intent(self):
        from review_sensei_github.grouped_inline_journal import (
            require_grouped_inline_transition,
        )

        source, projection, plan, base = self.inputs()
        pending = grouped_inline_journal(
            base, projection, source, identity=IDENTITY, plan=plan
        )
        confirmed = copy.deepcopy(base)
        confirmed["effects"][1].update(
            status="confirmed",
            response=[
                200,
                {"id": 42, "app_id": 9, "producer_id": None, "mutation": None},
            ],
        )
        confirmed["effects"][2].update(
            status="confirmed",
            response=[
                201,
                {"id": 43, "producer_id": 9, "app_id": None, "mutation": None},
            ],
        )
        done = grouped_inline_journal(
            confirmed, projection, source, identity=IDENTITY, plan=plan
        )
        require_grouped_inline_transition(
            pending, done, source, identity=IDENTITY, plan=plan
        )
        with self.assertRaises(ReviewInputError):
            require_grouped_inline_transition(
                done, pending, source, identity=IDENTITY, plan=plan
            )
        final = copy.deepcopy(confirmed["effects"][2])
        final["request"] = _request_identity(
            {
                "commit_id": source.transaction.head_sha,
                "event": "REQUEST_CHANGES",
                "body": "exact dynamic final union",
                "comments": [],
            }
        )
        final["request_sha256"] = _digest(
            {k: final[k] for k in ("method", "path", "request")}
        )
        final.update(status="pending", response=None)
        confirmed["effects"].append(final)
        with_final = grouped_inline_journal(
            confirmed, projection, source, identity=IDENTITY, plan=plan
        )
        require_grouped_inline_transition(
            done, with_final, source, identity=IDENTITY, plan=plan
        )
        self.assertEqual(
            cold_grouped_status(with_final, source, identity=IDENTITY, plan=plan)[
                "unconfirmed"
            ],
            (3,),
        )
        changed = copy.deepcopy(with_final)
        e = changed["effects"][3]
        e["request"]["body"] = _request_identity({"body": "different final"})["body"]
        e["request_sha256"] = _digest({k: e[k] for k in ("method", "path", "request")})
        with self.assertRaises(ReviewInputError):
            require_grouped_inline_transition(
                with_final, changed, source, identity=IDENTITY, plan=plan
            )

    def test_unbounded_receipts_and_noncompact_rejections_are_refused(self):
        source, projection, plan, base = self.inputs()
        for response in (
            [403, {"message": "x" * 100}],
            [429, None],
            [200, {"id": 2**63, "app_id": 9, "producer_id": None, "mutation": None}],
        ):
            changed = copy.deepcopy(base)
            changed["effects"][1].update(
                status="confirmed" if response[0] == 200 else "rejected",
                response=response,
            )
            with (
                self.subTest(response=response[0]),
                self.assertRaises(ReviewInputError),
            ):
                grouped_inline_journal(
                    changed, projection, source, identity=IDENTITY, plan=plan
                )
