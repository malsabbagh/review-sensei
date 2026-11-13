"""Actual current lifecycle origins retain hidden immutable assessment criteria."""

import copy
import random
import string
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.finding_lifecycle import _unpack_journal
from review_sensei_github.quarantine_evidence import (
    CurrentReadScope,
    _lifecycle_origin_inventory,
    _read_published_record,
    read_published_source,
)
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import decode_lifecycle_state
from review_sensei.finding_lifecycle_store import _decode_selected_lifecycle
from tests import test_quarantine_evidence as fixtures
from tests import test_review_run as entry


class LifecycleOriginTests(unittest.TestCase):
    def produce(self, directory, *, rich=False):
        class Fixture(entry.EntryFixture):
            absent_part_id = None

            def external(self, request, timeout):
                path = urlsplit(request.full_url).path
                if (
                    request.get_method() == "GET"
                    and self.absent_part_id is not None
                    and path
                    == f"/repos/owner/repo/issues/comments/{self.absent_part_id}"
                ):
                    self.trace.append((self.attempt, "GET", path))
                    return json_response({"message": "Not Found"}, 404)
                return super().external(request, timeout)

        fixture = Fixture(directory)
        fixtures.original_objects(fixture)
        fixture.active_finding_cap = 1
        fixture.provider_comments = [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": f"Independent external assessment {number}.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
            for number in range(3)
        ]
        if rich:
            random_source = random.Random(871)
            for comment in fixture.provider_comments:
                comment["body"] += " " + "".join(
                    random_source.choices(string.ascii_letters + string.digits, k=2500)
                )
        self.assertEqual(fixture.invoke(human=True), 0, fixture.error)
        record = parse_session_comment(
            fixture.comment["body"], identity=fixture.selected.identity
        )
        scope = CurrentReadScope(
            fixture.selected.ledger,
            record.identity(),
            fixture.live_base,
            fixture.live_head,
            "synthetic-workflow-read",
        )
        return fixture, record, scope

    def read(self, fixture, record, scope, retained=None):
        return _read_published_record(
            ledger=fixture.selected.ledger,
            original=record,
            current_scope=scope,
            retained_source=retained,
            complete_inventory=True,
        )

    def test_actual_hidden_inventory_uses_immutable_criteria_and_revalidates_native_body(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, scope = self.produce(directory)
            before = fixture.comment["body"]
            trace = len(fixture.trace)
            source, inventory = self.read(fixture, record, scope)
            self.assertEqual(
                source["schema_version"],
                "quarantine-lifecycle-published-assessment-source-v1",
            )
            self.assertEqual(len(inventory["assessments"]), 3)
            self.assertEqual(len(fixture.threads), 1)
            stored = _decode_selected_lifecycle(fixture.selected.ledger, record)
            state = decode_lifecycle_state(_unpack_journal(stored)["lifecycle"]).state
            expected = sorted(
                (
                    {
                        "instance": ob.criterion.obligation_id,
                        "concern": ob.criterion.resolution_criterion,
                    }
                    for ob in state.inventory.obligations
                ),
                key=lambda value: value["instance"],
            )
            self.assertEqual(inventory["assessments"], expected)
            # Pure projection test: changing the last displayed comment cannot
            # rename immutable original criteria or erase a human obligation.
            changed = replace(
                state,
                inventory=replace(
                    state.inventory,
                    obligations=tuple(
                        replace(
                            ob,
                            latest_comment=replace(
                                ob.latest_comment,
                                body="new projection",
                                needs_human=False,
                            ),
                        )
                        for ob in state.inventory.obligations
                    ),
                ),
            )
            self.assertEqual(
                _lifecycle_origin_inventory(changed, complete_inventory=True), inventory
            )
            blocking = replace(
                state,
                inventory=replace(
                    state.inventory,
                    obligations=tuple(
                        replace(
                            ob,
                            criterion=replace(
                                ob.criterion, needs_human=False, blocking=True
                            ),
                        )
                        for ob in state.inventory.obligations
                    ),
                ),
            )
            self.assertEqual(
                _lifecycle_origin_inventory(blocking, complete_inventory=True),
                inventory,
            )
            # Aggregate value qualification only: two separately scoped origins
            # never deduplicate equal immutable criterion identities.
            from review_sensei.bounded_evidence import (
                evidence_manifest,
                partition_evidence,
            )
            from review_sensei.quarantine_assessments import (
                origin_scope,
                pending_assessments,
            )
            from tests.test_quarantine_lineage import lineage_value

            binding = lineage_value()["origin_manifest"]["binding"]
            origins, inventories = [], {}
            for kind in ("published-current", "published-earlier"):
                origin = {
                    "schema_version": "quarantine-assessment-origin-v1",
                    "kind": kind,
                    "source_manifest": evidence_manifest(
                        source,
                        binding=binding,
                        item_count=1,
                        parts=(
                            parts := partition_evidence(
                                source, binding=binding, item_count=1
                            )
                        ),
                        storage_ids=tuple(str(100 + i) for i in range(len(parts))),
                    ),
                    "inventory_manifest": evidence_manifest(
                        inventory,
                        binding=binding,
                        item_count=3,
                        parts=(
                            parts := partition_evidence(
                                inventory, binding=binding, item_count=3
                            )
                        ),
                        storage_ids=tuple(str(200 + i) for i in range(len(parts))),
                    ),
                }
                origin["scope_sha256"] = origin_scope(origin)
                origins.append(origin)
                inventories[origin["scope_sha256"]] = inventory
            pending = pending_assessments(
                {
                    "schema_version": "quarantine-published-origins-v1",
                    "origins": sorted(origins, key=lambda row: row["scope_sha256"]),
                },
                inventories,
                {
                    "schema_version": "quarantine-assessment-state-v1",
                    "scope_sha256": "0" * 64,
                    "settlements": [],
                },
                scope="0" * 64,
            )
            self.assertEqual(len(pending), 6)
            self.assertEqual(len({instance for _, instance in pending}), 3)
            wrong_inventory = copy.deepcopy(inventories)
            wrong_inventory[next(iter(wrong_inventory))]["assessments"][0][
                "concern"
            ] = "f" * 64
            with self.assertRaisesRegex(ReviewInputError, "differs from origin"):
                pending_assessments(
                    {
                        "schema_version": "quarantine-published-origins-v1",
                        "origins": sorted(origins, key=lambda row: row["scope_sha256"]),
                    },
                    wrong_inventory,
                    {
                        "schema_version": "quarantine-assessment-state-v1",
                        "scope_sha256": "0" * 64,
                        "settlements": [],
                    },
                    scope="0" * 64,
                )
            self.assertEqual(
                self.read(fixture, record, scope, source), (source, inventory)
            )
            self.assertEqual(fixture.comment["body"], before)
            self.assertTrue(
                all(method == "GET" for _, method, _ in fixture.trace[trace:])
            )
            saved = copy.deepcopy(fixture.reviews)
            for fault in ("body", "producer", "head", "duplicate"):
                fixture.reviews = copy.deepcopy(saved)
                if fault == "body":
                    fixture.reviews[-1]["body"] += "tampered"
                elif fault == "producer":
                    fixture.reviews[-1]["user"]["id"] += 1
                elif fault == "head":
                    fixture.reviews[-1]["commit_id"] = "e" * 40
                else:
                    fixture.reviews.append(copy.deepcopy(fixture.reviews[-1]))
                with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                    self.read(fixture, record, scope, source)
            fixture.reviews = saved
            for fault in ("version", "criterion"):
                mutated = copy.deepcopy(source)
                if fault == "version":
                    mutated["schema_version"] = (
                        "quarantine-lifecycle-published-assessment-source-v99"
                    )
                else:
                    mutated["record"]["finding_lifecycle"]["manifest"]["sha256"] = (
                        "a" * 64
                    )
                with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                    self.read(fixture, record, scope, mutated)

    def test_actual_prior_run_archive_selects_complete_historical_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, original, _scope = self.produce(directory)
            fixture.next_run()

            def stop():
                raise KeyboardInterrupt()

            fixture.before_provider = stop
            self.assertEqual(fixture.invoke(), 130, fixture.error)
            previous = parse_session_comment(
                fixture.comment["body"], identity=fixture.selected.identity
            )
            scope = CurrentReadScope(
                fixture.selected.ledger,
                previous.identity(),
                fixture.live_base,
                fixture.live_head,
                "synthetic-workflow-read",
            )
            before = fixture.comment["body"]
            source, inventory = read_published_source(
                ledger=fixture.selected.ledger,
                previous=previous,
                current_scope=scope,
                complete_inventory=True,
            )
            self.assertEqual(source["record"]["record_sha256"], original.record_sha256)
            self.assertEqual(len(inventory["assessments"]), 3)
            self.assertEqual(fixture.comment["body"], before)
            self.assertEqual(
                read_published_source(
                    ledger=fixture.selected.ledger,
                    previous=previous,
                    current_scope=scope,
                    retained_source=source,
                    complete_inventory=True,
                ),
                (source, inventory),
            )

    def test_external_carrier_is_freshly_authenticated_not_served_from_prior_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, scope = self.produce(directory, rich=True)
            manifest = record.finding_lifecycle["manifest"]
            self.assertEqual(manifest["encoding"], "partitioned-json-v1")
            source, inventory = self.read(fixture, record, scope)
            self.assertEqual(len(inventory["assessments"]), 3)
            identifier = int(manifest["parts"][0]["storage_id"])
            saved = copy.deepcopy(fixture.extra_comments[identifier])
            for fault in ("body", "owner", "missing"):
                fixture.extra_comments[identifier] = copy.deepcopy(saved)
                if fault == "body":
                    fixture.extra_comments[identifier]["body"] += "altered"
                elif fault == "owner":
                    fixture.extra_comments[identifier]["user"]["id"] += 1
                else:
                    del fixture.extra_comments[identifier]
                    fixture.absent_part_id = identifier
                with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                    self.read(fixture, record, scope, source)
            fixture.extra_comments[identifier] = saved

    def test_dependency_carrier_is_not_mistaken_for_self_contained_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, scope = self.produce(directory)
            # Decoder-boundary negative only; this does not pretend the fixture
            # itself produced a v4 dependency or grant it historical authority.
            with patch(
                "review_sensei.finding_lifecycle_store._decode_selected_lifecycle",
                return_value={"schema_version": "finding-lifecycle-packed-v4"},
            ):
                with self.assertRaisesRegex(ReviewInputError, "self-contained v3"):
                    self.read(fixture, record, scope)
