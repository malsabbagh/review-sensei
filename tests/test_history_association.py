"""Owned direct association, sparse reads, and complete activation liability."""

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from review_sensei.assessment_history_index import (
    IndexedAssessmentJournal,
    history_envelope,
    resolve_history_part,
)
from review_sensei.bounded_evidence import (
    PARTITION_ENCODING,
    ActivationTailPlan,
    AuthenticatedPart,
    EvidenceReadBudget,
    TailDispatch,
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
    read_partitioned_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.history_association import parse_history
from review_sensei.hosting.github import GitHubHttp
from review_sensei.hosting.github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    _history_extra_ids,
    _history_preserve_sources,
    _tail_part_ids,
    _tail_plan_scope,
    read_session_assessment_queue,
)
from tests import test_assessment_history_index as index_fixture
from tests.fake_github_http import json_response
from tests.test_activation_tail import OPERATION, RESERVATION
from tests.test_assessment_queue_storage import active_summary

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def pack(document, binding, storage_id=None):
    parts = partition_evidence(document, binding=binding, item_count=8)
    assert len(parts) == 1
    raw = canonical_bytes(parts[0])
    identity = storage_id or hashlib.sha256(raw).hexdigest()
    manifest = evidence_manifest(
        document, binding=binding, item_count=8, parts=parts, storage_ids=(identity,)
    )
    return manifest, {identity: parts[0]}


class GraphState:
    def __init__(self, identity, head, comments):
        self.identity, self.head, self.comments = identity, head, comments
        self.calls = []
        self.after_child = None
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        self.calls.append((request.method, request.full_url))
        parsed = urlparse(request.full_url)
        if parsed.path.endswith(f"/issues/{self.identity.pull_request}/comments"):
            query = parse_qs(parsed.query)
            page = int(query["page"][0])
            return json_response(
                list(self.comments.values())[(page - 1) * 5 : page * 5]
            )
        if "/issues/comments/" in parsed.path:
            identity = int(parsed.path.rsplit("/", 1)[-1])
            response = json_response(
                self.comments.get(identity, {}),
                200 if identity in self.comments else 404,
            )
            if self.after_child and identity != 1000:
                self.after_child(identity)
            return response
        if parsed.path.endswith(f"/pulls/{self.identity.pull_request}"):
            return json_response({"head": {"sha": self.head}})
        raise AssertionError(request.full_url)


class HistoryAssociationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = index_fixture.IndexedHistoryTests(
            "test_varied_100_every_checkpoint_is_one_part"
        )
        fixture.profile(8)
        cls.original, cls.original_store, cls.original_binding, _, _ = (
            fixture.last_profile
        )

    def graph(self, *, hosted=False):
        binding = {
            **self.original_binding,
            "repository_id": 99,
            "producer": "github-bot:55" if hosted else "local-ledger",
        }
        current = self.original.to_document()
        store = {}
        for index, row in enumerate(current["sources"], 1001):
            ref = copy.deepcopy(row["reference"])
            packet = resolve_history_part(
                type(self.original.history_references()[0]).from_document(ref),
                reader=lambda identity: AuthenticatedPart(
                    self.original_store[identity], "local-ledger"
                ),
                expected_binding=self.original_binding,
                budget=EvidenceReadBudget(),
                item_count=8,
            )
            manifest, objects = pack(
                history_envelope(packet), binding, str(index) if hosted else None
            )
            store.update(objects)
            row["reference"] = {
                **manifest["parts"][0],
                "decoded_bytes": manifest["decoded_bytes"],
            }
        envelope = history_envelope(current)
        manifest, objects = pack(envelope, binding, "1003" if hosted else None)
        store.update(objects)
        root = {
            "schema_version": "1.0",
            "inventory_digest": self.original.queue.inventory_digest,
            "inventory_generation": 0,
            "state_manifest": manifest,
            "active_operation": None,
        }
        identity = SessionIdentity("owner/repo", 42, repository_id=99)
        record = SessionRecord.create(
            identity, generation=2, assessment_queue=root, now=NOW
        )
        return identity, binding, envelope, record, store

    def local(self, directory, *, enabled=True, budget=None):
        identity, binding, envelope, record, store = self.graph()
        ledger = LocalSessionLedger(
            Path(directory),
            evidence_budget=budget or EvidenceReadBudget(),
            enable_history_graph=enabled,
        )
        path = ledger._path(identity)
        path.parent.mkdir(parents=True)
        path.write_bytes(canonical_bytes(record.to_dict()))
        parts = Path(directory) / ".evidence" / "owner%2Frepo" / "42"
        parts.mkdir(parents=True)
        for key, value in store.items():
            (parts / key).write_bytes(canonical_bytes(value))
        return ledger, identity, binding, envelope, record, parts

    def hosted(self, *, enabled=True):
        identity, binding, envelope, record, store = self.graph(hosted=True)
        comments = {}
        for key, part in store.items():
            comments[int(key)] = self.comment(
                identity,
                int(key),
                "ReviewSensei immutable evidence part v1\n```json\n"
                + canonical_bytes(part).decode()
                + "\n```\n<!-- reviewsensei:evidence-part:v1 -->",
            )
        comments[1000] = self.comment(
            identity,
            1000,
            render_session_comment(repository_id=99, pull_request=42, record=record),
        )
        state = GraphState(identity, binding["head_sha"], comments)
        ledger = GitHubIssueCommentSessionLedger(
            state.http,
            token="synthetic",
            app_slug="sensei[bot]",
            evidence_budget=EvidenceReadBudget(),
            enable_history_graph=enabled,
        )
        return ledger, state, identity, binding, envelope, record

    @staticmethod
    def comment(identity, number, body):
        return {
            "id": number,
            "body": body,
            "user": {"id": 55, "type": "Bot", "login": "sensei[bot]"},
            "issue_url": f"https://api.github.test/repos/{identity.repository}/issues/{identity.pull_request}",
        }

    def associate(self, ledger, identity, binding, record, *, hosted=False):
        return ledger.associate_assessment_history(
            identity,
            expected_binding=binding,
            expected_root_sha256=record.record_sha256,
            expected_generation=record.generation,
            now=NOW,
            **({"max_scan_pages": 1} if hosted else {}),
        )

    def lookup(self, ledger, proof, envelope, index=1):
        row = envelope["journal"]["sources"][index]
        return ledger.read_associated_history(
            proof,
            source_digest=row["source_digest"],
            operation_id=row["operation_id"],
            now=NOW,
        )

    def test_local_sparse_owned_read_no_recursive_or_unselected_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, identity, binding, envelope, record, parts = self.local(directory)
            first = envelope["journal"]["sources"][0]["reference"]["storage_id"]
            (parts / first).unlink()
            with patch.object(ledger, "_read_part", wraps=ledger._read_part) as fetch:
                proof = self.associate(ledger, identity, binding, record)
                packet = self.lookup(ledger, proof, envelope)
            self.assertEqual(len(packet["sources"]), 1)  # reference data only
            requested = [call.args[1] for call in fetch.call_args_list]
            self.assertNotIn(first, requested)
            self.assertEqual(len(requested), 4)  # current x3 and selected child x1
            self.assertEqual(ledger.evidence_budget.calls, 15)
            self.assertEqual(proof.host_state().item_count, 8)
            self.assertEqual(
                proof.inventory_sha256,
                hashlib.sha256(
                    canonical_bytes(envelope["journal"]["sources"])
                ).hexdigest(),
            )

    def test_hosted_sparse_reads_charge_all_root_head_and_child_dispatches(self):
        ledger, state, identity, binding, envelope, record = self.hosted()
        proof = self.associate(ledger, identity, binding, record, hosted=True)
        self.lookup(ledger, proof, envelope)
        self.assertEqual(ledger.evidence_budget.calls, len(state.calls))
        self.assertEqual(len(state.calls), 10)  # 3*(scan,current,head)+child
        self.assertEqual(
            sum(url.endswith("/issues/comments/1002") for _, url in state.calls), 1
        )
        self.assertFalse(
            any(url.endswith("/issues/comments/1001") for _, url in state.calls)
        )

    def test_disabled_reader_foreign_context_stale_generation_and_unowned_lookup(self):
        for kind in ("disabled", "binding", "generation", "digest", "unknown"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                ledger, identity, binding, envelope, record, _ = self.local(
                    directory, enabled=kind != "disabled"
                )
                if kind == "binding":
                    binding["head_sha"] = "c" * 40
                if kind == "generation":
                    record = record.evolve(generation=3, now=NOW)
                if kind == "digest":
                    record = record.evolve(failed_attempts=1, now=NOW)
                with self.assertRaises(ReviewInputError):
                    proof = self.associate(ledger, identity, binding, record)
                    ledger.read_associated_history(
                        proof, source_digest="f" * 64, operation_id="e" * 64, now=NOW
                    )

    def test_proof_cannot_transfer_replace_budget_or_mutate_sealed_inventory(self):
        for kind in ("ledger", "budget", "payload", "generation", "digest"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                ledger, identity, binding, envelope, record, _ = self.local(directory)
                proof = self.associate(ledger, identity, binding, record)
                if kind == "ledger":
                    ledger = LocalSessionLedger(
                        Path(directory),
                        evidence_budget=ledger.evidence_budget,
                        enable_history_graph=True,
                    )
                elif kind == "budget":
                    ledger.evidence_budget = EvidenceReadBudget()
                elif kind == "payload":
                    object.__setattr__(proof, "_payload", b"{}")
                elif kind == "generation":
                    object.__setattr__(proof, "root_generation", 3)
                else:
                    object.__setattr__(proof, "inventory_sha256", "f" * 64)
                with self.assertRaises(ReviewInputError):
                    self.lookup(ledger, proof, envelope)

    def test_host_owner_association_head_and_after_child_root_fence(self):
        for kind in (
            "numeric-owner",
            "association",
            "head",
            "after-child",
            "child-owner",
            "float-child-owner",
            "float-child-id",
            "child-association",
            "missing",
        ):
            with self.subTest(kind=kind):
                ledger, state, identity, binding, envelope, record = self.hosted()
                if kind == "numeric-owner":
                    state.comments[1000]["user"]["id"] = True
                elif kind == "association":
                    state.comments[1000]["issue_url"] += "0"
                elif kind == "head":
                    state.head = "c" * 40
                with self.assertRaises(ReviewInputError):
                    proof = self.associate(
                        ledger, identity, binding, record, hosted=True
                    )
                    if kind == "after-child":

                        def move(_):
                            state.comments[1000]["body"] = render_session_comment(
                                repository_id=99,
                                pull_request=42,
                                record=record.evolve(generation=3, now=NOW),
                            )

                        state.after_child = move
                    elif kind == "child-owner":
                        state.comments[1002]["user"]["id"] = 56
                    elif kind == "float-child-owner":
                        state.comments[1002]["user"]["id"] = 55.0
                    elif kind == "float-child-id":
                        state.comments[1002]["id"] = 1002.0
                    elif kind == "child-association":
                        state.comments[1002]["issue_url"] += "0"
                    elif kind == "missing":
                        del state.comments[1002]
                    self.lookup(ledger, proof, envelope)

    def test_complete_refs_enter_tail_scope_and_legacy_mutation_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, identity, binding, envelope, record, _ = self.local(directory)
            read_session_assessment_queue(ledger, record)
            children = _history_extra_ids(ledger, record)
            self.assertEqual(len(children), 2)
            self.assertEqual(len(_tail_part_ids(record, history_ids=children)), 3)
            with self.assertRaisesRegex(ReviewInputError, "sealed activation tail"):
                ledger.replace(
                    identity,
                    lambda current: current.evolve(generation=3, now=NOW),
                    now=NOW,
                )
            active = active_summary()
            active["read_accounting"] = {
                "calls": ledger.evidence_budget.calls,
                "deadline_at_ms": ledger.evidence_budget.wall_deadline_ms,
            }
            root = copy.deepcopy(record.assessment_queue)
            root["active_operation"] = active
            draft = record.evolve(
                assessment_queue=root,
                reservation_id=RESERVATION,
                reserved_slot="verification",
                generation=3,
                now=NOW,
            )
            plain = _tail_plan_scope(
                "local",
                record,
                draft,
                operation=OPERATION,
                budget=ledger.evidence_budget,
            )
            graph = _tail_plan_scope(
                "local",
                record,
                draft,
                operation=OPERATION,
                budget=ledger.evidence_budget,
                old_history_ids=children,
                new_history_ids=children,
            )
            self.assertNotEqual(plain, graph)

    def install_envelope(self, ledger, identity, record, parts, envelope):
        context = record.assessment_queue["state_manifest"]["binding"]
        manifest, store = pack(envelope, context)
        for key, value in store.items():
            (parts / key).write_bytes(canonical_bytes(value))
        root = {**record.assessment_queue, "state_manifest": manifest}
        updated = record.evolve(assessment_queue=root, now=NOW)
        ledger._path(identity).write_bytes(canonical_bytes(updated.to_dict()))
        return updated

    def test_declared_hash_size_domain_and_exact_source_not_inferred(self):
        for kind in (
            "hash",
            "bytes",
            "decoded",
            "source",
            "domain",
            "alias",
            "unknown",
            "foreign-id",
            "extra",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                ledger, identity, binding, envelope, record, parts = self.local(
                    directory
                )
                row = envelope["journal"]["sources"][1]
                if kind == "hash":
                    row["reference"]["sha256"] = "f" * 64
                elif kind == "bytes":
                    row["reference"]["bytes"] += 1
                elif kind == "decoded":
                    row["reference"]["decoded_bytes"] += 1
                elif kind == "source":
                    row["source_digest"] = "f" * 64
                elif kind == "alias":
                    row["reference"] = copy.deepcopy(
                        envelope["journal"]["sources"][0]["reference"]
                    )
                elif kind == "unknown":
                    envelope["journal"]["schema_version"] = (
                        "assessment-history-index-v999"
                    )
                elif kind == "foreign-id":
                    row["reference"]["storage_id"] = "1002"
                elif kind == "extra":
                    envelope["journal"]["unidentified_references"] = []
                else:
                    part = json.loads(
                        (parts / row["reference"]["storage_id"]).read_bytes()
                    )
                    child = read_partitioned_evidence(
                        {
                            "encoding": PARTITION_ENCODING,
                            "binding": binding,
                            "sha256": part["inventory_sha256"],
                            "decoded_bytes": row["reference"]["decoded_bytes"],
                            "encoded_bytes": row["reference"]["bytes"] + 512,
                            "item_count": 8,
                            "parts": [
                                {
                                    key: value
                                    for key, value in row["reference"].items()
                                    if key != "decoded_bytes"
                                }
                            ],
                        },
                        reader=lambda _: AuthenticatedPart(part, "local-ledger"),
                        expected_binding=binding,
                        budget=EvidenceReadBudget(),
                    )
                    manifest, store = pack(child, {**binding, "head_sha": "c" * 40})
                    row["reference"] = {
                        **manifest["parts"][0],
                        "decoded_bytes": manifest["decoded_bytes"],
                    }
                    for key, value in store.items():
                        (parts / key).write_bytes(canonical_bytes(value))
                updated = self.install_envelope(
                    ledger, identity, record, parts, envelope
                )
                with self.assertRaises(ReviewInputError):
                    proof = self.associate(ledger, identity, binding, updated)
                    self.lookup(ledger, proof, envelope)

    def test_activation_reader_consumes_all_prepaid_direct_children_or_refuses(self):
        for missing in (False, True):
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger, identity, _, envelope, record, parts = self.local(directory)
                ids = (
                    record.assessment_queue["state_manifest"]["parts"][0]["storage_id"],
                ) + tuple(
                    row["reference"]["storage_id"]
                    for row in envelope["journal"]["sources"]
                )
                if missing:
                    (parts / ids[1]).unlink()
                steps = tuple(
                    TailDispatch(f"part:{key}:{unit}")
                    for key in ids
                    for unit in ("resolve", "directory", "read")
                )
                ticket = ledger.evidence_budget.reserve_tail(
                    ActivationTailPlan("local", "a" * 64, steps)
                )
                ticket.seal(record.record_sha256)
                ticket.start(scope_sha256="a" * 64, root_sha256=record.record_sha256)
                ledger._activation_ticket, ledger._activation_identity = (
                    ticket,
                    identity,
                )
                try:
                    if missing:
                        with self.assertRaises(ReviewInputError):
                            read_session_assessment_queue(ledger, record)
                    else:
                        read_session_assessment_queue(ledger, record)
                        ticket.finish()
                        self.assertEqual(ticket.dispatched, 9)
                    self.assertEqual(ledger.evidence_budget.calls, 9)
                finally:
                    ticket.abort()
                    ledger._activation_ticket = ledger._activation_identity = None

    def test_retention_complete_cardinality_and_declared_framed_bytes_refuse(self):
        from types import SimpleNamespace

        _, _, envelope, record, _ = self.graph()
        rows = []
        template = envelope["journal"]["sources"][0]
        for index in range(255):
            digest = hashlib.sha256(f"retained:{index}".encode()).hexdigest()
            rows.append(
                {
                    **copy.deepcopy(template),
                    "source_digest": digest,
                    "operation_id": digest,
                    "reference": {
                        "storage_id": digest,
                        "sha256": digest,
                        "bytes": 32_256,
                        "decoded_bytes": 2_097_152,
                    },
                    "active": False,
                }
            )
        envelope["journal"]["sources"] = rows
        # A real second root/baseline part consumes the same cardinality/bytes.
        root = copy.deepcopy(record.assessment_queue)
        fake = SimpleNamespace(
            assessment_queue=root,
            convergence_history={
                "baseline": {"parts": [{"storage_id": "f" * 64, "bytes": 1}]}
            },
        )
        with self.assertRaisesRegex(ReviewInputError, "retention"):
            parse_history(envelope, fake)
        fake.convergence_history = None
        root["state_manifest"]["parts"][0]["bytes"] = 32_257
        with self.assertRaisesRegex(ReviewInputError, "retention"):
            parse_history(envelope, fake)

    def test_graph_source_omission_accounting_substitution_and_bootstrap_refuse(self):
        for kind in ("omitted", "accounting", "reference", "legacy", "bootstrap"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                ledger, identity, _, envelope, record, parts = self.local(directory)
                # Authenticated current carrier comes from the existing owned fixture root.
                current = IndexedAssessmentJournal.from_document(
                    envelope["journal"]
                )._current()
                active = active_summary()
                active.update(
                    operation_id=current["operation_id"],
                    source_digest=current["source_digest"],
                )
                root = {**record.assessment_queue, "active_operation": active}
                before = record.evolve(assessment_queue=root, now=NOW)
                read_session_assessment_queue(ledger, before)
                changed = copy.deepcopy(envelope)
                if kind == "omitted":
                    changed["journal"]["sources"].pop()
                elif kind == "accounting":
                    changed["journal"]["sources"][0]["read_accounting"]["calls"] = 0
                elif kind == "reference":
                    changed["journal"]["sources"][0]["reference"]["decoded_bytes"] += 1
                after = self.install_envelope(ledger, identity, before, parts, changed)
                read_session_assessment_queue(ledger, after)
                if kind == "legacy":
                    before = before.evolve(assessment_queue=None, now=NOW)
                    # Queue absence cannot supply archived original authority.
                if kind == "bootstrap":
                    before = before.evolve(assessment_queue=None, now=NOW)
                with self.assertRaises(ReviewInputError):
                    _history_preserve_sources(ledger, before, after)

    def test_original_deadline_and_shared_request_limit_never_refill(self):
        for kind in ("deadline", "calls"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                clock = [10.0]
                budget = EvidenceReadBudget(
                    clock=lambda: clock[0], wall_clock=lambda: 1000
                )
                ledger, identity, binding, envelope, record, _ = self.local(
                    directory, budget=budget
                )
                proof = self.associate(ledger, identity, binding, record)
                original = (budget.deadline, budget.wall_deadline_ms)
                if kind == "deadline":
                    clock[0] = budget.deadline
                else:
                    while budget.calls < 60:
                        budget.consume()
                with self.assertRaises(ReviewInputError):
                    self.lookup(ledger, proof, envelope)
                self.assertEqual((budget.deadline, budget.wall_deadline_ms), original)
                self.assertLessEqual(budget.calls, 64)

    def test_lookup_context_uses_primitive_hashes_and_expired_receipts_remain_readable(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            ledger, identity, binding, envelope, record, _ = self.local(directory)
            with self.assertRaisesRegex(ReviewInputError, "enabled original reader"):
                ledger.associate_assessment_history(
                    identity,
                    expected_binding=binding,
                    expected_root_sha256=[],
                    expected_generation=record.generation,
                    now=NOW,
                )
            later = datetime(2027, 1, 1, tzinfo=timezone.utc)
            proof = ledger.associate_assessment_history(
                identity,
                expected_binding=binding,
                expected_root_sha256=record.record_sha256,
                expected_generation=record.generation,
                now=later,
            )
            row = envelope["journal"]["sources"][1]
            with self.assertRaisesRegex(ReviewInputError, "lookup identity"):
                ledger.read_associated_history(
                    proof,
                    source_digest=[row["source_digest"]],
                    operation_id=row["operation_id"],
                    now=later,
                )
            packet = ledger.read_associated_history(
                proof,
                source_digest=row["source_digest"],
                operation_id=row["operation_id"],
                now=later,
            )
            self.assertIsNotNone(
                IndexedAssessmentJournal.from_document(packet)._current()
            )

    def test_hosted_metadata_scan_boundary_refuses_without_extra_page(self):
        ledger, state, identity, binding, _, record = self.hosted()
        for index in range(194):
            state.comments[2000 + index] = {
                "id": 2000 + index,
                "body": "retained immutable or visible fixture",
            }
        with self.assertRaises(ReviewInputError):
            ledger.associate_assessment_history(
                identity,
                expected_binding=binding,
                expected_root_sha256=record.record_sha256,
                expected_generation=record.generation,
                max_scan_pages=39,
                now=NOW,
            )
        self.assertEqual(ledger.evidence_budget.calls, 39)
        self.assertEqual(len(state.calls), 39)
        self.assertTrue(state.calls[-1][1].endswith("page=39"))

    def test_actual_local_opt_in_genesis_tail_and_archived_bootstrap_refusal(self):
        for archived in (False, True):
            with (
                self.subTest(archived=archived),
                tempfile.TemporaryDirectory() as directory,
            ):
                identity, binding, envelope, _, _ = self.graph()
                operation = {
                    **OPERATION,
                    "inventory_digest": self.original.queue.inventory_digest,
                }
                ledger = LocalSessionLedger(
                    Path(directory),
                    evidence_budget=EvidenceReadBudget(),
                    enable_partition_writes=True,
                    enable_history_graph=True,
                )
                ledger.initialize(identity, now=NOW)
                ledger.reserve_for_tail(
                    identity,
                    operation_binding=operation,
                    slot="verification",
                    reservation_id=RESERVATION,
                    expected_generation=0,
                    now=NOW,
                )
                # Bootstrap rejects archived sources even if a caller supplied syntactically valid refs.
                if not archived:
                    envelope = history_envelope(
                        IndexedAssessmentJournal(self.original.queue).to_document()
                    )

                def prepare(record):
                    from review_sensei.session import assessment_queue_manifest_capacity

                    manifest = ledger.stage_evidence(
                        identity,
                        binding=binding,
                        document=envelope,
                        item_count=8,
                        max_manifest_bytes=assessment_queue_manifest_capacity(record),
                    )
                    active = active_summary()
                    active["read_accounting"] = {
                        "calls": ledger.evidence_budget.calls,
                        "deadline_at_ms": ledger.evidence_budget.wall_deadline_ms,
                    }
                    root = {
                        "schema_version": "1.0",
                        "inventory_digest": operation["inventory_digest"],
                        "inventory_generation": 0,
                        "state_manifest": manifest,
                        "active_operation": active,
                    }
                    return record.evolve(
                        assessment_queue=root, generation=record.generation + 1, now=NOW
                    )

                def seal(record, accounting):
                    root = copy.deepcopy(record.assessment_queue)
                    root["active_operation"]["read_accounting"] = dict(accounting)
                    return record.evolve(assessment_queue=root, now=NOW)

                kwargs = {
                    "operation_binding": operation,
                    "attempt_reservation_id": RESERVATION,
                    "seal_accounting": seal,
                    "now": NOW,
                }
                if archived:
                    with self.assertRaisesRegex(
                        ReviewInputError, "invent archived original accounting"
                    ):
                        ledger.replace_with_tail(identity, prepare, **kwargs)
                    self.assertIsNone(
                        ledger.load(identity, now=NOW).record.assessment_queue
                    )
                else:
                    result = ledger.replace_with_tail(identity, prepare, **kwargs)
                    self.assertEqual(
                        result.assessment_queue["active_operation"]["read_accounting"][
                            "calls"
                        ],
                        ledger.evidence_budget.calls,
                    )
                    self.assertEqual(result.generation, 2)
                    self.assertEqual(
                        read_session_assessment_queue(ledger, result), envelope
                    )

    def test_frozen_combined_reason_requires_both_exact_digests_and_primitive_reason(
        self,
    ):
        from review_sensei.hosting.github.errors import GitHubBrokerClientError
        from tests import test_review_transaction as original
        from tests.test_activation_tail import ConsumingBroker, feedback_request

        for kind in ("valid", "request", "dispatch", "array"):
            with self.subTest(kind=kind):
                budget = EvidenceReadBudget()
                broker = ConsumingBroker()
                record = SessionRecord.create(original.IDENTITY, now=NOW)
                broker.request = feedback_request(
                    record, budget, reason="admission-dispatch"
                )
                broker.request["mutation"].update(
                    request_digest="a" * 64, dispatch_digest="b" * 64
                )
                if kind == "request":
                    broker.request["mutation"]["request_digest"] = None
                elif kind == "dispatch":
                    broker.request["mutation"]["dispatch_digest"] = None
                elif kind == "array":
                    broker.request["mutation"]["reason"] = ["admission-dispatch"]
                if kind == "valid":
                    grant = broker.issue(budget)
                    self.assertEqual(
                        grant.attestation["mutation"]["reason"], "admission-dispatch"
                    )
                else:
                    with self.assertRaises(GitHubBrokerClientError):
                        broker.issue(budget)
