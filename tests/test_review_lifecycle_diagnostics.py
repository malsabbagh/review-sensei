"""Public lifecycle diagnostics; all external services are synthetic."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import IDENTITY
from test_review_run import EntryFixture

from review_sensei.errors import ReviewInputError

PREFIX = "review-sensei: lifecycle "


def outcome(test, stderr):
    lines = [
        line[len(PREFIX) :] for line in stderr.splitlines() if line.startswith(PREFIX)
    ]
    test.assertEqual(len(lines), 1, stderr)
    test.assertLessEqual(len((PREFIX + lines[0] + "\n").encode()), 4096)
    value = json.loads(lines[0])
    test.assertEqual(value["schema_version"], "review-lifecycle-outcome-v1")
    return value


class ReviewLifecycleDiagnosticsTests(unittest.TestCase):
    def test_handled_primary_and_cleanup_errors_keep_separate_closed_classifications(
        self,
    ):
        from review_sensei_github.api_run_requests import ApiRunSessionLedger

        class PrivateCleanupError(OSError):
            def __str__(self):
                raise AssertionError("cleanup text must never be inspected")

        for primary, code, kind in (
            (ReviewInputError("synthetic-primary-private"), 1, "input_or_protocol"),
            (KeyboardInterrupt(), 130, "interrupted"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fixture = EntryFixture(directory)

                def fail():
                    raise primary

                fixture.before_provider = fail
                with patch.object(
                    ApiRunSessionLedger,
                    "claim_analysis_cleanup",
                    side_effect=PrivateCleanupError(),
                ):
                    self.assertEqual(fixture.invoke(), code, fixture.error)
                value = outcome(self, fixture.error)
                self.assertEqual(value["primary_exception_kind"], kind)
                self.assertEqual(value["cleanup_exception_kind"], "os")
                self.assertEqual(value["exit_code"], code)
                self.assertNotIn("synthetic-primary-private", json.dumps(value))
                self.assertEqual(fixture.provider_calls, 1)
                self.assertIsNone(fixture.accounted.accounting.terminal_transaction)

    def test_artifact_policy_distinguishes_disabled_skipped_and_unavailable(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        for mode, kind, configured, policy in (
            ("none", "review", "none", "disabled"),
            ("diagnostics", "review", "diagnostics", "eligible"),
            ("diagnostics", "restart", "diagnostics", "eligible"),
            ("diagnostics", "command", "diagnostics", "skipped"),
            ("diagnostics", "reply", "diagnostics", "skipped"),
            ("all", "review", "unavailable", "unavailable"),
            ("secret-invalid", "review", "unavailable", "unavailable"),
        ):
            with (
                self.subTest(mode=mode, kind=kind),
                patch.dict(os.environ, {"ARTIFACTS": mode}),
            ):
                stream = io.StringIO()
                with redirect_stderr(stream), diagnostic.lifecycle(kind) as current:
                    current.returned(0)
                value = outcome(self, stream.getvalue())
                self.assertEqual(value["artifacts"], configured)
                self.assertEqual(value["artifact_policy_state"], policy)
                self.assertEqual(value["artifact_upload_state"], "unavailable")
                self.assertNotIn("secret-invalid", json.dumps(value))

    def test_diagnostic_cancellation_cannot_replace_business_result_or_error(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        from review_sensei import cli as core

        for fault in (OSError("private-output"), KeyboardInterrupt(), SystemExit(99)):

            class BrokenSink:
                def write(self, _):
                    raise fault

            with (
                self.subTest(fault=type(fault).__name__),
                redirect_stderr(BrokenSink()),
            ):
                with diagnostic.lifecycle("review") as current:
                    self.assertEqual(current.returned(17), 17)
                primary = ReviewInputError("original")
                with self.assertRaises(ReviewInputError) as raised:
                    with diagnostic.lifecycle("review"):
                        raise primary
                self.assertIs(raised.exception, primary)
                with patch.object(core, "_main", side_effect=KeyboardInterrupt()):
                    self.assertEqual(core.main([]), 130)

    def test_valid_primary_survives_unknown_cleanup_without_raw_labels(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        for cleanup in ("new-private-cleanup-kind", {"private": "value"}):
            stream = io.StringIO()
            with redirect_stderr(stream), diagnostic.lifecycle("review") as current:
                current.classified_failure("os", cleanup)
                current.returned(1)
            value = outcome(self, stream.getvalue())
            self.assertEqual(value["primary_exception_kind"], "os")
            self.assertEqual(value["cleanup_exception_kind"], "unexpected")
            self.assertNotIn("private", json.dumps(value))

    def test_neutral_observer_classifies_all_closed_kinds_without_messages(self):
        from review_sensei.failure_observation import exception_kind

        class PrivateError(Exception):
            def __str__(self):
                raise AssertionError("message must not be inspected")

        for error, expected in (
            (KeyboardInterrupt(), "interrupted"),
            (ReviewInputError("private"), "input_or_protocol"),
            (OSError("private"), "os"),
            (ValueError("private"), "value"),
            (SystemExit(99), "system_exit"),
            (PrivateError(), "unexpected"),
        ):
            self.assertEqual(exception_kind(error), expected)

    def test_host_diagnostics_do_not_import_the_engine_cli(self):
        code = """
