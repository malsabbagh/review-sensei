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
from tests import test_guarded_publication_batch as guarded_fixture
from tests import test_original_admission_projection as admission_fixture
from tests.test_review_transaction import IDENTITY


class GroupedInlineJournalTests(unittest.TestCase):
    def inputs(self):
        source, _, lifecycle, _, projection = (
            admission_fixture.OriginalAdmissionProjectionTests().inputs()
        )
        from review_sensei_github.native_review_groups import (
            NativeInline,
            NativeReviewPlan,
        )
        from review_sensei_github.publication import ReviewPublisher

        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.diff import DiffAnalysis
        from review_sensei.placement import HostPlacementFacts

        assembled = ReviewPublisher._assemble_review_body(
            object.__new__(ReviewPublisher),
            result=source,
            analysis=DiffAnalysis(
                changed_lines={"f.py": frozenset({1, 2})},
                changed_paths=("f.py",),
                diff_bytes=100,
                diff_files=1,
                diff_hunks=1,
                diff_lines=5,
            ),
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            read_facts=lambda: HostPlacementFacts(),
            auto_approve=True,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=source.transaction.head_sha,
            base_sha=source.transaction.base_sha,
            literal_malformed_fences=True,
        )
        plan = NativeReviewPlan.prepare(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            base_sha=source.transaction.base_sha,
            head_sha=source.transaction.head_sha,
            transaction_id=source.transaction.transaction_id,
            result_sha256=source.transaction.result_sha256,
            items=tuple(
                NativeInline.from_prepared(comment, body, anchor=anchor)
                for comment, _, body, anchor in assembled.inline_comments
            ),
        )
        base = guarded_fixture.GuardedPairSchemaTests().pair()
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

    def test_coherent_alternate_plan_part_and_descriptor_are_not_original(self):
        from review_sensei_github.native_review_groups import NativeReviewPlan

        source, projection, plan, base = self.inputs()
        self.assertGreater(len(plan.items), 1)
        alternate = NativeReviewPlan.prepare(
            repository_id=plan.repository_id,
            pull_request=plan.pull_request,
            base_sha=plan.base_sha,
            head_sha=plan.head_sha,
            transaction_id=plan.transaction_id,
            result_sha256=plan.result_sha256,
            items=(
                replace(plan.items[0], body=plan.items[0].body + " altered"),
                *plan.items[1:],
            ),
        )
        base["effects"][2]["request"] = _request_identity(alternate.part_payload(0))
        effect = base["effects"][2]
        effect["request_sha256"] = _digest(
            {k: effect[k] for k in ("method", "path", "request")}
        )
        with self.assertRaises(ReviewInputError):
            grouped_inline_journal(
                base, projection, source, identity=IDENTITY, plan=alternate
            )

    def test_replaced_original_cold_renderer_is_not_equivalent_authority(self):
        from unittest.mock import patch

        source, projection, plan, base = self.inputs()
        with patch(
            "review_sensei_github.native_union.reconstruct_original_plan",
            return_value=plan,
        ) as altered:
            with self.assertRaises(ReviewInputError):
                grouped_inline_journal(
                    base, projection, source, identity=IDENTITY, plan=plan
                )
            altered.assert_not_called()

    def test_display_index_permutation_keeps_original_native_order(self):
        from review_sensei_github.publication import (
            finding_index_marker,
            parse_finding_index,
        )

        source, projection, plan, base = self.inputs()
        original_index = projection["finding_index"]
        parsed = parse_finding_index(
            original_index,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=plan.head_sha,
        )
        projection["finding_index"] = finding_index_marker(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=plan.head_sha,
            base_sha=plan.base_sha,
            result_digest=parsed.result_digest,
            entries=tuple(reversed(parsed.entries)),
            complete_inventory=True,
        )
        self.assertNotEqual(projection["finding_index"], original_index)
        framed = grouped_inline_journal(
            base, projection, source, identity=IDENTITY, plan=plan
        )
        decoded = unpack_original_admission_journal(
            pack_original_admission_journal(
                framed, source, identity=IDENTITY, plan=plan
            ),
            source,
            identity=IDENTITY,
            plan=plan,
        )
        self.assertEqual(
            decoded["original_admission"]["finding_index"], projection["finding_index"]
        )
        self.assertEqual(decoded["grouped_inline_pair"]["plan_sha256"], plan.sha256)
