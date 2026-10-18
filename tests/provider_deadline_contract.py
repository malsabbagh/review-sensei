"""Credential-free deadline acceptance, reusable against an installed wheel."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import review_sensei
from review_sensei import ReviewService
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.execution import OutputAccounting, execute_call
from review_sensei.human_assessment import (
    HumanAssessmentService,
    HumanReviewFinding,
    PendingHumanReview,
)
from review_sensei.models import (
    ConversationContext,
    ProviderRequest,
    ProviderResponse,
    ReviewRequest,
)
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.providers.ollama import OllamaProvider
from review_sensei.providers.openai_compatible import OpenAICompatibleProvider
from review_sensei.providers.openrouter import (
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
)
from review_sensei.stages import Stage
from review_sensei.validation import ReviewLimits

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
"""
CLEAN = '{"summary":"Synthetic review complete.","comments":[]}'


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Provider:
    name = "synthetic"
    model = "synthetic"

    def __init__(self, clock, actions):
        self.clock = clock
        self.actions = iter(actions)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        elapsed, value = next(self.actions)
        self.clock.value += elapsed
        if isinstance(value, BaseException):
            raise value
        return ProviderResponse(text=value, provider=self.name, model=self.model)


class ProviderDeadlineContractTests(unittest.TestCase):
    def service_run(self, actions, *, mode="legacy", stages=None, retries=2, calls=8):
        clock = Clock()
        provider = Provider(clock, actions)
        resource = ResourceBudget.create(
            timeout_ms=1000,
            max_retry_attempts=retries,
            max_output_bytes=16384,
            max_provider_calls=calls,
        )
        result = ReviewService(
            provider, stages=stages, work_budgets=ReviewWorkBudgets(mode=mode)
        ).run(
            ReviewRequest(diff=DIFF),
            budget=resource,
            monotonic=clock,
            sleeper=clock.sleep,
        )
        return result, provider.requests

    def assert_pending(self, run, mode, reason):
        if mode == "legacy":
            self.assertEqual(run.outcome.diagnostic, reason)
            self.assertIsNone(run.result)
        else:
            self.assertEqual(run.outcome.status, "partial")
            self.assertEqual(run.outcome.diagnostic, "partial_coverage")
            self.assertEqual(run.result.review_status, "partial")
            self.assertIn(reason, run.result.summary)

    def test_stages_share_original_deadline_in_both_work_modes(self):
        stages = [
            Stage(name=name, prompt_template="{diff}", outputs=("summary", "comments"))
            for name in ("first", "second")
        ]
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, requests = self.service_run(
                    [(0.25, CLEAN), (0.25, CLEAN)], mode=mode, stages=stages
                )
                self.assertEqual(run.outcome.status, "reviewed")
                self.assertEqual([r.timeout_seconds for r in requests], [1.0, 0.75])

    def test_transport_retry_and_correction_share_original_deadline(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, requests = self.service_run(
                    [
                        (
                            0.125,
                            ProviderError(
                                "synthetic failure",
                                transient=True,
                                retry_after_seconds=0.125,
                            ),
                        ),
                        (0.25, "{"),
                        (0.125, CLEAN),
                    ],
                    mode=mode,
                )
                self.assertEqual(run.outcome.status, "reviewed")
                self.assertEqual(
                    [r.timeout_seconds for r in requests], [1.0, 0.75, 0.5]
                )
                self.assertEqual(run.outcome.retry_attempts, 1)
                self.assertEqual(run.outcome.structural_retries, 1)

    def test_expired_review_never_dispatches(self):
        provider = Provider(Clock(), [])
        for mode in ("legacy", "unified"):
            run = ReviewService(
                provider, work_budgets=ReviewWorkBudgets(mode=mode)
            ).run(ReviewRequest(diff=DIFF), budget=ResourceBudget.create(timeout_ms=0))
            self.assert_pending(
                run,
                mode,
                "planning-deadline-exceeded"
                if mode == "unified"
                else "deadline_exceeded",
            )
        self.assertEqual(provider.requests, [])

    def test_timeout_after_original_deadline_has_deadline_diagnostic(self):
        for mode in ("legacy", "unified"):
            for transient in (False, True):
                with self.subTest(mode=mode, transient=transient):
                    run, requests = self.service_run(
                        [(1, ProviderError("synthetic timeout", transient=transient))],
                        mode=mode,
                        retries=0,
                    )
                    self.assert_pending(run, mode, "deadline_exceeded")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(run.outcome.provider_calls, len(requests))
                    self.assertEqual(run.outcome.retry_attempts, 0)

    def test_transient_dispatches_are_charged_separately_from_retry_attempts(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, requests = self.service_run(
                    [
                        (0.125, ProviderError("synthetic failure", transient=True)),
                        (0.125, CLEAN),
                    ],
                    mode=mode,
                )
                self.assertEqual(run.outcome.status, "reviewed")
                self.assertEqual(len(requests), 2)
                self.assertEqual(run.outcome.provider_calls, len(requests))
                self.assertEqual(run.outcome.retry_attempts, 1)
                self.assertEqual(run.outcome.structural_retries, 0)

    def test_transient_retry_cannot_bypass_provider_call_ceiling(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, requests = self.service_run(
                    [(0.125, ProviderError("synthetic failure", transient=True))],
                    mode=mode,
                    calls=1,
                )
                self.assert_pending(run, mode, "provider_call_limit")
                self.assertEqual(len(requests), 1)
                self.assertEqual(run.outcome.provider_calls, len(requests))
                self.assertEqual(run.outcome.retry_attempts, 1)
                self.assertEqual(run.outcome.structural_retries, 0)

    def test_shorter_transport_timeout_keeps_retry_diagnostic(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, requests = self.service_run(
                    [(0.125, ProviderError("synthetic timeout", transient=True))] * 3,
                    mode=mode,
                )
                self.assert_pending(run, mode, "transport_retry_exhausted")
                self.assertEqual(len(requests), 3)

    def test_remaining_allowance_does_not_round_up_submillisecond_time(self):
        clock = Clock()
        tracker = ResourceBudgetTracker(
            ResourceBudget.create(timeout_ms=1000), monotonic=clock
        )
        clock.value = 0.9999
        self.assertGreater(tracker.remaining_seconds(), 0)
        self.assertLessEqual(tracker.remaining_seconds(), 0.00010000001)

    def test_durable_dispatch_callback_cannot_refresh_or_outlive_deadline(self):
        for elapsed in (0.25, 1.0):
            with self.subTest(callback_seconds=elapsed):
                clock = Clock()
                resource = ResourceBudget.create(timeout_ms=1000)
                tracker = ResourceBudgetTracker(resource, monotonic=clock)
                provider = Provider(clock, [(0, CLEAN)])
                budgets = ReviewWorkBudgets(mode="unified").effective(
                    limits=ReviewLimits(), resource=resource, mode="discovery"
                )
                accounting = OutputAccounting()
                result = execute_call(
                    ProviderRequest(prompt="synthetic"),
                    provider=provider,
                    validate=lambda response: response.text,
                    tracker=tracker,
                    budgets=budgets,
                    before_dispatch=lambda digest: setattr(clock, "value", elapsed),
                    output_accounting=accounting,
                )
                self.assertEqual(tracker.provider_calls, 1)
                if elapsed < 1:
                    self.assertIsNone(result.diagnostic)
                    self.assertEqual(provider.requests[0].timeout_seconds, 0.75)
                else:
                    self.assertEqual(provider.requests, [])
                    self.assertEqual(result.diagnostic, "deadline_exceeded")
                    self.assertGreater(accounting.unknown_response_bytes, 0)

    def test_multiple_discovery_batches_share_original_deadline(self):
        clock = Clock()
        provider = Provider(clock, [(0.25, CLEAN), (0.25, CLEAN)])
        request = ReviewRequest(diff=DIFF + DIFF.replace("app.py", "other.py"))
        run = ReviewService(
            provider,
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=100),
        ).run(request, budget=ResourceBudget.create(timeout_ms=1000), monotonic=clock)
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertEqual([r.timeout_seconds for r in provider.requests], [1.0, 0.75])

    def test_human_timeout_distinguishes_original_deadline_from_transport_limit(self):
        context = ConversationContext(
            base_sha="a" * 40,
            head_sha="b" * 40,
            diff_context="path=app.py\n" + DIFF,
        )
        pending = PendingHumanReview(
            "a" * 40, (HumanReviewFinding("c" * 64, "app.py", "Synthetic concern"),)
        )
        for elapsed in (0.125, 1.0):
            clock = Clock()
            tracker = ResourceBudgetTracker(
                ResourceBudget.create(timeout_ms=1000), monotonic=clock
            )
            provider = Provider(
                clock, [(elapsed, ProviderError("synthetic timeout", transient=True))]
            )
            expected_error = ProviderError if elapsed < 1 else ReviewInputError
            with (
                self.subTest(elapsed=elapsed),
                self.assertRaises(expected_error) as raised,
            ):
                HumanAssessmentService(provider).reply(
                    context=context,
                    pending=pending,
                    source_body="Synthetic explanation",
                    tracker=tracker,
                )
            self.assertEqual(provider.requests[0].timeout_seconds, 1.0)
            self.assertEqual(tracker.provider_calls, 1)
            if elapsed == 1:
                self.assertEqual(raised.exception.diagnostic, "deadline_exceeded")
            else:
                self.assertTrue(raised.exception.transient)

    def test_keyboard_interrupt_propagates_without_clean_result_or_retry(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode), self.assertRaises(KeyboardInterrupt):
                self.service_run([(0, KeyboardInterrupt())], mode=mode)

    def test_adapters_bound_current_timeout_before_transport(self):
        for name in ("ollama", "openai", "openrouter"):
            calls = []

            def opener(request, timeout, context):
                calls.append(timeout)
                raise TimeoutError("synthetic timeout")

            kwargs = {"timeout_seconds": 0.25}
            if name == "ollama":
                provider = OllamaProvider(opener=opener, **kwargs)
            elif name == "openai":
                provider = OpenAICompatibleProvider(
                    api_key="synthetic", opener=opener, **kwargs
                )
            else:
                provider = OpenRouterProvider(
                    api_key="synthetic",
                    model="synthetic",
                    routing_policy=OpenRouterRoutingPolicy(
                        upstream_provider="synthetic"
                    ),
                    _test_opener=opener,
                    **kwargs,
                )
            for remaining, expected in ((0.125, 0.125), (1.0, 0.25)):
                with (
                    self.subTest(adapter=name, remaining=remaining),
                    self.assertRaises(ProviderError),
                ):
                    provider.complete(
                        ProviderRequest(prompt="synthetic", timeout_seconds=remaining)
                    )
                self.assertEqual(calls[-1], expected)
                self.assertTrue(math.isfinite(calls[-1]))
            for invalid in (True, 0, -1, float("nan"), float("inf"), 1e100, "1"):
                provider.timeout_seconds = invalid
                before = len(calls)
                with (
                    self.subTest(adapter=name, invalid=repr(invalid)),
                    self.assertRaises(ProviderError),
                ):
                    provider.complete(
                        ProviderRequest(prompt="synthetic", timeout_seconds=1)
                    )
                self.assertEqual(len(calls), before)

    def test_adapter_timeout_range_is_validated_before_request_clamping(self):
        def construct(name, timeout, opener):
            if name == "ollama":
                return OllamaProvider(timeout_seconds=timeout, opener=opener)
            if name == "openai":
                return OpenAICompatibleProvider(
                    api_key="synthetic", timeout_seconds=timeout, opener=opener
                )
            return OpenRouterProvider(
                api_key="synthetic",
                model="synthetic",
                timeout_seconds=timeout,
                routing_policy=OpenRouterRoutingPolicy(upstream_provider="synthetic"),
                _test_opener=opener,
            )

        for name in ("ollama", "openai", "openrouter"):
            calls = []

            def opener(request, timeout, context):
                calls.append(timeout)
                raise TimeoutError("synthetic timeout")

            for timeout in (1e100, math.nextafter(threading.TIMEOUT_MAX, math.inf)):
                with (
                    self.subTest(adapter=name, invalid=timeout),
                    self.assertRaises(ValueError),
                ):
                    construct(name, timeout, opener)
                self.assertEqual(calls, [])
            for timeout in (3601, threading.TIMEOUT_MAX):
                provider = construct(name, timeout, opener)
                with (
                    self.subTest(adapter=name, valid=timeout),
                    self.assertRaises(ProviderError),
                ):
                    provider.complete(
                        ProviderRequest(prompt="synthetic", timeout_seconds=0.125)
                    )
                self.assertEqual(calls[-1], 0.125)

    @contextmanager
    def blocked_transport(self):
        entered = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                entered.set()
                release.wait(3)
                body = json.dumps({"response": CLEAN}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/api", entered
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_real_ollama_socket_obeys_remaining_review_deadline(self):
        with self.blocked_transport() as (endpoint, entered):
            provider = OllamaProvider(base_url=endpoint, timeout_seconds=10)
            started = time.monotonic()
            run = ReviewService(provider).run(
                ReviewRequest(diff=DIFF), budget=ResourceBudget.create(timeout_ms=150)
            )
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, 1.5)
            self.assert_pending(run, "legacy", "deadline_exceeded")

    def cli_process(self, endpoint, root, *, timeout_seconds="10"):
        (root / "input.patch").write_text(DIFF, encoding="utf-8")
        (root / "config.yml").write_text(
            "schema: 1\nadvanced:\n  review_work:\n    mode: unified\n  resources:\n    review_deadline_seconds: 1\n",
            encoding="utf-8",
        )
        # Select exactly the package whose import the caller's lane verified.
        # Isolated Python ignores host PYTHONPATH and user site packages.
        bootstrap = (
            "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
            "runpy.run_module('review_sensei',run_name='__main__')"
        )
        environment = {"HOME": str(root), "PATH": os.environ.get("PATH", "")}
        for name in ("SYSTEMROOT", "TMPDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
        return subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                bootstrap,
                str(Path(review_sensei.__file__).resolve().parent.parent),
                "--diff",
                "input.patch",
                "--config",
                "config.yml",
                "--provider",
                "local-ollama",
                "--model",
                "synthetic",
                "--base-url",
                endpoint,
                "--allow-custom-endpoint",
                "--timeout-seconds",
                timeout_seconds,
                "--format",
                "json",
                "--outcome",
                "outcome.json",
                "--no-learning-proposals",
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_fresh_cli_uses_total_deadline_with_longer_transport_limit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.blocked_transport() as (endpoint, entered),
        ):
            root = Path(directory)
            started = time.monotonic()
            process = self.cli_process(endpoint, root)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            stdout, stderr = process.communicate(timeout=6)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(process.returncode, 2, stderr.decode())
            result = json.loads(stdout)
            self.assertEqual(result["review_status"], "partial")
            self.assertIn("deadline_exceeded", result["summary"])
            outcome = json.loads((root / "outcome.json").read_text())
            self.assertEqual(outcome["status"], "partial")
            self.assertIn("partial_coverage", outcome["diagnostic"])

    @unittest.skipUnless(os.name == "posix", "POSIX signal delivery")
    def test_fresh_cli_interrupt_and_termination_exit_cleanly(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with (
                self.subTest(signal=signum),
                tempfile.TemporaryDirectory() as directory,
                self.blocked_transport() as (endpoint, entered),
            ):
                process = self.cli_process(endpoint, Path(directory))
                self.addCleanup(
                    lambda p=process: p.kill() if p.poll() is None else None
                )
                self.assertTrue(entered.wait(3), "CLI never entered synthetic provider")
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 130)
                self.assertEqual(stdout, b"")
                self.assertIn(b"cancelled", stderr)
                self.assertNotIn(b"Traceback", stderr)

    def test_fresh_cli_rejects_unrepresentable_timeout_before_network(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.blocked_transport() as (endpoint, entered),
        ):
            process = self.cli_process(
                endpoint, Path(directory), timeout_seconds="1e100"
            )
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 2)
            self.assertFalse(
                entered.is_set(), "invalid configured timeout reached transport"
            )
            self.assertEqual(stdout, b"")
            self.assertIn(b"timeout_seconds", stderr)
            self.assertNotIn(b"Traceback", stderr)
