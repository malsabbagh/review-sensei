"""Private lifecycle field fidelity and fail-closed wire boundary tests."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import fields, replace

from review_sensei.active_finding_window import (
    AuthoritativeDecisionInventory,
    apply_dispositions_to_window_state,
    commit_window_publication,
    evaluate_active_window_round,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import (
    FileDriftDescriptor,
    commit_drift_publication,
    evaluate_drift_round,
)
from review_sensei.finding_lifecycle import (
    MAX_LIFECYCLE_BYTES,
    decode_lifecycle_bytes,
    decode_lifecycle_state,
    encode_lifecycle_state,
    lifecycle_state_bytes,
)
from review_sensei.models import ReviewComment, ReviewResult

BASE = "a" * 40
HEAD = "b" * 40
NEXT = "c" * 40
INPUT = "d" * 64
PROOF = "e" * 64


def comment(path="src/one.py"):
    return ReviewComment(
        path=path,
        line=9,
        body="Original complete admitted finding payload.",
        blocking=False,
        severity="high",
        category="correctness",
        fix_effort="small",
        symbol="dispatch",
        defect_kind="unchecked-result",
        evidence_id="evidence-1",
        side="LEFT",
        effective_blocking=True,
        needs_human=True,
    )


def result(*comments):
    paths = tuple(dict.fromkeys(c.path for c in comments)) or (
        "src/one.py",
        "src/two.py",
    )
    return ReviewResult(
        summary="Review.",
        comments=tuple(comments),
        provider="fixture",
        review_status="complete",
        coverage=CoverageManifest(
            files=tuple(FileCoverage(path, "reviewed") for path in paths),
            enumerated_paths=paths,
            enumeration_complete=True,
        ),
    )


def active_state():
    return evaluate_active_window_round(
        result(comment(), comment("src/two.py")),
        head_sha=HEAD,
        base_sha=BASE,
        max_active_findings=1,
    )


def drift_state():
    return evaluate_drift_round(
        result(comment(), comment("src/two.py")),
        head_sha=HEAD,
        base_sha=BASE,
        max_active_findings=1,
    )


class LifecycleCodecTests(unittest.TestCase):
    def test_active_all_thirteen_fields_not_dataclass_equality(self):
        state = active_state()
        restored = decode_lifecycle_bytes(
            lifecycle_state_bytes(state, input_identity=(HEAD, BASE, INPUT))
        )
        self.assertEqual(restored.input_identity, (HEAD, BASE, INPUT))
        original = state.authoritative_decision_inventory.obligations
        recovered = restored.state.authoritative_decision_inventory.obligations
        self.assertEqual(len(recovered), 2)
        self.assertEqual(len(fields(ReviewComment)), 13)
        for before, after in zip(original, recovered, strict=True):
            for field in fields(ReviewComment):
                with self.subTest(field=field.name):
                    self.assertEqual(
                        getattr(before.comment, field.name),
                        getattr(after.comment, field.name),
                    )
        changed = replace(
            original[0], comment=replace(original[0].comment, body="Other body.")
        )
        self.assertEqual(original[0], changed)  # Equality is deliberately insufficient.
        self.assertNotEqual(original[0].comment.body, changed.comment.body)
        self.assertNotIn("effective_blocking", original[0].comment.to_dict())
        self.assertNotIn("needs_human", original[0].comment.to_dict())
        self.assertEqual(
            lifecycle_state_bytes(state), lifecycle_state_bytes(restored.state)
        )

    def test_every_comment_field_is_mandatory(self):
        for mode in (active_state, drift_state):
            document = encode_lifecycle_state(mode())
            inv_key = (
                "authoritative_decision_inventory"
                if mode is active_state
                else "inventory"
            )
            comment_key = "comment" if mode is active_state else "latest_comment"
            for field in fields(ReviewComment):
                candidate = copy.deepcopy(document)
                del candidate["state"][inv_key]["obligations"][0][comment_key][
                    field.name
                ]
                with (
                    self.subTest(mode=mode.__name__, field=field.name),
                    self.assertRaises(ReviewInputError),
                ):
                    decode_lifecycle_state(candidate)

    def test_drift_history_payload_and_native_binding_roundtrip(self):
        state = drift_state()
        oid = state.active_entries[0].obligation_id
        state = commit_drift_publication(
            state, created_threads={oid: ("PRRT_real-fixture-node", 987)}
        )
        history = {oid: frozenset({"1" * 64, "2" * 64})}
        restored = decode_lifecycle_bytes(
            lifecycle_state_bytes(state, concern_history=history)
        )
        self.assertEqual(restored.concern_history, history)
        for original, recovered in zip(
            state.inventory.obligations,
            restored.state.inventory.obligations,
            strict=True,
        ):
            for field in fields(ReviewComment):
                self.assertEqual(
                    getattr(original.latest_comment, field.name),
                    getattr(recovered.latest_comment, field.name),
                )
        binding = next(
            o.thread_binding
            for o in restored.state.inventory.obligations
            if o.obligation_id == oid
        )
        self.assertEqual(
            (binding.thread_id, binding.comment_id), ("PRRT_real-fixture-node", 987)
        )
        self.assertEqual(
            lifecycle_state_bytes(state), lifecycle_state_bytes(restored.state)
        )

    def test_uncertain_active_slot_and_omitted_blocker_survive(self):
        state = active_state()
        oid = state.display_active_findings.entries[0].obligation_id
        state = commit_window_publication(
            state, uncertain_targets={oid: "intent:exact-original-operation"}
        )
        restored = decode_lifecycle_state(encode_lifecycle_state(state)).state
        self.assertTrue(restored.display_active_findings.has_uncertain_slots)
        later = evaluate_active_window_round(
            result(),
            head_sha=NEXT,
            base_sha=BASE,
            max_active_findings=1,
            round_number=2,
            prior_inventory=restored.authoritative_decision_inventory,
            slots=restored.display_active_findings.slots,
        )
        recovered = decode_lifecycle_state(encode_lifecycle_state(later)).state
        self.assertFalse(recovered.approval_eligibility.approved)
        self.assertEqual(len(recovered.authoritative_decision_inventory.obligations), 2)
        self.assertTrue(
            all(
                o.lifecycle_state == "omitted-uncertain"
                for o in recovered.authoritative_decision_inventory.obligations
            )
        )
        self.assertEqual(
            {
                o.resolution_criterion
                for o in state.authoritative_decision_inventory.obligations
            },
            {
                o.resolution_criterion
                for o in recovered.authoritative_decision_inventory.obligations
            },
        )

    def test_ambiguous_drift_survives_without_fabricated_resolution(self):
        initial = drift_state()
        state = evaluate_drift_round(
            result(),
            head_sha=NEXT,
            base_sha=BASE,
            max_active_findings=1,
            prior_inventory=initial.inventory,
            round_number=2,
            drift_descriptors=(
                FileDriftDescriptor("file_copy", "src/one.py", ("src/copy.py",)),
            ),
        )
        restored = decode_lifecycle_state(encode_lifecycle_state(state)).state
        self.assertFalse(restored.approved)
        self.assertTrue(restored.inventory.has_ambiguous_drift)
        self.assertEqual(state.inventory, restored.inventory)
        for original, recovered in zip(
            state.inventory.obligations, restored.inventory.obligations, strict=True
        ):
            self.assertEqual(
                original.present_anchor.candidate_paths,
                recovered.present_anchor.candidate_paths,
            )
            self.assertEqual(
                original.criterion.resolution_criterion,
                recovered.criterion.resolution_criterion,
            )

    def test_pending_drift_intent_has_no_fabricated_native_comment(self):
        state = drift_state()
        oid = state.active_entries[0].obligation_id
        state = commit_drift_publication(
            state, uncertain_threads={oid: ("intent:original-operation", None, PROOF)}
        )
        restored = decode_lifecycle_bytes(lifecycle_state_bytes(state)).state
        binding = next(
            o.thread_binding
            for o in restored.inventory.obligations
            if o.obligation_id == oid
        )
        self.assertIsNone(binding.comment_id)
        self.assertEqual(binding.slot_state, "uncertain")
        self.assertEqual(binding.thread_id, "intent:original-operation")
        self.assertFalse(restored.approved)

    def test_terminal_proof_released_slot_and_next_obligation_survive(self):
        initial = active_state()
        entry = initial.display_active_findings.entries[0]
        published = commit_window_publication(
            initial, created_targets={entry.obligation_id: "PRRT_observed"}
        )
        changed = apply_dispositions_to_window_state(
            published,
            honored_fingerprints={entry.fingerprint},
            honored_instances=set(),
            actor="maintainer",
            head_sha=HEAD,
            slot_released=True,
        )
        restored = decode_lifecycle_bytes(lifecycle_state_bytes(changed)).state
        terminal = next(
            o
            for o in restored.authoritative_decision_inventory.obligations
            if o.obligation_id == entry.obligation_id
        )
        self.assertEqual(terminal.lifecycle_state, "authorized-override")
        self.assertIsNotNone(terminal.resolution_proof_sha256)
        self.assertEqual(terminal.target_state, "released")
        self.assertFalse(restored.approval_eligibility.approved)
        next_round = evaluate_active_window_round(
            result(),
            head_sha=NEXT,
            base_sha=BASE,
            max_active_findings=1,
            round_number=2,
            prior_inventory=restored.authoritative_decision_inventory,
            slots=restored.display_active_findings.slots,
        )
        self.assertNotEqual(
            next_round.display_active_findings.entries[0].obligation_id,
            entry.obligation_id,
        )
        document = encode_lifecycle_state(restored)
        for item in document["state"]["authoritative_decision_inventory"][
            "obligations"
        ]:
            if item["obligation_id"] == entry.obligation_id:
                item["resolution_proof_sha256"] = None
        with self.assertRaises(ReviewInputError):
            decode_lifecycle_state(document)

    def test_nested_records_are_closed(self):
        for source in (
            encode_lifecycle_state(active_state()),
            encode_lifecycle_state(drift_state()),
        ):
            paths = []

            def walk(value, path=()):
                if isinstance(value, dict):
                    paths.append(path)
                    for key, item in value.items():
                        walk(item, (*path, key))
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        walk(item, (*path, i))

            walk(source)
            for path in paths:
                # concern_history is a closed-pattern map, not a fixed record.
                candidate = copy.deepcopy(source)
                node = candidate
                for key in path:
                    node = node[key]
                node["unexpected_authority"] = "no"
                with self.subTest(path=path), self.assertRaises(ReviewInputError):
                    decode_lifecycle_state(candidate)

    def test_unknown_fields_version_purpose_and_cross_references_refuse(self):
        source = encode_lifecycle_state(active_state())
        mutations = [
            lambda d: d.update(schema_version="finding-lifecycle-payload-v2"),
            lambda d: d.update(purpose="human-inventory"),
            lambda d: d.update(extra="ignored-by-old-reader"),
            lambda d: d["state"].update(round_number=True),
            lambda d: d["state"]["display_active_findings"]["entries"][0].update(
                obligation_id="f" * 64
            ),
            lambda d: d["state"]["display_active_findings"]["entries"][0].update(
                required=False
            ),
            lambda d: d["state"]["authoritative_decision_inventory"][
                "obligations"
            ].append(
                copy.deepcopy(
                    d["state"]["authoritative_decision_inventory"]["obligations"][0]
                )
            ),
            lambda d: d["state"]["approval_eligibility"].update(
                approved=True,
                outcome="approved",
                withheld_reasons=[],
                recovery_steps=[],
            ),
            lambda d: d.update(input_identity=[NEXT, BASE, INPUT]),
            lambda d: d.update(concern_history={"f" * 64: [PROOF]}),
        ]
        for index, mutate in enumerate(mutations):
            candidate = copy.deepcopy(source)
            mutate(candidate)
            with self.subTest(index=index), self.assertRaises(ReviewInputError):
                decode_lifecycle_state(candidate)

    def test_native_slot_cross_reference_refuses_tampering(self):
        state = active_state()
        oid = state.display_active_findings.entries[0].obligation_id
        document = encode_lifecycle_state(
            commit_window_publication(
                state, created_targets={oid: "PRRT_observed-node"}
            )
        )
        document["state"]["display_active_findings"]["slots"][0]["proof_sha256"] = PROOF
        with self.assertRaises(ReviewInputError):
            decode_lifecycle_state(document)

    def test_duplicate_json_keys_nonfinite_overflow_and_invalid_bytes_refuse(self):
        for payload in (
            b'{"mode":"drift","mode":"active-window"}',
            b"NaN",
            b"\xff",
            b" " * (MAX_LIFECYCLE_BYTES + 1),
        ):
            with (
                self.subTest(payload_prefix=payload[:20]),
                self.assertRaises(ReviewInputError),
            ):
                decode_lifecycle_bytes(payload)

    def test_payload_capacity_refuses_without_truncation(self):
        state = active_state()
        originals = state.authoritative_decision_inventory.obligations
        large = tuple(
            replace(
                originals[1],
                obligation_id=f"{i:064x}",
                comment=replace(originals[1].comment, body="x" * 16_384),
            )
            for i in range(100)
        )
        state = replace(
            state,
            authoritative_decision_inventory=AuthoritativeDecisionInventory(
                (originals[0], *large)
            ),
        )
        with self.assertRaises(ReviewInputError):
            encode_lifecycle_state(state)

    def test_document_roundtrip_is_json_and_does_not_embed_previous_snapshots(self):
        document = encode_lifecycle_state(active_state())
        self.assertEqual(
            set(document),
            {
                "schema_version",
                "purpose",
                "mode",
                "state",
                "input_identity",
                "concern_history",
            },
        )
        restored = decode_lifecycle_state(json.loads(json.dumps(document)))
        self.assertEqual(encode_lifecycle_state(restored.state), document)
