"""Explicit caller recovery reconstructs original context without inference."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import review_sensei_github as github
from review_sensei_github.cli import main as github_main
from review_sensei_github.review_hooks import GitHubReviewHooks

import review_sensei.cli as engine_cli
from review_sensei.models import ProviderResponse, ReviewResult
from review_sensei.service import ReviewService
from review_sensei.session import LocalSessionLedger
from tests import test_default_publication_cli_journey as journey_fixture
from tests.fake_github_http import json_response
from tests.test_publication_recovery import APP, BASE, HEAD, IDENTITY, _Broker


class _AnalysisProvider:
    name = "ollama"
    model = "qwen3.5:4b"

    def complete(self, request):
        return ProviderResponse(
            text=json.dumps({"summary": "Original validated review.", "comments": []}),
            provider=self.name,
            model=self.model,
        )


class _Registry:
    def create(self, settings):
        return _AnalysisProvider()


class _FailedPublication(journey_fixture.JourneyState):
    def __init__(self):
        super().__init__()
        self.fail_review = True
        self.reviews = []

    def open(self, request, timeout):
        if (
            self.fail_review
            and request.method == "POST"
            and urlparse(request.full_url).path.endswith("/reviews")
        ):
            self.calls.append(("POST", urlparse(request.full_url).path, None))
            return json_response({}, 403)
        return super().open(request, timeout)


class TrustedPublicationRecoveryTests(unittest.TestCase):
    def original_analysis(self, root, *, head=HEAD, partial=False):
        (root / ".reviewsensei.yml").write_text(
            "schema: 1\ninference:\n  backend: local-ollama\n"
            "  model: qwen3.5:4b\ngithub:\n  publication_inventory: complete\n",
            encoding="utf-8",
        )
        (root / "review.patch").write_text(
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n"
            "+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            encoding="utf-8",
        )
        argv = [
            "--config",
            str(root / ".reviewsensei.yml"),
            "--diff",
            str(root / "review.patch"),
            "--repository",
            IDENTITY.repository,
            "--pull-request",
            "1",
            "--base-sha",
            BASE,
            "--head-sha",
            head,
            "--transaction",
            "--github-session-ledger",
            "--repository-id",
            "1",
            "--learning-root",
            str(root),
            "--no-learning-proposals",
            "--format",
            "json",
            "--output",
            str(root / "result.json"),
            "--configuration-context-output",
            str(root / "configuration.json"),
            "--admission-context-output",
            str(root / "admission.json"),
            "--outcome",
            str(root / "analysis-outcome.json"),
            "--exit-semantics",
            "operational",
        ]
        original_run = ReviewService.run

        def run(service, request, **kwargs):
            produced = original_run(service, request, **kwargs)
            if not partial:
                return produced
            return replace(
                produced,
                result=replace(produced.result, review_status="partial"),
                outcome=replace(
                    produced.outcome, status="partial", diagnostic="partial_coverage"
                ),
            )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(engine_cli, "default_registry", return_value=_Registry()),
            patch.object(
                GitHubReviewHooks,
                "resolve_ledger",
                return_value=LocalSessionLedger(root / "ledger"),
            ),
            patch.object(ReviewService, "run", new=run),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(github_main(["review", *argv]), 0)
        return argv

    def invoke(
        self,
        root,
        state,
        *,
        reconstruct=False,
        result_source=False,
        actions=False,
        extra=(),
    ):
        arguments = [
            "publish",
            "--allow-write",
            "--enable-review",
            "--no-auto-approve",
            "--oidc-token",
            "synthetic",
            "--repository",
            IDENTITY.repository,
            "--repository-id",
            "1",
            "--pull-request",
            "1",
            "--head-sha",
            HEAD,
            "--base-sha",
            BASE,
            "--base-branch",
            "main",
            "--app-slug",
            APP,
            "--diff",
            str(root / "review.patch"),
            "--session-ledger",
            str(root / "ledger"),
            "--config",
            str(root / ".reviewsensei.yml"),
            "--outcome",
            str(root / "publication-outcome.json"),
        ]
        if reconstruct:
            arguments += (
                ["--result", str(root / "result.json")]
                if result_source
                else ["--recover-retained"]
            )
            arguments += [
                "--reconstruct-contexts",
                "--trusted-context-root",
                str(root),
            ]
        else:
            arguments += [
                "--result",
                str(root / "result.json"),
                "--configuration-context",
                str(root / "configuration.json"),
                "--admission-context",
                str(root / "admission.json"),
            ]
        output, error = io.StringIO(), io.StringIO()
        environment = {"GITHUB_TOKEN": "synthetic-read"}
        if actions:
            environment["GITHUB_RUN_ID"] = "synthetic-recovery-run"
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.multiple(github, GitHubHttp=lambda: state.http, BrokerClient=_Broker),
            patch.object(
                engine_cli,
                "default_registry",
                side_effect=AssertionError("no provider construction"),
            ),
            patch.object(
                ReviewService, "run", side_effect=AssertionError("no inference")
            ),
            patch(
                "socket.socket.connect", side_effect=AssertionError("no live network")
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            try:
                code = github_main([*arguments, *extra])
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), error.getvalue()

    def test_explicit_recovery_after_ordinary_retry_emits_no_original_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = self.original_analysis(root)
            original = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text())
            )
            state = _FailedPublication()
            code, _, error = self.invoke(root, state)
            self.assertEqual(code, 1, error)
            ledger = LocalSessionLedger(root / "ledger")
            before = ledger.load(IDENTITY).record
            self.assertEqual(before.transaction.phase, "publication_failed")
            for name in ("result.json", "configuration.json", "admission.json"):
                (root / name).unlink()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(engine_cli, "default_registry", return_value=_Registry()),
                patch.object(
                    GitHubReviewHooks,
                    "resolve_ledger",
                    return_value=LocalSessionLedger(root / "ledger"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(github_main(["review", *argv]), 1)
            outcome = json.loads((root / "analysis-outcome.json").read_text())
            self.assertEqual(outcome["diagnostic"], "publication_recovery_required")
            self.assertEqual(outcome["provider_calls"], 0)
            self.assertFalse((root / "result.json").exists())
            self.assertFalse((root / "configuration.json").exists())
            self.assertFalse((root / "admission.json").exists())
            self.assertEqual(ledger.load(IDENTITY).record, before)
            state.fail_review = False
            code, output, error = self.invoke(root, state, reconstruct=True)
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)
            after = ledger.load(IDENTITY).record
            self.assertEqual(after.transaction.result_sha256, original.content_digest())
            self.assertEqual(
                after.transaction.transaction_id, before.transaction.transaction_id
            )
            self.assertEqual(
                after.completed_initial_reviews, before.completed_initial_reviews
            )
            self.assertEqual(
                after.completed_verification_rounds,
                before.completed_verification_rounds,
            )

    def test_reconstruction_refuses_changed_configuration_without_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root)
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            before = LocalSessionLedger(root / "ledger").load(IDENTITY).record
            (root / ".reviewsensei.yml").write_text(
                "schema: 1\ninference:\n  model: changed\n"
            )
            state.calls.clear()
            code, _, error = self.invoke(root, state, reconstruct=True)
            self.assertEqual(code, 1, error)
            self.assertIn("context", error)
            self.assertFalse(any(m != "GET" for m, _, _ in state.calls))
            self.assertEqual(
                LocalSessionLedger(root / "ledger").load(IDENTITY).record, before
            )

    def test_reconstructed_recovery_refuses_learning_prs_before_any_publication(self):
        for result_source in (False, True):
            with (
                self.subTest(result_source=result_source),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                self.original_analysis(root)
                state = _FailedPublication()
                self.assertEqual(self.invoke(root, state)[0], 1)
                ledger = LocalSessionLedger(root / "ledger")
                before = ledger.load(IDENTITY).record
                state.fail_review = False
                state.calls.clear()
                code, _, error = self.invoke(
                    root,
                    state,
                    reconstruct=True,
                    result_source=result_source,
                    extra=("--enable-learning-prs",),
                )
                self.assertEqual(code, 1, error)
                outcome = json.loads((root / "publication-outcome.json").read_text())
                self.assertEqual(outcome["diagnostic"], "recovery_learning_prs_refused")
                self.assertIn(
                    "publication recovery cannot write learning pull requests", error
                )
                self.assertEqual(state.calls, [])
                self.assertEqual(ledger.load(IDENTITY).record, before)

    def test_stale_head_missing_source_and_changed_diff_refuse_without_mutations(self):
        for fault in ("head", "source", "diff"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.original_analysis(root)
                state = _FailedPublication()
                self.assertEqual(self.invoke(root, state)[0], 1)
                ledger = LocalSessionLedger(root / "ledger")
                before = ledger.load(IDENTITY).record
                extra = ()
                if fault == "head":
                    extra = ("--head-sha", "c" * 40)
                elif fault == "source":
                    (root / ".reviewsensei.yml").unlink()
                else:
                    (root / "review.patch").write_text("not a valid unified diff")
                state.calls.clear()
                code, _, error = self.invoke(root, state, reconstruct=True, extra=extra)
                self.assertEqual(code, 1, error)
                self.assertFalse(any(m != "GET" for m, _, _ in state.calls))
                self.assertEqual(ledger.load(IDENTITY).record, before)

    def test_reconstructed_recovery_still_requires_the_actual_accepted_result(self):
        for changed in (False, True):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                self.original_analysis(root)
                result = ReviewResult.from_dict(
                    json.loads((root / "result.json").read_text())
                )
                state = _FailedPublication()
                self.assertEqual(self.invoke(root, state)[0], 1)
                journey_fixture.DefaultPublicationCliJourneyTests.accept_analysis(
                    self, state, result, changed=changed
                )
                state.calls.clear()
                state.fail_review = False
                code, output, error = self.invoke(
                    root, state, reconstruct=True, actions=True
                )
                if changed:
                    self.assertEqual(code, 1, error)
                    self.assertIn("does not match the accepted operation", error)
                    self.assertFalse(any(m != "GET" for m, _, _ in state.calls))
                else:
                    self.assertEqual(code, 0, error)
                    self.assertIn("published", output)
                    self.assertLessEqual(len(state.calls), 60)

    def test_overwritten_original_verification_baseline_requires_original_document(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root, head="c" * 40)
            self.original_analysis(root)
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")
            before = ledger.load(IDENTITY).record
            self.assertEqual(before.completed_verification_rounds, 1)
            state.calls.clear()
            code, _, error = self.invoke(root, state, reconstruct=True)
            self.assertEqual(code, 1, error)
            self.assertIn("supply the original admission context", error)
            self.assertFalse(any(m != "GET" for m, _, _ in state.calls))
            self.assertEqual(ledger.load(IDENTITY).record, before)

    def test_partial_checkpoint_reconstructs_the_authenticated_prior_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root, head="c" * 40)
            self.original_analysis(root, partial=True)
            original = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text())
            )
            self.assertEqual(original.review_status, "partial")
            expected_admission = json.loads((root / "admission.json").read_text())
            self.assertEqual(
                expected_admission["baseline"]["cache_key"]["head_sha"], "c" * 40
            )
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")
            before = ledger.load(IDENTITY).record
            state.fail_review = False
            code, output, error = self.invoke(root, state, reconstruct=True)
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)
            after = ledger.load(IDENTITY).record
            self.assertEqual(after.failed_attempts, before.failed_attempts)
            self.assertEqual(
                after.completed_verification_rounds,
                before.completed_verification_rounds,
            )
            self.assertEqual(after.transaction.result_sha256, original.content_digest())
            self.assertEqual(
                after.convergence_history["baseline"],
                before.convergence_history["baseline"],
            )
            self.assertFalse(
                any(review["state"] == "APPROVED" for review in state.reviews)
            )


if __name__ == "__main__":
    unittest.main()
