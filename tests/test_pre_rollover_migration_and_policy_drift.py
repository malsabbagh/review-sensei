"""Pre-rollover holds for legacy inventory migration and enrolled policy/config drift."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import review_run
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_action_reply_entry import ReplyEntryFixture
from test_action_run import CommandEntryFixture
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import EntryFixture, fixture_process_environment

from review_sensei.cli import _parser as engine_parser
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ProviderError


class _PreRolloverFixture(CommandEntryFixture):
    def external(self, request, timeout):
        response = super().external(request, timeout)
        path = urlsplit(request.full_url).path
        if (
            not self.command_active
            and "/actions/runs/" in path
            and path.endswith("/jobs")
        ):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
        return response


class _LegacyReplyFixture(ReplyEntryFixture):
    def external(self, request, timeout):
        response = super().external(request, timeout)
        path = urlsplit(request.full_url).path
        if "/actions/runs/" in path and path.endswith("/jobs"):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
        return response


def _commit_config(fixture: EntryFixture, config_bytes: bytes) -> None:
    fixture.config_bytes = config_bytes
    (fixture.root / ".reviewsensei.yml").write_bytes(config_bytes)
    subprocess.run(
        [
            "git",
            "-C",
            str(fixture.root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "user.useConfigOnly=true",
            "commit",
            "-am",
            "update trusted policy config",
        ],
        check=True,
        capture_output=True,
        env=dict(
            fixture_process_environment(),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
        ),
    )
    fixture.policy_ref = fixture.policy = subprocess.check_output(
        ["git", "-C", str(fixture.root), "rev-parse", "HEAD"],
        text=True,
    ).strip()


class PreRolloverMigrationAndPolicyDriftTests(unittest.TestCase):
    def test_ordinary_terminal_legacy_pr_holds_before_rollover_and_preserves_commands(
        self,
    ) -> None:
        for human in (False, True):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory:
                f = _PreRolloverFixture(directory)
                f.legacy_publication_fixture = True
                self.assertEqual(f.invoke(human=human), 0, f.error)

                previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNone(previous.finding_lifecycle)
                self.assertEqual(previous.completed_initial_reviews, 1)
                self.assertIsNotNone(previous.transaction)
                assert previous.transaction is not None
                self.assertEqual(
                    previous.api_run_accounting["terminal_transaction"],
                    previous.transaction.transaction_id,
                )

                previous_body = f.comment["body"]
                previous_extra = copy.deepcopy(f.extra_comments)
                previous_provider_calls = f.provider_calls
                previous_reviews = len(f.reviews)
                previous_run_id = previous.api_run_accounting["identity"]["run_id"]

                f.legacy_publication_fixture = False
                f.next_run()
                f.live_head = f.requested_head = "e" * 40
                self.assertEqual(f.invoke(human=human), 1, f.error)

                outcome = json.loads((f.root / "publication.json").read_text())
                self.assertEqual(
                    outcome,
                    {
                        "status": "handoff",
                        "publication_status": "handoff",
                        "accounting_terminal": False,
                        "terminal_reason": "finding_inventory_migration_required",
                        "review_id": None,
                        "diagnostic": "durable_finding_inventory_recovery_required",
                    },
                )
                self.assertFalse(
                    any(
                        method in {"POST", "PATCH"} and "/issues" in path
                        for _, method, path in f.trace
                    )
                )
                self.assertEqual(f.extra_comments, previous_extra)
                self.assertEqual(f.provider_calls, previous_provider_calls)
                self.assertEqual(len(f.reviews), previous_reviews)
                self.assertEqual(f.comment["body"], previous_body)
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(current.to_dict(), previous.to_dict())
                self.assertEqual(
                    current.api_run_accounting["terminal_transaction"],
                    previous.transaction.transaction_id,
                )

                f.past_jobs[previous_run_id] = "hosted"
                f.SOURCE_ID += 1
                f.select_command("@reviewsensei review status", new_run=True)
                self.assertEqual(
                    f.invoke_command(), 0, (f.error, f.stdout, f.exception)
                )
                status_record = parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                )
                self.assertEqual(
                    status_record.api_run_accounting["action"]["phase"], "terminal"
                )
                self.assertEqual(f.provider_calls, previous_provider_calls)

                f.SOURCE_ID += 1
                f.live_head = f.requested_head = HEAD
                f.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(
                    f.invoke_command(), 0, (f.error, f.stdout, f.exception)
                )
                verified_record = parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                )
                self.assertEqual(
                    verified_record.api_run_accounting["action"]["phase"],
                    "terminal",
                )
                self.assertEqual(f.provider_calls, previous_provider_calls)

    def test_enrolled_pr_config_drift_holds_before_rollover_and_inference(self) -> None:
        drift_cases = (
            (
                "model",
                lambda raw: raw.replace(
                    b"model: deepseek-v4.1-flash:cloud", b"model: qwen3.5:cloud"
                ),
            ),
            (
                "review_quality",
                lambda raw: raw + b"review:\n  quality: high-signal\n",
            ),
            (
                "backend_openrouter",
                lambda raw: raw.replace(
                    b"backend: cloud-ollama\n  model: deepseek-v4.1-flash:cloud",
                    b"backend: openrouter\n  model: deepseek/deepseek-v4.1-flash",
                ),
            ),
            (
                "review_work_unified",
                lambda raw: raw + b"advanced:\n  review_work:\n    mode: unified\n",
            ),
        )
        for name, mutate_config in drift_cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                f = _PreRolloverFixture(directory)
                self.assertEqual(f.invoke(human=True), 0, f.error)

                previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNotNone(previous.finding_lifecycle)
                self.assertIsNotNone(previous.transaction)
                assert previous.transaction is not None
                self.assertEqual(previous.transaction.phase, "publication_succeeded")
                self.assertEqual(
                    previous.api_run_accounting["terminal_transaction"],
                    previous.transaction.transaction_id,
                )

                previous_body = f.comment["body"]
                previous_extra = copy.deepcopy(f.extra_comments)
                previous_provider_calls = f.provider_calls
                previous_reviews = len(f.reviews)
                previous_run_id = previous.api_run_accounting["identity"]["run_id"]

                _commit_config(f, mutate_config(f.config_bytes))
                f.next_run()
                f.live_head = f.requested_head = "e" * 40
                self.assertEqual(f.invoke(human=True), 1, f.error)

                outcome = json.loads((f.root / "publication.json").read_text())
                self.assertEqual(
                    outcome,
                    {
                        "status": "handoff",
                        "publication_status": "handoff",
                        "accounting_terminal": False,
                        "terminal_reason": "finding_inventory_policy_drift",
                        "review_id": None,
                        "diagnostic": "durable_finding_inventory_recovery_required",
                    },
                )
                self.assertFalse(
                    any(
                        method in {"POST", "PATCH"} and "/issues" in path
                        for _, method, path in f.trace
                    )
                )
                self.assertEqual(f.extra_comments, previous_extra)
                self.assertEqual(f.provider_calls, previous_provider_calls)
                self.assertEqual(len(f.reviews), previous_reviews)
                self.assertEqual(f.comment["body"], previous_body)
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(current.to_dict(), previous.to_dict())
                self.assertEqual(
                    current.api_run_accounting["terminal_transaction"],
                    previous.transaction.transaction_id,
                )

                f.past_jobs[previous_run_id] = "hosted"
                f.SOURCE_ID += 1
                f.select_command("@reviewsensei review status", new_run=True)
                self.assertEqual(
                    f.invoke_command(), 0, (f.error, f.stdout, f.exception)
                )
                status_record = parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                )
                self.assertEqual(
                    status_record.api_run_accounting["action"]["phase"], "terminal"
                )
                self.assertEqual(f.provider_calls, previous_provider_calls)

    def test_enrolled_pr_review_mode_cli_drift_holds_before_rollover(self) -> None:
        from review_sensei_github import cli as package_cli

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(previous.finding_lifecycle)
            previous_body = f.comment["body"]
            previous_provider_calls = f.provider_calls

            f.next_run()
            f.live_head = f.requested_head = "e" * 40
            original_main = package_cli.main

            def strict_main(argv):
                updated = [
                    "strict" if index > 0 and argv[index - 1] == "--review-mode" else v
                    for index, v in enumerate(argv)
                ]
                return original_main(updated)

            with patch.object(package_cli, "main", side_effect=strict_main):
                self.assertEqual(f.invoke(human=True), 1, f.error)

            outcome = json.loads((f.root / "publication.json").read_text())
            self.assertEqual(
                outcome,
                {
                    "status": "handoff",
                    "publication_status": "handoff",
                    "accounting_terminal": False,
                    "terminal_reason": "finding_inventory_policy_drift",
                    "review_id": None,
                    "diagnostic": "durable_finding_inventory_recovery_required",
                },
            )
            self.assertFalse(
                any(
                    method in {"POST", "PATCH"} and "/issues" in path
                    for _, method, path in f.trace
                )
            )
            self.assertEqual(f.provider_calls, previous_provider_calls)
            self.assertEqual(f.comment["body"], previous_body)

    def test_enrolled_pr_unchanged_policy_and_config_proceeds_normally(self) -> None:
        cases = (
            ("default", b"", False),
            ("high_signal_after_verify", b"review:\n  quality: high-signal\n", True),
            (
                "unified_work_mode",
                b"advanced:\n  review_work:\n    mode: unified\n",
                False,
            ),
        )
        for name, extra_yaml, run_verify in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                f = _PreRolloverFixture(directory)
                if extra_yaml:
                    _commit_config(f, f.config_bytes + extra_yaml)
                self.assertEqual(f.invoke(human=False), 0, f.error)
                first = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNotNone(first.finding_lifecycle)
                self.assertEqual(f.provider_calls, 1)

                if run_verify:
                    f.past_jobs[first.api_run_accounting["identity"]["run_id"]] = (
                        "hosted"
                    )
                    f.SOURCE_ID += 1
                    f.select_command("@reviewsensei verify", new_run=True)
                    self.assertEqual(
                        f.invoke_command(), 0, (f.error, f.stdout, f.exception)
                    )
                    verified = parse_session_comment(
                        f.comment["body"], identity=IDENTITY
                    )
                    self.assertEqual(
                        verified.api_run_accounting["action"]["phase"], "terminal"
                    )
                    f.past_jobs[f.run_id] = "command"

                f.next_run()
                f.command_active = False
                f.owner_job = "hosted"
                f.event_name = "pull_request"
                f.live_head = f.requested_head = "e" * 40
                self.assertEqual(f.invoke(human=False), 0, f.error)

                outcome = json.loads((f.root / "publication.json").read_text())
                self.assertEqual(outcome["status"], "published")
                self.assertTrue(outcome["accounting_terminal"])
                self.assertIsNone(outcome["terminal_reason"])
                self.assertEqual(f.provider_calls, 2)

                second = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNotNone(second.finding_lifecycle)
                self.assertIsNotNone(second.transaction)
                assert second.transaction is not None
                self.assertEqual(second.transaction.phase, "publication_succeeded")
                self.assertEqual(
                    second.api_run_accounting["terminal_transaction"],
                    second.transaction.transaction_id,
                )

    def test_expected_transaction_digests_match_cli_on_custom_stages_and_modes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / ".reviewsensei.yml"
            config_path.write_text(
                "schema: 1\n"
                "inference:\n"
                "  backend: local-ollama\n"
                "  model: qwen3.5:4b\n"
                "review:\n"
                "  quality: balanced\n"
                "advanced:\n"
                "  review_work:\n"
                "    mode: unified\n",
                encoding="utf-8",
            )
            categories_dir = root / "categories"
            categories_dir.mkdir()
            (categories_dir / "security.json").write_text(
                json.dumps(
                    {
                        "id": "custom-security",
                        "title": "Custom Security",
                        "focus": ["Check authorization checks."],
                    }
                ),
                encoding="utf-8",
            )
            stages_dir = root / "stages"
            stages_dir.mkdir()
            (stages_dir / "01-triage.json").write_text(
                json.dumps(
                    {
                        "name": "custom-triage",
                        "outputs": ["summary", "comments"],
                        "category_ids": ["custom-security"],
                        "prompt_template": "Review {review_categories} for {diff}",
                    }
                ),
                encoding="utf-8",
            )
            diff_path = root / "review.patch"
            diff_path.write_text(
                "diff --git a/src/app.py b/src/app.py\n"
                "--- a/src/app.py\n"
                "+++ b/src/app.py\n"
                "@@ -1 +1,2 @@\n"
                " keep\n"
                "+change\n",
                encoding="utf-8",
                newline="\n",
            )
            (root / "response.json").write_text(
                json.dumps({"summary": "ok", "comments": []}),
                encoding="utf-8",
            )
            ledger_dir = root / "ledger"
            review_json = root / "review.json"
            cli_argv = [
                "--config",
                str(config_path),
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--model",
                "fixture-v1",
                "--fixture-response",
                str(root / "response.json"),
                "--repository",
                "owner/repo",
                "--pull-request",
                "1",
                "--base-sha",
                "a" * 40,
                "--head-sha",
                "b" * 40,
                "--review-mode",
                "strict",
                "--stages-dir",
                str(stages_dir),
                "--categories-dir",
                str(categories_dir),
                "--orchestrate-large-changes",
                "--session-ledger",
                str(ledger_dir),
                "--transaction",
                "--format",
                "json",
                "--output",
                str(review_json),
                "--no-learning-proposals",
            ]
            from review_sensei.cli import main as engine_main

            with patch.dict(os.environ, fixture_process_environment(), clear=True):
                self.assertEqual(engine_main(cli_argv), 0)
                tx = json.loads(review_json.read_text(encoding="utf-8"))["transaction"]
                parsed = engine_parser(include_hosted=True).parse_args(cli_argv)
                expected_policy, expected_config = (
                    review_run._expected_transaction_digests(
                        parsed, configuration_path=config_path
                    )
                )
            self.assertEqual(
                expected_policy,
                ReviewConvergencePolicy(mode="strict", quality="balanced").digest(),
            )
            self.assertEqual(expected_policy, tx["policy_digest"])
            self.assertEqual(expected_config, tx["configuration_digest"])

    def test_nonterminal_predecessor_is_not_masked_by_pre_rollover_hold(self) -> None:
        from review_sensei_github.session_ledger import render_session_comment

        # 1. Interrupted `publication_pending` predecessor (finding_lifecycle is None)
        #    must still hit normal non-terminal rollover refusal, never migration hold.
        with tempfile.TemporaryDirectory() as directory:
            f = _PreRolloverFixture(directory)

            def cancel() -> None:
                raise KeyboardInterrupt()

            f.before_publication = cancel
            self.assertEqual(f.invoke(), 130, f.error)
            pending = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert pending is not None and pending.transaction is not None
            self.assertEqual(pending.transaction.phase, "publication_pending")
            self.assertIsNone(pending.finding_lifecycle)

            f.before_publication = None
            f.next_run()
            self.assertEqual(f.invoke(), 1)
            self.assertIn("previous caller run is not terminal", f.error)
            self.assertNotIn("finding_inventory_migration_required", f.error)

        # 2. Wedged `phase=no-transaction` predecessor with legacy counters
        #    (`finding_lifecycle is None`) must preserve `phase=no-transaction`.
        from review_sensei.errors import ProviderError

        with tempfile.TemporaryDirectory() as directory:
            f2 = _PreRolloverFixture(directory)

            def crash_in_provider() -> None:
                raise ProviderError("provider connection lost mid-call", transient=True)

            f2.before_provider = crash_in_provider
            self.assertEqual(f2.invoke(), 1)
            wedged = parse_session_comment(f2.comment["body"], identity=IDENTITY)
            assert wedged is not None
            self.assertIsNone(wedged.transaction)
            self.assertIsNone(wedged.api_run_accounting["analysis_abort"])
            legacy_wedged = wedged.evolve(
                generation=wedged.generation + 1,
                completed_initial_reviews=1,
                finding_lifecycle=None,
            )
            f2.comment["body"] = render_session_comment(
                repository_id=123,
                pull_request=7,
                record=legacy_wedged,
            )
            f2.before_provider = None
            f2.next_run()
            self.assertEqual(f2.invoke(), 1)
            self.assertIn("previous caller run is not terminal", f2.error)
            self.assertIn("phase=no-transaction", f2.error)
            self.assertNotIn("finding_inventory_migration_required", f2.error)

    def test_pre_write_settlement_candidate_with_lifecycle_holds_on_policy_drift(
        self,
    ) -> None:
        def crash_in_provider() -> None:
            raise ProviderError("provider connection lost mid-call", transient=True)

        with (
            tempfile.TemporaryDirectory() as failed_dir,
            tempfile.TemporaryDirectory() as legacy_dir,
        ):
            # 1. `pre_write_settlement_candidate` carrying a prior `finding_lifecycle`
            #    must refuse with `finding_inventory_policy_drift` pre-rollover when
            #    configuration/policy drifted.
            f = _PreRolloverFixture(failed_dir)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            run1_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(run1_record.finding_lifecycle)
            self.assertIsNotNone(run1_record.transaction)
            assert run1_record.transaction is not None
            failed_tx = replace(run1_record.transaction, phase="publication_failed")

            f.next_run()
            f.before_provider = crash_in_provider
            self.assertEqual(f.invoke(human=True), 1)
            wedged = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(wedged.finding_lifecycle)
            candidate = wedged.evolve(
                generation=wedged.generation + 1,
                transaction=failed_tx,
                reservation_id=None,
                reserved_slot=None,
            )
            f.comment["body"] = render_session_comment(
                repository_id=123,
                pull_request=7,
                record=candidate,
            )
            previous_body = f.comment["body"]
            previous_extra = copy.deepcopy(f.extra_comments)
            previous_provider_calls = f.provider_calls
            f.before_provider = None
            _commit_config(
                f,
                f.config_bytes.replace(
                    b"model: deepseek-v4.1-flash:cloud", b"model: qwen3.5:cloud"
                ),
            )
            f.next_run()
            f.live_head = f.requested_head = "e" * 40
            self.assertEqual(f.invoke(human=True), 1, f.error)
            outcome = json.loads((f.root / "publication.json").read_text())
            self.assertEqual(
                outcome,
                {
                    "status": "handoff",
                    "publication_status": "handoff",
                    "accounting_terminal": False,
                    "terminal_reason": "finding_inventory_policy_drift",
                    "review_id": None,
                    "diagnostic": "durable_finding_inventory_recovery_required",
                },
            )
            self.assertEqual(f.comment["body"], previous_body)
            self.assertEqual(f.extra_comments, previous_extra)
            self.assertEqual(f.provider_calls, previous_provider_calls)

            # 2. Control: `pre_write_settlement_candidate` with `finding_lifecycle=None`
            #    must NOT raise `finding_inventory_migration_required` (normal rollover
            #    proceeds to settlement / terminal refusal rather than migration hold).
            f_legacy = _PreRolloverFixture(legacy_dir)
            f_legacy.before_provider = crash_in_provider
            self.assertEqual(f_legacy.invoke(human=True), 1)
            legacy_wedged = parse_session_comment(
                f_legacy.comment["body"], identity=IDENTITY
            )
            legacy_candidate = legacy_wedged.evolve(
                generation=legacy_wedged.generation + 1,
                transaction=None,
                failed_attempts=1,
                failed_attempts_head_sha=HEAD,
                reservation_id=None,
                reserved_slot=None,
                finding_lifecycle=None,
            )
            f_legacy.comment["body"] = render_session_comment(
                repository_id=123,
                pull_request=7,
                record=legacy_candidate,
            )
            f_legacy.before_provider = None
            f_legacy.next_run()
            f_legacy.live_head = f_legacy.requested_head = "e" * 40
            code = f_legacy.invoke(human=True)
            self.assertNotIn("finding_inventory_migration_required", f_legacy.error)
            if (f_legacy.root / "publication.json").exists():
                pub = json.loads((f_legacy.root / "publication.json").read_text())
                self.assertNotEqual(
                    pub.get("terminal_reason"),
                    "finding_inventory_migration_required",
                    (code, pub),
                )

    def test_review_run_execute_retained_none_holds_on_policy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            f = _PreRolloverFixture(directory)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            previous_provider_calls = f.provider_calls

            _commit_config(f, f.config_bytes + b"review:\n  quality: high-signal\n")
            f.next_run()
            f.live_head = f.requested_head = "e" * 40

            original_bootstrap = review_run.bootstrap

            def bypass_pre_rollover_check(*args, **kwargs):
                def _direct_rollover(
                    caller,
                    native,
                    identity,
                    frozen,
                    owner,
                    previous_root,
                    require_cessation,
                ):
                    caller.rollover_and_activate(
                        native,
                        identity,
                        frozen,
                        owner,
                        previous_root,
                        require_cessation,
                    )

                kwargs["rollover_service"] = _direct_rollover
                return original_bootstrap(*args, **kwargs)

            with patch.object(
                review_run, "bootstrap", side_effect=bypass_pre_rollover_check
            ):
                self.assertEqual(f.invoke(human=True), 1, f.error)

            outcome = json.loads((f.root / "publication.json").read_text())
            self.assertEqual(
                outcome["terminal_reason"], "finding_inventory_policy_drift"
            )
            self.assertEqual(f.provider_calls, previous_provider_calls)

    def test_legacy_pr_reply_run_blocked_pre_rollover_without_stranding(self) -> None:
        def _copy_state(src: EntryFixture, dst: EntryFixture) -> None:
            for attr in (
                "comment",
                "extra_comments",
                "reviews",
                "inline",
                "threads",
                "check_runs",
            ):
                setattr(dst, attr, copy.deepcopy(getattr(src, attr)))
            dst.root = dst.directory = src.root
            dst.policy = src.policy
            dst.policy_ref = src.policy_ref
            dst.config_bytes = src.config_bytes
            dst.run_id = src.run_id
            dst.live_head = src.live_head
            dst.requested_head = src.requested_head
            dst.past_jobs = copy.deepcopy(src.past_jobs)

        with tempfile.TemporaryDirectory() as directory:
            rev = _PreRolloverFixture(directory)
            rev.legacy_publication_fixture = True
            self.assertEqual(rev.invoke(human=True), 0, rev.error)
            previous = parse_session_comment(rev.comment["body"], identity=IDENTITY)
            self.assertIsNone(previous.finding_lifecycle)
            self.assertEqual(previous.completed_initial_reviews, 1)

            previous_body = rev.comment["body"]
            previous_extra = copy.deepcopy(rev.extra_comments)
            previous_reviews = copy.deepcopy(rev.reviews)
            previous_provider_calls = rev.provider_calls
            previous_run_id = previous.api_run_accounting["identity"]["run_id"]

            # 1. Pre-rollover hold blocks legacy `reply-run` before rollover/reactions/reply
            #    so the legacy PR's session comment is never stranded.
            with tempfile.TemporaryDirectory() as reply_dir:
                f = _LegacyReplyFixture(reply_dir)
                _copy_state(rev, f)
                f.legacy_publication_fixture = False
                f.next_run()
                f.prepare_reply(source_id=41)
                f.extra_comments[41] = f.source
                self.assertEqual(f.invoke_reply(), 1)
                self.assertIn("finding_inventory_migration_required", f.error)
                self.assertEqual(f.comment["body"], previous_body)
                self.assertEqual(f.extra_comments, {**previous_extra, 41: f.source})
                self.assertEqual(f.reviews, previous_reviews)
                self.assertEqual(f.reactions, [])
                self.assertEqual(f.reply_posts, 0)
                self.assertEqual(f.provider_calls, 0)

            # Legacy `@reviewsensei review status` and `@reviewsensei verify` still work
            # afterwards (confirming the legacy PR was not stranded).
            rev.legacy_publication_fixture = False
            rev.past_jobs[previous_run_id] = "hosted"
            rev.SOURCE_ID += 1
            rev.live_head = rev.requested_head = HEAD
            rev.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(rev.invoke_command(), 0, rev.error + rev.stdout)
            rev.SOURCE_ID += 1
            rev.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(rev.invoke_command(), 0, rev.error + rev.stdout)
            self.assertEqual(
                parse_session_comment(
                    rev.comment["body"], identity=IDENTITY
                ).api_run_accounting["action"]["phase"],
                "terminal",
            )
            self.assertEqual(rev.provider_calls, previous_provider_calls)

            # 2. Direct defense-in-depth check in `action_reply.execute_reply` BEFORE
            #    `caller.admit_action` / `caller.begin_action_effect`.
            with (
                tempfile.TemporaryDirectory() as d_rev,
                tempfile.TemporaryDirectory() as d_rep,
            ):
                rev2 = _PreRolloverFixture(d_rev)
                rev2.legacy_publication_fixture = True
                self.assertEqual(rev2.invoke(human=True), 0, rev2.error)
                f2 = _LegacyReplyFixture(d_rep)
                _copy_state(rev2, f2)
                f2.legacy_publication_fixture = False
                f2.next_run()
                f2.prepare_reply(source_id=42)
                f2.extra_comments[42] = f2.source
                with patch.object(
                    review_run.ReviewRun,
                    "_record_requires_finding_inventory_migration",
                    side_effect=[False, True],
                ):
                    self.assertNotEqual(f2.invoke_reply(), 0)
                self.assertIn("durable_finding_inventory_recovery_required", f2.error)
                self.assertIsNone(f2.action())
                self.assertEqual(f2.reply_posts, 0)
                self.assertEqual(f2.reactions, [])

    def test_maintainer_commands_block_on_policy_drift_while_status_stays_accessible(
        self,
    ) -> None:
        command_specs = (
            ("verify", lambda _ref: "@reviewsensei verify"),
            ("override", lambda ref: f"@reviewsensei override {ref} acceptable risk"),
            (
                "accept_coverage_uncertainty",
                lambda _ref: (
                    "@reviewsensei accept-coverage-uncertainty --reason audited manual review"
                ),
            ),
        )
        for label, make_body in command_specs:
            with (
                self.subTest(command=label),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = _PreRolloverFixture(directory)
                self.assertEqual(f.invoke(human=True), 0, f.error)
                first = parse_session_comment(f.comment["body"], identity=IDENTITY)
                index = parse_finding_index(
                    f.reviews[-1]["body"],
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                )
                ref = "RS-" + index.entries[0].instance[:6].upper()
                previous_reviews = copy.deepcopy(f.reviews)
                previous_resolved = set(f.resolved_threads)
                previous_provider_calls = f.provider_calls
                previous_run_id = first.api_run_accounting["identity"]["run_id"]

                _commit_config(f, f.config_bytes + b"review:\n  quality: high-signal\n")
                f.past_jobs[previous_run_id] = "hosted"

                body = make_body(ref)
                f.SOURCE_ID += 1
                f.select_command(body, new_run=True)
                self.assertEqual(
                    f.invoke_command(), 0, (f.error, f.stdout, f.exception)
                )
                self.assertIn(
                    "durable_finding_inventory_policy_drift",
                    f.stdout,
                    (body, f.stdout),
                )
                rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(rec.dispositions, ())
                self.assertEqual(f.reviews, previous_reviews)
                self.assertEqual(f.resolved_threads, previous_resolved)
                self.assertEqual(f.provider_calls, previous_provider_calls)

                # Read-only `@reviewsensei review status` remains accessible on policy drift.
                f.SOURCE_ID += 1
                f.select_command("@reviewsensei review status", new_run=True)
                self.assertEqual(
                    f.invoke_command(), 0, (f.error, f.stdout, f.exception)
                )
                self.assertNotIn("durable_finding_inventory_policy_drift", f.stdout)

        # Direct `_refinalize_command` check when `policy_digest` disagrees with
        # enrolled `finding_lifecycle["manifest"]["binding"]["policy_digest"]`.
        with tempfile.TemporaryDirectory() as directory:
            f = _PreRolloverFixture(directory)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            first = parse_session_comment(f.comment["body"], identity=IDENTITY)

            from review_sensei_github.application import GitHubApplication
            from review_sensei_github.publication import ReviewPublisher

            from review_sensei.disposition import MaintainerCommandResult

            refinalize_calls = 0

            def unexpected_refinalize(**_kwargs):
                nonlocal refinalize_calls
                refinalize_calls += 1
                raise AssertionError("refinalize_with_dispositions must not run")

            class _FakePublisher(ReviewPublisher):
                def __init__(self):
                    self.finalizer = SimpleNamespace(
                        refinalize_with_dispositions=unexpected_refinalize
                    )

            class _DummyCaller:
                pass

            fake_ledger = SimpleNamespace(
                enable_finding_lifecycle_writes=True,
                load=lambda _identity: SimpleNamespace(record=first),
                caller=_DummyCaller(),
            )
            fake_broker = SimpleNamespace(exchange=lambda *_a, **_k: "tok")
            app = GitHubApplication(
                broker=fake_broker,
                http=None,
                reviewer=_FakePublisher(),
                learner=None,
                replier=None,
            )
            with patch(
                "review_sensei_github.finding_lifecycle.restore_finding_lifecycle",
                return_value=(first, {"mode": "active-window", "obligations": []}),
            ):
                blocked = app._refinalize_command(
                    MaintainerCommandResult(
                        action="verify",
                        applied=True,
                        operator_paused=False,
                        summary="verify",
                    ),
                    oidc_token="oidc",
                    repository="owner/repo",
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                    app_slug="reviewsensei[bot]",
                    actor="alice",
                    record=first,
                    ledger=fake_ledger,
                    policy_digest="0" * 64,
                )
            self.assertTrue(blocked.evaluation_failed)
            self.assertIn("gate re-evaluation failed", blocked.summary)
            self.assertEqual(refinalize_calls, 0)

    def test_historical_receipt_omitted_policy_and_standalone_finalizer_helpers(
        self,
    ) -> None:
        from review_sensei_github.application import (
            GitHubApplication,
            GitHubWriteOptions,
        )
        from review_sensei_github.finding_lifecycle import (
            FindingLifecycleJournal,
            restore_finding_lifecycle,
        )
        from review_sensei_github.publication import (
            PublicationResult,
            ReviewApprovalFinalizer,
            ReviewPublisher,
        )

        from review_sensei.disposition import MaintainerCommandResult, OverrideReceipt
        from review_sensei.session import LocalSessionLedger

        with tempfile.TemporaryDirectory() as directory:
            f = _PreRolloverFixture(directory)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            first = parse_session_comment(f.comment["body"], identity=IDENTITY)
            pub = ReviewPublisher(http=f.selected.http)
            _, real_doc = restore_finding_lifecycle(pub, f.selected.ledger, IDENTITY)

            refinalize_calls = 0

            def ok_refinalize(**_kwargs):
                nonlocal refinalize_calls
                refinalize_calls += 1
                return PublicationResult(
                    status="approval_withheld", diagnostic="approval_withheld"
                )

            class _FakePublisher(ReviewPublisher):
                def __init__(self):
                    super().__init__(http=None)
                    self._active_window_states = dict(pub._active_window_states)
                    self._drift_states = dict(pub._drift_states)
                    self.finalizer = SimpleNamespace(
                        http=None,
                        refinalize_with_dispositions=ok_refinalize,
                    )

            class _DummyCaller:
                pass

            fake_ledger = SimpleNamespace(
                enable_finding_lifecycle_writes=True,
                load=lambda _identity: SimpleNamespace(record=first),
                caller=_DummyCaller(),
            )
            fake_broker = SimpleNamespace(exchange=lambda *_a, **_k: "tok")
            app = GitHubApplication(
                broker=fake_broker,
                http=None,
                reviewer=_FakePublisher(),
                learner=None,
                replier=None,
            )
            with (
                patch(
                    "review_sensei_github.finding_lifecycle.restore_finding_lifecycle",
                    return_value=(first, real_doc),
                ),
                patch.object(FindingLifecycleJournal, "finish", return_value=None),
            ):
                historical_res = app._refinalize_command(
                    MaintainerCommandResult(
                        action="dismiss",
                        applied=True,
                        operator_paused=False,
                        summary="Historical assessment dismissed",
                        receipts=(
                            OverrideReceipt(
                                "historical:scope/RS-ABCDEF",
                                "abcdef",
                                "recorded",
                                "dismiss",
                            ),
                        ),
                    ),
                    oidc_token="oidc",
                    repository="owner/repo",
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                    app_slug="reviewsensei[bot]",
                    actor="alice",
                    record=first,
                    ledger=fake_ledger,
                    policy_digest="0" * 64,
                )
            self.assertFalse(historical_res.evaluation_failed, historical_res.summary)
            self.assertEqual(refinalize_calls, 1)

            strict_lifecycle = copy.deepcopy(first.finding_lifecycle)
            strict_lifecycle["manifest"]["binding"]["policy_digest"] = (
                ReviewConvergencePolicy(mode="strict").digest()
            )
            strict_record = SimpleNamespace(
                operator_paused=False,
                quarantine_lineage=None,
                finding_lifecycle=strict_lifecycle,
            )
            local_ledger = LocalSessionLedger(Path(directory) / "local-ledger")
            local_ledger.initialize(IDENTITY)
            app.session_ledger = local_ledger
            with (
                patch(
                    "review_sensei_github.finding_lifecycle.restore_finding_lifecycle",
                    return_value=(strict_record, None),
                ),
                patch.object(
                    ReviewPublisher, "check_trust_boundary", return_value=None
                ),
            ):
                status_without_explicit_policy = app.apply_maintainer_command(
                    options=GitHubWriteOptions(
                        github_writes=False, github_session_ledger=False
                    ),
                    oidc_token=None,
                    repository="owner/repo",
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                    body="@reviewsensei verify",
                    actor_login="alice",
                    association="OWNER",
                    app_slug="reviewsensei[bot]",
                    convergence_policy=None,
                )
            self.assertNotIn(
                "durable_finding_inventory_policy_drift",
                status_without_explicit_policy.summary,
            )

        standalone_finalizer = ReviewApprovalFinalizer.__new__(ReviewApprovalFinalizer)
        self.assertEqual(
            standalone_finalizer._held_cleanup_instances(
                repository_id=123, pull_request=7, head_sha=HEAD
            ),
            frozenset(),
        )


if __name__ == "__main__":
    unittest.main()
