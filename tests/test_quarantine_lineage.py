"""Closed quarantine values retain independent origin-scoped obligations."""

import hashlib
import json
import unittest
from copy import deepcopy

from review_sensei.bounded_evidence import (
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError


def lineage_value():
    binding = {
        "repository": "owner/repo",
        "repository_id": 123,
        "pull_request": 7,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "policy_digest": "c" * 64,
        "configuration_digest": "d" * 64,
        "generation": 9,
        "producer": "github-bot:99",
        "purpose": "evidence",
        "schema_version": "1.0",
    }
    archive = {"schema_version": "synthetic-origin-for-value-validation"}
    parts = partition_evidence(archive, binding=binding, item_count=1)
    value = {
        "schema_version": "quarantine-lineage-v1",
        "predecessor_comment_id": 41,
        "predecessor_record_sha256": "e" * 64,
        "creation_run_id": 900,
        "registration_sha256": "f" * 64,
        "origin_count": 2,
        "origin_manifest": evidence_manifest(
            archive,
            binding=binding,
            item_count=1,
            parts=parts,
            storage_ids=("42",),
        ),
    }
    # Independent documented scope formula, before adding mutable state.
    value["scope_sha256"] = hashlib.sha256(
        canonical_bytes(
            {
                "domain": "quarantine-lineage-v1",
                "origin": dict(value),
            }
        )
    ).hexdigest()
    value["assessment_state_manifest"] = inline_manifest(
        {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": value["scope_sha256"],
            "settlements": [],
        },
        binding={**binding, "policy_digest": value["scope_sha256"]},
        item_count=0,
    )
    return value


def decision(origin):
    return {
        "origin_scope_sha256": origin,
        "instance": "1" * 64,
        "action": "dismiss",
        "actor": "alice",
        "role": "maintain",
        "head_sha": "2" * 40,
        "source_id": 1000,
        "source_updated_at": "2026-10-28T00:00:00Z",
        "source_body_sha256": "3" * 64,
        "reason": "The supplied test evidence disproves this specific concern.",
        "policy_sha256": "4" * 64,
    }


class QuarantineLineageValueTests(unittest.TestCase):
    def test_quarantined_record_uses_distinct_versioned_host_marker(self):
        from review_sensei_github.api_run_host import review_startup_plan
        from review_sensei_github.session_ledger import (
            parse_session_comment,
            render_session_comment,
        )

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
            initial_accounting,
        )
        from review_sensei.session import SessionIdentity, SessionRecord

        identity = SessionIdentity("owner/repo", 7, repository_id=123)
        state = initial_accounting(
            ApiRunIdentity(123, 900),
            FrozenApiConfiguration("c" * 40, "d" * 64, 512),
            ApiExecutionOwner(1, "hosted", 0),
            review_startup_plan("hosted"),
            prior_nonstartup_liability_upper_bound=0,
        )
        record = SessionRecord.create(
            identity,
            generation=10,
            api_run_accounting=state.to_document(),
            quarantine_lineage=lineage_value(),
        )
        body = render_session_comment(repository_id=123, pull_request=7, record=record)
        self.assertTrue(
            body.rstrip().splitlines()[-1].startswith("<!-- reviewsensei:session:v2 ")
        )
        self.assertEqual(parse_session_comment(body, identity=identity), record)
        self.assertIsNone(
            parse_session_comment(
                body.replace("session:v2 ", "session:v1 "), identity=identity
            )
        )

    def test_native_discovery_refuses_unverified_quarantine_before_any_write(self):
        from fake_github_http import json_response, make_http
        from review_sensei_github.api_run_host import review_startup_plan
        from review_sensei_github.session_ledger import (
            GitHubIssueCommentSessionLedger,
            render_session_comment,
        )

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
            initial_accounting,
        )
        from review_sensei.session import SessionIdentity, SessionRecord

        identity = SessionIdentity("owner/repo", 7, repository_id=123)
        state = initial_accounting(
            ApiRunIdentity(123, 900),
            FrozenApiConfiguration("c" * 40, "d" * 64, 512),
            ApiExecutionOwner(1, "hosted", 0),
            review_startup_plan("hosted"),
            prior_nonstartup_liability_upper_bound=0,
        )
        record = SessionRecord.create(
            identity,
            generation=10,
            api_run_accounting=state.to_document(),
            quarantine_lineage=lineage_value(),
        )
        body = render_session_comment(repository_id=123, pull_request=7, record=record)
        http, calls = make_http(
            [
                json_response(
                    [
                        {
                            "id": 50,
                            "body": body,
                            "user": {
                                "id": 99,
                                "login": "reviewsensei[bot]",
                                "type": "Bot",
                            },
                        }
                    ]
                )
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="synthetic", app_slug="reviewsensei[bot]"
        )
        result = ledger.load(identity)
        self.assertEqual(result.status, "integrity-failed")
        self.assertIsNone(result.record)
        self.assertIn("authenticated provenance", result.detail)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(method == "GET" for method, _, _ in calls))

    def test_session_digest_retains_lineage_and_disallows_in_place_conversion(self):
        from review_sensei_github.api_run_host import review_startup_plan

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
            initial_accounting,
        )
        from review_sensei.session import SessionIdentity, SessionRecord

        identity = SessionIdentity("owner/repo", 7, repository_id=123)
        state = initial_accounting(
            ApiRunIdentity(123, 900),
            FrozenApiConfiguration("c" * 40, "d" * 64, 512),
            ApiExecutionOwner(1, "hosted", 0),
            review_startup_plan("hosted"),
            prior_nonstartup_liability_upper_bound=0,
        )
        value = lineage_value()
        record = SessionRecord.create(
            identity,
            generation=10,
            api_run_accounting=state.to_document(),
            quarantine_lineage=value,
        )
        self.assertEqual(SessionRecord.from_dict(record.to_dict()), record)
        evolved = record.evolve(generation=11)
        self.assertEqual(evolved.quarantine_lineage, value)
        with self.assertRaises(ReviewInputError):
            record.evolve(quarantine_lineage=None)
        malformed = record.to_dict()
        malformed["quarantine_lineage"]["predecessor_record_sha256"] = "0" * 64
        with self.assertRaises(ReviewInputError):
            SessionRecord.from_dict(malformed)
        ordinary = SessionRecord.create(
            identity, api_run_accounting=state.to_document()
        )
        with self.assertRaisesRegex(ReviewInputError, "separately created"):
            ordinary.evolve(quarantine_lineage=value)

    def test_reference_roundtrip_cannot_drop_or_rewrite_origin(self):
        from review_sensei.quarantine_lineage import (
            normalize_lineage,
            require_lineage_successor,
        )

        value = lineage_value()
        self.assertEqual(normalize_lineage(json.loads(json.dumps(value))), value)
        require_lineage_successor(value, deepcopy(value))
        for altered in (
            None,
            {**value, "predecessor_comment_id": 40},
            {**value, "origin_count": 3},
            {**value, "unknown": True},
        ):
            with (
                self.subTest(altered=altered is None),
                self.assertRaises(ReviewInputError),
            ):
                require_lineage_successor(value, altered)
        with self.assertRaisesRegex(ReviewInputError, "separately created"):
            require_lineage_successor(None, value)

    def test_equal_fingerprints_in_distinct_origins_are_independent_decisions(self):
        from review_sensei.quarantine_lineage import validate_assessment_state

        value = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [decision("a" * 64), decision("b" * 64)],
        }
        self.assertEqual(
            validate_assessment_state(value, scope="0" * 64, item_count=2), value
        )
        value["settlements"].append(decision("a" * 64))
        with self.assertRaisesRegex(ReviewInputError, "duplicated"):
            validate_assessment_state(value, scope="0" * 64, item_count=3)

    def test_malformed_decision_and_unknown_origin_count_refuse(self):
        from review_sensei.quarantine_lineage import (
            normalize_lineage,
            validate_assessment_state,
        )

        for change in (
            {"actor": "bad/user"},
            {"head_sha": "x" * 40},
            {"source_updated_at": "2026-99-99T00:00:00Z"},
            {"source_id": True},
            {"instance": "1" * 6},
        ):
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                validate_assessment_state(
                    {
                        "schema_version": "quarantine-assessment-state-v1",
                        "scope_sha256": "0" * 64,
                        "settlements": [{**decision("a" * 64), **change}],
                    },
                    scope="0" * 64,
                    item_count=1,
                )
        with self.assertRaises(ReviewInputError):
            normalize_lineage({**lineage_value(), "origin_count": 3})

    def test_inline_decisions_cannot_roll_back_or_rewrite_earlier_items(self):
        from review_sensei.quarantine_lineage import require_lineage_successor

        empty = lineage_value()

        def with_decisions(items):
            value = deepcopy(empty)
            value["assessment_state_manifest"] = inline_manifest(
                {
                    "schema_version": "quarantine-assessment-state-v1",
                    "scope_sha256": value["scope_sha256"],
                    "settlements": items,
                },
                binding=empty["assessment_state_manifest"]["binding"],
                item_count=len(items),
            )
            return value

        one = with_decisions([decision("a" * 64)])
        two = with_decisions([decision("a" * 64), decision("b" * 64)])
        require_lineage_successor(empty, one)
        require_lineage_successor(one, two)
        altered = {
            **decision("a" * 64),
            "reason": "A later rewrite must not replace the original explanation.",
        }
        for after in (
            empty,
            with_decisions([altered]),
            with_decisions([altered, decision("b" * 64)]),
        ):
            with (
                self.subTest(count=after["assessment_state_manifest"]["item_count"]),
                self.assertRaises(ReviewInputError),
            ):
                require_lineage_successor(one, after)

    def test_inline_empty_cursor_has_closed_lineage_and_schema_support(self):
        from review_sensei.quarantine_lineage import normalize_lineage
        from review_sensei.quarantine_state import assessment_cursor
        from review_sensei.schemas import validate_public_document

        value = lineage_value()
        value["assessment_state_manifest"] = inline_manifest(
            assessment_cursor(scope=value["scope_sha256"]),
            binding=value["assessment_state_manifest"]["binding"],
            item_count=0,
        )
        self.assertEqual(normalize_lineage(value), value)
        validate_public_document(value, "quarantine-lineage")


