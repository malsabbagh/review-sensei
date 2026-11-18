"""Local legacy projection is distinct from lifecycle enrollment or migration."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import GitHubWriteOptions
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger
from tests import default_human_inventory_contract as legacy
from tests import test_durable_finding_lifecycle_restart as current


class LocalLegacyFindingProfileTests(unittest.TestCase):
    def bind(self, app, *, options=None, identity=legacy.IDENTITY, head=legacy.HEAD):
        return app._bind_finding_lifecycle_finalizer(
            app.reviewer.finalizer,
            options=options or GitHubWriteOptions(github_writes=True),
            repository=identity.repository,
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=head,
            app_slug=legacy.APP,
            token="synthetic-read",
        )

    def legacy_app(self, root, *, ledger_type=LocalSessionLedger, writes=False):
        ledger = ledger_type(root, enable_finding_lifecycle_writes=writes)
        result, _ = legacy.human_checkpoint(ledger, count=2, detail=1)
        state = legacy.DefaultHumanState(result.comments)
        return legacy.application(state, ledger), state

    def test_legacy_selection_preserves_original_local_transaction_without_io(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state = self.legacy_app(Path(directory))
            before = app.session_ledger.load(legacy.IDENTITY).record
            self.assertIsNotNone(before.transaction)
            self.assertIsNone(before.finding_lifecycle)
            self.assertIsNone(self.bind(app))
            self.assertEqual(app.session_ledger.load(legacy.IDENTITY).record, before)
            self.assertEqual(state.calls, [])

    def test_lifecycle_modes_and_custom_local_adapters_cannot_downgrade(self):
        class CustomLedger(LocalSessionLedger):
            pass

        for options, ledger_type, writes in (
            (GitHubWriteOptions(github_session_ledger=True), LocalSessionLedger, False),
            (GitHubWriteOptions(max_active_findings=1), LocalSessionLedger, False),
            (GitHubWriteOptions(enable_drift_tracking=True), LocalSessionLedger, False),
            (GitHubWriteOptions(), LocalSessionLedger, True),
            (GitHubWriteOptions(), CustomLedger, False),
        ):
            with self.subTest(options=options, ledger=ledger_type, writes=writes):
                with tempfile.TemporaryDirectory() as directory:
                    app, state = self.legacy_app(
                        Path(directory), ledger_type=ledger_type, writes=writes
                    )
                    before = app.session_ledger.load(legacy.IDENTITY).record
                    with self.assertRaisesRegex(
                        ReviewInputError, "inventory_recovery_required"
                    ):
                        self.bind(app, options=options)
                    self.assertEqual(
                        app.session_ledger.load(legacy.IDENTITY).record, before
                    )
                    self.assertEqual(state.calls, [])

    def test_existing_native_or_lifecycle_state_prevents_legacy_selection(self):
        for attribute in (
            "_active_window_states",
            "_drift_states",
            "_native_finding_receipt_records",
        ):
            with (
                self.subTest(attribute=attribute),
                tempfile.TemporaryDirectory() as directory,
            ):
                app, state = self.legacy_app(Path(directory))
                getattr(app.reviewer, attribute)[
                    (legacy.IDENTITY.repository_id, legacy.IDENTITY.pull_request)
                ] = object()
                with self.assertRaisesRegex(
                    ReviewInputError, "inventory_recovery_required"
                ):
                    self.bind(app)
                self.assertEqual(state.calls, [])

    def test_corrupt_selected_local_root_does_not_become_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state = self.legacy_app(Path(directory))
            path = app.session_ledger._path(legacy.IDENTITY)
            path.write_text("not a session document")
            with self.assertRaisesRegex(
                ReviewInputError, "selected session is unavailable"
            ):
                self.bind(app)
            self.assertEqual(path.read_text(), "not a session document")
            self.assertEqual(state.calls, [])

    def test_disabled_writer_still_restores_actual_enrollment_and_refuses_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            produced = current.DurableFindingRestartTests.run_phase(
                self, root, "window", "initial"
            )
            self.assertIsNone(produced["error"])
            ledger = LocalSessionLedger(root / "ledger")
            before = ledger.load(current.IDENTITY).record
            head = before.finding_lifecycle["manifest"]["binding"]["head_sha"]
            host = current.PersistentNativeService(root / "service.json", head)
            app = legacy.application(host, ledger)
            context = self.bind(app, identity=current.IDENTITY, head=head)
            self.assertIsNotNone(context)
            with self.assertRaisesRegex(
                ReviewInputError, "checkpoint authority unavailable"
            ):
                context.run(lambda: self.fail("disabled writer must not finalize"))
            self.assertEqual(ledger.load(current.IDENTITY).record, before)
            fresh = legacy.application(host, LocalSessionLedger(root / "ledger"))
            with patch(
                "review_sensei.finding_lifecycle_store.select_finding_lifecycle",
                side_effect=ReviewInputError("selected lifecycle part is missing"),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "selected lifecycle part is missing"
                ):
                    self.bind(fresh, identity=current.IDENTITY, head=head)
            self.assertEqual(ledger.load(current.IDENTITY).record, before)

    def test_native_ledger_never_uses_local_legacy_selection(self):
        ledger = GitHubIssueCommentSessionLedger(
            GitHubHttp(), token="synthetic-read", app_slug=legacy.APP
        )
        app = legacy.application(type("Host", (), {"http": ledger.http})(), ledger)
        with patch(
            "review_sensei_github.finding_lifecycle_consumers.finding_lifecycle_consumer"
        ) as factory:
            context = self.bind(app)
            self.assertIs(context, factory.return_value)
            factory.assert_called_once()
            context.bind.assert_called_once_with(app.reviewer.finalizer)
