"""Lossless snapshot page reuse independently of whole-history admission."""

import copy
import hashlib
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_disposition_snapshots import BASE, HEAD, IDENTITY, POLICY, RESULT, snapshot

from review_sensei.bounded_evidence import (
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
    inline_manifest,
    read_inline_evidence,
    read_partitioned_evidence,
    stage_partitioned_evidence,
)
from review_sensei.disposition_snapshot import (
    normalize_snapshot,
    prepare_local_paged_snapshot,
    read_paged_snapshot,
    stage_paged_snapshot,
    validate_page_index,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    SessionRecord,
    read_session_dispositions,
    stage_session_dispositions,
)


class SnapshotStore:
    """In-memory codec oracle, not a host capacity or publication certificate."""

    def __init__(self):
        self.parts = {}

    def stage_disposition_snapshot_batch(
        self,
        identity,
        *,
        record,
        binding,
        pages,
        values,
        result_digest,
        max_manifest_bytes,
    ):
        prepared = prepare_local_paged_snapshot(
            binding=binding,
            pages=pages,
            values=values,
            result_digest=result_digest,
            max_manifest_bytes=max_manifest_bytes,
        )
        for key, part in prepared.parts.items():
            self.parts[key] = AuthenticatedPart(
                copy.deepcopy(part), binding["producer"]
            )
        document = self.read_evidence(
            identity, prepared.manifest, expected_binding=binding
        )
        restored = read_paged_snapshot(
            document,
            root={
                "schema_version": "2.0",
                "result_digest": result_digest,
                "state_manifest": prepared.manifest,
            },
            reader=self.read_evidence,
            identity=identity,
        )
        if restored != values:
            raise ReviewInputError("snapshot oracle readback differs")
        return prepared.manifest

    def stage_evidence(
        self, identity, *, binding, document, item_count, max_manifest_bytes
    ):
        inline = inline_manifest(document, binding=binding, item_count=item_count)
        if inline is not None and len(canonical_bytes(inline)) <= max_manifest_bytes:
            return inline

        def write(part):
            key = hashlib.sha256(canonical_bytes(part)).hexdigest()
            self.parts[key] = AuthenticatedPart(
                copy.deepcopy(part), binding["producer"]
            )
            return key

        return stage_partitioned_evidence(
            document,
            binding=binding,
            item_count=item_count,
            max_manifest_bytes=max_manifest_bytes,
            writer=write,
            reader=self.parts.__getitem__,
            budget=EvidenceReadBudget(),
        )

    def read_evidence(self, identity, manifest, *, expected_binding):
        if manifest.get("encoding") == "inline-json-v1":
            return read_inline_evidence(manifest, expected_binding=expected_binding)
        return read_partitioned_evidence(
            manifest,
            expected_binding=expected_binding,
            reader=self.parts.__getitem__,
            budget=EvidenceReadBudget(),
        )


def decision(index):
    import base64

    return {
        "fingerprint": hashlib.sha256(str(index).encode()).hexdigest(),
        "action": "dismiss",
        "actor": "maintainer",
        "head_sha": "b" * 40,
        "expires_at": None,
        "reason": base64.b64encode(
            hashlib.shake_256(str(index).encode()).digest(384)
        ).decode(),
        "provenance": f"issue-comment:{index + 1}",
    }


def append(store, prior, values, generation):
    seed = snapshot()
    binding = {**seed["state_manifest"]["binding"], "generation": generation}
    record = SimpleNamespace(
        disposition_snapshot=prior,
        transaction=SimpleNamespace(result_sha256=seed["result_digest"]),
    )
    manifest = stage_paged_snapshot(
        store, record, values, binding=binding, identity=None, max_manifest_bytes=2048
    )
    root = normalize_snapshot(
        {
            "schema_version": "2.0",
            "result_digest": seed["result_digest"],
            "state_manifest": manifest,
        }
    )
    document = store.read_evidence(None, manifest, expected_binding=binding)
    restored = read_paged_snapshot(
        document, root=root, reader=store.read_evidence, identity=None
    )
    return root, document, restored


