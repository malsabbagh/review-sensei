"""Exact review-body update intents never replay an unknown native write."""

from __future__ import annotations

import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_journal as fixture

PATH = fixture.PATH + "/1201"
BODY = {"body": "Updated current eligibility 審査"}


class ReviewUpdateJournalTests(unittest.TestCase):
    def body_journal(self, *, retained=None):
        from review_sensei_github.publication import NativeFindingReceipt

        journal = fixture.journal(retained=retained)
        journal._record = SimpleNamespace(record_sha256="a" * 64)
        receipt = NativeFindingReceipt(
            repository_id=7,
            pull_request=1,
            head_sha=fixture.HEAD,
            base_sha=fixture.BASE,
            producer_id=9001,
            review_id=1201,
            instance="a" * 64,
            body_sha256=hashlib.sha256(fixture.REQUEST["body"].encode()).hexdigest(),
        ).to_dict()
        records = {receipt["instance"]: receipt}
        journal.reviewer._native_finding_receipt_records = {(7, 1): records}
        journal.reviewer.native_finding_receipts = lambda **kw: tuple(records.values())
        return journal, receipt

    def test_body_refresh_changes_only_matching_binding_proofs(self):
        from dataclasses import asdict, replace

        from review_sensei_github.publication import NativeFindingReceipt

        from review_sensei.active_finding_window import commit_window_publication
        from review_sensei.finding_drift import commit_drift_publication
        from tests.test_finding_lifecycle_codec import active_state, drift_state

        journal, old = self.body_journal()
        receipt = NativeFindingReceipt(
            **{k: v for k, v in old.items() if k != "schema_version"}
        )
        updated = {**old, "body_sha256": "f" * 64}
        new_receipt = NativeFindingReceipt(
            **{k: v for k, v in updated.items() if k != "schema_version"}
        )
        window = active_state()
        window = commit_window_publication(
            window,
            created_targets={
                window.display_active_findings.entries[0].obligation_id: receipt.target
            },
        )
        inventory = window.authoritative_decision_inventory
        window = replace(
            window,
            authoritative_decision_inventory=replace(
                inventory,
                obligations=tuple(
                    replace(o, target_proof_sha256=receipt.proof_sha256)
                    if o.native_target == receipt.target
                    else o
                    for o in inventory.obligations
                ),
            ),
            display_active_findings=replace(
                window.display_active_findings,
                slots=tuple(
                    replace(slot, proof_sha256=receipt.proof_sha256)
                    for slot in window.display_active_findings.slots
                ),
            ),
        )
        drift = drift_state()
        drift = commit_drift_publication(
            drift,
            created_threads={
                drift.active_entries[0].obligation_id: (receipt.target, 1201)
            },
        )
        drift = replace(
            drift,
            inventory=replace(
                drift.inventory,
                obligations=tuple(
                    replace(
                        o,
                        thread_binding=replace(
                            o.thread_binding, proof_sha256=receipt.proof_sha256
                        ),
                    )
                    if o.thread_binding
                    else o
                    for o in drift.inventory.obligations
                ),
            ),
            slots=tuple(
                replace(slot, proof_sha256=receipt.proof_sha256) for slot in drift.slots
            ),
        )
        import json

        for state in (window, drift):
            with self.subTest(kind=type(state).__name__):
                refreshed = journal._updated_binding_proof_state(state, old, updated)
                before = json.dumps(asdict(state), sort_keys=True)
                after = json.dumps(asdict(refreshed), sort_keys=True)
                self.assertIn(new_receipt.proof_sha256, after)
                self.assertEqual(
                    after,
                    before.replace(receipt.proof_sha256, new_receipt.proof_sha256),
                )
                unrelated = {**old, "body_sha256": "e" * 64}
                self.assertEqual(
                    asdict(
                        journal._updated_binding_proof_state(state, unrelated, updated)
                    ),
                    asdict(state),
                )

    def observe_body(self, journal, native=None):
        native = fixture.native_review() if native is None else native
        http = fixture.FakeHTTP((200, native))
        journal.request(http, "GET", PATH, token="synthetic")
        return journal.issue_review_update_precondition(native)

    def test_body_receipt_refresh_requires_actual_owned_prewrite_and_exact_postread(
        self,
    ):
        journal, before = self.body_journal()
        proof = self.observe_body(journal)
        http = fixture.FakeHTTP((200, fixture.native_review(**BODY)))
        journal._readback_http = http
        with (
            patch.object(journal, "_preflight_effect"),
            patch.object(journal, "checkpoint"),
        ):
            with journal.prepare_review_update(
                PATH, token="synthetic", body=BODY, precondition=proof
            ):
                self.assertEqual(
                    journal.effects[-1]["review_update_precondition"]["body_sha256"],
                    before["body_sha256"],
                )
                journal.request(http, "PUT", PATH, token="synthetic", body=BODY)
                self.assertEqual(journal._body_receipts(1201)[0], before)
        after = journal._body_receipts(1201)[0]
        self.assertEqual(
            after,
            {
                **before,
                "body_sha256": hashlib.sha256(BODY["body"].encode()).hexdigest(),
            },
        )
        self.assertEqual([m for m, _, _ in http.calls], ["PUT", "GET"])

    def test_unfunded_combined_update_preview_stops_before_any_native_dispatch(self):
        from tests.test_finding_lifecycle_codec import drift_state

        journal, before = self.body_journal()
        proof = self.observe_body(journal)
        preview = drift_state()
        http = fixture.FakeHTTP((200, fixture.native_review(**BODY)))
        journal._readback_http = http
        original_state = copy.deepcopy(journal.reviewer._drift_states)

        def exhausted():
            self.assertEqual(journal._review_update_capacity_states, (preview,))
            self.assertEqual(journal.reviewer._drift_states, original_state)
            self.assertEqual(journal._body_receipts(1201), [before])
            raise ReviewInputError(
                "synthetic complete future checkpoint tail is unfunded"
            )

        with (
            patch.object(journal, "_preflight_effect", side_effect=exhausted),
            patch.object(journal, "checkpoint") as checkpoint,
        ):
            with self.assertRaisesRegex(ReviewInputError, "tail is unfunded"):
                with journal.prepare_review_update(
                    PATH,
                    token="synthetic",
                    body=BODY,
                    precondition=proof,
                    capacity_states=(preview,),
                ):
                    self.fail("unfunded preview must not reach native scope")
            checkpoint.assert_not_called()
        self.assertEqual(http.calls, [])
        self.assertEqual(journal._review_update_capacity_states, ())
        self.assertEqual(journal.reviewer._drift_states, original_state)
        self.assertEqual(journal._body_receipts(1201), [before])

    def test_body_precondition_rejects_clone_missing_and_later_foreign_edit(self):
        for fault in (
            "clone",
            "missing",
            "changed-object",
            "later-edit",
            "foreign-producer",
        ):
            with self.subTest(fault=fault):
                journal, before = self.body_journal()
                native = fixture.native_review()
                http = fixture.FakeHTTP((200, native))
                journal._readback_http = http
                journal.request(http, "GET", PATH, token="synthetic")
                with self.assertRaises(ReviewInputError):
                    if fault == "clone":
                        journal.issue_review_update_precondition(copy.deepcopy(native))
                        continue
                    if fault == "changed-object":
                        native["body"] = "edited"
                        journal.issue_review_update_precondition(native)
                        continue
                    proof = journal.issue_review_update_precondition(native)
                    if fault in {"later-edit", "foreign-producer"}:
                        edited = (
                            fixture.native_review(body="foreign edit")
                            if fault == "later-edit"
                            else fixture.native_review(user={"id": 5, "type": "Bot"})
                        )
                        journal.request(
                            fixture.FakeHTTP((200, edited)),
                            "GET",
                            PATH,
                            token="synthetic",
                        )
                    with journal.prepare_review_update(
                        PATH,
                        token="synthetic",
                        body=BODY,
                        precondition=None if fault == "missing" else proof,
                    ):
                        self.fail("invalid precondition entered")
                self.assertEqual(journal.effects, [])
                self.assertEqual(journal._body_receipts(1201)[0], before)

    def test_issued_precondition_refuses_later_object_mutation_and_failed_fresh_reads(
        self,
    ):
        for fault in (
            "body",
            "producer",
            "head",
            "failed-list",
            "empty-list",
            "exception-list",
        ):
            with self.subTest(fault=fault):
                journal, before = self.body_journal()
                native = fixture.native_review()
                proof = self.observe_body(journal, native)
                journal._readback_http = fixture.FakeHTTP((200, native))
                if fault == "body":
                    native["body"] = "changed after issue"
                elif fault == "producer":
                    native["user"]["id"] = 5
                elif fault == "head":
                    native["commit_id"] = "f" * 40
                else:
                    response = (
                        TimeoutError("fresh read failed")
                        if fault == "exception-list"
                        else ((503, None) if fault == "failed-list" else (200, []))
                    )
                    try:
                        journal.request(
                            fixture.FakeHTTP(response),
                            "GET",
                            fixture.PATH,
                            token="synthetic",
                        )
                    except TimeoutError:
                        pass
                with self.assertRaises(ReviewInputError):
                    with journal.prepare_review_update(
                        PATH, token="synthetic", body=BODY, precondition=proof
                    ):
                        self.fail("stale or changed precondition entered")
                self.assertEqual(journal.effects, [])
                self.assertEqual(journal._body_receipts(1201)[0], before)

    def test_cold_lost_stored_body_put_uses_durable_precondition_and_never_reposts(
        self,
    ):
        journal, before = self.body_journal()
        proof = self.observe_body(journal)
        lost = fixture.FakeHTTP(TimeoutError("response lost after storage"))
        journal._readback_http = lost
        with (
            patch.object(journal, "_preflight_effect"),
            patch.object(journal, "checkpoint"),
        ):
            with self.assertRaises(TimeoutError):
                with journal.prepare_review_update(
                    PATH, token="synthetic", body=BODY, precondition=proof
                ):
                    journal.request(lost, "PUT", PATH, token="synthetic", body=BODY)
        saved = journal._document()
        for fault in (None, "missing", "old-body", "producer", "edited-postbody"):
            with self.subTest(fault=fault):
                retained = copy.deepcopy(saved)
                if fault == "missing":
                    retained["effects"][-1].pop("review_update_precondition")
                elif fault == "old-body":
                    retained["effects"][-1]["review_update_precondition"][
                        "body_sha256"
                    ] = "f" * 64
                elif fault == "producer":
                    retained["effects"][-1]["review_update_precondition"][
                        "producer_id"
                    ] = 5
                cold, _ = self.body_journal(retained=retained)
                native = fixture.native_review(
                    body="foreign edit" if fault == "edited-postbody" else BODY["body"]
                )
                http = fixture.FakeHTTP((200, native))
                with patch.object(cold, "checkpoint"):
                    if fault is None:
                        cold.request(http, "PUT", PATH, token="synthetic", body=BODY)
                        self.assertEqual(cold.effects[-1]["status"], "confirmed")
                        self.assertNotEqual(
                            cold._body_receipts(1201)[0]["body_sha256"],
                            before["body_sha256"],
                        )
                    else:
                        with self.assertRaises(ReviewInputError):
                            cold.request(
                                http, "PUT", PATH, token="synthetic", body=BODY
                            )
                        self.assertEqual(cold.effects[-1]["status"], "pending")
                        self.assertEqual(cold._body_receipts(1201)[0], before)
                self.assertTrue(all(m == "GET" for m, _, _ in http.calls))

    def pending_update(self):
        journal = fixture.journal()
        lost = fixture.FakeHTTP(TimeoutError("lost PUT response"))
        checkpoints = []
        with (
            patch.object(journal, "_preflight_effect"),
            patch.object(
                journal,
                "checkpoint",
                side_effect=lambda **kw: checkpoints.append(
                    copy.deepcopy(journal.effects)
                ),
            ),
        ):
            with self.assertRaises(TimeoutError):
                journal.request(lost, "PUT", PATH, token="synthetic", body=BODY)
        self.assertEqual(len(lost.calls), 1)
        self.assertEqual(checkpoints[0][-1]["status"], "pending")
        return journal

    def test_lost_stored_update_uses_exact_readback_without_repeating_put(self):
        journal = self.pending_update()
        http = fixture.FakeHTTP((200, fixture.native_review(**BODY)))
        with patch.object(journal, "checkpoint"):
            result = journal.request(http, "PUT", PATH, token="synthetic", body=BODY)
        self.assertEqual(result[0], 200)
        self.assertEqual([(m, p) for m, p, _ in http.calls], [("GET", PATH)])
        self.assertEqual(journal.effects[-1]["status"], "confirmed")

    def test_absent_wrong_head_id_producer_and_changed_body_keep_unknown(self):
        for changes in (
            {"id": 1202},
            {"commit_id": "f" * 40},
            {"user": {"id": 7, "type": "Bot"}},
            {"body": "different"},
            {"edited_by_human": True},
        ):
            with self.subTest(changes=changes):
                journal = self.pending_update()
                http = fixture.FakeHTTP(
                    (200, fixture.native_review(**{**BODY, **changes}))
                )
                with patch.object(journal, "checkpoint"):
                    with self.assertRaises(ReviewInputError):
                        journal.request(http, "PUT", PATH, token="synthetic", body=BODY)
                self.assertTrue(all(m == "GET" for m, _, _ in http.calls))
                self.assertEqual(journal.effects[-1]["status"], "pending")

    def test_prepared_update_is_one_use_and_bound_to_current_owned_root(self):
        journal = fixture.journal()
        journal._record = SimpleNamespace(record_sha256="a" * 64)
        http = fixture.FakeHTTP((200, fixture.native_review(**BODY)))
        journal._readback_http = http
        with (
            patch.object(journal, "_preflight_effect"),
            patch.object(journal, "checkpoint"),
        ):
            with journal.prepare_review_update(PATH, token="synthetic", body=BODY):
                self.assertEqual(http.calls, [])
                self.assertEqual(journal.effects[-1]["status"], "pending")
                journal.request(http, "PUT", PATH, token="synthetic", body=BODY)
                journal.request(http, "PUT", PATH, token="synthetic", body=BODY)
        self.assertEqual([m for m, _, _ in http.calls], ["PUT", "GET"])
        self.assertIsNone(journal._prepared_review_update)

    def test_prepared_update_refuses_changed_request_or_root_and_expires_on_crash(self):
        for fault in (
            "request",
            "root",
            "caller-root",
            "before-dispatch",
            "after-dispatch",
        ):
            with self.subTest(fault=fault):
                journal = fixture.journal()
                journal._record = SimpleNamespace(record_sha256="a" * 64)
                http = fixture.FakeHTTP(TimeoutError("native response lost"))
                journal._readback_http = http
                with (
                    patch.object(journal, "_preflight_effect"),
                    patch.object(journal, "checkpoint"),
                ):
                    with self.assertRaises(
                        (ReviewInputError, SystemExit, TimeoutError)
                    ):
                        with journal.prepare_review_update(
                            PATH, token="synthetic", body=BODY
                        ):
                            if fault == "before-dispatch":
                                raise SystemExit(74)
                            if fault == "root":
                                journal._record = SimpleNamespace(
                                    record_sha256="b" * 64
                                )
                            if fault == "caller-root":
                                journal.ledger.caller = SimpleNamespace(_root="b" * 64)
                            journal.request(
                                http,
                                "PUT",
                                PATH,
                                token="synthetic",
                                body={"body": "changed"}
                                if fault == "request"
                                else BODY,
                            )
                self.assertIsNone(journal._prepared_review_update)
                self.assertEqual(len(http.calls), int(fault == "after-dispatch"))
                fresh = fixture.journal(
                    retained=fixture.document(copy.deepcopy(journal.effects))
                )
                fresh._record = SimpleNamespace(record_sha256="a" * 64)
                absent = fixture.FakeHTTP((200, fixture.native_review(body="old body")))
                fresh._readback_http = absent
                with patch.object(fresh, "checkpoint"):
                    with self.assertRaises(ReviewInputError):
                        with fresh.prepare_review_update(
                            PATH, token="synthetic", body=BODY
                        ):
                            self.fail("cold pending intent cannot authorize dispatch")
                self.assertTrue(all(m == "GET" for m, _, _ in absent.calls))
                self.assertEqual(fresh.effects[-1]["status"], "pending")

    def test_other_put_routes_or_shapes_are_rejected_before_dispatch(self):
        for path, body in (
            (PATH.replace("/1/", "/2/"), BODY),
            (PATH + "/comments", BODY),
            (PATH, {**BODY, "state": "APPROVED"}),
        ):
            with self.subTest(path=path, body=body):
                journal = fixture.journal()
                http = fixture.FakeHTTP((200, fixture.native_review(**BODY)))
                with self.assertRaises(ReviewInputError):
                    journal.request(http, "PUT", path, token="synthetic", body=body)
                self.assertEqual(http.calls, [])
