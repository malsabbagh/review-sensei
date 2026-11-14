"""Pre-business command sizing does not install preview authority."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from review_sensei_github.action_representability import (
    guard_current_command_representability,
)
from review_sensei_github.application import GitHubApplication

from review_sensei.api_work import ACCOUNTING_VERSION
from review_sensei.disposition import (
    FindingDisposition,
    MaintainerCommand,
    MaintainerCommandResult,
    apply_session_command,
    decode_session_dispositions,
)
from review_sensei.session import LocalSessionLedger, SessionIdentity


class CommandCapacityBindingTests(unittest.TestCase):
    def test_guard_emits_detached_sizing_without_changing_selected_root(self):
        identity = SessionIdentity("owner/repo", 1, repository_id=1)
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            record = ledger.initialize(identity)
            caller = SimpleNamespace(
                accounting=SimpleNamespace(
                    version=ACCOUNTING_VERSION,
                    action=None,
                    configuration=SimpleNamespace(
                        limit=64, commit="a" * 40, digest="d" * 64
                    ),
                    owner=SimpleNamespace(generation=1),
                    blocks=(),
                ),
                usable=50,
                _block="b0",
                _effect_record=record,
                _action_effect_boundary=SimpleNamespace(intent_sha256="3" * 64),
                budget_view=None,
            )
            preview = {"stale": True}
            refusal = guard_current_command_representability(
                current_action=SimpleNamespace(run=SimpleNamespace(caller=caller)),
                ledger=ledger,
                identity=identity,
                command=MaintainerCommand(action="pause", actor="maintainer"),
                head_sha="b" * 40,
                writes_enabled=True,
                capacity_preview=preview,
            )
            self.assertIsNone(refusal)
            self.assertNotIn("stale", preview)
            self.assertTrue(preview["result"].applied)
            self.assertTrue(preview["record"].operator_paused)
            self.assertFalse(ledger.load(identity).record.operator_paused)
            details = preview["details"]
            self.assertEqual(details["original_root"], record.record_sha256)
            self.assertEqual(details["preview_root"], preview["record"].record_sha256)
            self.assertEqual(json.loads(details["dispositions"]), [])
            self.assertIs(caller._effect_record, record)

    def test_preview_and_actual_disposition_share_exact_source_timestamp(self):
        identity = SessionIdentity("owner/repo", 1, repository_id=1)
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            now = datetime(2026, 11, 13, 8, 9, 10, 123456, tzinfo=timezone.utc)
            record = ledger.initialize(identity, now=now)
            caller = SimpleNamespace(
                accounting=SimpleNamespace(
                    version=ACCOUNTING_VERSION,
                    action=None,
                    configuration=SimpleNamespace(
                        limit=64, commit="a" * 40, digest="d" * 64
                    ),
                    owner=SimpleNamespace(generation=1),
                    blocks=(),
                ),
                usable=50,
                _block="b0",
                _effect_record=record,
                _action_effect_boundary=SimpleNamespace(intent_sha256="3" * 64),
                budget_view=None,
            )
            command = MaintainerCommand(
                action="accept-risk",
                actor="maintainer",
                reason="accepted tradeoff",
                finding_fingerprint="f" * 64,
                head_sha="b" * 40,
            )
            preview = {}
            refusal = guard_current_command_representability(
                current_action=SimpleNamespace(run=SimpleNamespace(caller=caller)),
                ledger=ledger,
                identity=identity,
                command=command,
                head_sha="b" * 40,
                writes_enabled=True,
                finding_fingerprints=("f" * 64,),
                actor_role="OWNER",
                capacity_preview=preview,
                now=now,
            )
            self.assertIsNone(refusal)
            actual, outcome = apply_session_command(
                ledger,
                identity,
                command,
                finding_fingerprints=("f" * 64,),
                actor_role="OWNER",
                now=now,
            )
            self.assertTrue(outcome.applied)
            self.assertEqual(
                json.loads(preview["details"]["dispositions"]),
                list(actual.dispositions),
            )

    def test_snapshot_proof_fields_are_retained_outside_decision_projection(self):
        decision = FindingDisposition(
            fingerprint="f" * 64,
            action="accept-risk",
            reason="accepted tradeoff",
            actor="maintainer",
            head_sha="b" * 40,
        )
        row = {**decision.to_dict(), "resolution_kind": "authorized-override"}
        retained = json.dumps(row, sort_keys=True)
        self.assertEqual(decode_session_dispositions((row,)), (decision,))
        self.assertEqual(json.dumps(row, sort_keys=True), retained)

    def test_default_budget_two_finding_command_and_cold_verify_use_one_check_capability(
        self,
    ):
        from urllib.parse import urlsplit

        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.session import read_session_dispositions
        from tests.test_action_run import HEAD, IDENTITY, CommandEntryFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": f"Confirm interface assumption {number}.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for number in range(2)
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            external = fixture.external
            check_reads = []

            def require_check_permission(request, timeout):
                if (
                    fixture.command_active
                    and "/check-runs" in urlsplit(request.full_url).path
                ):
                    self.assertEqual(
                        request.get_header("Authorization"),
                        "Bearer synthetic-check_publish",
                    )
                    if request.method == "GET":
                        check_reads.append(request.full_url)
                return external(request, timeout)

            fixture.external = require_check_permission
            body = (
                "@reviewsensei override "
                + " ".join(
                    "RS-" + entry.instance[:6].upper() for entry in index.entries
                )
                + " acceptable risk"
            )
            for command in (body, "@reviewsensei verify"):
                before_mints = sum(
                    p.get("capability") == "check_publish"
                    for p in fixture.broker_requests
                )
                fixture.SOURCE_ID += 1
                fixture.select_command(command, new_run=True)
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
                self.assertNotIn("before business writes", fixture.stdout)
                current = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(
                    len(read_session_dispositions(fixture.selected.ledger, current)), 2
                )
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "terminal"
                )
                self.assertEqual(
                    sum(
                        p.get("capability") == "check_publish"
                        for p in fixture.broker_requests
                    )
                    - before_mints,
                    1,
                )
            self.assertTrue(check_reads)
            self.assertEqual(len(fixture.resolved_threads), 2)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in fixture.reviews), 1)
            self.assertEqual(fixture.provider_calls, 1)

    def test_default_budget_refuses_known_unfunded_tail_before_business_writes(self):
        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.session import read_session_dispositions
        from tests.test_action_run import HEAD, IDENTITY, CommandEntryFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": f"Confirm interface assumption {number}.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for number in range(20)
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            original = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            before = copy.deepcopy(
                (
                    fixture.reviews,
                    fixture.inline,
                    fixture.check_runs,
                    fixture.resolved_threads,
                )
            )
            fixture.SOURCE_ID += 1
            fixture.select_command(
                "@reviewsensei override "
                + " ".join(
                    "RS-" + entry.instance[:6].upper() for entry in index.entries[:10]
                )
                + " acceptable risk",
                new_run=True,
            )
            self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            self.assertIn("before business writes", fixture.stdout)
            self.assertIn("cannot fund the complete operation", fixture.stdout)
            current = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(
                read_session_dispositions(fixture.selected.ledger, current), ()
            )
            self.assertEqual(
                current.transaction.transaction_id, original.transaction.transaction_id
            )
            self.assertEqual(current.api_run_accounting["action"]["phase"], "terminal")
            self.assertEqual(current.api_run_accounting["configuration"]["limit"], 512)
            self.assertEqual(
                (
                    fixture.reviews,
                    fixture.inline,
                    fixture.check_runs,
                    fixture.resolved_threads,
                ),
                before,
            )
            self.assertEqual(fixture.business_writes, 0)
            self.assertEqual(fixture.review_put_attempts, 0)

    def test_unqualified_source_holds_before_any_callback_or_business_write(self):
        invoked = []
        app = object.__new__(GitHubApplication)
        app.reviewer = SimpleNamespace(
            finalizer=SimpleNamespace(
                prepare_command_capacity=lambda **kw: invoked.append(kw)
            )
        )
        result = app._preflight_command_finalization(
            command=MaintainerCommand(action="verify", actor="maintainer"),
            capacity_preview={
                "result": MaintainerCommandResult(
                    action="verify",
                    applied=False,
                    operator_paused=False,
                    summary="verify",
                )
            },
            record=SimpleNamespace(operator_paused=False),
            document=None,
            ledger=None,
            read_token="synthetic",
            evidence_budget=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="b" * 40,
            app_slug="review-sensei[bot]",
            incomplete_coverage=(),
            policy_digest=None,
        )
        self.assertFalse(result.applied)
        self.assertFalse(result.evaluation_failed)
        self.assertIn("before business writes", result.summary)
        self.assertEqual(invoked, [])
