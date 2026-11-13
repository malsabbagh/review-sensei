"""Current command projection proof never borrows historical source authority."""

import copy
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import quarantine_action
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    parse_finding_index,
)
from review_sensei_github.quarantine_native_scope import _authenticated_origin_digest

from review_sensei.errors import ReviewInputError
from tests import test_quarantine_lifecycle_origin as origin_fixtures
from tests.test_quarantine_journey import JourneyFixture, registered_resource


class QuarantineProjectionDigestTests(unittest.TestCase):
    def test_journey_import_aliases_share_synthetic_execution_sequence(self):
        import importlib

        bare = importlib.import_module("test_quarantine_journey")
        qualified = importlib.import_module("tests.test_quarantine_journey")
        self.assertIs(bare._RUNS, qualified._RUNS)

    def test_current_collision_authenticates_projection_and_rejects_bad_sources(self):
        for fault in (None, "missing-result", "changed-result", "stale-root"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = JourneyFixture(directory)
                old, registration = fixture.original_origins()
                fixture.provider_comments = [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Original published human assessment 0 needs external source validation.",
                        "severity": "high",
                        "blocking": False,
                        "needs_human": True,
                    }
                ]
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                    before = fixture.current_record()
                    view = approval_eligibility_from_body(
                        fixture.reviews[-1]["body"]
                    ).quarantine_assessments
                    index = parse_finding_index(
                        fixture.reviews[-1]["body"],
                        repository_id=123,
                        pull_request=7,
                        head_sha=fixture.live_head,
                    )
                    authenticated = fixture.selected.ledger._quarantine_authenticated
                    pending = next(
                        p
                        for p in view["pending"]
                        if any(
                            entry.instance == p["instance"]
                            or entry.lifecycle == item["concern"]
                            for item in authenticated.inventories[
                                p["origin_scope_sha256"]
                            ]["assessments"]
                            if item["instance"] == p["instance"]
                            for entry in index.entries
                        )
                    )
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command(
                        "@reviewsensei dismiss "
                        + pending["instance"]
                        + ' --reason "quarantine:'
                        + pending["origin_scope_sha256"]
                        + ' This exact historical concern has independently verified source evidence."',
                        new_run=True,
                    )
                    original = quarantine_action._current_publication_digest
                    observed = []

                    def check(ledger, record):
                        observed.append(record)
                        if fault == "stale-root":
                            return original(
                                ledger, record.evolve(generation=record.generation + 1)
                            )
                        if fault in {"missing-result", "changed-result"}:
                            load = ledger.load_retained_checkpoint_result

                            def changed(*args, **kwargs):
                                result = load(*args, **kwargs)
                                if fault == "missing-result":
                                    return None
                                changed_result = replace(
                                    result,
                                    transaction=None,
                                    summary="substituted analysis",
                                )
                                return replace(
                                    changed_result,
                                    transaction=replace(
                                        result.transaction,
                                        result_sha256=changed_result.content_digest(),
                                    ),
                                )

                            # Fault the authenticated-result reader's return at
                            # the consumer seam, not its authority checks or the
                            # lifecycle reader. No altered result is published.
                            with patch.object(
                                ledger,
                                "load_retained_checkpoint_result",
                                side_effect=changed,
                            ):
                                return original(ledger, record)
                        return original(ledger, record)

                    with patch.object(
                        quarantine_action,
                        "_current_publication_digest",
                        side_effect=check,
                    ):
                        status = fixture.invoke_command()
                    self.assertEqual(len(observed), 1, fixture.error)
                    self.assertEqual(status, 0 if fault is None else 1, fixture.error)
                    if fault is None:
                        self.assertIn("collides with a fresh finding", fixture.stdout)
                    elif fault != "stale-root":
                        self.assertIn(
                            "retained publication source conflicts", fixture.error
                        )
                    else:
                        self.assertIn(
                            "current session authority conflicts", fixture.error
                        )
                    after = fixture.current_record()
                    self.assertEqual(
                        after.quarantine_lineage, before.quarantine_lineage
                    )
                    self.assertEqual(after.dispositions, before.dispositions)
                    self.assertFalse(
                        any(r["state"] == "APPROVED" for r in fixture.reviews)
                    )
                    self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                    self.assertEqual(
                        after.api_run_accounting["configuration"]["limit"], 512
                    )

    def test_authenticated_historical_native_digest_uses_original_projection(self):
        producer = origin_fixtures.LifecycleOriginTests()
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, scope = producer.produce(directory)
            source, _ = producer.read(fixture, record, scope)
            index = parse_finding_index(
                source["review_body"],
                repository_id=record.repository_id,
                pull_request=record.pull_request,
                head_sha=record.transaction.head_sha,
            )
            self.assertEqual(
                _authenticated_origin_digest(source, record), index.result_digest
            )
            for fault in ("version", "head", "body"):
                changed = copy.deepcopy(source)
                if fault == "version":
                    changed["schema_version"] = (
                        "quarantine-published-assessment-source-v1"
                    )
                elif fault == "head":
                    changed["review_body"] = changed["review_body"].replace(
                        record.transaction.head_sha, "f" * 40
                    )
                else:
                    changed["review_body"] = "no finding index"
                with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                    _authenticated_origin_digest(changed, record)
