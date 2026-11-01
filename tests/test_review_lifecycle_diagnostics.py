"""Public lifecycle diagnostics; all external services are synthetic."""

import io
import json
import os
import subprocess
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


if __name__ == "__main__":
    unittest.main()