import importlib.abc,sys
class NoCli(importlib.abc.MetaPathFinder):
    def find_spec(self,name,*args):
        if name == 'review_sensei.cli':
            raise AssertionError('diagnostics must not depend on CLI import')
sys.meta_path.insert(0,NoCli())
from review_sensei.errors import ReviewInputError
from review_sensei_github.review_lifecycle_diagnostics import lifecycle
with lifecycle('review') as current:
    current.failed(ReviewInputError('private-primary'))
    current.returned(1)
assert 'review_sensei.cli' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("private-primary", result.stderr)

    def test_handled_primary_message_sink_cannot_change_original_exit(self):
        import builtins

        from review_sensei.errors import ReviewFormatError

        original_print = builtins.print
        faults = []

        def print_with_fault(*args, **kwargs):
            if any("synthetic-primary-output" in str(arg) for arg in args):
                faults.append("write")
                raise KeyboardInterrupt
            return original_print(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)

            def fail():
                raise ReviewFormatError("synthetic-primary-output")

            fixture.before_provider = fail
            with patch("builtins.print", side_effect=print_with_fault):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assertEqual(faults, ["write"])
            value = outcome(self, fixture.error)
            self.assertEqual(value["exit_code"], 1)
            self.assertEqual(value["primary_exception_kind"], "input_or_protocol")
            # A provider-entered refusal has no abort witness; retain that
            # independent cleanup refusal instead of treating it as settled.
            self.assertEqual(value["cleanup_exception_kind"], "input_or_protocol")

    def test_classification_observer_failure_and_fallback_preserve_original(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        from review_sensei import failure_observation as observation

        for fault in (OSError("private-observer"), KeyboardInterrupt(), SystemExit(99)):

            def broken(*_):
                raise fault

            with observation.observe_cli_failures(broken):
                observation.notify_cli_failure(
                    ReviewInputError("private-primary"), OSError("private-cleanup")
                )
        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review") as current:
            observation.notify_cli_failure(
                ReviewInputError("private-primary"), OSError("private-cleanup")
            )
            current.scope["overflow"] = "x" * 5000
            current.returned(1)
        value = outcome(self, stream.getvalue())
        self.assertEqual(value["reason"], "diagnostic_size_limit")
        self.assertEqual(value["primary_exception_kind"], "input_or_protocol")
        self.assertEqual(value["cleanup_exception_kind"], "os")
        self.assertNotIn("private", json.dumps(value))

    def test_command_failure_after_admission_does_not_blame_old_predecessor(self):
        from test_action_run import CommandEntryFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory)
            self.assertEqual(
                fixture.invoke_command("@reviewsensei review status"), 0, fixture.error
            )
            fixture.SOURCE_ID += 1
            fixture.select_command(
                "@reviewsensei override RS-ABC123 acceptable risk", new_run=True
            )
            self.assertEqual(fixture.invoke_command(), 1, fixture.error)
            self.assertIn(
                "current command review inventory is unavailable", fixture.error
            )
            value = outcome(self, fixture.error)
            self.assertIsNotNone(value["predecessor"])
            self.assertIsNotNone(value["scope"]["admitted"])
            self.assertEqual(value["phase"], "operation")
            self.assertEqual(value["step"], "operation")
            self.assertEqual(value["reason"], "operation_failed")
            self.assertEqual(value["caller"]["action_phase"], "pending")
            self.assertEqual(fixture.provider_calls, 0)

    def test_close_exceptions_preserve_returned_publication_and_unknown_completion(
        self,
    ):
        for already_published in (False, True):
            for fault, code in (
                (OSError("synthetic-private-tail"), 1),
                (KeyboardInterrupt(), 130),
            ):
                with (
                    self.subTest(
                        already_published=already_published, fault=type(fault).__name__
                    ),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = EntryFixture(directory)
                    if already_published:
                        head = fixture.live_head
                        self.assertEqual(fixture.invoke(), 0, fixture.error)
                        fixture.next_run()
                        fixture.requested_head = fixture.live_head = head
                    with patch.object(
                        ApiRunRequestTransport, "finish_review_run", side_effect=fault
                    ):
                        self.assertEqual(fixture.invoke(), code, fixture.error)
                    value = outcome(self, fixture.error)
                    self.assertEqual(value["phase"], "caller_close")
                    self.assertEqual(value["step"], "caller_close")
                    self.assertEqual(value["reason"], "caller_close_failed")
                    self.assertEqual(value["exit_code"], code)
                    self.assertEqual(
                        value["publication"]["publication_status"],
                        "already_published" if already_published else "published",
                    )
                    self.assertIsNone(value["publication"]["accounting_terminal"])
                    self.assertNotIn("synthetic-private-tail", json.dumps(value))
                    record = parse_session_comment(
                        fixture.comment["body"], identity=IDENTITY
                    )
                    self.assertEqual(record.transaction.phase, "publication_succeeded")
                    self.assertIsNone(record.api_run_accounting["terminal_transaction"])
                    self.assertEqual(fixture.provider_calls, 1)

    def test_broker_upstream_failure_is_identified_before_review_or_enrollment(self):
        from urllib.error import HTTPError
        from urllib.parse import urlsplit

        from review_sensei.broker_diagnostics import BrokerDiagnostic

        class RefusedFixture(EntryFixture):
            def external(self, request, timeout):
                if urlsplit(request.full_url).hostname == "broker.github.test":
                    self.trace.append(
                        (
                            self.attempt,
                            request.get_method(),
                            urlsplit(request.full_url).path,
                        )
                    )
                    diagnostic = BrokerDiagnostic("github_workflow_tag_unavailable")
                    raise HTTPError(
                        request.full_url,
                        503,
                        "synthetic upstream failure",
                        {"cf-ray": "a53bf0792e9475e4-ORD"},
                        io.BytesIO(
                            json.dumps(
                                {
                                    "error": "synthetic opaque upstream error",
                                    "diagnostic": {
                                        "version": 1,
                                        "code": diagnostic.code,
                                        "stage": diagnostic.stage,
                                        "action": diagnostic.action,
                                        "upstream_status": 429,
                                    },
                                }
                            ).encode()
                        ),
                    )
                return super().external(request, timeout)

        with tempfile.TemporaryDirectory() as directory:
            fixture = RefusedFixture(directory)
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["phase"], "bootstrap")
            self.assertEqual(value["step"], "broker_actions")
            self.assertEqual(value["broker"]["code"], "github_workflow_tag_unavailable")
            self.assertEqual(value["broker"]["upstream_status"], 429)
            self.assertEqual(value["broker"]["correlation_id"], "a53bf0792e9475e4-ORD")
            self.assertIsNone(value["predecessor"])
            self.assertIsNone(value["scope"]["frozen"])
            self.assertIsNone(fixture.comment)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertEqual(fixture.accounted.startup_dispatches, 6)

    def test_predecessor_without_transaction_is_explicit_and_not_completed(self):
        from review_sensei_github.review_run import ReviewRun

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            # Interrupt the engine boundary after real bootstrap enrollment.
            # This is a synthetic control, not evidence about Site142's tail.
            with patch.object(ReviewRun, "execute", return_value=1):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            previous = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertIsNone(previous.transaction)
            self.assertIsNone(previous.api_run_accounting["terminal_transaction"])
            before = fixture.comment["body"]
            fixture.next_run()
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["step"], "predecessor_admission")
            self.assertEqual(value["reason"], "predecessor_admission_failed")
            self.assertFalse(value["predecessor"]["transaction_present"])
            self.assertIsNone(value["predecessor"]["phase"])
            self.assertEqual(
                value["predecessor"]["record_sha256"], previous.record_sha256
            )
            self.assertIsNone(value["predecessor"]["terminal_transaction"])
            self.assertEqual(fixture.comment["body"], before)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertFalse(
                any(
                    m in {"POST", "PATCH"} and "/issues" in p
                    for _, m, p in fixture.trace
                )
            )

    def test_nonpolicy_default_branch_advance_is_an_admission_race_not_old_accounting(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            old = fixture.policy
            (fixture.root / "package-lock.json").write_text('{"synthetic":true}\n')
            subprocess.run(
                ["git", "-C", str(fixture.root), "add", "package-lock.json"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "synthetic non-policy advance",
                ],
                check=True,
                capture_output=True,
            )
            new = subprocess.check_output(
                ["git", "-C", str(fixture.root), "rev-parse", "HEAD"], text=True
            ).strip()
            before = subprocess.check_output(
                ["git", "-C", str(fixture.root), "show", old + ":.reviewsensei.yml"]
            )
            after = subprocess.check_output(
                ["git", "-C", str(fixture.root), "show", new + ":.reviewsensei.yml"]
            )
            self.assertEqual(before, after)
            subprocess.run(
                ["git", "-C", str(fixture.root), "checkout", "--detach", old],
                check=True,
                capture_output=True,
            )
            fixture.policy_ref = new
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assertIn(
                "trusted policy commit changed before admission", fixture.error
            )
            value = outcome(self, fixture.error)
            self.assertEqual(value["scope"]["requested"]["policy_commit"], old)
            self.assertEqual(value["scope"]["observed_policy"]["policy_commit"], new)
            self.assertIsNone(
                value["scope"]["observed_policy"]["policy_content_sha256"]
            )
            self.assertIsNone(value["predecessor"])
            self.assertEqual(value["reason"], "bootstrap_failed")
            self.assertEqual(value["step"], "default_policy_ref")
            self.assertIsNone(fixture.comment)
            self.assertEqual(fixture.provider_calls, 0)

    def test_all_three_captured_roots_still_refuse_through_public_command_entry(self):
        from review_lifecycle_incident_fixture import IncidentCommandFixture

        for name in ("403", "session122", "sitevault141"):
            incident = json.loads(
                (
                    Path(__file__).parent
                    / "fixtures"
                    / "regressions"
                    / ("review-lifecycle-" + name + ".json")
                ).read_text()
            )
            if name == "session122":
                clocks = incident["clock_evidence"]
                self.assertTrue(
                    clocks["api_run_created_at"].startswith("2026-11-01T09:")
                )
                self.assertTrue(clocks["log_exit_at"].startswith("2026-11-01T08:"))
                self.assertTrue(
                    clocks["ledger_updated_at"].startswith("2026-11-01T08:")
                )
                self.assertFalse(clocks["ordering_qualified"])
            with (
                self.subTest(incident=name),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = IncidentCommandFixture(directory, incident)
                before = fixture.comment["body"]
                previous = fixture.recorded()
                self.assertEqual(
                    previous.record_sha256, incident["expected"]["record_sha256"]
                )
                self.assertEqual(fixture.invoke_incident(), 1, fixture.error)
                self.assertIn("previous caller run is not terminal", fixture.error)
                value = outcome(self, fixture.error)
                self.assertEqual(value["kind"], "command")
                self.assertEqual(value["reason"], "predecessor_admission_failed")
                self.assertTrue(value["predecessor"]["transaction_present"])
                self.assertEqual(
                    value["predecessor"]["record_sha256"], previous.record_sha256
                )
                self.assertEqual(
                    value["predecessor"]["run_id"], incident["predecessor_run_id"]
                )
                self.assertEqual(
                    value["predecessor"]["original_tail_reason"], "unavailable"
                )
                self.assertEqual(
                    value["scope"]["requested"]["policy_commit"], fixture.policy
                )
                self.assertEqual(
                    value["predecessor"]["policy_commit"],
                    incident["expected"]["policy_commit"],
                )
                self.assertEqual(fixture.comment["body"], before)
                self.assertEqual(fixture.recorded(), previous)
                self.assertEqual(
                    len(previous.dispositions),
                    incident["expected"]["historical_dispositions"],
                )
                self.assertFalse(
                    any(
                        d["head_sha"] == previous.transaction.head_sha
                        for d in previous.dispositions
                    )
                )
                self.assertEqual(
                    previous.api_run_accounting["configuration"]["limit"],
                    incident["expected"]["frozen_limit"],
                )
                self.assertEqual(
                    len(previous.api_run_accounting["completed"]),
                    incident["expected"]["recorded_completed"],
                )
                self.assertEqual(fixture.native_mutations, [])
                self.assertEqual(fixture.provider_calls, 0)

    def test_changed_base_head_and_mixed_clock_metadata_do_not_rebind_predecessor(self):
        from review_lifecycle_incident_fixture import IncidentCommandFixture

        incident = json.loads(
            (
                Path(__file__).parent
                / "fixtures/regressions/review-lifecycle-sitevault141.json"
            ).read_text()
        )
        clocks = incident["clock_evidence"]
        self.assertTrue(clocks["api_run_created_at"].startswith("2026-11-01T09:"))
        self.assertTrue(clocks["log_exit_at"].startswith("2026-11-01T08:"))
        self.assertFalse(clocks["ordering_qualified"])
        for base_ref, base, head, stamp in (
            (
                "another-target",
                incident["expected"]["base_sha"],
                incident["expected"]["head_sha"],
                clocks["log_exit_at"],
            ),
            (
                "main",
                "b" * 40,
                incident["expected"]["head_sha"],
                clocks["api_run_updated_at"],
            ),
            (
                "main",
                incident["expected"]["base_sha"],
                "c" * 40,
                clocks["api_run_updated_at"],
            ),
        ):
            with (
                self.subTest(base_ref=base_ref, base=base, head=head),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = IncidentCommandFixture(directory, incident)
                fixture.synthetic_base_ref = base_ref
                fixture.live_base = base
                fixture.live_head = fixture.requested_head = head
                fixture.synthetic_cessation_time = stamp
                before = fixture.comment["body"]
                self.assertEqual(fixture.invoke_incident(), 1, fixture.error)
                value = outcome(self, fixture.error)
                self.assertEqual(value["scope"]["observed"]["base_ref"], base_ref)
                self.assertEqual(value["scope"]["observed"]["base_sha"], base)
                self.assertEqual(value["scope"]["observed"]["head_sha"], head)
                self.assertEqual(
                    value["predecessor"]["base_sha"], incident["expected"]["base_sha"]
                )
                self.assertEqual(
                    value["predecessor"]["head_sha"], incident["expected"]["head_sha"]
                )
                self.assertEqual(
                    value["predecessor"]["policy_commit"],
                    clocks["original_policy_commit"],
                )
                self.assertNotEqual(
                    value["scope"]["requested"]["policy_commit"],
                    clocks["original_policy_commit"],
                )
                self.assertEqual(value["scope"]["transition_reason"], "not_evaluated")
                self.assertEqual(fixture.comment["body"], before)
                self.assertEqual(fixture.native_mutations, [])
                self.assertEqual(fixture.provider_calls, 0)

    def test_public_success_reports_exact_scope_without_optional_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            fixture.before_entry = lambda: os.environ.__setitem__("ARTIFACTS", "none")
            fixture.provider_summary = "synthetic-provider-body-must-not-be-logged"
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["kind"], "review")
            self.assertEqual(value["phase"], "complete")
            self.assertEqual(value["reason"], "completed")
            self.assertEqual(value["exit_code"], 0)
            self.assertEqual(value["scope"]["admitted"]["head_sha"], fixture.live_head)
            self.assertEqual(value["scope"]["admitted"]["base_sha"], fixture.live_base)
            self.assertEqual(
                value["scope"]["admitted"]["policy_commit"], fixture.policy
            )
            self.assertTrue(value["publication"]["accounting_terminal"])
            self.assertEqual(value["artifacts"], "none")
            self.assertNotIn(fixture.provider_summary, json.dumps(value))
            self.assertEqual(fixture.provider_calls, 1)

    def test_known_withholding_and_finished_caller_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            self.assertEqual(fixture.invoke(human=True), 0, fixture.error)
            value = outcome(self, fixture.error)
            self.assertTrue(value["publication"]["accounting_terminal"])
            self.assertEqual(
                value["publication"]["diagnostic"], "human_adjudication_open"
            )
            self.assertFalse(
                any(review["state"] == "APPROVED" for review in fixture.reviews)
            )

    def test_uncertain_cleanup_reports_pending_without_marking_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            with patch.object(
                ReviewPublisher,
                "_finish_thread_cleanup",
                lambda _, value: replace(value, thread_resolution="unconfirmed"),
            ):
                self.assertEqual(fixture.invoke(human=True), 1, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["reason"], "thread_cleanup_unconfirmed")
            self.assertFalse(value["publication"]["accounting_terminal"])
            self.assertEqual(value["publication"]["publication_status"], "published")
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(record.transaction.phase, "publication_succeeded")
            self.assertIsNone(record.api_run_accounting["terminal_transaction"])

    def test_terminal_close_error_is_classified_without_copying_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            with patch.object(
                ApiRunRequestTransport,
                "finish_review_run",
                side_effect=ReviewInputError("synthetic-secret-must-not-be-logged"),
            ):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["reason"], "terminal_close_unconfirmed")
            self.assertFalse(value["publication"]["accounting_terminal"])
            self.assertNotIn("synthetic-secret", json.dumps(value))

    def test_bootstrap_refusal_names_predecessor_but_never_infers_its_tail_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            with patch.object(
                ReviewPublisher,
                "_finish_thread_cleanup",
                lambda _, value: replace(value, thread_resolution="unconfirmed"),
            ):
                self.assertEqual(fixture.invoke(human=True), 1, fixture.error)
            before = fixture.comment["body"]
            old = parse_session_comment(before, identity=IDENTITY)
            old_run = old.api_run_accounting["identity"]["run_id"]
            fixture.next_run()
            self.assertEqual(fixture.invoke(human=True), 1, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["phase"], "bootstrap")
            self.assertEqual(value["reason"], "predecessor_admission_failed")
            self.assertEqual(value["predecessor"]["record_sha256"], old.record_sha256)
            self.assertEqual(value["predecessor"]["run_id"], old_run)
            self.assertEqual(
                value["predecessor"]["original_tail_reason"], "unavailable"
            )
            self.assertEqual(fixture.comment["body"], before)
            self.assertEqual(fixture.provider_calls, 1)

    def test_already_published_return_reports_its_own_confirmed_close(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            head = fixture.live_head
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            fixture.next_run()
            fixture.requested_head = fixture.live_head = head
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(
                value["publication"]["publication_status"], "already_published"
            )
            self.assertTrue(value["publication"]["accounting_terminal"])
            self.assertEqual(fixture.provider_calls, 1)

    def test_next_full_review_keeps_frozen_and_current_scopes_within_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            old_head = fixture.live_head
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            fixture.next_run()
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["reason"], "completed")
            self.assertEqual(value["predecessor"]["head_sha"], old_head)
            self.assertEqual(value["scope"]["frozen"]["head_sha"], fixture.live_head)
            self.assertEqual(value["scope"]["admitted"]["head_sha"], fixture.live_head)
            self.assertEqual(value["scope"]["observed_policy"]["policy_ref"], "main")
            self.assertEqual(fixture.provider_calls, 2)

    def test_outcome_file_failure_does_not_mislabel_an_already_finished_caller(self):
        original = Path.write_text

        def write(path, *args, **kwargs):
            if path.name == "publication.json":
                raise OSError("synthetic-private-path-not-for-diagnostics")
            return original(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            with patch.object(Path, "write_text", write):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["reason"], "outcome_write_failed")
            self.assertEqual(value["exit_code"], 1)
            self.assertTrue(value["publication"]["accounting_terminal"])
            self.assertNotIn("synthetic-private-path", json.dumps(value))
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(
                record.api_run_accounting["terminal_transaction"],
                record.transaction.transaction_id,
            )

    def test_handled_engine_cancellation_keeps_exit_and_pending_publication(self):
        def stop():
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            fixture.before_provider = stop
            self.assertEqual(fixture.invoke(), 130, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["phase"], "analysis")
            self.assertEqual(value["reason"], "engine_stopped")
            self.assertEqual(value["exit_code"], 130)
            self.assertIsNone(value["publication"])


class DiagnosticOutputBoundaryTests(unittest.TestCase):
    def test_untrusted_cli_text_error_text_and_diff_body_are_not_rendered(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        marker = "synthetic-sensitive-provider-body"
        stream = io.StringIO()
        failure = ReviewInputError(marker)
        with redirect_stderr(stream), self.assertRaises(ReviewInputError) as raised:
            with diagnostic.lifecycle("review"):
                diagnostic.requested(
                    Namespace(
                        repository="owner/" + marker,
                        repository_id=123,
                        pull_request=7,
                        base_sha="a" * 40,
                        head_sha="b" * 40,
                    ),
                    "c" * 40,
                )
                diagnostic.observed(
                    {
                        "base": {"ref": marker + "\nsecret", "sha": "a" * 40},
                        "head": {"sha": "b" * 40},
                    }
                )
                diagnostic.analysis(
                    SimpleNamespace(
                        result=SimpleNamespace(
                            transaction=SimpleNamespace(
                                base_sha="a" * 40,
                                head_sha="b" * 40,
                                transaction_id="c" * 64,
                                configuration_digest="d" * 64,
                            )
                        ),
                        diff=marker,
                    )
                )
                raise failure
        self.assertIs(raised.exception, failure)
        value = outcome(self, stream.getvalue())
        self.assertNotIn(marker, stream.getvalue())
        self.assertIsNone(value["scope"]["requested"]["repository"])
        self.assertIsNone(value["scope"]["observed"]["base_ref"])
        self.assertIsNone(value["scope"]["admitted"])
        self.assertFalse(value["caller"]["accounting_available"])
        self.assertIsNone(value["caller"]["recorded_pending"])

    def test_oversized_diagnostic_has_explicit_bounded_fallback(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review") as current:
            current.scope["requested"] = {"bad-future-field": "x" * 10000}
            self.assertEqual(current.returned(7), 7)
        value = outcome(self, stream.getvalue())
        self.assertEqual(value["reason"], "diagnostic_size_limit")
        self.assertEqual(value["exit_code"], 7)
        self.assertFalse(value["authority"])
        self.assertNotIn("bad-future-field", stream.getvalue())

    def test_unavailable_accounting_view_has_safe_fallback_without_masking_result(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review") as current:
            diagnostic.caller(SimpleNamespace(_state=object()))
            self.assertEqual(current.returned(13), 13)
        value = outcome(self, stream.getvalue())
        self.assertEqual(value["reason"], "diagnostic_unavailable")
        self.assertEqual(value["exit_code"], 13)

    def test_broken_diagnostic_sink_preserves_result_and_original_exception(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        class BrokenSink:
            def write(self, _):
                raise OSError("synthetic sink unavailable")

        with redirect_stderr(BrokenSink()):
            with diagnostic.lifecycle("review") as current:
                self.assertEqual(current.returned(17), 17)
            failure = ReviewInputError("unchanged failure")
            with self.assertRaises(ReviewInputError) as raised:
                with diagnostic.lifecycle("review"):
                    raise failure
            self.assertIs(raised.exception, failure)

    def test_nested_and_subsequent_entries_do_not_reuse_another_scope(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        stream = io.StringIO()
        with redirect_stderr(stream):
            with diagnostic.lifecycle("review") as outer:
                diagnostic.requested(
                    Namespace(repository_id=123, pull_request=7), "a" * 40
                )
                with diagnostic.lifecycle("command") as inner:
                    diagnostic.requested(
                        Namespace(repository_id=456, pull_request=8), "b" * 40
                    )
                    inner.returned(1)
                outer.returned(0)
            with diagnostic.lifecycle("reply") as later:
                later.returned(0)
        values = [
            json.loads(line[len(PREFIX) :]) for line in stream.getvalue().splitlines()
        ]
        self.assertEqual([v["kind"] for v in values], ["command", "review", "reply"])
        self.assertEqual(values[0]["scope"]["requested"]["repository_id"], 456)
        self.assertEqual(values[1]["scope"]["requested"]["repository_id"], 123)
        self.assertIsNone(values[2]["scope"]["requested"])
        self.assertTrue(all(v["publication"] is None for v in values))


class CheckProofHookDiagnosticsTests(unittest.TestCase):
    def test_hook_failure_stages_are_closed_bounded_and_context_local(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        secret = "SENTINEL_PRIVATE_PROOF_TOKEN_https://private.invalid/account-999"
        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review"):
            for _ in range(100):
                for stage in (
                    "publication",
                    "response",
                    "dispatch",
                    secret,
                    secret * 1000,
                    None,
                    {},
                    object(),
                ):
                    diagnostic.check_proof_hook_failed(stage)
        value = outcome(self, stream.getvalue())
        self.assertEqual(
            value["check_proof_hook_failures"], ["dispatch", "response", "publication"]
        )
        self.assertNotIn(secret, stream.getvalue())
        diagnostic.check_proof_hook_failed("dispatch")
        later = io.StringIO()
        with redirect_stderr(later), diagnostic.lifecycle("command"):
            pass
        self.assertEqual(
            outcome(self, later.getvalue())["check_proof_hook_failures"], []
        )

    def test_original_hook_errors_only_add_stage_without_finishing_accounting(self):
        from urllib.error import HTTPError
        from urllib.parse import urlsplit

        from review_sensei_github import check_accounting

        secret = "SENTINEL_PRIVATE_PROOF_TOKEN_https://private.invalid/account-999"
        for hook, stage in (
            ("_dispatch", "dispatch"),
            ("_response", "response"),
            ("confirm_denied_publication", "publication"),
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                fixture = EntryFixture(directory)
                external = fixture.external

                def denied(request, timeout):
                    payload = json.loads(request.data) if request.data else {}
                    if payload.get("capability") == "check_publish":
                        fixture.trace.append(
                            (
                                fixture.attempt,
                                request.method,
                                urlsplit(request.full_url).path,
                            )
                        )
                        raise HTTPError(
                            request.full_url, 403, "synthetic", {}, io.BytesIO(b"{}")
                        )
                    return external(request, timeout)

                fixture.external = denied
                with patch.object(
                    check_accounting, hook, side_effect=RuntimeError(secret)
                ):
                    self.assertEqual(fixture.invoke(), 1, fixture.error)
                value = outcome(self, fixture.error)
                self.assertEqual(value["check_proof_hook_failures"], [stage])
                self.assertEqual(
                    value["publication"]["publication_status"], "published"
                )
                self.assertFalse(value["publication"]["accounting_terminal"])
                self.assertEqual(value["reason"], "publication_finalization_incomplete")
                self.assertNotIn(secret, fixture.error)
                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(record.transaction.phase, "publication_succeeded")
                self.assertIsNone(record.api_run_accounting["terminal_transaction"])
                self.assertEqual(
                    len(fixture.accounted.observations), len(fixture.trace)
                )
                self.assertEqual(fixture.provider_calls, 1)

    def test_broken_stage_reporter_never_replaces_the_original_operation(self):
        from urllib.error import HTTPError
        from urllib.parse import urlsplit

        from review_sensei_github import check_accounting
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        for hook in ("_dispatch", "_response", "confirm_denied_publication"):
            with self.subTest(hook=hook), tempfile.TemporaryDirectory() as directory:
                fixture = EntryFixture(directory)
                external = fixture.external

                def denied(request, timeout):
                    payload = json.loads(request.data) if request.data else {}
                    if payload.get("capability") == "check_publish":
                        fixture.trace.append(
                            (
                                fixture.attempt,
                                request.method,
                                urlsplit(request.full_url).path,
                            )
                        )
                        raise HTTPError(
                            request.full_url, 403, "synthetic", {}, io.BytesIO(b"{}")
                        )
                    return external(request, timeout)

                fixture.external = denied
                with (
                    patch.object(
                        check_accounting,
                        hook,
                        side_effect=RuntimeError("private-hook-detail"),
                    ),
                    patch.object(
                        diagnostic,
                        "check_proof_hook_failed",
                        side_effect=SystemExit("private-reporter-detail"),
                    ),
                ):
                    self.assertEqual(fixture.invoke(), 1, fixture.error)
                value = outcome(self, fixture.error)
                self.assertEqual(value["check_proof_hook_failures"], [])
                self.assertEqual(
                    value["publication"]["publication_status"], "published"
                )
                self.assertFalse(value["publication"]["accounting_terminal"])
                self.assertNotIn("private-hook-detail", fixture.error)
                self.assertNotIn("private-reporter-detail", fixture.error)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertIsNone(fixture.accounted.accounting.terminal_transaction)

    def test_primary_cancellation_is_not_swallowed_or_reclassified_as_hook_failure(
        self,
    ):
        from review_sensei_github import check_accounting

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            with patch.object(
                check_accounting,
                "_dispatch",
                side_effect=KeyboardInterrupt("private-cancellation-detail"),
            ):
                self.assertEqual(fixture.invoke(), 130, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(value["exception_kind"], "interrupted")
            self.assertEqual(value["check_proof_hook_failures"], [])
            self.assertNotIn("private-cancellation-detail", fixture.error)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertEqual(fixture.trace, [])
            self.assertEqual(fixture.accounted.observations[0]["outcome"], "unknown")

    def test_nested_contexts_fallback_and_broken_output_preserve_closed_stages(self):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review") as outer:
            diagnostic.check_proof_hook_failed("dispatch")
            with diagnostic.lifecycle("command"):
                diagnostic.check_proof_hook_failed("publication")
            diagnostic.check_proof_hook_failed("response")
            outer.scope["synthetic-oversized"] = "x" * 5000
        values = [
            json.loads(line[len(PREFIX) :]) for line in stream.getvalue().splitlines()
        ]
        self.assertEqual(values[0]["check_proof_hook_failures"], ["publication"])
        self.assertEqual(
            values[1]["check_proof_hook_failures"], ["dispatch", "response"]
        )
        self.assertEqual(values[1]["reason"], "diagnostic_size_limit")
        self.assertLessEqual(len(stream.getvalue().splitlines()[1].encode()) + 1, 4096)

        class BrokenOutput:
            def write(self, _value):
                raise BrokenPipeError("private-output-detail")

        original = KeyboardInterrupt("private-original-detail")
        with (
            redirect_stderr(BrokenOutput()),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            with diagnostic.lifecycle("review"):
                diagnostic.check_proof_hook_failed("dispatch")
                raise original
        self.assertIs(caught.exception, original)
        with redirect_stderr(BrokenOutput()), diagnostic.lifecycle("review") as current:
            diagnostic.check_proof_hook_failed("response")
            self.assertEqual(current.returned(37), 37)

    def test_stage_recorder_failure_and_malformed_state_have_no_opaque_output(self):
        from unittest.mock import Mock

        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        with patch.object(
            diagnostic,
            "_ACTIVE",
            Mock(get=Mock(side_effect=SystemExit("private-context-detail"))),
        ):
            diagnostic.check_proof_hook_failed("dispatch")
        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review") as current:
            current._check_proof_failure_mask = "SENTINEL_PRIVATE_STATE"
        self.assertIsNone(outcome(self, stream.getvalue())["check_proof_hook_failures"])
        self.assertNotIn("SENTINEL_PRIVATE_STATE", stream.getvalue())

    def test_recorded_tokens_cannot_close_an_unknown_mutation_or_add_requests(self):
        from urllib.parse import urlsplit

        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            external = fixture.external
            lost = False

            def unknown(request, timeout):
                nonlocal lost
                path = urlsplit(request.full_url).path
                if (
                    request.method == "POST"
                    and path.endswith("/check-runs")
                    and not lost
                ):
                    lost = True
                    fixture.trace.append((fixture.attempt, request.method, path))
                    raise TimeoutError("private-unknown-effect")
                return external(request, timeout)

            def before_publication():
                for stage in ("dispatch", "response", "publication"):
                    diagnostic.check_proof_hook_failed(stage)
                try:
                    fixture.http.request(
                        "POST",
                        "/repos/owner/repo/check-runs",
                        token="synthetic",
                        body={"head_sha": "b" * 40},
                    )
                except Exception:
                    pass

            fixture.external = unknown
            fixture.before_publication = before_publication
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            value = outcome(self, fixture.error)
            self.assertEqual(
                value["check_proof_hook_failures"],
                ["dispatch", "response", "publication"],
            )
            self.assertEqual(value["reason"], "terminal_close_unconfirmed")
            self.assertFalse(value["publication"]["accounting_terminal"])
            self.assertIsNone(fixture.accounted.accounting.terminal_transaction)
            unknowns = [
                o
                for o in fixture.accounted.observations
                if o["outcome"] == "unknown" and o.get("mutation")
            ]
            self.assertEqual(len(unknowns), 1)
            self.assertNotIn(
                unknowns[0]["request_id"], fixture.accounted._reconciled_mutations
            )
            self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertEqual(fixture.provider_calls, 1)

    def test_missing_or_unreadable_stage_state_does_not_break_fallback_or_cancellation(
        self,
    ):
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review") as current:
            del current._check_proof_failure_mask
            current.scope["oversized"] = "x" * 5000
            self.assertEqual(current.returned(37), 37)
        value = outcome(self, stream.getvalue())
        self.assertEqual(value["reason"], "diagnostic_size_limit")
        self.assertEqual(value["exit_code"], 37)
        self.assertIsNone(value["check_proof_hook_failures"])

        stream = io.StringIO()
        original = KeyboardInterrupt("private-primary-cancellation")
        with redirect_stderr(stream), self.assertRaises(KeyboardInterrupt) as caught:
            with diagnostic.lifecycle("review") as current:
                del current._check_proof_failure_mask
                current.scope["oversized"] = "x" * 5000
                raise original
        self.assertIs(caught.exception, original)
        value = outcome(self, stream.getvalue())
        self.assertEqual(value["exit_code"], 130)
        self.assertIsNone(value["check_proof_hook_failures"])
        self.assertNotIn("private-primary-cancellation", stream.getvalue())

        def unreadable(_self):
            raise SystemExit("private-unreadable-state")

        current = diagnostic.LifecycleDiagnostics("review")
        with patch.object(
            diagnostic.LifecycleDiagnostics,
            "_check_proof_failure_mask",
            property(unreadable),
            create=True,
        ):
            self.assertIsNone(current._check_proof_failures())


if __name__ == "__main__":
    unittest.main()
