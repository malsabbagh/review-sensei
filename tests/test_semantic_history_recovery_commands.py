"""Recovery disposition commands break semantic no-progress deadlocks safely."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import cli as package_cli
from review_sensei_github import review_run
from review_sensei_github.accepted_semantic_history import LIMIT
from review_sensei_github.api_run_history import read_action_result
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import parse_session_comment
from test_action_run import CommandEntryFixture
from test_api_work_lifecycle import forbid_replacement_phase_budget
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import fixture_process_environment

_FIELDS = (
    "comment",
    "extra_comments",
    "reviews",
    "inline",
    "check_runs",
    "threads",
    "run_id",
    "owner_job",
    "past_jobs",
    "provider_calls",
    "SOURCE_ID",
)


class RecoveryEntryFixture(CommandEntryFixture):
    """CommandEntryFixture extended with real reply-run transport support."""

    def __init__(self, directory, *, limit=512):
        super().__init__(directory, limit=limit)
        self.active_purpose = "command"

    def _expected_authority(self):
        authority = super()._expected_authority()
        if self.active_purpose == "reply":
            authority["purpose"] = "reply"
            authority["job"] = "hosted"
        return authority

    def invoke_reply(self, body="@reviewsensei explain this finding"):
        self.SOURCE_ID += 1
        self.past_jobs[self.run_id] = self.owner_job
        self.run_id += 1
        self.trace.clear()
        self.transports.clear()
        self.worker_observations.clear()
        self.command_active = True
        self.active_purpose = "reply"
        self.owner_job = "hosted"
        self.event_name = "issue_comment"
        self.command_body = body
        self.source = {
            "id": self.SOURCE_ID,
            "body": body,
            "updated_at": self.SOURCE_STAMP,
            "created_at": self.SOURCE_STAMP,
            "user": {"login": "alice", "type": "User", "id": 42},
            "author_association": "OWNER",
            "issue_url": f"{self.api_url}/repos/owner/repo/issues/7",
        }
        event = {
            "issue": {"number": 7},
            "comment": copy.deepcopy(self.source),
            "repository": {"id": 123, "full_name": "owner/repo"},
            "sender": copy.deepcopy(self.source["user"]),
        }
        event_file = self.root / "reply-event.json"
        event_file.write_text(json.dumps(event))

        def caller_factory(**kwargs):
            caller = ApiRunRequestTransport(**kwargs)
            self.accounted = caller
            self.transports.append(caller)
            return caller

        def broker_factory(**kwargs):
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token", **kwargs
            )

        def http_factory(**kwargs):
            http = GitHubHttp(api_url=self.api_url, **kwargs)
            self.http = http
            return http

        environment = {
            **fixture_process_environment(),
            "GITHUB_RUN_ID": str(self.run_id),
            "GITHUB_RUN_ATTEMPT": str(self.attempt),
            "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_EVENT_PATH": str(event_file),
            "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
            "GITHUB_ACTOR": "alice",
            "GITHUB_TRIGGERING_ACTOR": "alice",
            "GITHUB_TOKEN": "synthetic-workflow-read",
            "OLLAMA_API_KEY": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
        }
        output, errors = io.StringIO(), io.StringIO()
        previous_directory = Path.cwd()
        self.exception = None
        try:
            os.chdir(self.root)
            with ExitStack() as stack:
                for scope in (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(review_run, "physical_opener", self.external),
                    patch.object(
                        review_run, "ApiRunRequestTransport", side_effect=caller_factory
                    ),
                    patch.object(
                        review_run, "BrokerClient", side_effect=broker_factory
                    ),
                    patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                    forbid_replacement_phase_budget(),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    stack.enter_context(scope)
                return package_cli.main(
                    [
                        "reply-run",
                        "--policy-commit",
                        self.policy,
                        "--run-directory",
                        str(self.root),
                        "--",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "123",
                        "--pull-request",
                        "7",
                        "--head-sha",
                        self.live_head,
                        "--source-comment-id",
                        str(self.SOURCE_ID),
                        "--source-updated-at",
                        self.SOURCE_STAMP,
                        "--source-kind",
                        "issue",
                        "--allow-write",
                        "--enable-reply",
                        "--generate",
                        "--config",
                        str(self.root / ".reviewsensei.yml"),
                        "--provider",
                        "cloud-ollama",
                        "--model",
                        "deepseek-v4.1-flash:cloud",
                        "--base-url",
                        "https://ollama.com/api",
                    ]
                )
        except Exception as error:
            import traceback

            self.exception = error
            errors.write(traceback.format_exc())
            return 2
        finally:
            self.active_purpose = "command"
            os.chdir(previous_directory)
            self.stdout, self.error = output.getvalue(), errors.getvalue()


def _seed_two_status_streak(directory, *, partial=False, human=True):
    f = CommandEntryFixture(directory, limit=512)
    if partial:
        patch_file = f.root / "review.patch"
        patch_file.write_text(
            patch_file.read_text(encoding="utf-8")
            + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\n"
            "index 0000000..1111111\nBinary files /dev/null and b/image.bin differ\n",
            encoding="utf-8",
            newline="\n",
        )
    assert f.invoke(human=human) == 0, f.error
    for n in range(LIMIT):
        f.SOURCE_ID = 41 + n
        f.select_command("@reviewsensei review status", new_run=True)
        assert f.invoke_command() == 0, f.error
    record = parse_session_comment(f.comment["body"], identity=IDENTITY)
    history = read_action_result(f.selected.ledger, IDENTITY, record)["result"][
        "accepted_semantic_history"
    ]
    assert history["streak"] == LIMIT
    return f, record, history


def _subprocess_phase(root: Path, step: str) -> None:
    child = root / step
    child.mkdir()
    f = CommandEntryFixture(child, limit=512)
    native = root / "native.json"
    if step != "review":
        saved = json.loads(native.read_text())
        for key, value in saved.items():
            setattr(f, key, value)
        f.past_jobs = {int(key): value for key, value in f.past_jobs.items()}
        f.extra_comments = {int(key): value for key, value in f.extra_comments.items()}

    if step == "review":
        code = f.invoke(human=True)
    elif step in {"status_1", "status_2", "status_3"}:
        f.SOURCE_ID += 1
        f.select_command("@reviewsensei review status", new_run=True)
        code = f.invoke_command()
    elif step in {"verify_blocked", "verify_after"}:
        f.SOURCE_ID += 1
        f.select_command("@reviewsensei verify", new_run=True)
        code = f.invoke_command()
    elif step == "dismiss":
        index = parse_finding_index(
            f.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
        )
        assert index is not None
        instance = index.entries[0].instance
        f.SOURCE_ID += 1
        f.select_command(
            f'@reviewsensei dismiss {instance} --reason "Verified safe in audit."',
            new_run=True,
        )
        code = f.invoke_command()
    else:
        raise ValueError(f"unknown phase step: {step}")

    if code == 0 or step == "review":
        native.write_text(json.dumps({key: getattr(f, key) for key in _FIELDS}))

    record = parse_session_comment(f.comment["body"], identity=IDENTITY)
    payload = {
        "exit": code,
        "error": f.error,
        "stdout": f.stdout,
        "root_sha256": record.record_sha256,
        "reviews": len(f.reviews),
        "approved": sum(r["state"] == "APPROVED" for r in f.reviews),
        "provider_calls": f.provider_calls,
        "dispositions": len(record.dispositions),
    }
    if step != "review" and code == 0:
        payload["history"] = read_action_result(f.selected.ledger, IDENTITY, record)[
            "result"
        ]["accepted_semantic_history"]
    (root / f"{step}.json").write_text(json.dumps(payload))


class SemanticHistoryRecoveryCommandsTests(unittest.TestCase):
    def test_fresh_process_deadlock_recovery_allows_dismiss_then_verify_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(
                os.environ,
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(Path.cwd())
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
                GIT_AUTHOR_DATE="2026-11-13T00:00:00Z",
                GIT_COMMITTER_DATE="2026-11-13T00:00:00Z",
            )
            steps = (
                "review",
                "status_1",
                "status_2",
                "status_3",
                "verify_blocked",
                "dismiss",
                "verify_after",
            )
            results = {}
            for step in steps:
                call = subprocess.run(
                    [sys.executable, __file__, str(root), step],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(call.returncode, 0, call.stderr)
                results[step] = json.loads((root / f"{step}.json").read_text())

            self.assertEqual(results["review"]["exit"], 0, results["review"]["error"])
            self.assertEqual(results["review"]["approved"], 0)
            self.assertEqual(results["status_1"]["exit"], 0)
            self.assertEqual(results["status_1"]["history"]["streak"], 1)
            self.assertEqual(results["status_2"]["exit"], 0)
            self.assertEqual(results["status_2"]["history"]["streak"], 2)

            # 3rd status and 3rd verify remain blocked with unchanged root.
            for blocked in ("status_3", "verify_blocked"):
                self.assertEqual(results[blocked]["exit"], 1)
                self.assertIn(
                    "accepted_semantic_history_finite_no_progress_stop",
                    results[blocked]["error"],
                )
                self.assertEqual(
                    results[blocked]["root_sha256"],
                    results["status_2"]["root_sha256"],
                )
                self.assertEqual(results[blocked]["approved"], 0)

            # Dismiss recovers: clears the finding, resets streak to 0, allows verify.
            self.assertEqual(results["dismiss"]["exit"], 0, results["dismiss"]["error"])
            self.assertEqual(results["dismiss"]["dispositions"], 1)
            self.assertEqual(results["dismiss"]["history"]["streak"], 0)
            self.assertTrue(
                results["dismiss"]["history"]["records"][-1]["made_progress"]
            )
            self.assertEqual(results["dismiss"]["approved"], 1)
            self.assertEqual(
                results["verify_after"]["exit"], 0, results["verify_after"]["error"]
            )
            self.assertEqual(results["verify_after"]["approved"], 1)
            self.assertEqual(
                results["verify_after"]["provider_calls"],
                results["review"]["provider_calls"],
            )

    def test_non_advancing_routes_remain_blocked_including_prose_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, _ = _seed_two_status_streak(directory)
            before_root = record.record_sha256
            before_calls = f.provider_calls

            for command in (
                "@reviewsensei review status",
                "@reviewsensei verify",
                "@reviewsensei review pause",
            ):
                with self.subTest(command=command):
                    f.SOURCE_ID += 1
                    f.select_command(command, new_run=True)
                    self.assertEqual(f.invoke_command(), 1)
                    self.assertIn(
                        "accepted_semantic_history_finite_no_progress_stop", f.error
                    )
                    current = parse_session_comment(
                        f.comment["body"], identity=IDENTITY
                    )
                    self.assertEqual(current.record_sha256, before_root)

            # Prose reply is also still stopped on streak == LIMIT.
            with tempfile.TemporaryDirectory(dir=directory) as reply_dir:
                reply_fx = RecoveryEntryFixture(reply_dir)
                # Seed the same streak on RecoveryEntryFixture directly.
                assert reply_fx.invoke(human=True) == 0, reply_fx.error
                for n in range(LIMIT):
                    reply_fx.SOURCE_ID = 41 + n
                    reply_fx.select_command("@reviewsensei review status", new_run=True)
                    assert reply_fx.invoke_command() == 0, reply_fx.error
                reply_root = parse_session_comment(
                    reply_fx.comment["body"], identity=IDENTITY
                ).record_sha256
                reply_calls = reply_fx.provider_calls
                self.assertEqual(reply_fx.invoke_reply("@reviewsensei explain this"), 1)
                self.assertIn(
                    "accepted_semantic_history_finite_no_progress_stop", reply_fx.error
                )
                self.assertEqual(
                    parse_session_comment(
                        reply_fx.comment["body"], identity=IDENTITY
                    ).record_sha256,
                    reply_root,
                )
                self.assertEqual(reply_fx.provider_calls, reply_calls)
            self.assertEqual(f.provider_calls, before_calls)

    def test_accept_risk_and_override_reset_streak_and_allow_subsequent_verify(self):
        for style in ("accept-risk", "override"):
            with (
                self.subTest(style=style),
                tempfile.TemporaryDirectory() as directory,
            ):
                f, _, _ = _seed_two_status_streak(directory)
                index = parse_finding_index(
                    f.reviews[-1]["body"],
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                )
                assert index is not None
                instance = index.entries[0].instance
                if style == "accept-risk":
                    cmd = (
                        f"@reviewsensei accept-risk {instance} "
                        '--reason "Accepted risk with compensating guard."'
                    )
                else:
                    cmd = f"@reviewsensei override RS-{instance[:6].upper()}"
                f.SOURCE_ID += 1
                f.select_command(cmd, new_run=True)
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                after = parse_session_comment(f.comment["body"], identity=IDENTITY)
                history = read_action_result(f.selected.ledger, IDENTITY, after)[
                    "result"
                ]["accepted_semantic_history"]
                self.assertEqual(history["streak"], 0)
                self.assertTrue(history["records"][-1]["made_progress"])
                self.assertEqual(len(after.dispositions), 1)
                self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)

                f.SOURCE_ID += 1
                f.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))

    def test_accept_coverage_uncertainty_admitted_at_streak_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, _ = _seed_two_status_streak(directory, partial=True, human=False)

            f.SOURCE_ID += 1
            f.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(f.invoke_command(), 1)
            self.assertIn("accepted_semantic_history_finite_no_progress_stop", f.error)

            f.SOURCE_ID += 1
            f.select_command(
                '@reviewsensei accept-coverage-uncertainty --reason "Binary asset reviewed manually."',
                new_run=True,
            )
            self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
            after = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(len(after.dispositions), 1)
            self.assertEqual(
                after.dispositions[0]["action"], "accept-coverage-uncertainty"
            )
            retained = read_action_result(f.selected.ledger, IDENTITY, after)
            self.assertTrue(retained["result"]["applied"])
            self.assertEqual(
                after.api_run_accounting["action"]["outcome"],
                {"kind": "applied", "reason": None},
            )
            self.assertTrue(
                retained["result"]["accepted_semantic_history"]["records"][-1][
                    "disposition_entry"
                ]
            )

    def test_noop_disposition_command_keeps_streak_at_limit_and_still_blocks_verify(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f, _, _ = _seed_two_status_streak(directory)
            # An unpaused `review continue` makes no semantic progress.
            f.SOURCE_ID += 1
            f.select_command("@reviewsensei review continue", new_run=True)
            self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
            after = parse_session_comment(f.comment["body"], identity=IDENTITY)
            history = read_action_result(f.selected.ledger, IDENTITY, after)["result"][
                "accepted_semantic_history"
            ]
            self.assertEqual(history["streak"], LIMIT)
            self.assertFalse(history["records"][-1]["made_progress"])
            self.assertTrue(history["records"][-1]["disposition_entry"])

            f.SOURCE_ID += 1
            f.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(f.invoke_command(), 1)
            self.assertIn("accepted_semantic_history_finite_no_progress_stop", f.error)

    def test_repeated_noop_disposition_commands_halt_at_finite_limit_without_rollover(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f, _, _ = _seed_two_status_streak(directory)
            for expected_extra_entries in range(1, LIMIT + 1):
                f.SOURCE_ID += 1
                f.select_command("@reviewsensei review continue", new_run=True)
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                history = read_action_result(f.selected.ledger, IDENTITY, current)[
                    "result"
                ]["accepted_semantic_history"]
                self.assertEqual(history["streak"], LIMIT)
                self.assertFalse(history["records"][-1]["made_progress"])
                self.assertTrue(history["records"][-1]["disposition_entry"])
                self.assertEqual(
                    sum(
                        1
                        for item in history["records"]
                        if item.get("disposition_entry") is True
                    ),
                    expected_extra_entries,
                )

            bounded_root = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            ).record_sha256
            bounded_comments = len(f.extra_comments)
            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            assert index is not None
            instance = index.entries[0].instance
            for command in (
                "@reviewsensei review continue",
                f'@reviewsensei dismiss {instance} --reason "late recovery"',
            ):
                with self.subTest(command=command):
                    f.SOURCE_ID += 1
                    f.select_command(command, new_run=True)
                    self.assertEqual(f.invoke_command(), 1)
                    self.assertIn(
                        "accepted_semantic_history_finite_no_progress_stop", f.error
                    )
                    self.assertEqual(
                        parse_session_comment(
                            f.comment["body"], identity=IDENTITY
                        ).record_sha256,
                        bounded_root,
                    )
                    self.assertEqual(len(f.extra_comments), bounded_comments)

    def test_repeated_duplicate_accept_risk_halts_at_finite_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, _ = _seed_two_status_streak(directory)
            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            assert index is not None
            instance = index.entries[0].instance
            for expected_streak in (0, 1, 2):
                f.SOURCE_ID += 1
                f.select_command(
                    f'@reviewsensei accept-risk {instance} --reason "repeat attempt"',
                    new_run=True,
                )
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                history = read_action_result(f.selected.ledger, IDENTITY, current)[
                    "result"
                ]["accepted_semantic_history"]
                self.assertEqual(history["streak"], expected_streak)
                last_record = history["records"][-1]
                if expected_streak == 0:
                    self.assertTrue(last_record["made_progress"])
                    self.assertNotIn("disposition_entry", last_record)
                else:
                    self.assertFalse(last_record["made_progress"])
                    self.assertTrue(last_record["disposition_entry"])

            bounded_root = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            ).record_sha256
            bounded_comments = len(f.extra_comments)
            f.SOURCE_ID += 1
            f.select_command(
                f'@reviewsensei accept-risk {instance} --reason "4th attempt"',
                new_run=True,
            )
            self.assertEqual(f.invoke_command(), 1)
            self.assertIn("accepted_semantic_history_finite_no_progress_stop", f.error)
            self.assertEqual(
                parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).record_sha256,
                bounded_root,
            )
            self.assertEqual(len(f.extra_comments), bounded_comments)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        _subprocess_phase(Path(sys.argv[1]), sys.argv[2])
    else:
        unittest.main()
