"""Shared fresh-process baseline recovery contract for checkout and wheel lanes.

All data is synthetic. Only fixture transport observation/failure is injected;
CLI parsing, admission, planning, checkpointing and cancellation run unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import review_sensei
from review_sensei.baseline import baseline_from_history_document
from review_sensei.session import LocalSessionLedger, SessionIdentity

CHILD = r"""
import json, runpy, socket, sys, time
from pathlib import Path
source, installed, witness, barrier = sys.argv[1:5]
if source:
    sys.path.insert(0, source)
import review_sensei
from review_sensei.providers.fixture import FixtureProvider
origin = Path(review_sensei.__file__).resolve()
if installed == "1":
    assert origin.is_relative_to(Path(sys.prefix).resolve()), (origin, sys.prefix)
def denied(*args, **kwargs):
    raise AssertionError("live network is forbidden")
socket.socket = denied
socket.create_connection = denied
original = FixtureProvider.complete
calls = []
def complete(self, request):
    calls.append({"prompt_bytes": len(request.prompt.encode())})
    Path(witness).write_text(json.dumps({"package": str(origin), "calls": calls}))
    if barrier:
        Path(barrier + ".entered").touch()
        until = time.monotonic() + 20
        while not Path(barrier + ".release").exists():
            if Path(barrier + ".cancel").exists():
                raise KeyboardInterrupt()
            if time.monotonic() >= until:
                raise AssertionError("test provider barrier timed out")
            time.sleep(0.02)
    return original(self, request)
