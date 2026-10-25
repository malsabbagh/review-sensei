"""Retained-source reads share a previously confirmed caller tranche.

This is a reader/transport regression with an authenticated synthetic seed, not
an implementation or qualification of historical run-accounting rollover.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.api_run_host import HOSTED_API_STARTUP_PLAN
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.rescan import resolve_rescan
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
    initial_accounting,
)
from review_sensei.bounded_evidence import EvidenceReadBudget
from tests import test_retained_full_rescan as retained_fixture
from tests.default_human_inventory_contract import APP, IDENTITY


class CallerRescanReadTests(unittest.TestCase):
    def test_actual_resolver_and_reader_debit_one_existing_caller_tranche(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, local, env, args, source = (
                retained_fixture.RetainedFullRescanTests().prepare(directory)
            )
            original = local.load(IDENTITY).record
            run = ApiRunIdentity(IDENTITY.repository_id, 10)
            accounting = initial_accounting(
                run,
                FrozenApiConfiguration(
                    source.base_sha,
                    hashlib.sha256(b"synthetic frozen config").hexdigest(),
                    128,
                ),
                ApiExecutionOwner(1, "hosted", 0),
                HOSTED_API_STARTUP_PLAN,
                prior_nonstartup_liability_upper_bound=0,
            )
            seeded = original.evolve(api_run_accounting=accounting.to_document())
            state.session_comment = {
                "id": 600,
                "body": render_session_comment(
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    record=seeded,
                ),
                "user": state.app_user,
                "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            }
            original_open = state.http.opener
            caller = ApiRunRequestTransport(
                directory=root,
                run=run,
                plan=HOSTED_API_STARTUP_PLAN,
                attempt=1,
                opener=original_open,
            )
            state.http.opener = caller.open
            native = GitHubIssueCommentSessionLedger(
                state.http, token=local.token, app_slug=APP
            )
            native.load(IDENTITY)  # Independently authenticate the seeded Bot/root.
            caller.reserve_and_activate(
                native,
                IDENTITY,
                lambda record: record.evolve(
                    generation=record.generation + 1,
                    api_run_accounting=accounting.reserve(
                        "rescan-reads", 50
                    ).to_document(),
                ),
                block_id="rescan-reads",
            )
            # The explicit reader guard is preserved; it neither constructs a
            # caller allowance nor replaces the physical pre-dispatch meter.
            native.http.request("GET", "/repos/owner/repo/pulls/1", token=native.token)
            guard = EvidenceReadBudget()
            native.evidence_budget = guard
            before = state.session_comment["body"]
            prior = caller.accounting.confirmed
            self.assertEqual(prior, 1)
            offset = len(caller.observations)
            call_offset = len(state.rescan_calls)
            with (
                patch.dict(os.environ, env),
                patch.object(
                    EvidenceReadBudget,
                    "__init__",
                    side_effect=AssertionError("no fallback read window"),
                ),
            ):
                request = resolve_rescan(
                    args, native, actions_token="synthetic-actions"
                )
            self.assertEqual(request.mode, "full")
            self.assertEqual(request.source, source)
            reads = len(state.rescan_calls) - call_offset
            self.assertGreater(reads, 13)  # Real remote ledger fences are included.
            self.assertEqual(len(caller.observations) - offset, reads)
            self.assertEqual(caller.accounting.confirmed - prior, reads)
            self.assertEqual(caller.usable, 50 - prior - reads)
            self.assertTrue(
                all(method == "GET" for method, _ in state.rescan_calls[call_offset:])
            )
            self.assertEqual(state.session_comment["body"], before)
            self.assertEqual(
                len(parse_session_comment(before, identity=IDENTITY).dispositions), 20
            )
            self.measurement = {
                "requests": reads,
                "ordinary_confirmed": caller.accounting.confirmed,
                "ordinary_usable": caller.usable,
                "startup_liability": HOSTED_API_STARTUP_PLAN.liability,
                "unallocated": caller.accounting.remaining,
                "decisions_preserved": 20,
                "root_unchanged": state.session_comment["body"] == before,
                "same_transport": True,
                "fallback_budget_constructors": 0,
                "scope": "isolated authenticated reader/transport seam; historical run rollover not qualified",
            }