class QuarantineFullAnalysisBindingTests(unittest.TestCase):
    def test_binding_is_exact_to_scope_key_configuration_evidence_and_reservation(self):
        from types import SimpleNamespace

        from review_sensei.quarantine_lineage import (
            full_analysis_reservation,
            is_full_analysis_transaction,
        )
        from review_sensei.session import session_reservation_id

        scope, key_digest = "a" * 64, "b" * 64
        values = dict(
            head_sha="c" * 40,
            policy_digest="d" * 64,
            configuration_digest="e" * 64,
            evidence_digest="f" * 64,
        )
        ordinary = session_reservation_id(
            repository="owner/repo",
            pull_request=7,
            head_sha=values["head_sha"],
            kind="publish:"
            + key_digest
            + ":"
            + values["policy_digest"]
            + ":"
            + values["configuration_digest"]
            + ":"
            + values["evidence_digest"],
        )
        reservation = full_analysis_reservation(scope, ordinary)
        record = SimpleNamespace(
            repository="owner/repo",
            pull_request=7,
            quarantine_lineage={"scope_sha256": scope},
            transaction=SimpleNamespace(**values, reservation_id=reservation),
        )
        key = SimpleNamespace(digest=lambda: key_digest)
        self.assertTrue(is_full_analysis_transaction(record, key))
        for field in values:
            prior = getattr(record.transaction, field)
            setattr(record.transaction, field, "0" * len(prior))
            self.assertFalse(is_full_analysis_transaction(record, key))
            setattr(record.transaction, field, prior)
        self.assertFalse(
            is_full_analysis_transaction(
                record, SimpleNamespace(digest=lambda: "0" * 64)
            )
        )
        record.quarantine_lineage["scope_sha256"] = "0" * 64
        self.assertFalse(is_full_analysis_transaction(record, key))
        self.assertNotEqual(full_analysis_reservation(scope, reservation), reservation)
        for scope_value, reservation_value in (("", ordinary), (scope, "short")):
            with self.assertRaises(ReviewInputError):
                full_analysis_reservation(scope_value, reservation_value)
