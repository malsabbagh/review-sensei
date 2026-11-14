"""Complete known native batches admit no partially funded mutation prefix."""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from review_sensei_github.finding_lifecycle import _unpack_journal
from review_sensei_github.publication import NativeFindingReceipt

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import LocalSessionLedger
from tests import test_finding_lifecycle_journal as f


class RetentionLedger(LocalSessionLedger):
    """Adapter-shaped retention hook over a real local record."""


class NativeBatchTests(unittest.TestCase):
    def journal(self, root, ledger_type=LocalSessionLedger):
        value = f.journal()
        ledger = ledger_type(
            Path(root),
            enable_finding_lifecycle_writes=True,
            evidence_budget=EvidenceReadBudget(),
        )
        initial = ledger.initialize(f.IDENTITY)
        tx = ReviewTransaction.create(
            repository=f.REPO,
            pull_request=1,
            base_sha=f.BASE,
            head_sha=f.HEAD,
            policy_digest="2" * 64,
            configuration_digest="3" * 64,
            evidence_digest="4" * 64,
            reservation_id="5" * 64,
            generation=1,
        )
        value._record = ledger.replace(
            f.IDENTITY,
            lambda r: r.evolve(
                generation=initial.generation + 1,
                reservation_id="5" * 64,
                reserved_slot="initial",
                transaction=tx,
            ),
        )
        value.ledger = ledger
        value.operation_id = tx.transaction_id
        records = tuple(
            NativeFindingReceipt(
                repository_id=7,
                pull_request=1,
                head_sha=f.HEAD,
                base_sha=f.BASE,
                producer_id=9001,
                review_id=1201,
                instance=str(i) * 64,
                body_sha256=hashlib.sha256(f"old{i}".encode()).hexdigest(),
                thread_id=f"T{i}",
                comment_id=2100 + i,
                native_head_sha=f.HEAD,
            ).to_dict()
            for i in (1, 2)
        )
        value.reviewer.native_finding_receipts = lambda **kw: records
        value._native_budget = Mock()
        return value

    def requests(self):
        return tuple(
            (
                "PATCH",
                f"/repos/owner/repo/pulls/comments/{2100 + i}",
                {"body": f"changed{i}"},
            )
            for i in (1, 2)
        )

    def test_thirty_one_plus_two_refuses_before_any_checkpoint_or_native_effect(self):
        with tempfile.TemporaryDirectory() as root:
            value = self.journal(root)
            value.effects = [
                f.effect(
                    {**f.REQUEST, "body": str(i)},
                    status="confirmed",
                    response=[
                        200,
                        {
                            "id": i + 1,
                            "producer_id": 9001,
                            "app_id": None,
                            "mutation": None,
                        },
                    ],
                )
                for i in range(31)
            ]
            before = copy.deepcopy(value.effects)
            with patch.object(value, "checkpoint") as checkpoint:
                with self.assertRaisesRegex(ReviewInputError, "known batch exceeds"):
                    value.preflight_native_batch(self.requests(), native_calls=30)
                checkpoint.assert_not_called()
            self.assertEqual(value.effects, before)

    def test_actual_core_checks_cumulative_retention_before_any_part_or_effect(self):
        with tempfile.TemporaryDirectory() as root:
            value = self.journal(root, RetentionLedger)
            seen = []

            def retention(identity, **kwargs):
                seen.append(kwargs)
                if kwargs["added_parts"] > 2:
                    raise ReviewInputError("aggregate retained frontier is full")
                return {}

            value.ledger.preflight_finding_lifecycle_retention = retention
            before = {
                str(p): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()
            }
            with (
                patch(
                    "review_sensei.finding_lifecycle_store.inline_manifest",
                    return_value=None,
                ),
                patch.object(value.ledger.evidence_budget, "preflight"),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "aggregate retained frontier"
                ):
                    value.preflight_native_batch(self.requests(), native_calls=30)
            self.assertEqual(seen[0]["checkpoint_parts"], (1, 1, 1))
            self.assertEqual(value.effects, [])
            self.assertEqual(
                before,
                {str(p): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()},
            )

    def test_multi_edit_preflight_is_pure_and_prices_pending_prefixes_and_final(self):
        with tempfile.TemporaryDirectory() as root:
            value = self.journal(root)
            from review_sensei.finding_lifecycle_store import (
                preflight_finding_lifecycle,
            )

            captured = []

            def observed(*args, **kwargs):
                captured.append(kwargs)
                return preflight_finding_lifecycle(*args, **kwargs)

            with patch(
                "review_sensei.finding_lifecycle_store.preflight_finding_lifecycle",
                side_effect=observed,
            ):
                value.preflight_native_batch(self.requests(), native_calls=2)
            self.assertEqual(value.effects, [])
            self.assertEqual(len(captured), 1)
            docs = [
                _unpack_journal(d)
                for d in (captured[0]["document"], *captured[0]["future_documents"])
            ]
            self.assertEqual([len(d["effects"]) for d in docs], [1, 2, 2])
            self.assertEqual(
                [d["effects"][-1]["status"] for d in docs],
                ["pending", "pending", "confirmed"],
            )
            self.assertEqual(captured[0]["reserved_tail_calls"], 8)
            for method, path, body in self.requests():
                http = f.FakeHTTP(
                    (
                        200,
                        f.native_review(
                            id=int(path.rsplit("/", 1)[1]), body=body["body"]
                        ),
                    )
                )
                value.request(http, method, path, token="synthetic", body=body)
                self.assertEqual(len(http.calls), 1)
            self.assertEqual(
                [e["status"] for e in value.effects], ["confirmed", "confirmed"]
            )

    def test_legacy_intent_and_duplicate_requests_do_not_spend_new_effect_slots(self):
        with tempfile.TemporaryDirectory() as root:
            value = self.journal(root)
            method, path, body = self.requests()[0]
            value.effects = [
                f.effect(
                    body,
                    path=path,
                    method=method,
                    status="confirmed",
                    response=[
                        200,
                        {
                            "id": 2101,
                            "producer_id": 9001,
                            "app_id": None,
                            "mutation": None,
                        },
                    ],
                )
            ]
            value.effects.extend(
                f.effect(
                    {**f.REQUEST, "body": str(i)},
                    status="confirmed",
                    response=[
                        200,
                        {
                            "id": i + 1,
                            "producer_id": 9001,
                            "app_id": None,
                            "mutation": None,
                        },
                    ],
                )
                for i in range(30)
            )
            before = copy.deepcopy(value.effects)
            value.preflight_native_batch(
                (*self.requests(), self.requests()[1]), native_calls=3
            )
            self.assertEqual(value.effects, before)
            with self.assertRaisesRegex(ReviewInputError, "scope is unsupported"):
                value.preflight_native_batch(
                    (("POST", "/graphql", {"query": "mutation { deleteIssue }"}),),
                    native_calls=1,
                )

    def test_shared_budget_keeps_combined_liability_and_each_separate_domain_is_funded(
        self,
    ):
        from review_sensei.errors import EvidenceBudgetExhausted
        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

        for domain in ("shared", "ledger", "native"):
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as root:
                value = self.journal(root)
                value._native_budget = (
                    value.ledger.evidence_budget
                    if domain == "shared"
                    else EvidenceReadBudget()
                )
                captured = []

                def observed(*args, **kwargs):
                    plan = preflight_finding_lifecycle(*args, **kwargs)
                    captured.append(plan)
                    return plan

                with patch(
                    "review_sensei.finding_lifecycle_store.preflight_finding_lifecycle",
                    side_effect=observed,
                ):
                    value.preflight_native_batch(self.requests(), native_calls=2)
                self.assertEqual(
                    captured[-1]["reserved_tail_calls"], 8 if domain == "shared" else 6
                )
                calls = 2
                if domain == "shared":
                    calls = value._native_budget.ordinary_remaining() - 1
                    value._native_budget.preflight(calls)
                else:
                    budget = (
                        value.ledger.evidence_budget
                        if domain == "ledger"
                        else value._native_budget
                    )
                    needed = (
                        captured[-1]["conditional_calls"] + 6
                        if domain == "ledger"
                        else calls
                    )
                    while budget.ordinary_remaining() > needed - 1:
                        budget.consume()
                before = {
                    str(p): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()
                }
                with self.assertRaises(EvidenceBudgetExhausted):
                    value.preflight_native_batch(self.requests(), native_calls=calls)
                self.assertEqual(value.effects, [])
                self.assertEqual(
                    before,
                    {
                        str(p): p.read_bytes()
                        for p in Path(root).rglob("*")
                        if p.is_file()
                    },
                )

    def test_real_cold_two_target_drift_batch_publishes_both_edits(self):
        import json
        import os
        import subprocess
        import sys

        script = r"""
import sys,json
from pathlib import Path
from urllib.parse import urlparse
from tests import test_durable_finding_lifecycle_restart as f
options=f.GitHubWriteOptions
f.GitHubWriteOptions=lambda **kw: options(**{**kw,"max_active_findings":2})
original=f.PersistentNativeService._open
def native(self,request,timeout=None):
    response=original(self,request,timeout)
    if request.method=="POST" and urlparse(request.full_url).path.endswith("/check-runs"):
        self.check_runs[-1]["external_id"]=json.loads(request.data)["external_id"]
        self.save()
    return response
f.PersistentNativeService._open=native
f._worker(Path(sys.argv[1]),"drift",sys.argv[2],{})
"""
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:
            observations = []
            for phase in ("initial", "rename"):
                run = subprocess.run(
                    [sys.executable, "-c", script, root, phase],
                    cwd=repo,
                    env={
                        **os.environ,
                        "PYTHONPATH": os.pathsep.join(
                            str(repo / p)
                            for p in (
                                ".",
                                "src",
                                "packages/python/review-sensei-github/src",
                                "tests",
                            )
                        ),
                    },
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                value = json.loads(run.stdout.strip().splitlines()[-1])
                self.assertIsNone(value["error"], value)
                observations.append(value)
            self.assertEqual(len(observations[0]["threads"]), 2)
            updates = [
                c
                for c in observations[1]["calls"]
                if c[0] == "PATCH" and "/pulls/comments/" in c[1]
            ]
            self.assertEqual(len(updates), 2, observations[1])
            self.assertEqual(len(observations[1]["threads"]), 2)
            self.assertFalse(
                any(r["state"] == "APPROVED" for r in observations[1]["reviews"])
            )

    def test_publication_wrappers_preserve_real_batch_and_both_original_budgets(self):
        from types import SimpleNamespace

        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.publication_recovery import PublicationBudgetHttp

        with tempfile.TemporaryDirectory() as root:
            value = self.journal(root)
            native = EvidenceReadBudget()
            value._native_budget = native
            http = GitHubHttp(api_url="https://api.github.test")
            value.reviewer.http = http
            value.reviewer.finalizer = SimpleNamespace(
                http=http, evidence_budget=native, _operation_budget=None
            )
            value.reviewer.checks = SimpleNamespace(http=http)
            ledger_budget = value.ledger.evidence_budget
            before = copy.deepcopy(value.effects)
            with (
                value.transports(),
                patch.object(
                    value, "preflight_native_batch", wraps=value.preflight_native_batch
                ) as batch,
                patch.object(
                    native, "preflight", wraps=native.preflight
                ) as native_plan,
                patch.object(
                    ledger_budget, "preflight", wraps=ledger_budget.preflight
                ) as ledger_plan,
                patch.object(http, "request") as request,
            ):
                wrapped = PublicationBudgetHttp(value.reviewer.http, native)
                self.assertIs(wrapped.publication_budget, value._native_budget)
                wrapped.preflight_native_batch(self.requests(), native_calls=2)
                batch.assert_called_once_with(self.requests(), native_calls=2)
                native_plan.assert_called_with(2)
                self.assertTrue(ledger_plan.called)
                request.assert_not_called()
                # The same original native budget, one request short, refuses
                # through both wrappers before any checkpoint or HTTP effect.
                with patch.object(native, "ordinary_remaining", return_value=1):
                    with self.assertRaises(ReviewInputError):
                        wrapped.preflight_native_batch(self.requests(), native_calls=2)
                request.assert_not_called()
            self.assertEqual(value.effects, before)
            absent = PublicationBudgetHttp(http, native)
            self.assertIsNone(getattr(absent, "preflight_native_batch", None))

    def test_real_pending_never_becomes_synthetic_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            value = self.journal(root)
            value.effects = [f.effect()]
            before = copy.deepcopy(value.effects)
            with self.assertRaisesRegex(ReviewInputError, "remains uncertain"):
                value.preflight_native_batch(self.requests(), native_calls=30)
            self.assertEqual(value.effects, before)
