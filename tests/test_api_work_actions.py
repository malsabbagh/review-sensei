"""Native durable action intent and terminal proof without review transactions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.request import Request

from review_sensei_github.api_run_history import (
    read_action_result,
    read_previous_run,
    retained_binding,
)
from review_sensei_github.api_run_requests import (
    ApiRunRequestTransport,
    ApiRunSessionLedger,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    initial_accounting,
)
from review_sensei.bounded_evidence import (
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from tests.test_api_work_lifecycle import LifecycleFixture
from tests.test_api_work_persistence import BASE, HEAD, IDENTITY

SOURCE = {
    "kind": "issue_comment",
    "id": 41,
    "updated_at": "2026-10-25T12:00:00Z",
    "body_sha256": "c" * 64,
}
OUTCOME = {"kind": "applied", "reason": None}


class NativeActionTests(unittest.TestCase):
    def prepare(self, directory):
        f = LifecycleFixture(directory, limit=1024)
        native, configuration, _evidence, owner = f.prelude()
        f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
        ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
        return f, ledger

    def admit(self, f, ledger, purpose="command"):
        return f.accounted.admit_action(
            ledger,
            IDENTITY,
            purpose=purpose,
            source=SOURCE,
            head_sha=HEAD,
            operation_key="d" * 64,
        )

    def complete(self, f, ledger, *, outcome=OUTCOME):
        intent = self.admit(f, ledger)
        boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
        ledger.replace(
            IDENTITY,
            lambda record: record.evolve(
                generation=record.generation + 1, operator_paused=True
            ),
        )
        proof = f.accounted.settle_action_effect(
            ledger,
            IDENTITY,
            boundary,
            outcome=outcome,
            result_document={"action": "pause", "applied": True},
        )
        root = f.accounted.close_action(ledger, IDENTITY, proof)
        return intent, boundary, proof, root

    def test_applied_and_explicit_noop_are_retained_without_review_transaction(self):
        for outcome in (
            OUTCOME,
            {"kind": "already_applied", "reason": None},
            {"kind": "explicit_noop", "reason": "status"},
        ):
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as directory,
            ):
                f, ledger = self.prepare(directory)
                intent, _boundary, proof, root = self.complete(
                    f, ledger, outcome=outcome
                )
                self.assertIsNone(root.transaction)
                self.assertEqual(
                    ApiRunAccounting.from_document(root.api_run_accounting).action[
                        "intent"
                    ],
                    intent,
                )
                self.assertEqual(
                    read_action_result(ledger, IDENTITY, root)["outcome"], outcome
                )
                writes = sum(method == "PATCH" for _, method, _ in f.trace)
                f.accounted.close_action(ledger, IDENTITY, proof)
                self.assertEqual(
                    sum(method == "PATCH" for _, method, _ in f.trace), writes
                )
                self.assertEqual(f.accounted.accounting.configuration.limit, 1024)
                self.assertEqual(
                    f.accounted.accounting.remaining
                    + f.accounted.accounting.startup.liability
                    + f.accounted.accounting.confirmed
                    + f.accounted.accounting.uncertain
                    + f.accounted.accounting.reserved_undispatched,
                    1024,
                )

    def test_intent_precedes_effect_and_source_edit_refuses_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            f, ledger = self.prepare(directory)
            prior = f.accounted._root
            intent = self.admit(f, ledger)
            self.assertEqual(intent["source_root"], prior)
            action = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            ).api_run_accounting["action"]
            self.assertEqual(action["phase"], "pending")
            self.assertFalse(action["execution_started"])
            ledger.replace(
                IDENTITY, lambda record: record.evolve(generation=record.generation + 1)
            )
            self.assertEqual(self.admit(f, ledger), intent)
            before = len(f.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "source or operation conflicts"
            ):
                f.accounted.admit_action(
                    ledger,
                    IDENTITY,
                    purpose="command",
                    source={**SOURCE, "body_sha256": "e" * 64},
                    head_sha=HEAD,
                    operation_key="d" * 64,
                )
            self.assertEqual(len(f.trace), before)
            f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            persisted = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            ).api_run_accounting["action"]
            self.assertTrue(persisted["execution_started"])
            with self.assertRaisesRegex(ReviewInputError, "may have started"):
                f.accounted.begin_action_effect(ledger, IDENTITY, intent)

    def test_unknown_mutation_and_unsettled_provider_prevent_completion(self):
        for fault in ("mutation", "provider"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, ledger = self.prepare(directory)
                intent = self.admit(f, ledger)
                boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
                if fault == "provider":
                    f.accounted._provider_effect_unresolved = True
                else:
                    f.fault = "unknown-absent"
                    with self.assertRaises(Exception):
                        f.accounted.open(
                            Request(
                                "https://api.github.test/repos/owner/repo/issues/comments/1000000",
                                data=json.dumps({"body": f.comment["body"]}).encode(),
                                method="PATCH",
                            ),
                            timeout=5,
                        )
                before = len(f.trace)
                with self.assertRaisesRegex(ReviewInputError, "confirmed outcome"):
                    f.accounted.settle_action_effect(
                        ledger,
                        IDENTITY,
                        boundary,
                        outcome=OUTCOME,
                        result_document={"applied": True},
                    )
                self.assertEqual(len(f.trace), before)
                self.assertEqual(f.accounted.accounting.action["phase"], "pending")

    def test_lost_intent_entry_and_terminal_writes_read_back_once_without_replay(self):
        for stage in ("intent", "entry", "terminal"):
            for fault in ("unknown-stored", "unknown-absent"):
                with (
                    self.subTest(stage=stage, fault=fault),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f, ledger = self.prepare(directory)
                    if stage == "intent":

                        def operation():
                            return self.admit(f, ledger)
                    else:
                        intent = self.admit(f, ledger)
                        if stage == "entry":

                            def operation():
                                return f.accounted.begin_action_effect(
                                    ledger, IDENTITY, intent
                                )
                        else:
                            boundary = f.accounted.begin_action_effect(
                                ledger, IDENTITY, intent
                            )
                            proof = f.accounted.settle_action_effect(
                                ledger,
                                IDENTITY,
                                boundary,
                                outcome=OUTCOME,
                                result_document={"applied": True},
                            )

                            def operation():
                                return f.accounted.close_action(ledger, IDENTITY, proof)

                    before = sum(method == "PATCH" for _, method, _ in f.trace)
                    f.fault = fault
                    if fault == "unknown-stored":
                        operation()
                    else:
                        with self.assertRaisesRegex(ReviewInputError, "unconfirmed"):
                            operation()
                    self.assertEqual(
                        sum(method == "PATCH" for _, method, _ in f.trace), before + 1
                    )
                    self.assertGreaterEqual(f.accounted.accounting.uncertain, 1)
                    if fault == "unknown-absent":
                        with self.assertRaises(ReviewInputError):
                            f.accounted.reconcile_checkpoint(ledger, IDENTITY)
                        self.assertEqual(
                            sum(method == "PATCH" for _, method, _ in f.trace),
                            before + 1,
                        )

    def test_changed_result_or_unknown_noop_cannot_mint_terminal_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            f, ledger = self.prepare(directory)
            intent = self.admit(f, ledger)
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            before = len(f.trace)
            with self.assertRaisesRegex(ReviewInputError, "outcome"):
                f.accounted.settle_action_effect(
                    ledger,
                    IDENTITY,
                    boundary,
                    outcome={"kind": "explicit_noop", "reason": "provider_error"},
                    result_document={},
                )
            self.assertEqual(len(f.trace), before)
            proof = f.accounted.settle_action_effect(
                ledger,
                IDENTITY,
                boundary,
                outcome=OUTCOME,
                result_document={"applied": True},
            )
            f.accounted.close_action(ledger, IDENTITY, proof)
            forged = replace(proof, outcome={"kind": "already_applied", "reason": None})
            with self.assertRaisesRegex(ReviewInputError, "settled result proof"):
                f.accounted.close_action(ledger, IDENTITY, forged)
            state = f.accounted.accounting
            with self.assertRaisesRegex(
                ReviewInputError, "completion cannot be replaced"
            ):
                state.require_successor(
                    replace(
                        state,
                        action={
                            **state.action,
                            "outcome": {"kind": "explicit_noop", "reason": "status"},
                        },
                    )
                )

    def test_owner_succession_preserves_intent_and_opaque_predecessor_charges(self):
        with tempfile.TemporaryDirectory() as directory:
            f, ledger = self.prepare(directory)
            intent = self.admit(f, ledger)
            old_blocks = ApiRunAccounting.from_document(
                parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).api_run_accounting
            ).blocks
            native = f.recover_lifecycle()
            successor = ApiRunSessionLedger(native, f.accounted, IDENTITY)
            self.assertEqual(self.admit(f, successor), intent)
            self.assertEqual(
                f.accounted.accounting.action["intent"]["owner"], intent["owner"]
            )
            for block in old_blocks:
                self.assertIn(block, f.accounted.accounting.blocks)
            boundary = f.accounted.begin_action_effect(successor, IDENTITY, intent)
            proof = f.accounted.settle_action_effect(
                successor,
                IDENTITY,
                boundary,
                outcome=OUTCOME,
                result_document={"applied": True},
            )
            f.accounted.close_action(successor, IDENTITY, proof)
            self.assertEqual(f.accounted.accounting.action["phase"], "terminal")

    def test_mutated_settled_result_and_changed_owner_root_refuse_before_terminal_write(
        self,
    ):
        from review_sensei_github.session_ledger import render_session_comment

        for fault in ("result", "owner"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, ledger = self.prepare(directory)
                intent = self.admit(f, ledger)
                boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
                proof = f.accounted.settle_action_effect(
                    ledger,
                    IDENTITY,
                    boundary,
                    outcome=OUTCOME,
                    result_document={"applied": True},
                )
                if fault == "result":
                    proof.manifest["document"]["payload"]["sha256"] = "a" * 64
                    proof.manifest["sha256"] = hashlib.sha256(
                        canonical_bytes(proof.manifest["document"])
                    ).hexdigest()
                else:
                    root = parse_session_comment(f.comment["body"], identity=IDENTITY)
                    state = ApiRunAccounting.from_document(root.api_run_accounting)
                    takeover = state.claim_startup(ApiExecutionOwner(2, "hosted", 1))
                    changed = root.evolve(
                        generation=root.generation + 1,
                        api_run_accounting=takeover.to_document(),
                    )
                    f.comment["body"] = render_session_comment(
                        repository_id=123, pull_request=7, record=changed
                    )
                before = sum(method == "PATCH" for _, method, _ in f.trace)
                with self.assertRaises(ReviewInputError):
                    f.accounted.close_action(ledger, IDENTITY, proof)
                self.assertEqual(
                    sum(method == "PATCH" for _, method, _ in f.trace), before
                )

    def test_inherited_completed_review_cannot_close_pending_action(self):
        with tempfile.TemporaryDirectory() as directory:
            f, ledger = self.prepare(directory)
            self.admit(f, ledger)
            root = parse_session_comment(f.comment["body"], identity=IDENTITY)
            inherited = replace(
                ReviewTransaction.create(
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_digest="e" * 64,
                    configuration_digest="f" * 64,
                    evidence_digest="a" * 64,
                    reservation_id="b" * 64,
                    generation=1,
                ),
                phase="publication_succeeded",
                result_sha256="c" * 64,
            )
            root = root.evolve(
                transaction=inherited,
                last_committed_reservation_id=inherited.reservation_id,
            )
            self.assertEqual(root.transaction.phase, "publication_succeeded")
            with self.assertRaisesRegex(ReviewInputError, "unfinished action"):
                retained_binding(root, ledger.evidence_producer())
            state = f.accounted.accounting
            candidate = initial_accounting(
                ApiRunIdentity(123, 457),
                state.configuration,
                ApiExecutionOwner(1, "hosted", 0),
                state.startup,
                prior_nonstartup_liability_upper_bound=0,
            )
            # A syntactically valid history carrier must not make pending action
            # intent terminal merely because an inherited review completed.
            binding = {
                "repository": IDENTITY.repository,
                "repository_id": IDENTITY.repository_id,
                "pull_request": IDENTITY.pull_request,
                "base_sha": BASE,
                "head_sha": HEAD,
                "policy_digest": "e" * 64,
                "configuration_digest": state.configuration.digest,
                "generation": root.generation,
                "producer": ledger.evidence_producer(),
                "purpose": "evidence",
                "schema_version": "1.0",
            }
            document = {
                "schema_version": "caller-run-root-v1",
                "record": root.to_dict(),
            }
            parts = partition_evidence(document, binding=binding, item_count=1)
            manifest = evidence_manifest(
                document,
                binding=binding,
                item_count=1,
                parts=parts,
                storage_ids=("2000000",),
            )
            candidate = replace(
                candidate,
                previous_record={
                    "run_id": 456,
                    "root_sha256": root.record_sha256,
                    "manifest": manifest,
                },
            )
            with self.assertRaisesRegex(ReviewInputError, "not terminal"):
                root.rollover_api_accounting(candidate.to_document())

    def test_terminal_actions_archive_exact_roots_across_three_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            f, ledger = self.prepare(directory)
            _intent, _boundary, _proof, first = self.complete(f, ledger)
            first_state = ApiRunAccounting.from_document(first.api_run_accounting)
            prior_roots = [first]
            for run_id in (457, 458):
                before = prior_roots[-1]
                config = f.accounted.accounting.configuration
                f.trace = []
                owner_directory = Path(directory) / str(run_id)
                owner_directory.mkdir()
                caller = ApiRunRequestTransport(
                    directory=owner_directory,
                    run=ApiRunIdentity(123, run_id),
                    plan=f.plan,
                    attempt=1,
                    opener=f._external_open,
                )
                f.accounted = caller
                http = GitHubHttp(api_url="https://api.github.test", opener=caller.open)
                native = GitHubIssueCommentSessionLedger(
                    http, token="synthetic", app_slug="reviewsensei[bot]"
                )
                loaded = native.load(IDENTITY)
                self.assertEqual(loaded.record.record_sha256, before.record_sha256)
                caller.rollover_and_activate(
                    native,
                    IDENTITY,
                    config,
                    ApiExecutionOwner(1, "hosted", 0),
                    before,
                    lambda: None,
                )
                ledger = ApiRunSessionLedger(native, caller, IDENTITY)
                archived = read_previous_run(
                    ledger, IDENTITY, caller.accounting.previous_record
                )
                self.assertEqual(archived.to_dict(), before.to_dict())
                self.assertEqual(
                    ApiRunAccounting.from_document(
                        archived.api_run_accounting
                    ).identity.run_id,
                    run_id - 1,
                )
                _intent, _boundary, _proof, complete = self.complete(f, ledger)
                prior_roots.append(complete)
            second = read_previous_run(
                ledger, IDENTITY, f.accounted.accounting.previous_record
            )
            oldest = read_previous_run(
                ledger,
                IDENTITY,
                ApiRunAccounting.from_document(
                    second.api_run_accounting
                ).previous_record,
            )
            self.assertEqual(oldest.to_dict(), first.to_dict())
            self.assertEqual(
                ApiRunAccounting.from_document(oldest.api_run_accounting).blocks,
                first_state.blocks,
            )


if __name__ == "__main__":
    unittest.main()
