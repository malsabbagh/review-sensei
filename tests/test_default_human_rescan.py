"""Explicit rescan uses independently retained default human authority."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import test_default_human_inventory as inventory_acceptance
from default_human_inventory_contract import (
    APP,
    HEAD,
    IDENTITY,
    DefaultHumanState,
    human_checkpoint,
    human_inventory_parts,
    publish_human,
)
from fake_github_http import json_response
from review_sensei_github.rescan import resolve_rescan

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger


class DefaultHumanRescanTests(unittest.TestCase):
    def prepare(self, directory, *, count=77, detail=3, gate="partial"):
        path = Path(directory) / "ledger"
        result, key = human_checkpoint(
            LocalSessionLedger(path, enable_partition_writes=True),
            count=count,
            detail=detail,
            gate=gate,
        )
        state = DefaultHumanState(result.comments)
        publish_human(state, LocalSessionLedger(path), result, key, gate=gate)
        state.reviews[-1]["submitted_at"] = "2026-10-19T00:00:00Z"
        original = state.http.opener

        state.rescan_calls = []

        def opener(request, timeout):
            state.rescan_calls.append((request.method, request.full_url))
            suffix = urlsplit(request.full_url).path
            if suffix == "/repos/owner/repo/pulls/1":
                return json_response({**state.pr(), "number": 1})
            if suffix.endswith("/collaborators/alice/permission"):
                return json_response({"permission": "maintain"})
            if suffix.endswith("/actions/runs/10"):
                return json_response(
                    {
                        "id": 10,
                        "event": "workflow_dispatch",
                        "repository": {"id": 1},
                        "actor": {"login": "alice"},
                        "created_at": "2026-10-19T12:00:00Z",
                        "path": ".github/workflows/review-sensei-review.yml",
                    }
                )
            return original(request, timeout)

        state.http.opener = opener
        ledger = LocalSessionLedger(path)
        ledger.http, ledger.token = state.http, "synthetic"
        event = Path(directory) / "event.json"
        event.write_text(
            json.dumps(
                {
                    "repository": {"id": 1, "full_name": "owner/repo"},
                    "sender": {"login": "alice", "type": "User"},
                    "inputs": {
                        "pull_request_number": "1",
                        "operation": "review",
                        "rescan": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_ACTOR": "alice",
            "GITHUB_TRIGGERING_ACTOR": "alice",
            "GITHUB_RUN_ID": "10",
        }
        args = SimpleNamespace(
            repository=IDENTITY.repository,
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        return state, ledger, env, args

    def test_actual_partial_schema5_publication_can_authorize_explicit_rescan(self):
        for count, detail in ((77, 3), (100, 2), (250, 1)):
            with self.subTest(count=count):
                if sys.platform == "win32":
                    inventory_acceptance.DefaultHumanInventoryTests().assert_windows_rich_publication_hold(
                        count=count, detail=detail
                    )
                    continue
                with tempfile.TemporaryDirectory() as directory:
                    state, ledger, env, args = self.prepare(
                        directory, count=count, detail=detail
                    )
                    budget = ledger.evidence_budget
                    before = len(state.rescan_calls)
                    with patch.dict(os.environ, env):
                        authorized = resolve_rescan(
                            args, ledger, actions_token="actions"
                        )
                    self.assertEqual(authorized.request_id, 10)
                    self.assertEqual(authorized.review_id, state.reviews[-1]["id"])
                    self.assertIs(ledger.evidence_budget, budget)
                    self.assertLessEqual(budget.calls, 60)
                    self.assertEqual(len(state.rescan_calls) - before, budget.calls)
                    self.assertTrue(
                        all(call[0] == "GET" for call in state.rescan_calls[before:])
                    )

    def test_complete_schema5_publication_is_not_reopened_by_rescan(self):
        if sys.platform == "win32":
            inventory_acceptance.DefaultHumanInventoryTests().assert_windows_rich_publication_hold()
            return
        with tempfile.TemporaryDirectory() as directory:
            state, ledger, env, args = self.prepare(directory, gate=None)
            with patch.dict(os.environ, env):
                self.assertIsNone(resolve_rescan(args, ledger, actions_token="actions"))
            self.assertTrue(all(call[0] == "GET" for call in state.rescan_calls))

    def test_exhausted_original_budget_does_not_refresh_for_rescan(self):
        if sys.platform == "win32":
            inventory_acceptance.DefaultHumanInventoryTests().assert_windows_rich_publication_hold()
            return
        for exhausted in ("calls", "deadline"):
            with (
                self.subTest(exhausted=exhausted),
                tempfile.TemporaryDirectory() as directory,
            ):
                state, ledger, env, args = self.prepare(directory)
                clock = [0.0]
                budget = EvidenceReadBudget(clock=lambda: clock[0])
                ledger.evidence_budget = budget
                if exhausted == "calls":
                    for _ in range(60):
                        budget.consume()
                else:
                    clock[0] = 61.0
                before = len(state.rescan_calls)
                with patch.dict(os.environ, env), self.assertRaises(ReviewInputError):
                    resolve_rescan(args, ledger, actions_token="actions")
                self.assertEqual(len(state.rescan_calls), before)
                self.assertIs(ledger.evidence_budget, budget)
                self.assertEqual(budget.calls, 60 if exhausted == "calls" else 0)

    def test_changed_parts_owner_or_visible_prose_cannot_authorize_rescan(self):
        if sys.platform == "win32":
            inventory_acceptance.DefaultHumanInventoryTests().assert_windows_rich_publication_hold()
            return
        for fault in ("prose", "part", "owner"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state, ledger, env, args = self.prepare(directory)
                if fault == "prose":
                    state.inline[0]["body"] = "original explanation removed"
                elif fault == "part":
                    state.parts.remove(human_inventory_parts(state)[0])
                else:
                    state.reviews[-1]["user"]["id"] = 100
                before = len(state.rescan_calls)
                with patch.dict(os.environ, env), self.assertRaises(ReviewInputError):
                    resolve_rescan(args, ledger, actions_token="actions")
                self.assertTrue(
                    all(call[0] == "GET" for call in state.rescan_calls[before:])
                )