FixtureProvider.complete = complete
Path(witness).write_text(json.dumps({"package": str(origin), "calls": calls}))
sys.argv = ["review-sensei", *sys.argv[5:]]
runpy.run_module("review_sensei", run_name="__main__")
"""


class BaselineCLIContract:
    """Mixin whose assertions are identical for checkout and installed wheels."""

    installed = False

    def setUp(self):
        if self.installed:
            # The ledger and baseline helpers must come from the same wheel as
            # the CLI, even if a caller has preloaded one of these submodules.
            prefix = Path(sys.prefix).resolve()
            for name in ("review_sensei.baseline", "review_sensei.session"):
                self.assertTrue(
                    Path(sys.modules[name].__file__).resolve().is_relative_to(prefix),
                    name,
                )
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity = SessionIdentity("example/repo", 7)
        self.ledger = LocalSessionLedger(self.root / "ledger")
        self.diff = self.root / "change.patch"
        self.diff.write_text(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1,2 @@\n keep\n+change\n",
            encoding="utf-8",
        )
        self.response = self.root / "response.json"
        self.respond()
        self.config = self.root / "config.yml"
        self.config.write_text(
            "schema: 1\nadvanced:\n  review_work:\n    mode: legacy\n",
            encoding="utf-8",
        )
        self.sequence = 0

    def respond(self, comments=()):
        self.response.write_text(
            json.dumps({"summary": "Synthetic review.", "comments": list(comments)}),
            encoding="utf-8",
        )

    def record(self):
        return self.ledger.load(self.identity).record

    def baseline(self):
        return baseline_from_history_document(
            self.record().convergence_history["baseline"]
        )

    def start_cli(self, *, base="a", head="b", barrier="", extra=()):
        self.sequence += 1
        witness = self.root / f"witness-{self.sequence}.json"
        # Isolated mode ignores inherited Python paths and environment. Only
        # the checkout lane inserts its explicitly resolved runtime source.
        source = "" if self.installed else str(Path(review_sensei.__file__).parents[1])
        argv = [
            sys.executable,
            "-I",
            "-c",
            CHILD,
            source,
            str(int(self.installed)),
            str(witness),
            str(barrier),
            "--config",
            str(self.config),
            "--diff",
            str(self.diff),
            "--provider",
            "fixture",
            "--fixture-response",
            str(self.response),
            "--model",
            "fixture-model",
            "--repository",
            "example/repo",
            "--pull-request",
            "7",
            "--base-sha",
            base * 40,
            "--head-sha",
            head * 40,
            "--review-mode",
            "merge-focused",
            "--session-ledger",
            str(self.ledger.root),
            "--format",
            "json",
            "--output",
            str(self.root / "result.json"),
            "--outcome",
            str(self.root / "outcome.json"),
            "--configuration-context-output",
            str(self.root / "configuration.json"),
            "--admission-context-output",
            str(self.root / "admission.json"),
            "--no-learning-proposals",
            "--exit-semantics",
            "operational",
            *extra,
        ]
        environment = {"HOME": str(self.root / "home"), "PATH": os.defpath}
        for name in ("SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL"):
            if name in os.environ:
                environment[name] = os.environ[name]
        child = subprocess.Popen(
            argv,
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_child, child)
        return child, witness

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def finish_cli(self, child, witness):
        stdout, stderr = child.communicate(timeout=30)
        observed = json.loads(witness.read_text())
        outcome = self.root / "outcome.json"
        return (
            child.returncode,
            stderr,
            json.loads(outcome.read_text()) if outcome.exists() else {},
            observed,
        )

    def run_cli(self, **kwargs):
        return self.finish_cli(*self.start_cli(**kwargs))

    def seed(self, comments=()):
        self.respond(comments)
        code, stderr, _, observed = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(observed["calls"]), 1)
        return self.record()

    def test_same_head_base_change_full_refresh_and_exact_retry_have_one_round(self):
        before = self.seed()
        old = self.baseline()
        code, stderr, outcome, observed = self.run_cli(base="c")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            outcome["stage_summary"]["baseline_refresh"], "rebase-or-base-change"
        )
        self.assertEqual(len(observed["calls"]), 1)
        self.assertEqual(self.baseline().cache_key.base_sha, "c" * 40)
        admission = json.loads((self.root / "admission.json").read_text())
        self.assertEqual(baseline_from_history_document(admission["baseline"]), old)
        after = self.record()
        self.assertEqual(
            after.completed_verification_rounds,
            before.completed_verification_rounds + 1,
        )
        self.assertEqual(after.dispositions, before.dispositions)
        # A fresh process must recognize committed analysis even if inference
        # would now fail. It must preserve the original recovery artifacts.
        original_result = (self.root / "result.json").read_bytes()
        self.response.unlink()
        code, _, outcome, observed = self.run_cli(base="c")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["diagnostic"], "publication_recovery_required")
        self.assertEqual(observed["calls"], [])
        self.assertEqual(self.record(), after)
        self.assertEqual((self.root / "result.json").read_bytes(), original_result)

    def test_omitted_prior_concern_preserves_history_and_partial_status(self):
        before = self.seed(
            [
                {
                    "path": "app.py",
                    "line": 2,
                    "body": "Authorization failure; reject unauthorized requests.",
                    "blocking": True,
                    "severity": "high",
                    "defect_kind": "authz-failure",
                }
            ]
        )
        fingerprint = self.baseline().findings[0].fingerprint
        self.respond()
        code, stderr, outcome, observed = self.run_cli(base="c", head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(observed["calls"]), 1)
        self.assertEqual(outcome["status"], "partial")
        result = json.loads((self.root / "result.json").read_text())
        self.assertEqual(result["review_status"], "partial")
        self.assertIn(
            fingerprint,
            [
                item["fingerprint"]
                for item in result["finding_lifecycles"]
                if item["state"] == "uncertain"
            ],
        )
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)

    def test_same_head_model_change_refreshes_once_without_reusing_old_context(self):
        before = self.seed()
        arguments = ("--model", "fixture-model-v2")
        code, stderr, outcome, observed = self.run_cli(extra=arguments)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["stage_summary"]["baseline_refresh"], "model-change")
        self.assertEqual(len(observed["calls"]), 1)
        self.assertEqual(
            self.record().completed_verification_rounds,
            before.completed_verification_rounds + 1,
        )
        self.assertEqual(self.baseline().cache_key.model, "fixture-model-v2")
        generation = self.record().generation
        code, _, outcome, observed = self.run_cli(extra=arguments)
        self.assertEqual(code, 1)
        self.assertEqual(outcome["diagnostic"], "publication_recovery_required")
        self.assertEqual(observed["calls"], [])
        self.assertEqual(self.record().generation, generation)

    def test_operator_pause_remains_authoritative_during_baseline_invalidation(self):
        self.seed()
        self.ledger.replace(
            self.identity, lambda record: record.evolve(operator_paused=True)
        )
        before = self.record()
        code, stderr, outcome, observed = self.run_cli(base="c", head="d")
        self.assertEqual(code, 1, stderr)
        self.assertEqual(outcome["diagnostic"], "paused")
        self.assertEqual(observed["calls"], [])
        self.assertEqual(self.record(), before)

    def test_failed_and_binary_partial_refresh_cannot_replace_baseline(self):
        before = self.seed()
        self.response.unlink()
        code, _, _, observed = self.run_cli(base="c", head="d")
        self.assertNotEqual(code, 0)
        self.assertEqual(len(observed["calls"]), 1)
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 1)
        self.assertIsNone(self.record().reservation_id)
        self.respond()
        with self.diff.open("a") as handle:
            handle.write(
                "diff --git a/logo.bin b/logo.bin\nBinary files a/logo.bin and b/logo.bin differ\n"
            )
        code, stderr, outcome, _ = self.run_cli(base="c", head="e")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertIsNone(self.record().reservation_id)

    def test_oversized_item_refuses_without_inference_or_state_loss(self):
        before = self.seed()
        with self.diff.open("a") as handle:
            handle.write("+" + "x" * 1_100_000 + "\n")
        code, stderr, _, observed = self.run_cli(base="c", head="d")
        self.assertNotEqual(code, 0, stderr)
        self.assertEqual(observed["calls"], [])
        self.assertEqual(self.record(), before)

    def test_untrusted_baselines_refuse_without_inference(self):
        self.seed()
        original = self.record().convergence_history
        for corruption in ("foreign", "future", "incomplete", "missing"):
            with self.subTest(corruption=corruption):
                history = json.loads(json.dumps(original))
                if corruption == "foreign":
                    history["baseline"]["cache_key"]["repository"] = "example/foreign"
                elif corruption == "future":
                    history["baseline"]["generation"] = self.record().generation + 10
                elif corruption == "incomplete":
                    history["baseline"]["complete"] = False
                else:
                    history = None
                self.ledger.replace(
                    self.identity,
                    lambda record: record.evolve(convergence_history=history),
                )
                code, stderr, outcome, observed = self.run_cli(base="c", head="d")
                self.assertEqual(code, 1, stderr)
                self.assertEqual(
                    outcome["diagnostic"], "durable_baseline_recovery_required"
                )
                self.assertEqual(observed["calls"], [])
                self.assertEqual(self.record().convergence_history, history)
                self.assertIsNone(self.record().reservation_id)

    def await_provider(self, child, barrier):
        until = time.monotonic() + 15
        while not Path(str(barrier) + ".entered").exists():
            if child.poll() is not None or time.monotonic() >= until:
                self.fail("fixture provider barrier was not reached")
            time.sleep(0.02)

    def test_concurrent_fresh_process_cannot_take_refresh_ownership(self):
        self.seed()
        barrier = self.root / "provider"
        owner, witness = self.start_cli(base="c", head="d", barrier=barrier)
        self.await_provider(owner, barrier)
        code, stderr, outcome, observed = self.run_cli(base="c", head="d")
        self.assertEqual(code, 1, stderr)
        self.assertEqual(outcome["diagnostic"], "paused")
        self.assertEqual(observed["calls"], [])
        Path(str(barrier) + ".release").touch()
        code, stderr, _, observed = self.finish_cli(owner, witness)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(observed["calls"]), 1)
        self.assertEqual(self.baseline().cache_key.head_sha, "d" * 40)
        self.assertIsNone(self.record().reservation_id)

    def test_cancellation_unwinds_refresh_then_fresh_process_recovers(self):
        before = self.seed()
        barrier = self.root / "provider"
        child, witness = self.start_cli(base="c", head="d", barrier=barrier)
        self.await_provider(child, barrier)
        if os.name == "posix":
            child.terminate()
        else:
            # Windows TerminateProcess cannot deliver a Python signal handler.
            # Exercise the same supported cancellation unwind at transport.
            Path(str(barrier) + ".cancel").touch()
        code, stderr, _, _ = self.finish_cli(child, witness)
        self.assertEqual(code, 130, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.record().convergence_history, before.convergence_history)
        self.assertEqual(self.record().failed_attempts, 0)
        self.assertIsNone(self.record().reservation_id)
        code, stderr, _, observed = self.run_cli(base="c", head="d")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(observed["calls"]), 1)
        self.assertEqual(self.baseline().cache_key.head_sha, "d" * 40)