class PagedDispositionSnapshotTests(unittest.TestCase):
    def test_candidate_starts_at_first_closed_page_before_a_multi_page_migration(self):
        from review_sensei.disposition import FindingDisposition, _evolve_dispositions

        transaction = ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest=POLICY,
            configuration_digest="e" * 64,
            evidence_digest="a" * 64,
            reservation_id="f" * 64,
            generation=1,
        ).with_result(RESULT)
        for paged, enabled in ((False, True), (True, True), (True, False)):
            with (
                self.subTest(paged=paged, enabled=enabled),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(
                    Path(directory),
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=enabled,
                )
                record = SessionRecord.create(
                    IDENTITY,
                    generation=1,
                    transaction=transaction,
                    last_committed_reservation_id=transaction.reservation_id,
                    dispositions=tuple(decision(i) for i in range(7)),
                )
                new = FindingDisposition(**decision(7))
                with patch.object(
                    LocalSessionLedger,
                    "supports_paged_disposition_snapshot_writes",
                    new=property(lambda self: paged),
                ):
                    updated = _evolve_dispositions(
                        record, (new,), head_sha=HEAD, now=None, ledger=ledger
                    )
                self.assertEqual(
                    read_session_dispositions(ledger, updated),
                    (*record.dispositions, new.to_dict()),
                )
                self.assertEqual(
                    updated.disposition_snapshot is not None, paged and enabled
                )
                if paged and enabled:
                    self.assertEqual(
                        updated.disposition_snapshot["schema_version"], "2.0"
                    )

    def test_empty_partial_and_closed_page_boundaries_roundtrip(self):
        for count in (0, 7, 8, 9, 16, 17, 23):
            with (
                self.subTest(count=count),
                patch("review_sensei.disposition_snapshot.MAX_DISPOSITION_PAGES", 2),
            ):
                store = SnapshotStore()
                values = tuple(decision(i) for i in range(count))
                _, document, restored = append(store, None, values, 1)
                self.assertEqual(restored, values)
                self.assertEqual(len(document["pages"]), count // 8)
                self.assertEqual(len(document["tail"]), count % 8)
        store = SnapshotStore()
        with (
            patch("review_sensei.disposition_snapshot.MAX_DISPOSITION_PAGES", 2),
            self.assertRaisesRegex(ReviewInputError, "finite bounds"),
        ):
            append(store, None, tuple(decision(i) for i in range(24)), 1)
        self.assertEqual(store.parts, {})

    def test_malformed_root_and_closed_tail_refuse_before_page_reads(self):
        store = SnapshotStore()
        root, document, _ = append(store, None, (decision(0),), 1)
        malformed = [None, {}, {"state_manifest": {}}, []]
        for count in (None, True, -1, 1.5, "1", 4097):
            malformed.append(
                {
                    **root,
                    "state_manifest": {**root["state_manifest"], "item_count": count},
                }
            )
        for broken in malformed:
            with self.subTest(root=broken), self.assertRaises(ReviewInputError):
                validate_page_index(document, root=broken)
        damaged = copy.deepcopy(document)
        damaged["tail"][0]["unexpected"] = "unvalidated decision"
        with self.assertRaises(ReviewInputError):
            validate_page_index(damaged, root=root)

    def test_preparation_rejects_stale_page_reference_before_returning_write_set(self):
        store = SnapshotStore()
        values = tuple(decision(i) for i in range(8))
        root, document, _ = append(store, None, values, 1)
        changed = (
            {**values[0], "reason": "Changed independent rationale"},
            *values[1:],
        )
        with self.assertRaisesRegex(ReviewInputError, "page reference.*inventory"):
            prepare_local_paged_snapshot(
                binding=root["state_manifest"]["binding"],
                pages=document["pages"],
                values=changed,
                result_digest=root["result_digest"],
                max_manifest_bytes=2048,
            )

    def test_inline_reused_page_and_partitioned_index_remain_valid_manifests(self):
        from review_sensei.schemas import validate_public_document

        store = SnapshotStore()
        values = tuple(decision(i) for i in range(8))
        context = snapshot()["state_manifest"]["binding"]
        page = inline_manifest(
            {
                "schema_version": "disposition-page-v1",
                "result_digest": RESULT,
                "offset": 0,
                "dispositions": list(values),
            },
            binding=context,
            item_count=8,
        )
        self.assertIsNotNone(page)
        prepared = prepare_local_paged_snapshot(
            binding=context,
            pages=[page],
            values=values,
            result_digest=RESULT,
            max_manifest_bytes=1024,
        )
        for key, part in prepared.parts.items():
            store.parts[key] = AuthenticatedPart(part, context["producer"])
        root = {
            "schema_version": "2.0",
            "result_digest": RESULT,
            "state_manifest": prepared.manifest,
        }
        self.assertEqual(prepared.manifest["encoding"], "partitioned-json-v1")
        record = LocalDispositionBatchTests.record().evolve(disposition_snapshot=root)
        validate_public_document(record.to_dict(), "session-record")
        document = store.read_evidence(
            None, prepared.manifest, expected_binding=context
        )
        self.assertEqual(document["pages"], [page])
        self.assertEqual(
            read_paged_snapshot(
                document, root=root, reader=store.read_evidence, identity=None
            ),
            values,
        )

    def test_250_distinct_maximum_reasons_roundtrip_and_preserve_all_prior_carriers(
        self,
    ):
        store, root, values, saved = SnapshotStore(), None, (), {}
        for index in range(250):
            values = (*values, decision(index))
            root, document, restored = append(store, root, values, index + 1)
            self.assertEqual(restored, values)
            self.assertTrue(
                all(store.parts[key] == part for key, part in saved.items())
            )
            saved = copy.deepcopy(store.parts)
        self.assertEqual(len(document["pages"]), 31)
        self.assertEqual(len(document["tail"]), 2)
        # A representation regression oracle; not a claim about publication,
        # archives, request funding, or the current 256-part storage ceiling.
        self.assertLess(len(store.parts), 300)
        self.assertLess(
            sum(len(canonical_bytes(p.document)) for p in store.parts.values()),
            2_000_000,
        )

    def test_append_reuses_closed_pages_and_edit_replaces_only_affected_page(self):
        store = SnapshotStore()
        values = tuple(decision(i) for i in range(16))
        root, first, _ = append(store, None, values, 1)
        root, second, _ = append(store, root, (*values, decision(16)), 2)
        self.assertEqual(first["pages"], second["pages"])
        updated = (
            {**values[0], "reason": "Independent corrected rationale."},
            *values[1:],
            decision(16),
        )
        _, third, actual = append(store, root, updated, 3)
        self.assertNotEqual(third["pages"][0], second["pages"][0])
        self.assertEqual(third["pages"][1], second["pages"][1])
        self.assertEqual(actual, updated)

    def test_corruption_reordering_foreign_scope_future_generation_and_duplicate_refuse(
        self,
    ):
        for defect in ("order", "producer", "generation", "digest", "duplicate"):
            with self.subTest(defect=defect):
                store = SnapshotStore()
                root, document, _ = append(
                    store, None, tuple(decision(i) for i in range(17)), 1
                )
                damaged = copy.deepcopy(document)
                if defect == "order":
                    damaged["pages"].reverse()
                elif defect == "producer":
                    damaged["pages"][0]["binding"]["producer"] = "github-bot:123"
                elif defect == "generation":
                    damaged["pages"][0]["binding"]["generation"] = 2
                elif defect == "digest":
                    damaged["result_digest"] = "f" * 64
                else:
                    damaged["tail"][0] = decision(0)
                with self.assertRaises(ReviewInputError):
                    read_paged_snapshot(
                        damaged, root=root, reader=store.read_evidence, identity=None
                    )

    def test_terminal_profiles_keep_legacy_work_and_fund_expanded_tail_separately(self):
        from review_sensei_github.api_run_plan import (
            owner_allocations,
            terminal_requests,
        )

        legacy = owner_allocations(512, 132)
        expanded = owner_allocations(1152, 132)
        self.assertEqual(legacy.first_terminal, 50)
        self.assertEqual(legacy.first_work, 140)
        self.assertEqual(expanded.first_terminal, 68)
        self.assertEqual(expanded.first_terminal, terminal_requests(1152))
        self.assertEqual(expanded.first_total + expanded.successor_total + 132, 1152)


class BatchCandidateLedger(LocalSessionLedger):
    """Explicit qualification only; production adapters retain v1 writers."""

    @property
    def supports_paged_disposition_snapshot_writes(self):
        return True


class LocalDispositionBatchTests(unittest.TestCase):
    @staticmethod
    def record():
        transaction = ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest=POLICY,
            configuration_digest="e" * 64,
            evidence_digest="a" * 64,
            reservation_id="f" * 64,
            generation=1,
        ).with_result(RESULT)
        return SessionRecord.create(
            IDENTITY,
            generation=1,
            transaction=transaction,
            last_committed_reservation_id=transaction.reservation_id,
        )

    @staticmethod
    def parts(path):
        return {
            str(p.relative_to(path)): p.read_bytes()
            for p in path.rglob("*")
            if p.is_file() and ".evidence" in p.parts
        }

    def test_real_serialized_local_migration_33_and_64_has_one_complete_readback(self):
        for count in (33, 64):
            for platform in ("darwin", "win32"):
                with self.subTest(count=count, platform=platform):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory)
                        record = self.record()
                        seed = LocalSessionLedger(path)
                        seed.initialize(IDENTITY)
                        seed.replace(
                            IDENTITY,
                            lambda current: current.evolve(
                                generation=1,
                                transaction=record.transaction,
                                last_committed_reservation_id=record.last_committed_reservation_id,
                            ),
                        )
                        ledger = LocalSessionLedger(
                            path,
                            evidence_budget=EvidenceReadBudget(),
                            enable_disposition_snapshot_writes=True,
                        )
                        values = tuple(decision(i) for i in range(count))
                        with patch("review_sensei.session.sys.platform", platform):
                            written = ledger.replace(
                                IDENTITY,
                                lambda current: stage_session_dispositions(
                                    ledger, current, values, head_sha=HEAD, now=None
                                ),
                            )
                            self.assertLessEqual(ledger.evidence_budget.calls, 60)
                            fresh = LocalSessionLedger(
                                path, evidence_budget=EvidenceReadBudget()
                            )
                            loaded = fresh.load(IDENTITY)
                            self.assertEqual(loaded.record, written)
                            self.assertEqual(
                                read_session_dispositions(fresh, loaded.record), values
                            )

    def test_whole_batch_capacity_and_funding_refuse_before_any_immutable_write(self):
        for failure in ("manifest", "calls", "count", "bytes"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory)
                ledger = LocalSessionLedger(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=True,
                )
                values = tuple(decision(i) for i in range(33))
                binding = snapshot()["state_manifest"]["binding"]
                if failure == "calls":
                    for _ in range(50):
                        ledger.evidence_budget.consume()
                before = self.parts(path)
                bounds = (
                    {"MAX_STORED_PARTS": 3}
                    if failure == "count"
                    else {"MAX_STORED_PART_BYTES": 1}
                    if failure == "bytes"
                    else {}
                )
                # Downward-only test ceilings exercise complete batch refusal;
                # production retained-count and byte limits stay unchanged.
                with (
                    patch.multiple("review_sensei.session", **bounds)
                    if bounds
                    else nullcontext()
                ):
                    with self.assertRaises(ReviewInputError):
                        stage_paged_snapshot(
                            ledger,
                            self.record(),
                            values,
                            binding=binding,
                            identity=IDENTITY,
                            max_manifest_bytes=100 if failure == "manifest" else 8192,
                        )
                self.assertEqual(self.parts(path), before)
                if failure == "calls":
                    self.assertGreaterEqual(ledger.evidence_budget.calls, 50)

    def test_readback_failure_retains_every_attempted_part_and_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            ledger = BatchCandidateLedger(
                path,
                evidence_budget=EvidenceReadBudget(),
                enable_disposition_snapshot_writes=True,
            )
            with (
                patch.object(
                    ledger,
                    "read_evidence",
                    side_effect=ReviewInputError("lost readback"),
                ),
                self.assertRaisesRegex(ReviewInputError, "lost readback"),
            ):
                stage_session_dispositions(
                    ledger,
                    self.record(),
                    tuple(decision(i) for i in range(16)),
                    head_sha=HEAD,
                    now=None,
                )
            self.assertEqual(len(self.parts(path)), 2)
            self.assertEqual(ledger.evidence_budget.calls, 5)

    def test_adapter_without_batch_keeps_v1_and_refuses_v2_downgrade(self):
        class LegacyAdapter(LocalSessionLedger):
            stage_disposition_snapshot_batch = None

        for count in (33, 64):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                ledger = LegacyAdapter(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=True,
                )
                values = tuple(decision(i) for i in range(count))
                record = stage_session_dispositions(
                    ledger, self.record(), values, head_sha=HEAD, now=None
                )
                self.assertEqual(record.disposition_snapshot["schema_version"], "1.0")
                fresh = LegacyAdapter(path, evidence_budget=EvidenceReadBudget())
                self.assertEqual(read_session_dispositions(fresh, record), values)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            qualified = BatchCandidateLedger(
                path,
                evidence_budget=EvidenceReadBudget(),
                enable_disposition_snapshot_writes=True,
            )
            values = tuple(decision(i) for i in range(16))
            record = stage_session_dispositions(
                qualified, self.record(), values, head_sha=HEAD, now=None
            )
            before = self.parts(path)
            legacy = LegacyAdapter(
                path,
                evidence_budget=EvidenceReadBudget(),
                enable_disposition_snapshot_writes=True,
            )
            with self.assertRaisesRegex(ReviewInputError, "cannot downgrade"):
                stage_session_dispositions(
                    legacy, record, values, head_sha=HEAD, now=None
                )
            self.assertEqual(self.parts(path), before)
            self.assertEqual(legacy.evidence_budget.calls, 0)

    def test_candidate_batch_serialized_33_and_64_and_append_edit_preserve_old_parts(
        self,
    ):
        for count in (33, 64):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                record = self.record()
                seed = LocalSessionLedger(path)
                seed.initialize(IDENTITY)
                seed.replace(
                    IDENTITY,
                    lambda current: current.evolve(
                        generation=1,
                        transaction=record.transaction,
                        last_committed_reservation_id=record.last_committed_reservation_id,
                    ),
                )
                candidate = BatchCandidateLedger(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=True,
                )
                values = tuple(decision(i) for i in range(count))
                written = candidate.replace(
                    IDENTITY,
                    lambda current: stage_session_dispositions(
                        candidate, current, values, head_sha=HEAD, now=None
                    ),
                )
                self.assertEqual(written.disposition_snapshot["schema_version"], "2.0")
                self.assertLessEqual(candidate.evidence_budget.calls, 60)
                fresh = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
                self.assertEqual(read_session_dispositions(fresh, written), values)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            record = self.record()
            values = tuple(decision(i) for i in range(16))
            for update in (
                values,
                (*values, decision(16)),
                (
                    {**values[0], "reason": "Corrected independent rationale"},
                    *values[1:],
                    decision(16),
                ),
            ):
                candidate = BatchCandidateLedger(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=True,
                )
                before = self.parts(path)
                record = stage_session_dispositions(
                    candidate, record, update, head_sha=HEAD, now=None
                )
                after = self.parts(path)
                self.assertTrue(all(after[key] == raw for key, raw in before.items()))
                fresh = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
                self.assertEqual(read_session_dispositions(fresh, record), update)


