"""Lossless original data survives later supported state transitions; no grant."""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.original_admission_projection import (
    project_original_admission,
    require_preserved_original_admission,
    restore_original_admission,
)
from review_sensei_github.publication import (
    ReviewPublisher,
    finding_index_marker,
    review_result_digest,
)
from test_native_group_publication import GroupWriterBackend
from test_review_transaction import BASE_SHA, HEAD_SHA, IDENTITY

from review_sensei.active_finding_window import (
    apply_dispositions_to_window_state,
    commit_window_publication,
    evaluate_active_window_round,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diff import DiffAnalysis
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import (
    apply_dispositions_to_drift_state,
    commit_drift_publication,
    evaluate_drift_round,
)
from review_sensei.finding_lifecycle import encode_lifecycle_state
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewResult
from review_sensei.placement import HostPlacementFacts


class OriginalAdmissionProjectionTests(unittest.TestCase):
    def inputs(self, evaluate=evaluate_drift_round):
        backend = GroupWriterBackend()
        source = replace(
            backend.result,
            comments=tuple(
                replace(c, effective_blocking=True, needs_human=True)
                for c in backend.result.comments
            ),
        )
        state = evaluate(
            source, head_sha=HEAD_SHA, base_sha=BASE_SHA, max_active_findings=2
        )
        lifecycle = encode_lifecycle_state(
            state, input_identity=(HEAD_SHA, BASE_SHA, "d" * 64)
        )
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
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            literal_malformed_fences=True,
        )
        index = finding_index_marker(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            result_digest=review_result_digest(source),
            entries=assembled.index_entries,
            complete_inventory=True,
        )
        projection = project_original_admission(
            source, lifecycle, index, identity=IDENTITY
        )
        self.assertEqual(backend.calls, [])
        return source, state, lifecycle, index, projection

    def test_both_original_producer_indexes_restore_omitted_effective_flags(self):
        for evaluate in (evaluate_drift_round, evaluate_active_window_round):
            with self.subTest(mode=evaluate.__name__):
                source, _, lifecycle, index, projection = self.inputs(evaluate)
                public = ReviewResult.from_dict(source.to_dict())
                self.assertTrue(
                    all(c.effective_blocking is None for c in public.comments)
                )
                restored, actual_index = restore_original_admission(
                    projection, public, identity=IDENTITY
                )
                self.assertEqual(actual_index, index)
                self.assertEqual(restored.to_dict(), public.to_dict())
                self.assertTrue(
                    all(
                        c.effective_blocking and c.needs_human
                        for c in restored.comments
                    )
                )
                lifecycle["input_identity"][2] = "e" * 64
                self.assertEqual(
                    projection["original_lifecycle"]["input_identity"][2], "d" * 64
                )

    def test_native_binding_and_command_updates_do_not_replace_original_data(self):
        for evaluate in (evaluate_drift_round, evaluate_active_window_round):
            with self.subTest(mode=evaluate.__name__):
                source, state, _, index, projection = self.inputs(evaluate)
                original = canonical_bytes(projection)
                if evaluate is evaluate_drift_round:
                    oid = state.inventory.obligations[0].obligation_id
                    bound = commit_drift_publication(
                        state, created_threads={oid: ("THREAD", 71)}
                    )
                    later = apply_dispositions_to_drift_state(
                        bound,
                        honored_fingerprints=(),
                        honored_instances=(
                            finding_instance_fingerprint(source.comments[0]),
                        ),
                        actor="maintainer",
                        head_sha=HEAD_SHA,
                        slot_released=False,
                    )
                else:
                    oid = state.authoritative_decision_inventory.obligations[
                        0
                    ].obligation_id
                    bound = commit_window_publication(
                        state, created_targets={oid: "THREAD"}
                    )
                    later = apply_dispositions_to_window_state(
                        bound,
                        honored_fingerprints=(),
                        honored_instances=(
                            finding_instance_fingerprint(source.comments[0]),
                        ),
                        actor="maintainer",
                        head_sha=HEAD_SHA,
                        slot_released=False,
                    )
                self.assertNotEqual(state, later)
                retained = copy.deepcopy(projection)
                require_preserved_original_admission(projection, retained)
                restored, actual_index = restore_original_admission(
                    retained,
                    ReviewResult.from_dict(source.to_dict()),
                    identity=IDENTITY,
                )
                self.assertEqual(actual_index, index)
                self.assertTrue(restored.comments[0].needs_human)
                self.assertEqual(canonical_bytes(retained), original)
                substituted = copy.deepcopy(retained)
                substituted["original_lifecycle"] = encode_lifecycle_state(
                    later, input_identity=(HEAD_SHA, BASE_SHA, "d" * 64)
                )
                with self.assertRaises(ReviewInputError):
                    require_preserved_original_admission(projection, substituted)

    def test_changed_origin_scope_index_input_and_absence_are_not_defaults(self):
        source, _, _, _, projection = self.inputs()
        for key, value in (
            ("transaction_id", "e" * 64),
            ("result_sha256", "f" * 64),
            ("finding_index", "edited"),
            ("repository_id", True),
        ):
            changed = {**projection, key: value}
            with self.subTest(key=key), self.assertRaises(ReviewInputError):
                restore_original_admission(changed, source, identity=IDENTITY)
        changed = copy.deepcopy(projection)
        changed["original_lifecycle"]["input_identity"][0] = "e" * 40
        with self.assertRaises(ReviewInputError):
            restore_original_admission(changed, source, identity=IDENTITY)
        for changed in (None, {}, {**projection, "extra": True}):
            with self.assertRaises(ReviewInputError):
                restore_original_admission(changed, source, identity=IDENTITY)
        with self.assertRaises(ReviewInputError):
            require_preserved_original_admission(None, projection)

    def test_original_criterion_flags_placement_and_source_edits_are_refused(self):
        source, _, lifecycle, index, projection = self.inputs()
        for path, value in (
            (
                (
                    "state",
                    "inventory",
                    "obligations",
                    0,
                    "criterion",
                    "resolution_criterion",
                ),
                "f" * 64,
            ),
            (
                ("state", "inventory", "obligations", 0, "criterion", "needs_human"),
                False,
            ),
            (("state", "active_entries", 0, "placement"), "body"),
        ):
            changed = copy.deepcopy(projection)
            selected = changed["original_lifecycle"]
            for key in path[:-1]:
                selected = selected[key]
            selected[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ReviewInputError):
                restore_original_admission(changed, source, identity=IDENTITY)
        edited = replace(
            source,
            transaction=None,
            comments=(
                replace(source.comments[0], body="Edited origin"),
                *source.comments[1:],
            ),
        )
        edited = replace(
            edited,
            transaction=replace(
                source.transaction, result_sha256=edited.content_digest()
            ),
        )
        with self.assertRaises(ReviewInputError):
            restore_original_admission(projection, edited, identity=IDENTITY)
        forged_flags = replace(
            source,
            comments=tuple(
                replace(c, effective_blocking=False) for c in source.comments
            ),
        )
        with self.assertRaises(ReviewInputError):
            project_original_admission(
                forged_flags, lifecycle, index, identity=IDENTITY
            )
        changed_input = copy.deepcopy(projection)
        changed_input["original_lifecycle"]["input_identity"][2] = "e" * 64
        with self.assertRaises(ReviewInputError):
            require_preserved_original_admission(projection, changed_input)
