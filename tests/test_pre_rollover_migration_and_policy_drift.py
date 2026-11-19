"""Pre-rollover holds for legacy inventory migration and enrolled policy/config drift."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import review_run
from review_sensei_github.session_ledger import parse_session_comment
from test_action_run import CommandEntryFixture
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import EntryFixture, fixture_process_environment

from review_sensei.cli import _parser as engine_parser
from review_sensei.convergence import ReviewConvergencePolicy


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


if __name__ == "__main__":
    unittest.main()
