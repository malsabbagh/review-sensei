"""Journal's effect boundary: exact owned readback and no blind re-dispatch.

These focused boundary tests supplement, not replace, ordinary subprocess
qualification in test_durable_finding_lifecycle_restart.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.finding_lifecycle import (
    MAX_EFFECTS,
    MAX_RECEIPT_BYTES,
    VERSION,
    FindingLifecycleJournal,
    _body_matches,
    _digest,
    _maximum_native_allocation,
    _pack_journal,
    _request_identity,
    _unpack_journal,
    _validate,
    restore_finding_lifecycle,
)

from review_sensei.active_finding_window import evaluate_active_window_round
from review_sensei.bounded_evidence import (
    canonical_bytes,
    decode_evidence,
    encode_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import (
    decode_lifecycle_state,
    encode_lifecycle_state,
)
from review_sensei.finding_lifecycle_store import (
    read_finding_lifecycle,
    stage_finding_lifecycle,
)
from review_sensei.models import ReviewComment, ReviewTransaction
from review_sensei.session import LocalSessionLedger, SessionIdentity
from tests.test_finding_lifecycle_codec import (
    BASE,
    HEAD,
    INPUT,
    active_state,
    comment,
    result,
)

REPO = "owner/repo"
PATH = "/repos/owner/repo/pulls/1/reviews"
IDENTITY = SessionIdentity(REPO, 1, repository_id=7)
REQUEST = {
    "commit_id": HEAD,
    "body": "Owned exact marker and review summary.",
    "event": "REQUEST_CHANGES",
    "comments": [
        {"path": "src/one.py", "line": 9, "body": "Finding A.", "side": "RIGHT"}
    ],
}


def effect(request=None, *, path=PATH, method="POST", status="pending", response=None):
    request = copy.deepcopy(REQUEST if request is None else request)
    return {
        "method": method,
        "path": path,
        "request": request,
        "request_sha256": _digest({"method": method, "path": path, "request": request}),
        "status": status,
        "response": response,
    }


def document(effects=()):
    return {
        "schema_version": VERSION,
        "lifecycle": encode_lifecycle_state(
            active_state(), input_identity=(HEAD, BASE, INPUT)
        ),
        "effects": list(effects),
        "unknown_trust_effect": False,
        "native_receipts": [],
    }


def native_review(**changes):
    return {
        "id": 1201,
        "body": REQUEST["body"],
        "commit_id": HEAD,
        "state": "CHANGES_REQUESTED",
        "user": {"id": 9001, "type": "Bot", "login": "reviewsensei[bot]"},
        **changes,
    }


class FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class ScriptedHTTP(FakeHTTP):
    def __init__(self, responses):
        super().__init__(None)
        self.responses = iter(responses)

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return next(self.responses)


def native_inline(**changes):
    return {
        **REQUEST["comments"][0],
        "id": 8001,
        "pull_request_review_id": 1201,
        "user": {"id": 9001, "type": "Bot", "login": "reviewsensei[bot]"},
        **changes,
    }


def journal(*, retained=None):
    reviewer = SimpleNamespace(
        _active_window_states={(7, 1): active_state()},
        _drift_states={},
        _active_window_inputs={(7, 1): (HEAD, BASE, INPUT)},
        _drift_inputs={},
        _drift_concern_history={},
        _unknown_effect_holds=set(),
    )
    ledger = SimpleNamespace(evidence_producer=lambda: "github-bot:9001")
    value = FindingLifecycleJournal(
        reviewer,
        ledger,
        IDENTITY,
        operation_id="1" * 64,
        base_sha=BASE,
        head_sha=HEAD,
        policy_digest="2" * 64,
        configuration_digest="3" * 64,
        document=retained,
        app_slug="reviewsensei[bot]",
    )
    value.reviewer.native_finding_receipts = lambda **kwargs: ()
    value.plan(active_state(), INPUT)
    return value


class FindingLifecycleJournalTests(unittest.TestCase):
    def test_request_body_commitments_are_exact_immutable_and_utf8_bounded(self):
        original = {"body": "évidence", "comments": [{"body": "nested body"}]}
        saved = copy.deepcopy(original)
        committed = _request_identity(original)
        self.assertEqual(original, saved)
        self.assertEqual(
            committed["body"]["utf8_bytes"], len("évidence".encode("utf-8"))
        )
        self.assertTrue(_body_matches("évidence", committed["body"]))
        self.assertFalse(_body_matches("evidence", committed["body"]))
        self.assertFalse(_body_matches("foreign!", committed["body"]))
        self.assertFalse(
            _body_matches("évidence", {**committed["body"], "utf8_bytes": 8})
        )
        self.assertTrue(_body_matches("legacy", "legacy"))
        committed["comments"][0]["body"]["utf8_bytes"] = 0
        self.assertEqual(original, saved)
        for corrupt in (
            {"$body_sha256": "a" * 64, "utf8_bytes": True},
            {"$body_sha256": "a" * 64, "utf8_bytes": -1},
            {"$body_sha256": "a" * 64, "utf8_bytes": 524289},
            {"$body_sha256": "bad", "utf8_bytes": 0},
            {"$body_sha256": "a" * 64, "utf8_bytes": 0, "extra": 1},
        ):
            with self.subTest(corrupt=corrupt), self.assertRaises(ReviewInputError):
                _validate(document([effect({**REQUEST, "body": corrupt})]))

    def test_committed_body_replay_refuses_foreign_native_bytes_without_write(self):
        for mutation in (
            {"body": "Foreign same-length content replacement"},
            {"user": {"id": 123, "type": "Bot"}},
        ):
            value = journal(retained=document([effect(_request_identity(REQUEST))]))
            http = FakeHTTP((200, [native_review(**mutation)]))
            with self.subTest(mutation=mutation), patch.object(value, "checkpoint"):
                with self.assertRaises(ReviewInputError):
                    value.request(http, "POST", PATH, token="secret", body=REQUEST)
            self.assertTrue(http.calls)
            self.assertTrue(all(call[0] == "GET" for call in http.calls))
            self.assertEqual(value.effects[0]["status"], "pending")

    def test_checkpoint_refusal_prevents_dispatch(self):
        value = journal()
        http = FakeHTTP((201, native_review()))
        with (
            patch.object(value, "_preflight_effect"),
            patch.object(
                value, "checkpoint", side_effect=ReviewInputError("capacity held")
            ),
        ):
            with self.assertRaises(ReviewInputError):
                value.request(http, "POST", PATH, token="not-retained", body=REQUEST)
        self.assertEqual(http.calls, [])
        self.assertEqual(value.effects[0]["status"], "pending")
        self.assertNotIn("not-retained", str(value.effects))

    def test_response_loss_preserves_durable_intent_before_dispatch(self):
        value = journal()
        checkpoints = []
        http = FakeHTTP(TimeoutError("response lost"))
        with (
            patch.object(value, "_preflight_effect"),
            patch.object(
                value,
                "checkpoint",
                side_effect=lambda **kwargs: checkpoints.append(
                    copy.deepcopy(value.effects)
                ),
            ),
        ):
            with self.assertRaises(TimeoutError):
                value.request(http, "POST", PATH, token="not-retained", body=REQUEST)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(checkpoints[0][0]["status"], "pending")
        self.assertIsNone(checkpoints[0][0]["response"])
        self.assertEqual(value.effects[0]["status"], "pending")

    def test_readback_foreign_edited_duplicate_and_absent_preserve_unknown(self):
        for values in (
            [],
            [native_review(user={"id": 123, "type": "Bot"})],
            [native_review(edited_by_human=True)],
            [native_review(), native_review(id=1202)],
            [native_review(body="Human edit.")],
            [native_review(commit_id="f" * 40)],
        ):
            value = journal(retained=document([effect()]))
            http = FakeHTTP((200, values))
            with (
                self.subTest(values=values),
                patch.object(value, "checkpoint") as checkpoint,
            ):
                with self.assertRaises(ReviewInputError):
                    value.request(http, "POST", PATH, token="secret", body=REQUEST)
                checkpoint.assert_not_called()
                self.assertTrue(http.calls)
                self.assertTrue(all(c[0] == "GET" for c in http.calls))
                self.assertEqual(value.effects[0]["status"], "pending")

    def test_prior_pending_effect_prevents_different_write(self):
        value = journal(retained=document([effect()]))
        http = FakeHTTP((201, native_review()))
        with patch.object(value, "checkpoint") as checkpoint:
            with self.assertRaises(ReviewInputError):
                value.request(
                    http,
                    "POST",
                    PATH,
                    token="secret",
                    body={**REQUEST, "body": "Another operation"},
                )
        checkpoint.assert_not_called()
        self.assertEqual(http.calls, [])

    def test_retained_confirmed_review_id_must_match_readback(self):
        value = journal(
            retained=document(
                [effect(status="confirmed", response=[201, native_review()])]
            )
        )
        http = FakeHTTP((200, [native_review(id=9999)]))
        with self.assertRaises(ReviewInputError):
            value.request(http, "POST", PATH, token="secret", body=REQUEST)
        self.assertTrue(http.calls)
        self.assertTrue(all(c[0] == "GET" for c in http.calls))

    def test_effect_bound_refuses_before_new_native_dispatch(self):
        effects = [
            effect(
                {**REQUEST, "body": str(i)},
                status="confirmed",
                response=[201, native_review(id=i + 1)],
            )
            for i in range(MAX_EFFECTS)
        ]
        value = journal(retained=document(effects))
        http = FakeHTTP((201, native_review()))
        with self.assertRaises(ReviewInputError):
            value.request(http, "POST", PATH, token="secret", body=REQUEST)
        self.assertEqual(http.calls, [])

    def test_oversize_response_keeps_original_pending_intent(self):
        value = journal()
        http = FakeHTTP((201, {"body": "x" * MAX_RECEIPT_BYTES}))
        with (
            patch.object(value, "_preflight_effect"),
            patch.object(value, "checkpoint"),
        ):
            with self.assertRaises(ReviewInputError):
                value.request(http, "POST", PATH, token="secret", body=REQUEST)
        self.assertEqual(value.effects[0]["status"], "pending")
        self.assertIsNone(value.effects[0]["response"])
        self.assertEqual(len(http.calls), 1)

    def test_closed_envelope_and_contradictory_effects_refuse(self):
        for value in (
            {**document(), "unrecognized": True},
            document([effect(response=[201, native_review()])]),
            document([effect(), effect({**REQUEST, "body": "later"})]),
            document([effect(status="confirmed", response=[True, {}])]),
        ):
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                _validate(value)

    def test_native_receipt_types_and_snapshot_are_closed(self):
        valid = {
            "schema_version": "native-finding-receipt-v1",
            "repository_id": 7,
            "pull_request": 1,
            "head_sha": HEAD,
            "base_sha": BASE,
            "producer_id": 9001,
            "review_id": 1201,
            "instance": "4" * 64,
            "body_sha256": "5" * 64,
            "thread_id": "PRRT_exact_native_node",
            "comment_id": 8001,
            "native_head_sha": HEAD,
        }
        for change in (
            {"native_head_sha": {"forged": True}},
            {"native_head_sha": "invalid"},
            {"thread_id": None, "comment_id": 8001},
            {"thread_id": None, "comment_id": True},
            {"thread_id": "", "comment_id": 8001},
        ):
            candidate = document()
            candidate["native_receipts"] = [{**valid, **change}]
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                _validate(candidate)

    def test_pending_review_inline_producer_is_independently_numeric(self):
        value = journal(retained=document([effect()]))
        http = ScriptedHTTP(
            [
                (200, [native_review()]),
                (
                    200,
                    [
                        native_inline(
                            user={
                                "id": 444,
                                "type": "Bot",
                                "login": "reviewsensei[bot]",
                            }
                        )
                    ],
                ),
            ]
        )
        with patch.object(value, "checkpoint"):
            with self.assertRaises(ReviewInputError):
                value.request(http, "POST", PATH, token="secret", body=REQUEST)
        self.assertEqual(value.effects[0]["status"], "pending")

    def test_readback_retains_only_compact_receipt_not_unrelated_response_fields(self):
        value = journal(retained=document([effect()]))
        http = ScriptedHTTP(
            [
                (200, [native_review(unrelated_private_context="must-not-retain")]),
                (200, [native_inline()]),
            ]
        )
        # This unit isolates compact receipt projection; the real native adoption
        # path is qualified separately by cold response-loss replay tests.
        with (
            patch.object(value, "checkpoint"),
            patch.object(value, "_adopt_review_readback", return_value=True),
        ):
            value.request(http, "POST", PATH, token="secret", body=REQUEST)
        self.assertEqual(value.effects[0]["status"], "confirmed")
        self.assertNotIn("unrelated_private_context", value.effects[0]["response"][1])
        self.assertNotIn("body", value.effects[0]["response"][1])

    def test_pending_review_wrong_event_cannot_discharge_intent(self):
        value = journal(retained=document([effect()]))
        http = FakeHTTP((200, [native_review(state="APPROVED")]))
        with patch.object(value, "checkpoint"):
            with self.assertRaises(ReviewInputError):
                value.request(http, "POST", PATH, token="secret", body=REQUEST)
        self.assertEqual(value.effects[0]["status"], "pending")

    def test_pending_review_without_exact_inline_inventory_stays_unknown(self):
        value = journal(retained=document([effect()]))
        # This endpoint alone cannot prove which comments were in the review.
        # A real bounded comment readback is required before intent confirmation.
        http = FakeHTTP((200, [native_review()]))
        with patch.object(value, "checkpoint"):
            with self.assertRaises(ReviewInputError):
                value.request(http, "POST", PATH, token="secret", body=REQUEST)
        self.assertEqual(value.effects[0]["status"], "pending")


class LifecycleJournalPackingTests(unittest.TestCase):
    def test_v3_roundtrip_and_canonical_ordering(self):
        source = {
            "z": [{"thread_id": "PRRT_fixture", "id": 77, "proof_sha256": "f" * 64}],
            "a": {
                "native_target": "PRRT_fixture",
                "comment_id": 77,
                "body_sha256": "f" * 64,
            },
        }
        packed = _pack_journal(source)
        self.assertEqual(packed["schema_version"], "finding-lifecycle-packed-v3")
        self.assertEqual(_unpack_journal(packed), source)
        reordered = {"a": dict(reversed(list(source["a"].items()))), "z": source["z"]}
        self.assertEqual(
            canonical_bytes(packed), canonical_bytes(_pack_journal(reordered))
        )
        atoms = packed["native_atoms"]
        self.assertEqual(sum(a[0] == "native" for a in atoms), 1)
        self.assertEqual(sum(a[0] == "digest" for a in atoms), 1)
        # Equal numeric values are not knowledge that the underlying identities
        # will remain equal after dispatch. They get independent allocations.
        self.assertEqual(sum(a[0] == "integer" for a in atoms), 2)

    def test_full_lifecycle_document_is_lossless_after_v3_pack(self):
        source = document(
            [
                effect(
                    status="confirmed",
                    response=[
                        201,
                        {
                            "id": 1201,
                            "producer_id": 9001,
                            "app_id": None,
                            "mutation": None,
                        },
                    ],
                )
            ]
        )
        restored = _unpack_journal(_pack_journal(source))
        self.assertEqual(restored, source)
        _validate(restored)

    def test_maximum_allocation_covers_unknown_native_identity_widths(self):
        source = {
            "a": {"thread_id": "PRRT_small", "comment_id": 7},
            "b": {"native_target": "PRRT_small", "id": 7},
        }
        packed = _pack_journal(source)
        maximum = _maximum_native_allocation(packed)
        self.assertTrue(
            all(
                len(a[1].encode("utf-8")) == 256
                for a in maximum["native_atoms"]
                if a[0] == "native"
            )
        )
        self.assertTrue(
            all(
                len(str(a[1])) == 19
                for a in maximum["native_atoms"]
                if a[0] == "integer"
            )
        )
        unknown = copy.deepcopy(packed)
        for atom in unknown["native_atoms"]:
            atom[1] = "Y" * 256 if atom[0] == "native" else 8_888_888_888_888_888_888
        self.assertLessEqual(
            len(canonical_bytes(unknown)), len(canonical_bytes(maximum))
        )
        self.assertEqual(
            _unpack_journal(packed), source
        )  # Sizing did not mutate authority.

    def test_opaque_native_atoms_accept_printable_utf8_not_assumed_host_encoding(self):
        for native in ('opaque host "identity" \\ é', "é" * 128, '"' * 256, "\\" * 256):
            with self.subTest(native=native):
                source = {"thread_id": native}
                packed = _pack_journal(source)
                self.assertEqual(_unpack_journal(packed), source)
                maximum = _maximum_native_allocation(packed)
                self.assertGreaterEqual(
                    len(canonical_bytes(maximum)), len(canonical_bytes(packed))
                )
        with self.assertRaises(ReviewInputError):
            _pack_journal({"thread_id": "é" * 129})

    def test_corrupt_atom_table_and_references_refuse(self):
        packed = _pack_journal({"thread_id": "PRRT_observed"})
        for atom in (
            ["native", "x" * 257],
            ["native", "contains\ncontrol"],
            ["integer", True],
            ["integer", 2**63],
            ["unknown", "PRRT_observed"],
            ["native", "PRRT_observed", "ignored"],
            ["native"],
            [],
            {"kind": "native", "value": "PRRT_observed"},
        ):
            candidate = copy.deepcopy(packed)
            candidate["native_atoms"] = [atom]
            with self.subTest(atom=atom), self.assertRaises(ReviewInputError):
                _unpack_journal(candidate)
        for index in (-1, True, 1, 8192):
            candidate = copy.deepcopy(packed)
            candidate["journal"] = encode_evidence(
                {"thread_id": {"$native": index}}, max_decoded_bytes=1_048_576
            )
            with self.subTest(index=index), self.assertRaises(ReviewInputError):
                _unpack_journal(candidate)
        candidate = copy.deepcopy(packed)
        candidate["native_atoms"].append(["native", "unused"])
        with self.assertRaises(ReviewInputError):
            _unpack_journal(candidate)
        with self.assertRaises(ReviewInputError):
            _pack_journal({"payload": {"$native": 0}})

    def test_v2_migration_requires_closed_legacy_atom_records(self):
        current = _pack_journal({"thread_id": "PRRT_observed", "id": 7})
        legacy = {
            **current,
            "schema_version": "finding-lifecycle-packed-v2",
            "native_atoms": [
                {"kind": kind, "value": value}
                for kind, value in current["native_atoms"]
            ],
        }
        self.assertEqual(_pack_journal(_unpack_journal(legacy)), current)
        for malformed in (
            {"kind": "integer", "value": 7, "ignored": True},
            {"kind": "integer"},
            {"value": 7},
            ["integer", 7],
            None,
        ):
            candidate = copy.deepcopy(legacy)
            candidate["native_atoms"][0] = malformed
            with self.subTest(malformed=malformed), self.assertRaises(ReviewInputError):
                _unpack_journal(candidate)
        for mutation in (
            {"unknown": "cannot-ignore"},
            {"schema_version": "finding-lifecycle-packed-v4"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ReviewInputError):
                _unpack_journal({**legacy, **mutation})

    def test_backward_alias_of_numeric_atoms_is_not_canonical(self):
        candidate = _pack_journal({"a": {"id": 7}, "b": {"id": 7}})
        skeleton = decode_evidence(
            candidate["journal"],
            max_encoded_bytes=1_048_576,
            max_decoded_bytes=1_048_576,
        )
        skeleton["b"]["id"] = {"$native": 0}
        candidate["native_atoms"].pop()
        candidate["journal"] = encode_evidence(skeleton, max_decoded_bytes=1_048_576)
        with self.assertRaises(ReviewInputError):
            _unpack_journal(candidate)

    def test_atom_reference_in_non_native_field_is_not_canonical(self):
        candidate = _pack_journal({"thread_id": "PRRT_fixture"})
        candidate["journal"] = encode_evidence(
            {"body": {"$native": 0}}, max_decoded_bytes=1_048_576
        )
        with self.assertRaises(ReviewInputError):
            _unpack_journal(candidate)

    def test_future_decoded_capacity_refuses_before_checkpoint_or_native_effect(self):
        # The proposed pending inventory fits; the future native-bound inventory
        # does not. This must run the production preflight, not a patched denial.
        comments = [
            replace(comment(f"src/{index}.py"), body="x" * 16000) for index in range(60)
        ]
        state = evaluate_active_window_round(
            result(*comments), head_sha=HEAD, base_sha=BASE, max_active_findings=60
        )
        pending = encode_lifecycle_state(state, input_identity=(HEAD, BASE, INPUT))
        self.assertLessEqual(len(canonical_bytes(pending)), 1_048_576)
        value = journal()
        value.plan(state, INPUT)
        value.reviewer._active_window_states[(7, 1)] = state
        http = FakeHTTP((201, native_review()))
        with patch.object(value, "checkpoint") as checkpoint:
            with self.assertRaises(ReviewInputError):
                value.request(http, "POST", PATH, token="secret", body=REQUEST)
        checkpoint.assert_not_called()
        self.assertEqual(http.calls, [])
        self.assertEqual(value.effects, [])

    def test_real_local_preflight_is_read_only_and_funds_future_tail(self):
        with tempfile.TemporaryDirectory() as root:
            value = journal()
            ledger = LocalSessionLedger(
                Path(root), enable_finding_lifecycle_writes=True
            )
            initial = ledger.initialize(IDENTITY)
            tx = ReviewTransaction.create(
                repository=REPO,
                pull_request=1,
                base_sha=BASE,
                head_sha=HEAD,
                policy_digest="2" * 64,
                configuration_digest="3" * 64,
                evidence_digest="4" * 64,
                reservation_id="5" * 64,
                generation=1,
            )
            record = ledger.replace(
                IDENTITY,
                lambda r: r.evolve(
                    generation=initial.generation + 1,
                    reservation_id="5" * 64,
                    reserved_slot="initial",
                    transaction=tx,
                ),
            )
            value.ledger = ledger
            value._record = record
            value.operation_id = tx.transaction_id
            value.effects = [effect()]
            before = {
                p.relative_to(root): p.read_bytes()
                for p in Path(root).rglob("*")
                if p.is_file()
            }
            # Publication restores this invocation-only context to None.
            # A warm consumer must not confuse it with native authority.
            value.reviewer._native_finding_context = None
            plan = value._preflight_effect()
            after = {
                p.relative_to(root): p.read_bytes()
                for p in Path(root).rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)
            self.assertIsInstance(plan, dict)
            self.assertEqual(
                value._native_receipt_tail_calls,
                plan["conditional_calls"]
                - plan["checkpoints"][0]["conditional_calls"]
                + 6,
            )
            self.assertGreater(
                value._future_wire_bound,
                len(canonical_bytes(_pack_journal(value._document()))),
            )
            self.assertEqual(value._record.record_sha256, record.record_sha256)
            self.assertEqual(value._record.reservation_id, "5" * 64)


class CompleteCarrierMigrationTests(unittest.TestCase):
    """Compatibility for complete authenticated v1/v2 bytes, not historical claims.

    The pre-feature baseline-only format did not retain this complete document.
    These tests deliberately source a complete document from today's ordinary
    producer, encode that document in the supported older carrier shape, and
    migrate it without claiming that unavailable historical evidence existed.
    """

    def test_complete_v1_and_v2_carriers_migrate_to_v3_without_native_effects_or_new_funds(
        self,
    ):
        from review_sensei_github import ReviewPublisher

        from tests import test_durable_finding_lifecycle_restart as restart

        for mode, legacy_version in (
            (mode, version) for mode in ("window", "drift") for version in (1, 2)
        ):
            with (
                self.subTest(mode=mode, legacy_version=legacy_version),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                outcome = restart.DurableFindingRestartTests.run_phase(
                    self, root, mode, "initial"
                )
                self.assertIsNone(outcome["error"])
                self.assertEqual(outcome["status"], "published")
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                original = ledger.load(restart.IDENTITY).record
                self.assertIsInstance(original.transaction, ReviewTransaction)
                complete = _unpack_journal(read_finding_lifecycle(ledger, original))
                # These records come from actual ordinary adapter readback of
                # the persistent native fixture, not from handwritten receipts.
                self.assertTrue(complete["native_receipts"])
                before_service = (root / "service.json").read_bytes()
                reference = original.finding_lifecycle
                binding = reference["manifest"]["binding"]
                args = dict(
                    operation_id=reference["operation_id"],
                    base_sha=binding["base_sha"],
                    head_sha=binding["head_sha"],
                    policy_digest=binding["policy_digest"],
                    configuration_digest=binding["configuration_digest"],
                )
                if legacy_version == 1:
                    legacy = {
                        "schema_version": "finding-lifecycle-packed-v1",
                        "journal": encode_evidence(
                            complete, max_decoded_bytes=1_048_576
                        ),
                    }
                else:
                    current = _pack_journal(complete)
                    legacy = {
                        **current,
                        "schema_version": "finding-lifecycle-packed-v2",
                        "native_atoms": [
                            {"kind": kind, "value": value}
                            for kind, value in current["native_atoms"]
                        ],
                    }
                staged = stage_finding_lifecycle(
                    ledger,
                    original,
                    document=legacy,
                    item_count=len(complete["effects"]) + 1,
                    **args,
                )
                cold = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                publisher = ReviewPublisher(
                    http=restart.PersistentNativeService(
                        root / "service.json", binding["head_sha"]
                    ).http
                )
                record, restored_document = restore_finding_lifecycle(
                    publisher, cold, restart.IDENTITY
                )
                self.assertEqual(record.record_sha256, staged.record_sha256)
                self.assertEqual(restored_document, complete)
                journal = FindingLifecycleJournal(
                    publisher,
                    cold,
                    restart.IDENTITY,
                    record=record,
                    document=restored_document,
                    app_slug="reviewsensei[bot]",
                    **args,
                )
                journal.checkpoint()
                after_ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                migrated = after_ledger.load(restart.IDENTITY).record
                packed = read_finding_lifecycle(after_ledger, migrated)
                self.assertEqual(
                    packed["schema_version"], "finding-lifecycle-packed-v3"
                )
                after = _unpack_journal(packed)
                self.assertEqual(after, complete)
                self.assertEqual(after["native_receipts"], complete["native_receipts"])
                self.assertEqual(after["effects"], complete["effects"])
                self.assertEqual(before_service, (root / "service.json").read_bytes())
                for name in (
                    "completed_initial_reviews",
                    "completed_verification_rounds",
                    "failed_attempts",
                    "reservation_id",
                    "reserved_slot",
                    "last_committed_reservation_id",
                    "reservation_owner",
                    "transaction",
                    "api_run_accounting",
                    "continuation_grants",
                    "convergence_history",
                    "dispositions",
                    "assessment_queue",
                    "publication_staging",
                ):
                    with self.subTest(mode=mode, retained=name):
                        self.assertEqual(
                            getattr(original, name), getattr(migrated, name)
                        )
                before_state = decode_lifecycle_state(complete["lifecycle"]).state
                after_state = decode_lifecycle_state(after["lifecycle"]).state
                if mode == "window":
                    before_items = (
                        before_state.authoritative_decision_inventory.obligations
                    )
                    after_items = (
                        after_state.authoritative_decision_inventory.obligations
                    )
                    comments = [
                        (left.comment, right.comment)
                        for left, right in zip(before_items, after_items, strict=True)
                    ]

                    def criteria(ob):
                        return ob.resolution_criterion
                else:
                    before_items = before_state.inventory.obligations
                    after_items = after_state.inventory.obligations
                    comments = [
                        (left.latest_comment, right.latest_comment)
                        for left, right in zip(before_items, after_items, strict=True)
                    ]

                    def criteria(ob):
                        return ob.criterion.resolution_criterion

                self.assertEqual(
                    [criteria(ob) for ob in before_items],
                    [criteria(ob) for ob in after_items],
                )
                self.assertEqual(len(fields(ReviewComment)), 13)
                for before_comment, after_comment in comments:
                    for field in fields(ReviewComment):
                        with self.subTest(mode=mode, field=field.name):
                            self.assertEqual(
                                getattr(before_comment, field.name),
                                getattr(after_comment, field.name),
                            )
                    self.assertNotIn("effective_blocking", after_comment.to_dict())
                    self.assertNotIn("needs_human", after_comment.to_dict())
                # The dataclass intentionally excludes the comment payload
                # from equality. Migration must prove each field instead.
                item = after_items[0]
                payload_name = "comment" if mode == "window" else "latest_comment"
                changed_comment = replace(
                    getattr(item, payload_name),
                    needs_human=not getattr(item, payload_name).needs_human,
                )
                changed = replace(item, **{payload_name: changed_comment})
                self.assertEqual(item, changed)
                self.assertNotEqual(
                    getattr(item, payload_name).needs_human,
                    getattr(changed, payload_name).needs_human,
                )

    def test_authenticated_v1_missing_admission_field_refuses_without_migration(self):
        from review_sensei_github import ReviewPublisher

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LocalSessionLedger(root, enable_finding_lifecycle_writes=True)
            initial = ledger.initialize(IDENTITY)
            tx = ReviewTransaction.create(
                repository=REPO,
                pull_request=1,
                base_sha=BASE,
                head_sha=HEAD,
                policy_digest="2" * 64,
                configuration_digest="3" * 64,
                evidence_digest="4" * 64,
                reservation_id="5" * 64,
                generation=1,
            )
            record = ledger.replace(
                IDENTITY,
                lambda r: r.evolve(
                    generation=initial.generation + 1,
                    reservation_id="5" * 64,
                    reserved_slot="initial",
                    transaction=tx,
                ),
            )
            malformed = document()
            del malformed["lifecycle"]["state"]["authoritative_decision_inventory"][
                "obligations"
            ][0]["comment"]["needs_human"]
            staged = stage_finding_lifecycle(
                ledger,
                record,
                document={
                    "schema_version": "finding-lifecycle-packed-v1",
                    "journal": encode_evidence(malformed, max_decoded_bytes=1_048_576),
                },
                item_count=1,
                operation_id=tx.transaction_id,
                base_sha=BASE,
                head_sha=HEAD,
                policy_digest=tx.policy_digest,
                configuration_digest=tx.configuration_digest,
            )
            before = {
                p.relative_to(root): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            http = FakeHTTP((200, []))
            with self.assertRaises(ReviewInputError):
                restore_finding_lifecycle(
                    ReviewPublisher(http=http),
                    LocalSessionLedger(root, enable_finding_lifecycle_writes=True),
                    IDENTITY,
                )
            after = {
                p.relative_to(root): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)
            self.assertEqual(http.calls, [])
            self.assertEqual(
                LocalSessionLedger(root, enable_finding_lifecycle_writes=True)
                .load(IDENTITY)
                .record.record_sha256,
                staged.record_sha256,
            )
