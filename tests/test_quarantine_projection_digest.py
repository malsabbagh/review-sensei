"""Current command projection proof never borrows historical source authority."""

import copy
import re
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import quarantine_action
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
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
        for fault in (
            None,
            "missing-result",
            "changed-result",
            "foreign-transaction",
            "stale-root",
        ):
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
                    authenticated = fixture.selected.ledger._quarantine_authenticated
                    # Historical targets come from authenticated origins, not
                    # the fresh index. The old fixture depended on the namespace
                    # contamination that republished old obligations as fresh.
                    published_scope = next(
                        origin["scope_sha256"]
                        for origin in authenticated.archive["assessment_origins"][
                            "origins"
                        ]
                        if origin["kind"] == "published-assessment"
                    )
                    pending = next(
                        p
                        for p in view["pending"]
                        if p["origin_scope_sha256"] == published_scope
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

                    def check(ledger, record, **kwargs):
                        observed.append(record)
                        if fault == "stale-root":
                            return original(
                                ledger,
                                record.evolve(generation=record.generation + 1),
                                **kwargs,
                            )
                        if fault in {
                            "missing-result",
                            "changed-result",
                            "foreign-transaction",
                        }:
                            load = ledger.load_retained_checkpoint_result

                            def changed(*args, **kwargs):
                                result = load(*args, **kwargs)
                                if fault == "missing-result":
                                    return None
                                if fault == "foreign-transaction":
                                    tx = result.transaction
                                    identity = {
                                        key: getattr(tx, key)
                                        for key in (
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
                                    identity["reservation_id"] = "f" * 64
                                    return replace(
                                        result,
                                        transaction=replace(
                                            tx,
                                            reservation_id=identity["reservation_id"],
                                            transaction_id=tx.derive_id(**identity),
                                        ),
                                    )
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
                                return original(ledger, record, **kwargs)
                        return original(ledger, record, **kwargs)

                    collect = quarantine_action._current_collision_pairs
                    hidden_observed = []

                    def hidden_membership(retained, state, head_sha):
                        # Pure projection boundary using the actual authenticated
                        # state: omitted latest output/hidden active placement
                        # cannot remove an unresolved original criterion.
                        hidden = replace(state, active_entries=())
                        missing_latest = replace(
                            retained, transaction=None, comments=()
                        )
                        pairs = collect(missing_latest, hidden, head_sha)
                        for obligation in state.inventory.obligations:
                            if obligation.unresolved:
                                criterion = obligation.criterion
                                self.assertIn(
                                    (
                                        criterion.obligation_id,
                                        criterion.resolution_criterion,
                                    ),
                                    pairs,
                                )
                        hidden_observed.append(pairs)
                        return collect(retained, state, head_sha)

                    with (
                        patch.object(
                            quarantine_action,
                            "_current_publication_digest",
                            side_effect=check,
                        ),
                        patch.object(
                            quarantine_action,
                            "_current_collision_pairs",
                            side_effect=hidden_membership,
                        ),
                    ):
                        status = fixture.invoke_command()
                    self.assertEqual(len(observed), 1, fixture.error)
                    self.assertEqual(status, 0 if fault is None else 1, fixture.error)
                    if fault is None:
                        self.assertTrue(hidden_observed)
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

    def test_changed_current_native_digest_cannot_authorize_historical_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                before = fixture.current_record()
                eligibility = approval_eligibility_from_body(
                    fixture.reviews[-1]["body"]
                )
                pending = eligibility.quarantine_assessments["pending"][0]
                original_body = fixture.reviews[-1]["body"]
                fixture.reviews[-1]["body"] = re.sub(
                    r"<!-- reviewsensei:eligibility:v1 [^>]+ -->",
                    approval_eligibility_marker(
                        replace(eligibility, result_digest="f" * 64)
                    ),
                    original_body,
                )
                self.assertNotEqual(fixture.reviews[-1]["body"], original_body)
                native_before = copy.deepcopy(fixture.reviews)
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + pending["instance"]
                    + ' --reason "quarantine:'
                    + pending["origin_scope_sha256"]
                    + ' This exact historical concern has independent source evidence."',
                    new_run=True,
                )
                self.assertEqual(fixture.invoke_command(), 1, fixture.error)
                after = fixture.current_record()
                self.assertEqual(after.quarantine_lineage, before.quarantine_lineage)
                self.assertEqual(after.dispositions, before.dispositions)
                self.assertEqual(fixture.reviews, native_before)
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))

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
