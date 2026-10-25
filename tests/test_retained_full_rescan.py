"""Actual full-rescan resolver/reader on a retained-only generation16 root."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.default_human_inventory import (
    HumanInventoryReference,
    HumanInventoryStore,
    build_human_inventory_context,
)
from review_sensei_github.default_human_reader import (
    DefaultHumanInventoryReader,
    transaction_publication_digest,
)
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from review_sensei_github.rescan import resolve_rescan
from review_sensei_github.retained_publication_source import (
    RetainedPublishedInventorySource,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.human_inventory import inventory_from_result_document
from review_sensei.rescan import AuthorizedRescan, consume_rescan_request, rescan_source
from review_sensei.session import SessionLoadResult
from tests import test_default_human_rescan as rescan_fixture
from tests.default_human_inventory_contract import APP, HEAD, IDENTITY


class RetainedFullRescanTests(unittest.TestCase):
    def prepare(self, directory):
        state, ledger, env, args = rescan_fixture.DefaultHumanRescanTests().prepare(
            directory, count=20, detail=1
        )
        current = ledger.load(IDENTITY).record
        source = current.transaction
        result = ledger.load_retained_checkpoint_result(
            IDENTITY, transaction=source, head_sha=HEAD
        )
        # Complete-inventory publication already stages these exact parts even
        # when a small inventory also fits the inline marker. Select its valid
        # referenced wire form so the actual reader is mandatory on every OS.
        import re

        context = build_human_inventory_context(
            http=state.http,
            token=ledger.token,
            ledger=ledger,
            identity=IDENTITY,
            base_sha=source.base_sha,
            head_sha=HEAD,
            app_slug=APP,
            publication_digest=transaction_publication_digest(source),
            policy_digest=source.policy_digest,
            configuration_digest=source.configuration_digest,
            evidence_digest=source.evidence_digest,
            budget=EvidenceReadBudget(),
        )
        reference, document = HumanInventoryStore(
            state.http, ledger.token, ledger, context
        ).read_available()
        inline = approval_eligibility_from_body(state.reviews[-1]["body"])
        inventory = inventory_from_result_document(
            document["human_inventory"],
            result_digest=context.publication_digest,
            base_sha=context.base_sha,
        )
        marker = approval_eligibility_marker(
            replace(inline, human_review=inventory, human_inventory_reference=reference)
        )
        state.reviews[-1]["body"] = re.sub(
            r"<!-- reviewsensei:eligibility:v1 [A-Za-z0-9_=-]+ -->",
            lambda _match: marker,
            state.reviews[-1]["body"],
        )
        self.assertIsNone(approval_eligibility_from_body(state.reviews[-1]["body"]))
        decisions = [
            {
                "fingerprint": finding_instance_fingerprint(comment),
                "action": "accept-risk",
                "reason": "Synthetic retained decision",
                "actor": "alice",
                "head_sha": HEAD,
                "expires_at": None,
                "actor_role": "admin",
                "finding_reference": "RS-"
                + finding_instance_fingerprint(comment)[:6].upper(),
                "recorded_at": "2026-10-19T01:00:00Z",
                "policy_digest": source.policy_digest,
                "provenance": "comment:9",
                "resolution_kind": "accepted_unresolved",
                "target_kind": "finding",
            }
            for comment in result.comments[:20]
        ]
        retained = consume_rescan_request(
            current,
            AuthorizedRescan(
                "issue_comment", 9, source, state.reviews[-1]["id"], mode="remaining"
            ),
            "e" * 64,
        ).evolve(
            generation=16,
            transaction=None,
            reservation_id=None,
            reserved_slot=None,
            failed_attempts=1,
            failed_attempts_head_sha=HEAD,
            completed_initial_reviews=0,
            completed_verification_rounds=0,
            dispositions=decisions,
        )
        ledger.replace(IDENTITY, lambda _record: retained)
        return state, ledger, env, args, source

    def run_rescan(self, ledger, env, args):
        with (
            patch.dict(os.environ, env),
            patch.object(
                ledger,
                "replace",
                side_effect=AssertionError(
                    "rescan admission must not mutate the ledger"
                ),
            ),
        ):
            return resolve_rescan(args, ledger, actions_token="synthetic-actions")

    def test_full_rescan_accepts_gen16_retained_source_without_restoring_transaction(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            state, ledger, env, args, source = self.prepare(directory)
            before = ledger.load(IDENTITY).record.to_dict()
            self.assertEqual(before["generation"], 16)
            self.assertEqual(len(before["dispositions"]), 20)
            self.assertNotIn("transaction", before)
            self.assertIsNone(before["reservation_id"])
            self.assertEqual(rescan_source(ledger.load(IDENTITY).record, HEAD), source)
            offset = len(state.rescan_calls)
            request = self.run_rescan(ledger, env, args)
            self.assertEqual(request.mode, "full")
            self.assertEqual(request.source, source)
            self.assertEqual(request.review_id, state.reviews[-1]["id"])
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)
            self.assertTrue(
                all(method == "GET" for method, _ in state.rescan_calls[offset:])
            )
            self.assertTrue(
                any(
                    urlsplit(url).path.endswith("/issues/1/comments")
                    for _, url in state.rescan_calls[offset:]
                )
            )

    def test_changed_receipt_digest_or_producer_cannot_authorize_full_rescan(self):
        for fault in ("receipt", "review-id", "producer", "root-producer", "part"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state, ledger, env, args, _source = self.prepare(directory)
                if fault in {"receipt", "review-id"}:
                    changed = copy.deepcopy(ledger.load(IDENTITY).record.rescan_state)
                    if fault == "receipt":
                        changed["receipts"][-1]["source"]["result_sha256"] = "f" * 64
                    else:
                        changed["receipts"][-1]["review_id"] += 1
                    ledger.replace(IDENTITY, lambda r: r.evolve(rescan_state=changed))
                elif fault == "producer":
                    state.app_user["id"] += 1
                elif fault == "root-producer":
                    state.reviews[-1]["user"]["id"] += 1
                else:
                    from tests.default_human_inventory_contract import (
                        human_inventory_parts,
                    )

                    state.parts.remove(human_inventory_parts(state)[0])
                before = ledger.load(IDENTITY).record.to_dict()
                with self.assertRaises(ReviewInputError):
                    self.run_rescan(ledger, env, args)
                self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)

    def test_active_reservation_cannot_lend_retained_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            _state, ledger, env, args, _source = self.prepare(directory)
            ledger.replace(
                IDENTITY,
                lambda r: r.evolve(reservation_id="a" * 64, reserved_slot="initial"),
            )
            before = ledger.load(IDENTITY).record.to_dict()
            with self.assertRaises(ReviewInputError):
                self.run_rescan(ledger, env, args)
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)

    def test_exact_receipt_review_must_retain_its_own_visible_explanation(self):
        with tempfile.TemporaryDirectory() as directory:
            state, ledger, env, args, _source = self.prepare(directory)
            receipt_review = state.reviews[-1]
            earlier = copy.deepcopy(receipt_review)
            earlier["id"] -= 1
            state.reviews.insert(0, earlier)
            earlier_inline = copy.deepcopy(state.inline)
            for item in earlier_inline:
                item["pull_request_review_id"] = earlier["id"]
            state.inline[:] = earlier_inline
            before = ledger.load(IDENTITY).record.to_dict()
            with self.assertRaises(ReviewInputError):
                self.run_rescan(ledger, env, args)
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)

    def test_root_change_during_inventory_authentication_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            state, ledger, env, args, _source = self.prepare(directory)
            original = state.http.opener
            changed = []

            def opener(request, timeout):
                if not changed and urlsplit(request.full_url).path.startswith(
                    "/users/"
                ):
                    changed.append(True)
                    ledger.replace(
                        IDENTITY, lambda r: r.evolve(generation=r.generation + 1)
                    )
                return original(request, timeout)

            state.http.opener = opener
            with patch.dict(os.environ, env), self.assertRaises(ReviewInputError):
                resolve_rescan(args, ledger, actions_token="synthetic-actions")
            self.assertTrue(changed)
            self.assertEqual(len(ledger.load(IDENTITY).record.dispositions), 20)

    def test_ordinary_reader_keeps_current_transaction_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            state, ledger, _env, args, _source = self.prepare(directory)
            reader = DefaultHumanInventoryReader(
                http=state.http,
                ledger=ledger,
                token=ledger.token,
                repository=args.repository,
                pull_request=args.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                budget=EvidenceReadBudget(),
            )
            self.assertIsNone(
                approval_eligibility_from_body(
                    state.reviews[-1]["body"], human_inventory_reader=reader
                )
            )
            with self.assertRaisesRegex(
                ReviewInputError, "independent checkpoint unavailable"
            ):
                reader(HumanInventoryReference("a" * 64 + "\t" + "b" * 64, "c" * 64))

    def test_root_change_during_visible_explanation_read_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            state, ledger, env, args, _source = self.prepare(directory)
            original = state.http.opener
            changed = []

            def opener(request, timeout):
                if not changed and urlsplit(request.full_url).path.endswith(
                    f"/reviews/{state.reviews[-1]['id']}/comments"
                ):
                    changed.append(True)
                    ledger.replace(
                        IDENTITY, lambda r: r.evolve(generation=r.generation + 1)
                    )
                return original(request, timeout)

            state.http.opener = opener
            with patch.dict(os.environ, env), self.assertRaises(ReviewInputError):
                resolve_rescan(args, ledger, actions_token="synthetic-actions")
            self.assertTrue(changed)
            self.assertEqual(len(ledger.load(IDENTITY).record.dispositions), 20)

    def test_retained_inventory_is_read_only_and_cached_reads_revalidate_root(self):
        with tempfile.TemporaryDirectory() as directory:
            state, ledger, _env, args, source = self.prepare(directory)
            loaded = ledger.load(IDENTITY)
            proof = RetainedPublishedInventorySource.from_checkpoint(loaded, source)
            reader = DefaultHumanInventoryReader(
                http=state.http,
                ledger=ledger,
                token=ledger.token,
                repository=args.repository,
                pull_request=args.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                budget=EvidenceReadBudget(),
                _retained_source=proof,
            )
            eligibility = approval_eligibility_from_body(
                state.reviews[-1]["body"], human_inventory_reader=reader
            )
            self.assertIsNotNone(eligibility)
            before = ledger.load(IDENTITY).record.to_dict()
            calls = len(state.rescan_calls)
            for allow in (False, True):
                with self.assertRaisesRegex(ReviewInputError, "permits reads only"):
                    reader.store.stage({}, allow_start=allow)
            self.assertEqual(len(state.rescan_calls), calls)
            self.assertEqual(ledger.load(IDENTITY).record.to_dict(), before)
            ordinary = DefaultHumanInventoryReader(
                http=state.http,
                ledger=ledger,
                token=ledger.token,
                repository=args.repository,
                pull_request=args.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                budget=reader.budget,
            )
            with self.assertRaisesRegex(ReviewInputError, "handoff scope conflicts"):
                ordinary.attach_authenticated_store(reader.store)
            ledger.replace(IDENTITY, lambda r: r.evolve(generation=r.generation + 1))
            with self.assertRaises(ReviewInputError):
                reader(reader.reference)

    def test_receipt_reference_requires_exact_latest_applicable_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            _state, ledger, _env, _args, source = self.prepare(directory)
            original = ledger.load(IDENTITY)
            proof = RetainedPublishedInventorySource.from_checkpoint(original, source)
            changed = copy.deepcopy(original.record.rescan_state)
            changed["receipts"][-1]["review_id"] += 1
            altered = original.record.evolve(rescan_state=changed)
            with self.assertRaises(ReviewInputError):
                proof.select(SessionLoadResult("ok", record=altered))
            with self.assertRaises(ReviewInputError):
                RetainedPublishedInventorySource.from_checkpoint(
                    original, replace(source, phase="publication_failed")
                )

    def test_receipt_selection_uses_latest_for_this_head_and_never_an_older_same_head(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            _state, ledger, _env, _args, source = self.prepare(directory)
            loaded = ledger.load(IDENTITY)
            changed = copy.deepcopy(loaded.record.rescan_state)
            other_scope = {
                name: getattr(source, name)
                for name in (
                    "repository",
                    "pull_request",
                    "base_sha",
                    "head_sha",
                    "policy_digest",
                    "configuration_digest",
                    "evidence_digest",
                    "reservation_id",
                )
            }
            other_scope.update(head_sha="f" * 40, reservation_id="f" * 64)
            other = replace(
                source, **other_scope, transaction_id=source.derive_id(**other_scope)
            )
            later = copy.deepcopy(changed["receipts"][-1])
            later.update(
                request_id=11,
                reservation="f" * 64,
                ordinary_reservation="f" * 64,
                source=other.to_dict(),
            )
            changed["issue_comment"] = 11
            changed["receipts"].append(later)
            root = loaded.record.evolve(rescan_state=changed)
            scope = SessionLoadResult("ok", record=root)
            proof = RetainedPublishedInventorySource.from_checkpoint(scope, source)
            self.assertEqual(proof.select(scope), source)
            later["source"] = replace(source, result_sha256="f" * 64).to_dict()
            root = loaded.record.evolve(rescan_state=changed)
            with self.assertRaises(ReviewInputError):
                RetainedPublishedInventorySource.from_checkpoint(
                    SessionLoadResult("ok", record=root), source
                )
