"""Distinct read-only native capacity producer; not the old finalizer experiment."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.action_run import execute_command


class PreentryCapacityTests(unittest.TestCase):
    def test_source_capacity_observation_before_intent_is_not_admission(self):
        from dataclasses import replace

        from review_sensei_github import action_run
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.trust_disposition import (
            DispositionPreparation,
            observe_disposition_capacity,
            prepare_source_disposition,
        )
        from review_sensei_github.trust_effects import enable

        from review_sensei.errors import ReviewInputError
        from tests.test_trust_pause_cold import CurrentRoleEntryFixture

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentRoleEntryFixture(directory)
            f.provider_comments = [
                dict(
                    path="src/app.py",
                    line=2,
                    side="RIGHT",
                    body="Confirm this exact interface assumption.",
                    severity="high",
                    blocking=False,
                    needs_human=True,
                )
            ]
            self.assertEqual(f.opted(f.invoke), 0, f.error)
            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=f.requested_head,
            )
            finding = "RS-" + index.entries[0].instance[:6].upper()
            f.select_command(
                "@reviewsensei override " + finding + " acceptable risk", new_run=True
            )
            captured = []
            with patch.object(
                action_run,
                "execute_command",
                lambda run, args: captured.append((run, args)) or 0,
            ):
                self.assertEqual(f.opted(f.invoke_command), 0, f.error)
            run, args = captured[0]
            broker = BrokerClient(
                broker_url=run.broker.broker_url,
                control=run.broker.control,
                opener=run.caller.open,
                authority={**run.broker.authority, "version": 4},
            )
            session = broker.open_session(
                "synthetic-oidc",
                repository_id=123,
                pull_request=7,
                head_sha=run.head_sha,
                observe_actor_role=True,
            )
            run = replace(
                run, session_authority=session.attestation, session_grant=session.grant
            )
            enable(run.caller, broker, session, api_url=run.http.api_url)
            before, root, start = (
                copy.deepcopy(f.comment),
                run.caller._root,
                len(f.trace),
            )
            invoked = []

            def forged_intent(**kwargs):
                invoked.append(True)
                return {"purpose": "forged"}

            with patch.object(run.caller, "action_intent_document", forged_intent):
                with self.assertRaises(ReviewInputError):
                    prepare_source_disposition(run, args)
            self.assertEqual(invoked, [])
            self.assertEqual(f.comment, before)
            self.assertEqual(run.caller._root, root)
            self.assertIsNone(run.caller.accounting.action)
            # Separate paid collection for the subsequent actual source receipt.
            start = len(f.trace)
            provider_calls = f.provider_calls
            observation_start = len(run.caller.observations)
            receipt = prepare_source_disposition(run, args)
            for clone in (
                copy.copy(receipt),
                DispositionPreparation(receipt.root, receipt.intent_sha256),
            ):
                with self.assertRaises(ReviewInputError):
                    observe_disposition_capacity(clone)
            for field, changed in (
                ("app_slug", "foreign[bot]"),
                ("source_comment_id", -1),
            ):
                before_reads = len(run.caller.observations)
                with patch.object(args, field, changed):
                    with self.assertRaisesRegex(ReviewInputError, "custody changed"):
                        observe_disposition_capacity(receipt)
                self.assertEqual(len(run.caller.observations), before_reads)
            object.__setattr__(receipt, "root", "f" * 64)
            try:
                with self.assertRaisesRegex(ReviewInputError, "custody changed"):
                    observe_disposition_capacity(receipt)
            finally:
                object.__setattr__(receipt, "root", root)
            plan = observe_disposition_capacity(receipt)
            plan.require(plan.journal, http=plan.http, budget=plan.budget)
            before_sizing = len(run.caller.observations)
            original_effects = copy.deepcopy(plan.journal.effects)
            branches, pending, item_count, requests = (
                plan.journal._command_branch_documents(
                    plan, http=plan.http, budget=plan.budget
                )
            )
            self.assertTrue(branches)
            self.assertTrue(pending)
            self.assertTrue(requests)
            self.assertGreater(item_count, len(original_effects))
            self.assertEqual(len(plan.journal.effects), len(original_effects))
            self.assertEqual(plan.journal.effects, original_effects)
            self.assertEqual(len(run.caller.observations), before_sizing)
            self.assertIsNone(run.caller.accounting.action)
            count = len(run.caller.observations)
            with self.assertRaisesRegex(ReviewInputError, "unissued"):
                observe_disposition_capacity(receipt)
            self.assertEqual(len(run.caller.observations), count)
            self.assertEqual(plan.record.record_sha256, root)
            self.assertIsNone(plan.record.api_run_accounting.get("action"))
            self.assertEqual(f.provider_calls, provider_calls)
            observations = run.caller.observations[observation_start:]
            self.assertTrue(observations)
            self.assertTrue(all(item["outcome"] == "response" for item in observations))
            # The existing action-source verification and capability exchange
            # are the two control POSTs. Neither is a native mutation.
            # The actual source GraphQL discovery is charged, but not a mutation.
            self.assertEqual(sum(item["mutation"] for item in observations), 2)
            self.assertEqual(len(observations), len(f.trace[start:]))
            self.assertEqual(
                [
                    path
                    for _, method, path in f.trace[start:]
                    if method == "POST" and path != "/graphql"
                ],
                ["/api/github/session-grant", "/api/github/token"],
            )
            self.assertTrue(
                any(
                    method == "POST" and path == "/graphql"
                    for _, method, path in f.trace[start:]
                )
            )
            self.assertEqual(f.comment, before)
            self.assertEqual(run.caller._root, root)
            self.assertIsNone(run.caller.accounting.action)
            self.assertFalse(
                any(
                    method != "GET" and path.startswith("/repos/")
                    for _, method, path in f.trace[start:]
                )
            )
            self.assertEqual(run.caller.accounting.configuration.limit, 512)
            # A truthful detached preview never creates a native permit.
            with self.assertRaises(ReviewInputError):
                execute_command(run, args)
            self.assertEqual(f.comment, before)
