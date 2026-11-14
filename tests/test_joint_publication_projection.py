"""Data-only joint root bounds; no native caller, lease or reader is installed."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from review_sensei.api_work import (
    MAX_API_REQUEST_LIMIT,
    ApiRunIdentity,
    PendingApiRequest,
)
from review_sensei.bounded_evidence import INLINE_ENCODING, canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    _prepare_lifecycle,
    _prepare_lifecycle_mutation,
    project_joint_publication,
    read_finding_lifecycle,
)
from review_sensei.grouped_publication_declarations import GroupedDeclarations
from review_sensei.models import ReviewTransaction
from review_sensei.publication_staging import PublicationReceipt, PublicationStaging
from review_sensei.session import LocalSessionLedger
from tests.test_api_work import accounting
from tests.test_review_transaction import IDENTITY


class JointPublicationProjectionTests(unittest.TestCase):
    def inputs(self, directory):
        ledger = LocalSessionLedger(
            Path(directory), enable_finding_lifecycle_writes=True
        )
        ledger.initialize(IDENTITY)
        tx = ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="1" * 64,
            generation=1,
        ).with_result("2" * 64)
        declared = GroupedDeclarations(
            tx.transaction_id,
            tx.result_sha256,
            "3" * 64,
            ("a" * 32, "b" * 32, "c" * 32),
        )
        prepared = declared.prepare_final(
            part=PublicationReceipt("43", "d" * 64),
            manifest=PublicationReceipt("issue:45", "e" * 64),
            request_sha256="f" * 64,
        )
        stages = (
            None,
            declared,
            prepared,
            prepared.finish(final=PublicationReceipt("44", "f" * 64)),
        )
        record = ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                generation=current.generation + 1,
                transaction=tx,
                last_committed_reservation_id=tx.reservation_id,
            ),
        )
        arguments = dict(
            document={"required": ["retained"], "uncertain": True},
            operation_id=tx.transaction_id,
            base_sha=tx.base_sha,
            head_sha=tx.head_sha,
            policy_digest=tx.policy_digest,
            configuration_digest=tx.configuration_digest,
            item_count=2,
        )
        return ledger, record, arguments, stages

    def test_future_growth_selects_real_partition_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, record, arguments, _ = self.inputs(directory)
            arguments["document"] = {
                "retained": "".join(
                    hashlib.sha256(str(i).encode()).hexdigest() for i in range(20)
                )
            }
            selected = None
            for count in range(100, 191):
                state = (
                    replace(
                        accounting(limit=512),
                        identity=ApiRunIdentity(IDENTITY.repository_id, 456),
                    )
                    .reserve("ordinary", count)
                    .reserve("terminal", 10)
                    .reserve("successor", 190)
                )
                state = state.dispatch("ordinary", PendingApiRequest("old-unknown", 17))
                probe = record.evolve(api_run_accounting=state.to_document())
                normal = _prepare_lifecycle(ledger, probe, **arguments)[-1]
                projection = project_joint_publication(
                    probe, after=None, blocks=("ordinary", "terminal")
                )
                try:
                    joint = _prepare_lifecycle(
                        ledger, probe, **arguments, joint_projection=projection
                    )[-1]
                except ReviewInputError as error:
                    if "root capacity" not in str(error):
                        raise
                    continue
                if (
                    normal.finding_lifecycle["manifest"]["encoding"] == INLINE_ENCODING
                    and joint.finding_lifecycle["manifest"]["encoding"]
                    != INLINE_ENCODING
                ):
                    selected = state
                    break
            self.assertIsNotNone(
                selected, "no genuine inline/partition boundary was found"
            )
            state = selected
            record = ledger.replace(
                IDENTITY,
                lambda current: current.evolve(
                    generation=current.generation + 1,
                    api_run_accounting=state.to_document(),
                ),
            )
            projection = project_joint_publication(
                record, after=None, blocks=("ordinary", "terminal")
            )
            original = canonical_bytes(record.api_run_accounting)
            upper = json.loads(projection.accounting_upper)
            from review_sensei_github.materialized_publication_projection import (
                _future_accounting,
            )

            self.assertEqual(
                upper, _future_accounting(state, ("ordinary", "terminal"))[0]
            )
            blocks = {b["block_id"]: b for b in upper["blocks"]}
            source_blocks = {b["block_id"]: b for b in state.to_document()["blocks"]}
            self.assertEqual(blocks["successor"], source_blocks["successor"])
            self.assertEqual(
                upper["configuration"], state.to_document()["configuration"]
            )
            self.assertIn(
                {"request_id": "old-unknown", "deadline_unix_ms": 17},
                blocks["ordinary"]["pending"],
            )
            for name in ("ordinary", "terminal"):
                self.assertEqual(blocks[name]["undispatched"], 0)
                for pending in blocks[name]["pending"]:
                    if pending["request_id"] != "old-unknown":
                        self.assertEqual(len(pending["request_id"]), 128)
                        self.assertEqual(
                            pending["deadline_unix_ms"], MAX_API_REQUEST_LIMIT
                        )
            candidate, _, mutate = _prepare_lifecycle_mutation(
                ledger,
                record,
                **arguments,
                joint_projection=projection,
            )
            self.assertNotEqual(
                candidate.finding_lifecycle["manifest"]["encoding"], INLINE_ENCODING
            )
            actual = ledger.replace(IDENTITY, mutate)
            self.assertEqual(
                actual.finding_lifecycle["manifest"]["encoding"],
                candidate.finding_lifecycle["manifest"]["encoding"],
            )
            self.assertEqual(
                read_finding_lifecycle(ledger, actual), arguments["document"]
            )
            self.assertEqual(canonical_bytes(actual.api_run_accounting), original)

    def test_edited_projection_or_parent_is_refused_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, record, arguments, stages = self.inputs(directory)
            state = (
                replace(
                    accounting(limit=512),
                    identity=ApiRunIdentity(IDENTITY.repository_id, 456),
                )
                .reserve("ordinary", 20)
                .reserve("terminal", 10)
                .reserve("successor", 190)
            )
            state = state.dispatch(
                "ordinary", PendingApiRequest("retained-unknown", 17)
            )
            record = ledger.replace(
                IDENTITY,
                lambda current: current.evolve(
                    generation=current.generation + 1,
                    api_run_accounting=state.to_document(),
                ),
            )
            projection = project_joint_publication(
                record, after=stages[1], blocks=("ordinary", "terminal")
            )
            altered = json.loads(projection.accounting_upper)
            altered["blocks"][0]["pending"] = []
            variants = (
                replace(projection, parent_root="0" * 64),
                replace(projection, accounting_upper=canonical_bytes(altered)),
                replace(projection, staging=b"{}"),
            )
            staged = []

            def observe(frame, event, value):
                if (
                    event == "call"
                    and frame.f_code is LocalSessionLedger.stage_evidence.__code__
                ):
                    staged.append(True)

            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                for changed in variants:
                    with (
                        self.subTest(changed=changed.parent_root),
                        self.assertRaises(ReviewInputError),
                    ):
                        _prepare_lifecycle_mutation(
                            ledger, record, **arguments, joint_projection=changed
                        )
            finally:
                sys.setprofile(previous)
            self.assertEqual(staged, [])
            self.assertEqual(
                ledger.load(IDENTITY).record.record_sha256, record.record_sha256
            )
            # A copy is still only data. Equality neither issues nor activates a lease.
            self.assertEqual(copy.copy(projection), projection)
            _, _, prepared = _prepare_lifecycle_mutation(
                ledger,
                record,
                **arguments,
                joint_projection=projection,
            )
            object.__setattr__(projection, "accounting_upper", canonical_bytes(altered))
            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                with self.assertRaises(ReviewInputError):
                    prepared(record)
            finally:
                sys.setprofile(previous)
            self.assertEqual(staged, [])
            self.assertEqual(
                ledger.load(IDENTITY).record.record_sha256, record.record_sha256
            )

    def test_coherent_wider_tranche_substitution_never_reaches_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, record, arguments, stages = self.inputs(directory)
            state = (
                replace(
                    accounting(limit=512),
                    identity=ApiRunIdentity(IDENTITY.repository_id, 456),
                )
                .reserve("ordinary", 20)
                .reserve("terminal", 10)
                .reserve("successor", 190)
            )
            record = ledger.replace(
                IDENTITY,
                lambda current: current.evolve(
                    generation=current.generation + 1,
                    api_run_accounting=state.to_document(),
                ),
            )
            projection = project_joint_publication(
                record, after=stages[1], blocks=("ordinary", "terminal")
            )
            _, _, prepared = _prepare_lifecycle_mutation(
                ledger, record, **arguments, joint_projection=projection
            )
            wider = project_joint_publication(
                record, after=stages[1], blocks=("ordinary", "successor")
            )
            object.__setattr__(projection, "blocks", wider.blocks)
            object.__setattr__(projection, "accounting_upper", wider.accounting_upper)
            staged = []

            def observe(frame, event, value):
                if (
                    event == "call"
                    and frame.f_code is LocalSessionLedger.stage_evidence.__code__
                ):
                    staged.append(True)

            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                with self.assertRaises(ReviewInputError):
                    prepared(record)
            finally:
                sys.setprofile(previous)
            self.assertEqual(staged, [])
            self.assertEqual(
                ledger.load(IDENTITY).record.record_sha256, record.record_sha256
            )

    def test_four_original_local_mutators_preserve_joint_fields_and_cold_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, record, arguments, stages = self.inputs(directory)
            for index, stage in enumerate(stages):
                before = record
                document = {
                    "required": ["retained"],
                    "stop_prefix": index,
                    "unknown": ["unchanged"],
                }
                projection = project_joint_publication(before, after=stage)
                _, _, mutate = _prepare_lifecycle_mutation(
                    ledger,
                    before,
                    **{**arguments, "document": document},
                    joint_projection=projection,
                )
                record = ledger.replace(IDENTITY, mutate)
                self.assertEqual(record.generation, before.generation + 1)
                self.assertEqual(record.transaction, before.transaction)
                self.assertEqual(record.api_run_accounting, before.api_run_accounting)
                self.assertEqual(
                    record.publication_staging,
                    None if stage is None else stage.to_document(),
                )
                cold = LocalSessionLedger(
                    Path(directory), enable_finding_lifecycle_writes=True
                )
                loaded = cold.load(IDENTITY).record
                self.assertEqual(loaded, record)
                self.assertEqual(read_finding_lifecycle(cold, loaded), document)
                if stage is not None:
                    self.assertEqual(stage.cold_status()["mode"], "read-only")
                    with self.assertRaises(ReviewInputError):
                        PublicationStaging.from_document(loaded.publication_staging)
                staged = []

                def observe(frame, event, value):
                    if (
                        event == "call"
                        and frame.f_code is LocalSessionLedger.stage_evidence.__code__
                    ):
                        staged.append(True)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    with self.assertRaises(ReviewInputError):
                        mutate(record)
                finally:
                    sys.setprofile(previous)
                self.assertEqual(staged, [])

    def test_live_equality_bombs_refuse_without_callback_or_staging(self):
        callbacks = []

        class Bomb:
            def __eq__(self, other):
                callbacks.append(True)
                return True

        variants = (
            "parent_root",
            "staging",
            "blocks",
            "accounting_upper",
            "block_item",
        )
        for field in variants:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                ledger, record, arguments, stages = self.inputs(directory)
                projection = project_joint_publication(record, after=stages[1])
                _, _, mutate = _prepare_lifecycle_mutation(
                    ledger, record, **arguments, joint_projection=projection
                )
                object.__setattr__(
                    projection,
                    "blocks" if field == "block_item" else field,
                    (Bomb(),) if field == "block_item" else Bomb(),
                )
                staged = []

                def observe(frame, event, value):
                    if (
                        event == "call"
                        and frame.f_code is LocalSessionLedger.stage_evidence.__code__
                    ):
                        staged.append(True)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    with self.assertRaises(ReviewInputError):
                        mutate(record)
                finally:
                    sys.setprofile(previous)
                self.assertEqual(callbacks, [])
                self.assertEqual(staged, [])
                self.assertEqual(ledger.load(IDENTITY).record, record)