class SnapshotActivationCompositionTests(unittest.TestCase):
    def test_v1_and_v2_dispositions_compose_with_baseline_and_queue_tail(self):
        import test_review_transaction as fixture
        from test_activation_tail import activate, reserve_original
        from test_authenticated_partitions import binding
        from test_evidence_capacity import varied_baseline

        from review_sensei.baseline import baseline_complete_document
        from review_sensei.session import _disposition_page_ids, _tail_part_ids

        for schema in ("1.0", "2.0"):
            with (
                self.subTest(schema=schema),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory)
                seed = LocalSessionLedger(path, enable_partition_writes=True)
                seed.initialize(fixture.IDENTITY, now=fixture.NOW)
                values = tuple(
                    {**decision(i), "head_sha": fixture.HEAD_SHA} for i in range(8)
                )

                def seed_history(current):
                    baseline = seed.stage_evidence(
                        fixture.IDENTITY,
                        binding=binding(generation=0, purpose="baseline"),
                        document={
                            **baseline_complete_document(varied_baseline(1)),
                            "generation": 0,
                        },
                        item_count=1,
                        max_manifest_bytes=2048,
                    )
                    context = binding(generation=0, purpose="dispositions")
                    if schema == "2.0":
                        page = seed.stage_evidence(
                            fixture.IDENTITY,
                            binding=context,
                            document={
                                "schema_version": "disposition-page-v1",
                                "result_digest": RESULT,
                                "offset": 0,
                                "dispositions": list(values),
                            },
                            item_count=8,
                            max_manifest_bytes=1024,
                        )
                        document = {
                            "schema_version": "disposition-pages-v1",
                            "result_digest": RESULT,
                            "pages": [page],
                            "tail": [],
                        }
                    else:
                        document = {
                            "schema_version": "1.0",
                            "result_digest": RESULT,
                            "dispositions": list(values),
                        }
                    manifest = seed.stage_evidence(
                        fixture.IDENTITY,
                        binding=context,
                        document=document,
                        item_count=8,
                        max_manifest_bytes=2048,
                    )
                    return current.evolve(
                        disposition_snapshot={
                            "schema_version": schema,
                            "result_digest": RESULT,
                            "state_manifest": manifest,
                        },
                        convergence_history={
                            "state": "completed",
                            "baseline": baseline,
                            "progress": [],
                            "provenance": {"ledger_digest": current.record_sha256},
                        },
                        now=fixture.NOW,
                    )

                seeded = seed.replace(fixture.IDENTITY, seed_history, now=fixture.NOW)
                retained = LocalDispositionBatchTests.parts(path)
                ledger = LocalSessionLedger(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_partition_writes=True,
                )
                reserve_original(ledger)
                issued = []
                reserve = ledger.evidence_budget.reserve_tail

                def capture(plan):
                    ticket = reserve(plan)
                    issued.append(ticket)
                    return ticket

                with patch.object(
                    ledger.evidence_budget, "reserve_tail", side_effect=capture
                ):
                    written = activate(ledger)
                expected = _tail_part_ids(
                    written, disposition_page_ids=_disposition_page_ids(ledger, written)
                )
                self.assertEqual(len(expected), 4 if schema == "2.0" else 3)
                self.assertEqual(
                    [step.label for step in issued[0].plan.steps[:-4]],
                    [
                        f"part:{part_id}:{unit}"
                        for part_id in expected
                        for unit in ("resolve", "directory", "read")
                    ],
                )
                self.assertEqual(issued[0].dispatched, len(issued[0].plan.steps))
                self.assertLessEqual(ledger.evidence_budget.calls, 64)
                self.assertEqual(
                    written.disposition_snapshot, seeded.disposition_snapshot
                )
                self.assertTrue(
                    all(
                        LocalDispositionBatchTests.parts(path)[key] == raw
                        for key, raw in retained.items()
                    )
                )
                fresh = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
                loaded = fresh.load(fixture.IDENTITY, now=fixture.NOW)
                self.assertEqual(
                    read_session_dispositions(fresh, loaded.record), values
                )

    def test_external_index_plan_requires_complete_root_specific_read_proof(self):
        from review_sensei.session import _disposition_page_ids, _tail_part_ids

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            ledger = BatchCandidateLedger(
                path,
                evidence_budget=EvidenceReadBudget(),
                enable_disposition_snapshot_writes=True,
            )
            record = LocalDispositionBatchTests.record()
            context = snapshot()["state_manifest"]["binding"]
            values = tuple(decision(i) for i in range(17))
            manifest = stage_paged_snapshot(
                ledger,
                record,
                values,
                binding=context,
                identity=IDENTITY,
                max_manifest_bytes=1024,
            )
            written = record.evolve(
                disposition_snapshot={
                    "schema_version": "2.0",
                    "result_digest": RESULT,
                    "state_manifest": manifest,
                }
            )
            self.assertEqual(manifest["encoding"], "partitioned-json-v1")
            with self.assertRaisesRegex(ReviewInputError, "authenticated inventory"):
                _tail_part_ids(written)
            reader = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
            ids = _disposition_page_ids(reader, written)
            calls = reader.evidence_budget.calls
            self.assertEqual(len(ids), 2)
            self.assertEqual(_disposition_page_ids(reader, written), ids)
            self.assertEqual(reader.evidence_budget.calls, calls)
            changed = written.evolve(
                disposition_snapshot={
                    **written.disposition_snapshot,
                    "result_digest": "f" * 64,
                }
            )
            with self.assertRaises(ReviewInputError):
                _disposition_page_ids(reader, changed)
            self.assertGreater(reader.evidence_budget.calls, calls)
            self.assertEqual(_disposition_page_ids(reader, written), ids)
