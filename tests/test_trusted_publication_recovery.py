"""Local/core recovery and current Actions successors retain exact context."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import review_sensei_github as github
import test_default_publication_cli_journey as journey_fixture
from fake_github_http import json_response
from review_sensei_github.cli import main as github_main
from review_sensei_github.review_hooks import GitHubReviewHooks
from test_publication_recovery import APP, BASE, HEAD, IDENTITY, _Broker

import review_sensei.cli as engine_cli
from review_sensei.models import ProviderResponse, ReviewResult
from review_sensei.service import ReviewService
from review_sensei.session import LocalSessionLedger


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
            newline="\n",
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
            self.assertEqual(engine_cli.main(argv, hosted_hooks=GitHubReviewHooks()), 0)
        return argv

    def invoke(
        self,
        root,
        state,
        *,
        reconstruct=False,
        result_source=False,
        actions=False,
        remote=False,
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
            "--config",
            str(root / ".reviewsensei.yml"),
            "--outcome",
            str(root / "publication-outcome.json"),
        ]
        arguments += (
            ["--github-session-ledger"]
            if remote
            else ["--session-ledger", str(root / "ledger")]
        )
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
                self.assertEqual(
                    engine_cli.main(argv, hosted_hooks=GitHubReviewHooks()), 1
                )
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

    def test_remote_reconstruction_and_raw_recovery_share_one_read_phase(self):
        from review_sensei_github import (
            publication_context_recovery,
            publication_recovery,
        )
        from review_sensei_github.application import GitHubApplication
        from review_sensei_github.session_ledger import render_session_comment

        for fault in (None, "source", "missing_parts"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root, state = Path(directory), _FailedPublication()
                self.original_analysis(root)
                self.assertEqual(self.invoke(root, state)[0], 1)
                record = LocalSessionLedger(root / "ledger").load(IDENTITY).record
                state.session_comment = {
                    "id": 600,
                    "body": render_session_comment(
                        repository_id=1, pull_request=1, record=record
                    ),
                    "user": state.app_user,
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
                for item in state.parts:
                    item["updated_at"] = "2026-01-01T00:00:00Z"
                before = state.session_comment["body"]
                if fault == "source":
                    configuration = root / ".reviewsensei.yml"
                    configuration.write_text(
                        configuration.read_text().replace("qwen3.5:4b", "other-model")
                    )
                if fault == "missing_parts":
                    state.parts = [
                        item
                        for item in state.parts
                        if journey_fixture.RESULT_COMMENT_MARKER not in item["body"]
                    ]
                state.calls.clear()
                phases, builders, recoveries, publications, walks = [], [], [], [], []
                original_phase = publication_recovery.publication_recovery_read_phase
                original_builder = (
                    publication_context_recovery.reconstruct_publication_contexts
                )
                original_recover = GitHubApplication.recover_retained_review_result

                @contextmanager
                def phase_factory(**kwargs):
                    queries = []
                    original_open = state.http.opener

                    def observe(request, timeout):
                        parsed = urlparse(request.full_url)
                        if request.method == "GET" and parsed.path.endswith(
                            "/issues/1/comments"
                        ):
                            queries.append(parse_qs(parsed.query))
                        return original_open(request, timeout)

                    with patch.object(state.http, "opener", new=observe):
                        with original_phase(**kwargs) as phase:
                            phases.append((phase, kwargs))
                            try:
                                yield phase
                            finally:
                                walks.append(
                                    sum("since" not in query for query in queries)
                                )

                def builder(**kwargs):
                    builders.append(kwargs)
                    return original_builder(**kwargs)

                def recover(application, **kwargs):
                    recoveries.append(kwargs)
                    return original_recover(application, **kwargs)

                def publish(application, **kwargs):
                    self.assertIsNone(application.session_ledger)
                    publications.append(kwargs)
                    return SimpleNamespace(status="published")

                with (
                    patch.object(
                        publication_recovery,
                        "publication_recovery_read_phase",
                        side_effect=phase_factory,
                    ),
                    patch.object(
                        publication_context_recovery,
                        "reconstruct_publication_contexts",
                        side_effect=builder,
                    ),
                    patch.object(
                        GitHubApplication, "recover_retained_review_result", new=recover
                    ),
                    patch.object(GitHubApplication, "publish_review", new=publish),
                ):
                    code, _, error = self.invoke(
                        root, state, reconstruct=True, remote=True
                    )
                self.assertEqual(code, 0 if fault is None else 1, error)
                self.assertEqual(len(phases), 1)
                self.assertEqual(walks, [1])
                phase, arguments = phases[0]
                self.assertIsNot(phase.ledger, arguments["ledger"])
                self.assertIs(arguments["ledger"].http, state.http)
                self.assertIs(builders[0]["ledger"], phase.ledger)
                if fault != "source":
                    self.assertEqual(len(recoveries), 1)
                    self.assertIs(recoveries[0]["ledger"], phase.ledger)
                    self.assertIs(recoveries[0]["recovery_phase"], phase)
                    self.assertIs(recoveries[0]["budget"], arguments["budget"])
                self.assertEqual(len(publications), 1 if fault is None else 0)
                if publications:
                    self.assertIs(
                        publications[0]["publication_budget"], arguments["budget"]
                    )
                self.assertEqual(state.session_comment["body"], before)
                self.assertTrue(all(method == "GET" for method, _, _ in state.calls))
                calls = len(state.calls)
                self.assertEqual(phase.ledger.load(IDENTITY).status, "ok")
                self.assertGreater(len(state.calls), calls)

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

    def test_separate_actions_reconstruction_refuses_even_an_accepted_result(self):
        for changed in (False, True):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, state = Path(directory), _FailedPublication()
                self.original_analysis(root)
                self.assertEqual(self.invoke(root, state)[0], 1)
                result = ReviewResult.from_dict(
                    json.loads((root / "result.json").read_text())
                )
                journey_fixture.DefaultPublicationCliJourneyTests.accept_analysis(
                    self, state, result, changed=changed
                )
                before = LocalSessionLedger(root / "ledger").load(IDENTITY).record
                parts = [dict(item) for item in state.parts]
                state.calls.clear()
                code, _, error = self.invoke(
                    root, state, reconstruct=True, actions=True
                )
                self.assertEqual(code, 1, error)
                self.assertIn("Actions phase entry is retired", error)
                self.assertEqual(state.calls, [])
                self.assertEqual(state.parts, parts)
                self.assertEqual(
                    LocalSessionLedger(root / "ledger").load(IDENTITY).record, before
                )

    def test_current_original_run_successor_uses_retained_result_and_same_allowance(
        self,
    ):
        from review_sensei_github.session_ledger import parse_session_comment
        from test_api_work_lifecycle import caller_measurement
        from test_api_work_persistence import IDENTITY as CURRENT_IDENTITY
        from test_review_run import EntryFixture

        from review_sensei.api_work import ApiRunAccounting

        with tempfile.TemporaryDirectory() as directory:
            current = EntryFixture(directory, limit=512)

            def interrupted():
                raise KeyboardInterrupt()

            current.before_publication = interrupted
            self.assertEqual(current.invoke(), 130, current.error)
            pending = parse_session_comment(
                current.comment["body"], identity=CURRENT_IDENTITY
            )
            self.assertEqual(pending.transaction.phase, "publication_pending")
            old = ApiRunAccounting.from_document(pending.api_run_accounting)
            retained = {
                key: dict(value) for key, value in current.extra_comments.items()
            }
            for name in ("review.json", "configuration.json", "admission.json"):
                (current.root / name).unlink()
            current.attempt = 2
            current.before_publication = None
            self.assertEqual(current.invoke(), 0, current.error)
            self.assertEqual(current.provider_calls, 1)
            final = parse_session_comment(
                current.comment["body"], identity=CURRENT_IDENTITY
            )
            self.assertEqual(
                final.transaction.transaction_id, pending.transaction.transaction_id
            )
            self.assertEqual(
                final.transaction.result_sha256, pending.transaction.result_sha256
            )
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertEqual(
                final.completed_initial_reviews, pending.completed_initial_reviews
            )
            self.assertEqual(
                final.completed_verification_rounds,
                pending.completed_verification_rounds,
            )
            accounting = ApiRunAccounting.from_document(final.api_run_accounting)
            self.assertEqual(accounting.identity, old.identity)
            self.assertEqual(accounting.configuration, old.configuration)
            self.assertEqual(accounting.startup, old.startup)
            self.assertEqual(accounting.blocks[: len(old.blocks)], old.blocks)
            self.assertEqual(
                accounting.terminal_transaction, final.transaction.transaction_id
            )
            for key, item in retained.items():
                self.assertEqual(current.extra_comments[key], item)
            measurement = caller_measurement(current)
            self.assertEqual(
                measurement["physical_attempts"],
                sum(len(t.observations) for t in current.transports),
            )
            self.assertLessEqual(measurement["physical_attempts"], 512)
            self.assertEqual(measurement["terminal_owner"], "publication-completion")

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

    def test_pre_v2_original_context_remains_publishable_and_is_not_reconstructed(self):
        from review_sensei.context import (
            _configuration_digest,
            stage_configuration_digest,
        )

        original_key = ReviewService.context_cache_key

        def legacy_key(service, request, **kwargs):
            current = original_key(service, request, **kwargs)
            if current is None:
                return None
            stage_digest = stage_configuration_digest(service.stages)
            if request.work_policy_digest is not None:
                stage_digest = _configuration_digest(
                    {"stages": stage_digest, "work_policy": request.work_policy_digest}
                )
            return replace(
                current, model=request.model or "default", stage_digest=stage_digest
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(ReviewService, "context_cache_key", new=legacy_key):
                self.original_analysis(root)
            original_admission = (root / "admission.json").read_bytes()
            state = _FailedPublication()
            code, _, error = self.invoke(root, state)
            self.assertEqual(code, 1, error)
            ledger = LocalSessionLedger(root / "ledger")
            before = ledger.load(IDENTITY).record
            code, _, error = self.invoke(
                root, state, reconstruct=True, result_source=True
            )
            self.assertEqual(code, 1, error)
            self.assertIn("original", error)
            self.assertEqual(ledger.load(IDENTITY).record, before)
            self.assertEqual((root / "admission.json").read_bytes(), original_admission)
            state.fail_review = False
            code, output, error = self.invoke(root, state)
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)
            self.assertEqual((root / "admission.json").read_bytes(), original_admission)


if __name__ == "__main__":
    unittest.main()
